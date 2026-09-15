// The AMD runtime driver - see amd_runtime.h for the contract.
//
// Everything here is deliberately paranoid: the runtime exports nothing, so a
// mistake is not an error code, it is a jump into an address that may or may
// not be the function we meant. The hash check is the only thing standing
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

std::string narrow(const std::wstring &wide) {
    std::string out;
    out.reserve(wide.size());
    for (wchar_t c : wide) out.push_back(static_cast<char>(c & 0x7F));
    return out;
}

// hipGetDevicePropertiesR0600 fills a ~1 KB struct; the working implementation
// uses a 4096-byte, 8-byte aligned buffer because a smaller one is a stack
// overrun rather than an error return. Only one field is read: the adapter LUID
// at byte offset 272.
struct alignas(8) HipProps {
    unsigned char raw[4096];
};

constexpr size_t kHipPropsLuidOffset = 272;

}  // namespace

Runtime::~Runtime() {
    // No shutdown path exists in the runtime's public surface and the process
    // is about to go away anyway; releasing the modules here would be the one
    // call that could crash on the way out. The reference leaves it loaded
    // forever for the same reason: the engine keeps worker threads that hold
    // references into its own image.
}

bool Runtime::Load(const std::wstring &runtime_dir, ID3D12Device *device,
                   ID3D12CommandQueue *queue) {
    ready_ = false;
    last_error_.clear();

    const std::wstring runtime_path = runtime_dir + L"\\" + kRuntimeName;
    const std::wstring weights_path = runtime_dir + L"\\" + kWeightsName;
    const std::wstring ini_path = runtime_dir + L"\\" + kIniName;

    // --- 1. the runtime file, and is it the build we know ---------------
    if (GetFileAttributesW(runtime_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
        last_error_ = "dlssnr_amd_pass1.dll not found in " + narrow(runtime_dir);
        return false;
    }
    uint8_t actual[32] = {};
    if (!sha256_of_file(runtime_path, actual)) {
        last_error_ = "could not hash dlssnr_amd_pass1.dll";
        return false;
    }
    found_hash_ = to_hex(actual, 32);
    for (int i = 0; i < 32; ++i) {
        if (actual[i] != kRuntimeSha256[i]) {
            // Refused on purpose: the offsets below belong to one build. A
            // different one does not fail, it jumps into nothing.
            last_error_ = "dlssnr_amd_pass1.dll is not the build these offsets "
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
    // before the module is loaded. The working installation ships it empty.
    if (GetFileAttributesW(ini_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
        HANDLE ini = CreateFileW(ini_path.c_str(), GENERIC_WRITE, 0, nullptr,
                                 CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (ini != INVALID_HANDLE_VALUE) CloseHandle(ini);
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
    // The R0600 suffix is the ROCm 6.0 hipDeviceProp_t ABI name, not a typo.
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

    // Device selection is by adapter LUID, not by index: the two APIs
    // enumerate in their own orders. A mismatch is fatal - textures allocated
    // on one adapter are not reachable from the other - so this does NOT fall
    // back to "first device" silently.
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
        if (have_target &&
            memcmp(props.raw + kHipPropsLuidOffset, &target, sizeof(target)) == 0) {
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

    // --- 3. the runtime module and its three addresses ------------------
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
    init_ = reinterpret_cast<InitFn>(
        reinterpret_cast<uintptr_t>(module_) + rva::kInit);
    record_ = reinterpret_cast<RecordFn>(
        reinterpret_cast<uintptr_t>(module_) + rva::kRecord);
    notify_ = reinterpret_cast<NotifyFn>(
        reinterpret_cast<uintptr_t>(module_) + rva::kNotify);

    // --- 4. bind the device and the queue the caller owns ---------------
    // The engine holds these pointers for its own threads, so the caller's
    // references must outlive this object - and the engine's reference is
    // taken explicitly.
    At<ID3D12Device *>(module_, rva::kDevice) = device;
    if (device != nullptr) device->AddRef();
    At<ID3D12CommandQueue *>(module_, rva::kQueue) = queue;
    if (queue != nullptr) queue->AddRef();
    At<int>(module_, rva::kHipDevice) = chosen;

    // Inline=1: the engine completes the job on the frame it was given, which
    // is what the rest of the chain assumes. Async hands back the previous
    // frame and the chain has no path for that. Interop=1 likewise pinned.
    // These writes land AFTER the runtime's DllMain (which reads the ini), so
    // they win over the file - deliberately. Order matters and mirrors the
    // reference: Inline, Enabled, Inline again, Interop - then init.
    At<uint8_t>(module_, rva::kInlineMode) = 1;
    At<uint8_t>(module_, rva::kEnabled) = 1;
    At<uint8_t>(module_, rva::kInlineMode) = 1;
    At<uint8_t>(module_, rva::kInterop) = 1;

    // --- 5. init ---------------------------------------------------------
    // Arg 1 is the engine's own in-image context struct, not a host object:
    // it is the same address the device, queue and HIP index were written to.
    ctx_ = reinterpret_cast<void *>(reinterpret_cast<uintptr_t>(module_) +
                                    rva::kInitCtx);
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

    // Desktop capture has no engine motion vectors, so the "FSR inputs" path
    // does not apply; the caller feeds its own motion texture. Depth is left
    // off until the worker actually has a depth source.
    At<uint8_t>(module_, rva::kUseFsrInputs) = 0;
    At<uint8_t>(module_, rva::kUseDepth) = 0;

    ready_ = true;
    return true;
}

void Runtime::SetOptions(const Options &opt) {
    if (!ready_ || module_ == nullptr) return;
    At<float>(module_, rva::kLocalTone) = opt.local_tone;
    At<float>(module_, rva::kLocalStructure) = opt.local_structure;
    At<float>(module_, rva::kSkinStructure) = opt.skin_structure;
    At<uint8_t>(module_, rva::kEnabled) = opt.enabled ? 1 : 0;
}

bool Runtime::Record(ID3D12CommandList *list, ID3D12Resource *colour,
                     ID3D12Resource *motion, ID3D12Resource *depth,
                     ID3D12Resource *exposure, float scale_x, float scale_y) {
    if (!ready_ || record_ == nullptr) return false;

    // Per-frame state, written strictly before the record call (the reference
    // writes these every frame in this order). The depth pair is written even
    // though depth is unused - the engine expects the fields populated.
    At<uint8_t>(module_, rva::kPerPassFlag) = 1;
    At<uint32_t>(module_, rva::kDepthInverted) = 0;
    At<uint8_t>(module_, rva::kDepthExplicit) = 1;

    Packet packet{};
    packet.list = list;
    packet.colour = colour;        // read AND written in place
    packet.colourState = kPacketState;
    packet.motion = motion;        // required non-null in the reference; the
                                   // caller supplies a zeroed field without motion
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
    return true;
}

void Runtime::Notify(ID3D12CommandQueue *queue, ID3D12CommandList *list) {
    if (!ready_ || notify_ == nullptr) return;
    ID3D12CommandList *lists[] = {list};
    __try {
        notify_(queue, 1, lists);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // Nothing useful to do here - the caller finds out through the
        // output not changing and the log carries the fact.
        last_error_ = "the engine raised an exception in notify";
    }
}

uint32_t Runtime::RecordedJobs() const {
    if (!ready_ || module_ == nullptr) return 0;
    return At<uint32_t>(module_, rva::kJobCounter);
}

uint32_t Runtime::CompletedJobs() const {
    if (!ready_ || module_ == nullptr) return 0;
    return At<uint32_t>(module_, rva::kSyncCounter);
}

bool Runtime::LastRecordRefused() const {
    if (!ready_ || module_ == nullptr) return true;
    return At<uint8_t>(module_, rva::kStatusFlag) != 0;
}

void Runtime::InvalidateHistory() {
    if (!ready_ || module_ == nullptr) return;
    At<uint8_t>(module_, rva::kWantHistory) = 0;
    At<void *>(module_, rva::kHistory) = nullptr;
}

}  // namespace amd_nr
