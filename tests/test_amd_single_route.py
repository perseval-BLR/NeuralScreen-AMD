"""The engine is fed by ONE route, not two, and the log proves which.

Four live reports (RX 7900 XTX, 9070 XT, 9060 XT, and the first 9070 XT) show
the same signature on every card:

    network job 1 done in 469 ms (history off)
    network job 1 done in 235 ms (history off)
    ... every job is job 1, history never turns on

The engine never sees a SEQUENCE. It re-initialises on every frame, which is
why the picture is black and why the cost is seconds rather than the 16 ms its
own arithmetic takes.

The cause is that this host fed the engine TWICE per frame: once through the
FSR dispatch it follows (dispatch A), and again through the runtime's packet
call (`Record`). The dispatch is the route the engine actually reads - its own
notes say it takes its colour from the output of the dispatch it follows - so
the packet is a second, differently-shaped statement about which frame is
current. The one external host that produces a picture never uses the packet
call at all.

Checked here, structurally, because the failure is invisible at runtime (the
fence retires, the counters look alive, the picture is black):

1. `Record` is not on the default path - it sits behind the NS_AMD_PACKET
   switch.
2. `Notify` still runs: the host disables the runtime's own
   ExecuteCommandLists detour (patch 0x1ffc), so the engine is told about the
   submission by hand. Removing this would make the dispatch route dead too.
3. The completion wait matches the route: the job counter only when a packet
   was recorded (nothing increments it otherwise), the queue fence always.
4. The switch is read once per run and reported in the log, so a user's log
   says which route their build used.
5. Dispatch A is still in the list - the packet was the redundant one, not the
   dispatch.

Run:  runtime\\python.exe tests\\test_amd_single_route.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def main() -> int:
    failures = []
    src = BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- 1. the packet is off the default path ----------------------------
    i = src.find("// ---- the packet, only when asked for")
    j = src.find("if (!engine_ok || engine_failed)")
    if i < 0 or j < 0:
        failures.append("the packet block or the wait block is gone - this "
                        "test can no longer see the frame path")
        block = ""
    else:
        block = src[i:j]

    record_at = block.find("runtime.Record(")
    guard_at = block.find("if (g_amd.use_packet)")
    if record_at < 0:
        failures.append("Record is gone entirely - the packet path no longer "
                        "exists, so NS_AMD_PACKET=1 cannot restore it")
    elif guard_at < 0 or guard_at > record_at:
        failures.append("Record is called UNGUARDED - the engine is fed twice "
                        "per frame again, which is the black-picture signature")

    # --- 2. Notify must still happen --------------------------------------
    if "runtime.Notify(" not in block:
        failures.append("Notify is gone: the host disables the runtime's own "
                        "ExecuteCommandLists detour, so the engine would never "
                        "learn about the submission")

    # --- 3. the wait matches the route ------------------------------------
    # TWO conditions now, and the second one is the fix for a real regression:
    # the gate used to be `use_packet` alone, and WaitJobs returns false when
    # the build publishes no completed-jobs counter - so on v0.3.1 (which has
    # no address for it) every packet frame was read as "the engine did not
    # finish" and skipped. The counter wait now also requires the counter.
    # Anchored on the CALL, not on the bare word: a comment above it mentions
    # WaitJobs by name, and a first version of this check searched from that
    # mention - which put the actual gate outside the window and read the code
    # as ungated.
    # Matched on the GATE LINE itself, not on a name appearing anywhere in the
    # neighbourhood: the flag is also declared and discussed in a comment above,
    # so a check for the bare name still passed with the gate reverted to its
    # buggy form. The gate is one line, and it is that line that must carry both
    # conditions.
    call_at = block.find("g_amd.runtime.WaitJobs(")
    window = block[max(0, call_at - 900): call_at + 100] if call_at >= 0 else ""
    gate = ""
    for line in window.splitlines():
        if "if (" in line and "use_packet" in line and "WaitJobs" not in line:
            gate = line.strip()
            break
    wait_gated = "use_packet" in gate
    counter_gated = "counter_wait_available" in gate
    if call_at < 0:
        failures.append("WaitJobs is gone - the packet route has no completion "
                        "wait at all")
    elif not wait_gated:
        failures.append("WaitJobs runs on the dispatch-only route: nothing "
                        "increments the job counter there, so every frame "
                        "would time out and be skipped")
    elif not counter_gated:
        failures.append("WaitJobs is gated on the packet route alone: on a "
                        "build with no completed-jobs counter it returns false "
                        "and every frame is skipped as if the engine had "
                        "timed out - the wait must also require the counter")
    if "SyncCountKnown()" not in block:
        failures.append("the bridge never asks whether the build publishes the "
                        "completed-jobs counter - it cannot tell 'the wait is "
                        "unavailable' from 'the engine did not finish'")
    if "WaitFenceValue(" not in block:
        failures.append("the queue-fence wait is gone - the dispatch-only "
                        "route has no completion signal")

    # --- 4. the switch is reported ----------------------------------------
    if "NS_AMD_PACKET" not in src:
        failures.append("NS_AMD_PACKET is never read - the switch cannot be "
                        "used and no log line says which route ran")
    if not re.search(r'packet path %s', src):
        failures.append("the run does not report which route feeds the engine")

    # --- 5. dispatch A is still there -------------------------------------
    if "fsr.DispatchNet(" not in src:
        failures.append("dispatch A is gone: that dispatch IS the route the "
                        "engine follows, so removing it blacks the picture")

    # --- 6. the switch defaults OFF ---------------------------------------
    if not re.search(r"bool use_packet = false;", src):
        failures.append("use_packet does not default to false - a fresh build "
                        "would still feed the engine twice")

    # --- 7. the feed diagnostic reports, and never gates ------------------
    # The engine's counters are bumped by `Record`, so on the dispatch route
    # they stand still by design (every Radeon log we have shows `sync 0` in
    # every report). They are useful as evidence about the FEED - did the engine
    # queue anything at all - and useless as a gate, because gating on a counter
    # that nothing increments skips every frame.
    if "frames_without_job" not in src:
        failures.append("the feed diagnostic is gone: a run where the engine "
                        "records no job at all would look like a picture problem")
    if "recorded NO job in the last" not in src:
        failures.append("the diagnostic never says its verdict in words")
    # It must not be a gate: no frame may be skipped on account of it.
    #
    # Anchored on the diagnostic's own log line, and the window stops at the
    # line that closes its block. A wider window caught the ORDINARY timeout
    # handler further down (`++g_amd.timeouts` on a real engine failure), which
    # is a different thing entirely - so the check has to see only the
    # diagnostic's own body.
    anchor = src.find("recorded NO job in the last")
    if anchor < 0:
        block = ""
    else:
        end = src.find("if (!engine_ok || engine_failed)", anchor)
        block = src[max(0, anchor - 1100):end if end > 0 else anchor + 1400]
    for bad in ("return false", "++g_amd.timeouts", "g_amd.failed = true"):
        if bad in block:
            failures.append(f"the feed diagnostic GATES the frame ({bad}) - a "
                            f"counter that stands still on this route would then "
                            f"skip every frame, which is the deadlock it exists "
                            f"to describe")
    # And the counter it reads must be the job counter, not the sync counter:
    # only the job counter says "something was queued".
    if "JobCount()" not in block:
        failures.append("the diagnostic does not read the job counter, so it "
                        "cannot tell 'nothing was queued' from 'nothing finished'")

    print(f"    feed-diagnostic block: {len(block)} chars")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print("OK: one route feeds the engine, and the wait matches that route")
    return 0


if __name__ == "__main__":
    sys.exit(main())
