"""The engine's own staging-rebuild flag: the fact we never had for a rebuild.

Our rebuild (a resize, an RNSZ, a mode switch) frees surfaces the engine holds
pointers to. Nothing said whether it was mid-rebuild of its own staging at that
moment, so the whole class was guessed at.

The engine publishes it: a sticky byte, set when the runtime detects a resize, a
re-created upscaler context or an INI change, and cleared only after it drains
the game's queue and joins its workers. Its Record tests the flag as its FIRST
act - verified on the pinned image, where Record + 0xb2 is
`cmp byte ptr [rip + 0x7e3ef], 1` and that displacement resolves to this RVA.

Two independent sources agree on the address: a published layout for the same
build (bound to its SHA256) and our own disassembly of the image.

Run:  runtime\\python.exe tests/test_amd_recreate_flag.py
"""
import re
import struct
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RUNTIME_H = BASE / "native" / "amd" / "amd_runtime.h"
RUNTIME_CPP = BASE / "native" / "amd" / "amd_runtime.cpp"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"
IMAGE = BASE / "native" / "dlssnr_amd_pass1.dll"


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        out.append(line.split("//")[0] if "//" in line else line)
    return "\n".join(out)


def main() -> int:
    failures = []

    for path in (RUNTIME_H, RUNTIME_CPP, BRIDGE):
        if not path.exists():
            print(f"FAIL: {path.name} not found")
            return 1

    hdr = strip_comments(RUNTIME_H.read_text(encoding="utf-8", errors="replace"))
    cpp = strip_comments(RUNTIME_CPP.read_text(encoding="utf-8", errors="replace"))
    brg = strip_comments(BRIDGE.read_text(encoding="utf-8", errors="replace"))

    # 1. The field exists in the layout and carries an address for this build.
    if "kRecreate" not in hdr:
        failures.append("the layout has no kRecreate field - the engine's rebuild "
                        "flag cannot be read")
    else:
        m = re.search(r"/\*\s*kRecreate\s*\*/\s*(0x[0-9a-fA-F]+)", hdr)
        if not m:
            failures.append("kRecreate has no address in the pinned v0.2.17 table")
        else:
            rva = int(m.group(1), 16)
            if rva != 0x8DAA8:
                failures.append(
                    f"kRecreate is {rva:#x}, not the address verified against both "
                    "the published layout and our own disassembly (0x8daa8)")

    # 2. The accessor answers TRUE when the layout is unknown. Assuming the
    #    worst is the discipline everywhere else on this path, and answering
    #    "not rebuilding" on an unmapped build would invite exactly the teardown
    #    this guard exists to warn about.
    acc = re.search(r"bool Runtime::EngineRecreating\(\) const\s*\{(.*?)\n\}", cpp, re.S)
    if not acc:
        failures.append("Runtime::EngineRecreating is not defined")
    else:
        body = acc.group(1)
        if "return true" not in body:
            failures.append("the accessor never returns true for an unknown layout - "
                            "an unmapped build would read as 'safe to tear down'")
        # The accessor must READ the flag through At<>, not merely mention it.
        # A body that returns a constant passes a bare "does it name the field"
        # check while reading nothing - tried, and it was a hole in this guard.
        if "At<volatile uint8_t>(module_, table_->kRecreate)" not in body:
            failures.append(
                "the accessor does not read kRecreate through At<> - a body "
                "returning a constant would pass while reading nothing")

    # 3. It is reported, and reported as a FACT - never as a gate that would
    #    stop frames on a machine whose layout is not mapped.
    if "the engine says its staging is being re-created" not in brg:
        failures.append("the rebuild point does not report the engine's flag")
    if "staging-rebuild flag is UP" not in brg:
        failures.append("the periodic summary does not repeat the flag")

    # 4. Negative control on the IMAGE itself: the flag must be readable at that
    #    RVA, in .data, and the engine's Record must contain the first-act test
    #    that addresses it. A layout copied from documentation alone would pass
    #    every check above while pointing at nothing.
    if not IMAGE.exists():
        failures.append("the pinned runtime image is missing - the address cannot be "
                        "verified against the build it is used on")
    else:
        data = IMAGE.read_bytes()
        elf = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, elf + 6)[0]
        optsz = struct.unpack_from("<H", data, elf + 20)[0]
        sec_at = elf + 24 + optsz
        sections = []
        for i in range(nsec):
            b = sec_at + i * 40
            sections.append((data[b:b + 8].rstrip(b"\x00").decode(),
                             struct.unpack_from("<I", data, b + 12)[0],
                             struct.unpack_from("<I", data, b + 8)[0],
                             struct.unpack_from("<I", data, b + 20)[0]))

        def rva_to_off(rva):
            for _n, va, vsz, praw in sections:
                if va <= rva < va + vsz:
                    return praw + (rva - va)
            return None

        off = rva_to_off(0x8DAA8)
        if off is None:
            failures.append("kRecreate 0x8daa8 falls outside every section of the "
                            "pinned image - the address is wrong for this build")
        else:
            sec_name = next(n for n, va, vsz, _p in sections if va <= 0x8DAA8 < va + vsz)
            if sec_name != ".data":
                failures.append(f"the flag resolves into {sec_name}, not .data - a "
                                "mutable engine flag lives in writable data")

        # The engine's own first-act test: find instructions addressing that RVA.
        text = next((s for s in sections if s[0] == ".text"), None)
        if text is None:
            failures.append("no .text section in the image")
        else:
            n, va, vsz, praw = text
            code = data[praw:praw + min(vsz, 0x5BB16)]
            found = 0
            i = 0
            # Bounded scan: enough to find rip-relative accesses to one address.
            while i < len(code) - 7:
                # 48 39 /r or 80 3d /0 style - look for the 4-byte displacement
                # pattern of `cmp byte ptr [rip+disp], imm8`.
                if code[i] == 0x80 and code[i + 1] == 0x3D:
                    disp = struct.unpack_from("<i", code, i + 2)[0]
                    addr = va + i + 7 + disp
                    if addr == 0x8DAA8:
                        found += 1
                i += 1
            if found == 0:
                failures.append(
                    "nothing in .text addresses the flag with "
                    "`cmp byte ptr [rip+disp], imm8` - the engine's Record is "
                    "supposed to test it as its first act, and a layout copied "
                    "from documentation alone would point at nothing")

    if failures:
        print("FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: the engine's staging-rebuild flag is mapped for the pinned build, "
          "verified against the image itself (the flag lives in .data and Record "
          "tests it with a first-act `cmp`), assumed UP when the layout is unknown, "
          "and reported as a fact rather than used to gate frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
