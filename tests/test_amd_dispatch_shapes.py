"""The staging sets the engine reports must be OURS, and this names which is which.

WHY THIS EXISTS
---------------
On 19.09 the #3 thread was told, as a diagnosis, that

  "the engine appears to be picking up a second FSR dispatch on your machine,
   and switching between it and ours. When it picks that one, it reads a
   surface we never wrote, which is exactly a zero mean."

That was WRONG, and the two staging sets it was based on are both produced by
this program. Worked out from our own dispatch descriptors:

  dispatch A  DispatchNet      renderSize=WORK  upscaleSize=WORK  motion=motion
  dispatch B  DispatchUpscale  renderSize=WORK  upscaleSize=OUT   motion=nullptr

and the sets the reporter's engine logged, side by side:

  colour WORK    | motion WORK dxgi34 | depth WORK | residual ON   <- A
  colour OUT     | motion 0x0  dxgi -1 | depth WORK | residual OFF <- B

Colour is the upscaleSize, depth is the renderSize, motion is the motionVectors
resource - all three match, in both sets. B is not a foreign dispatch; it is the
one that steps work resolution up to the display, and it carries no motion
vectors on purpose so the runtime knows not to process it ("ignoring upscaler
dispatches without motion vectors ... following the one with motion vectors" -
the rule is in the runtime's own log, quoted in that same comment).

So the "second context" was never the explanation, and the engine's own
accounting says the dispatches were being taken: 1.00 per frame.

WHAT THIS LOCKS
---------------
The two descriptors keep the shapes that make the staging sets explainable:
A is 1:1 WITH motion vectors, B goes work->out WITHOUT them. Change either and
the engine's log stops being readable by the table above, which is the tool that
settles a black-frame report.

Run:  runtime\\python.exe tests/test_amd_dispatch_shapes.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FSR = BASE / "native" / "amd" / "amd_fsr.cpp"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def body_of(code: str, signature: str) -> str:
    at = code.find(signature)
    if at < 0:
        return ""
    depth, started = 0, False
    for i in range(at, len(code)):
        c = code[i]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return code[at:i + 1]
    return code[at:]


def main() -> int:
    failures: list[str] = []
    if not FSR.exists():
        print("FAIL: amd_fsr.cpp is missing")
        return 1
    code = strip_comments(FSR.read_text(encoding="utf-8", errors="replace"))

    # --- dispatch A: work -> work, WITH motion vectors ----------------------
    net = body_of(code, "::DispatchNet(")
    if not net:
        failures.append("DispatchNet could not be read - the shapes cannot be "
                        "checked and a black-frame report cannot be explained")
    else:
        if not re.search(r"d\.motionVectors\s*=\s*ffxApiGetResourceDX12\(\s*motion\b", net):
            failures.append(
                "dispatch A does not bind the motion resource - the runtime "
                "follows the dispatch WITH motion vectors, so without them it "
                "processes nothing")
        if not re.search(r"d\.upscaleSize\s*=\s*\{\s*work_w_,\s*work_h_\s*\}", net):
            failures.append(
                "dispatch A is no longer 1:1 (upscaleSize != work) - the network "
                "must run on a frame that was NOT resampled")

    # --- dispatch B: work -> out, WITHOUT motion vectors --------------------
    up = body_of(code, "::DispatchUpscale(")
    if not up:
        failures.append("DispatchUpscale could not be read")
    else:
        # B's vectors come from its own `motion` parameter, which the bridge
        # passes as null unless NS_AMD_UPSCALE_MV=1 (that arm and its default
        # are locked by test_amd_upscale_mv_knob.py). What must never happen is
        # B binding A's surface or any fixed resource: then the runtime would
        # follow it on the shipped path and charge the network for the upscale.
        mv = re.search(r"d\.motionVectors\s*=\s*ffxApiGetResourceDX12\(\s*(\w+)", up)
        if not mv or mv.group(1) not in ("nullptr", "motion"):
            failures.append(
                "dispatch B binds a motion resource other than its nullable "
                "parameter - the runtime would follow it and charge the network "
                "for the upscale, which is the opposite of the two-dispatch design")
        if not re.search(r"d\.renderSize\s*=\s*\{\s*work_w_,\s*work_h_\s*\}", up) or \
           not re.search(r"d\.upscaleSize\s*=\s*\{\s*out_w_,\s*out_h_\s*\}", up):
            failures.append(
                "dispatch B is no longer work -> out - the staging table that "
                "identifies the engine's surfaces no longer matches")

    if failures:
        print("FAIL: the dispatch shapes no longer explain the engine's staging")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: A is work->work with motion vectors, B is work->out without them - "
          "the two staging sets the engine reports are both ours")
    return 0


if __name__ == "__main__":
    sys.exit(main())
