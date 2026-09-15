"""Drive the worker's --live protocol far enough to exercise the AMD path.

The AMD pass only starts after a valid stream header arrives, and every branch
of its startup (runtime missing, wrong build, no HIP, engine refuse) says
something different in the log. Testing those by hand means building a header
in Python and watching the worker's stderr - which is exactly what this does.

Usage:
    runtime\\python.exe tools\\test_amd_worker.py [--any-gpu]

It starts native\\nvngx.dll --live with NS_AMD=1 (and NS_AMD_ANY_GPU=1 when
--any-gpu is passed, so the adapter gate can be walked through on a machine
with no Radeon), sends a 320x180 header, waits for the worker to talk, and
prints everything it said. The worker is then killed - nothing is left
running.

This is a maintainer's tool. It ships with the repository, not with the
release archive.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

VIDEO_MAGIC = 0x32563544   # "D5V2", the legacy 56-byte header
VIDEO_MAGIC_EXT = 0x33563544  # "D5V3", 64-byte header with full_w/full_h


def header(width: int, height: int, warmup: int = 2) -> bytes:
    """The 64-byte stream header the worker reads before its first frame."""
    return (
        struct.pack("<10I",
                    VIDEO_MAGIC_EXT, width, height, warmup,
                    0,          # frame_count: 0 = LIVE (unbounded)
                    0, 0, 0, 0, 0,   # profile, preset, style, auto_mask, ui_correction
                    ) +
        struct.pack("<4f", 1.0, 1.0, 1.0, 1.0) +
        struct.pack("<2I", 0, 0)   # full_w, full_h: 0 = legacy 1:1
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--any-gpu", action="store_true",
                    help="lift the vendor rule so a non-Radeon machine walks "
                         "the rest of the path")
    ap.add_argument("--size", default="320x180",
                    help="stream size (default 320x180; small is fine - the "
                         "worker only needs a valid header to start)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    worker = root / "native" / "nvngx.dll"
    if not worker.is_file():
        print(f"no worker at {worker} - build it first (native\\build-host.bat)",
              file=sys.stderr)
        return 2

    w, h = (int(x) for x in args.size.lower().split("x"))
    env = dict(os.environ)
    env["NS_AMD"] = "1"
    if args.any_gpu:
        env["NS_AMD_ANY_GPU"] = "1"

    print(f"starting {worker.name} --live at {w}x{h} "
          f"({'any GPU' if args.any_gpu else 'Radeon only'})")
    proc = subprocess.Popen(
        [str(worker), "--live"], cwd=str(worker.parent), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(header(w, h))
        proc.stdin.flush()
        # The worker reads its header, then reports; eight seconds is far more
        # than it needs and keeps a hung engine from holding the test forever.
        deadline = time.time() + 8
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else b""
        proc.wait(timeout=5)

    text = out.decode("utf-8", errors="replace")
    print("--- worker output ---")
    print(text.rstrip() or "(silent)")
    print("--- end ---")

    # A one-line verdict so the tool can be used in a script.
    if "[amd] ===== AMD path active =====" in text:
        print("VERDICT: the AMD pass came up")
        return 0
    if "[amd] the runtime did not come up:" in text:
        reason = [l for l in text.splitlines() if "did not come up" in l]
        print(f"VERDICT: the pass was refused - {reason[0].split(':', 1)[1].strip() if reason else ''}")
        return 1
    if "no AMD adapter found" in text:
        print("VERDICT: no Radeon in this machine (expected without --any-gpu)")
        return 1
    print("VERDICT: the worker never reached the AMD path")
    return 1


if __name__ == "__main__":
    sys.exit(main())
