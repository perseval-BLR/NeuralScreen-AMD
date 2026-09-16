// NeuralScreen.exe - the friendly launcher.
//
// Does what NeuralScreen.vbs does, but as something a person can recognise: it
// has the program's icon, it is what you pin to a taskbar, and a double-click
// on it does not go through the script host.
//
// The work itself is unchanged - find a Python, sanity-check the two native
// DLLs, start main.py with pythonw so no console window appears, and get out
// of the way. Nothing is waited on: the overlay lives in the tray from then on.
//
// Every failure ends in a message box rather than a silent exit. A launcher
// that quietly does nothing is the worst possible outcome: the program draws
// over the desktop and otherwise gives no sign of itself, so "nothing
// happened" is indistinguishable from "it started".
//
// The .vbs stays in the repository next to this. Neither route is strictly
// better: an unsigned .exe meets SmartScreen on a machine that has never seen
// it, while a .vbs looks more suspicious to antivirus heuristics.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlwapi.h>
#include <string>

static const wchar_t *kTitle = L"NeuralScreen AMD";

static void Fail(const wchar_t *text)
{
    MessageBoxW(nullptr, text, kTitle, MB_ICONERROR | MB_OK);
}

static bool FileThere(const std::wstring &path)
{
    const DWORD a = GetFileAttributesW(path.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

// The folder this exe sits in, with a trailing backslash. Everything is
// resolved relative to it, never to the working directory: a shortcut can be
// started from anywhere at all.
static std::wstring ExeDir()
{
    wchar_t buf[MAX_PATH] = {};
    const DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return L"";
    std::wstring s(buf, n);
    const size_t cut = s.find_last_of(L'\\');
    return cut == std::wstring::npos ? L"" : s.substr(0, cut + 1);
}

static std::wstring EnvVar(const wchar_t *name)
{
    wchar_t buf[MAX_PATH] = {};
    const DWORD n = GetEnvironmentVariableW(name, buf, MAX_PATH);
    return (n > 0 && n < MAX_PATH) ? std::wstring(buf, n) : std::wstring();
}

// NEURALSCREEN_PYTHON -> the bundled runtime -> pythonw from PATH. The bundled
// one is what the release archive ships; the other two are for a machine that
// already has Python and a checkout without the runtime.
static std::wstring FindPython(const std::wstring &dir)
{
    const std::wstring override_ = EnvVar(L"NEURALSCREEN_PYTHON");
    if (!override_.empty() && FileThere(override_)) return override_;

    const std::wstring bundled = dir + L"runtime\\pythonw.exe";
    if (FileThere(bundled)) return bundled;

    wchar_t found[MAX_PATH] = {};
    if (SearchPathW(nullptr, L"pythonw.exe", nullptr, MAX_PATH, found, nullptr))
        return std::wstring(found);
    return L"";
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int)
{
    const std::wstring dir = ExeDir();
    if (dir.empty())
    {
        Fail(L"NeuralScreen could not work out where it is installed.");
        return 1;
    }

    const std::wstring main_py = dir + L"main.py";
    if (!FileThere(main_py))
    {
        Fail(L"main.py is missing next to NeuralScreen.exe.\n\n"
             L"Unpack the whole release archive into one folder and run it "
             L"from there.");
        return 1;
    }

    // The NVIDIA runtime is 165 MB and is not in the repository, so a checkout
    // without it is the single most likely way to end up here.
    if (!FileThere(dir + L"native\\nvngx_dlssnr.dll"))
    {
        Fail(L"native\\nvngx_dlssnr.dll is missing.\n\n"
             L"The neural runtimes read it: on the AMD path the Radeon "
             L"runtime's installer takes the network weights out of it, and "
             L"the NVIDIA path loads it directly.\n\n"
             L"Download the release archive again and unpack the whole "
             L"thing. See README.md, section What you need.");
        return 1;
    }
    if (!FileThere(dir + L"native\\nvngx.dll"))
    {
        Fail(L"native\\nvngx.dll is missing.\n\n"
             L"Build it with native\\build-host.bat, or download the release "
             L"archive again.");
        return 1;
    }

    const std::wstring python = FindPython(dir);
    if (python.empty())
    {
        Fail(L"pythonw.exe was not found.\n\n"
             L"Use the release archive - it comes with a portable Python - or "
             L"install Python and make sure it is on PATH.");
        return 1;
    }

    // -u keeps the log unbuffered, so NeuralScreen.log is useful while the
    // program is still running rather than only after it exits.
    std::wstring cmd = L"\"" + python + L"\" -u \"" + main_py + L"\"";

    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi = {};
    std::wstring mutable_cmd = cmd;
    const BOOL ok = CreateProcessW(nullptr, &mutable_cmd[0], nullptr, nullptr,
                                   FALSE, CREATE_NO_WINDOW, nullptr,
                                   dir.c_str(), &si, &pi);
    if (!ok)
    {
        const DWORD err = GetLastError();
        wchar_t msg[512];
        wsprintfW(msg, L"NeuralScreen could not start Python (error %lu).\n\n%s",
                  err, python.c_str());
        Fail(msg);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
