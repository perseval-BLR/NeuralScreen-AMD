"""Two ways a Radeon user lost the whole program, both from the same click.

Issue #2, second half. The user ran v0.1.7 on a hybrid box (RX 9070 XT + RTX
3080) and reported two symptoms after touching the GPU picker and the neural
pass switch:

  * an infinite loop - every card in the picker reverted, forever;
  * a purple screen - no neural pass, just a magenta slab with the menu on it.

Both have the same root: the AMD path can be switched OFF from the menu
("CPU DIS", "NVOFA"), and in that state this build has neither NGX nor an AMD
runtime. The worker treated that as fatal and exited on startup. The app counted
three dead workers, turned the pass off, and every GPU switch reverted onto a
card with nothing wrong with it - so clicking through the list looped.

Checked here:
1. A video run whose NGX init failed CARRIES ON - the video loop has a
   passthrough branch of its own for a missing feature, and the program must
   reach it instead of dying first. Only --test and a game PID stay fatal.
2. The magenta fill in draw_overlay is only a key-cutter when the layer is
   keyed. On an OPAQUE layer (what a worker restart leaves behind) it paints
   the whole screen. draw_overlay must key the layer itself rather than trust
   each caller to have done it.
3. The two failures are distinguished for the user: a missing FILE is an
   install fault, not a verdict that the card cannot run the pass (that part
   lives in test_amd_install_verdict.py).

Run:  runtime\\python.exe tests\\test_amd_worker_survives.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SRC = BASE / "native" / "dlss5-feed-host64.cpp"
DISPLAY = BASE / "display.py"


def main() -> int:
    failures = []
    host = SRC.read_text(encoding="utf-8", errors="replace")
    display = DISPLAY.read_text(encoding="utf-8", errors="replace")

    # --- 1. an NGX-less video run carries on ------------------------------
    i = host.find("if (!InitNgx())")
    if i < 0:
        failures.append("the NGX init block is gone from main()")
    else:
        block = host[i:i + 1600]
        # The refusal must be limited to the modes that genuinely need NGX.
        if "return 1;" not in block:
            failures.append("nothing refuses a missing NGX any more - --test "
                            "and a game PID would run without a feature")
        # A bare `video` gate: the AMD-path predicate must NOT be part of the
        # condition, or clicking the backend switch to CPU kills the worker.
        #
        # The condition is read by walking BACK from the log line rather than
        # with a regex over the parenthesis: a nested call like
        # `video && AmdPathRequestedEarly()` (exactly the shape that caused the
        # bug) breaks any `[^)]*` pattern, and the check would then "catch" a
        # regression by accident instead of by reading the condition.
        carry = block.find("Log(\"[host] the NGX library is not present")
        if carry < 0:
            failures.append("the 'carrying on' line is gone - a Radeon user "
                            "gets a dead worker instead of a picture")
        else:
            head = block[:carry]
            # The nearest `if (` before the line is the one guarding it.
            cond_at = head.rfind("if (")
            cond = head[cond_at + 4:head.find("\n", cond_at)] if cond_at >= 0 else ""
            if "AmdPathRequestedEarly" in cond:
                failures.append("the carry-on still depends on the AMD path "
                                "predicate: switching the backend to CPU on a "
                                "Radeon kills every worker (the issue #2 loop)")
            if "video" not in cond:
                failures.append("the carry-on no longer checks that this is a "
                                "video run - --test would survive a missing NGX")

    # --- 2. the magenta fill keys the layer ------------------------------
    j = display.find("def draw_overlay")
    if j < 0:
        failures.append("draw_overlay is gone")
    else:
        body = display[j:display.find("\n    def ", j + 10)]
        fill = body.find("self.screen.fill(CHROMA_KEY)")
        if fill < 0:
            failures.append("draw_overlay no longer fills with the colour key")
        else:
            before = body[:fill]
            # The guard must appear in draw_overlay itself, before the fill.
            if "LAYER_OPAQUE" not in before or "set_hud_only(True)" not in before:
                failures.append("draw_overlay fills the key without first "
                                "keying the layer: an OPAQUE layer (a worker "
                                "restart leaves one) paints the screen magenta")

    # --- 3. the verdict tokens still exist where the docs say ------------
    for token in ("the NGX library is not present",):
        if token not in host:
            failures.append(f"the worker no longer logs {token!r} - the "
                            f"verdict readers look for it")

    print(f"    draw_overlay body: {len(body) if j >= 0 else 0} chars")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print("OK: a Radeon keeps its worker when the pass is switched off, and "
          "the magenta fill cannot reach an unkeyed layer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
