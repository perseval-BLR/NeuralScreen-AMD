// The FidelityFX upscaler bridge: the piece that makes the neural runtime
// actually process our frames.
//
// Why this exists at all, in one paragraph: the DLSS-NR runtime is a
// standalone `version.dll` proxy written to live inside a game. It hooks
// D3D12/DXGI and the FidelityFX upscaler, and its whole model of "where is
// the frame" is the upscaler dispatch. Its own log says so when it cannot
// find one:
//
//     frames 9000 dispatches 0 (0.00/frame) submitted 0 ready -1 route backbuffer
//
// That is the first live report from an RX 9070 XT in one line. The engine
// had initialised, was running jobs (`network job 100 done in 16 ms`), and
// the picture was black - because with no dispatch to follow it fell back to
// routing through a backbuffer, and the frame it enhanced was never ours.
//
// So the host has to be a game in the one way that matters: it must dispatch
// the real FSR upscaler, on its own device, every frame. Two dispatches, and
// the split is the whole design (measured by the reference host, and it is
// what their documentation calls the fifth trap):
//
//   A: work -> work (1:1), motion vectors bound. The runtime follows THIS one
//      - in its own words it ignores "upscaler dispatches without motion
//      vectors" - so the network runs at the work resolution on a frame that
//      has not been resampled.
//   B: work -> output, no motion vectors. The runtime ignores it; FSR does
//      the upscale to the display resolution.
//
// At scale 1.0 the work resolution IS the output resolution: B does not
// exist, the network sees the full frame, and nothing is resampled.
//
// The DLL is not redistributable and is not shipped here: the user brings
// their own copy, exactly like the runtime and the weights. Nothing in this
// file links against it - every entry point is fetched by name, so a machine
// without it gets the packet path and a line in the log rather than a crash.

#pragma once

#include <cstdint>
#include <string>

#include <d3d12.h>

namespace amd_fsr {

// The FidelityFX upscaler the ecosystem ships (`amd_fidelityfx_upscaler_dx12.dll`,
// FFX API 4.1.1). Loaded by name from the AMD runtime folder.
inline constexpr const wchar_t *kUpscalerName = L"amd_fidelityfx_upscaler_dx12.dll";

// The FFX entry points, fetched by name. Types mirror ffx_api.h; they are
// declared here as plain function pointers so this header does not drag the
// MIT headers into every translation unit that includes the bridge.
struct Api {
    void *module = nullptr;
    int (*create)(void **ctx, void *desc, const void *callbacks) = nullptr;
    int (*dispatch)(void **ctx, const void *desc) = nullptr;
    int (*destroy)(void **ctx, const void *callbacks) = nullptr;
};

// One upscaler context. Two of them exist per frame set (A and B above).
struct Context {
    void *handle = nullptr;
};

// Told the name of each step of the context setup, before it starts and after
// it returns. This file has no log of its own; the worker passes a function
// that writes the line and remembers the step for its stall watch.
using StepFn = void (*)(const char *step);

class Upscaler {
public:
    ~Upscaler();

    // Loads the DLL and resolves the three entry points. `dir` is searched
    // first, then the usual DLL search path.
    bool Load(const std::wstring &dir, std::string &why);

    // Creates the two contexts. `upscaling` is false when work == output,
    // and then only the network context exists (there is nothing to scale).
    // `step` may be null. When it is not, every ffxCreateContext call is
    // bracketed by two calls naming it, so a setup that never returns (seen on
    // an RX 9070 XT: the worker stopped between its surfaces and its contexts,
    // six launches in a row) leaves the last step it entered in the log.
    bool CreateContexts(ID3D12Device *device, UINT work_w, UINT work_h,
                        UINT out_w, UINT out_h, std::string &why,
                        StepFn step = nullptr);

    void ReleaseContexts();

    // Dispatch B on a module of its own (NS_AMD_UPSCALE_PRIVATE=1). The runtime
    // hooks ffxCreateContext/ffxDispatch in the upscaler module it found, and
    // picks the dispatch it processes by "has motion vectors" - so B with
    // vectors on the SHARED module put it into a staging re-create loop (80
    // re-creations, 4 network jobs on a 7900 XTX), and the motion-vector test
    // measured that loop as well as the vectors. A second copy of the same
    // file, loaded from a private folder, is a separate image the hooks were
    // never installed in, so B can carry vectors without the runtime seeing it.
    //
    // Same file name on purpose, only the folder differs: a driver that
    // recognises the upscaler by name would otherwise run B down a different
    // path and the arm would change two things. Must be called after Load.
    // Returns false (and B stays on the shared module) when the copy cannot be
    // made or the loader hands back the shared module.
    bool LoadPrivateB(std::string &why);
    bool PrivateB() const { return private_b_; }
    const std::wstring &PrivateBPath() const { return private_b_path_; }

    bool Ready() const { return api_.module != nullptr; }
    bool ContextsReady() const { return ctx_net_.handle != nullptr; }
    //: True when a second context exists, i.e. work != output.
    bool Upscaling() const { return upscaling_; }

    // Dispatch A: the one the runtime follows. Records FSR's own work
    // (1:1 resample, which is a no-op at matching sizes) and writes into
    // `net`; the runtime then runs the network on that surface in place.
    // `exposure` is the 1x1 R32F surface the network must be handed instead of
    // letting FSR adapt its own. The FFX API has a field for exactly this -
    // `ffxDispatchDescUpscale::exposure`, "Optional resource containing a 1x1
    // exposure value" - and this call used to leave it null while arming
    // FFX_UPSCALE_ENABLE_AUTO_EXPOSURE at context creation. That combination is
    // why the engine logs `staging ready: ... exposure no`: with no resource
    // bound and the input black, its own adaptation ran away to its ceiling
    // (9999.9980), which is the brightness pump the fixed 1.0 value exists to
    // stop.
    bool DispatchNet(ID3D12CommandList *list, ID3D12Resource *color,
                     ID3D12Resource *depth, ID3D12Resource *motion,
                     ID3D12Resource *exposure, ID3D12Resource *net,
                     float frame_ms, bool reset, std::string &why);

    // Dispatch B: plain FSR upscale from the network's output to the display
    // resolution, deliberately without motion vectors so the runtime leaves
    // it alone. Only meaningful when Upscaling().
    //
    // `exposure` is the SAME 1x1 surface A is handed, and it is a parameter
    // rather than null for one measured reason. The fifth probe run (22.09)
    // showed B's output saturated at ~64,000 of a 65,504 half-float ceiling on
    // every bright frame while its input - A's output - stayed at 0.10-0.42 on
    // both the bright and the dark frames. So the corruption is made inside B,
    // and the one field A fills that B does not is this one.
    //
    // It is passed as a nullable knob (`NS_AMD_UPSCALE_EXPOSURE=1`), default
    // off: the field is documented optional and B carries `preExposure = 1.0`,
    // so binding it is a hypothesis with a measurement behind it, not a proven
    // fix. The default stays exactly as it shipped until a reporter's frame
    // says otherwise - a guess that silently changes the picture for everyone
    // is how a one-variable test turns into a regression.
    //
    // `motion` is null on the shipped path and a surface of B's own under
    // `NS_AMD_UPSCALE_MV=1`. The motion vectors are the one input of B that
    // ffx_upscale.h does not mark optional, and B's output alternates with
    // complementary halves on two different upscaler paths, so whether they
    // are bound is the next single variable. motionVectorScale stays {0, 0}
    // in both arms, so the surface's contents never reach the result.
    bool DispatchUpscale(ID3D12CommandList *list, ID3D12Resource *net,
                         ID3D12Resource *depth, ID3D12Resource *exposure,
                         ID3D12Resource *motion,
                         ID3D12Resource *out,
                         float frame_ms, bool reset, std::string &why);

private:
    // The module B's context is created, dispatched and destroyed through:
    // the private copy under the arm, the shared module otherwise.
    const Api &ApiB() const { return private_b_ ? api_b_ : api_; }

    Api api_;
    Api api_b_;          // the private copy, when LoadPrivateB succeeded
    bool private_b_ = false;
    std::wstring private_b_path_;
    Context ctx_net_;    // A
    Context ctx_up_;     // B
    bool upscaling_ = false;
    UINT work_w_ = 0, work_h_ = 0, out_w_ = 0, out_h_ = 0;
};

}  // namespace amd_fsr
