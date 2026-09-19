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
// The pinned image is danielblnc **v0.2.17** with the two in-place patches that
// the ecosystem's external hosts apply to it: one neutralises the notify call
// that would execute the frame twice, one corrects the log text for a timed-out
// frame. Both working external hosts drive a runtime this way - the MIT host
// publishes exactly this pair for v0.2.17 - and its size and hash are enforced
// here.
//
// WHY v0.2.17 AND NOT v0.2.14: the maintainer's own release notes for this
// build carry "Fixed crashes and black screens on multi-GPU systems" and
// "Improved HDR exposure handling", both of which land on the reports this
// project actually receives (two Radeons plus an iGPU in one machine, and a
// 9070 XT that faults inside D3D12Core before the engine records anything).
// v0.2.14 predates all of it.
//
// WHAT IS NOT PATCHED ON THIS BUILD, and why:
//   * The v0.2.14 hook-installer kill (0x1ffc there, 0x6006 here) MUST NOT be
//     applied. On v0.2.14 that thread only installed D3D12/DXGI detours, so
//     killing it was free. On v0.2.17 the same thread ALSO resolves the proxy:
//     it builds the system paths for d3d12.dll and dxgi.dll and loads them. With
//     the CreateThread nopped those stay null and the first call through one
//     lands on address 0 - measured elsewhere as 0xc0000005 at 0000000000000000
//     on the first frame after Enabled. Consequence: on this build the runtime
//     installs its own detours and the host does not fight that.
//   * The timeout-fallback shader edit (0x62bd4 on v0.2.14) is unnecessary:
//     v0.2.17 exposes the same choice as ToneChannels bit 4 set with bit 2
//     clear, which the host writes per frame instead of editing the binary.
//   * The GPU wait spin cap is dropped on every build - it bounds the wait
//     below the network's real cost, so it makes inline mode time out on
//     essentially every frame.
//
// The stock upstream images are DIFFERENT and refused by hash: vanilla v0.2.14
// is 1062237... and every release from v0.2.15 moved the data region
// (v0.2.17: four different deltas, +0x16980..+0x16BB0; v0.3.0: again), so their
// tables are different tables.
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
//: The same build with the five documented patches applied. Kept as a separate
//: FILE rather than a replacement, so switching between the two is one
//: environment variable instead of a reinstall (see RuntimeImageName).
inline constexpr const wchar_t *kRuntimeNamePatched = L"dlssnr_amd_pass1_patched.dll";
inline constexpr const wchar_t *kWeightsName = L"dlssnr_on_amd_weights.bin";
inline constexpr const wchar_t *kIniName = L"dlssnr_on_amd.ini";
inline constexpr const wchar_t *kHipName = L"amdhip64_7.dll";

// The one build this table belongs to (see the header comment). Size is a
// hard gate too: it catches truncation and re-extraction mistakes before the
// hash does any work.
//
// Two images are known: the stock v0.2.17 payload, and the same image with the
// two in-place patches the ecosystem's hosts apply (byte-identical table - the
// patches change bytes, not layout). The patched one is the pair that has been
// checked against a published build; the stock one is accepted so a machine
// that has only the unmodified file still runs and reports. On this build BOTH
// are expected to work: unlike v0.2.14, the runtime's own setup thread is left
// alive here (it resolves the proxy), so the runtime may install its detours
// while the host drives from outside - the MIT host runs exactly this way.
inline constexpr uint64_t kRuntimeSize = 7248384;
inline constexpr uint8_t kRuntimeSha256Patched[32] = {
    0xc8, 0xa5, 0xd3, 0xaf, 0x65, 0xf3, 0x50, 0x58, 0xa2, 0x27, 0x4f, 0xa3,
    0xfb, 0xd3, 0xaa, 0x7a, 0x71, 0x3f, 0xf8, 0x6c, 0x33, 0x75, 0xe1, 0x2d,
    0x74, 0xaf, 0x7d, 0x96, 0x18, 0x27, 0x90, 0x66,
};
inline constexpr uint8_t kRuntimeSha256Stock[32] = {
    0xbc, 0x97, 0xf3, 0xb0, 0x67, 0x18, 0xe1, 0x90, 0x42, 0xac, 0xaf, 0x22,
    0x7b, 0xfe, 0x15, 0xd1, 0xe4, 0x3d, 0x49, 0x77, 0xf9, 0xdc, 0x2e, 0x39,
    0x99, 0x4f, 0xcc, 0x51, 0x14, 0x45, 0xff, 0x4e,
};

enum class ImageKind { Unknown, Stock, Patched };

// --- offsets inside the runtime image -------------------------------------
//
// All RVAs, added to the module base. The table is independently verified in
// SPEC-OPTISCALER.md (section 4): entry points against the PE function table,
// every data RVA against the writable .data section bounds.
namespace rva {
// The callable addresses.
inline constexpr uintptr_t kInit = 0x19240;      // bool(void *ctx, const std::string *weights)
inline constexpr uintptr_t kRecord = 0xf600;     // void(Packet *)
inline constexpr uintptr_t kNotify = 0x9170;     // void(ID3D12CommandQueue *, UINT, ID3D12CommandList *const *)
inline constexpr uintptr_t kShutdown = 0x12690;  // void(void) - stops the engine's workers

// One-time bindings (written by the host before Init).
inline constexpr uintptr_t kDevice = 0x8cee8;   // ID3D12Device * (host AddRefs)
inline constexpr uintptr_t kQueue = 0x8cef0;    // ID3D12CommandQueue * (host AddRefs)
inline constexpr uintptr_t kInitCtx = 0x8cef8;  // the ctx struct the Init call takes
inline constexpr uintptr_t kHipDevice = 0x8dad0;
inline constexpr uintptr_t kInlineMode = 0x8d6c0;  // pinned to 1
inline constexpr uintptr_t kInterop = 0x8d82c;     // pinned to 1
inline constexpr uintptr_t kEnabled = 0x8d9bc;
inline constexpr uintptr_t kFlagAfterInit = 0x8d218;  // written only AFTER init returns

// Per-frame knobs.
inline constexpr uintptr_t kUseFsrInputs = 0x8d9be;
inline constexpr uintptr_t kUseDepth = 0x8d9bf;
inline constexpr uintptr_t kPerPassFlag = 0x8d9bd;  // Temporal / per-pass flag
inline constexpr uintptr_t kDepthInverted = 0x8d9b0;  // UINT, 0
inline constexpr uintptr_t kDepthExplicit = 0x8d9b4;  // uint8, 1
inline constexpr uintptr_t kLocalTone = 0x8d9d0;
inline constexpr uintptr_t kLocalStructure = 0x8d9d4;
inline constexpr uintptr_t kSkinStructure = 0x8d9d8;
inline constexpr uintptr_t kCharMask = 0x8d9e0;
inline constexpr uintptr_t kToneChannels = 0x8d9e4;
inline constexpr uintptr_t kScale = 0x8d9dc;        // the network's strength. The runtime
                                                    // reads it in the job-recording function
                                                    // (0x140a0..0x15e79), the same place it
                                                    // reads LocalTone/LocalStructure - so a
                                                    // per-frame write here reaches the network
                                                    // the same way those two do.

// History control.
inline constexpr uintptr_t kHistory = 0x8d010;      // void *, nullptr invalidates
inline constexpr uintptr_t kWantHistory = 0x8d018;  // uint8

// Status and accounting (read-only for the host).
inline constexpr uintptr_t kJobCounter = 0x8d914;    // UINT, jobs recorded
inline constexpr uintptr_t kStatusFlag = 0x8d21a;    // uint8, non-zero after record = engine gave up
inline constexpr uintptr_t kSyncCounter = 0x8d6f4;   // UINT, jobs completed
inline constexpr uintptr_t kTimeoutCounter = 0x8d6f8;  // UINT, engine-side GPU wait timeouts

// Frame-acceptance machinery.
inline constexpr uintptr_t kPendingList = 0x8d908;  // ID3D12CommandList * - equals the list iff the
                                                    // engine accepted the record; the real acceptance
                                                    // test (record's void return says nothing)

// Deliberately NOT written by the host:
//   0x8d808/0x8d80c  the engine's watchdog job pair. The watchdog function
//            (0x16260..0x1652d) stores the job id into BOTH dwords when a
//            timeout fires, and reads them back. Two things follow, and the
//            second is why this note is longer than the others: it is a
//            counter, not a handle, so a host that treats the pair as a
//            pointer to clear crashes on the next recording; and a host that
//            just zeroes it (which is what this driver used to do) is
//            erasing the engine's own record of which job timed out.
//            The engine resets its real GPU abort flag itself, through
//            hipMemcpyAsync - it does not need help, and it is the only side
//            that knows when the flag is stale.
//
//            This address was in the table as `kAbortWord` until 0.2.0. The
//            port derived it as 0x76c68 + 0x16ba0 (the delta that fits the
//            fields around it), but 0x76c68 is not in that region: the
//            reference that documents it computes 0x76c68 + 0x16a20 and lands
//            on 0x8d688, and the two answers differ by 0x180. Neither could
//            be confirmed without a card, so the write is gone rather than
//            guessed at - nothing the driver needs depends on it.
//   the wait allowance / iteration ceiling. The engine maintains it and
//            shortens its own budget after each timeout; writing it fights the
//            engine (documented in both reference implementations).
//   0x8d9c0  Tonemap. The ini file keeps the last word; its own default is -1.
//   the wait allowance / iteration ceiling. The engine maintains it and
//            shortens its own budget after each timeout; writing it fights the
//            engine (documented in both reference implementations).
//   0x8d9c0  Tonemap. The ini file keeps the last word; its own default is -1.
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
    //: Whether the wait could apply at all to the loaded build (see the .cpp).
    bool HooksApplicable() const { return hooks_applicable_; }
    unsigned long HooksMs() const { return hooks_ms_; }

    //: The engine's OWN verdict that it came up: the `engine init ok` line it
    //: writes to its own log right after its initialisation returns.
    //:
    //: This is a different question from Ready(), and the difference is the
    //: whole reason the field exists. Load() calls the engine's init entry and
    //: checks its return value - and our logs show `AMD path active` with the
    //: engine's own log carrying no `engine init ok` and no `env: HIP` at all,
    //: on twelve consecutive launches of one machine. So a host that reports
    //: Ready() can still be talking to an engine that never started, and the
    //: only witness is the engine's own line.
    //:
    //: Never gates anything. It is read once, after init, and printed.
    bool EngineInitSeen() const { return engine_init_seen_; }
    //: Whether the wait for that line could apply (false for a build that
    //: writes no such line), so the log can say "not applicable" rather than
    //: "not seen" - the same three-answer discipline as HooksApplicable().
    bool EngineInitApplicable() const { return engine_init_applicable_; }

    // Which of the two known images was found (Stock when the host and the
    // runtime would both try to drive the frame; Patched when the runtime's
    // own hook installer is off and only the host drives).
    ImageKind Kind() const { return kind_; }

    // Why not ready, one line, for the log and the UI.
    const std::string &LastError() const { return last_error_; }

    // The sha256 the loader found on disk, hex - quoted in diagnostics so a
    // "it does not work" report carries the build identity with it.
    const std::string &FoundHash() const { return found_hash_; }

    // Where the runtime's own log ENDED before this run loaded the module.
    // Everything at or after this offset was written by THIS run. Anything
    // that reads that log must start here: the file is appended to across
    // launches, so a read bounded only by "the last N KB" can quote a previous
    // launch's line as if it were this one's - which is exactly how a stale
    // `encoded mean` would reach a report and be read as the current frame.
    unsigned long long LogFrom() const { return log_from_; }

    // Applies the host's parameters. Cheap - three stores.
    void SetOptions(const Options &opt);


    // The network's strength. Intensity is the menu's 0..1 control; this turns
    // it into the runtime's own number. BOTH the per-frame field write and the
    // ini write go through it, so the two cannot disagree.
    static float ScaleMax();
    // The ini file's `Scale`, written on change so the next launch starts from
    // the slider rather than from whatever the file held.
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
    //: False when the loaded build cannot print the detour lines at all (the
    //: patched image, whose hook installer is disabled on purpose). The two are
    //: different answers and the log must not confuse them: "not seen" sends a
    //: reader after a fault, "not applicable" is simply the shape of that build.
    bool hooks_applicable_ = false;
    unsigned long hooks_ms_ = 0;
    //: The engine's own `engine init ok` line, read from ITS log after our
    //: init call returned success - the only witness that the engine really
    //: started. `hooks_seen_` answers a different question (our detours) and
    //: reads identically whether the engine came up or not.
    bool engine_init_seen_ = false;
    //: False when this build cannot print such a line at all, so the log says
    //: "not applicable" instead of sending a reader after a fault that is not
    //: there.
    bool engine_init_applicable_ = true;
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
