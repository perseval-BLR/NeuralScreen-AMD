// A standalone check of the FidelityFX bridge: load the upscaler, create both
// contexts, dispatch, and prove pixels actually move.
//
// This exists because the bridge is the one piece of the AMD path that can be
// verified on a machine WITHOUT a Radeon. The neural runtime cannot: it needs
// HIP, which ships with AMD's driver. But FSR itself is cross-vendor - the
// upscaler runs on any D3D12 device - so the thing the runtime attaches to is
// testable here, and if this fails there is no point shipping the rest.
//
// Three stages, each answering one question:
//
//   1. THE BRIDGE   - the DLL loads, both contexts build, both dispatches are
//                     accepted on a real D3D12 device.
//   2. THE STATES   - the same dispatch with the output surface created
//                     readable and writable. This is an A/B on a hypothesis
//                     that was believed and had to be tested: a surface created
//                     in the wrong state was going to be blamed for a black
//                     picture. It is not the cause - both cases write every
//                     pixel - and the measurement is kept so nobody re-guesses
//                     it from the code.
//   3. THE CHAIN    - THE HOST'S OWN CONVERSION SHADER, then the dispatch, with
//                     the result read back and counted at each step. This is
//                     the stage that matters for "the picture is black while
//                     everything reports healthy": the engine's log says
//                     `encoded mean 0.000`, which means the frame it was handed
//                     was black. The frame it is handed is the OUTPUT of the
//                     dispatch, and that dispatch reads what our conversion
//                     shader wrote. So: if the conversion output is black, that
//                     is our bug and it is reproducible on any GPU. If the
//                     conversion output carries pixels and the dispatch's output
//                     does not, the fault is in the hand-off. Either answer is
//                     worth more than another round of reading the code.
//
// Run:  native\probe_fsr.exe
// amd_fidelityfx_upscaler_dx12.dll must sit beside the exe.

#include <windows.h>
#include <d3d12.h>
#include <d3dcompiler.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "../native/amd/amd_fsr.h"
#include "../native/amd/amd_shaders.h"

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3dcompiler.lib")

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

UINT Align256(UINT v) { return (v + 255u) & ~255u; }

ID3D12Resource *Tex(ID3D12Device *dev, UINT w, UINT h, DXGI_FORMAT f,
                    D3D12_RESOURCE_STATES state) {
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w; rd.Height = h; rd.DepthOrArraySize = 1; rd.MipLevels = 1;
    rd.Format = f; rd.SampleDesc.Count = 1;
    rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
               D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    ID3D12Resource *t = nullptr;
    dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, state,
                                 nullptr, __uuidof(ID3D12Resource), (void **)&t);
    return t;
}

void Barrier(ID3D12GraphicsCommandList *cl, ID3D12Resource *r,
             D3D12_RESOURCE_STATES from, D3D12_RESOURCE_STATES to) {
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r;
    b.Transition.StateBefore = from;
    b.Transition.StateAfter = to;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cl->ResourceBarrier(1, &b);
}

bool SimpleList(ID3D12Device *dev, ID3D12CommandAllocator **alloc,
                ID3D12GraphicsCommandList **cl) {
    if (FAILED(dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                           __uuidof(ID3D12CommandAllocator),
                                           (void **)alloc))) return false;
    return SUCCEEDED(dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT,
                                            *alloc, nullptr,
                                            __uuidof(ID3D12GraphicsCommandList),
                                            (void **)cl));
}

bool SubmitAndWait(ID3D12Device *dev, ID3D12GraphicsCommandList *cl, std::string &why) {
    if (FAILED(cl->Close())) { why = "list close failed"; return false; }
    ID3D12CommandQueue *q = nullptr;
    D3D12_COMMAND_QUEUE_DESC qd{};
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void **)&q))) {
        why = "queue failed"; return false;
    }
    ID3D12Fence *fence = nullptr;
    dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void **)&fence);
    HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    ID3D12CommandList *lists[] = { cl };
    q->ExecuteCommandLists(1, lists);
    q->Signal(fence, 1);
    if (fence->GetCompletedValue() < 1) {
        fence->SetEventOnCompletion(1, ev);
        if (WaitForSingleObject(ev, 8000) != WAIT_OBJECT_0) { why = "GPU wait timed out"; }
    }
    CloseHandle(ev); fence->Release(); q->Release();
    return why.empty();
}

// Fills `dst` with a known non-black pattern so "did anything arrive" is
// answerable by counting. bpp is 4 for BGRA8, 8 for RGBA16F.
bool UploadPattern(ID3D12Device *dev, ID3D12GraphicsCommandList *cl,
                   ID3D12Resource *dst, DXGI_FORMAT fmt, UINT w, UINT h,
                   const uint8_t *pixel, UINT bpp, std::string &why,
                   ID3D12Resource **upload_out) {
    const UINT pitch = Align256(w * bpp);
    const UINT64 bytes = (UINT64)pitch * h;

    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC bd{};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = bytes; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.Format = DXGI_FORMAT_UNKNOWN; bd.SampleDesc.Count = 1;
    bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *up = nullptr;
    if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &bd,
                                            D3D12_RESOURCE_STATE_GENERIC_READ,
                                            nullptr, __uuidof(ID3D12Resource),
                                            (void **)&up))) {
        why = "upload buffer failed";
        return false;
    }
    uint8_t *p = nullptr;
    D3D12_RANGE none{0, 0};
    if (FAILED(up->Map(0, &none, (void **)&p))) { up->Release(); why = "map failed"; return false; }
    for (UINT y = 0; y < h; ++y) {
        uint8_t *row = p + (size_t)y * pitch;
        for (UINT x = 0; x < w; ++x) memcpy(row + (size_t)x * bpp, pixel, bpp);
    }
    up->Unmap(0, nullptr);

    D3D12_TEXTURE_COPY_LOCATION d{}, s{};
    d.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    d.pResource = dst; d.SubresourceIndex = 0;
    s.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    s.pResource = up;
    s.PlacedFootprint.Offset = 0;
    s.PlacedFootprint.Footprint.Format = fmt;
    s.PlacedFootprint.Footprint.Width = w;
    s.PlacedFootprint.Footprint.Height = h;
    s.PlacedFootprint.Footprint.Depth = 1;
    s.PlacedFootprint.Footprint.RowPitch = pitch;
    cl->CopyTextureRegion(&d, 0, 0, 0, &s, nullptr);

    *upload_out = up;
    return true;
}

// Counts pixels that are not all-zero, by copying back and inspecting.
int NonBlackPixels(ID3D12Device *dev, ID3D12GraphicsCommandList *cl,
                   ID3D12Resource *src, D3D12_RESOURCE_STATES src_state,
                   DXGI_FORMAT fmt, UINT w, UINT h, UINT bpp, std::string &why) {
    const UINT pitch = Align256(w * bpp);
    const UINT64 bytes = (UINT64)pitch * h;

    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd{};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = bytes; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.Format = DXGI_FORMAT_UNKNOWN; bd.SampleDesc.Count = 1;
    bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *rb = nullptr;
    if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &bd,
                                            D3D12_RESOURCE_STATE_COPY_DEST,
                                            nullptr, __uuidof(ID3D12Resource),
                                            (void **)&rb))) {
        why = "readback buffer failed";
        return -1;
    }

    Barrier(cl, src, src_state, D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION d{}, s{};
    d.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    d.pResource = rb;
    d.PlacedFootprint.Offset = 0;
    d.PlacedFootprint.Footprint.Format = fmt;
    d.PlacedFootprint.Footprint.Width = w;
    d.PlacedFootprint.Footprint.Height = h;
    d.PlacedFootprint.Footprint.Depth = 1;
    d.PlacedFootprint.Footprint.RowPitch = pitch;
    s.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    s.pResource = src; s.SubresourceIndex = 0;
    cl->CopyTextureRegion(&d, 0, 0, 0, &s, nullptr);
    Barrier(cl, src, D3D12_RESOURCE_STATE_COPY_SOURCE, src_state);

    std::string err;
    if (!SubmitAndWait(dev, cl, err)) { rb->Release(); why = err; return -1; }

    int non_black = 0;
    uint8_t *p = nullptr;
    D3D12_RANGE all{0, (SIZE_T)bytes};
    if (SUCCEEDED(rb->Map(0, &all, (void **)&p))) {
        for (UINT y = 0; y < h; ++y) {
            const uint8_t *row = p + (size_t)y * pitch;
            for (UINT x = 0; x < w; ++x) {
                const uint8_t *px = row + (size_t)x * bpp;
                bool any = false;
                for (UINT c = 0; c < bpp; ++c) if (px[c]) { any = true; break; }
                if (any) ++non_black;
            }
        }
        D3D12_RANGE none{0, 0};
        rb->Unmap(0, &none);
    } else {
        why = "readback map failed";
        non_black = -1;
    }
    rb->Release();
    return non_black;
}

// A root signature matching the host's own binding layout: one table holding
// two SRVs (t0, t1) and one UAV (u0), plus four 32-bit constants in b0. The
// extra SRV slot is there because the host writes three descriptors per slot
// and the layout has to agree with the shaders it already compiles.
bool MakeRootSignature(ID3D12Device *dev, ID3D12RootSignature **out) {
    D3D12_DESCRIPTOR_RANGE ranges[2]{};
    ranges[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    ranges[0].NumDescriptors = 2;
    ranges[0].BaseShaderRegister = 0;
    ranges[0].OffsetInDescriptorsFromTableStart = 0;
    ranges[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    ranges[1].NumDescriptors = 1;
    ranges[1].BaseShaderRegister = 0;
    ranges[1].OffsetInDescriptorsFromTableStart = 2;

    D3D12_ROOT_PARAMETER params[2]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable.NumDescriptorRanges = 2;
    params[0].DescriptorTable.pDescriptorRanges = ranges;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[1].Constants.ShaderRegister = 0;
    params[1].Constants.Num32BitValues = 4;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_ROOT_SIGNATURE_DESC rs{};
    rs.NumParameters = 2;
    rs.pParameters = params;
    rs.Flags = D3D12_ROOT_SIGNATURE_FLAG_NONE;

    ID3DBlob *blob = nullptr, *errb = nullptr;
    if (FAILED(D3D12SerializeRootSignature(&rs, D3D_ROOT_SIGNATURE_VERSION_1,
                                           &blob, &errb))) {
        if (errb) errb->Release();
        return false;
    }
    const HRESULT hr = dev->CreateRootSignature(0, blob->GetBufferPointer(),
                                                blob->GetBufferSize(),
                                                __uuidof(ID3D12RootSignature),
                                                (void **)out);
    blob->Release();
    if (errb) errb->Release();
    return SUCCEEDED(hr);
}

// Compiles one of the host's own HLSL strings - the same source the worker
// compiles, so a bug in it shows up here.
bool MakePso(ID3D12Device *dev, ID3D12RootSignature *rs, const char *hlsl,
             bool srgb, ID3D12PipelineState **out, std::string &why) {
    D3D_SHADER_MACRO macros[] = { { "SRGB", srgb ? "1" : "0" }, { nullptr, nullptr } };
    ID3DBlob *code = nullptr, *errb = nullptr;
    const HRESULT hr = D3DCompile(hlsl, strlen(hlsl), "host_shader.hlsl",
                                  macros, nullptr, "CSMain", "cs_5_0",
                                  D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &code, &errb);
    if (FAILED(hr)) {
        why = errb ? (const char *)errb->GetBufferPointer() : "compile failed";
        if (code) code->Release();
        if (errb) errb->Release();
        return false;
    }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd{};
    pd.pRootSignature = rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer();
    pd.CS.BytecodeLength = code->GetBufferSize();
    const HRESULT h2 = dev->CreateComputePipelineState(&pd,
                                                       __uuidof(ID3D12PipelineState),
                                                       (void **)out);
    code->Release();
    if (errb) errb->Release();
    if (FAILED(h2)) { why = "CreateComputePipelineState failed"; return false; }
    return true;
}

}  // namespace

int main() {
    printf("probe_fsr - the FidelityFX bridge, checked without a Radeon\n\n");

    std::string why;
    amd_fsr::Upscaler fsr;

    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe);
    const size_t slash = dir.find_last_of(L'\\');
    if (slash != std::wstring::npos) dir.resize(slash);

    // ---- 1. the bridge ----------------------------------------------------
    printf("1. the bridge\n");
    const bool loaded = fsr.Load(dir, why);
    Check(loaded, "the upscaler DLL loads and exposes the FFX entries");
    if (!loaded) {
        printf("\n  %s\n", why.c_str());
        printf("\nRESULT: 1 check(s) failed - the DLL must sit beside this exe\n");
        return 1;
    }

    ID3D12Device *dev = MakeDevice();
    Check(dev != nullptr, "a D3D12 device exists to host the upscaler");
    if (dev == nullptr) return 1;

    const UINT work_w = 640, work_h = 360;   // the network extent
    const UINT out_w = 960, out_h = 540;     // the display extent
    Check(fsr.CreateContexts(dev, work_w, work_h, out_w, out_h, why),
          "both FSR contexts are created against that device");
    if (!fsr.ContextsReady()) {
        printf("\n  %s\n", why.c_str());
        printf("\nRESULT: the bridge could not build its contexts\n");
        return 1;
    }
    Check(fsr.Upscaling(), "work != display is reported as upscaling");

    ID3D12Resource *depth = Tex(dev, work_w, work_h, DXGI_FORMAT_R32_FLOAT,
                                D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    ID3D12Resource *motion = Tex(dev, work_w, work_h, DXGI_FORMAT_R16G16_FLOAT,
                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    ID3D12Resource *out = Tex(dev, out_w, out_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                              D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    Check(depth && motion && out, "the dispatch surfaces exist");

    // ---- 2. the state A/B -------------------------------------------------
    // A hypothesis that was believed and had to be measured: the output surface
    // was created in a readable state, and that was going to be blamed for the
    // black picture. It is not the cause.
    printf("\n2. the output surface's creation state (an A/B)\n");
    const D3D12_RESOURCE_STATES kStates[2] = {
        D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,   // A - created readable
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS,            // B - created writable
    };
    const char *kNames[2] = { "created readable", "created writable" };
    int state_non_black[2] = { -1, -1 };
    bool state_dispatch[2] = { false, false };

    for (int k = 0; k < 2; ++k) {
        ID3D12Resource *net = Tex(dev, work_w, work_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                                  kStates[k]);
        if (net == nullptr) { printf("  net creation failed for case %d\n", k); continue; }

        ID3D12CommandAllocator *alloc = nullptr;
        ID3D12GraphicsCommandList *cl = nullptr;
        SimpleList(dev, &alloc, &cl);

        ID3D12Resource *upload = nullptr;
        std::string why2;
        // A known non-black input, so a zero result means the dispatch wrote
        // nothing rather than "the input was black anyway".
        const uint8_t px[8] = { 0x00, 0x38, 0x00, 0x34, 0x00, 0x30, 0x00, 0x3C };
        if (UploadPattern(dev, cl, depth, DXGI_FORMAT_R32_FLOAT, work_w, work_h,
                          px, 4, why2, &upload) &&
            UploadPattern(dev, cl, motion, DXGI_FORMAT_R16G16_FLOAT, work_w, work_h,
                          px, 8, why2, &upload)) {
            state_dispatch[k] = fsr.DispatchNet(cl, motion, depth, motion, net,
                                                16.6f, false, why2);
            if (state_dispatch[k]) {
                state_non_black[k] = NonBlackPixels(
                    dev, cl, net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                    DXGI_FORMAT_R16G16B16A16_FLOAT, work_w, work_h, 8, why2);
            } else {
                printf("  dispatch refused for case %d: %s\n", k, why2.c_str());
            }
        } else {
            printf("  upload failed for case %d: %s\n", k, why2.c_str());
        }

        if (upload) upload->Release();
        if (cl) cl->Release();
        if (alloc) alloc->Release();
        net->Release();
        printf("  net %-20s dispatched=%-3s non-black pixels=%d\n",
               kNames[k], state_dispatch[k] ? "yes" : "no", state_non_black[k]);
    }

    // ---- 3. the chain: the host's conversion, then the dispatch -----------
    // `encoded mean 0.000` in the engine's log means the frame it received was
    // black. That frame is the dispatch's output, and the dispatch reads what
    // the host's conversion shader wrote. So the chain is measured in two
    // steps, and the first one is the one nobody has checked.
    printf("\n3. the chain: the host's conversion shader, then the dispatch\n");

    ID3D12RootSignature *rs = nullptr;
    std::string why3;
    if (!MakeRootSignature(dev, &rs)) {
        printf("  root signature failed\n");
        return 1;
    }
    ID3D12PipelineState *pso_in = nullptr;
    const bool have_pso = MakePso(dev, rs, kAmdInHlsl, true, &pso_in, why3);
    Check(have_pso, "the host's conversion shader compiles (sRGB on)");
    if (!have_pso) printf("  %s\n", why3.c_str());

    int conv_non_black = -1, net_non_black = -1;
    if (have_pso) {
        // A desktop-like sRGB frame: mid-grey with a warm patch. Never black.
        const uint8_t bgra[4] = { 200, 120, 80, 255 };

        ID3D12Resource *color = Tex(dev, work_w, work_h, DXGI_FORMAT_R8G8B8A8_UNORM,
                                   D3D12_RESOURCE_STATE_COPY_DEST);
        // The host's own creation states: fsr_in and net readable.
        ID3D12Resource *fsr_in = Tex(dev, work_w, work_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                                     D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        ID3D12Resource *net = Tex(dev, work_w, work_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                                  D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);

        ID3D12DescriptorHeap *heap = nullptr;
        D3D12_DESCRIPTOR_HEAP_DESC hd{};
        hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
        hd.NumDescriptors = 8;
        hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap), (void **)&heap);
        const UINT stride = dev->GetDescriptorHandleIncrementSize(
            D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
        D3D12_CPU_DESCRIPTOR_HANDLE cpu0 = heap->GetCPUDescriptorHandleForHeapStart();
        D3D12_GPU_DESCRIPTOR_HANDLE gpu0 = heap->GetGPUDescriptorHandleForHeapStart();

        D3D12_SHADER_RESOURCE_VIEW_DESC sd{};
        sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        sd.Texture2D.MipLevels = 1;
        D3D12_UNORDERED_ACCESS_VIEW_DESC ud{};
        ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;

        ID3D12CommandAllocator *alloc = nullptr;
        ID3D12GraphicsCommandList *cl = nullptr;
        SimpleList(dev, &alloc, &cl);

        ID3D12Resource *upload = nullptr;
        std::string why4;
        bool ok = UploadPattern(dev, cl, color, DXGI_FORMAT_R8G8B8A8_UNORM,
                                work_w, work_h, bgra, 4, why4, &upload);

        if (ok) {
            Barrier(cl, color, D3D12_RESOURCE_STATE_COPY_DEST,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
            // The host's descriptors for its conversion pass: colour in, and
            // the fp16 target out.
            sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
            dev->CreateShaderResourceView(color, &sd, cpu0);
            dev->CreateShaderResourceView(color, &sd, {cpu0.ptr + stride});
            sd.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
            dev->CreateShaderResourceView(fsr_in, &sd, {cpu0.ptr + 2 * stride});
            ud.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
            dev->CreateUnorderedAccessView(fsr_in, nullptr, &ud, {cpu0.ptr + stride});

            Barrier(cl, fsr_in, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            ID3D12DescriptorHeap *heaps[] = { heap };
            cl->SetDescriptorHeaps(1, heaps);
            cl->SetComputeRootSignature(rs);
            cl->SetPipelineState(pso_in);
            cl->SetComputeRootDescriptorTable(0, gpu0);
            const UINT dims[4] = { work_w, work_h, work_w, work_h };
            cl->SetComputeRoot32BitConstants(1, 4, dims, 0);
            cl->Dispatch((work_w + 7) / 8, (work_h + 7) / 8, 1);
            Barrier(cl, fsr_in, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);

            conv_non_black = NonBlackPixels(dev, cl, fsr_in,
                D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                DXGI_FORMAT_R16G16B16A16_FLOAT, work_w, work_h, 8, why4);
            printf("  the conversion's output (what the dispatch reads): "
                   "non-black pixels=%d\n", conv_non_black);
        } else {
            printf("  chain setup failed: %s\n", why4.c_str());
        }

        // And the second step: dispatch on that converted frame, read the
        // output back. This is the frame the engine takes its colour from.
        if (conv_non_black > 0) {
            SimpleList(dev, &alloc, &cl);
            std::string why5;
            if (fsr.DispatchNet(cl, fsr_in, depth, motion, net, 16.6f, false, why5)) {
                net_non_black = NonBlackPixels(dev, cl, net,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                    DXGI_FORMAT_R16G16B16A16_FLOAT, work_w, work_h, 8, why5);
                printf("  the dispatch's output (what the engine edits):   "
                       "non-black pixels=%d\n", net_non_black);
            } else {
                printf("  the dispatch refused: %s\n", why5.c_str());
            }
            if (cl) cl->Release();
            if (alloc) alloc->Release();
        }

        if (upload) upload->Release();
        if (heap) heap->Release();
        if (color) color->Release();
        if (fsr_in) fsr_in->Release();
        if (net) net->Release();
        if (pso_in) pso_in->Release();
    }
    if (rs) rs->Release();

    // ---- verdict ----------------------------------------------------------
    printf("\n");
    Check(state_dispatch[0] && state_dispatch[1],
          "the dispatch is accepted in both creation states");
    Check(state_non_black[1] > 0,
          "the output receives pixels when created writable");
    Check(conv_non_black > 0,
          "THE HOST'S CONVERSION PRODUCES A NON-BLACK FRAME");
    Check(net_non_black > 0,
          "the dispatch's output carries pixels (the engine's input)");

    for (ID3D12Resource *r : { depth, motion, out }) if (r) r->Release();
    dev->Release();

    printf("\n");
    if (g_failures == 0) {
        printf("RESULT: the bridge works - the upscaler loads, both contexts "
               "build, both dispatches are accepted, and every stage of the "
               "frame chain carries pixels\n");
        return 0;
    }
    printf("RESULT: %d check(s) failed - see above for which stage is black\n",
           g_failures);
    return 1;
}
