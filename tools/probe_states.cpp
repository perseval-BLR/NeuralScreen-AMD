// Validates the frame's resource-state bookkeeping against the D3D12 debug
// layer - the only authority on whether a barrier is legal.
//
// Why this exists: reading the bridge's code turned up a barrier whose
// StateBefore did not match the state its resource was actually created in
// (`up_out` was created UNORDERED_ACCESS while the first barrier claimed it was
// NON_PIXEL_SHADER_RESOURCE). A barrier that lies about the state it is leaving
// is undefined behaviour, and D3D12 says so itself. But "undefined" is not an
// answer: the debug layer either flags it or it does not, and that decides
// whether this is a real defect or a harmless inconsistency.
//
// So the two sequences are run one after the other, the debug layer's stored
// messages are read after each, and the error count is what settles it:
//
//   A. THE OLD SEQUENCE  - resource created writable, first barrier claims it
//                          was readable (the state the code used to be in)
//   B. THE NEW SEQUENCE  - resource created readable, and the barriers tell the
//                          truth about where it is going
//
// If the debug layer is not installed this says so and exits 0: no answer is
// better than a guessed one.
//
// Run:  native\probe_states.exe

#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <string>

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "dxgi.lib")

namespace {

int g_failures = 0;

void Check(bool ok, const char *what) {
    printf("  %-58s %s\n", what, ok ? "OK" : "FAILED");
    if (!ok) ++g_failures;
}

ID3D12Device *MakeDevice(bool debug) {
    IDXGIFactory4 *factory = nullptr;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory4), (void **)&factory))) return nullptr;
    ID3D12Device *dev = nullptr;
    for (UINT i = 0; ; ++i) {
        IDXGIAdapter1 *adapter = nullptr;
        if (factory->EnumAdapters1(i, &adapter) != S_OK) break;
        DXGI_ADAPTER_DESC1 desc{};
        adapter->GetDesc1(&desc);
        const bool software = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
        if (!software && SUCCEEDED(D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0,
                                                     __uuidof(ID3D12Device), (void **)&dev))) {
            printf("  using adapter: %ls\n", desc.Description);
            adapter->Release();
            break;
        }
        adapter->Release();
    }
    factory->Release();
    (void)debug;
    return dev;
}

ID3D12Resource *Tex(ID3D12Device *dev, UINT w, UINT h, DXGI_FORMAT f,
                    D3D12_RESOURCE_STATES state) {
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w; rd.Height = h; rd.DepthOrArraySize = 1; rd.MipLevels = 1;
    rd.Format = f; rd.SampleDesc.Count = 1;
    rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
               D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    ID3D12Resource *t = nullptr;
    dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, state,
                                 nullptr, __uuidof(ID3D12Resource), (void **)&t);
    return t;
}

void Barrier(ID3D12GraphicsCommandList *cl, ID3D12Resource *r,
             D3D12_RESOURCE_STATES from, D3D12_RESOURCE_STATES to) {
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r;
    b.Transition.StateBefore = from;
    b.Transition.StateAfter = to;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cl->ResourceBarrier(1, &b);
}

// Runs one sequence to completion and reports how many debug-layer errors it
// produced. `lie` selects the old bookkeeping.
int RunSequence(ID3D12Device *dev, ID3D12InfoQueue *iq, bool lie) {
    const UINT w = 256, h = 256;
    // A: created writable, and the first barrier claims readable (the lie).
    // B: created readable, and the barrier tells the truth.
    ID3D12Resource *tex = Tex(dev, w, h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                              lie ? D3D12_RESOURCE_STATE_UNORDERED_ACCESS
                                  : D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    if (tex == nullptr) return -1;

    ID3D12CommandAllocator *alloc = nullptr;
    dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                __uuidof(ID3D12CommandAllocator), (void **)&alloc);
    ID3D12GraphicsCommandList *cl = nullptr;
    dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, nullptr,
                           __uuidof(ID3D12GraphicsCommandList), (void **)&cl);

    Barrier(cl, tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    cl->Close();

    ID3D12CommandQueue *q = nullptr;
    D3D12_COMMAND_QUEUE_DESC qd{};
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void **)&q);
    ID3D12CommandList *lists[] = { cl };
    q->ExecuteCommandLists(1, lists);

    ID3D12Fence *fence = nullptr;
    dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void **)&fence);
    HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    q->Signal(fence, 1);
    if (fence->GetCompletedValue() < 1) {
        fence->SetEventOnCompletion(1, ev);
        WaitForSingleObject(ev, 4000);
    }

    int errors = 0;
    if (iq != nullptr) {
        const UINT64 stored = iq->GetNumStoredMessages();
        for (UINT64 i = 0; i < stored; ++i) {
            SIZE_T len = 0;
            if (FAILED(iq->GetMessage(i, nullptr, &len)) || len == 0) continue;
            std::string buf(len, '\0');
            auto *msg = reinterpret_cast<D3D12_MESSAGE *>(&buf[0]);
            if (FAILED(iq->GetMessage(i, msg, &len))) continue;
            // Only the levels that mean "you asked for something illegal".
            if (msg->Severity == D3D12_MESSAGE_SEVERITY_ERROR ||
                msg->Severity == D3D12_MESSAGE_SEVERITY_CORRUPTION) {
                ++errors;
                if (errors <= 2)
                    printf("      debug layer: %.*s\n",
                           static_cast<int>(msg->DescriptionByteLength > 220
                                                ? 220 : msg->DescriptionByteLength),
                           msg->pDescription);
            }
        }
        iq->ClearStoredMessages();
    }

    CloseHandle(ev);
    fence->Release();
    q->Release();
    cl->Release();
    alloc->Release();
    tex->Release();
    return errors;
}

}  // namespace

int main() {
    printf("probe_states - is the frame's state bookkeeping legal?\n\n");

    ID3D12Debug *dbg = nullptr;
    if (FAILED(D3D12GetDebugInterface(__uuidof(ID3D12Debug), (void **)&dbg))) {
        printf("  the D3D12 debug layer is NOT installed on this machine\n");
        printf("  (Settings > System > Optional features > Graphics Tools)\n\n");
        printf("RESULT: no answer - the debug layer is what decides this, and it "
               "is not here. Nothing is claimed either way.\n");
        return 0;
    }
    dbg->EnableDebugLayer();
    printf("  the D3D12 debug layer is on\n");

    ID3D12Device *dev = MakeDevice(true);
    Check(dev != nullptr, "a device was created with the debug layer on");
    if (dev == nullptr) { dbg->Release(); return 1; }

    ID3D12InfoQueue *iq = nullptr;
    dev->QueryInterface(__uuidof(ID3D12InfoQueue), (void **)&iq);

    printf("\n  A. the OLD bookkeeping (created writable, barrier claims readable)\n");
    const int old_errors = RunSequence(dev, iq, true);
    printf("     -> %d debug-layer error(s)\n", old_errors);

    printf("\n  B. the NEW bookkeeping (created readable, barrier tells the truth)\n");
    const int new_errors = RunSequence(dev, iq, false);
    printf("     -> %d debug-layer error(s)\n", new_errors);

    printf("\n");
    Check(new_errors == 0,
          "the new bookkeeping is legal (no debug-layer errors)");

    if (old_errors > 0 && new_errors == 0) {
        printf("  %-58s %s\n", "the old barrier LIED about its state:",
               "CONFIRMED by the debug layer");
    } else if (old_errors == 0) {
        printf("  %-58s %s\n", "this machine does not flag the old sequence:",
               "no difference");
    }

    if (iq) iq->Release();
    dev->Release();
    dbg->Release();

    printf("\n");
    if (g_failures == 0) {
        printf("RESULT: the new state bookkeeping passes the debug layer\n");
        return 0;
    }
    printf("RESULT: %d check(s) failed\n", g_failures);
    return 1;
}
