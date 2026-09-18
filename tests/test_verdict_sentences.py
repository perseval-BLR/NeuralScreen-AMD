"""Every SAFE PASSTHROUGH the worker can print must be readable as a FAILURE.

A live report (issue #2, second half) caught this the hard way: the worker
printed

    [video] no neural runtime on this machine - SAFE PASSTHROUGH

and the menu kept its "processing" line. The verdict list had the token for
one SAFE PASSTHROUGH sentence and not for the others, so a machine with no
neural pass at all was presented as healthy - the user reads "processing",
sees an untouched picture, and has nothing to go on.

The failure mode is a LIST that someone forgets to extend when a new refusal
sentence is added, so this test derives the sentences FROM THE SOURCE instead
of naming them: whatever `Log("...[SAFE PASSTHROUGH]...")` the worker can
emit has to produce a verdict. A test that only checked the sentences we
already knew about would have passed on the broken code.

Checked:
1. Every SAFE PASSTHROUGH sentence in the worker's source yields a verdict.
2. The healthy line still yields True (the list was not made too greedy).
3. A refusal sentence is not misread as "card not supported" - the menu's
   two sentences mean different things to the user.

Run:  runtime\\python.exe tests\\test_verdict_sentences.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import settings_io  # noqa: E402

HOST = BASE / "native" / "dlss5-feed-host64.cpp"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def main() -> int:
    failures = []
    src = HOST.read_text(encoding="utf-8", errors="replace")
    src += BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- 1. derive the sentences from the source --------------------------
    sentences = re.findall(r'Log\(\s*"(\[[^"]*SAFE PASSTHROUGH[^"]*)"', src)
    # De-duplicate, keep order.
    seen, uniq = set(), []
    for s in sentences:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        failures.append("no SAFE PASSTHROUGH sentences found - the pattern "
                        "or the log calls changed, so this test proved nothing")

    blind = []
    for line in uniq:
        if settings_io.nr_verdict(reversed([line])) is not False:
            blind.append(line)
    if blind:
        for line in blind:
            failures.append(f"the worker can print this and the menu will not "
                            f"read it as a failure: {line!r}")

    # --- 2. the healthy line is still healthy -----------------------------
    for ok_line in ("[amd] ===== AMD path active =====",
                    "[pure] feature 18 ready"):
        if settings_io.nr_verdict(reversed([ok_line])) is not True:
            failures.append(f"a working pass no longer reads as success: "
                            f"{ok_line!r}")

    # --- 3. no refusal is misread as a hardware verdict -------------------
    for line in uniq:
        if settings_io.nr_verdict_unsupported([line]):
            failures.append(f"a soft refusal is being read as 'card not "
                            f"supported' (the user is told to give up on a "
                            f"fixable fault): {line!r}")

    # --- 4. the install faults stay distinct ------------------------------
    # A missing runtime must be an install fault AND a failure at the same
    # time: the menu says "no neural pass", a GPU switch is not reverted.
    install_line = ("[amd] the runtime did not come up: dlssnr_amd_pass1.dll "
                    "not found in <PATH>")
    if not settings_io.nr_verdict_is_install([install_line]):
        failures.append("a missing runtime file is no longer an install fault")
    if settings_io.nr_verdict(reversed([install_line])) is not False:
        failures.append("a missing runtime file does not read as a failure")

    print(f"    sentences derived from the worker: {len(uniq)}")
    for s in uniq:
        print(f"      {s[:88]}")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print("OK: every refusal the worker can print reaches the menu as one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
