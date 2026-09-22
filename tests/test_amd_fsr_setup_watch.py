"""A stalled FSR context setup names the step it is stuck in.

WHY THIS EXISTS
---------------
On v0.3.21 an RX 9070 XT (#1, 22.09) stopped six launches in a row right after
"engine surfaces at 1664x936": no "FSR contexts" line, no frame, no error, and
ten seconds later the program killed the worker. Between those two lines the
worker makes two ffxCreateContext calls, one of which loads a driver
component, and the log could not say which one never returned. A stall that
leaves no line is a stall that can only be guessed at.

WHAT THIS LOCKS
---------------
1. Each ffxCreateContext call (A and B) is bracketed: a step is named directly
   before it and directly after it, so the last line says where it stopped.
2. The worker passes its step function to CreateContexts - a callback nobody
   passes logs nothing.
3. The step is stored atomically before it is logged, so the watch reads the
   step the setup is actually in.
4. A second thread waits on an event for kFsrStallMs and, on timeout, logs the
   open step. kFsrStallMs is well under the 10 s after which the worker is
   killed, or the line would never be written.
5. The watch cannot outlive what it reads: the event is set after the call, the
   thread is joined, and only then is the event closed. And it only reads and
   logs - it never touches the upscaler.

Run:  runtime\\python.exe tests/test_amd_fsr_setup_watch.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FSR = BASE / "native" / "amd" / "amd_fsr.cpp"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"

KILL_BUDGET_MS = 10000   # main kills a worker that stays silent this long


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def body_of(code: str, signature: str) -> str:
    """The body of the DEFINITION of `signature` (declarations are skipped)."""
    for m in re.finditer(re.escape(signature), code):
        tail = code[m.end():m.end() + 400]
        brace, semi = tail.find("{"), tail.find(";")
        if brace < 0 or (0 <= semi < brace):
            continue
        depth, started = 0, False
        for i in range(m.start(), len(code)):
            c = code[i]
            if c == "{":
                depth += 1
                started = True
            elif c == "}":
                depth -= 1
                if started and depth == 0:
                    return code[m.start():i + 1]
    return ""


def flat(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def main() -> int:
    failures: list[str] = []
    for p in (FSR, BRIDGE):
        if not p.is_file():
            print(f"FAIL: {p.relative_to(BASE)} is missing")
            return 1
    fsr = strip_comments(FSR.read_text(encoding="utf-8", errors="replace"))
    br = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # --- 1. both create calls bracketed ------------------------------------
    cc = flat(body_of(fsr, "Upscaler::CreateContexts("))
    if not cc:
        failures.append("CreateContexts could not be read")
    else:
        if not re.search(r"StepFn step\)", cc):
            failures.append("CreateContexts takes no step function")
        if "const auto say = [step](const char *s) { if (step != nullptr) step(s); };" not in cc:
            failures.append("the step function is not called through a null-safe wrapper")
        if not re.search(r'say\("[^"]*\(A\): calling"\); ffxReturnCode_t rc = create_fn\(AsCtx\(&ctx_net_\.handle\)'
                         r'[^;]*; say\("[^"]*\(A\): returned"\);', cc):
            failures.append("A's ffxCreateContext is not named directly before and after the call")
        if not re.search(r'say\(private_b_ \? "[^"]*: calling" : "[^"]*\(B\): calling"\); '
                         r'rc = create_b\(AsCtx\(&ctx_up_\.handle\)[^;]*; say\("[^"]*\(B\): returned"\);', cc):
            failures.append("B's ffxCreateContext is not named directly before and after the call")

    # --- 2. the worker passes its step function -----------------------------
    call = re.search(r"g_amd\.fsr\.CreateContexts\(([^;]*)\);", br)
    if not call or not call.group(1).rstrip().endswith("AmdFsrStep"):
        failures.append("the worker does not pass AmdFsrStep to CreateContexts")

    # --- 3. stored, then logged ---------------------------------------------
    st = flat(body_of(br, "static void AmdFsrStep("))
    at_store = st.find("InterlockedExchangePointer(&g_fsr_step,")
    at_log = st.find('Log("[amd] FSR setup: %s", step);')
    if at_store < 0:
        failures.append("the step is not stored atomically")
    if at_log < 0:
        failures.append("the step is not logged")
    if at_store >= 0 and at_log >= 0 and at_log < at_store:
        failures.append("the step is logged before it is stored - the watch could read the previous one")

    # --- 4. the watch: event wait, timeout logs the open step, under budget --
    wt = flat(body_of(br, "static DWORD WINAPI AmdStallWatchThread("))
    if "if (WaitForSingleObject(w->done, w->ms) == WAIT_TIMEOUT)" not in wt:
        failures.append("the watch does not wait on the setup's event with a timeout")
    if "InterlockedCompareExchangePointer(&g_fsr_step, nullptr, nullptr)" not in wt:
        failures.append("the watch does not read the open step atomically")
    if "the FSR context setup has not returned after %lu ms - still in:" not in wt:
        failures.append("the watch does not log the open step on a timeout")
    if "g_amd.fsr" in wt:
        failures.append("the watch touches the upscaler - it must only read and log")
    ms = re.search(r"static const DWORD kFsrStallMs = (\d+);", br)
    if not ms:
        failures.append("no kFsrStallMs")
    elif not (0 < int(ms.group(1)) <= KILL_BUDGET_MS // 2):
        failures.append(f"kFsrStallMs is {ms.group(1)} - it must be well under the "
                        f"{KILL_BUDGET_MS} ms after which the worker is killed")

    # --- 5. lifetime: call, set, join, close ---------------------------------
    fb = flat(br)
    seq = re.search(
        r"AmdStallWatch watch\{ CreateEventW\(nullptr, TRUE, FALSE, nullptr\), kFsrStallMs \};.*?"
        r"CreateThread\(nullptr, 0, AmdStallWatchThread, &watch, 0, nullptr\).*?"
        r"const bool made = g_amd\.fsr\.CreateContexts\(.*?\); "
        r"if \(watch\.done != nullptr\) SetEvent\(watch\.done\); "
        r"if \(watcher != nullptr\) \{ WaitForSingleObject\(watcher, INFINITE\); CloseHandle\(watcher\); \} "
        r"if \(watch\.done != nullptr\) CloseHandle\(watch\.done\);", fb)
    if not seq:
        failures.append("the watch is not started before the call and set, joined and closed "
                        "after it in that order - it could outlive the event it waits on")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: each ffxCreateContext is named before and after, and a setup that does "
          "not return within the stall window logs the step it is stuck in")
    return 0


if __name__ == "__main__":
    sys.exit(main())
