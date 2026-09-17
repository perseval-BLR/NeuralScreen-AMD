# Third-party notices

NeuralScreen AMD ships its own code only. Two components of the AMD path are
other people's work and are brought by the user, never bundled:

## The neural runtime and its weights

`dlssnr_amd_pass1.dll` (a `version.dll` proxy) and
`dlssnr_on_amd_weights.bin` come from **danielblnc/DLSS-NR-on-AMD**, whose
licence forbids redistribution and modification, and whose weights are derived
from NVIDIA's own DLSS Neural Rendering binaries. They are also not source-
available. Neither file is included in any release archive or committed to
this repository - `tools/prepare_amd_runtime.py` turns a copy the user has
into the build this driver knows how to drive, and refuses anything else.

## The FidelityFX upscaler

`amd_fidelityfx_upscaler_dx12.dll` is AMD's FSR upscaler, taken from the
OptiScaler package. The neural runtime takes its frame from an FSR upscale
dispatch, so this DLL is required at run time - but the binary is not
redistributed here either; `native/AMD.md` says where to copy it from, and
`tools/prepare_amd_runtime.py` warns when it is missing.

## FidelityFX API headers (vendored)

`native/include/ffx/` contains the official FidelityFX API headers from the
AMD FidelityFX SDK
(https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK), used under the
**MIT** licence - see the copyright and permission notice at the head of each
file. They are kept in the SDK's own directory layout so their relative
includes resolve unchanged.

One local modification, in `api/include/ffx_api.h`: `FFX_API_ENTRY` is wrapped
in `#ifndef` so a consumer can define it as `__declspec(dllimport)`. Upstream
defines it unconditionally as `dllexport`, which is correct for the DLL that
implements the API and wrong for anything that only calls it.

## Everything else

The Python runtime bundled with a release is CPython plus its dependencies
(numpy, opencv, Pillow, mss, dxcam, av, comtypes, pywin32-ctypes), each under
its own licence - the full texts are in the archive under
`runtime/Lib/site-packages/*/LICENSE*`.
