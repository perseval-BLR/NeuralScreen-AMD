"""The first Radeon report must be readable, and this pins every gap it found.

Issue #1, the first live report from an RX 9070 XT, read like this:

    [main] worker window unavailable (the worker stopped after 0 of 4 reply
    bytes) - output through pygame
    [main] worker lost while sending - restarting (1/3)
    ...
    [main] the worker died 3 times in a row - NR OFF
    [main] worker exited cleanly (code 3221225477)
    [main] overlay menu opened
    [main] overlay menu closed
    [main] overlay menu opened
    [main] exit: tray or the quit hotkey (frames processed 0)

Four separate reporting failures are visible in those lines, and each one
cost a round trip. This pins the fix for each:

1. ``3221225477`` is ``0xC0000005`` - an ACCESS_VIOLATION - and the log called
   it "exited cleanly". The single line a reader needed was the line that
   lied.
2. The crash happened on the engine's own thread, where the host's ``__try``
   blocks cannot see it, so the worker died without writing anything about
   why. The Windows-level filter now names the code, the faulting module and
   its offset, and its "[crash]" tag is in the log filter so the lines reach
   ``NeuralScreen.log`` instead of being dropped.
3. "overlay menu opened" while the user reported "can't open settings": the
   menu is drawn into the overlay window, and the failure path had hidden it.
   The menu now brings the layer up.
4. Nothing collected the engine's own ``dlssnr_on_amd.log`` - the one file
   that records what the runtime was doing when it faulted.

Checked against the sources, plus the exit-code table driven directly. Run:

    runtime\\python.exe tests\\test_radeon_report_gaps.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

NATIVE = BASE / "native"


def main() -> int:
    failures = []
    pipeline_src = (BASE / "pipeline.py").read_text(encoding="utf-8")
    host_src = (NATIVE / "dlss5-feed-host64.cpp").read_text(encoding="utf-8",
                                                           errors="replace")
    bridge_src = (NATIVE / "amd" / "amd_bridge.inl").read_text(
        encoding="utf-8", errors="replace")
    runtime_src = (NATIVE / "amd" / "amd_runtime.cpp").read_text(
        encoding="utf-8", errors="replace")
    runtime_h = (NATIVE / "amd" / "amd_runtime.h").read_text(
        encoding="utf-8", errors="replace")
    commands_src = (BASE / "commands.py").read_text(encoding="utf-8")
    display_src = (BASE / "display.py").read_text(encoding="utf-8")
    main_src = (BASE / "main.py").read_text(encoding="utf-8")

    # --- 1. a crashed worker no longer reads as a clean exit --------------
    from pipeline import _EXIT_CODE_NAMES
    for code, name in ((0xC0000005, "ACCESS_VIOLATION"),
                       (0xC00000FD, "STACK_OVERFLOW"),
                       (0xC0000374, "HEAP_CORRUPTION"),
                       (0xC0000409, "STACK_BUFFER_OVERRUN"),
                       (0xC0000135, "DLL_NOT_FOUND")):
        if _EXIT_CODE_NAMES.get(code) != name:
            failures.append(f"exit code 0x{code:08X} does not resolve to {name}")
    if "_EXIT_CODE_NAMES.get(code & 0xFFFFFFFF)" not in pipeline_src:
        failures.append("shutdown_worker does not consult the exit-code table - "
                        "a crash would read as 'exited cleanly' again")
    if "the worker CRASHED" not in pipeline_src:
        failures.append("shutdown_worker has no crash line - the reader is "
                        "left with a bare number")

    # --- 2. the filter that names a fault on the engine's own thread ------
    if "SetUnhandledExceptionFilter(CrashFilter)" not in host_src:
        failures.append("the crash filter is never installed - a fault on the "
                        "engine's own thread kills the worker silently")
    for token in ("ACCESS_VIOLATION", "ExceptionAddress", "GetModuleHandleExA"):
        if token not in host_src:
            failures.append(f"the crash filter does not report {token}")
    # The offsets and the counts are what name the stage; without them the
    # report says "it crashed" and nothing about where.
    if "AmdCrashCounters" not in host_src or "AmdCrashCounters" not in bridge_src:
        failures.append("the crash report carries no AMD counters - the log "
                        "cannot say whether the engine had started")
    # And the tag has to be one the Python side forwards, or the whole filter
    # writes into a log nobody sees.
    if '"[crash]"' not in pipeline_src:
        failures.append("[crash] is not in _LOG_ALWAYS - the crash lines are "
                        "dropped before they reach NeuralScreen.log")
    if "[crash]" not in host_src:
        failures.append("the crash filter writes untagged lines - they would "
                        "be filtered out of the shared log")

    # --- 3. the menu must be reachable with a dead pipeline ---------------
    if "def show_for_menu" not in display_src:
        failures.append("display has no show_for_menu - the menu can still be "
                        "invisible after the worker dies")
    if "st.display.show_for_menu()" not in commands_src:
        failures.append("opening the menu does not raise the layer - this is "
                        "the 'can't open settings' report from issue #1")
    # With worker_failed the loop skips the frame path entirely, so nothing
    # else paints the menu.
    if "if st.display.menu.visible:" not in main_src:
        failures.append("the worker_failed branch does not paint the menu - it "
                        "would open into a loop that never draws it")
    # _reveal_pending is exactly why set_visible(True) is not enough: it is
    # only cleared by reveal(), which waits for a real frame exchange, and on
    # a broken install that never happens. Search the whole method body.
    body = display_src.split("def show_for_menu")[1]
    body = body.split("\n    def ")[0]          # up to the next method
    if "_reveal_pending = False" not in body:
        failures.append("show_for_menu does not clear _reveal_pending - "
                        "set_visible would refuse to show the window")

    # --- 4. the engine's own log reaches the report ----------------------
    if "dlssnr_on_amd.log" not in commands_src:
        failures.append("the diagnostic package does not collect the engine's "
                        "own log - the only witness to a fault inside it")
    if "sanitize_text" not in commands_src.split("def _copy_log")[0][-400:] and \
       "sanitize_text" not in commands_src.split("_copy_log(BASE_DIR")[0][-600:]:
        failures.append("the captured logs are not scrubbed before shipping")

    # --- 5. the three engine-surface flags -------------------------------
    # ALLOW_RENDER_TARGET was documented as load-bearing in our own comment
    # and never set: the engine transitions from a state the resource was
    # never created for.
    if "D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET" not in bridge_src:
        failures.append("engine surfaces are created without "
                        "ALLOW_RENDER_TARGET - the packet declares "
                        "render-target, and the reference carries both flags")

    # --- 6. the engine's knobs are actually written -----------------------
    if "SetOptions(opt)" not in bridge_src:
        failures.append("SetOptions is never called - the engine runs on "
                        "whatever its DllMain left in those fields")
    for field in ("kCharMask", "kToneChannels"):
        if field not in runtime_src:
            failures.append(f"{field} is never written - the reference writes "
                            f"it every frame")
    if "auto_mask" not in runtime_h or "tone_channels" not in runtime_h:
        failures.append("Options has no mask/channels field to carry them")

    # --- 7. the inline wait budget ---------------------------------------
    # An empty ini leaves the engine on its 600 ms default, which no job on a
    # real Radeon fits inside.
    if "InlineWaitMs" not in runtime_src:
        failures.append("the ini is created empty - the engine keeps its "
                        "600 ms inline budget and skips every frame")

    # --- 8. regression set: the second Radeon report ----------------------
    # v0.1.4 fixed the reporting and shipped three new bugs of its own, all
    # visible in the same user's second report. Pinned one by one.
    #
    # (a) The engine's log recorded `mode async` although this host writes
    #     Inline=1: the write ORDER is part of the contract, and Inline has to
    #     be written twice (before Enabled, and again after it, right before
    #     Interop and Init). One write is not enough.
    # The access goes through the per-build table now (`table_->kInlineMode`),
    # because the host drives two releases: a bare `rva::` constant would be the
    # v0.2.17 address on a v0.3.1 image. Both spellings are counted so this check
    # does not depend on which one the code uses - the contract being tested is
    # HOW MANY TIMES the field is written, not how it is addressed.
    inline_writes = (runtime_src.count("table_->kInlineMode) = 1")
                     + runtime_src.count("rva::kInlineMode) = 1"))
    if inline_writes < 2:
        failures.append("Inline is written once - the reference writes it twice "
                        "around Enabled, and the engine comes up async without it")
    if "mode async" not in runtime_src:
        failures.append("the reason the double write exists is not recorded - "
                        "the next reader will delete it again")
    # (b) The menu opened on a magenta screen and swallowed every click:
    #     draw_overlay fills with CHROMA_KEY, which only cuts out if the layer
    #     is keyed, and the failure branch's `continue` skipped the only code
    #     that read mouse events.
    if "st.display.set_hud_only(True)" not in main_src:
        failures.append("the failure branch paints the menu without keying the "
                        "layer - the chroma fill shows as a magenta screen")
    events_before = main_src.find("for ev in pygame.event.get()")
    worker_failed_at = main_src.find("if st.worker_failed:")
    if events_before < 0 or worker_failed_at < 0:
        failures.append("the event pump or the worker_failed branch is gone")
    elif events_before > worker_failed_at:
        failures.append("the event pump sits AFTER the worker_failed branch - "
                        "its `continue` skips it, so the menu ignores clicks")
    # (c) The engine is handed a list whose last binding must be its own
    #     slot 0 (work surface as SRV and UAV), not whatever the host's
    #     motion pass left behind.
    if "AmdBindTriplet(0, g_amd.net" not in bridge_src:
        failures.append("no slot-0 rebind before the record - the engine gets "
                        "a list whose bindings belong to the host's last pass")
    # (d) The report has to be reachable when the overlay is not: the tray
    #     icon is a Windows-owned menu and works whenever the process lives.
    if "_diagnostics" not in (BASE / "tray.py").read_text(encoding="utf-8"):
        failures.append("the tray has no diagnostics item - a user whose "
                        "overlay misbehaves cannot produce a report at all")
    commands_src2 = (BASE / "commands.py").read_text(encoding="utf-8")
    if 'cmd == "diagnostics"' not in commands_src2:
        failures.append("the tray's diagnostics command is not routed")

    # --- 9. the FSR bridge (the dispatch the engine follows) --------------
    # v0.1.5 made the engine initialise, run jobs and produce a BLACK picture:
    # its own log said `dispatches 0 ... route backbuffer` over 9000 frames.
    # The runtime is a game proxy - it takes the frame from a FidelityFX
    # upscale dispatch it hooks, and a host that never dispatches leaves it
    # nothing to attach to. Pinned here because losing any of it puts us back
    # to that black screen.
    fsr_h = (NATIVE / "amd" / "amd_fsr.h").read_text(encoding="utf-8", errors="replace")
    fsr_c = (NATIVE / "amd" / "amd_fsr.cpp").read_text(encoding="utf-8", errors="replace")
    if "ffxDispatch" not in fsr_c:
        failures.append("the bridge never calls ffxDispatch - the runtime has "
                        "no dispatch to follow and the picture stays black")
    if "FFX_API_DISPATCH_DESC_TYPE_UPSCALE" not in fsr_c:
        failures.append("the dispatch descriptor is not the upscale one")
    # The split itself: A carries motion vectors (the runtime follows it and
    # runs the network), B must NOT (or the network is charged for the upscale
    # too). Losing either half breaks the picture differently.
    if "DispatchNet" not in fsr_h or "DispatchUpscale" not in fsr_h:
        failures.append("the two dispatches are not separate - A must carry "
                        "motion vectors and B must not")
    if fsr_c.count("motionVectors = ffxApiGetResourceDX12(nullptr)") == 0:
        failures.append("the upscale dispatch binds motion vectors - the "
                        "runtime would run the network on it as well")
    if "UseFsrInputs) = 1" not in runtime_src:
        failures.append("UseFsrInputs is not 1 - the ffxDispatch hook is never "
                        "armed and no frame is processed, silently")
    # The ini has to carry it too: the runtime reads that key in its DllMain,
    # and the host's later write cannot arm a hook that was never installed.
    if 'L"UseFsrInputs",  L"1"' not in runtime_src:
        failures.append("UseFsrInputs is not written to the ini - the runtime "
                        "reads it there, before any of our flag writes land")
    # Upgrading matters as much as writing: the file is created once, so a
    # per-file check would leave everyone who already ran an older build with
    # the old keys - exactly the people testing this fix.
    if "GetPrivateProfileStringW" not in runtime_src:
        failures.append("the ini is only written when absent - an existing file "
                        "keeps the old keys and UseFsrInputs stays unset")
    # The order the runtime needs: it hooks D3D12/DXGI from its own thread, and
    # a swapchain created before those land is invisible to it.
    #
    # WHICH LINES prove that depends on the image, and the wait has to know
    # which one it is looking at. Patch 0x1ffc disables the runtime's own
    # hook-installer thread on purpose (the host owns the frame), so the PATCHED
    # image never logs `hooked IDXGIFactory...` - waiting for one is a check
    # that cannot pass, and it reported "hooks were NOT seen" on every healthy
    # Radeon whose logs we have. The STOCK image does write them, and for it the
    # swapchain line is the one that must land before we create ours.
    #
    # The old version of this check demanded `ffxCreateContext` / `engine init
    # ok` as markers. Both are written AFTER Load() returns in this host's own
    # order (AmdInit calls the upscaler's Load later), so waiting for either is
    # waiting for something this function is itself responsible for. That is the
    # same class of mistake the check was written to catch, which is why it is
    # pinned here instead.
    if "kReadyMarkers" not in runtime_src:
        failures.append("the host does not wait for the runtime to be ready - a "
                        "swapchain created first is invisible to it")
    if "HooksApplicable" not in runtime_src and "hooks_applicable_" not in runtime_src:
        failures.append("the readiness wait does not distinguish the two images - "
                        "the patched one cannot print the detour lines it waits for")
    for marker in ("hooked IDXGISwapChain1::Present1", "env: d3d12 device yes"):
        if marker not in runtime_src:
            failures.append(f"the readiness marker {marker!r} is gone from the "
                            f"host - the stock image's wait has nothing to look for")
    # And the marker the patched image cannot write must not be the one the
    # stock wait depends on, or the wait is conditional on nothing.
    if "engine init ok" in runtime_src.split("kReadyMarkers")[1][:400]:
        failures.append("the wait still depends on 'engine init ok', which this "
                        "host only writes after Load() returns - it waits for "
                        "something it is itself responsible for")

    # The upscale has to run AFTER the engine has edited the surface.
    #
    # In this host the engine works INLINE: its log says "mode inline
    # (same-frame, the game waits for the network)", and the working host puts
    # dispatch A, dispatch B and the composite in ONE command list for exactly
    # that reason. So B follows A in the list, and the property to protect is
    # "B is recorded after A", not "B lives in a second submission" - the second
    # submission was the bug: with a 5000 ms fence wait between them, the engine
    # spent every frame reporting it was still "waiting for the capture".
    upscale_at = bridge_src.find("DispatchUpscale")
    net_dispatch_at = bridge_src.find("DispatchNet")
    if upscale_at < 0 or net_dispatch_at < 0:
        failures.append("the dispatch calls are gone from the frame path")
    elif not (net_dispatch_at < upscale_at):
        failures.append("the upscale is recorded before the network's dispatch - "
                        "it would scale the frame the network has not touched")
    # One submission per frame: a second submission between the two dispatches
    # is the split this fix removed, and with it the mid-frame fence wait.
    #
    # Only the frame function is searched. A raw find over the file also matches
    # the exposure texture's own upload (a different function, before the frame
    # path) and even the words "EndCommands()" inside a comment - both were
    # false positives on the first run of this check.
    frame_fn = bridge_src.find("static bool AmdEvaluateVideo(VideoState &v, int reset, UINT64 *submitted)\n{")
    dispatch_calls = [m.start() for m in re.finditer(r"^\s*const UINT64 \w+ = EndCommands\(\);",
                                                     bridge_src, re.M)]
    if frame_fn >= 0:
        in_frame = [a for a in dispatch_calls if a > frame_fn]
        # The frame does exactly one submission. Anything more means the frame
        # is split again, and the engine waits for a capture it has already
        # been given.
        if len(in_frame) > 1:
            failures.append(f"the frame submits {len(in_frame)} times - the engine "
                            "reads its frame off ONE submission plus the present, so "
                            "a split frame costs it a wait every frame")
    else:
        failures.append("the frame function is gone - this check can no longer "
                        "see how many times a frame is submitted")

    # --- 10. the upscaler ships, and the build refuses without it ---------
    # Its absence is the quietest failure in the whole path: the pass comes up,
    # the network runs, the self-check says healthy, and the picture is black -
    # the runtime takes its frame from an FSR dispatch. AMD's licence permits
    # redistributing this binary, so it belongs in the archive; and a build that
    # silently omitted it would be indistinguishable from a broken one.
    zip_src = (BASE / "build_release_zip.py").read_text(encoding="utf-8")
    if "amd_fidelityfx_upscaler_dx12.dll" not in zip_src:
        failures.append("the archive does not carry the FidelityFX upscaler - "
                        "the pass would run and paint nothing")
    # The guard, located by position: the raise must come after the upscaler
    # is named (a plain substring search over the whole file would pass on any
    # "Refusing to build" anywhere in it).
    up_at = zip_src.find('_UPSCALER = "native/amd_fidelityfx_upscaler_dx12.dll"')
    refuse_at = zip_src.find("Refusing to build", up_at if up_at >= 0 else 0)
    if up_at < 0:
        failures.append("the upscaler check is gone from the build")
    elif refuse_at < 0 or refuse_at - up_at > 1200:
        failures.append("the build does not refuse when the upscaler is "
                        "missing - it would ship a black-screen build")
    notices = (BASE / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for token in ("Advanced Micro Devices", "signedbin",
                  "d0dcccc74a43c44ba435b7a369b456e0970d8a4464e4bd683119b374f2c9fb46"):
        if token not in notices:
            failures.append(f"THIRD-PARTY-NOTICES.md does not carry {token!r} - "
                            "the redistributed binary needs its notice")

    # --- 11. issue #2: the panel must not name a card NVAPI cannot see ----
    # A Radeon hint on a hybrid machine matched no NVAPI card, and the
    # fallback named the first one (an RTX 3080) while the worker ran the
    # Radeon. probe() must answer NOTHING in that case; startup fills the gap
    # from the DXGI name.
    gpu_src = (BASE / "gpuinfo.py").read_text(encoding="utf-8")
    if "return None" not in gpu_src:
        failures.append("gpuinfo no longer answers None for an unmatched hint - "
                        "issue #2 can come back")
    startup_src = (BASE / "startup.py").read_text(encoding="utf-8")
    if "_pick_driver" not in startup_src:
        failures.append("the driver line does not follow the working card - "
                        "a hybrid machine reports the other card's driver")
    if "gpu_describe(gpu_info) or _working_card_name(st.cfg)" not in startup_src:
        failures.append("the displayed card no longer falls back to the DXGI "
                        "name - a Radeon would show as unknown")

    print("    exit codes name the crash: "
          f"{'ok' if not failures else 'FAILED'}")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} gap(s) from issue #1 are open again")
        return 1
    print("    OK: the first Radeon report would now be readable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
