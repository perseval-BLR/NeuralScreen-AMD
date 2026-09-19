"""The AMD path's two documented traps: the hook wait and the intensity slider.

Both were found by reading the working hosts (their own notes list them as
things that cost them time), and both are silent when they are wrong - which is
why they are worth a test rather than a comment.

1. THE HOOK WAIT MUST READ ONLY WHAT THIS RUN APPENDED.

   The runtime's `dlssnr_on_amd.log` is opened in APPEND mode across runs, so a
   search of the whole file is satisfied by the FIRST launch's
   `hooked IDXGISwapChain1::Present1` line. The wait then returns instantly and
   the host creates its swapchain into a detour that is not installed yet -
   which the runtime cannot see, and the documented result is a silent
   passthrough. Our own Radeon log shows the symptom:

       [amd] the runtime's D3D12/DXGI hooks are in place (0 ms)

   Zero milliseconds is not "fast", it is "did not wait".

2. THE INTENSITY SLIDER MUST REACH THE NETWORK.

   On the AMD path the strength is NOT an in-image parameter: the runtime reads
   `Scale` from its own ini. Writing the value anywhere else leaves the slider
   moving the host's own composite and nothing else - which is what it did.

Checked here in the source, since both are about what the code does rather than
what it computes:

Run:  runtime\\python.exe tests\\test_amd_runtime_traps.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RUNTIME_CPP = BASE / "native" / "amd" / "amd_runtime.cpp"
RUNTIME_H = BASE / "native" / "amd" / "amd_runtime.h"
BRIDGE = BASE / "native" / "amd" / "amd_bridge.inl"


def main() -> int:
    failures = []
    cpp = RUNTIME_CPP.read_text(encoding="utf-8", errors="replace")
    hdr = RUNTIME_H.read_text(encoding="utf-8", errors="replace")
    bridge = BRIDGE.read_text(encoding="utf-8", errors="replace")

    # --- 1. the hook wait --------------------------------------------------
    # LogContains must take a start offset, and the wait must pass it.
    if "from_byte" not in cpp:
        failures.append(
            "LogContains no longer takes a start offset - it would search the "
            "whole append-mode log and a previous run's hook lines would "
            "satisfy the wait")
    if "LogEndOffset" not in cpp:
        failures.append("nothing records where the log ended before this run")
    if "log_from_" not in cpp:
        failures.append("the hook wait does not use the start offset")
    # And the mark has to be taken BEFORE the module is loaded, or it would
    # include the very lines the wait is looking for.
    load_at = cpp.find("module_ = LoadLibraryExW(runtime_path.c_str()")
    mark_at = cpp.find("log_from_ = LogEndOffset(")
    if load_at < 0:
        failures.append("the runtime module is no longer loaded by name")
    elif mark_at < 0:
        failures.append("the log mark is never taken")
    elif mark_at > load_at:
        failures.append(
            "the log mark is taken AFTER the module loads, so the hook lines "
            "this run writes are already behind it and the wait still cannot "
            "see them")

    # --- 2. the intensity slider ------------------------------------------
    for token, why in (
        ("WriteScale", "nothing writes the runtime's Scale key"),
        ("kIniName", "the ini path is not used"),
        ('L"Scale"', "the Scale key is not written"),
    ):
        if token not in cpp:
            failures.append(why)
    if "intensity" not in hdr:
        failures.append("the Options block carries no intensity - the slider "
                        "cannot reach the runtime")
    # The bridge has to fill it from the settings...
    if "opt.intensity = g_video_options.intensity" not in bridge:
        failures.append("the bridge does not pass the menu's intensity into "
                        "the runtime options")
    # ...and the value has to be the CALIBRATED one, not raw: the runtime's own
    # ceiling is 0.125 and 1.0 on that number turns an image into halos.
    #
    # The calibration moved from WriteScale into ScaleMax() so the per-frame
    # field write and the ini write share one number, so the search follows it
    # there. The lambda body is what makes the old pattern miss.
    m = re.search(r"float v = ([0-9.]+)f", cpp) or re.search(r"float scale_max = ([0-9.]+)f", cpp)
    if not m:
        failures.append("the scale ceiling is gone - the slider's 1.0 has no "
                        "meaning")
    else:
        top = float(m.group(1))
        if not (0.0 < top <= 0.125):
            failures.append(
                f"the slider's 1.0 maps to Scale={top}, which is above the "
                "runtime's ceiling of 0.125 - that is the halo end of the "
                "range, not the working end")
    # The field write is the fix for the dead slider, and it is the one that
    # has to be in SetOptions: the ini is parsed ONCE (call_once in the first
    # CreateSwapChain detour), so a file write after startup reaches nothing.
    #
    # The access goes through the table now (`table_->kScale`), because the host
    # drives two builds and a bare constant would be the v0.2.17 address on a
    # v0.3.1 image. Both the write and the fact that it reads the table are
    # checked: a reverted access would put the wrong address on the second build.
    if "At<float>(module_, table_->kScale)" not in cpp:
        if "At<float>(module_, rva::kScale)" in cpp:
            failures.append("SetOptions writes a BARE kScale - correct for "
                            "v0.2.17 and wrong on any other build; the offset has "
                            "to come from the loaded image's table")
        else:
            failures.append("SetOptions does not write the Scale FIELD - the "
                            "intensity would only reach the ini, which the runtime "
                            "parsed once at startup and never reads again")
    # No runtime offset may be reached without the table: the two builds put the
    # same field at different addresses, so a hardcoded one silently reads or
    # writes the wrong place on the other build. `rva::kV0217` / `rva::kV0310`
    # are the tables themselves and assigning one to table_ is how it is meant
    # to be chosen, so only field names count here.
    bare = re.findall(r"rva::(k(?!V0)\w+)", cpp)
    if bare:
        failures.append("these offsets bypass the per-build table: "
                        + ", ".join(sorted(set(bare))) +
                        " - a hardcoded address is the v0.2.17 one and does not "
                        "follow the hash check that selects the other table")
    # Writing the file every frame would be a file write per frame for nothing.
    if "last_intensity_" not in cpp:
        failures.append("the ini is rewritten on every frame instead of only "
                        "when the slider moves")

    # --- the log must not misname the build that is running ---------------
    # The driver drives three images across two releases, and the log line is
    # how a report says which one ran. v0.3.1 was reported as "stock v0.2.17"
    # for one release: every offset it used was right and the sentence about
    # them was wrong, which is the harder kind of wrong to notice from a log
    # pasted into an issue.
    #
    # v0.3.1 gets its own ImageKind for exactly this reason, so the check names
    # all three: each release must be distinguishable in the output.
    bridge_kinds = re.findall(r"case amd_nr::ImageKind::(\w+):", bridge)
    for kind in ("Stock", "Patched", "Stock0310"):
        if kind not in bridge_kinds:
            failures.append(f"the log cannot name ImageKind::{kind} - a build "
                            f"would be reported as one of the others")
    if "ImageKind::Stock0310" not in cpp:
        failures.append("nothing ever sets ImageKind::Stock0310 - v0.3.1 would be "
                        "logged as the v0.2.17 stock image, which is a wrong "
                        "statement about which release is loaded")
    # The hand-made Notify is for the PATCHED image alone. Written as "not
    # Patched" so a newly added unpatched image cannot silently start being
    # notified by hand - that would announce every submission twice.
    if "Kind() != amd_nr::ImageKind::Patched" not in bridge:
        if "Kind() == amd_nr::ImageKind::Stock" in bridge:
            failures.append("the Notify gate lists builds by name again "
                            "(== Stock) - a third unpatched image would then be "
                            "notified by hand and every submission announced twice")

    checks = [
        ("hook wait reads only this run's lines",
         "from_byte" in cpp and "log_from_" in cpp and 0 <= mark_at < load_at),
        ("log mark taken before the module loads", 0 <= mark_at < load_at),
        ("intensity reaches the runtime's Scale",
         "opt.intensity = g_video_options.intensity" in bridge
         and "WriteScale" in cpp),
        ("the slider's top is the calibrated value, not the ceiling",
         bool(m) and 0.0 < float(m.group(1)) <= 0.125),
        ("the ini is written on change, not per frame",
         "last_intensity_" in cpp),
        ("every build the driver loads has its own name in the log",
         all(k in bridge_kinds for k in ("Stock", "Patched", "Stock0310"))),
    ]
    print("    checks:")
    for name, ok in checks:
        print(f"      {'ok  ' if ok else 'FAIL'} {name}")

    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s) in the AMD path's traps")
        return 1
    print("OK: the hook wait is a real wait and the slider reaches the network")
    return 0


if __name__ == "__main__":
    sys.exit(main())
