"""The baked panel must land where the USER saw it, not at the frame's centre.

THE BUG (#107, third report: "the menu in the screenshot looks transparent /
incorrectly composited")

`OverlayMenu.draw` re-lays the panel out for whatever surface it is handed, and
the frame being saved is not the screen in one-window mode - the capture is the
WINDOW, while the panel is placed on the screen. Measured before the fix:

    panel on a 2560x1600 screen : (1010, 232)
    panel on a 1513x1522 frame  : (486, 193)     <- 524 px away

so the panel in the file sat where the user never saw it, and whatever ran past
the frame's edge was cut.

WHAT THIS LOCKS

On a frame that is a WINDOW inside the screen, the panel's pixels must land at
`panel_rect - window_origin`. The measured window comes from the live log
(1513x1522 at x=349), which is the case that failed.

Run:  runtime\\python.exe tests/test_bake_menu_position.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np  # noqa: E402
import pygame  # noqa: E402

from display import Display  # noqa: E402

SCREEN = (2560, 1600)
WINDOW = (349, 0, 1513, 1522)          # origin x, origin y, w, h - from the log
FRAME_RGB = 17
#: A second monitor's corner on the virtual desktop. Every other case here
#: runs at (0, 0), which is exactly where the origin mistake cannot show.
MONITOR_ORIGIN = (3840, 0)


def state() -> dict:
    return {
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
    failures = []

    d = Display(*SCREEN, fullscreen=False)
    d.menu.set_state(state())
    d.menu.visible = True
    d.menu.page = "main"

    ox, oy, w, h = WINDOW
    d.set_window_layer(ox, oy, w, h)
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., :3] = FRAME_RGB
    frame[..., 3] = 255
    frame = np.ascontiguousarray(frame)
    surface = pygame.image.frombuffer(frame, (w, h), "RGBX")
    before = frame.copy()
    d.draw_capture_overlay(surface)

    changed = (frame[..., :3] != before[..., :3]).any(axis=2)
    if not changed.any():
        print("FAIL: the panel never reached the frame")
        return 1
    ys, xs = np.where(changed)
    got = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

    p = d.menu.panel_rect
    want = (max(0, p.x - ox), max(0, p.y - oy),
            min(w - 1, p.right - 1 - ox), min(h - 1, p.bottom - 1 - oy))
    print(f"panel on the screen : {tuple(p)}")
    print(f"panel in the frame  : {got}")
    print(f"expected (p - origin): {want}")
    if got != want:
        failures.append(
            f"the baked panel is at {got}, not at {want} - it is placed by the "
            f"frame's size instead of the screen's, which is what the reporter "
            f"saw as 'transparent / wrongly composited'")

    # And the ordinary case: the frame IS the screen - no offset, unchanged.
    d2 = Display(*SCREEN, fullscreen=False)
    d2.menu.set_state(state())
    d2.menu.visible = True
    d2.menu.page = "main"
    full = np.zeros((SCREEN[1], SCREEN[0], 4), dtype=np.uint8)
    full[..., :3] = FRAME_RGB
    full[..., 3] = 255
    full = np.ascontiguousarray(full)
    surf2 = pygame.image.frombuffer(full, SCREEN, "RGBX")
    before2 = full.copy()
    d2.draw_capture_overlay(surf2)
    changed2 = (full[..., :3] != before2[..., :3]).any(axis=2)
    ys2, xs2 = np.where(changed2)
    p2 = d2.menu.panel_rect
    got2 = (int(xs2.min()), int(ys2.min()), int(xs2.max()), int(ys2.max()))
    want2 = (p2.x, p2.y, p2.right - 1, p2.bottom - 1)
    if got2 != want2:
        failures.append(
            f"on a full-screen frame the panel is at {got2}, not {want2} - the "
            f"zero-offset case regressed")

    # The third case: the same window, on a monitor that does not start at
    # (0, 0). `_window_layer` is in VIRTUAL-DESKTOP pixels while the layer
    # starts at this monitor's corner, so the corner has to come off the offset
    # exactly as show() takes it off. Without that the panel is pushed a whole
    # monitor to the left and misses the frame entirely - and every other case
    # here runs at origin (0, 0), where the mistake is invisible.
    d3 = Display(*SCREEN, fullscreen=False)
    d3.menu.set_state(state())
    d3.menu.visible = True
    d3.menu.page = "main"
    d3.set_origin(*MONITOR_ORIGIN)
    d3.set_window_layer(MONITOR_ORIGIN[0] + ox, MONITOR_ORIGIN[1] + oy, w, h)
    frame3 = np.zeros((h, w, 4), dtype=np.uint8)
    frame3[..., :3] = FRAME_RGB
    frame3[..., 3] = 255
    frame3 = np.ascontiguousarray(frame3)
    surf3 = pygame.image.frombuffer(frame3, (w, h), "RGBX")
    before3 = frame3.copy()
    d3.draw_capture_overlay(surf3)
    changed3 = (frame3[..., :3] != before3[..., :3]).any(axis=2)
    if not changed3.any():
        failures.append(
            f"on a monitor at {MONITOR_ORIGIN} the panel never reached the "
            f"frame - the window's desktop coordinates were used as the "
            f"offset, so the menu was blitted a monitor's width off the file")
    else:
        ys3, xs3 = np.where(changed3)
        got3 = (int(xs3.min()), int(ys3.min()), int(xs3.max()), int(ys3.max()))
        p3 = d3.menu.panel_rect
        # The same expectation as the first case: the panel is laid out against
        # the layer, and the layer's corner is the monitor's.
        want3 = (max(0, p3.x - ox), max(0, p3.y - oy),
                 min(w - 1, p3.right - 1 - ox), min(h - 1, p3.bottom - 1 - oy))
        print(f"panel in the frame, monitor at {MONITOR_ORIGIN}: {got3}")
        if got3 != want3:
            failures.append(
                f"on a monitor at {MONITOR_ORIGIN} the baked panel is at "
                f"{got3}, not at {want3} - the layer's own corner was not "
                f"subtracted from the window's desktop position")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the panel lands where the user saw it - on a window-sized "
          "frame, on a full-screen one, and on a monitor that does not start "
          "at (0, 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
