"""The engine is stopped BEFORE the device it runs on is handed back.

WHY THIS EXISTS
---------------
`Runtime::~Runtime()` in amd_runtime.cpp is deliberately empty, and says why:

    "the worker owns the device and the queue, and the engine's worker threads
     must not be stopped while a submission may still be in flight. The
     reference stops workers only from an explicit Shutdown call after
     everything has drained, and so do we."

`Runtime::Shutdown()` exists, forwards to the runtime's own kShutdown (the
function that stops the engine's workers), and was called from NOWHERE. Every
worker exit therefore released the D3D12 device and the command queue while the
engine's threads were still running against them.

Measured on a reporter's machine (RX 7900 XTX, v0.3.12): the process aborted
with exception 0xC0000409, parameter 7 - FAST_FAIL_FATAL_APP_EXIT, which is what
the UCRT abort() raises - on THIRTEEN of thirteen launches: once on the
window-mode switch and twice on exit, in each of his three runs, and the module
it died in was dlssnr_amd_pass1.dll, the engine itself.

What this locks
---------------
1. AmdShutdown() exists and calls g_amd.runtime.Shutdown().
2. Each teardown path that hands the device back calls AmdShutdown() BEFORE the
   first resource-closing call in that path (CleanupVideoNgx / CloseDda /
   ClosePresent / CloseSharedInput / CloseOut / SpoutBridgeShutdown).

The ORDER is the contract, not the presence: stopping the engine after the
device is released is exactly the bug, so a call that exists but sits below the
teardown is still a failure.

3. The process does NOT leave through the runtime's own unload path. Upstream
   hosts the same runtime and hit the same abort; their fix is to end the
   process without that teardown:

     "the worker leaves without running the hosted runtime's teardown (it was
      failing fast, 0xC0000409, on a normal session end)"

   Our dump agrees: the fault is at dlssnr_amd_pass1.dll + 0x3a885, which is not
   the runtime's shutdown entry. So the exits call ExitWithoutRuntimeTeardown,
   and only when a runtime was actually hosted - a machine on the NVIDIA path
   must still exit normally.

Run:  runtime\\python.exe tests/test_amd_engine_shutdown.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"
HOST = BASE / "native" / "dlss5-feed-host64.cpp"

# The calls that hand the device, the queue or the window back. Whichever comes
# FIRST in a function is the line AmdShutdown() has to beat.
TEARDOWN = ("CleanupVideoNgx(", "CloseDda(", "ClosePresent(", "CloseSharedInput(",
            "CloseOut(", "SpoutBridgeShutdown(", "CloseGray(", "CloseMotionScaler(")


def strip_comments(src: str) -> str:
    """Code only: a comment quoting the old order must not count as the order."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def body_of(code: str, signature: str) -> str:
    at = code.find(signature)
    if at < 0:
        return ""
    depth, started = 0, False
    for i in range(at, len(code)):
        c = code[i]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return code[at:i + 1]
    return code[at:]


def main() -> int:
    failures: list[str] = []
    if not BRIDGE.exists() or not HOST.exists():
        print("FAIL: the AMD bridge or the host is missing")
        return 1

    bridge = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))
    host = strip_comments(HOST.read_text(encoding="utf-8", errors="replace"))

    # --- 1. AmdShutdown exists and forwards to the runtime's own Shutdown ----
    fn = body_of(bridge, "static void AmdShutdown()")
    if not fn:
        failures.append(
            "AmdShutdown() is gone - the runtime's own destructor is empty on "
            "purpose and expects an explicit stop, so nothing stops the engine")
    elif not re.search(r"g_amd\.runtime\.Shutdown\s*\(\s*\)", fn):
        failures.append(
            "AmdShutdown() no longer calls g_amd.runtime.Shutdown() - it is "
            "then a name that does nothing, and the engine keeps running")

    # --- 2. every teardown path stops the engine FIRST ----------------------
    # Anchored on the log line each path writes as it leaves, not on a function
    # boundary: RunVideo is one enormous function containing all of them, so a
    # call anywhere inside it satisfied a function-level check no matter where
    # it sat - and a negative control that put the call AFTER the cleanup
    # passed, which is how this was found. Each anchor names exactly one path.
    anchors = (
        ("the video loop's exit", "[pure] complete:"),
        ("the live stream's close", "[live] input stream closed after"),
        ("the host loop's exit", "[host] unknown tag"),
    )
    checked = 0
    for name, anchor in anchors:
        at = host.find(anchor)
        if at < 0:
            continue
        # The path is what follows the anchor until the function closes.
        window = host[at:at + 1400]
        ends = [window.find(t) for t in TEARDOWN if window.find(t) >= 0]
        if not ends:
            continue          # not a teardown path (nothing is released here)
        checked += 1
        stop = window.find("AmdShutdown()")
        first = min(ends)
        if stop < 0:
            failures.append(
                f"{name} hands the device back without stopping the engine: "
                f"no AmdShutdown() before {window[first:window.find(chr(10), first)].strip()}")
        elif stop > first:
            failures.append(
                f"{name} stops the engine AFTER releasing resources "
                f"('{window[first:window.find(chr(10), first)].strip()}') - the "
                f"engine's threads then run against a dead device, which is the "
                f"abort this exists to prevent")

    if checked == 0:
        failures.append(
            "no teardown path was found to check - the checker is looking at "
            "the wrong file or the paths were renamed")

    # --- 3. the exit does not run the runtime's own unload path -------------
    # Only when a runtime was hosted: a machine on the NVIDIA path has no third
    # party's detours to unload, and must keep exiting normally.
    if "ExitWithoutRuntimeTeardown" not in host:
        failures.append(
            "ExitWithoutRuntimeTeardown is gone - the runtime's unload path "
            "then runs on every exit, and upstream measured that it aborts "
            "(0xC0000409) on a normal session end")
    else:
        at = host.find("if (AmdHostedRuntime()) ExitWithoutRuntimeTeardown(code);")
        if at < 0:
            failures.append(
                "ExitWithoutRuntimeTeardown is not guarded by AmdHostedRuntime() "
                "- an unconditional process kill would take the exit code's "
                "ordinary path away from machines that never hosted a runtime")
        else:
            # Both worker entry points must take it: the video loop and Serve.
            if host.count("if (AmdHostedRuntime()) ExitWithoutRuntimeTeardown(code);") < 2:
                failures.append(
                    "only one of the two worker entry points leaves without the "
                    "runtime's teardown - the other still aborts")
            if "TerminateProcess(GetCurrentProcess()" not in host:
                failures.append(
                    "ExitWithoutRuntimeTeardown no longer terminates the "
                    "process - it is then just a return, and the unload path "
                    "runs anyway")

    if failures:
        print("FAIL: the engine is not stopped before its device is released")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"OK: AmdShutdown() forwards to the runtime's own stop, and all "
          f"{checked} teardown paths call it before the first resource release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
