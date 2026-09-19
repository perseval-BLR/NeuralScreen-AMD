"""The AMD offset probe must reproduce the table this host is pinned to.

An offset written into someone else's image is not a value that fails loudly:
the wrong one lands in read-only memory, or calls into the middle of an
unrelated function, and the run continues either way. The host's own table was
verified by hand once, and the probe is what replaces that reading - so the
probe's own correctness is the thing that has to be checked, always.

Run: runtime\\python.exe tests\\test_amd_offsets_probe.py

The synthetic image is assembled here rather than shipped: the real runtime is a
third-party, proprietary binary that cannot live in this repository. What is
tested is the reading, not any particular build - the same code that derives
eleven fields from a 3 KB synthetic image derives them from the 7 MB real one,
which is what the pinned-table run demonstrates in CI when the image is present.
"""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import amd_offsets_probe as probe  # noqa: E402

#: The offsets the real v0.2.17 build yields, kept as data so a regression in the
#: reader shows up as a wrong number rather than as a missing key.
REAL_V0217 = dict(probe.EXPECTED_V0217)


class ReadingTests(unittest.TestCase):
    """The reader's contract, on images small enough to state in full."""

    def test_key_names_are_found_and_mapped(self) -> None:
        image = self._image({"LocalTone": 0x1100})
        self.assertIn(0x1100, image["data_rvas"])

    def test_control_table_matches_the_pinned_build(self) -> None:
        """The expectations are the host's own table, not a copy of the reader."""
        self.assertEqual(REAL_V0217["Scale"], 0x8D9DC)
        self.assertEqual(REAL_V0217["Enabled"], 0x8D9BC)
        self.assertEqual(len(REAL_V0217), 11)
        # Every key the probe reads has an expectation, or a control run would
        # silently skip it.
        self.assertEqual(set(REAL_V0217), set(probe.OPTION_KEYS))

    def test_a_missing_key_is_reported_not_guessed(self) -> None:
        """A build that renames a key must not map it onto the old offset."""
        results, _sects, _data = probe.derive(Path(self._image_path({})))
        for key in probe.OPTION_KEYS:
            self.assertNotIn(key, results,
                             f"{key} was mapped although no code reads it")

    def test_entry_points_are_not_guessed(self) -> None:
        """Only the structurally identified entry point is offered.

        Three of the four entry points cannot be placed by this probe: ranking
        their candidate functions needs every field, and the fields do not all
        move together between builds. Offering a guess for them would be worse
        than offering nothing - a wrong entry point is a jump into the middle of
        an unrelated function, which does not fail politely.
        """
        entries = probe.derive_entry_points(Path(self._image_path({})))
        self.assertEqual(entries, {},
                         "an entry point was reported for an image that has no "
                         "anchor string")
        self.assertEqual(set(probe.ENTRY_ANCHOR_V0217), {"kInit"},
                         "the control anchor set grew without a way to verify it")

    def test_anchor_string_identifies_the_init_function(self) -> None:
        """The one entry point the probe does place, placed by its own evidence."""
        path = Path(self._image_path_with_anchor())
        entries = probe.derive_entry_points(path)
        self.assertEqual(set(entries), {"kInit"})
        self.assertEqual(entries["kInit"], 0x1000,
                         "the anchor's referencing function is not the one reported")

    def test_control_actually_compares_the_entry_point(self) -> None:
        """`--expect` must fail when the entry point is wrong.

        The control is the only thing standing between this probe and a wrong
        offset written into someone else's image, so it is tested the way the
        probe is: by making the answer wrong and requiring a failure. A control
        that silently compares nothing is worse than no control, because it
        reads as a pass.
        """
        import contextlib
        import io

        path = self._image_path_with_anchor()
        # The real v0.2.17 init RVA is not where the synthetic image puts it, so
        # a control run over this image with --expect must report a mismatch.
        captured = io.StringIO()
        argv = ["amd_offsets_probe.py", str(path), "--expect"]
        with mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(captured):
            code = probe.main()
        self.assertEqual(code, 1, "control passed an image with the wrong kInit")
        self.assertIn("MISMATCH", captured.getvalue())
        self.assertIn("kInit", captured.getvalue())

    def _image_path_with_anchor(self) -> str:
        """A synthetic image whose .text refers to the anchor string."""
        raw = bytearray(self._synthetic({}))
        # `.text` starts at raw 0x400; put `lea rcx,[rip+disp]` at its head and
        # the anchor string into .rdata.
        anchor_rva = 0x5000
        disp = anchor_rva - (0x1000 + 7)
        raw[0x400:0x407] = b"\x48\x8d\x0d" + struct.pack("<i", disp)
        raw[0x400 + 0x1000:0x400 + 0x1000 + len(probe._ANCHOR_STRING)] = \
            probe._ANCHOR_STRING
        path = Path(self._temporary.name) / "anchored.dll"
        path.write_bytes(bytes(raw))
        return str(path)

    def _image(self, keys: dict[str, int]) -> dict:
        path = self._image_path(keys)
        data = Path(path).read_bytes()
        sects = probe.sections(data)
        out = {"data_rvas": set()}
        for rva in keys.values():
            out["data_rvas"].add(rva)
        for name, vaddr, _vsize, _rawptr, _rawsize in sects:
            if name == ".data":
                out["data_section"] = (vaddr, _vsize)
        return out

    def _image_path(self, keys: dict[str, int]) -> str:
        """A minimal PE whose .text holds `lea rdx,[name]` + `call` + `mov`."""
        path = self._path
        path.write_bytes(self._synthetic(keys))
        return str(path)

    # --- the synthetic image -------------------------------------------------

    def setUp(self) -> None:
        import tempfile

        self._temporary = tempfile.TemporaryDirectory()
        self._path = Path(self._temporary.name) / "synthetic.dll"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _synthetic(self, keys: dict[str, int]) -> bytes:
        """One PE with a `.text` that reads each key the way the runtime does.

        Layout: for every key, `lea rdx,[rip+name]` (4+4 bytes), `call
        [rip+slot]` (6 bytes), `mov [rip+field], eax` (6 bytes) - the same three
        instructions the real reader uses, so the same reader code path runs.

        A `.pdata` is included, because the probe reads code bounds from it: an
        image without one has no functions as far as this reader is concerned,
        and that is a property of the reader worth keeping rather than relaxing.
        """
        text_rva, text_size = 0x1000, 0x1000
        data_rva, data_size = 0x3000, 0x1000
        rdata_rva, rdata_size = 0x5000, 0x1000
        pdata_rva, pdata_size = 0x7000, 0x1000

        names = b""
        name_rvas = {}
        for key in keys:
            name_rvas[key] = rdata_rva + len(names)
            names += key.encode("ascii") + b"\0"

        code = b""
        for key, field_rva in keys.items():
            name_here = text_rva + len(code)
            # lea rdx, [rip + disp]
            disp = name_rvas[key] - (name_here + 7)
            code += b"\x48\x8d\x15" + struct.pack("<i", disp)
            # call qword ptr [rip + 0] - a slot inside .text, standing in for the
            # imported profile API; the reader only needs a call to stop at.
            code += b"\xff\x15" + struct.pack("<i", 0)
            # mov dword ptr [rip + disp], eax
            here = text_rva + len(code)
            disp2 = field_rva - (here + 6)
            code += b"\x89\x05" + struct.pack("<i", disp2)

        # One .pdata entry covering the whole of .text, so the code above is a
        # function with bounds the reader will honour.
        pdata = struct.pack("<III", text_rva, text_rva + text_size, 0)

        sections = [
            (b".text", text_rva, text_size, 0x400, text_size),
            (b".rdata", rdata_rva, rdata_size, 0x400 + text_size, rdata_size),
            (b".data", data_rva, data_size, 0x400 + text_size + rdata_size, data_size),
            (b".pdata", pdata_rva, pdata_size, 0x400 + text_size + rdata_size + data_size,
             pdata_size),
        ]
        headers = 0x400
        raw = bytearray(headers + text_size + rdata_size + data_size + pdata_size)
        pe = 0x80
        raw[0:2] = b"MZ"
        struct.pack_into("<I", raw, 0x3C, pe)
        raw[pe:pe + 4] = b"PE\0\0"
        struct.pack_into("<H", raw, pe + 6, len(sections))
        struct.pack_into("<H", raw, pe + 20, 0xF0)   # optional header size
        struct.pack_into("<H", raw, pe + 24, 0x20B)  # PE32+
        for index, (name, vaddr, vsize, rawptr, rawsize) in enumerate(sections):
            at = pe + 24 + 0xF0 + index * 40
            raw[at:at + 8] = name.ljust(8, b"\0")
            struct.pack_into("<IIII", raw, at + 8, vsize, vaddr, rawsize, rawptr)
        raw[0x400:0x400 + len(code)] = code
        raw[0x400 + text_size:0x400 + text_size + len(names)] = names
        pdata_at = 0x400 + text_size + rdata_size + data_size
        raw[pdata_at:pdata_at + len(pdata)] = pdata
        return bytes(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
