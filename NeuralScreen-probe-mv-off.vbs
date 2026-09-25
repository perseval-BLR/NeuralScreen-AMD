' NeuralScreen-probe-mv-off.vbs - the per-frame probe with the flicker fix OFF
' (NS_AMD_PROBE_EACH=1, NS_AMD_UPSCALE_MV=0)
'
' The A/B against the shipped default. Since v0.3.24 the upscale dispatch is
' handed motion vectors, and that is the fix: with the vectors removed the
' alternating output of the upscale step comes back, which is the defect this
' build closes.
'
' Use it to see the difference on your own card: run this launcher, then
' NeuralScreen-probe.vbs, in the same window at the same work scale. The first
' should flicker where the second does not. One run each, while the defect is
' visible - the per-frame readback makes the picture slow, so this is a
' diagnosis and not a way to play.
'
' Why this file exists at all: the probe is the instrument that says WHICH
' surface carries a defect, and asking a reporter to set an environment
' variable by hand is asking a question most of them cannot answer. One of
' them said so plainly - "I have no experience with coding, and I don't know
' how to set the NS_AMD_PROBE_EACH=1 environment variable" - after being asked
' twice. A launcher named in the reply, double-clicked, is the whole
' instruction.
'
' Use it for ONE run, while the defect is visible. The readback is a full
' GPU-to-CPU sync, so the picture runs slowly: this is a diagnosis, not a way
' to play.
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


' --- The arm under test: the motion vectors OFF ---
'
' The private copy stays ON (it is the default and the vectors are gated on it),
' so this launcher changes exactly one thing against the shipped build.
shell.Environment("Process")("NS_AMD_PROBE_EACH") = "1"
shell.Environment("Process")("NS_AMD_UPSCALE_MV") = "0"

' --- Launch with no window (window style 0), without waiting ---
Dim extra, arg, q
q = Chr(34)
extra = ""
For Each arg In WScript.Arguments
    extra = extra & " " & q & arg & q
Next
shell.Run """" & py & """ -u """ & dir & "\main.py""" & extra, 0, False
