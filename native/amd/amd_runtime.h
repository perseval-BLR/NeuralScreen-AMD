// The AMD side of the neural pass: the driver for the third-party runtime.
//
// Everything NVIDIA about the worker lives behind one interface - init the
// engine once, then per frame hand it a colour resource and get the processed
// result back on the same command list. The AMD runtime (dlssnr_amd_pass1.dll,
// the DLSS-NR-on-AMD project's runtime) offers the same shape of interface, but
// a very different mechanism: it exports NOTHING. There is no CreateFeature, no
// EvaluateFeature - the DLL is driven by writing to fixed offsets inside its
// image and calling three internal addresses.
//
// That makes every offset below a hard contract with ONE binary. A wrong
// offset does not return an error - it jumps into whatever is there. So the
// loader checks size + sha256 first and refuses anything it does not recognise
// rather than guessing.
//
// --- which build this table belongs to -------------------------------------
//
// The pinned image is danielblnc v0.2.14 with the five in-place patches that
// the ecosystem's external hosts apply to it: the first two disable the
// runtime's own hook-install thread and its own notify call, so the DLL does
// not fight the host for ExecuteCommandLists ("the two cannot both hold the
// wheel"). Both working external hosts - OptiScaler's PreSR path and the
// Magpie fork - drive exactly this image. Its size and hash are enforced here.
//
// The stock upstream images are DIFFERENT and refused: vanilla v0.2.14 is
// 1062237... and the newer releases moved the whole data region (v0.2.17:
// +0x16A20..+0x16BB0; v0.3.0: again), so their tables are different. If a
// newer build is ever wanted, the recipe is in
// dlss5/tmp/amd-red-research/SPEC-OPTISCALER.md section 12, and every RVA must
// be re-derived against that image before anything is written.
//
// The user supplies the runtime and the weights: they are third-party binaries
// that cannot be redistributed (NVIDIA-derived weights). This module never
// ships them - it names them and checks that what it finds is the build it
// knows how to drive.

#pragma once

#include <cstdint>
#include <string>

#include <d3d12.h>

namespace amd_nr {

// --- the files the runtime consists of ------------------------------------
//
// Both live next to the worker unless BYO overrides the location - the same
// "bring your own" model as native/libraries/ for the NVIDIA runtime.
inline constexpr const wchar_t *kRuntimeName = L"dlssnr_amd_pass1.dll";
inline constexpr const wchar_t *kWeightsName = L"dlssnr_on_amd_weights.bin";
inline constexpr const wchar_t *kIniName = L"dlssnr_on_amd.ini";
inline constexpr const wchar_t *kHipName = L"amdhip64_7.dll";

// The one build this table belongs to (see the header comment). Size is a
// hard gate too: it catches truncation and re-extraction mistakes before the
// hash does any work.
//
// Two images are known: the stock v0.2.14 payload, and the same image with the
// five in-place patches the ecosystem's hosts apply (byte-identical table - the
// patches change bytes, not layout). The patched one is the tested path; the
// stock one is accepted so a machine that only has v0.2.14 still runs and
// reports, but it will try to drive itself at the same time as the host and is
// expected to misbehave (the patch's own reason: "the two cannot both hold the
// wheel").
inline constexpr uint64_t kRuntimeSize = 7156224;
inline constexpr uint8_t kRuntimeSha256Patched[32] = {
    0x3c, 0x9c, 0xa1, 0x3f, 0x0f, 0x5f, 0xc3, 0x6a, 0x69, 0x0b, 0xa4, 0x24,
    0xc4, 0x57, 0x00, 0x3b, 0xcf, 0xcc, 0x10, 0x80, 0xb4, 0xb7, 0x85, 0x97,
    0x4c, 0xdd, 0x7e, 0x9a, 0xe2, 0xbc, 0x1d, 0xd8,
};
inline constexpr uint8_t kRuntimeSha256Stock[32] = {
    0x10, 0x62, 0x23, 0x72, 0x3f, 0xd9, 0x26, 0x6c, 0x44, 0xd3, 0x8d, 0xc2,
    0xfb, 0x77, 0x93, 0x39, 0x48, 0xab, 0x37, 0x80, 0x3f, 0x46, 0xbf, 0xce,
    0xa2, 0xba, 0xe3, 0xa0, 0xa4, 0x74, 0xac, 0x84,
};

enum class ImageKind { Unknown, Stock, Patched };

// --- offsets inside the runtime image -------------------------------------
//
// All RVAs, added to the module base. The table is independently verified in
// SPEC-OPTISCALER.md (section 4): entry points against the PE function table,
// every data RVA against the writable .data section bounds.
namespace rva {
// The callable addresses.
inline constexpr uintptr_t kInit = 0x12380;      // bool(void *ctx, const std::string *weights)
inline constexpr uintptr_t kRecord = 0xa0b0;     // void(Packet *)
inline constexpr uintptr_t kNotify = 0x4640;     // void(ID3D12CommandQueue *, UINT, ID3D12CommandList *const *)
inline constexpr uintptr_t kShutdown = 0xc520;   // void(void) - stops the engine's workers

// One-time bindings (written by the host before Init).
inline constexpr uintptr_t kDevice = 0x764c8;   // ID3D12Device * (host AddRefs)
inline constexpr uintptr_t kQueue = 0x764d0;    // ID3D12CommandQueue * (host AddRefs)
inline constexpr uintptr_t kInitCtx = 0x764d8;  // the ctx struct the Init call takes
inline constexpr uintptr_t kHipDevice = 0x76f20;
inline constexpr uintptr_t kInlineMode = 0x76be0;  // pinned to 1
inline constexpr uintptr_t kInterop = 0x76c8c;     // pinned to 1
inline constexpr uintptr_t kEnabled = 0x76e1c;
inline constexpr uintptr_t kFlagAfterInit = 0x767f8;  // written only AFTER init returns

// Per-frame knobs.
inline constexpr uintptr_t kUseFsrInputs = 0x76e1e;
inline constexpr uintptr_t kUseDepth = 0x76e1f;
inline constexpr uintptr_t kPerPassFlag = 0x76e1d;  // Temporal / per-pass flag
inline constexpr uintptr_t kDepthInverted = 0x76e10;  // UINT, 0
inline constexpr uintptr_t kDepthExplicit = 0x76e14;  // uint8, 1
inline constexpr uintptr_t kLocalTone = 0x76e30;
inline constexpr uintptr_t kLocalStructure = 0x76e34;
inline constexpr uintptr_t kSkinStructure = 0x76e38;
inline constexpr uintptr_t kCharMask = 0x76e40;
inline constexpr uintptr_t kToneChannels = 0x76e44;

// History control.
inline constexpr uintptr_t kHistory = 0x765f0;      // void *, nullptr invalidates
inline constexpr uintptr_t kWantHistory = 0x765f8;  // uint8

// Status and accounting (read-only for the host).
inline constexpr uintptr_t kJobCounter = 0x76d74;    // UINT, jobs recorded
inline constexpr uintptr_t kStatusFlag = 0x767fa;    // uint8, non-zero after record = engine gave up
inline constexpr uintptr_t kSyncCounter = 0x76c14;   // UINT, jobs completed
inline constexpr uintptr_t kTimeoutCounter = 0x76c18;  // UINT, engine-side GPU wait timeouts

// Frame-acceptance machinery.
inline constexpr uintptr_t kPendingList = 0x76d68;  // ID3D12CommandList * - equals the list iff the
                                                    // engine accepted the record; the real acceptance
                                                    // test (record's void return says nothing)
inline constexpr uintptr_t kAbortWord = 0x76c68;    // volatile LONG - stale watchdog abort token,
                                                    // cleared by the host after each accepted record

// Deliberately NOT written by the host:
//   0x76c44  wait allowance / iteration ceiling. The engine maintains it and
//            shortens its own budget after each timeout; writing it fights the
//            engine (documented in both reference implementations).
//   0x76e20  Tonemap. The ini file keeps the last word; its own default is -1.
}  // namespace rva

// --- the job packet --------------------------------------------------------
//
// 0x50 bytes, laid out exactly as the engine reads it. The state fields are NOT
// reconciled against the real resource state: the reference writes the same
// fixed token for colour, motion, depth and exposure alike, including where the
// resource demonstrably sits in a different state. It is a token the engine
// expects, not a barrier description.
//
// Note: `colour` must be the SAME resource the result is read back from - the
// engine works in place. Pointing it at a separate output resource leaves the
// engine staring at an empty texture.
//
// Also: the packet conveys no render size, no jitter and no camera-cut reset,
// and the void return of the record call is not an acceptance signal - the
// kPendingList check after the call is.
#pragma pack(push, 8)
struct Packet {
    ID3D12CommandList *list;
    ID3D12Resource *colour;
    uint32_t colourState;
    uint32_t pad14;
    ID3D12Resource *motion;
    uint32_t motionState;
    uint32_t pad24;
    ID3D12Resource *depth;
    uint32_t depthState;
    uint32_t pad34;
    ID3D12Resource *exposure;
    uint32_t exposureState;
    float scaleX;
    float scaleY;
    uint32_t pad4c;
};
#pragma pack(pop)

static_assert(sizeof(Packet) == 0x50, "Packet must be 0x50 bytes");
static_assert(offsetof(Packet, scaleX) == 0x44, "scaleX must sit at 0x44");

// The fixed token every state field carries.
inline constexpr uint32_t kPacketState = 4;

// The runtime leaves its output in NON_PIXEL_SHADER_RESOURCE; barriers that
// follow the engine start from there.
inline constexpr uint32_t kStateShaderRead = 0xC0;  // D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE

// --- parameter set the host can change between frames ----------------------

struct Options {
    float local_tone = 0.5f;
    float local_structure = 1.0f;
    float skin_structure = -1.0f;
    bool enabled = true;
    //: The engine's character/auto mask. The reference writes it per frame
    //: from its own switch; leaving it out is not the same as writing 0.
    bool auto_mask = true;
    //: Bitfield of the tone channels the style enables (0, 1 or 2). Read by
    //: the engine per frame like the three intensities above.
    uint32_t tone_channels = 0;
    //: The menu's Intensity slider, 0..1. This one does NOT travel through the
    //: in-image parameter block: the runtime reads its strength from `Scale`
    //: in its OWN ini, so it is written to the file instead (see WriteIni).
    //: Sending it any other way leaves the slider dead - which it was.
    float intensity = 1.0f;
};

// --- the driver ------------------------------------------------------------

class Runtime {
public:
    Runtime() = default;
    ~Runtime();
    Runtime(const Runtime &) = delete;
    Runtime &operator=(const Runtime &) = delete;

    // Loads the runtime and the HIP library, checks size + hash, binds the
    // device and queue the caller already owns, selects the HIP device that
    // matches the D3D12 adapter (by LUID - the two APIs enumerate in their own
    // orders), then runs the init sequence and calls Init with the weights
    // path.
    //
    // `runtime_dir` is where the files live (worker's own folder by default;
    // BYO folder honored by the caller). Returns false and logs a reason on
    // any mismatch - never throws, never guesses.
    bool Load(const std::wstring &runtime_dir, ID3D12Device *device,
              ID3D12CommandQueue *queue);

    // True once Load() finished and the engine is usable.
    bool Ready() const { return ready_; }
    //: Whether the runtime's own D3D12/DXGI detours were seen in its log, and
    //: how long they took. The host logs it: a missing hook is the difference
    //: between a neural pass and a silent passthrough.
    bool HooksSeen() const { return hooks_seen_; }
    unsigned long HooksMs() const { return hooks_ms_; }

    // Which of the two known images was found (Stock when the host and the
    // runtime would both try to drive the frame; Patched when the runtime's
    // own hook installer is off and only the host drives).
    ImageKind Kind() const { return kind_; }

    // Why not ready, one line, for the log and the UI.
    const std::string &LastError() const { return last_error_; }

    // The sha256 the loader found on disk, hex - quoted in diagnostics so a
    // "it does not work" report carries the build identity with it.
    const std::string &FoundHash() const { return found_hash_; }

    // Applies the host's parameters. Cheap - three stores.
    void SetOptions(const Options &opt);

    //: Writes the menu's Intensity into the runtime's ini as `Scale`, which is
    //: where the runtime actually reads its strength from. Called from
    //: SetOptions when the value changes; public only so a test can drive it.
    void WriteScale(float intensity);

    //: The last `Scale` written into the ini, or -1 when nothing has been
    //: written. The bridge logs it - this unit has no Log().
    float ScaleWritten() const { return scale_written_; }

    // One frame: hand the engine the colour resource it should process. The
    // result lands in the same resource (the engine works in place), so the
    // caller's output resource is simply the colour resource.
    //
    // Must be called between the caller's Begin/EndCommands, on the worker's
    // single command-list thread. Returns true when the engine ACCEPTED the
    // record (verified against the pending-list marker, not the void return).
    //
    // After the caller submits the list, call Notify() and then wait - see
    // JobCount() / SyncCount() - before touching the colour resource again.
    bool Record(ID3D12CommandList *list, ID3D12Resource *colour,
                ID3D12Resource *motion, ID3D12Resource *depth = nullptr,
                ID3D12Resource *exposure = nullptr,
                float scale_x = 1.0f, float scale_y = 1.0f);

    // Tells the engine the recorded work has been submitted on `queue`.
    // Call AFTER the host's ExecuteCommandLists, never before: a capture-wait
    // kernel launched early can occupy the GPU while the captured frame is
    // still queued on the CPU.
    void Notify(ID3D12CommandQueue *queue, ID3D12CommandList *list);

    // Waits until the engine's completed-jobs counter reaches `wanted` (a
    // snapshot taken after Record). Returns false on timeout - the caller
    // should then skip the frame and invalidate history rather than disable
    // the effect; a single slow frame (first frames build the pipeline) is a
    // frame to skip, not a verdict.
    bool WaitJobs(uint32_t wanted, uint32_t timeout_ms) const;

    // Stops the engine's workers. Call only after every submission has
    // drained (the engine's threads must not be stopped under live work).
    void Shutdown();

    // Engine job accounting, for the wait loop and for diagnostics.
    // Snapshot JobCount() after Record; poll SyncCount() until it reaches the
    // snapshot - NOT a fence, the network runs on the engine's own worker and
    // a queue fence says nothing about it.
    uint32_t JobCount() const;
    uint32_t SyncCount() const;

    // Monotonically increasing engine-side GPU-wait timeout count. When it
    // grows, the engine gave up on a frame: treat the output as stale, skip
    // the frame and invalidate history before the next record.
    uint32_t TimeoutCount() const;

    // True when the engine latched its own failure (status flag non-zero).
    bool FailedOnEngineSide() const;

    // History control: a loading screen or a >250 ms gap leaves history
    // describing a scene that is no longer there; invalidate before the next
    // record in those cases.
    void InvalidateHistory();

private:
    bool ready_ = false;
    //: Set by the hook wait in Load(): whether the runtime announced its
    //: detours, and how long they took to appear.
    bool hooks_seen_ = false;
    unsigned long hooks_ms_ = 0;
    //: Where the runtime's log ended before this run loaded the module. The
    //: hook wait searches only bytes after this, because the log is appended
    //: to across runs and a whole-file search is satisfied by a previous
    //: launch's lines - which is how the wait came to report 0 ms.
    unsigned long long log_from_ = 0;
    //: The Intensity the ini currently carries, so the file is rewritten only
    //: when the slider actually moves.
    float last_intensity_ = -1.0f;
    //: The last `Scale` written to the ini, for the caller to log. -1 means
    //: nothing has been written yet.
    float scale_written_ = -1.0f;
    //: Where the runtime's ini lives, kept for WriteScale.
    std::wstring ini_path_;
    ImageKind kind_ = ImageKind::Unknown;
    std::string last_error_;
    std::string found_hash_;

    // Raw handles: HMODULE/HIP device, kept as void* so this header does not
    // drag windows.h into every inclusion.
    void *module_ = nullptr;
    void *hip_ = nullptr;
    int hip_device_ = -1;

    using InitFn = bool(__fastcall *)(void *, const std::string *);
    using RecordFn = void(__fastcall *)(Packet *);
    using NotifyFn = void(__fastcall *)(ID3D12CommandQueue *, unsigned,
                                        ID3D12CommandList *const *);
    using HipSetFn = int (*)(int);

    InitFn init_ = nullptr;
    RecordFn record_ = nullptr;
    NotifyFn notify_ = nullptr;
    HipSetFn hip_set_ = nullptr;

    void *ctx_ = nullptr;   // base + rva::kInitCtx, passed to Init
};

}  // namespace amd_nr
