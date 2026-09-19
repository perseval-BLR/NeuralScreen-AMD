"""Derive the AMD runtime's option offsets from the binary itself.

Every address this host writes into `dlssnr_amd_pass1.dll` is a raw offset into
someone else's image, and a wrong one does not fail politely: it writes into
read-only memory or calls into the middle of an unrelated function. The table in
`native/amd/amd_runtime.h` is therefore pinned to ONE build, and moving to a new
one means re-deriving every entry.

Reading the disassembly by hand is how that was done, and it is how a stale
entry survived until a card found it. This does the reading instead.

    runtime\\python.exe tools\\amd_offsets_probe.py <dlssnr_amd_pass1.dll>

HOW IT WORKS, and why this is evidence rather than a guess.

The runtime reads its options from an ini file at startup. Its reader calls the
Windows profile API once per key and then stores the result - so in the
instruction stream a key's address, the load call and the destination field
appear in that order:

    lea rdx, [rip+A]      ; A -> the key's name, e.g. "LocalTone"
    lea rcx, [rip+B]      ; B -> the ini section
    ...
    call [rip+C]          ; GetPrivateProfileInt / ...String
    mov [rip+D], eax      ; D -> THE FIELD the key writes

That `D` is the offset, and it is derived from the shape of the code rather than
from a table copied out of another project. The key names come from the section
the build already uses, so a build that renames a key is reported as missing
rather than silently mapped onto the old offset.

WHAT IT CANNOT PROVE. A field being written from a key does not prove the engine
READS it at run time, and two builds can share a shape while differing in the
field a key lands in. So the output is a candidate table: it is checked against
the build this host is already pinned to first (--expect), and only a table that
reproduces that one is offered for a new build.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

try:
    import capstone
except ImportError:  # pragma: no cover - a clear message beats a traceback
    sys.exit("capstone is required: runtime\\python.exe -m pip install capstone")

#: The ini keys the host writes, and the section they live in. A key that is not
#: in this list is not reported, so adding one here is how a new option appears.
OPTION_KEYS = (
    "Enabled",
    "Temporal",
    "UseFsrInputs",
    "UseDepth",
    "Tonemap",
    "LocalTone",
    "LocalStructure",
    "SkinStructure",
    "Scale",
    "UseAutoMask",
    "ToneChannels",
)

#: The offsets this host already relies on, for the build it is pinned to. A
#: probe run must reproduce these before its output is trusted for another
#: build - the control that turns "the disassembly looked right" into evidence.
#: The values are the ones in native/amd/amd_runtime.h for v0.2.17.
EXPECTED_V0217 = {
    "Enabled": 0x8D9BC,
    "Temporal": 0x8D9BD,
    "UseFsrInputs": 0x8D9BE,
    "UseDepth": 0x8D9BF,
    "Tonemap": 0x8D9C0,
    "LocalTone": 0x8D9D0,
    "LocalStructure": 0x8D9D4,
    "SkinStructure": 0x8D9D8,
    "Scale": 0x8D9DC,
    "UseAutoMask": 0x8D9E0,
    "ToneChannels": 0x8D9E4,
}


def sections(data: bytes):
    """(name, virtual address, virtual size, raw pointer, raw size) per section."""
    if data[:2] != b"MZ":
        raise ValueError("not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional = struct.unpack_from("<H", data, pe + 20)[0]
    out = []
    for index in range(count):
        at = pe + 24 + optional + index * 40
        name = data[at:at + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, at + 8)
        out.append((name, vaddr, vsize, rawptr, rawsize))
    return out


def rva_to_offset(sects, rva: int):
    for _, vaddr, vsize, rawptr, rawsize in sects:
        if vaddr <= rva < vaddr + max(vsize, rawsize):
            return rawptr + (rva - vaddr)
    return None


def offset_to_rva(sects, offset: int):
    for _, vaddr, _, rawptr, rawsize in sects:
        if rawptr <= offset < rawptr + rawsize:
            return vaddr + (offset - rawptr)
    return None


def functions(data: bytes, sects):
    """(start, end) per function, from .pdata - the only honest code bounds."""
    pdata = next((s for s in sects if s[0] == ".pdata"), None)
    if pdata is None:
        return []
    at = rva_to_offset(sects, pdata[1])
    out = []
    for index in range(pdata[2] // 12):
        begin, end, _unwind = struct.unpack_from("<III", data, at + index * 12)
        if begin:
            out.append((begin, end))
    return sorted(out)


def key_rvas(data: bytes, sects):
    """Where each ini key's name sits, matched on the name the section uses."""
    found = {}
    for key in OPTION_KEYS:
        needle = key.encode("ascii") + b"\0"
        start = 0
        while True:
            at = data.find(needle, start)
            if at < 0:
                break
            start = at + 1
            rva = offset_to_rva(sects, at)
            if rva is None:
                continue
            # A key name is referenced by the reader; a string inside the weights
            # blob or a message is not. Keep the first in a read-only section and
            # let the code walk decide whether anything points at it.
            section = next((s[0] for s in sects
                            if s[1] <= rva < s[1] + max(s[2], s[4])), "")
            if section in (".rdata", ".data", ".text"):
                found.setdefault(key, []).append(rva)
    return found


def walk(data: bytes, sects, start: int, end: int):
    """Instructions of one function, with each rip-relative target resolved."""
    at = rva_to_offset(sects, start)
    if at is None:
        return []
    engine = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    engine.detail = True
    out = []
    for ins in engine.disasm(data[at:at + (end - start)], start):
        target = None
        for op in ins.operands:
            if (op.type == capstone.x86.X86_OP_MEM
                    and op.mem.base == capstone.x86.X86_REG_RIP):
                target = ins.address + ins.size + op.mem.disp
        out.append((ins.address, ins.mnemonic, ins.op_str, target))
    return out


def derive(path: Path):
    """key -> (field RVA, evidence address) for every key the reader stores."""
    data = path.read_bytes()
    sects = sections(data)
    funcs = functions(data, sects)
    all_funcs = {start for start, _ in funcs}
    names = key_rvas(data, sects)
    by_rva = {}
    for key, rvas in names.items():
        for rva in rvas:
            by_rva.setdefault(rva, key)

    results = {}
    for start, end in funcs:
        body = walk(data, sects, start, end)
        if not body:
            continue
        for index, (_addr, mnemonic, _ops, target) in enumerate(body):
            if mnemonic != "lea" or target not in by_rva:
                continue
            key = by_rva[target]
            # Walk forward to the NEXT load call only. A key's whole handling is
            # `lea <name>` -> `call <profile API>` -> store, so the search must
            # stop at the first call: continuing past it picks up the next key's
            # call and attributes its field to this key, which is how `UseDepth`
            # first came back as `Tonemap`'s offset.
            call_at = None
            for scan in range(index + 1, min(index + 10, len(body))):
                _a2, mn2, _ops2, tgt2 = body[scan]
                if mn2 == "call":
                    call_at = scan
                    break
            if call_at is None:
                continue

            # Everything up to the next call after that one belongs to this key.
            # Two shapes carry the answer: a store of the loaded value
            # (`mov [rip+X], eax`, `movss [rip+X], xmm0`), or the boolean test
            # of it (`setne [rip+X]`). The SET is checked first and wins: when a
            # key is a switch, the set is the field and a later store in the same
            # window would be a different option's.
            #
            # One further call is allowed inside the window, and only before a
            # field is found: the string keys convert before they store
            # (`call <strtod>` -> `cvtsd2ss` -> `movss [rip+X]`), so stopping at
            # the first call after the load loses exactly those four. A second
            # call *after* a field is the next key's, and ends the window.
            field = None
            extra_call = False
            for scan in range(call_at + 1, min(call_at + 24, len(body))):
                _a3, mn3, ops3, tgt3 = body[scan]
                if mn3 == "call":
                    if field is not None or extra_call:
                        break
                    extra_call = True
                    continue
                if tgt3 is None:
                    continue
                section = next((s[0] for s in sects
                                if s[1] <= tgt3 < s[1] + max(s[2], s[4])), "?")
                if section != ".data":
                    continue
                if mn3 in ("setne", "sete", "setnz", "setz"):
                    field = (tgt3, body[index][0])
                    break
                if field is None and mn3 in ("mov", "movss", "movsd") and \
                        ops3.startswith(("dword ptr [rip", "byte ptr [rip",
                                         "word ptr [rip", "qword ptr [rip")):
                    field = (tgt3, body[index][0])
            if field is not None:
                results.setdefault(key, []).append(field)
    return results, sects, data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dll", type=Path, help="the runtime image to read")
    parser.add_argument("--expect", action="store_true",
                        help="compare against the offsets the host is pinned to "
                             "(v0.2.17) and fail on any difference")
    parser.add_argument("--key", action="append", default=None,
                        help="restrict to one key (repeatable)")
    args = parser.parse_args()

    if not args.dll.is_file():
        sys.exit(f"no such file: {args.dll}")

    results, sects, data = derive(args.dll)
    keys = args.key or sorted(OPTION_KEYS)
    print(f"image: {args.dll.name}  ({len(data):,} bytes, "
          f"{len(functions(data, sects))} functions)")
    print()
    print(f"{'key':16} {'field RVA':>11}  {'evidence':>10}  section")
    print("-" * 58)
    failures = []
    for key in keys:
        hits = results.get(key, [])
        if not hits:
            print(f"{key:16} {'MISSING':>11}  {'-':>10}  the reader does not "
                  f"store it in this build")
            if args.expect:
                failures.append(key)
            continue
        # A key can be handled in more than one place; the option reader is the
        # one that does not sit inside another function.
        rva, evidence = hits[0]
        section = next((s[0] for s in sects
                        if s[1] <= rva < s[1] + max(s[2], s[4])), "?")
        print(f"{key:16} {rva:#11x}  {evidence:#10x}  {section}")
        if args.expect:
            want = EXPECTED_V0217.get(key)
            if want is None:
                continue
            if rva != want:
                failures.append(key)
                print(f"{'':16} {'':>11}  EXPECTED {want:#x} - MISMATCH")

    if args.expect:
        print()
        if failures:
            print(f"CONTROL FAILED for: {', '.join(sorted(set(failures)))}")
            print("The method does not reproduce the table this host relies on,")
            print("so its output for another build cannot be trusted.")
            return 1
        print("CONTROL PASSED: every key reproduces the pinned v0.2.17 table.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
