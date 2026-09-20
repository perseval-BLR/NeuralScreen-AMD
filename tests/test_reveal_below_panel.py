r"""The picture window is revealed UNDER the panel, not over it and back.

THE MEASUREMENT (#89, a reporter's v1.17.0 package - the first one carrying
the `[z]` lines)

    18:42:51.928  [present] window revealed on the first Present
    18:42:51.937  [z] picture-above-hud (changed) class='NeuralScreenPresent'
    18:42:51.948  [z] hud-on-top (changed)        class='pygame'

Nine reveals in that session, nine losses of the top, one to one, with gaps of
10, 10, 10, 11, 19, 19, 53, 54 and 55 ms - one to three monitor refreshes with
no panel on the screen. It was never a fight with a foreign window: the picture
window is created hidden on purpose and then shown with ShowWindow, which puts
a topmost window above every other topmost window, the panel included. The
z-order guard then put the panel back, one refresh too late to be invisible.
The same single-refresh absence was measured frame by frame in another
reporter's video (#107).

WHAT THIS LOCKS

1. The panel publishes its own handle, and re-publishes it whenever its window
   may have been recreated - a stale handle puts the picture silently back on
   top.
2. The worker is started in a way that inherits it.
3. The reveal inserts the picture directly below that handle in ONE operation,
   guarded: the panel has to exist and be topmost, because SetWindowPos placed
   after a non-topmost window drops the picture out of the topmost band
   altogether - behind other applications, which is far worse than a flash.
   When the guard says no, the old behaviour is what happens.

Point 3 is read as source. No Python test can watch two processes' windows
being ordered against each other, and the alternative - trusting that it is
still right - is what cost this project the month between #107 being opened
and the log that answered it.

Run:  runtime\python.exe tests\test_reveal_below_panel.py
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

import display as display_mod  # noqa: E402
from display import Display  # noqa: E402

STATE = {
    "theme": "dark", "nr": True, "gpu_ok": True, "profile": "Natural",
    "profiles": ["Natural"], "params": {}, "split": 0.0, "work_scale": 0.65,
    "work_scale_cap": 0.65, "work_scale_min": 0.1, "style": 1,
    "work_size": "", "monitors": [], "monitor": "", "windows": [],
    "window_current": "", "hotkeys": {}, "screenshot_dir": "",
    "screenshot_mode": "ask", "screenshot_format": "png",
}


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures: list[str] = []

    # ---- 1. the panel publishes its handle, and keeps it current ---------
    os.environ.pop("NS_HUD_HWND", None)
    d = Display(640, 360)
    d.menu.set_state(dict(STATE))

    real_info = pygame.display.get_wm_info
    try:
        for handle in (0xABCD, 0x1234BEEF):
            pygame.display.get_wm_info = lambda h=handle: {"window": h}
            d._move_to_origin()
            got = os.environ.get("NS_HUD_HWND")
            if got != str(handle):
                failures.append(
                    f"the panel published NS_HUD_HWND={got!r} for window "
                    f"0x{handle:X} - the worker would place the picture "
                    f"against a window that is not the panel")
    finally:
        pygame.display.get_wm_info = real_info

    # A handle that cannot be read must not leave a stale one behind as a
    # fact: the worker checks IsWindow, but a wrong LIVE handle passes that.
    # This is why it is re-published on every move rather than published once.
    if "_move_to_origin" not in dir(d):
        failures.append("_move_to_origin is gone - the publish has no home "
                        "and this test is checking nothing")

    # ---- 2. the worker inherits the environment -------------------------
    pipe = io.open(BASE / "pipeline.py", encoding="utf-8").read()
    m = re.search(r"subprocess\.Popen\((?:[^()]|\([^()]*\))*\)", pipe, re.S)
    if m is None:
        failures.append("no subprocess.Popen found in pipeline.py")
    elif re.search(r"\benv\s*=", m.group(0)):
        failures.append(
            "the worker is started with an explicit env= - NS_HUD_HWND is "
            "passed by inheritance, so an explicit environment has to carry "
            "it or the picture goes back on top")

    # ---- 3. the reveal, read as source ----------------------------------
    cpp = io.open(BASE / "native" / "dlss5-feed-host64.cpp",
                  encoding="utf-8", errors="surrogateescape").read()
    body = re.search(r"static void RevealOnFirstPresent\(\)\s*\{.*?\n\}",
                     cpp, re.S)
    if body is None:
        failures.append("RevealOnFirstPresent is gone from the worker")
    else:
        b = body.group(0)
        if "SetWindowPos" not in b or "SWP_SHOWWINDOW" not in b:
            failures.append(
                "the reveal no longer shows and orders the window in one "
                "operation: a plain ShowWindow puts the picture above the "
                "panel and leaves the guard to correct it a refresh later")
        if "WS_EX_TOPMOST" not in b:
            failures.append(
                "the reveal does not check that the panel is topmost - "
                "inserting after a non-topmost window would drop the picture "
                "out of the topmost band, behind other applications")
        if "IsWindow" not in b:
            failures.append(
                "the reveal does not check that the published handle is "
                "still a window: a set_mode can hand the panel a new one")
        if "ShowWindow" not in b:
            failures.append(
                "the reveal has no fallback - with no usable panel handle it "
                "must still show the picture, the old way")

    if "NS_HUD_HWND" not in cpp:
        failures.append("the worker never reads NS_HUD_HWND")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the panel publishes its handle and keeps it current, the "
          "worker inherits it, and the picture is shown below the panel in "
          "one guarded operation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
