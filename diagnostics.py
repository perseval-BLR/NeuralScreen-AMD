"""Privacy-safe, deterministic diagnostic bundles for NeuralScreen.

The module deliberately has no dependency on the UI or the processing
pipeline, so it can still produce a report after either of them fails.  The
future UI integration only needs to construct :class:`DiagnosticBundleRequest`
and call :func:`create_diagnostic_bundle`.

The resulting ZIP always contains exactly two files:

``diagnostics.json``
    Application/runtime identity, graphics environment and failure details.

``log_tail.txt``
    A bounded, scrubbed tail of ``NeuralScreen.log``.

No configuration or environment dump is collected.  ZIP metadata and JSON
ordering are fixed, making identical inputs produce identical bytes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence
import zipfile


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_BYTES = 64 * 1024
MAX_LOG_BYTES = 256 * 1024
SCHEMA = "neuralscreen.diagnostics/v1"
_REDACTED = "<REDACTED>"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class DiagnosticBundleRequest:
    """Inputs for one diagnostic bundle.

    ``failure_stage`` is required so reports cannot silently lose the most
    useful routing fact.  ``failure_details`` may contain HRESULT, SEH and DRED
    fields.  The module recursively scrubs every supplied string and replaces
    values under secret-looking keys.

    ``system_snapshot`` and ``runtime_signature`` are optional injection
    points for callers that already have authoritative data.  When omitted,
    this module performs best-effort, read-only Windows probes.

    ``sensitive_values`` is for application-specific opaque values which have
    no recognisable secret prefix.  They are removed in addition to usernames,
    home/temp paths, absolute paths and common credential formats.
    """

    failure_stage: str
    failure_details: Mapping[str, Any] = field(default_factory=dict)
    app_version: str | None = None
    commit: str | None = None
    runtime_path: str | os.PathLike[str] | None = None
    log_path: str | os.PathLike[str] | None = None
    system_snapshot: Mapping[str, Any] | None = None
    runtime_signature: Mapping[str, Any] | None = None
    sensitive_values: Sequence[str] = field(default_factory=tuple)
    max_log_bytes: int = DEFAULT_LOG_BYTES


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_RE = re.compile(
    r"(?i)\b(?P<key>proxy-authorization|authorization)"
    r"(?P<sep>\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<key>[a-z0-9_.-]*"
    r"(?:api[_-]?key|access[_-]?key|client[_-]?secret|secret|token|"
    r"password|passwd|pwd|cookie|session[_-]?id)"
    r"[a-z0-9_.-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_URI_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)"
    r"[^/@\s:]+:[^/@\s]+@"
)
_KNOWN_TOKEN_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
)
_SECRET_KEY_PARTS = (
    "apikey",
    "accesskey",
    "clientsecret",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "sessionid",
    "privatekey",
    "connectionstring",
)

# Quoted paths are removed precisely.  For an unquoted path the rest of that
# line is removed as well: Windows paths may legally contain spaces, so a more
# optimistic boundary can leave the private half of a path in the report.
_QUOTED_PATH_RE = re.compile(
    r"(?P<quote>[\"'])(?P<path>"
    r"(?:[A-Za-z]:[\\/]|\\\\(?!\.\\)|/(?!/))"
    r"[^\"'\r\n]*)(?P=quote)"
)
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/]|\\\\(?!\.\\))[^\r\n]*"
)
_POSIX_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/(?!/)"
    r"(?:[^/\s<>\"'|]+/)+[^\r\n<>\"'|]*"
)
_DISPLAY_DEVICE_RE = re.compile(r"\\\\\.\\DISPLAY\d+", re.IGNORECASE)


def _secret_key(key: object) -> bool:
    normal = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return any(part in normal for part in _SECRET_KEY_PARTS)


def _privacy_literals(extra: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """Return known private paths, usernames and caller-supplied literals."""
    paths: set[str] = set()
    users: set[str] = set()
    custom: set[str] = {str(value) for value in extra if str(value)}

    for name in ("USERPROFILE", "HOME", "TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            paths.add(value)
    try:
        paths.add(str(Path.home()))
    except Exception:
        pass
    try:
        paths.add(tempfile.gettempdir())
    except Exception:
        pass
    for name in ("USERNAME", "USER"):
        value = os.environ.get(name)
        if value:
            users.add(value)
    try:
        users.add(getpass.getuser())
    except Exception:
        pass

    # Match either separator style.  Longest first prevents a parent path
    # from exposing the private suffix of a more specific temp directory.
    path_variants = set()
    for value in paths:
        stripped = value.rstrip("\\/")
        if stripped:
            path_variants.update(
                (stripped, stripped.replace("\\", "/"), stripped.replace("/", "\\"))
            )
    return (
        sorted(path_variants, key=len, reverse=True),
        sorted((u for u in users if u), key=len, reverse=True),
        sorted(custom, key=len, reverse=True),
    )


def _replace_literal(text: str, value: str, replacement: str, *, token: bool) -> str:
    if not value:
        return text
    pattern = re.escape(value)
    if token:
        pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
    return re.sub(pattern, lambda _match: replacement, text, flags=re.IGNORECASE)


def _redact_absolute_paths(text: str) -> str:
    # Win32 display identifiers look like UNC paths but are useful, stable
    # hardware identities.  Shield and restore them around generic path rules.
    devices: list[str] = []

    def shield(match: re.Match[str]) -> str:
        devices.append(match.group(0))
        return f"__NS_DISPLAY_{len(devices) - 1}__"

    text = _DISPLAY_DEVICE_RE.sub(shield, text)
    text = _QUOTED_PATH_RE.sub(lambda m: m.group("quote") + "<PATH>" + m.group("quote"), text)
    text = _WINDOWS_PATH_RE.sub("<PATH>", text)
    text = _POSIX_PATH_RE.sub("<PATH>", text)
    for index, device in enumerate(devices):
        text = text.replace(f"__NS_DISPLAY_{index}__", device)
    return text


def sanitize_text(text: object, *, sensitive_values: Sequence[str] = ()) -> str:
    """Return text safe to place in a support bundle.

    The scrubber is intentionally conservative: an unquoted absolute path
    consumes the remainder of its line rather than guessing where a path with
    spaces ends.  URLs are preserved, while drive, UNC and POSIX paths are not.
    """
    result = str(text)
    paths, users, custom = _privacy_literals(sensitive_values)

    for value in paths:
        result = _replace_literal(result, value, "<HOME_OR_TEMP>", token=False)
    for value in users:
        # Do not use <USER>: on an account literally named "User" that
        # placeholder redacts itself again and breaks the fail-closed
        # idempotence check.
        result = _replace_literal(result, value, "<ACCOUNT>", token=True)
    for value in custom:
        result = _replace_literal(result, value, _REDACTED, token=False)

    result = _PRIVATE_KEY_RE.sub(_REDACTED, result)
    result = _AUTH_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", result
    )
    result = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", result
    )
    result = _URI_CREDENTIAL_RE.sub(
        lambda m: f"{m.group('scheme')}{_REDACTED}@", result
    )
    for pattern in _KNOWN_TOKEN_RES:
        result = pattern.sub(_REDACTED, result)
    return _redact_absolute_paths(result)


def _sanitize_value(value: Any, sensitive_values: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        clean = {}
        for key, item in value.items():
            clean_key = sanitize_text(key, sensitive_values=sensitive_values)
            clean[clean_key] = (
                _REDACTED
                if _secret_key(key)
                else _sanitize_value(item, sensitive_values)
            )
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, sensitive_values) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_sanitize_value(item, sensitive_values) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, Path):
        return sanitize_text(value, sensitive_values=sensitive_values)
    if isinstance(value, bytes):
        return f"<BYTES:{len(value)}>"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(value, sensitive_values=sensitive_values)


def _assert_scrubbed(text: str, sensitive_values: Sequence[str]) -> None:
    """Fail closed if another scrub pass can still remove private material."""
    if sanitize_text(text, sensitive_values=sensitive_values) != text:
        raise ValueError("diagnostic privacy check found unsanitized data")


def _assert_report_scrubbed(report: Mapping[str, Any], sensitive_values: Sequence[str]) -> None:
    """Check values before JSON escaping changes Win32 display identifiers."""
    if _sanitize_value(report, sensitive_values) != report:
        raise ValueError("diagnostic privacy check found unsanitized data")


def _read_manifest(base_dir: Path) -> tuple[str | None, str | None]:
    path = base_dir / "VERSION.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    version_match = re.search(r"(?m)^NeuralScreen\s+(\S+)\s*$", text)
    commit_match = re.search(r"(?mi)^commit:\s*([0-9a-f]{7,64})\s*$", text)
    return (
        version_match.group(1) if version_match else None,
        commit_match.group(1).lower() if commit_match else None,
    )


def _version_from_source(base_dir: Path) -> str | None:
    try:
        tree = ast.parse((base_dir / "settings_io.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def discover_application_identity(base_dir: Path = BASE_DIR) -> dict[str, str]:
    """Discover app version and commit without importing application modules."""
    version, commit = _read_manifest(base_dir)
    version = version or _version_from_source(base_dir) or "unknown"
    if not commit:
        try:
            completed = subprocess.run(
                ["git", "-C", str(base_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            candidate = completed.stdout.strip().lower()
            if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate):
                commit = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    return {"name": "NeuralScreen", "version": version, "commit": commit or "unknown"}


def _runtime_candidate(explicit: str | os.PathLike[str] | None) -> tuple[Path, str]:
    if explicit is not None:
        return Path(explicit), "explicit"
    configured = os.environ.get("NS_NR_DLL")
    if configured:
        return Path(configured), "configured"
    byo = BASE_DIR / "native" / "libraries" / "nvngx_dlssnr.dll"
    if byo.is_file():
        return byo, "libraries-candidate"
    return BASE_DIR / "native" / "nvngx_dlssnr.dll", "bundled"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authenticode_signature(path: Path) -> dict[str, str]:
    if os.name != "nt":
        return {"status": "unavailable"}
    # Prefer PowerShell 7 when present.  A parent PowerShell 7 session may
    # prepend its module folder to PSModulePath; Windows PowerShell then sees
    # an incompatible Microsoft.PowerShell.Security module first.
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not powershell:
        return {"status": "unavailable"}
    script = (
        "& { param([string]$p)"
        "$m=Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\"
        "Microsoft.PowerShell.Security.psd1';"
        "if(Test-Path -LiteralPath $m){Import-Module $m -ErrorAction Stop};"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "$c=$s.SignerCertificate;"
        "[ordered]@{status=[string]$s.Status;"
        "subject=$(if($c){[string]$c.Subject}else{''});"
        "issuer=$(if($c){[string]$c.Issuer}else{''});"
        "thumbprint=$(if($c){[string]$c.Thumbprint}else{''})}"
        "|ConvertTo-Json -Compress}"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return {"status": "unavailable"}
        data = json.loads(completed.stdout)
        return {
            "status": str(data.get("status") or "unknown"),
            "subject": str(data.get("subject") or ""),
            "issuer": str(data.get("issuer") or ""),
            "thumbprint": str(data.get("thumbprint") or ""),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable"}


def inspect_runtime(
    runtime_path: str | os.PathLike[str] | None = None,
    *,
    signature: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the runtime filename, digest and best-effort signature identity."""
    path, source = _runtime_candidate(runtime_path)
    result: dict[str, Any] = {
        "name": path.name or "nvngx_dlssnr.dll",
        "source": source,
        "size_bytes": None,
        "sha256": "",
        "signature": {"status": "not-checked"},
    }
    try:
        result["size_bytes"] = path.stat().st_size
        result["sha256"] = _sha256(path)
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    result["signature"] = dict(signature) if signature is not None else _authenticode_signature(path)
    return result


def _windows_gpus() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    base_name = (
        r"SYSTEM\CurrentControlSet\Control\Class"
        r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    found: list[dict[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_name) as base:
            count = winreg.QueryInfoKey(base)[0]
            for index in range(count):
                sub_name = winreg.EnumKey(base, index)
                if not re.fullmatch(r"\d{4}", sub_name):
                    continue
                try:
                    with winreg.OpenKey(base, sub_name) as key:
                        name = str(winreg.QueryValueEx(key, "DriverDesc")[0]).strip()
                        driver = str(winreg.QueryValueEx(key, "DriverVersion")[0]).strip()
                        try:
                            provider = str(winreg.QueryValueEx(key, "ProviderName")[0]).strip()
                        except OSError:
                            provider = ""
                except OSError:
                    continue
                if name:
                    found.append(
                        {"name": name, "driver_version": driver, "provider": provider}
                    )
    except OSError:
        return []
    unique = {json.dumps(item, sort_keys=True): item for item in found}
    return sorted(unique.values(), key=lambda item: (item["name"], item["driver_version"]))


def _windows_displays() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class MonitorInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        displays: list[dict[str, Any]] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def callback(monitor, _dc, _rect, _data):
            info = MonitorInfoEx()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcMonitor
                displays.append(
                    {
                        "name": info.szDevice,
                        "left": int(rect.left),
                        "top": int(rect.top),
                        "width": int(rect.right - rect.left),
                        "height": int(rect.bottom - rect.top),
                        "primary": bool(info.dwFlags & 1),
                    }
                )
            return True

        callback_ref = callback_type(callback)
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, callback_ref, 0)
        return sorted(displays, key=lambda item: (item["left"], item["top"], item["name"]))
    except Exception:
        return []


def collect_system_snapshot() -> dict[str, Any]:
    """Collect non-identifying OS, GPU/driver and display information."""
    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "gpus": _windows_gpus(),
        "displays": _windows_displays(),
    }


def _tail(path: Path, limit: int) -> tuple[str, dict[str, Any]]:
    metadata = {"included": False, "truncated": False, "bytes": 0}
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(-limit, os.SEEK_END)
            raw = handle.read(limit)
    except OSError:
        return "", metadata
    metadata["included"] = True
    metadata["truncated"] = size > limit
    return raw.decode("utf-8", errors="replace"), metadata


def _bounded_utf8_tail(text: str, limit: int) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return raw
    # Drop a partial leading UTF-8 sequence after slicing from the end.
    return raw[-limit:].decode("utf-8", errors="ignore").encode("utf-8")


#: How much of a minidump a bundle may carry. The worker's own crash is a
#: fast fail, and a fast fail cannot be caught: 0xC0000409 (STACK_BUFFER_OVERRUN)
#: is __fastfail, which bypasses SEH, the vectored handlers and
#: SetUnhandledExceptionFilter alike, so the host's CrashFilter is never called
#: and no [crash] line with an address or a module is ever written. A three-Radeon
#: report showed exactly that: six crashes in one log, zero [crash] lines.
#:
#: That leaves the process's own dump as the only place the faulting stack
#: exists, and Windows writes one only when LocalDumps is configured. This is
#: the collector for it - so a bundle carries the reason instead of the symptom.
CRASH_DUMP_BYTES = 96 * 1024 * 1024
_CRASH_DUMP_LIMIT = 3
#: Only our own process's dumps, by executable name. A bundle must never ship
#: some other program's crash (the same machine is a workstation).
_CRASH_DUMP_EXES = ("nvngx.dll", "dlss5-feed-host64.exe")
#: Slack when comparing a dump's mtime against this process's start: FILETIME
#: granularity plus the moment between the process starting and the folder
#: entry being stamped. Two seconds is far below the gap between sessions and
#: far above any scheduling jitter.
_CRASH_DUMP_SLACK_S = 2.0


def process_start_time() -> float:
    """When THIS process started, as a POSIX timestamp (0.0 if unknown).

    Used to decide whether a dump belongs to this run at all. Windows keeps
    dumps in a folder ACROSS sessions, so a collector that only sorts by
    modification time happily ships the previous session's crash as if it were
    this one's - and a reader then diagnoses a build that is not in front of
    them.

    Measured cost of not having this: one reporter's bundle carried the same
    three dumps in two consecutive packages, and those dumps were from an
    older release folder entirely (`...-v0.1.17-alpha-full\`) with an older
    runtime image (SizeOfImage 0x6df000 = v0.2.14) than the build under test.
    The faulting stack was read and believed before the paths gave it away.
    """
    if os.name != "nt":
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return 0.0
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if ticks == 0:
            return 0.0
        # FILETIME is 100 ns intervals since 1601-01-01.
        return ticks / 10_000_000.0 - 11_644_473_600.0
    except Exception:
        return 0.0


def _crash_dump_dirs() -> list[Path]:
    """Where Windows writes dumps, in the order it is configured to."""
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "CrashDumps")
    program = os.environ.get("ProgramData")
    if program:
        dirs.append(Path(program) / "Microsoft" / "Windows" / "WER" / "ReportArchive")
    return dirs


#: Our own dumps, written by the worker's CrashFilter next to its log. These
#: are the ones that matter: they need no registry key and no administrator, so
#: they exist on machines where Windows Error Reporting is not configured -
#: which, measured, is four out of five reporters.
_OWN_DUMP_GLOB = ("*.dmp",)


def _own_worker_dirs() -> list[Path]:
    """Folders that may hold our own dump, newest first."""
    dirs: list[Path] = []
    here = Path(__file__).resolve().parent
    dirs.append(here / "native")
    dirs.append(here)
    # The worker's own directory is the one the app was started from.
    if getattr(sys, "frozen", False):
        dirs.insert(0, Path(sys.executable).resolve().parent / "native")
    return dirs


def collect_own_crash_dumps(limit: int = _CRASH_DUMP_LIMIT) -> list[tuple[str, bytes]]:
    """Dumps THIS process's worker wrote, as ``(name, bytes)``.

    Preferred over the WER folder: ours need no configuration to exist, so they
    are present exactly when the crash we are diagnosing happened here.
    """
    started = process_start_time()
    cutoff = started - _CRASH_DUMP_SLACK_S if started > 0 else 0.0
    found: list[tuple[float, Path]] = []
    for directory in _own_worker_dirs():
        try:
            for entry in directory.glob("*.dmp"):
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if cutoff > 0.0 and mtime < cutoff:
                    continue
                found.append((mtime, entry))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    dumps: list[tuple[str, bytes]] = []
    for _, path in found[:limit]:
        try:
            if path.stat().st_size > CRASH_DUMP_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        # Read the signature rather than trusting the extension: a truncated
        # or unrelated file would otherwise ride along and waste a reader's
        # time trying to open it.
        if not data.startswith(b"MDMP"):
            continue
        dumps.append((f"crashdump/{len(dumps) + 1}.dmp", data))
    return dumps


def collect_crash_dumps(limit: int = _CRASH_DUMP_LIMIT) -> list[tuple[str, bytes]]:
    """Newest minidumps of OUR worker, as ``(name, bytes)`` for the archive.

    Read-only and best effort: a missing folder, a dump another process still
    holds, anything at all - the bundle is built without it rather than fail.

    ONLY DUMPS FROM THIS RUN. A dump written before this process started is a
    previous session's crash, and shipping it as this run's is worse than
    shipping nothing: the faulting stack looks authoritative, names real
    modules, and describes a build that is not the one under test. Measured on
    a report where the same three dumps appeared in two consecutive packages,
    both from an older release folder - so the reader was sent after a crash
    three builds old.

    The filter is the process start time, with a slack for clock granularity
    and for a dump written immediately as the process came up.
    """
    started = process_start_time()
    cutoff = started - _CRASH_DUMP_SLACK_S if started > 0 else 0.0

    found: list[tuple[float, Path]] = []
    for directory in _crash_dump_dirs():
        try:
            for entry in directory.glob("*.dmp"):
                name = entry.name.casefold()
                if not any(exe.casefold() in name for exe in _CRASH_DUMP_EXES):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if cutoff > 0.0 and mtime < cutoff:
                    continue          # a previous session's crash
                found.append((mtime, entry))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)

    dumps: list[tuple[str, bytes]] = []
    for _, path in found[:limit]:
        try:
            if path.stat().st_size > CRASH_DUMP_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        # The archive name is ours, never the raw file name: a dump name can
        # carry a username, and this zip is meant to be attached to an issue.
        dumps.append((f"crashdump/{len(dumps) + 1}.dmp", data))
    return dumps


#: The AMD runtime writes its own log beside itself, and it is the ONLY place
#: that says whether the engine came up: `env: HIP device ...`, `engine init ok`,
#: `staging ready`, the network's own job timings. Our log cannot answer that -
#: it checks OUR hooks, so `hooks are in place` reads the same whether the
#: engine started or not. That gap cost a two-day detour: every black-picture
#: report carried the engine's `encoded mean 0.000` while nothing said the
#: engine had ever initialised, and the answer was sitting in a file nobody
#: attached. Collect it so a bundle carries both halves.
_AMD_LOG_NAMES = ("dlssnr_on_amd.log",)
#: The runtime searches these, in this order, for the files it drives.
_AMD_LOG_DIRS = ("native", "native/amd", "")


def _amd_log_candidates() -> list[Path]:
    """Where the AMD runtime's own log may sit, next to the worker."""
    out: list[Path] = []
    override = os.environ.get("NS_AMD_DIR")
    if override:
        out.append(Path(override) / _AMD_LOG_NAMES[0])
    for name in _AMD_LOG_DIRS:
        for log in _AMD_LOG_NAMES:
            out.append(BASE_DIR / name / log if name else BASE_DIR / log)
    return out


def collect_runtime_log(max_bytes: int = MAX_LOG_BYTES) -> tuple[bytes, dict[str, Any]]:
    """The AMD engine's own log tail, as ``(bytes, metadata)`` for the archive.

    Read-only and best effort: the file is written by another process while we
    read it, so a partial tail is normal and never an error. The metadata is
    reported even when the file is absent, because "the engine wrote no log"
    and "we did not look in the right place" need different follow-ups.

    The tail, not the whole file: it is opened in append mode across runs, so
    its head describes a session that ended long ago.
    """
    meta: dict[str, Any] = {
        "found": False,
        "file": None,
        "bytes": 0,
        "note": (
            "the AMD runtime's own log; the only place that says whether the "
            "engine initialised (`engine init ok`) and what its network did"
        ),
    }
    for path in _amd_log_candidates():
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                raw = handle.read(max_bytes)
        except OSError:
            continue
        # The NAME only, never the full path: the path carries a username, and
        # this archive is meant to be attached to a public issue.
        meta.update({"found": True, "file": path.name, "bytes": len(raw)})
        return raw, meta
    return b"", meta


def crash_dump_policy() -> dict[str, Any]:
    """Whether this machine WOULD have written one, and where to look.

    Reported even when no dump was found, because the two answers differ:
    "the crash left no dump" and "this machine is not configured to write
    dumps at all" need different follow-ups, and a report that says only
    "no dump" sends the reader after the wrong one.
    """
    configured = False
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps",
            )
            try:
                winreg.QueryValueEx(key, "DumpFolder")
                configured = True
            except OSError:
                # The parent key exists, but only per-application subkeys do -
                # which does NOT catch a process we did not name.
                configured = False
            finally:
                winreg.CloseKey(key)
        except OSError:
            configured = False
        except ImportError:
            configured = False
    return {
        "local_dumps_configured": configured,
        "searched": [str(p) for p in _crash_dump_dirs()],
        "note": (
            "a fast fail (0xC0000409) bypasses SetUnhandledExceptionFilter, so "
            "the worker's own crash line cannot exist; the dump is the only "
            "record of the faulting stack"
        ),
    }


def _json_bytes(report: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.external_attr = 0o600 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _atomic_zip(
    destination: Path,
    report: bytes,
    log_tail: bytes,
    crash_dumps: Sequence[tuple[str, bytes]] = (),
    runtime_log: bytes = b"",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w+b") as handle:
            with zipfile.ZipFile(handle, mode="w") as archive:
                _write_zip_entry(archive, "diagnostics.json", report)
                _write_zip_entry(archive, "log_tail.txt", log_tail)
                if runtime_log:
                    _write_zip_entry(archive, "amd_runtime_log_tail.txt", runtime_log)
                for name, data in crash_dumps:
                    _write_zip_entry(archive, name, data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def create_diagnostic_bundle(
    destination: str | os.PathLike[str], request: DiagnosticBundleRequest
) -> Path:
    """Build and atomically publish a sanitized diagnostic ZIP.

    ``destination`` is the final ``.zip`` path.  The previous file, if any,
    remains intact unless a complete new archive has been flushed and closed.
    No UI or application state is mutated.
    """
    if not isinstance(request, DiagnosticBundleRequest):
        raise TypeError("request must be DiagnosticBundleRequest")
    if not isinstance(request.failure_stage, str) or not request.failure_stage.strip():
        raise ValueError("failure_stage must be a non-empty string")
    if not 1 <= request.max_log_bytes <= MAX_LOG_BYTES:
        raise ValueError(f"max_log_bytes must be between 1 and {MAX_LOG_BYTES}")

    target = Path(destination)
    if target.suffix.casefold() != ".zip":
        raise ValueError("diagnostic destination must end in .zip")

    identity = discover_application_identity()
    if request.app_version is not None:
        identity["version"] = request.app_version
    if request.commit is not None:
        identity["commit"] = request.commit

    runtime = inspect_runtime(request.runtime_path, signature=request.runtime_signature)
    system = (
        dict(request.system_snapshot)
        if request.system_snapshot is not None
        else collect_system_snapshot()
    )
    log_path = Path(request.log_path) if request.log_path is not None else BASE_DIR / "NeuralScreen.log"
    raw_log, log_metadata = _tail(log_path, request.max_log_bytes)

    safe_log_text = sanitize_text(raw_log, sensitive_values=request.sensitive_values)
    safe_log = _bounded_utf8_tail(safe_log_text, request.max_log_bytes)
    log_metadata["bytes"] = len(safe_log)

    # The dumps come with the bundle: on a fast-fail crash they are the only
    # record of the faulting stack (see collect_crash_dumps). The policy is
    # reported even when none was found, so "no dump here" is never confused
    # with "this machine does not write dumps".
    # Ours first: a dump this run wrote beats one Windows happened to keep.
    crash_dumps = collect_own_crash_dumps()
    source = "worker" if crash_dumps else "none"
    if not crash_dumps:
        crash_dumps = collect_crash_dumps()
        source = "wer" if crash_dumps else "none"
    log_metadata["crash_dumps"] = {
        "included": len(crash_dumps),
        "source": source,
        "policy": crash_dump_policy(),
        # Said out loud so a reader can tell a bundle that carried this run's
        # crash from one that carried nothing (the folder is per-machine and
        # outlives the session).
        "only_this_run": True,
        "process_started": process_start_time(),
    }

    # The engine's own log rides along too. Our log answers "what did the host
    # do"; only that file answers "did the engine come up", and a report that
    # carries one without the other cannot be diagnosed - which is exactly what
    # every black-picture report so far was.
    raw_runtime_log, runtime_log_meta = collect_runtime_log(request.max_log_bytes)
    if raw_runtime_log:
        safe_runtime_text = sanitize_text(
            raw_runtime_log.decode("utf-8", errors="replace"),
            sensitive_values=request.sensitive_values,
        )
        safe_runtime_log = _bounded_utf8_tail(safe_runtime_text, request.max_log_bytes)
        runtime_log_meta["bytes"] = len(safe_runtime_log)
    else:
        safe_runtime_log = b""
    log_metadata["runtime_log"] = runtime_log_meta

    report = _sanitize_value(
        {
            "schema": SCHEMA,
            "application": identity,
            "runtime": runtime,
            "graphics": {
                "gpus": system.get("gpus", []),
                "displays": system.get("displays", []),
            },
            "system": system.get("os", {}),
            "failure": {
                "stage": request.failure_stage.strip(),
                "details": dict(request.failure_details),
            },
            "log": log_metadata,
        },
        request.sensitive_values,
    )
    report_bytes = _json_bytes(report)

    # A second pass is the publication gate: if sanitization is not
    # idempotent, no archive replaces the existing destination.
    _assert_report_scrubbed(report, request.sensitive_values)
    _assert_scrubbed(safe_log.decode("utf-8", errors="strict"), request.sensitive_values)
    _atomic_zip(target, report_bytes, safe_log, crash_dumps, safe_runtime_log)
    return target


__all__ = [
    "CRASH_DUMP_BYTES",
    "DEFAULT_LOG_BYTES",
    "MAX_LOG_BYTES",
    "DiagnosticBundleRequest",
    "collect_crash_dumps",
    "collect_runtime_log",
    "collect_system_snapshot",
    "crash_dump_policy",
    "create_diagnostic_bundle",
    "discover_application_identity",
    "inspect_runtime",
    "sanitize_text",
]
