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


def table_offsets(text: str, table_name: str) -> dict:
    """{name: value} from any of the header's tables, by its C++ name.

    Same parse as header_offsets, which is now the v0.2.17 special case of this
    one: the probe carries constants for both tables, so the comparison has to
    be able to name either.
    """
    start = text.find(f"inline constexpr Table {table_name}")
    if start < 0:
        return {}
    body = text[start:text.find("};", start)]
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"/\*\s*(k\w+)\s*\*/\s*0x([0-9a-fA-F]+)", body)}


def probe_offsets(text: str) -> dict:
    """{name: value} from the probe's own kRva* constants.

    The probe carries TWO sets now, one per build, because it has to call into
    whichever image the program would load - calling v0.2.17's addresses on a
    v0.3.1 image would execute something else and report the resulting
    exception as "wrong build or wrong device", which is the wrong conclusion
    about the right build.

    The names are normalised so the second set compares against the header's
    second table: `kRvaInit0310` here is `kInit` in `kV0310`. Without that, a
    copy that drifted from the header would look like an unrelated constant.
    """
    out = {}
    for m in re.finditer(
            r"constexpr\s+uintptr_t\s+(kRva\w+)\s*=\s*0x([0-9a-fA-F]+)", text):
        name, value = m.group(1), int(m.group(2), 16)
        out[name] = value
    return out


def normalise_probe(offsets: dict) -> tuple[dict, dict]:
    """Probe constants -> header names, split by which build they belong to.

    Returns ({v0.2.17 names: values}, {v0.3.1 names: values}). The split is the
    point: each set has to be compared against its OWN table in the header, and
    the header's two tables use the same field names (`kV0217.kInit` and
    `kV0310.kInit` are different addresses for the same field).

    kRvaInitCtx/kRvaHipDevice/kRvaInit are the header's kInitCtx/kHipDevice/
    kInit; the `0310`-suffixed variants are those same fields of the second
    table, so the suffix is dropped once the build is known.
    """
    mapping = {"kRvaInitCtx": "kInitCtx", "kRvaHipDevice": "kHipDevice",
               "kRvaInit": "kInit"}
    old_build, new_build = {}, {}
    for name, value in offsets.items():
        base, is_0310 = (name[:-4], True) if name.endswith("0310") else (name, False)
        header_name = mapping.get(base, base)
        (new_build if is_0310 else old_build)[header_name] = value
    return old_build, new_build


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



# --- the cross-check that found a real bug --------------------------------
# A second, independently published layout (OptiScaler's dlssnr AMD backend)
# corrected a wrong address in this project: our v0.3.1 init context had been
# derived by carrying the v0.2.17 relation "context = kDevice + 0x10" into a
# build where it is kDevice + 0x18.
#
# What makes that source usable is that it is CALIBRATED: its v0.2.17 entry has
# to reproduce our own v0.2.17 values, which a live Radeon run confirmed. If it
# ever stops agreeing on the build we can check, it must not be used to correct
# the build we cannot.
#
# The expected values are written out here rather than fetched, so the check
# still works offline; they are the published table's v0.2.17 entry.
CALIBRATION_0217 = {
    "init": 0x19240, "record": 0xf600, "notify": 0x9170, "shutdown": 0x12690,
    "device": 0x8cee8, "queue": 0x8cef0, "engine": 0x8cef8, "hipOrdinal": 0x8dad0,
    "configuredInline": 0x8d6c0, "interop": 0x8d82c, "enabled": 0x8d9bc,
    "initDone": 0x8d218, "nativeFailure": 0x8d21a, "fsrInputs": 0x8d9be,
    "depthPresent": 0x8d9bf, "temporal": 0x8d9bd, "depthInverted": 0x8d9b0,
    "explicitDepth": 0x8d9b4, "tone": 0x8d9d0, "structure": 0x8d9d4,
    "skin": 0x8d9d8, "charMask": 0x8d9e0, "toneChannels": 0x8d9e4,
    "historyView": 0x8d010, "historyValid": 0x8d018, "jobId": 0x8d914,
    "jobDone": 0x8d6f4, "timeoutCount": 0x8d6f8, "pendingList": 0x8d908,
}

# Our own confirmed v0.2.17 values, by the same names.
OURS_0217 = {
    "init": "kInit", "record": "kRecord", "notify": "kNotify", "shutdown": "kShutdown",
    "device": "kDevice", "queue": "kQueue", "engine": "kInitCtx", "hipOrdinal": "kHipDevice",
    "configuredInline": "kInlineMode", "interop": "kInterop", "enabled": "kEnabled",
    "initDone": "kFlagAfterInit", "nativeFailure": "kStatusFlag", "fsrInputs": "kUseFsrInputs",
    "depthPresent": "kUseDepth", "temporal": "kPerPassFlag", "depthInverted": "kDepthInverted",
    "explicitDepth": "kDepthExplicit", "tone": "kLocalTone", "structure": "kLocalStructure",
    "skin": "kSkinStructure", "charMask": "kCharMask", "toneChannels": "kToneChannels",
    "historyView": "kHistory", "historyValid": "kWantHistory", "jobId": "kJobCounter",
    "jobDone": "kSyncCounter", "timeoutCount": "kTimeoutCounter", "pendingList": "kPendingList",
}


def calibration_is_sound(header_text: str) -> tuple[bool, list[str]]:
    """Does the published table agree with ours on the build we confirmed?

    Returns (ok, mismatches). Called on the v0.2.17 half only: that is the build
    a live run has validated, so a disagreement there means the other table is
    describing something else and must not be used to correct v0.3.1.
    """
    ours = table_offsets(header_text, "kV0217")
    bad = []
    for their_name, our_name in OURS_0217.items():
        want = CALIBRATION_0217[their_name]
        have = ours.get(our_name)
        if have is None:
            bad.append(f"{our_name} missing from kV0217")
        elif have != want:
            bad.append(f"{our_name}: ours 0x{have:x}, published 0x{want:x}")
    return (not bad), bad


def check_calibration() -> int:
    ok, bad = calibration_is_sound(HEADER.read_text(encoding="utf-8", errors="replace"))
    if not ok:
        print("FAIL: the published layout no longer agrees with our confirmed "
              "v0.2.17 values, so it cannot be trusted to correct v0.3.1:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print(f"PASS: the published layout reproduces all {len(CALIBRATION_0217)} of "
          f"our confirmed v0.2.17 values - it is a calibrated second opinion")
    return 0


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
    # Two sets now, and each must be compared against ITS OWN table: kRvaInit
    # against kV0217's kInit, kRvaInit0310 against kV0310's kInit. Comparing a
    # v0.3.1 constant against the v0.2.17 table would be a false alarm, and
    # comparing it against nothing is how a drifted copy would hide.
    header_0310 = table_offsets(
        HEADER.read_text(encoding="utf-8", errors="replace"), "kV0310")
    probe_old, probe_new = normalise_probe(probe)
    if probe_new and not header_0310:
        failures.append("the probe carries v0.3.1 constants but the header has "
                        "no kV0310 table at all")
    for label, table, probe_set in (("", header, probe_old),
                                    (" (kV0310)", header_0310, probe_new)):
        for pname, value in sorted(probe_set.items()):
            if pname not in table:
                failures.append(f"probe const {pname}{label} (0x{value:x}) has no "
                                f"{pname} in the header's table - they have drifted apart")
            elif table[pname] != value:
                failures.append(f"{pname}{label} = 0x{value:x} in the probe, but the "
                                f"header says {pname} = 0x{table[pname]:x} - a port "
                                f"updated one and not the other")

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
    # unnamed address instead. Both builds' constants count: the probe picks
    # one of the two by hash, so either name is legitimate - a bare `0x...`
    # is not.
    if probe and ("reinterpret_cast<uintptr_t>(mod) + rva_init" not in probe_text
                  and "reinterpret_cast<uintptr_t>(mod) + kRvaInit" not in probe_text):
        failures.append("the probe no longer uses kRvaInit for the init call - "
                        "it may be calling an address the header does not name")

    # The cross-check's own premise: a second layout is only evidence if it
    # agrees on the build we have validated.
    _cal_ok, _cal_bad = calibration_is_sound(
        HEADER.read_text(encoding="utf-8", errors="replace"))
    for b in _cal_bad:
        failures.append("published-layout calibration broken: " + b)

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
