"""A missing runtime file is not a verdict about the graphics card.

Issue #2's second half: the user clicked through the GPU picker and it looped
forever. Every card answered `[amd] the runtime did not come up: dlssnr_amd_
pass1.dll not found`, which the shared verdict list reads as a FAILURE - so
each switch was reverted AND each card was written into `gpu_no_nr`, the list
of cards that "cannot run the neural pass". Neither is true: nothing was wrong
with any of those cards. The file was simply not installed.

The distinction has to exist somewhere, because two callers ask the same
question for different reasons:

  * the menu wants "no neural pass" for both - the user sees the same thing;
  * a GPU switch must NOT be reverted, and a card must NOT be marked, when the
    fault is a file the user can copy in.

So the install faults are their own set, and `gpu_came_up` consults it first.

Checked:
1. The lines from the user's own log are an install fault.
2. A card that genuinely cannot run the pass is NOT an install fault - the
   distinction must not swallow the real verdict.
3. A working card is neither.
4. The worker's refusal strings still match: the tokens are substrings of what
   the bridge actually logs, not invented text.

Run:  runtime\\python.exe tests\\test_amd_install_verdict.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import settings_io  # noqa: E402

BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"
RUNTIME_CPP = BASE / "native" / "amd" / "amd_runtime.cpp"


def main() -> int:
    failures = []

    # --- 1. the user's own lines ------------------------------------------
    user_tail = [
        "[amd] the runtime did not come up: dlssnr_amd_pass1.dll not found in <PATH>",
        "[amd] ===== AMD path off - the raw frame passes through =====",
        "[video] the neural pass is not running - SAFE PASSTHROUGH",
    ]
    if not settings_io.nr_verdict_is_install(user_tail):
        failures.append("the user's own log lines are not recognised as an "
                        "install fault - the picker would loop again")
    # It must still read as "the pass is not running": the menu is honest.
    if settings_io.nr_verdict(reversed(user_tail)) is not False:
        failures.append("an install fault no longer reads as 'the pass is not "
                        "running' - the menu would claim the pass works")

    # --- 2. a genuinely unsupported card ----------------------------------
    for line in ("[amd] this GPU is not supported by the AMD neural pass - it needs",
                 "no AMD adapter found"):
        lines = [line, "[amd] ===== AMD path off - the raw frame passes through ====="]
        if settings_io.nr_verdict_is_install(lines):
            failures.append(f"a card fault is being read as an install fault: "
                            f"{line!r} - a bad card would never be marked")
        if settings_io.nr_verdict(reversed(lines)) is not False:
            failures.append(f"an unsupported card no longer reads as failure: "
                            f"{line!r}")

    # --- 3. a working card ------------------------------------------------
    good = ["[amd] ===== AMD path active ====="]
    if settings_io.nr_verdict_is_install(good):
        failures.append("a working pass is read as an install fault")
    if settings_io.nr_verdict(reversed(good)) is not True:
        failures.append("a working pass no longer reads as success")

    # --- 4. the tokens are real strings from the worker -------------------
    # A verdict list that names text nobody logs is a list that never fires.
    bridge = BRIDGE.read_text(encoding="utf-8", errors="replace")
    cpp = RUNTIME_CPP.read_text(encoding="utf-8", errors="replace")
    source = bridge + cpp
    for token in settings_io.NR_VERDICT_INSTALL:
        # The bridge builds some of these from pieces; require the head of the
        # token to appear so a rename in the worker is caught here.
        head = token.split(":")[0].split(" - ")[0][:28]
        if head not in source:
            failures.append(f"the install token {token!r} names text the worker "
                            f"never logs (looked for {head!r})")

    # --- 5. gpu_came_up consults the install set ---------------------------
    pipeline_src = (BASE / "pipeline.py").read_text(encoding="utf-8")
    if "nr_verdict_is_install" not in pipeline_src:
        failures.append("gpu_came_up does not consult the install faults - a "
                        "missing file would still revert the switch")
    # And it must be checked BEFORE the general verdict, or the failure wins.
    first_install = pipeline_src.find("if nr_verdict_is_install(tail):")
    first_verdict = pipeline_src.find("verdict = nr_verdict(reversed(tail))")
    if first_install < 0 or first_verdict < 0:
        failures.append("gpu_came_up's verdict order is gone")
    elif first_install > first_verdict:
        failures.append("the install check runs AFTER the general verdict, so "
                        "a missing file is still read as a bad card")

    print(f"    install faults named: {len(settings_io.NR_VERDICT_INSTALL)}")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print("OK: a missing file is not read as a bad card, and a bad card still is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
