"""The per-frame probe launcher exists, sets its variable, and ships.

WHY THIS EXISTS
---------------
The surface probe answers the one question a flicker report cannot answer by
itself: WHICH surface carries the defect. Its cadence is every 300th frame, so
on the short diagnostic runs it was built for it never fires once - and it is
turned to every frame by an environment variable.

Asking a reporter to set that variable by hand is asking something most of them
cannot do. One said so plainly, twice:

    "Sorry that I have no experience with coding, and I don't know how to set
     the NS_AMD_PROBE_EACH=1 environment variable. PLS tell me how I can do
     that in detail."

The same reporter had already attached three diagnostic packages. He is not
short of willingness; the instruction was wrong for the audience. The precedent
was already in this project - NeuralScreen-diag.vbs exists so a reporter never
touches NS_PHASE - and this is the same answer for the same reason.

WHAT THIS LOCKS
---------------
1. The launcher exists and sets NS_AMD_PROBE_EACH=1 in the LAUNCHED PROCESS's
   environment, which is where the worker reads it (GetEnvironmentVariableA at
   probe startup, before the worker is spawned).
2. It sets exactly that variable - not NS_PHASE, which is the other launcher's
   job, and not both at once: two diagnostic modes in one run make a log that
   says two things.
3. It reaches the archive. A launcher left out of the release is no answer at
   all, and the reporter who needs it does not have the repository.
4. autocheck requires it by name, so a future edit that drops it from the
   archive fails the build instead of silently shipping the old instruction.
5. The launcher carries the same pre-flight checks as the other two (pythonw,
   the runtime, the worker): failing silently on a missing file is the same
   class of defect as the one this file answers.

Run:  runtime\\python.exe tests/test_probe_launcher.py
"""
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LAUNCHERS = {
    "NeuralScreen.vbs": None,
    "NeuralScreen-diag.vbs": "NS_PHASE",
    "NeuralScreen-probe.vbs": "NS_AMD_PROBE_EACH",
}
BUILD = BASE / "build_release_zip.py"
AUTOCHECK = BASE / "tests" / "autocheck.py"


def main() -> int:
    failures: list[str] = []

    # --- 1/2/5. each launcher sets its own variable, and only its own -------
    for name, var in LAUNCHERS.items():
        path = BASE / name
        if not path.is_file():
            failures.append(f"{name} is missing")
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        # The apostrophe comment marker makes ' in prose dangerous here: read
        # the Environment assignment itself, then compare the variable NAME.
        set_vars = re.findall(r'shell\.Environment\("Process"\)\("([^"]+)"\)\s*=\s*"1"',
                              src)
        if var is None:
            if set_vars:
                failures.append(
                    f"{name} is the plain launcher but sets {set_vars} - a "
                    "diagnostic mode nobody asked for")
        else:
            if var not in set_vars:
                failures.append(
                    f"{name} does not set {var} - it is the whole reason the "
                    f"file exists (sets: {set_vars or 'nothing'})")
            extra = [v for v in set_vars if v != var]
            if extra:
                failures.append(
                    f"{name} also sets {extra} - two diagnostic modes in one "
                    "run make a log that says two things at once")
        # The pre-flight checks the other launchers carry.
        #
        # By MECHANISM, not by name: searching for the bare word "nvngx.dll"
        # passed with the whole FileExists branch deleted, because the string
        # also lives in the comments and in the launch line. A test that reads
        # a name instead of a mechanism is the pitfall this suite already has
        # a note about - it was caught here by the mutation run.
        checks = (
            (r'fso\.FileExists\(dir\s*&\s*"\\native\\nvngx_dlssnr\.dll"\)',
             "the NGX runtime check"),
            (r'fso\.FileExists\(dir\s*&\s*"\\native\\nvngx\.dll"\)',
             "the worker check"),
            (r'If py = "pythonw" Then\s*\n\s*Dim found[\s\S]{0,600}?'
             r'fso\.FileExists\(part\s*&\s*"\\pythonw\.exe"\)',
             "the python check"),
        )
        for pattern, why in checks:
            if not re.search(pattern, src):
                failures.append(f"{name} has no {why}")
        if 'dir & "\\main.py"' not in src:
            failures.append(f"{name} does not launch main.py")

    # The probe's own reader must look for the variable this launcher sets.
    bridge = BASE / "native" / "amd" / "amd_bridge.inl"
    if bridge.is_file():
        b = bridge.read_text(encoding="utf-8", errors="replace")
        if 'GetEnvironmentVariableA("NS_AMD_PROBE_EACH"' not in b:
            failures.append(
                "the bridge no longer reads NS_AMD_PROBE_EACH - the launcher "
                "would set a variable nothing consumes")

    # --- 3/4. it ships, and the build fails without it ---------------------
    for path, label in ((BUILD, "build_release_zip.py"),
                        (AUTOCHECK, "tests/autocheck.py")):
        if not path.is_file():
            failures.append(f"{label} is missing")
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if "NeuralScreen-probe.vbs" not in src:
            failures.append(
                f"{label} does not name NeuralScreen-probe.vbs - a launcher "
                "the reporter cannot get is no answer")

    # --- 6. the variable actually REACHES the launched process ------------
    #
    # Everything above reads the file. This one runs it: the launcher is
    # started by cscript against a stub main.py that writes down what it
    # inherited, and the child's own answer is compared with what the bridge
    # consumes. A launcher that sets the variable in the wrong scope - the
    # usual mistake - writes it for itself and the worker never sees it, and
    # no amount of reading the source shows that.
    failures.extend(behaviour())

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: the per-frame probe launcher exists, sets only its own variable "
          "in the launched process, carries the pre-flight checks, and ships")
    return 0


def behaviour() -> list:
    """Start each launcher for real and read what its child inherited."""
    import shutil
    import subprocess
    import tempfile
    import time

    problems: list[str] = []
    runtime = BASE / "runtime" / "pythonw.exe"
    if not runtime.is_file():
        return ["runtime/pythonw.exe is missing - cannot start a launcher"]

    sandbox = Path(tempfile.mkdtemp(prefix="ns-probe-launcher-"))
    try:
        (sandbox / "native").mkdir()
        # The launchers check for these before they start anything.
        (sandbox / "native" / "nvngx.dll").write_bytes(b"stub")
        (sandbox / "native" / "nvngx_dlssnr.dll").write_bytes(b"stub")
        (sandbox / "main.py").write_text(
            "import os, sys\n"
            "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),\n"
            "                       'seen.txt'), 'w') as f:\n"
            "    f.write(repr(os.environ.get('NS_AMD_PROBE_EACH')))\n"
            "    f.write('|')\n"
            "    f.write(repr(os.environ.get('NS_PHASE')))\n",
            encoding="utf-8", newline="")

        env = dict(os.environ)
        env["NEURALSCREEN_PYTHON"] = str(runtime)
        # The parent must not carry either variable, or the child would inherit
        # it and this check would pass with the launcher doing nothing at all.
        for var in ("NS_AMD_PROBE_EACH", "NS_PHASE"):
            env.pop(var, None)

        for name in LAUNCHERS:
            shutil.copy(BASE / name, sandbox / name)
            seen = sandbox / "seen.txt"
            if seen.exists():
                seen.unlink()
            try:
                subprocess.run(["cscript", "//nologo", str(sandbox / name)],
                               capture_output=True, text=True, env=env,
                               cwd=str(sandbox), timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                problems.append(f"{name} could not be started: {exc}")
                continue
            for _ in range(40):
                if seen.exists():
                    break
                time.sleep(0.25)
            if not seen.exists():
                problems.append(f"{name} never started main.py")
                continue
            probe, phase = seen.read_text(encoding="utf-8").split("|", 1)
            got = {"NS_AMD_PROBE_EACH": probe, "NS_PHASE": phase}
            for var in ("NS_AMD_PROBE_EACH", "NS_PHASE"):
                want = "'1'" if var == LAUNCHERS[name] else "None"
                if got[var] != want:
                    problems.append(
                        f"{name}: the child inherited {var}={got[var]}, "
                        f"expected {want} - the variable is set in the wrong "
                        "scope, so the worker never sees it")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return problems


if __name__ == "__main__":
    sys.exit(main())
