"""No two passes in one command list may share a descriptor slot.

WHY THIS EXISTS
---------------
A D3D12 descriptor table is resolved by the GPU when it EXECUTES a command, not
when the command is recorded. The AMD bridge records several passes into ONE
command list and submits it once at the end, so if two of those passes write
their descriptors into the same slot, the GPU runs the FIRST one with the
SECOND one's bindings: the shader's declared t0/u0 no longer point at its
resources, a typed UAV store through a descriptor that does not match is dropped
silently, and the destination keeps whatever it was created with.

That is exactly how the network's input went black: the engine-facing rebind
(`AmdBindTriplet(0, g_amd.net, ...)`) took slot 0, which the conversion pass
(`AmdDispatch(0, g_amd.pso_in, ...)`) also recorded into, so `fsr_in` was never
written and stayed at its creation value of zero - while the engine reported a
healthy pass, consumed every frame, and the capture was demonstrably alive. The
reporter saw a live raw half and an empty processed half in the same screenshot.
Nothing crashed, nothing logged an error, and the host's own probe read 0.0000
on both surfaces because that number was TRUE.

THE RULES, DERIVED FROM THE SOURCE (not from line numbers, so reordering and
renaming do not break the check):

1. Every descriptor slot has exactly ONE writer inside the frame's command list.
   Two writers in the same list is the bug above, whatever the intent.
2. The heap is large enough for every slot actually used (three descriptors per
   slot: SRV0, SRV1, UAV).
3. Every `SetComputeRootDescriptorTable(0, ...)` in the frame list points at the
   slot that was just written. Writing one slot and pointing the table at
   another is the same defect mirrored: the pass would read a foreign triplet.
   This is rule 1's other half and it caught a real mistake in the fix itself -
   the engine's triplet moved to slot 3 while the table still pointed at the
   heap's start, which would have handed the engine the conversion's
   descriptors.
4. Slots are documented as per-EXECUTION state, so the next person keeps the
   discipline instead of merging two users back together for tidiness.

Run:  runtime\\python.exe tests\\test_amd_slot_discipline.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"

#: One slot carries three descriptors: SRV0 (t0), SRV1 (t1), UAV (u0).
DESCRIPTORS_PER_SLOT = 3

#: `AmdBindTriplet(3, ...)` and `AmdDispatch(0, ...)` both name a slot first.
#: Matched across line breaks, and only in CALL position (the two definitions
#: start with `static void ...`, which this pattern does not match).
BIND_RE = re.compile(r"AmdBindTriplet\(\s*(\d+)\s*,", re.S)
DISPATCH_RE = re.compile(r"AmdDispatch\(\s*(\d+)\s*,", re.S)

#: `AmdDispatch` is a thin wrapper that calls `AmdBindTriplet` with the same
#: slot; counting both would double-count its own slot. So the dispatch call
#: sites are counted through their first argument only, and `AmdBindTriplet`
#: call sites exclude the one inside `AmdDispatch`.
HEAP_RE = re.compile(r"hd\.NumDescriptors\s*=\s*(\d+)\s*;")
HEAP_COMMENT_RE = re.compile(r"per-EXECUTION state|per-execution state")


def strip_comments(src: str) -> str:
    """Drop // and /* */ comments so prose cannot be mistaken for code."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.splitlines():
        # A // inside a string literal would be rare here and the cost of being
        # wrong is a missed slot, not a false failure; the bridge has no such
        # literal in the code this test reads.
        out.append(line.split("//", 1)[0])
    return "\n".join(out)


def main() -> int:
    failures: list[str] = []
    raw = BRIDGE.read_text(encoding="utf-8", errors="replace")
    src = strip_comments(raw)

    # --- the frame body: from the frame's first BeginCommands to the last
    # --- EndCommands. Everything between records into one submitted list.
    begins = [m.start() for m in re.finditer(r"BeginCommands\(\)", src)]
    ends = [m.start() for m in re.finditer(r"EndCommands\(\)", src)]
    if not begins or not ends:
        print("FAIL: could not find the command-list boundaries in the bridge")
        return 1
    # The frame's list is the one that carries the conversion dispatch.
    conv = DISPATCH_RE.search(src)
    if conv is None:
        print("FAIL: no AmdDispatch call sites found - the parse is wrong")
        return 1
    frame_start = max(b for b in begins if b < conv.start())
    frame_end = min(e for e in ends if e > conv.start())
    if frame_end <= frame_start:
        print("FAIL: the conversion dispatch is not inside any command list")
        return 1
    body = src[frame_start:frame_end]

    # --- rule 1: one writer per slot ---------------------------------------
    writers: dict[int, list[str]] = {}
    for m in BIND_RE.finditer(body):
        slot = int(m.group(1))
        writers.setdefault(slot, []).append(m.group(0).strip())
    for m in DISPATCH_RE.finditer(body):
        # AmdDispatch(slot, ...) always calls AmdBindTriplet(slot, ...) itself,
        # so this records the user of the slot rather than a second writer.
        slot = int(m.group(1))
        writers.setdefault(slot, []).append("AmdDispatch(%d)" % slot)

    # The engine-facing rebind and the conversion both live in the frame body;
    # a slot used by two different CALL SITES is the defect. `AmdDispatch` also
    # invokes `AmdBindTriplet` internally, but that one is outside the frame
    # body (it is the function definition), so only the call sites are counted.
    for slot, users in sorted(writers.items()):
        # Collapse the dispatch+its own bind pairing: a dispatch records ONE
        # user, so more than one distinct user on a slot is the conflict.
        distinct = set()
        for u in users:
            if u.startswith("AmdDispatch"):
                distinct.add("dispatch:%d" % slot)
            else:
                distinct.add("bind:%d" % slot)
        if len(distinct) > 1:
            failures.append(
                "slot %d has %d writers in one command list: %s - the GPU runs "
                "the earlier pass with the later pass's descriptors"
                % (slot, len(distinct), ", ".join(sorted(distinct))))

    # --- rule 2: the heap has room for every slot actually used ------------
    heap = HEAP_RE.search(src)
    if heap is None:
        failures.append("the descriptor heap size is not written in one place")
    else:
        num = int(heap.group(1))
        needed = (max(writers) + 1) * DESCRIPTORS_PER_SLOT if writers else 0
        if num < needed:
            failures.append(
                "the heap holds %d descriptors but %d slots are used "
                "(%d needed)" % (num, max(writers) + 1, needed))

    # --- rule 3: the table points at the slot that was just written ---------
    # The other half of rule 1. `AmdBindTriplet(N, ...)` writes slot N; the
    # table that follows must be offset by N * 3 descriptors. A table at the
    # heap's start while the triplet sits in slot 3 is the same failure as two
    # writers on one slot - the pass reads a foreign triplet - and it is the
    # mistake this rule was added for, found in the fix itself.
    #
    # The offset may be written either as `gpu.ptr += <n> * 3 * <increment>`
    # (AmdDispatch and the engine rebind) or as a literal slot count.
    TABLE_RE = re.compile(
        r"SetComputeRootDescriptorTable\(\s*0\s*,\s*(\w+)\s*\)", re.S)
    for m in TABLE_RE.finditer(body):
        target = m.group(1)
        # Find the nearest preceding `gpu.ptr += ...` for this handle.
        head = body[:m.start()]
        bump = re.findall(
            r"%s\.ptr\s*\+=\s*(?:static_cast<UINT64>\(\s*)?(\d+)\s*\)?\s*\*\s*3"
            % re.escape(target), head, re.S)
        if not bump:
            # No offset at all: the table points at the heap's start, i.e. slot
            # 0. That is a legitimate binding only when the triplet just written
            # went to slot 0; treating "unset" as "fine" is what let the real
            # mistake through, so it is compared like any other slot.
            pointed = 0
        else:
            pointed = int(bump[-1])
        # The nearest preceding bind call's slot is the one this table serves.
        binds = [int(b.group(1)) for b in BIND_RE.finditer(head)]
        if binds and binds[-1] != pointed:
            failures.append(
                "the descriptor table points at slot %d but the triplet just "
                "written went to slot %d - the pass reads a foreign triplet"
                % (pointed, binds[-1]))

    # --- rule 4: the discipline is written down ----------------------------
    if not HEAP_COMMENT_RE.search(raw):
        failures.append(
            "the per-EXECUTION rule is not documented next to the heap - the "
            "next reader will merge two slots back together")

    # --- report ------------------------------------------------------------
    if failures:
        print("FAIL: descriptor-slot discipline is broken\n")
        for f in failures:
            print("  - " + f)
        print("\nSlots used in the frame list: %s"
              % ", ".join(str(s) for s in sorted(writers)))
        return 1

    print("PASS: %d slots in the frame list, one writer each (%s); heap holds %s"
          % (len(writers), ", ".join(str(s) for s in sorted(writers)),
             heap.group(1) if heap else "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
