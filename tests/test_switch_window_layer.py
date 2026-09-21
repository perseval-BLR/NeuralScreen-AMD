"""The layer geometry is written when the mode changes, not when the menu closes.

THE BUG (a fourth report of the #107 class, reached by a different route)

`Display._window_layer` is the origin `draw_capture_overlay` shifts the panel by
when the frame being saved is a WINDOW inside the screen. Before this fix it was
written in exactly two places:

  * `move_to`  - which `follow_window` SKIPS while the menu is open, because the
                 user may be dragging the panel by its title bar (`not
                 menu.visible` gate);
  * `set_window_layer` - which `apply_menu_action` calls when the menu CLOSES.

Choosing a window mode FROM AN OPEN MENU runs neither: the pipeline is rebuilt
for the new geometry, the menu stays open, and `_window_layer` still describes
the previous one. The panel is laid out against the screen and then blitted as
if the frame sat at the old origin, so the reporter saw the menu baked into his
recording - "transparent / incorrectly composited".

WHAT THIS LOCKS

1. `switch_window` writes the geometry itself, at the one moment it really
   changes, and it does so BEFORE the rebuild that consumes it.
2. The window case calls `set_window_layer` with the rect it just measured.
3. The desktop case CLEARS it: no window means no origin to shift by, and a
   value left over from the previous mode would shift the panel by a window
   that is not being captured.
4. `clear_window_layer` exists and really forgets the origin, and a full-screen
   frame after it is not shifted - the behavioural half, which fails on the old
   code with no `clear_window_layer` at all.

Run:  runtime\\python.exe tests/test_switch_window_layer.py
"""
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

PIPELINE = BASE / "pipeline.py"

SCREEN = (2560, 1600)


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def switch_window_body(code: str) -> str:
    """The body of switch_window, up to the next top-level def."""
    start = code.find("def switch_window(")
    if start < 0:
        return ""
    end = code.find("\ndef ", start + 1)
    return code[start:end if end > 0 else len(code)]


def main() -> int:
    failures: list[str] = []
    if not PIPELINE.exists():
        print("FAIL: pipeline.py is missing")
        return 1
    code = strip_comments(PIPELINE.read_text(encoding="utf-8", errors="replace"))
    body = switch_window_body(code)

    if not body:
        failures.append("switch_window is gone from the pipeline - this test's "
                        "anchor is stale, re-derive it before trusting a result")
    else:
        # --- 1. the geometry is written here at all ------------------------
        if "set_window_layer" not in body:
            failures.append(
                "switch_window does not write the layer geometry - a mode chosen "
                "from an OPEN menu runs neither move_to (gated on the menu being "
                "closed) nor set_window_layer (called on close), so the panel is "
                "baked at the previous window's origin")
        if "clear_window_layer" not in body:
            failures.append(
                "switch_window does not CLEAR the layer geometry when it goes "
                "back to the desktop - a leftover origin shifts the panel by a "
                "window that is no longer being captured")
        # --- 2. it must happen BEFORE the rebuild consumes it --------------
        write_at = body.find("set_window_layer")
        rebuild_at = body.find("rebuild_pipeline(")
        if write_at < 0 or rebuild_at < 0:
            failures.append("the geometry write or the rebuild call is gone from "
                            "switch_window - the ordering check guards nothing")
        elif write_at > rebuild_at:
            failures.append(
                "the geometry is written AFTER rebuild_pipeline - the rebuild "
                "draws the first frames of the new mode with the old origin, "
                "which is the baked panel the reporter saw")
        # --- 3. the window case uses the rect it measured ------------------
        if not re.search(r"if\s+rect\s+is\s+not\s+None:\s*\n\s*st\.display\.set_window_layer"
                         r"\(\s*\*rect\s*\)", body):
            failures.append(
                "the window case does not pass the measured rect to "
                "set_window_layer - anything else is a second guess at geometry "
                "switch_window already computed")

    # --- 4. the behavioural half -------------------------------------------
    import numpy as np   # noqa: E402
    import pygame        # noqa: E402
    from display import Display   # noqa: E402

    if not hasattr(Display, "clear_window_layer"):
        failures.append(
            "Display has no clear_window_layer - there is no way to go back to a "
            "full-screen frame, so the desktop case keeps the last window's "
            "origin")
    else:
        pygame.init()
        pygame.display.set_mode((64, 64))
        d = Display(*SCREEN, fullscreen=False)
        d.menu.set_state({
            "theme": "dark", "nr": True, "gpu_ok": True, "profile": "Natural",
            "profiles": ["Natural"], "params": {}, "split": 0.0, "work_scale": 0.65,
            "work_scale_cap": 0.65, "work_scale_min": 0.1, "style": 1,
            "work_size": "", "monitors": [], "monitor": "", "windows": [],
            "window_current": "", "hotkeys": {}, "screenshot_dir": "",
            "screenshot_mode": "ask", "screenshot_format": "png",
        })
        d.menu.visible = True
        d.menu.page = "main"
        # A window was being captured, then the user went back to the desktop.
        d.set_window_layer(349, 0, 1513, 1522)
        d.clear_window_layer()
        if d._window_layer is not None:
            failures.append(
                f"clear_window_layer left {d._window_layer!r} behind - the panel "
                "would still be shifted by it")
        # And the frame that follows covers the screen: no shift at all.
        w, h = SCREEN
        frame = np.zeros((h, w, 4), dtype=np.uint8)
        frame[..., :3] = 17
        frame[..., 3] = 255
        frame = np.ascontiguousarray(frame)
        before = frame.copy()
        d.draw_capture_overlay(pygame.image.frombuffer(frame, (w, h), "RGBX"))
        changed = (frame[..., :3] != before[..., :3]).any(axis=2)
        if not changed.any():
            failures.append("after clearing, the panel never reached a "
                            "full-screen frame")
        else:
            ys, xs = np.where(changed)
            p = d.menu.panel_rect
            got = (int(xs.min()), int(ys.min()))
            want = (p.x, p.y)
            if got != want:
                failures.append(
                    f"after clearing, the panel on a full-screen frame is at {got}, "
                    f"not {want} - a leftover window origin is still shifting it")

    # --- 5. the end-to-end case: a mode chosen from an OPEN MENU -----------
    # The static checks above say the geometry is written; this one runs the
    # real switch_window with the menu open and bakes a frame, which is the
    # sequence the reporter performed. Measured on the pre-fix code: the panel
    # lands 349 px left of where he saw it - exactly the window's origin x.
    import types   # noqa: E402
    import channels   # noqa: E402
    import pipeline   # noqa: E402

    WINDOW = (349, 0, 1513, 1522)
    dw = Display(*SCREEN, fullscreen=False)
    dw.menu.set_state({
        "theme": "dark", "nr": True, "gpu_ok": True, "profile": "Natural",
        "profiles": ["Natural"], "params": {}, "split": 0.0, "work_scale": 0.65,
        "work_scale_cap": 0.65, "work_scale_min": 0.1, "style": 1,
        "work_size": "", "monitors": [], "monitor": "", "windows": [],
        "window_current": "", "hotkeys": {}, "screenshot_dir": "",
        "screenshot_mode": "ask", "screenshot_format": "png",
    })
    dw.menu.visible = True     # the menu is OPEN while the mode changes
    dw.menu.page = "main"
    st = types.SimpleNamespace(
        display=dw, lang="en", want_dda=True, width=SCREEN[0], height=SCREEN[1],
        work_w=SCREEN[0], work_h=SCREEN[1], work_scale=0.65,
        capture=types.SimpleNamespace(resolution=SCREEN), output_rgba=None,
        follow_size=None, follow_pos=None, follow_resize=None, window_hwnd=None)
    dw.enter_switch_mode = lambda *a, **k: None
    dw.exit_switch_mode = lambda *a, **k: None
    dw.alert = lambda *a, **k: None
    saved = (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
             pipeline.window_frame_rect, channels.probe_window_capture)
    pipeline.teardown_pipeline = lambda s: None
    pipeline.rebuild_pipeline = lambda s, note: None
    pipeline.window_frame_rect = lambda hwnd: WINDOW
    channels.probe_window_capture = lambda s, hwnd: (WINDOW[2], WINDOW[3])
    try:
        pipeline.switch_window(st, 0x1234)
    finally:
        (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
         pipeline.window_frame_rect, channels.probe_window_capture) = saved

    if dw._window_layer != WINDOW:
        failures.append(
            f"after switch_window with the menu open, _window_layer is "
            f"{dw._window_layer!r} instead of {WINDOW!r} - the bake that follows "
            f"shifts the panel by the wrong origin")
    else:
        fw, fh = WINDOW[2], WINDOW[3]
        f = np.zeros((fh, fw, 4), dtype=np.uint8)
        f[..., :3] = 17
        f[..., 3] = 255
        f = np.ascontiguousarray(f)
        b = f.copy()
        dw.draw_capture_overlay(pygame.image.frombuffer(f, (fw, fh), "RGBX"))
        c = (f[..., :3] != b[..., :3]).any(axis=2)
        if not c.any():
            failures.append("with the menu open through a mode switch, the panel "
                            "never reached the baked frame")
        else:
            ys4, xs4 = np.where(c)
            got4 = (int(xs4.min()), int(ys4.min()))
            p4 = dw.menu.panel_rect
            want4 = (max(0, p4.x - WINDOW[0]), max(0, p4.y - WINDOW[1]))
            if got4 != want4:
                failures.append(
                    f"with the menu open through a mode switch, the baked panel is "
                    f"at {got4}, not {want4} - the panel is baked at the previous "
                    f"window's origin, which is the reporter's 'transparent / "
                    f"incorrectly composited' recording")

    if failures:
        print("FAIL: the layer geometry is not written when the mode changes")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: switch_window writes the layer geometry before the rebuild, the "
          "window case uses the measured rect, the desktop case clears it, and a "
          "full-screen frame after clearing is not shifted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
