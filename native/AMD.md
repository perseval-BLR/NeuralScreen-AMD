# AMD / Radeon (experimental)

NeuralScreen can run its neural pass on an **AMD Radeon RX 7000 / 9000**
(RDNA3 / RDNA4) instead of an NVIDIA card. This is experimental: it is the
same kind of neural rendering, but through a different runtime, and it has
been tested far less. It either works or it tells you exactly why it did not -
and the "why" is what makes the next fix possible.

**Requirements**

- Radeon RX 7000 or 9000 series (RX 6000 is not supported by the runtime)
- Windows 11, DirectX 12
- AMD Adrenalin **26.1.1 or newer** (ships the HIP 7 runtime the pass needs)
- Your own copy of the neural runtime (below)

## 1. Prepare the runtime

The pass drives a third-party runtime (the DLSS-NR-on-AMD project). It cannot
be shipped inside NeuralScreen - its licence forbids redistribution, and its
weights are derived from NVIDIA's own - so you bring your own copy. Two
commands turn it into the shape NeuralScreen expects.

1. Download `dlssnr_on_amd_setup.exe` **v0.2.14** from
   https://github.com/danielblnc/DLSS-NR-on-AMD/releases (other versions will
   be refused - the driver's offset table belongs to this one build).
2. Copy it into the `native` folder of NeuralScreen and run it there
   (answer `y` to "Use this folder?"). It writes `version.dll`,
   `dlssnr_on_amd.ini` and `dlssnr_on_amd_weights.bin`.
3. Run the preparation script from the NeuralScreen folder:

   ```
   runtime\python.exe tools\prepare_amd_runtime.py
   ```

   It verifies the runtime is the build the driver knows, applies the five
   documented patches (without them the runtime installs its own hooks and
   fights NeuralScreen for the frame), and writes `dlssnr_amd_pass1.dll`.
4. Check the result:

   ```
   native\probe_amd.exe
   ```

   It reports what it found, whether the build is the known one, and whether
   HIP sees your card. **Keep `native\probe_amd.log`** - it is the first thing
   a bug report should carry.

## 2. Turn it on

In the menu: **Settings -> Processing -> Neural pass and motion -> AMD
(Radeon RDNA3+)**. The worker restarts and the log says what happened.

The pass runs the network at a **reduced resolution** (the same "Network
resolution" control the NVIDIA path uses) and composes the result back at full
size. This matters more here than on NVIDIA: the AMD network costs roughly
30 ms per megapixel, so at "full screen" on a 4K desktop a frame would take a
quarter of a second. A reduced network resolution is the one control that
moves the frame rate much - start there.

## 3. What the log says

The worker's log (the app's log window, or `dlss5-feed-host.log` next to the
worker) carries `[amd]` lines:

| Line | Meaning |
|---|---|
| `[amd] ===== AMD path active =====` | the engine came up; frames are processed |
| `[amd] runtime: patched v0.2.14 (the tested build)` | the right build was found |
| `[amd] sha256: 3c9ca13f...` | which build is actually on disk |
| `[amd] the runtime did not come up: <reason>` | the exact reason, in words |
| `[amd] engine surfaces at 1280x720` | the resolution the network runs at |
| `[amd] N frames, avg X ms, worst Y ms, timeouts Z` | every 30 s: how it is doing |
| `[amd] WARNING: the runtime is unpatched...` | you skipped the patching step |

The app itself shows the pass as running (or not) in the menu; a failure never
takes the overlay down - the raw frame simply keeps going through, and the
reason is in the log.

## 4. If it does not work

Collect these three things and open an issue:

1. `native\probe_amd.log` (from step 1.4)
2. the worker log (`dlss5-feed-host.log` next to `native\nvngx.dll`)
3. your GPU model and driver version

The log names the exact failure - a missing file, an unknown build, no HIP
device, or the engine's own refusal reason. That is the point of this first
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
