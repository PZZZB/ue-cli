"""ue_backend.py — Engine discovery + offline command execution (UAT/UBT).

Handles finding UE installations, locating tools, and running subprocess
commands for build/cook/package operations that don't require a running editor.
"""

import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Engine discovery ──────────────────────────────────────────────────


def find_engine_root(uproject_path: Optional[str] = None) -> Optional[str]:
    """Discover the Unreal Engine root directory.

    Strategy:
    1. If uproject_path given, parse EngineAssociation from .uproject:
       a. If it's a directory path, use directly
       b. If it's a version string (e.g. "5.7"), look up in registry
    2. Check UE_ENGINE_ROOT environment variable
    3. Windows registry (any registered engine, no version filter)

    Returns:
        Engine root path or None.
    """
    # Strategy 1: From .uproject
    if uproject_path:
        uproject = Path(uproject_path)
        if uproject.exists():
            try:
                data = json.loads(uproject.read_text(encoding="utf-8-sig"))
                assoc = data.get("EngineAssociation", "")
                if assoc:
                    # 1a: Path-based association (custom build)
                    if os.path.isdir(assoc):
                        return str(assoc)
                    # 1b: Version/GUID association — look up in registry
                    #     UE stores official engines in HKLM by version ("5.7"),
                    #     custom builds in HKCU by GUID ("{...}").
                    version_root = _find_engine_by_association(assoc)
                    if version_root:
                        return version_root
            except (json.JSONDecodeError, OSError):
                pass

    # Strategy 2: Environment variable
    env_root = os.environ.get("UE_ENGINE_ROOT")
    if env_root and _validate_engine_root(env_root):
        return env_root

    # Strategy 3: Windows registry (any registered engine)
    reg_root = _find_engine_from_registry()
    if reg_root:
        return reg_root

    return None


def _validate_engine_root(path: str) -> bool:
    """Check if a path is a valid UE engine root."""
    p = Path(path)
    # Must have Engine/Binaries and Engine/Build
    return (
        (p / "Engine" / "Binaries").is_dir()
        or (p / "Engine" / "Build").is_dir()
        or (p / "Engine" / "Source").is_dir()
    )


def _read_engine_version(engine_root: str) -> Optional[str]:
    """Read engine version (major.minor) from Build.version file.

    Args:
        engine_root: Path to engine root directory.

    Returns:
        Version string like "5.7" or None if not readable.
    """
    build_version_path = Path(engine_root) / "Engine" / "Build" / "Build.version"
    if not build_version_path.is_file():
        return None
    try:
        data = json.loads(build_version_path.read_text(encoding="utf-8"))
        major = data.get("MajorVersion")
        minor = data.get("MinorVersion")
        if major is not None and minor is not None:
            return f"{major}.{minor}"
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _find_engine_by_association(assoc: str) -> Optional[str]:
    """Find engine root by EngineAssociation string.

    Mirrors UE's own resolution logic:
    1. HKLM SOFTWARE\\EpicGames\\Unreal Engine\\<version> — official installs
       The subkey name IS the version (e.g. "5.7").
    2. HKCU SOFTWARE\\Epic Games\\Unreal Engine\\Builds — custom/source builds
       Value names are GUIDs like "{F9E7804A-...}", values are paths.

    Args:
        assoc: EngineAssociation from .uproject — version string ("5.7")
               or GUID ("{F9E7804A-46B1-30B0-1C7B-4B99E6AAB63F}").

    Returns:
        Engine root path or None.
    """
    if sys.platform != "win32":
        return None

    # 1. HKLM — subkey name matches version directly
    result = _find_engine_in_hklm(assoc)
    if result:
        return result

    # 2. HKCU Builds — lookup by GUID name, or scan by Build.version
    return _find_engine_in_hkcu(assoc)


def _find_engine_in_hklm(version: str) -> Optional[str]:
    """Look up engine in HKLM by version subkey name."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\EpicGames\Unreal Engine",
        )
        try:
            subkey = winreg.OpenKey(key, version)
            install_dir, _ = winreg.QueryValueEx(subkey, "InstalledDirectory")
            if _validate_engine_root(install_dir):
                return install_dir
        except OSError:
            pass
    except (ImportError, OSError):
        pass
    return None


def _find_engine_in_hkcu(assoc: str) -> Optional[str]:
    """Look up engine in HKCU Builds.

    If assoc is a GUID like "{F9E7804A-...}", look up the value by name.
    Otherwise, scan all entries and match by Build.version.
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Epic Games\Unreal Engine\Builds",
        )
        # GUID lookup: assoc is the value name
        if assoc.startswith("{") and assoc.endswith("}"):
            try:
                install_dir, _ = winreg.QueryValueEx(key, assoc)
                if isinstance(install_dir, str) and _validate_engine_root(install_dir):
                    return install_dir
            except OSError:
                pass
            return None
        # Version string: scan all entries, match by Build.version
        i = 0
        while True:
            try:
                name, install_dir, vtype = winreg.EnumValue(key, i)
                if isinstance(install_dir, str) and _validate_engine_root(install_dir):
                    engine_ver = _read_engine_version(install_dir)
                    if engine_ver == assoc:
                        return install_dir
                i += 1
            except OSError:
                break
    except (ImportError, OSError):
        pass
    return None


def _find_engine_from_registry() -> Optional[str]:
    """Try to find any engine path from Windows registry."""
    if sys.platform != "win32":
        return None
    # HKLM — official installs
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\EpicGames\Unreal Engine",
        )
        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(key, i)
                subkey = winreg.OpenKey(key, subkey_name)
                install_dir, _ = winreg.QueryValueEx(
                    subkey, "InstalledDirectory"
                )
                if _validate_engine_root(install_dir):
                    return install_dir
                i += 1
            except OSError:
                break
    except (ImportError, OSError):
        pass
    # HKCU — custom builds
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Epic Games\Unreal Engine\Builds",
        )
        i = 0
        while True:
            try:
                name, install_dir, vtype = winreg.EnumValue(key, i)
                if isinstance(install_dir, str) and _validate_engine_root(install_dir):
                    return install_dir
                i += 1
            except OSError:
                break
    except (ImportError, OSError):
        pass
    return None


_EDITOR_BINARY_PREFIXES = ("UnrealEditor", "UE4Editor")


def _editor_binary_candidates(engine_root: str | Path) -> list[Path]:
    root = Path(engine_root)
    bin_dir = root / "Engine" / "Binaries" / "Win64"
    return [
        bin_dir / "UnrealEditor.exe",
        bin_dir / "UnrealEditor-Cmd.exe",
        bin_dir / "UE4Editor.exe",
        bin_dir / "UE4Editor-Cmd.exe",
    ]


def find_editor_exe(engine_root: str) -> Optional[str]:
    """Locate the editor executable for UE5 or UE4."""
    for c in _editor_binary_candidates(engine_root):
        if c.exists():
            return str(c)
    return None


def get_editor_binary_prefix(engine_root: str | None) -> str:
    """Return the editor binary prefix used by an engine: UnrealEditor or UE4Editor."""
    if not engine_root:
        return "UnrealEditor"
    root = Path(engine_root)
    bin_dir = root / "Engine" / "Binaries" / "Win64"
    for prefix in _EDITOR_BINARY_PREFIXES:
        if (bin_dir / f"{prefix}.exe").exists() or (bin_dir / f"{prefix}-Cmd.exe").exists():
            return prefix
        if (bin_dir / f"{prefix}.modules").exists():
            return prefix
    return "UnrealEditor"


def find_uat(engine_root: str) -> Optional[str]:
    """Locate RunUAT.bat."""
    root = Path(engine_root)
    candidates = [
        root / "Engine" / "Build" / "BatchFiles" / "RunUAT.bat",
        root / "RunUAT.bat",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def find_build_bat(engine_root: str) -> Optional[str]:
    """Locate Build.bat."""
    root = Path(engine_root)
    candidates = [
        root / "Engine" / "Build" / "BatchFiles" / "Build.bat",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def find_generate_project_files(engine_root: str) -> Optional[str]:
    """Locate GenerateProjectFiles.bat."""
    root = Path(engine_root)
    candidates = [
        root / "GenerateProjectFiles.bat",
        root / "Engine" / "Build" / "BatchFiles" / "GenerateProjectFiles.bat",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def run_uat(
    engine_root: str,
    command: str,
    args: list[str] | None = None,
    log_file: str | None = None,
    log_label: str = "uat",
    project_dir: str | None = None,
    heartbeat_seconds: float = 60.0,
    on_start=None,
) -> dict:
    """Execute a UAT command synchronously.

    stdout/stderr are redirected directly to ``log_file`` (allocated under
    the project's ``Saved/Logs/`` if not provided) — never buffered in the
    caller's memory. This prevents huge build logs from polluting AI context.

    While the child runs, a heartbeat line is written to the calling
    process's stderr every ``heartbeat_seconds`` seconds (see
    ``_run_subprocess``) so AI callers tailing the stream can tell the
    build is still alive.

    This function blocks until UAT exits. For long-running builds (5-15 min
    compile, 15-30 min package), the caller is expected to wrap this in
    its own background mechanism rather than fork a detached child here —
    AI harnesses routinely use kill-on-job-close Job Objects that would
    kill any "detached" descendant when the CLI returns anyway.

    Args:
        engine_root: Path to engine root.
        command: UAT command name (e.g., "BuildCookRun").
        args: Additional arguments.
        log_file: Absolute path to write combined stdout+stderr. If None,
            a timestamped file is allocated under ``<project_dir>/Saved/Logs``
            (or the system temp dir if ``project_dir`` is None).
        log_label: Short tag used in the auto-allocated log filename
            (e.g. "compile", "cook", "package"). Also used as the
            heartbeat label.
        project_dir: Project root for default log location.
        heartbeat_seconds: Period between stderr "still alive" lines.
            Default 60s. Pass 0 to disable.

    Returns:
        ``{"returncode": int, "log_file": str, "duration_seconds": float}``.
        On startup failure (FileNotFoundError etc.): returncode=-1 and an
        extra ``"error"`` key with a short message.
    """
    uat = find_uat(engine_root)
    if not uat:
        return {
            "returncode": -1,
            "log_file": "",
            "duration_seconds": 0.0,
            "error": "RunUAT.bat not found",
        }

    cmd = [uat, command] + (args or [])
    if log_file is None:
        log_file = _allocate_log_path(project_dir, log_label)
    result = _run_subprocess(
        cmd,
        log_file=log_file,
        heartbeat_seconds=heartbeat_seconds,
        heartbeat_label=log_label,
        on_start=on_start,
    )
    result["command"] = cmd
    return result


def run_build(
    engine_root: str,
    target: str,
    platform: str = "Win64",
    config: str = "Development",
    extra_args: list[str] | None = None,
    log_file: str | None = None,
    log_label: str = "build",
    project_dir: str | None = None,
    heartbeat_seconds: float = 60.0,
    on_start=None,
) -> dict:
    """Execute Build.bat.

    stdout/stderr are redirected to ``log_file`` (see ``run_uat``). A
    heartbeat is written to stderr every ``heartbeat_seconds`` seconds.

    Args:
        engine_root: Path to engine root.
        target: Build target name.
        platform: Target platform.
        config: Build configuration.
        extra_args: Additional arguments.
        log_file: Absolute path to write combined output. Auto-allocated
            under ``<project_dir>/Saved/Logs`` if None.
        log_label: Short tag for the auto-allocated filename; also used
            as the heartbeat label.
        project_dir: Project root for default log location.
        heartbeat_seconds: Period between stderr heartbeats. Pass 0 to
            disable.

    Returns:
        Same shape as ``run_uat``.
    """
    build_bat = find_build_bat(engine_root)
    if not build_bat:
        return {
            "returncode": -1,
            "log_file": "",
            "duration_seconds": 0.0,
            "error": "Build.bat not found",
        }

    cmd = [build_bat, target, platform, config] + (extra_args or [])
    if log_file is None:
        log_file = _allocate_log_path(project_dir, log_label)
    return _run_subprocess(
        cmd,
        log_file=log_file,
        heartbeat_seconds=heartbeat_seconds,
        heartbeat_label=log_label,
        on_start=on_start,
    )


# Safety timeout: 24 hours. Not user-configurable.
# The AI should use `build stop` to cancel long-running builds.
_SAFETY_TIMEOUT = 86400


def _allocate_log_path(project_dir: str | None, label: str) -> str:
    """Build an absolute path under Saved/Logs for a new CLI log file.

    Falls back to the system temp dir when project_dir is None or not a
    directory, so we never fail just because the caller omitted it.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"cli_{label}_{ts}.log"
    if project_dir and Path(project_dir).is_dir():
        log_dir = Path(project_dir) / "Saved" / "Logs"
    else:
        import tempfile
        log_dir = Path(tempfile.gettempdir()) / "ue_cli_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir / filename)


def _build_output_encoding() -> str:
    return "mbcs" if sys.platform == "win32" else "utf-8"


def _decode_process_output(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "mbcs", "cp936", "gbk"):
        try:
            return data.decode(encoding, errors="replace")
        except Exception:
            continue
    return str(data)


def _windows_process_exists(pid: int) -> bool | None:
    """Return whether PID currently identifies a live Windows process."""
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        error_invalid_parameter = 87

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(synchronize, False, int(pid))
        if not handle:
            if ctypes.get_last_error() == error_invalid_parameter:
                return False
            return None

        try:
            wait_result = wait_for_single_object(handle, 0)
            if wait_result == wait_timeout:
                return True
            if wait_result == wait_object_0:
                return False
            return None
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_process_identity(pid: int) -> dict:
    """Return a fast, PID-reuse-safe identity from the Windows kernel.

    Unlike the richer ``Win32_Process`` CIM query, this uses process handles
    directly and remains responsive when WMI/CIM is overloaded.  The process
    creation timestamp is stable for the lifetime of a PID and therefore lets
    callers verify that persisted task metadata still refers to the same
    process before terminating it.
    """
    identity = {
        "query_ok": False,
        "found": False,
        "pid": int(pid),
        "identity_source": "win32_process_times",
    }
    if sys.platform != "win32":
        identity["error"] = "Windows process identity is unavailable on this platform."
        return identity

    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        error_invalid_parameter = 87

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        get_process_times.restype = wintypes.BOOL
        query_image_name = kernel32.QueryFullProcessImageNameW
        query_image_name.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query_image_name.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        access = process_query_limited_information | synchronize
        handle = open_process(access, False, int(pid))
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == error_invalid_parameter:
                identity.update(query_ok=True, found=False)
            else:
                identity.update(
                    error_code=error_code,
                    error=ctypes.FormatError(error_code).strip(),
                )
            return identity

        try:
            wait_result = wait_for_single_object(handle, 0)
            if wait_result == wait_object_0:
                identity.update(query_ok=True, found=False)
                return identity
            if wait_result != wait_timeout:
                identity["error"] = f"WaitForSingleObject returned {wait_result}."
                return identity

            created = FileTime()
            exited = FileTime()
            kernel_time = FileTime()
            user_time = FileTime()
            if not get_process_times(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                error_code = ctypes.get_last_error()
                identity.update(
                    found=True,
                    error_code=error_code,
                    error=ctypes.FormatError(error_code).strip(),
                )
                return identity

            creation_time = (
                int(created.dwHighDateTime) << 32
            ) | int(created.dwLowDateTime)
            identity.update(
                query_ok=True,
                found=True,
                creation_time=creation_time,
            )

            image_buffer = ctypes.create_unicode_buffer(32768)
            image_size = wintypes.DWORD(len(image_buffer))
            if query_image_name(
                handle,
                0,
                image_buffer,
                ctypes.byref(image_size),
            ):
                identity["image_path"] = image_buffer.value
            return identity
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        identity["error"] = str(exc)
        return identity


def _terminate_windows_process_result(pid: int, *, wait_timeout_ms: int = 3000) -> dict:
    """Terminate one verified Windows process without WMI/CIM or taskkill."""
    result = {
        "ok": False,
        "pid": int(pid),
        "method": "TerminateProcess",
    }
    if sys.platform != "win32":
        result["error"] = "TerminateProcess is unavailable on this platform."
        return result

    try:
        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        error_invalid_parameter = 87

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        terminate_process = kernel32.TerminateProcess
        terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
        terminate_process.restype = wintypes.BOOL
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(process_terminate | synchronize, False, int(pid))
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == error_invalid_parameter:
                result.update(
                    ok=True,
                    already_exited=True,
                    suggestion="Process already exited before native termination.",
                )
            else:
                result.update(
                    error_code=error_code,
                    error=ctypes.FormatError(error_code).strip(),
                )
            return result

        try:
            if wait_for_single_object(handle, 0) == wait_object_0:
                result.update(
                    ok=True,
                    already_exited=True,
                    suggestion="Process already exited before native termination.",
                )
                return result

            if not terminate_process(handle, 1):
                error_code = ctypes.get_last_error()
                result.update(
                    error_code=error_code,
                    error=ctypes.FormatError(error_code).strip(),
                )
                return result

            wait_result = wait_for_single_object(handle, int(wait_timeout_ms))
            result["wait_result"] = int(wait_result)
            if wait_result == wait_object_0:
                result.update(
                    ok=True,
                    suggestion="Native process-handle termination succeeded.",
                )
            elif wait_result == wait_timeout:
                result.update(
                    timeout=True,
                    error=f"Process did not exit within {wait_timeout_ms} ms.",
                )
            else:
                result["error"] = f"WaitForSingleObject returned {wait_result}."
            return result
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        result["error"] = str(exc)
        return result


def _taskkill_access_denied(result: dict) -> bool:
    text = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".lower()
    return any(token in text for token in ("access is denied", "拒绝访问", "存取被拒"))


def _classify_kill_result(result: dict) -> dict:
    text = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".lower()
    access_denied = _taskkill_access_denied(result)
    already_exited = any(token in text for token in ("not found", "not running", "没有找到", "找不到", "未找到"))
    reported_missing = already_exited or "no running instance" in text
    taskkill_succeeded = result.get("returncode") == 0 and not access_denied
    process_exists = result.get("process_exists_after_taskkill")
    if process_exists is not None:
        already_exited = reported_missing or not process_exists

    result["access_denied"] = access_denied
    result["already_exited"] = already_exited
    if reported_missing and process_exists is True:
        result["pid_state_race"] = True
    if taskkill_succeeded and process_exists is True:
        result["pid_state_race"] = True
        result["kill_confirmed_by_taskkill"] = True
    if process_exists is True:
        result["ok"] = False
    if already_exited:
        result["ok"] = True
        result["method"] = result.get("method", "taskkill") + "_already_exited"
        result["retry_suggested"] = False
        result["suggestion"] = "Process already exited before taskkill completed."
    elif access_denied:
        result["retry_suggested"] = False
        result["suggestion"] = (
            "Taskkill was denied. Run ue-cli from an elevated administrator shell, "
            "or close the UnrealEditor.exe process manually from Task Manager."
        )
    elif taskkill_succeeded:
        result["ok"] = True
        result["retry_suggested"] = False
        result["suggestion"] = "Taskkill reported successful process-tree termination."
    elif process_exists is True:
        result["retry_suggested"] = True
        result["suggestion"] = (
            f"Taskkill completed, but PID {result.get('pid')} is still running. "
            "Retry editor close; if it remains, inspect permissions or close the process manually."
        )
    elif not result.get("ok"):
        result["retry_suggested"] = True
        result["suggestion"] = (
            "Taskkill failed. Retry editor close once; if it repeats, inspect the pid "
            "and close UnrealEditor.exe manually."
        )
    return result


def _kill_process_tree_result(pid: int) -> dict:
    """Kill a process tree and return diagnostic details."""
    if sys.platform != "win32":
        try:
            import signal
            os.kill(pid, signal.SIGKILL)
            return {
                "ok": True,
                "pid": pid,
                "method": "os.kill",
                "retry_suggested": False,
            }
        except ProcessLookupError as exc:
            return {
                "ok": True,
                "pid": pid,
                "method": "os.kill_already_exited",
                "error": str(exc),
                "already_exited": True,
                "retry_suggested": False,
                "suggestion": "Process already exited.",
            }
        except PermissionError as exc:
            return {
                "ok": False,
                "pid": pid,
                "method": "os.kill",
                "error": str(exc),
                "access_denied": True,
                "retry_suggested": False,
                "suggestion": "Permission denied while killing process; rerun with sufficient privileges.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "pid": pid,
                "method": "os.kill",
                "error": str(exc),
                "retry_suggested": True,
                "suggestion": "Process kill failed; retry or close the process manually.",
            }

    cmd = ["taskkill", "/F", "/T", "/PID", str(pid)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            timeout=3,
        )
        result = {
            "ok": proc.returncode == 0,
            "pid": pid,
            "method": "taskkill",
            "command": cmd,
            "returncode": proc.returncode,
            "stdout": _decode_process_output(proc.stdout).strip(),
            "stderr": _decode_process_output(proc.stderr).strip(),
        }
        result["process_exists_after_taskkill"] = _windows_process_exists(pid)
        process_exists = result["process_exists_after_taskkill"]
        confirmation_attempts = 0
        if process_exists is True and not _taskkill_access_denied(result):
            for _ in range(5):
                confirmation_attempts += 1
                time.sleep(0.2)
                process_exists = _windows_process_exists(pid)
                if process_exists is not True:
                    break
            result["process_exists_after_taskkill"] = process_exists
            result["post_taskkill_confirmation_attempts"] = confirmation_attempts
            result["post_taskkill_confirmation_seconds"] = confirmation_attempts * 0.2
        return _classify_kill_result(result)
    except subprocess.TimeoutExpired as exc:
        fallback = _terminate_windows_process_result(pid)
        return {
            "ok": bool(fallback.get("ok")),
            "pid": pid,
            "method": "taskkill_then_TerminateProcess",
            "command": cmd,
            "taskkill_error": str(exc),
            "taskkill_timeout": True,
            "native_fallback": fallback,
            "already_exited": bool(fallback.get("already_exited")),
            "retry_suggested": not fallback.get("ok"),
            "suggestion": (
                "Taskkill timed out; native process-handle termination succeeded."
                if fallback.get("ok")
                else "Taskkill and native process-handle termination both failed."
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "pid": pid,
            "method": "taskkill",
            "command": cmd,
            "error": str(exc),
            "retry_suggested": True,
            "suggestion": "Taskkill failed before completion. Retry or close UnrealEditor.exe manually.",
        }


def _kill_process_tree(pid: int) -> bool:
    """Kill a process and all its descendants using taskkill /F /T.

    Uses the /T flag to terminate the entire process tree,
    which is critical for killing UAT→UBT→MSBuild→cl.exe chains.

    Args:
        pid: Process ID to kill.

    Returns:
        True if the kill command succeeded.
    """
    return bool(_kill_process_tree_result(pid).get("ok"))


def _windows_cmdline_to_argv(cmdline: str) -> list[str]:
    """Parse a Windows command line using the same API as the CRT."""
    if sys.platform != "win32" or not cmdline:
        return []

    try:
        import ctypes
        import ctypes.wintypes

        argc = ctypes.c_int()
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        argv = shell32.CommandLineToArgvW(cmdline, ctypes.byref(argc))
        if not argv:
            return []
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            kernel32.LocalFree(argv)
    except Exception:
        return []


def _strip_uproject_arg(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().strip('"')
    return value if value.lower().endswith(".uproject") else ""


def _extract_uproject_from_cmdline(cmdline: str) -> str:
    """Extract the .uproject path from an UnrealEditor command line."""
    args = _windows_cmdline_to_argv(cmdline)
    if args:
        for index, arg in enumerate(args):
            lowered = arg.lower().lstrip("-/")
            if lowered.startswith("project="):
                project = _strip_uproject_arg(arg.split("=", 1)[1])
                if project:
                    return project
            if lowered == "project" and index + 1 < len(args):
                project = _strip_uproject_arg(args[index + 1])
                if project:
                    return project

            direct = _strip_uproject_arg(arg)
            if direct:
                return direct

    quoted = re.search(r'"([^"]+?\.uproject)"', cmdline, flags=re.IGNORECASE)
    if quoted:
        return quoted.group(1)

    project_arg = re.search(
        r'[-/]Project=(?:"([^"]+?\.uproject)"|([^\s"]+?\.uproject))',
        cmdline,
        flags=re.IGNORECASE,
    )
    if project_arg:
        return project_arg.group(1) or project_arg.group(2) or ""

    direct = re.search(
        r'([A-Za-z]:\\[^\s"]+?\.uproject|[^\s"]+?\.uproject)',
        cmdline,
        flags=re.IGNORECASE,
    )
    return direct.group(1) if direct else ""


def _set_job_kill_on_close(job_handle, enabled: bool) -> bool:
    """Enable or disable kill-on-close for a Windows Job Object."""
    if sys.platform != "win32" or not job_handle:
        return False

    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL

        info = _ExtendedLimitInformation()
        if enabled:
            info.BasicLimitInformation.LimitFlags = 0x00002000
        return bool(
            kernel32.SetInformationJobObject(
                job_handle,
                9,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
    except Exception:
        return False


def _attach_kill_on_close_job(proc):
    """Attach a build root to a kill-on-close Windows Job Object."""
    if sys.platform != "win32":
        return None

    job_handle = None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job_handle = kernel32.CreateJobObjectW(None, None)
        process_handle = getattr(proc, "_handle", None)
        if (
            not job_handle
            or not process_handle
            or not _set_job_kill_on_close(job_handle, True)
            or not kernel32.AssignProcessToJobObject(job_handle, process_handle)
        ):
            if job_handle:
                kernel32.CloseHandle(job_handle)
            return None
        return job_handle
    except Exception:
        if job_handle:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(job_handle)
            except Exception:
                pass
        return None


def _resume_suspended_process(pid: int) -> bool:
    """Resume the primary thread of a newly suspended Windows process."""
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        class _ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while has_entry:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if thread:
                        try:
                            return kernel32.ResumeThread(thread) != 0xFFFFFFFF
                        finally:
                            kernel32.CloseHandle(thread)
                entry.dwSize = ctypes.sizeof(entry)
                has_entry = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            return False
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return False


def _release_kill_on_close_job(job_handle, *, preserve_processes: bool) -> bool:
    """Close a build Job Object, disarming it after successful builds."""
    if sys.platform != "win32" or not job_handle:
        return False

    try:
        import ctypes
        from ctypes import wintypes

        disarmed = not preserve_processes or _set_job_kill_on_close(job_handle, False)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        closed = bool(kernel32.CloseHandle(job_handle))
        return disarmed and closed
    except Exception:
        return False


def _run_subprocess(
    cmd: list[str],
    log_file: str,
    cwd: str | None = None,
    heartbeat_seconds: float = 60.0,
    heartbeat_label: str = "build",
    on_start=None,
) -> dict:
    """Run a subprocess with stdout+stderr redirected to ``log_file``.

    The child's stdout and stderr are wired directly to an on-disk file, so
    output never transits Python memory and the pipe-buffer deadlock class
    (child blocks when ~64KB of unread output fills the OS pipe) is
    impossible by construction. This is the key property for UE build
    commands, whose combined output can reach tens of MB.

    While the child runs, a heartbeat line is written to stderr every
    ``heartbeat_seconds`` seconds so AI callers watching the subprocess
    can tell the build is still alive:

        [build] elapsed 2m00s  log 1.23 MB

    ``log`` is the current size of the build log file — it grows while
    UAT is producing output, so a stalled log is a strong hint that the
    build is genuinely stuck (vs. just slow). The log path itself is
    printed once up front by the caller, not repeated in every beat.
    Setting ``heartbeat_seconds <= 0`` disables heartbeats.

    Uses Popen so we can track the PID and kill the entire process tree on
    timeout (fixes the orphan MSBuild/UBT bug).

    Args:
        cmd: Command vector.
        log_file: Absolute path to the log file. Parent dir will be created.
            Overwritten if it already exists.
        cwd: Working directory for the child.
        heartbeat_seconds: Period between "still alive" stderr prints.
            Default 60s. Pass 0 or a negative value to disable.
        heartbeat_label: Tag used in the heartbeat line (e.g. "compile").

    Returns:
        ``{"returncode": int, "log_file": str, "duration_seconds": float}``.
        ``returncode == -1`` for launch failures (with an ``"error"`` key),
        ``-2`` for timeouts (with an ``"error"`` key naming the timeout).
    """
    is_windows = sys.platform == "win32"
    if is_windows:
        for item in cmd:
            value = str(item)
            if '"' in value or any(char in value for char in ("\0", "\r", "\n")):
                return {
                    "returncode": -1,
                    "log_file": str(log_file),
                    "duration_seconds": 0.0,
                    "error": "Unsafe Windows argv: literal quotes and NUL/CR/LF are not allowed.",
                }

    launch_cmd: list[str] | str = cmd
    startupinfo = None
    if is_windows:
        # Redirected MSVC diagnostics use the system ANSI code page. Give
        # detached workers a hidden console with the same encoding so UBT
        # decodes native tool output without losing localized text.
        encoded_items = [
            base64.b64encode(str(item).encode("utf-8")).decode("ascii")
            for item in cmd
        ]
        item_expressions = "\n".join(
            "[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{item}'))"
            for item in encoded_items
        )
        safe_assignments = "\r\n".join(
            f'set "UE_CLI_SAFE_{index}=!UE_CLI_ARG_{index}!"'
            for index in range(len(cmd))
        )
        argument_refs = " ".join(
            f'"%UE_CLI_SAFE_{index}%"' for index in range(len(cmd))
        )
        wrapper_content = (
            "@echo off\r\n"
            "setlocal EnableDelayedExpansion\r\n"
            f"{safe_assignments}\r\n"
            "setlocal DisableDelayedExpansion\r\n"
            "set \"VSLANG=\"\r\n"
            "set \"DOTNET_CLI_UI_LANGUAGE=\"\r\n"
            "set \"DOTNET_CLI_FORCE_UTF8_ENCODING=\"\r\n"
            "chcp %UE_CLI_NATIVE_CP% >nul\r\n"
            f"{argument_refs}\r\n"
        )
        encoded_wrapper = base64.b64encode(wrapper_content.encode("ascii")).decode("ascii")
        script = (
            "$ProgressPreference = 'SilentlyContinue'\n"
            "$ErrorActionPreference = 'Stop'\n"
            "$items = @(\n"
            f"{item_expressions}\n"
            ")\n"
            "if ($items.Count -lt 1) { exit 87 }\n"
            "$nativeEncoding = [Text.Encoding]::Default\n"
            "[Console]::OutputEncoding = $nativeEncoding\n"
            "[Environment]::SetEnvironmentVariable(\n"
            "  'UE_CLI_NATIVE_CP', [string]$nativeEncoding.CodePage, 'Process')\n"
            "for ($index = 0; $index -lt $items.Count; $index++) {\n"
            "  [Environment]::SetEnvironmentVariable(\n"
            "    ('UE_CLI_ARG_' + $index), [string]$items[$index], 'Process')\n"
            "}\n"
            "$wrapper = [IO.Path]::Combine("
            "[IO.Path]::GetTempPath(), ('ue_cli_build_' + $PID + '.cmd'))\n"
            "$content = [Text.Encoding]::ASCII.GetString("
            f"[Convert]::FromBase64String('{encoded_wrapper}'))\n"
            "[IO.File]::WriteAllText($wrapper, $content, [Text.Encoding]::ASCII)\n"
            "$exitCode = 1\n"
            "try {\n"
            "  & $wrapper\n"
            "  $exitCode = $LASTEXITCODE\n"
            "} finally {\n"
            "  if ([IO.File]::Exists($wrapper)) { [IO.File]::Delete($wrapper) }\n"
            "}\n"
            "exit $exitCode\n"
        )
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        launch_cmd = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_script,
        ]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    # Ensure the log path is writable before spawning anything.
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    proc = None
    job_handle = None

    try:
        log_fh = open(log_path, "wb")
    except OSError as e:
        return {
            "returncode": -1,
            "log_file": str(log_path),
            "duration_seconds": 0.0,
            "error": f"Failed to open log file: {e}",
        }

    try:
        try:
            creationflags = 0
            if is_windows:
                # Suspend before first instruction so no child can escape the
                # Job Object between CreateProcess and assignment.
                creationflags = subprocess.CREATE_NEW_CONSOLE | 0x00000004
            proc = subprocess.Popen(
                launch_cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                shell=False,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            if is_windows:
                job_handle = _attach_kill_on_close_job(proc)
                if job_handle is None:
                    proc.kill()
                    proc.wait()
                    return {
                        "returncode": -1,
                        "log_file": str(log_path),
                        "duration_seconds": round(time.monotonic() - started, 2),
                        "error": "Failed to attach build process to Windows Job Object",
                    }
                if not _resume_suspended_process(proc.pid):
                    released = _release_kill_on_close_job(
                        job_handle,
                        preserve_processes=False,
                    )
                    job_handle = None
                    if not released:
                        proc.kill()
                    proc.wait()
                    return {
                        "returncode": -1,
                        "log_file": str(log_path),
                        "duration_seconds": round(time.monotonic() - started, 2),
                        "error": "Failed to resume build process after Windows Job Object assignment",
                    }
            if on_start is not None:
                on_start(proc)
        except FileNotFoundError as e:
            return {
                "returncode": -1,
                "log_file": str(log_path),
                "duration_seconds": round(time.monotonic() - started, 2),
                "error": f"Command not found: {e}",
            }
        except Exception as e:
            return {
                "returncode": -1,
                "log_file": str(log_path),
                "duration_seconds": round(time.monotonic() - started, 2),
                "error": str(e),
            }

        # Poll-wait with heartbeats. We can't use proc.wait(timeout=...) in
        # a tight loop without paying context-switch cost, but polling once
        # per heartbeat_seconds is essentially free.
        do_heartbeat = heartbeat_seconds and heartbeat_seconds > 0
        poll_interval = (
            min(heartbeat_seconds, 5.0) if do_heartbeat else 5.0
        )
        next_beat = started + heartbeat_seconds if do_heartbeat else float("inf")
        deadline = started + _SAFETY_TIMEOUT

        while True:
            try:
                proc.wait(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if now >= deadline:
                    # Safety timeout tripped — kill the whole tree.
                    _kill_process_tree(proc.pid)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return {
                        "returncode": -2,
                        "log_file": str(log_path),
                        "duration_seconds": round(now - started, 2),
                        "error": f"Command timed out after {_SAFETY_TIMEOUT}s",
                    }
                if do_heartbeat and now >= next_beat:
                    _emit_heartbeat(
                        heartbeat_label, now - started, log_path,
                    )
                    # Schedule the next beat relative to the last one, so
                    # drift doesn't accumulate if a poll is slow.
                    next_beat += heartbeat_seconds

        if proc.returncode == 0 and job_handle is not None:
            released = _release_kill_on_close_job(
                job_handle,
                preserve_processes=True,
            )
            job_handle = None
            if not released:
                return {
                    "returncode": -1,
                    "log_file": str(log_path),
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "error": "Failed to release build Job Object after successful command",
                }

        return {
            "returncode": proc.returncode,
            "log_file": str(log_path),
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        if job_handle is not None:
            _release_kill_on_close_job(
                job_handle,
                preserve_processes=False,
            )
        try:
            log_fh.close()
        except Exception:
            pass


def _format_elapsed(seconds: float) -> str:
    """Format a duration as ``1h02m`` / ``12m34s`` / ``45s``."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _format_size(nbytes: int) -> str:
    """Format a byte count as KB/MB/GB."""
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:.2f} GB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / 1024 ** 2:.2f} MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f} KB"
    return f"{nbytes} B"


def _emit_heartbeat(label: str, elapsed: float, log_path: Path) -> None:
    """Write ``[label] elapsed <t>  log <size>`` to stderr.

    Semantics:
      - elapsed — wall-clock time since the child was spawned. Lets the
        AI see "still running" without inferring it from stream silence.
      - log — current size of the build log file. Grows while UAT is
        producing output; a stalled value is a strong signal that the
        build is genuinely stuck. Naming it ``log`` (not ``size``) makes
        this unambiguous on a single line of output.

    The log *path* is deliberately omitted: callers are expected to
    print it once before the build starts, and repeating it every
    heartbeat wastes AI tokens without adding signal.

    Swallows all exceptions — a heartbeat failure must never disturb the
    actual build.
    """
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
        sys.stderr.write(
            f"[{label}] elapsed {_format_elapsed(elapsed)}  "
            f"log {_format_size(size)}\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def get_engine_version(engine_root: str) -> Optional[str]:
    """Read engine version from Build.version."""
    version_file = Path(engine_root) / "Engine" / "Build" / "Build.version"
    if version_file.exists():
        try:
            data = json.loads(version_file.read_text(encoding="utf-8"))
            major = data.get("MajorVersion", "?")
            minor = data.get("MinorVersion", "?")
            patch = data.get("PatchVersion", "?")
            return f"{major}.{minor}.{patch}"
        except (json.JSONDecodeError, OSError):
            pass
    return None


# ── Remote Control config ────────────────────────────────────────────────

_REMOTE_CONTROL_INI_SECTION = "/Script/RemoteControlCommon.RemoteControlSettings"
_UE4_WEB_REMOTE_CONTROL_INI_SECTION = "/Script/WebRemoteControl.WebRemoteControlSettings"
_REMOTE_CONTROL_REQUIRED_SETTINGS = {
    "bRestrictServerAccess": "True",
    "bAllowConsoleCommandRemoteExecution": "True",
    "bEnableRemotePythonExecution": "True",
    'AllowedOrigin': '"*"',
}
_EDITOR_AUTOMATION_PLUGINS = (
    "RemoteControl",
    "PythonScriptPlugin",
    "EditorScriptingUtilities",
)


def _is_plugin_enabled_in_uproject(project_dir: str, plugin_name: str) -> bool:
    """Check if a plugin is enabled in .uproject (read-only).

    Returns True if the plugin entry exists with Enabled=True.
    """
    project_file = Path(project_dir)
    if project_file.is_file() and project_file.suffix == ".uproject":
        uproject_path = project_file
    else:
        uproject_files = list(Path(project_dir).glob("*.uproject"))
        if not uproject_files:
            return False
        uproject_path = uproject_files[0]

    try:
        data = json.loads(uproject_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    for p in data.get("Plugins", []):
        if p.get("Name") == plugin_name and p.get("Enabled") is True:
            return True
    return False


def _ensure_plugin_enabled(project_dir: str, plugin_name: str) -> bool:
    """Ensure a plugin is enabled in the .uproject file.

    Returns:
        True if the file was modified, False otherwise.
    """
    project_file = Path(project_dir)
    if project_file.is_file() and project_file.suffix == ".uproject":
        uproject_path = project_file
    else:
        uproject_files = list(Path(project_dir).glob("*.uproject"))
        if not uproject_files:
            return False
        uproject_path = uproject_files[0]

    try:
        data = json.loads(uproject_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False

    plugins = data.get("Plugins", [])
    for p in plugins:
        if p.get("Name") == plugin_name:
            if p.get("Enabled") is True:
                return False
            p["Enabled"] = True
            break
    else:
        plugins.append({"Name": plugin_name, "Enabled": True})

    data["Plugins"] = plugins
    uproject_path.write_text(json.dumps(data, indent="\t") + "\n", encoding="utf-8")
    return True


def _find_plugin_descriptor(
    project_dir: str,
    plugin_name: str,
    engine_root: str | None = None,
) -> Path | None:
    """Find a project or engine plugin descriptor without changing files."""
    candidates = [
        Path(project_dir) / "Plugins" / plugin_name / f"{plugin_name}.uplugin",
    ]
    if engine_root:
        engine_plugins = Path(engine_root) / "Engine" / "Plugins"
        candidates.extend([
            engine_plugins / "VirtualProduction" / plugin_name / f"{plugin_name}.uplugin",
            engine_plugins / "Experimental" / plugin_name / f"{plugin_name}.uplugin",
            engine_plugins / "Runtime" / plugin_name / f"{plugin_name}.uplugin",
            engine_plugins / "Editor" / plugin_name / f"{plugin_name}.uplugin",
            engine_plugins / plugin_name / f"{plugin_name}.uplugin",
        ])

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    for root in [Path(project_dir) / "Plugins"]:
        if root.is_dir():
            found = next(root.rglob(f"{plugin_name}.uplugin"), None)
            if found:
                return found
    return None


def _source_plugin_recovery(
    loadability: dict,
    *,
    uproject_path: str | None,
    engine_root: str | None,
    editor_binary_prefix: str,
) -> dict | None:
    """Return a copy-pasteable UBT recovery for an uncompiled source plugin."""
    if (
        loadability.get("reason") != "source_only_modules_uncompiled"
        or not uproject_path
        or not engine_root
        or not loadability.get("descriptor")
    ):
        return None

    build_script = Path(engine_root) / "Engine" / "Build" / "BatchFiles" / "Build.bat"
    if not build_script.is_file():
        return None

    descriptor = loadability["descriptor"]
    build_command = (
        f'& "{build_script}" {editor_binary_prefix} Win64 Development '
        f'-Project="{uproject_path}" -Plugin="{descriptor}" '
        "-NoUBTMakefiles -NoHotReload -WaitMutex"
    )
    return {
        "kind": "compile_source_plugin",
        "shell": "powershell",
        "build_command": build_command,
        "setup_command": f'ue-cli --project "{uproject_path}" editor enable-remote',
        "retry_command": f'ue-cli --project "{uproject_path}" editor launch',
    }


def _plugin_unavailable_suggestion(plugin_name: str, loadability: dict) -> str:
    recovery = loadability.get("recovery")
    if recovery:
        return (
            f"Close editors using this project. Run in {recovery['shell']}: "
            f"{recovery['build_command']} Then run: {recovery['setup_command']} "
            "Finally retry the original editor launch command."
        )
    return (
        f"Install or compile the engine {plugin_name} plugin first. "
        "ue-cli editor automation requires RemoteControl, "
        "PythonScriptPlugin, and EditorScriptingUtilities."
    )


def _check_plugin_loadable(
    project_dir: str,
    plugin_name: str,
    engine_root: str | None = None,
    editor_binary_prefix: str = "UnrealEditor",
    uproject_path: str | None = None,
) -> dict:
    """Check if enabling a plugin is likely to load or compile cleanly."""
    descriptor = _find_plugin_descriptor(project_dir, plugin_name, engine_root)
    if descriptor is None:
        return {
            "available": False,
            "plugin": plugin_name,
            "reason": "descriptor_missing",
            "message": f"{plugin_name} plugin descriptor was not found in project or engine plugins.",
        }

    try:
        data = json.loads(descriptor.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "plugin": plugin_name,
            "descriptor": str(descriptor),
            "reason": "descriptor_unreadable",
            "message": f"{plugin_name} plugin descriptor could not be read: {exc}",
        }

    plugin_dir = descriptor.parent
    modules = [
        m.get("Name")
        for m in data.get("Modules", [])
        if m.get("Name")
        and m.get("Type", "Runtime")
        in {"Runtime", "RuntimeNoCommandlet", "Editor", "Developer", "UncookedOnly"}
    ]
    if not modules:
        return {
            "available": True,
            "plugin": plugin_name,
            "descriptor": str(descriptor),
            "reason": "content_only_or_no_modules",
            "modules": [],
        }

    missing = []
    source_only = []
    for module_name in modules:
        module_binary = plugin_dir / "Binaries" / "Win64" / f"{editor_binary_prefix}-{module_name}.dll"
        module_source = plugin_dir / "Source" / module_name / f"{module_name}.Build.cs"
        if module_binary.is_file():
            continue
        if module_source.is_file():
            source_only.append({
                "module": module_name,
                "binary": str(module_binary),
                "source": str(module_source),
            })
        else:
            missing.append({
                "module": module_name,
                "binary": str(module_binary),
                "source": str(module_source),
            })

    if source_only:
        result = {
            "available": False,
            "plugin": plugin_name,
            "descriptor": str(descriptor),
            "reason": "source_only_modules_uncompiled",
            "modules": modules,
            "source_only_modules": source_only,
            "message": (
                f"{plugin_name} plugin has source but no {editor_binary_prefix} module binaries. "
                "Automatic setup will not enable it because the editor cannot load uncompiled plugin modules."
            ),
        }
        recovery = _source_plugin_recovery(
            result,
            uproject_path=uproject_path,
            engine_root=engine_root,
            editor_binary_prefix=editor_binary_prefix,
        )
        if recovery:
            result["recovery"] = recovery
        return result

    if missing:
        return {
            "available": False,
            "plugin": plugin_name,
            "descriptor": str(descriptor),
            "reason": "module_missing",
            "modules": modules,
            "missing_modules": missing,
            "message": f"{plugin_name} plugin modules are missing for {editor_binary_prefix}.",
        }

    return {
        "available": True,
        "plugin": plugin_name,
        "descriptor": str(descriptor),
        "reason": "module_binary_or_source_found",
        "modules": modules,
    }


def _requires_remote_function_permission(project_dir: str, engine_root: str | None) -> bool:
    """Detect the function-call gate by capability, including source-engine forks."""
    descriptor = _find_plugin_descriptor(project_dir, "RemoteControl", engine_root)
    if descriptor is None:
        return False
    header = descriptor.parent / "Source/RemoteControlCommon/Public/RemoteControlSettings.h"
    try:
        return "bAllowAnyRemoteFunctionCall" in header.read_text(encoding="utf-8-sig")
    except OSError:
        return False


def _remote_function_permission_enabled(project_dir: str) -> bool:
    from cli_anything.unreal.core.project import get_config

    settings = get_config(project_dir, "DefaultRemoteControl.ini").get(
        _REMOTE_CONTROL_INI_SECTION, {}
    )
    return str(settings.get("bAllowAnyRemoteFunctionCall", "")).lower() in ("true", "1")


def ensure_remote_control_config(
    project_dir: str,
    engine_root: str | None = None,
    editor_binary_prefix: str | None = None,
    uproject_path: str | None = None,
) -> dict:
    """Ensure the project has Remote Control configured for CLI use.

    Creates or updates DefaultRemoteControl.ini to enable:
    - Remote console command execution
    - Remote Python execution
    - Allow all origins

    Also enables the RemoteControl, PythonScriptPlugin, and
    EditorScriptingUtilities plugins in the .uproject file, but only after
    verifying that all three plugins have loadable editor module binaries.

    Args:
        project_dir: Path to project root directory.
        uproject_path: Exact project file used in source-plugin recovery commands.

    Returns:
        {"status": "ok"|"created"|"updated", "file": str, "changes": [...]}
    """
    if editor_binary_prefix is None:
        editor_binary_prefix = get_editor_binary_prefix(engine_root)
    config_dir = Path(project_dir) / "Config"
    config_file = config_dir / "DefaultRemoteControl.ini"
    changes = []

    if engine_root:
        for plugin_name in _EDITOR_AUTOMATION_PLUGINS:
            loadable = _check_plugin_loadable(
                project_dir,
                plugin_name,
                engine_root=engine_root,
                editor_binary_prefix=editor_binary_prefix,
                uproject_path=uproject_path,
            )
            if not loadable.get("available", False):
                return {
                    "status": "unavailable",
                    "file": str(config_file),
                    "changes": [],
                    "error": (
                        f"{plugin_name} plugin is not available/loadable for this engine; "
                        "no project files were modified."
                    ),
                    "details": loadable,
                    "suggestion": _plugin_unavailable_suggestion(plugin_name, loadable),
                }

    if not config_dir.is_dir():
        config_dir.mkdir(parents=True, exist_ok=True)

    for plugin_name in _EDITOR_AUTOMATION_PLUGINS:
        if _ensure_plugin_enabled(project_dir, plugin_name):
            changes.append(f"Enabled {plugin_name} plugin in .uproject")

    requires_function_permission = _requires_remote_function_permission(project_dir, engine_root)

    if not config_file.exists():
        # Create new config
        lines = [f"\n[{_REMOTE_CONTROL_INI_SECTION}]"]
        for key, value in _REMOTE_CONTROL_REQUIRED_SETTINGS.items():
            lines.append(f"{key}={value}")
        if requires_function_permission:
            lines.append("bAllowAnyRemoteFunctionCall=True")
        lines.append("")
        config_file.write_text("\n".join(lines), encoding="utf-8")
        changes.append("Created DefaultRemoteControl.ini with all settings")
        return {"status": "created", "file": str(config_file), "changes": changes}

    # Read existing config and check/update settings
    content = config_file.read_text(encoding="utf-8-sig")
    updated = False

    for key, required_value in _REMOTE_CONTROL_REQUIRED_SETTINGS.items():
        if key not in content:
            # Key missing — append before the last line of the section
            # Simple approach: append to file
            if _REMOTE_CONTROL_INI_SECTION not in content:
                content += f"\n[{_REMOTE_CONTROL_INI_SECTION}]\n"
            content += f"{key}={required_value}\n"
            changes.append(f"Added {key}={required_value}")
            updated = True
        elif f"{key}=False" in content or f"{key}=false" in content:
            content = content.replace(f"{key}=False", f"{key}={required_value}")
            content = content.replace(f"{key}=false", f"{key}={required_value}")
            changes.append(f"Changed {key} from False to {required_value}")
            updated = True

    if updated:
        config_file.write_text(content, encoding="utf-8")

    if requires_function_permission and not _remote_function_permission_enabled(project_dir):
        from cli_anything.unreal.core.project import set_config

        set_config(project_dir, "DefaultRemoteControl.ini", _REMOTE_CONTROL_INI_SECTION,
                   "bAllowAnyRemoteFunctionCall", "True")
        changes.append("Enabled bAllowAnyRemoteFunctionCall for CLI UObject function calls")

    if changes:
        return {"status": "updated", "file": str(config_file), "changes": changes}
    return {"status": "ok", "file": str(config_file), "changes": []}


def check_remote_control_config(
    project_dir: str,
    editor_binary_prefix: str | None = None,
    engine_root: str | None = None,
) -> dict:
    """Check if Remote Control is properly configured.

    Returns:
        {"configured": bool, "issues": [...], "file": str|None}
    """
    config_file = Path(project_dir) / "Config" / "DefaultRemoteControl.ini"
    issues = []

    if not _is_plugin_enabled_in_uproject(project_dir, "RemoteControl"):
        issues.append(
            "RemoteControl plugin is not enabled in .uproject. "
            "Remote Control HTTP server will not start. "
            "Run: ue-cli editor enable-remote"
        )

    if not _is_plugin_enabled_in_uproject(project_dir, "PythonScriptPlugin"):
        issues.append(
            "PythonScriptPlugin is not enabled in .uproject. "
            "Python script execution will fail. "
            "Run: ue-cli editor enable-remote"
        )

    if not _is_plugin_enabled_in_uproject(project_dir, "EditorScriptingUtilities"):
        issues.append(
            "EditorScriptingUtilities is not enabled in .uproject. "
            "Editor asset scripting will fail. "
            "Run: ue-cli editor enable-remote"
        )

    if not config_file.exists():
        return {
            "configured": False,
            "issues": [
                "DefaultRemoteControl.ini not found. "
                "Remote console commands and Python execution will be blocked. "
                "Run: ue-cli editor enable-remote"
            ] + issues,
            "file": None,
        }

    content = config_file.read_text(encoding="utf-8-sig")

    if "bAllowConsoleCommandRemoteExecution=True" not in content:
        issues.append(
            "bAllowConsoleCommandRemoteExecution is not True. "
            "Console commands (exec, cvar set) will fail."
        )

    if "bEnableRemotePythonExecution=True" not in content:
        issues.append(
            "bEnableRemotePythonExecution is not True. "
            "Python script execution will fail."
        )

    if (_requires_remote_function_permission(project_dir, engine_root)
            and not _remote_function_permission_enabled(project_dir)):
        issues.append(
            "This engine restricts Remote Control function calls. "
            "bAllowAnyRemoteFunctionCall is not True in RemoteControlSettings; "
            "CLI Python, bridge, and arbitrary UObject calls may be blocked. "
            "Run: ue-cli editor enable-remote, then restart the editor."
        )

    port = read_rc_port(project_dir, editor_binary_prefix)

    return {
        "configured": len(issues) == 0,
        "issues": issues,
        "file": str(config_file),
        "port": port,
    }


def _parse_rc_port(ini_content: str) -> int | None:
    """Parse RemoteControlHttpServerPort from an INI file content string.

    Returns:
        Port number (int) if found, None otherwise.
    """
    for line in ini_content.splitlines():
        line = line.strip()
        if line.startswith("RemoteControlHttpServerPort="):
            try:
                return int(line.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def _rc_port_config(project_dir: str, editor_binary_prefix: str | None = None) -> tuple[Path, str]:
    """Return the engine-specific config file and section for Remote Control ports."""
    config_dir = Path(project_dir) / "Config"
    if editor_binary_prefix == "UE4Editor":
        return (
            config_dir / "DefaultWebRemoteControl.ini",
            _UE4_WEB_REMOTE_CONTROL_INI_SECTION,
        )
    return config_dir / "DefaultRemoteControl.ini", _REMOTE_CONTROL_INI_SECTION


def read_rc_port(
    project_dir: str,
    editor_binary_prefix: str | None = None,
) -> int | None:
    """Read the Remote Control HTTP port from project config.

    Looks for ``RemoteControlHttpServerPort`` in the engine-specific config:
    ``DefaultWebRemoteControl.ini`` for UE4, otherwise
    ``DefaultRemoteControl.ini``.

    Args:
        project_dir: Path to the UE project root.
        editor_binary_prefix: ``UE4Editor`` selects UE4's WebRemoteControl config.

    Returns:
        Port number (int) if configured, None to use the default.
    """
    config_file, _section = _rc_port_config(project_dir, editor_binary_prefix)
    if not config_file.exists():
        return None
    try:
        content = config_file.read_text(encoding="utf-8-sig")
    except Exception:
        return None
    return _parse_rc_port(content)


def _write_rc_port(
    project_dir: str,
    port: int,
    editor_binary_prefix: str | None = None,
) -> str:
    """Persist the launch port in the config file read by this engine generation."""
    config_file, section = _rc_port_config(project_dir, editor_binary_prefix)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if not config_file.exists():
        config_file.write_text(
            f"\n[{section}]\n"
            f"RemoteControlHttpServerPort={port}\n",
            encoding="utf-8",
        )
        return str(config_file)

    content = config_file.read_text(encoding="utf-8-sig")
    key = "RemoteControlHttpServerPort="
    if f"[{section}]" in content and _parse_rc_port(content) == port:
        return str(config_file)
    if key in content:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(key):
                lines[i] = f"{key}{port}"
                break
        content = "\n".join(lines)
        if not content.endswith("\n"):
            content += "\n"
    else:
        if f"[{section}]" not in content:
            content = content.rstrip("\n") + f"\n\n[{section}]\n"
        content = content.rstrip("\n") + f"\n{key}{port}\n"
    config_file.write_text(content, encoding="utf-8")
    return str(config_file)


def is_tcp_port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Return True when a local TCP listener accepts connections."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def resolve_available_port(
    project_dir: str,
    desired_port: int,
    editor_binary_prefix: str | None = None,
) -> int:
    """If *desired_port* is already occupied by another editor, find and persist an available one.

    Scans upward from desired_port+1 (max 10 attempts). When a free port is
    found, writes it to the engine-specific Remote Control config so the editor
    picks it up on launch.

    Returns the port to use (may be the original if it's free).
    """
    if not is_tcp_port_in_use(desired_port):
        return desired_port

    # Find next free port
    for offset in range(1, 11):
        candidate = desired_port + offset
        if not is_tcp_port_in_use(candidate):
            _write_rc_port(
                project_dir,
                candidate,
                editor_binary_prefix=editor_binary_prefix,
            )
            return candidate

    # All 10 candidates occupied — fall back to original and let UE fail naturally
    return desired_port


# ── Build status checks ─────────────────────────────────────────────────

def check_engine_build(engine_root: str) -> dict:
    """Check if the engine has been compiled and is ready to run.

    Checks for:
    1. Editor executable exists (UnrealEditor* for UE5, UE4Editor* for UE4)
    2. Editor modules file exists (module mappings + BuildId)
    3. Editor target file exists (build config)
    4. Build.version is valid

    Args:
        engine_root: Path to engine root.

    Returns:
        {"ready": bool, "build_id": str, "errors": [...], "warnings": [...], "details": {...}}
    """
    root = Path(engine_root)
    bin_dir = root / "Engine" / "Binaries" / "Win64"
    errors = []
    warnings = []
    details = {"engine_root": engine_root}
    build_id = ""
    editor_binary_prefix = get_editor_binary_prefix(engine_root)
    details["editor_binary_prefix"] = editor_binary_prefix

    # Check 1: editor executable
    editor_exe = None
    for candidate in (bin_dir / f"{editor_binary_prefix}.exe", bin_dir / f"{editor_binary_prefix}-Cmd.exe"):
        if candidate.exists():
            editor_exe = candidate
            break
    if editor_exe is None:
        checked = ", ".join(p.name for p in _editor_binary_candidates(engine_root))
        errors.append(
            f"Editor executable not found at {bin_dir} (checked {checked}). "
            "Engine has not been compiled. Build the engine from source first."
        )
    else:
        size = editor_exe.stat().st_size
        details["editor_exe"] = str(editor_exe)
        details["editor_exe_size"] = size
        if size < 100_000:
            warnings.append(
                f"{editor_exe.name} is unusually small ({size} bytes). "
                "Engine build may be incomplete."
            )

    # Check 2: editor modules file (BuildId + module mappings)
    modules_file = bin_dir / f"{editor_binary_prefix}.modules"
    if not modules_file.exists():
        errors.append(
            f"{editor_binary_prefix}.modules not found. "
            "Engine modules have not been compiled."
        )
    else:
        try:
            modules_data = json.loads(modules_file.read_text(encoding="utf-8"))
            build_id = modules_data.get("BuildId", "")
            module_count = len(modules_data.get("Modules", {}))
            details["engine_build_id"] = build_id
            details["engine_module_count"] = module_count
            if module_count < 10:
                warnings.append(
                    f"Only {module_count} engine modules found. "
                    "Build may be incomplete."
                )
        except (json.JSONDecodeError, OSError):
            warnings.append(f"Could not parse {editor_binary_prefix}.modules")

    # Check 3: editor target file (build metadata)
    target_file = bin_dir / f"{editor_binary_prefix}.target"
    if not target_file.exists():
        warnings.append(
            f"{editor_binary_prefix}.target not found. "
            "Engine may not have a complete build."
        )

    # Check 4: Build.version
    version = get_engine_version(engine_root)
    if version:
        details["engine_version"] = version
    else:
        warnings.append("Could not read engine version from Build.version")

    details["ready"] = len(errors) == 0
    return {
        "ready": len(errors) == 0,
        "build_id": build_id,
        "errors": errors,
        "warnings": warnings,
        "details": details,
    }


def check_project_build(
    uproject_path: str,
    engine_build_id: str = "",
    editor_binary_prefix: str = "UnrealEditor",
) -> dict:
    """Check if a project's C++ code has been compiled and matches the engine.

    For Blueprint-only projects (no Source/ dir), compilation is not needed
    BUT the project's UnrealEditor.modules BuildId must match the engine's.

    Checks for:
    1. Whether the project has C++ code (Source/ directory)
    2. UnrealEditor-{ProjectName}.dll exists in Binaries/Win64/
    3. BuildId in project's UnrealEditor.modules matches engine BuildId
    4. DLL is not stale (newer than Source/ files)

    Args:
        uproject_path: Path to .uproject file.
        engine_build_id: Engine's BuildId for version matching.

    Returns:
        {"ready": bool, "needs_compile": bool, "errors": [...], "warnings": [...], "details": {...}}
    """
    path = Path(uproject_path)
    project_dir = path.parent
    project_name = path.stem
    errors = []
    warnings = []
    details = {
        "project": project_name,
        "project_path": str(path),
        "editor_binary_prefix": editor_binary_prefix,
    }

    # ── Check BuildId match (critical for custom engine builds) ─────
    project_modules_file = project_dir / "Binaries" / "Win64" / f"{editor_binary_prefix}.modules"
    project_build_id = ""

    if project_modules_file.exists():
        try:
            mod_data = json.loads(project_modules_file.read_text(encoding="utf-8"))
            project_build_id = mod_data.get("BuildId", "")
            details["project_build_id"] = project_build_id
            details["project_module_names"] = list(mod_data.get("Modules", {}).keys())
        except (json.JSONDecodeError, OSError):
            pass

    if engine_build_id and project_build_id:
        if engine_build_id != project_build_id:
            errors.append(
                f"BuildId MISMATCH: engine='{engine_build_id[:8]}...' vs project='{project_build_id[:8]}...'. "
                f"Project was compiled with a different engine version. "
                f"Launching will fail with 'modules built with a different engine version'. "
                f"Recompile: ue-cli --project {uproject_path} build compile"
            )
            details["build_id_match"] = False
        else:
            details["build_id_match"] = True
    elif engine_build_id and not project_build_id:
        if (project_dir / "Binaries" / "Win64").is_dir():
            warnings.append(
                f"Could not read project BuildId from {editor_binary_prefix}.modules. "
                "Cannot verify engine/project version match."
            )

    # ── Check if project has C++ code ───────────────────────────────
    source_dir = project_dir / "Source"
    has_source = source_dir.is_dir()
    details["has_cpp_source"] = has_source

    if not has_source:
        # Blueprint-only project — no C++ compilation needed
        # But BuildId still must match if binaries exist
        ready = len(errors) == 0
        return {
            "ready": ready,
            "needs_compile": not ready,
            "errors": errors,
            "warnings": warnings,
            "details": {**details, "note": "Blueprint-only project, no C++ compilation needed"},
        }

    # Count source files
    cpp_files = list(source_dir.rglob("*.cpp"))
    h_files = list(source_dir.rglob("*.h"))
    details["cpp_files"] = len(cpp_files)
    details["header_files"] = len(h_files)

    # Check for compiled DLL
    bin_dir = project_dir / "Binaries" / "Win64"
    if not bin_dir.is_dir():
        errors.append(
            f"Binaries/Win64/ directory not found. "
            f"Project '{project_name}' has never been compiled. "
            f"Run: ue-cli build compile --project {uproject_path}"
        )
        return {
            "ready": False,
            "needs_compile": True,
            "errors": errors,
            "warnings": warnings,
            "details": details,
        }

    # Find project DLLs — they follow the pattern UnrealEditor-{ModuleName}.dll
    # Read module names from .uproject
    try:
        uproject_data = json.loads(path.read_text(encoding="utf-8-sig"))
        modules = [m["Name"] for m in uproject_data.get("Modules", [])]
    except Exception:
        modules = [project_name]

    details["expected_modules"] = modules
    missing_modules = []
    stale_modules = []

    # Find newest source file timestamp
    newest_source_time = 0
    for src in cpp_files + h_files:
        mtime = src.stat().st_mtime
        if mtime > newest_source_time:
            newest_source_time = mtime

    for module_name in modules:
        dll_path = bin_dir / f"{editor_binary_prefix}-{module_name}.dll"
        if not dll_path.exists():
            missing_modules.append(module_name)
        else:
            dll_time = dll_path.stat().st_mtime
            details[f"dll_{module_name}_size"] = dll_path.stat().st_size
            # Check if DLL is older than source
            if newest_source_time > dll_time:
                stale_modules.append(module_name)

    if missing_modules:
        errors.append(
            f"Compiled modules not found: {', '.join(missing_modules)}. "
            f"Project C++ code has not been compiled. "
            f"Run: ue-cli build compile --project {uproject_path}"
        )

    if stale_modules:
        warnings.append(
            f"Modules may be stale (source newer than binary): {', '.join(stale_modules)}. "
            f"Consider recompiling."
        )

    # Check for .target file
    target_file = bin_dir / f"{project_name}Editor.target"
    if not target_file.exists():
        # Try alternative naming
        targets = list(bin_dir.glob("*.target"))
        if not targets:
            warnings.append(
                f"{project_name}Editor.target not found. "
                "Build metadata may be missing."
            )
        else:
            details["target_files"] = [t.name for t in targets]

    needs_compile = len(missing_modules) > 0 or len(errors) > 0
    return {
        "ready": len(errors) == 0 and not needs_compile,
        "needs_compile": needs_compile,
        "errors": errors,
        "warnings": warnings,
        "details": details,
    }


def preflight_check(uproject_path: str, engine_root: str | None = None) -> dict:
    """Read-only preflight check before launching editor.

    Checks engine, project, Remote Control, and bridge readiness without
    modifying project files. Commands with explicit mutation semantics, such
    as ``editor launch`` and ``editor enable-remote``, prepare prerequisites.

    Args:
        uproject_path: Path to .uproject file.
        engine_root: Engine root (auto-detected if None).

    Returns:
        {"ready": bool, "engine": {...}, "project": {...}}
    """
    if engine_root is None:
        engine_root = find_engine_root(uproject_path)

    result = {"ready": False, "read_only": True}

    if not engine_root:
        result["engine"] = {
            "ready": False,
            "errors": ["Could not find engine root. Set UE_ENGINE_ROOT or use --engine-root."],
            "warnings": [],
            "details": {},
        }
        result["project"] = {"ready": False, "errors": [], "warnings": [], "details": {}}
        return result

    engine_check = check_engine_build(engine_root)
    editor_binary_prefix = engine_check.get("details", {}).get(
        "editor_binary_prefix",
        get_editor_binary_prefix(engine_root),
    )
    project_check = check_project_build(
        uproject_path,
        engine_build_id=engine_check.get("build_id", ""),
        editor_binary_prefix=editor_binary_prefix,
    )

    # Check Remote Control config
    project_dir = str(Path(uproject_path).parent)
    rc_check = check_remote_control_config(
        project_dir,
        editor_binary_prefix=editor_binary_prefix,
        engine_root=engine_root,
    )
    plugin_checks = {
        plugin_name: _check_plugin_loadable(
            project_dir,
            plugin_name,
            engine_root=engine_root,
            editor_binary_prefix=editor_binary_prefix,
            uproject_path=uproject_path,
        )
        for plugin_name in _EDITOR_AUTOMATION_PLUGINS
    }
    plugin_loadable = {
        "available": all(check.get("available", False) for check in plugin_checks.values()),
        "plugins": plugin_checks,
    }
    unavailable_plugin = next(
        (
            (plugin_name, check)
            for plugin_name, check in plugin_checks.items()
            if not check.get("available", False)
        ),
        None,
    )
    rc_check["plugin_loadable"] = plugin_loadable
    rc_check["auto_fixed"] = False
    if unavailable_plugin is not None:
        plugin_name, unavailable_check = unavailable_plugin
        rc_check["configured"] = False
        rc_check["issues"] = [
            f"{plugin_name} plugin is not available/loadable for this engine; "
            "preflight did not modify project files."
        ]
        rc_check["auto_fixed"] = False
        rc_check["fix_result"] = {
            "status": "unavailable",
            "file": str(Path(project_dir) / "Config" / "DefaultRemoteControl.ini"),
            "changes": [],
            "error": (
                f"{plugin_name} plugin is not available/loadable for this engine; "
                "preflight did not modify .uproject or DefaultRemoteControl.ini."
            ),
            "details": unavailable_check,
            "suggestion": _plugin_unavailable_suggestion(plugin_name, unavailable_check),
        }
    elif not rc_check["configured"]:
        rc_check["suggestion"] = (
            "Run ue-cli editor enable-remote to prepare the project explicitly, "
            "or use editor launch, which prepares editor automation as part of startup."
        )

    # Check bridge plugin readiness (informational — auto-fixed during launch)
    bridge_issues = []
    from cli_anything.unreal.core.plugin_bridge import get_bundled_version, get_plugin_binary_status

    bridge_descriptor = Path(project_dir) / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
    bundled_version = get_bundled_version()
    deployed_version = None
    if bridge_descriptor.is_file():
        try:
            deployed_version = json.loads(
                bridge_descriptor.read_text(encoding="utf-8-sig")
            ).get("VersionName")
        except (OSError, json.JSONDecodeError):
            bridge_issues.append("CliAnythingBridge descriptor could not be read")
    else:
        bridge_issues.append("CliAnythingBridge plugin source is not deployed")

    if bridge_descriptor.is_file() and deployed_version != bundled_version:
        bridge_issues.append(
            f"CliAnythingBridge plugin version {deployed_version or 'unknown'} "
            f"does not match bundled version {bundled_version or 'unknown'}"
        )
    bridge_enabled = _is_plugin_enabled_in_uproject(project_dir, "CliAnythingBridge")
    if not bridge_enabled:
        bridge_issues.append("CliAnythingBridge plugin not enabled in .uproject")
    bridge_binary_status = get_plugin_binary_status(project_dir, engine_root=engine_root)
    if bridge_descriptor.is_file() and not bridge_binary_status.get("ready", False):
        bridge_issues.append(
            bridge_binary_status.get("message", "CliAnythingBridge binary is not ready")
        )
    result["bridge_plugin"] = {
        "ready": len(bridge_issues) == 0,
        "issues": bridge_issues,
        "auto_fixable": True,
        "bundled_version": bundled_version,
        "deployed_version": deployed_version,
        "binary_status": bridge_binary_status,
    }

    result["engine"] = engine_check
    result["project"] = project_check
    result["remote_control"] = rc_check
    remote_ready = rc_check["configured"]
    result["ready"] = engine_check["ready"] and project_check["ready"] and remote_ready
    result["engine_root"] = engine_root

    return result


def find_running_editors(timeout: float | None = None) -> list[dict]:
    """Find running UnrealEditor processes and their project paths.

    Uses PowerShell (preferred) with WMIC fallback on Windows.

    Returns a list of dicts: [{"pid": int, "project": str, "cmdline": str}, ...]
    """
    if sys.platform != "win32":
        return []

    editors = []
    deadline = time.monotonic() + timeout if timeout is not None else None

    def bounded_timeout(limit: float) -> float | None:
        if deadline is None:
            return limit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return max(0.01, min(limit, remaining))

    # ── Method 1: PowerShell (reliable on modern Windows) ──────────
    try:
        ps_cmd = (
            'Get-CimInstance Win32_Process -Filter "Name like \'%UnrealEditor%\' OR Name like \'%UE4Editor%\'" '
            '| Select-Object ProcessId, CommandLine '
            '| ConvertTo-Json -Compress'
        )
        process_timeout = bounded_timeout(15)
        if process_timeout is None:
            return editors
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=process_timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            # PowerShell returns a single object if 1 result, array if multiple
            if isinstance(data, dict):
                data = [data]
            for proc in data:
                cmdline = proc.get("CommandLine", "")
                pid = proc.get("ProcessId", 0)
                project = _extract_uproject_from_cmdline(cmdline)
                editors.append({
                    "pid": int(pid),
                    "project": project,
                    "cmdline": cmdline,
                })
            return editors
    except Exception:
        pass

    # ── Method 2: WMIC fallback ────────────────────────────────────
    try:
        process_timeout = bounded_timeout(10)
        if process_timeout is None:
            return editors
        result = subprocess.run(
            ["wmic", "process", "where",
             "(name like '%UnrealEditor%' or name like '%UE4Editor%')",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=process_timeout,
            shell=True,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line or "ProcessId" in line or "Node" in line:
                    continue
                parts = line.split(",")
                if len(parts) >= 3:
                    cmdline = ",".join(parts[1:-1])
                    pid = parts[-1].strip()
                    project = _extract_uproject_from_cmdline(cmdline)
                    editors.append({
                        "pid": int(pid) if pid.isdigit() else 0,
                        "project": project,
                        "cmdline": cmdline,
                    })
    except Exception:
        pass

    return editors


def detect_ue_dialogs(process_id: int | None = None) -> list[dict]:
    """Detect modal dialogs blocking a running Unreal Editor on Windows.

    Uses the Windows API (EnumWindows) to find top-level and child windows
    that look like modal dialogs (e.g., "Restore Packages", "Overwrite",
    "Save Changes", "Warning", "Fatal Error" popups). When ``process_id``
    is supplied, only windows owned by that process are inspected. This
    covers early-startup dialogs that appear before the main editor window.

    Returns:
        List of dicts: [{"title": str, "hwnd": int, "process_id": int}, ...].
        Empty list if no dialogs found or not on Windows.
    """
    if sys.platform != "win32":
        return []

    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32

    DIALOG_KEYWORDS = [
        "overwrite", "override", "save changes", "save asset",
        "warning", "error", "fatal", "assertion", "missing",
        "confirmation", "delete", "replace",
        # Recovery / autosave
        "autosave", "recover", "auto-save", "unsaved",
        "crash", "restore", "unexpected shutdown", "恢复包",
    ]

    target_pid = int(process_id) if process_id is not None else None
    results: list[dict] = []
    seen_hwnds: set[int] = set()

    def _get_title(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _get_process_id(hwnd) -> int:
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _add_if_dialog(hwnd, title: str, pid: int) -> None:
        if not title or int(hwnd) in seen_hwnds:
            return
        title_lower = title.lower()
        if not any(keyword in title_lower for keyword in DIALOG_KEYWORDS):
            return
        seen_hwnds.add(int(hwnd))
        results.append({"title": title, "hwnd": int(hwnd), "process_id": pid})

    ue_main_windows: list[tuple[int, int]] = []

    def _enum_main_windows(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_title(hwnd)
        pid = _get_process_id(hwnd)
        if target_pid is not None:
            if pid != target_pid:
                return True
            ue_main_windows.append((int(hwnd), pid))
            _add_if_dialog(hwnd, title, pid)
        elif "UnrealEditor" in title or "UE4Editor" in title:
            ue_main_windows.append((int(hwnd), pid))
            _add_if_dialog(hwnd, title, pid)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(WNDENUMPROC(_enum_main_windows), 0)

    def _enum_children(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_title(hwnd)
        _add_if_dialog(hwnd, title, _get_process_id(hwnd))
        return True

    for main_hwnd, _pid in ue_main_windows:
        user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_enum_children), 0)

    return results


# ── Build process detection & management ────────────────────────────────

_BUILD_PROCESS_NAMES = [
    "MSBuild.exe",
    "UnrealBuildTool.exe",
    "bk-ubt-tool.exe",
    "cl.exe",
    "link.exe",
]


class BuildProcessProbeError(RuntimeError):
    """Raised when a caller requires a conclusive build-process probe."""

    def __init__(self, message: str, *, details: dict):
        super().__init__(message)
        self.details = details


def find_running_build_processes(
    uproject_path: str | None = None,
    include_cmdline: bool = True,
    *,
    query_timeout: float = 15,
    fail_on_error: bool = False,
) -> list[dict]:
    """Find running build processes (MSBuild, UBT, cl.exe, etc.).

    If uproject_path is given, only returns processes whose command line
    references that .uproject file.

    Args:
        uproject_path: Filter by this project's .uproject path.
        include_cmdline: If True (default), each returned dict includes the
            full ``cmdline`` string from Win32_Process. If False, ``cmdline``
            is omitted entirely — useful for AI callers who only need
            {pid, name, project} and would otherwise pay a large token
            cost for every cl.exe's multi-KB compile command line.
        query_timeout: Maximum seconds to wait for the Windows process query.
        fail_on_error: Raise ``BuildProcessProbeError`` instead of treating a
            failed or timed-out query as an empty process list.

    Returns a list of dicts:
        include_cmdline=True:  [{"pid", "name", "cmdline", "project"}, ...]
        include_cmdline=False: [{"pid", "name", "project"}, ...]
    """
    if sys.platform != "win32":
        return []

    processes = []

    # Build PowerShell filter for process names
    name_filters = " or ".join(f"Name = '{n}'" for n in _BUILD_PROCESS_NAMES)

    try:
        ps_cmd = (
            f'Get-CimInstance Win32_Process -Filter "{name_filters}" '
            '| Select-Object ProcessId, Name, CommandLine '
            '| ConvertTo-Json -Compress'
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=query_timeout,
        )
        if result.returncode != 0:
            if fail_on_error:
                stderr = (result.stderr or "").strip()
                raise BuildProcessProbeError(
                    "Windows build-process query failed.",
                    details={
                        "reason": "query_failed",
                        "returncode": result.returncode,
                        "stderr": stderr[-2000:],
                    },
                )
            return processes
        if result.stdout.strip():
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]
            # Helper: identify idle MSBuild node-reuse daemons
            def _is_idle_msbuild(name, cmdline):
                return name == "MSBuild.exe" and (
                    "/nodeMode:1" in cmdline or
                    "/nr:true" in cmdline or
                    "/nodeReuse" in cmdline
                )

            for proc in data:
                pid = proc.get("ProcessId", 0)
                name = proc.get("Name", "")
                cmdline = proc.get("CommandLine", "") or ""

                # Skip idle MSBuild daemons immediately (Rider/VS keep these
                # alive for fast incremental builds — they are NOT active builds)
                if _is_idle_msbuild(name, cmdline):
                    continue

                # Extract .uproject path from command line
                project = ""
                for token in cmdline.split():
                    if ".uproject" in token:
                        # Handle -project=X.uproject and plain X.uproject
                        if "=" in token:
                            token = token.split("=", 1)[1]
                        token = token.strip('"').strip("'")
                        if token.endswith(".uproject"):
                            project = token
                            break

                # Filter by project if requested
                if uproject_path:
                    # If cmdline contains a .uproject, it must match
                    norm_project = project.replace("/", "\\").lower()
                    norm_uproject = uproject_path.replace("/", "\\").lower()
                    if project:
                        # This process has a .uproject in its cmdline
                        if norm_project != norm_uproject:
                            continue
                    else:
                        # No .uproject in cmdline (e.g., bk-ubt-tool, cl.exe).
                        # Include if any process in the result set DOES reference
                        # this project (they're likely part of the same build).
                        # We defer this check — collect all first, filter after.
                        pass

                processes.append({
                    "pid": int(pid),
                    "name": name,
                    "cmdline": cmdline,
                    "project": project,
                })
            # Post-filter: if uproject_path given, processes without
            # .uproject in their cmdline (e.g., bk-ubt-tool, cl.exe)
            # are only included if at least one process explicitly
            # references this project.
            if uproject_path:
                has_matching_project = any(
                    p["project"] and
                    p["project"].replace("/", "\\").lower() ==
                    uproject_path.replace("/", "\\").lower()
                    for p in processes
                )
                if has_matching_project:
                    # Keep all — project-specific + associated processes
                    pass
                else:
                    # No process explicitly references this project.
                    # Discard all ambiguous processes — they belong to other
                    # projects or are idle daemons we already filtered.
                    processes = []
            if not include_cmdline:
                processes = [
                    {k: v for k, v in p.items() if k != "cmdline"}
                    for p in processes
                ]
            return processes
    except subprocess.TimeoutExpired as exc:
        if fail_on_error:
            raise BuildProcessProbeError(
                f"Windows build-process query timed out after {query_timeout:g} seconds.",
                details={
                    "reason": "timeout",
                    "timeout_seconds": query_timeout,
                },
            ) from exc
    except BuildProcessProbeError:
        raise
    except Exception as exc:
        if fail_on_error:
            raise BuildProcessProbeError(
                "Windows build-process query returned an invalid result.",
                details={
                    "reason": "invalid_result",
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                },
            ) from exc

    return processes


def kill_build_processes(
    uproject_path: str | None = None,
    *,
    query_timeout: float = 15,
    fail_on_probe_error: bool = False,
) -> dict:
    """Kill all build processes for a project (or all build processes).

    Finds running build processes via find_running_build_processes(),
    kills each with _kill_process_tree(), waits, and re-checks.

    Args:
        uproject_path: If given, only kill processes for this project.
            If None, kill all build processes.
        query_timeout: Maximum seconds for each Windows process scan.
        fail_on_probe_error: Raise ``BuildProcessProbeError`` when a scan is
            inconclusive instead of treating it as an empty process list.

    Returns:
        {"killed": [pid, ...], "remaining": [pid, ...], "status": "ok"|"partial"|"none"}
    """
    processes = find_running_build_processes(
        uproject_path,
        query_timeout=query_timeout,
        fail_on_error=fail_on_probe_error,
    )

    if not processes:
        return {"killed": [], "remaining": [], "status": "none"}

    killed = []
    failed = []

    for proc in processes:
        if _kill_process_tree(proc["pid"]):
            killed.append(proc["pid"])
        else:
            failed.append(proc["pid"])

    # Wait for processes to actually terminate
    import time
    time.sleep(3)

    # Re-check: some processes may have spawned new children
    remaining_procs = find_running_build_processes(
        uproject_path,
        query_timeout=query_timeout,
        fail_on_error=fail_on_probe_error,
    )
    remaining_pids = [p["pid"] for p in remaining_procs if p["pid"] not in killed]

    # Second pass: kill any remaining
    if remaining_pids:
        time.sleep(5)
        for pid in remaining_pids:
            _kill_process_tree(pid)
        time.sleep(3)
        # Final check
        final_procs = find_running_build_processes(
            uproject_path,
            query_timeout=query_timeout,
            fail_on_error=fail_on_probe_error,
        )
        remaining_pids = [p["pid"] for p in final_procs]

    status = "ok"
    if remaining_pids:
        status = "partial"
    elif not killed:
        status = "none"

    return {
        "killed": killed,
        "remaining": remaining_pids,
        "status": status,
    }
