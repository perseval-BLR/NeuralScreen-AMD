"""The HUD's frame-delta reads what it is, and a reset is called a reset.

WHY THIS EXISTS
---------------
The periodic HUD line printed `scene <x>` where `x` is the MEAN PIXEL DELTA
between this frame and the previous one. The name made two readings look
obvious, and both are wrong:

  * `scene 1.000` looks like "the picture is moving". It is not: guides.py
    returns `scene_score = 1.0` whenever `previous_gray is None`, so it means
    "first frame after a reset - there is nothing to compare with yet".
  * `scene 0.000` on a still desktop looks like "the frame is black". It is
    not: a desktop that does not change has no delta by definition, and the
    capture can be perfectly alive.

Both readings sent a diagnosis the wrong way on real reports (vizo01's v0.3.11
run prints one of each). This guard keeps the honest name and keeps the reset
flagged, because a number whose label misleads is worse than no number.

WHAT THIS LOCKS
---------------
1. The HUD line no longer calls the field `scene`.
2. The value reported is still the frame delta from the guides.
3. A reset frame is announced as a reset instead of printing a bare 1.000.

Run:  runtime\\python.exe tests/test_hud_frame_delta.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MAIN = BASE / "main.py"
GUIDES = BASE / "guides.py"


def strip_comments(text: str) -> str:
    """Drop comments so prose quoting the old name is not read as code."""
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line.split("#")[0] if "#" in line else line)
    return "\n".join(out)


def main() -> int:
    failures: list[str] = []
    if not MAIN.exists() or not GUIDES.exists():
        print("FAIL: main.py or guides.py is missing")
        return 1

    src = MAIN.read_text(encoding="utf-8", errors="replace")
    code = strip_comments(src)

    # --- 1. the label is honest ---------------------------------------------
    # `scene_score` as a Python identifier is fine; what must not come back is
    # the field being PRINTED under the name `scene`.
    if re.search(r'f"\s*\|\s*scene\s', code):
        failures.append(
            "the HUD line prints the field as `scene` again - it is the mean "
            "pixel delta, and the name reads as 'the picture is moving', which "
            "is the opposite of what a reset frame shows")
    if "frame_delta" not in code:
        failures.append(
            "the HUD line no longer names the field `frame_delta` - the reader "
            "cannot tell what the number is")

    # --- 2. the value still comes from the guides ---------------------------
    if "guide.scene_score" not in code:
        failures.append(
            "the reported value is no longer the guides' frame delta")

    # --- 3. a reset is announced, not printed as a bare 1.0 ------------------
    # guides.py is where the misleading 1.0 is produced; if that ever changes
    # the flag below must change with it.
    gsrc = GUIDES.read_text(encoding="utf-8", errors="replace")
    reset_at = gsrc.find("previous_gray is None")
    if reset_at < 0:
        failures.append(
            "guides.py no longer branches on `previous_gray is None` - the "
            "1.0-returning reset path moved and this guard is now blind")
    else:
        window = gsrc[reset_at:reset_at + 300]
        if "scene_score = 1.0" not in window:
            failures.append(
                "the reset path no longer sets scene_score = 1.0 - update this "
                "guard, it was written for that behaviour")
    if "guide.reset" not in code:
        failures.append(
            "the HUD line does not report the reset flag, so a reset frame "
            "still prints a bare 1.000 that reads as `the picture is moving`")

    if failures:
        print("FAIL: the frame-delta reading misleads again")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the HUD reports the frame delta under its own name, and a "
          "reset frame says it is a reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
