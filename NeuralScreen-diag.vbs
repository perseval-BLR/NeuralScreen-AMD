' NeuralScreen-diag.vbs - diagnostic launcher (NS_PHASE=1)
' Same as NeuralScreen.vbs, but turns on the worker phase profiler:
' the log then contains [phase]/[pure]/[host] lines that show exactly
' where the worker dies. Use it when asked for a diagnostic log.
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

' --- Diagnostic mode: NS_PHASE=1 turns on the worker phase profiler ---
shell.Environment("Process")("NS_PHASE") = "1"

' --- Launch with no window (window style 0), without waiting ---
shell.Run """" & py & """ -u """ & dir & "\main.py""", 0, False
