"""Backend selection, restart, fallback and scene tracking without NVIDIA hardware."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import numpy as np
import pygame
import commands
import pipeline
import settings_io
from guides import TemporalGuideGenerator
from motion_backend import MotionBackendStatus, normalize_backend
from test_ui_buttons import build, paint, find
from test_config_atomic import _payload, GOOD
import json
import tempfile


def main():
    native = (ROOT / "native" / "nvofa.inl").read_text(encoding="utf-8-sig")
    dump = native.split("static void DumpNvofa(VideoState &v)", 2)[-1]
    assert "if (!pair.first) continue;" in dump, \
        "an ordinary NVOFA dump must skip the opt-in cost texture when absent"
    confidence = (ROOT / "tests" / "experiment_nvofa_confidence.py").read_text(
        encoding="utf-8")
    assert "NS_NVOFA_COST='1'" in confidence, \
        "the confidence experiment reads cost files and must request the channel"

    assert all(normalize_backend(v) == "cpu" for v in [None, {}, [], 1, "gpu", "CPU"])
    assert normalize_backend("nvofa") == "nvofa"
    # The AMD pass is a backend value of its own on this build.
    assert normalize_backend("amd") == "amd"
    assert _payload()["motion_backend"] == "cpu"
    assert _payload(dict(GOOD, motion_backend="nvofa"))["motion_backend"] == "nvofa"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        for value in (None, "nvofa", "obsolete"):
            path.write_text(json.dumps(dict(GOOD, motion_backend=value)), encoding="utf-8")
            assert settings_io.load_config(path)["motion_backend"] == normalize_backend(value)
    state = MotionBackendStatus()
    worker, replacement = object(), object()
    assert not state.update(worker, [])
    assert state.update(worker, ["[nvofa] active: driver"])
    assert state.update(worker, ["old logs evicted"])
    assert not state.update(worker, ["[nvofa] active: driver", "[nvofa] unavailable: execute"])
    assert state.failed
    assert not state.install_fault, "an NVOFA failure is not an install fault"
    assert not state.update(worker, []) and state.failed
    assert not state.update(replacement, []) and not state.failed

    # A missing AMD runtime is an INSTALL fault, NOT a motion-backend failure.
    #
    # Both used to set `failed`, and `main.py` reads that flag to show an alert
    # whose text is about NVOFA - so a Radeon owner who had never selected
    # NVOFA was told "NVOFA unavailable - using CPU DIS" and asked why the
    # program "always looks to hook on NVOFA" (issue #2, second report).
    runtime_missing = MotionBackendStatus()
    assert not runtime_missing.update(
        worker, ["[amd] the runtime did not come up: dlssnr_amd_pass1.dll not found"])
    assert runtime_missing.install_fault, \
        "a missing runtime must be reported as an install fault"
    assert not runtime_missing.failed, \
        "a missing runtime must NOT read as the motion backend failing - that " \
        "alert blames NVOFA, which this run never tried"
    assert not runtime_missing.amd
    # ...and the AMD pass coming up clears it again.
    assert runtime_missing.update(worker, ["[amd] ===== AMD path active ====="])
    assert not runtime_missing.install_fault and runtime_missing.amd

    # The main loop must actually USE the new state, and keep the old alert for
    # the case it was written for.
    main_src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    assert "motion_status.install_fault" in main_src, \
        "main.py never consults install_fault, so a missing runtime still " \
        "shows the NVOFA message"
    assert '"amd_runtime_missing"' in main_src, \
        "the install fault has no message of its own"
    assert '"motion_fallback"' in main_src, \
        "the NVOFA alert was replaced instead of being kept for its own case"
    for key in ("en", "ru"):
        assert "amd_runtime_missing" in settings_io.UI_STRINGS[key], \
            f"the alert text is missing from the {key} strings"

    guide = TemporalGuideGenerator(320, 180, emit_small=True)
    rng = np.random.default_rng(17)
    gray = rng.integers(60, 170, (180, 320), dtype=np.uint8)
    guide.process(gray=gray)
    original = guide.dis
    guide.dis = Mock(wraps=original)
    shifted = np.roll(gray, 3, axis=1)
    assert not guide.process(gray=shifted, compute_motion=False).motion.any()
    guide.dis.calc.assert_not_called()
    # Fallback resumes against the last captured image, not a stale CPU pair.
    assert np.array_equal(guide.previous_gray, shifted)
    guide.process(gray=np.roll(shifted, 2, axis=1))
    guide.dis.calc.assert_called_once()
    assert guide.process(gray=np.full_like(gray, 255), compute_motion=False).reset

    st = SimpleNamespace(cfg={}, lang="en")
    with patch.dict(os.environ), patch.object(settings_io, "save_menu_layout") as save, \
         patch.object(pipeline, "teardown_pipeline") as down, \
         patch.object(pipeline, "rebuild_pipeline") as up:
        commands.apply_menu_action(st, ("motion_backend", "nvofa"))
        assert st.cfg["motion_backend"] == os.environ["NS_MOTION_BACKEND"] == "nvofa"
        save.assert_called_once_with(st); down.assert_called_once_with(st)
        assert up.call_count == 1
        pipeline.apply_motion_backend(st, "nvofa")
        assert up.call_count == 1
        pipeline.apply_motion_backend(st, "cpu")
        assert os.environ["NS_MOTION_BACKEND"] == "cpu" and up.call_count == 2

    pygame.init()
    try:
        menu = build(); menu.page = "settings"; paint(menu)
        item = find(menu, "choice", "motion_backend")
        assert item and item.payload == ["cpu", "nvofa", "amd"]
        assert menu._pick("motion_backend", "amd") == [("motion_backend", "amd")]
        assert menu._pick("motion_backend", "nvofa") == [("motion_backend", "nvofa")]
    finally:
        pygame.quit()
    print("PASS: NVOFA selection/restart, fallback/restart tracking, CPU resume and scene cuts")


if __name__ == "__main__":
    main()
