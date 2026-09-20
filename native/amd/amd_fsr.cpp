// The FidelityFX bridge - see amd_fsr.h for why it exists and what the two
// dispatches are for.

#include "amd_fsr.h"

#include <windows.h>

#include <cstdio>
#include <cstring>

// The MIT FidelityFX API headers. Vendored under native/include/ffx in the
// layout AMD ships them (ffx/upscalers/include reaches up into
// ffx/api/include), so they stay byte-identical to upstream and can be
// updated by copying a new SDK over them. The DLL itself is loaded by name
// at run time and is never linked or shipped.
//
// FFX_API_ENTRY is imported, not exported: upstream marks every entry point
// dllexport, which is right for the DLL implementing the API and wrong here -
// this host only CALLS them, through the pointers it resolves at run time.
// Without this the linker would look for ffxCreateContext inside our own
// binary and fail to link.
#define FFX_API_ENTRY __declspec(dllimport)
#include "ffx/api/include/ffx_api.h"
#include "ffx/api/include/ffx_api_types.h"
#include "ffx/upscalers/include/ffx_upscale.h"
#include "ffx/api/include/dx12/ffx_api_dx12.h"

namespace amd_fsr {

namespace {

// The FFX entry points have C linkage and a fixed shape; the struct in the
// header keeps them opaque so the rest of the worker does not include these
// headers. The casts are the only place that knows the real signatures.
using CreateFn = ffxReturnCode_t (*)(ffxContext *, ffxCreateContextDescHeader *,
                                     const ffxAllocationCallbacks *);
using DispatchFn = ffxReturnCode_t (*)(ffxContext *, const ffxDispatchDescHeader *);
using DestroyFn = ffxReturnCode_t (*)(ffxContext *, const ffxAllocationCallbacks *);

ffxContext *AsCtx(void *p) { return reinterpret_cast<ffxContext *>(p); }

}  // namespace

Upscaler::~Upscaler() {
    // Contexts first: they hold references into the DLL's own state. The
    // module is deliberately NEVER unloaded - the same reason the neural
    // runtime is pinned (its worker threads hold references into its image),
    // and an unload under them is a crash rather than a clean exit.
    ReleaseContexts();
}

bool Upscaler::Load(const std::wstring &dir, std::string &why) {
    if (api_.module != nullptr) return true;

    const std::wstring beside = dir + L"\\" + kUpscalerName;
    HMODULE mod = LoadLibraryExW(beside.c_str(), nullptr,
                                 LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                 LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (mod == nullptr) mod = LoadLibraryW(kUpscalerName);
    if (mod == nullptr) {
        why = "amd_fidelityfx_upscaler_dx12.dll not found - the runtime needs "
              "a real FSR dispatch to follow, and without it no frame is "
              "processed (bring your own copy: it is not redistributable)";
        return false;
    }

    api_.create = reinterpret_cast<int (*)(void **, void *, const void *)>(
        GetProcAddress(mod, "ffxCreateContext"));
    api_.dispatch = reinterpret_cast<int (*)(void **, const void *)>(
        GetProcAddress(mod, "ffxDispatch"));
    api_.destroy = reinterpret_cast<int (*)(void **, const void *)>(
        GetProcAddress(mod, "ffxDestroyContext"));
    if (api_.create == nullptr || api_.dispatch == nullptr || api_.destroy == nullptr) {
        why = "the upscaler DLL has no ffxCreateContext/ffxDispatch/"
              "ffxDestroyContext - not an FFX API build";
        return false;
    }
    api_.module = mod;
    return true;
}

bool Upscaler::CreateContexts(ID3D12Device *device, UINT work_w, UINT work_h,
                              UINT out_w, UINT out_h, std::string &why) {
    if (!Ready()) { why = "the upscaler is not loaded"; return false; }
    if (ctx_net_.handle != nullptr) return true;   // already built

    work_w_ = work_w; work_h_ = work_h;
    out_w_ = out_w; out_h_ = out_h;
    upscaling_ = (work_w != out_w || work_h != out_h);

    ffxCreateBackendDX12Desc backend{};
    backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12;
    backend.device = device;

    // The colour handed over is linear light (the network is built for fp16
    // linear), and the flag changes how both the upscaler and the hooked
    // network read it.
    ffxCreateContextDescUpscale net_desc{};
    net_desc.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    net_desc.header.pNext = &backend.header;
    // NO FFX_UPSCALE_ENABLE_AUTO_EXPOSURE.
    //
    // The flag asks the upscaler to adapt exposure from the input, and it was
    // armed here while the dispatch bound no exposure resource at all - so with
    // a black input the adaptation had nothing to hold on to and the network
    // read a runaway value. The engine logged it as `exposure 9999.9980` on
    // every Radeon report.
    //
    // The network is now handed a fixed 1x1 exposure instead. Measured on the
    // reference host: its own adaptation wandered 0.645..0.925 on input stuck
    // at 0.490..0.502, which reads as a brightness pump; a constant is the
    // stable answer and the resource still travels through the FFX field the
    // API provides for it.
    net_desc.flags = FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE;
    // A is 1:1 on purpose: its job is to be the dispatch the runtime follows,
    // not to scale anything.
    net_desc.maxRenderSize = { work_w, work_h };
    net_desc.maxUpscaleSize = { work_w, work_h };

    const auto create_fn = reinterpret_cast<CreateFn>(api_.create);
    ffxReturnCode_t rc = create_fn(AsCtx(&ctx_net_.handle), &net_desc.header, nullptr);
    if (rc != FFX_API_RETURN_OK) {
        ctx_net_.handle = nullptr;
        why = "ffxCreateContext (network) returned " + std::to_string(static_cast<int>(rc));
        return false;
    }

    if (upscaling_) {
        // B: the upscale to the display. Created with the same flags; the
        // difference at dispatch time is the motion vectors, which is what
        // tells the runtime to leave this one alone.
        ffxCreateContextDescUpscale up_desc{};
        up_desc.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
        up_desc.header.pNext = &backend.header;
        up_desc.flags = net_desc.flags;
        up_desc.maxRenderSize = { work_w, work_h };
        up_desc.maxUpscaleSize = { out_w, out_h };
        rc = create_fn(AsCtx(&ctx_up_.handle), &up_desc.header, nullptr);
        if (rc != FFX_API_RETURN_OK) {
            ctx_up_.handle = nullptr;
            why = "ffxCreateContext (upscale) returned " + std::to_string(static_cast<int>(rc));
            return false;
        }
    }
    return true;
}

void Upscaler::ReleaseContexts() {
    if (api_.module == nullptr) return;
    const auto destroy_fn = reinterpret_cast<DestroyFn>(api_.destroy);
    if (ctx_net_.handle != nullptr) { destroy_fn(AsCtx(&ctx_net_.handle), nullptr); ctx_net_.handle = nullptr; }
    if (ctx_up_.handle != nullptr) { destroy_fn(AsCtx(&ctx_up_.handle), nullptr); ctx_up_.handle = nullptr; }
}

bool Upscaler::DispatchNet(ID3D12CommandList *list, ID3D12Resource *color,
                           ID3D12Resource *depth, ID3D12Resource *motion,
                           ID3D12Resource *exposure, ID3D12Resource *net,
                           float frame_ms, bool reset, std::string &why) {
    if (ctx_net_.handle == nullptr) { why = "no network context"; return false; }

    ffxDispatchDescUpscale d{};
    d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    d.commandList = list;
    d.color = ffxApiGetResourceDX12(color, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    d.depth = ffxApiGetResourceDX12(depth, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    // The motion vectors are the flag that says "process this one". Bound
    // here and deliberately null in DispatchUpscale.
    d.motionVectors = ffxApiGetResourceDX12(motion, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    d.output = ffxApiGetResourceDX12(net, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
    // The 1x1 exposure the network is handed. Bound as COMPUTE_READ because the
    // FFX runtime samples it; passing nullptr here is what made the engine
    // report `exposure no` and adapt its own on a black input.
    //
    // Left optional on purpose: a caller with no exposure surface still gets a
    // valid dispatch, and the engine's own reading is then the honest one.
    d.exposure = ffxApiGetResourceDX12(exposure, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    d.jitterOffset = { 0.0f, 0.0f };
    d.motionVectorScale = { static_cast<float>(work_w_), static_cast<float>(work_h_) };
    d.renderSize = { work_w_, work_h_ };
    d.upscaleSize = { work_w_, work_h_ };
    d.enableSharpening = false;
    d.sharpness = 0.0f;
    d.frameTimeDelta = frame_ms > 0.0f ? frame_ms : 16.6f;
    d.preExposure = 1.0f;
    d.reset = reset;
    d.cameraNear = 0.1f;
    d.cameraFar = 1000.0f;
    d.cameraFovAngleVertical = 1.0f;
    d.viewSpaceToMetersFactor = 1.0f;

    const auto dispatch_fn = reinterpret_cast<DispatchFn>(api_.dispatch);
    const ffxReturnCode_t rc = dispatch_fn(AsCtx(&ctx_net_.handle), &d.header);
    if (rc != FFX_API_RETURN_OK) {
        why = "ffxDispatch (network) returned " + std::to_string(static_cast<int>(rc));
        return false;
    }
    return true;
}

bool Upscaler::DispatchUpscale(ID3D12CommandList *list, ID3D12Resource *net,
                               ID3D12Resource *depth, ID3D12Resource *out,
                               float frame_ms, bool reset, std::string &why) {
    if (ctx_up_.handle == nullptr) { why = "no upscale context"; return false; }

    ffxDispatchDescUpscale d{};
    d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    d.commandList = list;
    d.color = ffxApiGetResourceDX12(net, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    d.depth = ffxApiGetResourceDX12(depth, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    // No motion vectors: this is how the runtime knows this dispatch is not
    // the one to run the network on, so the network is not charged for it.
    d.motionVectors = ffxApiGetResourceDX12(nullptr);
    d.output = ffxApiGetResourceDX12(out, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
    d.jitterOffset = { 0.0f, 0.0f };
    d.motionVectorScale = { 0.0f, 0.0f };
    d.renderSize = { work_w_, work_h_ };
    d.upscaleSize = { out_w_, out_h_ };
    d.enableSharpening = false;
    d.sharpness = 0.0f;
    d.frameTimeDelta = frame_ms > 0.0f ? frame_ms : 16.6f;
    d.preExposure = 1.0f;
    d.reset = reset;
    d.cameraNear = 0.1f;
    d.cameraFar = 1000.0f;
    d.cameraFovAngleVertical = 1.0f;
    d.viewSpaceToMetersFactor = 1.0f;

    const auto dispatch_fn = reinterpret_cast<DispatchFn>(api_.dispatch);
    const ffxReturnCode_t rc = dispatch_fn(AsCtx(&ctx_up_.handle), &d.header);
    if (rc != FFX_API_RETURN_OK) {
        why = "ffxDispatch (upscale) returned " + std::to_string(static_cast<int>(rc));
        return false;
    }
    return true;
}

}  // namespace amd_fsr
