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
  310.8.0.0) that must be in the same folder. This build already ships that
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
  * writes TWO files beside the worker, from the one you supplied:
      dlssnr_amd_pass1.dll          the stock build. This is what runs.
      dlssnr_amd_pass1_patched.dll  the same build with the five patches.

  STOCK is the default because that is the shape the one host that produces a
  picture runs: it never modifies the runtime and never drives it by hand - the
  engine installs its own hooks and owns the frame. The patched build disables
  that hook installer (patch 0x1ffc), so the host has to drive everything itself.
  Both are the same file with a few bytes changed, so the driver's offset table
  belongs to either, and switching between them is one variable:
  NS_AMD_PATCHED=1 selects the patched one. That is what a live A/B needs - the
  same machine and session, one setting apart.

  Pass --download to fetch the installer from the author's own release page
  instead of copying it in by hand. The file is not redistributed here: it
  travels from his releases to your machine, verified against the size and
  hash published for that release.

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

#: Where --download gets the installer. The author's own release page, so the
#: file goes from him to the user and never through this project - his licence
#: forbids redistribution and asks for a link instead. The size is pinned:
#: a release is immutable, so a download of a different size is not the file
#: this driver's patches were written against.
INSTALLER_URL = ("https://github.com/danielblnc/DLSS-NR-on-AMD/releases/download/"
                 "v0.2.14/dlssnr_on_amd_setup.exe")
INSTALLER_NAME = "dlssnr_on_amd_setup.exe"
INSTALLER_SIZE = 7_396_082

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


def download_installer(folder: Path) -> int:
    """Fetch the author's installer into `folder`. 0 on success.

    The runtime cannot be shipped here (its licence forbids redistribution and
    asks for a link to the release page instead), so this walks to that page
    and back with the file. Nothing of it passes through this project, and the
    size is checked against the pinned one: a release is immutable, so any
    other size is not the build the offsets and patches belong to.
    """
    import urllib.error
    import urllib.request

    dst = folder / INSTALLER_NAME
    if dst.is_file() and dst.stat().st_size == INSTALLER_SIZE:
        print(f"{dst.name} is already here ({INSTALLER_SIZE} bytes)")
        return 0
    print(f"downloading {INSTALLER_NAME} from the author's release page...")
    print(f"  {INSTALLER_URL}")
    tmp = dst.with_suffix(".part")
    try:
        with urllib.request.urlopen(INSTALLER_URL, timeout=120) as resp, \
                open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        print(f"download failed: {exc}\n"
              "Download it by hand from https://github.com/danielblnc/"
              "DLSS-NR-on-AMD/releases (v0.2.14) and put it in this folder.",
              file=sys.stderr)
        return 6
    got = tmp.stat().st_size
    if got != INSTALLER_SIZE:
        tmp.unlink(missing_ok=True)
        print(f"the downloaded file is {got} bytes, expected {INSTALLER_SIZE} - "
              "this is not v0.2.14. Nothing was written.", file=sys.stderr)
        return 6
    tmp.replace(dst)
    print(f"downloaded: {dst} ({got} bytes)")
    return 0


def stock_dst_is_valid(folder: Path) -> bool:
    """Whether the stock copy beside the worker is the build the driver knows."""
    p = folder / "dlssnr_amd_pass1.dll"
    return p.is_file() and sha256_of(p) == STOCK_SHA256


def patched_dst_is_valid(folder: Path) -> bool:
    """Whether the patched copy beside the worker is the build the driver knows."""
    p = folder / "dlssnr_amd_pass1_patched.dll"
    return p.is_file() and sha256_of(p) == PATCHED_SHA256


def unpatch(data: bytearray) -> None:
    """Undo the five patches, in reverse, asserting the bytes first.

    `apply_patches` is the only writer of these bytes, so the inverse is exact:
    each entry lists (offset, stock_bytes, patched_bytes) and this walks it the
    other way. It exists so an already-patched file the user hands us can still
    produce the stock half of the A/B - the two copies differ by these bytes and
    nothing else.
    """
    for off, before_hex, after_hex in PATCHES:
        after = bytes.fromhex(after_hex)
        before = bytes.fromhex(before_hex)
        if data[off:off + len(after)] != after:
            raise ValueError(
                f"offset 0x{off:x}: expected the patched bytes {after_hex[:24]}..., "
                f"found {data[off:off + len(after)].hex()[:24]}...; "
                "this file is not this build")
        data[off:off + len(after)] = before


def drop_loose_proxy(folder: Path, dst: Path) -> None:
    """Remove a `version.dll` sitting beside the worker.

    The worker statically imports VERSION.dll (file-version reads for its
    signature checks), and the runtime is distributed AS `version.dll` - that
    is the name a game imports, which is how the proxy gets into a game at all.
    Both facts point at the same hazard: a copy of the runtime lying in the
    folder the worker is loaded from can be resolved by that import instead of
    the system's own version.dll, putting a second, self-initialising engine in
    the process - entering before the host has bound its device, queue or flags,
    running its own hook installer and its own frame loop.

    Measured, not assumed: with a copy of version.dll beside a test exe,
    `GetModuleHandleW("version.dll")` reports THAT file; with no copy it
    reports C:\\Windows\\SYSTEM32\\VERSION.dll. version.dll is not a KnownDLL
    (checked in the registry: 37 entries, none of them version), so the local
    file wins - which is exactly why the runtime's own distribution name is
    dangerous HERE and not inside a game.

    Our Radeon log is the witness that a second engine is there: one worker run
    loads the runtime more than once, and the load that installs the
    D3D12/DXGI detours is NOT the one this host drives. Two engines in one
    process is the documented route to a black picture ("the two cannot both
    hold the wheel").

    The host that produces a picture sidesteps this by never importing the name:
    its build links only kernel32/user32/d3d12/dxgi and loads the runtime
    through an explicit path, and its probe carries a note that a static import
    makes the exe refuse to start when the runtime is absent. We do import the
    name, so we remove the file that can hijack it. With no local copy the
    import resolves to the system's version.dll and the only engine in the
    process is the one the host loads BY NAME (`dst`).
    """
    loose = folder / "version.dll"
    if not loose.is_file():
        return
    have = sha256_of(loose)
    if have not in (STOCK_SHA256, PATCHED_SHA256):
        # Somebody else's version.dll - a game's own copy of the runtime, or an
        # unrelated module. It is not ours to delete, and it cannot be THIS
        # build (the one the offsets belong to), so say what it is and leave it.
        print(f"\nNOTE: {loose} is present but is not this runtime "
              f"(sha256 {have[:16]}...). Left alone.\n"
              "  If the pass misbehaves with that file in place, the folder is "
              "holding a second copy of the runtime that this host does not "
              "drive - move it out by hand.", file=sys.stderr)
        return
    try:
        loose.unlink()
        print(f"removed {loose.name}: beside the worker it is resolved as the "
              "system version.dll and starts a second engine; the host loads "
              f"{dst.name} by name instead")
    except OSError as exc:
        print(f"\nWARNING: could not remove {loose} ({exc})\n"
              "  A version.dll beside the worker can be resolved as the system "
              "version.dll at process start, which starts a SECOND, "
              "self-initialising copy of the runtime. Move or delete it by hand "
              "before running the pass.", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?",
                    help="folder with the installer's output "
                         "(default: the program's native folder)")
    ap.add_argument("--download", action="store_true",
                    help="fetch the installer from the author's release page "
                         "instead of copying it in by hand")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    folder = Path(args.folder).resolve() if args.folder else (root / "native")
    if not folder.is_dir():
        print(f"no such folder: {folder}", file=sys.stderr)
        return 2

    if args.download:
        rc = download_installer(folder)
        if rc != 0:
            return rc
        print("next: run the installer in that folder and answer y to "
              '"Use this folder?", then run this script again without '
              "--download.")

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

    # A second run: `version.dll` is gone (we removed it) and dst is one of the
    # two images. Whichever one it is, make sure BOTH exist, so the A/B stays a
    # single variable rather than something the user has to re-run this script
    # for. The loose proxy is dropped on every path: a second run is exactly the
    # case where the installer has just put `version.dll` back.
    if src == dst:
        have = sha256_of(src)
        if have in (STOCK_SHA256, PATCHED_SHA256):
            data = bytearray(src.read_bytes())
            if have == PATCHED_SHA256:
                print(f"{dst.name}: the patched build")
                if not stock_dst_is_valid(folder):
                    data2 = bytearray(data)
                    unpatch(data2)
                    (folder / "dlssnr_amd_pass1.dll").write_bytes(bytes(data2))
                    print("written the STOCK copy next to it (what runs by default)")
            else:
                print(f"{dst.name}: the stock build (runs by default)")
                if not patched_dst_is_valid(folder):
                    p = bytearray(data)
                    apply_patches(p)
                    (folder / "dlssnr_amd_pass1_patched.dll").write_bytes(bytes(p))
                    print("written the PATCHED copy next to it (NS_AMD_PATCHED=1)")
            drop_loose_proxy(folder, folder / "dlssnr_amd_pass1.dll")
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
        # The user pointed us at an already-patched file. Reconstruct the stock
        # one from it so both sides of the A/B exist either way.
        print("the file given is already the patched build; the stock copy is "
              "written next to it")
        dst.write_bytes(bytes(data))
        print(f"written: {dst}")
        drop_loose_proxy(folder, dst)
        return 0
    else:
        print(f"this runtime is not the build the driver knows.\n"
              f"  found:    {have}\n"
              f"  expected: {STOCK_SHA256} (v0.2.14, from the official installer)\n"
              "Download that version's installer from the project's releases "
              "page and run it in this folder.", file=sys.stderr)
        return 4

    # BOTH images are written, and that is the point of this script now.
    #
    # They are the same build with five in-place patches, so the driver's offset
    # table belongs to either one. Having both files side by side makes the
    # choice a single environment variable (NS_AMD_PATCHED=1) instead of a
    # reinstall, which is what a live A/B needs: the same machine, the same
    # session, one setting apart.
    #
    # STOCK is the default, because that is the shape the one external host that
    # produces a picture runs: it never modifies the runtime, never writes into
    # its image, and never notifies the engine by hand - the engine installs its
    # own hooks and owns the frame from there. The patched build is the opposite
    # shape (patch 0x1ffc disables that hook installer), and it stays available
    # for the comparison.
    stock_dst = folder / "dlssnr_amd_pass1.dll"
    patched_dst = folder / "dlssnr_amd_pass1_patched.dll"
    stock_dst.write_bytes(bytes(data))
    print(f"written: {stock_dst}  <- what runs by default")

    patched = bytearray(data)
    try:
        apply_patches(patched)
    except ValueError as exc:
        print(f"patching failed: {exc}", file=sys.stderr)
        return 5
    after = hashlib.sha256(bytes(patched)).hexdigest()
    if after != PATCHED_SHA256:
        print(f"patching produced an unexpected result ({after})", file=sys.stderr)
        return 5
    patched_dst.write_bytes(bytes(patched))
    print(f"written: {patched_dst}  <- NS_AMD_PATCHED=1 runs this one")
    print("applied the five patches (hash verified)")

    drop_loose_proxy(folder, stock_dst)

    # The FidelityFX upscaler is the other half of the picture, and its absence
    # is not a crash - it is a pass that runs and processes nothing (the runtime
    # only takes frames from an FSR dispatch it can hook). It SHIPS with the
    # release, so this is normally satisfied; the check stays for the case of a
    # user who deleted it.
    upscaler = folder / "amd_fidelityfx_upscaler_dx12.dll"
    if upscaler.is_file():
        print(f"the FidelityFX upscaler is present ({upscaler.name}, "
              f"{upscaler.stat().st_size // (1024 * 1024)} MB)")
    else:
        print(f"\nWARNING: {upscaler.name} is missing from this folder.")
        print("  The neural pass needs it: the runtime takes the frame from a "
              "FidelityFX upscale dispatch and processes nothing without one.")
        print("  It ships inside this build's archive (native/AMD.md) - restore "
              "it, or take the copy from OptiScaler's FSR package.")

    weights = folder / "dlssnr_on_amd_weights.bin"
    if not weights.is_file():
        print(f"\nWARNING: {weights.name} is missing - the installer produces it "
              "from the NVIDIA nvngx_dlssnr.dll; without it the engine cannot "
              "initialise.")

    print("\nNext: in NeuralScreen AMD open the settings page and set "
          "'Neural pass' to 'AMD Radeon (RDNA3+)', then restart the pipeline. "
          "The worker's log ([amd] lines) says what happened, and the engine's "
          "own dlssnr_on_amd.log says what it did with the frames.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
