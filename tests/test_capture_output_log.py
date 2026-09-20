"""The capture log names the outputs it can actually use (issue #96 gap).

A diagnostic package lists every display driver in the registry, including
display-only adapters (Parsec, Cherry, virtual desktop tools) that DXGI never
reports as an adapter. So "the bundle lists a Parsec display but the log never
mentions it" had no honest answer, and a reporter could not say whether the
capture was on the real monitor or the virtual one.

The worker now enumerates the chosen adapter's outputs into the log, one line
per output plus a total, right where the captured output is picked.

Checked here by reading the source the worker is built from (the log itself
needs a live desktop capture):
  * the enumeration happens inside EnumCaptureOutput, before the NS_OUTPUT
    match, so the lines appear even when the named output is not found;
  * every output is named with its device name and rectangle;
  * a total line reports how many outputs exist, so "0 outputs" cannot be
    mistaken for "no capture attempt";
  * the enumeration releases each output it probes (no leak per call).

Run:  runtime\\python.exe tests\\test_capture_output_log.py
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "native" / "dlss5-feed-host64.cpp"


def body_of(text: str, signature: str) -> str:
    """The body of the function whose signature contains `signature`.

    A forward declaration must not be mistaken for the definition, so the
    LAST occurrence wins, and the body runs to the matching closing brace of
    the signature's own parentheses.
    """
    idx = text.rfind(signature)
    if idx < 0:
        return ""
    start = text.find("{", idx)
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def main() -> int:
    if not WORKER.exists():
        print(f"FAIL: {WORKER} not found")
        return 1
    src = WORKER.read_text(encoding="utf-8", errors="replace")
    failures: list[str] = []

    body = body_of(src, "static IDXGIOutput *EnumCaptureOutput")
    if not body:
        failures.append("EnumCaptureOutput has no body in the worker source")

    if body:
        # 1. The enumeration exists, names each output, and totals them.
        if "EnumOutputs(i, &probe)" not in body:
            failures.append("the output enumeration is gone - the log can no "
                            "longer say which outputs the adapter exposes")
        if "[cap] output %u: %ls" not in body:
            failures.append("per-output lines are gone - a virtual display "
                            "would be invisible in the log")
        if "exposes %u output(s)" not in body:
            failures.append("the total line is gone - '0 outputs' cannot be "
                            "told from 'no attempt'")
        # 2. Device name and rectangle are both reported: a name alone does
        #    not say where the output is, and the rectangle is what shows a
        #    virtual display sitting outside the real desktop.
        if "d.DeviceName" not in body or "DesktopCoordinates.left" not in body:
            failures.append("an output line omits its name or its rectangle")
        # 3. It must run BEFORE the NS_OUTPUT match, so the lines are written
        #    even when the named output is missing - that is the case where a
        #    reporter most needs them. The match is the name comparison, not
        #    the reading of the variable: reading happens first either way.
        enum_at = body.find("EnumOutputs(i, &probe)")
        match_at = body.find("wcscmp(desc.DeviceName, want)")
        if match_at < 0:
            failures.append("the NS_OUTPUT match is no longer in this function")
        elif enum_at > match_at:
            failures.append("the enumeration runs after the NS_OUTPUT match - "
                            "a missing named output would leave no trace")
        # 4. Each probed output is released.
        probes = body.count("EnumOutputs(i, &probe)")
        releases = body.count("probe->Release()")
        if probes and releases < probes:
            failures.append(f"{probes} output probe(s) but only {releases} "
                            f"release(s) - the enumeration leaks")

    print("=" * 60)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the capture log names every output the adapter exposes, before "
          "the NS_OUTPUT match, and reports the total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
