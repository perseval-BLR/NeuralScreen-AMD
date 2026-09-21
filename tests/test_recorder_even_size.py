"""The recorder opens at EVEN dimensions, because yuv420p has no odd column.

WHY THIS EXISTS
---------------
A reporter's recording on a Radeon produced a 924-byte file with no frames in
it. The log says why, in two lines:

    [record] video codec: hevc_amf
    [record] recording started: ...
    [record] encoding aborted: [Errno 1313558101] Unknown error occurred:
             'avcodec_send_frame()'

1313558101 is 0x4E4B4E55 - the ASCII bytes "UNKN". That is AMF's
AMF_UNKNOWN, a bare failure with no cause attached, and it arrived on the FIRST
frame.

The cause is the frame size. yuv420p subsamples chroma 2x2, so it needs BOTH
dimensions even: an odd width has no chroma column to sit on. The recorder
already knew about the HEIGHT (`An odd height is rounded up by the encoder`)
and said nothing about the width - and windowed mode makes odd widths normal,
because a borderless window measures 1059 px. In that reporter's own run the
window was 1059x720: the height was already even and only the width was wrong.

It went unnoticed because NVENC tolerates the odd value. AMF does not.

WHAT THIS LOCKS
---------------
1. Both dimensions are made even before the stream is opened.
2. The encoder is opened at those sizes, not at the raw ones.
3. A frame that arrives one pixel larger is CROPPED to match, because handing
   an encoder a frame of a size different from its own stream is the same
   abort by another route.
4. `write()` still compares against the REAL size - the frame arrives at the
   display's size, and that check is what catches a mode change.

Run:  runtime\\python.exe tests/test_recorder_even_size.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RECORDER = BASE / "recorder.py"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def main() -> int:
    failures: list[str] = []
    if not RECORDER.exists():
        print("FAIL: recorder.py is missing")
        return 1
    raw = RECORDER.read_text(encoding="utf-8", errors="replace")
    code = strip_comments(raw)

    # --- 1. both dimensions are made even --------------------------------
    if not re.search(r"enc_w\s*=\s*width\s*\+\s*\(\s*width\s*&\s*1\s*\)", code):
        failures.append(
            "the WIDTH is not rounded to even - yuv420p has no chroma column for "
            "an odd width, and AMF aborts the first frame with a bare UNKNOWN "
            "(this is the 924-byte recording)")
    if not re.search(r"enc_h\s*=\s*height\s*\+\s*\(\s*height\s*&\s*1\s*\)", code):
        failures.append(
            "the HEIGHT is not rounded to even - the old comment claimed the "
            "encoder does it, which is true for NVENC and not for AMF")

    # --- 2. the stream is opened at the even sizes ------------------------
    if not re.search(r"_open_video_stream\(\s*enc_w\s*,\s*enc_h", code):
        failures.append(
            "the stream is not opened at the even sizes - the sizes are computed "
            "and then discarded")
    if not re.search(r"self\._stream\.width\s*=\s*enc_w", code):
        failures.append("the stream width is not set to the even value")
    if not re.search(r"self\._stream\.height\s*=\s*enc_h", code):
        failures.append("the stream height is not set to the even value")

    # --- 3. a larger frame is cropped -------------------------------------
    if not re.search(r"rgba\s*=\s*rgba\[:self\.enc_height,\s*:self\.enc_width\]", code):
        failures.append(
            "a frame larger than the encoder's own size is not cropped - passing "
            "it through is the same abort by another route")

    # --- 4. write() still validates against the REAL size ------------------
    if not re.search(r"rgba\.shape\[0\]\s*!=\s*self\.height", code):
        failures.append(
            "write() no longer checks the frame against the real size - that "
            "check is what catches a display-mode change, and the frames DO "
            "arrive at the display's size")

    # The even sizes must be recorded on the instance, or _encode_one cannot
    # know what to crop to.
    if not re.search(r"self\.enc_width,\s*self\.enc_height\s*=", code):
        failures.append(
            "the even sizes are not kept on the instance, so the crop in "
            "_encode_one has nothing to compare against")

    if failures:
        print("FAIL: the recorder can hand the encoder an odd-sized frame")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the recorder opens at even dimensions, crops a larger frame to "
          "match, and still validates incoming frames against the real size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
