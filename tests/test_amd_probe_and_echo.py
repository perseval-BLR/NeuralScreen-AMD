"""A path that comes from argv[0] is made absolute before LoadLibraryEx, and the
engine's health echo reads only this run's bytes.

Two bugs, both ours, both found by reporters on live cards, and both of the
same kind: a measurement tool that quietly answers about the wrong thing.

1. `probe_amd --init` died with `LoadLibrary failed (error 87)` from BOTH the
   extracted folder and from inside the native folder (RX 7900 XTX report, and
   the same line in a 9070 XT report). 87 is ERROR_INVALID_PARAMETER, and the
   parameter is the path: `dir` is built from argv[0], so it is relative
   whenever the command runs from the folder the README prints, while
   `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR` accepts only a fully qualified path.
   Measured on the bench, all four combinations:

       relative + DLL_LOAD_DIR  -> 87     .relative + DLL_LOAD_DIR -> 87
       relative + DEFAULT_DIRS  -> 126    relative, no flags       -> 126

   The flag was ADDED to fix 126, one release before the 87 report - so this is
   a fix that became a regression, and only a run from a second folder shows it.

2. The engine log is appended to across launches. `AmdEngineHealth` read the
   last 32 KB with no lower bound, so if this run appended nothing yet, the
   `encoded mean` it quoted belonged to a PREVIOUS launch - and that line is
   exactly the one a black-picture report is read for. `LogContains` was always
   careful about this; the health echo was not.

Checked structurally, because neither can be produced on an NVIDIA bench: the
runtime needs a Radeon and the stale line needs a previous run that wrote one.

Run:  runtime\\python.exe tests\\test_amd_probe_and_echo.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PROBE = BASE / "native" / "amd" / "probe_amd.cpp"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"
RUNTIME_H = BASE / "native" / "amd" / "amd_runtime.h"


def main() -> int:
    failures = []
    probe = PROBE.read_text(encoding="utf-8", errors="replace")
    bridge = BRIDGE.read_text(encoding="utf-8", errors="replace")
    header = RUNTIME_H.read_text(encoding="utf-8", errors="replace")

    # --- 1. the probe resolves argv[0]'s folder before using it -------------
    if "GetFullPathNameW" not in probe:
        failures.append("probe: nothing makes the folder absolute - a relative "
                        "argv[0] reaches LoadLibraryEx with a flag that "
                        "rejects it (error 87)")
    elif "std::wstring absolute_dir(" not in probe:
        failures.append("probe: absolute_dir() is gone but GetFullPathNameW is "
                        "still called somewhere - the resolution is not in one "
                        "named place any more")

    # Every path that ends up in a LoadLibraryEx call must be resolved.
    # The entry point is where argv[0] and the optional argument arrive.
    entry = probe[probe.find("int wmain("):] if "int wmain(" in probe else ""
    if not entry:
        failures.append("probe: wmain is gone - this test cannot see the entry "
                        "point that builds the folder")
    else:
        for src, what in (("absolute_dir(exe_dir)", "argv[0]'s folder"),
                          ("absolute_dir(argv[i])", "the folder given as an argument")):
            if src not in entry:
                failures.append(f"probe: {what} is not passed through "
                                f"absolute_dir() at the entry point")

    # And the call that needs it must use the resolved value. The name is
    # `checked_name` rather than `kRuntimeName` since the probe learned to check
    # whichever image the program would load (NS_AMD_V0310 picks the v0.3.1
    # file), so the path is built from a variable - but from `dir`, which is the
    # part this test exists for: a relative folder is the error-87 bug.
    at = probe.find("(dir + L\"\\\\\" + checked_name)")
    if at < 0:
        at = probe.find("(dir + L\"\\\\\" + kRuntimeName)")
    if at < 0:
        failures.append("probe: the runtime load no longer builds its path from "
                        "`dir` - this test can no longer see it")
    else:
        # the two loader flags must still be there: dropping them was the
        # earlier bug (126), and the fix for it must not be undone either way
        window = probe[at:at + 400]
        if "LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR" not in window:
            failures.append("probe: DLL_LOAD_DIR is gone from the runtime load - "
                            "that is the error-126 bug coming back")
        if "LOAD_LIBRARY_SEARCH_DEFAULT_DIRS" not in window:
            failures.append("probe: DEFAULT_DIRS is gone from the runtime load")

    # --- 2. the health echo starts at this run's offset ---------------------
    if "unsigned long long LogFrom() const" not in header:
        failures.append("header: Runtime::LogFrom() is gone - the health echo "
                        "has no way to know where this run's bytes begin")
    if "log_from_ = LogEndOffset(" not in RUNTIME_H.read_text(encoding="utf-8") and \
       "log_from_ = LogEndOffset(" not in (BASE / "native" / "amd" / "amd_runtime.cpp").read_text(encoding="utf-8"):
        failures.append("runtime: log_from_ is never set from the log's size "
                        "before the module loads")

    health_at = bridge.find("static void AmdEngineHealth()")
    if health_at < 0:
        failures.append("bridge: AmdEngineHealth is gone - this test cannot see "
                        "the health echo")
    else:
        # The function body ends at the next top-level definition.
        nxt = bridge.find("\n// The 1x1 exposure", health_at)
        body = bridge[health_at:nxt if nxt > 0 else health_at + 4000]
        if "LogFrom()" not in body:
            failures.append("bridge: the health echo does not read this run's "
                            "offset - it can quote a previous launch's "
                            "`encoded mean` as the current frame")
        # A bare `size - kWant` clamp is the bug: the read must never start
        # before `from`.
        if re.search(r"pos\.Part\s*=\s*size\.Part\s*-\s*kWant\b", body):
            failures.append("bridge: the health echo clamps to the last 32 KB "
                            "with no lower bound again - that is the stale-line "
                            "bug verbatim")

    # --- 3. both are named in the log a user reads -------------------------
    # The probe prints the folder it works in; a relative one was the visible
    # symptom, so it has to stay visible for the next report.
    if 'out("directory: %ls' not in probe:
        failures.append("probe: the folder is no longer printed - a report would "
                        "not show whether resolution worked")

    if failures:
        print("FAIL: the probe/echo fixes are not in place:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: the probe resolves its folder before loading, and the health "
          "echo reads only this run's bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
