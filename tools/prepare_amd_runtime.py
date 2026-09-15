"""Prepare the AMD neural runtime for NeuralScreen.

The AMD neural pass drives a third-party runtime (DLSS-NR-on-AMD). That
runtime cannot be shipped with NeuralScreen - its licence forbids
redistribution, and its weights are derived from NVIDIA's own - so the user
brings their own copy. This script turns the copy they have into the shape the
worker expects.

WHAT IT EXPECTS TO FIND
  A folder with the output of danielblnc's official installer
  (dlssnr_on_amd_setup.exe, run once in that folder):

      version.dll                    the runtime itself
      dlssnr_on_amd.ini              settings (created empty by the installer)
      dlssnr_on_amd_weights.bin      the network weights

  The installer builds the weights from a copy of nvngx_dlssnr.dll (build
  310.8.0.0) that must be in the same folder. NeuralScreen already ships that
  file as part of its own NVIDIA path (native/nvngx_dlssnr.dll), so the
  default folder is the program's own native\\ directory:

      1. copy dlssnr_on_amd_setup.exe into  <NeuralScreen>\\native\\
      2. run it there and answer y to "Use this folder?"
      3. run this script

WHAT IT DOES
  * verifies the runtime is the build the driver knows (v0.2.14, 7,156,224
    bytes, sha256 1062237...);
  * applies the five in-place patches every external host applies to it -
    without them the runtime installs its own hooks and fights the host for
    the frame ("the two cannot both hold the wheel"). The patches are
    documented in the community's own runtime-patches.json; this script
    applies the same bytes to YOUR copy, on YOUR machine, for your own use;
  * writes the result as dlssnr_amd_pass1.dll next to the worker.

  Pass --keep-stock to skip the patching step. The driver accepts the
  unpatched build too, with a warning - it is useful for a report but is
  expected to glitch.

Run from the NeuralScreen folder:   python tools\\prepare_amd_runtime.py
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

# The build the driver's offset table belongs to (v0.2.14, extracted payload).
STOCK_SIZE = 7_156_224
STOCK_SHA256 = "106223723fd9266c44d38dc2fb77933948ab37803f46bfcea2bae3a0a474ac84"
PATCHED_SHA256 = "3c9ca13f0f5fc36a690ba424c457003bcfcc1080b4b785974cdd7e9ae2bc1dd8"

# The five patches, exactly as the ecosystem's runtime-patches.json carries
# them - file offsets, with the expected bytes asserted before anything is
# written so a different build cannot be silently corrupted.
#
#   0x1ffc  disable the runtime's own hook-installer thread (it would install
#           detours on ExecuteCommandLists and Present - the host does that)
#   0x3a53  a notify call after the hook above; without the hook it would
#           execute the frame twice
#   0x62bd4 timeout fallback: keep the current input instead of showing a
#           stale residual
#   0x6321d the matching log message
#   0x625ac bound the GPU wait loop
PATCHES = [
    (0x1FFC, "ff15d6910600", "31c090909090"),
    (0x3A53, "ff15e7280700", "909090909090"),
    (0x62BD4,
     "69662028666c6167732e4c6f61642831322920213d2030292064203d20707265765b69642e78795d2e7267623b",
     "69662028666c6167732e4c6f61642831322920213d2030292072657475726e3b20202020202020202020202020"),
    (0x6321D,
     "70726576696f757320726573696475616c2073686f776e",
     "63757272656e7420696e707574206b6570742020202020"),
    (0x625AC,
     "0a676c6f62616c6c79636f686572656e74205257427974654164647265737342756666657220666c616773203a207265676973746572287530293b0a42797465416464726573734275666665722061626f727462203a207265676973746572287430293b2020202f2f2075706c6f61642d6865617020776f7264207772697474656e2062792074686520686f7374207761746368646f673a2067697665207570206f6e206672616d6573203c3d20746869732076616c756520287265616c2d74696d65206275646765742c20696e646570656e64656e74206f6620746865207370696e2072617465290a636275666665722043203a20726567697374657228623029207b2075696e74206d6f64653b2075696e742076616c75653b2075696e74206d6178497465723b2075696e74207061643b207d0a5b6e756d7468726561647328312c20312c2031295d20766f6964206d61696e2829207b0a20202020696620286d6f6465203d3d203029207b20666c6167732e53746f726528302c2076616c7565293b2072657475726e3b207d0a2020202075696e742076203d20303b2075696e742069203d20303b0a20202020666f7220283b2069203c206d6178497465723b202b2b6929207b20666c6167732e496e7465726c6f636b656441646428342c20302c2076293b206966202876203e3d2076616c756529207b20666c6167732e53746f72652831362c2069293b20666c6167732e53746f72652831322c2030293b2072657475726e3b207d2069662028286920262032353529203d3d203235352026262061626f7274622e4c6f6164283029203e3d2076616c75652920627265616b3b207d0a20202020666c6167732e53746f72652831362c2069293b20666c6167732e53746f72652831322c2031293b20666c6167732e496e7465726c6f636b656441646428382c20312c2076293b2020202020202020202020202020202020202020202f2f2074696d6564206f75743a20636f756e7420697420616e642074656c6c20746865206170706c79207061737320746f207265757365207468652070726576696f7573206672616d65277320726573696475616c0a7d",
     "0a676c6f62616c6c79636f686572656e74205257427974654164647265737342756666657220666c616773203a207265676973746572287530293b0a42797465416464726573734275666665722061626f727462203a207265676973746572287430293b2020200a636275666665722043203a20726567697374657228623029207b2075696e74206d6f64653b2075696e742076616c75653b2075696e74206d6178497465723b2075696e74207061643b207d0a5b6e756d7468726561647328312c20312c2031295d20766f6964206d61696e2829207b0a20202020696620286d6f6465203d3d203029207b20666c6167732e53746f726528302c2076616c7565293b2072657475726e3b207d0a2020202075696e742076203d20303b2075696e742069203d20303b0a20202020666f7220283b2069203c206d696e286d6178497465722c203230393731353275293b202b2b6929207b20666c6167732e496e7465726c6f636b656441646428342c20302c2076293b206966202876203e3d2076616c756529207b20666c6167732e53746f72652831362c2069293b20666c6167732e53746f72652831322c2030293b2072657475726e3b207d2069662028286920262032353529203d3d203235352026262061626f7274622e4c6f6164283029203e3d2076616c75652920627265616b3b207d0a20202020666c6167732e53746f72652831362c2069293b20666c6167732e53746f72652831322c2031293b20666c6167732e496e7465726c6f636b656441646428382c20312c2076293b2020202020202020202020202020202020202020200a7d2020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020"),
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pe_payload_offset(data: bytes) -> int:
    """Where the appended payload starts: the end of the PE's own image.

    The installer is a PE with the runtime appended raw. The image ends at
    the highest (raw pointer + raw size) across its own section table, and
    the payload begins right there. The same rule the community's
    extract_runtime.py uses.
    """
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    sect = coff + 20 + opt_size
    end = 0
    for i in range(num_sections):
        off = sect + i * 40
        raw_size, raw_ptr = struct.unpack_from("<II", data, off + 16)
        end = max(end, raw_ptr + raw_size)
    return end


def apply_patches(data: bytearray) -> None:
    for off, before_hex, after_hex in PATCHES:
        before = bytes.fromhex(before_hex)
        got = bytes(data[off:off + len(before)])
        if got != before:
            raise ValueError(
                f"patch at 0x{off:X}: expected {before_hex[:24]}..., "
                f"found {got.hex()[:24]}... - this is not the build the "
                "patches were written for")
        data[off:off + len(before)] = bytes.fromhex(after_hex)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?",
                    help="folder with the installer's output "
                         "(default: the program's native folder)")
    ap.add_argument("--keep-stock", action="store_true",
                    help="do not patch; keep the runtime's own hooks "
                         "(useful for a report, expected to glitch)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    folder = Path(args.folder).resolve() if args.folder else (root / "native")
    if not folder.is_dir():
        print(f"no such folder: {folder}", file=sys.stderr)
        return 2

    src = folder / "version.dll"
    dst = folder / "dlssnr_amd_pass1.dll"
    if not src.is_file():
        # Tolerate a second run: the patched file is already in place.
        if dst.is_file():
            src = dst
        else:
            print(f"not found: {src}\n"
                  "Run the official installer (dlssnr_on_amd_setup.exe) in "
                  "this folder first - see the docstring.", file=sys.stderr)
            return 2

    # A file the worker already accepted is left alone.
    if src == dst:
        have = sha256_of(src)
        if have in (STOCK_SHA256, PATCHED_SHA256):
            kind = "already patched" if have == PATCHED_SHA256 else "stock"
            print(f"{dst.name}: {kind}, nothing to do")
            if have == STOCK_SHA256 and not args.keep_stock:
                print("tip: pass nothing to patch it, or --keep-stock to keep it as is")
            return 0

    data = bytearray(src.read_bytes())

    # The installer writes the runtime as version.dll. If the file is bigger
    # than the payload, the payload is extracted from the end of the PE
    # (some distributions ship it appended rather than as a loose DLL).
    if len(data) != STOCK_SIZE:
        try:
            off = pe_payload_offset(data)
            payload = data[off:off + STOCK_SIZE]
            if len(payload) != STOCK_SIZE:
                print(f"the payload extracted from {src.name} is "
                      f"{len(payload)} bytes, expected {STOCK_SIZE}",
                      file=sys.stderr)
                return 3
            data = bytearray(payload)
            print(f"extracted a {STOCK_SIZE}-byte payload from {src.name}")
        except ValueError as exc:
            print(f"cannot use {src.name}: {exc}", file=sys.stderr)
            return 3

    have = hashlib.sha256(data).hexdigest()
    if have == STOCK_SHA256:
        pass
    elif have == PATCHED_SHA256:
        print("the file is already the patched build")
        if src != dst:
            dst.write_bytes(bytes(data))
            print(f"written: {dst}")
        return 0
    else:
        print(f"this runtime is not the build the driver knows.\n"
              f"  found:    {have}\n"
              f"  expected: {STOCK_SHA256} (v0.2.14, from the official installer)\n"
              "Download that version's installer from the project's releases "
              "page and run it in this folder.", file=sys.stderr)
        return 4

    if not args.keep_stock:
        try:
            apply_patches(data)
        except ValueError as exc:
            print(f"patching failed: {exc}", file=sys.stderr)
            return 5
        after = hashlib.sha256(data).hexdigest()
        if after != PATCHED_SHA256:
            print(f"patching produced an unexpected result ({after})", file=sys.stderr)
            return 5
        print("applied the five patches (hash verified)")
    else:
        print("keeping the stock build - the runtime will install its own hooks")

    dst.write_bytes(bytes(data))
    print(f"written: {dst}")
    print("\nNext: in NeuralScreen open the settings page and set "
          "'Neural pass and motion' to 'AMD (Radeon RDNA3+)', then restart "
          "the pipeline. The worker's log ([amd] lines) says what happened.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
