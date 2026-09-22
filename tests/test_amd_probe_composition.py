"""The probe says what a half-float surface is MADE of, not only its mean.

WHY THIS EXISTS
---------------
The flicker report rests on two readings of the upscale's output: a hard
0.0000 on an RX 9070 XT, and ~64,000 on an RX 7900 XTX. The mean cannot say
what either one is. AmdMeanFromHalf decodes NaN as 0 and infinity as 1 (so one
bad pixel cannot swallow a frame's number), which makes a surface full of NaN
read exactly 0.0000 - the same as a black one - and a mean of 64,000 cannot say
whether most pixels sit near the ceiling or a few sit at infinity. Everything
said about those two states so far was inferred from means.

WHAT THIS LOCKS
---------------
1. The classifier reads the RAW half bits: exponent all ones is NaN (mantissa
   set) or infinity (mantissa clear); exponent and mantissa both zero is an
   exact zero (+0 and -0); a finite exponent field of 25 or more is >= 1024.
2. It counts the same three colour channels the mean averages, in the same
   loop, so the two numbers describe one set of values.
3. The mean is untouched: AmdMeanFromHalf still decodes NaN as 0 and infinity
   as 1, so a new log compares with every old one.
4. Only a half-float surface fills the counts; an RGBA8 frame leaves them alone
   rather than reporting zeros for categories it cannot have.
5. The upscale's output AND `net` are both counted, and one log line prints
   both: `net` is written by A on every frame in the same submission, so it is
   the control that says which categories B made.

Run:  runtime\\python.exe tests/test_amd_probe_composition.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
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


def main() -> int:
    failures: list[str] = []
    if not BRIDGE.is_file():
        print("FAIL: amd_bridge.inl is missing")
        return 1
    br = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))
    flat = re.sub(r"\s+", " ", br)

    # --- 1. the classifier reads the raw bits -------------------------------
    cls = re.sub(r"\s+", " ", body_of(br, "static void AmdClassifyHalf("))
    if not cls:
        failures.append("no AmdClassifyHalf()")
    else:
        if "(hv >> 10) & 0x1Fu" not in cls or "hv & 0x3FFu" not in cls:
            failures.append("the classifier does not split the half into exponent and mantissa")
        if "++c.n;" not in cls:
            failures.append("the classifier does not count the channels it classifies")
        if not re.search(r"if \(expo == 0x1F\) \{ if \(mant\) \+\+c\.nan; else \+\+c\.inf; \}", cls):
            failures.append("NaN and infinity are not told apart by the mantissa under an all-ones exponent")
        if not re.search(r"else if \(expo == 0 && mant == 0\) \+\+c\.zero;", cls):
            failures.append("an exact zero is not counted from a zero exponent AND a zero mantissa")
        if not re.search(r"else if \(expo >= 25\) \+\+c\.huge;", cls):
            failures.append("the >= 1024 bucket is not exponent field >= 25")

    meas = body_of(br, "static bool AmdMeasureSurface(")
    if not meas:
        failures.append("AmdMeasureSurface could not be read")
    else:
        m = re.sub(r"\s+", " ", meas)
        # --- 2. same channels, same loop ------------------------------------
        if not re.search(r"for \(UINT c = 0; c < 3; \+\+c\) \{ sum \+= AmdMeanFromHalf\(px\[c\]\); "
                         r"AmdClassifyHalf\(px\[c\], counted\); \}", m):
            failures.append("the classifier does not run on the mean's own three colour channels")
        # --- 4. only a half surface fills the counts ------------------------
        if not re.search(r"if \(comp != nullptr && spec\.is_half\) \*comp = counted;", m):
            failures.append("the counts are not handed back only for a half-float surface")

    # --- 3. the mean is untouched --------------------------------------------
    mean = re.sub(r"\s+", " ", body_of(br, "static float AmdMeanFromHalf("))
    if "else if (expo == 0x1F) v = mant ? 0.0f : 1.0f;" not in mean:
        failures.append("AmdMeanFromHalf no longer decodes NaN as 0 and infinity as 1 - "
                        "new means would not compare with old logs")

    # --- 5. both surfaces counted, one line prints both -----------------------
    if not re.search(r"AmdMeasureSurface\( g_amd\.net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, "
                     r"kNetSpec, &netm, &net_comp\)", flat):
        failures.append("`net` (the control) is not counted")
    if not re.search(r"g_amd\.up_out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, kUpSpec, &upm, &up_comp\)", flat):
        failures.append("the upscale's output is not counted")
    line = re.search(r'Log\("\[amd\] the upscale\'s own output, what it is made of:.*?\);', flat)
    if not line:
        failures.append("no log line for the upscale output's composition")
    else:
        text = line.group(0)
        for k in ("up_comp.nan", "up_comp.inf", "up_comp.zero", "up_comp.huge",
                  "net_comp.nan", "net_comp.inf", "net_comp.zero", "net_comp.huge"):
            if k not in text:
                failures.append(f"the composition line does not print {k}")

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: the probe counts NaN, infinity, exact zero and >= 1024 from the raw "
          "bits of the upscale's output and of `net`, beside an unchanged mean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
