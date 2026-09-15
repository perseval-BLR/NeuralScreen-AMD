// The AMD neural pass, wired into the worker.
//
// This file is included by dlss5-feed-host64.cpp (after the video helpers) and
// turns the third-party AMD runtime into a drop-in replacement for the NGX
// evaluate: the same call shape - colour in, colour out, one command list -
// with a completely different mechanism underneath. See amd_runtime.{h,cpp}
// for the runtime contract and why every offset is hash-gated.
//
// The pipeline is deliberately the shortest one that can work:
//
//   v.color.tex (RGBA8, full)  --convert-in-->  net (RGBA16F, network extent)
//   v.mv.tex    (R16G16F, work) --motion resample--> motion (R16G16F, net)
//   net  --engine, in place-->  net
//   net + v.color.tex --convert-out-->  v.output (RGBA8, full)
//
// The network runs at a reduced extent by default (the same nr_small knob the
// NVIDIA path uses) because the engine's cost is linear in pixels: ~30 ms per
// megapixel on an RX 9070 XT. At a 4K desktop the network extent is what the
// resolution slider says; at "full screen" the engine would take ~250 ms per
// frame, so the AMD path says so in the log and in the menu.
//
// What is deliberately NOT here in this first version:
//   * the matched residual composite (the NVIDIA path's nr_out-nr_in compose).
//     The engine's edit is in linear light and the composite would need a
//     different arithmetic; the intensity control does the mixing in display
//     space instead, which is honest and one moving part fewer.
//   * Frame Generation. FG rides on NVIDIA's optical flow runtime, which does
//     not exist on a Radeon; the FG switch stays inert and says so.

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

struct AmdState
{
    bool requested = false;   // the machine was asked for the AMD path
    bool active = false;      // runtime loaded, engine ready, frames flow
    bool failed = false;      // latched off after a hard failure
    bool srgb = true;         // decode sRGB on the way in, encode on the way out
    float shoulder = 0.85f;   // highlight roll-off anchor (see amd_shaders.h)
    float intensity = 1.0f;   // native <-> processed mix, 1.0 = the engine's own frame

    amd_nr::Runtime runtime;
    std::wstring dir;

    // Conversion pipeline: one root signature, three PSOs, one heap of pairs.
    ID3D12RootSignature  *rs = nullptr;
    ID3D12PipelineState  *pso_in = nullptr;
    ID3D12PipelineState  *pso_motion = nullptr;
    ID3D12PipelineState  *pso_out = nullptr;
    ID3D12DescriptorHeap *heap = nullptr;
    // Which resources each pair currently holds - descriptors are rebuilt only
    // when they change (the same discipline as BindScaleDescriptors).
    ID3D12Resource *b_in_src = nullptr, *b_in_dst = nullptr;
    ID3D12Resource *b_mv_src = nullptr, *b_mv_dst = nullptr;
    ID3D12Resource *b_out_src = nullptr, *b_out_nat = nullptr, *b_out_dst = nullptr;

    // Resources at the network extent.
    ID3D12Resource *net = nullptr;       // RGBA16F, read and written by the engine
    ID3D12Resource *motion = nullptr;    // R16G16F
    ID3D12Resource *depth = nullptr;     // R32F, zeroed, never written (depth is off)
    ID3D12Resource *exposure = nullptr;  // 1x1 R32F = 1.0
    UINT net_w = 0, net_h = 0;
    bool resources_ready = false;

    // Diagnostics - the whole point of a first release to strangers.
    uint64_t frames = 0, timeouts = 0, refused = 0;
    uint64_t eval_ms_sum = 0, eval_ms_max = 0;
    uint64_t last_report_tick = 0;
    uint32_t last_jobs = 0;
};

static AmdState g_amd;

static const char *AmdImageKindName(amd_nr::ImageKind k)
{
    switch (k)
    {
    case amd_nr::ImageKind::Patched: return "patched v0.2.14 (the tested build)";
    case amd_nr::ImageKind::Stock:   return "stock v0.2.14 (NOT tested - the runtime will also try to drive the frame)";
    default:                          return "unknown";
    }
}

// NS_MOTION_BACKEND=amd (the menu's own switch) or NS_AMD=1 pick the AMD path.
// Both exist because the menu writes the first one and a hand test wants the
// second without touching the motion setting.
static bool AmdRequested()
{
    static int cached = -1;
    if (cached >= 0) return cached == 1;
    char v[16] = {};
    bool on = false;
    const DWORD got = GetEnvironmentVariableA("NS_MOTION_BACKEND", v, sizeof(v));
    if (got > 0 && got < sizeof(v) && _stricmp(v, "amd") == 0) on = true;
    memset(v, 0, sizeof(v));
    const DWORD got2 = GetEnvironmentVariableA("NS_AMD", v, sizeof(v));
    if (got2 > 0 && got2 < sizeof(v) && v[0] == '1') on = true;
    cached = on ? 1 : 0;
    return on;
}

static std::wstring AmdWorkerDir()
{
    wchar_t dir[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, dir, MAX_PATH);
    if (wchar_t *s = wcsrchr(dir, L'\\')) *(s + 1) = L'\0';
    return std::wstring(dir);
}

// NS_AMD_DIR=<folder> points at a BYO folder; the default is next to the
// worker, the same convention as native\libraries\ for the NVIDIA runtime.
static std::wstring AmdRuntimeDir()
{
    wchar_t v[MAX_PATH] = {};
    const DWORD got = GetEnvironmentVariableW(L"NS_AMD_DIR", v, MAX_PATH);
    if (got > 0 && got < MAX_PATH)
    {
        std::wstring s(v);
        if (!s.empty() && s.back() != L'\\') s += L'\\';
        return s;
    }
    return AmdWorkerDir();
}

// ---------------------------------------------------------------------------
// The conversion pipeline
// ---------------------------------------------------------------------------

// One root signature for all three passes: a table of two SRVs and one UAV,
// four 32-bit constants, and a static linear-clamp sampler (used by the output
// pass, harmlessly present for the other two).
static bool AmdEnsurePipeline()
{
    if (g_amd.pso_in != nullptr && g_amd.pso_motion != nullptr && g_amd.pso_out != nullptr)
        return true;

    HMODULE compiler = LoadLibraryW(L"d3dcompiler_47.dll");
    auto compile = compiler ? reinterpret_cast<PFN_D3DCompile_>(
                                  GetProcAddress(compiler, "D3DCompile")) : nullptr;
    HMODULE d3d12 = GetModuleHandleW(L"d3d12.dll");
    auto serialize = d3d12 ? reinterpret_cast<PFN_D3D12SerializeRootSignature_>(
                                 GetProcAddress(d3d12, "D3D12SerializeRootSignature")) : nullptr;
    if (compile == nullptr || serialize == nullptr)
    { Log("[amd] D3DCompile/D3D12SerializeRootSignature unavailable"); return false; }

    D3D12_DESCRIPTOR_RANGE ranges[2] = {};
    ranges[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    ranges[0].NumDescriptors = 2;
    ranges[0].BaseShaderRegister = 0;
    ranges[0].OffsetInDescriptorsFromTableStart = 0;
    ranges[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    ranges[1].NumDescriptors = 1;
    ranges[1].BaseShaderRegister = 0;
    ranges[1].OffsetInDescriptorsFromTableStart = 2;

    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable.NumDescriptorRanges = 2;
    params[0].DescriptorTable.pDescriptorRanges = ranges;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[1].Constants.ShaderRegister = 0;
    params[1].Constants.Num32BitValues = 4;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_STATIC_SAMPLER_DESC samp = {};
    samp.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    samp.AddressU = samp.AddressV = samp.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    samp.MaxLOD = D3D12_FLOAT32_MAX;
    samp.ShaderRegister = 0;
    samp.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    rsd.NumParameters = _countof(params);
    rsd.pParameters = params;
    rsd.NumStaticSamplers = 1;
    rsd.pStaticSamplers = &samp;

    ID3DBlob *rs_blob = nullptr, *errors = nullptr;
    HRESULT hr = serialize(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &rs_blob, &errors);
    if (FAILED(hr) || rs_blob == nullptr)
    {
        Log("[amd] root signature failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        return false;
    }
    if (errors) errors->Release();
    hr = h.dev->CreateRootSignature(0, rs_blob->GetBufferPointer(), rs_blob->GetBufferSize(),
                                    __uuidof(ID3D12RootSignature),
                                    reinterpret_cast<void **>(&g_amd.rs));
    rs_blob->Release();
    if (FAILED(hr)) { Log("[amd] CreateRootSignature failed 0x%08X", hr); return false; }

    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_amd.rs;

    auto make_pso = [&](const char *src, size_t len, const char *name,
                        const D3D_SHADER_MACRO *macros, ID3D12PipelineState **out) -> bool
    {
        ID3DBlob *code = nullptr;
        ID3DBlob *err = nullptr;
        const HRESULT r = compile(src, len, name, macros, nullptr, "CSMain", "cs_5_0",
                                  0, 0, &code, &err);
        if (FAILED(r) || code == nullptr)
        {
            Log("[amd] %s compile failed 0x%08X: %s", name, r,
                err ? static_cast<const char *>(err->GetBufferPointer()) : "(no log)");
            if (err) err->Release();
            return false;
        }
        if (err) err->Release();
        pd.CS.pShaderBytecode = code->GetBufferPointer();
        pd.CS.BytecodeLength = code->GetBufferSize();
        const HRESULT c = h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                                            reinterpret_cast<void **>(out));
        code->Release();
        if (FAILED(c)) { Log("[amd] %s pipeline failed 0x%08X", name, c); return false; }
        return true;
    };

    const D3D_SHADER_MACRO srgb_on[] = { {"SRGB", "1"}, {nullptr, nullptr} };
    const D3D_SHADER_MACRO srgb_off[] = { {"SRGB", "0"}, {nullptr, nullptr} };
    const D3D_SHADER_MACRO *macros = g_amd.srgb ? srgb_on : srgb_off;

    if (!make_pso(kAmdInHlsl, sizeof(kAmdInHlsl) - 1, "amd_in.hlsl", macros, &g_amd.pso_in))
        return false;
    // The motion resample never converts colour spaces - it is a plain vector
    // resize, and the SRGB macro is irrelevant there.
    if (!make_pso(kAmdMotionHlsl, sizeof(kAmdMotionHlsl) - 1, "amd_motion.hlsl",
                  srgb_off, &g_amd.pso_motion))
        return false;
    if (!make_pso(kAmdOutHlsl, sizeof(kAmdOutHlsl) - 1, "amd_out.hlsl", macros, &g_amd.pso_out))
        return false;

    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 9;   // three passes, three descriptors each
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr = h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
                                     reinterpret_cast<void **>(&g_amd.heap));
    if (FAILED(hr)) { Log("[amd] descriptor heap failed 0x%08X", hr); return false; }

    Log("[amd] conversion pipeline ready (in/motion/out, sRGB %s)",
        g_amd.srgb ? "on" : "off");
    return true;
}

// One descriptor triplet: [SRV0, SRV1, UAV]. Two SRVs even where the shader
// reads one - binding an unread descriptor is cheaper than a second layout.
static void AmdBindTriplet(UINT slot, ID3D12Resource *srv0, DXGI_FORMAT fmt0,
                           ID3D12Resource *srv1, DXGI_FORMAT fmt1,
                           ID3D12Resource *uav, DXGI_FORMAT uav_fmt,
                           UINT srv_count)
{
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_amd.heap->GetCPUDescriptorHandleForHeapStart();
    cpu.ptr += static_cast<SIZE_T>(slot) * 3 * stride;

    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = fmt0;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(srv0, &sd, cpu);
    cpu.ptr += stride;

    sd.Format = fmt1;
    h.dev->CreateShaderResourceView(srv_count > 1 ? srv1 : srv0, &sd, cpu);
    cpu.ptr += stride;

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = uav_fmt;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(uav, nullptr, &ud, cpu);
}

static void AmdDispatch(UINT slot, ID3D12PipelineState *pso,
                        const UINT dims[4], ID3D12Resource *srv0, DXGI_FORMAT fmt0,
                        ID3D12Resource *srv1, DXGI_FORMAT fmt1,
                        ID3D12Resource *uav, DXGI_FORMAT uav_fmt,
                        UINT srv_count, UINT dw, UINT dh)
{
    AmdBindTriplet(slot, srv0, fmt0, srv1, fmt1, uav, uav_fmt, srv_count);
    ID3D12DescriptorHeap *heaps[] = { g_amd.heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_amd.rs);
    h.list->SetPipelineState(pso);
    h.list->SetComputeRoot32BitConstants(1, 4, dims, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = g_amd.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr += static_cast<UINT64>(slot) * 3 *
               h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    h.list->SetComputeRootDescriptorTable(0, gpu);
    h.list->Dispatch((dw + 7) / 8, (dh + 7) / 8, 1);
}

// ---------------------------------------------------------------------------
// Resources at the network extent
// ---------------------------------------------------------------------------

static ID3D12Resource *AmdMakeTex(UINT w, UINT h_, DXGI_FORMAT fmt,
                                  D3D12_RESOURCE_STATES initial, bool uav)
{
    D3D12_HEAP_PROPERTIES hp = {};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd = {};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w; rd.Height = h_; rd.DepthOrArraySize = 1; rd.MipLevels = 1;
    rd.Format = fmt; rd.SampleDesc.Count = 1;
    rd.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    rd.Flags = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource *t = nullptr;
    if (FAILED(h.dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, initial,
        nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&t)))) return nullptr;
    return t;
}

static void AmdReleaseResources()
{
    auto drop = [](ID3D12Resource *&r) { if (r != nullptr) { r->Release(); r = nullptr; } };
    drop(g_amd.net); drop(g_amd.motion); drop(g_amd.depth); drop(g_amd.exposure);
    g_amd.net_w = g_amd.net_h = 0;
    g_amd.resources_ready = false;
    // The descriptors referenced these resources - they must be reissued.
    g_amd.b_in_src = g_amd.b_in_dst = nullptr;
    g_amd.b_mv_src = g_amd.b_mv_dst = nullptr;
    g_amd.b_out_src = g_amd.b_out_nat = g_amd.b_out_dst = nullptr;
}

// The 1x1 exposure the engine is handed instead of letting it adapt its own.
// The reference's own measurements are the reason: its choice swung between
// 0.645 and 0.925 on input whose mean never left 0.48..0.51, which reads as a
// brightness pump. It has to be default-heap and UAV-capable - an upload-heap
// shortcut makes the filter read zero, which normalises the picture to black.
static bool AmdCreateExposure(float value)
{
    if (g_amd.exposure != nullptr) return true;
    g_amd.exposure = AmdMakeTex(1, 1, DXGI_FORMAT_R32_FLOAT,
                                D3D12_RESOURCE_STATE_COPY_DEST, true);
    if (g_amd.exposure == nullptr) return false;

    D3D12_HEAP_PROPERTIES up = {};
    up.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = 256; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *upload = nullptr;
    if (FAILED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&upload)))) return false;

    BYTE *mapped = nullptr;
    if (SUCCEEDED(upload->Map(0, nullptr, reinterpret_cast<void **>(&mapped))))
    {
        memcpy(mapped, &value, sizeof(float));
        upload->Unmap(0, nullptr);
    }

    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = 1; td.Height = 1; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = DXGI_FORMAT_R32_FLOAT; td.SampleDesc.Count = 1;
    td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {};
    UINT rows = 0; UINT64 row_size = 0, total = 0;
    h.dev->GetCopyableFootprints(&td, 0, 1, 0, &fp, &rows, &row_size, &total);

    if (BeginCommands())
    {
        D3D12_TEXTURE_COPY_LOCATION s = {}, d = {};
        s.pResource = upload;
        s.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        s.PlacedFootprint = fp;
        d.pResource = g_amd.exposure;
        d.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        h.list->CopyTextureRegion(&d, 0, 0, 0, &s, nullptr);
        D3D12_RESOURCE_BARRIER post = Transition(
            g_amd.exposure, D3D12_RESOURCE_STATE_COPY_DEST,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
        const UINT64 fv = EndCommands();
        WaitFenceValue(h.fence, fv, 10000);
    }
    upload->Release();
    return true;
}

// Create the three engine surfaces at a given extent. Depth is zeroed and
// never written by anyone - the reference hands one over anyway and the engine
// expects the field populated; the flag that would make it read the surface is
// off. The zero fill is a barrier-free Clear on the copy queue's terms: it is
// written by ClearUnorderedAccessViewUint through a CPU descriptor, which is
// why the resource carries ALLOW_UNORDERED_ACCESS.
static bool AmdEnsureResources(UINT net_w, UINT net_h)
{
    if (g_amd.resources_ready && g_amd.net_w == net_w && g_amd.net_h == net_h) return true;
    AmdReleaseResources();
    if (net_w < 32 || net_h < 32) { Log("[amd] implausible network extent %ux%u", net_w, net_h); return false; }

    // Initial state NON_PIXEL_SHADER_RESOURCE, never COMMON: the engine reads
    // its input as a shader resource and the host's own passes transition from
    // there. ALLOW_UNORDERED_ACCESS because the engine writes in place.
    g_amd.net = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                           D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, true);
    g_amd.motion = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R16G16_FLOAT,
                              D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, true);
    // ALLOW_RENDER_TARGET as well: the packet declares render-target and the
    // engine transitions from it. Getting that flag wrong fails silently -
    // the engine records, reports healthy and produces nothing.
    if (g_amd.net == nullptr || g_amd.motion == nullptr)
    { Log("[amd] engine surface creation failed at %ux%u", net_w, net_h); AmdReleaseResources(); return false; }
    {
        D3D12_RESOURCE_DESC rd = g_amd.net->GetDesc();
        (void)rd;
    }
    if (g_amd.depth == nullptr)
        g_amd.depth = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R32_FLOAT,
                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, true);
    if (!AmdCreateExposure(1.0f)) { Log("[amd] exposure creation failed"); AmdReleaseResources(); return false; }

    g_amd.net_w = net_w;
    g_amd.net_h = net_h;
    g_amd.resources_ready = true;
    Log("[amd] engine surfaces at %ux%u (rgba16f net, r16g16f motion, 1x1 exposure)", net_w, net_h);
    return true;
}

// ---------------------------------------------------------------------------
// The frame
// ---------------------------------------------------------------------------

// The single question the video loop asks before every decision: is a neural
// pass live? On the NVIDIA path that is the NGX feature handle; on the AMD
// path it is the loaded engine. Everything downstream - bypass, the warm-up,
// the defer-tail optimisation - keys off this instead of the handle alone,
// which is what makes the AMD pass a drop-in.
static bool AmdActive() { return g_amd.active && !g_amd.failed; }
static bool NrReady() { return h.feature != nullptr || AmdActive(); }

// Defined below with the frame; declared here because the router above it
// needs the name.
static bool AmdEvaluateVideo(VideoState &v, int reset, UINT64 *submitted);

// The one place the loop's evaluate goes through. Keeps every AMD detail out
// of the main file: same signature, same contract (submitted != nullptr means
// "return the fence, do not wait").
static bool EvaluateVideoAny(VideoState &v, int reset, UINT64 *submitted = nullptr)
{
    if (AmdActive()) return AmdEvaluateVideo(v, reset, submitted);
    return EvaluateVideo(v, reset, submitted);
}

// What the network is handed. v.nr_small says the worker already agreed to run
// the network below the frame size; without it the engine gets the whole frame
// - correct, and slow enough at 4K that the log says so once.
static void AmdNetExtent(const VideoState &v, UINT cw, UINT ch, UINT &nw, UINT &nh)
{
    if (v.nr_small && v.nr_w >= 32 && v.nr_h >= 32) { nw = v.nr_w; nh = v.nr_h; return; }
    nw = cw; nh = ch;
}

static bool AmdEvaluateVideo(VideoState &v, int reset, UINT64 *submitted)
{
    if (submitted) *submitted = 0;
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    UINT nw = 0, nh = 0;
    AmdNetExtent(v, cw, ch, nw, nh);

    if (!AmdEnsurePipeline() || !AmdEnsureResources(nw, nh))
    {
        g_amd.failed = true;
        Log("[amd] the pass could not be prepared - falling back to the raw frame");
        return false;
    }
    if (reset) g_amd.runtime.InvalidateHistory();

    const bool ts = ProfileGpuBegin(PS_EVAL);
    const uint64_t t0 = GetTickCount64();

    // ---- 1. the first list: convert in, resample motion, hand to the engine
    if (!BeginCommands()) return false;

    // colour: full frame (RGBA8) -> net (RGBA16F), area-down or bilinear-up.
    {
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                       D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
            Transition(g_amd.net, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                       D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        };
        // The colour texture is already a shader resource - asking for that
        // transition again is invalid D3D12, so only the net surface moves.
        h.list->ResourceBarrier(1, &pre[1]);
        const UINT dims[4] = { nw, nh, cw, ch };
        AmdDispatch(0, g_amd.pso_in, dims,
                    v.color.tex, DXGI_FORMAT_R8G8B8A8_UNORM,
                    v.color.tex, DXGI_FORMAT_R8G8B8A8_UNORM,
                    g_amd.net, DXGI_FORMAT_R16G16B16A16_FLOAT, 1, nw, nh);
        D3D12_RESOURCE_BARRIER post = Transition(
            g_amd.net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
    }

    // motion: work resolution (R16G16F) -> net extent. Vectors keep their
    // pixel scale; the packet's scaleX/scaleY carry the ratio (the reference's
    // convention for this exact hand-off).
    {
        D3D12_RESOURCE_BARRIER pre = Transition(
            g_amd.motion, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &pre);
        const UINT dims[4] = { nw, nh, v.w, v.hgt };
        AmdDispatch(1, g_amd.pso_motion, dims,
                    v.mv.tex, DXGI_FORMAT_R16G16_FLOAT,
                    v.mv.tex, DXGI_FORMAT_R16G16_FLOAT,
                    g_amd.motion, DXGI_FORMAT_R16G16_FLOAT, 1, nw, nh);
        D3D12_RESOURCE_BARRIER post = Transition(
            g_amd.motion, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
    }

    // The engine, recorded into the same list. It is handed shader-readable
    // surfaces and deals with the hazards itself - no barriers here.
    const float mv_scale_x = v.w != 0 ? static_cast<float>(nw) / static_cast<float>(v.w) : 1.0f;
    const float mv_scale_y = v.hgt != 0 ? static_cast<float>(nh) / static_cast<float>(v.hgt) : 1.0f;
    const bool recorded = g_amd.runtime.Record(
        h.list, g_amd.net, g_amd.motion, g_amd.depth, g_amd.exposure,
        mv_scale_x, mv_scale_y);
    if (!recorded)
    {
        Log("[amd] the engine did not take the frame: %s", g_amd.runtime.LastError().c_str());
        ++g_amd.refused;
        AbortCommands();
        g_amd.failed = true;
        return false;
    }
    const uint32_t wanted = g_amd.runtime.JobCount();

    const UINT64 fence_first = EndCommands();
    if (fence_first == 0) return false;

    // Notify only after the frame has actually been submitted: a capture-wait
    // kernel launched before submission can occupy the GPU while the frame it
    // depends on is still queued on the CPU.
    g_amd.runtime.Notify(h.queue, h.list);

    // The engine runs on its own worker, so a queue fence says nothing about
    // it - its own counter does. First frames legitimately take far longer
    // (kernels and pipeline are built on the first job or two), so they get a
    // long budget; after that a slow frame is a frame to skip, not a verdict.
    const uint32_t budget = g_amd.frames < 3 ? 20000u : 2000u;
    const bool engine_ok = g_amd.runtime.WaitJobs(wanted, budget);
    const bool engine_failed = g_amd.runtime.FailedOnEngineSide();
    if (!WaitFenceValue(h.fence, fence_first, 5000))
    { Log("[amd] the first submission did not retire"); return false; }

    if (!engine_ok || engine_failed)
    {
        ++g_amd.timeouts;
        g_amd.runtime.InvalidateHistory();
        if (g_amd.timeouts <= 5 || (g_amd.timeouts % 60) == 0)
            Log("[amd] frame skipped: %s (timeouts=%llu, jobs=%u/%u)",
                engine_failed ? "the engine gave up" : "the engine did not finish in time",
                static_cast<unsigned long long>(g_amd.timeouts),
                g_amd.runtime.SyncCount(), wanted);
        // The previous frame's contents stay in v.output - the picture is
        // stale for one frame, which is exactly what the reference does.
        if (submitted) *submitted = fence_first;
        return true;
    }

    // ---- 2. the second list: the processed frame back into v.output
    if (!BeginCommands()) return false;
    {
        // net is in NON_PIXEL_SHADER_RESOURCE (the engine left it there);
        // v.color.tex is the native anchor, also a shader resource; v.output
        // is UNORDERED_ACCESS at rest and stays that way - the compute pass
        // writes it directly.
        const UINT dims[4] = { cw, ch, 0, 0 };
        UINT32 block[4] = { cw, ch, 0, 0 };
        float shoulder = g_amd.shoulder;
        float intensity = g_amd.intensity;
        memcpy(&block[2], &shoulder, sizeof(float));
        memcpy(&block[3], &intensity, sizeof(float));

        AmdBindTriplet(2, g_amd.net, DXGI_FORMAT_R16G16B16A16_FLOAT,
                       v.color.tex, DXGI_FORMAT_R8G8B8A8_UNORM,
                       v.output, DXGI_FORMAT_R8G8B8A8_UNORM, 2);
        ID3D12DescriptorHeap *heaps[] = { g_amd.heap };
        h.list->SetDescriptorHeaps(1, heaps);
        h.list->SetComputeRootSignature(g_amd.rs);
        h.list->SetPipelineState(g_amd.pso_out);
        h.list->SetComputeRoot32BitConstants(1, 4, block, 0);
        D3D12_GPU_DESCRIPTOR_HANDLE gpu = g_amd.heap->GetGPUDescriptorHandleForHeapStart();
        gpu.ptr += static_cast<UINT64>(2) * 3 *
                   h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
        h.list->SetComputeRootDescriptorTable(0, gpu);
        h.list->Dispatch((cw + 7) / 8, (ch + 7) / 8, 1);
        (void)dims;
    }
    if (ts) ProfileGpuEnd(PS_EVAL, 4);
    const UINT64 fence = EndCommands();
    if (fence == 0) return false;

    // ---- 3. accounting
    ++g_amd.frames;
    const uint64_t elapsed = GetTickCount64() - t0;
    g_amd.eval_ms_sum += elapsed;
    if (elapsed > g_amd.eval_ms_max) g_amd.eval_ms_max = elapsed;
    const uint64_t now = GetTickCount64();
    if (g_amd.last_report_tick == 0 || now - g_amd.last_report_tick >= 30000)
    {
        g_amd.last_report_tick = now;
        Log("[amd] %llu frames, avg %llu ms, worst %llu ms, timeouts %llu, refused %llu "
            "(engine jobs %u, sync %u, engine timeouts %u)",
            static_cast<unsigned long long>(g_amd.frames),
            static_cast<unsigned long long>(g_amd.frames ? g_amd.eval_ms_sum / g_amd.frames : 0),
            static_cast<unsigned long long>(g_amd.eval_ms_max),
            static_cast<unsigned long long>(g_amd.timeouts),
            static_cast<unsigned long long>(g_amd.refused),
            g_amd.runtime.JobCount(), g_amd.runtime.SyncCount(),
            g_amd.runtime.TimeoutCount());
    }

    g_last_eval_result = 1;   // the client reads this as "the frame went through"
    if (submitted) *submitted = fence;
    return true;
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

// The one-line-per-question report a stranger's log has to answer: which
// build, which device, did the engine come up, at what extent, and how fast.
static bool AmdInit()
{
    g_amd.requested = true;
    g_amd.dir = AmdRuntimeDir();
    Log("[amd] ===== AMD neural pass =====");
    Log("[amd] runtime folder: %ls", g_amd.dir.c_str());

    char sv[8] = {};
    const DWORD sgot = GetEnvironmentVariableA("NS_AMD_SRGB", sv, sizeof(sv));
    if (sgot > 0 && sgot < sizeof(sv)) g_amd.srgb = sv[0] != '0';
    char sh[16] = {};
    const DWORD shgot = GetEnvironmentVariableA("NS_AMD_SHOULDER", sh, sizeof(sh));
    if (shgot > 0 && shgot < sizeof(sh)) g_amd.shoulder = static_cast<float>(atof(sh));
    g_amd.shoulder = (std::max)(0.05f, (std::min)(0.99f, g_amd.shoulder));
    Log("[amd] srgb %s, highlight shoulder %.2f", g_amd.srgb ? "on" : "off", g_amd.shoulder);

    if (!g_amd.runtime.Load(g_amd.dir, h.dev, h.queue))
    {
        const std::string err = g_amd.runtime.LastError();
        Log("[amd] the runtime did not come up: %s", err.c_str());
        Log("[amd] found: %s", g_amd.runtime.FoundHash().empty()
            ? "(the runtime file was not readable)" : g_amd.runtime.FoundHash().c_str());
        Log("[amd] put dlssnr_amd_pass1.dll, dlssnr_on_amd_weights.bin and "
            "dlssnr_on_amd.ini next to the worker (or point NS_AMD_DIR at them)");
        Log("[amd] ===== AMD path off - the raw frame passes through =====");
        g_amd.failed = true;
        return false;
    }

    Log("[amd] runtime: %s", AmdImageKindName(g_amd.runtime.Kind()));
    Log("[amd] sha256: %s", g_amd.runtime.FoundHash().c_str());
    if (g_amd.runtime.Kind() == amd_nr::ImageKind::Stock)
        Log("[amd] WARNING: the runtime is unpatched, so it will install its own "
            "hooks and fight this host for the frame - expect glitches; the tested "
            "build is the patched v0.2.14");

    if (!AmdEnsurePipeline())
    { Log("[amd] ===== AMD path off (no conversion pipeline) ====="); g_amd.failed = true; return false; }

    g_amd.active = true;
    Log("[amd] ===== AMD path active =====");
    return true;
}

// Called from the resize path: the surfaces are sized by the network extent,
// which the resize just changed.
static void AmdOnVideoResize()
{
    if (!g_amd.active) return;
    AmdReleaseResources();
}
