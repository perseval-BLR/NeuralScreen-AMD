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
    //: The card is not a Radeon. A different verdict from `failed`: nothing
    //: is broken and nothing can be fixed, so the menu says "card not
    //: supported" rather than "no neural pass".
    bool unsupported = false;
    bool srgb = true;         // decode sRGB on the way in, encode on the way out
    float shoulder = 0.85f;   // highlight roll-off anchor (see amd_shaders.h)
    float intensity = 1.0f;   // native <-> processed mix, 1.0 = the engine's own frame
    //: The last `Scale` the runtime accepted, so the change is logged once and
    //: not once per frame. -1 = nothing written yet.
    float scale_logged = -1.0f;

    amd_nr::Runtime runtime;
    std::wstring dir;

    // The FidelityFX bridge. Without it the runtime has no dispatch to follow
    // and routes the frame through a backbuffer instead of processing ours -
    // the black picture in the second live report. See amd_fsr.h.
    amd_fsr::Upscaler fsr;
    //: The display-resolution surface FSR upscales into, at work != output.
    ID3D12Resource *up_out = nullptr;
    //: Set once the contexts exist for the current extents.
    bool fsr_ready = false;
    //: Frames the engine actually took (the runtime's own counter, read back
    //: so the log can show that the dispatch route worked).
    uint64_t fsr_frames = 0, fsr_failures = 0;
    //: Whether the frame is ALSO handed to the engine through its packet call.
    //:
    //: Default OFF, and that is the change: the hosts that produce a picture
    //: feed the engine through its packet call, NOT through the FSR dispatch -
    //: they hook Record (r + 0xf600) themselves. That was measured on their
    //: own sources, and it is the opposite of what this comment used to claim.
    //:
    //: What the logs show is the consequence, not the cause: every job is
    //: `job 1 ... history off`, so the engine never sees a sequence and starts
    //: over every frame. THIS route (dispatch-alone) is the untested one.
    //:
    //: Kept as a switch rather than deleted outright: the dispatch route is the
    //: only one this host has measured end to end, and the packet route needs
    //: Record, Notify and Shutdown derived for the loaded build - which is what
    //: tools/amd_offsets_probe.py does not yet do for every release.
    //: NS_AMD_PACKET=1 turns it on for the A/B.
    bool use_packet = false;
    //: The HIP index this host resolved by adapter LUID, or -1 if none matched.
    //: Kept (not thrown away) because the engine-init retry needs it: both
    //: hosts that produce a picture write this index themselves, and a machine
    //: where the engine's own auto match never lands is the case it exists for.
    int hip_index_resolved = -1;
    //: Whether the one repair attempt has been made. Bounded to one per run:
    //: an engine that refuses both the auto match and an explicit index is not
    //: going to accept a third call, and repeating it would only fill the log.
    bool engine_retry_done = false;
    //: Whether the "the engine never came up, so it is not fed" line has been
    //: printed. Once: the fallback is taken every frame and repeating it would
    //: bury the rest of the log.
    bool engine_dead_announced = false;
    //: Last frame's wall time, handed to the FSR dispatch (it uses it for its
    //: temporal accumulation). 16.6 ms until a second frame has been timed.
    float last_frame_ms = 16.6f;
    uint64_t last_frame_tick = 0;

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
    //: The converted frame the FSR dispatch A reads. Separate from `net`
    //: because the engine takes its colour from the dispatch's OUTPUT and
    //: works in place: `net` is where the dispatch writes and where the
    //: engine then edits, so the source has to be somewhere else.
    ID3D12Resource *fsr_in = nullptr;    // RGBA16F at the work extent
    ID3D12Resource *motion = nullptr;    // R16G16F
    ID3D12Resource *depth = nullptr;     // R32F, zeroed, never written (depth is off)
    ID3D12Resource *exposure = nullptr;  // 1x1 R32F = 1.0
    UINT net_w = 0, net_h = 0;
    //: Output (display) extent, kept alongside so a resize of either one
    //: rebuilds the pair.
    UINT out_w = 0, out_h = 0;
    bool resources_ready = false;

    // Diagnostics - the whole point of a first release to strangers.
    uint64_t frames = 0, timeouts = 0, refused = 0;
    uint64_t eval_ms_sum = 0, eval_ms_max = 0;
    uint64_t last_report_tick = 0;
    uint32_t last_jobs = 0;

    // The feed diagnostic (dispatch route only): how long the engine's job
    // counter has stood still while we kept handing it frames. `last_job_seen`
    // is the value it had the last time it moved.
    uint32_t last_job_seen = 0;
    uint64_t frames_without_job = 0;
    uint64_t jobs_seen_total = 0;

    // The frame's start, read by AmdFrameAccounting. Kept in the state rather
    // than a local because the frame now has two exits and both count.
    uint64_t frame_t0 = 0;
};

static AmdState g_amd;

static const char *AmdImageKindName(amd_nr::ImageKind k)
{
    switch (k)
    {
    case amd_nr::ImageKind::Patched: return "patched v0.2.17 (the tested build)";
    case amd_nr::ImageKind::Stock:   return "stock v0.2.17 (both images are driven by the host; "
                                            "the runtime's own setup thread stays alive in each)";
    case amd_nr::ImageKind::Stock0310: return "stock v0.3.1 (the runtime's next release, "
                                              "unpatched - the offset table belongs to THIS one)";
    default:                          return "unknown";
    }
}

// NS_MOTION_BACKEND=amd (the menu's own switch) or NS_AMD=1 pick the AMD path.
// This build defaults to it: no choice recorded means "amd". This is the same
// predicate the early adapter code uses (AmdPathRequestedEarly) - one source
// of truth, called from both places.
static bool AmdRequested()
{
    static int cached = -1;
    if (cached < 0) cached = AmdPathRequestedEarly() ? 1 : 0;
    return cached == 1;
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
    // Both flags always: ALLOW_UNORDERED_ACCESS because the engine writes in
    // place, ALLOW_RENDER_TARGET because the packet declares render-target and
    // the engine transitions from it. The reference carries the pair on every
    // engine surface; dropping either one does not fail, the engine records,
    // reports healthy and produces nothing (or faults on the transition).
    rd.Flags = uav ? (D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                      D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET)
                   : D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource *t = nullptr;
    if (FAILED(h.dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, initial,
        nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&t)))) return nullptr;
    return t;
}

static void AmdReleaseResources()
{
    auto drop = [](ID3D12Resource *&r) { if (r != nullptr) { r->Release(); r = nullptr; } };
    drop(g_amd.net); drop(g_amd.fsr_in); drop(g_amd.up_out);
    drop(g_amd.motion); drop(g_amd.depth); drop(g_amd.exposure);
    // The FSR contexts are sized to the extents that just went away, so they
    // go with them; the next frame rebuilds both together.
    g_amd.fsr.ReleaseContexts();
    g_amd.fsr_ready = false;
    g_amd.net_w = g_amd.net_h = 0;
    g_amd.resources_ready = false;
    // The descriptors referenced these resources - they must be reissued.
    g_amd.b_in_src = g_amd.b_in_dst = nullptr;
    g_amd.b_mv_src = g_amd.b_mv_dst = nullptr;
    g_amd.b_out_src = g_amd.b_out_nat = g_amd.b_out_dst = nullptr;
}

// The engine's own verdict, echoed into OUR log.
//
// The third report from a real Radeon (issue #1) had everything healthy on our
// side - the pass active, the network running at 16 ms a job, the self-check
// fine - while the picture was black. The one line that said so lived in the
// ENGINE's log: `auto-exposure: encoded mean 0.000`, which means the frame it
// was handed was black. Nothing in NeuralScreen.log carried it, so the report
// read as "everything works and nothing is on screen".
//
// That asymmetry is the expensive part of a black-screen round: the host is
// confident and the engine knows. This reads the engine's log after the fact
// and repeats the numbers that decide it, so the next report says it in one
// file.
static void AmdEngineHealth()
{
    if (g_amd.dir.empty()) return;
    const std::wstring path = g_amd.dir + L"\\dlssnr_on_amd.log";
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;

    // This run's bytes only. The log is appended to across launches, and a
    // tail read with no lower bound can quote a PREVIOUS launch's line as if
    // it belonged to this one: `Encoded mean` from an earlier run would be
    // reported as the current frame's measure, and a reader chasing a black
    // picture would be sent after a run that is not the one in front of them.
    // The loader records where the file ended before it loaded the module.
    const unsigned long long from = g_amd.runtime.LogFrom();
    LARGE_INTEGER size{};
    constexpr LONGLONG kWant = 32 * 1024;
    std::string text;
    if (GetFileSizeEx(file, &size) && size.QuadPart > 0 &&
        static_cast<unsigned long long>(size.QuadPart) > from)
    {
        // The tail of THIS run only: never below `from`, and never more than
        // the last few KB of it.
        const unsigned long long total =
            static_cast<unsigned long long>(size.QuadPart) - from;
        const unsigned long long want =
            total > static_cast<unsigned long long>(kWant)
                ? static_cast<unsigned long long>(kWant)
                : total;
        LARGE_INTEGER pos{};
        pos.QuadPart = size.QuadPart - static_cast<LONGLONG>(want);
        if (SetFilePointerEx(file, pos, nullptr, FILE_BEGIN))
        {
            text.resize(static_cast<size_t>(want));
            DWORD got = 0;
            if (!ReadFile(file, &text[0], static_cast<DWORD>(text.size()), &got, nullptr))
                text.clear();
            else
                text.resize(got);
        }
    }
    CloseHandle(file);
    if (text.empty()) return;

    // The last match of each line that decides whether a frame arrived.
    auto last_with = [&text](const char *needle) -> std::string {
        const size_t at = text.rfind(needle);
        if (at == std::string::npos) return std::string();
        size_t end = text.find('\n', at);
        if (end == std::string::npos) end = text.size();
        std::string line = text.substr(at, end - at);
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n'))
            line.pop_back();
        return line;
    };

    // WHICH MODE the engine is in, and which surface that means it reads.
    //
    // This is the one line that was missing from every report we have. From
    // v0.3.0 the runtime can run the network on the render-resolution colour
    // FSR is about to upscale ("pre-upscale"), where the corrected copy comes
    // back through the upscaler - not out of the dispatch's output, which is
    // what this host assumes. The two modes read DIFFERENT surfaces, so a
    // reader cannot judge a black frame without knowing which one ran.
    {
        const std::string mode = last_with("pre-upscale mode");
        const std::string staging = last_with("staging ready");
        if (!mode.empty())
            Log("[amd] engine mode: %s", mode.c_str());
        else if (!staging.empty())
            Log("[amd] engine mode: post-upscale (no pre-upscale line; the "
                "engine reads the output of the dispatch it follows)");
        if (!staging.empty())
            Log("[amd] %s", staging.c_str());
    }

    const std::string mean = last_with("encoded mean");
    if (!mean.empty())
    {
        // NOT called black on its own, and this correction cost a wrong
        // diagnosis to learn.
        //
        // `encoded mean 0.000` is present in EVERY log we have ever collected,
        // including runs on v0.2.14 that were known to produce a picture, and
        // including runs whose `self-check: pre-block zero bytes` reads the
        // healthy 0.13%. The number describes the ENCODE stage of the engine's
        // OWN chain, not the frame this host handed it, so on its own it says
        // nothing about whether the input was black.
        //
        // What separates the two cases is the exposure the engine settles on:
        // 4.0000 is its ordinary value on real content, while 9999.9980 is what
        // it runs away to when the input really is zero.
        const bool runaway = mean.find("9999.9980") != std::string::npos ||
                             mean.find("9999.99") != std::string::npos;
        Log("[amd] the engine's own measure: %s%s", mean.c_str(),
            runaway ? "  <- the exposure ran away to its ceiling: the input to "
                      "the network was zero (compare with the capture line below)"
                    : "");
    }
    // ...and OURS, on the capture, in the same breath.
    //
    // The engine's number alone cannot separate the two cases a black picture
    // splits into, and they need opposite work:
    //
    //   capture mean > 0, engine mean 0  -> the frame WAS there; the loss is
    //                                       inside our own pass
    //   capture mean == 0                -> there was nothing to lose; go and
    //                                       look at the capture instead
    //
    // Until now only the engine's half was reported, so a reader could not
    // tell them apart from any log we have ever received.
    {
        const LONG milli = InterlockedCompareExchange(&g_cap_mean_milli, 0, 0);
        if (milli < 0)
            Log("[amd] the capture's own mean luminance: not measured yet "
                "(no captured frame has reached the guides)");
        else
            Log("[amd] the capture's own mean luminance: %.3f over %llu frames "
                "(%llu of them fully black)%s",
                static_cast<double>(milli) / 1000.0,
                static_cast<unsigned long long>(g_cap_frames),
                static_cast<unsigned long long>(g_cap_dark_frames),
                milli == 0 ? "  <- THE CAPTURE ITSELF IS BLACK; the pass is not "
                             "what lost the picture" : "");
    }
    const std::string jobs = last_with("network job");
    if (!jobs.empty()) Log("[amd] the engine's last job: %s", jobs.c_str());
    const std::string frames = last_with("frames ");
    if (!frames.empty()) Log("[amd] the engine's route: %s", frames.c_str());
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

// Create the three engine surfaces at a given extent. Depth is never written
// by anyone and is not cleared either - and that is deliberate, not an
// oversight: the working host creates its depth this way too ("R32F, flat: a
// desktop frame has none"), a desktop capture has no depth buffer to hand over,
// and the engine's own switch for reading it is off (`UseDepth=0` in the ini,
// and the driver writes that key itself). What matters is that the field is
// populated, which it is.
//
// An earlier version of this comment claimed a zero fill "written by
// ClearUnorderedAccessViewUint through a CPU descriptor". No such call exists
// anywhere in native/amd/ - the resource is created, bound and handed over
// untouched. The comment was describing an operation that was never written;
// corrected rather than implemented, because the host that produces a picture
// does not do it either.
static bool AmdEnsureResources(UINT net_w, UINT net_h, UINT out_w, UINT out_h)
{
    if (g_amd.resources_ready && g_amd.net_w == net_w && g_amd.net_h == net_h &&
        g_amd.out_w == out_w && g_amd.out_h == out_h) return true;
    AmdReleaseResources();
    if (net_w < 32 || net_h < 32) { Log("[amd] implausible network extent %ux%u", net_w, net_h); return false; }

    // Initial state NON_PIXEL_SHADER_RESOURCE, never COMMON: the engine reads
    // its input as a shader resource and the host's own passes transition from
    // there. `net` is the one exception and the reason is below.
    //
    // A hypothesis was tested here and dropped: probe_fsr creates the dispatch
    // output readable and writable, dispatches in both cases, and counts what
    // comes back - BOTH write every pixel (230400/230400 on the bench). So a
    // creation state alone does not black a frame, and the black picture in
    // issue #1 has some other cause, still open. What IS fixed here is the
    // state machinery, because a barrier that lies is undefined behaviour:
    // `up_out` used to be created writable while the barrier opening the frame
    // claimed it was leaving readable (tests/test_amd_state_bookkeeping.py
    // fails on that code and passes on this one).
    //
    // Three creation flags are load-bearing, and getting any of them wrong
    // fails silently - the engine records, reports healthy and produces
    // nothing (or faults). ALLOW_UNORDERED_ACCESS because the engine writes in
    // place; ALLOW_RENDER_TARGET because the packet declares render-target and
    // the engine transitions from it. The reference carries both on every
    // engine surface, so this host does too.
    g_amd.net = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                           D3D12_RESOURCE_STATE_UNORDERED_ACCESS, true);
    g_amd.motion = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R16G16_FLOAT,
                              D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, true);
    // The converted frame the FSR dispatch reads, and the display-resolution
    // surface it upscales into. Both linear fp16: the network is built around
    // linear light, and fp16 is required for the runtime's typed UAV store
    // (B8G8R8A8 does not guarantee one).
    g_amd.fsr_in = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                              D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, true);
    if (out_w != net_w || out_h != net_h)
        // Writable, matching the working reference: dispatch B declares this as
        // its OUTPUT, so it is born in the state its own dispatch names and the
        // frame's round trip returns it there.
        g_amd.up_out = AmdMakeTex(out_w, out_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                                  D3D12_RESOURCE_STATE_UNORDERED_ACCESS, true);
    if (g_amd.net == nullptr || g_amd.motion == nullptr || g_amd.fsr_in == nullptr ||
        ((out_w != net_w || out_h != net_h) && g_amd.up_out == nullptr))
    { Log("[amd] engine surface creation failed at %ux%u", net_w, net_h); AmdReleaseResources(); return false; }
    if (g_amd.depth == nullptr)
        g_amd.depth = AmdMakeTex(net_w, net_h, DXGI_FORMAT_R32_FLOAT,
                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, true);
    if (!AmdCreateExposure(1.0f)) { Log("[amd] exposure creation failed"); AmdReleaseResources(); return false; }

    g_amd.net_w = net_w;
    g_amd.net_h = net_h;
    g_amd.out_w = out_w;
    g_amd.out_h = out_h;
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

// The one line the crash filter can print about the AMD pass: how far it got
// before the process died. Defined here because g_amd lives here; declared
// beside the filter, which is compiled long before this file reaches the
// bridge.
static void AmdCrashCounters(char *out, size_t cap)
{
    // The two diagnostic counters are written as numbers only when the loaded
    // build publishes them: reading address 0 would return the PE header, and a
    // crash line is the last place that should carry an invented number.
    const bool sync_known = g_amd.active && g_amd.runtime.SyncCountKnown();
    const bool timeouts_known = g_amd.active && g_amd.runtime.TimeoutCountKnown();
    _snprintf_s(out, cap, _TRUNCATE,
                "amd active=%d failed=%d frames=%llu timeouts=%llu refused=%llu "
                "engine jobs=%u sync=%s engine timeouts=%s",
                g_amd.active ? 1 : 0, g_amd.failed ? 1 : 0,
                static_cast<unsigned long long>(g_amd.frames),
                static_cast<unsigned long long>(g_amd.timeouts),
                static_cast<unsigned long long>(g_amd.refused),
                g_amd.active ? g_amd.runtime.JobCount() : 0u,
                sync_known ? std::to_string(g_amd.runtime.SyncCount()).c_str() : "n/a",
                timeouts_known ? std::to_string(g_amd.runtime.TimeoutCount()).c_str()
                               : "n/a");
}

// Defined below with the frame; declared here because the router above it
// needs the name.
static bool AmdEvaluateVideo(VideoState &v, int reset, UINT64 *submitted);
// Defined below the frame path; the skipped-frame exit calls it too, so the
// counters describe the whole run rather than only the frames that finished.
static void AmdFrameAccounting();

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

// The engine's verdict, asked where it can be answered, with the one repair
// we know how to make.
//
// WHAT IT REPLACES. Load() used to wait 3 s for the engine's own
// `engine init ok` and report "never came up" when it did not appear. The
// engine writes that line only after the host's swapchain exists, and Load()
// runs before the swapchain is created, so the wait always expired - on a
// healthy engine too. Measured: 4 launches, engine up in all 4, 4 false
// negatives.
//
// WHAT IT DOES NOW, once per run:
//   1. polls until the engine has had its chance (from the frame path, so the
//      swapchain is already there);
//   2. if the engine is up, says so and stops asking;
//   3. if it is NOT up, retries init ONCE with the HIP index WE resolved,
//      written into the engine's own field.
//
// WHY THE RETRY. Both hosts that produce a picture write the resolved index
// into `HipDevice` themselves (zmodelerlover, Magpie: `At<int>(runtime,
// kRvaHipDevice) = hipDevice`). We resolve the same index by adapter LUID at
// +272 and then deliberately keep it to ourselves, leaving the engine's field
// at -1 (auto). In every dead-engine log we have, the engine's own
// `matches the game's D3D12 adapter` line is absent - it never reaches its own
// device selection. Handing it our index is the cheap, reversible thing to
// try, and it is tried SECOND, only when auto failed: the auto path is the
// upstream fix for multi-GPU/iGPU reports and most machines are healthy on it.
//
// Never gated on: it is a repair attempt plus a line in the log. A run where
// the engine is up is untouched.
static void AmdEngineInitSettled()
{
    if (!g_amd.runtime.EngineInitApplicable()) return;
    if (g_amd.runtime.EngineInitSeen()) return;
    if (!g_amd.runtime.PollEngineInit(1500)) return;     // still pending

    if (g_amd.runtime.EngineInitSeen())
    {
        Log("[amd] engine init: ok (the engine wrote its own 'engine init ok')");
        return;
    }

    // Settled and absent.
    if (g_amd.engine_retry_done)
    {
        Log("[amd] engine init: THE ENGINE NEVER CAME UP, and the retry with our "
            "own HIP index (%d) did not change it. Nothing downstream of this "
            "line describes a working pass; the cause is in the engine's own "
            "log next to the DLL, not in the picture.",
            g_amd.hip_index_resolved);
        return;
    }

    if (g_amd.hip_index_resolved < 0)
    {
        Log("[amd] engine init: THE ENGINE NEVER CAME UP - our init call returned "
            "success but its own log carries no 'engine init ok' after this "
            "launch. No HIP index was resolved either, so the retry that usually "
            "follows is not available on this machine.");
        g_amd.engine_retry_done = true;
        return;
    }

    g_amd.engine_retry_done = true;
    Log("[amd] engine init: THE ENGINE NEVER CAME UP - retrying once with the HIP "
        "index this host resolved (%d) instead of the engine's own auto match; "
        "both hosts that produce a picture write that index themselves",
        g_amd.hip_index_resolved);
    if (g_amd.runtime.RetryInitWithHipIndex(g_amd.hip_index_resolved))
    {
        // Give the engine the same chance again, but do not block the picture
        // for it: the next frames poll and the verdict lands when it lands.
        Log("[amd] the retry returned success; the engine's own line decides");
        return;
    }
    Log("[amd] the retry could not be made (the engine refused the init call)");
}

static bool AmdEvaluateVideo(VideoState &v, int reset, UINT64 *submitted)
{
    if (submitted) *submitted = 0;
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    UINT nw = 0, nh = 0;
    AmdNetExtent(v, cw, ch, nw, nh);

    // The engine's verdict, asked from here because only here can it be
    // answered (the swapchain exists by now). Once per run, then it is inert.
    if (!g_amd.runtime.EngineInitSettled()) AmdEngineInitSettled();

    // THE GATE. An engine that has conclusively not come up is not fed.
    //
    // Measured reason: a reporter's worker died on frame 0 with
    // ACCESS_VIOLATION at D3D12Core.dll + 0x12ACCC (faulting read 0x18C) on
    // every launch, on two different builds, with `engine init ok` absent from
    // its log each time. We were driving a full frame - surfaces, FSR
    // contexts, the dispatch, the submission - at an engine that had never
    // initialised, and the crash landed inside D3D12 rather than anywhere that
    // named the engine.
    //
    // The retry above is tried first, so a machine that the engine accepts
    // after an explicit HIP index still gets its pass. Only a settled ABSENT
    // verdict, after the retry, reaches this line.
    //
    // Falling back rather than failing: the worker stays alive, the capture,
    // the overlay and the menu keep working, and the picture is the raw frame
    // - which is what the "card not supported" path already does, and it is
    // strictly more useful to the user than a crash loop.
    if (g_amd.runtime.EngineInitApplicable() && g_amd.runtime.EngineInitSettled() &&
        !g_amd.runtime.EngineInitSeen())
    {
        if (!g_amd.engine_dead_announced)
        {
            g_amd.engine_dead_announced = true;
            Log("[amd] the engine did not come up on this machine, so it is not "
                "fed any more - the raw frame passes through. The program stays "
                "alive (capture, overlay, menu); the picture is unprocessed. "
                "Its own log next to the DLL is the file that explains why.");
            Log("[amd] ===== AMD path off - the engine never initialised =====");
        }
        g_amd.failed = true;
        return false;
    }

    if (!AmdEnsurePipeline() || !AmdEnsureResources(nw, nh, cw, ch))
    {
        g_amd.failed = true;
        Log("[amd] the pass could not be prepared - falling back to the raw frame");
        return false;
    }
    if (reset) g_amd.runtime.InvalidateHistory();

    // The FSR contexts are sized to the extents, so they are (re)built here
    // whenever the frame set is. A failure is not fatal to the program - the
    // caller falls back to the packet path - but it IS fatal to the picture,
    // and the log says which.
    if (!g_amd.fsr.ContextsReady()) {
        std::string why;
        if (!g_amd.fsr.CreateContexts(h.dev, nw, nh, cw, ch, why)) {
            Log("[amd] the FidelityFX contexts could not be created: %s", why.c_str());
            g_amd.failed = true;
            return false;
        }
        Log("[amd] FSR contexts: network %ux%u (1:1), upscale %s",
            nw, nh, g_amd.fsr.Upscaling() ? "work -> display" : "none (work == display)");
    }

    // The engine's own knobs, written every frame. Until now SetOptions was
    // defined and never called, so the engine ran on whatever its DllMain
    // left in those fields: the sliders moved the host's own composite and
    // nothing else, and the character mask was whatever the previous writer
    // put there. The reference writes the same block per frame.
    {
        amd_nr::Options opt;
        opt.local_tone = g_video_options.local_tone;
        opt.local_structure = g_video_options.local_structure;
        opt.skin_structure = g_video_options.skin_structure;
        opt.auto_mask = g_video_options.auto_mask != 0;
        // The menu's Intensity. It is not an in-image parameter like the three
        // above: the runtime takes its strength from `Scale` in its own ini, so
        // this value is written to the FILE (see Runtime::WriteScale). Sending
        // it any other way leaves the slider dead, which it was.
        opt.intensity = g_video_options.intensity;
        // The style index selects the tone channels, not a scale: the
        // engine's Structure channel adds AO, contact shadows and SSS, and
        // its skin channel routes structure through a character mask - both
        // wrong for a desktop's flat colour and hard edges. The reference's
        // own mapping: Default(0) and anything unknown keep the caller's
        // count, Natural(1) turns the channels off, Cinematic(2) needs at
        // least one.
        switch (g_video_options.style)
        {
        case 1:  opt.tone_channels = 0; break;
        case 2:  opt.tone_channels = 1; break;
        default: opt.tone_channels = 0; break;
        }
        g_amd.runtime.SetOptions(opt);
        // The slider reaching the network is worth one line, once per change:
        // it is the difference between an Intensity control that does something
        // and one that only moves the host's own composite (which is what it
        // used to do).
        {
            const float written = g_amd.runtime.ScaleWritten();
            if (written >= 0.0f && written != g_amd.scale_logged)
            {
                g_amd.scale_logged = written;
                Log("[amd] intensity %.2f -> the runtime's Scale=%.5f",
                    g_video_options.intensity, written);
            }
        }
    }

    const bool ts = ProfileGpuBegin(PS_EVAL);
    g_amd.frame_t0 = GetTickCount64();

    // ---- 1. the first list: convert in, resample motion, hand to the engine
    if (!BeginCommands()) return false;

    // colour: full frame (RGBA8) -> fsr_in (RGBA16F, linear light), area-down
    // or bilinear-up.
    //
    // It lands in fsr_in and NOT in net, because the engine takes its colour
    // from the OUTPUT of the dispatch it follows and works in place: net is
    // what the dispatch writes and what the engine then edits. Handing the
    // engine our own converted frame and skipping the dispatch is exactly the
    // path that produced `dispatches 0 ... route backbuffer` - a black picture
    // with a healthy network behind it.
    {
        D3D12_RESOURCE_BARRIER pre = Transition(
            g_amd.fsr_in, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &pre);
        const UINT dims[4] = { nw, nh, cw, ch };
        AmdDispatch(0, g_amd.pso_in, dims,
                    v.color.tex, DXGI_FORMAT_R8G8B8A8_UNORM,
                    v.color.tex, DXGI_FORMAT_R8G8B8A8_UNORM,
                    g_amd.fsr_in, DXGI_FORMAT_R16G16B16A16_FLOAT, 1, nw, nh);
        D3D12_RESOURCE_BARRIER post = Transition(
            g_amd.fsr_in, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
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

    // ---- 2. the FSR dispatch the engine follows -------------------------
    //
    // This is the piece the runtime is built around, and the reason a host
    // that skips it gets a black picture however well the network runs. Two
    // dispatches per frame:
    //
    //   A  work -> work, MOTION VECTORS BOUND. The runtime follows the
    //      dispatch that carries motion vectors (its own log: "ignoring
    //      upscaler dispatches without motion vectors ... following the one
    //      with motion vectors"), takes the frame from A's OUTPUT and runs the
    //      network on it, in place.
    //   B  work -> display, no motion vectors. The runtime ignores it; plain
    //      FSR upscales the network's result to the output resolution. It does
    //      not exist when work == display, because then there is nothing to
    //      scale.
    //
    // The result is that the network runs at the work resolution on a frame
    // that has NOT been resampled, and the step up to the display resolution
    // is FSR's job rather than a bilinear stretch.
    {
        const float frame_ms = g_amd.last_frame_ms > 0.0f ? g_amd.last_frame_ms : 16.6f;
        std::string why;
        if (!g_amd.fsr.DispatchNet(h.list, g_amd.fsr_in, g_amd.depth,
                                   g_amd.motion, g_amd.net, frame_ms,
                                   reset != 0, why)) {
            Log("[amd] %s", why.c_str());
            ++g_amd.fsr_failures;
            AbortCommands();
            g_amd.failed = true;
            return false;
        }
        // The upscale (B) is NOT here on purpose: it has to run after the
        // engine has edited `net`, and the engine works through its own
        // worker between this submission and the second list. Recorded here,
        // B would scale the frame the network has not touched yet. It sits at
        // the head of the second list instead.
        ++g_amd.fsr_frames;
        if (g_amd.fsr_frames == 1 || (g_amd.fsr_frames % 300) == 0)
            Log("[amd] FSR dispatch %llu: network at %ux%u%s",
                static_cast<unsigned long long>(g_amd.fsr_frames), nw, nh,
                g_amd.fsr.Upscaling() ? ", then the upscale" : " (1:1, no upscale)");
    }

    // The engine, recorded into the same list. It is handed shader-readable
    // surfaces and deals with the hazards itself - no barriers here.
    //
    // The command list's LAST binding before the record matters: the
    // reference re-binds the heap, re-sets the root signature and points
    // table 0 at SLOT 0 (the engine's own set: the work surface as an SRV and
    // the same resource as a UAV) immediately before filling the packet, and
    // says why - "the engine opens the resource for HIP access itself; this
    // binding is here so the command list matches the one the engine was
    // written against". Our motion pass above leaves table 0 on slot 1, so
    // without this the engine is handed a list whose bindings are somebody
    // else's.
    {
        AmdBindTriplet(0, g_amd.net, DXGI_FORMAT_R16G16B16A16_FLOAT,
                       g_amd.net, DXGI_FORMAT_R16G16B16A16_FLOAT,
                       g_amd.net, DXGI_FORMAT_R16G16B16A16_FLOAT, 1);
        ID3D12DescriptorHeap *heaps[] = { g_amd.heap };
        h.list->SetDescriptorHeaps(1, heaps);
        h.list->SetComputeRootSignature(g_amd.rs);
        D3D12_GPU_DESCRIPTOR_HANDLE gpu = g_amd.heap->GetGPUDescriptorHandleForHeapStart();
        h.list->SetComputeRootDescriptorTable(0, gpu);
    }

    const float mv_scale_x = v.w != 0 ? static_cast<float>(nw) / static_cast<float>(v.w) : 1.0f;
    const float mv_scale_y = v.hgt != 0 ? static_cast<float>(nh) / static_cast<float>(v.hgt) : 1.0f;

    // ---- the packet, only when asked for --------------------------------
    // Default: NOT recorded, and the reason recorded here for a long time was
    // wrong. It said the host that produces a picture does not use this call.
    // The opposite is true: BOTH hosts that produce a picture feed the engine
    // through the packet and hook Record themselves, and neither of them uses
    // the dispatch route this driver defaults to.
    //
    // What the logs show is real - every job comes back `job 1 ... history off`,
    // so the engine never sees a sequence and starts over each frame - but that
    // is the consequence of the route, not evidence for it. The dispatch-alone
    // path is the untested one; the packet path is the one with working
    // installations behind it. The default is unchanged on purpose (flipping it
    // would change what every existing report is a report about), and until
    // v0.3.1 it could not even be tried there: kRecord had no address.
    //
    // NS_AMD_PACKET=1 turns it on. On v0.3.1 that is now a real option - the
    // packet entry points were resolved from an independent published layout,
    // calibrated against our confirmed v0.2.17 values.
    uint32_t wanted = 0;
    if (g_amd.use_packet)
    {
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
        wanted = g_amd.runtime.JobCount();
    }
    else if (g_amd.frames == 0)
    {
        Log("[amd] the engine is fed by the dispatch alone (no packet recorded)");
    }

    // ---- the submission is ONE list, and it happens at the end -----------
    //
    // What used to be here: EndCommands() for dispatch A alone, then a
    // 5000 ms wait on that submission's fence, then BeginCommands() for a
    // second list carrying dispatch B and the composite.
    //
    // The one host that produces a picture does the opposite, and its own
    // comment says why: it records A, B and the composite into a SINGLE list,
    // calls Close/ExecuteCommandLists once, and presents immediately after,
    // because "the runtime runs its network inline off this submission and the
    // present that follows". Splitting the frame across two submissions with a
    // CPU fence wait in between is exactly what our logs show the engine
    // objecting to: `job 1 ... history off` on every line, a frame landing on
    // every SECOND dispatch, and the other one waiting out ~3.5 s for a
    // "capture" that had already been submitted. Nothing above needs that wait
    // either - the packet path is off by default, and the engine's own counters
    // are read from its image, not from the fence.
    //
    // So the two lists are now one. Dispatch B and the final composite follow
    // dispatch A directly, and the single EndCommands below submits the lot.

    // The upscale dispatch (B) and the composite follow A in the SAME list.
    //
    // This is the part that used to be separated, and the separation was
    // wrong. Our old note said B "has to run after the engine has edited
    // `net`, and the engine works through its own worker between this
    // submission and the second list". The reference host says the opposite
    // and its log line is in our own file too: the engine runs its network
    // INLINE, off the submission itself ("mode inline (same-frame, the game
    // waits for the network)"). So the engine's work is inserted into the very
    // command list that carries dispatch A, and the frame it produces exists
    // by the time B is reached in that same list. Splitting the frame into two
    // submissions is what made each frame cost a fence round trip and left the
    // engine's job reporting that it spent seconds "waiting for the capture".
    //
    // Barrier round trip below, unchanged and taken from the host that WORKS:
    //
    //   net      created UNORDERED_ACCESS (dispatch A declares it as its
    //            output), readable while dispatch B reads it as its colour,
    //            and returned to writable at the end of every frame
    //   up_out   created UNORDERED_ACCESS (dispatch B declares it as its
    //            output), readable while the final pass reads it, and returned
    //            to writable at the end of every frame
    //
    // The old code had `up_out` created writable while the barrier that opened
    // the frame claimed it was leaving readable - a barrier that lies about the
    // state its resource is in, which D3D12 defines as undefined behaviour.
    // tests/test_amd_state_bookkeeping.py fails on that code and passes on this
    // one.
    //
    // This is alignment with a working implementation, NOT a proven fix for the
    // black picture in issue #1: probe_fsr dispatches with the surface created
    // either way and both write every pixel, so the creation state alone does
    // not black a frame. That cause is still open.
    if (g_amd.fsr.Upscaling()) {
        const float frame_ms = g_amd.last_frame_ms > 0.0f ? g_amd.last_frame_ms : 16.6f;
        // net: writable where the engine left it -> readable for dispatch B,
        // which declares it as its colour input.
        D3D12_RESOURCE_BARRIER n0 = Transition(
            g_amd.net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &n0);
        // up_out needs nothing before the dispatch: it is already writable, and
        // that is what the dispatch declares for its output.
        std::string why;
        if (!g_amd.fsr.DispatchUpscale(h.list, g_amd.net, g_amd.depth,
                                       g_amd.up_out, frame_ms, reset != 0, why)) {
            Log("[amd] %s", why.c_str());
            ++g_amd.fsr_failures;
            AbortCommands();
            g_amd.failed = true;
            return false;
        }
        D3D12_RESOURCE_BARRIER r = Transition(
            g_amd.up_out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &r);
    } else {
        // 1:1: nothing upscales, so the final pass reads the network's own
        // surface - writable where the engine left it, readable for the read.
        D3D12_RESOURCE_BARRIER n0 = Transition(
            g_amd.net, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &n0);
    }
    {
        // final_src is readable now (either the upscale's output or the
        // network's surface); v.color.tex is the native anchor, also a shader
        // resource; v.output is UNORDERED_ACCESS at rest and stays that way -
        // the compute pass writes it directly.
        const UINT dims[4] = { cw, ch, 0, 0 };
        UINT32 block[4] = { cw, ch, 0, 0 };
        float shoulder = g_amd.shoulder;
        float intensity = g_amd.intensity;
        memcpy(&block[2], &shoulder, sizeof(float));
        memcpy(&block[3], &intensity, sizeof(float));

        // The source of the final pass is whatever the frame ended up in: the
        // FSR upscale's output when there was one to do, otherwise the
        // network's own surface (work == display, nothing was resampled).
        ID3D12Resource *final_src = g_amd.fsr.Upscaling() && g_amd.up_out != nullptr
                                        ? g_amd.up_out : g_amd.net;
        AmdBindTriplet(2, final_src, DXGI_FORMAT_R16G16B16A16_FLOAT,
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

        // Both engine surfaces go back to the state their own dispatch declares
        // for them, which is where the next frame starts:
        //
        //   net     -> UNORDERED_ACCESS (dispatch A declares it as its output)
        //   up_out  -> UNORDERED_ACCESS (dispatch B declares it as its output)
        //
        // net was left readable by dispatch B reading it as its colour, or by
        // the 1:1 branch reading it here; up_out was left readable by the pass
        // just recorded. Skipping either one means the next frame's dispatch is
        // told a state the resource is not in - the same class of lie the
        // creation states used to carry.
        {
            D3D12_RESOURCE_BARRIER n1 = Transition(
                g_amd.net, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            h.list->ResourceBarrier(1, &n1);
            if (g_amd.fsr.Upscaling() && g_amd.up_out != nullptr)
            {
                D3D12_RESOURCE_BARRIER o1 = Transition(
                    g_amd.up_out, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                h.list->ResourceBarrier(1, &o1);
            }
        }
    }
    if (ts) ProfileGpuEnd(PS_EVAL, 4);
    const UINT64 fence = EndCommands();
    if (fence == 0) return false;

    // ---- the tick the engine needs ---------------------------------------
    //
    // The engine's frame is driven off the submission AND the present that
    // follows it. The working host's own comment: its swapchain "exists to
    // drive the runtime's per-frame tick", and its `Present()` runs straight
    // after `ExecuteCommandLists` with nothing waited on in between.
    //
    // What used to break that here was not a missing present - PresentFrame
    // below does present this frame - it was the CPU stall sitting between the
    // submission and it: a 5000 ms wait on this submission's fence, and then a
    // whole second command list to record. The engine, which reads its frame
    // off the submission-plus-present pair, spent that window reporting it was
    // "waiting for the capture".
    //
    // With the frame now in one list, the order is: submit the dispatches ->
    // PresentFrame records the copy into the overlay and presents it. Only a
    // GPU copy separates them, no CPU wait. That is the working host's order.
    //
    // Deliberately NOT presented twice: a second present per frame flips a
    // second (stale) back buffer to the compositor, and this overlay is what
    // the user sees.

    // Notify, for the image whose own announce was removed.
    //
    // WHO TELLS THE ENGINE ABOUT THE SUBMISSION depends on WHICH BUILD is
    // loaded, and that is a property of the file - its hash - so it is read
    // from the loader rather than guessed from a log line.
    //
    // Both v0.2.17 images install their own ExecuteCommandLists detour: the
    // setup thread that installs it has to stay alive on this build (see the
    // loader - nopping it faults at address 0). What differs is what the
    // detour does afterwards.
    //
    // STOCK: the detour carries its own notify call, so the engine hears about
    // the submission by itself. Calling Notify here as well would announce one
    // submission twice - exactly the duplicate that patch 0x8583 exists to
    // remove ("without the hook it would execute the frame twice").
    //
    // PATCHED (patch 0x8583): that call inside the detour is nopped, so
    // nothing announces the submission and this call is the only thing that
    // keeps the dispatch route alive. The host supplies what the patch took
    // out.
    // "Unpatched" is the question, not "the stock one": v0.3.1 ships with one
    // published image and it is unpatched, so its own detour announces the
    // submission exactly as v0.2.17's stock image does. Written as "not
    // Patched" rather than listing builds, so a third unpatched image cannot
    // be added and silently start receiving hand-made Notify calls - which
    // would announce every submission twice.
    const bool engine_owns_submission =
        g_amd.runtime.Kind() != amd_nr::ImageKind::Patched;
    if (!engine_owns_submission)
        g_amd.runtime.Notify(h.queue, h.list);

    // ---- completion ------------------------------------------------------
    //
    // Packet on: the engine's own counter is the authority - it publishes the
    // list it accepted, and the sync counter moves when that job is done.
    // Packet off: there is no accepted job to count, and the reference waits
    // on the QUEUE FENCE instead, because its engine runs inline off the
    // submission and the present that follows. Gating on a counter that
    // nothing increments would skip every frame.
    //
    // That asymmetry is a property of the FEED, not of the engine: `Record` is
    // what increments the job counter, and both hosts that poll those counters
    // (Magpie, zmodelerlover/dlss5-neural-amd) feed the engine through the
    // packet. Both hosts that use the ffxDispatch route - the reference and us
    // - wait on the queue fence. Polling the counter instead would deadlock
    // here: in every Radeon log we have, `sync` is 0 in every single report.
    //
    // So the counters are used on this route as a DIAGNOSTIC, never as a gate.
    const uint32_t budget = g_amd.frames < 3 ? 20000u : 2000u;
    bool engine_ok = true;
    // The wait is available only where the completed-jobs counter is published.
    // WaitJobs returns false when it is not - correctly, because there is
    // nothing to poll - and treating that as "the engine did not finish"
    // skipped EVERY frame on a build whose table has no address for the
    // counter. v0.2.17 publishes it; v0.3.1 does not (neither table maps it).
    //
    // Without the counter the fence below is the only completion signal, and on
    // this route that is the right one anyway: the engine runs its network
    // inline off the submission, so the fence retiring IS the work finishing.
    // The counter was a second opinion, not the gate.
    const bool counter_wait_available = g_amd.runtime.SyncCountKnown();
    if (g_amd.use_packet && counter_wait_available)
        engine_ok = g_amd.runtime.WaitJobs(wanted, budget);
    else if (g_amd.use_packet && g_amd.frames == 0)
        Log("[amd] the packet is recorded, but this build does not publish the "
            "completed-jobs counter - completion is taken from the fence, not "
            "from the engine's own count");
    const bool engine_failed = g_amd.runtime.FailedOnEngineSide();
    if (!WaitFenceValue(h.fence, fence, 5000))
    { Log("[amd] the submission did not retire"); return false; }

    // Packet off: the engine's counters are the only proof it did anything at
    // all. Worth one line, because "the fence retired" is true even when the
    // engine ignored us completely.
    //
    // The sync counter is printed as a number only when the loaded build
    // publishes it. v0.3.1 has no derived address for it, and printing 0 would
    // be a claim ("nothing has completed") where the truth is "we cannot see
    // it" - the two send a reader to different places.
    if (!g_amd.use_packet && (g_amd.frames == 0 || (g_amd.frames % 300) == 0))
    {
        if (g_amd.runtime.SyncCountKnown())
            Log("[amd] engine counters after %llu frames: jobs %u, sync %u",
                static_cast<unsigned long long>(g_amd.frames),
                g_amd.runtime.JobCount(), g_amd.runtime.SyncCount());
        else
            Log("[amd] engine counters after %llu frames: jobs %u, sync n/a "
                "(this build does not publish the completed-jobs counter; the "
                "engine's own log is the witness here)",
                static_cast<unsigned long long>(g_amd.frames),
                g_amd.runtime.JobCount());
    }

    // Does the engine actually RECORD anything when we feed it by dispatch?
    //
    // This is the question every report so far could not answer. The log lines
    // we had said `route fsr` (so the engine found our dispatch) while the job
    // counter read 0 or 1 and never moved - and there was no line that turned
    // that combination into a sentence. It matters because the two cases need
    // opposite work: an engine that records and produces a black frame is a
    // picture problem, an engine that records nothing is a feeding problem.
    //
    // Reported, never gated. Nothing here skips a frame: a counter that fails
    // to move is information, and acting on it would be the deadlock the note
    // above describes.
    if (!g_amd.use_packet)
    {
        const uint32_t jobs_now = g_amd.runtime.JobCount();
        if (jobs_now == g_amd.last_job_seen)
        {
            ++g_amd.frames_without_job;
            if (g_amd.frames_without_job == 60)
                Log("[amd] the engine has recorded NO job in the last %llu frames, "
                    "although it is following our dispatch (route fsr) - the frames "
                    "reach it but nothing is queued; this is the feeding side, not "
                    "the picture",
                    static_cast<unsigned long long>(g_amd.frames_without_job));
        }
        else
        {
            if (g_amd.frames_without_job >= 60)
                Log("[amd] the engine recorded a job again after %llu frames without "
                    "one (jobs %u -> %u)",
                    static_cast<unsigned long long>(g_amd.frames_without_job),
                    g_amd.last_job_seen, jobs_now);
            g_amd.frames_without_job = 0;
            g_amd.last_job_seen = jobs_now;
            g_amd.jobs_seen_total = jobs_now;
        }
    }

    if (!engine_ok || engine_failed)
    {
        ++g_amd.timeouts;
        g_amd.runtime.InvalidateHistory();
        if (g_amd.timeouts <= 5 || (g_amd.timeouts % 60) == 0)
        {
            // Which counter is quoted follows which one the wait used: with no
            // completed-jobs counter the reason is the fence, and printing a
            // number from a counter this build does not publish would be a
            // reading that was never taken.
            if (g_amd.runtime.SyncCountKnown())
                Log("[amd] frame skipped: %s (timeouts=%llu, jobs=%u/%u)",
                    engine_failed ? "the engine gave up"
                                  : "the engine did not finish in time",
                    static_cast<unsigned long long>(g_amd.timeouts),
                    g_amd.runtime.SyncCount(), wanted);
            else
                Log("[amd] frame skipped: %s (timeouts=%llu)",
                    engine_failed ? "the engine gave up"
                                  : "the engine did not finish in time",
                    static_cast<unsigned long long>(g_amd.timeouts));
        }
        // The previous frame's contents stay in v.output - the picture is
        // stale for one frame, which is exactly what the reference does.
        if (submitted) *submitted = fence;
        // ---- 3. accounting
        AmdFrameAccounting();
        return true;
    }

    // ---- 3. accounting
    AmdFrameAccounting();

    g_last_eval_result = 1;   // the client reads this as "the frame went through"
    if (submitted) *submitted = fence;
    return true;
}

// Per-frame accounting and the periodic report. Split out because the frame
// has two exits now (the skipped one and the ordinary one) and both have to
// count, or the numbers in the log stop describing the run.
static void AmdFrameAccounting()
{
    const uint64_t t0 = g_amd.frame_t0;
    ++g_amd.frames;
    const uint64_t elapsed = GetTickCount64() - t0;
    g_amd.eval_ms_sum += elapsed;
    if (elapsed > g_amd.eval_ms_max) g_amd.eval_ms_max = elapsed;
    {
        // Frame time for the FSR dispatch: it drives its temporal accumulation,
        // and a stale constant would make its history wrong.
        const uint64_t now_ms = GetTickCount64();
        if (g_amd.last_frame_tick != 0)
            g_amd.last_frame_ms = static_cast<float>(now_ms - g_amd.last_frame_tick);
        g_amd.last_frame_tick = now_ms;
    }
    const uint64_t now = GetTickCount64();
    if (g_amd.last_report_tick == 0 || now - g_amd.last_report_tick >= 30000)
    {
        g_amd.last_report_tick = now;
        // sync/engine-timeouts are printed only when the build publishes them;
        // "n/a" and 0 are different answers (see SyncCountKnown). Built as
        // strings because the field is either a number or the word n/a, and a
        // format string cannot switch between "%u" and "n/a" in place.
        char sync_buf[32], timeout_buf[32];
        _snprintf_s(sync_buf, sizeof(sync_buf), _TRUNCATE, "%u",
                    g_amd.runtime.SyncCount());
        _snprintf_s(timeout_buf, sizeof(timeout_buf), _TRUNCATE, "%u",
                    g_amd.runtime.TimeoutCount());
        Log("[amd] %llu frames, avg %llu ms, worst %llu ms, timeouts %llu, refused %llu "
            "(engine jobs %u, sync %s, engine timeouts %s, jobs seen %llu, %llu frames "
            "since one)",
            static_cast<unsigned long long>(g_amd.frames),
            static_cast<unsigned long long>(g_amd.frames ? g_amd.eval_ms_sum / g_amd.frames : 0),
            static_cast<unsigned long long>(g_amd.eval_ms_max),
            static_cast<unsigned long long>(g_amd.timeouts),
            static_cast<unsigned long long>(g_amd.refused),
            g_amd.runtime.JobCount(),
            g_amd.runtime.SyncCountKnown() ? sync_buf : "n/a",
            g_amd.runtime.TimeoutCountKnown() ? timeout_buf : "n/a",
            static_cast<unsigned long long>(g_amd.jobs_seen_total),
            static_cast<unsigned long long>(g_amd.frames_without_job));
        // And what the ENGINE saw, in our file: a healthy host with a black
        // picture is the failure this whole path failed to describe, and the
        // only witness to it is the engine's own measure.
        AmdEngineHealth();
    }
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
    // The packet path is OFF unless asked for: see `use_packet` in AmdState.
    // Read once, here, because it is a property of the run, not of a frame.
    {
        char pk[8] = {};
        const DWORD pgot = GetEnvironmentVariableA("NS_AMD_PACKET", pk, sizeof(pk));
        g_amd.use_packet = pgot > 0 && pgot < sizeof(pk) && pk[0] == '1';
    }
    Log("[amd] srgb %s, highlight shoulder %.2f, packet path %s",
        g_amd.srgb ? "on" : "off", g_amd.shoulder,
        g_amd.use_packet ? "on (NS_AMD_PACKET=1)" : "off (the dispatch feeds the engine)");

    // The card check comes first, because it is the one answer the user can
    // act on: everything else (a missing runtime, an old driver) has a fix,
    // and "this is not a Radeon" does not. The program itself keeps running
    // either way - this only decides whether the pass is attempted, and the
    // sentence the menu shows.
    if (!g_radeon_present && !g_amd_any_gpu_flag)
    {
        Log("[amd] this GPU is not supported by the AMD neural pass - it needs "
            "a Radeon RX 7000 or 9000 (RDNA3/RDNA4)");
        Log("[amd] the card in this machine: %s",
            g_amd_card_name.empty() ? "(unknown)" : g_amd_card_name.c_str());
        Log("[amd] the program itself keeps running; the picture is not processed");
        Log("[amd] ===== AMD path off - the raw frame passes through =====");
        g_amd.unsupported = true;
        g_amd.failed = true;
        return false;
    }

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
    // The hook wait's verdict. A runtime whose detours never appeared cannot
    // see our frames at all - the pass would run and process nothing, which is
    // the failure this release exists to end, so it is worth a line of its own.
    //
    // Three answers, not two, and they must not be confused: the patched build
    // cannot print the detour lines by design (its installer is disabled on
    // purpose), so for it the question does not apply. Reporting "NOT seen"
    // there told every reader of a live log to look for a fault that was not
    // there, on three different Radeons.
    if (!g_amd.runtime.HooksApplicable())
        Log("[amd] detour wait: not applicable - this is the patched image, whose "
            "hook installer is disabled on purpose and which this host drives by hand");
    else if (g_amd.runtime.HooksSeen())
        Log("[amd] the runtime's D3D12/DXGI hooks are in place (%lu ms)",
            g_amd.runtime.HooksMs());
    else
        Log("[amd] the runtime's hooks were NOT seen - the frames may not reach "
            "it; its own log sits next to the DLL");

    // The engine's own verdict, which is NOT the line above. Our detour check
    // answers "did the runtime install its hooks"; it reads identically whether
    // the engine started or not, so a report could say `AMD path active` twelve
    // times while the engine's own log carried no `engine init ok` and no
    // `env: HIP` at all - and every later number in the report was measured
    // against an engine that never came up. This line closes that hole.
    // The engine's verdict is NOT printed here any more.
    //
    // It was, and it was wrong every time it was tested. The engine
    // initialises only after the host's swapchain exists - its own order is
    // `swapchain created` -> `env: HIP` -> `using HIP device` ->
    // `engine init ok` - and the swapchain is created well after this
    // function. Asking here meant asking too early: one reporter sent four
    // launches, all four had `engine init ok` in the engine's log, and all
    // four were told "THE ENGINE NEVER CAME UP".
    //
    // It is asked from the frame loop instead, where the answer can exist.
    // See AmdEngineInitSettled below.
    if (!g_amd.runtime.EngineInitApplicable())
        Log("[amd] engine init: not applicable for this build");

    // Re-assert the crash filter: the engine installs its own from DllMain and
    // replaces the process's, which is why the second Radeon report carried no
    // [crash] line while the engine's own log carried four. Its filter writes
    // only to dlssnr_on_amd.log - the file the user is NOT asked to attach
    // first - while ours writes the log they are. Both should end up in a
    // report, so ours is re-installed once the engine has had its turn.
    SetUnhandledExceptionFilter(CrashFilter);

    // Which image is in play, said out loud. The images are a deliberate A/B and
    // the log line is how a report says which one ran: the unpatched builds keep
    // the runtime's own hooks (the shape both working hosts run), the patched
    // one has them disabled and is driven by hand.
    if (g_amd.runtime.Kind() == amd_nr::ImageKind::Stock)
        Log("[amd] runtime image: STOCK - the engine installs its own hooks and "
            "owns the submission; this host does not notify it by hand");
    else if (g_amd.runtime.Kind() == amd_nr::ImageKind::Stock0310)
        Log("[amd] runtime image: STOCK v0.3.1 - the engine installs its own "
            "hooks and owns the submission; this host does not notify it by hand "
            "(the offset table is the one for this release)");
    else if (g_amd.runtime.Kind() == amd_nr::ImageKind::Patched)
        Log("[amd] runtime image: PATCHED - the hook installer is disabled, so "
            "this host notifies the engine by hand (NS_AMD_PATCHED=1)");

    if (!AmdEnsurePipeline())
    { Log("[amd] ===== AMD path off (no conversion pipeline) ====="); g_amd.failed = true; return false; }

    // The engine is only given a frame when it has an FSR dispatch to follow:
    // its own log says `dispatches 0 ... route backbuffer` when it does not,
    // and that is a black picture however well the network runs. Checked here
    // once so the failure is a sentence in the log rather than a silent
    // passthrough.
    if (!g_amd.fsr.Ready()) {
        std::string why;
        if (!g_amd.fsr.Load(g_amd.dir, why)) {
            Log("[amd] the FidelityFX upscaler did not load: %s", why.c_str());
            Log("[amd] ===== AMD path off - the raw frame passes through =====");
            g_amd.failed = true;
            return false;
        }
        Log("[amd] FidelityFX upscaler loaded (the dispatch the engine follows)");
    }

    // The index the loader resolved, kept for the engine-init retry. Both
    // hosts that produce a picture write this into the engine themselves; we
    // only use it when the engine's own auto match did not land.
    g_amd.hip_index_resolved = g_amd.runtime.ResolvedHipIndex();

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
