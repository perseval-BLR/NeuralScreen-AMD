"""`NrReady()` decides "the pass is running" on BOTH paths - not `h.feature`.

WHY THIS EXISTS
---------------
The RNSZ rebuild path logged its verdict as

    Log("[video] RNSZ applied at %ux%u: %s, %s", ..., 
        h.feature != nullptr ? "feature ready" : "SAFE PASSTHROUGH", ...)

and `h.feature` is the NGX feature. On the AMD path there IS no NGX feature:
`AmdInit` replaces the feature end to end and never creates one, and the code
says so at the call site ("When it is on, no NGX call is made at all"). So on a
Radeon the pointer is null while the neural pass runs perfectly - and the line
printed

    RNSZ applied at 722x608: SAFE PASSTHROUGH, matched residual

after every resize, on every Radeon report, including the runs whose log shows
the engine taking one job per frame and `jobs seen` climbing. "SAFE PASSTHROUGH"
in the same breath as "matched residual" is the tell: one string is the NGX
feature's verdict and the other is the AMD pass's state, and they cannot both be
true.

The predicate that means "a neural pass is live, whichever path" already existed
and is used one line above:

    static bool NrReady() { return h.feature != nullptr || AmdActive(); }

WHAT THIS LOCKS
---------------
1. The rebuild verdict asks `NrReady()`, not the NGX pointer alone.
2. The verdict is still printed (a report needs it) - with both strings present.
3. The same mistake is not reintroduced anywhere else that reports a pass
   verdict while an AMD pass can be live.

Run:  runtime\\python.exe tests/test_amd_rnsz_verdict.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HOST = BASE / "native" / "dlss5-feed-host64.cpp"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def main() -> int:
    failures: list[str] = []
    if not HOST.exists() or not BRIDGE.exists():
        print("FAIL: the host or the bridge is missing")
        return 1
    code = strip_comments(HOST.read_text(encoding="utf-8", errors="replace"))
    bridge = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # --- the predicate exists and covers both paths -------------------------
    m = re.search(r"static bool NrReady\(\)\s*\{([^}]*)\}", bridge, re.S)
    if not m:
        failures.append("NrReady() is gone - there is no predicate that means "
                        "'a neural pass is live' for both paths")
    else:
        body = m.group(1)
        if "h.feature" not in body or "AmdActive()" not in body:
            failures.append(
                "NrReady() no longer covers both paths (it must be the NGX "
                "feature OR the AMD pass) - a Radeon would read as not ready")

    # --- the rebuild verdict uses it, not the NGX pointer -------------------
    applied = re.search(
        r'Log\("\[video\] RNSZ applied at %ux%u: %s, %s",[^;]*;', code, re.S)
    if not applied:
        failures.append("the RNSZ applied line is gone from the host")
    else:
        call = applied.group(0)
        if "NrReady()" not in call:
            failures.append(
                "the RNSZ rebuild verdict does not ask NrReady() - on a Radeon "
                "there is no NGX feature, so it reports SAFE PASSTHROUGH while "
                "the neural pass is running")
        if "h.feature != nullptr" in call:
            failures.append(
                "the RNSZ rebuild verdict judges by the NGX feature pointer "
                "again - that pointer is null on the AMD path by design")
        # Both strings must survive: a verdict a reader cannot see is useless.
        for token in ('"feature ready"', '"SAFE PASSTHROUGH"'):
            if token not in call:
                failures.append(f"the verdict no longer prints {token}")

    # --- no OTHER verdict line judges a live pass by h.feature alone --------
    # A line that says "SAFE PASSTHROUGH" (a pass verdict) while keying on
    # h.feature alone is the same bug in a new place.
    for m2 in re.finditer(r'Log\("[^"]*(?:SAFE PASSTHROUGH|not running)[^"]*"[^;]*;', code, re.S):
        line = m2.group(0)
        if "h.feature" in line and "NrReady" not in line:
            failures.append(
                "a pass verdict still keys on h.feature alone: "
                + " ".join(line.split())[:90])

    if failures:
        print("FAIL: a Radeon's live pass can be reported as passthrough")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: the pass verdict asks NrReady(), so a running AMD pass is not "
          "reported as SAFE PASSTHROUGH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
