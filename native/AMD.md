# NeuralScreen AMD - the Radeon neural pass

NeuralScreen AMD runs its neural pass on an **AMD Radeon RX 7000 / 9000**
(RDNA3 / RDNA4). The program itself starts on any machine: on a card the pass
does not support it opens, captures and shows its menu as usual, and the
status line says **card not supported** instead of processing the picture.

This is an experiment that has not run on a Radeon yet: the pass was written
against the runtime's own offsets and verified by compilation and a walk of
every startup gate, but no AMD card has produced a processed frame with it.
It either works or it tells you exactly why it did not - and the "why" is
what makes the next fix possible.

**Requirements**

- Radeon RX 7000 or 9000 series (RX 6000 is not supported by the runtime)
- Windows 11, DirectX 12
- AMD Adrenalin **26.1.1 or newer** (ships the HIP 7 runtime the pass needs)

Nothing else. The neural runtime and its weights **ship in this archive** - unpack
and run. `native\probe_amd.exe` says whether everything is in place.

## 1. The runtime is already installed

Everything the pass needs is in `native\`:

| File | What it is |
| --- | --- |
| `dlssnr_amd_pass1.dll` | the runtime, stock build - **this is what runs** |
| `dlssnr_amd_pass1_patched.dll` | the same build with the patches below |
| `dlssnr_on_amd_weights.bin` | the network weights, 153 tensors |
| `dlssnr_on_amd.ini` | the keys the runtime reads from its own DllMain |
| `amd_fidelityfx_upscaler_dx12.dll` | AMD's FSR upscaler, needed for the frame |

The runtime comes from the **DLSS-NR-on-AMD** project and is driven here as a
component of this build. Its weights were produced from `nvngx_dlssnr.dll`,
which this archive also carries (it is NVIDIA's redistributable and the same file
the NVIDIA path uses on hybrid machines).

You do not need to run an installer, and you do not need to fetch anything.

### Which of the runtime builds runs

Two of the three images are the same **v0.2.17** build; the patched one has two
byte-level corrections, all the same length, so no address moves and the
driver's offset table accepts either. The difference between them is a single variable:

```
dlssnr_amd_pass1.dll          stock    <- runs by default
dlssnr_amd_pass1_patched.dll  patched  <- set NS_AMD_PATCHED=1 before starting
```

The two corrections are small and specific: one removes the notify call the
runtime makes after `ExecuteCommandLists` (it would announce the same submission
twice), one corrects a log line about a timed-out frame. Neither disables the
runtime's own hooks - on this build that is not an option, and worth knowing if
you came from an older version of this program: the setup thread that installs
those hooks also resolves the runtime's own `d3d12.dll`/`dxgi.dll` proxy, so
disabling it leaves those null and the first call through one lands on address
zero. That was the previous version's patch; it is gone.

### The second release: v0.3.1

There is a **second release** the driver knows, not just a second variant of the
same one - and it is the one to try when the picture stays black:

```
dlssnr_amd_pass1_v0310.dll    v0.3.1   <- set NS_AMD_V0310=1 before starting
```

It is **in the archive already**, beside the other two images - unpacking is all
it takes. To switch to it, set `NS_AMD_V0310=1` before starting the program.

If you would rather prepare it yourself from the author's installer, this does
the same thing in one command:

```
runtime\python.exe tools\prepare_amd_runtime.py --download-v0310
```

That fetches v0.3.1's installer from the author's release page, takes the runtime
out of it and writes the file. Nothing is installed and no second GUI is run: the
image is located by its own SHA-256 inside the installer, so a wrong or truncated
download is refused rather than written.

The file is the build as its author made it, **unpatched**: the two corrections
above are byte edits at v0.2.17 offsets and do not exist in this image.

Why it is here: on the same Radeon architecture this release is reported working
- `route fsr`, no timeouts, healthy self-check - while v0.2.17 shows a black
picture in most reports this project has received. Its own notes also fix what
this program kept meeting by hand: stalls, three crashes, a memory leak, and
startup crashes in RE Engine games.

The driver picks the offset table from each file's own SHA-256, so a mislabelled
file is refused rather than driven with the wrong addresses, and the log names
the build it accepted.

Two things stay unproven on v0.3.1, and the log says so rather than hiding them:
two diagnostic counters (jobs completed, engine-side timeouts) have no known
address in that build and are reported as zero instead of being read, and there
is no patched variant of it.

`native\probe_amd.exe` names which image it found, and it checks the one the
program would actually load - it reads the same `NS_AMD_V0310` variable, and the worker's log opens
with `runtime image: STOCK` or `PATCHED`.

### If you supply your own copy instead

`tools\prepare_amd_runtime.py` still works for a runtime you install yourself:
point it at the folder the author's installer wrote, and it produces the same
two files plus the A/B pair. It also **deletes `version.dll`** from that folder,
and that is not housekeeping: NeuralScreen's worker reads file versions, so it
statically imports `VERSION.dll` - a system module name. Windows resolves such an
import from the program's own folder first, and `version.dll` is not a KnownDLL,
so the runtime sitting there under that name (the installer names it exactly
that) is the file the import binds to. That starts a **second, self-initialising
copy of the engine** inside the worker, before NeuralScreen has set anything up,
with its own frame loop. Two engines in one process is the documented way to get
a black picture. Nothing is lost by removing it: the worker loads the runtime by
name (`dlssnr_amd_pass1.dll`).

## 2. Check it, then run it

   ```
   native\probe_amd.exe
   ```

   It reports what it found, which runtime image it is, and whether HIP sees your
   card. **Keep `native\probe_amd.log`** - it is the first thing a bug report
   should carry.

   It works from any folder, and its first line prints the folder it looked in.
   That line is worth a glance in a report: the path is made absolute before
   anything is loaded, so a relative one there means the check looked somewhere
   other than where you are.

The FidelityFX upscaler (`amd_fidelityfx_upscaler_dx12.dll`) needs nothing from
you: it **ships in the archive**, because AMD's licence permits redistributing
that binary (see `THIRD-PARTY-NOTICES.md`). It is not optional - without it the
pass runs, reports healthy and paints a black picture:

> The runtime is written as a `version.dll` proxy for a *game*: it hooks the
> FidelityFX upscale dispatch and takes the frame from there. A host that never
> dispatches FSR gives it nothing to attach to, and it falls back to routing a
> backbuffer that was never ours - the network runs, the picture stays black,
> and the runtime's own log says `frames N dispatches 0 ... route backbuffer`.

## 2. It is on by default

This build runs the AMD pass out of the box. The switch lives under
**Settings -> Processing -> Neural pass**: **AMD Radeon (RDNA3+)** is the
default, **CPU DIS** and **NVOFA motion (NVIDIA)** turn the pass off. Changing
it restarts the worker, and the log says what happened.

The pass runs the network at a **reduced resolution** (the same "Network
resolution" control the NVIDIA path uses) and composes the result back at
full size. How much one frame costs on a Radeon is exactly one of the things
this test is meant to find out - until then, the network resolution is the
control worth trying first if a frame takes too long.

## 3. What the log says

The app's log - `NeuralScreen.log` next to the program - carries `[amd]`
lines. Nothing has to be turned on: every line below is written always.

| Line | Meaning |
|---|---|
| `[amd] ===== AMD path active =====` | the engine came up; frames are processed |
| `[amd] runtime: patched v0.2.17 (the tested build)` | the right build was found |
| `[amd] sha256: 81efaadc...` | which build is actually on disk |
| `[amd] the runtime did not come up: <reason>` | the exact reason, in words |
| `[amd] the runtime's D3D12/DXGI hooks are in place (N ms)` | the runtime can see our frames |
| `[amd] runtime image: STOCK` or `PATCHED` | which of the two v0.2.17 images is in play |
| `[amd] runtime image: STOCK v0.3.1` | the newer release is loaded (`NS_AMD_V0310=1`); a different offset table applies |
| `[amd] the runtime's hooks were NOT seen` | it cannot see them - the pass will process nothing |
| `[amd] FidelityFX upscaler loaded` | the dispatch the engine follows is available |
| `[amd] FSR contexts: network 1664x936 (1:1), upscale work -> display` | the two dispatches are set up |
| `[amd] FSR dispatch N: network at 1664x936, then the upscale` | frames are reaching the engine |
| `[amd] engine surfaces at 1280x720` | the resolution the network runs at |
| `[amd] N frames, avg X ms, worst Y ms, timeouts Z` | every 30 s: how it is doing, and how many frames have gone by since the engine last queued a job |
| `[amd] the engine has recorded NO job in the last 60 frames` | the frames reach it but nothing is queued - a feeding problem, not a picture one |
| `[amd] engine init: ok (the engine wrote its own 'engine init ok')` | the engine itself confirmed it came up - the line below is not this |
| `[amd] engine init: THE ENGINE NEVER CAME UP` | our init call returned success, but the engine's own log has no `engine init ok`: nothing after this line describes a working pass |
| `[amd] the engine's own measure: ... encoded mean 0.000` | the engine measured the frame it was handed and found it black |
| `[amd] the capture's own mean luminance: 0.412 over N frames` | the same measurement on OUR side: what the desktop capture actually contained |
| `[amd] ... (N of them fully black)` | how many captured frames were entirely black - a non-zero count means the capture, not the pass, is losing the picture |
| `[amd] WARNING: the runtime is unpatched...` | you skipped the patching step |
| `[amd] this GPU is not supported by the AMD neural pass` | the card is not a Radeon: the program runs, the picture is not processed |

The app itself shows the pass as running (or not) in the menu; a failure never
takes the overlay down - the raw frame simply keeps going through, and the
reason is in the log.

## 4. If it does not work

Press **Settings -> Program -> Create diagnostic package** and attach what it
writes. One button, one file: the package carries `NeuralScreen.log`, your card
model and driver version, the runtime's hash, and the stage where it stopped -
with your user name and absolute paths replaced by placeholders before anything
is written. Three logs are copied next to the archive as well: the probe's own
`probe_amd.log`, and **`dlssnr_on_amd.log`** - the runtime's own log, which
records the staging formats, per-job timings, timeouts and faults from inside
the engine. That last one is the most useful file on a machine where the picture
never appears.

What it does not do: it never uploads anything by itself. The ZIP lands in a
`diagnostics` folder next to the program and stays there until you send it.

**If the program disappears or the settings will not open:** that is a crash,
not a silent exit, and the report says so. `NeuralScreen.log` gets a line like

    [crash] === CRASH: ACCESS_VIOLATION (0xC0000005) at dlssnr_amd_pass1.dll + 0x...

naming the code, the module that faulted and the offset inside it, plus how far
the pass had got (`frames=0` means it died building the first frame). The menu
itself works even when the pass never started - open it with **Num 2**.

**If the menu itself is unusable** (a magenta slab over the screen, or buttons
that do not react), do not fight it: the tray icon has its own item,
**Create diagnostic package**, and the tray menu is drawn by Windows outside
this program's window. It produces the same ZIP. That path exists because a
user hit exactly this and could not send a report at all.

Otherwise the log names the exact failure - a missing file, an unknown build, no
HIP device, or the engine's own refusal reason. That is the point of this first
release: it is built to explain itself.

**If your antivirus flags the download as a virus.** It is a false positive.
The archive carries the embedded Python interpreter the program runs on, and its
stdlib file `runtime/python313.zip` (3,825,631 bytes) is the *unmodified* file
from python.org: SHA-256
`1916abd946d2044ec8c04c3319f96c8415d5b6fce01e125622827f2b7756cbab`, byte for byte
identical to the one inside
[python-3.13.15-embed-amd64.zip](https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip).
Heuristics flag packed interpreters often enough that this is a known false
positive, not a detection of anything in this program. Download the release
straight from the release page rather than through a third-party mirror, allow
it in your antivirus, and check the hash yourself first if you prefer: unpack
the archive and compare `runtime/python313.zip` against the value above. The
release page lists a SHA-256 for the archive itself, so the download can be
verified before it is unpacked.

## Known limits of this first version

- **No Frame Generation.** FG rides on an NVIDIA-only optical-flow runtime;
  the switch stays off on a Radeon.
- **No depth guide.** The engine is handed a zeroed depth surface, which is
  what the reference does too until a game provides one.
- **The motion guide** comes from NeuralScreen's own estimator, resampled to
  the network's resolution - the same field the NVIDIA path uses.
- **The first frames are slow** (the engine builds its kernels); a frame that
  takes too long is skipped, not fatal.
- The runtime is third-party, closed-source and hash-locked. A different
  version is refused rather than guessed at - a wrong guess there does not
  return an error, it jumps into nothing.

## The card is chosen by the runtime, not by this program

The runtime has a device field, and it is left at **-1 (auto)**: the runtime
matches a HIP device to the D3D12 device behind the first presented swapchain
and records which one it took in `dlssnr_on_amd.log` (`matches the game's
D3D12 adapter`).

That is deliberate, and it is the fix rather than an omission. The field is an
override, not a hint: with an index written into it the runtime uses that index
and skips the match. One physical card routinely enumerates two to four times
with different LUIDs, and on a machine with an integrated Radeon the HIP index 0
is the iGPU - which is how "auto picked the wrong device" happens in the first
place. v0.2.17 is the build whose release notes name this: *fixed crashes and
black screens on multi-GPU systems*.

So if a report says the wrong device was used, the useful question is what the
runtime logged, not what this program computed. A machine where nothing matches
is refused before the runtime is allowed to build a frame.

## Two fields that were read wrong, and what the disassembly showed

Both of these were found by taking the runtime apart rather than by a report,
and both have the same shape: a value this program wrote with confidence into
a place the engine does not read.

### The engine's watchdog counter is not an abort token

The driver wrote `InterlockedExchange(..., 0)` into `0x8d808` after every
accepted frame, to clear what its table called a stale abort token. It is not
one. The engine's watchdog function (`0x16260..0x1652d`) stores the job id into
`0x8d808` and `0x8d80c` when a timeout fires and reads them back, so the clear
was erasing the engine's own record of which job timed out - and the note right
below the constant in the same header already said so. The write is gone; the
engine resets its real abort flag itself through `hipMemcpyAsync`.

The address came from arithmetic, not from a measurement: the port applied the
delta that fits the fields around it (`0x76c68 + 0x16ba0`), while the reference
that documents the field computes `0x76c68 + 0x16a20` and lands on `0x8d688`.
The two answers differ by `0x180`, neither could be confirmed without a card,
and so the honest move was to stop writing rather than pick one. Nothing the
driver needs depends on either.

### Intensity reached the file, not the network

The menu's Intensity was written to `Scale` in the runtime's ini, on the
reading that this is where the runtime takes its strength from. For this build
it is not. The ini is parsed by one function (`0x7af0..0x801a`) that is reached
through a `call_once` guard inside the first `CreateSwapChain` detour
(`0x97c0`), immediately before the engine's `Init` - and it runs **once**. A
value written to the file after that is read by nothing.

The field is read per frame: `0x140a0..0x15e79` is the recording function, the
same one that reads Local Tone, Local Structure and Skin Structure, all of
which this driver has always written directly. Intensity is written there now,
every frame, and the ini is still updated on change so the next launch starts
from the slider.

### Why this survived

Both writes compiled, both were logged, and both looked right in a log. The
only thing that catches this class is reading what the engine does with the
field afterwards - which is what `tests/test_amd_offsets_agree.py` now forces
for the first one: an offset that appears both in the table and in the note
that says the host must not write it fails the suite.

## The offset table is checked, not trusted

The runtime exports nothing, so every address this program writes into it is a
raw offset - and a wrong one does not fail politely. A stale data offset that
has drifted into `.rdata` is a silent write into read-only memory; a drifted
entry point is called rather than faulted. Both survive a clean build.

`tools\prepare_amd_runtime.py` therefore holds the whole table against the
image's sections before it writes anything, and refuses to write when a value is
in the wrong kind of section. The table is named in three places - the driver's
header, the probe, and that script - and `tests\test_amd_offsets_agree.py` pins
them to each other by name and by value, so a port cannot update two and forget
the third. That failure has already cost the project this table came from a
crash on the first frame.
