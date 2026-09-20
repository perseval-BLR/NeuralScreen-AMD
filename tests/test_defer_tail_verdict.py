"""The defer-tail optimisation says whether it is on, and which input decided it.

WHY THIS EXISTS
---------------
`defer_tail` defers the neural waits to the present fence. It is chosen from
three facts - `!g_hdr_capture`, the warm-up, and `PresentModeActive` - and it ran
ENTIRELY SILENTLY.

A reporter's package (RX 7900 XTX, v0.3.12) alternated between a correct frame
and a blown-white one at exactly his present period: 133 ms per state, a 0.266 s
cycle, 3.8 Hz, with each state lasting as long as one presented frame. His log
carried ZERO lines about the deferral, so whether it was even active was
unanswerable from the report - the one question that decides where to look next
could not be asked of the evidence.

That is the same shape as the z-order guard's silent decisions, which cost two
rounds of guessing before it was given a decision log (v1.x, issue #96/#89). The
rule both cases teach: a branch that changes the rendering path must say that it
ran and what it chose, because "never ran" and "ran and chose wrong" are
different bugs and a log that stays quiet cannot tell them apart.

WHAT THIS LOCKS
---------------
1. The choice is logged on every CHANGE of state, not per frame - the line would
   otherwise drown the log at 60 fps.
2. Both states are logged: the reporter's case is the OFF state just as much as
   the ON one.
3. The line names the three inputs that decided it (`hdr_capture`, the HDR
   switch, `present_mode`), because with the choice alone you still cannot tell
   WHICH fact flipped - and on a machine with HDR on at the OS level, the switch
   and the capture flag disagree by design.

Run:  runtime\\python.exe tests/test_defer_tail_verdict.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HOST = BASE / "native" / "dlss5-feed-host64.cpp"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def main() -> int:
    failures: list[str] = []
    if not HOST.exists():
        print("FAIL: the worker source is missing")
        return 1
    code = strip_comments(HOST.read_text(encoding="utf-8", errors="replace"))

    # Find where the deferral is decided, then read what follows it.
    at = code.find("defer_tail = !g_hdr_capture")
    if at < 0:
        print("FAIL: the defer-tail choice is gone - this test has nothing to "
              "check and the choice it guards no longer exists")
        return 1
    # The decision's own statement, plus the lines after it, up to the next
    # thing the frame loop does (remembering what this frame will show).
    after = code[at:code.find("g_last_out_bypass", at)]

    # --- 1. both states are logged ------------------------------------------
    if '"[video] defer-tail on' not in after:
        failures.append(
            "the ON state is not logged - a report where the deferral was active "
            "still could not say so, which is the whole reason this line exists")
    if '"[video] defer-tail off' not in after:
        failures.append(
            "the OFF state is not logged - the reporter's own case was OFF-like "
            "silence, and 'never ran' vs 'ran and chose wrong' stays unanswerable")

    # --- 2. it is throttled per change, not per frame -----------------------
    if "defer_logged" not in after:
        failures.append(
            "there is no change-tracking state next to the log - an unthrottled "
            "line would be written every frame at 60 fps and bury the log it is "
            "meant to inform")
    else:
        if not re.search(r"now_defer\s*!=\s*defer_logged", after):
            failures.append(
                "the log is not gated on a CHANGE of the choice - it is then "
                "either per frame or never")

    # --- 3. the three deciding facts are named ------------------------------
    for fact in ("g_hdr_capture", "HdrEnabled()", "PresentModeActive(v)"):
        if fact not in after:
            failures.append(
                f"the line does not name {fact} - with the choice alone you "
                f"cannot tell which fact flipped, and on an HDR desktop the "
                f"switch and the capture flag disagree by design")

    if failures:
        print("FAIL: the defer-tail choice is not answerable from the log")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the defer-tail choice is logged on every change, in both states, "
          "and the line names the three facts that decided it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
