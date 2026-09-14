"""Editor control commands."""

from __future__ import annotations

import ast
import codecs
import json
import re
import shlex
import subprocess as sp
import sys
import time
from pathlib import Path

import click

from cli_anything.unreal.commands import (
    AppError,
    AppState,
    _guard_editor_project,
    ensure_inferred_project,
    handle_error,
    output,
    require_editor,
    require_project,
)
from cli_anything.unreal.core.tasks import (
    FINAL_TASK_STATUSES,
    TaskDiscoveryTimeout,
    TaskLockTimeout,
    TaskWorkerSpawnError,
    cancel_task,
    load_task,
    submit_task,
    task_data_path,
    task_progress,
    transition_task,
    wait_for_task,
)
from cli_anything.unreal.errors import raise_for_legacy_error


DEFAULT_EDITOR_LAUNCH_FOREGROUND_WAIT_SECONDS = 30
DEFAULT_EDITOR_LAUNCH_WORKER_TIMEOUT_SECONDS = 300
DEFAULT_EDITOR_STATUS_TIMEOUT_SECONDS = 15
REMOTE_UNREACHABLE_GRACE_SECONDS = 60
REMOTE_UNREACHABLE_CACHE_TTL_SECONDS = 7200
EDITOR_LOG_CAPTURE_LIMIT_BYTES = 2 * 1024 * 1024
EDITOR_LOG_READ_CHUNK_BYTES = 64 * 1024
EDITOR_EXEC_INLINE_LOG_LIMIT_BYTES = 16 * 1024
DEFAULT_EDITOR_EXEC_LOG_WAIT_SECONDS = 1.0
DEFAULT_AUTOMATION_LOG_WAIT_SECONDS = 300.0
EDITOR_CLOSE_PROCESS_GRACE_SECONDS = 10

_AUTOMATION_INLINE_LOG_PATTERN = re.compile(
    r"Automation\s+RunTests|Test\s+Started|Test\s+Completed|"
    r"Result=\{|Automation\s+Test\s+Queue\s+Empty",
    re.IGNORECASE,
)


def _editor_status_timeout_error(
    state: AppState,
    timeout: float,
    phase: str,
    *,
    task_id: str | None = None,
) -> AppError:
    details = {
        "timeout_seconds": timeout,
        "blocking_phase": phase,
        "project": state.session.project_path,
    }
    if task_id:
        details["task_id"] = task_id
    return AppError(
        "EDITOR_STATUS_TIMEOUT",
        f"Editor status exceeded its {timeout:g}s deadline during {phase}.",
        exit_code=4,
        suggestion="Retry with status --timeout <seconds> or editor status --timeout <seconds> to allow more time.",
        details=details,
    )


def _read_stdin_python_code(stream=None) -> str:
    """Read piped Python without letting the Windows console code page eat a BOM."""
    stream = sys.stdin if stream is None else stream
    raw_stream = getattr(stream, "buffer", None)
    if raw_stream is None:
        return stream.read().removeprefix("\ufeff")

    raw = raw_stream.read()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            raise
        return raw.decode(encoding)


def _load_command_project(state: AppState, project_path: str | None) -> None:
    if not project_path:
        return
    try:
        state.session.load_project(project_path)
    except FileNotFoundError:
        raise AppError(
            "PROJECT_NOT_FOUND",
            f"Project not found: {project_path}",
            exit_code=3,
            suggestion="Pass --project <path-to.uproject> before or after editor launch.",
        )
    state.project_is_explicit = True
    state.project_is_inferred = False
    if not state.port_is_explicit:
        from cli_anything.unreal.utils.ue_backend import get_editor_binary_prefix, read_rc_port

        configured_port = read_rc_port(
            state.session.project_dir,
            editor_binary_prefix=get_editor_binary_prefix(state.session.engine_root),
        )
        state.session.port = configured_port if configured_port is not None else 30010


def _project_option(func):
    return click.option("--project", "project_path", type=click.Path(), help="Path to .uproject file")(func)


_VIEWPORT_CAMERA_SCRIPT = """\
import unreal
_fn = getattr(unreal.EditorLevelLibrary, "get_level_viewport_camera_info", None)
if _fn is None:
    _subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    _fn = getattr(_subsystem, "get_level_viewport_camera_info", None)
if _fn is None:
    result = {
        "error": "Active Level Viewport camera is not exposed by this UE Python API.",
        "tried": [
            "unreal.EditorLevelLibrary.get_level_viewport_camera_info",
            "unreal.LevelEditorSubsystem.get_level_viewport_camera_info",
        ],
    }
else:
    loc, rot = _fn()
    result = {
        "loc": [loc.x, loc.y, loc.z],
        "rot": [rot.roll, rot.pitch, rot.yaw],
    }
"""


def _get_viewport_camera(api, timeout: int) -> dict:
    from cli_anything.unreal.core.script_runner import run_python_code

    result = run_python_code(api, _VIEWPORT_CAMERA_SCRIPT, timeout=timeout, save=False)
    if "error" in result:
        raise AppError(
            "VIEWPORT_CAMERA_FAILED",
            f"Could not read Level Viewport camera: {result['error']}",
            details=result,
        )
    if "loc" not in result or "rot" not in result:
        raise AppError(
            "VIEWPORT_CAMERA_FAILED",
            "Could not read Level Viewport camera: script returned no loc/rot.",
            details=result,
        )
    return {"loc": result["loc"], "rot": result["rot"]}


def _set_viewport_game_view(api, mode: str | None) -> dict:
    from cli_anything.unreal.core.script_runner import run_python_code

    requested_mode = (mode or "get").lower()
    script = f"""
import unreal
_mode = {json.dumps(requested_mode)}
_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
_get = getattr(_subsystem, "editor_get_game_view", None)
_set = getattr(_subsystem, "editor_set_game_view", None)
_active_viewport = False
_viewport_error = None
try:
    _bounds = unreal.CliAnythingBridgeLibrary.get_active_viewport_screen_bounds()
    _active_viewport = int(_bounds.z) > 0 and int(_bounds.w) > 0
except Exception as _exc:
    _viewport_error = str(_exc)
if not _active_viewport:
    result = {{
        "error": "No active Level Viewport could be verified.",
        "mode": _mode,
        "viewport_error": _viewport_error,
        "suggestion": "Focus a Level Viewport and ensure the bundled bridge plugin is current.",
    }}
elif _get is None or (_mode != "get" and _set is None):
    result = {{
        "error": "Level Viewport game-view state is not exposed by this UE Python API.",
        "mode": _mode,
        "tried": [
            "LevelEditorSubsystem.editor_get_game_view",
            "LevelEditorSubsystem.editor_set_game_view",
        ],
    }}
else:
    _before = bool(_get())
    _desired = _before
    if _mode == "toggle":
        _desired = not _before
    elif _mode == "on":
        _desired = True
    elif _mode == "off":
        _desired = False
    if _mode != "get" and _desired != _before:
        _set(_desired)
    _after = bool(_get())
    result = {{
        "status": "ok",
        "mode": _mode,
        "before": _before,
        "after": _after,
        "changed": _after != _before,
    }}
    if _after != _desired:
        result["error"] = "Level Viewport game-view state did not reach the requested value."
"""
    result = run_python_code(api, script, save=False)
    if result.get("error"):
        raise AppError(
            "VIEWPORT_GAME_VIEW_FAILED",
            f"Could not update Level Viewport game view: {result['error']}",
            exit_code=3,
            details=result,
        )
    required = {"before", "after", "changed"}
    if not required.issubset(result):
        raise AppError(
            "VIEWPORT_GAME_VIEW_FAILED",
            "Could not read Level Viewport game-view state.",
            exit_code=3,
            details=result,
        )
    return {
        "status": result.get("status", "ok"),
        "mode": result.get("mode", requested_mode),
        "before": result["before"],
        "after": result["after"],
        "changed": result["changed"],
    }


def _camera_changed(before: dict, after: dict, tolerance: float = 1e-3) -> bool:
    for key in ("loc", "rot"):
        for a, b in zip(before.get(key, []), after.get(key, [])):
            if abs(float(a) - float(b)) > tolerance:
                return True
    return False


def _jump_viewport_bookmark_win32(project_title_hint: str, index: int) -> dict:
    if sys.platform != "win32":
        raise AppError(
            "UNSUPPORTED_PLATFORM",
            "Viewport bookmark keyboard simulation is only supported on Windows.",
            exit_code=2,
        )

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value
                if "Unreal Editor" in title and (not project_title_hint or project_title_hint in title):
                    found.append((int(hwnd), title))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        raise AppError(
            "EDITOR_WINDOW_NOT_FOUND",
            f"Could not find a visible Unreal Editor window matching project title: {project_title_hint}",
            exit_code=3,
            suggestion="Make sure the editor window is visible and not minimized to another desktop.",
        )

    hwnd, title = found[0]
    user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    user32.BringWindowToTop(wintypes.HWND(hwnd))
    time.sleep(0.2)

    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise AppError("EDITOR_WINDOW_RECT_FAILED", "Could not read Unreal Editor window bounds.", exit_code=3)

    x = int(rect.left + (rect.right - rect.left) // 2)
    y = int(rect.top + (rect.bottom - rect.top) // 2)
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
    time.sleep(0.1)

    vk = 0x30 + int(index)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 0x0002, 0)
    time.sleep(0.5)

    return {"hwnd": hwnd, "title": title, "focus_point": [x, y]}


@click.group("editor")
def editor_group():
    """Editor control commands."""


def _parse_scan_range(scan_range: str) -> tuple[int, int]:
    parts = str(scan_range).split("-", 1)
    start = int(parts[0])
    end = int(parts[1]) if len(parts) > 1 else start
    if end < start:
        start, end = end, start
    return start, end


def _project_config_port(project_path: str | None) -> int | None:
    if not project_path:
        return None
    try:
        from cli_anything.unreal.utils.ue_backend import (
            find_engine_root,
            get_editor_binary_prefix,
            read_rc_port,
        )

        return read_rc_port(
            str(Path(project_path).parent),
            editor_binary_prefix=get_editor_binary_prefix(find_engine_root(project_path)),
        )
    except Exception:
        return None


def _editor_log_error(project_path: str | None) -> str | None:
    if not project_path:
        return None
    try:
        project = Path(project_path)
        return _check_log_errors(project.parent / "Saved" / "Logs" / f"{project.stem}.log")
    except Exception:
        return None


def _compact_editor_entry(status: str, pid: int | None, port: int | None, project_path: str | None) -> dict:
    return {
        "status": status,
        "pid": pid,
        "port": port,
        "project_path": project_path or None,
    }


def _deduplicate_editor_status_instances(instances: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[tuple[int | None, int | None, str | None]] = set()
    for entry in instances:
        project_path = entry.get("project_path")
        if project_path:
            try:
                project_path = Path(project_path).resolve().as_posix().lower()
            except Exception:
                project_path = Path(project_path).as_posix().lower()
        identity = (entry.get("pid"), entry.get("port"), project_path)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(entry)
    return unique


def _remote_unreachable_cache_path() -> Path:
    return task_data_path("editor_remote_unreachable.json")


def _remote_unreachable_key(project_path: str | None, pid: int | None, port: int | None) -> str:
    project = str(project_path or "").lower()
    return f"{project}|{pid or ''}|{port or ''}"


def _load_remote_unreachable_cache() -> dict:
    path = _remote_unreachable_cache_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_remote_unreachable_cache(cache: dict) -> None:
    path = _remote_unreachable_cache_path()
    now = time.time()
    compact = {
        key: value
        for key, value in cache.items()
        if now - float(value.get("last_seen_at") or value.get("first_seen_at") or 0) <= REMOTE_UNREACHABLE_CACHE_TTL_SECONDS
    }
    try:
        path.write_text(json.dumps(compact, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _clear_remote_unreachable_observation(project_path: str | None, pid: int | None, port: int | None) -> None:
    cache = _load_remote_unreachable_cache()
    key = _remote_unreachable_key(project_path, pid, port)
    if key in cache:
        cache.pop(key, None)
        _save_remote_unreachable_cache(cache)


def _record_remote_unreachable_observation(entry: dict) -> dict:
    now = time.time()
    key = _remote_unreachable_key(entry.get("project_path"), entry.get("pid"), entry.get("port"))
    cache = _load_remote_unreachable_cache()
    obs = cache.get(key) or {"first_seen_at": now, "count": 0}
    obs["last_seen_at"] = now
    obs["count"] = int(obs.get("count") or 0) + 1
    cache[key] = obs
    _save_remote_unreachable_cache(cache)
    obs = dict(obs)
    obs["unreachable_seconds"] = int(max(0, now - float(obs.get("first_seen_at") or now)))
    return obs


def _add_transient_unreachable_hint(entry: dict, observation: dict) -> None:
    entry["status"] = "unreachable"
    entry["message"] = "Remote Control API is temporarily unreachable; UnrealEditor is still running and may be busy with PIE, loading, shader work, or startup."
    entry["suggestion"] = "Retry editor status before relaunching. Do not terminate the editor unless it remains unreachable past the stale grace period or the process exits."
    entry["unreachable_seconds"] = observation.get("unreachable_seconds", 0)
    entry["stale_after_seconds"] = REMOTE_UNREACHABLE_GRACE_SECONDS
    project_path = entry.get("project_path")
    if project_path:
        entry["next_command"] = f'ue-cli --project "{project_path}" editor status'


def _add_launching_recovery_hint(entry: dict, task: dict) -> None:
    entry["status"] = "launching"
    entry["task_id"] = task.get("task_id")
    entry["launch_task_status"] = task.get("status")
    if task.get("worker_pid"):
        entry["worker_pid"] = task.get("worker_pid")
    log_file = task.get("log_file") or (task.get("result") or {}).get("log_file")
    if log_file:
        entry["log_file"] = log_file
    entry["message"] = "UnrealEditor process is running for an active launch task, but the Remote Control API is not reachable yet."
    entry["suggestion"] = "Wait for startup to finish or inspect the launch task; do not start another launch unless the task times out or fails."
    project_path = entry.get("project_path")
    if project_path and task.get("task_id"):
        entry["next_command"] = f'ue-cli --project "{project_path}" editor status {task["task_id"]}'
    snapshot = task.get("_task_state_snapshot")
    if snapshot:
        entry["task_state_source"] = snapshot["source"]
        entry["task_state_may_be_stale"] = True
        entry["task_discovery_timeout"] = {
            "blocking_phase": "task_discovery",
            "task_id": snapshot["task_id"],
            "timeout_seconds": snapshot["lock_timeout_seconds"],
        }
        entry["message"] = (
            "UnrealEditor is still running and Remote Control is not reachable; "
            "task discovery timed out, so the last published launch state was retained."
        )


def _add_offline_recovery_hint(entry: dict, observation: dict | None = None) -> None:
    entry["message"] = "UnrealEditor process is running, but the Remote Control API is not reachable."
    if observation:
        entry["unreachable_seconds"] = observation.get("unreachable_seconds", 0)
        entry["stale_after_seconds"] = REMOTE_UNREACHABLE_GRACE_SECONDS
    project_path = entry.get("project_path")
    if project_path:
        entry["suggestion"] = (
            "Remote Control has stayed unreachable past the stale grace period. Run editor launch to "
            "terminate the stale matching process and start a fresh editor if needed."
        )
        entry["next_command"] = f'ue-cli --project "{project_path}" editor launch'
    else:
        entry["suggestion"] = "Run editor launch with --project <path-to.uproject> to start a reachable editor."


def _add_failed_health_probe_hint(entry: dict, error: Exception) -> None:
    entry["status"] = "unreachable"
    entry["listener_reachable"] = True
    entry["health_probe"] = "failed"
    entry["health_probe_error"] = str(error)[:500]
    entry["message"] = (
        "Remote Control answered /remote/info, but the functional editor health probe failed; "
        "UnrealEditor may be busy or hung."
    )
    entry["suggestion"] = (
        "Wait, then retry editor status. If the same editor remains unresponsive, close or restart "
        "that existing session; do not launch another editor alongside it."
    )
    project_path = entry.get("project_path")
    if project_path:
        entry["next_command"] = f'ue-cli --project "{project_path}" editor status'


def _add_online_bridge_status(entry: dict, timeout: float = 5.0) -> None:
    if entry.get("status") != "online" or not entry.get("port"):
        return

    from cli_anything.unreal.core.plugin_bridge import get_bundled_version, get_loaded_plugin_version
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

    bundled = get_bundled_version()
    loaded = None
    probe_failed = False
    probe_error: Exception | None = None
    try:
        loaded = get_loaded_plugin_version(
            UEEditorAPI(port=int(entry["port"])),
            timeout=timeout,
            raise_on_error=True,
        )
    except Exception as exc:
        probe_failed = True
        probe_error = exc
        loaded = None

    entry["bridge_version"] = loaded
    entry["bundled_version"] = bundled
    if probe_failed and probe_error is not None:
        entry["plugin_match"] = None
        _add_failed_health_probe_hint(entry, probe_error)
        return

    project_path = entry.get("project_path")
    if loaded is None:
        entry["plugin_match"] = False
        entry["bridge_status"] = "missing_or_unversioned"
        entry["degraded_mode"] = "remote_control_only"
        entry["read_only_commands_available"] = True
        entry["remote_control_commands_available"] = True
        entry["run_script_no_save_available"] = True
        entry["bridge_commands_available"] = False
        entry["message"] = (
            "Editor is online via Remote Control, but CliAnythingBridge is missing or does not expose a version. "
            "Remote Control commands remain available without a restart; bridge-only commands may fail until "
            "the plugin is upgraded and the editor is restarted."
        )
        entry["suggestion"] = (
            "For a non-mutating validation script, editor run-script --no-save can still run in "
            "remote-control-only mode. --no-save skips ue-cli's automatic save; it does not sandbox script side effects. "
            "Schedule editor plugin-upgrade when bridge commands are needed and a restart is acceptable."
        )
        if project_path:
            entry["no_restart_command"] = (
                f'ue-cli --output json --project "{project_path}" editor run-script --no-save -'
            )
            entry["upgrade_command"] = f'ue-cli --project "{project_path}" editor plugin-upgrade'
        return

    entry["plugin_match"] = bundled is not None and loaded == bundled
    if not entry["plugin_match"]:
        entry["restart_required"] = True
        entry["restart_scope"] = "bridge_commands_only"
        entry["bridge_status"] = "version_mismatch"
        entry["degraded_mode"] = "remote_control_only"
        entry["read_only_commands_available"] = True
        entry["remote_control_commands_available"] = True
        entry["run_script_no_save_available"] = True
        entry["bridge_commands_available"] = False
        entry["message"] = (
            f"running editor loaded CliAnythingBridge {loaded}, "
            f"but ue-cli bundles {bundled or 'unknown'}. UE cannot hot-reload this C++ bridge safely; "
            "Remote Control remains available, including editor run-script --no-save. "
            "Bridge-backed commands require editor plugin-upgrade before use."
        )
        entry["suggestion"] = (
            "For a non-mutating validation script, use editor run-script --no-save without restarting. "
            "--no-save skips ue-cli's automatic save; it does not sandbox script side effects. "
            "Schedule editor plugin-upgrade only when bridge-backed commands are needed and a restart is acceptable."
        )
        if project_path:
            entry["no_restart_command"] = (
                f'ue-cli --output json --project "{project_path}" editor run-script --no-save -'
            )
            entry["upgrade_command"] = f'ue-cli --project "{project_path}" editor plugin-upgrade'


def _add_online_bridge_statuses(
    entries: list[dict],
    timeout: float = 5.0,
) -> None:
    targets = [
        entry for entry in entries
        if entry.get("status") == "online" and entry.get("port")
    ]
    if not targets:
        return
    if len(targets) == 1:
        _add_online_bridge_status(targets[0], timeout=timeout)
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        futures = [
            executor.submit(_add_online_bridge_status, entry, timeout=timeout)
            for entry in targets
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass


def _scan_editor_status_instances(
    state: AppState,
    scan_range: str,
    *,
    include_bridge_status: bool = True,
    project_filter: str | None = None,
    timeout: float | None = None,
) -> list[dict]:
    from cli_anything.unreal.utils.ue_backend import find_running_editors
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI, scan_editor_ports

    start, end = _parse_scan_range(scan_range)
    deadline = time.monotonic() + timeout if timeout is not None else None

    def remaining_timeout(phase: str) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _editor_status_timeout_error(state, timeout, phase)
        return max(0.01, remaining)
    extra_ports: set[int] = set()
    if state.session.port:
        extra_ports.add(int(state.session.port))
    configured_port = _project_config_port(state.session.project_path)
    if configured_port and int(configured_port) == int(state.session.port):
        extra_ports.add(int(configured_port))

    if sys.platform == "win32":
        running = find_running_editors(
            timeout=remaining_timeout("process_discovery")
        )
        remaining_timeout("process_discovery")
    else:
        running = []
    if project_filter:
        running = [
            proc for proc in running
            if _same_project_path(proc.get("project"), project_filter)
        ]
    proc_config_port_by_pid: dict[int, int] = {}
    for proc in running:
        proc_port = _project_config_port(proc.get("project") or None)
        if proc_port:
            extra_ports.add(int(proc_port))
            try:
                proc_config_port_by_pid[int(proc.get("pid", 0))] = int(proc_port)
            except (TypeError, ValueError):
                pass

    online_by_port: dict[int, dict] = {}
    port_scan_timeout = remaining_timeout("port_scan")
    if port_scan_timeout is None:
        port_scan_timeout = 0.35
    else:
        port_scan_timeout = min(0.35, port_scan_timeout)
    for item in scan_editor_ports(
        port_range=(start, end),
        timeout=port_scan_timeout,
    ):
        online_by_port[int(item["port"])] = item
    remaining_timeout("port_scan")
    for port in sorted(extra_ports):
        if start <= port <= end:
            continue
        extra_scan_timeout = remaining_timeout("configured_port_scan")
        if extra_scan_timeout is None:
            extra_scan_timeout = 0.35
        else:
            extra_scan_timeout = min(0.35, extra_scan_timeout)
        for item in scan_editor_ports(
            port_range=(port, port),
            timeout=extra_scan_timeout,
        ):
            online_by_port[int(item["port"])] = item
        remaining_timeout("configured_port_scan")

    process_by_pid: dict[int, dict] = {}
    for proc in running:
        try:
            process_by_pid[int(proc.get("pid", 0))] = proc
        except (TypeError, ValueError):
            pass

    pid_by_port: dict[int, int | None] = {}
    port_by_pid: dict[int, int] = {}
    ports_to_resolve = sorted(online_by_port)
    owner_timeout = remaining_timeout("port_owner_resolution")
    if owner_timeout is None:
        owner_timeout = 3
    else:
        owner_timeout = min(3, owner_timeout)
    if len(ports_to_resolve) <= 1:
        resolved = [
            (
                port,
                UEEditorAPI._get_pid_listening_on_port(
                    port,
                    timeout=owner_timeout,
                ),
            )
            for port in ports_to_resolve
        ]
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        resolved = []
        with ThreadPoolExecutor(max_workers=min(8, len(ports_to_resolve))) as executor:
            futures = {
                executor.submit(
                    UEEditorAPI._get_pid_listening_on_port,
                    port,
                    timeout=owner_timeout,
                ): port
                for port in ports_to_resolve
            }
            for future in as_completed(futures):
                port = futures[future]
                try:
                    resolved.append((port, future.result()))
                except Exception:
                    resolved.append((port, None))
    remaining_timeout("port_owner_resolution")
    for port, owner_pid in resolved:
        pid_by_port[port] = owner_pid
        if owner_pid:
            port_by_pid[int(owner_pid)] = port

    instances: list[dict] = []
    used_ports: set[int] = set()
    for proc in running:
        try:
            pid = int(proc.get("pid", 0))
        except (TypeError, ValueError):
            pid = None
        project_path = proc.get("project") or None
        port = port_by_pid.get(pid) if pid is not None else None
        if port is None and pid is not None:
            configured_proc_port = proc_config_port_by_pid.get(pid)
            if (
                configured_proc_port in online_by_port
                and pid_by_port.get(configured_proc_port) is not None
                and int(pid_by_port[configured_proc_port]) == pid
            ):
                port = configured_proc_port
        if port is None and pid is not None and _same_project_path(project_path, state.session.project_path):
            probe_port = int(
                state.session.port
                or proc_config_port_by_pid.get(pid)
                or _project_config_port(project_path)
                or 30010
            )
            if probe_port not in online_by_port:
                try:
                    probe_timeout = remaining_timeout("project_port_scan")
                    if probe_timeout is None:
                        probe_timeout = 3.0
                    else:
                        probe_timeout = min(3.0, probe_timeout)
                    for item in scan_editor_ports(
                        port_range=(probe_port, probe_port),
                        timeout=probe_timeout,
                    ):
                        online_by_port[int(item["port"])] = item
                except Exception:
                    pass
                remaining_timeout("project_port_scan")
            if probe_port in online_by_port:
                try:
                    probe_timeout = remaining_timeout("port_owner_resolution")
                    owner_pid = UEEditorAPI._get_pid_listening_on_port(
                        probe_port,
                        timeout=3 if probe_timeout is None else min(3, probe_timeout),
                    )
                except Exception:
                    owner_pid = None
                remaining_timeout("port_owner_resolution")
                pid_by_port[probe_port] = owner_pid
                if owner_pid:
                    port_by_pid[int(owner_pid)] = probe_port
                if owner_pid is not None and int(owner_pid) == pid:
                    port = probe_port
                    port_by_pid[pid] = probe_port
        if port is not None:
            used_ports.add(port)
            entry = _compact_editor_entry("online", pid, port, project_path)
            _clear_remote_unreachable_observation(project_path, pid, port)
        else:
            entry = _compact_editor_entry("offline", pid, _project_config_port(project_path), project_path)
            try:
                active_launch = _active_launch_task_for_project(
                    project_path,
                    pid,
                    timeout=remaining_timeout("task_discovery"),
                )
            except (TaskDiscoveryTimeout, TaskLockTimeout) as exc:
                raise _editor_status_timeout_error(
                    state,
                    timeout,
                    "task_discovery",
                    task_id=exc.task_id,
                ) from exc
            log_error = _editor_log_error(project_path)
            if active_launch:
                _add_launching_recovery_hint(entry, active_launch)
            elif log_error:
                _add_offline_recovery_hint(entry)
            else:
                observation = _record_remote_unreachable_observation(entry)
                if int(observation.get("unreachable_seconds") or 0) < REMOTE_UNREACHABLE_GRACE_SECONDS:
                    _add_transient_unreachable_hint(entry, observation)
                else:
                    _add_offline_recovery_hint(entry, observation)
            if log_error:
                entry["log_error"] = log_error
        instances.append(entry)

    for port in sorted(online_by_port):
        if port in used_ports:
            continue
        owner_pid = pid_by_port.get(port)
        owner = process_by_pid.get(int(owner_pid)) if owner_pid else None
        project_path = owner.get("project") if owner else None
        entry = _compact_editor_entry(
            "online",
            int(owner_pid) if owner_pid else None,
            port,
            project_path,
        )
        if owner_pid is None:
            entry["ownership"] = "unknown"
        elif owner is None:
            entry["ownership"] = "unverified_process"
        else:
            entry["ownership"] = "verified"
        instances.append(entry)

    instances = _deduplicate_editor_status_instances(instances)
    instances = _filter_editor_status_instances(instances, project_filter)
    if include_bridge_status and any(
        entry.get("status") == "online" and entry.get("port")
        for entry in instances
    ):
        bridge_timeout = remaining_timeout("bridge_status")
        _add_online_bridge_statuses(
            instances,
            timeout=5.0 if bridge_timeout is None else min(5.0, bridge_timeout),
        )
    return instances


def _filter_editor_status_instances(instances: list[dict], project_path: str | None) -> list[dict]:
    if not project_path:
        return instances
    return [
        item for item in instances
        if _same_project_path(item.get("project_path"), project_path)
    ]


def _launch_wait_timeouts(timeout: int | None) -> tuple[int, int]:
    if timeout is not None:
        return min(timeout, DEFAULT_EDITOR_LAUNCH_FOREGROUND_WAIT_SECONDS), timeout
    return DEFAULT_EDITOR_LAUNCH_FOREGROUND_WAIT_SECONDS, DEFAULT_EDITOR_LAUNCH_WORKER_TIMEOUT_SECONDS


def _recover_online_launch_result(
    state: AppState,
    task_id: str,
    current_task: dict,
    *,
    recovered_from: str = "launch_task_wait",
    status_timeout: float | None = None,
) -> dict | None:
    payload = current_task.get("payload") or {}
    target_project = payload.get("project_path") or state.session.project_path
    if not target_project:
        return None
    if state.session.project_path and not _same_project_path(state.session.project_path, target_project):
        return None

    deadline = (
        time.monotonic() + status_timeout
        if status_timeout is not None
        else None
    )

    def bounded_timeout(limit: float) -> float | None:
        if deadline is None:
            return limit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return max(0.01, min(limit, remaining))

    try:
        task_port = current_task.get("resolved_port")
        if task_port is None:
            task_port = payload.get("port")
        scan_range = str(task_port) if task_port is not None else "30010-30020"
        scan_timeout = (
            bounded_timeout(status_timeout)
            if status_timeout is not None
            else None
        )
        if status_timeout is not None and scan_timeout is None:
            return None
        instances = _scan_editor_status_instances(
            state,
            scan_range,
            project_filter=target_project,
            timeout=scan_timeout,
        )
        instances = _filter_editor_status_instances(instances, target_project)
    except Exception:
        return None

    task_pid = current_task.get("pid")
    if task_pid is None:
        return None
    online = next(
        (
            item for item in instances
            if item.get("status") == "online"
            and (
                item.get("pid") is not None
                and int(item["pid"]) == int(task_pid)
            )
        ),
        None,
    )
    if online is None:
        return None

    online_port = online.get("port")
    if online_port is None:
        return None
    try:
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        owner_timeout = bounded_timeout(3.0)
        if owner_timeout is None:
            return None
        owner_pid = UEEditorAPI._get_pid_listening_on_port(
            int(online_port),
            timeout=owner_timeout,
        )
    except Exception:
        return None
    if owner_pid is None or int(owner_pid) != int(task_pid):
        return None

    recorded_identity = current_task.get("editor_process_identity")
    identity_verified = False
    if recorded_identity:
        try:
            from cli_anything.unreal.core.tasks import _recorded_process_identity_matches
            from cli_anything.unreal.utils.ue_backend import _windows_process_identity

            current_identity = _windows_process_identity(int(task_pid))
        except Exception:
            return None
        if not isinstance(current_identity, dict) or not (
            current_identity.get("query_ok")
            and current_identity.get("found")
            and _recorded_process_identity_matches(recorded_identity, current_identity)
        ):
            return None
        identity_verified = True
    elif sys.platform == "win32" and int(current_task.get("task_state_version") or 1) >= 2:
        return None

    requested_map = payload.get("map_path")
    map_verification = None
    if requested_map:
        try:
            from cli_anything.unreal.core.scene import _verify_current_level

            verify_timeout = bounded_timeout(5.0)
            if verify_timeout is None:
                return None
            api = UEEditorAPI(port=int(online.get("port") or task_port or state.session.port))
            map_verification = _verify_current_level(
                api,
                requested_map,
                verify_timeout=verify_timeout,
            )
        except Exception:
            return None
        if map_verification.get("status") != "ok":
            return None

    result = dict(online)
    result["startup_phase"] = "ready"
    result["task_id"] = task_id
    result["command"] = "editor.launch"
    result["launch_task_status"] = current_task.get("status", "unknown")
    result["recovered_from"] = recovered_from
    result["process_identity_verified"] = identity_verified
    if not result.get("pid") and current_task.get("pid"):
        result["pid"] = current_task.get("pid")
    if current_task.get("log_file") and not result.get("log_file"):
        result["log_file"] = current_task.get("log_file")
    if requested_map:
        result["requested_map"] = requested_map
        result["map_verification"] = map_verification
    return result


@editor_group.command("status")
@click.option("--scan-range", default="30010-30020", help="Port range to scan")
@click.option("--all", "show_all", is_flag=True, default=False, help="Show all editor instances instead of filtering to --project.")
@click.option(
    "--timeout",
    default=DEFAULT_EDITOR_STATUS_TIMEOUT_SECONDS,
    type=click.FloatRange(min=0.1),
    show_default=True,
    help="Maximum seconds for editor and task discovery.",
)
@_project_option
@click.argument("task_id", required=False)
@handle_error
@click.pass_obj
def editor_status(state: AppState, scan_range, show_all, timeout, project_path, task_id):
    """Show detected Unreal Editor sessions and launch-task status."""
    _load_command_project(state, project_path)
    if task_id:
        try:
            task = load_task(task_id, timeout=timeout)
        except TaskLockTimeout as exc:
            raise _editor_status_timeout_error(
                state,
                timeout,
                "task_discovery",
                task_id=exc.task_id,
            ) from exc
        if task is None:
            raise AppError("TASK_NOT_FOUND", f"Task not found: {task_id}", exit_code=3)
        task_status = task.get("status")
        recoverable_launch = (
            task.get("command") == "editor.launch"
            and (
                task_status == "timeout"
                or (
                    task_status == "running"
                    and task.get("phase") == "waiting_remote_control"
                )
            )
        )
        if recoverable_launch:
            recovered = _recover_online_launch_result(
                state,
                task_id,
                task,
                recovered_from="launch_task_status",
                status_timeout=timeout,
            )
            if recovered is not None:
                task = transition_task(
                    task_id,
                    expected_statuses={task_status},
                    status="completed",
                    phase="online",
                    result_patch=recovered,
                    result_remove=(
                        "failure_kind",
                        "api_route_healthy",
                        "next_command",
                        "error",
                    ),
                    error=None,
                ) or task
        output(task_progress(task), state)
        return

    if not show_all:
        ensure_inferred_project(state)
        if state.session.project_path:
            from cli_anything.unreal.core.confirmations import raise_if_editor_blocked

            raise_if_editor_blocked(state.session.project_path)
    scan_options = {"timeout": timeout}
    if not show_all and state.session.project_path:
        scan_options["project_filter"] = state.session.project_path
    instances = _scan_editor_status_instances(state, scan_range, **scan_options)
    if not show_all:
        instances = _filter_editor_status_instances(instances, state.session.project_path)
        if state.session.project_path and any(
            item.get("status") != "online" for item in instances
        ):
            from cli_anything.unreal.core.confirmations import raise_if_editor_blocked

            raise_if_editor_blocked(
                state.session.project_path,
                include_windows=True,
            )
    output(instances, state)


@editor_group.command("preflight")
@handle_error
@click.pass_obj
def editor_preflight(state: AppState):
    """Run read-only editor startup preflight checks."""
    from cli_anything.unreal.utils.ue_backend import preflight_check

    require_project(state)
    output(preflight_check(state.session.project_path, state.session.engine_root), state)


@editor_group.group("viewport")
def viewport_group():
    """Viewport commands."""


@viewport_group.command("camera")
@click.option("--timeout", default=30, type=int, help="Seconds to wait when reading the viewport camera.")
@handle_error
@click.pass_obj
def viewport_camera(state: AppState, timeout):
    """Read the active Level Viewport camera."""
    api = require_editor(state)
    output(_get_viewport_camera(api, timeout), state)


@viewport_group.command("game-view")
@click.argument(
    "mode",
    required=False,
    type=click.Choice(["on", "off", "toggle"], case_sensitive=False),
)
@handle_error
@click.pass_obj
def viewport_game_view(state: AppState, mode):
    """Read or change the active Level Viewport game-view state."""
    api = require_editor(state)
    output(_set_viewport_game_view(api, mode), state)


@viewport_group.group("bookmark")
def viewport_bookmark_group():
    """Level Viewport bookmark commands."""


@viewport_bookmark_group.command("jump")
@click.option("--index", required=True, type=click.IntRange(0, 9), help="Bookmark index to jump to (0-9).")
@click.option("--timeout", default=30, type=int, help="Seconds to wait when reading the viewport camera.")
@handle_error
@click.pass_obj
def viewport_bookmark_jump(state: AppState, index, timeout):
    """Jump to a Level Viewport bookmark using the editor's numeric shortcut."""
    require_project(state)
    if sys.platform != "win32":
        raise AppError(
            "UNSUPPORTED_PLATFORM",
            "Viewport bookmark keyboard simulation is only supported on Windows.",
            exit_code=2,
        )
    api = require_editor(state)
    before = _get_viewport_camera(api, timeout)
    window = _jump_viewport_bookmark_win32(state.session.project_name or "", index)
    after = _get_viewport_camera(api, timeout)

    if not _camera_changed(before, after):
        raise AppError(
            "BOOKMARK_JUMP_UNCHANGED",
            "Viewport camera did not change after sending the bookmark shortcut.",
            exit_code=3,
            suggestion="Ensure the Level Viewport is focused, the bookmark exists, the numeric shortcut is unchanged, and the correct editor window was activated.",
            details={"index": index, "window": window, "before": before, "after": after},
        )

    output({"status": "jumped", "index": index, "window": window, "before": before, "after": after}, state)


def _find_matching_project_editors(project_path: str | None) -> tuple[list[dict], list[dict]]:
    from cli_anything.unreal.utils.ue_backend import find_running_editors

    running = find_running_editors()
    matches = [
        proc for proc in running
        if _same_project_path(proc.get("project", ""), project_path)
    ]
    return running, matches


def _same_windows_process_identity(expected: dict, current: dict) -> bool:
    if int(expected.get("pid") or 0) != int(current.get("pid") or 0):
        return False
    if expected.get("creation_time") != current.get("creation_time"):
        return False

    expected_image = str(expected.get("image_path") or "")
    current_image = str(current.get("image_path") or "")
    if expected_image and current_image:
        return (
            expected_image.replace("/", "\\").casefold()
            == current_image.replace("/", "\\").casefold()
        )
    return True


def _capture_project_editor_targets(matches: list[dict]) -> list[dict]:
    """Keep PID-reuse-safe identities after CIM project metadata disappears."""
    from cli_anything.unreal.utils.ue_backend import _windows_process_identity

    targets = []
    for proc in matches:
        try:
            pid = int(proc.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if not pid:
            continue

        target = {"pid": pid, "project": proc.get("project", "")}
        identity = _windows_process_identity(pid)
        if (
            identity.get("query_ok")
            and identity.get("found")
            and identity.get("creation_time") is not None
        ):
            target["process_identity"] = {
                key: identity[key]
                for key in ("pid", "creation_time", "image_path", "identity_source")
                if key in identity
            }
        targets.append(target)
    return targets


def _partition_editor_close_targets(targets: list[dict], owner_pid: int) -> tuple[list[dict], list[dict]]:
    """Separate the editor receiving the Slate close request from same-project stale peers."""
    graceful_targets = [target for target in targets if int(target["pid"]) == int(owner_pid)]
    if not graceful_targets:
        raise AppError(
            "EDITOR_PROJECT_VERIFICATION_FAILED",
            f"Editor port owner PID {owner_pid} is not one of the verified project editor processes.",
            exit_code=3,
            suggestion="Retry after editor process discovery is stable; no editor request was sent.",
            details={
                "listener_pid": int(owner_pid),
                "target_pids": sorted(int(target["pid"]) for target in targets),
            },
        )
    stale_targets = [target for target in targets if int(target["pid"]) != int(owner_pid)]
    return graceful_targets, stale_targets


def _finish_partitioned_editor_close(
    project_path: str | None,
    port: int,
    target_pids: list[int],
    graceful_result: dict,
    stale_targets: list[dict],
) -> dict:
    """Close stale peers immediately, then combine all close evidence."""
    if not stale_targets:
        result = {"status": "closed", "port": port}
        result.update(graceful_result)
        result["port"] = port
        return result

    stale_result = _kill_matching_project_editors(
        project_path,
        port,
        success_message="Closed stale same-project UnrealEditor processes after the active editor exited.",
        failure_message="The active editor exited, but stale same-project UnrealEditor processes could not all be terminated.",
        expected_targets=stale_targets,
    )
    result = {
        "status": "closed" if stale_result and stale_result.get("status") == "closed" else "failed",
        "port": port,
        "method": "graceful_exit_and_stale_process_close",
        "target_pids": target_pids,
        "graceful_close": graceful_result,
        "stale_close": stale_result,
    }
    if result["status"] != "closed":
        raise AppError(
            "EDITOR_CLOSE_FAILED",
            "The active editor exited, but stale same-project UnrealEditor processes could not all be terminated.",
            exit_code=3,
            details=result,
        )
    return result


def _editor_launch_terminal_result(
    state: AppState,
    task_id: str,
    task: dict,
) -> dict:
    """Return verified success or raise for every unsuccessful terminal state."""
    progress = task_progress(task)
    status = task.get("status")
    if status == "completed":
        return progress
    if status == "timeout":
        online_result = _recover_online_launch_result(state, task_id, task)
        if online_result is not None:
            return online_result
    error = task.get("error") or {}
    defaults = {
        "failed": ("TASK_EXECUTION_FAILED", "Editor launch failed.", 3),
        "timeout": ("EDITOR_LAUNCH_TIMEOUT", "Editor launch timed out.", 4),
        "cancelled": ("EDITOR_LAUNCH_CANCELLED", "Editor launch was cancelled.", 4),
    }
    code, message, exit_code = defaults.get(
        status,
        ("EDITOR_LAUNCH_STATE_INVALID", "Editor launch ended in an invalid state.", 3),
    )
    raise AppError(
        error.get("code", code),
        error.get("message", message),
        exit_code=exit_code,
        details=progress,
    )


def _dirty_package_names(response: object, operation: str) -> list[str]:
    """Extract a verified dirty-package out parameter from Remote Control."""

    if not isinstance(response, dict):
        raise AppError(
            "EDITOR_DIRTY_STATE_UNKNOWN",
            f"{operation} returned an unrecognized response; the editor was not asked to quit.",
            exit_code=3,
            details={"operation": operation, "response": str(response)},
        )
    if response.get("error") or response.get("errorMessage"):
        message = response.get("error") or response.get("errorMessage")
        raise AppError(
            "EDITOR_DIRTY_STATE_UNKNOWN",
            f"Could not inspect dirty packages: {message}",
            exit_code=3,
            details={"operation": operation, "response": response},
        )

    value = None
    found = False
    for key, candidate in response.items():
        if str(key).replace("_", "").casefold() in {
            "outdirtypackages",
            "returnvalue",
        }:
            value = candidate
            found = True
            break
    if not found or not isinstance(value, (list, tuple)):
        raise AppError(
            "EDITOR_DIRTY_STATE_UNKNOWN",
            f"{operation} did not return a verifiable dirty-package list; the editor was not asked to quit.",
            exit_code=3,
            details={"operation": operation, "response": response},
        )

    names = []
    for item in value or []:
        if isinstance(item, dict):
            item_name = next(
                (
                    item.get(key)
                    for key in (
                        "ObjectPath",
                        "objectPath",
                        "PathName",
                        "path_name",
                        "Name",
                        "name",
                    )
                    if item.get(key)
                ),
                None,
            )
            names.append(str(item_name or item))
        else:
            names.append(str(item))
    return names


_EDITOR_LOADING_AND_SAVING_UTILS = (
    "/Script/UnrealEd.Default__EditorLoadingAndSavingUtils"
)


def _query_dirty_editor_packages(api) -> dict:
    try:
        maps = _dirty_package_names(
            api.call_function(_EDITOR_LOADING_AND_SAVING_UTILS, "GetDirtyMapPackages", {}),
            "GetDirtyMapPackages",
        )
        content = _dirty_package_names(
            api.call_function(_EDITOR_LOADING_AND_SAVING_UTILS, "GetDirtyContentPackages", {}),
            "GetDirtyContentPackages",
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            "EDITOR_DIRTY_STATE_UNKNOWN",
            "Could not inspect dirty packages; the editor was not asked to quit.",
            exit_code=3,
            suggestion=(
                "Retry when Remote Control is responsive; use editor close --force only "
                "when the request already authorizes data loss."
            ),
            details={"error": str(exc)},
        ) from exc
    return {
        "map_packages": maps,
        "content_packages": content,
        "count": len(maps) + len(content),
    }


def _call_close_save_function(api, function_name: str, parameters: dict) -> dict:
    """Call one save-stage function and require a truthful return value."""
    try:
        response = api.call_function(
            _EDITOR_LOADING_AND_SAVING_UTILS,
            function_name,
            parameters,
        )
    except Exception as exc:
        raise AppError(
            "EDITOR_SAVE_BEFORE_CLOSE_FAILED",
            f"{function_name} failed during editor close; the editor was not asked to quit.",
            exit_code=3,
            details={
                "function": function_name,
                "parameters": parameters,
                "error": str(exc),
            },
        ) from exc
    if (
        not isinstance(response, dict)
        or response.get("error")
        or response.get("errorMessage")
        or not response.get("ReturnValue")
    ):
        raise AppError(
            "EDITOR_SAVE_BEFORE_CLOSE_FAILED",
            f"{function_name} was not confirmed during editor close; the editor was not asked to quit.",
            exit_code=3,
            details={
                "function": function_name,
                "parameters": parameters,
                "response": response,
            },
        )
    return response


def _save_all_dirty_editor_packages(api) -> None:
    """Save all dirty packages and require Unreal to confirm success."""

    _call_close_save_function(
        api,
        "SaveDirtyPackages",
        {"bSaveMapPackages": True, "bSaveContentPackages": True},
    )


def _resolve_transient_dirty_map_world(api, package_path: str) -> str:
    """Resolve the active World object without guessing from its package name."""

    from cli_anything.unreal.core.script_runner import run_python_code

    current = run_python_code(
        api,
        r'''
import unreal
try:
    world = unreal.EditorLevelLibrary.get_editor_world()
except Exception:
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
if world is None:
    result = {"error": "No editor world is active."}
else:
    outermost = world.get_outermost()
    result = {
        "status": "ok",
        "world": world.get_path_name(),
        "package": outermost.get_name() if outermost else "",
        "name": world.get_name(),
    }
''',
        save=False,
    )
    if (
        not isinstance(current, dict)
        or current.get("status") != "ok"
        or not current.get("world")
        or current.get("package") != package_path
    ):
        raise AppError(
            "EDITOR_SAVE_BEFORE_CLOSE_FAILED",
            "Could not resolve the dirty transient map's live World object; the editor was not asked to quit.",
            exit_code=3,
            details={
                "function": "ResolveEditorWorld",
                "map_package": package_path,
                "response": current,
            },
        )
    return str(current["world"])


def _save_transient_dirty_maps(api, map_packages: list[str]) -> list[dict]:
    """Move dirty transient maps to deterministic project asset paths."""

    saved = []
    for package_path in map_packages:
        if not package_path.startswith("/Temp/"):
            continue
        map_name = package_path.rsplit("/", 1)[-1]
        world_path = _resolve_transient_dirty_map_world(api, package_path)
        asset_path = f"/Game/__UeCliAutoSave_{map_name}"
        _call_close_save_function(
            api,
            "SaveMap",
            {"World": world_path, "AssetPath": asset_path},
        )
        _call_close_save_function(api, "LoadMap", {"Filename": asset_path})
        saved.append({
            "map_package": package_path,
            "world": world_path,
            "asset_path": asset_path,
        })
    return saved


def _save_dirty_editor_packages_if_needed(api) -> dict:
    dirty = _query_dirty_editor_packages(api)
    transient_map_saves = _save_transient_dirty_maps(api, dirty["map_packages"])
    remaining = _query_dirty_editor_packages(api) if transient_map_saves else dirty
    if remaining["count"]:
        _save_all_dirty_editor_packages(api)
    return {
        **dirty,
        "saved_count": dirty["count"],
        "transient_map_saves": transient_map_saves,
    }


def _project_process_exit_evidence(
    project_path: str | None,
    targets: list[dict],
) -> tuple[bool, dict]:
    from cli_anything.unreal.utils.ue_backend import (
        _windows_process_exists,
        _windows_process_identity,
    )

    pid_evidence = []
    needs_project_scan = False
    for target in targets:
        pid = int(target["pid"])
        item = {"pid": pid, "project_match": False}
        expected_identity = target.get("process_identity")
        if expected_identity:
            current_identity = _windows_process_identity(pid)
            item["identity_query_ok"] = bool(current_identity.get("query_ok"))
            if current_identity.get("query_ok"):
                item["exists"] = bool(current_identity.get("found"))
                item["identity_matches"] = bool(
                    current_identity.get("found")
                    and _same_windows_process_identity(expected_identity, current_identity)
                )
                if current_identity.get("found") and not item["identity_matches"]:
                    item["pid_reused"] = True
                item["target_exited"] = not item["identity_matches"]
                pid_evidence.append(item)
                continue
            item["identity_error"] = current_identity.get("error")

        item["exists"] = _windows_process_exists(pid)
        item["target_exited"] = item["exists"] is False
        needs_project_scan = needs_project_scan or not item["target_exited"]
        pid_evidence.append(item)

    if targets and all(item["target_exited"] for item in pid_evidence):
        public_evidence = [
            {key: value for key, value in item.items() if key != "target_exited"}
            for item in pid_evidence
        ]
        return True, {"matching_pids": [], "pids": public_evidence}

    matching_pids = set()
    if needs_project_scan and project_path:
        _, matches = _find_matching_project_editors(project_path)
        for proc in matches:
            try:
                matching_pids.add(int(proc.get("pid", 0)))
            except (TypeError, ValueError):
                continue

    for item in pid_evidence:
        if item["pid"] in matching_pids:
            item["project_match"] = True
            item["exists"] = True

    public_evidence = [
        {key: value for key, value in item.items() if key != "target_exited"}
        for item in pid_evidence
    ]
    return False, {
        "matching_pids": sorted(matching_pids),
        "pids": public_evidence,
    }


def _kill_matching_project_editors(
    project_path: str | None,
    port: int,
    *,
    success_message: str,
    failure_message: str,
    expected_targets: list[dict] | None = None,
) -> dict | None:
    if not project_path and not expected_targets:
        return None

    from cli_anything.unreal.utils.ue_backend import (
        _kill_process_tree_result,
        _windows_process_exists,
        _windows_process_identity,
    )

    if expected_targets is not None:
        candidates = [dict(target) for target in expected_targets]
    else:
        _, matches = _find_matching_project_editors(project_path)
        candidates = _capture_project_editor_targets(matches)
    if not candidates:
        return None

    closed = []
    failed = []
    for proc in candidates:
        try:
            pid = int(proc.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        entry = {"pid": pid, "project": proc.get("project", "")}
        if pid:
            expected_identity = proc.get("process_identity")
            if expected_identity:
                current_identity = _windows_process_identity(pid)
                if current_identity.get("query_ok"):
                    if not current_identity.get("found"):
                        entry.update({"already_exited": True, "skipped": True})
                        closed.append(entry)
                        continue
                    if not _same_windows_process_identity(expected_identity, current_identity):
                        entry.update({"already_exited": True, "pid_reused": True, "skipped": True})
                        closed.append(entry)
                        continue
                else:
                    entry["kill_result"] = {
                        "ok": False,
                        "error": "Unable to verify the original editor process identity before termination.",
                        "identity_query": current_identity,
                        "retry_suggested": True,
                        "suggestion": (
                            "Retry editor close; do not kill this PID until its original process identity can be verified."
                        ),
                    }
                    failed.append(entry)
                    continue
            else:
                _, current_matches = _find_matching_project_editors(project_path)
                current_pids = set()
                for current in current_matches:
                    try:
                        current_pids.add(int(current.get("pid", 0)))
                    except (TypeError, ValueError):
                        continue
            if not expected_identity and pid not in current_pids:
                process_exists = _windows_process_exists(pid)
                if process_exists is False:
                    entry.update({"already_exited": True, "skipped": True})
                    closed.append(entry)
                    continue

                if process_exists is True:
                    error = "Process is still running, but its project identity no longer matches."
                else:
                    error = "Unable to verify whether the process still exists or matches the project."
                entry["kill_result"] = {
                    "ok": False,
                    "error": error,
                    "process_exists_after_rescan": process_exists,
                    "retry_suggested": True,
                    "suggestion": (
                        "Retry editor close after refreshing editor status; do not kill this PID "
                        "until it is confirmed to belong to the target project."
                    ),
                }
                failed.append(entry)
                continue
        kill_result = _kill_process_tree_result(pid) if pid else {
            "ok": False,
            "error": "Missing process id.",
            "retry_suggested": False,
        }
        expected_identity = proc.get("process_identity")
        if kill_result.get("ok") and expected_identity:
            identity_confirmation_attempts = 0
            while True:
                identity_confirmation_attempts += 1
                current_identity = _windows_process_identity(pid)
                identity_still_running = bool(
                    current_identity.get("query_ok")
                    and current_identity.get("found")
                    and _same_windows_process_identity(expected_identity, current_identity)
                )
                if not identity_still_running or identity_confirmation_attempts >= 15:
                    break
                time.sleep(0.2)
            if (
                identity_still_running
                and kill_result.get("already_exited")
                and kill_result.get("pid_state_race")
            ):
                # taskkill can report that the parent PID disappeared while an
                # earlier identity sample still observed it. Resolve that
                # narrow transition with one final native existence probe.
                final_process_exists = _windows_process_exists(pid)
                kill_result = dict(kill_result)
                kill_result["final_process_exists_after_taskkill"] = final_process_exists
                if final_process_exists is False:
                    identity_still_running = False
                    kill_result.update({
                        "ok": True,
                        "already_exited": True,
                        "process_exists_after_taskkill": False,
                        "identity_still_running": False,
                        "retry_suggested": False,
                        "suggestion": "Final process-existence probe confirmed that the editor exited.",
                    })
            if identity_still_running:
                kill_result = dict(kill_result)
                if kill_result.get("already_exited"):
                    kill_result["taskkill_reported_missing"] = True
                method = str(kill_result.get("method") or "")
                if method.endswith("_already_exited"):
                    kill_result["method"] = method[:-len("_already_exited")]
                kill_result.update({
                    "ok": False,
                    "already_exited": False,
                    "process_exists_after_taskkill": True,
                    "error": "Termination returned success, but the original editor process identity is still running.",
                    "identity_still_running": True,
                    "target_identity_verification": current_identity,
                    "identity_confirmation_attempts": identity_confirmation_attempts,
                    "identity_confirmation_seconds": round(
                        (identity_confirmation_attempts - 1) * 0.2,
                        1,
                    ),
                    "retry_suggested": True,
                    "suggestion": "Retry editor close or terminate the reported UnrealEditor PID manually.",
                })
            elif not current_identity.get("query_ok") and (
                kill_result.get("process_exists_after_taskkill") is not False
                and not kill_result.get("already_exited")
            ):
                kill_result = dict(kill_result)
                kill_result.update({
                    "ok": False,
                    "error": "Termination returned success, but final editor process identity verification failed.",
                    "target_identity_verification": current_identity,
                    "retry_suggested": True,
                    "suggestion": "Retry editor close; success requires verified exit of the original editor PID.",
                })
        if kill_result.get("ok"):
            closed.append(entry)
        else:
            entry["kill_result"] = kill_result
            failed.append(entry)

    if failed:
        result = {
            "status": "failed",
            "port": port,
            "message": failure_message,
            "failed_processes": failed,
        }
        if closed:
            result["closed_processes"] = closed
    else:
        return {
            "status": "closed",
            "port": port,
            "method": "process_tree_kill",
            "message": success_message,
            "closed_processes": closed,
        }
    suggestions = [
        item.get("kill_result", {}).get("suggestion")
        for item in failed
        if item.get("kill_result", {}).get("suggestion")
    ]
    if suggestions:
        result["suggestion"] = suggestions[0]
    retry_flags = [
        item.get("kill_result", {}).get("retry_suggested")
        for item in failed
        if "kill_result" in item
    ]
    if retry_flags:
        result["retry_suggested"] = any(bool(flag) for flag in retry_flags)
    return result


def _wait_for_project_editor_exit(
    project_path: str | None,
    port: int,
    *,
    timeout: float = 60.0,
    targets: list[dict] | None = None,
) -> dict | None:
    """Wait for same-project UnrealEditor processes to exit, then kill stale lock holders."""
    if (not project_path and not targets) or sys.platform != "win32":
        return None

    deadline = time.time() + timeout
    last_process_evidence = None
    while time.time() < deadline:
        if targets:
            confirmed_exit, process_evidence = _project_process_exit_evidence(
                project_path,
                targets,
            )
            last_process_evidence = process_evidence
            if confirmed_exit:
                return {
                    "status": "closed",
                    "method": "process_exit",
                    "target_pids": sorted(int(target["pid"]) for target in targets),
                    "pid_evidence": process_evidence["pids"],
                }
            time.sleep(1)
            continue
        _, matches = _find_matching_project_editors(project_path)
        if not matches:
            return {"status": "closed", "method": "process_exit"}
        time.sleep(1)

    kill_result = _kill_matching_project_editors(
        project_path,
        port,
        success_message="Editor API closed but UnrealEditor process still held project DLLs; terminated matching process before compile.",
        failure_message="Editor API closed but UnrealEditor process still held project DLLs and could not be terminated.",
        expected_targets=targets,
    )
    if kill_result:
        return kill_result

    # The process can exit between the timeout loop and the kill scan.
    if targets:
        confirmed_exit, process_evidence = _project_process_exit_evidence(
            project_path,
            targets,
        )
        if confirmed_exit:
            return {
                "status": "closed",
                "method": "process_exit_after_timeout_race",
                "target_pids": sorted(int(target["pid"]) for target in targets),
                "pid_evidence": process_evidence["pids"],
            }
        return {
            "status": "timeout",
            "port": port,
            "message": "Timed out waiting for the original UnrealEditor process to exit before compile.",
            "target_pids": sorted(int(target["pid"]) for target in targets),
            "last_process_evidence": process_evidence or last_process_evidence,
        }

    _, final_matches = _find_matching_project_editors(project_path)
    if not final_matches:
        return {
            "status": "closed",
            "method": "process_exit_after_timeout_race",
        }

    return {
        "status": "timeout",
        "port": port,
        "message": "Timed out waiting for matching UnrealEditor process to exit before compile.",
        "running_processes": [
            {"pid": proc.get("pid"), "project": proc.get("project", "")}
            for proc in final_matches
        ],
    }


def _extract_compile_lock_error(log_file: str | None) -> dict:
    if not log_file:
        return {}
    try:
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    match = re.search(r"LNK1104:\s*cannot open file\s+['\"]?([^'\"\r\n]+)['\"]?", text, re.IGNORECASE)
    if not match:
        return {}

    locked_file = match.group(1).strip()
    details = {
        "lock_error": "LNK1104",
        "locked_file": locked_file,
    }
    if locked_file.lower().endswith(".dll"):
        details["lock_hint"] = (
            "A running UnrealEditor or stale child process is holding this DLL. "
            "Close/kill editors for this project, then retry editor plugin-upgrade."
        )
    return details


def _require_rooted_level_path(path: str) -> str:
    normalized = str(path).strip()
    if normalized.startswith("/"):
        return normalized
    raise AppError(
        "INVALID_LEVEL_PATH",
        f"Level paths must include an Unreal mount root: {normalized or '<empty>'}",
        exit_code=2,
        suggestion="Pass a rooted package path such as /Game/Maps/Oregon_Main.",
        details={"path": normalized},
    )


def _normalize_launch_map_path(map_path: str | None, project_dir: str) -> str | None:
    if map_path is None:
        return None

    raw_path = str(map_path).strip()
    filesystem_path = Path(raw_path)
    if not filesystem_path.is_absolute():
        return _require_rooted_level_path(raw_path)

    if filesystem_path.suffix.lower() != ".umap":
        raise AppError(
            "INVALID_MAP_PATH",
            f"Absolute --map paths must name a .umap file: {raw_path}",
            exit_code=2,
            suggestion="Pass a /Game/... package path or an absolute .umap under the project Content directory.",
        )

    content_dir = (Path(project_dir) / "Content").resolve()
    try:
        relative_path = filesystem_path.resolve().relative_to(content_dir)
    except ValueError:
        raise AppError(
            "MAP_OUTSIDE_PROJECT_CONTENT",
            f"Absolute --map path is outside this project's Content directory: {raw_path}",
            exit_code=2,
            suggestion=f'Use a .umap under "{content_dir}" or pass its /Game/... package path.',
        )
    return "/Game/" + relative_path.with_suffix("").as_posix()


# Canonical launch lifecycle implementation lives below ``commands`` so task
# workers never import the Click layer. Keep these names as compatibility
# re-exports for existing callers and patch paths.
from cli_anything.unreal.core.editor_lifecycle import (  # noqa: E402
    _active_launch_task_for_project,
    _build_launch_cmd,
    _check_already_running,
    _check_log_errors,
    _remote_control_launch_error,
    _same_project_path,
)


def _launch_editor_result(
    state: AppState,
    *,
    map_path: str | None,
    no_wait: bool,
    timeout: int | None,
    extra_args: list[str],
    unattended: bool,
    no_remote: bool,
    skip_restore: bool = False,
) -> dict:
    """Submit an editor launch and return the same result used by the CLI."""
    if skip_restore and no_remote:
        raise AppError(
            "EDITOR_SKIP_RESTORE_REQUIRES_BRIDGE",
            "--skip-restore requires controlled launch with CliAnythingBridge.",
            exit_code=2,
            suggestion="Remove --no-remote, or handle Restore Packages in the editor UI.",
            details={
                "skip_restore": True,
                "no_remote": True,
                "editor_started": False,
            },
        )
    launch_error = None if no_remote else _remote_control_launch_error(extra_args)
    if launch_error:
        raise AppError(
            launch_error["code"],
            launch_error["message"],
            exit_code=2,
            suggestion=launch_error["suggestion"],
            details=launch_error["details"],
        )
    foreground_timeout, worker_timeout = _launch_wait_timeouts(timeout)
    duplicate = _check_already_running(state.session, state)
    if duplicate is not None:
        duplicate_status = duplicate.get("status")
        code = {
            "already_running": "ALREADY_RUNNING",
            "starting": "EDITOR_STARTING",
            "zombie": "EDITOR_ALREADY_RUNNING_OFFLINE",
        }.get(duplicate_status, "EDITOR_ALREADY_RUNNING")
        details = dict(duplicate)
        details.update({
            "decision": "preserve_existing_editor",
            "requires_user_input": False,
        })
        raise AppError(
            code,
            duplicate.get("message", "An editor process already exists for this project."),
            exit_code=3,
            suggestion="The existing editor was preserved. Restore its Remote Control endpoint or close it through the verified close workflow before retrying launch.",
            details=details,
        )

    payload = {
        "project_path": state.session.project_path,
        "port": None if no_remote else state.session.port,
        "map_path": map_path,
        "timeout": worker_timeout,
        "extra_args": extra_args,
        "unattended": unattended,
        "no_remote": no_remote,
        "skip_restore": skip_restore,
    }
    try:
        task = submit_task("editor.launch", payload)
    except TaskWorkerSpawnError as error:
        raise AppError(
            error.code,
            str(error),
            exit_code=3,
            suggestion="Inspect details for the Windows process-creation failure.",
            details=error.details,
        ) from error
    if no_wait:
        return {
            "task_id": task["task_id"],
            "status": "submitted",
            "suggested_poll_interval_seconds": 5,
        }

    final_task = wait_for_task(task["task_id"], foreground_timeout)
    if final_task is None:
        try:
            current = load_task(task["task_id"]) or task
        except PermissionError:
            current = task
        if current.get("status") in FINAL_TASK_STATUSES:
            return _editor_launch_terminal_result(
                state,
                task["task_id"],
                current,
            )
        progress = task_progress(current)
        progress["status"] = "launching"
        progress["foreground_wait_timeout_seconds"] = foreground_timeout
        progress["message"] = "Editor launch is still in progress; poll this task or editor status."
        progress["next_command"] = f'ue-cli --project "{state.session.project_path}" editor status {task["task_id"]}'
        return progress

    return _editor_launch_terminal_result(
        state,
        task["task_id"],
        final_task,
    )


@editor_group.command("launch")
@_project_option
@click.option(
    "--map",
    "map_path",
    default=None,
    help="Rooted level package path (/Game/...) or absolute .umap under project Content.",
)
@click.option("--no-wait", is_flag=True, default=False)
@click.option(
    "--timeout",
    default=None,
    type=int,
    help="Max seconds allotted to background editor startup; foreground wait remains bounded.",
)
@click.option(
    "--extra-arg",
    "extra_args",
    multiple=True,
    metavar="ARG",
    help="Extra UE command-line argument forwarded verbatim to UnrealEditor.exe "
         "(repeat for multiple, e.g. --extra-arg -vulkan --extra-arg -ResX=1280).",
)
@click.option(
    "--unattended/--no-unattended",
    default=False,
    help="Launch without interactive dialogs. Default: interactive editor.",
)
@click.option(
    "--no-remote",
    is_flag=True,
    default=False,
    help="Start UnrealEditor without Remote Control or CliAnythingBridge preparation/verification.",
)
@click.option(
    "--skip-restore",
    is_flag=True,
    default=False,
    help="Explicitly decline Restore Packages during startup; recovered autosaves are not loaded.",
)
@handle_error
@click.pass_obj
def editor_launch(
    state: AppState,
    project_path,
    map_path,
    no_wait,
    timeout,
    extra_args,
    unattended,
    no_remote,
    skip_restore,
):
    """Launch the editor, controlled by default.

    May update .uproject, DefaultRemoteControl.ini (UE5),
    DefaultWebRemoteControl.ini (UE4), and project CliAnythingBridge files
    when editor integration needs preparation. --no-remote explicitly skips
    those automation changes and verifies only that the editor process starts.
    """
    _load_command_project(state, project_path)
    require_project(state)
    map_path = _normalize_launch_map_path(map_path, state.session.project_dir)
    extra_args = list(extra_args) if extra_args else []
    output(
        _launch_editor_result(
            state,
            map_path=map_path,
            no_wait=no_wait,
            timeout=timeout,
            extra_args=extra_args,
            unattended=unattended,
            no_remote=no_remote,
            skip_restore=skip_restore,
        ),
        state,
    )


editor_group.add_command(editor_launch, "start")


@editor_group.command("cancel")
@click.argument("task_id")
@handle_error
@click.pass_obj
def editor_cancel(state: AppState, task_id):
    task = cancel_task(task_id)
    if task is None:
        raise AppError("TASK_NOT_FOUND", f"Task not found: {task_id}", exit_code=3)
    progress = task_progress(task)
    error = progress.get("error") or {}
    if error.get("code") == "TASK_CANCEL_FAILED":
        raise AppError(
            "TASK_CANCEL_FAILED",
            error.get("message", "Editor launch cancellation failed."),
            exit_code=4,
            suggestion="Retry cancellation after inspecting the recorded editor process diagnostics.",
            details=progress,
        )
    output(progress, state)


@editor_group.command("close")
@click.option(
    "--save-dirty",
    is_flag=True,
    default=False,
    help="Save all dirty map/content packages before closing.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Allow discarding unsaved packages and terminating offline/stale project editors.",
)
@handle_error
@click.pass_obj
def editor_close(state: AppState, save_dirty: bool, force: bool):
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

    if save_dirty and force:
        raise AppError(
            "EDITOR_CLOSE_OPTION_CONFLICT",
            "--save-dirty and --force cannot be used together.",
            exit_code=2,
        )

    ensure_inferred_project(state)
    try:
        api = require_editor(state)
        api_alive = True
    except AppError as exc:
        offline_codes = {"EDITOR_UNREACHABLE"}
        if force:
            offline_codes.update({
                "EDITOR_BLOCKED_BY_DIALOG",
                "EDITOR_PROJECT_NOT_RUNNING",
                "EDITOR_PROJECT_VERIFICATION_FAILED",
            })
        if exc.code not in offline_codes:
            raise
        api = UEEditorAPI(port=state.session.port)
        api_alive = False
    output(
        _close_editor_for_project(
            api,
            state,
            api_alive=api_alive,
            save_dirty=save_dirty,
            force=force,
        ),
        state,
    )


def _close_editor_for_project(
    api,
    state: AppState,
    *,
    api_alive: bool | None = None,
    save_dirty: bool = False,
    force: bool = False,
) -> dict:
    """Close the targeted editor using the same robust path for all callers."""
    if api_alive is None:
        api_alive = api.is_alive()
    if not api_alive:
        if state.session.project_path and sys.platform == "win32":
            try:
                _running, matches = _find_matching_project_editors(
                    state.session.project_path
                )
            except Exception as exc:
                raise AppError(
                    "EDITOR_PROJECT_VERIFICATION_FAILED",
                    "Could not verify the offline project editor process; nothing was terminated.",
                    exit_code=3,
                    details={
                        "project": state.session.project_path,
                        "port": state.session.port,
                        "error": str(exc),
                    },
                ) from exc
            if matches and not force:
                raise AppError(
                    "EDITOR_DIRTY_STATE_UNKNOWN",
                    "The project editor process is running but Remote Control is offline, so unsaved packages cannot be verified. Nothing was terminated.",
                    exit_code=3,
                    suggestion="The editor was preserved automatically. Restore Remote Control and retry; use --force only when the request already authorizes data loss.",
                    details={
                        "project": state.session.project_path,
                        "port": state.session.port,
                        "matching_pids": sorted(int(item["pid"]) for item in matches),
                        "decision": "preserve_and_leave_running",
                        "requires_user_input": False,
                    },
                )
        kill_result = _kill_matching_project_editors(
            state.session.project_path,
            state.session.port,
            success_message="Remote Control API was offline; terminated matching UnrealEditor process.",
            failure_message="Remote Control API was offline and matching UnrealEditor process could not be terminated.",
        )
        if kill_result:
            if kill_result.get("status") == "closed":
                return kill_result
            raise AppError("EDITOR_CLOSE_FAILED", kill_result["message"], exit_code=3, details=kill_result)

        return {"status": "offline", "port": state.session.port, "message": "No editor running on this port."}

    targets = []
    graceful_targets = []
    stale_targets = []
    target_pids = []
    if sys.platform == "win32":
        if state.session.project_path:
            from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

            verified_owner = _guard_editor_project(state, UEEditorAPI)
            running, matches = _find_matching_project_editors(state.session.project_path)
            if not matches:
                raise AppError(
                    "EDITOR_PROJECT_NOT_RUNNING",
                    f"Remote Control API is alive on port {state.session.port}, but no running UnrealEditor process matches this project.",
                    exit_code=3,
                    details={
                        "port": state.session.port,
                        "project": state.session.project_path,
                        "running_editors": [
                            {"pid": editor.get("pid"), "project": editor.get("project", "")}
                            for editor in running
                        ],
                    },
                )
            targets = _capture_project_editor_targets(matches)
            graceful_targets, stale_targets = _partition_editor_close_targets(
                targets,
                int(verified_owner["pid"]),
            )
            if stale_targets and not force:
                raise AppError(
                    "EDITOR_STALE_PEERS_UNSAFE",
                    "Multiple editors are running for this project, and unsaved state cannot be checked for non-listening peers. Nothing was closed.",
                    exit_code=3,
                    suggestion="All editors were preserved automatically. Resolve each verified instance separately; use --force only when the request already authorizes data loss.",
                    details={
                        "project": state.session.project_path,
                        "listener_pid": int(verified_owner["pid"]),
                        "stale_pids": sorted(int(item["pid"]) for item in stale_targets),
                        "decision": "preserve_and_leave_running",
                        "requires_user_input": False,
                    },
                )
        else:
            from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

            try:
                api_owner_pid = UEEditorAPI._get_pid_listening_on_port(state.session.port)
                api_owner_pid = int(api_owner_pid) if api_owner_pid is not None else None
            except (OSError, TypeError, ValueError):
                api_owner_pid = None
            if api_owner_pid is None:
                raise AppError(
                    "EDITOR_CLOSE_TARGET_UNKNOWN",
                    f"Remote Control API is alive on port {state.session.port}, but its owning process could not be identified.",
                    exit_code=3,
                    suggestion="Retry with --project so ue-cli can identify and verify the target editor process.",
                    details={"port": state.session.port},
                )
            targets = _capture_project_editor_targets([{
                "pid": api_owner_pid,
                "project": "",
            }])
            graceful_targets = targets
        target_pids = sorted({int(target["pid"]) for target in targets})

    if force:
        save_evidence = {"saved_count": None, "policy": "force"}
    elif save_dirty:
        save_evidence = {
            **_save_dirty_editor_packages_if_needed(api),
            "policy": "save_then_close",
        }
    else:
        dirty = _query_dirty_editor_packages(api)
        if dirty["count"]:
            raise AppError(
                "EDITOR_DIRTY_PACKAGES",
                "Editor has unsaved packages; nothing was saved or closed.",
                exit_code=3,
                suggestion=(
                    "Review the reported packages, then rerun with --save-dirty to save "
                    "them or --force only when discarding them is authorized."
                ),
                details={
                    **dirty,
                    "decision": "preserve_and_leave_running",
                },
            )
        save_evidence = {
            **dirty,
            "saved_count": 0,
            "transient_map_saves": [],
            "policy": "require_clean",
        }

    from cli_anything.unreal.core.editor_lifecycle import (
        capture_editor_disconnect_context,
        close_editor_disconnect_context,
        verify_editor_shutdown,
    )

    disconnect_context = capture_editor_disconnect_context(api, state.session.project_path)
    try:
        def close_result(result: dict) -> dict:
            result = dict(result)
            result["shutdown_evidence"] = verify_editor_shutdown(
                disconnect_context, forced=result.get("method") == "process_tree_kill",
            )
            result["save_evidence"] = save_evidence
            return result

        # MainFrame broadcasts editor close before subsystem teardown, allowing
        # asset editors to release their subsystem references safely. Bound the
        # expected HTTP disconnect while retaining process verification below.
        api.exec_console("CLOSE_SLATE_MAINFRAME", timeout=1)
        deadline = time.time() + 30
        last_process_evidence = None
        while time.time() < deadline:
            if not api.is_alive():
                drain_result = _wait_for_project_editor_exit(
                    state.session.project_path,
                    state.session.port,
                    timeout=EDITOR_CLOSE_PROCESS_GRACE_SECONDS,
                    targets=graceful_targets,
                )
                if drain_result is None:
                    drain_result = {"status": "closed", "port": state.session.port}
                if drain_result.get("status") == "closed":
                    return close_result(_finish_partitioned_editor_close(
                        state.session.project_path,
                        state.session.port,
                        target_pids,
                        drain_result,
                        stale_targets,
                    ))
                raise AppError(
                    "EDITOR_CLOSE_FAILED",
                    drain_result.get("message", "Editor API closed but UnrealEditor process did not exit."),
                    exit_code=3,
                    details=drain_result,
                )
            if graceful_targets:
                confirmed_exit, process_evidence = _project_process_exit_evidence(
                    state.session.project_path,
                    graceful_targets,
                )
                last_process_evidence = process_evidence
                if confirmed_exit:
                    graceful_result = {
                        "status": "closed",
                        "port": state.session.port,
                        "method": "project_process_exit",
                        "target_pids": sorted(int(target["pid"]) for target in graceful_targets),
                        "pid_evidence": process_evidence["pids"],
                    }
                    return close_result(_finish_partitioned_editor_close(
                        state.session.project_path,
                        state.session.port,
                        target_pids,
                        graceful_result,
                        stale_targets,
                    ))
            time.sleep(2)

        kill_result = _kill_matching_project_editors(
            state.session.project_path,
            state.session.port,
            success_message="Editor did not close gracefully within 30s; terminated matching UnrealEditor process.",
            failure_message="Editor did not close within 30s and matching UnrealEditor process could not be terminated.",
            expected_targets=targets,
        )
        if kill_result and kill_result.get("status") == "closed":
            return close_result(kill_result)

        details = kill_result or {"status": "timeout", "port": state.session.port}
        details.setdefault("stage", "wait_for_project_process_exit")
        if target_pids:
            details["target_pids"] = target_pids
            details["last_process_evidence"] = last_process_evidence
        raise AppError("EDITOR_CLOSE_TIMEOUT", "Editor did not close within 30s.", exit_code=3, details=details)
    finally:
        close_editor_disconnect_context(disconnect_context)


def _exec_console_with_log_capture(api, command: str, timeout: int = 15) -> dict:
    """Run a console command through Python so ExecutePythonCommandEx returns logs."""
    marker = f"{time.time_ns()}"
    begin = f"__ue_cli_exec_begin__:{marker}"
    end = f"__ue_cli_exec_end__:{marker}"
    automation_setup = ""
    if re.match(r"^\s*Automation(?:\s|$)", command, re.IGNORECASE):
        automation_setup = """
    _settings_class = unreal.load_class(None, "/Script/Engine.AutomationTestSettings")
    if _settings_class is None:
        raise RuntimeError("AutomationTestSettings class is unavailable")
    _settings = unreal.get_default_object(_settings_class)
    try:
        _settings.set_editor_property("DefaultInteractiveFramerate", 1.0)
    except Exception as _settings_error:
        _settings_error_text = str(_settings_error)
        if (
            "Failed to find property" not in _settings_error_text
            or "DefaultInteractiveFramerate" not in _settings_error_text
        ):
            raise
        unreal.log_warning(
            "ue-cli: DefaultInteractiveFramerate is unavailable; continuing "
            "without the automation FPS override."
        )
"""
    script = f"""
import unreal
_cmd = {json.dumps(command)}
_begin = {json.dumps(begin)}
_end = {json.dumps(end)}
_world = None
try:
    _world = unreal.EditorLevelLibrary.get_editor_world()
except Exception:
    try:
        _world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    except Exception:
        _world = None
unreal.log(_begin)
try:
{automation_setup}
    unreal.SystemLibrary.execute_console_command(_world, _cmd)
finally:
    unreal.log(_end)
"""
    result = api.exec_python_ex(script, timeout=timeout)
    if result.get("error") or result.get("ReturnValue") is False:
        return {
            "error": result.get("error") or "Python console execution failed",
            "raw": result,
            "_log_begin_marker": begin,
            "_log_end_marker": end,
        }

    captured: list[dict] = []
    inside = False
    saw_begin = False
    saw_end = False
    for item in result.get("LogOutput", []) or []:
        line = str(item.get("Output", ""))
        if "__ue_cli_exec_begin__:" in line:
            inside = True
            saw_begin = True
            continue
        if "__ue_cli_exec_end__:" in line:
            inside = False
            saw_end = True
            continue
        if inside:
            captured.append(item)

    if not saw_begin and not saw_end:
        captured = list(result.get("LogOutput", []) or [])

    log_text = "\n".join(str(item.get("Output", "")) for item in captured)
    return {
        "status": "executed",
        "command": command,
        "capture_mode": "python_log_output",
        "log_output": captured,
        "log_text": log_text,
        "_log_begin_marker": begin,
        "_log_end_marker": end,
    }


def _automation_inline_log_entry(item: dict) -> bool:
    return bool(
        _AUTOMATION_INLINE_LOG_PATTERN.search(str(item.get("Output", "")))
    )


def _bound_editor_exec_logs(
    result: dict,
    log_file: Path | None,
    omitted_line_count: int,
) -> None:
    """Keep one bounded inline log suffix; full diagnostics stay in ``log_file``."""
    raw_entries = list(result.get("log_output") or [])
    entries = [item for item in raw_entries if isinstance(item, dict)]
    omitted_line_count += len(raw_entries) - len(entries)

    kept_reversed: list[dict] = []
    used_bytes = 0
    for index in range(len(entries) - 1, -1, -1):
        item = entries[index]
        line_bytes = len(
            str(item.get("Output", "")).encode("utf-8", errors="replace")
        ) + 1
        if used_bytes + line_bytes > EDITOR_EXEC_INLINE_LOG_LIMIT_BYTES:
            omitted_line_count += index + 1
            break
        kept_reversed.append(item)
        used_bytes += line_bytes

    kept = list(reversed(kept_reversed))
    result["log_output"] = kept
    result["log_text"] = "\n".join(
        str(item.get("Output", "")) for item in kept
    )
    result["omitted_line_count"] = omitted_line_count
    if log_file:
        result["log_file"] = str(log_file)


def _editor_exec_request_failure(result: dict) -> dict | None:
    """Return structured request failure details from an exec result."""
    if not isinstance(result, dict):
        return None
    raw = result.get("raw")
    if isinstance(raw, dict) and raw.get("error"):
        return raw
    if result.get("error") and "raw" not in result:
        return result
    return None


def _can_safely_fallback_editor_exec(result: dict) -> bool:
    """Only retry when Remote Control explicitly rejected the first request."""
    failure = _editor_exec_request_failure(result)
    if not failure:
        return False
    if failure.get("error_type") == "HTTPError":
        return failure.get("status_code") in {400, 404}
    return bool(re.search(r"\b(?:400|404) Client Error\b", str(failure.get("error", ""))))


def _raise_editor_exec_failure(
    result: dict,
    command: str,
    timeout: int,
    *,
    dispatch_mode: str,
    fallback_attempted: bool,
    observation_task: dict | None = None,
) -> None:
    """Report failed or ambiguous dispatch without repeating the command."""
    request_failure = _editor_exec_request_failure(result)
    delivery_unknown = request_failure is not None
    observed_result = dict((observation_task or {}).get("result") or {})
    delivery_state = observed_result.get("delivery_state") or (
        "unknown" if delivery_unknown else "attempted"
    )
    completion_state = observed_result.get("completion_state") or (
        "unknown" if delivery_unknown else "failed"
    )
    details = {
        "status": (
            str(observation_task.get("status"))
            if observation_task
            else ("unknown" if delivery_unknown else "failed")
        ),
        "command": command,
        "dispatch_mode": dispatch_mode,
        "delivery_state": delivery_state,
        "completion_state": completion_state,
        "fallback_attempted": fallback_attempted,
        "timeout_seconds": timeout,
        "error": result.get("error") or "Console command execution failed",
    }
    if request_failure:
        details["request_error"] = request_failure
    if observation_task:
        task_id = observation_task["task_id"]
        details.update({
            "task_id": task_id,
            "task_status": observation_task.get("status"),
            "next_command": f"ue-cli task status {task_id}",
            "wait_command": f"ue-cli task wait {task_id} --timeout {max(timeout, 60)}",
            "suggested_poll_interval_seconds": observation_task.get(
                "suggested_poll_interval_seconds",
                5,
            ),
        })

    if delivery_unknown:
        if delivery_state == "confirmed" and completion_state == "running":
            raise AppError(
                "EDITOR_EXEC_IN_PROGRESS",
                "Editor command delivery is confirmed and execution is still running.",
                exit_code=4,
                suggestion=(
                    f"Poll `{details['next_command']}`. Do not resend the command."
                ),
                details=details,
            )
        raise AppError(
            "EDITOR_EXEC_DELIVERY_UNKNOWN",
            "Editor command delivery or completion is unknown; ue-cli did not retry it.",
            exit_code=3,
            suggestion=(
                (
                    f"Poll `{details['next_command']}` without redispatching the command."
                    if observation_task
                    else "Inspect the editor state and project log before deciding whether to retry. "
                    "The command may already have run, especially if it is non-idempotent."
                )
            ),
            details=details,
        )

    raise AppError(
        "EDITOR_EXEC_FAILED",
        "Editor command execution failed; ue-cli did not retry it.",
        exit_code=3,
        suggestion="Inspect the command error and editor log before retrying.",
        details=details,
    )


def _resolve_editor_log_file(state: AppState) -> Path | None:
    """Find the active editor log file for the current project/port."""
    project_path = getattr(state.session, "project_path", None)
    project_dir = state.session.project_dir
    if not project_dir:
        try:
            from cli_anything.unreal.utils.ue_backend import find_running_editors
            from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

            pid = UEEditorAPI._get_pid_listening_on_port(state.session.port)
            for editor in find_running_editors():
                if pid and int(editor.get("pid", 0)) == int(pid):
                    project = editor.get("project")
                    if project:
                        project_path = str(project)
                        project_dir = str(Path(project).parent)
                        break
        except Exception:
            project_dir = None

    if not project_dir:
        return None

    log_dir = Path(project_dir) / "Saved" / "Logs"
    if not log_dir.is_dir():
        return None

    if project_path:
        project_log = log_dir / f"{Path(project_path).stem}.log"
        if project_log.exists():
            return project_log

    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _read_log_delta(
    log_file: Path | None,
    start_pos: int,
    wait_seconds: float = 1.0,
    begin_marker: str | None = None,
    end_marker: str | None = None,
    completion_markers: tuple[str, ...] = (),
) -> str:
    if not log_file:
        return ""

    from cli_anything.unreal.utils.ue_http_api import _decode_windows_command_output

    deadline = time.time() + max(wait_seconds, 0.0)
    data = b""
    last_size: int | None = None
    stable_since: float | None = None
    scan_pos = start_pos
    marker_overlap = b""
    completion_tokens = list(completion_markers)
    if begin_marker and end_marker:
        completion_tokens.append(end_marker)
    completion_bytes = tuple(
        marker.encode("utf-8").lower()
        for marker in completion_tokens
    )
    max_completion_bytes = max((len(marker) for marker in completion_bytes), default=0)
    marker_overlap_size = max(max_completion_bytes - 1, 0)
    while True:
        completion_found = False
        try:
            size = log_file.stat().st_size
            if completion_bytes:
                if size < scan_pos:
                    scan_pos = 0
                    data = b""
                    marker_overlap = b""

                with log_file.open("rb") as fh:
                    fh.seek(scan_pos)
                    while True:
                        chunk = fh.read(EDITOR_LOG_READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        scan_pos += len(chunk)
                        data = (data + chunk)[-EDITOR_LOG_CAPTURE_LIMIT_BYTES:]

                        lowered = marker_overlap + chunk.lower()
                        completion_found = any(marker in lowered for marker in completion_bytes)
                        marker_overlap = (
                            lowered[-marker_overlap_size:] if marker_overlap_size else b""
                        )
                        if completion_found:
                            break
            else:
                offset = start_pos if size >= start_pos else 0
                with log_file.open("rb") as fh:
                    fh.seek(offset)
                    data = fh.read(EDITOR_LOG_CAPTURE_LIMIT_BYTES)
        except Exception:
            data = b""
            size = 0

        now = time.time()
        if data:
            if completion_bytes:
                if completion_found:
                    break
            else:
                if size == last_size:
                    if stable_since is None:
                        stable_since = now
                    if (now - stable_since) >= 0.2 or now >= deadline:
                        break
                else:
                    last_size = size
                    stable_since = now

        if now >= deadline:
            break
        time.sleep(0.1)

    text = _decode_windows_command_output(data)
    lines = text.splitlines()
    if begin_marker and end_marker:
        captured: list[str] = []
        inside = False
        saw_begin = False
        for line in lines:
            if begin_marker in line:
                inside = True
                saw_begin = True
                continue
            if end_marker in line:
                break
            if inside:
                captured.append(line)
        if saw_begin:
            lines = captured
        else:
            return ""
    else:
        lines = [
            line
            for line in lines
            if "__ue_cli_exec_begin__:" not in line and "__ue_cli_exec_end__:" not in line
        ]
    return "\n".join(lines).strip()


def _is_python_console_command(command: str) -> bool:
    """Return whether *command* runs Python through UE's ``py`` console command."""
    return bool(re.match(r"\s*py(?:\s|$)", command, re.IGNORECASE))


def _python_console_error_lines(command: str, result: dict) -> list[str]:
    """Collect synchronous Python errors captured for one console command."""
    if not _is_python_console_command(command):
        return []

    errors: list[str] = []
    file_error_pattern = re.compile(
        r"^\s*(?:\[[^\]\r\n]*\]\s*)*LogPython:\s*Error(?:\s*:|$)",
        re.IGNORECASE,
    )
    for item in result.get("log_output", []) or []:
        if not isinstance(item, dict):
            continue
        line = str(item.get("Output", "")).strip()
        if not line:
            continue
        is_error_entry = str(item.get("Type", "")).lower() == "error"
        is_python_file_error = bool(file_error_pattern.match(line))
        if (is_error_entry or is_python_file_error) and line not in errors:
            errors.append(line)
    return errors


def _is_live_coding_compile(command: str) -> bool:
    """Return whether *command* starts UE's out-of-process Live Coding compile."""
    return bool(re.fullmatch(r"\s*LiveCoding\.Compile\s*", command, re.IGNORECASE))


def _deterministic_compile_commands(state: AppState) -> list[str]:
    project_path = getattr(state.session, "project_path", None)
    project_arg = f'"{project_path}"' if project_path else "<path-to-.uproject>"
    prefix = f"ue-cli --project {project_arg}"
    return [
        f"{prefix} editor close",
        f"{prefix} build compile",
    ]


@editor_group.command("live-coding-compile")
@click.option(
    "--timeout",
    default=600,
    type=click.IntRange(min=1),
    help="Max seconds to wait for the UE5 Live Coding terminal result.",
)
@click.option(
    "--log-wait",
    default=3.0,
    type=click.FloatRange(min=0.0),
    help="Max seconds to wait for the terminal result to flush to the project log.",
)
@handle_error
@click.pass_obj
def editor_live_coding_compile(state: AppState, timeout: int, log_wait: float):
    """Compile through UE5 Live Coding and wait for its terminal result."""
    from cli_anything.unreal.core.editor_lifecycle import (
        capture_editor_disconnect_context,
        close_editor_disconnect_context,
        collect_editor_disconnect_diagnostics,
    )
    from cli_anything.unreal.core.live_coding import (
        finalize_live_coding_compile_result,
        get_live_coding_sync_support,
        raise_live_coding_request_failure,
    )

    ensure_inferred_project(state)
    support = get_live_coding_sync_support(state.session.engine_root)
    if support["supported"] is False:
        raise AppError(
            "LIVECODING_SYNC_UNSUPPORTED",
            "Synchronous Live Coding result observation is unavailable for this engine.",
            exit_code=3,
            suggestion=(
                "Use Unreal Engine 5 on Windows, or close the editor and run "
                "`ue-cli --project <path-to.uproject> build compile`."
            ),
            details=support,
        )

    api = require_editor(state)
    log_file = _resolve_editor_log_file(state)
    log_start = log_file.stat().st_size if log_file and log_file.exists() else 0
    disconnect_context = capture_editor_disconnect_context(api, state.session.project_path)
    started_at = time.monotonic()
    try:
        result = _exec_console_with_log_capture(
            api,
            "LiveCoding.CompileSync",
            timeout=timeout,
        )
        duration_seconds = time.monotonic() - started_at
        if result.get("error"):
            diagnostics = collect_editor_disconnect_diagnostics(disconnect_context)
            raise_live_coding_request_failure(
                result,
                timeout=timeout,
                duration_seconds=duration_seconds,
                support=support,
                diagnostics=diagnostics,
            )

        log_begin_marker = result.pop("_log_begin_marker", None)
        log_end_marker = result.pop("_log_end_marker", None)
        file_log_text = ""
        if log_begin_marker and log_end_marker:
            file_log_text = _read_log_delta(
                log_file,
                log_start,
                wait_seconds=log_wait,
                begin_marker=log_begin_marker,
                end_marker=log_end_marker,
            )
        if file_log_text:
            result["log_file"] = str(log_file)
            existing_output = list(result.get("log_output") or [])
            seen_output = {
                str(item.get("Output", ""))
                for item in existing_output
                if isinstance(item, dict)
            }
            result["log_output"] = existing_output + [
                {
                    "Type": "Info",
                    "Output": line,
                    "Source": "editor_log_file",
                }
                for line in file_log_text.splitlines()
                if line and line not in seen_output
            ]
            result["log_text"] = "\n".join(
                str(item.get("Output", ""))
                for item in result["log_output"]
                if isinstance(item, dict)
            )
            result["capture_mode"] = "editor_log_file"

        _bound_editor_exec_logs(result, log_file, 0)
        result = finalize_live_coding_compile_result(
            result,
            duration_seconds=duration_seconds,
            support=support,
        )
        output(result, state)
    finally:
        close_editor_disconnect_context(disconnect_context)


@editor_group.command("exec")
@click.option(
    "--timeout",
    default=15,
    show_default=True,
    type=click.IntRange(min=1),
    help="Max seconds to wait for synchronous command dispatch and captured output.",
)
@click.option(
    "--log-wait",
    default=None,
    type=click.FloatRange(min=0.0),
    help=(
        "Max seconds to collect project Output Log lines. Defaults to 300 for "
        "Automation RunTests and 1 for other commands; cannot observe separate-process logs."
    ),
)
@click.argument("command")
@handle_error
@click.pass_obj
def editor_exec(state: AppState, timeout, log_wait, command):
    """Execute a console command in the editor.

    Sends a UE console command directly (e.g. stat unit, renderdoc.captureframe).
    For Python execution, use ``editor run-script -c "code"`` for one-liners
    or ``editor run-script -`` for stdin.
    """
    automation_run = bool(
        re.match(r"^\s*Automation\s+RunTests(?:\s|$)", command, re.IGNORECASE)
    )
    if log_wait is None:
        log_wait = (
            DEFAULT_AUTOMATION_LOG_WAIT_SECONDS
            if automation_run
            else DEFAULT_EDITOR_EXEC_LOG_WAIT_SECONDS
        )

    api = require_editor(state)
    live_coding_compile = _is_live_coding_compile(command)
    log_file = _resolve_editor_log_file(state)
    log_start = log_file.stat().st_size if log_file and log_file.exists() else 0

    result = _exec_console_with_log_capture(api, command, timeout=timeout)
    log_begin_marker = result.pop("_log_begin_marker", None)
    log_end_marker = result.pop("_log_end_marker", None)
    if result.get("error"):
        if not _can_safely_fallback_editor_exec(result):
            observation_task = None
            if (
                _editor_exec_request_failure(result)
                and log_begin_marker
                and log_end_marker
                and log_file
            ):
                from cli_anything.unreal.core.editor_exec import (
                    create_editor_exec_observation,
                )

                observation_task = create_editor_exec_observation(
                    command=command,
                    project_path=state.session.project_path,
                    log_file=log_file,
                    log_start=log_start,
                    begin_marker=log_begin_marker,
                    end_marker=log_end_marker,
                )
                if observation_task.get("status") == "completed":
                    observed = dict(observation_task.get("result") or {})
                    observed.update({
                        "task_id": observation_task["task_id"],
                        "capture_mode": "editor_log_file",
                        "transport_timeout_recovered": True,
                        "timeout_seconds": timeout,
                    })
                    output(observed, state)
                    return
                if observation_task.get("status") == "failed":
                    observed = dict(observation_task.get("result") or {})
                    observed["task_id"] = observation_task["task_id"]
                    task_error = observation_task.get("error") or {}
                    raise AppError(
                        str(task_error.get("code") or "EDITOR_EXEC_FAILED"),
                        str(
                            task_error.get("message")
                            or "Editor command failed after its transport response timed out."
                        ),
                        exit_code=3,
                        suggestion=(
                            "Fix the captured command error before retrying; the command "
                            "was not redispatched."
                        ),
                        details=observed,
                    )
            _raise_editor_exec_failure(
                result,
                command,
                timeout,
                dispatch_mode="python_log_output",
                fallback_attempted=False,
                observation_task=observation_task,
            )
        log_begin_marker = None
        log_end_marker = None
        result = api.exec_console(command, timeout=timeout)
        if not isinstance(result, dict):
            result = {"raw": result}
        if result.get("error"):
            _raise_editor_exec_failure(
                result,
                command,
                timeout,
                dispatch_mode="remote_console",
                fallback_attempted=True,
            )
        if result:
            result.setdefault("capture_mode", "remote_console")

    if "error" in result and "400" in str(result["error"]):
        result["hint"] = (
            "Console command execution may be disabled in Remote Control settings. "
            "Run: ue-cli editor enable-remote"
        )
    elif not result or result == {}:
        result = {
            "status": "executed",
            "command": command,
            "capture_mode": "remote_console",
            "note": "Command executed. Console output is not captured by Remote Control API. "
                    "Check editor Output Log for results.",
        }

    omitted_line_count = 0
    if automation_run:
        immediate_output = list(result.get("log_output") or [])
        focused_output = [
            item
            for item in immediate_output
            if isinstance(item, dict) and _automation_inline_log_entry(item)
        ]
        omitted_line_count += len(immediate_output) - len(focused_output)
        result["log_output"] = focused_output

    file_log_text = ""
    if automation_run:
        file_log_text = _read_log_delta(
            log_file,
            log_start,
            wait_seconds=log_wait,
            completion_markers=("Automation Test Queue Empty",),
        )
    elif log_begin_marker and log_end_marker:
        file_log_text = _read_log_delta(
            log_file,
            log_start,
            wait_seconds=log_wait,
            begin_marker=log_begin_marker,
            end_marker=log_end_marker,
        )
    if file_log_text:
        result["log_file"] = str(log_file)
        existing_output = list(result.get("log_output") or [])
        seen_output = {
            str(item.get("Output", ""))
            for item in existing_output
            if isinstance(item, dict)
        }
        file_lines = [line for line in file_log_text.splitlines() if line]
        if automation_run:
            focused_file_lines = [
                line
                for line in file_lines
                if _AUTOMATION_INLINE_LOG_PATTERN.search(line)
            ]
            omitted_line_count += len(file_lines) - len(focused_file_lines)
            file_lines = focused_file_lines
        result["log_output"] = existing_output + [
            {
                "Type": "Info",
                "Output": line,
                "Source": "editor_log_file",
            }
            for line in file_lines
            if line not in seen_output
        ]
        if not result.get("log_text"):
            result["log_text"] = file_log_text
            result["capture_mode"] = "editor_log_file"

    if automation_run:
        automation_completed = (
            "automation test queue empty" in file_log_text.lower()
        )
        result["automation_completed"] = automation_completed
        result["log_capture_status"] = (
            "completed" if automation_completed else "timeout"
        )
        result["log_wait_seconds"] = log_wait
        if not automation_completed:
            result["suggestion"] = (
                "Automation did not emit 'Automation Test Queue Empty' within "
                "--log-wait. Inspect the project log or retry with a longer wait."
            )
            if log_file:
                escaped_log = str(log_file).replace("'", "''")
                result["log_file"] = str(log_file)
                result["next_command"] = (
                    "powershell -NoProfile -Command "
                    f"\"Get-Content -LiteralPath '{escaped_log}' -Tail 200\""
                )

    python_errors = _python_console_error_lines(command, result)
    if python_errors:
        result["status"] = "failed"
        result["python_error"] = "\n".join(python_errors)
        _bound_editor_exec_logs(result, log_file, omitted_line_count)
        raise AppError(
            "PYTHON_EXEC_FAILED",
            "Unreal Python command raised a synchronous exception.",
            exit_code=3,
            suggestion=(
                "Fix the Python error and retry. For structured Python execution, use "
                "`editor run-script -c \"...\"`."
            ),
            details=result,
        )

    if live_coding_compile and not result.get("error"):
        next_commands = _deterministic_compile_commands(state)
        result.update({
            "status": "submitted",
            "completion_observable": False,
            "completion_status": "unknown",
            "log_capture_status": "unobservable",
            "log_wait_seconds": log_wait,
            "next_commands": next_commands,
        })
        _bound_editor_exec_logs(result, log_file, omitted_line_count)
        raise AppError(
            "LIVECODING_RESULT_UNOBSERVABLE",
            "LiveCoding.Compile was submitted, but editor exec cannot observe the "
            "asynchronous LiveCodingConsole completion result; compile success is unknown.",
            exit_code=3,
            suggestion=(
                "Do not treat this command as a successful compile. For a deterministic "
                f"result, run `{next_commands[0]}` and then `{next_commands[1]}`; otherwise "
                "inspect the separate LiveCodingConsole manually."
            ),
            details=result,
        )

    if automation_run and not result.get("automation_completed"):
        _bound_editor_exec_logs(result, log_file, omitted_line_count)
        raise AppError(
            "AUTOMATION_RESULT_TIMEOUT",
            "Automation completion was not observed before --log-wait expired.",
            exit_code=4,
            suggestion=result.get("suggestion"),
            details=result,
        )

    _bound_editor_exec_logs(result, log_file, omitted_line_count)
    output(result, state)


def _is_transport_disconnect_result(result: dict) -> bool:
    """Return True when a command failed because the editor connection dropped."""
    if not isinstance(result, dict) or result.get("error_type"):
        return False
    text = " ".join(str(result.get(key, "")) for key in ("error", "traceback"))
    markers = (
        "ConnectionResetError",
        "Connection aborted",
        "RemoteDisconnected",
        "Connection refused",
        "forcibly closed",
        "WinError 10054",
        "10054",
    )
    return any(marker in text for marker in markers)


def _is_transport_timeout_result(result: dict) -> bool:
    """Return True when a command timed out waiting for an editor HTTP response."""
    if not isinstance(result, dict) or result.get("error_type"):
        return False
    text = " ".join(str(result.get(key, "")) for key in ("error", "traceback")).lower()
    markers = (
        "read timed out",
        "read timeout",
        "readtimeout",
        "timed out",
        "timeouterror",
    )
    return any(marker in text for marker in markers)


def _raise_editor_connection_lost(
    result: dict,
    operation: str,
    *,
    diagnostics: dict | None = None,
) -> None:
    details = dict(result)
    details["failure_kind"] = "transport_disconnect"
    details["operation"] = operation
    if diagnostics:
        diagnostic_failure_kind = diagnostics.get("failure_kind")
        if diagnostic_failure_kind and diagnostic_failure_kind != "transport_disconnect":
            details["transport_failure_kind"] = "transport_disconnect"
        details.update(diagnostics)
    if operation == "editor run-script":
        details["delivery_state"] = "unknown"
        details["retry_safe"] = False
    if details.get("failure_kind") in {"editor_process_exited", "editor_crash_detected"}:
        suggestion = (
            "Inspect fatal_log_tail and log_file, then fix the Unreal Engine or project crash cause and relaunch the editor. "
            "The script may have partially executed; assess side effects before any retry."
        )
    else:
        suggestion = (
            "Run editor status to check whether Unreal Editor crashed, exited, or is restarting; "
            "relaunch if offline, then assess possible side effects before retrying."
        )
    raise AppError(
        "EDITOR_CONNECTION_LOST",
        f"Editor connection was lost while running {operation}.",
        exit_code=3,
        details=details,
        suggestion=suggestion,
    )


def _format_cli_command(parts: list[str]) -> str:
    if sys.platform == "win32":
        return sp.list2cmdline(parts)
    return shlex.join(parts)


def _editor_run_script_retry_command(
    state: AppState,
    script_path: str | None,
    *,
    timeout: int,
    no_save: bool,
) -> str | None:
    if not script_path or script_path == "-":
        return None

    parts = ["ue-cli", "--output", state.output_mode]
    if state.session.project_path:
        parts.extend(["--project", str(Path(state.session.project_path).resolve())])
    if state.port_is_explicit:
        parts.extend(["--port", str(state.session.port)])
    parts.extend(["editor", "run-script", "--timeout", str(timeout)])
    if no_save:
        parts.append("--no-save")
    parts.append(str(Path(script_path).resolve()))
    return _format_cli_command(parts)


def _raise_editor_script_timeout(
    result: dict,
    operation: str,
    timeout: int,
    *,
    retry_timeout: int,
    retry_command: str | None,
    observation_task: dict | None = None,
) -> None:
    details = dict(result)
    details["failure_kind"] = "transport_timeout"
    details["operation"] = operation
    details["timeout_seconds"] = timeout
    observed_result = dict((observation_task or {}).get("result") or {})
    details["delivery_state"] = observed_result.get("delivery_state", "unknown")
    details["completion_state"] = observed_result.get("completion_state", "unknown")
    details["retry_timeout_seconds"] = retry_timeout
    if retry_command:
        details["retry_command"] = retry_command

    if observation_task:
        task_id = observation_task["task_id"]
        details.update({
            "task_id": task_id,
            "task_status": observation_task.get("status"),
            "phase": observation_task.get("phase"),
            "next_command": f"ue-cli task status {task_id}",
            "wait_command": f"ue-cli task wait {task_id} --timeout {max(timeout, 60)}",
            "suggested_poll_interval_seconds": observation_task.get(
                "suggested_poll_interval_seconds",
                5,
            ),
            "log_file": observed_result.get("log_file"),
        })

    if (
        observation_task
        and details["delivery_state"] == "confirmed"
        and details["completion_state"] == "running"
    ):
        raise AppError(
            "EDITOR_SCRIPT_IN_PROGRESS",
            "Editor script delivery is confirmed and execution is still running.",
            exit_code=4,
            details=details,
            suggestion=(
                f"Poll `{details['next_command']}` or wait with `{details['wait_command']}`. "
                "Do not resend the script while this observation remains non-terminal."
            ),
        )

    retry_guidance = (
        f"After verifying it is safe to repeat, retry with: {retry_command}"
        if retry_command
        else (
            "After verifying it is safe to repeat, rerun the original editor run-script "
            f"command with --timeout {retry_timeout}."
        )
    )
    raise AppError(
        "EDITOR_SCRIPT_TIMEOUT",
        f"Editor script timed out after {timeout} seconds without returning a result.",
        exit_code=3,
        details=details,
        suggestion=(
            (
                f"Poll `{details['next_command']}` without redispatching the script."
                if observation_task
                else "Run editor status and inspect the project Output Log because the previous script may still "
                f"complete in-editor after the HTTP timeout. {retry_guidance}"
            )
        ),
    )


def _raise_level_command_failed(result: dict, operation: str, code: str) -> None:
    resolved_code = str(result.get("code") or code)
    expected_rejection = (
        result.get("dispatch_state") in {
            "blocked_unsafe",
            "blocked_dirty",
            "blocked_precondition",
        }
        or result.get("failure_kind") == "unsupported_engine_version"
    )
    exit_code = 2 if expected_rejection else 3
    raise AppError(
        resolved_code,
        str(result.get("error") or f"{operation} failed."),
        exit_code=exit_code,
        details=result,
        suggestion=(
            result.get("suggestion")
            or "Run editor status, verify the active editor world, then retry the level command after the editor is online."
        ),
    )


def _unsafe_run_script_operation(code: str) -> dict | None:
    """Detect UE Python operations known to crash unattended editor sessions."""
    if not code:
        return None

    operation = _unsafe_top_level_call(code)
    if operation:
        details = {
            "operation": operation,
            "reason": "known_world_teardown_crash",
            "safe_workflow": [
                "ue-cli --project <Project.uproject> editor new-level /Game/Path/Level",
                "ue-cli --project <Project.uproject> editor open-level /Game/Path/Level",
                "ue-cli --project <Project.uproject> editor run-script <actor_setup.py>",
                "ue-cli --project <Project.uproject> editor save-level",
            ],
        }
        if operation == "EditorLoadingAndSavingUtils.new_blank_map":
            details["safe_workflow"] = [
                "ue-cli --project <Project.uproject> editor new-blank-level",
                "ue-cli --project <Project.uproject> editor run-script --no-save <actor_setup.py>",
            ]
        if operation == "EditorLevelLibrary.load_level":
            details["duplicate_world_workflow"] = [
                "ue-cli --project <Project.uproject> editor run-script <duplicate_and_save_only.py>",
                "ue-cli --project <Project.uproject> editor close",
                "ue-cli --project <Project.uproject> editor launch --map /Game/Path/Level",
                "ue-cli --project <Project.uproject> editor run-script <actor_setup.py>",
            ]
            details["duplicate_world_requirement"] = (
                "duplicate_and_save_only.py must save the duplicated World without calling a map-load API."
            )
        return details
    for match in re.finditer(r"open\s*\(\s*r?[\"']([^\"']+\.py)[\"']", code):
        source_path = Path(match.group(1))
        if not source_path.is_file():
            continue
        try:
            nested_code = source_path.read_text(encoding="utf-8")
        except OSError:
            continue
        unsafe = _unsafe_run_script_operation(nested_code)
        if unsafe:
            unsafe = dict(unsafe)
            unsafe["source_path"] = str(source_path)
            return unsafe
    return None


def _unsafe_top_level_call(code: str) -> str | None:
    """Find top-level map transition calls while allowing reusable helper defs."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        patterns = (
            (r"\bEditorLevelLibrary\s*\.\s*load_level\s*\(", "EditorLevelLibrary.load_level"),
            (r"\bload_level\s*\(", "EditorLevelLibrary.load_level"),
            (r"\bEditorLoadingAndSavingUtils\s*\.\s*load_map\s*\(", "EditorLoadingAndSavingUtils.load_map"),
            (r"\bload_map\s*\(", "EditorLoadingAndSavingUtils.load_map"),
            (r"\bEditorLoadingAndSavingUtils\s*\.\s*new_blank_map\s*\(", "EditorLoadingAndSavingUtils.new_blank_map"),
            (r"\bnew_blank_map\s*\(", "EditorLoadingAndSavingUtils.new_blank_map"),
        )
        for pattern, operation in patterns:
            if re.search(pattern, code):
                return operation
        return None

    function_defs = {
        statement.name: statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    class Visitor(ast.NodeVisitor):
        found: str | None = None

        def __init__(self):
            self._visiting_functions: set[str] = set()

        def visit_FunctionDef(self, node):  # noqa: N802
            return

        def visit_AsyncFunctionDef(self, node):  # noqa: N802
            return

        def visit_ClassDef(self, node):  # noqa: N802
            return

        def visit_Lambda(self, node):  # noqa: N802
            return

        def visit_Call(self, node):  # noqa: N802
            name = _call_name(node.func)
            if name in {"load_map", "EditorLoadingAndSavingUtils.load_map", "unreal.EditorLoadingAndSavingUtils.load_map"}:
                self.found = "EditorLoadingAndSavingUtils.load_map"
                return
            if name in {"load_level", "EditorLevelLibrary.load_level", "unreal.EditorLevelLibrary.load_level"}:
                self.found = "EditorLevelLibrary.load_level"
                return
            if name in {"new_blank_map", "EditorLoadingAndSavingUtils.new_blank_map", "unreal.EditorLoadingAndSavingUtils.new_blank_map"}:
                self.found = "EditorLoadingAndSavingUtils.new_blank_map"
                return
            if name in function_defs and name not in self._visiting_functions:
                self._visiting_functions.add(name)
                try:
                    for statement in function_defs[name].body:
                        if self.found:
                            return
                        self.visit(statement)
                finally:
                    self._visiting_functions.discard(name)
                if self.found:
                    return
            self.generic_visit(node)

    visitor = Visitor()
    for statement in tree.body:
        if visitor.found:
            break
        visitor.visit(statement)
    return visitor.found


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _raise_unsafe_run_script_operation(unsafe: dict) -> None:
    suggestion = (
        "Use editor new-level or editor open-level for map transitions, then run a separate script "
        "for actor/content setup and save with editor save-level."
    )
    if unsafe["operation"] == "EditorLevelLibrary.load_level":
        suggestion = (
            "For an ordinary transition, use editor open-level outside run-script. If the script duplicated "
            "the active World, duplicate and save it without loading it, run editor close, then start a fresh "
            "editor with editor launch --map before continuing setup."
        )
    elif unsafe["operation"] == "EditorLoadingAndSavingUtils.new_blank_map":
        suggestion = (
            "Use editor new-blank-level outside run-script, then run the setup script separately. "
            "The next run-script automatically uses the active transient editor world."
        )
    raise AppError(
        "UNSAFE_RUN_SCRIPT_OPERATION",
        f"{unsafe['operation']} is blocked in editor run-script because it can crash unattended editor sessions.",
        exit_code=2,
        details=unsafe,
        suggestion=suggestion,
    )


@editor_group.command("run-script")
@click.argument("script_path", type=click.Path(exists=False), required=False, default=None)
@click.option("-c", "--code", default=None, help="Short inline Python code; use '-' or a script file for multiline code.")
@click.option("--timeout", default=30, show_default=True, type=int, help="Max seconds to wait for results.")
@click.option("--no-save", "no_save", is_flag=True, default=False, help="Skip auto-saving dirty packages after script execution.")
@handle_error
@click.pass_obj
def editor_run_script(state: AppState, script_path, code, timeout, no_save):
    """Execute Python in the editor with structured result capture.

    Provide a script file path, "-" to read code from stdin, or short inline
    code via -c:

    \b
        editor run-script myscript.py
        editor run-script -
        editor run-script -c "result = {'hello': 'world'}"

    For multiline Python in PowerShell, pipe a here-string into
    ``editor run-script -`` so shell argument splitting cannot corrupt code
    or indentation.

    The script should set a ``result`` dict variable.  It will be
    automatically captured and returned as structured JSON output.

    By default, dirty packages are saved after execution.
    Use --no-save to skip this.
    """
    if not script_path and not code:
        raise AppError("MISSING_INPUT", "Provide a script file path, '-' for stdin, or use -c for short inline code.",
                       suggestion="editor run-script myscript.py  OR  editor run-script -  OR  editor run-script -c \"code\"")
    if script_path and code:
        raise AppError("AMBIGUOUS_INPUT", "Provide either a script file path or -c, not both.")

    from cli_anything.unreal.core.script_runner import run_python_code, run_python_script

    stdin_code = None
    if script_path == "-":
        stdin_code = _read_stdin_python_code()
        if stdin_code == "":
            raise AppError(
                "MISSING_STDIN_CODE",
                "No Python code was received on stdin.",
                exit_code=2,
                suggestion="Pipe a PowerShell here-string: @' ... '@ | ue-cli editor run-script -",
            )
    elif script_path:
        path = Path(script_path)
        if not path.is_file():
            raise AppError(
                "FILE_NOT_FOUND",
                f"Script file not found: {script_path}",
                exit_code=3,
                suggestion="Pass an existing .py file, '-' for stdin, or -c for a short one-liner.",
            )

    if code is not None or stdin_code is not None:
        unsafe = _unsafe_run_script_operation(code if code is not None else stdin_code)
        if unsafe:
            _raise_unsafe_run_script_operation(unsafe)
    else:
        script_code = Path(script_path).read_text(encoding="utf-8")
        unsafe = _unsafe_run_script_operation(script_code)
        if unsafe:
            _raise_unsafe_run_script_operation(unsafe)

    api = require_editor(state)
    log_file = _resolve_editor_log_file(state)
    try:
        log_start = log_file.stat().st_size if log_file else 0
    except OSError:
        log_start = 0
    from cli_anything.unreal.core.editor_lifecycle import (
        capture_editor_disconnect_context,
        close_editor_disconnect_context,
        collect_editor_disconnect_diagnostics,
    )

    disconnect_context = capture_editor_disconnect_context(api, state.session.project_path)
    try:
        if code is not None or stdin_code is not None:
            result = run_python_code(
                api, code if code is not None else stdin_code,
                project_dir=state.session.project_dir,
                timeout=timeout,
                save=not no_save,
                log_observation=True,
            )
        else:
            result = run_python_script(
                api, script_path,
                project_dir=state.session.project_dir,
                timeout=timeout,
                save=not no_save,
                log_observation=True,
            )
        log_begin_marker = result.pop("_log_begin_marker", None) if isinstance(result, dict) else None
        log_end_marker = result.pop("_log_end_marker", None) if isinstance(result, dict) else None
        log_result_marker = result.pop("_log_result_marker", None) if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("error"):
            if _is_transport_disconnect_result(result):
                diagnostics = collect_editor_disconnect_diagnostics(disconnect_context)
                _raise_editor_connection_lost(
                    result,
                    "editor run-script",
                    diagnostics=diagnostics,
                )
            if _is_transport_timeout_result(result):
                retry_timeout = max(60, timeout * 2)
                retry_command = _editor_run_script_retry_command(
                    state,
                    script_path,
                    timeout=retry_timeout,
                    no_save=no_save,
                )
                observation_task = None
                if log_begin_marker and log_end_marker and log_result_marker and log_file:
                    from cli_anything.unreal.core.editor_exec import (
                        create_editor_run_script_observation,
                    )

                    source_kind = (
                        "inline"
                        if code is not None
                        else "stdin"
                        if stdin_code is not None
                        else "file"
                    )
                    source_path = (
                        str(Path(script_path).resolve())
                        if source_kind == "file" and script_path
                        else None
                    )
                    observation_task = create_editor_run_script_observation(
                        project_path=state.session.project_path,
                        log_file=log_file,
                        log_start=log_start,
                        begin_marker=log_begin_marker,
                        end_marker=log_end_marker,
                        result_marker=log_result_marker,
                        source_kind=source_kind,
                        source_path=source_path,
                        no_save=no_save,
                    )
                    if observation_task.get("status") == "completed":
                        observed = dict(observation_task.get("result") or {})
                        script_result = dict(observed.get("script_result") or {})
                        script_result.update({
                            "task_id": observation_task["task_id"],
                            "capture_mode": "editor_log_file",
                            "transport_timeout_recovered": True,
                            "timeout_seconds": timeout,
                        })
                        output(script_result, state)
                        return
                    if observation_task.get("status") == "failed":
                        task_error = dict(observation_task.get("error") or {})
                        raise AppError(
                            str(task_error.get("code") or "SCRIPT_EXECUTION_FAILED"),
                            str(task_error.get("message") or "Editor script failed after its HTTP response timed out."),
                            exit_code=3,
                            details=task_progress(observation_task),
                            suggestion=(
                                "Fix the captured script error before retrying; the script was not redispatched."
                            ),
                        )
                _raise_editor_script_timeout(
                    result,
                    "editor run-script",
                    timeout,
                    retry_timeout=retry_timeout,
                    retry_command=retry_command,
                    observation_task=observation_task,
                )
            raise AppError(
                "SCRIPT_EXECUTION_FAILED",
                str(result.get("error")),
                exit_code=3,
                details=result,
                suggestion="Fix the UE Python script exception and rerun editor run-script.",
            )
        output(result, state)
    finally:
        close_editor_disconnect_context(disconnect_context)


@editor_group.command("api-discover")
@click.argument("target")
@click.option("--query", "-q", default=None, help="Case-insensitive regex filter for property/function names.")
@click.option("--detail", "-d", default=None, metavar="NAMES", help="Comma-separated names to get full detail for.")
@click.option("--timeout", default=30, type=int, help="Max seconds to wait for results.")
@handle_error
@click.pass_obj
def editor_api_discover(state: AppState, target, query, detail, timeout):
    """Discover the API surface of a UE class via C++ reflection.

    TARGET is auto-detected: class name, asset/subobject path, or actor path.
    Use -d to drill into specific properties/functions.
    """
    from cli_anything.unreal.core.script_runner import api_discover
    api = require_editor(state)
    result = api_discover(api, target, query=query, detail=detail, timeout=timeout)
    raise_for_legacy_error(
        result,
        default_code="API_DISCOVER_FAILED",
        default_message="Editor API discovery failed.",
    )
    output(result, state)


@editor_group.command("enable-remote")
@handle_error
@click.pass_obj
def editor_enable_remote(state: AppState):
    """Enable Remote Control features for CLI use.

    Enables RemoteControl in .uproject and creates/updates
    DefaultRemoteControl.ini for remote console and Python execution.
    Requires editor restart to take effect.
    """
    from cli_anything.unreal.utils.ue_backend import ensure_remote_control_config, get_editor_binary_prefix

    require_project(state)
    result = ensure_remote_control_config(
        state.session.project_dir,
        engine_root=state.session.engine_root,
        editor_binary_prefix=get_editor_binary_prefix(state.session.engine_root),
        uproject_path=state.session.project_path,
    )
    raise_for_legacy_error(
        result,
        default_code="REMOTE_CONTROL_ENABLE_FAILED",
        default_message="Remote Control configuration could not be enabled.",
    )
    if isinstance(result, dict) and result.get("status") in {"failed", "unavailable"}:
        raise AppError(
            "REMOTE_CONTROL_ENABLE_FAILED",
            "Remote Control configuration could not be enabled.",
            exit_code=3,
            suggestion=result.get("suggestion"),
            details=result,
        )
    output(result, state)


@editor_group.command("plugin-version")
@handle_error
@click.pass_obj
def editor_plugin_version(state: AppState):
    """Check CliAnythingBridge plugin version (bundled vs loaded).

    Reports the version bundled with the CLI package and the version
    currently loaded in the running editor (if any).
    """
    from cli_anything.unreal.core.plugin_bridge import get_bundled_version, get_loaded_plugin_version

    bundled = get_bundled_version()
    loaded = None

    ensure_inferred_project(state)
    if state.session.project_dir:
        api = require_editor(state)
        loaded = get_loaded_plugin_version(api)

    output({
        "bundled_version": bundled,
        "loaded_version": loaded,
        "match": loaded is not None and loaded == bundled,
    }, state)


@editor_group.command("plugin-upgrade")
@handle_error
@click.pass_obj
def editor_plugin_upgrade(state: AppState):
    """Upgrade the CliAnythingBridge plugin if a newer version is available.

    Workflow: deploy updated plugin source, compile the bridge module,
    restart the editor if it was running, verify the new version.
    """
    from cli_anything.unreal.core.plugin_bridge import (
        ensure_plugin_deployed,
        finalize_plugin_deployment,
        get_bundled_version,
        get_loaded_plugin_version,
        rollback_plugin_deployment,
    )

    require_project(state)
    project_dir = state.session.project_dir

    bundled = get_bundled_version()

    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI
    api = UEEditorAPI(port=state.session.port)
    if sys.platform == "win32":
        try:
            _running_editors, matching_editors = _find_matching_project_editors(
                state.session.project_path
            )
        except Exception as exc:
            raise AppError(
                "EDITOR_PROJECT_VERIFICATION_FAILED",
                "Could not enumerate project editor processes before plugin upgrade.",
                exit_code=3,
                suggestion="Retry after process discovery is available; no editor request was sent.",
                details={
                    "project": state.session.project_path,
                    "port": state.session.port,
                    "error": str(exc),
                },
            ) from exc
        editor_was_running = bool(matching_editors)
    else:
        editor_was_running = api.is_alive()
    loaded = None

    if editor_was_running:
        try:
            api = require_editor(state)
        except AppError as exc:
            if sys.platform != "win32" or exc.code not in {
                "EDITOR_UNREACHABLE",
                "EDITOR_PROJECT_NOT_RUNNING",
                "EDITOR_PROJECT_VERIFICATION_FAILED",
            }:
                raise
            # The target process is independently verified by its .uproject
            # command line. Close that process tree without sending anything to
            # an unverified Remote Control port.
            _close_editor_for_project(api, state, api_alive=False)
        else:
            loaded = get_loaded_plugin_version(api)
            if loaded == bundled:
                output({"status": "up_to_date", "version": bundled}, state)
                return
            _close_editor_for_project(api, state)

        drain_result = _wait_for_project_editor_exit(state.session.project_path, state.session.port, timeout=60)
        if drain_result and drain_result.get("status") not in {"closed", "offline"}:
            raise AppError(
                "EDITOR_CLOSE_FAILED",
                "Editor process did not fully exit before plugin compile; project DLLs may still be locked.",
                exit_code=3,
                suggestion="Close or kill UnrealEditor processes for this project, then retry editor plugin-upgrade.",
                details=drain_result,
            )

    deploy = ensure_plugin_deployed(
        project_dir,
        preserve_existing=True,
        force_redeploy=True,
    )
    if not deploy["deployed"]:
        raise AppError(
            "DEPLOY_FAILED",
            deploy.get("error", "Deployment failed"),
            details=deploy,
        )
    if deploy.get("action") == "update_pending_locked":
        raise AppError(
            "BRIDGE_DEPLOY_LOCKED",
            deploy.get("error", "Bridge plugin could not be replaced."),
            suggestion=deploy.get("suggestion"),
            details=deploy,
        )

    from cli_anything.unreal.core.plugin_bridge import compile_bridge_plugin
    build_result = compile_bridge_plugin(
        state.session.project_path,
        engine_root=state.session.engine_root,
    )
    if build_result.get("status") == "error":
        rollback = rollback_plugin_deployment(deploy)
        details = dict(build_result)
        details.update(_extract_compile_lock_error(build_result.get("log_file", "")))
        details["bridge_rollback"] = rollback
        suggestion = None
        if details.get("locked_file"):
            suggestion = (
                "A DLL is locked during compile. Close all UnrealEditor processes for this project "
                "or stop the process holding the DLL, then retry editor plugin-upgrade."
            )
        elif build_result.get("recovery_command"):
            suggestion = f"Run: {build_result['recovery_command']}"
        raise AppError(
            build_result.get("code", "BRIDGE_MODULE_COMPILE_FAILED"),
            build_result.get("error", "Build failed"),
            suggestion=suggestion,
            details=details,
        )

    deployment_commit = finalize_plugin_deployment(deploy)

    if editor_was_running:
        from cli_anything.unreal.utils.ue_backend import find_editor_exe
        engine_root = state.session.engine_root
        editor_exe = find_editor_exe(engine_root) if engine_root else None
        if editor_exe:
            cmd = _build_launch_cmd(
                editor_exe,
                state.session.project_path,
                None,
            )
            sp.Popen(cmd, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            restarted_api = None
            for _ in range(60):
                time.sleep(2)
                try:
                    restarted_api = require_editor(state, timeout=1)
                    break
                except AppError:
                    continue

            if restarted_api is None:
                raise AppError(
                    "EDITOR_RESTART_UNREACHABLE",
                    "The project editor did not return on a verified Remote Control port after plugin upgrade.",
                    exit_code=4,
                    suggestion="Run editor status and inspect the project log before retrying.",
                    details={
                        "project": state.session.project_path,
                        "port": state.session.port,
                    },
                )
            new_loaded = get_loaded_plugin_version(restarted_api)
            if new_loaded == bundled:
                output({
                    "status": "upgraded",
                    "version": bundled,
                    "previous_version": loaded,
                    "bridge_build": build_result,
                    "bridge_deployment_commit": deployment_commit,
                }, state)
            else:
                output({
                    "status": "version_mismatch",
                    "expected": bundled,
                    "loaded": new_loaded,
                }, state)
            return

    output({
        "status": "deployed",
        "action": deploy.get("action"),
        "version": deploy.get("version", bundled),
        "plugin_dir": deploy.get("plugin_dir"),
        "needs_restart": editor_was_running,
        "bridge_build": build_result,
        "bridge_deployment_commit": deployment_commit,
    }, state)


@editor_group.group("cvar")
def cvar_group():
    """Get and set console variables."""


@cvar_group.command("get")
@click.argument("name")
@click.option(
    "--timeout",
    default=10,
    show_default=True,
    type=click.IntRange(min=1),
    help="Total seconds allowed for editor selection and the CVar query.",
)
@handle_error
@click.pass_obj
def cvar_get(state: AppState, name, timeout):
    """Get a console variable value."""
    deadline = time.monotonic() + timeout
    try:
        api = require_editor(state, timeout=timeout, accept_listener=True)
    except AppError as error:
        if error.code != "EDITOR_UNREACHABLE" or time.monotonic() < deadline:
            raise
        raise AppError(
            "CVAR_GET_TIMEOUT",
            f"CVar query timed out after {timeout} seconds: {name}",
            exit_code=3,
            suggestion=(
                "Run editor status. Retry after PIE or the game thread becomes responsive, "
                "or increase editor cvar get --timeout."
            ),
            details={
                "name": name,
                "timeout_seconds": timeout,
                "request_stage": "editor_selection",
            },
        )

    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        info = {
            "name": name,
            "error": f"CVar query timed out after {timeout} seconds.",
            "request_stage": "editor_selection",
        }
    else:
        info = api.get_cvar_info(name, timeout=remaining)

    if info.get("error"):
        if _is_transport_timeout_result(info):
            details = dict(info)
            details["timeout_seconds"] = timeout
            raise AppError(
                "CVAR_GET_TIMEOUT",
                f"CVar query timed out after {timeout} seconds: {name}",
                exit_code=3,
                suggestion=(
                    "Run editor status. Retry after PIE or the game thread becomes responsive, "
                    "or increase editor cvar get --timeout."
                ),
                details=details,
            )
        if _is_transport_disconnect_result(info):
            _raise_editor_connection_lost(info, "editor cvar get")
        raise AppError(
            "CVAR_GET_FAILED",
            f"CVar query failed: {name}",
            exit_code=3,
            suggestion="Run editor status, wait for the editor to become responsive, then retry.",
            details=info,
        )

    value = str(info.get("value", ""))
    exists = info.get("exists")
    if exists is False:
        raise AppError(
            "CVAR_NOT_FOUND",
            f"CVar not found: {name}",
            exit_code=2,
            suggestion="Check the CVar name, or search with UE console command help.",
            details=info,
        )
    if value == "" and exists is None:
        raise AppError(
            "CVAR_GET_AMBIGUOUS_EMPTY",
            f"CVar returned an empty value, but existence could not be verified: {name}",
            exit_code=2,
            suggestion="Run: ue-cli editor plugin-upgrade, restart editor, then retry.",
            details=info,
        )
    result = {"name": info.get("name", name), "value": value}
    if exists is not None:
        result["exists"] = exists
    if info.get("verification"):
        result["verification"] = info["verification"]
    output(result, state)


@cvar_group.command(
    "set",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("name")
@click.argument("value", nargs=-1, type=click.UNPROCESSED)
@handle_error
@click.pass_obj
def cvar_set(state: AppState, name, value):
    """Set a console variable value."""
    if not value:
        raise AppError(
            "MISSING_CVAR_VALUE",
            "CVar set requires a value.",
            exit_code=2,
            suggestion="Use: ue-cli editor cvar set NAME VALUE",
        )
    value = " ".join(value)
    api = require_editor(state)
    result = api.set_cvar(name, value)
    if "error" in result and "400" in str(result["error"]):
        raise AppError(
            "CVAR_SET_FAILED",
            "CVar set failed. Remote console command execution is disabled.",
            suggestion="Run: ue-cli editor enable-remote, then restart editor.",
            details={"name": name, "value": value},
        )
    if _is_transport_disconnect_result(result):
        _raise_editor_connection_lost(result, "editor cvar set")
    raise_for_legacy_error(
        result,
        default_code="CVAR_SET_FAILED",
        default_message=f"CVar set failed: {name}",
    )
    output({"name": name, "value": value, "status": "ok", **result}, state)


@editor_group.command("new-level")
@click.argument("level_path")
@click.option("--template", default=None, help="Optional path to a template level to clone")
@handle_error
@click.pass_obj
def editor_new_level(state: AppState, level_path, template):
    """Create and open a new level."""
    from cli_anything.unreal.core.scene import new_level
    api = require_editor(state)
    result = new_level(api, level_path, template)
    if _is_transport_disconnect_result(result):
        _raise_editor_connection_lost(result, "editor new-level")
    if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed"):
        _raise_level_command_failed(result, "editor new-level", "EDITOR_NEW_LEVEL_FAILED")
    output(result, state)


@editor_group.command("new-blank-level")
@click.option(
    "--discard-dirty-map",
    is_flag=True,
    default=False,
    help="Allow unsaved map changes to be discarded during the transition.",
)
@click.option(
    "--timeout",
    default=120,
    show_default=True,
    type=click.IntRange(min=1),
    help="Max seconds to wait for the synchronous NewBlankMap request.",
)
@handle_error
@click.pass_obj
def editor_new_blank_level(state: AppState, discard_dirty_map, timeout):
    """Create and open an unsaved transient blank level."""
    from cli_anything.unreal.core.scene import new_blank_level
    api = require_editor(state)
    result = new_blank_level(
        api,
        discard_dirty_map=discard_dirty_map,
        timeout=timeout,
    )
    if _is_transport_disconnect_result(result):
        _raise_editor_connection_lost(result, "editor new-blank-level")
    if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed"):
        _raise_level_command_failed(
            result,
            "editor new-blank-level",
            "EDITOR_NEW_BLANK_LEVEL_FAILED",
        )
    output(result, state)


@editor_group.command("open-level")
@click.argument("level_path")
@click.option(
    "--reload",
    "reload_level",
    is_flag=True,
    default=False,
    help="Restart the editor on the currently active level.",
)
@click.option(
    "--discard-unsaved",
    is_flag=True,
    default=False,
    help="Authorize discarding all unsaved editor packages during --reload.",
)
@click.option(
    "--timeout",
    default=300,
    show_default=True,
    type=click.IntRange(min=1),
    help="Max seconds for LoadLevel or replacement editor startup.",
)
@handle_error
@click.pass_obj
def editor_open_level(state: AppState, level_path, reload_level, discard_unsaved, timeout):
    """Open an existing level using a rooted package path such as /Game/Maps/MyMap."""
    from cli_anything.unreal.core.scene import open_level, prepare_level_reload

    level_path = _require_rooted_level_path(level_path)
    if discard_unsaved and not reload_level:
        raise AppError(
            "EDITOR_OPEN_LEVEL_OPTION_CONFLICT",
            "--discard-unsaved is valid only with --reload.",
            exit_code=2,
        )
    if reload_level and not discard_unsaved:
        raise AppError(
            "EDITOR_RELOAD_DISCARD_REQUIRED",
            "Reloading the active level can discard unsaved editor state.",
            exit_code=2,
            suggestion="Rerun with both --reload and --discard-unsaved only after authorizing loss of all unsaved editor packages.",
        )

    if reload_level:
        ensure_inferred_project(state)
        require_project(state)
    api = require_editor(state)
    if reload_level:
        reload_preflight = prepare_level_reload(api, level_path)
        if reload_preflight.get("error") or reload_preflight.get("status") == "failed":
            _raise_level_command_failed(
                reload_preflight,
                "editor open-level --reload",
                "EDITOR_RELOAD_LEVEL_FAILED",
            )
        close_result = _close_editor_for_project(
            api,
            state,
            api_alive=True,
            force=True,
        )
        try:
            launch_result = _launch_editor_result(
                state,
                map_path=level_path,
                no_wait=False,
                timeout=timeout,
                extra_args=[],
                unattended=False,
                no_remote=False,
            )
        except AppError as exc:
            details = dict(exc.details or {})
            details.update({
                "reload_path": level_path,
                "discard_unsaved": True,
                "previous_editor": close_result,
            })
            raise AppError(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
                suggestion=exc.suggestion,
                details=details,
            ) from exc

        launch_status = launch_result.get("status")
        reload_complete = launch_status in {"completed", "online"}
        result = {
            "status": "reloaded" if reload_complete else launch_status,
            "completion_state": "completed" if reload_complete else "pending",
            "path": level_path,
            "discard_unsaved": True,
            "reloaded_by": "editor_restart",
            "previous_editor": close_result,
            "launch": launch_result,
        }
        for key in ("task_id", "next_command", "suggested_poll_interval_seconds"):
            if launch_result.get(key) is not None:
                result[key] = launch_result[key]
        output(result, state)
        return

    result = open_level(api, level_path, timeout=timeout)
    if _is_transport_disconnect_result(result):
        _raise_editor_connection_lost(result, "editor open-level")
    if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed"):
        _raise_level_command_failed(result, "editor open-level", "EDITOR_OPEN_LEVEL_FAILED")
    output(result, state)


@editor_group.command("save-level")
@handle_error
@click.pass_obj
def editor_save_level(state: AppState):
    """Save the current level."""
    from cli_anything.unreal.core.scene import save_level
    api = require_editor(state)
    result = save_level(api)
    if _is_transport_disconnect_result(result):
        _raise_editor_connection_lost(result, "editor save-level")
    if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed"):
        _raise_level_command_failed(result, "editor save-level", "EDITOR_SAVE_LEVEL_FAILED")
    output(result, state)
