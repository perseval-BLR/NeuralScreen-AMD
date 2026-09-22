"""The upscale dispatch's exposure is one variable, off by default, and named.

WHY THIS EXISTS
---------------
The fifth probe run (22.09) put the flicker's mechanism inside dispatch B: its
input - A's output - is flat at 0.10-0.42 on both the bright and the dark
frames, while B's own output sits at ~64,000 of a 65,504 half-float ceiling on
every bright frame (58 of 58) and near zero on the dark ones (0 of 58).

A and B fill the same descriptor apart from one field, so that field is the
natural first test: A binds the 1x1 exposure, B has never bound it. The FFX API
documents the field as optional and B carries `preExposure = 1.0`, so this is a
hypothesis with a measurement behind it - NOT a fix. The rules this test locks
are the ones that keep a hypothesis from becoming a silent change:

1. It is ONE variable: the SAME 1x1 surface A is handed, or null. Nothing else
   about dispatch B moves.
2. It is OFF unless asked for. A guess that quietly changes the picture for
   every reporter is a regression, not a test.
3. The ARM IS NAMED IN THE LOG. A one-variable test whose arm is not printed is
   two runs that are secretly the same run - the reason NS_AMD_INTEROP announces
   itself, and the same class of mistake.
4. Both arms are legal FFX calls: the field is optional, so passing null must
   remain a valid dispatch (that is what makes it usable as an A/B).
5. The descriptor is otherwise unchanged: if binding the exposure also moved
   the depth, the sizes or `preExposure`, the test would measure two things.

Run:  runtime\\python.exe tests/test_upscale_exposure_knob.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HDR = BASE / "native" / "amd" / "amd_fsr.h"
SRC = BASE / "native" / "amd" / "amd_fsr.cpp"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def main() -> int:
    failures: list[str] = []
    for p in (HDR, SRC, BRIDGE):
        if not p.is_file():
            print(f"FAIL: {p.relative_to(BASE)} is missing")
            return 1
    hdr = HDR.read_text(encoding="utf-8", errors="replace")
    src = SRC.read_text(encoding="utf-8", errors="replace")
    br = BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- 4. the parameter exists and is passed to the FFX field ------------
    if not re.search(r"bool DispatchUpscale\(ID3D12CommandList \*list, ID3D12Resource \*net,\s*\n\s*"
                     r"ID3D12Resource \*depth, ID3D12Resource \*exposure,", hdr):
        failures.append("DispatchUpscale has no exposure parameter")
    if "d.exposure = ffxApiGetResourceDX12(exposure, FFX_API_RESOURCE_STATE_COMPUTE_READ);" not in src:
        failures.append("the upscale descriptor never sets d.exposure from the parameter")

    # --- 1/5. the knob moves exactly ONE field inside B --------------------
    # Comparing A against B field-by-field is the wrong instrument: B is a
    # different dispatch and differs structurally in five fields on purpose
    # (color is A's output, output is the display surface, motion vectors are
    # null, and motionVectorScale / upscaleSize describe the upscale). What
    # must hold is that the KNOB touches exactly one field: if its variable
    # reached a second assignment, the test would be measuring two things.
    b_block = src[src.find("bool Upscaler::DispatchUpscale"):]
    uses = re.findall(r"^\s*d\.(\w+)\s*=.*\b(exposure|up_exposure)\b", b_block, re.M)
    if len(uses) != 1 or uses[0][0] != "exposure":
        failures.append(
            f"the knob reaches more than the exposure field: {uses} - it must "
            "move exactly one variable to be an A/B")
    if "up_exposure" in b_block.split("d.exposure")[0] or "up_exposure" in b_block.split("d.exposure")[-1]:
        failures.append("the knob variable is used outside the exposure assignment")
    a_block = src[src.find("bool Upscaler::DispatchNet"):src.find("bool Upscaler::DispatchUpscale")]
    if "d.exposure" not in a_block:
        failures.append("dispatch A no longer binds the exposure - the comparison base is gone")
    if "d.preExposure" not in b_block:
        failures.append("dispatch B lost its preExposure")
    # preExposure is the field that would silently turn this into two variables.
    pe_a = re.search(r"d\.preExposure\s*=\s*([^;]+);", a_block)
    pe_b = re.search(r"d\.preExposure\s*=\s*([^;]+);", b_block)
    if not pe_a or not pe_b or pe_a.group(1).strip() != pe_b.group(1).strip():
        failures.append(
            "the two dispatches disagree on preExposure - binding the exposure "
            "while also changing the gain would measure two things at once")

    # --- 2. off by default, and the default path passes null ---------------
    knob = re.search(r'GetEnvironmentVariableA\("NS_AMD_UPSCALE_EXPOSURE"', br)
    if not knob:
        failures.append("no NS_AMD_UPSCALE_EXPOSURE knob")
    else:
        decl = br[br.rfind("static const bool", 0, knob.start()):knob.end() + 120]
        if 'v[0] == \'1\'' not in decl:
            failures.append("the knob does not require the value to be exactly 1")
        if "up_exposure ? g_amd.exposure : nullptr" not in br:
            failures.append(
                "the call does not fall back to null when the knob is off - "
                "binding by default would change every reporter's picture")
        # The `: nullptr` arm is what makes the default arm bit-identical to
        # what shipped. A default of `g_amd.exposure` would be the silent change.
        if re.search(r"up_exposure \? g_amd\.exposure : g_amd\.exposure", br):
            failures.append("both arms of the knob bind the exposure")

    # --- 3. the arm is named in the log, exactly once ----------------------
    if "the upscale dispatch's exposure:" not in br:
        failures.append("the log never names which exposure arm ran")
    one_shot = re.search(r"if \(!g_amd\.up_exposure_logged\)\s*\{\s*g_amd\.up_exposure_logged = true;", br)
    if not one_shot:
        failures.append(
            "the arm is not printed once per run - a per-frame line would bury "
            "the report it belongs to")
    if not re.search(r"bool up_exposure_logged = false;", br):
        failures.append("the state has no up_exposure_logged flag")

    # --- the knob is a documented A/B, not a silent default ---------------
    if "NS_AMD_UPSCALE_EXPOSURE=1" not in hdr:
        failures.append("the header does not name the knob for a reader of the code")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: the upscale exposure knob moves exactly one field, is off by "
          "default, and names its arm in the log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
