"""The engine is handed THIS frame's exposure, not a value frozen at startup.

WHY THIS EXISTS
---------------
The host computes an exposure for every captured frame: `UpdateAdaptiveExposure`
in the worker takes the frame's own mean luminance out of the gray channel,
maps it through a dark/lit window, and smooths it with a time constant
(PaperWhite). That number is handed to NGX on the NVIDIA path.

On the AMD path it was computed and then DISCARDED. The engine got a 1x1 R32F
texture written ONCE at surface creation - `AmdCreateExposure(1.0f)` - and never
touched again. So the AMD picture ran on a fixed exposure forever, and a
reporter's report says what that costs: "The good frames are over-exposed too -
sky and grass wash out to white - so 1.000 may still be high for this content."

The upstream working fork hands its engine a per-frame value the same way, which
is the second half of the evidence: this is not a new idea being tried here, it
is a known input that this path was missing.

WHAT THIS LOCKS
---------------
1. AmdUpdateExposure exists, takes a value, and writes it through the staging
   buffer into the 1x1 texture.
2. AmdEvaluateVideo calls it EVERY frame, before the dispatch that reads the
   texture - a per-frame value that is written after the dispatch is the
   previous frame's exposure, which is a subtler version of the same bug.
3. It refuses to hand the engine a zero or a NaN: a bad exposure normalises the
   picture to black, and the fallback must be a defined value rather than
   whatever the caller passed.
4. The staging buffer is released with the texture it feeds, and the mapping is
   removed first - writing through a stale mapping into a released resource is
   a use-after-free the driver would blame the device for.

The submission is skipped when the value has not changed: it is already
smoothed, and this is a 1x1 copy whose only job is to move a float. That is an
economy, not a contract, so it is NOT asserted here - the assertions are about
the value reaching the engine at all.

Run:  runtime\\python.exe tests/test_amd_exposure_update.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def body_of(code: str, signature: str) -> str:
    """The body of the DEFINITION of `signature`.

    A forward declaration matches the signature too, and following it lands on
    whichever function is defined next - so the definition is found by requiring
    the opening brace on the next non-empty line, which a declaration ends with
    a semicolon instead of.
    """
    for m in re.finditer(re.escape(signature), code):
        tail = code[m.end():m.end() + 80]
        brace = tail.find("{")
        semi = tail.find(";")
        if brace < 0:
            continue
        if semi >= 0 and semi < brace:
            continue          # a declaration: `signature(...);`
        at = m.start()
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
    return ""


def main() -> int:
    failures: list[str] = []
    if not BRIDGE.exists():
        print("FAIL: the AMD bridge is missing")
        return 1
    code = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # --- 1. the update function exists and writes the value -----------------
    upd = body_of(code, "static bool AmdUpdateExposure(float value)")
    if not upd:
        failures.append(
            "AmdUpdateExposure is gone - the engine then gets the exposure that "
            "was written once at startup, which is the over-exposure a reporter "
            "measured on bright content")
    else:
        if "memcpy(g_amd.exposure_up_map" not in upd:
            failures.append(
                "AmdUpdateExposure does not write the value into the staging "
                "buffer - nothing moves the number towards the engine")
        if "AmdCopyExposureIntoTexture" not in upd:
            failures.append(
                "AmdUpdateExposure does not copy the staging buffer into the 1x1 "
                "texture the dispatch binds - the value never reaches the engine")

    # --- 2. it is called every frame, before the dispatch ------------------
    ev = body_of(code, "static bool AmdEvaluateVideo(VideoState &v, int reset")
    if not ev:
        failures.append("AmdEvaluateVideo could not be read")
    else:
        call = ev.find("AmdUpdateExposure(")
        if call < 0:
            failures.append(
                "AmdEvaluateVideo never calls AmdUpdateExposure - the per-frame "
                "value is computed for the NGX path and discarded on this one, "
                "which is exactly the bug this guards")
        else:
            # The dispatch that binds the exposure texture must come after.
            disp = ev.find("AmdDispatch", call)
            if disp < 0:
                # Fall back to the name the bridge actually uses for the FSR
                # dispatch, so a rename does not silently pass this check.
                disp = ev.find("dispatch", call)
            if disp < 0:
                failures.append(
                    "no dispatch call follows the exposure update in "
                    "AmdEvaluateVideo - the order cannot be checked")
        if not re.search(r"AmdUpdateExposure\(\s*g_pw_exposure\s*\)", ev):
            failures.append(
                "AmdEvaluateVideo does not pass g_pw_exposure - the host's own "
                "per-frame value is what belongs here, not an invented one")

    # --- 3. a bad value cannot reach the engine ----------------------------
    if upd and not re.search(r"if\s*\(\s*!\s*\(\s*value\s*>\s*0", upd):
        failures.append(
            "AmdUpdateExposure does not reject a zero or NaN value - a bad "
            "exposure normalises the picture to black, which is a worse failure "
            "than the over-exposure this fixes")

    # --- 4. the staging buffer dies with the texture -----------------------
    rel = body_of(code, "static void AmdReleaseResources()")
    if not rel:
        failures.append("AmdReleaseResources could not be read")
    else:
        # The RELEASE of the staging resource, not merely a mention of the name:
        # the Unmap condition names exposure_up too, so a check for the string
        # alone passed even with the release deleted (found by negative control).
        released = re.search(r"drop\(\s*g_amd\.exposure_up\s*\)", rel) or \
                   re.search(r"g_amd\.exposure_up\s*->\s*Release\s*\(\s*\)", rel)
        if not released:
            failures.append(
                "AmdReleaseResources does not release the exposure staging "
                "buffer - it leaks on every resize and rebuild")
        else:
            unmapped = rel.find("Unmap")
            dropped = rel.find("drop(g_amd.exposure_up)")
            if dropped < 0:
                dropped = rel.find("exposure_up->Release")
            if unmapped < 0 or unmapped > dropped:
                failures.append(
                    "the exposure staging mapping is not removed before its "
                    "resource is released - a later write through it is a "
                    "use-after-free the driver reports as a device removal")

    if failures:
        print("FAIL: the engine is not handed this frame's exposure")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: every frame's exposure is written through the staging buffer into "
          "the texture the dispatch binds, bad values are rejected, and the "
          "staging dies with the texture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
