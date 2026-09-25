"""The upscale dispatch's motion vectors are gated on the private copy.

WHY THIS EXISTS
---------------
The per-frame probe runs of 22.09 (issue #1 on an RX 9070 XT, issue #3 on an
RX 7900 XTX) show dispatch B's output alternating while its input is flat, and
the two states of each pair are COMPLEMENTARY: the fraction of the surface that
is wrong on one frame is the fraction that is right on the next

    1664x936  -> 2560x1440   100%  / 0%     (0.0473 vs 0.0000, input 0.049)
    1792x1006 -> 2560x1440   ~75%  / ~25%   (0.0114 vs 0.0333, input 0.047)
    2496x1356 -> 3840x2160   99.94% / 0.04% (63,962 vs 23.4)

So B rewrites every pixel every frame, and the rewrite is wrong on alternate
frames. It fails the same way on two cards whose FFX upscaler DLL takes
different paths, which points at what we hand B rather than at one
implementation. Of B's inputs, the motion vectors are the one ffx_upscale.h does
not mark optional, and B passed null.

The isolation came from the reporter's five runs on 25.09 (issue #3), all on a
7900 XTX with the same window and work scale:

    private, no vectors   92 of 98 frames carry a value >= 1024, flickers
    private + vectors      0 of 126,                            no flicker

Both runs are HEALTHY - 0 staging re-creations, 55 engine jobs over 60 frames -
so the vectors are the active ingredient and not a second thing changed at once.
They are now the SHIPPED DEFAULT, so binding them is no longer the variable.

WHAT THIS LOCKS
---------------
1. One variable: B's motionVectors come from a nullable parameter, and nothing
   else in B's descriptor reads it. motionVectorScale stays {0, 0}, so the
   surface's contents never reach the result.
2. THE GATE. The vectors may be bound ONLY while the private copy is in force.
   The runtime picks the dispatch it processes by "has motion vectors" and there
   is no ini key for that choice (the whole v0.2.17 surface is Enabled LocalTone
   LocalStructure SkinStructure UseAutoMask ToneChannels Scale Temporal Tonemap
   UseFsrInputs UseDepth Interop Inline InlineWaitMs HipDevice), so vectors on a
   B the runtime can see make it follow both and re-create staging on every
   switch - measured: 92 re-creations for 1 engine job over 54 frames. This is
   the one property that cannot be left to a comment.
3. It is B's OWN surface, not A's `motion`: A's carries real vectors, and
   handing it over would change what the vectors say as well as whether they
   exist.
4. The surface exists only under AmdUpscaleMvArm(), i.e. never on a run where
   the copy failed to load.
5. The line names what RAN, and a refused arm says why.
6. The surface is released with the others, so a resize cannot leak it or hand
   B a surface of the old extent.

Run:  runtime\\python.exe tests/test_amd_upscale_mv_knob.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FSR = BASE / "native" / "amd" / "amd_fsr.cpp"
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
    for p in (FSR, BRIDGE):
        if not p.is_file():
            print(f"FAIL: {p.relative_to(BASE)} is missing")
            return 1
    fsr = strip_comments(FSR.read_text(encoding="utf-8", errors="replace"))
    br = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # --- 1. B takes its vectors from a nullable parameter, nothing else ----
    up = body_of(fsr, "Upscaler::DispatchUpscale(")
    if not up:
        failures.append("DispatchUpscale could not be read")
    else:
        head = up[:up.find("{")]
        if not re.search(r"ID3D12Resource\s*\*motion\s*,", head):
            failures.append("DispatchUpscale has no `motion` parameter")
        if not re.search(r"d\.motionVectors\s*=\s*ffxApiGetResourceDX12\(\s*motion\s*,", up):
            failures.append("B's motionVectors are not taken from the `motion` parameter")
        uses = re.findall(r"^\s*d\.(\w+)\s*=.*\bmotion\b", up, re.M)
        if uses != ["motionVectors"]:
            failures.append(f"the motion parameter reaches more than one field: {uses}")
        if not re.search(r"d\.motionVectorScale\s*=\s*\{\s*0\.0f\s*,\s*0\.0f\s*\}", up):
            failures.append(
                "B's motionVectorScale is no longer {0, 0} - the surface's contents "
                "would reach the result and the arm would test two things")

    # --- 2. the gate: vectors only while B is out of the runtime's sight -----
    arm = body_of(br, "static bool AmdUpscaleMvArm(")
    if not arm:
        failures.append("no AmdUpscaleMvArm()")
    else:
        if 'GetEnvironmentVariableA("NS_AMD_UPSCALE_MV"' not in arm:
            failures.append("AmdUpscaleMvArm does not read NS_AMD_UPSCALE_MV")
        # Default BOUND: an unset variable must not disable the fix that shipped.
        if not re.search(r"sizeof\(v\)\)\s*==\s*0\s*\|\|\s*v\[0\]\s*!=\s*'0'", arm):
            failures.append(
                "the arm is not bound by default - an unset NS_AMD_UPSCALE_MV "
                "must leave the shipped fix ON (only =0 turns it off)")
        # THE GATE. Without it, vectors on a visible B put the runtime into the
        # staging re-create loop and the run measures the loop, not the fix.
        if "g_amd.fsr.PrivateB()" not in arm:
            failures.append(
                "the arm does not require the private copy - vectors on a B the "
                "runtime can see drive its staging re-create loop (92 "
                "re-creations for 1 engine job over 54 frames)")
        if not re.search(r"return\s+asked\s*&&\s*g_amd\.fsr\.PrivateB\(\)\s*;", flat(arm)):
            failures.append(
                "the gate is not the arm's RESULT - a variable checked anywhere "
                "else can be bypassed by the call site")
    assigns = [m.start() for m in re.finditer(r"g_amd\.up_motion\s*=(?!=)", br)]
    if len(assigns) != 1:
        failures.append(f"g_amd.up_motion is assigned {len(assigns)} times, expected once "
                        "(the release helper drops it through a reference)")
    else:
        stmt_start = br.rfind(";", 0, assigns[0])
        guard = br[stmt_start:assigns[0]]
        if not re.search(r"if\s*\(.*AmdUpscaleMvArm\(\)", guard, re.S):
            failures.append(
                "g_amd.up_motion is created without the AmdUpscaleMvArm() guard - "
                "and that guard is what carries the private-copy gate")

    # --- 3. B's own surface, passed as the motion argument -----------------
    call = re.search(r"g_amd\.fsr\.DispatchUpscale\((.*?)\)\)", br, re.S)
    if not call:
        failures.append("the DispatchUpscale call could not be read")
    else:
        args = [a.strip() for a in call.group(1).split(",")]
        if len(args) < 6 or args[4] != "g_amd.up_motion":
            failures.append(
                f"the fifth argument (motion) is {args[4] if len(args) > 4 else '?'}, "
                "not g_amd.up_motion")
        if "g_amd.motion" in args:
            failures.append("B is handed A's motion surface")

    # --- 4. the arm is named, once ------------------------------------------
    if "the upscale dispatch's motion vectors: %s" not in br:
        failures.append("the log never names which motion-vector arm ran")
    if not re.search(r"if \(!g_amd\.up_motion_logged\)\s*\{\s*g_amd\.up_motion_logged = true;", br):
        failures.append("the arm is not printed once per run")

    # --- 5. released with the other surfaces --------------------------------
    rel = body_of(br, "static void AmdReleaseResources(")
    if "drop(g_amd.up_motion)" not in rel:
        failures.append("AmdReleaseResources does not release g_amd.up_motion")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: B's motion vectors are one nullable variable, created only under "
          "NS_AMD_UPSCALE_MV=1, on a surface of B's own, named once in the log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
