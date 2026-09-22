r"""Shell chrome and our own windows never count as "something covered us".

WHY THIS EXISTS
---------------
Ported from the MAIN line, where it was measured (#107: 18 windows took the
top in one reporter's log). The shape is the same on both sides of the port:

    [z] foreign-above-hud  class='Shell_TrayWnd'                 pid=8020
    [z] foreign-above-hud  class='XamlExplorerHostIslandWindow'  pid=8020
    [z] foreign-above-hud  class='#32770' title='Save As'        pid=46044

pid 8020 owns the shell; pid 46044 is the client itself (its own modal Save As
dialog, the window the user is looking at while the screenshot is written).
Neither is a stranger that took our place. Both were read as one, so the guard
raised the picture and then the panel over a state that was already correct -
one DWM recompose with the picture over the panel in between, which reads as
the panel blinking. The worker's own ReassertPresentTopmost did the same thing
every 300 frames.

The AMD line carried the older rule on BOTH sides: the C++ re-assert decided by
a class-literal list (pygame / NeuralScreenPresent) and the Python walk had no
pid rule at all, while the worker's z-order guard and the client's own dialog
still took the top slot and triggered a raise-and-return.

WHAT THIS LOCKS
---------------
1. Both sides apply the rule: the client walk (display.py) and the worker's
   re-assert (native/dlss5-feed-host64.cpp).
2. The rule is by PROCESS, not by a class list: the shell's pid comes from
   GetShellWindow, because the flyouts above the taskbar are not Shell_TrayWnd
   and a list would have to name every one.
3. The window this exists for - a real application over the picture - still
   raises it.
4. The skip is CONSULTED by the walk, not merely defined next to it.

The classification is read as source; the two file paths cannot be exercised
without two processes and a live desktop.

Run:  runtime\\python.exe tests/test_foreign_window_rule.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def strip_prose(text: str) -> str:
    """Drop comments and docstrings so a check reads code, not prose.

    A test that a comment can satisfy tests nothing: the docstring here names
    GetShellWindow and Shell_TrayWnd while explaining what the rule is for, so
    a version that did none of it would stay green if the check read prose.
    """
    without_docs = re.sub(r'""".*?"""', "", text, flags=re.S)
    without_docs = re.sub(r"'''.*?'''", "", without_docs, flags=re.S)
    return "\n".join(line for line in without_docs.splitlines()
                     if not line.lstrip().startswith("#"))


def strip_cpp_prose(text: str) -> str:
    """The same for C++: `//` and `/* */` are prose, not code.

    Found by mutation here: the worker check searched the function body for
    SetWindowPos, and the body's own comment says "two SetWindowPos on a state
    that was already correct" - so deleting the actual call left the test
    GREEN. Exactly the failure the docstring above warns about, one language
    over. Every C++ check reads this stripped form.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line)
                     for line in without_blocks.splitlines())


def main() -> int:
    failures: list[str] = []
    display = (BASE / "display.py").read_text(encoding="utf-8")
    cpp = (BASE / "native" / "dlss5-feed-host64.cpp").read_text(
        encoding="utf-8", errors="surrogateescape")

    # ---- 1. the client side -------------------------------------------------
    helper = re.search(r"def _own_or_shell_window\(self, hwnd\).*?(?=\n    @|\n    def )",
                       display, re.S)
    if helper is None:
        failures.append(
            "display.py has no _own_or_shell_window: the z-order walk reads "
            "the taskbar and our own dialog as strangers again")
    else:
        body = helper.group(0)
        code = strip_prose(body)
        if "GetShellWindow" not in code:
            failures.append(
                "the shell's process is no longer asked for - the shell's pid "
                "comes from its desktop window, not from the taskbar's class")
        if "GetCurrentProcessId" not in code:
            failures.append(
                "_own_or_shell_window does not compare against our own pid - "
                "the program's own Save As dialog would count as a stranger")
        # The comparison itself: both pids must be in it. Checking that the
        # attribute is merely mentioned passes on a version that reads it and
        # then ignores it.
        if not re.search(r"got\s+in\s*\(\s*self\._own_pid\s*,\s*self\._shell_pid\s*\)",
                         code):
            failures.append(
                "_own_or_shell_window does not compare against BOTH our own and "
                "the shell's pid - the taskbar would count as a stranger again")
        if "Shell_TrayWnd" in code:
            failures.append(
                "_own_or_shell_window decides by class name: the shell's pid is "
                "the rule, because the flyouts above the taskbar are not "
                "Shell_TrayWnd")
        # The walk has to consult it, or the helper is dead code.
        walk = display.split("def _top_real_window", 1)[-1].split("\n    def ", 1)[0]
        if "_own_or_shell_window(" not in strip_prose(walk):
            failures.append(
                "_top_real_window never calls _own_or_shell_window: the rule "
                "exists but the guard does not apply it")

    # ---- 2. the worker side -------------------------------------------------
    reassert = re.search(r"static void ReassertPresentTopmost\(\)\s*\{.*?\n\}",
                         strip_cpp_prose(cpp), re.S)
    if reassert is None:
        failures.append("ReassertPresentTopmost is gone from the worker")
    else:
        b = strip_cpp_prose(reassert.group(0))
        shell_helper = cpp.split("static DWORD ShellProcessId()", 1)[-1][:400]
        if "GetShellWindow" not in strip_cpp_prose(shell_helper):
            failures.append(
                "the worker does not ask for the shell's desktop window: the "
                "shell pid must come from there, not from a class list")
        if "GetCurrentProcessId" not in b:
            failures.append(
                "the worker's re-assert does not compare against its own pid")
        if "GetWindowThreadProcessId" not in b:
            failures.append(
                "the worker's re-assert does not look up the top window's "
                "process - it raises the picture over anything again")
        # The comparison as one expression: checking that the helper exists
        # passes on a version that never consults it, and checking for the
        # substrings passes on one that only names them in the log line.
        gate = re.sub(r"\s+", " ", " ".join(re.findall(r"if \(pid != 0.*?\)\)", b, re.S)))
        for term, what in (("(shell != 0 && pid == shell)", "the shell's pid"),
                           ("(client != 0 && pid == client)", "the client's own pid"),
                           ("pid == self", "this process")):
            if term not in gate:
                failures.append(
                    f"the worker's re-assert gate does not compare against "
                    f"{what}: that window would raise the picture over the "
                    f"panel again")
        if "SetWindowPos" not in b:
            failures.append(
                "ReassertPresentTopmost no longer raises the picture at all: "
                "the borderless-game case (a real window over ours) is what "
                "this function exists for")
        if "IsWindowVisible" not in b:
            failures.append(
                "the worker's re-assert no longer skips hidden windows")
        # The old class-literal rule must be gone from the gate: leaving it in
        # as a second, weaker test is how the shell's flyouts slip through.
        if "Shell_TrayWnd" in b:
            failures.append(
                "the worker's re-assert decides by class name - the flyouts "
                "above the taskbar are not Shell_TrayWnd")

    # ---- 3. both sides share the rule ---------------------------------------
    # Read from code, not prose: the comment above ShellProcessId names the
    # helper it defines, so an unstripped check would pass on a version that
    # deleted the helper and left the sentence explaining it.
    if "ShellProcessId" not in strip_cpp_prose(cpp):
        failures.append(
            "the worker reads the shell pid inline instead of through one "
            "helper - the client and the worker are supposed to share the rule")
    if not re.search(r"static DWORD HudProcessId\(\)", strip_cpp_prose(cpp)):
        failures.append(
            "the worker has no HudProcessId: our own program's windows would "
            "count as strangers")

    if failures:
        print("FAIL: shell and own-program windows are treated as strangers")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: shell chrome and our own windows are skipped by both the client "
          "walk and the worker's re-assert, while a real window still raises")
    return 0


if __name__ == "__main__":
    sys.exit(main())
