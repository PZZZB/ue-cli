"""Editor launch lifecycle helpers shared by commands and task workers."""

from __future__ import annotations

import re
import socket
import sys
import time
from pathlib import Path

from cli_anything.unreal.core.tasks import (
    FINAL_TASK_STATUSES,
    TaskDiscoveryTimeout,
    TaskLockTimeout,
    iter_task_snapshots,
    load_task,
)


_FATAL_LOG_PATTERNS = [
    "modules are missing or built with a different engine version",
    "Still incompatible or missing module:",
    "Engine modules cannot be compiled at runtime",
    "Missing or incompatible modules",
    "Plugin .* failed to load",
    "Fatal Error:",
    "Assertion failed:",
]

_RUNTIME_FATAL_LOG_PATTERN = re.compile(
    r"Assertion failed:|Fatal Error:|LowLevelFatalError|Unhandled Exception|"
    r"Crash in runnable thread|Signal 11 caught|StaticShutdownAfterError",
    re.IGNORECASE,
)

_WINDOWS_STATUS_ENTRYPOINT_NOT_FOUND = 0xC0000139
_MISSING_VIRTUAL_SHADER_SOURCE_PATTERN = re.compile(
    r"Couldn't find source file of virtual shader path\s+['\"](?P<shader_path>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_FILESYSTEM_DDC_MAINTAINER_THREAD_PATTERN = re.compile(
    r"Runnable thread FileSystemCacheStoreMaintainer crashed",
    re.IGNORECASE,
)
_FILESYSTEM_DDC_MAINTAINER_STACK_PATTERN = re.compile(
    r"FFileSystemCacheStoreMaintainer::(?:CreateContentRoot|Scan|Loop)",
    re.IGNORECASE,
)
_PLUGIN_LOAD_FAILURE_PATTERN = re.compile(
    r"\bPlugin\s+['\"](?P<plugin>[^'\"]+)['\"]\s+failed to load\b",
    re.IGNORECASE,
)
_PLUGIN_LOAD_MODULE_PATTERN = re.compile(
    r"\bmodule\s+['\"](?P<module>[^'\"]+)['\"]\s+could not be (?:found|loaded)\b",
    re.IGNORECASE,
)
_SKIP_RESTORE_PACKAGES_ARG = "-CliAnythingSkipRestorePackages"


def _is_active_launch_task_for_project(
    task: dict | None,
    project_path: str,
    pid: int | None,
    *,
    now: float,
) -> bool:
    if not task or task.get("command") != "editor.launch":
        return False
    if task.get("status") in FINAL_TASK_STATUSES:
        return False
    if now - float(task.get("updated_at") or task.get("created_at") or 0) > 7200:
        return False
    payload = task.get("payload") or {}
    if not _same_project_path(payload.get("project_path"), project_path):
        return False
    task_pid = task.get("pid") or (task.get("result") or {}).get("pid")
    if pid is not None and task_pid is None:
        return False
    if pid is not None and task_pid is not None:
        try:
            return int(task_pid) == int(pid)
        except (TypeError, ValueError):
            return False
    return True


def _active_launch_task_for_project(
    project_path: str | None,
    pid: int | None = None,
    *,
    timeout: float | None = None,
) -> dict | None:
    if not project_path:
        return None
    now = time.time()
    deadline = time.monotonic() + timeout if timeout is not None else None

    def remaining_timeout(task_id: str | None = None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TaskDiscoveryTimeout(task_id, timeout)
        return remaining

    snapshots = iter_task_snapshots(timeout=remaining_timeout())
    candidates = [
        task
        for task in snapshots
        if _is_active_launch_task_for_project(
            task,
            project_path,
            pid,
            now=now,
        )
    ]
    for snapshot in candidates:
        task_id = snapshot.get("task_id")
        if not task_id:
            continue
        try:
            current = load_task(
                task_id,
                timeout=remaining_timeout(task_id),
            )
        except TaskLockTimeout as exc:
            task = dict(snapshot)
            task["_task_state_snapshot"] = {
                "source": "last_published_snapshot",
                "task_id": exc.task_id,
                "lock_timeout_seconds": exc.timeout,
            }
            return task
        if _is_active_launch_task_for_project(
            current,
            project_path,
            pid,
            now=now,
        ):
            return current
    return None

def _summarize_startup_precheck(check: dict) -> dict:
    errors = check.get("engine", {}).get("errors", []) + check.get("project", {}).get("errors", [])
    warnings = check.get("engine", {}).get("warnings", []) + check.get("project", {}).get("warnings", [])
    for issue in check.get("bridge_plugin", {}).get("issues", []):
        warnings.append(issue)
    remote_control = check.get("remote_control", {})
    recovery = None
    if not remote_control.get("configured", True):
        fix_result = remote_control.get("fix_result", {})
        remote_error = fix_result.get("error")
        if remote_error and remote_error not in errors:
            errors.append(remote_error)
        elif not remote_error:
            for issue in remote_control.get("issues", []):
                if issue not in errors:
                    errors.append(issue)
        recovery = fix_result.get("details", {}).get("recovery")

    result = {
        "ready": check.get("ready", False),
        "errors": errors,
        "warnings": warnings,
    }
    if recovery:
        result["remote_control_recovery"] = recovery
    return result

def _summarize_direct_launch_precheck(check: dict) -> dict:
    """Keep engine/project blockers while recording skipped automation checks."""
    engine = check.get("engine", {})
    project = check.get("project", {})
    controlled = _summarize_startup_precheck(check)
    errors = list(engine.get("errors", [])) + list(project.get("errors", []))
    warnings = list(engine.get("warnings", [])) + list(project.get("warnings", []))
    ignored_automation_issues = [
        item
        for item in controlled.get("errors", []) + controlled.get("warnings", [])
        if item not in errors and item not in warnings
    ]
    return {
        "ready": bool(engine.get("ready", False) and project.get("ready", False)),
        "errors": errors,
        "warnings": warnings,
        "automation_mode": "not_requested",
        "ignored_automation_issues": ignored_automation_issues,
    }


def _same_project_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve().as_posix().lower() == Path(right).resolve().as_posix().lower()
    except Exception:
        return Path(left).as_posix().lower() == Path(right).as_posix().lower()

def _check_already_running(session, state) -> dict | None:
    from cli_anything.unreal.utils.ue_backend import (
        _windows_process_exists,
        detect_ue_dialogs,
        find_running_editors,
    )
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

    running = find_running_editors()
    api = UEEditorAPI(port=state.session.port)
    api_alive = api.is_alive()
    api_owner_pid = None
    if api_alive:
        try:
            api_owner_pid = UEEditorAPI._get_pid_listening_on_port(state.session.port)
        except Exception:
            api_owner_pid = None
    dialogs = detect_ue_dialogs() if sys.platform == "win32" else []
    for editor_proc in running:
        proc_project = editor_proc.get("project", "")
        if _same_project_path(proc_project, session.project_path):
            editor_pid = int(editor_proc["pid"])
            api_belongs_to_editor = api_alive and (api_owner_pid is None or int(api_owner_pid) == editor_pid)
            if api_belongs_to_editor:
                return {
                    "status": "already_running",
                    "pid": editor_pid,
                    "project": proc_project,
                    "message": f"Editor is already running for this project (PID {editor_pid}).",
                }
            if sys.platform == "win32" and _windows_process_exists(editor_pid) is False:
                continue
            active_launch = _active_launch_task_for_project(proc_project, editor_pid)
            if active_launch:
                starting = {
                    "status": "starting",
                    "pid": editor_pid,
                    "project": proc_project,
                    "port": state.session.port,
                    "task_id": active_launch.get("task_id"),
                    "launch_task_status": active_launch.get("status"),
                    "message": f"Editor launch is already in progress for this project (PID {editor_pid}), but the API is not reachable yet.",
                    "suggestion": "Wait for the active launch task or inspect it with editor status <task_id> before retrying launch.",
                }
                if active_launch.get("task_id"):
                    starting["next_command"] = f'ue-cli --project "{proc_project}" editor status {active_launch["task_id"]}'
                return starting
            zombie = {
                "status": "zombie",
                "pid": editor_pid,
                "project": proc_project,
                "message": f"Found UnrealEditor.exe for this project but the API is not reachable on port {state.session.port}.",
                "suggestion": (
                    "The existing editor is preserved. Restore its Remote Control endpoint, "
                    "or use editor close --force only when data loss is authorized."
                ),
            }
            if api_alive and api_owner_pid is not None:
                zombie["api_owner_pid"] = int(api_owner_pid)
                zombie["message"] = (
                    f"Found UnrealEditor.exe for this project (PID {editor_pid}) but Remote Control "
                    f"port {state.session.port} belongs to PID {api_owner_pid}."
                )
            if dialogs:
                zombie["dialogs"] = [{"title": dialog["title"]} for dialog in dialogs]
                zombie["status"] = "starting"
                zombie["message"] = f"Editor process exists for this project but startup is still blocked or incomplete on port {state.session.port}."
                zombie["suggestion"] = "Wait briefly or dismiss any blocking dialogs before retrying."
            return zombie
    return None

def _check_port_in_use(poll_port, state) -> dict | None:
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

    api_check = UEEditorAPI(port=poll_port)
    if api_check.is_alive():
        return {
            "status": "already_running",
            "port": poll_port,
            "message": f"An editor is already responding on port {poll_port}.",
        }
    from cli_anything.unreal.utils.ue_backend import is_tcp_port_in_use

    if is_tcp_port_in_use(poll_port):
        return {
            "status": "port_in_use",
            "port": poll_port,
            "message": f"TCP port {poll_port} is already in use.",
        }
    return None

def _deploy_bridge(session, state) -> dict:
    from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed

    return ensure_plugin_deployed(session.project_dir, preserve_existing=True)

def _launch_extra_arg_parts(arg) -> tuple[str, str | None]:
    raw = str(arg).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    token = raw.lstrip("-/")
    name, separator, value = token.partition("=")
    if not separator:
        return name.casefold(), None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return name.casefold(), value

def _remote_control_launch_error(extra_args) -> dict | None:
    for arg in extra_args or ():
        name, _ = _launch_extra_arg_parts(arg)
        if name != "nullrhi":
            continue
        return {
            "code": "EDITOR_LAUNCH_NULLRHI_UNSUPPORTED",
            "message": "editor launch cannot use Remote Control with -NullRHI.",
            "suggestion": "Remove -NullRHI; controlled editor launch requires a render-capable UnrealEditor process.",
            "details": {
                "incompatible_argument": str(arg),
                "required_service": "WebRemoteControl",
                "editor_started": False,
            },
        }
    return None

def _resolve_launch_log_file(project_dir, project_name, extra_args=None) -> Path:
    for arg in extra_args or ():
        name, value = _launch_extra_arg_parts(arg)
        if name == "abslog" and value:
            return Path(value)
    return Path(project_dir) / "Saved" / "Logs" / f"{project_name}.log"

def _build_launch_cmd(
    editor_exe,
    project_path,
    map_path,
    extra_args=None,
    *,
    unattended: bool = False,
    skip_restore: bool = False,
) -> list:
    cmd = [editor_exe, project_path]
    if map_path:
        # Unreal parses maps as URL parameters only before command-line flags.
        cmd.append(map_path)
    cmd.append("-nosplash")
    if unattended:
        cmd.append("-unattended")
    if skip_restore:
        cmd.append(_SKIP_RESTORE_PACKAGES_ARG)
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args if arg is not None and str(arg) != "")
    return cmd

def _extract_log_error(text: str) -> tuple[str | None, str | None]:
    for pattern in _FATAL_LOG_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 300)
            return text[start:end].strip(), pattern
    return None, None


def _plugin_load_failure_diagnostics(error_text: str | None) -> dict | None:
    """Extract one editor startup plugin/module failure without changing it."""
    if not error_text:
        return None

    matching_line = None
    plugin_match = None
    for line in str(error_text).splitlines() or [str(error_text)]:
        match = _PLUGIN_LOAD_FAILURE_PATTERN.search(line)
        if match:
            matching_line = line.strip()
            plugin_match = match
    if plugin_match is None or matching_line is None:
        return None

    result = {
        "failure_kind": "plugin_load_failure",
        "diagnostic": matching_line,
        "plugin": plugin_match.group("plugin"),
        "suggestion": (
            "Verify that this plugin is enabled and that its module is built for "
            "the selected Unreal Engine, then retry editor launch."
        ),
    }
    module_match = _PLUGIN_LOAD_MODULE_PATTERN.search(str(error_text))
    if module_match:
        result["module"] = module_match.group("module")
    return result


def _full_editor_rebuild_diagnostics(
    *,
    project_path: str | None,
    log_error: str | None = None,
    returncode: int | None = None,
) -> dict:
    """Recognize launch signatures that need a full, non-module Editor rebuild."""
    diagnostic_basis = None
    failure_kind = None
    shader_path = None
    if log_error:
        shader_match = _MISSING_VIRTUAL_SHADER_SOURCE_PATTERN.search(log_error)
        if shader_match:
            diagnostic_basis = "registered_virtual_shader_source_missing"
            failure_kind = "engine_binary_source_mismatch"
            shader_path = shader_match.group("shader_path")

    normalized_returncode = None
    if returncode is not None:
        try:
            normalized_returncode = int(returncode) & 0xFFFFFFFF
        except (TypeError, ValueError):
            normalized_returncode = None
        if normalized_returncode == _WINDOWS_STATUS_ENTRYPOINT_NOT_FOUND:
            diagnostic_basis = "windows_status_entrypoint_not_found"
            failure_kind = "engine_binary_entrypoint_mismatch"

    if failure_kind is None:
        return {}

    result = {
        "failure_kind": failure_kind,
        "likely_cause": "stale_or_mixed_engine_binaries",
        "diagnostic_basis": diagnostic_basis,
        "requires_full_editor_rebuild": True,
        "suggestion": (
            "The checked-out Engine source and loaded DLL set may be inconsistent. "
            "Run a full Editor target compile without --module, then retry editor launch."
        ),
    }
    if project_path:
        result["recovery_command"] = (
            f'ue-cli --project "{project_path}" build compile '
            "--platform Win64 --config Development"
        )
    if shader_path:
        result["missing_virtual_shader_path"] = shader_path
    if normalized_returncode == _WINDOWS_STATUS_ENTRYPOINT_NOT_FOUND:
        result["windows_status"] = "STATUS_ENTRYPOINT_NOT_FOUND"
        result["returncode_hex"] = "0xC0000139"
    return result

def _check_log_errors(log_file: Path) -> str | None:
    if not log_file.exists():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
        excerpt, _pattern = _extract_log_error(text)
        return excerpt
    except Exception:
        return None

def _check_log_errors_incremental(log_file: Path, offset: int) -> tuple[str | None, int]:
    if not log_file.exists():
        return None, offset
    try:
        size = log_file.stat().st_size
        if offset < 0 or offset > size:
            offset = 0
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            text = handle.read()
            new_offset = handle.tell()
        if not text:
            return None, new_offset
        excerpt, _pattern = _extract_log_error(text)
        return excerpt, new_offset
    except Exception:
        return None, offset

def _tcp_port_accepts_connection(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False

def _read_log_tail(log_file: Path, max_bytes: int = 128 * 1024) -> str:
    if not log_file.exists():
        return ""
    try:
        size = log_file.stat().st_size
        with log_file.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""

def _bounded_log_tail_lines(
    log_file: Path,
    *,
    since_offset: int | None = None,
    limit: int = 8,
    max_line_chars: int = 500,
) -> list[str]:
    """Return a small startup-log tail suitable for structured error output."""
    if not log_file.exists():
        return []
    try:
        size = log_file.stat().st_size
        offset = int(since_offset or 0)
        if offset < 0 or offset > size:
            offset = 0
        with log_file.open("rb") as handle:
            handle.seek(max(offset, size - 128 * 1024, 0))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [
        line if len(line) <= max_line_chars else line[:max_line_chars] + "..."
        for line in lines[-limit:]
    ]


def _bounded_fatal_log_tail_lines(
    log_file: Path,
    *,
    since_offset: int,
    limit: int = 8,
    max_line_chars: int = 500,
) -> list[str]:
    """Return bounded fatal/assert context written after an operation began."""
    if not log_file.exists():
        return []
    try:
        size = log_file.stat().st_size
        offset = int(since_offset)
        if offset < 0 or offset > size:
            offset = 0
        with log_file.open("rb") as handle:
            handle.seek(max(offset, size - 128 * 1024, 0))
            text = handle.read().decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return []

    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_line).strip()
        if line:
            lines.append(line)
    matched = [index for index, line in enumerate(lines) if _RUNTIME_FATAL_LOG_PATTERN.search(line)]
    if not matched:
        return []

    selected: list[str] = []
    for index in matched:
        for line in lines[index:min(len(lines), index + 3)]:
            if line not in selected:
                selected.append(line)
    return [
        line if len(line) <= max_line_chars else line[:max_line_chars] + "..."
        for line in selected[-limit:]
    ]


class _EditorProcessExitProbe:
    """Keep a Windows process handle so exit code survives process teardown."""

    def __init__(self, pid: int):
        self.pid = int(pid)
        self._handle = None
        self._kernel32 = None
        if sys.platform != "win32" or self.pid <= 0:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            handle = open_process(0x00100000 | 0x1000, False, self.pid)
            if handle:
                self._kernel32 = kernel32
                self._handle = handle
        except (AttributeError, OSError, TypeError, ValueError):
            self._handle = None
            self._kernel32 = None

    def snapshot(self) -> dict:
        result = {"editor_pid": self.pid}
        if self._handle and self._kernel32:
            try:
                import ctypes
                from ctypes import wintypes

                get_exit_code = self._kernel32.GetExitCodeProcess
                get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                get_exit_code.restype = wintypes.BOOL
                exit_code = wintypes.DWORD()
                if get_exit_code(self._handle, ctypes.byref(exit_code)):
                    code = int(exit_code.value)
                    alive = code == 259  # STILL_ACTIVE
                    result.update(
                        process_alive=alive,
                        process_exit_status="running" if alive else "exited",
                    )
                    if not alive:
                        result["process_exit_code"] = code
                        result["process_exit_code_hex"] = f"0x{code:08X}"
                    return result
            except (AttributeError, OSError, TypeError, ValueError):
                pass

        from cli_anything.unreal.utils.ue_backend import _windows_process_exists

        alive = _windows_process_exists(self.pid)
        if alive is not None:
            result.update(
                process_alive=alive,
                process_exit_status="running" if alive else "exited",
            )
        return result

    def close(self) -> None:
        if not self._handle or not self._kernel32:
            return
        try:
            self._kernel32.CloseHandle(self._handle)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        finally:
            self._handle = None


def capture_editor_disconnect_context(api, project_path: str | None) -> dict:
    """Capture PID handle and log offset before a potentially fatal editor call."""
    raw_pid = getattr(api, "_verified_editor_pid", None)
    pid = None
    if isinstance(raw_pid, (int, str)) and not isinstance(raw_pid, bool):
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = None

    context: dict = {}
    if pid and pid > 0:
        context["process_probe"] = _EditorProcessExitProbe(pid)

    if project_path:
        project = Path(project_path)
        extra_args = None
        cmdline = getattr(api, "_verified_editor_cmdline", None)
        if isinstance(cmdline, str) and cmdline:
            from cli_anything.unreal.utils.ue_backend import _windows_cmdline_to_argv

            extra_args = _windows_cmdline_to_argv(cmdline)
        log_file = _resolve_launch_log_file(project.parent, project.stem, extra_args)
        context["log_file"] = log_file
        try:
            context["log_offset"] = log_file.stat().st_size
        except OSError:
            context["log_offset"] = 0
    return context


def collect_editor_disconnect_diagnostics(context: dict) -> dict:
    """Collect bounded crash evidence after an editor transport disconnect."""
    probe = context.get("process_probe")
    process = probe.snapshot() if probe is not None else {}
    log_file = context.get("log_file")
    fatal_log_tail = []
    if isinstance(log_file, Path):
        fatal_log_tail = _bounded_fatal_log_tail_lines(
            log_file,
            since_offset=int(context.get("log_offset") or 0),
        )

    details = dict(process)
    if isinstance(log_file, Path) and log_file.exists():
        details["log_file"] = str(log_file)
    if fatal_log_tail:
        details["fatal_log_tail"] = fatal_log_tail

    if process.get("process_alive") is False:
        details["failure_kind"] = "editor_process_exited"
    elif fatal_log_tail:
        details["failure_kind"] = "editor_crash_detected"
    return details


def verify_editor_shutdown(context: dict, *, forced: bool = False) -> dict:
    """Reject a shutdown crash even when process disappearance was confirmed."""
    from cli_anything.unreal.errors import UeCliError

    details = collect_editor_disconnect_diagnostics(context)
    exit_code = details.get("process_exit_code")
    if details.get("fatal_log_tail") or (
        isinstance(exit_code, int) and exit_code != 0 and not forced
    ):
        raise UeCliError(
            "EDITOR_CLOSE_CRASHED",
            "Editor exited abnormally during shutdown; a clean close was not verified.",
            exit_code=3,
            details={**details, "stage": "editor_shutdown"},
        )
    return details


def close_editor_disconnect_context(context: dict) -> None:
    probe = context.get("process_probe")
    if probe is not None:
        probe.close()


def _external_editor_crash_diagnostics(lines: list[str]) -> dict:
    """Classify known editor-owned crash stacks without implying a ue-cli failure."""
    text = "\n".join(lines)
    if not (
        _FILESYSTEM_DDC_MAINTAINER_THREAD_PATTERN.search(text)
        and _FILESYSTEM_DDC_MAINTAINER_STACK_PATTERN.search(text)
    ):
        return {}

    return {
        "failure_kind": "external_editor_ddc_crash",
        "likely_cause": "unreal_engine_filesystem_ddc_maintainer_crash",
        "external_component": "Unreal Engine DerivedDataCache",
        "diagnostic_basis": "FileSystemCacheStoreMaintainer crash stack",
        "editor_automation_dispatched": False,
        "retry_safe_after_exit": True,
        "suggestion": (
            "Unreal Editor crashed in its file-system DDC maintainer before Remote Control "
            "came online. Retry editor launch once. If the same signature repeats, preserve "
            "log_file and CrashReportClient artifacts and repair the Engine/DDC environment; "
            "ue-cli cannot repair this editor crash."
        ),
    }

def _remote_control_health_from_lines(
    lines: list[str],
    *,
    limit: int = 8,
    target_port: int | None = None,
) -> dict:
    falcon_lines = [line for line in lines if re.search(r"FalconTunnel|connect failed", line, re.IGNORECASE)]
    http_restart_lines = [
        line for line in lines
        if re.search(r"LogHttpServerModule:.*(Stopping all listeners|All listeners stopped|Starting all listeners|All listeners started)", line, re.IGNORECASE)
        or re.search(r"Start(Http|Gateway|Websocket)Server on port", line, re.IGNORECASE)
        or re.search(r"HttpListener.*(unable to bind|Created new HttpListener)", line, re.IGNORECASE)
    ]
    remote_lines = [
        line for line in lines
        if re.search(r"RemoteControl|WebRemoteControl|WebSocket", line, re.IGNORECASE)
    ]

    latest_stop = max(
        (
            index for index, line in enumerate(lines)
            if re.search(r"LogHttpServerModule:.*Stopping all listeners", line, re.IGNORECASE)
        ),
        default=None,
    )
    cycle_lines = lines[latest_stop:] if latest_stop is not None else lines
    cycle_http_lines = [line for line in cycle_lines if line in http_restart_lines]
    cycle_falcon_lines = [line for line in cycle_lines if line in falcon_lines]

    def _mentions_target_port(line: str) -> bool:
        if target_port is None:
            return True
        return bool(re.search(rf"(?<!\d){re.escape(str(target_port))}(?!\d)", line))

    target_bind_lines = [
        line for line in cycle_lines
        if re.search(r"HttpListener.*unable to bind", line, re.IGNORECASE)
        and _mentions_target_port(line)
    ]
    restart_completed = bool(
        latest_stop is not None
        and any(
            re.search(r"LogHttpServerModule:.*All listeners started", line, re.IGNORECASE)
            for line in cycle_lines
        )
    )

    hints: list[str] = []
    for group in (
        target_bind_lines,
        cycle_http_lines,
        cycle_falcon_lines,
        remote_lines,
        http_restart_lines,
        falcon_lines,
    ):
        for line in group[-limit:]:
            if line not in hints:
                hints.append(line)
            if len(hints) >= limit:
                break
        if len(hints) >= limit:
            break

    if not hints:
        return {}

    result = {"log_hints": hints}
    if latest_stop is not None:
        if target_bind_lines:
            result["http_server_restart_status"] = "bind_failed"
        elif restart_completed:
            result["http_server_restart_status"] = "completed"
        else:
            result["http_server_restart_status"] = "incomplete"

    if target_bind_lines:
        result["likely_cause"] = "remote_control_port_bind_failed"
        port_label = str(target_port) if target_port is not None else "the requested Remote Control port"
        result["cause_hint"] = (
            f"Unreal's HttpListener failed to bind {port_label}."
        )
    elif latest_stop is not None and not restart_completed:
        if cycle_falcon_lines:
            result["likely_cause"] = "http_server_restart_incomplete_by_project_plugin"
            result["cause_hint"] = (
                "A project plugin restarted Unreal's HttpServer, but the latest log cycle did not record listener startup completion."
            )
        else:
            result["likely_cause"] = "http_server_restart_incomplete"
            result["cause_hint"] = (
                "Unreal's latest HttpServer restart did not record listener startup completion."
            )
    elif restart_completed:
        result["cause_hint"] = (
            "The HttpServer listener restart completed without a logged bind failure. "
            "This log sequence alone does not show that Remote Control routes were lost."
        )
    elif remote_lines:
        result["likely_cause"] = "remote_control_started_but_not_reachable"
        result["cause_hint"] = "Remote Control logged startup lines, but its HTTP route did not answer."
    return result

def _extract_remote_control_health_hints(
    text: str,
    *,
    limit: int = 8,
    target_port: int | None = None,
) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    return _remote_control_health_from_lines(lines, limit=limit, target_port=target_port)

def _extract_remote_control_health_hints_from_log(
    log_file: Path,
    *,
    since_offset: int | None = None,
    limit: int = 8,
    target_port: int | None = None,
) -> dict:
    if not log_file.exists():
        return {}
    matched: list[str] = []
    try:
        size = log_file.stat().st_size
        offset = int(since_offset or 0)
        if offset < 0 or offset > size:
            offset = 0
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                if re.search(
                    r"FalconTunnel|connect failed|RemoteControl|WebRemoteControl|WebSocket|"
                    r"LogHttpServerModule:.*(Stopping all listeners|All listeners stopped|Starting all listeners|All listeners started)|"
                    r"Start(Http|Gateway|Websocket)Server on port|"
                    r"HttpListener.*(unable to bind|Created new HttpListener)",
                    line,
                    re.IGNORECASE,
                ):
                    matched.append(line)
    except OSError:
        return {}

    if not matched:
        return {}
    return _remote_control_health_from_lines(matched, limit=limit, target_port=target_port)

def _diagnose_api_unreachable(
    log_file: Path,
    port: int,
    *,
    since_offset: int | None = None,
    expected_process_id: int | None = None,
) -> dict:
    tcp_connect_succeeded = _tcp_port_accepts_connection(port)
    listener_pid = None
    if sys.platform == "win32":
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        try:
            listener_pid = UEEditorAPI._get_pid_listening_on_port(port)
        except (OSError, TypeError, ValueError):
            listener_pid = None
    port_listening = tcp_connect_succeeded or listener_pid is not None
    if port_listening and not tcp_connect_succeeded:
        failure_kind = "api_listener_unresponsive"
    elif tcp_connect_succeeded:
        failure_kind = "api_route_unhealthy"
    else:
        failure_kind = "api_not_listening"
    result = {
        "port_listening": port_listening,
        "tcp_connect_succeeded": tcp_connect_succeeded,
        "api_route_healthy": False,
        "failure_kind": failure_kind,
    }
    if listener_pid is not None:
        result["listener_pid"] = int(listener_pid)
        if expected_process_id is not None:
            try:
                result["listener_owned_by_editor"] = int(listener_pid) == int(expected_process_id)
            except (TypeError, ValueError):
                pass
    hints = _extract_remote_control_health_hints_from_log(
        log_file,
        since_offset=since_offset,
        target_port=port,
    )
    if not hints:
        hints = _extract_remote_control_health_hints(
            _read_log_tail(log_file),
            target_port=port,
        )
    result.update(hints)
    if port_listening and not tcp_connect_succeeded:
        result["likely_cause"] = "tcp_listener_not_accepting_connections"
        owner = f" under PID {listener_pid}" if listener_pid is not None else ""
        result["cause_hint"] = (
            f"The OS TCP table reports port {port} LISTENING{owner}, but an active TCP connection attempt failed. "
            "The editor or its HTTP server may still be starting, busy, or stalled."
        )
        result["suggestion"] = (
            "Keep the existing editor process; inspect its responsiveness and startup log, then retry editor status. "
            "Restart it only if startup remains stalled and unsaved work is safe."
        )
    elif port_listening and result.get("http_server_restart_status") == "completed" and not result.get("likely_cause"):
        result["suggestion"] = (
            f"Remote Control port is listening on {port}, but the HTTP route did not answer. "
            "The HttpServer listener restart completed without bind errors, so do not restart the editor from this signal alone; "
            "poll the active launch task or retry editor status."
        )
    elif port_listening and result.get("likely_cause") == "remote_control_port_bind_failed":
        result["suggestion"] = (
            f"Remote Control port is listening on {port}, but the HTTP route did not answer and the log shows a bind failure "
            "for that port. Inspect port ownership and the reported HttpListener log hints, then retry editor status."
        )
    elif port_listening and result.get("likely_cause") == "http_server_restart_incomplete_by_project_plugin":
        result["suggestion"] = (
            f"Remote Control port is listening on {port}, but the HTTP route did not answer and the log shows an incomplete "
            "listener restart or bind failure. Inspect the reported project-plugin log hints, then retry editor status."
        )
    elif port_listening:
        result["suggestion"] = (
            f"Remote Control port is listening on {port}, but the HTTP route did not answer. "
            "The editor may still be initializing or temporarily busy; retry editor status."
        )
    else:
        result["suggestion"] = (
            f"Port {port} is not accepting TCP connections yet. The editor may still be starting, blocked by a dialog, "
            "or Remote Control may not have bound the configured port."
        )
    return result

def _restore_packages_blocker(proc) -> dict | None:
    if sys.platform != "win32":
        return None
    try:
        process_id = int(proc.pid)
    except (AttributeError, TypeError, ValueError):
        return None
    if process_id <= 0:
        return None

    from cli_anything.unreal.utils.ue_backend import detect_ue_dialogs

    try:
        dialogs = detect_ue_dialogs(process_id=process_id)
    except Exception:
        return None

    for dialog in dialogs:
        title = str(dialog.get("title") or "")
        title_lower = title.casefold()
        is_english_restore = "restore" in title_lower and "package" in title_lower
        is_simplified_chinese_restore = title.strip() == "恢复包"
        if not (is_english_restore or is_simplified_chinese_restore):
            continue
        return {
            "title": title,
            "hwnd": dialog.get("hwnd"),
            "process_id": process_id,
        }
    return None

def _wait_for_api(proc, poll_port, timeout, log_file, state, on_progress=None) -> dict:
    from cli_anything.unreal.utils.ue_backend import _emit_heartbeat
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

    if not state.json_output:
        try:
            if timeout is not None:
                sys.stderr.write(f"[editor] waiting for Remote Control API on port {poll_port} (timeout {timeout}s), log {log_file}\n")
            else:
                sys.stderr.write(f"[editor] waiting for Remote Control API on port {poll_port} (no timeout), log {log_file}\n")
            sys.stderr.flush()
        except Exception:
            pass

    api = UEEditorAPI(port=poll_port)
    start_time = time.time()
    progress_start = time.monotonic()
    deadline = start_time + timeout if timeout is not None else float("inf")
    poll_interval = 5.0
    heartbeat_interval = 60.0
    next_beat = start_time + heartbeat_interval
    result = {}
    active_restore_blocker = None
    startup_log_offset = log_file.stat().st_size if log_file.exists() else 0
    log_offset = startup_log_offset

    while time.time() < deadline:
        returncode = proc.poll()
        elapsed_seconds = int(time.monotonic() - progress_start)
        progress = {
            "status": "waiting_for_remote_control",
            "startup_phase": "waiting_for_remote_control",
            "elapsed_seconds": elapsed_seconds,
            "port": poll_port,
            "process_alive": returncode is None,
            "log_file": str(log_file),
        }
        if returncode is not None:
            if on_progress is not None:
                try:
                    on_progress(progress)
                except Exception:
                    pass
            crash_result = {
                "status": "crashed",
                "failure_kind": "editor_process_exited",
                "startup_phase": "waiting_for_remote_control",
                "returncode": returncode,
                "port": poll_port,
                "process_alive": False,
                "elapsed_seconds": elapsed_seconds,
                "log_file": str(log_file),
                "error": f"Editor process exited with code {returncode} before API came online.",
                "suggestion": "Inspect log_tail and log_file for the startup failure, then retry editor launch after resolving it.",
            }
            log_tail = _bounded_log_tail_lines(
                log_file,
                since_offset=startup_log_offset,
            )
            if log_tail:
                crash_result["log_tail"] = log_tail
            diagnostic_lines = _bounded_log_tail_lines(
                log_file,
                since_offset=startup_log_offset,
                limit=64,
                max_line_chars=2000,
            )
            project_path = getattr(state.session, "project_path", None)
            crash_result.update(
                _full_editor_rebuild_diagnostics(
                    project_path=project_path,
                    returncode=returncode,
                )
            )
            crash_result.update(_external_editor_crash_diagnostics(diagnostic_lines))
            if project_path:
                crash_result["next_command"] = (
                    f'ue-cli --project "{project_path}" editor launch'
                )
            return crash_result

        restore_blocker = _restore_packages_blocker(proc)
        if restore_blocker is not None:
            active_restore_blocker = restore_blocker
            blocked_result = {
                "status": "waiting_for_user_action",
                "startup_phase": "blocked_by_restore_packages",
                "blocking_reason": "restore_packages",
                "port": poll_port,
                "process_alive": True,
                "elapsed_seconds": elapsed_seconds,
                "log_file": str(log_file),
                "blocking_dialog": restore_blocker,
                "message": "Editor startup is waiting for a choice in the Restore Packages dialog.",
                "suggestion": (
                    "Choose Restore Selected or Skip Restore in the Unreal Editor dialog, "
                    "then keep polling this launch task. Do not start a second editor for this project."
                ),
            }
            if on_progress is not None:
                try:
                    on_progress(blocked_result)
                except Exception:
                    pass
            now = time.time()
            if now >= next_beat:
                if not state.json_output:
                    _emit_heartbeat("editor", now - start_time, Path(log_file))
                next_beat += heartbeat_interval
            time.sleep(poll_interval)
            continue
        active_restore_blocker = None

        if on_progress is not None:
            try:
                on_progress(progress)
            except Exception:
                pass

        if api.is_alive():
            owner_pid = None
            owner_verified = sys.platform != "win32"
            if sys.platform == "win32":
                try:
                    owner_pid = UEEditorAPI._get_pid_listening_on_port(poll_port)
                    owner_verified = (
                        owner_pid is not None
                        and int(owner_pid) == int(proc.pid)
                    )
                except (AttributeError, OSError, TypeError, ValueError):
                    owner_verified = False
            if owner_verified:
                return {
                    "status": "online",
                    "startup_phase": "ready",
                    "port": poll_port,
                    "process_alive": True,
                    "process_id": int(proc.pid),
                    "port_owner_pid": int(owner_pid) if owner_pid is not None else None,
                    "port_owner_verified": sys.platform == "win32",
                    "startup_time_seconds": int(time.time() - start_time),
                }
            progress.update({
                "startup_phase": "waiting_for_port_owner",
                "api_reachable": True,
                "expected_process_id": int(proc.pid),
                "port_owner_pid": int(owner_pid) if owner_pid is not None else None,
                "port_owner_verified": False,
            })
            if on_progress is not None:
                try:
                    on_progress(progress)
                except Exception:
                    pass

        elapsed = time.time() - start_time
        if elapsed > 2:
            log_error, log_offset = _check_log_errors_incremental(log_file, log_offset)
            if log_error:
                error_result = {
                    "status": "error_dialog",
                    "startup_phase": "blocked_by_error_dialog",
                    "port": poll_port,
                    "process_alive": True,
                    "elapsed_seconds": elapsed_seconds,
                    "log_file": str(log_file),
                    "error": f"Editor appears stuck on an error dialog: {log_error}",
                }
                plugin_load_failure = _plugin_load_failure_diagnostics(log_error)
                if plugin_load_failure:
                    error_result.update(plugin_load_failure)
                error_result.update(
                    _full_editor_rebuild_diagnostics(
                        project_path=getattr(state.session, "project_path", None),
                        log_error=log_error,
                    )
                )
                return error_result

        now = time.time()
        if now >= next_beat:
            if not state.json_output:
                _emit_heartbeat("editor", now - start_time, Path(log_file))
            next_beat += heartbeat_interval
        time.sleep(poll_interval)

    result["startup_phase"] = "waiting_for_remote_control"
    result["port"] = poll_port
    result["process_alive"] = proc.poll() is None
    result["status"] = "timeout"
    result["log_file"] = str(log_file)
    if timeout is not None:
        result["error"] = f"Editor API did not respond within {timeout}s on port {poll_port}."
    else:
        result["error"] = f"Editor API did not respond on port {poll_port}."
    log_error, _ = _check_log_errors_incremental(log_file, log_offset)
    if not log_error:
        log_error = _check_log_errors(log_file)
    if log_error:
        result["error"] += f" Log hint: {log_error}"
    diagnostics = _diagnose_api_unreachable(
        log_file,
        poll_port,
        since_offset=startup_log_offset,
        expected_process_id=getattr(proc, "pid", None),
    )
    result.update(diagnostics)
    if active_restore_blocker is not None:
        result.update({
            "failure_kind": "blocked_by_restore_packages",
            "startup_phase": "blocked_by_restore_packages",
            "blocking_reason": "restore_packages",
            "blocking_dialog": active_restore_blocker,
            "error": (
                "Editor startup remained blocked by the Restore Packages dialog "
                f"for the {timeout}s launch timeout."
                if timeout is not None
                else "Editor startup remained blocked by the Restore Packages dialog."
            ),
            "suggestion": (
                "Choose Restore Selected or Skip Restore in the existing Unreal Editor, "
                "then run editor status. Do not start a second editor for this project."
            ),
        })
    return result
