"""The runtime offset table exists in one place, and every copy of it agrees.

The runtime exports nothing, so this project addresses it by raw offsets into
its image. That makes every one of those numbers a live hazard: a wrong one does
not fail politely, it writes into read-only memory or calls into the middle of an
unrelated function.

There are two files that name those addresses - `native/amd/amd_runtime.h` for
the worker and `native/amd/probe_amd.cpp` for the standalone check - and a port
has to update both. That is not a hypothetical: the same split in the project
this table was re-derived from cost them a crash, because a port updated two of
four copies and the two left behind kept a stale counter, which landed in
read-only memory on the first evaluation.

So this test pins the relationship rather than the numbers:

1. every offset the probe hardcodes is ALSO in the header, with the same value;
2. no other file under native/amd/ writes a raw address of its own - a literal
   like `At<T>(module, 0x1234)` outside the header means a third copy exists;
3. the entry points the probe calls are the ones the header names, so the
   program cannot check itself against a table it no longer shares.

It does NOT check the numbers against the binary - that needs the runtime, and
`tools/prepare_amd_runtime.py` plus the probe's own hash gate do it on a machine
that has one.

Run:  runtime\\python.exe tests\\test_amd_offsets_agree.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
AMD = BASE / "native" / "amd"
HEADER = AMD / "amd_runtime.h"
PROBE = AMD / "probe_amd.cpp"


def header_offsets(text: str) -> dict:
    """{name: value} from the rva namespace of the header."""
    ns = text[text.find("namespace rva {"):]
    ns = ns[: ns.find("}  // namespace rva")]
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"(k\w+)\s*=\s*0x([0-9a-fA-F]+)", ns)}


def probe_offsets(text: str) -> dict:
    """{name: value} from the probe's own kRva* constants."""
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"constexpr\s+uintptr_t\s+(kRva\w+)\s*=\s*0x([0-9a-fA-F]+)", text)}


def main() -> int:
    failures = []
    header = header_offsets(HEADER.read_text(encoding="utf-8", errors="replace"))
    probe_text = PROBE.read_text(encoding="utf-8", errors="replace")
    probe = probe_offsets(probe_text)

    if not header:
        failures.append("the header names no offsets at all - this test cannot "
                        "see the table it is meant to pin")
    if not probe:
        failures.append("the probe names no offsets - either it stopped "
                        "addressing the runtime or the constants were renamed")

    # --- 1. every constant the probe carries must match the header ---------
    # kRvaInit <-> kInit, kRvaHipDevice <-> kHipDevice, kRvaInitCtx <-> kInitCtx
    for pname, value in sorted(probe.items()):
        hname = "k" + pname[len("kRva"):]          # kRvaInit -> kInit
        if hname not in header:
            failures.append(f"probe const {pname} (0x{value:x}) has no "
                            f"{hname} in the header's table - they have drifted apart")
        elif header[hname] != value:
            failures.append(f"{pname} = 0x{value:x} in the probe, but the header "
                            f"says {hname} = 0x{header[hname]:x} - a port updated "
                            f"one and not the other")

    # --- 2. no third copy: a raw address written outside the header --------
    # The forms that reach the runtime. `At<T>(module_, 0x...)` is the header's
    # own idiom, so only files OTHER than the header are searched, and the
    # probe's own named constants are how it is supposed to do it.
    at_call = re.compile(r"At<[^>]+>\([^,]+,\s*0x([0-9a-fA-F]+)\s*\)")
    plus_raw = re.compile(r"reinterpret_cast<uintptr_t>\([^)]+\)\s*\+\s*0x([0-9a-fA-F]+)")
    for path in sorted(AMD.iterdir()):
        if path.suffix.lower() not in (".cpp", ".h", ".inl") or path == HEADER:
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in at_call.findall(line) + plus_raw.findall(line):
                failures.append(f"{path.name}:{num} writes the address 0x{m} "
                                f"itself - offsets belong in amd_runtime.h, and a "
                                f"second copy is what this test exists to prevent")

    # --- 3. the probe calls what the header names -------------------------
    # If the probe addresses the runtime through its own constants only, the
    # check above covers it; this makes sure it has not started calling an
    # unnamed address instead.
    if probe and "reinterpret_cast<uintptr_t>(mod) + kRvaInit" not in probe_text:
        failures.append("the probe no longer uses kRvaInit for the init call - "
                        "it may be calling an address the header does not name")

    if failures:
        print("FAIL: the runtime offset table is not in one place:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"PASS: {len(header)} offsets in the header, {len(probe)} in the "
          f"probe, every shared name agrees, and no other file writes one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
