"""The tripwire reads THREE surfaces of one frame, and the first is the capture.

WHY THIS EXISTS
---------------
For the whole history of this tracker the probe measured two surfaces, `fsr_in`
and `net` - both on the far side of the conversion. When both read zero the
report could not say which side lost the picture, because the two zeros are ONE
fact counted twice: `fsr_in` is what the conversion wrote and `net` is what the
dispatch read from it. The surface the chain actually starts from - the captured
frame in `v.color.tex` - was never measured at all, so a dead capture and a dead
conversion read identically.

That is the question the black-frame hunt kept asking and could not answer:
"was the frame there and we lost it, or was there nothing to lose?". Three
numbers from the same frame answer it in one line:

  frame == 0              -> the capture is empty; the fault is upstream
  frame > 0, fsr_in == 0  -> the capture was alive and OUR conversion lost it
  fsr_in > 0, net == 0    -> the conversion delivered, the dispatch did not

WHAT THIS LOCKS
---------------
1. All three surfaces are measured, over the SAME spec-driven reader.
2. `v.color.tex` is read as RGBA8 at the frame's own size - the dispatch
   surfaces are FP16 at the network's size, and reading one as the other
   reports a plausible number that means nothing.
3. Each of the three verdicts still names a DIFFERENT next action, so the line
   cannot collapse back into "something is black".

Run:  runtime\\python.exe tests/test_amd_tripwire.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def main() -> int:
    failures: list[str] = []
    if not BRIDGE.exists():
        print("FAIL: amd_bridge.inl is missing")
        return 1
    raw = BRIDGE.read_text(encoding="utf-8", errors="replace")
    code = strip_comments(raw)

    # --- 1. the third surface is measured -----------------------------------
    if not re.search(r"AmdMeasureSurface\(\s*\n?\s*v\.color\.tex", code):
        failures.append(
            "the probe does not measure v.color.tex - the captured frame is the "
            "surface the chain starts from, and without it a zero at fsr_in and "
            "a zero at net cannot say which side lost the picture")
    if "probe_frame_mean" not in code:
        failures.append(
            "the capture's mean is not kept in the state, so the tripwire has "
            "nothing to compare")
    if "got_frame" not in code:
        failures.append(
            "the third measurement's result is not tracked - a failed readback "
            "would be read as a zero surface")

    # --- 2. the reader is spec-driven, and the capture has its own spec -----
    if "AmdSurfaceSpec" not in code:
        failures.append(
            "the reader is not spec-driven - it cannot serve both an FP16 "
            "network-sized surface and an RGBA8 full-resolution one")
    if "DXGI_FORMAT_R8G8B8A8_UNORM" not in code:
        failures.append(
            "no RGBA8 spec exists - v.color.tex is 8-bit, and copying it under "
            "the FP16 footprint describes a different resource")
    if "is_half" not in code:
        failures.append(
            "the reader does not branch on half-vs-8-bit, so one of the two "
            "formats is decoded with the other's arithmetic")
    # The 8-bit branch must actually read bytes, not halves.
    if not re.search(r"const uint8_t \*px = row \+ \(size_t\)x \* 4", code):
        failures.append(
            "there is no 8-bit pixel reader - the capture would be decoded as "
            "half floats and report a meaningless mean")

    # --- 3. the verdicts stay distinguishable -------------------------------
    body = code[code.find("AmdEngineHealth"):]
    verdicts = [
        "THE CAPTURE IS EMPTY",
        "the CAPTURE was alive and the CONVERSION lost it",
        "the conversion delivered and the DISPATCH did not",
    ]
    for v in verdicts:
        if v not in body:
            failures.append(
                f"the tripwire no longer distinguishes this case: '{v}' - the "
                f"three zeros need three different next actions")
    # And the numbers are printed whether or not a verdict fires.
    if "the tripwire, same frame" not in body:
        failures.append("the tripwire line is gone from the report")

    if failures:
        print("FAIL: the tripwire cannot name the failing side")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the probe measures the captured frame, the converted input and "
          "the dispatch output of one frame, and each outcome names a different "
          "side to look at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
