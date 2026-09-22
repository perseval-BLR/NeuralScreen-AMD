"""Dispatch B can run on a private copy of the upscaler: one variable, off by default, named.

WHY THIS EXISTS
---------------
v0.3.21 bound motion vectors to dispatch B (NS_AMD_UPSCALE_MV=1). On a
7900 XTX (#3, 22.09) that run showed no corruption at all - including the four
warm-up frames, which on the default arm alternate 0% / 83.8% at >= 1024 - but
the runtime, which picks the dispatch it processes by "has motion vectors",
stopped ignoring B and re-created its staging 80 times for 4 network jobs. So
the test measured that loop as well as the vectors.

The runtime hooks ffxCreateContext / ffxDispatch inside the upscaler module it
found. A second copy of the same file, loaded by full path from a private
folder, is a separate image (checked on the bench: distinct handle, distinct
ffxDispatch address) whose code the hooks were never written into. B on that
image can carry motion vectors without the runtime seeing B at all.

WHAT THIS LOCKS
---------------
1. The copy is the shared module's OWN file (GetModuleFileNameW on it), under
   the SAME file name, in a private folder, loaded by full path. Same name so a
   driver that recognises the upscaler by name treats both alike - otherwise
   the arm would change two things.
2. A loader that hands back the shared module is a FAILURE, not a success: B
   would be on the hooked image and the log would lie.
3. B's context is created, dispatched and destroyed through ApiB(); A's only
   through the shared module. ApiB() is the shared module unless the copy
   loaded, so the default is what shipped.
4. The arm is NS_AMD_UPSCALE_PRIVATE, exactly "1", and LoadPrivateB is called
   only under it.
5. The log names the arm that RAN, including a requested arm that failed.

Run:  runtime\\python.exe tests/test_amd_upscale_private_arm.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FSR = BASE / "native" / "amd" / "amd_fsr.cpp"
HDR = BASE / "native" / "amd" / "amd_fsr.h"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


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
    for p in (FSR, HDR, BRIDGE):
        if not p.is_file():
            print(f"FAIL: {p.relative_to(BASE)} is missing")
            return 1
    fsr = strip_comments(FSR.read_text(encoding="utf-8", errors="replace"))
    hdr = strip_comments(HDR.read_text(encoding="utf-8", errors="replace"))
    br = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # --- 1/2. the copy: own file, same name, full path, not the shared module
    lp = flat(body_of(fsr, "Upscaler::LoadPrivateB("))
    if not lp:
        failures.append("no Upscaler::LoadPrivateB")
    else:
        if "GetModuleFileNameW(static_cast<HMODULE>(api_.module), src, MAX_PATH)" not in lp:
            failures.append("the copy is not taken from the shared module's own file")
        if not re.search(r"const std::wstring dst = dir \+ L\"\\\\\" \+ kUpscalerName;", lp):
            failures.append("the copy does not keep the upscaler's file name")
        if "CopyFileW(src, dst.c_str(), FALSE)" not in lp:
            failures.append("the shared file is not copied to the private path")
        if "LoadLibraryExW(dst.c_str()," not in lp:
            failures.append("the private copy is not loaded by its full path")
        guard = re.search(r"if \(mod == static_cast<HMODULE>\(api_\.module\)\) \{[^}]*return false; \}", lp)
        if not guard:
            failures.append("a loader that returns the shared module is not treated as a failure")
        for exp in ("ffxCreateContext", "ffxDispatch", "ffxDestroyContext"):
            if f'GetProcAddress(mod, "{exp}")' not in lp:
                failures.append(f"the private copy's {exp} is not resolved from it")
        # private_b_ may only become true after every check above.
        at_true = lp.find("private_b_ = true;")
        at_guard = guard.start() if guard else -1
        at_exports = lp.find("b.destroy == nullptr")
        if at_true < 0 or at_true < at_guard or at_true < at_exports:
            failures.append("private_b_ is set before the copy is proven to be a working second image")

    # --- 3. B through ApiB(), A through the shared module, default = shared
    if not re.search(r"const Api &ApiB\(\) const \{ return private_b_ \? api_b_ : api_; \}", flat(hdr)):
        failures.append("ApiB() is not 'the private copy if it loaded, else the shared module'")
    cc = flat(body_of(fsr, "Upscaler::CreateContexts("))
    if "create_fn(AsCtx(&ctx_net_.handle)" not in cc or "reinterpret_cast<CreateFn>(api_.create)" not in cc:
        failures.append("A's context is not created through the shared module")
    if not re.search(r"reinterpret_cast<CreateFn>\(ApiB\(\)\.create\);.*?rc = create_b\(AsCtx\(&ctx_up_\.handle\)", cc):
        failures.append("B's context is not created through ApiB()")
    rc = flat(body_of(fsr, "Upscaler::ReleaseContexts("))
    if not re.search(r"reinterpret_cast<DestroyFn>\(ApiB\(\)\.destroy\);.*destroy_b\(AsCtx\(&ctx_up_\.handle\)", rc):
        failures.append("B's context is not destroyed by the module that created it")
    if "destroy_fn(AsCtx(&ctx_net_.handle)" not in rc:
        failures.append("A's context is no longer destroyed through the shared module")
    du = flat(body_of(fsr, "Upscaler::DispatchUpscale("))
    if "reinterpret_cast<DispatchFn>(ApiB().dispatch)" not in du:
        failures.append("B is not dispatched through ApiB()")
    dn = flat(body_of(fsr, "Upscaler::DispatchNet("))
    if "reinterpret_cast<DispatchFn>(api_.dispatch)" not in dn:
        failures.append("A is not dispatched through the shared module - the runtime would lose it")

    # --- 4. the arm, exactly "1", and the only caller is under it ------------
    arm = body_of(br, "static bool AmdUpscalePrivateArm(")
    if not arm:
        failures.append("no AmdUpscalePrivateArm()")
    else:
        if 'GetEnvironmentVariableA("NS_AMD_UPSCALE_PRIVATE"' not in arm:
            failures.append("the arm does not read NS_AMD_UPSCALE_PRIVATE")
        if "v[0] == '1'" not in arm:
            failures.append("the arm does not require the value to be exactly 1")
    calls = [m.start() for m in re.finditer(r"\bLoadPrivateB\(", br)]
    if len(calls) != 1:
        failures.append(f"LoadPrivateB is called {len(calls)} times in the bridge, expected once")
    else:
        before = br[max(0, calls[0] - 200):calls[0]]
        if not re.search(r"if \(AmdUpscalePrivateArm\(\)\)\s*\{\s*std::string pwhy;\s*if \(g_amd\.fsr\.$",
                         before.rstrip() + " ", re.S) and \
           not re.search(r"if \(AmdUpscalePrivateArm\(\)\)\s*\{\s*std::string pwhy;\s*if \(g_amd\.fsr\.\s*$", before):
            failures.append("LoadPrivateB is not called only under AmdUpscalePrivateArm()")

    # --- 5. the log names the arm that ran -----------------------------------
    fb = flat(br)
    for needle, why in (
        ("the upscale dispatch's module: a private copy (%ls)", "the success arm"),
        ("was asked for and did NOT take effect (%s)", "the requested-but-failed arm"),
        ("the upscale dispatch's module: shared with the network", "the default arm"),
    ):
        if needle not in fb:
            failures.append(f"the log does not name {why}")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: B can run on a private copy of the upscaler (same file, same name, "
          "its own image), only under NS_AMD_UPSCALE_PRIVATE=1, and the log names "
          "the arm that ran")
    return 0


if __name__ == "__main__":
    sys.exit(main())
