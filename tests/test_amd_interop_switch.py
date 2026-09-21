"""Interop is the A/B arm a reporter can flip, and the log says which arm ran.

WHY THIS EXISTS
---------------
#4 (RogueVoo, RX 7600, Windows 10) dies with a read at offset 0x18C inside
D3D12Core - a device-child object whose back-reference is NULL - with
`last job -1`, before the engine has done anything. Upstream has the same fault
open in their own standalone host (their #84) and name zero-copy interop as
where it happens; our shape matches (a host, not a game, handing shared
resources over).

Our side had no way to test the other value: `kInterop = 1` was written
unconditionally, so the only way to try the copy arm was to rebuild. This adds
the arm - `NS_AMD_INTEROP=0` - WITHOUT changing the default, because upstream's
own copy path is reported to fail too (0x887A0005). Flipping a default on a
guess trades a fault we have diagnosed for one we have not.

WHAT THIS LOCKS
---------------
1. The write site reads the env var instead of pinning the field to 1.
2. The DEFAULT stays 1: with nothing set, interop is on. This is the property
   that keeps every existing report comparable.
3. The value reaches the log. A one-variable A/B that leaves no trace turns two
   runs into one run done twice - the failure this tracker keeps hitting.
4. The ini key is NOT the control, and the code says so: the runtime reads the
   ini in its DllMain and this write lands after that.

Run:  runtime\\python.exe tests/test_amd_interop_switch.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RUNTIME_CPP = BASE / "native" / "amd" / "amd_runtime.cpp"
RUNTIME_H = BASE / "native" / "amd" / "amd_runtime.h"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def main() -> int:
    failures: list[str] = []
    for p in (RUNTIME_CPP, RUNTIME_H, BRIDGE):
        if not p.exists():
            print(f"FAIL: {p.name} is missing")
            return 1

    code = strip_comments(RUNTIME_CPP.read_text(encoding="utf-8", errors="replace"))
    head = strip_comments(RUNTIME_H.read_text(encoding="utf-8", errors="replace"))
    bridge = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # --- 1. the field is no longer pinned --------------------------------
    if "GetEnvironmentVariableA(\"NS_AMD_INTEROP\"" not in code:
        failures.append(
            "the write site does not read NS_AMD_INTEROP - the copy arm cannot be "
            "selected without a rebuild, which is the whole point of the switch")

    # The assignment must use a computed value, not the literal 1. A file that
    # still writes `= 1` there has the switch defined and not connected.
    if not re.search(r"At<uint8_t>\(module_, table_->kInterop\)\s*=\s*interop\s*;", code):
        failures.append(
            "kInterop is not assigned from the computed value - the env var is read "
            "and then thrown away")

    # --- 2. the default stays ON -----------------------------------------
    # `uint8_t interop = 1;` is the default; the env var can only lower it.
    if not re.search(r"uint8_t\s+interop\s*=\s*1\s*;", code):
        failures.append(
            "the default is no longer 1 - every existing report ran zero-copy, and "
            "changing the default silently makes them non-comparable")
    # ...and only an explicit "0" lowers it, not any non-empty value.
    if not re.search(r"v\[0\]\s*==\s*'0'\s*\)\s*interop\s*=\s*0", code):
        failures.append(
            "the env var lowers the value for something other than an explicit '0' "
            "- a stray NS_AMD_INTEROP=anything would then disable zero-copy")

    # --- 3. the value reaches the log ------------------------------------
    if "InteropWritten()" not in head:
        failures.append(
            "the runtime exposes no InteropWritten() - this unit has no Log(), so "
            "the bridge cannot report which arm ran")
    if "interop_" not in head:
        failures.append("the value is not kept in a member, so nothing can read it back")
    if "InteropWritten()" not in bridge:
        failures.append(
            "the bridge never logs the interop arm - a reporter who set the variable "
            "cannot tell from the log whether it took effect")

    # The "not written" case must be distinguishable from "on": a default that
    # reports 1 before any load would claim a write that never happened.
    if not re.search(r"int\s+interop_\s*=\s*-1\s*;", head):
        failures.append(
            "interop_ does not start at -1 - 'nothing written' would read as 'on', "
            "which is the class of invented value this project keeps removing")

    # --- 4. the ini is not claimed as the control ------------------------
    # The comment has to say so, because a reader WILL assume the ini key decides
    # it (it is the obvious place). Checked on the raw text, comments included.
    raw = RUNTIME_CPP.read_text(encoding="utf-8", errors="replace")
    if "does NOT decide interop" not in raw:
        failures.append(
            "nothing warns that the ini key is not the control - a reader will "
            "flip Interop=0 in the file, see no change, and conclude the switch "
            "is broken (the runtime reads the ini before this write)")

    if failures:
        print("FAIL: the interop switch is not a usable A/B arm")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: NS_AMD_INTEROP selects the copy arm without changing the default, "
          "the value reaches the log, and the ini is documented as not being the "
          "control")
    return 0


if __name__ == "__main__":
    sys.exit(main())
