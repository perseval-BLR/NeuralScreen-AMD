"""Run everything: the static checks, the regression tests, the smoke test.

There is one reason this file exists. The tests were run by hand, one at a
time, and that is how a regression shipped: audit #3 broke the shared-memory
pixel channel, `test_out_shm.py` caught it, and nobody ran `test_out_shm.py`.
One command, one verdict, a non-zero exit on any failure.

    runtime\\python.exe run_tests.py            static + tests + smoke
    runtime\\python.exe run_tests.py --gui      ... and the full GUI cycle
    runtime\\python.exe run_tests.py --only out_shm      one test by name
    runtime\\python.exe run_tests.py --no-smoke          skip the smoke test

Two of the stages take over the screen for a few seconds each (the overlay is
raised for real) and the machine should be left alone while they run. Nothing
here needs the network except the release-notes check inside autocheck.

Only tests tracked by git are run: the working copy also holds throwaway
probes, and those are nobody's regression suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Console encodings, both directions: the children are told to print utf-8 so
# their output decodes here, and our own stdout replaces anything the console
# codepage cannot represent instead of dying on it.
#
# NS_MOTION_BACKEND=cpu is deliberate, and it is what makes this suite mean
# anything on this branch. The AMD build defaults to the AMD neural pass
# (AmdPathRequestedEarly: no recorded choice means "amd"), and the AMD pass
# cannot run on a machine without a Radeon AND the third-party runtime - on
# any other box it degrades to a passthrough, by design. The tests below drive
# the worker DIRECTLY (Popen, no startup.py), so without this they inherited
# the branch default and measured the passthrough: 14 of them failed with
# "the network did not run" / "changes nothing between its two ends", which
# reads as 14 broken features and is really one wrong backend. A suite that
# cries wolf on every run is a suite nobody reads - and a real regression
# hides in the noise.
#
# These tests are the NGX-pipeline tests: they name the path they measure.
# The AMD default is covered by test_amd_worker_survives (the condition that
# used to kill the worker) and by the live runs on Radeon machines.
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8",
                 NS_MOTION_BACKEND="cpu")

#: Tests that must NOT get the default above. They measure the CAPTURE path
#: (WGC/DDA window capture, its frame pool, its size handling), which is
#: upstream of the neural pass and indifferent to which backend is selected -
#: but not indifferent to being handed an override it never asked for:
#: test_wgc_capture runs OK/FAIL/OK on one unchanged build with the variable
#: set (measured, three runs), while it is a clean pass without it. A flaky
#: test is worse than a missing one: it teaches people to ignore the summary.
NO_DEFAULT_BACKEND = {"test_wgc_capture.py", "test_dda_capture.py"}
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent  # the project root (tests/ lives inside it)
PY = ROOT / "runtime" / "python.exe"
TIMEOUT = 600
# How long to wait for the previous test's processes to die before
# starting the next one. See settle().
SETTLE_LIMIT = 10.0
#: (label, seconds) per test - reported in the summary so a run where
#: nothing ever waited is visible as evidence, not as silence.
SETTLES: list = []

# What each test is for, in one line - a failing name should not send anyone
# digging through the file to find out what broke.
ABOUT = {
    "test_nvofa_controls.py": "motion backend selection, CPU fallback and scene tracking without NVIDIA hardware",
    "test_bypass.py": "NR OFF shows the raw capture and the pipeline survives",
    "test_out_ring.py": "read_out reuses its buffers and never overwrites one in use",
    "test_verdict_forget.py": "the feature-18 verdict dies with the worker that gave it",
    "test_audio_limiter.py": "the soft limiter keeps the recording from clipping",
    "test_audio_pack.py": "the audio format structs are byte-packed, truncated formats rejected",
    "test_adaptive_exposure.py": "adaptive exposure brightens dark scenes, lit scenes untouched",
    "test_capture_visibility.py": "outside capture sees the overlay only without WDA",
    "test_config.py": "the config loader validates, clamps and resolves",
    "test_theme_rebuild.py": "the chosen theme survives a pipeline rebuild",
    "test_gpu_select.py": "the GPU picker drives config, environment and restart",
    "test_hdr_switch.py": "HDR compatibility is off by default and switchable in 12 languages",
    "test_settings_hints.py": "every hint is one line and fits its row, in 12 languages",
    "test_rebuild_warmup.py": "a rebuild warms up briefly; only a cold launch pays 120 frames",
    "test_param_apply.py": "a parameter change applies without rebuilding the feature",
    "test_param_effect.py": "every menu parameter changes the picture; the dead ones are named",
    "test_direct_reconstruction.py": "the two Boost composites are a live switch, and they differ",
    "test_motion_trust.py": "vectors where nothing moved are dropped before NGX sees them",
    "test_live_resize.py": "a window resize reconfigures the worker instead of replacing it",
    "test_spout_adapter.py": "the Spout bridge lives on the card the worker runs on",
    "test_present_window_leak.py": "closing the picture window destroys it, six cycles",
    "test_pixels_after_resize.py": "a resize does not put a hole in the recording",
    "test_alert_position.py": "alerts sit at the top centre of the screen, not the overlay",
    "test_stdout_noise.py": "a stray printf cannot corrupt the worker protocol",
    "test_hdr_shaders.py": "the HDR capture and composite shaders, run on WARP",
    "test_hdr_capture.py": "the whole HDR path on real hardware (runs only on an HDR display)",
    "test_gpu_rollback.py": "a card that cannot run the network reverts itself",
    "test_monitor_resize.py": "a desktop resolution change rebuilds once, after it settles",
    "test_save_dialog.py": "the Save As struct is valid; the fallback still catches",
    "test_window_follow_size.py": "capture size vs frame size: no rebuild loop",
    "test_module_layers.py": "no module imports main, no dangling names, all import clean",
    "test_amd_state_bookkeeping.py": "every AMD bridge barrier starts from a real state and none is a no-op",
    "test_amd_slot_discipline.py": "no two passes in one command list share a descriptor slot: the GPU resolves tables at execution time",
    "test_hud_frame_delta.py": "the HUD's frame delta is named for what it is, and a reset frame says it is a reset",
    "test_amd_tripwire.py": "three surfaces of one frame: the capture, the converted input and the dispatch output each name a different failing side",
    "test_amd_probe_spec_match.py": "the probe verifies its spec against the resource before copying, so a diagnostic cannot remove the device",
    "test_amd_probe_sentinel.py": "the probe poisons its readback and refuses to report a mean when the copy never executed",
    "test_amd_interop_switch.py": "NS_AMD_INTEROP selects the copy arm without changing the default, and the log says which arm ran",
    "test_rnsz_failure_stops.py": "a failed RNSZ stops, is reported, and releases what it built - it does not answer ok",
    "test_capture_output_log.py": "the capture log names every output the adapter exposes, before the NS_OUTPUT match",
    "test_amd_rnsz_verdict.py": "the RNSZ rebuild verdict asks NrReady(), so a live AMD pass is not called SAFE PASSTHROUGH",
    "test_amd_dispatch_shapes.py": "A is work->work with motion vectors and B is work->out without them, so the engine staging table stays readable",
    "test_amd_engine_shutdown.py": "the engine is stopped before the device it runs on is released",
    "test_defer_tail_verdict.py": "the defer-tail choice says whether it is on and which fact decided it",
    "test_amd_exposure_update.py": "the engine is handed this frame's exposure, not a value frozen at startup",
    "test_amd_probe_cadence.py": "the probe can run every frame on request, and it measures the presented surface",
    "test_amd_probe_upscale_output.py": "the probe measures the FSR upscale's output when it exists, in the state the frame leaves it in, and reports it",
    "test_amd_exposure_curve.py": "the AMD path's exposure curve can bring bright content down, and it is reversible",
    "test_amd_runtime_traps.py": "the AMD hook wait is a real wait and the intensity slider reaches the network",
    "test_amd_engine_init.py": "the engine-init verdict is asked after the swapchain exists, the retry is bounded, a dead engine is not fed, and the mean is not called black",
    "test_amd_own_measure.py": "the host measures its own surface with the engine's metric, on its own submission",
    "test_amd_recreate_flag.py": "the engine's staging-rebuild flag is mapped, verified against the image, and reported rather than gated",
    "test_amd_dump_identity.py": "a dump says which image it came from, and the freshness claim matches the filter it describes",
    "test_amd_exposure_bound.py": "the 1x1 exposure reaches the FFX dispatch field named for it, with auto-exposure off",
    "test_diagnostic_bundle.py": "the bundle carries both logs, our own dump in preference to WER's, and never a previous session's crash",
    "test_amd_install_verdict.py": "a missing runtime file is not read as a bad graphics card",
    "test_amd_worker_survives.py": "a Radeon keeps its worker when the pass is off, and the magenta fill cannot reach an unkeyed layer",
    "test_verdict_sentences.py": "every refusal the worker can print reaches the menu as one",
    "test_amd_single_route.py": "one route feeds the engine (no packet on the default path) and the wait matches it",
    "test_amd_probe_and_echo.py": "the probe resolves its folder before loading, and the health echo reads only this run",
    "test_amd_offsets_agree.py": "the runtime offset table lives in one place and every copy of it agrees",
    "test_pacing.py": "the frame limiter holds the rate, never catches up, and is inert unless a cap is set",
    "test_config_atomic.py": "the config write is atomic and persists profile/params/monitor",
    "test_dred_diag.py": "the worker logs DRED/device-removed diagnostics at startup",
    "test_env_header.py": "the log header carries version/OS/HDR and survives broken probes",
    "test_hotkey_once.py": "one press of a hotkey fires exactly one command",
    "test_hotkey_rebind.py": "hotkeys can be reassigned from the config",
    "test_hotkey_bindings.py": "parsing, aliases and defaults survive binding changes",
    "test_hotkey_held.py": "a key held at startup is the baseline, not an event",
    "test_hotkey_remap_no_fire.py": "a key pressed during a remap is the baseline, not an event",
    "test_header_footer.py": "the header collapse icon and the one-window footer button",
    "test_i18n.py": "every language has the same keys, none empty",
    "test_menu_position.py": "the menu position is fixed - saved offset honoured, clamped",
    "test_monitor_identity.py": "monitors are identified by DXGI devicename, not by position",
    "test_monitor_switch.py": "a monitor switch cannot crash the capture - stale indices fall back",
    "test_mss_fallback.py": "the GDI fallback (mss) opens when dxcam cannot (Optimus)",
    "test_menu_scroll.py": "the menu scrolls and the wheel lands where it should",
    "test_menu_state_keys.py": "every key menu_payload produces reaches the menu",
    "test_silent_spots.py": "the silent spots from issue #29 speak: log, alert, split warning",
    "test_skip_static.py": "static frames are skipped; transitions and want_pixels are not",
    "test_monitor_origin.py": "the chosen monitor's origin reaches overlay and worker",
    "test_monitor_windows.py": "both output windows really follow the chosen monitor",
    "test_overlay_toolwindow.py": "the overlay is a tool window - one taskbar button only",
    "test_motion_small.py": "the downscaled motion field is upscaled on the GPU",
    "test_mv_validation.py": "noise-floor motion vectors are zeroed, real motion survives",
    "test_nr_small.py": "the reduced-resolution mode produces a real picture",
    "test_out_shm.py": "the pixel channel through shared memory",
    "test_out_status.py": "0x00000000 is a skipped frame, only 0xBAD00000 raises",
    "test_odd_frame_size.py": "a frame whose row pitch needs padding survives",
    "test_recorder_audio.py": "the audio track keeps up with the video",
    "test_recorder_even_size.py": "the recorder opens at even dimensions, so a codec that requires yuv420p cannot abort on the first frame",
    "test_recorder_even_size_live.py": "an odd-width recording encodes real frames and reads back, where the reporter got a 924-byte file",
    "test_recorder_fallback.py": "the NVENC codec chain falls back AV1->HEVC->H.264",
    "test_rec_indicator.py": "the recording indicator draws only while recording, never in the file",
    "test_bake_menu_position.py": "the baked menu lands where the user saw it, not at the frame centre",
    "test_switch_window_layer.py": "the layer geometry is written when the mode changes, not when the menu closes",
    "test_amd_framegen_refusal.py": "Frame Generation is refused on a Radeon at the one gate, logged, and shown as unavailable instead of offered",
    "test_probe_launcher.py": "the per-frame probe ships as a one-click launcher, so a reporter never sets the variable by hand",
    "test_reveal_below_panel.py": "the picture is shown below the panel in one guarded operation, not on top of it",
    "test_zorder_decision_log.py": "the z-order guard names what it saw and which branch it took",
    "test_shot_dir.py": "the screenshot folder is configured, persisted and shown on the button",
    "test_spout_toggle.py": "the Spout2 toggle drives the config, the environment and the restart",
    "test_swappable_runtime.py": "the swappable runtime is driven by the config",
    "test_presets.py": "user presets load, apply, and survive a broken config",
    "test_recovery.py": "only 0xBAD00001 is a hard failure, everything else auto-revives",
    "test_recorder_thread.py": "the encoder thread and a clean close",
    "test_residual.py": "the matched residual composite keeps 1:1 detail at reduced work",
    "test_residual_split.py": "residual and the wipe compose in the same frame",
    "test_resize.py": "the on-the-fly resize reconfigures the feature without a restart",
    "test_reveal.py": "the present window stays hidden until the first Present",
    "test_focus_z_order.py": "focused target stays below the worker picture and HUD",
    "test_split.py": "the before/after wipe leaves the left side untouched",
    "test_taskbar_window.py": "the taskbar button exists, opens the menu, closes cleanly",
    "test_ui_buttons.py": "every control on every page fires the right command",
    "test_controls_visual.py": "the switch, the slider ticks and the Quit edge draw",
    "test_wgc_capture.py": "the worker captures one window (the single-window input)",
    "test_window_filter.py": "the window list holds only real taskbar windows",
    "test_windows_page.py": "the windows page lists, highlights and switches",
    "test_window_mode.py": "the one-window hotkey switches the pipeline and back",
    "test_window_surround.py": "a window-sized frame's surround is keyed, the layer is really keyed",
    "test_window_mode_menu.py": "the menu stays fully visible across the window-mode switch",
    "test_switch_veil.py": "the mode-switch veil eases in/out and owns the layer",
}


def tracked_tests() -> list:
    try:
        out = subprocess.check_output(["git", "ls-files", "tests/test_*.py"], cwd=ROOT,
                                      text=True, encoding="utf-8", errors="replace")
        names = [n.strip() for n in out.splitlines() if n.strip()]
        if names:
            return sorted(names)
    except Exception as exc:
        print(f"(git ls-files failed: {exc!r} - falling back to a glob)")
    return sorted(p.name for p in ROOT.glob("tests/test_*.py"))


def _our_stuck_processes() -> list:
    """PIDs of processes this suite left behind, and only those.

    Three shapes, and the third is why this exists:

      pythonw.exe  - a test that started the app through the VBS launcher;
      nvngx.dll    - the worker, whose image name is the DLL;
      python.exe   - a test that ran `main.py` DIRECTLY (test_dred_diag,
                     test_direct_reconstruction, test_feature_leak all do).

    A stuck `python.exe` was invisible to the previous version, which watched
    only the first two. It still holds the single-instance mutex, so the NEXT
    test that starts the app sees "NeuralScreen is already running", never
    reaches NR, and fails - reading exactly like a flaky test
    (test_dred_diag failed this way in a suite run while passing alone).

    `python.exe` is also the interpreter RUNNING this file, and it is a very
    common image name, so the match is narrowed by command line: only
    processes whose cmdline mentions this project's directory or one of its
    scripts. Killing an unrelated python the user happens to be running is
    not a risk worth taking to fix a test harness.
    """
    import csv
    import io

    try:
        import psutil
    except ImportError:
        psutil = None

    me = os.getpid()
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True).stdout
    text = out.decode("cp1251", errors="replace")
    rows = [row for row in csv.reader(io.StringIO(text)) if row and len(row) > 1]
    pids = [r[1] for r in rows
            if r[0].lower() in ("pythonw.exe", "nvngx.dll")]
    if psutil is not None:
        marker = str(ROOT).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] == me:
                    continue
                if (proc.info["name"] or "").lower() != "python.exe":
                    continue
                cmd = " ".join(proc.info["cmdline"] or []).lower()
                if marker in cmd or "main.py" in cmd or "run_tests" in cmd:
                    pids.append(str(proc.info["pid"]))
            except (psutil.Error, KeyError):
                continue
    seen, out_pids = set(), []
    for pid in pids:
        if pid not in seen:
            seen.add(pid)
            out_pids.append(pid)
    return out_pids


def settle(limit: float = SETTLE_LIMIT) -> float:
    """Wait until none of our processes are left, and say how long it took.

    The tests run back to back and each GUI test starts a worker that holds
    the NGX feature on the card. When the previous one has not finished
    dying, the next test waits for its first NR frame while the old worker
    is still on the GPU - and test_window_mode, which has the longest wait
    in the suite, is the one that runs out of patience. The suite was
    measuring process teardown rather than the code under test.

    Returns the seconds spent waiting (0.0 when the field was already
    clear). It never fails a run: past the limit it returns what it waited
    and the caller says so out loud, because a process that will not die is
    itself worth knowing about.

    Waiting alone is not enough, and that is what this used to do. A worker
    that outlives its test keeps holding the mutex, and the next GUI test
    sees "NeuralScreen is already running" and fails WITHOUT running - the
    failure moves around the suite from run to run (dred_diag, reveal,
    window_mode_menu, smoke: different every time, all of them innocent).
    That reads exactly like flaky tests and is really one stuck process.
    Past the patience budget the leftovers are killed by PID, which is what
    the tests themselves do on their own workers.
    """
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if not _our_stuck_processes():
            break
        time.sleep(0.25)
    else:
        killed = _kill_leftovers()
    return time.monotonic() - started


def _kill_leftovers() -> list:
    """Kill processes this suite left behind, by PID. Returns what it killed.

    taskkill on the IMAGE NAME is what the tests use, and it is unreliable
    for pythonw.exe: several tools in this project are pythonw, and a kill by
    name can race a legitimate start. Killing the exact PIDs is narrow and
    repeatable - and for python.exe the PID came from a command-line match,
    so nothing outside this project is touched.
    """
    done = []
    for pid in _our_stuck_processes():
        r = subprocess.run(["taskkill", "/F", "/PID", pid, "/T"],
                           capture_output=True)
        if r.returncode == 0:
            done.append(pid)
        time.sleep(0.2)
    if done:
        print(f"    (killed {len(done)} leftover process(es): "
              f"{', '.join(done)})")
    return done


def run(label: str, args: list, note: str = "") -> dict:
    print(f"\n>>> {label}" + (f"  ({note})" if note else ""))
    waited = settle()
    SETTLES.append((label, waited))
    if waited > 0.5:
        print(f"    (waited {waited:.1f}s for the previous test's "
              f"processes to exit)")
    started = time.monotonic()
    try:
        # Only file arguments get the tests/ prefix - flags (--smoke,
        # --gui) must pass through untouched, otherwise autocheck.py runs
        # without its flag and fails on the stale zip instead of doing
        # the smoke/GUI cycle.
        args = [a if a.startswith("tests/") or a.startswith("-")
                else f"tests/{a}" for a in args]
        # The capture tests get the ambient environment - see
        # NO_DEFAULT_BACKEND. Everything else is told which path it measures.
        #
        # Compare BASENAMES. The first version of this line used
        # `label in a`, and label is the full "tests/test_bypass.py" while `a`
        # is the same string - so it was true for every test, the variable was
        # popped for all of them, and 13 tests went back to failing on the
        # branch default it was meant to correct. A condition that is always
        # true reads exactly like one that works, which is why the suite
        # result is what catches it, not the code.
        env = dict(CHILD_ENV)
        names = {Path(a).name for a in args if a.startswith("tests/")}
        if names & NO_DEFAULT_BACKEND:
            env.pop("NS_MOTION_BACKEND", None)
        r = subprocess.run([str(PY)] + args, cwd=ROOT, timeout=TIMEOUT,
                           capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace")
        took = time.monotonic() - started
        ok = r.returncode == 0
        tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-3:]
        if not ok:
            tail = ([l for l in (r.stdout or "").splitlines() if l.strip()][-8:]
                    or [(r.stderr or "").strip()[-400:]])
        for l in tail:
            print(f"    {l}")
        print(f"    [{'PASS' if ok else 'FAIL'}] {took:.1f}s")
        return {"label": label, "ok": ok, "took": took}
    except subprocess.TimeoutExpired:
        took = time.monotonic() - started
        print(f"    [FAIL] timed out after {took:.0f}s")
        return {"label": label, "ok": False, "took": took}


def main() -> int:
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    if not PY.exists():
        print(f"no {PY} - run this with the bundled runtime")
        return 1

    print(f"NeuralScreen test run - {ROOT}")
    print("The screen is taken over a few times; leave the machine alone.")
    print("=" * 70)
    results = []

    if only is None:
        results.append(run("static checks", ["autocheck.py"],
                           "build, archive, docs, git"))
    for name in tracked_tests():
        if only is not None and only not in name:
            continue
        results.append(run(name, [name], ABOUT.get(name, "")))
    if only is None and "--no-smoke" not in sys.argv:
        results.append(run("smoke", ["autocheck.py", "--smoke"],
                           "launch -> processing -> exit"))
    if "--gui" in sys.argv:
        results.append(run("GUI cycle", ["autocheck.py", "--gui"],
                           "launch -> record -> exit"))

    print("\n" + "=" * 70)
    width = max(len(r["label"]) for r in results)
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['label']:<{width}}  {r['took']:6.1f}s")
    failed = [r["label"] for r in results if not r["ok"]]
    total = sum(r["took"] for r in results)
    if SETTLES:
        worst_label, worst = max(SETTLES, key=lambda x: x[1])
        print(f"settle: {sum(s for _, s in SETTLES):.1f}s total, "
              f"longest {worst:.1f}s before {worst_label}")
    print("=" * 70)
    if failed:
        print(f"RESULT: {len(failed)} of {len(results)} FAILED in {total:.0f}s - {failed}")
        return 1
    print(f"RESULT: all {len(results)} green in {total:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
