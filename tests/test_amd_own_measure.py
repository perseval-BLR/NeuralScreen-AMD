"""The host-side probe: does OUR OWN surface hold a frame?

The engine reports `auto-exposure: encoded mean %.3f` about what it RECEIVED.
We had no matching number about what we SENT, and that gap is why four
black-screen reports in a row could not be split into their two very different
causes:

    our dispatch wrote nothing into `net`        -> the loss is on OUR side
    `net` holds a real frame, the engine reads 0 -> the loss is in what the
                                                    engine reads

Both numbers must come out of ONE definition or they cannot be compared, so the
probe copies the engine's: the mean over the three COLOUR channels only. Alpha is
excluded on purpose - the probe this was lifted from counted it at first, and a
frame of pure black RGB with alpha 1 passed as "not black".

Run:  runtime\\python.exe tests/test_amd_own_measure.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"



def strip_comments(text: str) -> str:
    """The code without its comments.

    Every earlier guard in this project that searched raw source was fooled by
    text that is not code - here by the comment that explains WHY the call is
    not EndCommands(): the word is in the explanation, so a search for it found
    the warning and reported the bug it warns about.
    """
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("//"):
            continue
        code = line.split("//")[0] if "//" in line else line
        out.append(code)
    return "\n".join(out)

def function_body(text: str, signature: str) -> str:
    """The body of one function, from its DEFINITION to its closing brace."""
    at = text.find(signature)
    if at < 0:
        return ""
    rest = text[at + len(signature):]
    brace = rest.find("{")
    if brace < 0:
        return ""
    depth = 0
    started = False
    for i, ch in enumerate(rest[brace:], start=brace):
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return rest[:i + 1]
    return ""


def main() -> int:
    failures = []
    if not BRIDGE.exists():
        print("FAIL: amd_bridge.inl not found")
        return 1
    text = BRIDGE.read_text(encoding="utf-8", errors="replace")

    body = function_body(strip_comments(text), "static bool AmdMeasureSurface(")
    if not body:
        failures.append("AmdMeasureSurface is gone - nothing measures our own surface")
    else:
        # 1. The metric must match the engine's: colour channels only.
        if "for (UINT c = 0; c < 3; ++c)" not in body:
            failures.append(
                "the mean is not taken over the three colour channels - alpha "
                "would be counted, and a black frame with alpha 1 reads as "
                "'not black' (measured on the probe: it did exactly that)")

        # 2. It must submit ITS OWN list. EndCommands() closes and submits
        #    h.list, the frame's list, so a copy recorded here would never run
        #    and the readback would report the buffer as it was ALLOCATED
        #    (zeros) - a black surface that was never sampled.
        if "EndCommands()" in body:
            failures.append(
                "the probe submits through EndCommands(), which submits h.list - "
                "the frame's own command list. The copy recorded here would "
                "never execute and the readback would report zeros, calling a "
                "surface black that was never read")
        if "ExecuteCommandLists" not in body:
            failures.append("the probe never submits its own copy")
        if "Signal" not in body:
            failures.append(
                "the probe does not signal a fence of its own, so the wait "
                "below can be satisfied by an older signal and read the buffer "
                "before the copy lands")

        # 3. A failure must not invent a value.
        if "return false" not in body:
            failures.append("the probe has no failure path - it would report a mean "
                            "for a readback that never happened")

        # 4. Half-float decoding must be present and must handle zero.
        if "AmdMeanFromHalf" not in body:
            failures.append("the half-float decode is not used - RGBA16F cannot be "
                            "read as bytes")
    decode = function_body(strip_comments(text), "static float AmdMeanFromHalf(")
    if not decode:
        failures.append("AmdMeanFromHalf is gone")
    else:
        if "1024.0f" not in decode or "5.9604645e-8f" not in decode:
            failures.append("the half-float decode lost its normal or subnormal path")

    # 4b. THE ORDER IS THE MEASUREMENT. The probe reads `fsr_in` and `net` by
    # recording a copy and waiting for it - so if it runs while the dispatches
    # are still only RECORDED, it reads every surface as it was before the
    # frame. Measured: five readbacks on a live 7900 XTX all read 0.0000/0.0000
    # while the capture read 0.334 and the engine ran 1.00 dispatch per frame.
    # The reporter who sent them said, unprompted, that the probe should be
    # confirmed to read the intended resource - he was right.
    code = strip_comments(text)
    call_at = code.find("AmdMeasureSurface(\n            g_amd.fsr_in")
    submit_at = code.find("const UINT64 fence = EndCommands();")
    if call_at < 0 or submit_at < 0:
        failures.append("the probe call or the frame submission is missing")
    elif call_at < submit_at:
        failures.append(
            "the probe runs BEFORE the frame's submission - it would read the "
            "surfaces as they were before the dispatches executed and report "
            "zeros on a working frame (this is what happened on a 7900 XTX)")

    # 5. It must be CALLED, not merely defined: an unused probe is a comment.
    if "g_amd.fsr_in, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, &conv" not in text or \
       "g_amd.net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, &netm" not in text:
        failures.append(
            "the probe is defined but never called on both surfaces - only the "
            "pair (input AND dispatch output) can name the side")

    # 6. The state handed to the probe must be the state the resource IS IN.
    #
    # The frame leaves the two surfaces in different states - fsr_in readable,
    # net writable - and the probe used to declare both as readable. A barrier
    # that names the wrong "from" tells D3D12 about a transition that never
    # happened, which is the same class of lie the final pass warns about for
    # net/up_out. The states are checked by name because a constant there is a
    # claim about a resource the probe does not own.
    if "D3D12_RESOURCE_STATES state" not in code:
        failures.append(
            "AmdMeasureSurface takes no state parameter - it declares a fixed "
            "state for both surfaces, and the two are left in different ones")
    else:
        if "Transition(src, state," not in code:
            failures.append(
                "the probe's entry barrier does not use the state it was given - "
                "it would declare a transition that never happened")
        if "Transition(src, D3D12_RESOURCE_STATE_COPY_SOURCE,\n                                           state)" not in code:
            failures.append(
                "the probe does not return the resource to the state it found it "
                "in - the next frame's dispatches would be told a state the "
                "resource is not in")
        if "g_amd.net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, &netm" not in text:
            failures.append(
                "net is measured as if it were readable, but the final pass "
                "leaves it in UNORDERED_ACCESS for the next dispatch")

    # 7. And its result must reach the log, or the report carries no evidence.
    if "what WE hand over" not in text:
        failures.append("the measured pair is never printed - the report would "
                        "still lack the number")
    if "our own measure, last taken" not in text:
        failures.append("the periodic summary does not repeat the measurement")

    if failures:
        print("FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: the host measures its own surface with the engine's own metric "
          "(colour channels only), submits its own copy, signals its own fence, "
          "reports both surfaces, and has a failure path that invents nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
