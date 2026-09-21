"""Frame generation is refused on a Radeon, in the worker and in the menu.

WHY THIS EXISTS
---------------
A 9070 XT flipped the FG switch on and the worker died 21 ms later:

    04:02:01.745  [fg] UI: on, 2x
    04:02:01.766  [crash] CRASH: ACCESS_VIOLATION (0xC0000005) at nvngx.dll
                  + 0xCA05, tried to read address 0x0
    04:02:01.766  [crash] CRASH: amd active=1 failed=0 frames=1293
    [main] the worker CRASHED: ACCESS_VIOLATION (exit code 3221225477)

`nvngx.dll` is this worker (/Fe:nvngx.dll in build-host.bat), so that fault is
our own dereference of a null pointer - not NVIDIA's runtime refusing politely.
Frame generation is an NGX feature end to end (NVSDK_NGX_D3D12_*, served by
nvngx_dlssg.dll); on a card with no NGX there is nothing to serve it, and the
path was entered anyway. The switch was offered, unchecked, on a card where it
can only take the session down.

The refusal is decided in ONE place, FgRequested, because it is the single gate
every FG path passes through: PresentFrame (dlss5-feed-host64.cpp:2915) and the
HDR presenter (hdr_present.inl:103) both ask it before entering FgPresent. A
guard anywhere else would leave the other entry open.

WHAT THIS LOCKS
---------------
1. FgRequested returns false when the chosen adapter is a Radeon, before it
   consults NS_FRAMEGEN or the UI switch - a preference cannot outrank a card
   that has no runtime.
2. The guard is on `g_radeon_present` (the adapter actually picked), not on "is
   the AMD pass live": a pass that FAILED is exactly when a user starts looking
   at other switches, and FG cannot work in that state either.
3. The refusal is logged when the switch goes on, so the log does not say "on"
   and then nothing.
4. The menu gets the fact from the worker's own line, and draws the row as
   unavailable with a reason rather than offering a live switch.
5. A disabled toggle sends NO action - a click on it must not reach the action
   table, or the guard above turns a click into a silent no-op.
6. Frame generation is NOT refused on an NVIDIA card: the guard must not spread
   to the machines where the feature is the point.

Run:  runtime\\python.exe tests/test_amd_framegen_refusal.py
"""
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
FRAMEGEN = BASE / "native" / "frame_generation.inl"
HOST = BASE / "native" / "dlss5-feed-host64.cpp"
UI = BASE / "overlay_ui.py"
SETTINGS = BASE / "settings_io.py"

RADEON_LINE = ("04:02:04.049  [host] adapter 0 is the Radeon - the AMD pass "
               "runs there")


def behavior() -> list[str]:
    """The menu and the refusal watcher, run for real rather than grepped."""
    problems: list[str] = []
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    import settings_io
    from unittest.mock import patch
    from types import SimpleNamespace
    import test_ui_buttons as tb

    pygame.init()
    try:
        # --- the row, on a Radeon -----------------------------------------
        menu = tb.build()
        menu.set_state({"nr": True, "fg_radeon": True, "frame_multiplier": 3,
                        "frame_generation": True})
        tb.paint(menu)
        row = tb.find(menu, "toggle", "frame_generation")
        if row is None:
            problems.append("the FG row disappears entirely on a Radeon - the "
                            "feature should be named, with the reason under it")
        else:
            if not row.extra.get("disabled"):
                problems.append("the FG row is still a live control on a Radeon")
            if not row.extra.get("hint"):
                problems.append("the FG row gives no reason on a Radeon")
            actions = tb.click(menu, row)
            if actions:
                problems.append(
                    f"clicking the Radeon FG row still emits {actions} - the "
                    "guard turns that into an invisible no-op")
        mult = [i for i in menu.items
                if i.kind == "button" and i.key.startswith("frame_multiplier:")]
        if mult:
            problems.append(
                f"the multiplier buttons are drawn on the Radeon row: "
                f"{[i.key for i in mult]}")

        # --- the same row on an NVIDIA card stays fully live ---------------
        menu.set_state({"fg_radeon": False, "frame_multiplier": 3,
                        "frame_generation": True})
        tb.paint(menu)
        row = tb.find(menu, "toggle", "frame_generation")
        if row is None or row.extra.get("disabled"):
            problems.append(
                "the FG row is unusable on a machine with an NGX card - the "
                "refusal must not spread to the cards the feature is for")
        mult = [i for i in menu.items
                if i.kind == "button" and i.key.startswith("frame_multiplier:")]
        if len(mult) != 3:
            problems.append(f"the multiplier group is gone on NVIDIA: {len(mult)}")
        elif row is not None:
            actions = tb.click(menu, row)
            if actions != [("toggle", "frame_generation")]:
                problems.append(f"a normal FG click emits {actions}")

        # --- a config that arrived switched ON, on a Radeon ----------------
        alerts: list = []
        st = SimpleNamespace(
            cfg={"frame_generation": True},
            worker_logs=["[host] adapter 0 runs the network and the capture",
                         RADEON_LINE, "[amd] ===== AMD path active ====="],
            fg_alerted=False, lang="en",
            display=SimpleNamespace(alert=lambda *a, **k: alerts.append(a)))
        with patch.object(settings_io, "save_menu_layout"):
            settings_io.refresh_fg_ok(st)
        if st.cfg["frame_generation"] is not False:
            problems.append(
                "a Radeon run still reports Frame Generation as ON - the header "
                "would carry the flag for a feature that cannot start")
        if len(alerts) != 1:
            problems.append(f"expected one alert, got {len(alerts)}")
        with patch.object(settings_io, "save_menu_layout"):
            settings_io.refresh_fg_ok(st)
        if len(alerts) != 1:
            problems.append("the Radeon refusal alerts more than once")

        # --- and it does NOT fire on a machine with an NGX card ------------
        alerts2: list = []
        st2 = SimpleNamespace(
            cfg={"frame_generation": True},
            worker_logs=["[host] adapter 0 runs the network and the capture",
                         "[ngx] NVSDK_NGX_D3D12_Init -> 0x00000001",
                         "[fg] UI: on, 2x", "[fg] 2x enabled at 3840x2160"],
            fg_alerted=False, lang="en",
            display=SimpleNamespace(alert=lambda *a, **k: alerts2.append(a)))
        with patch.object(settings_io, "save_menu_layout"):
            settings_io.refresh_fg_ok(st2)
        if st2.cfg["frame_generation"] is not True:
            problems.append("a working FG on an NGX card was switched off")
        if alerts2:
            problems.append(f"an NGX card got the Radeon alert: {alerts2}")
    finally:
        pygame.quit()
    return problems


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def main() -> int:
    failures: list[str] = []
    for path in (FRAMEGEN, HOST, UI, SETTINGS):
        if not path.exists():
            print(f"FAIL: {path.name} is missing")
            return 1
    fg = strip_comments(FRAMEGEN.read_text(encoding="utf-8", errors="replace"))
    host = strip_comments(HOST.read_text(encoding="utf-8", errors="replace"))
    ui = strip_comments(UI.read_text(encoding="utf-8", errors="replace"))
    st = strip_comments(SETTINGS.read_text(encoding="utf-8", errors="replace"))

    # --- 1. the guard exists, and it is in FgRequested ---------------------
    body = re.search(r"static bool FgRequested\(\)\s*\{(.*?)\n\}", fg, re.S)
    if not body:
        failures.append(
            "FgRequested is gone or was renamed - it is the single gate every FG "
            "path passes through, and the guard has to live there")
    else:
        guard = re.search(r"if\s*\(\s*g_radeon_present\s*\)\s*return\s*false\s*;",
                          body.group(1))
        if not guard:
            failures.append(
                "FgRequested does not refuse on a Radeon - this is the path that "
                "dereferenced a null NGX feature and crashed the worker 21 ms "
                "after the switch went on")
        else:
            # --- 2. the guard is not a preference --------------------------
            head = body.group(1)[:guard.start()]
            if re.search(r"if\s*\(\s*!?\s*g_fg\.failed", head):
                failures.append(
                    "the Radeon guard sits after the failure check - a failed "
                    "state must not be able to reach the NGX call")
            if "g_radeon_present" not in fg:
                failures.append("g_radeon_present is not referenced in "
                                "frame_generation.inl at all")

    # --- 3. the host owns that flag, and it means "the adapter picked" -----
    if not re.search(r"g_radeon_present\s*=\s*picked_vendor\s*==\s*0x1002u", host):
        failures.append(
            "g_radeon_present is no longer assigned from the picked adapter's "
            "vendor - the guard would then read a flag nothing sets")
    if not re.search(r"static bool g_radeon_present", host):
        failures.append("g_radeon_present is not declared in the host")

    # The flag must be visible where FgRequested is compiled: the include comes
    # after the declaration, and if that order ever flips the build breaks (or
    # worse, a second flag is read). Exactly ONE declaration is required - a
    # second one after the include is the same defect wearing a passing test.
    decls = [m.start() for m in re.finditer(r"static bool g_radeon_present", host)]
    inc = host.find('#include "frame_generation.inl"')
    if len(decls) != 1:
        failures.append(
            f"g_radeon_present is declared {len(decls)} times - the guard must "
            "read the host's single flag, and a second declaration is how the "
            "include order silently stops mattering")
    if not decls or inc < 0 or decls[0] > inc:
        failures.append(
            "frame_generation.inl is included before g_radeon_present is "
            "declared - the guard cannot read the host's own flag")

    # --- 4. the refusal reaches the log -----------------------------------
    if "refused: this run is on a Radeon" not in fg:
        failures.append(
            "an enabled-on-a-Radeon attempt logs no refusal - the log would say "
            "\"on\" and then nothing, which is the silent switch of issue #76 "
            "one layer deeper")

    # --- 5. the menu learns the fact from the worker ----------------------
    if "def radeon_present" not in st:
        failures.append(
            "settings_io has no radeon_present - the menu cannot tell a Radeon "
            "from the config's motion_backend, which says what was ASKED for")
    else:
        reader = re.search(r"def radeon_present\(st\).*?return False", st, re.S)
        if reader and "is the Radeon - the AMD pass runs there" not in reader.group(0):
            failures.append(
                "radeon_present does not read the worker's own Radeon line - it "
                "must be the same fact the host holds, not a second guess")
    # The KEY in the payload, not the word: the alert's own string key is also
    # "fg_radeon", so a bare membership test passes while the menu is still
    # never told.
    if not re.search(r'"fg_radeon"\s*:', st):
        failures.append("menu_payload publishes no fg_radeon flag")

    # --- 6. the row is drawn unavailable, and it sends nothing ------------
    if "fg_radeon" not in ui:
        failures.append(
            "the menu ignores fg_radeon - a live-looking switch on a card where "
            "the feature cannot run")
    else:
        row = re.search(r"if bool\(self\.state\.get\(\"fg_radeon\"\)\):(.*?)\n            else:",
                        ui, re.S)
        if not row or "disabled" not in row.group(1):
            failures.append(
                "the Radeon FG row is not marked disabled - it still reads as a "
                "control the user can flip")
        # The refusal must also drop the multiplier buttons: they belong to a
        # feature that cannot run, and the inline group is drawn by the same call.
        if row and "frame_multiplier:" in row.group(1):
            failures.append(
                "the multiplier buttons are still drawn on the Radeon row - the "
                "group is part of the FG control and must go with it")
    # A disabled toggle must not emit an action. The CONDITION is checked, not
    # the word: the branch carries a comment explaining the rule, and a
    # membership test on "disabled" passed even when the guard itself was
    # removed from the code under it.
    toggle_branch = re.search(r'elif item\.kind == "toggle":(.*?)\n            elif',
                              ui, re.S)
    if not toggle_branch or 'item.extra.get("disabled")' not in toggle_branch.group(1):
        failures.append(
            "handle_event sends the toggle action even when the item is "
            "disabled - a click on the Radeon row would reach the action table")

    # --- 7. it does not spread to NVIDIA --------------------------------
    # The guard is keyed on the Radeon fact alone; nothing here may refuse FG
    # for a machine that has an NGX card.
    if re.search(r"if\s*\(\s*AmdActive\s*\(\s*\)\s*\)\s*return\s*false", fg):
        failures.append(
            "the guard asks whether the AMD pass is LIVE - on a Radeon whose "
            "pass failed, or with the pass switched off, FG would be allowed "
            "again on a card that cannot run it")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures:
            print(f"  - {line}")
        return 1
    # --- 8. and the behavior itself, not only the shape of the code --------
    behavior_problems = behavior()
    if behavior_problems:
        print(f"FAIL: {len(behavior_problems)} behavior problem(s)")
        for line in behavior_problems:
            print(f"  - {line}")
        return 1
    print("OK: FG is refused on a Radeon at the one gate, logged, and the menu "
          "shows why instead of offering the switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
