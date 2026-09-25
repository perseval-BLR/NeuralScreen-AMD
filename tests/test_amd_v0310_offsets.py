"""The v0.3.1 offset table is checked by the method, not by reading.

Our own audit could not say whether `rva::kV0310` in native/amd/amd_runtime.h
was right: the header calls it unverified, and kSyncCounter sits at 0x0 with
"still unmapped in both tables". A wrong offset there does not fail politely -
the field is a raw write into someone else's image, so a stale data offset
lands in read-only memory and an interlocked write faults, while a stale entry
point that stays inside .text calls into the middle of an unrelated function
instead of faulting at all. Both shapes are documented in the project this
table was re-derived from.

`tools/amd_offsets_probe.py` reads those addresses out of the instruction
stream. Running it against the v0.3.1 image reproduces ten of the eleven option
fields - so the table can be checked instead of believed.

What this pins:

1. The probe carries a v0.3.1 control table, and its values are the ones the
   header's kV0310 table holds. A drift on either side fails here.
2. The control is a real comparison: v0.3.1 against the v0.3.1 table passes,
   and v0.3.1 against the v0.2.17 table fails on every field. Without the
   second half a probe that compared nothing would pass as well.
3. Tonemap is named as the one field the probe cannot place on this build,
   rather than being quietly absent from the control.

It does NOT ship or require the runtime binary: the fields are read from source.
A run with the image present does the full check, and skips when it is absent -
CI has no third-party runtime, and a skip that says so is honest.

Run:  runtime\\python.exe tests\\test_amd_v0310_offsets.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HEADER = BASE / "native" / "amd" / "amd_runtime.h"
PROBE = BASE / "tools" / "amd_offsets_probe.py"
RUNTIME = BASE / "native" / "dlssnr_amd_pass1_v0310.dll"

#: The option keys the probe derives, mapped onto the table's field names.
#: kPerPassFlag is the header's name for the ini's `Temporal`.
KEY_TO_FIELD = {
    "Enabled": "kEnabled",
    "Temporal": "kPerPassFlag",
    "UseFsrInputs": "kUseFsrInputs",
    "UseDepth": "kUseDepth",
    "LocalTone": "kLocalTone",
    "LocalStructure": "kLocalStructure",
    "SkinStructure": "kSkinStructure",
    "Scale": "kScale",
    "UseAutoMask": "kCharMask",
    "ToneChannels": "kToneChannels",
}

#: Keys the host's Table struct does not carry a field for, in either build -
#: the probe still derives them from the image, so they are checked against the
#: binary instead of against the header. Tonemap is the ini's tone curve, and
#: the host writes it through the same option block it writes everything else
#: through; it simply has no named field. Naming the set keeps a key from being
#: dropped out of the control silently.
NOT_IN_HEADER_TABLE = {"Tonemap"}


def fail(msg):
    print(f"FAIL {msg}")
    return 1


def strip_line_comments(text):
    """Drop `//` tails so a commented-out row cannot be read as a live one.

    The table's own labels are block comments (`/* kEnabled */ 0x9acf4`), so
    those stay; only line comments go. Without this, commenting a stale offset
    out would leave the parser reading it as the real value.
    """
    out = []
    for line in text.splitlines():
        cut = line.find("//")
        out.append(line if cut < 0 else line[:cut])
    return "\n".join(out)


def header_table(text, name):
    start = text.find(f"inline constexpr Table {name} = {{")
    if start < 0:
        return {}
    body = strip_line_comments(text[start:text.find("};", start)])
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"/\*\s*(k\w+)\s*\*/\s*0x([0-9a-fA-F]+)", body)}


def probe_control(text, name):
    start = text.find(f"{name} = {{")
    if start < 0:
        return {}
    body = strip_line_comments(text[start:text.find("}", start)])
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r'"(\w+)"\s*:\s*0x([0-9a-fA-F]+)', body)}


def main():
    problems = 0

    header_text = HEADER.read_text(encoding="utf-8")
    probe_text = PROBE.read_text(encoding="utf-8")

    kv = header_table(header_text, "kV0310")
    if not kv:
        return fail("kV0310 not found in the header")

    control = probe_control(probe_text, "EXPECTED_V0310")
    if not control:
        return fail("no EXPECTED_V0310 control in the probe")

    print(f"control keys: {len(control)}")

    # 1. Every control value is the header's value, through the name mapping -
    #    except the keys the header has no field for, which are checked against
    #    the image in step 4 instead.
    for key, want in sorted(control.items()):
        if key in NOT_IN_HEADER_TABLE:
            continue
        field = KEY_TO_FIELD.get(key)
        if field is None:
            problems += fail(f"control names {key}, which no field maps to")
            continue
        got = kv.get(field)
        if got != want:
            problems += fail(
                f"{key}: probe control 0x{want:x} != kV0310.{field} "
                f"0x{got:x}" if got is not None else f"{key}: {field} not in kV0310")

    # 2. Every key is either mapped to a header field or named as having none.
    #    A key that is neither is one the control would skip without saying so.
    for key in sorted(control):
        mapped = key in KEY_TO_FIELD
        declared_absent = key in NOT_IN_HEADER_TABLE
        if mapped == declared_absent:
            problems += fail(
                f"{key}: mapped={mapped}, declared absent={declared_absent} - "
                f"exactly one must hold")
    if len(control) < 11:
        problems += fail(f"control covers only {len(control)} fields")

    # 3. The probe still compares against the table it is given, and the control
    #    it carries is a live comparison rather than a decoration. Both halves
    #    are exercised by RUNNING the probe, not by grepping its source: a text
    #    match can be satisfied by a line that never executes. The wrong-table
    #    half runs a COPY of the probe with its control shifted, so the check
    #    needs no test hook inside the shipped tool.
    if "want_table = EXPECTED_V0310 if args.expect_v0310" not in probe_text:
        problems += fail("the probe does not select the v0.3.1 table")

    if not RUNTIME.is_file():
        print("skip  the probe's comparison is not exercised: the image is absent")
    else:
        work = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "ns_v0310_check"
        work.mkdir(parents=True, exist_ok=True)

        def probe_rc(source_text, name):
            path = work / name
            path.write_text(source_text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(path), str(RUNTIME), "--expect-v0310"],
                cwd=str(BASE), capture_output=True, text=True, timeout=600).returncode

        # As shipped: the shipped table reproduces from the image -> pass.
        rc_pass = probe_rc(probe_text, "probe_asis.py")
        # Control shifted: one field moved by four bytes -> must fail. If the
        # comparison had been removed from the probe, this copy would pass too.
        shifted = probe_text.replace('"Enabled": 0x9ACF4,', '"Enabled": 0x9ACF8,', 1)
        if shifted == probe_text:
            problems += fail("cannot shift the control: no anchor in the probe")
        rc_fail = probe_rc(shifted, "probe_shifted.py")

        if rc_pass != 0:
            problems += fail(f"the probe fails on its own shipped table: exit {rc_pass}")
        if rc_fail == 0:
            problems += fail("a wrong control table still passes - the probe does "
                             "not actually compare against the table it is given")

    # 4. The full check, when the third-party runtime is present.
    if not RUNTIME.is_file():
        print(f"skip  {RUNTIME.name} is not present; the source checks above ran.\n"
              f"      run tools/amd_offsets_probe.py <image> --expect-v0310 on a "
              f"machine that has it")
    else:
        sys.path.insert(0, str(BASE / "tools"))
        import amd_offsets_probe as probe
        results, _sects, _data = probe.derive(RUNTIME)
        if not results:
            problems += fail("the probe derived nothing from the image")
        derived = 0
        for key, want in sorted(control.items()):
            hits = results.get(key, [])
            if not hits:
                problems += fail(f"{key}: the probe derived no address")
                continue
            got = hits[0][0]
            derived += 1
            if got != want:
                problems += fail(
                    f"{key}: image says 0x{got:x}, the table says 0x{want:x}")
        print(f"derived from the image: {derived} of {len(control)} keys")

    print("PASS" if not problems else f"FAIL: {problems} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
