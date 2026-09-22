' NeuralScreen-probe-private.vbs - the per-frame probe with the upscale on a
' private copy of the upscaler (NS_AMD_PROBE_EACH=1, NS_AMD_UPSCALE_PRIVATE=1)
'
' The control for NeuralScreen-probe-mv.vbs. The neural runtime hooks the
' upscaler DLL it finds; here the upscale step runs on a second copy of the
' same file that it never hooked, so the runtime does not see that dispatch at
' all. Nothing else changes. If this run alone changes the flicker, the runtime
' seeing the upscale was part of it; if not, the -mv run isolates the vectors.
' A test, not a fix. Slow picture: a diagnosis, not a way to play.

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

' --- The arm under test: the upscale on a private copy of the upscaler ---
'
' The worker's log says whether the copy loaded ("the upscale dispatch's module:
' ...").
shell.Environment("Process")("NS_AMD_UPSCALE_PRIVATE") = "1"

' --- Launch with no window (window style 0), without waiting ---
Dim extra, arg, q
q = Chr(34)
extra = ""
For Each arg In WScript.Arguments
    extra = extra & " " & q & arg & q
Next
shell.Run """" & py & """ -u """ & dir & "\main.py""" & extra, 0, False
