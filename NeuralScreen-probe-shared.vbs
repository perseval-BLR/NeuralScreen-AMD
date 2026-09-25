' NeuralScreen-probe-shared.vbs - the per-frame probe with the private copy OFF
' (NS_AMD_PROBE_EACH=1, NS_AMD_UPSCALE_PRIVATE=0)
'
' The other half of the A/B, and the honest version of a launcher that used to
' exist as probe-private: the private copy is now the SHIPPED DEFAULT, so asking
' for it proves nothing. This one turns it off and returns the upscale dispatch
' to the module the runtime hooked.
'
' The motion vectors are refused on this path by the build itself - vectors on a
' dispatch the runtime can see make it re-create its staging on every switch
' (92 re-creations for 1 engine job over 54 frames, measured on a 7900 XTX),
' which measures the loop instead of the picture. So this run is the OLD
' picture: no flicker fix, not a broken one.
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


' --- The arm under test: the private copy OFF ---
'
' The vectors follow the copy automatically (the build gates them on it), so
' there is nothing else to set here.
shell.Environment("Process")("NS_AMD_PROBE_EACH") = "1"
shell.Environment("Process")("NS_AMD_UPSCALE_PRIVATE") = "0"

' --- Launch with no window (window style 0), without waiting ---
Dim extra, arg, q
q = Chr(34)
extra = ""
For Each arg In WScript.Arguments
    extra = extra & " " & q & arg & q
Next
shell.Run """" & py & """ -u """ & dir & "\main.py""" & extra, 0, False
