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

### Which of the two runtime builds runs

They are one build with 55 bytes changed in four places, all in place and all
the same length, so no address moves and the driver's offset table accepts
either. The difference between them is a single variable:

```
dlssnr_amd_pass1.dll          stock    <- runs by default
dlssnr_amd_pass1_patched.dll  patched  <- set NS_AMD_PATCHED=1 before starting
```

The stock build is the default because that is the shape the external host that
produces a picture runs: it never modifies the runtime and never drives the
engine by hand - the engine installs its own hooks and owns the frame from
there. The patched build disables that hook installer (patch `0x1ffc`), so
NeuralScreen has to drive everything itself. `native\probe_amd.exe` names which
one it found, and the worker's log opens with `runtime image: STOCK` or
`PATCHED`.

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
| `[amd] runtime: patched v0.2.14 (the tested build)` | the right build was found |
| `[amd] sha256: 81efaadc...` | which build is actually on disk |
| `[amd] the runtime did not come up: <reason>` | the exact reason, in words |
| `[amd] the runtime's D3D12/DXGI hooks are in place (N ms)` | the runtime can see our frames |
| `[amd] detour wait: not applicable` | the patched image is loaded, whose hook installer is off by design - nothing to wait for |
| `[amd] the runtime's hooks were NOT seen` | it cannot see them - the pass will process nothing |
| `[amd] FidelityFX upscaler loaded` | the dispatch the engine follows is available |
| `[amd] FSR contexts: network 1664x936 (1:1), upscale work -> display` | the two dispatches are set up |
| `[amd] FSR dispatch N: network at 1664x936, then the upscale` | frames are reaching the engine |
| `[amd] engine surfaces at 1280x720` | the resolution the network runs at |
| `[amd] N frames, avg X ms, worst Y ms, timeouts Z` | every 30 s: how it is doing, and how many frames have gone by since the engine last queued a job |
| `[amd] the engine has recorded NO job in the last 60 frames` | the frames reach it but nothing is queued - a feeding problem, not a picture one |
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
