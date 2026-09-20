"""The probe can be made to measure every frame, and it measures the presented one.

WHY THIS EXISTS
---------------
The AMD probe reads three surfaces back to the CPU - the captured frame, the
conversion's output and the network's surface - and it exists to answer "where
was the picture lost". It ran on every 300th frame, because a readback is a full
GPU->CPU sync.

That cadence put it out of reach of the reports it was built for. A reporter's
package alternates between a correct picture and a blown-white one at his present
period (133 ms per state), and its three launches ran 51, 73 and 216 frames - so
`(fsr_frames % 300) == 1` never fired once, and his log carries no probe numbers
at all. The instrument was there; the report that needed it could not reach it.

TWO THINGS THIS LOCKS
---------------------
1. NS_AMD_PROBE_EACH=1 makes the probe run every frame. It stays OFF by default:
   the sync is real, and the probe is for a diagnosis run, not for playing.
2. The probe measures the PRESENTED surface too (`v.output`, what PresentFrame
   copies into the backbuffer), not only the composite's inputs. A per-frame
   alternation at the presentation point is exactly the question a frame-by-frame
   flicker asks, and none of the three older surfaces is that point - a steady
   `net` with an alternating `v.output` would be invisible without this.

A test, not a description: the checks are on the properties that make the
instrument usable, and each one fails on the code as it was.
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

    # --- 1. the cadence can be overridden, and stays off by default ---------
    if "NS_AMD_PROBE_EACH" not in code:
        failures.append(
            "NS_AMD_PROBE_EACH is gone - the probe is back to every 300th frame, "
            "and a diagnosis run shorter than 300 frames carries no numbers at all "
            "(that is how a real report became unreadable)")
    else:
        if not re.search(r'GetEnvironmentVariableA\(\s*"NS_AMD_PROBE_EACH"', code):
            failures.append("NS_AMD_PROBE_EACH is not read from the environment")
        # The default must remain the 300-frame cadence: an unconditional
        # per-frame readback would cost a sync every frame for everyone.
        if not re.search(r"probe_each\s*\|\|\s*\(g_amd\.fsr_frames\s*%\s*300\)\s*==\s*1", code):
            failures.append(
                "the 300-frame cadence is no longer the fallback - either the "
                "probe now syncs every frame for everyone, or the override does "
                "not compose with it")
        # ...and it must default to OFF.
        if re.search(r'NS_AMD_PROBE_EACH",\s*v,\s*sizeof\(v\)\)\s*>\s*0\s*&&\s*v\[0\]\s*!=\s*\'0\'', code):
            failures.append(
                "NS_AMD_PROBE_EACH defaults to ON - any value other than an empty "
                "string enables it, which is not a default anyone asked for")

    # --- 2. the presented surface is measured -------------------------------
    if "probe_out_mean" not in code:
        failures.append(
            "the probe does not measure the presented surface - a frame that "
            "alternates at the presentation point cannot be told from one that "
            "alternates before the composite")
    else:
        if not re.search(r"AmdMeasureSurface\(\s*v\.output\s*,", code):
            failures.append(
                "v.output is not passed to AmdMeasureSurface - the field exists "
                "but nothing measures the surface the present copies from")
        # Its state must be the one the frame left it in: the composite writes
        # it as a UAV and it stays UNORDERED_ACCESS at rest. Naming another
        # state declares a transition that never happened.
        if not re.search(r"AmdMeasureSurface\(\s*v\.output\s*,\s*"
                         r"D3D12_RESOURCE_STATE_UNORDERED_ACCESS", code):
            failures.append(
                "v.output is measured in a state it is not in - the composite "
                "leaves it UNORDERED_ACCESS at rest, and a wrong state is a "
                "barrier that lies")
        # A number that nothing prints is not an instrument.
        if "the presented surface, this frame" not in raw and \
           "the presented surface: mean" not in raw:
            failures.append(
                "probe_out_mean is measured but never logged - the value cannot "
                "reach the report it is meant to make readable")

    if failures:
        print("FAIL: the probe cannot answer a per-frame alternation")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the probe can run every frame on request while defaulting to the "
          "300-frame cadence, and it measures and reports the presented surface")
    return 0


if __name__ == "__main__":
    sys.exit(main())
