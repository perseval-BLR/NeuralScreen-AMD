"""The probe never copies a surface the spec does not describe.

WHY THIS EXISTS
---------------
v0.3.13 shipped a spec-driven surface probe: one reader that measured the
dispatch surfaces (FP16, network extent) and the captured frame (RGBA8, frame
extent). It was written to explain a black picture and it did the opposite -
it turned a working-ish build into one that processed ZERO frames, on two
reporters' machines, on every launch:

    [amd] what WE hand over: converted input mean 0.3091, dispatch output mean 0.3068
    [amd] the submission did not retire
    [main] worker silent/dead on frame 0 - restarting (1/3)
    [main] the worker left with a non-zero code (7)

The cause was the frame's spec: it declared `v.w` x `v.hgt` (the CLIENT's work
size) while `v.color.tex` and `v.output` are created at `cw` x `ch` - the
composition size, which is FULL resolution whenever the frame is upscaled. On
an RX 7900 XTX that is 2496x1404 declared against 3840x2160 real; on an RX 9070
XT the pair is 1664x936 against 2560x1440. D3D12 answers a mismatched copy by
removing the device, and the frame's own fence wait then returns immediately
(WaitFenceValue exits on UINT64_MAX without logging), so the failure surfaced
as a submission that "did not retire" in the SAME MILLISECOND - not a timeout.

WHAT THIS LOCKS
---------------
1. The spec is checked against the resource's own description before any copy.
2. A disagreement does NOT copy - it reports and returns false, so a
   diagnostic can never remove the device again.
3. The frame spec is built from the size the resources are created at, not
   from the client's work size - the same expression the creation site uses.

The check in (1)+(2) is the durable half: it holds even if a future surface
gets a spec that drifts, which is exactly how this one was introduced.

Run:  runtime\\python.exe tests/test_amd_probe_spec_match.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"
HOST = BASE / "native" / "dlss5-feed-host64.cpp"


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

    # --- 1. The probe verifies the spec against the resource ----------------
    if "GetDesc()" not in code:
        failures.append(
            "the probe does not read the resource's own description - the spec "
            "is an unverified claim again, and a mismatched copy removes the "
            "device (that is what made v0.3.13 process zero frames)")

    # The comparison must be against the SIZE the spec asks for, and it must
    # return rather than fall through into the copy.
    measure = code[code.find("AmdMeasureSurface"):]
    measure = measure[:measure.find("\nstatic ")] if "\nstatic " in measure else measure
    if not re.search(r"rw\s*!=\s*mw\s*\|\|\s*rh\s*!=\s*mh", measure):
        failures.append(
            "the probe does not compare the resource's size with the spec's - "
            "the check exists but tests nothing, so a drift would still be "
            "copied")
    # A mismatch must RETURN, and it must do so BEFORE the copy is recorded.
    mismatch_at = measure.find("rw != mw || rh != mh")
    copy_at = measure.find("CopyTextureRegion")
    if mismatch_at < 0:
        failures.append("no size check in the probe")
    elif copy_at < 0:
        failures.append("the probe no longer copies anything - the check guards nothing")
    elif mismatch_at > copy_at:
        failures.append(
            "the size check runs AFTER the copy - it would report a mismatch it "
            "had already copied, which is the device removal it exists to stop")
    # And the format is checked too: it is part of "does this spec describe
    # this resource", and reading FP16 as RGBA8 reports a meaningless mean.
    if "rd.Format != spec.format" not in measure:
        failures.append(
            "the probe does not verify the FORMAT either - reading one format "
            "as another reports a plausible number that means nothing")

    # --- 2. The frame spec uses the size the resources are CREATED at -------
    # The creation site, read from the host, so the test is anchored to the
    # real expression rather than to a copy of it.
    if not HOST.exists():
        failures.append("dlss5-feed-host64.cpp is missing")
    else:
        host = strip_comments(HOST.read_text(encoding="utf-8", errors="replace"))
        if "const UINT cw = v.upscale ? full_w : w;" not in host:
            failures.append(
                "the creation site's size expression changed - this test's "
                "anchor is stale, re-derive it before trusting the result")
        else:
            # v.color.tex and v.output are both created at cw x ch.
            if not re.search(r"CreateVideoTex\(\s*v\.color,\s*cw,\s*ch,", host):
                failures.append(
                    "v.color.tex is no longer created at cw x ch - the probe's "
                    "frame spec may now be right for a different reason, check "
                    "the creation site")
    if not re.search(r"kFrameSpec\{\s*cw,\s*ch,", code):
        failures.append(
            "the frame spec is NOT built from cw/ch - those are exactly the "
            "sizes the resources are created at; anything else (v.w/v.hgt is "
            "the client's work size) declares a smaller surface than the "
            "resource and the copy removes the device")
    # The old, broken expression must be gone.
    if re.search(r"const AmdSurfaceSpec kFrameSpec\{\s*\(UINT\)v\.w", code):
        failures.append(
            "the frame spec still uses the client's work size (v.w/v.hgt) - "
            "this is the defect that made v0.3.13 process zero frames")

    if failures:
        print("FAIL: the probe can copy a surface its spec does not describe")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the probe verifies its spec (size and format) against the "
          "resource and refuses to copy a mismatch, and the frame spec is "
          "built from the size the resources are created at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
