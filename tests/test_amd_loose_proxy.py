"""The loose `version.dll` must not be left beside the worker.

The worker is an EXE that statically imports VERSION.dll (it reads file
versions for its signature checks). Windows resolves a static import in the
executable's own folder FIRST, and version.dll is not a KnownDLL - measured on
this machine: with a copy of version.dll beside a test exe, `GetModuleHandleW(
"version.dll")` reports THAT file, and without one it reports
C:\\Windows\\SYSTEM32\\VERSION.dll.

The runtime is distributed AS `version.dll` - the name a game imports is how
the proxy gets into a game at all. So a copy of it lying in the folder the
worker starts from is not an inert leftover: it is the module that import binds
to, which puts a second, self-initialising engine inside the process. It enters
before the host has bound its device, queue or flags, runs its own hook
installer and its own frame loop, and it is the reason one recorded worker run
loads the runtime more than once.

Two engines in one process is the documented route to a black picture ("the two
cannot both hold the wheel"). The external host that produces a picture never
imports the name at all - its build links only kernel32/user32/d3d12/dxgi and
loads the runtime through an explicit path. We do import the name, so the file
that hijacks it has to go; `prepare_amd_runtime.py` loads the runtime BY NAME
(dlssnr_amd_pass1.dll) and must therefore remove the loose copy.

Checked here, structurally, because at run time this failure is silent: the
pass starts, reports healthy and paints nothing.

Run:  runtime\\python.exe tests\\test_amd_loose_proxy.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PREP = BASE / "tools" / "prepare_amd_runtime.py"


def main() -> int:
    failures = []
    src = PREP.read_text(encoding="utf-8", errors="replace")

    # --- 1. the helper exists and its docstring carries the reason ---------
    if "def drop_loose_proxy(" not in src:
        failures.append("drop_loose_proxy is gone: nothing removes the loose "
                        "version.dll, so it can be imported by the worker")
        print(f"    prepare script: {len(src)} chars")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1

    body = src[src.find("def drop_loose_proxy("):]
    body = body[:body.find("\ndef ")] if "\ndef " in body else body

    # --- 2. it targets the loose proxy, not dlssnr_amd_pass1.dll ----------
    if 'folder / "version.dll"' not in body:
        failures.append("the helper does not look for version.dll beside the "
                        "worker - it would remove the wrong file")
    if "unlink()" not in body:
        failures.append("the helper never removes the file")

    # --- 2b. it only removes a file that IS this runtime -------------------
    # `folder` is a user-supplied path. Pointed at a game's own folder, a bare
    # version.dll is the game's runtime - deleting it would break the game.
    # The delete is therefore gated on the file being the known image.
    gate = body.find("STOCK_SHA256, PATCHED_SHA256")
    kill = body.find("unlink()")
    if gate < 0:
        failures.append("the helper does not check the file is this runtime "
                        "before deleting it - pointed at a game folder it "
                        "would remove the game's own version.dll")
    elif kill >= 0 and gate > kill:
        failures.append("the hash check happens AFTER the delete, so it cannot "
                        "protect anything")

    # --- 3. it is CALLED on every path that leaves the files in place -----
    # A second run of the script is exactly the case where the installer's
    # `version.dll` is back, so every path that writes an image must drop it -
    # including the two early returns in main() and the already-patched one.
    # Counted by function name (the arguments differ per call site), and the
    # definition itself does not count.
    calls = re.findall(r"(?<!def )drop_loose_proxy\(", src)
    if len(calls) < 3:
        failures.append(f"drop_loose_proxy is called {len(calls)} time(s): it "
                        "must run on the early-return paths too, or a second "
                        "run leaves the hijacking file in place")

    # --- 4. the host loads the runtime BY NAME, never as version.dll -------
    runtime_h = BASE / "native" / "amd" / "amd_runtime.h"
    rh = runtime_h.read_text(encoding="utf-8", errors="replace")
    if 'kRuntimeName = L"dlssnr_amd_pass1.dll"' not in rh:
        failures.append("the host no longer loads the runtime by its own name "
                        "- if it loads version.dll, removing the loose copy "
                        "would break the pass instead of fixing it")
    if re.search(r'kRuntimeName\s*=\s*L"version\.dll"', rh):
        failures.append("the host loads the runtime AS version.dll: it would "
                        "then collide with the system module of the same name")

    # --- 5. the docstring keeps the measured fact, not a guess ------------
    for token in ("static import", "KnownDLL"):
        if token not in body:
            failures.append(f"the helper's own docstring no longer explains "
                            f"'{token}' - the next reader cannot tell why the "
                            "file is dangerous")

    print(f"    prepare script: {len(src)} chars, {len(calls)} call(s)")

    # --- 6. the A/B is ONE VARIABLE, and both images are written ----------
    #
    # The driver reads NS_AMD_PATCHED to pick which file to load, and the script
    # produces both, so the comparison needs no reinstall: same machine, same
    # session, one setting apart. Losing either half makes the A/B impossible to
    # run from a user's log.
    driver = (BASE / "native" / "amd" / "amd_runtime.cpp").read_text(
        encoding="utf-8", errors="replace")
    header = (BASE / "native" / "amd" / "amd_runtime.h").read_text(
        encoding="utf-8", errors="replace")
    if "NS_AMD_PATCHED" not in driver:
        failures.append("the driver never reads NS_AMD_PATCHED - the patched "
                        "image cannot be selected, so the A/B is unreachable")
    if "kRuntimeNamePatched" not in header:
        failures.append("amd_runtime.h has no name for the patched image")
    if 'dlssnr_amd_pass1_patched.dll' not in header:
        failures.append("the patched image has no file name - the two copies "
                        "would collide")
    # The default must be the STOCK image, whatever else is selectable. The
    # expression became a three-way choice when the driver gained a second
    # RELEASE (v0.3.1) as well as the second variant, so the check is now on the
    # shape rather than one exact string: the fall-through branch is the stock
    # name, and each other image is reachable only through its own flag.
    if "kRuntimeName);" not in driver and "kRuntimeName;" not in driver:
        failures.append("no branch falls through to the stock image name - the "
                        "driver must pick a non-default image only when asked")
    for flag, name in (("NS_AMD_PATCHED", "kRuntimeNamePatched"),
                       ("NS_AMD_V0310", "kRuntimeNameV0310")):
        if flag not in driver or name not in driver:
            failures.append(f"{name} is not selectable through {flag} - a build "
                            f"that cannot be chosen is a build nobody can test")
    # ...and both files must actually be produced.
    if "dlssnr_amd_pass1_patched.dll" not in src:
        failures.append("the prepare script never writes the patched copy")
    if "unpatch(" not in src:
        failures.append("there is no way back from an already-patched file, so "
                        "a user who has one cannot produce the stock half")

    # --- 7. the probe has to know both images ----------------------------
    probe = (BASE / "native" / "amd" / "probe_amd.cpp").read_text(
        encoding="utf-8", errors="replace")
    if "kStockHash" not in probe or "kPatchedHash" not in probe:
        failures.append("probe_amd only accepts one image: it would call the "
                        "stock build UNKNOWN, which is now the default")
    if "kRuntimePatchedName" not in probe:
        failures.append("probe_amd does not report the patched copy")

    # --- 8. the probe build must land where the ARCHIVE reads it ----------
    # build_release_zip.py ships `native/probe_amd.exe`; build-probe-amd.bat
    # compiles into `native/amd/`. Without the copy the archive silently packs
    # the previous probe - which happened: a probe that did not know the new
    # runtime image shipped alongside a build that did.
    bat = (BASE / "native" / "amd" / "build-probe-amd.bat").read_text(
        encoding="utf-8", errors="replace")
    if "probe_amd.exe" not in bat:
        failures.append("the probe build script does not name its output")
    if "..\\probe_amd.exe" not in bat or "copy /Y" not in bat.lower() and "copy /y" not in bat.lower():
        failures.append("the probe build does not copy its output to "
                        "native\\probe_amd.exe - the archive would ship the "
                        "previous probe")
    shipped = (BASE / "build_release_zip.py").read_text(
        encoding="utf-8", errors="replace")
    if '"native/probe_amd.exe"' not in shipped:
        failures.append("the archive no longer ships native/probe_amd.exe - "
                        "this check is looking at the wrong path")

    # --- 9. the runtime ships in the archive (unpack and it works) --------
    # Every runtime image must be in the payload, or the archive is not
    # self-contained and the user is back to a manual step before the first run
    # - which the logs show is the step that gets skipped ("dlssnr_amd_pass1.dll
    # not found", and no picture).
    #
    # All THREE, including v0.3.1: it is the build to try when the picture stays
    # black, and it spent one release as a file the user had to fetch with a
    # command. That is the same shape of mistake this check exists for, so the
    # check names it too - a runtime that is only reachable by hand is a runtime
    # most users will not reach.
    shipped_list = shipped  # build_release_zip.py source, read above
    for f in ("native/dlssnr_amd_pass1.dll",
              "native/dlssnr_amd_pass1_patched.dll",
              "native/dlssnr_amd_pass1_v0310.dll",
              "native/dlssnr_on_amd_weights.bin",
              "native/dlssnr_on_amd.ini"):
        if f'"{f}"' not in shipped_list:
            failures.append(f"{f} is not in the archive payload - the archive "
                            "is then not self-contained")
    # ...and the filter that hides native/ must let them through: everything in
    # native/ that is not a .dll is dropped unless it is named in RUNTIME_ASSETS.
    zip_src = (BASE / "build_release_zip.py").read_text(encoding="utf-8",
                                                        errors="replace")
    for f in ("native/dlssnr_on_amd_weights.bin", "native/dlssnr_on_amd.ini"):
        if f'"{f}"' not in zip_src.split("RUNTIME_ASSETS")[1].split(")")[0]:
            failures.append(f"{f} is not in RUNTIME_ASSETS, so the native/ "
                            "filter drops it from the archive")
    # The archive must NOT carry a version.dll - that name is the hijack.
    if '"native/version.dll"' in shipped_list:
        failures.append("the archive would ship a version.dll: beside the "
                        "worker that name is the system module, and the import "
                        "would bind the runtime into every process")

    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print("OK: the loose version.dll goes, and every runtime image the driver "
          "can load exists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
