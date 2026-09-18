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

// Whether a line containing `needle` appears in the part of the file written
// SINCE `from_byte`. Returns true and, on a hit, reports where the file ended.
//
// The runtime's log is opened in append mode ACROSS RUNS, so searching the
// whole file is not a check at all: the second launch of the day finds the
// first launch's "hooked ... Present1" line instantly and skips the wait it was
// supposed to perform. Our own Radeon log shows exactly that, and it is the
// reason the hook wait reported `0 ms`:
//
//     [amd] the runtime's D3D12/DXGI hooks are in place (0 ms)
//
// The runtime needs the time before it can see our swapchain, so a zero here
// means the host created its swapchain into a detour that was not installed
// yet - the documented route to a silent passthrough. Reading only what this
// run appended is what makes the answer mean something.
bool LogContains(const std::wstring &path, const char *needle,
                 unsigned long long from_byte, unsigned long long *end_byte) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ |
                              FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;

    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart <= 0) {
        CloseHandle(file);
        return false;
    }
    if (end_byte != nullptr) *end_byte = static_cast<unsigned long long>(size.QuadPart);
    if (size.QuadPart <= static_cast<LONGLONG>(from_byte)) {
        CloseHandle(file);   // this run has appended nothing yet
        return false;
    }

    LARGE_INTEGER pos{};
    pos.QuadPart = static_cast<LONGLONG>(from_byte);
    if (!SetFilePointerEx(file, pos, nullptr, FILE_BEGIN)) {
        CloseHandle(file);
        return false;
    }

    std::string text;
    char buf[4096];
    DWORD got = 0;
    // Bounded: one run's banner is tiny, and the cap only guards against a
    // runaway writer.
    while (text.size() < 256 * 1024 &&
           ReadFile(file, buf, sizeof(buf), &got, nullptr) && got > 0) {
        text.append(buf, got);
    }
    CloseHandle(file);
    return text.find(needle) != std::string::npos;
}

//: Where the runtime's log ended before this run started. Everything the hook
//: wait searches must fall after it, or the wait is satisfied by a previous
//: run's lines and does nothing.
unsigned long long LogEndOffset(const std::wstring &path) {
    WIN32_FILE_ATTRIBUTE_DATA info{};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &info))
        return 0;
    return (static_cast<unsigned long long>(info.nFileSizeHigh) << 32) |
           static_cast<unsigned long long>(info.nFileSizeLow);
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

    // --- 0. which of the two images to drive --------------------------------
    //
    // The STOCK build is the default. That is a change of direction, and the
    // reason is the one host that produces a picture: it drives the runtime
    // WITHOUT modifying it - no writes into the image at all, no hand-made
    // Notify call - because the build it uses installs its own hooks and owns
    // the frame from there. Our patched build is the opposite shape: patch
    // 0x1ffc disables that hook installer so the host has to drive everything
    // by hand.
    //
    // Both images are the same file with five in-place patches, so the offset
    // table above belongs to either one - the patches change bytes, not layout.
    // Which is loaded is therefore a one-variable A/B, and it needs no
    // reinstall: prepare_amd_runtime.py writes both files side by side.
    //
    // NS_AMD_PATCHED=1 selects the patched image (the previously tested path).
    bool want_patched = false;
    {
        char v[8] = {};
        const DWORD got = GetEnvironmentVariableA("NS_AMD_PATCHED", v, sizeof(v));
        want_patched = got > 0 && got < sizeof(v) && v[0] == '1';
    }
    const std::wstring chosen_name = want_patched ? kRuntimeNamePatched : kRuntimeName;
    const std::wstring runtime_path = runtime_dir + L"\\" + chosen_name;
    const std::wstring weights_path = runtime_dir + L"\\" + kWeightsName;
    const std::wstring ini_path = runtime_dir + L"\\" + kIniName;
    // Kept for WriteScale: the menu's Intensity reaches the network as `Scale`
    // in this file, which is the only route the runtime offers for it.
    ini_path_ = ini_path;

    // --- 1. the runtime file, size and hash ------------------------------
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExW(runtime_path.c_str(), GetFileExInfoStandard, &fad)) {
        last_error_ = narrow(chosen_name) + " not found in " + narrow(runtime_dir);
        return false;
    }
    {
        LARGE_INTEGER li{};
        li.LowPart = fad.nFileSizeLow;
        li.HighPart = static_cast<LONG>(fad.nFileSizeHigh);
        const uint64_t size = static_cast<uint64_t>(li.QuadPart);
        if (size != kRuntimeSize) {
            last_error_ = narrow(chosen_name) + " is " + std::to_string(size) +
                          " bytes, the known build is " +
                          std::to_string(kRuntimeSize) + " (this table belongs "
                          "to exactly one image; refusing rather than guessing)";
            return false;
        }
    }
    uint8_t actual[32] = {};
    if (!sha256_of_file(runtime_path, actual)) {
        last_error_ = "could not hash " + narrow(chosen_name);
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
            last_error_ = narrow(chosen_name) + " is not a build these offsets "
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
    // and carry the right keys before the module is loaded. Several of them
    // cannot be reached any other way:
    //
    //   UseFsrInputs   arms the ffxDispatch hook, but only when it is read
    //                  from the FILE - the runtime reads it there and the
    //                  host's later write cannot re-arm a hook that was never
    //                  installed. This is the key that decides whether the
    //                  pass runs at all.
    //   Scale          the network's strength, and the menu's Intensity. The
    //                  runtime's own default (0.03125) is what makes the pass
    //                  look invisible: measured on real content, 0.03 is where
    //                  detail appears without ringing and 0.125 (the ceiling)
    //                  produces halos.
    //   InlineWaitMs   the inline wait budget. The built-in default is 600 ms
    //                  while a job takes tens of milliseconds, so every host
    //                  in the ecosystem lowers it.
    //
    // Written per key and only when that key is ABSENT: a user who tuned
    // InlineWaitMs or Scale by hand keeps their value, while a copy left over
    // from an earlier version - which wrote a file with one key in it, or an
    // empty one - is completed rather than ignored. That upgrade path matters:
    // the file is created once and every later release would otherwise run
    // against the first version's keys, which is precisely how UseFsrInputs
    // would stay unset for the very people testing the fix.
    if (GetFileAttributesW(ini_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
        HANDLE ini = CreateFileW(ini_path.c_str(), GENERIC_WRITE, 0, nullptr,
                                 CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (ini != INVALID_HANDLE_VALUE) CloseHandle(ini);
    }
    {
        const wchar_t *keys[][2] = {
            { L"Enabled",       L"1" },
            { L"UseFsrInputs",  L"1" },   // arms the ffxDispatch hook
            { L"UseDepth",      L"0" },
            { L"Interop",       L"1" },
            { L"Inline",        L"1" },
            { L"InlineWaitMs",  L"200" },
            { L"Temporal",      L"1" },
            { L"Tonemap",       L"-1" },  // -1 = auto by input format
            { L"HipDevice",     L"-1" },  // -1 = match the D3D12 adapter
            { L"Scale",         L"0.03000" },
            { L"UseAutoMask",   L"1" },
            { L"ToneChannels",  L"0" },
        };
        for (const auto &kv : keys) {
            wchar_t have[64] = {};
            const DWORD got = GetPrivateProfileStringW(
                L"DlssNrOnAmd", kv[0], L"\x1", have, _countof(have), ini_path.c_str());
            // The default sentinel is one character, so anything longer means
            // the key is present and the user's value stands.
            if (got > 1) continue;
            WritePrivateProfileStringW(L"DlssNrOnAmd", kv[0], kv[1], ini_path.c_str());
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
    // Where the runtime's log ended BEFORE it loads: the hook wait below reads
    // only what this run appends, and this is the mark it reads from. The
    // runtime appends to the same file across runs, so a check against the
    // whole file would find a previous launch's hook lines and skip the wait.
    log_from_ = LogEndOffset(runtime_dir + L"\\dlssnr_on_amd.log");

    // The DLL-load-dir flag is what lets the runtime find its own dependencies
    // next to itself.
    module_ = LoadLibraryExW(runtime_path.c_str(), nullptr,
                             LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                 LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (module_ == nullptr) {
        last_error_ = "LoadLibrary of " + narrow(chosen_name) + " failed (error " +
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
    // The ORDER is load-bearing, and so is writing Inline TWICE. The
    // reference does exactly this (its step 4, then again at step 6 right
    // before Interop and Init) and says why: "the ini is read in the
    // runtime's DllMain and these writes land after that, so anything set
    // here wins. That was tried the other way round - leaving them to the
    // ini so Inline=0 could be tested - and the result was a black frame,
    // so they are pinned again. Do not unpin them without testing one
    // variable at a time."
    //
    // Our own first Radeon report showed what losing that costs: the
    // runtime's log recorded `mode async` while this host believed it had
    // asked for inline, and inline is what the whole chain assumes - async
    // hands back an earlier frame and nothing downstream has a path for it.
    // InlineWaitMs alone does not buy the mode back.
    //
    // Inline=1: the engine completes the job on the frame it was given.
    // Interop=1: zero-copy shared textures (the runtime's own key).
    //
    // UseFsrInputs=1 is the one that took a second live report to find, and
    // it is the difference between a working pass and a silent one: the flag
    // arms the runtime's ffxDispatch hook. With 0 the runtime initialises,
    // runs its threads, reports healthy - and never processes a frame,
    // saying nothing. Its own log is the witness:
    //
    //     frames 9000 dispatches 0 (0.00/frame) submitted 0 ready -1 route backbuffer
    //
    // Nine thousand frames, no dispatch, the picture black. This host was
    // the source of that line: it fed the frame through its own packet and
    // never dispatched FSR, so the engine had nothing to attach to and fell
    // back to routing a backbuffer that was never ours.
    //
    // UseDepth=0: depth is off until the worker has a depth source worth
    // handing over.
    At<uint8_t>(module_, rva::kInlineMode) = 1;
    At<uint8_t>(module_, rva::kEnabled) = 1;
    At<uint8_t>(module_, rva::kInlineMode) = 1;   // again, after Enabled
    At<uint8_t>(module_, rva::kInterop) = 1;
    At<uint8_t>(module_, rva::kUseFsrInputs) = 1;
    At<uint8_t>(module_, rva::kUseDepth) = 0;

    // --- 6b. wait for the runtime's own hooks ---------------------------
    // The runtime installs its D3D12/DXGI detours from a thread it starts on
    // load, and it builds a dummy device first. Anything the host creates
    // before those land is invisible to it: a swapchain made too early never
    // enters its swapchain -> queue map, and its only fallback
    // (IDXGISwapChain::GetDevice for a D3D12 queue) cannot work - the frame is
    // discarded as "a swapchain that is not on our device" and never retried.
    //
    // This is a wait for EVIDENCE, not a sleep: the runtime names every detour
    // in its own log, and the last one it prints is Present1. The budget is
    // generous because it costs nothing when the hooks land early, and the
    // alternative - creating our swapchain first - is the silent passthrough
    // this whole release is about.
    {
        // The runtime's log lives in its own directory and is opened in APPEND
        // mode across runs, so the wait below searches only what THIS run
        // appended: `log_from` is where the file ended before the module was
        // loaded. Without that the check passes on the first line a previous
        // launch left behind, the wait returns instantly, and the swapchain is
        // created into a detour that is not installed yet - which is the
        // silent-passthrough path, and it is what our own log showed:
        //
        //     [amd] the runtime's D3D12/DXGI hooks are in place (0 ms)
        // Readiness is read from what THIS image actually writes, and that is
        // not the D3D12/DXGI detour list: patch 0x1ffc disables the runtime's
        // own hook-installer thread on purpose (the host owns the frame), so
        // our runs never log `hooked IDXGIFactory...` at all - live logs from
        // three different Radeons show the wait failing on every single launch
        // while the engine was working. Waiting for a line our own patch
        // removed is a check that cannot pass; it reported "the runtime's hooks
        // were NOT seen" on healthy machines and sent readers after a fault
        // that was not there.
        //
        // What the engine DOES print once it is really up, verified on those
        // same live logs:
        //
        //     engine init ok
        //     hooked amd_fidelityfx_upscaler_dx12.dll!ffxCreateContext
        //
        // The ffxCreateContext line is the load-bearing one: it appears when
        // the runtime detours OUR upscaler from OUR process, which is exactly
        // the condition the old wait was trying to establish. `engine init ok`
        // is what the engine says when it has accepted the device and the
        // weights.
        const std::wstring log_path = runtime_dir + L"\\dlssnr_on_amd.log";
        // The FIRST of these to appear ends the wait.
        const char *kReadyMarkers[] = {
            "hooked amd_fidelityfx_upscaler_dx12.dll!ffxCreateContext",
            "engine init ok",
        };
        const DWORD budget_ms = 5000;
        DWORD waited = 0;
        bool hooked = false;
        while (waited < budget_ms) {
            unsigned long long now_end = 0;
            for (const char *marker : kReadyMarkers) {
                if (LogContains(log_path, marker, log_from_, &now_end)) {
                    hooked = true;
                    break;
                }
            }
            if (hooked) break;
            Sleep(25);
            waited += 25;
        }
        if (hooked) {
            hooks_ms_ = waited;
            hooks_seen_ = true;
        } else {
            hooks_seen_ = false;
        }
    }

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

    // Intensity is the exception: it is NOT in that block. The runtime reads
    // its strength from `Scale` in its own ini, so the slider has to be
    // written to the file - and only when it changes, because rewriting the
    // ini per frame would be a file write per frame for nothing.
    //
    // This was dead: the slider moved the host's own composite and nothing
    // else, so the network's strength never changed on the AMD path while the
    // NVIDIA path's did.
    if (opt.intensity != last_intensity_) {
        last_intensity_ = opt.intensity;
        WriteScale(opt.intensity);
    }
}

void Runtime::WriteScale(float intensity) {
    if (ini_path_.empty()) return;

    // Intensity 1.0 means this much network strength, and the number is
    // calibrated on real content rather than guessed - it is the one value the
    // reference measured and published:
    //
    //   0.005  invisible (1.0/255 of contribution)
    //   0.03   detail appears - skin, hair, fabric (6.1/255)
    //   0.125  the runtime's own ceiling: halos, crunch, fringing (55/255)
    //
    // So the slider spans [0, kScaleMax] and its top is deliberately NOT the
    // runtime's ceiling: 1.0 has to mean "the setting that looks right", not
    // "the most the runtime will accept". NS_AMD_NR_SCALE_MAX moves it for
    // taste without a rebuild.
    float scale_max = 0.03f;
    char env[32] = {};
    if (GetEnvironmentVariableA("NS_AMD_NR_SCALE_MAX", env, sizeof(env))) {
        const float v = static_cast<float>(atof(env));
        if (v > 0.0f && v <= 4.0f) scale_max = v;
    }
    const float clamped = intensity < 0.0f ? 0.0f
                        : (intensity > 1.0f ? 1.0f : intensity);
    const float scale = clamped * scale_max;

    char value[32] = {};
    _snprintf_s(value, sizeof(value), _TRUNCATE, "%.5f", scale);
    if (!WritePrivateProfileStringW(L"DlssNrOnAmd", L"Scale",
                                    std::wstring(value, value + strlen(value)).c_str(),
                                    ini_path_.c_str())) {
        last_error_ = "the intensity could not be written to the runtime's ini";
        return;
    }
    // Kept for the caller to log: this translation unit has no Log() (the
    // bridge owns it), which is the same reason HooksSeen()/HooksMs() exist.
    // The write is the difference between a slider that reaches the network and
    // one that does not, so it is worth a line.
    scale_written_ = scale;
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
