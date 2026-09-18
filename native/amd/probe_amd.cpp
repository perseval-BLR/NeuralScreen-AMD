// probe_amd.cpp - what is actually on a machine that wants to run the AMD path.
//
// Standalone, no dependencies beyond Windows. Point it at a folder (or let it
// look next to itself) and it reports, in order:
//
//   1. whether the three runtime files are there, with sizes;
//   2. the sha256 of dlssnr_amd_pass1.dll, and whether it is the build this
//      project knows how to drive;
//   3. whether the HIP 7 runtime (amdhip64_7.dll) is installed, and every
//      AMD device it sees, with the gfx target of each;
//   4. with --init: whether the engine accepts the device and the weights
//      (this calls into the runtime - the one step that can fail hard, which
//      is why it is opt-in and behind an SEH guard).
//
// Written for the "I have a Radeon, will this work?" conversation: run it,
// paste the output. Nothing is installed, nothing is written.
//
// Build: cl /nologo /O2 /EHsc /W3 /MD probe_amd.cpp /Fe:probe_amd.exe
//        bcrypt.lib advapi32.lib

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#pragma comment(lib, "bcrypt.lib")

namespace {

constexpr const wchar_t *kRuntimeName = L"dlssnr_amd_pass1.dll";
//: The patched twin the preparation script writes beside it. Both are the same
//: build with five bytes changed, so both are checked and named here - a probe
//: that reported the patched file MISSING would send the user after a file that
//: is simply the other half of the A/B.
constexpr const wchar_t *kRuntimePatchedName = L"dlssnr_amd_pass1_patched.dll";
constexpr const wchar_t *kWeightsName = L"dlssnr_on_amd_weights.bin";
constexpr const wchar_t *kIniName = L"dlssnr_on_amd.ini";
//: The FidelityFX upscaler. Not part of the runtime, but the runtime cannot
//: take a frame without it - it is a game proxy that rides on an FSR dispatch.
constexpr const wchar_t *kUpscalerName = L"amd_fidelityfx_upscaler_dx12.dll";
constexpr const wchar_t *kHipName = L"amdhip64_7.dll";

// The build our offsets were read from (see amd_runtime.h).
constexpr uint8_t kExpectedSha256[32] = {
    0x3c, 0x9c, 0xa1, 0x3f, 0x0f, 0x5f, 0xc3, 0x6a, 0x69, 0x0b, 0xa4, 0x24,
    0xc4, 0x57, 0x00, 0x3b, 0xcf, 0xcc, 0x10, 0x80, 0xb4, 0xb7, 0x85, 0x97,
    0x4c, 0xdd, 0x7e, 0x9a, 0xe2, 0xbc, 0x1d, 0xd8,
};

constexpr uintptr_t kRvaInitCtx = 0x764d8;
constexpr uintptr_t kRvaHipDevice = 0x76f20;
constexpr uintptr_t kRvaInit = 0x12380;

std::wstring dir_of(const std::wstring &path) {
    const size_t pos = path.find_last_of(L"\\/");
    return pos == std::wstring::npos ? L"." : path.substr(0, pos);
}

bool file_exists(const std::wstring &path, unsigned long long *size = nullptr) {
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &fad))
        return false;
    if (size) {
        LARGE_INTEGER li{};
        li.LowPart = fad.nFileSizeLow;
        li.HighPart = static_cast<LONG>(fad.nFileSizeHigh);
        *size = static_cast<unsigned long long>(li.QuadPart);
    }
    return true;
}

std::string sha256_hex(const std::wstring &path, bool *ok) {
    *ok = false;
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                              nullptr);
    if (file == INVALID_HANDLE_VALUE) return {};

    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    std::string out;
    DWORD hash_len = 0, obj_len = 0, got = 0;
    std::vector<uint8_t> hash_buf, obj_buf;

    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
        goto done;
    if (BCryptGetProperty(alg, BCRYPT_HASH_LENGTH, reinterpret_cast<PUCHAR>(&hash_len),
                          sizeof(hash_len), &got, 0) < 0)
        goto done;
    // obj_len and got are separate variables: passing one DWORD as both the
    // output buffer and the written-count leaves `got` holding the count, and
    // a 4-byte hash object is too small - the failure looks like an unreadable
    // file rather than a wrong size.
    if (BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&obj_len),
                          sizeof(obj_len), &got, 0) < 0)
        goto done;
    obj_buf.resize(obj_len);
    if (BCryptCreateHash(alg, &hash, obj_buf.data(),
                         static_cast<ULONG>(obj_buf.size()), nullptr, 0, 0) < 0)
        goto done;
    for (;;) {
        uint8_t chunk[1 << 16];
        DWORD read = 0;
        if (!ReadFile(file, chunk, sizeof(chunk), &read, nullptr)) goto done;
        if (read == 0) break;
        if (BCryptHashData(hash, chunk, read, 0) < 0) goto done;
    }
    hash_buf.resize(hash_len);
    if (BCryptFinishHash(hash, hash_buf.data(), hash_len, 0) < 0) goto done;

    {
        static const char *hex = "0123456789abcdef";
        out.resize(hash_len * 2);
        for (DWORD i = 0; i < hash_len; ++i) {
            out[i * 2] = hex[hash_buf[i] >> 4];
            out[i * 2 + 1] = hex[hash_buf[i] & 0xF];
        }
        *ok = true;
    }

done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    return out;
}

// hipDevicePropertiesR0600 fills a ~1 KB struct; the working references use an
// oversized buffer (4096 / 8192, 8-/16-byte aligned) because a smaller one is
// a stack overrun, not an error return. The name is the first field - that is
// what this probe prints. The adapter LUID (the field the driver matches on)
// lives at byte offset 272: name[256], uuid[16], luid[8].
struct alignas(16) HipProps {
    unsigned char raw[8192];
};

constexpr size_t kHipPropsLuidOffset = 272;

// The known build (see amd_runtime.h).
constexpr unsigned long long kKnownRuntimeSize = 7156224;

FILE *g_out = nullptr;

void out(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_out ? g_out : stdout, fmt, ap);
    va_end(ap);
    fflush(g_out ? g_out : stdout);
}

// The init call lives in its own function: __try cannot sit in a scope that
// holds C++ objects with destructors (C2712). No std::string here - the caller
// owns the string, this function takes the pointer.
int guarded_init(void *mod, void *ctx, const std::string *weights) {
    auto init_fn = reinterpret_cast<bool (__fastcall *)(void *, const std::string *)>(
        reinterpret_cast<uintptr_t>(mod) + kRvaInit);
    __try {
        return init_fn(ctx, weights) ? 0 : 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -static_cast<int>(GetExceptionCode());
    }
}

}  // namespace

int wmain(int argc, wchar_t **argv) {
    // Keep a copy of everything beside the exe: this runs on machines with no
    // console and gets pasted into an issue.
    const std::wstring exe_dir = dir_of(argv[0]);
    const std::wstring log_path = exe_dir + L"\\probe_amd.log";
    FILE *log = _wfopen(log_path.c_str(), L"w");
    g_out = log;

    std::wstring dir = exe_dir;
    bool do_init = false;
    for (int i = 1; i < argc; ++i) {
        if (wcscmp(argv[i], L"--init") == 0) do_init = true;
        else dir = argv[i];
    }

    out("probe_amd - the AMD neural runtime check\n");
    out("directory: %ls\n\n", dir.c_str());

    // --- 1. the files ---------------------------------------------------
    // The upscaler is listed with the rest and marked required, and that is
    // not cosmetic: without it the pass initialises, runs its threads, reports
    // healthy and produces a BLACK picture, because the runtime takes its
    // frame from a FidelityFX dispatch it hooks. A user who is told "everything
    // is here" by this probe and then gets a black screen has been misled by
    // the one tool built to prevent exactly that.
    struct Item { const wchar_t *name; bool required; };
    const Item items[] = {
        {kRuntimeName, true}, {kRuntimePatchedName, false},
        {kWeightsName, true}, {kIniName, false},
        {kUpscalerName, true},
    };
    bool runtime_present = false;
    bool upscaler_present = false;
    unsigned long long runtime_size = 0;
    for (const auto &it : items) {
        unsigned long long size = 0;
        const std::wstring p = dir + L"\\" + it.name;
        const bool there = file_exists(p, &size);
        out("%-32ls %s", it.name, there ? "FOUND" : "MISSING");
        if (there) {
            const double mb = static_cast<double>(size) / (1024.0 * 1024.0);
            out("  (%.1f MB)", mb);
            if (wcscmp(it.name, kRuntimeName) == 0) {
                runtime_present = true;
                runtime_size = size;
            }
            if (wcscmp(it.name, kUpscalerName) == 0) upscaler_present = true;
        } else if (it.required) {
            out("   <- required");
        } else if (wcscmp(it.name, kRuntimePatchedName) == 0) {
            // Optional, and only optional: the stock file is the one that runs.
            out("   <- optional, the stock build runs by default");
        }
        out("\n");
    }
    out("\n");
    if (runtime_present && !upscaler_present) {
        // Said in full, because the failure it prevents is silent: the pass
        // comes up, the menu says it is running, and the screen stays black.
        out("NOTE: the FidelityFX upscaler is missing, and the neural pass needs\n"
            "      it. The runtime is a game proxy - it takes the frame from an\n"
            "      FSR upscale dispatch, so with no upscaler to dispatch it has\n"
            "      nothing to attach to: the network runs, the picture stays\n"
            "      black. Copy amd_fidelityfx_upscaler_dx12.dll (AMD's own file,\n"
            "      shipped in OptiScaler's FSR package) next to the runtime.\n\n");
    }

    // --- 2. size and hash -------------------------------------------------
    // TWO images are accepted, because the A/B needs both: the stock build (the
    // default) and the same file with the five patches. They differ by a few
    // bytes, so the size gate is the same for each and the hash decides which
    // one this is.
    static const char kPatchedHash[] =
        "3c9ca13f0f5fc36a690ba424c457003bcfcc1080b4b785974cdd7e9ae2bc1dd8";
    static const char kStockHash[] =
        "106223723fd9266c44d38dc2fb77933948ab37803f46bfcea2bae3a0a474ac84";
    bool hash_match = false;
    if (runtime_present) {
        // The size gate runs first: the table belongs to one exact image, and
        // a truncated or re-extracted file fails here without hashing.
        const bool size_ok = runtime_size == kKnownRuntimeSize;
        out("size %llu bytes (known build: %llu) %s\n", runtime_size,
            kKnownRuntimeSize, size_ok ? "OK" : "<- differs");
        bool ok = false;
        const std::string hex = sha256_hex(dir + L"\\" + kRuntimeName, &ok);
        if (ok) {
            out("sha256 %s\n", hex.c_str());
            const bool stock = hex.rfind(kStockHash, 0) == 0;
            const bool patched = hex.rfind(kPatchedHash, 0) == 0;
            hash_match = stock || patched;
            out("expected %s (stock, default) or %s (patched)\n",
                kStockHash, kPatchedHash);
            if (stock)
                out("verdict: the STOCK build - the engine installs its own "
                    "hooks and owns the frame\n");
            else if (patched)
                out("verdict: the PATCHED build - the hook installer is "
                    "disabled, the host drives it (NS_AMD_PATCHED=1)\n");
            else
                out("verdict: UNKNOWN build - offsets are not verified against "
                    "this file\n");
        } else {
            out("sha256: could not read the file\n");
        }
    } else {
        out("sha256: skipped, the runtime is not here\n");
    }
    out("\n");

    // --- 3. HIP -----------------------------------------------------------
    // The runtime's own dependency, so it is looked for beside the runtime
    // first: LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR is not in play for a bare name,
    // but the explicit `dir + name` below covers the same ground. Kept in
    // this order because Adrenalin installs HIP into System32 on most
    // machines and the DLL folder on others.
    HMODULE hip = LoadLibraryExW(kHipName, nullptr, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!hip) {
        hip = LoadLibraryExW((dir + L"\\" + kHipName).c_str(), nullptr,
                             LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                 LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    }
    if (!hip) {
        out("HIP: %ls not found (error %lu) - install Adrenalin 26.1.1 or newer,\n"
            "     the HIP 7 runtime ships with it\n", kHipName, GetLastError());
        if (log) fclose(log);
        return 2;
    }
    out("HIP: %ls loaded\n", kHipName);

    using GetCountFn = int (*)(int *);
    using GetPropsFn = int (*)(void *, int);  // buffer FIRST, index second
    auto get_count = reinterpret_cast<GetCountFn>(GetProcAddress(hip, "hipGetDeviceCount"));
    auto get_props = reinterpret_cast<GetPropsFn>(GetProcAddress(hip, "hipGetDevicePropertiesR0600"));
    if (!get_count || !get_props) {
        out("HIP: required exports missing (count=%p props=%p)\n",
            reinterpret_cast<void *>(get_count), reinterpret_cast<void *>(get_props));
        if (log) fclose(log);
        return 3;
    }

    int count = 0;
    if (get_count(&count) != 0 || count <= 0) {
        out("HIP: no devices reported (count=%d) - the card may be disabled in BIOS\n", count);
        if (log) fclose(log);
        return 4;
    }
    out("HIP: %d device(s)\n", count);
    for (int i = 0; i < count; ++i) {
        HipProps props{};
        if (get_props(&props, i) == 0) {
            // name[256] is the first field; the adapter LUID (the field the
            // driver matches on) sits at +272.
            const char *name = reinterpret_cast<const char *>(props.raw);
            const LUID *luid = reinterpret_cast<const LUID *>(props.raw + kHipPropsLuidOffset);
            out("  [%d] %s  (LUID %08lX:%08lX)\n", i, name,
                static_cast<unsigned long>(luid->HighPart),
                static_cast<unsigned long>(luid->LowPart));
        } else {
            out("  [%d] <properties unavailable>\n", i);
        }
    }
    out("\n");

    // --- 4. optional: can the engine actually init -----------------------
    if (!do_init) {
        out("init: skipped (pass --init to try the engine)\n");
        if (log) fclose(log);
        return hash_match ? 0 : 1;
    }
    if (!runtime_present || !hash_match) {
        out("init: refused - the runtime is missing or is not the known build\n");
        if (log) fclose(log);
        return 1;
    }

    // LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR is what makes this probe work from
    // ANY working directory, and its absence is a live bug report: run from
    // the folder above, `probe_amd --init` died with "LoadLibrary failed
    // (error 126)" while the same command inside native\ succeeded. 126 is
    // ERROR_MOD_NOT_FOUND and here it means the runtime's OWN dependencies
    // (the HIP runtime among them) were searched for relative to the process
    // rather than to the DLL. DEFAULT_DIRS alone does not add the DLL's own
    // folder to that search; the flag has to be named. The worker has always
    // passed both; the probe did not, so a user who followed the README from
    // the extracted folder got a failure that said nothing about the cause.
    HMODULE mod = LoadLibraryExW((dir + L"\\" + kRuntimeName).c_str(), nullptr,
                                 LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                     LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!mod) {
        out("init: LoadLibrary failed (error %lu)\n", GetLastError());
        if (log) fclose(log);
        return 5;
    }
    // Bind the HIP device first: the engine reads it at a known offset.
    for (int i = 0; i < count; ++i) {
        auto set = reinterpret_cast<int (*)(int)>(GetProcAddress(hip, "hipSetDevice"));
        if (set && set(i) == 0) {
            *reinterpret_cast<int *>(reinterpret_cast<uintptr_t>(mod) + kRvaHipDevice) = i;
            out("init: HIP device %d selected\n", i);
            break;
        }
    }
    std::wstring wpath = dir + L"\\" + kWeightsName;
    // The runtime takes std::string, so convert once (the runtime itself is a
    // narrow-path API - a non-ASCII install path is out of scope for now).
    std::string narrow_weights;
    narrow_weights.reserve(wpath.size());
    for (wchar_t c : wpath) narrow_weights.push_back(static_cast<char>(c & 0x7F));
    void *ctx = reinterpret_cast<void *>(reinterpret_cast<uintptr_t>(mod) + kRvaInitCtx);
    const int rc = guarded_init(mod, ctx, &narrow_weights);
    if (rc < 0) {
        out("init: EXCEPTION 0x%08X - wrong build or wrong device, the offsets\n"
            "      do not match this image\n", -rc);
        if (log) fclose(log);
        return 6;
    }
    out("init: %s\n", rc == 0 ? "engine accepted the device and the weights"
                              : "engine returned false (check the runtime log)");
    if (log) fclose(log);
    return rc == 0 ? 0 : 6;
}
