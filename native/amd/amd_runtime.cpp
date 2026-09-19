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

//: Returned by guarded_init when the build's table carries no address for the
//: entry point. Not an exception code - the caller distinguishes it so the log
//: says "this build does not publish it" instead of reporting a fault that did
//: not happen. Chosen outside the range GetExceptionCode() uses.
constexpr int kNoInitAddress = -1000;


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
// C++ objects (C2712). Raw pointers only - which is why the offset arrives as
// a plain uintptr_t rather than being read from the table here: this function
// has no `this`, and the table belongs to the instance.
int guarded_init(void *module, uintptr_t init_rva, void *ctx,
                 const std::string *weights) {
    using InitFn = bool(__fastcall *)(void *, const std::string *);
    // 0 = the table has no address for this build. Without this, module + 0 is
    // the PE header and the call would execute it as code - the exception
    // handler would then report a wrong-build fault about a correctly built
    // image, which is the wrong conclusion (and the probe had the same shape).
    if (init_rva == 0) return kNoInitAddress;
    auto fn = reinterpret_cast<InitFn>(
        reinterpret_cast<uintptr_t>(module) + init_rva);
    __try {
        return fn(ctx, weights) ? 0 : 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -static_cast<int>(GetExceptionCode());
    }
}

int guarded_shutdown(void *module, uintptr_t shutdown_rva) {
    using Fn = void (*)(void);
    auto fn = reinterpret_cast<Fn>(reinterpret_cast<uintptr_t>(module) + shutdown_rva);
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
    // The STOCK build is the default. Both hosts that produce a picture drive
    // the runtime WITHOUT modifying it - no writes into the image at all, no
    // hand-made Notify call - because the build they use installs its own hooks
    // and owns the frame from there. Our patched build is the opposite shape:
    // patch 0x1ffc disables that hook installer so the host has to drive
    // everything by hand.
    //
    // Both images are the same file with a few in-place patches, so the offset
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
    // NS_AMD_V0310=1 selects the v0.3.1 image, which the user gets by running
    // prepare_amd_runtime.py on that release's own installer. It is a separate
    // FILE rather than a replacement for the same reason the patched variant is:
    // switching builds becomes one variable and no reinstall, and both stay
    // available for a direct comparison on the same machine. The offset table is
    // chosen from the file's hash, not from this flag - the flag only says which
    // file to open.
    bool want_v0310 = false;
    {
        char v[8] = {};
        const DWORD got = GetEnvironmentVariableA("NS_AMD_V0310", v, sizeof(v));
        want_v0310 = got > 0 && got < sizeof(v) && v[0] == '1';
    }
    const std::wstring chosen_name = want_v0310 ? kRuntimeNameV0310
                                                : (want_patched ? kRuntimeNamePatched
                                                                : kRuntimeName);
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
    uint64_t size = 0;
    {
        LARGE_INTEGER li{};
        li.LowPart = fad.nFileSizeLow;
        li.HighPart = static_cast<LONG>(fad.nFileSizeHigh);
        size = static_cast<uint64_t>(li.QuadPart);
    }
    // The size is checked against BOTH known builds rather than one, because
    // the same host now drives two of them and the table is picked from the
    // pair (size, hash) below. The check still does its original job: it catches
    // truncation and a half-extracted file before the hash is computed.
    if (size != kRuntimeSize && size != kRuntimeSize0310) {
        last_error_ = narrow(chosen_name) + " is " + std::to_string(size) +
                      " bytes, the known builds are " +
                      std::to_string(kRuntimeSize) + " (v0.2.17) and " +
                      std::to_string(kRuntimeSize0310) + " (v0.3.1) - this table "
                      "belongs to exactly those images; refusing rather than guessing";
        return false;
    }
    uint8_t actual[32] = {};
    if (!sha256_of_file(runtime_path, actual)) {
        last_error_ = "could not hash " + narrow(chosen_name);
        return false;
    }
    found_hash_ = to_hex(actual, 32);
    {
        // The hash is what selects the table - never the size alone, and never
        // a version string the file claims. A build whose hash is unknown is
        // refused: its layout is different by definition, and driving it with
        // the wrong table writes into read-only memory.
        auto matches = [&actual](const uint8_t (&want)[32]) {
            for (int i = 0; i < 32; ++i)
                if (actual[i] != want[i]) return false;
            return true;
        };
        if (matches(kRuntimeSha256Stock)) {
            kind_ = ImageKind::Stock;
            table_ = &rva::kV0217;
        } else if (matches(kRuntimeSha256Patched)) {
            kind_ = ImageKind::Patched;
            table_ = &rva::kV0217;
        } else if (matches(kRuntimeSha256Stock0310)) {
            // v0.3.1 has one published image; the patched variant does not
            // exist for it because the patches themselves were never carried
            // over (see the note in prepare_amd_runtime.py).
            //
            // Stock0310 and not Stock: the offset table is chosen from this, and
            // the log prints it. Reporting v0.3.1 as "stock v0.2.17" would make
            // a report claim the wrong release was loaded - in a log whose whole
            // job is to say which build ran.
            kind_ = ImageKind::Stock0310;
            table_ = &rva::kV0310;
            build_ = Build::V0310;
        } else {
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

    // Device selection. Two things are going on here and they are NOT the same
    // job: finding which HIP device matches the D3D12 device we hand over (so
    // a mismatch can be refused loudly), and telling the runtime which device
    // to use (which the runtime now does better than we can).
    //
    // The match is by adapter LUID at +272, not by index: the two APIs
    // enumerate in their own orders (the same lesson as issue #81 on the
    // NVIDIA side), and the textures the host hands over are only reachable
    // from the matching adapter. One physical card regularly appears two to
    // four times with DIFFERENT LUIDs, so "the first AMD adapter" is exactly
    // the heuristic that produced upstream's multi-GPU black screens.
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
    // But the runtime is NOT told this index, and that is deliberate.
    //
    // The ini key `HipDevice` is an OVERRIDE: with an index written there the
    // runtime uses it and logs `(HipDevice in the ini)`, skipping its own
    // selection entirely. With -1 it matches a HIP device to the D3D12 device
    // behind the first presented swapchain and says so in its own log
    // (`matches the game's D3D12 adapter`, `<- the adapter the game renders
    // on`). That auto path IS the upstream fix for this project's
    // multi-GPU/iGPU reports (v0.2.17: "Fixed crashes and black screens on
    // multi-GPU systems"), and it is the path the maintainer points at when a
    // user's card is picked wrong. Writing our own index pre-empts it - and
    // the runtime's log line is how a report proves which device it took, so
    // pre-empting it also removes the evidence.
    //
    // The value written above (kHipDevice) is therefore -1, and the LUID pass
    // above is kept for what it is good at: refusing a machine where NO HIP
    // device matches the adapter, before the runtime wastes a frame on it.

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
    // Address 0 in the table means "this build does not publish it" - the
    // derived tables carry 0 for the entry points that were never derived on
    // v0.3.1. `module_ + 0` is NOT that address: it is the MZ header of the
    // image, so a call through it would execute 0x00905A4D as code. Each one
    // becomes nullptr instead, and the callers already check for nullptr.
    const uintptr_t base = reinterpret_cast<uintptr_t>(module_);
    init_ = table_->kInit == 0 ? nullptr
        : reinterpret_cast<InitFn>(base + table_->kInit);
    record_ = table_->kRecord == 0 ? nullptr
        : reinterpret_cast<RecordFn>(base + table_->kRecord);
    notify_ = table_->kNotify == 0 ? nullptr
        : reinterpret_cast<NotifyFn>(base + table_->kNotify);

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
    At<ID3D12Device *>(module_, table_->kDevice) = device;
    if (device != nullptr) device->AddRef();
    At<ID3D12CommandQueue *>(module_, table_->kQueue) = queue;
    if (queue != nullptr) queue->AddRef();
    // -1 = auto: the runtime matches a HIP device to the D3D12 device behind
    // the first presented swapchain itself, and says which one it took in
    // its own log. See the selection block above for why this is not the
    // index we just computed.
    At<int>(module_, table_->kHipDevice) = -1;

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
    At<uint8_t>(module_, table_->kInlineMode) = 1;
    At<uint8_t>(module_, table_->kEnabled) = 1;
    At<uint8_t>(module_, table_->kInlineMode) = 1;   // again, after Enabled
    At<uint8_t>(module_, table_->kInterop) = 1;
    At<uint8_t>(module_, table_->kUseFsrInputs) = 1;
    At<uint8_t>(module_, table_->kUseDepth) = 0;

    // --- 6b. wait for the runtime's own hooks ---------------------------
    // The runtime installs its D3D12/DXGI detours from a thread it starts on
    // load, and it builds a dummy device first. Anything the host creates
    // before those land is invisible to it: a swapchain made too early never
    // enters its swapchain -> queue map, and its only fallback
    // (IDXGISwapChain::GetDevice for a D3D12 queue) cannot work - the frame is
    // discarded as "a swapchain that is not on our device" and never retried.
    //
    // This is a wait for EVIDENCE, not a sleep. The evidence is the detour
    // lines the runtime prints for itself - and WHICH LINES those are depends
    // on which build is loaded, which this loader already knows by hash
    // (kind_, set above):
    //
    //   STOCK   the hook-installer thread is intact, so every detour is
    //           announced: `hooked ID3D12CommandQueue::ExecuteCommandLists`,
    //           `hooked IDXGIFactory2::CreateSwapChainForHwnd`,
    //           `hooked IDXGISwapChain1::Present1`. The last of these is the
    //           one that has to be in place before our swapchain is created.
    //
    //   PATCHED patch 0x1ffc DISABLES that installer on purpose (the host owns
    //           the frame), so none of those lines will ever appear. Waiting
    //           for one is a check that cannot pass - and that is exactly what
    //           it did: live logs from three different Radeons carried
    //           "the runtime's hooks were NOT seen" on every launch while the
    //           engine was working. For this build there is nothing to wait
    //           for, and the honest answer is "not applicable", not "not seen".
    //
    // The ffxCreateContext line is NOT usable as a marker either: the runtime
    // only detours our upscaler once the upscaler is loaded, and this host
    // loads it after this call returns (AmdInit -> fsr.Load). Waiting for it
    // here waits for something this function is itself responsible for.
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
        const std::wstring log_path = runtime_dir + L"\\dlssnr_on_amd.log";
        // On v0.2.17 the setup thread is alive in BOTH images: the patch that
        // used to disable it must not be applied any more (the same thread
        // resolves the d3d12.dll/dxgi.dll proxy, and nop'ing it faults at
        // address 0). Both images therefore install their detours and both
        // print the lines this wait looks for, so it applies to both.
        //
        // What still differs between them is the NOTIFY: patch 0x8583 removes
        // the call the detour makes after ExecuteCommandLists, so on the
        // patched image the host supplies it. A detour being installed is a
        // different question from who announces the submission.
        {
            hooks_applicable_ = true;
            // The FIRST of these to appear ends the wait: the swapchain is the
            // one that must land before we create ours, and it is printed last.
            const char *kReadyMarkers[] = {
                "hooked IDXGISwapChain1::Present1",
                "env: d3d12 device yes",
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
    }

    // --- 7. init ---------------------------------------------------------
    // Arg 1 is the engine's own in-image context struct, not a host object:
    // the address itself, where the device, queue and HIP index were written.
    ctx_ = reinterpret_cast<void *>(reinterpret_cast<uintptr_t>(module_) +
                                    table_->kInitCtx);
    // The device is bound on this thread once more right before init - the
    // engine selects it on its own threads from the stored index.
    hip_set_(chosen);
    const std::string weights_narrow = narrow(weights_path);
    const int rc = guarded_init(module_, table_->kInit, ctx_, &weights_narrow);
    if (rc == kNoInitAddress) {
        // Said as what it is. Falling through to the exception branch would
        // print "EXCEPTION 0x00000001", which reads as a crash of the engine
        // and is not one.
        last_error_ = "this build does not publish the init entry point "
                      "(the offset table has no address for it)";
        return false;
    }
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
    At<uint8_t>(module_, table_->kFlagAfterInit) = 1;

    // Ask the ENGINE's own log whether it actually came up, because our return
    // value cannot answer that. `engine init %s` is the engine's own format
    // string and the line appears only after its initialisation runs; a machine
    // whose engine never reached it still gets `AMD path active` from the host,
    // which is how twelve consecutive launches read as healthy while the engine
    // had not started.
    //
    // Read only what THIS run appended (log_from_): the file is appended to
    // across runs, so a whole-file search is answered by a previous launch.
    {
        const std::wstring engine_log = runtime_dir + L"\\dlssnr_on_amd.log";
        const DWORD budget_ms = 3000;
        DWORD waited = 0;
        while (waited < budget_ms) {
            if (LogContains(engine_log, "engine init ok", log_from_, nullptr)) {
                engine_init_seen_ = true;
                break;
            }
            Sleep(25);
            waited += 25;
        }
    }

    ready_ = true;
    return true;
}

void Runtime::Shutdown() {
    if (module_ == nullptr) return;
    // kShutdown is 0 on a build that does not publish it. Calling
    // module_ + 0 would run the PE header.
    if (table_->kShutdown != 0)
        guarded_shutdown(module_, table_->kShutdown);
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
    At<float>(module_, table_->kLocalTone) = cl01(opt.local_tone);
    At<float>(module_, table_->kLocalStructure) = cl01(opt.local_structure);
    At<float>(module_, table_->kSkinStructure) = cl01(opt.skin_structure);
    At<uint32_t>(module_, table_->kCharMask) = opt.auto_mask ? 1u : 0u;
    At<uint32_t>(module_, table_->kToneChannels) = (opt.tone_channels & ~2u) | 4u;
    At<uint8_t>(module_, table_->kEnabled) = opt.enabled ? 1 : 0;

    // Intensity: the network's strength, and the slider that was dead.
    //
    // It used to be written to the ini file and nowhere else, on the reading
    // that the runtime takes this one from `Scale` in its own file. That
    // reading is wrong for this build, and the disassembly says why: the ini
    // is parsed by one function (0x7af0..0x801a) reached through a call_once
    // guard inside the first CreateSwapChain detour (0x97c0), immediately
    // before the engine's Init. It runs ONCE. A value written to the file
    // after that is never read by anything - so the slider moved the host's
    // own composite while the network's strength stayed at whatever the file
    // held when the guard ran.
    //
    // The field itself is read per frame: 0x140a0..0x15e79 is the recording
    // function, the same one that reads LocalTone and LocalStructure, and this
    // driver has always written those directly. Intensity belongs there with
    // them.
    //
    // The file is still written, on change, because it answers a different
    // question: it is what the next launch starts from. The field is what
    // this launch uses.
    const float intensity_clamped = opt.intensity < 0.0f ? 0.0f
                                  : (opt.intensity > 1.0f ? 1.0f : opt.intensity);
    At<float>(module_, table_->kScale) = intensity_clamped * ScaleMax();
    if (opt.intensity != last_intensity_) {
        last_intensity_ = opt.intensity;
        WriteScale(opt.intensity);
    }
}

float Runtime::ScaleMax() {
    // Intensity 1.0 means this much network strength, and the number is
    // calibrated on real content rather than guessed - it is the one value the
    // reference measured and published:
    //
    //   0.005  invisible (1.0/255 of contribution)
    //   0.03   detail appears - skin, hair, fabric (6.1/255)
    //   0.125  the runtime's own ceiling: halos, crunch, fringing (55/255)
    //
    // So the slider spans [0, this] and its top is deliberately NOT the
    // runtime's ceiling: 1.0 has to mean "the setting that looks right", not
    // "the most the runtime will accept". NS_AMD_NR_SCALE_MAX moves it for
    // taste without a rebuild.
    // Read once: this is called per frame (SetOptions), and the answer cannot
    // change inside a run.
    static const float scale_max = [] {
        float v = 0.03f;
        char env[32] = {};
        if (GetEnvironmentVariableA("NS_AMD_NR_SCALE_MAX", env, sizeof(env))) {
            const float parsed = static_cast<float>(atof(env));
            if (parsed > 0.0f && parsed <= 4.0f) v = parsed;
        }
        return v;
    }();
    return scale_max;
}

void Runtime::WriteScale(float intensity) {
    if (ini_path_.empty()) return;

    const float clamped = intensity < 0.0f ? 0.0f
                        : (intensity > 1.0f ? 1.0f : intensity);
    const float scale = clamped * ScaleMax();

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
    At<uint8_t>(module_, table_->kPerPassFlag) = 1;   // Temporal
    At<uint32_t>(module_, table_->kDepthInverted) = 0;
    At<uint8_t>(module_, table_->kDepthExplicit) = 1;

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
        At<ID3D12CommandList *>(module_, table_->kPendingList) == list;
    if (!accepted) {
        last_error_ = "the engine did not take the recorded list";
        return false;
    }
    // Nothing else is written on acceptance.
    //
    // What used to be here: a `InterlockedExchange(..., 0)` on 0x8d808, to
    // clear what the driver believed was a stale abort token. That address is
    // the engine's watchdog job counter - the watchdog function stores the job
    // id into 0x8d808 and 0x8d80c when a timeout fires and reads them back -
    // so the "clear" was erasing the engine's own record of which job timed
    // out, on every accepted frame. The engine resets its real abort flag
    // itself, through hipMemcpyAsync.
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
    // A build that does not expose the completed-jobs counter cannot be waited
    // on by it. Read at address 0 it would return the PE header ("MZ\0\0" =
    // 0x00905A4D, 9 466 189) and the loop would return instantly, claiming the
    // jobs finished. Saying "not waitable" is the honest answer; the caller
    // treats it the same as a timeout.
    if (table_->kSyncCounter == 0) return false;
    const ULONGLONG deadline = GetTickCount64() + timeout_ms;
    while (At<uint32_t>(module_, table_->kSyncCounter) < wanted) {
        if (GetTickCount64() > deadline) return false;
        Sleep(1);  // yield, not spin: the worker needs the core too
    }
    return true;
}

uint32_t Runtime::JobCount() const {
    if (!ready_ || module_ == nullptr) return 0;
    return At<uint32_t>(module_, table_->kJobCounter);
}

uint32_t Runtime::SyncCount() const {
    if (!ready_ || module_ == nullptr) return 0;
    // 0 = "this build does not publish it", and reading address 0 would return
    // the PE header instead - a made-up number in a log whose whole job is to
    // be believed. SyncKnown() is how a caller tells that from a real 0.
    if (table_->kSyncCounter == 0) return 0;
    return At<uint32_t>(module_, table_->kSyncCounter);
}

uint32_t Runtime::TimeoutCount() const {
    if (!ready_ || module_ == nullptr) return 0;
    if (table_->kTimeoutCounter == 0) return 0;   // see SyncCount()
    return At<uint32_t>(module_, table_->kTimeoutCounter);
}

// Whether this build publishes the two diagnostic counters at all. The log
// prints "n/a" rather than a number when it does not, because a 0 that means
// "unknown" and a 0 that means "nothing went wrong" read identically and are
// not the same answer.
bool Runtime::SyncCountKnown() const {
    return ready_ && module_ != nullptr && table_->kSyncCounter != 0;
}

bool Runtime::TimeoutCountKnown() const {
    return ready_ && module_ != nullptr && table_->kTimeoutCounter != 0;
}

bool Runtime::FailedOnEngineSide() const {
    if (!ready_ || module_ == nullptr) return true;
    return At<uint8_t>(module_, table_->kStatusFlag) != 0;
}

void Runtime::InvalidateHistory() {
    if (!ready_ || module_ == nullptr) return;
    At<uint8_t>(module_, table_->kWantHistory) = 0;
    At<void *>(module_, table_->kHistory) = nullptr;
}

}  // namespace amd_nr
