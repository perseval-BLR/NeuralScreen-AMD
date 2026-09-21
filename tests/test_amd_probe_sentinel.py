"""The probe cannot report a zero it never measured, and its failures are visible.

WHY THIS EXISTS
---------------
Two weaknesses were named when the probe was built and both survived into the
builds that shipped:

1. `probe_failed` was incremented and NEVER printed. A reader saw the lines that
   did appear and had no way to know that others did not - the printed means
   came from an unknown subset of frames, and nothing said so.
2. The readback buffer is allocated once and reused for every measured surface,
   with no sentinel. A copy that never executes leaves the buffer as it was
   allocated - zeros - and the probe then reports a perfectly black surface it
   never sampled. That is the ONE failure this instrument cannot tell from a
   real black frame, and a real black frame is exactly what it exists to
   diagnose. The function's own comment records the artifact; nothing guarded
   it.

WHAT THIS LOCKS
---------------
1. The buffer is poisoned before the copy is recorded.
2. The poison is checked on the RAW bytes, before any mean is computed - so no
   arithmetic can sit between the sentinel and the verdict.
3. An untouched buffer makes the probe REFUSE to answer (return false) instead
   of reporting a zero, and says why in the log.
4. The failure count reaches the log, with the totals, so a partial failure is
   visible rather than implied.

Run:  runtime\\python.exe tests/test_amd_probe_sentinel.py
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

    The window is opened to 400 characters, not 80: these signatures wrap
    across two and three lines (this file's style), so a shorter window finds
    no brace and the function reads as missing - which is how this helper
    failed on its first use.
    """
    for m in re.finditer(re.escape(signature), code):
        tail = code[m.end():m.end() + 400]
        brace, semi = tail.find("{"), tail.find(";")
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
        print("FAIL: amd_bridge.inl is missing")
        return 1
    code = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    measure = body_of(code, "static bool AmdMeasureSurface(ID3D12Resource *src")
    if not measure:
        print("FAIL: AmdMeasureSurface could not be read")
        return 1

    # --- 1. the buffer is poisoned before the copy ------------------------
    poison_at = measure.find("memset(poison, 0xA5")
    if poison_at < 0:
        failures.append(
            "the readback is never poisoned - a copy that does not execute "
            "leaves the buffer's zeros and the probe reports a black surface it "
            "never sampled")
    # ...and BEFORE the copy is recorded. Poisoning afterwards proves nothing.
    copy_at = measure.find("CopyTextureRegion")
    if poison_at >= 0 and copy_at >= 0 and poison_at > copy_at:
        failures.append(
            "the buffer is poisoned AFTER the copy is recorded - the sentinel "
            "would overwrite the very data it is meant to validate")

    # --- 2. the sentinel is checked on raw bytes, before the mean ---------
    check_at = measure.find("p[i] != 0xA5")
    if check_at < 0:
        failures.append(
            "the sentinel is never checked - poisoning it without reading it "
            "back changes nothing")
    sum_at = measure.find("double sum = 0.0")
    if check_at >= 0 and sum_at >= 0 and check_at > sum_at:
        failures.append(
            "the sentinel is checked AFTER the mean loop starts - arithmetic "
            "sits between the sentinel and the verdict")

    # --- 3. an untouched buffer REFUSES to answer -------------------------
    if check_at >= 0:
        after = measure[check_at:check_at + 900]
        if "return false;" not in after:
            failures.append(
                "a buffer still carrying its sentinel does not make the probe "
                "refuse - it would fall through and report the zero it never "
                "measured, which is the exact artifact this guards against")
        if "sentinel" not in after:
            failures.append(
                "the refusal is silent - the reader is not told that a mean is "
                "missing, so a hole in the log looks like a frame that was "
                "not sampled")

    # --- 4. the failure count reaches the log -----------------------------
    if "probe_failed" not in code:
        failures.append("probe_failed is gone entirely")
    else:
        # It must be LOGGED, not merely incremented: an unused counter is a
        # comment. The search is bounded to the Log() call that names it, and
        # the bound is the closing paren of that call rather than the next
        # semicolon: an argument list spans lines and contains semicolons-free
        # casts, while `[^;]*` ran past the call and swallowed whatever came
        # after it (so probe_frames in the SAME call read as absent).
        logged = None
        for m in re.finditer(r"Log\(", code):
            # Walk to the matching close paren of this call.
            depth, i = 1, m.end()
            while i < len(code) and depth > 0:
                if code[i] == "(":
                    depth += 1
                elif code[i] == ")":
                    depth -= 1
                i += 1
            call = code[m.start():i]
            if "probe_failed" in call:
                logged = call
                break
        if logged is None:
            failures.append(
                "probe_failed is counted and never printed - a partial failure "
                "stays invisible and the printed means silently describe a "
                "subset of frames")
        elif "probe_frames" not in logged:
            # ...and the totals must be in the same call, or the reader cannot
            # tell how big the subset was.
            failures.append(
                "the failure count is printed without the total number of "
                "passes - 'failed 3 times' means nothing without 'of how many'")

    if failures:
        print("FAIL: the probe can still report a zero it never measured")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the readback is poisoned before the copy, an untouched buffer "
          "makes the probe refuse and say so, and its failure count reaches the "
          "log with the totals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
