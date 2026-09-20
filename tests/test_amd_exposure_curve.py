"""The AMD path's exposure curve can bring bright content DOWN, not only up.

WHY THIS EXISTS
---------------
The host computes an exposure from the captured frame's own luminance, and the
AMD path had none at all: the engine was handed a 1x1 texture written once at
surface creation with the value 1.0. That is a fixed exposure, and a reporter
reports what it costs - "The good frames are over-exposed too - sky and grass
wash out to white - so 1.000 may still be high for this content."

The obvious fix - hand it the NGX curve's value - does not work, and the numbers
say so. That curve runs inside min=1.00 .. max=1.10, so it can only ever RAISE
exposure; evaluated on the reporter's own frame means (0.345 / 0.385 / 0.439) it
returns 1.0089 / 1.0007 / 1.0000. A curve whose floor IS the problem value cannot
answer the complaint, however often it is evaluated.

So this path gets its own curve, shaped like the working upstream fork's
(`1.0 + (0.35 - avg) * 2.0`, clamped 0.5 .. 2.0). It is symmetric about 0.35:
dark content lifts, bright content comes DOWN. That is the direction the reporter
needs and the direction the shared curve cannot go.

WHAT THIS LOCKS
---------------
1. The separate curve exists and is actually the value handed to the engine -
   not merely defined.
2. It is REVERSIBLE: NS_AMD_PW=0 falls back to the host's own value, so a bad
   shape on real hardware is one environment variable from the old behaviour
   rather than a rebuild. That matters because this curve cannot be measured
   here - there is no AMD card in this machine.
3. It is SMOOTHED over time. A raw per-frame mapping would step the brightness
   whenever the scene changes, which is a visible flicker of its own - the exact
   class of bug this program has spent a week on.
4. It refuses a bad input: nothing measured yet falls back, and the clamp keeps
   the value inside the range the fork uses, so the engine cannot be handed a
   zero (which normalises the picture to black).

The curve's arithmetic is checked in python here rather than in C++, because the
shape is a formula and the formula is what has to be right.

Run:  runtime\\python.exe tests/test_amd_exposure_curve.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def fork_like(avg: float) -> float:
    """The shape this path is supposed to implement."""
    t = 1.0 + (0.35 - avg) * 2.0
    return min(2.0, max(0.5, t))


def main() -> int:
    failures: list[str] = []
    if not BRIDGE.exists():
        print("FAIL: the AMD bridge is missing")
        return 1
    raw = BRIDGE.read_text(encoding="utf-8", errors="replace")
    code = strip_comments(raw)

    # --- 1. the curve exists and is the value handed over -------------------
    if "AmdExposureForEngine" not in code:
        failures.append(
            "AmdExposureForEngine is gone - this path is back to the host's curve, "
            "whose floor of 1.00 cannot bring bright content down (the complaint)")
    elif not re.search(r"AmdUpdateExposure\(\s*AmdExposureForEngine\(\s*\)\s*\)", code):
        failures.append(
            "AmdExposureForEngine is defined but not the value passed to "
            "AmdUpdateExposure - the curve would be dead code")
    else:
        if not re.search(r"target\s*=\s*1\.0f\s*\+\s*\(\s*centre\s*-\s*avg\s*\)\s*\*\s*2\.0f", code):
            failures.append(
                "the curve no longer uses (centre - avg) * 2.0 - the shape is what "
                "makes bright content come down, and it is the fork's own formula")
        if not re.search(r"target\s*<\s*0\.5f\s*\)\s*\?\s*0\.5f", code) or \
           not re.search(r"target\s*>\s*2\.0f\s*\)\s*\?\s*2\.0f", code):
            failures.append(
                "the 0.5 .. 2.0 clamp is gone - the fork clamps there, and without "
                "it a dark frame or a NaN can drive the engine's exposure anywhere")
        # The centre must be a default, not a fitted number. Fitting it to one
        # reporter's frames would flatten exactly the content he complains about
        # and brighten everything else, and the judgement cannot be made from a
        # log - so it stays upstream's constant and stays adjustable.
        if "NS_AMD_PW_CENTER" not in code:
            failures.append(
                "the curve's centre is not adjustable - then the one number in it "
                "that is a JUDGEMENT cannot be changed on real hardware without a "
                "rebuild, and it was deliberately not fitted to a log")
        elif not re.search(r"return\s+0\.35f", code):
            failures.append(
                "the centre no longer defaults to the fork's 0.35 - a value fitted "
                "here would be a guess from one reporter's numbers")

    # --- 2. it is reversible ------------------------------------------------
    # The check is on the READ and the FALLBACK, not on the name: the string
    # NS_AMD_PW also appears in the log line this function prints, so a search
    # for the name alone passed even with the variable renamed (found by
    # negative control).
    if not re.search(r'GetEnvironmentVariableA\(\s*"NS_AMD_PW"\s*,\s*v\s*,\s*sizeof\(v\)\)', code):
        failures.append(
            "NS_AMD_PW is not read (renamed, or not read at all) - a bad curve on "
            "real hardware would then need a rebuild to escape, on a branch nobody "
            "here can test")
    elif not re.search(r"if\s*\(\s*!\s*enabled\s*\)\s*return\s+g_pw_exposure", code):
        failures.append(
            "NS_AMD_PW does not fall back to the host's own value - the escape "
            "hatch is named but does nothing")

    # --- 3. it is smoothed --------------------------------------------------
    if not re.search(r"smoothed\s*\+=\s*\(\s*target\s*-\s*smoothed\s*\)\s*\*\s*a", code):
        failures.append(
            "the curve is not smoothed - a per-frame mapping steps the brightness "
            "on a scene change, which is a flicker of its own making")

    # --- 4. the arithmetic itself -------------------------------------------
    # The shape has to do the two things the reasoning claims: lift dark content
    # and bring bright content DOWN. A curve that failed either would be a
    # different bug wearing this one's name.
    dark = fork_like(0.10)
    mid = fork_like(0.35)
    lit = fork_like(0.45)
    very_lit = fork_like(0.60)
    if not dark > 1.0:
        failures.append(
            f"the shape does not lift dark content (0.10 -> {dark:.2f}) - dark "
            f"scenes stay underexposed, which is the failure the curve exists for")
    if abs(mid - 1.0) > 0.001:
        failures.append(
            f"the shape is not unity at its own centre (0.35 -> {mid:.3f}) - the "
            f"reporter's own bright-but-not-extreme content would be shifted for "
            f"no reason")
    if not lit < 1.0:
        failures.append(
            f"the shape does NOT bring bright content down (0.45 -> {lit:.2f}) - "
            f"this is the whole point: the reporter's frames measure 0.345-0.439 "
            f"and he says 1.000 is too high for them")
    if not very_lit < lit:
        failures.append(
            f"the shape is not monotonic downwards above its centre "
            f"(0.45 -> {lit:.2f}, 0.60 -> {very_lit:.2f})")
    # And it must differ from the shared curve where it matters: on content well
    # above the centre - the bright frames the complaint is about. The reporter's
    # own darkest frame (0.344) is BELOW the fork's centre, so the curve lifts
    # there exactly as the fork does; the difference is on his brightest (0.439),
    # and that is the number his sentence is about ("sky and grass wash out").
    def host_curve(avg, dark_t=0.10, lit_t=0.40, mn=1.00, mx=1.10):
        t = (avg - dark_t) / (lit_t - dark_t)
        t = min(1.0, max(0.0, t))
        return mx - (mx - mn) * (t * t * (3.0 - 2.0 * t))
    for avg in (0.42, 0.439, 0.50):
        if not fork_like(avg) < host_curve(avg) - 0.05:
            failures.append(
                f"at mean {avg} this curve ({fork_like(avg):.3f}) is not "
                f"meaningfully below the host's ({host_curve(avg):.4f}) - bright "
                f"content, which is the complaint, would not change")
    # And it must NOT be uniformly darker: dark content has to keep its lift, or
    # this trades the reported bug for the underexposure the host's curve exists
    # to prevent.
    if fork_like(0.10) < host_curve(0.10):
        failures.append(
            f"dark content is darker than the host's curve made it "
            f"({fork_like(0.10):.3f} vs {host_curve(0.10):.4f}) - the lift dark "
            f"scenes need would be gone")

    if failures:
        print("FAIL: the AMD exposure curve does not do what it claims")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the AMD path has its own exposure curve - symmetric about 0.35, "
          "reversible with NS_AMD_PW=0, smoothed, clamped, and measurably below "
          "the host's curve on the reporter's own frame means")
    return 0


if __name__ == "__main__":
    sys.exit(main())
