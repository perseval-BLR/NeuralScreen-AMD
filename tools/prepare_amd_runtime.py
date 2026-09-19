"""Rebuild the AMD neural runtime pair from a copy you installed yourself.

The AMD neural pass drives a third-party runtime (DLSS-NR-on-AMD). **This build
ships it**, so you normally have nothing to do here: `native\\` already carries
`dlssnr_amd_pass1.dll`, `dlssnr_amd_pass1_patched.dll`, the weights and the ini.

This script exists for the case where you want to produce that pair yourself -
from the author's own installer, from an older runtime you already have, or to
check that a copy is the build this driver's offset table belongs to.

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
  * verifies the runtime is a build the driver knows. Two are accepted, each by
    its own size and sha256: **v0.2.17** (7,248,384 bytes, the build the pinned
    offset table and the patches belong to) and **v0.3.1** (7,304,192 bytes,
    written unpatched). An older build - v0.2.14, 7,156,224 bytes - is refused
    by name, because every release from v0.2.15 moved the data region;
  * applies the in-place patches every external host applies to v0.2.17 -
    without them the runtime installs its own hooks and fights the host for
    the frame ("the two cannot both hold the wheel"). The patches are
    documented in the community's own runtime-patches.json; this script
    applies the same bytes to YOUR copy, on YOUR machine, for your own use.
    One patch from that list is deliberately left out - the GPU wait spin cap
    (0x625ac). It bounds the wait shader below the network's real cost, so the
    inline wait would expire on every frame; both hosts that produce a picture
    refuse it, for exactly this reason. See PATCHES below.
  * writes the runtime images beside the worker, from the one you supplied:
      dlssnr_amd_pass1.dll          the v0.2.17 stock build. This is what runs.
      dlssnr_amd_pass1_patched.dll  the same build with those patches.
    and, with --download-v0310, also
      dlssnr_amd_pass1_v0310.dll    v0.3.1, unpatched (NS_AMD_V0310=1).

  STOCK is the default because that is the shape the hosts that produce a
  picture run: they never modify the runtime and never drive it by hand - the
  engine installs its own hooks and owns the frame. The patched build disables
  that hook installer (patch 0x1ffc), so the host has to drive everything itself.
  Those two are the same file with a few bytes changed, so the driver's offset
  table belongs to either, and switching between them is one variable:
  NS_AMD_PATCHED=1 selects the patched one. That is what a live A/B needs - the
  same machine and session, one setting apart.

  It also DELETES `version.dll` from that folder: beside the worker that name is
  resolved as the system module by the worker's own import, which starts a
  second, self-initialising engine in the process. See drop_loose_proxy below.

  Pass --download to fetch the installer from the author's own release page
  instead of copying it in by hand.

Run from the NeuralScreen folder:   python tools\\prepare_amd_runtime.py
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

# The build the driver's offset table belongs to (v0.2.17, extracted payload).
STOCK_SIZE = 7_248_384
STOCK_SHA256 = "bc97f3b06718e19042acaf227bfe15d1e43d4977f9dc2e39994fcc511445ff4e"
#: The two patches below applied to the stock file. The pair is the same one the
#: MIT host publishes for this build, and this hash is what comes out of it -
#: reproduced here and checked against the published value, byte for byte.
#: It is also the hash the driver's own image check pins.
PATCHED_SHA256 = "c8a5d3af65f35058a2274fa3fbd3aa7a713ff86c3375e12d74af7d9618279066"

#: The SECOND build the driver knows: v0.3.1, the maintainer's current release.
#:
#: Why it is here at all: on the same Radeon architecture the newer release is
#: reported working (upstream issue #182, RDNA3 gfx1100 - `route fsr`, no
#: timeouts, healthy self-check) while v0.2.17 shows a black picture in this
#: project's own reports. Its notes also close what this host kept meeting by
#: hand ("new wait method ... reduces the chance of stalls", "Fixed three
#: crashes"). The driver carries an offset table for it; this script is how a
#: user ends up with it.
#:
#: NO PATCHES ARE APPLIED to this build, and that is deliberate rather than an
#: omission. The two patches this project uses are byte-level edits to v0.2.17
#: at offsets that do not exist here (checked: the `before` bytes of every
#: published v0.3.0 patch are absent from this image, except one log string).
#: The published patch set for v0.3.0 does not apply either. v0.3.1 ships as
#: the maintainer built it, and the driver drives it unmodified - which is the
#: same shape as the stock v0.2.17 path that already works this way.
V0310_SIZE = 7_304_192
V0310_SHA256 = "b108d6407eb7f094a4f9111edd778eee7b978b648d413a9fc7aeedfdd914c154"

#: Where --download gets the installer. The author's own release page, so the
#: file goes from him to the user and never through this project - his licence
#: forbids redistribution and asks for a link instead. The size is pinned:
#: a release is immutable, so a download of a different size is not the file
#: this driver's patches were written against.
INSTALLER_URL = ("https://github.com/danielblnc/DLSS-NR-on-AMD/releases/download/"
                 "v0.2.17/dlssnr_on_amd_setup.exe")
INSTALLER_NAME = "dlssnr_on_amd_setup.exe"
INSTALLER_SIZE = 7_538_418

#: The same thing for the second release. A release is immutable, so the size is
#: pinned here too: a download of a different size is not the image the v0.3.1
#: offset table was derived against, and the script refuses it.
INSTALLER_V0310_URL = ("https://github.com/danielblnc/DLSS-NR-on-AMD/releases/"
                       "download/v0.3.1/dlssnr_on_amd_setup.exe")
INSTALLER_V0310_NAME = "dlssnr_on_amd_setup_v0310.exe"
INSTALLER_V0310_SIZE = 7_598_347

# The patches applied to the runtime, with the expected bytes asserted before
# anything is written so a different build cannot be silently corrupted.
#
#   0x8583  the notify call the runtime makes after ExecuteCommandLists. On
#           v0.2.14 the host had to disable the hook-installer thread for this
#           to matter; on v0.2.17 that thread must STAY ALIVE (it resolves the
#           proxy), so the runtime does install its own ExecuteCommandLists
#           detour - and that detour already calls the notify entry itself. The
#           call here would execute the frame a second time.
#   0x6e3db report the timeout fallback accurately: with ToneChannels bit 4 set
#           and bit 2 clear the shader shows nothing, not the previous residual.
#
# What is NOT here, and must not come back:
#
#   0x6006 (0x1ffc on v0.2.14) "disable the hook-installer thread" - REMOVED,
#           and this one is not a tuning choice. On v0.2.14 that thread only
#           installed D3D12/DXGI detours, so killing it was free. On v0.2.17 the
#           same thread ALSO resolves the proxy: it builds the system paths for
#           d3d12.dll and dxgi.dll and loads them. Nop the CreateThread and those
#           stay null - the first call through one lands on address 0, measured
#           as 0xc0000005 at 0000000000000000 on the first frame after Enabled.
#           The thread proc grew between the two builds and shares only its
#           opening bytes with the old one, which is the shape of a function that
#           took on a second job.
#
#   0x62bd4 / 0x6321d "timeout keeps its input" - NOT NEEDED on this build.
#           v0.2.17 exposes that choice as ToneChannels bit 4 on with bit 2
#           clear, and the driver writes those bits per frame. Editing the
#           shader as well would fight it. (0x6e3db above is only the log text.)
#
#   0x625ac "bound the GPU wait loop" - REMOVED, and it should stay removed.
#           It caps the wait shader's spin at 2097152 iterations, which at the
#           measured rate (~371k iterations/ms, from Magpie's backend spec) is
#           about 5.7 ms - LESS than the network itself costs on this hardware
#           (8-16 ms per job in every Radeon log we have, 9-187 ms in the MIT
#           host's). A cap below the real cost makes the inline wait expire on
#           effectively every frame, and the apply pass then keeps its input.
#           That is the exact failure the cap was meant to prevent.
#           Both working hosts reach the same conclusion from opposite sides:
#           Magpie's spec records that writing `262144 + pixels/2` produced
#           "every one of the 13 timeouts in the run", and the MIT host lists
#           the cap under `dropped` - "so inline mode would time out on every
#           frame". Do not re-add it on the strength of the upstream list.
PATCHES = [
    (0x8583, "ff156f490800", "909090909090"),
    (0x6E3DB,
     "70726576696f757320726573696475616c2073686f776e",
     "63757272656e7420696e707574206b6570742020202020"),
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


#: Every offset this driver writes, taken from the table the C++ side uses.
#: Kept as a literal here on purpose: this module is the one a user runs to
#: CHECK a runtime, so it must be able to disagree with the header rather than
#: import it. `tests/test_amd_offsets_agree.py` pins the relationship between
#: the two so they cannot drift silently.
KNOWN_OFFSETS = {
    "kInit": 0x19240, "kRecord": 0xF600, "kNotify": 0x9170, "kShutdown": 0x12690,
    "kDevice": 0x8CEE8, "kQueue": 0x8CEF0, "kInitCtx": 0x8CEF8,
    "kHipDevice": 0x8DAD0, "kInlineMode": 0x8D6C0, "kInterop": 0x8D82C,
    "kEnabled": 0x8D9BC, "kFlagAfterInit": 0x8D218, "kUseFsrInputs": 0x8D9BE,
    "kUseDepth": 0x8D9BF, "kPerPassFlag": 0x8D9BD, "kDepthInverted": 0x8D9B0,
    "kDepthExplicit": 0x8D9B4, "kLocalTone": 0x8D9D0, "kLocalStructure": 0x8D9D4,
    "kSkinStructure": 0x8D9D8, "kCharMask": 0x8D9E0, "kToneChannels": 0x8D9E4,
    "kScale": 0x8D9DC, "kHistory": 0x8D010, "kWantHistory": 0x8D018,
    "kJobCounter": 0x8D914, "kStatusFlag": 0x8D21A, "kSyncCounter": 0x8D6F4,
    "kTimeoutCounter": 0x8D6F8, "kPendingList": 0x8D908,
}

#: The entry points: these must land in executable code, the rest in writable
#: data. Named separately because that is the split the check is about.
ENTRY_POINTS = ("kInit", "kRecord", "kNotify", "kShutdown")


def sections(data: bytes):
    """[(name, virtual address, virtual size)] per PE section."""
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    sect = coff + 20 + opt_size
    out = []
    for i in range(num_sections):
        off = sect + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("latin1")
        vsize, vaddr = struct.unpack_from("<II", data, off + 8)
        out.append((name, vaddr, vsize))
    return out


def check_offsets(data: bytes) -> list:
    """Every offset against the section it should live in. [] when sane.

    This is the check that runs BEFORE anything is written, and it catches the
    failure mode that costs a debugging session rather than producing an error:
    a stale data offset that has drifted into `.rdata` is not a crash on read,
    it is a write into read-only memory, and an entry point that drifted into
    the middle of an unrelated function gets CALLED rather than faulted.

    It cannot prove the offsets are right - only the runtime's own code can
    (which is what the probe's hash gate and, ultimately, a Radeon are for).
    What it proves is that they are at least shaped like the addresses they
    claim to be, on the build they claim to belong to.
    """
    problems = []
    try:
        secs = sections(data)
    except ValueError as exc:
        return [f"cannot read the PE sections: {exc}"]
    text = next((s for s in secs if s[0] == ".text"), None)
    data_sec = next((s for s in secs if s[0] == ".data"), None)
    if text is None or data_sec is None:
        return ["the image has no .text or no .data section - refusing to "
                "check an image this table was not derived from"]
    for name, value in sorted(KNOWN_OFFSETS.items()):
        want = text if name in ENTRY_POINTS else data_sec
        inside = want[1] <= value < want[1] + want[2]
        if not inside:
            problems.append(f"{name} 0x{value:x} is not inside {want[0]} "
                            f"[0x{want[1]:x}..0x{want[1] + want[2]:x})")
    return problems


def extract_installer_payload(installer: Path, destination: Path,
                              size: int, sha256: str, label: str) -> int:
    """Write the runtime image carried inside `installer`. 0 on success.

    The installer appends its payload to the end of its own PE, so the image is
    found by scanning for the byte range whose SHA-256 is the one expected. The
    scan starts at the PE's payload offset, which keeps it to the appended
    region instead of the whole 7.5 MB file - measured at ~0.1 s that way
    against a 70 s full scan in the same interpreter.

    The hash is what identifies it: a different release, a truncated download or
    a corrupted file all fail the same way, and nothing is written when they do.
    """
    if destination.is_file() and sha256_of(destination) == sha256:
        print(f"{destination.name} is already here ({size} bytes)")
        return 0
    data = installer.read_bytes()
    if len(data) < size:
        print(f"{installer.name} is smaller than the payload it should carry",
              file=sys.stderr)
        return 3
    import hashlib as _hashlib

    try:
        start = pe_payload_offset(data)
    except ValueError as exc:
        print(f"cannot read {installer.name}: {exc}", file=sys.stderr)
        return 3
    for candidate in range(start, len(data) - size + 1):
        if _hashlib.sha256(data[candidate:candidate + size]).hexdigest() == sha256:
            destination.write_bytes(data[candidate:candidate + size])
            print(f"written: {destination}  <- the {label} build, unmodified "
                  f"(payload at {candidate:#x})")
            print("note: this is NOT what runs by default. Set NS_AMD_V0310=1 "
                  "before starting the program to use it; the driver accepts "
                  "both and picks the offset table from each file's own hash.")
            return 0
    print(f"no {label} payload found inside {installer.name} - the download is "
          f"not the release this was written for", file=sys.stderr)
    return 3


def download_installer(folder: Path, url: str = INSTALLER_URL,
                       name: str = INSTALLER_NAME,
                       size: int = INSTALLER_SIZE) -> int:
    """Fetch the author's installer into `folder`. 0 on success.

    The runtime cannot be shipped here (its licence forbids redistribution and
    asks for a link to the release page instead), so this walks to that page
    and back with the file. Nothing of it passes through this project, and the
    size is checked against the pinned one: a release is immutable, so any
    other size is not the build the offsets and patches belong to.

    `url`/`name`/`size` come from the caller because there are two releases now,
    and each installer is a different length.
    """
    import urllib.error
    import urllib.request

    dst = folder / name
    if dst.is_file() and dst.stat().st_size == size:
        print(f"{dst.name} is already here ({size} bytes)")
        return 0
    print(f"downloading {name} from the author's release page...")
    print(f"  {url}")
    tmp = dst.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, \
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
              "DLSS-NR-on-AMD/releases and put it in this folder.",
              file=sys.stderr)
        return 6
    got = tmp.stat().st_size
    if got != size:
        tmp.unlink(missing_ok=True)
        print(f"the downloaded file is {got} bytes, expected {size} - "
              "this is not the release it was asked for. Nothing was written.",
              file=sys.stderr)
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
    """Undo the patches, in reverse, asserting the bytes first.

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
    ap.add_argument("--download-v0310", action="store_true",
                    help="fetch v0.3.1's installer as well, and write its "
                         "runtime as dlssnr_amd_pass1_v0310.dll (selected at run "
                         "time with NS_AMD_V0310=1)")
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

    # v0.3.1, when asked for. The payload is taken straight out of the
    # installer's own bytes rather than by running it: the installer is a GUI
    # that writes version.dll into a folder, and asking a user to run a second
    # one and then pick which file to keep is a step that can go wrong silently.
    # The image is located by its hash, so a wrong release cannot be mistaken
    # for the right one.
    if args.download_v0310:
        rc = download_installer(folder, INSTALLER_V0310_URL,
                                INSTALLER_V0310_NAME, INSTALLER_V0310_SIZE)
        if rc != 0:
            return rc
        rc = extract_installer_payload(
            folder / INSTALLER_V0310_NAME,
            folder / "dlssnr_amd_pass1_v0310.dll",
            V0310_SIZE, V0310_SHA256, "v0.3.1")
        if rc != 0:
            return rc
        drop_loose_proxy(folder, folder / "dlssnr_amd_pass1_v0310.dll")
        return 0

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
    #
    # The search is done for BOTH known payload sizes, because the driver now
    # knows two builds and the installer that carries each one has a different
    # length. Trying one fixed size first (the original shape) would reject a
    # v0.3.1 installer with "expected 7248384" and send the user after the wrong
    # release - measured on the real v0.3.1 installer, whose payload sits at
    # 0x47c00 and is 7304192 bytes.
    wanted_sizes = [(STOCK_SIZE, STOCK_SHA256), (V0310_SIZE, V0310_SHA256)]
    if len(data) not in [size for size, _ in wanted_sizes]:
        extracted = None
        for size, want in wanted_sizes:
            if len(data) <= size:
                continue
            try:
                off = pe_payload_offset(data)
            except ValueError as exc:
                print(f"cannot use {src.name}: {exc}", file=sys.stderr)
                return 3
            # The payload may sit anywhere before the end; the installer appends
            # it, so the honest search is forward from the PE offset.
            for candidate in range(off, len(data) - size + 1):
                if hashlib.sha256(bytes(data[candidate:candidate + size])).hexdigest() == want:
                    extracted = bytearray(data[candidate:candidate + size])
                    print(f"extracted a {size}-byte payload from {src.name} "
                          f"(offset {candidate:#x})")
                    break
            if extracted is not None:
                break
        if extracted is None:
            print(f"no known runtime payload found inside {src.name}; expected "
                  f"{STOCK_SIZE} bytes (v0.2.17) or {V0310_SIZE} bytes (v0.3.1) "
                  f"with a matching hash", file=sys.stderr)
            return 3
        data = extracted

    have = hashlib.sha256(data).hexdigest()
    if have == V0310_SHA256:
        # v0.3.1 - the second build the driver drives. It ships as the
        # maintainer built it: no patches are applied, because the patches this
        # project uses are v0.2.17 byte edits and the published v0.3.0 set does
        # not apply here either (checked, see V0310_SHA256's note). Writing it
        # under its own name keeps both builds side by side, exactly like the
        # v0.2.17 pair, so switching is one environment variable and no
        # reinstall.
        v0310_dst = folder / "dlssnr_amd_pass1_v0310.dll"
        v0310_dst.write_bytes(bytes(data))
        print(f"written: {v0310_dst}  <- the v0.3.1 build, unmodified")
        print("note: this is NOT what runs by default. The default image is "
              "dlssnr_amd_pass1.dll (v0.2.17); the driver accepts both and "
              "picks the offset table from each file's own hash.")
        return 0
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
              f"  expected: {STOCK_SHA256} (v0.2.17, from the official installer)\n"
              "Download that version's installer from the project's releases "
              "page and run it in this folder.", file=sys.stderr)
        return 4

    # The offset table before anything is written. The hash above proves the
    # image is the build this driver was written against; this proves the table
    # still describes addresses of the right KIND in it. Cheap, and it is the
    # check that would have turned a known crash into a message: a stale data
    # offset is a silent write into read-only memory, and a drifted entry point
    # gets CALLED instead of faulting.
    problems = check_offsets(bytes(data))
    if problems:
        print("the offset table does not match this image:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Refusing to write: the table in native/amd/amd_runtime.h belongs "
              "to a different build than the one in this folder.", file=sys.stderr)
        return 6
    print(f"offsets: {len(KNOWN_OFFSETS)} checked against the image's sections, "
          f"all in the right kind ({len(ENTRY_POINTS)} entry points in .text)")

    # BOTH images are written, and that is the point of this script now.
    #
    # They are the same build with a few in-place patches, so the driver's offset
    # table belongs to either one. Having both files side by side makes the
    # choice a single environment variable (NS_AMD_PATCHED=1) instead of a
    # reinstall, which is what a live A/B needs: the same machine, the same
    # session, one setting apart.
    #
    # STOCK is the default, because that is the shape the hosts that produce a
    # picture run: they never modify the runtime, never write into its image,
    # and never notify the engine by hand - the engine installs its own hooks
    # and owns the frame from there. The patched build is the opposite shape
    # (patch 0x1ffc disables that hook installer), and it stays available for
    # the comparison.
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
    print("applied the patches (hash verified)")

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
