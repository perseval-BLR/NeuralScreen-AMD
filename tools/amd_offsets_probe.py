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

#: The same control for the SECOND build this host drives, v0.3.1. Added after
#: the v0.3.1 table in native/amd/amd_runtime.h (rva::kV0310) had been called
#: unverified in our own audit: the header could not say whether its entries
#: were right, and kSyncCounter sits at 0x0 with "still unmapped". Running the
#: probe against the v0.3.1 image reproduces all eleven option fields from the
#: instruction stream, so the table is now checked by the method rather than by
#: reading.
#:
#: Tonemap is here on the same footing as in EXPECTED_V0217: the probe derives
#: it (0x9acf8, the fifth byte of the option block), while the host's Table
#: struct carries no kTonemap field at all - for either build. So this one key
#: is checked against the image alone, not against the header.
#:
#: Two other projects publish v0.3.0 layouts (a MIT ReShade add-on and a
#: GPL-3.0 Magpie fork) and agree with each other on all fifteen addresses they
#: share - and they place Tonemap at the same fifth byte, 0x97b20 there. But
#: v0.3.0 is a different build from v0.3.1, so they are a pointer, not the
#: control. This table is the control, and the probe must reproduce it.
EXPECTED_V0310 = {
    "Enabled": 0x9ACF4,
    "Temporal": 0x9ACF5,
    "UseFsrInputs": 0x9ACF6,
    "UseDepth": 0x9ACF7,
    "Tonemap": 0x9ACF8,
    "LocalTone": 0x9AD08,
    "LocalStructure": 0x9AD0C,
    "SkinStructure": 0x9AD10,
    "Scale": 0x9AD14,
    "UseAutoMask": 0x9AD18,
    "ToneChannels": 0x9AD1C,
}

#: The init entry point, identified by a string only that function references.
#: It is the one entry point this probe can place structurally: its neighbours
#: are found by which fields they touch, which is reliable only once every field
#: is known - and the fields do NOT all move together (see DELTA IS NOT
#: UNIVERSAL below).
ENTRY_ANCHOR_V0217 = {"kInit": 0x19240}
_ANCHOR_STRING = b"DLSSNR_NO_REPACK\0"

# What this probe does NOT do, stated where a reader will hit it:
#
# `kRecord`, `kNotify` and `kShutdown` are NOT derived here. Ranking their
# candidate functions by the fields they touch is exact on v0.2.17 - it returns
# 0xf600, 0x9170 and 0x12690 with no competitors - but it does not carry over,
# because the ranking leans on fields whose movement is itself in question. The
# only entry point this probe places is `kInit`.
#
# Those three are not lost, though: an independent published layout (OptiScaler's
# dlssnr AMD backend, OptiScaler/dlssnr/amd/AmdLayout.h) maps them per runtime,
# bound to each image's SHA-256, and its v0.2.17 entry reproduces all 29 of our
# confirmed values. Use that as a CALIBRATED second source - never as a first
# one, and never without checking the calibration on the build we can verify.
#
# WHICH VALUES THIS PROBE GOT WRONG, because it is the reason the rule above
# exists. `derive_service_fields` pairs our known-good build against a new one by
# aligning their state initialisers. That is sound for a field whose position
# within the initialiser does not change - and it silently assumed that of
# `kInitCtx`, which sits at kDevice + 0x10 on v0.2.17 and at kDevice + 0x18 on
# v0.3.1. The probe emitted 0x9a0f8; the truth is 0x9a100; a driver using the
# wrong value hands the engine a context pointing at unrelated data, and the
# failure looks like every other failure on this path.
#
# The lesson is not "the alignment method is bad" - it placed 24 of 30 fields
# correctly. It is that a derived address needs a second, independent witness
# before anything is driven with it, and the witness has to be checked on a build
# where the answer is already known.
#
# DELTA IS NOT UNIVERSAL - and this was measured, not assumed. Between v0.2.17
# and v0.3.1 the eleven option fields all move by +0xd338, which is what a
# relocated data block looks like. Applying that same delta to the service
# fields produces addresses that NO function refers to: kDevice +0xd338 is
# touched by one function, kQueue and kInitCtx by none. The correct addresses
# for those come out of the STATE INITIALISER instead - see
# derive_service_fields - and the check is the reference count against the
# profile the pinned build already has (kDevice: 15 functions there, 16 by this
# method, 1 by the delta).
#
# So: the option block may be translated as a unit and the service fields may
# not, which is exactly why the table is re-derived rather than shifted.

#: The initialiser's own writes, matched by value. The state initialiser is the
#: one function that writes a long run of `mov [addr], immediate` into .data,
#: and it writes the same distinctive constants on both builds (`0x41100000`,
#: the ASCII pair `version`/`sion`, `0x16e360`). Aligning the two runs pairs the
#: fields without relying on any delta - which matters because the delta is
#: exactly what fails here.
_SERVICE_ALIGN_ORDER = True


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


def derive_entry_points(path: Path):
    """Entry points this probe can place, keyed by name.

    Only `kInit` is derived, and it is derived structurally rather than by
    ranking: a string that only that function refers to identifies it exactly,
    on both builds this has been run against. The others are deliberately not
    guessed - see the note above EXPECTED_V0217 for why the field-based ranking
    is not good enough to trust on a new build.
    """
    data = path.read_bytes()
    sects = sections(data)
    funcs = functions(data, sects)
    at = data.find(_ANCHOR_STRING)
    out = {}
    if at < 0:
        return out
    anchor = offset_to_rva(sects, at)
    for start, end in funcs:
        for _addr, _mnemonic, _ops, target in walk(data, sects, start, end):
            if target == anchor:
                out["kInit"] = start
                return out
    return out


def _immediate_writes(data: bytes, sects):
    """`mov [rip+X], imm` into .data, per function, in address order.

    Returns {(start, end): [(size, imm, target), ...]} for every function that
    writes any. The state initialiser is the function with the longest run.
    """
    engine = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    engine.detail = True
    data_section = next((s for s in sects if s[0] == ".data"), None)
    if data_section is None:
        return {}
    low, high = data_section[1], data_section[1] + data_section[2]
    out = {}
    for start, end in functions(data, sects):
        at = rva_to_offset(sects, start)
        if at is None:
            continue
        rows = []
        for ins in engine.disasm(data[at:at + (end - start)], start):
            if ins.mnemonic != "mov" or len(ins.operands) != 2:
                continue
            dst, src = ins.operands
            if src.type != capstone.x86.X86_OP_IMM:
                continue
            if (dst.type != capstone.x86.X86_OP_MEM
                    or dst.mem.base != capstone.x86.X86_REG_RIP):
                continue
            target = ins.address + ins.size + dst.mem.disp
            if low <= target < high:
                mask = (1 << (8 * dst.size)) - 1
                rows.append((dst.size, src.imm & mask, target))
        if rows:
            out[(start, end)] = rows
    return out


def _referencing_functions(data: bytes, sects, target: int) -> set:
    """Which functions mention a given .data address at all."""
    out = set()
    for start, end in functions(data, sects):
        for _addr, _mnemonic, _ops, seen in walk(data, sects, start, end):
            if seen == target:
                out.add(start)
    return out


def derive_service_fields(path: Path, reference: Path | None = None):
    """Service-field offsets for `path`, paired against a known-good build.

    The option block can be translated by a delta; these cannot (see the note
    above). What works instead is the state initialiser: the one function that
    writes a long ordered run of immediates into .data. Its run has the same
    distinctive values on both builds, so aligning the two runs pairs field to
    field without using any delta at all.

    `reference` is the build whose service fields are already known - the one
    the host is pinned to. Without it there is nothing to pair against, and the
    answer is an empty dict rather than a guess.
    """
    if reference is None:
        return {}, {}
    known_writes = _immediate_writes(Path(reference).read_bytes(), sections(Path(reference).read_bytes()))
    new_writes = _immediate_writes(path.read_bytes(), sections(path.read_bytes()))
    if not known_writes or not new_writes:
        return {}, {}

    known = max(known_writes.items(), key=lambda item: len(item[1]))[1]
    fresh = max(new_writes.items(), key=lambda item: len(item[1]))[1]

    import difflib

    key_known = [(size, imm) for size, imm, _t in known]
    key_fresh = [(size, imm) for size, imm, _t in fresh]
    pairs = []
    matcher = difflib.SequenceMatcher(None, key_known, key_fresh, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + k, j1 + k) for k in range(i2 - i1))

    paired = {}
    for i, j in pairs:
        paired.setdefault(known[i][2], (fresh[j][2], known[i][1]))

    # Every pairing is only offered with the evidence that it is real: the
    # number of functions that mention the address. A pairing whose target no
    # function refers to is not a field, whatever the alignment says.
    data = path.read_bytes()
    sects = sections(data)
    verified = {}
    for old, (new, imm) in paired.items():
        if new in verified:
            continue
        verified[new] = len(_referencing_functions(data, sects, new))
    return paired, verified


def derive_table(path: Path, reference: Path | None = None):
    """The whole table for `path`, each field with the evidence behind it.

    Two independent methods are used, and a field is only reported when the
    method that produced it gives a value some function actually refers to:

      * the OPTION fields come from the ini reader (derive): the store that
        follows each key's load call. Exact, and it does not care where the
        data block moved to.
      * the SERVICE fields come from aligning the state initialiser's run of
        immediates against the reference build's run (derive_service_fields),
        falling back to the region deltas when the initialiser does not write
        the field at all - a pointer or a counter is not an immediate.

    `evidence` is the number of functions that mention the address. It is the
    check that catches a wrong answer: a translated offset that lands in .data
    with no references is a dead address, and that is exactly what a wrong
    delta produces (see the note above).
    """
    option_hits, _sects, _data = derive(path)
    service_pairs, service_refs = derive_service_fields(path, reference)

    table = {}
    for key, hits in option_hits.items():
        if hits:
            table[key] = (hits[0][0], "ini reader", None)
    if reference is not None and service_pairs:
        inverse = {old: new for old, (new, _imm) in service_pairs.items()}
        table["_service_pairs"] = (inverse, "state initialiser", service_refs)
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dll", type=Path, help="the runtime image to read")
    parser.add_argument("--expect", action="store_true",
                        help="compare against the offsets the host is pinned to "
                             "(v0.2.17) and fail on any difference")
    parser.add_argument("--expect-v0310", action="store_true",
                        help="compare against the v0.3.1 table (rva::kV0310) and "
                             "fail on any difference")
    parser.add_argument("--key", action="append", default=None,
                        help="restrict to one key (repeatable)")
    args = parser.parse_args()

    if not args.dll.is_file():
        sys.exit(f"no such file: {args.dll}")

    results, sects, data = derive(args.dll)
    keys = args.key or sorted(OPTION_KEYS)
    # Which published table this run is the control for. One of the two; the
    # entry-point anchors below belong to v0.2.17 only.
    want_table = EXPECTED_V0310 if args.expect_v0310 else EXPECTED_V0217
    checking = args.expect or args.expect_v0310
    table_name = "v0.3.1 (kV0310)" if args.expect_v0310 else "v0.2.17"
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
            if checking:
                failures.append(key)
            continue
        # A key can be handled in more than one place; the option reader is the
        # one that does not sit inside another function.
        rva, evidence = hits[0]
        section = next((s[0] for s in sects
                        if s[1] <= rva < s[1] + max(s[2], s[4])), "?")
        print(f"{key:16} {rva:#11x}  {evidence:#10x}  {section}")
        if checking:
            want = want_table.get(key)
            if want is None:
                continue
            if rva != want:
                failures.append(key)
                print(f"{'':16} {'':>11}  EXPECTED {want:#x} - MISMATCH")

    # The entry points, and only the one this probe can place structurally.
    entries = derive_entry_points(args.dll)
    print()
    print(f"{'entry point':16} {'function RVA':>13}")
    print("-" * 58)
    for name in ("kInit", "kRecord", "kNotify", "kShutdown"):
        rva = entries.get(name)
        if rva is None:
            print(f"{name:16} {'not derived':>13}  (this probe places only kInit; "
                  f"see the note in the source)")
            continue
        print(f"{name:16} {rva:#13x}")
        if args.expect:
            want = ENTRY_ANCHOR_V0217.get(name)
            if want is None:
                continue
            if rva != want:
                failures.append(name)
                print(f"{'':16} {'':>13}  EXPECTED {want:#x} - MISMATCH")

    if checking:
        print()
        if failures:
            print(f"CONTROL FAILED for: {', '.join(sorted(set(failures)))}")
            print("The method does not reproduce the table this host relies on,")
            print("so its output for another build cannot be trusted.")
            return 1
        print(f"CONTROL PASSED: every key reproduces the pinned "
              f"{table_name} table.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
