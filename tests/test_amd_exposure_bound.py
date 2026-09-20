"""The 1x1 exposure must actually reach the FFX dispatch that feeds the engine.

The engine reports what it received: `staging ready: ... exposure no` while our
own log said we had created a 1x1 exposure surface. Both were true, and the gap
between them is the whole bug:

  * `ffxDispatchDescUpscale` HAS a field for it - "Optional resource containing a
    1x1 exposure value" - and the dispatch left it null;
  * the context was created with FFX_UPSCALE_ENABLE_AUTO_EXPOSURE, so the
    upscaler adapted from an input that was black, and the value it settled on
    ran away to its ceiling (the engine logged `exposure 9999.9980` on every
    Radeon report).

A 1x1 surface was even the right shape. It was never bound.

Run:  runtime\\python.exe tests/test_amd_exposure_bound.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FSR_H = ROOT / "native" / "amd" / "amd_fsr.h"
FSR_CPP = ROOT / "native" / "amd" / "amd_fsr.cpp"
BRIDGE = ROOT / "native" / "amd" / "amd_bridge.inl"


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("//"):
            continue
        out.append(line.split("//")[0] if "//" in line else line)
    return "\n".join(out)


def main() -> int:
    failures: list[str] = []
    for path in (FSR_H, FSR_CPP, BRIDGE):
        if not path.exists():
            print(f"FAIL: {path.name} is missing")
            return 1

    hdr = strip_comments(FSR_H.read_text(encoding="utf-8", errors="replace"))
    cpp = strip_comments(FSR_CPP.read_text(encoding="utf-8", errors="replace"))
    brg = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # 1. The dispatch must take an exposure surface at all. A signature without
    #    it is how the field stayed null for every release.
    if not re.search(r"DispatchNet\([^;]*ID3D12Resource \*exposure", hdr, re.S):
        failures.append(
            "DispatchNet takes no exposure resource - the FFX field cannot be "
            "filled and the engine will keep reporting `exposure no`")

    # 2. It must be BOUND in the dispatch description. Declaring the parameter
    #    and never assigning the field is the same bug with extra steps.
    if not re.search(r"d\.exposure\s*=\s*ffxApiGetResourceDX12\(", cpp):
        failures.append(
            "the dispatch description never sets `d.exposure` - the resource "
            "reaches the function and stops there")

    # 3. AUTO_EXPOSURE must be OFF. It is the flag that made the upscaler adapt
    #    from a black input instead of using the value we hand it.
    if "FFX_UPSCALE_ENABLE_AUTO_EXPOSURE" in cpp:
        failures.append(
            "FFX_UPSCALE_ENABLE_AUTO_EXPOSURE is still armed - the upscaler will "
            "adapt its own exposure from the input instead of using the 1x1 "
            "surface, which is the run-away the engine logs as 9999.9980")

    # 4. The caller must actually PASS the surface the frame created.
    if not re.search(r"DispatchNet\(h\.list,\s*g_amd\.fsr_in,\s*g_amd\.depth,\s*\n?\s*g_amd\.motion,\s*g_amd\.exposure,", brg):
        failures.append(
            "the frame does not pass g_amd.exposure into DispatchNet - the "
            "surface is created and released with everything else, unused")

    # 5. And the surface must genuinely be 1x1 R32F, the shape the FFX header
    #    names for this field. A different shape is a different claim.
    if not re.search(r"AmdMakeTex\(1,\s*1,\s*DXGI_FORMAT_R32_FLOAT", brg):
        failures.append(
            "the exposure surface is not a 1x1 R32F - the FFX header specifies "
            "exactly that for this field")

    if failures:
        print("FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: the 1x1 R32F exposure is passed to the dispatch and bound into "
          "the FFX field named for it, with auto-exposure off so the handed "
          "value is the one used")
    return 0


if __name__ == "__main__":
    sys.exit(main())
