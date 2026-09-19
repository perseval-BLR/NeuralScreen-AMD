"""A dump in a bundle must say which image it came from, and the claim must match the filter.

Two failures this guards, both measured on real reports rather than imagined:

1. THREE DUMPS A DAY OLDER THAN THE LOG travelled in one bundle, and the image
   that faulted was one this installation does NOT ship. An offset was quoted
   from that dump and compared against our own image - a comparison that could
   not hold, because an RVA means different code in different builds. It had to
   be corrected publicly. The module's (size of image, time date stamp) is the
   fact that settles it, and it is read out of the minidump.

2. ``only_this_run: True`` was written UNCONDITIONALLY while the collectors
   applied no age filter whenever the cutoff came out at zero (unknown start
   time, or a process younger than the slack). A reader would take a previous
   session's crash for this run's - the exact failure the filter was added to
   end. The claim and the filter now come from one function.

Run:  runtime\\python.exe tests/test_amd_dump_identity.py
"""
from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import diagnostics  # noqa: E402


def build_dump(*, written: int, exception_code: int, parameter: int,
               module: str, size_of_image: int, timestamp: int,
               rva: int, base: int = 0x7FFC00000000) -> bytes:
    """A minimal but well-formed minidump carrying the four things we read.

    The header and its directory come FIRST and the offsets are computed from
    the real sizes, because the payload that follows depends on where it lands.
    Building the payload first and patching offsets afterwards is how the first
    version of this builder pointed the module list into empty bytes - it
    parsed to nothing and looked like a hole in the reader.
    """
    name = module.encode("utf-16-le")
    dir_entries = 2

    header_size = 32 + dir_entries * 12
    modules_at = header_size
    name_at = modules_at + 4 + 108
    exc_at = name_at + 4 + len(name)

    header = bytearray(header_size)
    header[0:4] = b"MDMP"
    struct.pack_into("<I", header, 8, dir_entries)
    struct.pack_into("<I", header, 12, 32)
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 20, written)
    struct.pack_into("<III", header, 32, 4, 0, modules_at)
    struct.pack_into("<III", header, 44, 6, 0, exc_at)

    entry = bytearray(108)
    struct.pack_into("<Q", entry, 0, base)
    struct.pack_into("<I", entry, 8, size_of_image)
    struct.pack_into("<I", entry, 12, 0)          # checksum
    struct.pack_into("<I", entry, 16, timestamp)
    struct.pack_into("<I", entry, 20, name_at)

    # MINIDUMP_EXCEPTION, to spec: the fixed part is 32 bytes and the parameter
    # array follows it. `ExceptionInformation[0]` is the number that matters -
    # for a fast fail (0xC0000409) it is the fail-fast code, and 7 is abort().
    # Writing the value into NumberParameters instead, which is where the first
    # version of this builder put it, makes the reader disagree with the format.
    exception = bytearray()
    exception += struct.pack("<II", 1, 0)                    # thread id, alignment
    exception += struct.pack("<IIQQ", exception_code, 0, 0, base + rva)
    exception += struct.pack("<I", 1)                        # number of parameters
    exception += struct.pack("<I", 0)                        # unused alignment
    info = [parameter] + [0] * 14                            # ExceptionInformation
    exception += struct.pack("<15Q", *info)

    out = bytearray()
    out += header
    out += struct.pack("<I", 1) + entry                      # module list
    out += struct.pack("<I", len(name)) + name               # its name
    out += exception
    return bytes(out)


#: The build vizo01's dumps faulted in - a runtime image this installation does
#: not ship. Real numbers from that package.
FOREIGN = dict(size_of_image=0x6DF000, timestamp=0x6A9C4984)
#: Ours, from the pinned v0.2.17 image actually in the archive.
OURS = dict(size_of_image=0x6F5000, timestamp=0x6A9E5826)


class DumpIdentityTests(unittest.TestCase):
    def test_reads_what_the_dump_says_about_itself(self) -> None:
        data = build_dump(written=1789768498, exception_code=0xC0000409,
                          parameter=7, module="dlssnr_amd_pass1.dll",
                          rva=0x33B75, **FOREIGN)
        info = diagnostics._minidump_identity(data)
        self.assertEqual(info["exception_code"], 0xC0000409)
        self.assertEqual(info["exception_parameter"], 7)
        self.assertEqual(info["faulted_module"], "dlssnr_amd_pass1.dll")
        self.assertEqual(info["faulted_module_size_of_image"], 0x6DF000)
        self.assertEqual(info["faulted_module_timestamp"], 0x6A9C4984)
        self.assertEqual(info["faulted_rva"], 0x33B75)
        self.assertEqual(info["written"], 1789768498)

    def test_a_foreign_image_is_flagged_and_an_ours_is_not(self) -> None:
        """The negative control: the SAME parser, told a true image, must not flag it.

        Without this, a guard that answered "foreign" to everything would look
        like it worked.
        """
        shipped = [
            {"name": "dlssnr_amd_pass1.dll", **OURS},
            {"name": "dlssnr_amd_pass1_v0310.dll",
             "size_of_image": 0x703000, "timestamp": 0x6AA71FC0},
        ]
        foreign = build_dump(written=1789768498, exception_code=0xC0000409,
                             parameter=7, module="dlssnr_amd_pass1.dll",
                             rva=0x33B75, **FOREIGN)
        ours = build_dump(written=1789768498, exception_code=0xC0000409,
                          parameter=7, module="dlssnr_amd_pass1.dll",
                          rva=0x3A885, **OURS)
        with mock.patch.object(diagnostics, "shipped_runtime_identities",
                               return_value=shipped):
            described = diagnostics.describe_crash_dumps(
                [("crashdump/1.dmp", foreign), ("crashdump/2.dmp", ours)])
        self.assertIs(described[0]["belongs_to_this_install"], False)
        self.assertIn("NOT one this installation ships", described[0]["note"])
        self.assertIs(described[1]["belongs_to_this_install"], True)
        self.assertNotIn("note", described[1])

    def test_an_unreadable_dump_does_not_break_the_bundle(self) -> None:
        """A dump Windows truncated is still shipped - just without a verdict."""
        described = diagnostics.describe_crash_dumps(
            [("crashdump/1.dmp", b"MDMP\x00\x01\x02")])
        self.assertEqual(described[0]["name"], "crashdump/1.dmp")
        self.assertNotIn("belongs_to_this_install", described[0])

    def test_the_claim_matches_the_filter_it_describes(self) -> None:
        """`only_this_run` must be false whenever the age filter could not run.

        Both ways the cutoff can come out at zero are checked, because the old
        code said True in both while collecting everything it could find.
        """
        with mock.patch.object(diagnostics, "process_start_time",
                               return_value=0.0):
            cutoff, why = diagnostics.dump_freshness_cutoff()
            self.assertEqual(cutoff, 0.0)
            self.assertIn("start time is unknown", why)

        with mock.patch.object(diagnostics, "process_start_time",
                               return_value=1.0):
            cutoff, why = diagnostics.dump_freshness_cutoff()
            self.assertEqual(cutoff, 0.0)
            self.assertIn("younger than the freshness slack", why)

        with mock.patch.object(diagnostics, "process_start_time",
                               return_value=1_789_000_000.0):
            cutoff, why = diagnostics.dump_freshness_cutoff()
            self.assertGreater(cutoff, 0.0)
            self.assertEqual(why, "")

    def test_the_collectors_and_the_report_use_the_same_cutoff(self) -> None:
        """One source of truth, checked by breaking the report's copy.

        The bug was a second, unconditional claim next to the filter. Counting
        call sites is what catches a re-introduction.
        """
        source = (ROOT / "diagnostics.py").read_text(encoding="utf-8")
        code = "\n".join(
            line.split("#")[0] for line in source.splitlines()
            if not line.lstrip().startswith("#"))
        # Calls only: the definition itself would otherwise be counted, which is
        # exactly the off-by-one that made this check fail on its first run.
        calls = [line for line in code.splitlines()
                 if "dump_freshness_cutoff()" in line and not line.lstrip().startswith("def ")]
        self.assertEqual(len(calls), 3,
                         "the cutoff must come from the one function: twice in "
                         "the collectors, once in the report")
        self.assertNotIn('"only_this_run": True', code,
                         "the claim must be computed, never written as a constant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
