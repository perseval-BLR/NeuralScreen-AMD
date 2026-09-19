"""What the menu shows, and what the config keeps.

Two directions of one subject. Out: the payload the overlay menu renders -
profiles, presets, sliders, monitors, hotkey captions, the GPU line and
whether neural rendering is really running on it. In: the two things a user
changes through the menu that have to survive a restart - the menu's own
position and size, and the hotkey assignments.

Both go into config.json through the atomic writer rather than over the live
file: a crash mid-write used to truncate the config and lose every setting.
"""
from __future__ import annotations

import json
import os
import sys
import winreg
from pathlib import Path

from paths import BASE_DIR
from capture import devicename_for_output_idx, list_adapters, list_monitors
from i18n import STRINGS as UI_STRINGS
# The work caps are the worker's contract, not a setting: the same two
# numbers size the shared motion buffer in the SHMI handshake.
from protocol import WORK_MAX_H, WORK_MAX_W  # noqa: F401
from winapi import list_capturable_windows
from resolution_limits import safe_processing_size


def _work_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """The NGX work resolution: scale of full, but no larger than
    WORK_MAX_W/H (NGX goes silent at 4K - the limit verified in isolation).

    Two rules that come from being bitten:

    At 1:1 the answer is the frame itself, with no rounding. Rounding to the
    nearest even number turned a 539-pixel-high window (900x500 plus its title
    bar) into a 540-high work size - larger than the frame - and the worker
    died on the header. An odd size at 1:1 stays on the legacy path, which is
    known to work (test_odd_frame_size).

    And a downscale rounds DOWN, never up: the work resolution must never
    exceed the frame it came from.
    """
    if scale >= 1.0:
        w, h = int(width), int(height)
    else:
        w = max(64, int(width * scale) // 2 * 2)
        h = max(64, int(height * scale) // 2 * 2)
    w, h = safe_processing_size(int(width), int(height), min(w, int(width)), min(h, int(height)))
    if w > WORK_MAX_W or h > WORK_MAX_H:
        k = min(WORK_MAX_W / w, WORK_MAX_H / h)
        w = max(64, int(w * k) // 2 * 2)
        h = max(64, int(h * k) // 2 * 2)
    return min(w, int(width)), min(h, int(height))


def hotkey_labels(bindings: dict) -> dict:
    """Bindings -> {command: "Num1"} for the captions on the menu buttons."""
    return {cmd: name for _mods, _vk, cmd, name in bindings.values()}

from i18n import STRINGS as UI_STRINGS


# The project page: README, hotkeys, requirements. Opened from the menu.
REPO_URL = "https://github.com/perseval-BLR/NeuralScreen-AMD"


CHANNEL_URL = "https://www.youtube.com/@perseval_BLR/videos"


PRESET_NAME_PREFIX = "Preset"


# The global hotkeys live in hotkeys.py (RegisterHotKey). The layout and the
# reasons behind the combinations are in that module's docstring.
WORK_SCALE_STEP = 0.05
# Frame limiter, brought over from the NVIDIA main line with pacing.py.
# "unlimited" is the default, so this changes nothing until someone sets a cap;
# it exists on the AMD path because the pass runs inline and a slow frame there
# otherwise turns into a catch-up burst on the next iteration.
FRAME_LIMIT_MODES = ("30", "60", "custom", "unlimited")
FRAME_LIMIT_CUSTOM_MIN = 15
FRAME_LIMIT_CUSTOM_MAX = 240


def frame_limit_fps(cfg: dict) -> int:
    """Resolve the persisted limiter mode to an FPS cap; zero is unlimited."""
    mode = str(cfg.get("frame_limit_mode", "unlimited"))
    if mode in ("30", "60"):
        return int(mode)
    if mode != "custom":
        return 0
    try:
        value = int(cfg.get("frame_limit_custom", 90))
    except (TypeError, ValueError, OverflowError):
        value = 90
    return min(FRAME_LIMIT_CUSTOM_MAX, max(FRAME_LIMIT_CUSTOM_MIN, value))

WORK_SCALE_MAX = 1.0


def _next_preset_name(presets: dict) -> str:
    """The first free "Preset N" name (Preset 1, Preset 2, ...)."""
    n = 1
    while f"{PRESET_NAME_PREFIX} {n}" in presets:
        n += 1
    return f"{PRESET_NAME_PREFIX} {n}"


def _set_autostart(enabled: bool) -> bool:
    """Enable/disable autostart with Windows (HKCU Run).

    We launch NeuralScreen.vbs through wscript - a hidden launcher with no
    console. Returns True on success.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            vbs = str(BASE_DIR / "NeuralScreen.vbs")
            winreg.SetValueEx(key, "NeuralScreen AMD", 0, winreg.REG_SZ,
                              f'wscript.exe "{vbs}"')
        else:
            try:
                winreg.DeleteValue(key, "NeuralScreen AMD")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        print(f"[main] autostart not configured: {exc}", file=sys.stderr)
        return False


# The version shown in the menu header. Kept in sync with native/launcher.rc
# (FileVersion/ProductVersion) and build_release_zip.py at release time.
APP_VERSION = "0.3.9"


# The channel label: the header shows the version, the channel lives in the
# settings page (user rule 2026-09-08).
CHANNEL_LABEL = "@perseval_BLR"


# --- DLSS 5 NR profiles (field order as in the converter) -----------------
#
# local_tone is half a point lower in every profile than it was through
# 1.8.1 (user, 13.09). The local tone mapping is the part that lifts
# shadows and flattens contrast, and at the old values it was doing more of
# that than the picture wanted - most visibly on dark scenes, where the
# brightening this program does anyway meets it head on. The four sliders
# still reach everything they reached: this moves where the profiles sit,
# not what the range allows.
#
# The profiles no longer carry `profile`, `preset` or `ui_correction`.
# Measured on the 310.8.0 runtime: every value of all three produces a
# byte-identical frame (colour-sweep-20260913, and tests/test_param_effect.py
# reports them every run). They are still sent - the wire layout is shared
# with the resize command and with a hundred tests - but they are sent as a
# fixed zero by the two packers, and nobody has to wonder about them again.
#
# Intensity is clamped at 1.00 inside NVIDIA's DLL, so the 1.65 and 2.50 the
# strong profiles used to ask for were the same picture as 1.00 all along.
# Extreme's local_structure comes down from 2.00 to the new 1.50 ceiling,
# which is the one real change here: measured, that is a detail metric of
# -14.4% against -13.5%, about a percent of the picture.
#
# The auto mask is on in every profile. Measured here on three real frames,
# a frozen input so anything moving between consecutive outputs is the
# network trembling rather than the picture changing:
#
#   source  mask   wiggle  shadows  peak   edges  detail  ms/frame
#   text       0    0.209    0.167    10   1.110   0.965      5.26
#   text       1    0.158    0.131    10   1.096   0.932      5.15
#   film       0    0.363    0.207     9   0.971   0.666      5.26
#   film       1    0.314    0.176     7   0.959   0.669      5.30
#   game       0    0.335    0.304     8   1.225   1.176      5.28
#   game       1    0.308    0.286     6   1.213   1.157      5.26
#
# It damps the trembling by 8-24% and the shadows by 6-22%, takes the peaks
# down (9->7, 8->6), and costs nothing in time - the per-frame figures are
# the same within noise. It is not free: on text it costs 3.4% of the fine
# detail. Text is also where it damps the most, and shimmering text is what
# people report, so that is the trade taken.
#
# The other reason is arithmetic: skin_structure is inert without it. With
# the mask off in two of the four profiles, the fourth slider in the menu
# did nothing at all in those two.
PROFILES = {
    "Faithful": dict(style=0, auto_mask=1,
                     intensity=0.70, local_tone=0.25, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(style=1, auto_mask=1,
                    intensity=1.00, local_tone=0.50, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(style=2, auto_mask=1,
                               intensity=1.00, local_tone=0.90, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(style=2, auto_mask=1,
                                intensity=1.00, local_tone=1.50, local_structure=1.50, skin_structure=1.5),
}


WORK_SCALE_MIN = 0.1


# How far each slider really reaches. One shared 0..2.5 was wrong in both
# directions: it promised travel that did nothing (issue #40, "low effect
# strength" - the user was turning a knob that had stopped answering), and
# it allowed values where the picture gets worse rather than stronger.
#
#   intensity        clamped at 1.0 inside NVIDIA's DLL. 1.0, 1.25, 1.5, 2
#                    and 2.5 all hash to the same frame (re-measured 15.09
#                    on 310.8.0 and again with the +0.5 tops request - still
#                    dead). The slider STAYS at 1.0: a longer travel would be
#                    the exact lie the range was rebuilt to remove.
#   tone, structure  the detail metric keeps climbing past 1.5, and so does
#                    the shimmer: the metric counts trembling noise as fine
#                    detail. Measured 15.09: 2.0 still moves the picture
#                    (distinct frames) and is a stronger look, so the top
#                    moved 1.5 -> 2.0 on request; shimmer at 2.0/2.0 is
#                    ~2.1 of 255 against ~1.5 at the old tops (test_param_effect
#                    pins the ceilings).
#   skin_structure   inert unless auto_mask is on, and -1 is "off".
#                    2.5 measured alive on 15.09; the top moved 2.0 -> 2.5.
PARAM_RANGE = {
    "intensity": (0.0, 1.0),
    "local_tone": (0.0, 2.0),
    "local_structure": (0.0, 2.0),
    "skin_structure": (-1.0, 2.5),
}


def param_range(key: str) -> tuple:
    """The (low, high) a parameter is allowed. Unknown keys get the widest."""
    return PARAM_RANGE.get(key, (0.0, 1.5))


def clamp_param(key: str, value: float) -> float:
    """Pull a value into range - for configs written before the range was."""
    lo, hi = param_range(key)
    return min(max(float(value), lo), hi)


# The four sliders a user preset stores. The same keys as PROFILES carries,
# minus the NGX plumbing (profile/preset/style/auto_mask/ui_correction stay
# tied to the built-in profile the preset was saved from).
PRESET_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")


DEFAULT_LANG = "en"


# The NGX plumbing a preset carries along with the four sliders: the range
# it must be in, and what to use when it is not there at all. Presets saved
# by builds up to 1.8.2 also carry profile/preset/ui_correction; those are
# read and thrown away, because they do nothing (see PARAM_RANGE). A preset
# saved by this build does not have them, and must still load.
_PRESET_INT_KEYS = {
    "style": (0, 2, 1),
    "auto_mask": (0, 1, 0),
}


def load_presets(cfg: dict) -> dict:
    """The user presets from the config, validated.

    A preset is a full params snapshot: the four sliders plus the style and
    the auto mask, so applying it reproduces the look it was saved with.
    A broken entry is dropped - it must neither take the program down nor
    be offered in the menu. A merely OLD entry is not broken: values wider
    than the ranges allow today are pulled in, and the three dead fields a
    pre-1.8.3 preset carries are ignored.
    """
    raw = cfg.get("presets")
    if not isinstance(raw, dict):
        return {}
    presets: dict = {}
    for name, values in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(values, dict):
            continue
        clean = {}
        ok = True
        for key in PRESET_KEYS:
            if key not in values:
                ok = False
                break
            try:
                v = float(values[key])
            except (TypeError, ValueError, OverflowError):
                ok = False
                break
            if (isinstance(values[key], bool) or v != v
                    or v in (float("inf"), float("-inf"))):
                ok = False
                break
            # Out of range is an older build, not a broken preset: the
            # ranges shrank when they were measured, and a preset saved at
            # intensity 2.5 was already giving the picture 1.0 gives. It is
            # pulled in, not thrown away - losing someone's saved look over
            # a number that never did anything would be indefensible.
            clean[key] = clamp_param(key, v)
        if not ok:
            continue
        for key, (lo, hi, fallback) in _PRESET_INT_KEYS.items():
            v = values.get(key, fallback)
            if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                ok = False
                break
            clean[key] = v
        if ok:
            presets[name.strip()] = clean
    return presets


def load_config(path: Path) -> dict:
    """Load and validate config.json."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("config.json: root must be an object")
    required = {"monitor", "width", "height", "fullscreen", "warmup", "profile",
                "intensity", "local_tone", "local_structure", "skin_structure"}
    missing = required - set(cfg)
    if missing:
        raise ValueError(f"config.json: missing fields: {sorted(missing)}")
    if not isinstance(cfg["profile"], str):
        raise ValueError("config.json: field profile must be a string")
    if cfg["profile"] not in PROFILES:
        # A user preset name, or a stale reference to a deleted preset.
        # A stale reference must not take the program down - fall back to
        # the default profile (the menu still lists the surviving presets).
        if cfg["profile"] not in load_presets(cfg):
            print(f"[main] config.json: unknown profile {cfg['profile']!r}; "
                  f"falling back to 'Natural'", file=sys.stderr)
            cfg["profile"] = "Natural"
    for key in ("width", "height", "warmup"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ValueError(f"config.json: field {key} must be a positive integer")
    # Out of range is not an error any more, it is an old config. The
    # ranges shrank when they were measured (intensity 2.5 -> 1.0 and so
    # on), and a user who had 2.5 saved was already getting the picture 1.0
    # gives - refusing to start over a number that never did anything would
    # be the worst of both. Nonsense is still an error: "abc" is a broken
    # file, 2.5 is a file written by an older build.
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        if cfg[key] is None:
            continue
        if isinstance(cfg[key], bool):
            raise ValueError(f"config.json: field {key} must be a finite number")
        try:
            value = float(cfg[key])
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"config.json: field {key} must be a finite number")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"config.json: field {key} must be a finite number")
        pulled = clamp_param(key, value)
        if pulled != value:
            print(f"[main] config.json: {key} {value:g} is outside the "
                  f"measured range {param_range(key)}; using {pulled:g} - "
                  f"the same picture the old value gave",
                  file=sys.stderr)
        cfg[key] = pulled
    # work_scale: 0.1..1.0 - the NGX processing resolution relative to the output
    scale = float(cfg.get("work_scale", 1.0))
    cfg["work_scale"] = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, scale))
    # lang: the language of the HUD/alerts/menu (en/ru, DEFAULT_LANG by default)
    lang = str(cfg.get("lang", DEFAULT_LANG))
    if lang not in UI_STRINGS:
        lang = DEFAULT_LANG
    cfg["lang"] = lang
    from motion_backend import normalize_backend
    cfg["motion_backend"] = normalize_backend(cfg.get("motion_backend"))
    cfg["frame_generation"] = bool(cfg.get("frame_generation", False))
    try:
        cfg["frame_multiplier"] = min(4, max(2, int(cfg.get("frame_multiplier", 2))))
    except (ValueError, TypeError, OverflowError):
        cfg["frame_multiplier"] = 2
    return cfg


def resolve_params(cfg: dict) -> dict:
    """Profile + custom NR parameters from the config (null = use the profile).

    A user preset is a full params snapshot and wins over the built-in
    profile it was saved from; the per-key overrides below still apply on
    top (they are the live slider values).
    """
    if cfg["profile"] in PROFILES:
        params = dict(PROFILES[cfg["profile"]])
        # A built-in profile never picks the model any more (user rule
        # 15.09): profiles move the four sliders, the model is its own
        # control. A fresh config with no style key gets Natural; a saved
        # one keeps whatever the user chose (the override below).
        params["style"] = 1
    else:
        params = dict(load_presets(cfg).get(cfg["profile"], PROFILES["Natural"]))
        # A user preset DOES carry the model it was saved with - that is
        # what "save preset" promises.
    # The live style overrides either source: it is the value the user
    # last set in the menu, and it is saved on every menu close.
    style = cfg.get("style")
    if isinstance(style, int) and not isinstance(style, bool) and 0 <= style <= 2:
        params["style"] = style
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        # Saved presets are written by whatever build the user had, so they
        # are pulled into range here as well - validate_config only sees the
        # live slider values, not the presets behind them.
        if key in params:
            params[key] = clamp_param(key, params[key])
        value = cfg.get(key)
        if value is not None:
            params[key] = clamp_param(key, value)
    return params


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write data to path atomically: a temp file in the same directory,
    flushed and fsynced, then os.replace() over the target.

    A crash mid-write used to truncate config.json in place and the program
    lost the user's settings. The temp file lives next to the target so the
    replace is a rename within one volume - atomic on Windows. On failure the
    temp file is removed and the original is left untouched.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _autostart_enabled() -> bool:
    """Is autostart currently on? (HKCU Run, the NeuralScreen value)."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "NeuralScreen AMD")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _menu_layout_payload(cfg: dict, params: dict, monitor: int, lang: str,
                         work_scale: float, split_pos: float,
                         startup_menu: bool, nr_small: bool, menu) -> dict:
    """The settings _save_menu_layout persists into config.json.

    Everything the user can change in the menu: the panel geometry, the
    processing settings and the NR parameters. profile/params/monitor are
    included because the menu changes them in memory only (cfg/params are
    updated live) - without this save they would be lost on the next launch.

    monitor is saved as the DXGI devicename (e.g. '\\\\.\\DISPLAY1') so the
    saved monitor keeps pointing at the same physical display when the
    arrangement changes; old configs with a positional int still load.
    """
    monitor_name = devicename_for_output_idx(int(monitor))
    return {
        "menu_scale": round(menu.user_scale, 2),
        "menu_height": (None if menu.user_height is None
                        else int(menu.user_height)),
        "open_menu_on_start": startup_menu,
        "split": round(split_pos, 2),
        "nr_small": bool(nr_small),
        "work_scale": round(work_scale, 2),
        "theme": menu.state.get("theme", "light"),
        "lang": lang,
        "menu_offset": [int(menu.offset[0]), int(menu.offset[1])],
        "profile": cfg["profile"],
        "intensity": params["intensity"],
        "local_tone": params["local_tone"],
        "local_structure": params["local_structure"],
        "skin_structure": params["skin_structure"],
        # Style is a user choice now, not a property of the profile, so it
        # has to survive a restart like the four sliders do.
        "style": int(params.get("style", 1)),
        "monitor": monitor_name if monitor_name is not None else int(monitor),
        "rec_indicator": bool(cfg.get("rec_indicator", True)),
        "screenshot_dir": cfg.get("screenshot_dir") or "",
        # The Spout2 bridge choice must survive a restart: the worker
        # reads NS_SPOUT at startup, and main sets it from this flag.
        "spout": bool(cfg.get("spout", False)),
        # HDR compatibility, the same hand-off: the worker reads NS_HDR at
        # startup and main sets it from this flag. Experimental, off.
        "hdr": bool(cfg.get("hdr", False)),
        "motion_backend": cfg.get("motion_backend", "cpu"),
        # Which card runs the network and the capture. An index, as
        # DXGI enumerates adapters - the same number the worker takes
        # in NS_GPU and prints in its "[host] adapter N" lines.
        "gpu": int(cfg.get("gpu", 0)),
        # Adapters whose worker could not bring the neural pass up. Kept so
        # the picker can mark them after a restart too; cleared per adapter
        # as soon as one of them works (issue #33).
        "gpu_no_nr": [int(i) for i in (cfg.get("gpu_no_nr") or [])],
        # Skip static frames: no new frame from the capture - the network
        # idles instead of re-running on the same picture. A per-frame flag,
        # so it survives a restart through the config alone.
        "skip_static": bool(cfg.get("skip_static", False)),
        "frame_generation": bool(cfg.get("frame_generation", False)),
        "frame_multiplier": min(4, max(2, int(cfg.get("frame_multiplier", 2)))),
        # The user's saved presets. Without this key "Save preset" wrote
        # everything EXCEPT the preset: the menu said "Preset saved", the
        # save really did succeed, and the preset was gone on the next
        # launch - the code's own "it will not survive a restart" branch
        # could never fire, because nothing had failed (audit F1).
        "presets": dict(cfg.get("presets") or {}),
    }




def work_scale_cap(st) -> float:
    """The scale above which the work size just hits the NGX cap.

    Rounded down to the slider's own step so the value is reachable:
    a cap the slider cannot land on exactly would leave the top of the
    range doing nothing, which is the whole thing being fixed here.
    """
    raw = min(1.0, WORK_MAX_W / max(1, st.width), WORK_MAX_H / max(1, st.height))
    return max(0.35, int(raw / 0.05) * 0.05)


#: What the worker says about the neural pass, in its own words. ONE set,
#: read by both places that ask: settings_io.refresh_gpu_ok (which drives
#: the dot in the menu and the alert) and pipeline.gpu_came_up (which
#: decides whether a GPU switch is kept or reverted). They used to carry a
#: token list each, and the lists had already drifted apart - a rename in
#: the worker would have blinded one of them and left the other working,
#: which is the worst shape for a bug like this to take.
NR_VERDICT_OK = ("feature 18 ready",
                 "[amd] ===== AMD path active =====",)  # both neural passes
NR_VERDICT_FAIL = ("feature 18 create failed",   # [pure], the direct refusal
                   "NR feature unavailable",     # [video], SAFE PASSTHROUGH
                   "NGX unavailable",            # [host], nothing came up
                   "no NVIDIA adapter found",    # [host], nothing to run on
                   "[amd] the runtime did not come up:",  # [amd], the engine's own reason
                   # The three below are the SAME verdict written by other code
                   # paths, and they were missing - a live report caught it: the
                   # worker said "no neural runtime on this machine - SAFE
                   # PASSTHROUGH" and the menu kept its "processing" line, so a
                   # machine with no pass at all looked healthy. Every
                   # SAFE PASSTHROUGH the worker can print has to be readable
                   # here, or the user is told the opposite of the truth.
                   "no neural runtime on this machine",   # [video], NGX-less run
                   "the neural pass is not running",      # [video], generic
                   "RNSZ: the AMD pass is not running",   # [video], resize path
                   "RNSZ: feature create failed")         # [video], resize path

#: The pass did not start for a reason that is about the INSTALL, not the card:
#: a missing runtime file, the wrong build, no HIP. Kept apart from the failure
#: list above because the two mean different things to a caller that is deciding
#: what to do about it - the menu shows both as "no neural pass", but a GPU
#: switch must NOT be reverted, and the card must NOT be marked, on a fault the
#: user fixes by copying a file (issue #2: clicking through the picker looped,
#: because every card was judged and condemned for a file that was not there).
NR_VERDICT_INSTALL = ("not found in",
                      "is not a build these offsets belong to",
                      "amdhip64_7.dll not found",
                      "HIP reports no devices",
                      "dlssnr_on_amd_weights.bin not found",
                      "not found next to the runtime")

#: The card is not the kind the pass runs on. A verdict of its own: nothing
#: is broken and nothing the user can install will change it, so the menu
#: says "card not supported" rather than "no neural pass".
NR_VERDICT_UNSUPPORTED = ("[amd] this GPU is not supported",
                          "no AMD adapter found")


def nr_verdict_unsupported(lines) -> bool:
    """Whether the refusal was the HARDWARE rather than a fixable fault."""
    return any(token in line for line in lines
               for token in NR_VERDICT_UNSUPPORTED)


def nr_verdict_is_install(lines) -> bool:
    """Whether the pass stopped for an INSTALL fault rather than a card one.

    Read by pipeline.gpu_came_up: a missing file says nothing about the card,
    so a GPU switch must not be reverted on it.
    """
    return any(token in line for line in lines for token in NR_VERDICT_INSTALL)


def nr_verdict(lines):
    """True / False / None from the worker's log lines, NEWEST FIRST.

    None means the worker has not said yet - which is not a failure. A card
    that takes its time still works, and treating silence as a refusal
    would be worse than the bug the callers guard against.

    The "card not supported" line is an absolute statement about the
    hardware, and it is emitted BEFORE the pass gives up, so it is checked
    over the whole window first: newest-first alone would read the later
    "the neural pass is not running" line as the verdict and lose the
    reason the user can act on.

    The caller may hand over an iterator (`reversed(...)`), and this walks
    the lines twice - so it is materialised once, here.
    """
    lines = list(lines)
    for line in lines:
        if any(token in line for token in NR_VERDICT_UNSUPPORTED):
            return False
    for line in lines:
        if any(token in line for token in NR_VERDICT_OK):
            return True
        if any(token in line for token in NR_VERDICT_FAIL):
            return False
    return None


def refresh_gpu_ok(st) -> None:
    """Whether NR works - from the worker's answer, not the architecture.

    Only the worker knows for sure: it calls CreateFeature and gets
    the NGX code back. The architecture only tells us what NVIDIA
    promises. Once decided, the answer is not revisited - worker
    restarts add lines but the verdict does not change.
    """
    if st.gpu_ok is not None:
        return
    # The refusal lines are named once, in nr_verdict: the real one from the
    # worker is "[pure] direct feature 18 create failed" ("Unsupported GPU
    # architecture" lives inside nvngx_dlssnr.dll and never reaches its
    # stderr), and SAFE PASSTHROUGH is the same verdict - the worker stays
    # alive and shows the raw frame, so: no feature, no NR.
    tail = list(st.worker_logs[-80:])
    verdict = nr_verdict(reversed(tail))
    if verdict is None:
        return
    st.gpu_ok = verdict
    # Which reason it was. The menu says "card not supported" for the one
    # the user cannot fix and "no neural pass" for everything else.
    st.gpu_unsupported = bool(verdict is False and nr_verdict_unsupported(tail))
    if not verdict:
        # The red dot alone was not enough: in issue #29 the user picked a
        # card that cannot run the pass and nothing on screen said so. One
        # alert per verdict - a fresh worker clears gpu_ok and the alert can
        # speak again.
        if not st.gpu_alerted:
            st.gpu_alerted = True
            # Eight seconds, not the usual two and a half. This is not a
            # notification that something was applied - it is the reason the
            # picture will look untouched for the rest of the session, and
            # the people who reported it as a black screen had not seen it
            # at all. The standing version of the same fact is in the menu's
            # status line, for whoever looks later.
            #
            # The hardware verdict gets its own sentence: "this card is not
            # supported" is the end of the conversation, while "cannot run
            # the pass" invites the user to look for a fix that may exist.
            if st.gpu_unsupported:
                st.display.alert(UI_STRINGS[st.lang].get(
                    "gpu_unsupported_long",
                    "This graphics card is not supported by the AMD neural "
                    "pass - it needs a Radeon RX 7000 or 9000 (RDNA3/RDNA4). "
                    "The program keeps working; the picture is not processed."),
                    10.0)
            else:
                st.display.alert(UI_STRINGS[st.lang].get(
                    "gpu_nr_fail",
                    "This GPU cannot run the neural pass - the picture stays "
                    "unprocessed"), 8.0)


# FG's own refusal lines, the same shape as NR's: the worker says what the
# runtime answered. Init_Ext logs its HRESULT on every attempt, so success
# must be excluded by VALUE (NVSDK_NGX_Result_Success is 0x00000001).
FG_VERDICT_FAIL = ("[fg] CreateFeature failed",
                   "[fg] nvngx_dlssg.dll load failed",
                   "[fg] presenter failed")


def fg_verdict(lines):
    """True / False / None from the worker's FG lines, NEWEST FIRST.

    Only lines NEWER than the last "[fg] UI: on" marker count: a refusal
    from an earlier, already-handled attempt must not flip the switch
    again. Success is the presenter's own "[fg] Nx enabled at ..." line.
    None means the runtime has not answered yet.
    """
    for line in lines:
        if "[fg] UI: on" in line:
            return None            # just enabled - no answer yet
        if "[fg] UI: off" in line:
            return None            # disabled - nothing to judge
        if "enabled at" in line and "[fg]" in line:
            return True            # "[fg] 2x enabled at 3840x2160"
        if "[fg] Init_Ext -> 0x" in line and "0x00000001" not in line:
            return False           # the FG runtime itself refused
        if any(token in line for token in FG_VERDICT_FAIL):
            return False
    return None


def refresh_fg_ok(st) -> None:
    """The FG switch reflects reality (issue #76).

    When the FG runtime refuses on this card the switch used to stay ON
    while nothing interpolated - no rate split in the header, no reason
    anywhere on screen. The worker knows (it logs the refusal), so on a
    refusal the switch goes back off with an alert naming the reason.
    Once per attempt: flipping the switch back on arms the alert again.
    """
    if not bool(st.cfg.get("frame_generation", False)):
        return
    if getattr(st, "fg_alerted", False):
        return
    verdict = fg_verdict(reversed(st.worker_logs[-200:]))
    if verdict is not False:
        return
    st.fg_alerted = True
    st.cfg["frame_generation"] = False
    save_menu_layout(st)
    # Eight seconds like the NR refusal: this is the reason frame
    # generation will not appear at all, not a notification that something
    # was applied.
    st.display.alert(UI_STRINGS[st.lang].get(
        "fg_fail",
        "Frame Generation could not start on this GPU - the switch is "
        "back off"), 8.0)


def warn_hdr(st) -> None:
    """Warn only when an HDR display actually uses the SDR capture path.

    Display discovery precedes the first frame. Wait for its capture format:
    FP16 scRGB uses an SDR neural proxy and preserves the original HDR signal.
    """
    if st.hdr_alerted:
        return
    capture_sdr = None
    for line in reversed(st.worker_logs):
        if capture_sdr is None and "[hdr] capture=" in line:
            capture_sdr = "capture=SDR;" in line
        if "[dda] output colour space " in line:
            if "HDR IS ON for the captured display" not in line or capture_sdr is not True:
                return
            st.hdr_alerted = True
            st.display.alert(UI_STRINGS[st.lang].get(
                "hdr_on",
                "HDR display is using SDR capture. HDR brightness and colours "
                "are not preserved."), duration=6.0)
            print("[main] HDR display is using SDR capture; HDR is not preserved")
            return


def _no_nr(st) -> set:
    """Adapter indices whose worker could not bring the neural pass up."""
    try:
        return {int(i) for i in (st.cfg.get("gpu_no_nr") or [])}
    except (TypeError, ValueError):
        return set()


def _gpu_label(index) -> str:
    """"<dxgi index>: <name>" for the picker - the card that will really run.

    The value in the config is a DXGI index and it can name something that
    is not an NVIDIA card (a hybrid laptop's integrated GPU sits at 0, which
    is the shipped default) or nothing at all. The worker treats the index
    as a wish and falls back to the first usable card; the menu has to agree
    with it, or the picker shows an empty field on the machines where the
    setting matters most (issue #34).
    """
    adapters = list_adapters()
    if not adapters:
        return ""
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        wanted = None
    for i, name in adapters:
        if i == wanted:
            return f"{i}: {name}"
    i, name = adapters[0]
    return f"{i}: {name}"



def _fg_displayed_fps(st) -> float | None:
    """The frame rate the presenter actually shows (real + generated).

    The worker reports it every two seconds - "[fg] displayed 87.1 FPS".
    The pipeline counter stays the honest network rate; this is what the
    screen really shows with Frame Generation on. None while FG is off.
    """
    for line in reversed(st.worker_logs[-200:]):
        if "[fg] displayed" in line:
            try:
                return float(line.split("displayed ", 1)[1].split(" FPS", 1)[0])
            except (ValueError, IndexError):
                return None
        # A marker line for FG-off resets the reading - the toggle logs one.
        if "[fg] UI: off" in line:
            return None
    return None



def _worker_idle(st) -> bool:
    """Is the network idling on an unchanged screen right now?

    The worker announces a stretch ONCE - "[skip] no new frame" - and
    announces its end when the screen moves again. So the state is the
    LATEST of those two markers, not the presence of the first one in the
    last few lines: the menu used to look at worker_logs[-3:], and one
    unrelated line was enough to push the single announcement out and turn
    the readout back into a frame rate while nothing was being processed
    (audit).

    The window is bounded because this runs on every frame the menu is open:
    during a stretch the worker is otherwise quiet, so 200 lines is a long
    way past the marker, and a scan of the whole 2000-line buffer sixty
    times a second is not worth the difference.
    """
    for line in reversed(st.worker_logs[-200:]):
        if "[skip] no new frame" in line:
            return True
        if "[skip] the screen changed" in line:
            return False
    return False


def menu_payload(st) -> dict:
    """The current state for the menu - a single source of truth."""
    refresh_gpu_ok(st)
    # The list is FROZEN while the page that shows it is open, and sorted by
    # title rather than by z-order.
    #
    # EnumWindows answers in z-order, this payload is rebuilt on every frame
    # the menu is up, and z-order changes whenever anything takes the focus -
    # including the window the pointer is travelling towards. So the rows
    # re-ordered under the cursor between the hover and the click, and the
    # click landed on whatever had moved into that position: picked
    # WireGuard, got Claude (user, 13.09).
    #
    # Frozen means frozen: a window that appears or closes while the list is
    # up does not shuffle the rows either. Closing the page and opening it
    # again takes a fresh reading - which is the only way to take one, and
    # enough: the picker is a few clicks, not a live monitor.
    _menu = getattr(getattr(st, "display", None), "menu", None)
    if getattr(_menu, "page", "") == "windows" and getattr(st, "window_list", None):
        wins = st.window_list
    else:
        wins = sorted(list_capturable_windows(),
                      key=lambda hw: (str(hw[1]).casefold(), hw[0]))
        st.window_list = wins
    # The devicename is the stable identity: the menu hands it back
    # on a switch, so a reorder cannot redirect the capture.
    monitor_entries = [f"{i}: {w}x{h} ({dev})"
                       for i, w, h, dev in list_monitors()]
    return {
        "nr": not st.paused,
        "work_scale": st.work_scale,
        # Where the work size hits the 2560x1440 cap. Everything above
        # it lands on the same resolution, so the slider puts "the whole
        # screen" there instead of a dead stretch.
        "work_scale_cap": work_scale_cap(st),
        "work_scale_min": WORK_SCALE_MIN,
        "nr_small": st.nr_small,
        "screen_size": f"{st.width}x{st.height}",
        "profile": st.cfg["profile"],
        "profiles": list(PROFILES) + list(st.presets),
        "preset_active": st.cfg["profile"] in st.presets,
        "params": {k: st.params[k] for k in
                   ("intensity", "local_tone",
                    "local_structure", "skin_structure")},
        # What the CURRENT profile puts each parameter at. The menu draws it
        # as a tick under the slider, so "how far have I moved this from
        # Natural" is visible instead of remembered.
        # The range each slider draws. It lives here, next to the
        # measurement that set it, rather than being a second copy of the
        # numbers inside the menu.
        "param_ranges": {k: list(v) for k, v in PARAM_RANGE.items()},
        # Which of the three looks is live - its own control since the
        # measurement showed it is the strongest lever we have.
        "style": int(st.params.get("style", 1)),
        "param_defaults": {
            k: float(v) for k, v in
            (PROFILES.get(st.cfg["profile"])
             or st.presets.get(st.cfg["profile"]) or {}).items()
            if k in ("intensity", "local_tone", "local_structure",
                     "skin_structure")},
        "lang": st.lang,
        "recording": st.recorder is not None,
        "work_size": f"{st.work_w}x{st.work_h}",
        "rec_seconds": (st.recorder.duration_ms / 1000.0) if st.recorder else 0.0,
        "rec_indicator": bool(st.cfg.get("rec_indicator", True)),
        "screenshot_dir": st.cfg.get("screenshot_dir") or "",
        "spout": bool(st.cfg.get("spout", False)),
        "hdr": bool(st.cfg.get("hdr", False)),
        "motion_backend": st.cfg.get("motion_backend", "cpu"),
        "skip_static": bool(st.cfg.get("skip_static", False)),
        "frame_generation": bool(st.cfg.get("frame_generation", False)),
        "frame_multiplier": min(4, max(2, int(st.cfg.get("frame_multiplier", 2)))),
        # Is the network idling on an unchanged screen right now? The
        # worker says so in its log; without this the menu shows a
        # healthy FPS while nothing is being processed, and the skip
        # reads as "it does not work" (user, 12.09).
        "idle": _worker_idle(st),
        # What the presenter actually shows while FG interpolates; the
        # HUD pairs it with the network rate as "42 / 84 fps".
        "display_fps": _fg_displayed_fps(st),
        # The list, with a note on any adapter whose worker could not bring
        # the neural pass up. DXGI reports some cards twice (one user has a
        # single 5080 listed as adapters 0 and 2) and the two entries are
        # indistinguishable by name - so the menu offered a choice between
        # two identical-looking lines, one of which kills the pipeline
        # (issue #33). The note is what we actually know: it was tried and
        # it did not work. The entry stays selectable.
        "gpus": [f"{i}: {name}" + (f" - {UI_STRINGS[st.lang].get('gpu_no_nr', 'no neural pass')}"
                                   if i in _no_nr(st) else "")
                 for i, name in list_adapters()],
        # The saved index may name no NVIDIA card at all. On a hybrid laptop
        # adapter 0 is the integrated GPU and "gpu": 0 is what the program
        # ships with, so the picker came up EMPTY on exactly the machines
        # where the setting matters most (issue #34). The worker already
        # falls back to the first usable card in that case - the menu says
        # the same thing now instead of showing a blank.
        "gpu": _gpu_label(st.cfg.get("gpu")),
        "open_on_start": st.startup_menu,
        "autostart": _autostart_enabled(),
        "split": st.split_pos,
        "gpu_text": st.gpu_text,
        # The four facts the log header carries, for the About block. A
        # reporter can read them off the menu instead of being asked which
        # version and which driver - which is the first exchange on almost
        # every issue.
        "about": dict(getattr(st, "environment", None) or {},
                      gpu=st.gpu_text or ""),
        "gpu_ok": st.gpu_ok,
        # The reason behind a failed verdict, when the reason is the card
        # itself. The menu's status line picks its sentence off this.
        "gpu_unsupported": bool(getattr(st, "gpu_unsupported", False)),
        "window_mode": st.window_hwnd is not None,
        "monitor_devicename": st.capture.devicename,
        "monitors": monitor_entries,
        "monitor": next(
            (m for m in monitor_entries
             if m.startswith(f"{st.monitor}: ")),
            str(st.monitor)),
        "windows": [f"{h:X}: {t}" for h, t in wins],
        "window_current": next(
            (f"{h:X}: {t}" for h, t in wins if h == st.window_hwnd), ""),
        "version": APP_VERSION,
        "channel": CHANNEL_LABEL,
    }


def save_menu_layout(st) -> bool:
    """Remember the panel size and position in config.json.

    We write on menu close and on exit rather than on every mouse
    move: dragging would otherwise hammer the file dozens of times
    per second. Returns False when the write failed - the callers
    that promise the user something (presets, hotkeys) show an
    alert then.
    """
    try:
        data = json.loads(st.cfg_path.read_text(encoding="utf-8"))
        data.update(_menu_layout_payload(
            st.cfg, st.params, st.monitor, st.lang, st.work_scale, st.split_pos,
            st.startup_menu, st.nr_small, st.display.menu))
        _atomic_write_json(st.cfg_path, data)
        return True
    except Exception as exc:
        print(f"[main] could not save the menu layout: {exc}", file=sys.stderr)
        return False


def save_hotkeys(st, mapping: dict) -> bool:
    """Write the assignments into config.json.

    Separate from _save_menu_layout: that one runs on menu close,
    while the user expects a key to be saved right away. Returns
    False when the write failed - the caller shows an alert.
    """
    try:
        data = json.loads(st.cfg_path.read_text(encoding="utf-8"))
        data["hotkeys"] = dict(mapping)
        _atomic_write_json(st.cfg_path, data)
        return True
    except Exception as exc:
        print(f"[main] could not save the hotkeys: {exc}",
              file=sys.stderr)
        return False
