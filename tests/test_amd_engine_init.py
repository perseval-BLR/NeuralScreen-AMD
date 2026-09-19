"""The four things this build changed, each one a measured failure first.

Every check here is written against a FUNCTION, not against a name. A name
appears in comments, in copies, and in the log strings of the very code being
guarded, so a search for the name passes while the code is broken - which is
how three earlier guards in this project were written and had to be rewritten.

1. THE ENGINE-INIT VERDICT MUST BE ASKED AFTER THE SWAPCHAIN EXISTS.

   Load() used to wait 3 s for the engine's own `engine init ok` line and
   report "THE ENGINE NEVER CAME UP" when it did not appear. The engine writes
   that line only after the host's swapchain is created - its own order is
   `swapchain created` -> `env: HIP` -> `using HIP device` -> `engine init ok`
   - and Load() runs before that swapchain exists. The wait therefore always
   expired. Measured on one reporter's run: four launches, `engine init ok`
   present in the engine's log all four times, four false negatives from us.

2. THE RETRY MUST EXIST, AND IT MUST BE BOUNDED.

   Both hosts that produce a picture write the HIP index they resolved into the
   engine (`At<int>(runtime, kRvaHipDevice) = hipDevice`). We resolve the same
   index by adapter LUID and used to keep it to ourselves, leaving the engine's
   field at -1 (auto). The retry hands it over - once, only after auto failed.

3. AN ENGINE THAT NEVER CAME UP MUST NOT BE FED.

   A reporter's worker died on frame 0 at D3D12Core.dll + 0x12ACCC (read of
   0x18C) on every launch, on two builds, with `engine init ok` absent every
   time. We were driving whole frames - surfaces, FSR contexts, dispatch,
   submission - at an engine that had never initialised.

4. `encoded mean 0.000` IS NOT A BLACK FRAME.

   It is present in EVERY log collected, including runs on v0.2.14 known to
   produce a picture and runs whose `self-check: pre-block zero bytes` reads
   the healthy 0.13%. It describes the engine's own encode stage, not the frame
   we handed it. The exposure value is what separates the cases: 4.0000 is its
   ordinary value, 9999.9980 is the ceiling it runs to on a zero input.

Run:  runtime\\python.exe tests\\test_amd_engine_init.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RUNTIME_CPP = BASE / "native" / "amd" / "amd_runtime.cpp"
RUNTIME_H = BASE / "native" / "amd" / "amd_runtime.h"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def function_body(text: str, signature: str) -> str:
    """The body of one function, from its DEFINITION to its closing brace.

    Written because every earlier guard in this project that searched a whole
    file was fooled by a copy of the same code somewhere else - and the copies
    are real: `WaitJobs` and `SyncCount` both mention the same counter, and the
    log strings of the guarded code contain the very words being searched for.

    Skips a forward declaration. This file declares several of these functions
    before defining them (the router above needs the name), and starting at the
    declaration runs the search into the NEXT function's body instead - which
    made this guard report a healthy frame path as missing its verdict.
    """
    at = 0
    while True:
        at = text.find(signature, at)
        if at < 0:
            return ""
        # A declaration ends at ';' before any '{': skip it and keep looking.
        rest = text[at + len(signature):]
        brace = rest.find("{")
        semi = rest.find(";")
        if brace < 0:
            return ""
        if 0 <= semi < brace:
            at += len(signature)
            continue
        depth = 0
        started = False
        for i in range(at, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
                started = True
            elif c == "}":
                depth -= 1
                if started and depth == 0:
                    return text[at:i + 1]
        return text[at:]


def main() -> int:
    failures = []
    cpp = RUNTIME_CPP.read_text(encoding="utf-8", errors="replace")
    hdr = RUNTIME_H.read_text(encoding="utf-8", errors="replace")
    bridge = BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- 1. the verdict moved out of Load() -------------------------------
    load_body = function_body(cpp, "bool Runtime::Load(")
    if not load_body:
        failures.append("Runtime::Load is gone - the loader this guard describes "
                        "cannot be checked")
    else:
        # The wait must NOT be inside Load: that is the false negative.
        #
        # Checked as a CALL WITH THAT NEEDLE, not as the mere presence of
        # LogContains and not as the needle string. Both weaker forms are
        # wrong here and both were tried: Load legitimately calls LogContains
        # to wait for the runtime's own hook lines, and the phrase
        # "engine init ok" sits in Load's comment explaining why the wait moved
        # out. Only the call with this needle is the returning bug.
        if re.search(r'LogContains\s*\([^;]*"engine init ok"', load_body, re.S):
            failures.append(
                "Load() waits for 'engine init ok' again - the engine writes it "
                "only after the host's swapchain exists, which is created AFTER "
                "Load returns, so this wait always expires and reports a healthy "
                "engine as dead (measured: 4 of 4 launches)")
        # And the path must be remembered, or the later poll cannot run.
        if "engine_log_path_" not in load_body:
            failures.append(
                "Load() no longer records the engine log path, so the frame-loop "
                "poll has nothing to read")

    # The poll must exist and must distinguish pending from absent.
    poll = function_body(cpp, "bool Runtime::PollEngineInit(")
    if not poll:
        failures.append("Runtime::PollEngineInit is gone - nothing asks the "
                        "engine's verdict where it can be answered")
    else:
        if "engine_init_seen_" not in poll:
            failures.append("PollEngineInit never records the 'up' verdict")
        if "engine_init_absent_" not in poll:
            failures.append(
                "PollEngineInit has no 'conclusively absent' verdict - without "
                "it 'not yet' and 'not ever' cannot be told apart, which is the "
                "exact confusion this function exists to remove")
        # The pending case must RETURN false rather than conclude, and it must
        # be reached by a real decision - not merely present somewhere.
        #
        # Two holes were found here by breaking the code on purpose:
        #   (a) a bare `return false;` satisfied the old check while every path
        #       above it concluded `absent` immediately, so "not yet" could no
        #       longer be answered at all;
        #   (b) deleting the "has anything been written yet" test was not
        #       noticed, although that test IS the pending verdict: without it
        #       an engine that simply has not initialised yet is declared dead.
        if "return false;" not in poll:
            failures.append("PollEngineInit cannot answer 'still pending'")
        # The comparison itself, not the variable: `size_now` is declared at
        # the top of the function, so a name search passes with the test gone.
        if not re.search(r"size_now\s*<=\s*log_from_", poll):
            failures.append(
                "PollEngineInit no longer checks whether the engine wrote "
                "anything this run - without that test 'has not initialised "
                "yet' and 'never will' are the same answer, which is the bug "
                "this function exists to fix")
        # And the 'cannot read the file' case must stay pending, not absent.
        if not re.search(r"LogEndOffsetEx\s*\([^)]*\)\s*\)\s*return false", poll):
            failures.append(
                "PollEngineInit concludes when it cannot read the engine log - an "
                "unreadable file is not evidence that the engine failed")
        # It must bound its own wait by ACCUMULATING and COMPARING, or it
        # concludes on the first call, before the engine can init.
        #
        # Not `"budget_ms" in poll`: the parameter is in the signature, so that
        # check passes with the comparison deleted - it did.
        if not re.search(r"waited_ms_\s*\+=", poll):
            failures.append("PollEngineInit does not advance a wait clock")
        if not re.search(r"waited_ms_\s*>=\s*budget_ms", poll):
            failures.append(
                "PollEngineInit never compares its wait against the budget - it "
                "would declare the engine absent on the very first call")

    # The bridge must call it from the frame path.
    if "AmdEngineInitSettled()" not in bridge:
        failures.append("nothing in the bridge asks the engine's verdict")
    else:
        eval_body = function_body(bridge, "static bool AmdEvaluateVideo(")
        if "AmdEngineInitSettled" not in eval_body:
            failures.append(
                "the engine verdict is not asked from the frame path - that is "
                "the only place the swapchain exists, so asking anywhere else "
                "reproduces the original false negative")

    # --- 2. the retry ------------------------------------------------------
    retry = function_body(cpp, "bool Runtime::RetryInitWithHipIndex(")
    if not retry:
        failures.append(
            "Runtime::RetryInitWithHipIndex is gone - the repair both working "
            "hosts apply by default (writing the resolved HIP index into the "
            "engine) is unavailable again")
    else:
        if "kHipDevice" not in retry:
            failures.append("the retry does not write the engine's HipDevice "
                            "field, so it retries with the same failing input")
        if "guarded_init" not in retry:
            failures.append("the retry does not call the engine's init - it "
                            "writes a field and returns")
        if "index < 0" not in retry:
            failures.append("the retry does not refuse a negative index; "
                            "-1 is the auto match, i.e. what already failed")

    # The resolved index must be KEPT, not discarded.
    if "ResolvedHipIndex" not in hdr:
        failures.append("the loader's resolved HIP index is not exposed")
    if "g_amd.hip_index_resolved = g_amd.runtime.ResolvedHipIndex();" not in bridge:
        failures.append(
            "the bridge does not keep the resolved HIP index, so the retry has "
            "nothing to hand over")

    # And the retry must be bounded to one attempt per run.
    settled = function_body(bridge, "static void AmdEngineInitSettled(")
    if not settled:
        failures.append("AmdEngineInitSettled is gone")
    else:
        # The bound must be a GATE, not a mention. `engine_retry_done` appears
        # in the assignment that sets it and in the log line that explains the
        # retry, so a search for the name passes while the guard is gone - it
        # did, and that is how this check was found to be a hole.
        if not re.search(r"if\s*\(\s*g_amd\.engine_retry_done\s*\)", settled):
            failures.append(
                "AmdEngineInitSettled has no one-attempt bound: it would retry "
                "every frame and bury the log")
        # Order matters: the retry must come AFTER the absent verdict, never
        # before it, or a healthy engine gets its auto path pre-empted.
        at_poll = settled.find("PollEngineInit")
        at_retry = settled.find("RetryInitWithHipIndex")
        if at_poll < 0 or at_retry < 0:
            failures.append("AmdEngineInitSettled no longer both polls and retries")
        elif at_poll > at_retry:
            failures.append(
                "the retry runs BEFORE the verdict is known - it must be the "
                "fallback after auto failed, not a replacement for it")
        # Seen must return before any retry.
        at_seen = settled.find("EngineInitSeen()")
        if at_seen < 0 or (at_retry >= 0 and at_seen > at_retry):
            failures.append(
                "an engine that IS up is not short-circuited before the retry - "
                "a healthy run would be re-initialised")

    # --- 3. the gate -------------------------------------------------------
    if "engine_dead_announced" not in bridge:
        failures.append("the dead-engine fallback has no once-only announcer")
    else:
        eval_body = function_body(bridge, "static bool AmdEvaluateVideo(")
        gate_at = eval_body.find("engine_dead_announced")
        if gate_at < 0:
            failures.append(
                "the frame path does not fall back when the engine never came up "
                "- it drives surfaces, FSR contexts, a dispatch and a submission "
                "at an engine that is not there (crash: D3D12Core + 0x12ACCC)")
        else:
            # The gate must require a SETTLED, ABSENT verdict - never a pending
            # one, and never a live engine.
            window = eval_body[max(0, gate_at - 700):gate_at + 300]
            if "EngineInitSettled()" not in window:
                failures.append("the gate does not require a settled verdict - it "
                                "would fire while the answer is still pending")
            if "!g_amd.runtime.EngineInitSeen()" not in window:
                failures.append("the gate does not require the engine to be "
                                "absent - it would fire on a live engine")

    # --- 4. the mean is not called black -----------------------------------
    health = function_body(bridge, "static void AmdEngineHealth()")
    if not health:
        failures.append("AmdEngineHealth is gone")
    else:
        mean_at = health.find("encoded mean")
        if mean_at < 0:
            failures.append("the engine's own measure is no longer reported")
        else:
            # The old wording was the bug: it told every reader of a live log
            # to look for a black frame that was not there.
            if "BLACK frame (our side produced it)" in health:
                failures.append(
                    "'encoded mean 0.000' is called a black frame again - it is "
                    "present in every log collected, including healthy v0.2.14 "
                    "runs with self-check 0.13%, so the claim sends readers to "
                    "the wrong half of the problem")
            if "9999.99" not in health:
                failures.append(
                    "the mean line no longer consults the exposure, which is the "
                    "value that actually separates a zero input from a normal one")

    if failures:
        print("FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: the engine-init verdict is asked where it can be answered, "
          "the retry is bounded and comes after auto, the dead-engine fallback "
          "is gated on a settled verdict, and the mean is no longer called black")
    return 0


if __name__ == "__main__":
    sys.exit(main())
