# FidelityFX API headers (vendored)

Source: AMD FidelityFX SDK, `Kits/FidelityFX/{api,upscalers}/include`
(https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK), **MIT**.
Fetched 2026-09-16, kept in the SDK's own directory layout so the headers'
relative includes resolve unchanged.

Why they are here: the AMD DLSS-NR runtime takes its colour, motion and depth
from an FSR upscaler dispatch, so hosting it means issuing that dispatch
ourselves (`docs/AMD_HIP_HOSTING.md`). Only headers — the implementation is
the `amd_fidelityfx_upscaler_dx12.dll` that ships with the pack.

Local modification, one line in `api/include/ffx_api.h`: `FFX_API_ENTRY` is
wrapped in `#ifndef` so a consumer can define it as `__declspec(dllimport)`.
Upstream defines it unconditionally as `dllexport`, which is right for the DLL
that implements the API and wrong for anything that calls it.

MIT text: see the header of any of these files.
