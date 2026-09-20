"""A failed RNSZ must stop the command, not answer "ok" (audit B2).

The rebuild path of the RNSZ handler calls CreateVideoResources; when it returns
false the code logged, wrote a FAILURE ack, and then fell straight through into
CreateFeature and wrote a SUCCESS ack with ok = 1 four lines later. The client
therefore believed the resize had been applied - and the next frame ran on a
half-built VideoState.

Why that is a crash and not just a wrong acknowledgement:
CreateVideoTex can fail AFTER it has already assigned v.tex (a footprint
mismatch, or a failed CreateCommittedResource for the upload buffer), leaving
v.upload null. FillUpload dereferences v.upload unconditionally, reached from
UploadVideoFrame on the pipe path and from UploadMotionOnly in DDA/WGC mode
whenever MOTS is not active. CreateVideoResources can also leave v.output or
v.readback null, and DownloadVideoFrame and PresentFrame dereference those.
So the worker died with an access violation, with no [failure] line in the log,
and the client's restart hid the cause.

Checked here:

* the failure branch stops the command (a `continue`/`return`) instead of
  falling through to the feature creation;
* it does NOT write a success ack afterwards;
* it releases the half-built state, so the next frame cannot run on it;
* the failure ack is still written, so the client is told;
* the worker compiles with all of it.

Run:  runtime\\python.exe tests\\test_rnsz_failure_stops.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "dlss5-feed-host64.cpp"


def _rnsz_failure_branch(src: str) -> str:
    """The block that runs when CreateVideoResources fails during RNSZ."""
    needle = "if (!CreateVideoResources(v, rc.width, rc.height,"
    start = src.find(needle)
    if start < 0:
        return ""
    # From there to the ack that follows the feature creation.
    end = src.find("NVSDK_NGX_Result rr = NVSDK_NGX_Result_Fail;", start)
    return src[start:end if end > 0 else start + 4000]


def main() -> int:
    failures = []
    src = SOURCE.read_text(encoding="utf-8", errors="replace")
    if not src:
        print("FAIL: the worker source is not readable")
        return 1

    branch = _rnsz_failure_branch(src)
    if not branch:
        print("FAIL: the RNSZ CreateVideoResources call is gone - this test no "
              "longer covers the path it exists for")
        return 1

    # The branch has to END the command. A bare `if` body that just logs and
    # writes the failure ack then falls through is exactly the bug.
    #
    # The search is anchored to the LAST release call in the branch, not to the
    # branch's start: `if (!WriteExact(...)) return 3;` sits four lines above
    # the real terminator, so a branch-wide search for `continue|return N;` was
    # satisfied by that unrelated return. Deleting the actual `continue;`
    # re-introduced the audit-B2 fall-through and the test still printed OK.
    last_release = max(branch.rfind("ReleaseVideoTextures(v)"),
                       branch.rfind("SafeReleaseFeature(h.feature)"))
    tail = branch[last_release:] if last_release >= 0 else branch
    if not re.search(r"\b(continue|return\s+\d+)\s*;", tail):
        failures.append("the failure branch does not stop the command - it "
                        "falls through into CreateFeature and then writes a "
                        "SUCCESS ack, so the client believes a resize it "
                        "never got")
    # And it must not contain a success ack of its own.
    if "VideoResizeAck ack" in branch or re.search(r"RESIZE_ACK_MAGIC,\s*1u", branch):
        failures.append("the failure branch writes a success ack (ok = 1)")

    # The failure ack itself must stay: silence would be its own bug.
    if "RESIZE_ACK_MAGIC, 0u" not in branch:
        failures.append("the failure branch no longer tells the client - an "
                        "RNSZ it cannot honour would go unanswered")

    # The half-built state must be released, or the next frame uses it.
    for call, why in (
        ("ReleaseVideoTextures(v)", "the textures the failed attempt left"),
        ("SafeReleaseFeature(h.feature)", "the feature whose textures no "
                                          "longer match"),
    ):
        if call not in branch:
            failures.append(f"the failure branch does not release {why}")

    # The null-dereference sources the audit named must still be the shape it
    # described: if FillUpload stopped dereferencing v.upload unguarded, the
    # urgency of this fix would change and the test should be revisited.
    if "v.upload->Map(" not in src:
        failures.append("FillUpload no longer dereferences v.upload - revisit "
                        "this test's reasoning")

    # It compiles.
    vswhere = Path(os.environ.get("ProgramFiles(x86)",
                                  r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio/Installer/vswhere.exe"
    try:
        install = subprocess.check_output(
            [str(vswhere), "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            text=True, timeout=120).strip()
    except Exception:
        install = ""

    if install:
        vcvars = Path(install) / "VC/Auxiliary/Build/vcvars64.bat"
        out_obj = ROOT / "_work" / "nvngx_rnsz.obj"
        command = (f'"{vcvars}" >nul && cl /nologo /c /O2 /EHsc /W3 /MD '
                   f'/std:c++17 /Iinclude /Isrc "{SOURCE}" /Fo:"{out_obj}"')
        result = subprocess.run(
            'cmd /d /s /c "' + command + '"', cwd=str(ROOT / "native"),
            capture_output=True, text=True, encoding="cp866",
            errors="replace", timeout=540)
        combined = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or "error C" in combined:
            failures.append("the worker does not compile:\n" +
                            "\n".join(l for l in combined.splitlines()
                                      if "error" in l.lower())[:500])
        try:
            out_obj.unlink()
        except OSError:
            pass

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a failed RNSZ stops, is reported, and releases what it built")
    return 0


if __name__ == "__main__":
    sys.exit(main())
