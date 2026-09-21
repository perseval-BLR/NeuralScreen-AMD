"""The probe measures the FSR upscale's own output, and only when it exists.

WHY THIS EXISTS
---------------
Every other link in the frame had a number before this one did: the captured
frame, the conversion's output, the network's surface, and the surface the
present copies from. The link between the network and the composite - the FSR
upscale's output, `up_out` - was the last one without a measurement, and it is
exactly the one a reporter's bisect pointed at.

The bisect: Work Scale to 1:1, one variable, no rebuild. The alternation stopped
at 1:1 and continued below it. Measured from the reporter's own recording, not
taken on his word:

    1102x756 work buffer, output 1698x1164 : 231 of 231 processed frames
                                            alternating (mean |lag1| 115.5)
    1816x1221 work buffer, output 1816x1221: 2 of 329 (mean |lag1| 0.52)

At 1:1 `upscaling_` is false (amd_fsr.cpp), so `up_out` is never created and the
final pass reads `net` instead. The one thing that changed between the two runs
is the EXISTENCE of this surface. A diagnosis that cannot read it cannot rule it
in or out, and the two remaining candidates - the FSR pass and the composite -
are separated by nothing else.

WHAT THIS LOCKS
---------------
1. `up_out` is passed to AmdMeasureSurface - the surface, not a proxy for it.
2. Its declared state is UNORDERED_ACCESS: the block closing the frame returns
   it to what dispatch B declares as its output, so that is where it really is.
   Naming NON_PIXEL_SHADER_RESOURCE would record a transition from a state the
   resource is not in - the class of lie test_amd_state_bookkeeping.py exists
   for, and the defect that test was written after finding.
3. It is measured ONLY when it exists: guarded by `Upscaling()` and a non-null
   `up_out`. At 1:1 the spec would describe a resource that is not there.
4. The spec is built from the size the resource is CREATED at (`out_w`/`out_h`,
   the same pair the creation site passes to AmdMakeTex), not from the work size
   - declaring a smaller footprint than the resource is the mismatched copy that
   removed the device in v0.3.13.
5. The value reaches the log, in both the per-frame line and the health summary.
   A number nothing prints is not an instrument.

Run:  runtime\\python.exe tests/test_amd_probe_upscale_output.py
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
        print("FAIL: the AMD bridge is missing")
        return 1
    raw = BRIDGE.read_text(encoding="utf-8", errors="replace")
    code = strip_comments(raw)

    # --- 1. the surface itself is measured ---------------------------------
    if "probe_up_mean" not in code:
        failures.append(
            "the probe does not measure the FSR upscale's output - it is the one "
            "link between the network and the composite with no number, and a "
            "reporter's 1:1 bisect pointed straight at it")
    else:
        # The call must be on `g_amd.up_out` - not on `net` under another name.
        call = re.search(r"AmdMeasureSurface\(\s*g_amd\.up_out\s*,\s*"
                         r"(D3D12_RESOURCE_STATE_\w+)", code)
        if not call:
            failures.append(
                "g_amd.up_out is not passed to AmdMeasureSurface - the field "
                "exists but nothing measures the upscale's own output")
        else:
            # --- 2. the state is the one the frame left it in --------------
            # The block closing the frame transitions up_out back to
            # UNORDERED_ACCESS for the next dispatch (see the round-trip comment
            # there), so that is where the probe finds it.
            if call.group(1) != "D3D12_RESOURCE_STATE_UNORDERED_ACCESS":
                failures.append(
                    f"up_out is measured in {call.group(1)}, which is not the "
                    "state the frame leaves it in - the closing block returns it "
                    "to UNORDERED_ACCESS for the next dispatch, so any other "
                    "state is a barrier that lies")
            closing = code
            if not re.search(r"Transition\(\s*g_amd\.up_out,\s*"
                             r"D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,\s*"
                             r"D3D12_RESOURCE_STATE_UNORDERED_ACCESS", closing):
                failures.append(
                    "the frame no longer returns up_out to UNORDERED_ACCESS - "
                    "this test's anchor is stale, re-derive the probe's state "
                    "from the code before trusting the result")

        # --- 3. only when the surface exists -------------------------------
        # At 1:1 there is no upscale, so there is no up_out, and a spec built
        # for it would describe a resource that is not there.
        if not re.search(r"g_amd\.fsr\.Upscaling\(\)\s*&&\s*g_amd\.up_out\s*!=\s*nullptr"
                         r"\s*&&\s*AmdMeasureSurface\(", code):
            failures.append(
                "up_out is measured without checking that it exists - at 1:1 "
                "the upscale is skipped and the resource is never created, so "
                "the spec would be an unverified claim about a null resource")

        # --- 4. the spec matches the creation size -------------------------
        if not re.search(r"kUpSpec\{\s*g_amd\.out_w\s*,\s*g_amd\.out_h\s*,", code):
            failures.append(
                "the upscale spec is not built from out_w/out_h - those are the "
                "size the resource is created at (AmdMakeTex(out_w, out_h, ...)); "
                "anything else declares a smaller surface than the resource and "
                "that mismatched copy removes the device")

        # --- 5. it reaches the log -----------------------------------------
        # Two lines, checked separately, because they say different things and
        # one can go missing on its own: the per-frame line is what a
        # frame-by-frame alternation is read from (the probe at NS_AMD_PROBE_EACH
        # cadence prints it for every frame, and an alternation is only visible
        # between consecutive frames), while the summary line carries the last
        # value into a log a reporter sends after playing normally.
        if "the upscale's own output, this frame" not in raw:
            failures.append(
                "probe_up_mean has no per-frame log line - the value cannot be "
                "read across consecutive frames, which is the only way a "
                "period-2 alternation is visible")
        if "the upscale's own output: mean" not in raw:
            failures.append(
                "probe_up_mean has no health-summary line - a reporter who plays "
                "normally would send a log with no upscale number in it at all")

    if failures:
        print("FAIL: the upscale's output cannot be read, or is read wrongly")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the probe measures the FSR upscale's own output in the state the "
          "frame leaves it in, only when it exists, at the size it was created "
          "at, and reports it in both the per-frame line and the health summary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
