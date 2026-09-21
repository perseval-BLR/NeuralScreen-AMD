"""A real recording at an odd width: the bug is reproduced, then must be gone.

This is the LIVE half of test_recorder_even_size.py. The static half checks the
source; this one actually encodes frames and reads the file back, because the
defect was a codec refusing to encode - which no amount of reading the source
proves either way.

How the bug is reproduced without a Radeon: libx264 refuses odd dimensions for
yuv420p for exactly the same reason AMF does (2x2 chroma subsampling), so an
odd-width source reproduces the class. AMF on the reporter's machine answered
`0x4E4B4E55` ("UNKN") on the FIRST frame; x264 refuses at open.

What must hold afterwards:
  * the recorder accepts an odd-sized frame without raising;
  * frames are actually written (`written > 0`) - the reporter's file had none;
  * the file exists and its video stream is even-sized.

WHAT THIS TEST CANNOT PROVE, and it was measured rather than assumed: removing
the crop in `_encode_one` does NOT make this test fail, because NVENC tolerates
a frame one pixel wider than its own stream. The crop is therefore DEFENSIVE -
it makes the frame match the declared stream exactly, which is what AMF is
entitled to require - and it is guarded by the static half
(`test_recorder_even_size.py`), not by an encoder on this machine. Recorded here
so the next reader does not mistake the missing negative control for an
oversight.

Run:  runtime\\python.exe tests/test_recorder_even_size_live.py
"""
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402

from recorder import VideoRecorder  # noqa: E402


def make_frame(w: int, h: int, value: int) -> np.ndarray:
    f = np.zeros((h, w, 4), dtype=np.uint8)
    f[..., 0] = value
    f[..., 3] = 255
    return f


def main() -> int:
    failures: list[str] = []

    # The reporter's own geometry: a borderless window, odd WIDTH, even height.
    w, h = 1059, 720
    if w % 2 == 0:
        print("FAIL: the fixture is not odd - this test would prove nothing")
        return 1

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "odd.mp4")
        try:
            # audio=False: WASAPI loopback is not what is under test here.
            rec = VideoRecorder(path, w, h, fps=30, audio=False)
        except Exception as exc:
            print(f"FAIL: the recorder refused an odd width: {exc}")
            return 1

        # The sizes the encoder was actually opened at.
        if rec.enc_width % 2 or rec.enc_height % 2:
            failures.append(
                f"the encoder was opened at {rec.enc_width}x{rec.enc_height} - "
                f"not both even, which is what AMF aborts on")
        if rec.enc_width < w or rec.enc_height < h:
            failures.append(
                f"the encoder size {rec.enc_width}x{rec.enc_height} is SMALLER "
                f"than the frame {w}x{h} - the crop would cut the picture")

        wrote = 0
        try:
            for i in range(20):
                rec.write(make_frame(w, h, 20 + i * 8))
                wrote += 1
                time.sleep(0.01)
        except Exception as exc:
            print(f"FAIL: write() raised on an odd-sized frame: {exc}")
            return 1

        # close() joins the encoder thread and writes the trailer, so the file
        # is only complete after it returns - and everything below reads the
        # file. Doing this outside the `with` block was the test's own bug: the
        # directory (and the file) is gone by then.
        try:
            rec.close()
        except Exception as exc:
            print(f"FAIL: close() raised: {exc}")
            return 1

        p = Path(path)
        if not p.exists():
            print("FAIL: no file was written at all")
            return 1
        size = p.stat().st_size

        if rec.written == 0:
            failures.append(
                f"ZERO frames encoded ({size} bytes) - this is the reporter's "
                f"924-byte file, reproduced")
        elif size < 2000:
            failures.append(
                f"only {size} bytes for {rec.written} frames - the container looks "
                f"empty, which is the shape of the original fault")

        # Read it back: the stream must be even-sized and carry frames.
        try:
            import av
            with av.open(path) as c:
                vs = c.streams.video[0]
                if vs.width % 2 or vs.height % 2:
                    failures.append(
                        f"the written stream is {vs.width}x{vs.height} - odd")
                n = sum(1 for _ in c.decode(video=0))
                if n == 0:
                    failures.append("the file decodes to zero video frames")
        except Exception as exc:
            failures.append(f"the file could not be read back: {exc}")

        if failures:
            print("FAIL: an odd-width recording still fails")
            for f in failures:
                print("  - " + f)
            return 1
        print(f"OK: {w}x{h} (odd width) recorded {rec.written} frames into {size} "
              f"bytes, encoder opened at {rec.enc_width}x{rec.enc_height}, and the "
              f"file reads back with frames")
        return 0


if __name__ == "__main__":
    sys.exit(main())
