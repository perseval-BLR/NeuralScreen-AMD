"""Build the NeuralScreen AMD release archive: git files + artifacts + runtime.

The archive carries a VERSION.txt manifest (git commit, runtime SHA-256,
target architectures) so a user can tell exactly which build they have.
The build refuses to package a runtime whose kernels do not match the
expected set (the v1.5.0 mistake: a zip that claimed "everything" but
carried a Blackwell-only DLL).
"""
import hashlib
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# The script must work from any directory: every path is relative to git.
BASE = Path(__file__).resolve().parent
os.chdir(BASE)

VERSION = "0.3.17"
# This build is the AMD one: the pass runs on a Radeon through a third-party
# runtime the user prepares (native/AMD.md). The NVIDIA path stays in the
# binary for hybrid machines, and the bundled nvngx_dlssnr.dll is still the
# one the kernel check below describes - the same file is what the AMD
# runtime's installer reads the network weights from, which is why it ships.
TARGET_ARCHS = "AMD: Radeon RX 7000/9000 (RDNA3/RDNA4); NVIDIA path kept for hybrids (sm_86/89/120, spoof 0x1B0)"

files = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
extra = [
    # The BYO folder doc ships so user installs have the drop location ready.
    "native/libraries/README.md",
    # The AMD path's instructions: how to prepare the runtime, how to turn it
    # on, what the [amd] log lines mean. Same reason as the BYO doc - the
    # folder it describes is right there.
    "native/AMD.md",
    "native/nvngx_dlssg.dll",
    "resolution_limits.py",
    "NeuralScreen.exe",
    "NeuralScreen.vbs",
    "NeuralScreen-diag.vbs",
    "README.ru.md",
    "native/nvngx.dll",
    # The module the NGX calls leave from. Its file name is what the
    # feature library checks; without it Neural Rendering does not start.
    "native/nvngx.dll_ns-forwarder.dll",
    "native/nvngx_dlssnr.dll",
    # The AMD probe: the first thing to run on a Radeon machine. It answers
    # "are the files here, is this the known build, does HIP see the card"
    # before any of the pass is wired in - and its log is what a report
    # should carry when the pass does not start.
    "native/probe_amd.exe",
    # The FidelityFX upscaler. Bundled, unlike the runtime itself: it is AMD's
    # own signed DLL (AMD, California, Authenticode valid) and AMD's licence
    # grants binary redistribution, naming this exact file under its MIT terms
    # (docs/license.md, Kits\FidelityFX\signedbin\...). Without it the pass
    # runs, reports healthy and paints nothing - the runtime takes its frame
    # from an FSR dispatch, so with no upscaler to dispatch it has nothing to
    # attach to. Shipping it turns a silent black screen into a working pass.
    "native/amd_fidelityfx_upscaler_dx12.dll",
    # --- the AMD neural runtime, bundled -------------------------------------
    #
    # The archive is meant to be "unpack it and it works". Until this build it
    # was not: the runtime was a separate install step the user had to perform,
    # and the logs say what that cost - a 9070 XT owner whose pass never started
    # had exactly one line about it (`dlssnr_amd_pass1.dll not found`), asked why
    # the program "always looks to hook on NVOFA", and got no picture. A step
    # that must be performed before the first run is a step that gets skipped.
    #
    # So the four files the installer would have produced ship here:
    #
    #   dlssnr_amd_pass1.dll          the runtime, stock build (this is what runs)
    #   dlssnr_amd_pass1_patched.dll  the same build with the four patches
    #                                 (55 bytes, four places)
    #                                 (NS_AMD_PATCHED=1 selects it - the A/B)
    #   dlssnr_amd_pass1_v0310.dll    the runtime's NEXT release, v0.3.1
    #                                 (NS_AMD_V0310=1 selects it)
    #   dlssnr_on_amd_weights.bin     the network weights, 153 tensors
    #   dlssnr_on_amd.ini             the keys the runtime reads in its DllMain
    #
    # v0.3.1 ships too, and that is deliberate: it is the build to try when the
    # picture stays black, and its own release notes name fixes for what this
    # program kept meeting by hand - a wait method that reduces stalls, three
    # crashes, a memory leak, RE Engine startup crashes. Leaving it as a file the
    # user had to fetch with a command repeated the exact mistake the paragraph
    # above describes: `--download-v0310` existed, and a step that has to be
    # performed is a step that gets skipped. The user asked for every runtime to
    # be in the archive (19.09), and the archive is 7.3 MB heavier for it.
    #
    # It is NOT the default. v0.2.17 runs unless the user asks otherwise, because
    # a swap changes what every existing report is a report about.
    #
    # The weights were produced from `nvngx_dlssnr.dll`, which this archive has
    # always carried - verified byte-identical (sha256 dcc0dc24...) to the copy
    # that producing install used. The two v0.2.17 images are one build with five
    # bytes changed, which is why the driver's offset table accepts either.
    #
    # NOT here, on purpose: anything named `version.dll`. That name is how the
    # runtime is distributed for games, and beside this worker it is resolved as
    # the SYSTEM module by the worker's own import - a second engine inside the
    # process, entering before anything is bound. The preparation script removes
    # one if it finds it; the archive must not put one back.
    "native/dlssnr_amd_pass1.dll",
    "native/dlssnr_amd_pass1_patched.dll",
    "native/dlssnr_amd_pass1_v0310.dll",
    "native/dlssnr_on_amd_weights.bin",
    "native/dlssnr_on_amd.ini",
]
# tcl/tk stays out of the archive: the tkinter settings window is gone and the
# whole interface lives in the overlay menu. Nothing in the project imports
# tkinter (PIL/_tkinter_finder pulls it lazily and only for ImageTk).
TK_SKIP = ("runtime/tcl/", "runtime/tcl86t.dll", "runtime/tk86t.dll",
           "runtime/_tkinter.pyd", "runtime/Lib/tkinter/")

# The runtime was assembled for other work and drags in packages the program
# never touches: a web frontend, dataframes, a bundler. Checked through
# sys.modules after importing every project module — only av, cv2, numpy, PIL,
# pygame, pystray, dxcam and comtypes are needed. The rest is cut, ~130 MB
# before compression.
DROP_PACKAGES = {
    # gradio and its surroundings
    "gradio", "gradio_client", "hf_gradio", "huggingface_hub", "hf_xet",
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
    "annotated_types", "annotated_doc", "typing_inspection",
    "safehttpx", "groovy", "pydub", "python_multipart", "multipart",
    "orjson", "httpx", "httpcore", "h11", "anyio", "idna", "certifi",
    "fsspec", "filelock", "jinja2", "markupsafe", "tqdm", "audioop",
    "audioop_lts", "brotli", "_brotli", "yaml", "_yaml", "pyyaml",
    "semantic_version", "tomlkit", "typer", "click", "shellingham",
    "rich", "markdown_it", "markdown_it_py", "mdurl", "pygments",
    # dataframes and time zones
    "pandas", "pytz", "tzdata", "dateutil", "python_dateutil",
    # bundler
    "PyInstaller", "pyinstaller", "_pyinstaller_hooks_contrib",
    "pyinstaller_hooks_contrib", "altgraph", "pefile", "peutils", "ordlookup",
    "psutil",
    # the end user has no use for a package manager
    "pip",
    # setuptools and its distutils shim: nothing in the project imports
    # pkg_resources or setuptools (checked with grep over every module).
    # ~2.8 MB of dead weight in the archive (audit #3, N2).
    "setuptools", "pkg_resources", "_distutils_hack", "distutils-precedence.pth",
}
SP = "runtime/Lib/site-packages/"

# Dev-only files that must NOT reach the release archive (user rule
# 2026-09-08: the archive contains only what the program needs to run).
DEV_ONLY = {
    "autocheck.py",
    "run_tests.py",
    "build_release_zip.py",
    # A release-verification tool for the maintainer. It shipped to users and
    # would fail on the first line of work: it reads build_release_zip.py,
    # which is dev-only and not in the archive.
    "verify_github.py",
    # The docs folder holds the README screenshots - repository/release
    # assets, not program code. The archive must contain exactly what the
    # program needs to run (user rule 2026-09-08).
    "docs/",
    # Spout2 SDK dev baggage: the headers, the test tools and the build
    # scripts are for the repository, not for the end user. The release
    # needs only the two DLLs (Spout.dll, SpoutDX.dll) and the bridge
    # sources that the worker links.
    "native/include/spout/",
    "native/spout_bridge.h",
    "native/spout_bridge.cpp",
    "native/spout_sender.cpp",
    "native/spout_receiver.cpp",
    "native/spout_roundtrip.cpp",
    "native/spout_compile_check.cpp",
    "native/spout_adapter_check.cpp",
    "native/build-spout-test.bat",
    "native/build-spout-check.bat",
    "native/build-spout-adapter.bat",
    "native/SpoutDX.lib",
}


def _drop_sitepackage(norm: str) -> bool:
    if not norm.startswith(SP):
        return False
    entry = norm[len(SP):].split("/", 1)[0]
    for name in DROP_PACKAGES:
        # the package itself, its .py module, its .libs folder and dist-info
        if (entry == name or entry == name + ".py" or entry == name + ".libs"
                or entry.startswith(name + "-")):
            return True
    return False


def _skip(path: str) -> bool:
    norm = path.replace("\\", "/")
    if any(norm == p or norm.startswith(p) for p in TK_SKIP):
        return True
    # Dev-only files: the tests, the test runner and the release builder are
    # for the repository, not for the end user. The archive must contain
    # exactly what the program needs to run (user rule 2026-09-08).
    # '/test/' and '/tests/'-style paths catch the third-party testing
    # folders that ship inside site-packages (pygame/tests, comtypes/test,
    # win32ctypes/tests, numpy/testing) - library developer baggage.
    if norm in DEV_ONLY or norm.startswith("tests/") or norm.startswith("test_"):
        return True
    # The docs folder holds the README screenshots - repository/release
    # assets, not program code (user rule 2026-09-08: the archive must
    # contain exactly what the program needs to run).
    if norm.startswith("docs/"):
        return True
    # Spout2 SDK dev baggage: the headers, the test tools and the build
    # scripts are for the repository, not for the end user. The release
    # needs only the two DLLs (Spout.dll, SpoutDX.dll).
    if norm.startswith("native/include/spout/") or norm.startswith("native/spout_"):
        return True
    if norm.startswith("native/build-spout-"):
        return True
    if norm == "native/SpoutDX.lib":
        return True
    # The maintainer's own tools: the AMD preparation script and the worker
    # test driver live in tools/ and ship with the repository. The preparation
    # script DOES ship (the user runs it) - see the extra list; the worker
    # driver does not (it needs the built worker and a terminal).
    if norm == "tools/test_amd_worker.py":
        return True
    # The worker's source, the NGX headers and the import library are not
    # something the program runs: the archive carries the built DLLs. It could
    # not be rebuilt from the archive in any case - spout_bridge.cpp is
    # dev-only - so shipping half a source tree only invited the question.
    # The repository has all of it.
    # ... except the files the program itself runs or hands to the user:
    #   native/neuralscreen.ico - the tray icon (tray.py)
    #   native/probe_amd.exe    - the AMD probe the user runs by hand
    #   native/AMD.md           - the AMD path's instructions
    #   native/tools/*.py       - the user-facing helper scripts
    # Excluded, they do not crash anything - but the AMD path would be
    # undiscoverable and undiagnosable, which is worse. Named one by one
    # rather than by extension: the rule is "these files", and a .exe dropped
    # into native/ tomorrow is still developer baggage.
    RUNTIME_ASSETS = (
        "native/neuralscreen.ico",
        "native/probe_amd.exe",
        "native/AMD.md",
        # The AMD neural runtime's non-DLL half: the network weights and the ini
        # the runtime reads from its own DllMain. The "native/ is DLLs and build
        # inputs" rule is about keeping developer baggage out; these two are
        # runtime data, and without them the pass cannot start at all - which is
        # the failure this bundling exists to remove. Named one by one, same as
        # the rest of this list: a .bin dropped into native/ tomorrow is still
        # baggage.
        "native/dlssnr_on_amd_weights.bin",
        "native/dlssnr_on_amd.ini",
    )
    if (norm.startswith("native/") and not norm.endswith(".dll")
            and norm not in RUNTIME_ASSETS):
        return True
    if "/test/" in norm or "/tests/" in norm or "/testing/" in norm:
        return True
    # numpy's C test modules (_multiarray_tests.pyd etc.) ship next to the
    # real modules inside numpy/_core - same rule, they are developer
    # baggage, not program code.
    if "_tests." in norm or norm.startswith(SP + "pygame/tests/"):
        return True
    # pygame demo/docs folders and numpy pytest config: not program code.
    # NOTE: numpy/_pytesttester.py itself MUST stay - numpy/__init__.py
    # imports it unconditionally (verified).
    if norm.startswith(SP + "pygame/examples/") or norm.startswith(SP + "pygame/docs/"):
        return True
    if norm == SP + "numpy/conftest.py" or norm.startswith(SP + "numpy/ma/testutils"):
        return True
    if norm == SP + "numpy/_pytesttester.pyi":
        return True
    # .pyc/__pycache__ is dead weight (~13 MB in the zip): pythonw regenerates
    # them on the fly, a distribution does not need them.
    if norm.endswith(".pyc") or "/__pycache__/" in norm:
        return True
    return _drop_sitepackage(norm)


for root, _dirs, fs in os.walk("runtime"):
    for f in fs:
        path = os.path.join(root, f)
        if _skip(path):
            continue
        extra.append(path)

seen = set()
uniq = []
for f in files + extra:
    norm = f.replace("\\", "/")
    if norm in seen:
        continue
    seen.add(norm)
    # The same dev-only filter applies to the git-tracked files: the
    # tests and the builders must not reach the archive (user rule
    # 2026-09-08).
    if _skip(norm):
        continue
    uniq.append(norm)

# The license must be in every archive: a build that lost it is not a release.
if "LICENSE" not in uniq:
    raise SystemExit("LICENSE is missing from the archive payload. Refusing to build.")

# The FidelityFX upscaler must be there too, and for a quieter reason: without
# it the AMD pass initialises, runs its threads, prints a healthy self-check
# and paints a BLACK picture - the runtime takes its frame from an FSR
# dispatch, so with no upscaler to dispatch it has nothing to attach to. A
# release missing this file is indistinguishable from a broken one, and the
# user gets no error to report. Checked on disk rather than in `uniq` because
# it is a build input kept out of git (see .gitignore).
_UPSCALER = "native/amd_fidelityfx_upscaler_dx12.dll"
if not os.path.isfile(_UPSCALER):
    raise SystemExit(
        f"{_UPSCALER} is missing - the AMD pass would run and paint nothing. "
        "Get it from AMD's FSR SDK release (FidelityFX-Samples-*-prebuilt.zip, "
        "Samples/Upscalers/FidelityFX_FSR/dx12/x64/Release/) or from OptiScaler's "
        "FSR package, and put it in native/. Refusing to build.")
_upscaler_data = Path(_UPSCALER).read_bytes()
_upscaler_sha = hashlib.sha256(_upscaler_data).hexdigest()
print(f"upscaler: {_UPSCALER} {len(_upscaler_data)} bytes, "
      f"sha256 {_upscaler_sha[:16]}...")

out = f"neuralscreen-amd-v{VERSION}-full.zip"

# The runtime kernel check: the v1.5.0 zip shipped a Blackwell-only DLL as
# if it were universal. The kernel names live inside CUDA fatbin records
# (compressed cubins - plain string search finds nothing). Port of the
# proven parser from DLSS5-Autopilot (core/gpu.py, MIT).
import struct
import collections

_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)
SM_NAMES = {75: "sm_75", 86: "sm_86", 89: "sm_89", 120: "sm_120"}
KNOWN_SM = set(SM_NAMES) | {50, 52, 53, 60, 61, 62, 70, 72, 80, 90, 100, 101, 110}


def dll_architectures(path: str) -> set[int]:
    """Supported sm versions, from the CUDA fatbin records inside the DLL."""
    try:
        d = Path(path).read_bytes()
    except OSError:
        return set()
    found: collections.Counter[int] = collections.Counter()
    off = 0
    while True:
        i = d.find(_FATBIN_MAGIC, off)
        if i < 0:
            break
        off = i + 4
        try:
            hsize = struct.unpack_from("<H", d, i + 6)[0]
            fatsize = struct.unpack_from("<Q", d, i + 8)[0]
            if hsize < 16 or not (0 < fatsize <= len(d)):
                continue
            p, end = i + hsize, i + hsize + fatsize
            while p < end - 32:
                ehdr = struct.unpack_from("<I", d, p + 4)[0]
                payload = struct.unpack_from("<Q", d, p + 8)[0]
                if ehdr < 24 or ehdr > 4096 or not (0 < payload <= len(d)):
                    break
                for so in (24, 28, 20):
                    if p + so + 4 > len(d):
                        continue
                    sm = struct.unpack_from("<I", d, p + so)[0]
                    if sm in KNOWN_SM:
                        found[sm] += 1
                        break
                p += ehdr + payload
        except Exception:
            continue
    return set(found)


dll_path = Path("native/nvngx_dlssnr.dll")
dll_data = dll_path.read_bytes()
dll_sha = hashlib.sha256(dll_data).hexdigest()
print(f"runtime: {dll_path.name} {len(dll_data)} bytes, sha256 {dll_sha[:16]}...")
archs = dll_architectures(str(dll_path))
print(f"  kernels found: {', '.join(sorted(SM_NAMES.get(a, f'sm_{a}') for a in archs)) or 'NONE'}")
for want in (75, 86, 89, 120):
    if want not in archs:
        raise SystemExit(
            f"RUNTIME MISMATCH: sm_{want} not found in {dll_path.name} - the "
            f"archive would not run on the claimed cards. Refusing to build.")
    print(f"  kernel sm_{want}: ok")

# The manifest: built-from commit, runtime identity, target architectures.
# The user can verify which build they have without asking anyone.
commit = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], text=True).strip()
# One tag, one set of bytes. The archive is rebuilt after every commit so the
# static checks can compare it against HEAD - which means a zip built today
# still carries yesterday's version number, passes every gate, and would be
# accepted by a release that was tagged a dozen commits ago. Nothing caught
# that: autocheck compares the zip with HEAD, verify_github compares it with
# what is published, and neither has ever looked at the tag. So the manifest
# says which it is, and the build says it out loud.
try:
    tagged = subprocess.check_output(
        ["git", "rev-parse", "v" + VERSION + "^{commit}"], text=True,
        stderr=subprocess.DEVNULL).strip()
except Exception:
    tagged = ""
if not tagged:
    tag_line = "tag: v" + VERSION + " does not exist yet - release candidate\n"
elif tagged == commit:
    tag_line = "tag: v" + VERSION + "\n"
else:
    ahead = subprocess.check_output(
        ["git", "rev-list", "--count", "v" + VERSION + "..HEAD"],
        text=True).strip()
    tag_line = ("tag: NOT v" + VERSION + " - " + ahead
                + " commits past it (tagged " + tagged[:9] + ")\n")
    print("  NOTE: this archive is " + ahead + " commits past v" + VERSION
          + ". It is a development build - do NOT upload it to that tag. "
            "Bump the version, build, then tag.")
version_txt = (
    f"NeuralScreen AMD {VERSION}\n"
    f"commit: {commit}\n"
    + tag_line +
    f"runtime: nvngx_dlssnr.dll sha256 {dll_sha}\n"
    f"kernel archs: {', '.join(sorted(SM_NAMES.get(a, f'sm_{a}') for a in archs))}\n"
    f"targets: {TARGET_ARCHS}\n"
    f"built: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
)

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    z.writestr("VERSION.txt", version_txt)
    for f in uniq:
        if not os.path.isfile(f):
            print("MISSING:", f)
            continue
        # config.json comes ONLY from git HEAD, never from disk: the working
        # copy holds the developer's personal menu_offset/menu_scale/theme and
        # those must not ship. Comparing against the worktree is NOT enough —
        # a staged personal config (git add) would make the diff clean and the
        # personal values would leak into the zip (audit #2, R1).
        if f == "config.json":
            try:
                data = subprocess.check_output(["git", "show", "HEAD:config.json"])
                z.writestr(f, data)
            except subprocess.CalledProcessError:
                # config.json has never been committed — take it from disk.
                z.write(f, f)
            continue
        z.write(f, f)
print("entries:", len(uniq))
print("size:", os.path.getsize(out))
