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
    wait_gated = "if (g_amd.use_packet)" in block[block.find("WaitJobs") - 300:
                                              block.find("WaitJobs") + 100] \
        if "WaitJobs" in block else False
    if "WaitJobs" not in block:
        failures.append("WaitJobs is gone - the packet route has no completion "
                        "wait at all")
    elif not wait_gated:
        failures.append("WaitJobs runs on the dispatch-only route: nothing "
                        "increments the job counter there, so every frame "
                        "would time out and be skipped")
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

    print(f"    frame-path block: {len(block)} chars")
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
