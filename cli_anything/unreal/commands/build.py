"""Build command definitions."""

from __future__ import annotations

import codecs
import re
import sys
import time
from pathlib import Path

import click

from cli_anything.unreal.core.build import (
    validate_cook_ini_override,
    validate_cook_package,
    validate_module_name,
    validate_package_uat_value,
)
from cli_anything.unreal.commands import AppError, AppState, _same_project_path, handle_error, output, require_project
from cli_anything.unreal.core.tasks import (
    FINAL_TASK_STATUSES,
    TaskWorkerSpawnError,
    cancel_task,
    load_task,
    reconcile_task_state,
    submit_task,
    task_progress,
)
from cli_anything.unreal.utils.ue_backend import _allocate_log_path, _build_output_encoding, find_running_editors
from cli_anything.unreal.utils.ue_http_api import UEEditorAPI


def _build_payload(state: AppState, label: str, **kwargs) -> dict:
    if not state.session.engine_root:
        raise AppError(
            "ENGINE_NOT_FOUND",
            "Could not resolve the project's engine association or an explicit engine root.",
            exit_code=3,
            suggestion="Register the project's EngineAssociation or set UE_ENGINE_ROOT to the intended engine directory.",
            details={"project_path": state.session.project_path},
        )
    payload = {
        "project_path": state.session.project_path,
        "engine_root": state.session.engine_root,
        "log_file": _allocate_log_path(state.session.project_dir, label),
    }
    payload.update(kwargs)
    return payload


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
            suggestion="Pass --project <path-to.uproject>.",
        )


def _project_option(func):
    return click.option("--project", "project_path", type=click.Path(), help="Path to .uproject file")(func)


def _validate_package_value(ctx, param, value):
    values = value if isinstance(value, tuple) else (() if value is None else (value,))
    labels = {
        "maps": "map",
        "cook_flavor": "cook flavor",
        "uat_args": "UAT argument",
        "packages": "cook package",
        "output_dir": "cook output directory",
        "ini_overrides": "ini override",
    }
    try:
        for item in values:
            if param.name == "packages":
                validate_cook_package(item)
            elif param.name == "ini_overrides":
                validate_cook_ini_override(item)
            else:
                validate_package_uat_value(
                    item,
                    label=labels.get(param.name, param.name),
                    require_option=param.name == "uat_args",
                    require_value=param.name == "output_dir",
                )
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc
    return value


def _validate_module_value(ctx, param, value):
    values = value if isinstance(value, tuple) else (() if value is None else (value,))
    try:
        for item in values:
            validate_module_name(item)
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc
    return value


def _editor_compile_lock_risk(state: AppState, platform: str) -> dict | None:
    if sys.platform != "win32" or platform.lower() != "win64" or not state.session.project_path:
        return None

    try:
        running = find_running_editors()
    except Exception:
        return None

    matches = [
        editor for editor in running
        if _same_project_path(editor.get("project", ""), state.session.project_path)
    ]
    if not matches:
        return None

    online = False
    try:
        online = UEEditorAPI(port=state.session.port).is_alive()
    except Exception:
        online = False

    return {
        "project_path": state.session.project_path,
        "platform": platform,
        "online": online,
        "port": state.session.port,
        "running_editors": [
            {"pid": editor.get("pid"), "project": editor.get("project", "")}
            for editor in matches
        ],
        "next_command": f'ue-cli --project "{state.session.project_path}" editor close',
    }


def _guard_compile_against_editor_locks(state: AppState, platform: str) -> None:
    risk = _editor_compile_lock_risk(state, platform)
    if not risk:
        return
    raise AppError(
        "EDITOR_RUNNING_LOCKS_DLLS",
        "UnrealEditor is running for this project; build compile can fail at link because editor/plugin DLLs are locked.",
        exit_code=3,
        suggestion=(
            f"Run: {risk['next_command']}; then retry build compile. "
            "For code changes while the editor stays open, use Unreal Live Coding/hot reload outside ue-cli build compile."
        ),
        details=risk,
    )


_MSVC_COMMAND_LINE_WARNING_PATTERN = re.compile(
    r"^\s*(?:.*[\\/])?cl(?:\.exe)?\s*:\s*.*?\b"
    r"(?P<warning>warning\s+D\d{4}\b)",
    re.IGNORECASE,
)


class _MSVCWarningFolder:
    """Fold repeated MSVC command-line warnings in the live stream only."""

    def __init__(self, log_file: str | None):
        self.log_file = log_file or ""
        self.pending = ""
        self.seen: set[str] = set()
        self.suppressed = 0

    def reset(self) -> None:
        self.pending = ""
        self.seen.clear()
        self.suppressed = 0

    def _render_line(self, line: str) -> str:
        match = _MSVC_COMMAND_LINE_WARNING_PATTERN.search(line)
        if match is None:
            return line
        key = " ".join(line[match.start("warning"):].split()).casefold()
        if key in self.seen:
            self.suppressed += 1
            return ""
        self.seen.add(key)
        return line

    def feed(self, text: str, *, final: bool = False) -> str:
        lines = (self.pending + text).splitlines(keepends=True)
        self.pending = ""
        if not final and lines and not lines[-1].endswith("\n"):
            self.pending = lines.pop()

        rendered = "".join(self._render_line(line) for line in lines)
        if final and self.pending:
            rendered += self._render_line(self.pending)
            self.pending = ""
        if final and self.suppressed:
            if rendered and not rendered.endswith(("\r", "\n")):
                rendered += "\n"
            rendered += (
                f"[ue-cli] folded {self.suppressed} repeated MSVC command-line warning lines; "
                f"full log: {self.log_file}\n"
            )
        return rendered


def _stream_log_delta(
    log_file: str | None,
    offset: int = 0,
    *,
    decoder=None,
    warning_folder: _MSVCWarningFolder | None = None,
    final: bool = False,
) -> int:
    if not log_file:
        return offset
    path = Path(log_file)
    try:
        size = path.stat().st_size
    except OSError:
        return offset
    if size < offset:
        offset = 0
        if decoder is not None:
            decoder.reset()
        if warning_folder is not None:
            warning_folder.reset()
    if size <= offset:
        if final and decoder is not None:
            text = decoder.decode(b"", final=True)
            if warning_folder is not None:
                text = warning_folder.feed(text, final=True)
            sys.stderr.write(text)
        return offset
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return offset
    if data:
        if decoder is None:
            text = data.decode(_build_output_encoding(), errors="replace")
        else:
            text = decoder.decode(data, final=final)
        if warning_folder is not None:
            text = warning_folder.feed(text, final=final)
        sys.stderr.write(text)
        sys.stderr.flush()
    return size


def _wait_for_task_with_log_stream(task_id: str, timeout: int | None, log_file: str | None) -> dict | None:
    deadline = None if timeout is None else time.time() + timeout
    decoder = codecs.getincrementaldecoder(_build_output_encoding())(errors="replace")
    warning_folder = _MSVCWarningFolder(log_file)
    offset = 0
    try:
        while True:
            offset = _stream_log_delta(
                log_file,
                offset,
                decoder=decoder,
                warning_folder=warning_folder,
            )
            task = load_task(task_id)
            if (
                task is not None
                and task.get("command", "").startswith("build.")
                and task.get("status") not in FINAL_TASK_STATUSES
                and task.get("worker_pid")
            ):
                task = reconcile_task_state(task_id) or task
            if task is None:
                return None
            if task.get("status") in FINAL_TASK_STATUSES:
                return task
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.5)
    finally:
        _stream_log_delta(
            log_file,
            offset,
            decoder=decoder,
            warning_folder=warning_folder,
            final=True,
        )


def _run_task(command: str, payload: dict, *, timeout: int | None, no_wait: bool, timeout_code: str):
    try:
        task = submit_task(command, payload)
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

    try:
        final_task = _wait_for_task_with_log_stream(
            task["task_id"], timeout, payload.get("log_file")
        )
    except (KeyboardInterrupt, click.Abort):
        cancelled = cancel_task(task["task_id"])
        if cancelled is not None:
            error = cancelled.get("error", {})
            if error.get("code") == "TASK_CANCEL_FAILED":
                raise AppError(
                    "TASK_CANCEL_FAILED",
                    error.get(
                        "message",
                        "Interrupted build task cancellation failed.",
                    ),
                    exit_code=4,
                    suggestion=(
                        "Inspect remaining process diagnostics before retrying "
                        "the build."
                    ),
                    details=task_progress(cancelled),
                ) from None
        raise
    if final_task is None:
        current = load_task(task["task_id"]) or task
        if timeout is None:
            message = "Task did not finish (no timeout was set)."
        else:
            message = f"Task did not finish within {timeout}s."
        progress = task_progress(current)
        progress["wait"] = {
            "status": "timeout",
            "code": timeout_code,
            "timeout_seconds": timeout,
            "task_continues": True,
        }
        progress["next_command"] = f"ue-cli task status {task['task_id']}"
        raise AppError(
            timeout_code,
            message,
            exit_code=4,
            details=progress,
        )

    progress = task_progress(final_task)
    final_status = final_task.get("status")
    if final_status in {"failed", "timeout"}:
        error = final_task.get("error", {})
        raise AppError(
            error.get("code", "TASK_TIMEOUT" if final_status == "timeout" else "TASK_EXECUTION_FAILED"),
            error.get("message", "Task timed out" if final_status == "timeout" else "Task execution failed"),
            exit_code=3,
            details=progress,
        )
    if final_status == "cancelled":
        error = final_task.get("error", {})
        raise AppError(
            error.get("code", "TASK_CANCELLED"),
            error.get("message", "Task was cancelled."),
            exit_code=4,
            details=progress,
        )
    return progress


@click.group("build")
def build_group():
    """Build system commands."""


@build_group.command("compile")
@_project_option
@click.option(
    "--config",
    "--configuration",
    "build_config",
    default="Development",
    type=click.Choice(["Development", "Shipping", "DebugGame", "Test"]),
)
@click.option("--platform", default="Win64")
@click.option(
    "--target",
    help="Unsupported Editor target override; build compile infers it from --project.",
)
@click.option(
    "--module",
    "modules",
    multiple=True,
    callback=_validate_module_value,
    help="Compile only this Editor module through UBT; repeat for multiple modules.",
)
@click.option("--no-wait", is_flag=True, default=False)
@click.option("--timeout", type=int, default=None)
@handle_error
@click.pass_obj
def build_compile(
    state: AppState,
    project_path,
    build_config,
    platform,
    target,
    modules,
    no_wait,
    timeout,
):
    _load_command_project(state, project_path)
    require_project(state)
    if target:
        module_options = "".join(f' --module "{module}"' for module in modules)
        replacement_command = (
            f'ue-cli --project "{state.session.project_path}" build compile '
            f"--platform {platform} --config {build_config}{module_options}"
        )
        raise AppError(
            "BUILD_TARGET_INFERRED",
            "build compile infers the Editor target from --project; --target is not supported.",
            exit_code=2,
            suggestion=f"Remove --target. Run: {replacement_command}",
            details={
                "provided_target": target,
                "replacement_command": replacement_command,
            },
        )
    if modules and platform.lower() != "win64":
        raise AppError(
            "MODULE_COMPILE_PLATFORM",
            "Module-targeted compile is supported only for Win64 Editor targets.",
            exit_code=2,
        )
    _guard_compile_against_editor_locks(state, platform)
    payload = _build_payload(
        state,
        "compile",
        build_config=build_config,
        platform=platform,
        modules=list(modules),
    )
    result = _run_task("build.compile", payload, timeout=timeout, no_wait=no_wait, timeout_code="BUILD_WAIT_TIMEOUT")
    output(result, state)


@build_group.command("cook")
@_project_option
@click.option("--platform", default="Win64")
@click.option(
    "--package",
    "packages",
    multiple=True,
    callback=_validate_package_value,
    help="Package seed to cook; repeat for multiple packages.",
)
@click.option(
    "--output-dir",
    type=click.Path(),
    callback=_validate_package_value,
    help="Root directory for cooked output.",
)
@click.option(
    "--ini",
    "ini_overrides",
    multiple=True,
    callback=_validate_package_value,
    help="Per-run UE ini override without the -ini: prefix; repeat as needed.",
)
@click.option("--no-wait", is_flag=True, default=False)
@click.option("--timeout", type=int, default=None)
@handle_error
@click.pass_obj
def build_cook(
    state: AppState,
    project_path,
    platform,
    packages,
    output_dir,
    ini_overrides,
    no_wait,
    timeout,
):
    _load_command_project(state, project_path)
    require_project(state)
    payload = _build_payload(
        state,
        "cook",
        platform=platform,
        packages=packages,
        output_dir=output_dir,
        ini_overrides=ini_overrides,
    )
    result = _run_task("build.cook", payload, timeout=timeout, no_wait=no_wait, timeout_code="BUILD_WAIT_TIMEOUT")
    output(result, state)


@build_group.command("package")
@_project_option
@click.option("--platform", default="Win64")
@click.option("--config", "build_config", default="Development", type=click.Choice(["Development", "Shipping", "DebugGame", "Test"]))
@click.option("--output-dir", type=click.Path(), help="Archive output directory")
@click.option("--map", "maps", multiple=True, callback=_validate_package_value, help="Cook only this map; repeat for multiple maps")
@click.option("--cook-flavor", callback=_validate_package_value, help="UAT cook flavor, for example ASTC")
@click.option(
    "--uat-arg",
    "uat_args",
    multiple=True,
    callback=_validate_package_value,
    help=(
        "Additional BuildCookRun/UAT argv; Cook Commandlet flags require "
        "-AdditionalCookerOptions=..."
    ),
)
@click.option("--no-wait", is_flag=True, default=False)
@click.option("--timeout", type=int, default=None)
@handle_error
@click.pass_obj
def build_package(
    state: AppState,
    project_path,
    platform,
    build_config,
    output_dir,
    maps,
    cook_flavor,
    uat_args,
    no_wait,
    timeout,
):
    _load_command_project(state, project_path)
    require_project(state)
    payload = _build_payload(
        state,
        "package",
        platform=platform,
        build_config=build_config,
        output_dir=output_dir,
        maps=maps,
        cook_flavor=cook_flavor,
        uat_args=uat_args,
    )
    result = _run_task("build.package", payload, timeout=timeout, no_wait=no_wait, timeout_code="BUILD_WAIT_TIMEOUT")
    output(result, state)


@build_group.command("status")
@_project_option
@click.argument("task_id", required=False)
@handle_error
@click.pass_obj
def build_status_cmd(state: AppState, project_path, task_id):
    _load_command_project(state, project_path)
    require_project(state)
    if task_id:
        task = reconcile_task_state(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", f"Task not found: {task_id}", exit_code=3)
        output(task_progress(task), state)
        return

    from cli_anything.unreal.core.build import build_status

    output(build_status(state.session.project_path), state)


@build_group.command("cancel")
@click.argument("task_id")
@handle_error
@click.pass_obj
def build_cancel(state: AppState, task_id):
    from cli_anything.unreal.core.tasks import cancel_task

    task = cancel_task(task_id)
    if task is None:
        raise AppError("TASK_NOT_FOUND", f"Task not found: {task_id}", exit_code=3)
    progress = task_progress(task)
    error = progress.get("error", {})
    if error.get("code") == "TASK_CANCEL_FAILED":
        raise AppError(
            "TASK_CANCEL_FAILED",
            error.get("message", "Build task cancellation failed."),
            exit_code=4,
            suggestion="Retry cancellation after inspecting the remaining process diagnostics.",
            details=progress,
        )
    output_integrity = progress.get("output_integrity", {})
    if output_integrity.get("code") == "BUILD_CANCELLED_OUTPUTS_INCOMPLETE":
        raise AppError(
            "BUILD_CANCELLED_OUTPUTS_INCOMPLETE",
            output_integrity["message"],
            exit_code=4,
            suggestion=f"Run: {output_integrity['recovery_command']}",
            details=progress,
        )
    output(progress, state)


@build_group.command("stop")
@_project_option
@handle_error
@click.pass_obj
def build_stop(state: AppState, project_path):
    from cli_anything.unreal.core.build import stop_build

    _load_command_project(state, project_path)
    require_project(state)
    result = stop_build(state.session.project_path)
    if result.get("status") == "partial" or result.get("remaining"):
        raise AppError(
            "TASK_CANCEL_FAILED",
            "Build cancellation did not terminate every project-owned process.",
            exit_code=4,
            suggestion="Retry cancellation after inspecting the remaining process diagnostics.",
            details=result,
        )
    output(result, state)


@build_group.command("is-building")
@_project_option
@handle_error
@click.pass_obj
def build_is_building(state: AppState, project_path):
    from cli_anything.unreal.core.build import is_building
    from cli_anything.unreal.utils.ue_backend import BuildProcessProbeError

    _load_command_project(state, project_path)
    require_project(state)
    try:
        result = is_building(state.session.project_path)
    except BuildProcessProbeError as exc:
        raise AppError(
            "BUILD_STATE_PROBE_FAILED",
            str(exc),
            exit_code=4,
            suggestion="Retry the state query after Windows process discovery recovers.",
            details=exc.details,
        ) from exc
    output(result, state)
