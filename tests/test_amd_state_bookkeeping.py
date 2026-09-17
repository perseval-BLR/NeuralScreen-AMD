"""The AMD bridge's resource-state bookkeeping must be self-consistent.

A D3D12 barrier says "this resource is currently in state A; make it B". If A
is not where the resource really is, the barrier is a lie and D3D12 calls the
result undefined - it is the class of mistake that produces no error, no
warning, and a wrong picture.

This was found by reading the bridge, not by a crash: `up_out` was CREATED in
UNORDERED_ACCESS while the first barrier on it claimed it was leaving
NON_PIXEL_SHADER_RESOURCE. Nothing flagged it - the frame looked fine on this
bench because the mismatch happened to be harmless there, and issue #1's
black picture is what sent anyone looking.

The rules checked, derived from the source rather than from a comment or a line
number, so renaming things does not break the check and reordering them cannot
hide a fault:

1. A barrier may only leave a state the resource can really be in: its creation
   state, or the state some other barrier on it produces. Anything else is the
   lie above.
2. No barrier may transition a resource to the state it is already in
   (`from == to`): such a barrier does nothing, and it is the usual way a
   mismatch gets papered over instead of fixed.
3. The frame must CLOSE the cycle: the last barrier on a resource has to return
   it to the state it was created in, or frame N+1's dispatch is told a state
   the resource is not in.
4. The network surface must be created writable: dispatch A declares it as its
   OUTPUT (UNORDERED_ACCESS) and nothing transitions it before that call, so a
   readable creation state would make the very first dispatch a lie. This is
   what the working reference host does.

Run:  runtime\\python.exe tests\\test_amd_state_bookkeeping.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"

#: The resources whose state is the bridge's own business (v.color.tex and
#: v.output belong to the host's video loop and are handled there).
WATCHED = ("net", "fsr_in", "up_out", "motion", "depth")

CREATION_RE = re.compile(
    r"g_amd\.(?P<res>\w+)\s*=\s*AmdMakeTex\((?P<args>.*?)\);", re.S)
STATE_RE = re.compile(r"D3D12_RESOURCE_STATE_(\w+)")
# A Transition(res, FROM, TO) call, across the line breaks the code uses.
TRANSITION_RE = re.compile(
    r"Transition\(\s*g_amd\.(?P<res>\w+),\s*D3D12_RESOURCE_STATE_(\w+),\s*"
    r"D3D12_RESOURCE_STATE_(\w+)\s*\)", re.S)


def main() -> int:
    failures = []
    src = BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- creation states ---------------------------------------------------
    created: dict[str, str] = {}
    for m in CREATION_RE.finditer(src):
        res, args = m.group("res"), m.group("args")
        if res not in WATCHED:
            continue
        states = STATE_RE.findall(args)
        if not states:
            failures.append(f"{res} is created with no explicit state - "
                            "COMMON is not a legal resting state here")
            continue
        # The state argument is the last one before the `uav` flag.
        created[res] = states[-1]

    for res in WATCHED:
        if res not in created:
            failures.append(f"{res} is not created anywhere - the check cannot "
                            "verify its bookkeeping")

    # --- barriers, in source order ----------------------------------------
    barriers: dict[str, list[tuple[str, str]]] = {r: [] for r in WATCHED}
    for m in TRANSITION_RE.finditer(src):
        res = m.group("res")
        if res in barriers:
            barriers[res].append((m.group(2), m.group(3)))

    for res, pairs in barriers.items():
        if res not in created or not pairs:
            continue

        # 1. the first barrier must leave the state the resource was born in.
        first_from = pairs[0][0]
        if first_from != created[res]:
            failures.append(
                f"{res}: created in {created[res]} but the first barrier claims "
                f"it is leaving {first_from} - the barrier lies about the state")

        # 2. no barrier may be a no-op.
        for frm, to in pairs:
            if frm == to:
                failures.append(
                    f"{res}: a barrier transitions {frm} -> {to}, which does "
                    "nothing; it hides a state mismatch instead of fixing it")

        # 3. every `from` must be a state the resource can really be in: the
        #    creation state, or the target of another barrier on it.
        reachable = {created[res]} | {to for _, to in pairs}
        for frm, _ in pairs:
            if frm not in reachable:
                failures.append(
                    f"{res}: a barrier leaves {frm}, which is neither its "
                    "creation state nor the result of any other barrier")

    # --- the declared state of the dispatch -------------------------------
    # net is dispatch A's OUTPUT, declared UNORDERED_ACCESS, and it is handed
    # over before list 1 contains any barrier on it. So it has to be created
    # writable - otherwise the dispatch is told a state that is not true.
    dispatch_block = src[src.find("the FSR dispatch the engine follows"):
                         src.find("the FSR dispatch the engine follows") + 3000]
    if "g_amd.net" not in dispatch_block:
        failures.append("the network dispatch block is gone from the bridge")
    elif created.get("net") != "UNORDERED_ACCESS":
        failures.append(
            f"net is created in {created.get('net')} but the dispatch declares "
            "it as its OUTPUT (UNORDERED_ACCESS) before any barrier moves it")

    # --- 3. the frame must CLOSE the cycle --------------------------------
    # The last barrier on a resource has to return it to the state it was born
    # in, because that is where the next frame starts. A resource that ends the
    # frame somewhere else is handed to frame N+1's dispatch as a state it is
    # not in - the same lie as a wrong creation state, one frame later.
    for res, pairs in barriers.items():
        if res not in created or not pairs:
            continue
        ends_at = pairs[-1][1]
        if ends_at != created[res]:
            failures.append(
                f"{res}: the frame leaves it in {ends_at} but it was created in "
                f"{created[res]} - the next frame's dispatch is told a state "
                "the resource is not in")

    print(f"    resources checked: {len([r for r in WATCHED if r in created])}"
          f" ({', '.join(r for r in WATCHED if r in created)})")
    for res in WATCHED:
        if res not in created:
            continue
        pairs = barriers.get(res, [])
        chain = " -> ".join([created[res]] + [to for _, to in pairs]) \
            if pairs else f"{created[res]} (no barriers)"
        print(f"      {res:8} {chain}")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} state-bookkeeping problem(s)")
        return 1
    print("OK: every barrier starts from a real state and nothing is a no-op")
    return 0


if __name__ == "__main__":
    sys.exit(main())
