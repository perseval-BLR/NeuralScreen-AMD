' NeuralScreen-probe-mv-shared.vbs - the per-frame probe with the upscale's
' motion-vector arm bound on the SHARED upscaler module
' (NS_AMD_PROBE_EACH=1, NS_AMD_UPSCALE_MV=1)
'
' What it isolates: the third run in issue #3 came back clean - no flicker, no
' frame above 1024 in the upscale's output, out of 68 samples - but it changed
' TWO things against the baseline: the upscale ran on a private copy of the
' FidelityFX DLL, and it was handed motion vectors. The reporter isolated the
' first one himself (private alone, 59 of 68 frames still bad, worse than the
' baseline's 34 of 69), so the vectors are the likely cause - and he asked for
' this combination specifically, because "motion vectors without the private
' copy" was never run in a build where it could say anything.
'
' Expect the runtime to follow this dispatch: it picks the dispatch it processes
' by "has motion vectors", and this one now has them on the module its hooks are
' installed in. That is what happened in v0.3.21 (80 staging re-creations for 4
' network jobs, and the run measured the loop as much as the vectors), and the
' private copy added in v0.3.22 was a way around that loop, not a fix for it.
' The worker's log says which happened: "the engine's staging-rebuild flag is
' UP" / "input textures changed" lines mean the loop came back, and then this
' run answers a different question than the one it was asked - the picture will
' still tell you whether the flicker is gone, which is what it is for.
'
' Read by the worker once, when it first builds its surfaces. The worker's log
' names the arm ("the upscale dispatch's motion vectors: bound ...", and the
' module line "shared with the network dispatch (the default)"). A test, not a
' fix. Slow picture, like the other probe launchers: a diagnosis, not a way to
' play.

Option Explicit

Dim fso, shell, dir, py, nvruntime, devpython
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)

' --- Finding Python: NEURALSCREEN_PYTHON -> runtime\pythonw.exe -> dev path -> PATH ---
py = shell.ExpandEnvironmentStrings("%NEURALSCREEN_PYTHON%")
If py = "%NEURALSCREEN_PYTHON%" Then py = ""
If py <> "" And Not fso.FileExists(py) Then py = ""

If py = "" Then
    nvruntime = dir & "\runtime\pythonw.exe"
    If fso.FileExists(nvruntime) Then py = nvruntime
End If

If py = "" Then
    py = "pythonw"
End If

' --- Python check: launching blind without it means a silent failure ---
If py = "pythonw" Then
    Dim found
    found = False
    Dim pathVar, parts, part
    pathVar = shell.ExpandEnvironmentStrings("%PATH%")
    parts = Split(pathVar, ";")
    For Each part In parts
        If part <> "" And fso.FileExists(part & "\pythonw.exe") Then
            found = True
            Exit For
        End If
    Next
    If Not found Then
        MsgBox "NeuralScreen AMD: pythonw.exe not found." & vbCrLf & _
               "Install Python or unpack the release archive (it bundles a portable runtime).", _
               16, "NeuralScreen AMD"
        WScript.Quit 1
    End If
End If

' --- NGX runtime check (165 MB, not kept in git) ---
If Not fso.FileExists(dir & "\native\nvngx_dlssnr.dll") Then
    MsgBox "NeuralScreen AMD: native\nvngx_dlssnr.dll not found." & vbCrLf & _
           "Re-download the release archive, or see README.md, section " & _
           "What you need.", 16, "NeuralScreen AMD"
    WScript.Quit 1
End If

' --- Worker check (a build artefact) ---
If Not fso.FileExists(dir & "\native\nvngx.dll") Then
    MsgBox "NeuralScreen AMD: native\nvngx.dll not found." & vbCrLf & _
           "Build it with native\build-host.bat or re-download the release archive.", _
           16, "NeuralScreen AMD"
    WScript.Quit 1
End If

' --- Diagnostic mode: the probe reports on every frame ---
'
' The variable is set in the LAUNCHED PROCESS's environment, which main.py and
' then the worker inherit - the probe reads it with GetEnvironmentVariableA at
' its own startup, so it has to be in place before the worker is spawned.
shell.Environment("Process")("NS_AMD_PROBE_EACH") = "1"

' --- The arm under test: the upscale dispatch gets motion vectors ---
'
' NS_AMD_UPSCALE_PRIVATE is deliberately NOT set here: the upscale runs on the
' same upscaler module the runtime hooked, so the runtime can see this dispatch.
' That is the point of this arm - it is the one combination the previous builds
' could not measure.
shell.Environment("Process")("NS_AMD_UPSCALE_MV") = "1"

' --- Launch with no window (window style 0), without waiting ---
Dim extra, arg, q
q = Chr(34)
extra = ""
For Each arg In WScript.Arguments
    extra = extra & " " & q & arg & q
Next
shell.Run """" & py & """ -u """ & dir & "\main.py""" & extra, 0, False
