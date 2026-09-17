# Third-party notices

NeuralScreen AMD ships its own code plus one binary belonging to AMD. The
neural runtime and its weights are other people's work and are brought by the
user, never bundled.

## The FidelityFX upscaler (bundled, under AMD's licence)

`amd_fidelityfx_upscaler_dx12.dll` ships in every release archive. It is AMD's
own FSR upscaler - Authenticode-signed by Advanced Micro Devices, version
4.1.1.2740 - and it is required at run time: the neural runtime takes its frame
from an FSR upscale dispatch, so with no upscaler to dispatch it has nothing to
attach to, and the pass runs, reports healthy and paints a black picture.

Copyright (C) Advanced Micro Devices, Inc.

AMD's FidelityFX SDK licence (docs/license.md in
https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK) grants, for the
files not listed as exempt:

> REDISTRIBUTION: Permission is hereby granted, free of charge, to any person
> obtaining a copy of this software and associated documentation files (the
> "Software"), to install, reproduce, copy and distribute copies of the
> Software, in binary form only, and to permit persons to whom the Software is
> provided to do the same, provided that the following conditions are met:
>
> No reverse engineering, decompilation, or disassembly of this Software is
> permitted.
>
> Redistributions must reproduce the above copyright notice, this permission
> notice, and the following disclaimers and notices in the Software
> documentation and/or other materials provided with the Software.

`Kits\FidelityFX\signedbin\amd_fidelityfx_upscaler_dx12.dll` is named among the
exempt files, which are under the **MIT** licence (the full text is at the end
of that same licence file). The file distributed here is that DLL: 28,761,864
bytes, sha256 `d0dcccc74a43c44ba435b7a369b456e0970d8a4464e4bd683119b374f2c9fb46`,
identical to the copy inside AMD's own `FidelityFX-Samples-v2.3.0-prebuilt.zip`.

The disclaimers above apply in full: the software is provided "as is", without
warranty of any kind. AMD's name is used here only to identify the file and to
credit its author; nothing in this project is endorsed by AMD.

## The neural runtime and its weights

`dlssnr_amd_pass1.dll` (a `version.dll` proxy) and
`dlssnr_on_amd_weights.bin` come from **danielblnc/DLSS-NR-on-AMD**, whose
licence forbids redistribution and modification, and whose weights are derived
from NVIDIA's own DLSS Neural Rendering binaries. They are also not source-
available. Neither file is included in any release archive or committed to
this repository - `tools/prepare_amd_runtime.py` turns a copy the user has into
the build this driver knows how to drive, and refuses anything else. With
`--download` it fetches the author's own installer from the author's release
page, so the file travels from him to you and never through this project.

## FidelityFX API headers (vendored)

`native/include/ffx/` contains the official FidelityFX API headers from the
AMD FidelityFX SDK, used under the **MIT** licence - see the copyright and
permission notice at the head of each file. They are kept in the SDK's own
directory layout so their relative includes resolve unchanged.

One local modification, in `api/include/ffx_api.h`: `FFX_API_ENTRY` is wrapped
in `#ifndef` so a consumer can define it as `__declspec(dllimport)`. Upstream
defines it unconditionally as `dllexport`, which is correct for the DLL that
implements the API and wrong for anything that only calls it.

## Everything else

The Python runtime bundled with a release is CPython plus its dependencies
(numpy, opencv, Pillow, mss, dxcam, av, comtypes, pywin32-ctypes), each under
its own licence - the full texts are in the archive under
`runtime/Lib/site-packages/*/LICENSE*`.

The NVIDIA neural runtime (`native/nvngx_dlssnr.dll`, `native/nvngx_dlssg.dll`)
is NVIDIA's redistributable and ships under its own terms; the NVIDIA path
exists in this build for hybrid machines.
