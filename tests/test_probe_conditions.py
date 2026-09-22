"""The probe prints the CONDITION its per-frame value was taken under.

WHY THIS EXISTS
---------------
The per-frame probe answers exactly one question: does a surface alternate
frame to frame. Its line printed the value and nothing else - not whether the
frame was a history-reset frame, not whether the upscale existed in the path at
all. Both of those are legitimate reasons for a mean to move, so a reader could
not tell a real alternation from a reset, and the defect hunt that relied on
those numbers was reading them blind on that axis.

That is the same class of defect the probe has already been fixed for twice: in
v0.3.18 it stopped reporting a mean it had never measured (a copy that did not
execute read as a perfectly black surface), and the failure counter it kept was
never printed, so a partial failure was invisible. An instrument that answers
with one hand while withholding the condition is the third instance.

The `reset` flag is decided per frame in the host (`fh.reset`, `frame == 0`,
a capture pause) and reached the log NOWHERE - `d.reset` is written into both
FFI dispatches and the log has no line about it. In a 136-frame run with a
window resize in the middle, two engine rebuilds, and a capture stall, the
reader had no way to see any of them.

WHAT THIS LOCKS
---------------
1. The per-frame line carries `reset=` and `upscale=`, from the live condition
   rather than a constant.
2. The per-frame line's condition is the SAME `reset` the dispatches were
   handed this frame - not a cached or recomputed value.
3. The health summary carries the session totals: how many probed passes were
   reset frames, how many had the upscale in the path.
4. Those totals are printed only when the probe actually ran (no empty
   "0 of 0" line on a session where the probe never fired).
5. The counters are incremented inside the probe block, where the measurement
   is - not on a path that skips it.

Run:  runtime\\python.exe tests/test_probe_conditions.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def main() -> int:
    failures: list[str] = []
    if not BRIDGE.is_file():
        print("FAIL: native/amd/amd_bridge.inl is missing")
        return 1
    src = BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- 1/2. the per-frame line carries the live condition ----------------
    frame_line = re.search(
        r'Log\("\[amd\] the presented surface, this frame: mean %\.4f \(native anchor "\s*'
        r'"%\.4f\)([^;]*?);',
        src, re.S)
    if not frame_line:
        failures.append("the per-frame presented-surface line is gone")
    else:
        body = frame_line.group(1)
        for needle, why in (("reset=%d", "the reset flag"), ("upscale=%s", "the upscale state")):
            if needle not in body:
                failures.append(f"the per-frame line no longer prints {why}")
        if "g_amd.probe_last_reset" in body:
            failures.append(
                "the per-frame line reads a cached probe_last_reset instead of "
                "this frame's own flag - a cached value describes a different frame")
        # It must pass `reset` (the function argument) or a clearly live value.
        if not re.search(r"\breset\b", body.split('"')[0] + body):
            failures.append("the per-frame line does not print this frame's reset")
    # The upscale state must be the live expression, not a stored bool.
    up_live = re.search(r'\(g_amd\.fsr\.Upscaling\(\) && g_amd\.up_out != nullptr\)\s*\?\s*"on"\s*:\s*"off"',
                        src)
    if not up_live:
        failures.append(
            "the upscale condition is not the live Upscaling() && up_out check - "
            "a stored flag would go stale exactly when the geometry changes")

    # --- 5. counters incremented inside the probe block --------------------
    inc = src.find("++g_amd.probe_reset_frames")
    frames_inc = src.find("++g_amd.probe_frames")
    if inc < 0:
        failures.append("probe_reset_frames is never incremented")
    if frames_inc < 0:
        failures.append("probe_frames is never incremented")
    if inc >= 0 and frames_inc >= 0 and abs(inc - frames_inc) > 400:
        failures.append(
            "the reset counter is incremented far from the frame counter - it "
            "must count the same passes the probe measured")
    if "if (reset) ++g_amd.probe_reset_frames;" not in src:
        failures.append(
            "the reset counter is not guarded by `if (reset)` - it would count "
            "every pass as a reset")
    if "if (g_amd.fsr.Upscaling() && g_amd.up_out != nullptr) ++g_amd.probe_upscale_frames;" not in src:
        failures.append(
            "the upscale counter uses a different condition than the line it "
            "explains")

    # --- 3/4. the health summary carries the totals, guarded --------------
    health = re.search(r"probe conditions:.*?\);", src, re.S)
    if not health:
        failures.append("the health summary has no probe-conditions line")
    else:
        h = health.group(0)
        for needle, why in (("probe_reset_frames", "the reset total"),
                            ("probe_frames", "the pass total"),
                            ("probe_upscale_frames", "the upscale total")):
            if needle not in h:
                failures.append(f"the health line does not print {why}")
        if "if (g_amd.probe_frames != 0)" not in src[:health.start()][-260:]:
            failures.append(
                "the health line is not guarded by probe_frames != 0 - a session "
                "where the probe never ran would print '0 of 0'")

    # --- the fields themselves exist --------------------------------------
    for field in ("probe_reset_frames", "probe_upscale_frames"):
        if not re.search(rf"uint64_t {field}\s*=\s*0;", src):
            failures.append(f"the state has no {field} field")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: the probe prints the condition its per-frame value was taken "
          "under, counts those conditions for the session, and prints the "
          "totals only when it ran")
    return 0


if __name__ == "__main__":
    sys.exit(main())
