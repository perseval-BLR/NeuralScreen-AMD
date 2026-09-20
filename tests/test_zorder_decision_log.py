"""The z-order guard must say what it saw and what it decided.

Why this exists: a user reports "the panel is invisible over the picture"
(#96, #89) and sends a diagnostic bundle. Until now the guard decided
silently - nothing in the log named the window it found or the branch it
took - so the only available next step was to guess, and a retest could not
distinguish "the guard never ran" from "the guard ran and chose wrong".
Both of those happened for real on this project.

What this locks:
  * describe_window names a live window (class, title, pid, rect) and never
    raises on a dead or bogus handle - it runs in the render path;
  * every branch of raise_topmost leaves a line: the healthy steady state
    too, so a log proves the code was reached rather than skipped;
  * the log is throttled per DECISION, not per call - the guard runs every
    frame while the menu is open and must not drown the log it feeds.

The decision branches are driven through a fake window layer rather than a
real GPU session: the branch selection is the logic under test, and it must
be provable without a second monitor or a worker.

Run:  runtime\\python.exe tests\\test_zorder_decision_log.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import display  # noqa: E402


class _FakeUser32:
    """The user32 surface raise_topmost touches, with settable answers."""

    def __init__(self, top):
        self.top = top
        self.present = 0x2222
        self.raises = []

    def FindWindowW(self, cls, title):
        return self.present if cls == "NeuralScreenPresent" else 0

    def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
        self.raises.append(int(hwnd))
        return 1


class _FakeDisplay(display.Display):
    """A Display whose windows and walker are scripted.

    `_zlog` is NOT replaced: the branch must call the real one, so a typo in
    a decision name or a missing call shows up here. The lines are captured
    by redirecting stdout around the drive instead.
    """

    def __init__(self, top):
        self.__dict__["_fake"] = _FakeUser32(top)
        self._zlog_sig = None
        self._zlog_t = 0.0

    def _top_real_window(self):
        return self.__dict__["_fake"].top


def _drive(top, hud):
    """Run one raise_topmost with a scripted stack; return (lines, raises)."""
    import contextlib
    import io

    disp = _FakeDisplay(top)
    fake = disp.__dict__["_fake"]
    real_mod = display.user32
    real_wm = display.pygame.display.get_wm_info
    display.user32 = fake
    display.pygame.display.get_wm_info = lambda: {"window": hud}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            disp.raise_topmost()
    finally:
        display.user32 = real_mod
        display.pygame.display.get_wm_info = real_wm
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    return lines, fake.raises


def main() -> int:
    failures = []

    HUD = 0x1111
    FOREIGN = 0x3333

    # ---- describe_window on a live window ------------------------------
    import pygame
    pygame.init()
    pygame.display.set_mode((160, 120))
    info = pygame.display.get_wm_info()
    hwnd = info.get("window")
    if not hwnd:
        failures.append("no HWND from pygame - cannot check describe_window")
    else:
        text = display.describe_window(hwnd)
        for field in ("hwnd=0x", "pid=", "class=", "title=", "rect=("):
            if field not in text:
                failures.append(f"describe_window is missing {field!r}: {text}")
        if "pygame" not in text:
            failures.append(f"describe_window did not name the class: {text}")
        # A dead handle must degrade, not raise: it runs in the render path.
        try:
            junk = display.describe_window(0xDEADBEEF)
            if not junk.startswith("hwnd=0x"):
                failures.append(f"describe_window on a bad handle: {junk}")
        except Exception as exc:
            failures.append(f"describe_window raised on a bad handle: {exc!r}")
        try:
            display.describe_window(0)
            display.describe_window(None)  # type: ignore[arg-type]
        except Exception as exc:
            failures.append(f"describe_window raised on a null handle: {exc!r}")

    # ---- every branch leaves a line ------------------------------------
    PRESENT = 0x2222

    def _decision(lines):
        """The decision keyword from the first log line, or ''."""
        if not lines:
            return ""
        return lines[0].split("] ", 1)[-1].split(" ", 1)[0]

    # 1. the HUD holds the top: healthy, and it must still be visible in a log
    lines, raises = _drive(HUD, HUD)
    if _decision(lines) != "hud-on-top":
        failures.append(f"HUD on top logged {lines}, expected hud-on-top")
    if raises:
        failures.append(f"HUD on top must not raise anything, raised {raises}")

    # 2. the picture took the band: the panel is invisible - raise the HUD
    lines, raises = _drive(PRESENT, HUD)
    if _decision(lines) != "picture-above-hud":
        failures.append(f"picture on top logged {lines}, "
                        f"expected picture-above-hud")
    if raises != [HUD]:
        failures.append(f"picture on top must raise the HUD, raised {raises}")

    # 3. a foreign window took the slot: re-assert the pair (picture first,
    #    HUD last - that order is what puts the HUD on top)
    lines, raises = _drive(FOREIGN, HUD)
    if _decision(lines) != "foreign-above-hud":
        failures.append(f"foreign on top logged {lines}, "
                        f"expected foreign-above-hud")
    if raises != [PRESENT, HUD]:
        failures.append(f"foreign on top must re-assert the pair, raised "
                        f"{raises} (expected picture {hex(PRESENT)} then HUD "
                        f"{hex(HUD)})")
    # The line has to name the window that took the slot, otherwise the log
    # cannot tell the user's problem from a healthy one.
    if lines and "0x" not in lines[0]:
        failures.append(f"the decision line does not name the window: "
                        f"{lines[0]}")

    # 4. nothing covers us
    lines, raises = _drive(None, HUD)
    if _decision(lines) != "nothing-covers":
        failures.append(f"no cover logged {lines}, expected nothing-covers")
    if raises:
        failures.append(f"nothing-covering must not raise, raised {raises}")

    # ---- the log is throttled per decision, not per call ---------------
    # Drive the real print path and count lines. The guard is called every
    # frame while the menu is open; without throttling this floods the log.
    import io
    import contextlib
    import time

    class _Counting(display.Display):
        def __init__(self):
            self._zlog_sig = None
            self._zlog_t = 0.0

    counter = _Counting()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(50):
            counter._zlog("hud-on-top", "top=none")     # same state, 50 calls
        counter._zlog("picture-above-hud", "top=0x9")   # a change
        counter._zlog("picture-above-hud", "top=0x9")   # same again
    printed = [l for l in buf.getvalue().splitlines() if l.strip()]
    if len(printed) != 2:
        failures.append(f"throttling printed {len(printed)} lines for 50 "
                        f"identical calls plus one change (expected 2): "
                        f"{printed}")
    if printed and "(changed)" not in printed[0]:
        failures.append(f"the first line is not marked as a change: {printed}")

    # A persistent state must reappear after the quiet interval so it is
    # visible next to a later event, rather than only at the very start.
    counter2 = _Counting()
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        counter2._zlog("hud-on-top", "top=none")
        counter2._zlog_t -= 6.0      # pretend five quiet seconds passed
        counter2._zlog("hud-on-top", "top=none")
    again = [l for l in buf2.getvalue().splitlines() if l.strip()]
    if len(again) != 2:
        failures.append(f"a state that persists was not re-logged after the "
                        f"quiet interval: {again}")
    _ = time

    # ---- the guard never lets diagnostics break the render path --------
    class _Hostile(display.Display):
        def __init__(self):
            self._zlog_sig = None
            self._zlog_t = 0.0

    hostile = _Hostile()
    buf3 = io.StringIO()
    try:
        object.__setattr__(hostile, "_zlog_sig", object())
        with contextlib.redirect_stdout(buf3):
            hostile._zlog("x", "y")
    except Exception as exc:
        failures.append(f"_zlog raised on an odd signature: {exc!r}")

    for f in failures[:15]:
        print("FAIL:", f)
    if len(failures) > 15:
        print(f"... and {len(failures) - 15} more")
    if failures:
        return 1
    print("OK: the guard names its decision on every branch, the log is "
          "throttled per decision, and a bad handle cannot break the render "
          "path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
