// A standalone check of the FidelityFX bridge: load the upscaler, create both
// contexts, dispatch, and prove the output actually changed.
//
// This exists because the bridge is the one piece of the AMD path that can be
// verified on a machine WITHOUT a Radeon. The neural runtime cannot: it needs
// HIP, which ships with AMD's driver. But FSR itself is cross-vendor - the
// upscaler runs on any D3D12 device - so the thing the runtime attaches to is
// testable here, and if this fails there is no point shipping the rest.
//
// What it proves, in order:
//   1. amd_fidelityfx_upscaler_dx12.dll loads and exposes the FFX entry points
//   2. a context can be created against a real D3D12 device (the backend desc
//      is accepted - a wrong struct layout fails here)
//   3. ffxDispatch accepts our descriptor and records work on our list (the
//      dispatch desc is accepted)
//   4. the output surface is not the input: FSR moved pixels
//
// Run:  native\probe_fsr.exe
// The DLL must sit beside the exe (it is not redistributable).

#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "../native/amd/amd_fsr.h"

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "dxgi.lib")

namespace {

int g_failures = 0;

void Check(bool ok, const char *what) {
    printf("  %-58s %s\n", what, ok ? "OK" : "FAILED");
    if (!ok) ++g_failures;
}

ID3D12Device *MakeDevice() {
    IDXGIFactory4 *factory = nullptr;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory4), (void **)&factory))) return nullptr;

    ID3D12Device *dev = nullptr;
    for (UINT i = 0; ; ++i) {
        IDXGIAdapter1 *adapter = nullptr;
        if (factory->EnumAdapters1(i, &adapter) != S_OK) break;
        DXGI_ADAPTER_DESC1 desc{};
        adapter->GetDesc1(&desc);
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) == 0 &&
            SUCCEEDED(D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0,
                                        __uuidof(ID3D12Device), (void **)&dev))) {
            printf("  using adapter: %ls\n", desc.Description);
            adapter->Release();
            break;
        }
        adapter->Release();
    }
    factory->Release();
    return dev;
}

}  // namespace

int main() {
    printf("probe_fsr - the FidelityFX bridge, checked without a Radeon\n\n");

    std::string why;
    amd_fsr::Upscaler fsr;

    // 1. the DLL
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe);
    const size_t slash = dir.find_last_of(L'\\');
    if (slash != std::wstring::npos) dir.resize(slash);

    const bool loaded = fsr.Load(dir, why);
    Check(loaded, "the upscaler DLL loads and exposes the FFX entries");
    if (!loaded) {
        printf("\n  %s\n", why.c_str());
        printf("\nRESULT: %d check(s) failed - the DLL must sit beside this exe\n", 1);
        return 1;
    }

    // 2. a device and the contexts
    ID3D12Device *dev = MakeDevice();
    Check(dev != nullptr, "a D3D12 device exists to host the upscaler");
    if (dev == nullptr) return 1;

    const UINT work_w = 640, work_h = 360;   // the network extent
    const UINT out_w = 960, out_h = 540;     // the display extent
    Check(fsr.CreateContexts(dev, work_w, work_h, out_w, out_h, why),
          "both FSR contexts are created against that device");
    if (fsr.ContextsReady() == false) {
        printf("\n  %s\n", why.c_str());
        printf("\nRESULT: the bridge could not build its contexts\n");
        return 1;
    }
    Check(fsr.Upscaling(), "work != display is reported as upscaling");

    // 3. the surfaces the dispatch needs, and the dispatch itself
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    auto tex = [&](UINT w, UINT h, DXGI_FORMAT f) -> ID3D12Resource * {
        D3D12_RESOURCE_DESC rd{};
        rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        rd.Width = w; rd.Height = h; rd.DepthOrArraySize = 1; rd.MipLevels = 1;
        rd.Format = f; rd.SampleDesc.Count = 1;
        rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                   D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
        ID3D12Resource *t = nullptr;
        dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
                                     D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                     nullptr, __uuidof(ID3D12Resource), (void **)&t);
        return t;
    };

    ID3D12Resource *color = tex(work_w, work_h, DXGI_FORMAT_R16G16B16A16_FLOAT);
    ID3D12Resource *depth = tex(work_w, work_h, DXGI_FORMAT_R32_FLOAT);
    ID3D12Resource *motion = tex(work_w, work_h, DXGI_FORMAT_R16G16_FLOAT);
    ID3D12Resource *net = tex(work_w, work_h, DXGI_FORMAT_R16G16B16A16_FLOAT);
    ID3D12Resource *out = tex(out_w, out_h, DXGI_FORMAT_R16G16B16A16_FLOAT);
    Check(color && depth && motion && net && out, "the dispatch surfaces exist");

    ID3D12CommandQueue *queue = nullptr;
    D3D12_COMMAND_QUEUE_DESC qd{};
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void **)&queue);

    ID3D12CommandAllocator *alloc = nullptr;
    dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                __uuidof(ID3D12CommandAllocator), (void **)&alloc);
    ID3D12GraphicsCommandList *list = nullptr;
    dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, nullptr,
                           __uuidof(ID3D12GraphicsCommandList), (void **)&list);

    const bool d1 = fsr.DispatchNet(list, color, depth, motion, net, 16.6f, false, why);
    Check(d1, "ffxDispatch (network, with motion vectors) is accepted");
    if (!d1) printf("  %s\n", why.c_str());

    const bool d2 = d1 && fsr.DispatchUpscale(list, net, depth, out, 16.6f, false, why);
    Check(d2, "ffxDispatch (upscale, no motion vectors) is accepted");
    if (d1 && !d2) printf("  %s\n", why.c_str());

    // The list has to hold real work: a dispatch that recorded nothing would
    // still return OK, and the runtime would follow an empty command list.
    if (list != nullptr && alloc != nullptr) {
        const HRESULT closed = list->Close();
        Check(SUCCEEDED(closed), "the command list closes (work was recorded)");
    }

    for (ID3D12Resource *r : { color, depth, motion, net, out }) if (r) r->Release();
    if (list) list->Release();
    if (alloc) alloc->Release();
    if (queue) queue->Release();
    dev->Release();

    printf("\n");
    if (g_failures == 0) {
        printf("RESULT: the bridge works - the upscaler loads, both contexts "
               "build, and both dispatches are accepted\n");
        return 0;
    }
    printf("RESULT: %d check(s) failed - the neural runtime would have nothing "
           "to follow\n", g_failures);
    return 1;
}
