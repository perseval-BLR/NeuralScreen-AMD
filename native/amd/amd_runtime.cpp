// The AMD runtime driver - see amd_runtime.h for the contract.
//
// Everything here is deliberately paranoid: the runtime exports nothing, so a
// mistake is not an error code, it is a jump into an address that may or may
// not be the function we meant. The size+hash gate is the only thing standing
// between a user with a different build and that jump, and it runs first.

#include "amd_runtime.h"

#include <windows.h>
#include <bcrypt.h>
#include <d3d12.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <vector>

#pragma comment(lib, "bcrypt.lib")

namespace amd_nr {

namespace {

// Raw access to a byte offset inside the loaded image.
template <class T>
T &At(void *module, uintptr_t rva) {
    return *reinterpret_cast<T *>(reinterpret_cast<uintptr_t>(module) + rva);
}

std::string to_hex(const uint8_t *data, size_t len) {
    static const char *hex = "0123456789abcdef";
    std::string out(len * 2, '0');
    for (size_t i = 0; i < len; ++i) {
        out[i * 2] = hex[data[i] >> 4];
        out[i * 2 + 1] = hex[data[i] & 0xF];
    }
    return out;
}

bool sha256_of_file(const std::wstring &path, uint8_t out[32]) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                              nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;

    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    bool ok = false;
    DWORD hash_len = 0, obj_len = 0, got = 0;
    std::vector<uint8_t> obj;

    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
        goto done;
    if (BCryptGetProperty(alg, BCRYPT_HASH_LENGTH, reinterpret_cast<PUCHAR>(&hash_len),
                          sizeof(hash_len), &got, 0) < 0 || hash_len != 32)
        goto done;
    if (BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&obj_len),
                          sizeof(obj_len), &got, 0) < 0)
        goto done;
    obj.resize(obj_len);
    if (BCryptCreateHash(alg, &hash, obj.data(), obj_len, nullptr, 0, 0) < 0)
        goto done;
    for (;;) {
        uint8_t chunk[1 << 16];
        DWORD read = 0;
        if (!ReadFile(file, chunk, sizeof(chunk), &read, nullptr)) goto done;
        if (read == 0) break;
        if (BCryptHashData(hash, chunk, read, 0) < 0) goto done;
    }
    if (BCryptFinishHash(hash, out, 32, 0) < 0) goto done;
    ok = true;

done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    return ok;
}

// The engine's Init call, isolated so __try does not sit in a scope holding
// C++ objects (C2712). Raw pointers only.
int guarded_init(void *module, void *ctx, const std::string *weights) {
    using InitFn = bool(__fastcall *)(void *, const std::string *);
    auto fn = reinterpret_cast<InitFn>(
        reinterpret_cast<uintptr_t>(module) + rva::kInit);
    __try {
        return fn(ctx, weights) ? 0 : 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -static_cast<int>(GetExceptionCode());
    }
}

int guarded_shutdown(void *module) {
    using Fn = void (*)(void);
    auto fn = reinterpret_cast<Fn>(reinterpret_cast<uintptr_t>(module) + rva::kShutdown);
    __try {
        fn();
        return 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -static_cast<int>(GetExceptionCode());
    }
}

std::string narrow(const std::wstring &wide) {
    std::string out;
    out.reserve(wide.size());
    for (wchar_t c : wide) out.push_back(static_cast<char>(c & 0x7F));
    return out;
}

// hipGetDevicePropertiesR0600 fills a struct around a kilobyte; the working
// implementations use an oversized buffer (4096 or 8192, 8-/16-byte aligned)
// because a smaller one is a stack overrun rather than an error return. Only
// one field is read: name[256], uuid[16], then luid[8] at +272.
struct alignas(16) HipProps {
    unsigned char raw[8192];
};

constexpr size_t kHipPropsLuidOffset = 272;

// The D3DCompile import rebind. The runtime compiles its embedded HLSL at
// init; a host process that has some other d3dcompiler_47.dll loaded (an old
// one shipped by a game, for instance) would give it a compiler that rejects
// its FP16 typed UAV load shader. Point just this module's import slot at the
// System32 build. The slot address is fixed for this pinned image (it lives in
// that image's .rdata import table).
constexpr uintptr_t kD3DCompileIatRva = 0x6bb50;

void rebind_d3dcompiler(void *module, std::string *warn) {
    HMODULE compiler = LoadLibraryExW(L"d3dcompiler_47.dll", nullptr,
                                      LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (compiler == nullptr) {
        if (warn) *warn = "System32 d3dcompiler_47.dll not found";
        return;
    }
    void *proc = reinterpret_cast<void *>(
        GetProcAddress(compiler, "D3DCompile"));
    if (proc == nullptr) {
        if (warn) *warn = "D3DCompile not exported by System32 compiler";
        return;
    }
    void **slot = reinterpret_cast<void **>(
        reinterpret_cast<uintptr_t>(module) + kD3DCompileIatRva);
    DWORD old = 0;
    if (!VirtualProtect(slot, sizeof(void *), PAGE_READWRITE, &old)) {
        if (warn) *warn = "VirtualProtect on the D3DCompile import failed";
        return;
    }
    InterlockedExchangePointer(slot, proc);
    VirtualProtect(slot, sizeof(void *), old, &old);
}

}  // namespace

Runtime::~Runtime() {
    // No shutdown on the destructor by default: the worker owns the device and
    // the queue, and the engine's worker threads must not be stopped while a
    // submission may still be in flight. The reference stops workers only from
    // an explicit Shutdown call after everything has drained, and so do we.
}

bool Runtime::Load(const std::wstring &runtime_dir, ID3D12Device *device,
                   ID3D12CommandQueue *queue) {
    ready_ = false;
    last_error_.clear();

    const std::wstring runtime_path = runtime_dir + L"\\" + kRuntimeName;
    const std::wstring weights_path = runtime_dir + L"\\" + kWeightsName;
    const std::wstring ini_path = runtime_dir + L"\\" + kIniName;

    // --- 1. the runtime file, size and hash ------------------------------
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExW(runtime_path.c_str(), GetFileExInfoStandard, &fad)) {
        last_error_ = "dlssnr_amd_pass1.dll not found in " + narrow(runtime_dir);
        return false;
    }
    {
        LARGE_INTEGER li{};
        li.LowPart = fad.nFileSizeLow;
        li.HighPart = static_cast<LONG>(fad.nFileSizeHigh);
        const uint64_t size = static_cast<uint64_t>(li.QuadPart);
        if (size != kRuntimeSize) {
            last_error_ = "dlssnr_amd_pass1.dll is " + std::to_string(size) +
                          " bytes, the known build is " +
                          std::to_string(kRuntimeSize) + " (this table belongs "
                          "to exactly one image; refusing rather than guessing)";
            return false;
        }
    }
    uint8_t actual[32] = {};
    if (!sha256_of_file(runtime_path, actual)) {
        last_error_ = "could not hash dlssnr_amd_pass1.dll";
        return false;
    }
    found_hash_ = to_hex(actual, 32);
    {
        bool patched = true, stock = true;
        for (int i = 0; i < 32; ++i) {
            if (actual[i] != kRuntimeSha256Patched[i]) patched = false;
            if (actual[i] != kRuntimeSha256Stock[i]) stock = false;
        }
        if (patched) kind_ = ImageKind::Patched;
        else if (stock) kind_ = ImageKind::Stock;
        else {
            last_error_ = "dlssnr_amd_pass1.dll is not a build these offsets "
                          "belong to (found " + found_hash_ + "); refusing rather "
                          "than guessing";
            return false;
        }
    }
    if (GetFileAttributesW(weights_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
        last_error_ = "dlssnr_on_amd_weights.bin not found next to the runtime";
        return false;
    }

    // The engine reads its ini from its own DllMain, so the file must exist
    // before the module is loaded, and the keys that matter have to be in it
    // by then. The working installation creates it empty and lets the engine
    // fall back to its built-in defaults; those defaults are wrong for a
    // desktop host in one specific way: InlineWaitMs is 600 ms, while a job
    // on a real Radeon takes tens of milliseconds. A frame that overruns the
    // budget is a skipped frame, so every host in the ecosystem lowers it
    // (the add-on writes 100, the desktop distributions write 25). The
    // runtime clamps whatever is read into [50, 5000].
    //
    // Written only when absent: a user who tuned their own copy keeps it.
    if (GetFileAttributesW(ini_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
        HANDLE ini = CreateFileW(ini_path.c_str(), GENERIC_WRITE, 0, nullptr,
                                 CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (ini != INVALID_HANDLE_VALUE) {
            CloseHandle(ini);
            WritePrivateProfileStringW(L"DlssNrOnAmd", L"InlineWaitMs", L"100",
                                       ini_path.c_str());
        }
    }

    // --- 2. HIP ---------------------------------------------------------
    hip_ = LoadLibraryExW(kHipName, nullptr, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (hip_ == nullptr) {
        hip_ = LoadLibraryExW((runtime_dir + L"\\" + kHipName).c_str(), nullptr,
                              LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    }
    if (hip_ == nullptr) {
        last_error_ = "amdhip64_7.dll not found - the HIP 7 runtime ships with "
                      "Adrenalin 26.1.1 or newer";
        return false;
    }
    // The R0600 suffix is the ROCm 6.0 hipDeviceProp_t ABI name, not a typo:
    // the un-suffixed export would mis-size the struct.
    auto get_count = reinterpret_cast<int (*)(int *)>(
        GetProcAddress(reinterpret_cast<HMODULE>(hip_), "hipGetDeviceCount"));
    auto get_props = reinterpret_cast<int (*)(void *, int)>(
        GetProcAddress(reinterpret_cast<HMODULE>(hip_), "hipGetDevicePropertiesR0600"));
    hip_set_ = reinterpret_cast<HipSetFn>(
        GetProcAddress(reinterpret_cast<HMODULE>(hip_), "hipSetDevice"));
    if (get_count == nullptr || get_props == nullptr || hip_set_ == nullptr) {
        last_error_ = "amdhip64_7.dll is missing hipGetDeviceCount / "
                      "hipGetDevicePropertiesR0600 / hipSetDevice";
        return false;
    }
    int count = 0;
    if (get_count(&count) != 0 || count <= 0) {
        last_error_ = "HIP reports no devices - is the Radeon enabled?";
        return false;
    }

    // Device selection is by adapter LUID at +272: the two APIs enumerate in
    // their own orders (the same lesson as issue #81 on the NVIDIA side), and
    // the textures the host hands over are only reachable from the matching
    // adapter. A mismatch is fatal, not a fallback.
    LUID target{};
    bool have_target = false;
    if (device != nullptr) {
        target = device->GetAdapterLuid();
        have_target = true;
    }
    int chosen = -1;
    for (int i = 0; i < count; ++i) {
        HipProps props{};
        if (get_props(&props, i) != 0) continue;
        if (have_target && memcmp(props.raw + kHipPropsLuidOffset, &target, 8) == 0) {
            chosen = i;
            break;
        }
    }
    if (have_target && chosen < 0) {
        last_error_ = "no HIP device matches the D3D12 adapter - the neural "
                      "runtime would write into memory the adapter cannot reach";
        return false;
    }
    if (chosen < 0) chosen = 0;  // no device to compare against; single-GPU case
    hip_device_ = chosen;
    hip_set_(chosen);

    // --- 3. the runtime module ------------------------------------------
    // The DLL-load-dir flag is what lets the runtime find its own dependencies
    // next to itself.
    module_ = LoadLibraryExW(runtime_path.c_str(), nullptr,
                             LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                 LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (module_ == nullptr) {
        last_error_ = "LoadLibrary of dlssnr_amd_pass1.dll failed (error " +
                      std::to_string(GetLastError()) + ")";
        return false;
    }
    // Pin the module: the engine starts worker threads holding references into
    // its own image, and unloading under them cannot be made safe. Retained
    // even on the failure paths below - the CRT has already registered HIP
    // kernels by the time anything can fail.
    {
        HMODULE pinned = nullptr;
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_PIN,
                           reinterpret_cast<LPCWSTR>(module_), &pinned);
    }
    init_ = reinterpret_cast<InitFn>(
        reinterpret_cast<uintptr_t>(module_) + rva::kInit);
    record_ = reinterpret_cast<RecordFn>(
        reinterpret_cast<uintptr_t>(module_) + rva::kRecord);
    notify_ = reinterpret_cast<NotifyFn>(
        reinterpret_cast<uintptr_t>(module_) + rva::kNotify);

    // --- 4. the D3DCompile import ---------------------------------------
    // Best-effort: the System32 compiler is the one the runtime's embedded
    // shaders were written for. A failure here is logged but not fatal -
    // the engine may still compile with whatever the process has.
    {
        std::string warn;
        rebind_d3dcompiler(module_, &warn);
        if (!warn.empty()) last_error_ = "d3dcompiler rebind: " + warn + " (continuing)";
    }

    // --- 5. bind the device and the queue the caller owns ---------------
    // The engine holds these pointers for its own threads; the host's
    // references must outlive this object and the engine's reference is taken
    // explicitly. These are process-lifetime references.
    At<ID3D12Device *>(module_, rva::kDevice) = device;
    if (device != nullptr) device->AddRef();
    At<ID3D12CommandQueue *>(module_, rva::kQueue) = queue;
    if (queue != nullptr) queue->AddRef();
    At<int>(module_, rva::kHipDevice) = chosen;

    // --- 6. mode flags, in the reference order --------------------------
    // Inline=1: the engine completes the job on the frame it was given, which
    // is what the rest of the chain assumes; async hands back an earlier frame
    // and the chain has no path for that. Interop=1: zero-copy shared
    // textures. These writes land AFTER the runtime's DllMain (which reads the
    // ini), so they win over the file - deliberately.
    //
    // UseFsrInputs=0: this host is not the FSR path; the frame and the guides
    // are handed over through the packet. Depth is off until the worker has a
    // depth source worth handing over.
    //
    // NOT written: Tonemap (0x76e20) - the ini keeps the last word there, its
    // own default is already "auto by input format"; and the wait allowance
    // (0x76c44) - the engine maintains it and writing it fights the engine.
    At<uint8_t>(module_, rva::kInlineMode) = 1;
    At<uint8_t>(module_, rva::kInterop) = 1;
    At<uint8_t>(module_, rva::kEnabled) = 1;
    At<uint8_t>(module_, rva::kUseFsrInputs) = 0;
    At<uint8_t>(module_, rva::kUseDepth) = 0;

    // --- 7. init ---------------------------------------------------------
    // Arg 1 is the engine's own in-image context struct, not a host object:
    // the address itself, where the device, queue and HIP index were written.
    ctx_ = reinterpret_cast<void *>(reinterpret_cast<uintptr_t>(module_) +
                                    rva::kInitCtx);
    // The device is bound on this thread once more right before init - the
    // engine selects it on its own threads from the stored index.
    hip_set_(chosen);
    const std::string weights_narrow = narrow(weights_path);
    const int rc = guarded_init(module_, ctx_, &weights_narrow);
    if (rc < 0) {
        last_error_ = "the engine raised an exception on init (code " +
                      std::to_string(-rc) +
                      ") - wrong build or wrong device";
        return false;
    }
    if (rc != 0) {
        last_error_ = "the engine refused to init (check the runtime's own log)";
        return false;
    }

    // Only after init returns: the flag the engine expects set once ready.
    At<uint8_t>(module_, rva::kFlagAfterInit) = 1;

    ready_ = true;
    return true;
}

void Runtime::Shutdown() {
    if (module_ == nullptr) return;
    guarded_shutdown(module_);
    ready_ = false;
}

void Runtime::SetOptions(const Options &opt) {
    if (!ready_ || module_ == nullptr) return;
    // The engine reads every one of these per frame, and the reference
    // clamps the three intensities into [0, 2] - which is also what turns
    // this host's "-1 means off" into a legal 0. The mask and the channel
    // bitfield sit in the same block and are written every frame too: not
    // writing them is not the same as writing zero, the engine simply keeps
    // whatever the last writer left there.
    auto cl01 = [](float v) { return v < 0.0f ? 0.0f : (v > 2.0f ? 2.0f : v); };
    At<float>(module_, rva::kLocalTone) = cl01(opt.local_tone);
    At<float>(module_, rva::kLocalStructure) = cl01(opt.local_structure);
    At<float>(module_, rva::kSkinStructure) = cl01(opt.skin_structure);
    At<uint32_t>(module_, rva::kCharMask) = opt.auto_mask ? 1u : 0u;
    At<uint32_t>(module_, rva::kToneChannels) = opt.tone_channels;
    At<uint8_t>(module_, rva::kEnabled) = opt.enabled ? 1 : 0;
}

bool Runtime::Record(ID3D12CommandList *list, ID3D12Resource *colour,
                     ID3D12Resource *motion, ID3D12Resource *depth,
                     ID3D12Resource *exposure, float scale_x, float scale_y) {
    if (!ready_ || record_ == nullptr) return false;

    // Per-frame state, written strictly before the record call, in the
    // reference order. The depth pair is written even though depth is unused -
    // the engine expects the fields populated with the explicit convention.
    At<uint8_t>(module_, rva::kPerPassFlag) = 1;   // Temporal
    At<uint32_t>(module_, rva::kDepthInverted) = 0;
    At<uint8_t>(module_, rva::kDepthExplicit) = 1;

    Packet packet{};
    packet.list = list;
    packet.colour = colour;        // read AND written in place
    packet.colourState = kPacketState;
    packet.motion = motion;        // the engine tolerates null at its own
                                   // peril; the caller supplies a zeroed field
    packet.motionState = kPacketState;
    packet.depth = depth;
    packet.depthState = kPacketState;
    packet.exposure = exposure;
    packet.exposureState = kPacketState;
    packet.scaleX = scale_x;
    packet.scaleY = scale_y;

    __try {
        record_(&packet);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        last_error_ = "the engine raised an exception while recording a job";
        return false;
    }

    // The void return says nothing. The engine publishes the list it took in
    // the pending-list marker; if it is not this list, the frame was not
    // accepted and the host must not wait on it.
    const bool accepted =
        At<ID3D12CommandList *>(module_, rva::kPendingList) == list;
    if (!accepted) {
        last_error_ = "the engine did not take the recorded list";
        return false;
    }
    // A stale watchdog abort token from an earlier timeout would poison this
    // frame's wait; clear it now that the record is confirmed.
    InterlockedExchange(reinterpret_cast<volatile LONG *>(
                            reinterpret_cast<uintptr_t>(module_) + rva::kAbortWord),
                        0);
    return true;
}

void Runtime::Notify(ID3D12CommandQueue *queue, ID3D12CommandList *list) {
    if (!ready_ || notify_ == nullptr) return;
    ID3D12CommandList *lists[] = {list};
    __try {
        notify_(queue, 1, lists);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // Nothing useful to do here - the caller finds out through the wait
        // timing out and the log carries the fact.
        last_error_ = "the engine raised an exception in notify";
    }
}

bool Runtime::WaitJobs(uint32_t wanted, uint32_t timeout_ms) const {
    if (!ready_ || module_ == nullptr) return false;
    const ULONGLONG deadline = GetTickCount64() + timeout_ms;
    while (At<uint32_t>(module_, rva::kSyncCounter) < wanted) {
        if (GetTickCount64() > deadline) return false;
        Sleep(1);  // yield, not spin: the worker needs the core too
    }
    return true;
}

uint32_t Runtime::JobCount() const {
    if (!ready_ || module_ == nullptr) return 0;
    return At<uint32_t>(module_, rva::kJobCounter);
}

uint32_t Runtime::SyncCount() const {
    if (!ready_ || module_ == nullptr) return 0;
    return At<uint32_t>(module_, rva::kSyncCounter);
}

uint32_t Runtime::TimeoutCount() const {
    if (!ready_ || module_ == nullptr) return 0;
    return At<uint32_t>(module_, rva::kTimeoutCounter);
}

bool Runtime::FailedOnEngineSide() const {
    if (!ready_ || module_ == nullptr) return true;
    return At<uint8_t>(module_, rva::kStatusFlag) != 0;
}

void Runtime::InvalidateHistory() {
    if (!ready_ || module_ == nullptr) return;
    At<uint8_t>(module_, rva::kWantHistory) = 0;
    At<void *>(module_, rva::kHistory) = nullptr;
}

}  // namespace amd_nr
