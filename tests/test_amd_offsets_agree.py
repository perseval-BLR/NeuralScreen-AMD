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
    """{name: value} from the header's pinned v0.2.17 table.

    The table became a struct (`rva::kV0217`) when the host started driving two
    builds: the same names now appear in `kV0310` with different values, so the
    parse has to name the build it wants. The v0.2.17 one is what the probe and
    the prepare tool still carry - they check a runtime whose offsets are the
    pins, and a third copy that silently followed the newer build would stop
    being able to disagree.

    The values are read out of the braced initialiser by its comments, which is
    what makes this robust against the field order changing: each entry is
    `/* kName */ 0x...`.
    """
    start = text.find("inline constexpr Table kV0217")
    if start < 0:
        return {}
    body = text[start:text.find("};", start)]
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"/\*\s*(k\w+)\s*\*/\s*0x([0-9a-fA-F]+)", body)}


def probe_offsets(text: str) -> dict:
    """{name: value} from the probe's own kRva* constants."""
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"constexpr\s+uintptr_t\s+(kRva\w+)\s*=\s*0x([0-9a-fA-F]+)", text)}


def tool_offsets() -> dict:
    """{name: value} from tools/prepare_amd_runtime.py's KNOWN_OFFSETS.

    That table exists on purpose - the script a user runs to check a runtime has
    to be able to DISAGREE with the header rather than import it, or a wrong
    offset would be confirmed by the very file it came from. So it is a third
    copy, and this test is what keeps it honest.
    """
    import importlib.util
    path = BASE / "tools" / "prepare_amd_runtime.py"
    spec = importlib.util.spec_from_file_location("ns_prepare_amd_runtime", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # main() is guarded, nothing runs
    return dict(mod.KNOWN_OFFSETS)


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

    # --- 3. the third copy: the tool's own table ---------------------------
    # tools/prepare_amd_runtime.py carries KNOWN_OFFSETS so it can check a
    # runtime without importing the header (a table that confirms itself is
    # worthless). Every name it has must agree with the header, and it must not
    # have invented one of its own.
    try:
        tool = tool_offsets()
    except Exception as exc:                       # noqa: BLE001 - report, don't crash
        tool = None
        failures.append(f"cannot read the offset table in tools/prepare_amd_runtime.py "
                        f"({type(exc).__name__}: {exc}) - if that table was renamed, "
                        f"update this test; if it was removed, the check the user "
                        f"runs before writing a runtime is gone")
    if tool:
        for name, value in sorted(tool.items()):
            if name not in header:
                failures.append(f"tools/prepare_amd_runtime.py names {name} "
                                f"(0x{value:x}), which the header does not - a "
                                f"fourth copy, or a rename that missed the header")
            elif header[name] != value:
                failures.append(f"tools/prepare_amd_runtime.py says {name} = "
                                f"0x{value:x}, the header says 0x{header[name]:x} - "
                                f"this is the drift the test exists to catch")

    # --- 4. the header must not contradict itself --------------------------
    # The table above and the "deliberately NOT written" note below it are two
    # statements about the same image, and an address in both is an address the
    # driver both declares and forbids. 0x8d808 lived in both for a release:
    # listed as `kAbortWord` and named in the note as the engine's watchdog
    # counter. The write won, because the note is a comment.
    #
    # The check is textual and deliberately narrow - it looks for the note, then
    # for each declared offset inside it - because the two halves are written in
    # different styles and only a human reading both would notice.
    header_text = HEADER.read_text(encoding="utf-8", errors="replace")
    marker = "Deliberately NOT written by the host"
    if marker not in header_text:
        failures.append(f"the header has no '{marker}' note any more - either it "
                        f"was renamed (update this test) or the guard against "
                        f"writing engine-owned fields was dropped")
    else:
        note = header_text[header_text.find(marker):]
        note = note[:note.find("}  // namespace rva")]
        for name, value in sorted(header.items()):
            if f"0x{value:x}" in note.lower():
                failures.append(f"{name} (0x{value:x}) is in the offset table AND "
                                f"named in the note that says the host must not "
                                f"write it - one of the two is wrong, and the "
                                f"table wins at runtime")

    # --- 5. the probe calls what the header names -------------------------
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
