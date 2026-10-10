"""Shared CLI protocol helpers and command registration."""

from __future__ import annotations

import functools
import json
import sys
import time
from pathlib import Path

import click

from cli_anything.unreal._version import __version__
from cli_anything.unreal.core.session import Session
from cli_anything.unreal.errors import UeCliError
from cli_anything.unreal.utils.repl_skin import ReplSkin


# Compatibility export for command modules and third-party callers.  Domain
# layers can now raise the same typed error without importing ``commands``.
AppError = UeCliError


_PROJECT_VERIFICATION_RETRY_DELAYS = (0.1, 0.2)
_PROJECT_VERIFICATION_RETRY_TIMEOUT = 2.0


class AppState:
    """Holds mutable session state shared across Click commands."""

    def __init__(self):
        self.json_output: bool = True
        self.session: Session = Session()
        self.port_is_explicit: bool = False
        self.project_is_explicit: bool = False
        self.project_is_inferred: bool = False
        self.skin: ReplSkin = ReplSkin("unreal", version=__version__)
        self.in_repl: bool = False
        self.output_mode: str = "json"


def _global_project_option_suggestion() -> str:
    """Show the active command with Click's required global-option ordering."""

    context = click.get_current_context(silent=True)
    command_parts: list[str] = []
    while context is not None and context.parent is not None:
        if context.info_name:
            command_parts.append(context.info_name)
        context = context.parent
    command_path = " ".join(reversed(command_parts)) or "<command>"
    return (
        "Place the global option before the command: "
        f"ue-cli --project <path-to.uproject> {command_path}."
    )


def ensure_inferred_project(state: AppState) -> str | None:
    """Bind the nearest cwd project before resolving an editor target."""

    if state.session.project_path:
        return state.session.project_path

    from cli_anything.unreal.core.session import (
        AmbiguousProjectError,
        find_nearest_uproject,
    )

    try:
        project_path = find_nearest_uproject()
    except AmbiguousProjectError as exc:
        candidates = [str(path.resolve()) for path in exc.candidates]
        raise AppError(
            "PROJECT_AMBIGUOUS",
            f"Multiple .uproject files found in {exc.directory}.",
            exit_code=2,
            suggestion=_global_project_option_suggestion(),
            details={
                "directory": str(exc.directory),
                "candidates": candidates,
            },
        ) from exc
    if project_path is None:
        return None

    state.session.load_project(project_path)
    state.project_is_inferred = True
    if not state.port_is_explicit:
        from cli_anything.unreal.utils.ue_backend import get_editor_binary_prefix, read_rc_port

        state.session.port = read_rc_port(
            state.session.project_dir,
            editor_binary_prefix=get_editor_binary_prefix(state.session.engine_root),
        ) or 30010
    return state.session.project_path


def emit_json(payload: dict | list) -> None:
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def success_payload(result) -> dict:
    return {"status": "success", "result": result}


def error_payload(
    code: str,
    message: str,
    *,
    suggestion: str | None = None,
    details=None,
) -> dict:
    payload = {
        "status": "error",
        "code": code,
        "message": message,
    }
    if suggestion:
        payload["suggestion"] = suggestion
    if details is not None:
        payload["details"] = details
    return payload


def output(data, state: AppState):
    """Emit either structured JSON or a compact text rendering."""
    if state.json_output:
        emit_json(success_payload(data))
        return

    from cli_anything.unreal.utils.output import format_text_output
    click.echo(format_text_output(data), nl=False)


def fail(
    state: AppState,
    code: str,
    message: str,
    *,
    exit_code: int = 1,
    suggestion: str | None = None,
    details=None,
):
    if state.json_output:
        emit_json(error_payload(code, message, suggestion=suggestion, details=details))
    else:
        state.skin.error(message)
        if suggestion:
            state.skin.hint(suggestion)
    if not state.in_repl:
        raise SystemExit(exit_code)


def handle_error(f):
    """Decorator for consistent protocol-level error handling."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        state = _get_state()
        try:
            return f(*args, **kwargs)
        except SystemExit:
            raise
        except AppError as e:
            fail(
                state,
                e.code,
                e.message,
                exit_code=e.exit_code,
                suggestion=e.suggestion,
                details=e.details,
            )
        except click.UsageError as e:
            fail(
                state,
                "INVALID_ARGUMENT",
                str(e),
                exit_code=2,
                suggestion="Check --help for the expected arguments.",
            )
        except FileNotFoundError as e:
            fail(
                state,
                "FILE_NOT_FOUND",
                str(e),
                exit_code=3,
            )
        except ConnectionError as e:
            fail(
                state,
                "EDITOR_UNREACHABLE",
                str(e),
                exit_code=4,
                suggestion=f"Editor not reachable on port {state.session.port}. Launch with: editor launch --project <path-to-.uproject>",
            )
        except Exception as e:
            fail(
                state,
                "INTERNAL_ERROR",
                f"{type(e).__name__}: {e}",
                exit_code=1,
            )

    return wrapper


def _get_state() -> AppState:
    try:
        ctx = click.get_current_context()
        return ctx.obj
    except RuntimeError:
        return AppState()


def require_project(state: AppState):
    ensure_inferred_project(state)
    if not state.session.is_loaded:
        raise AppError(
            "PROJECT_REQUIRED",
            "No project loaded.",
            exit_code=2,
            suggestion="Pass --project <path-to.uproject>.",
        )


@click.command("preflight")
@handle_error
@click.pass_obj
def preflight_cmd(state: AppState):
    """Run read-only editor startup preflight checks."""
    from cli_anything.unreal.utils.ue_backend import preflight_check

    require_project(state)
    output(preflight_check(state.session.project_path, state.session.engine_root), state)


def _same_project_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve().as_posix().lower() == Path(right).resolve().as_posix().lower()
    except Exception:
        return Path(left).as_posix().lower() == Path(right).as_posix().lower()


def _project_mismatch_details(state: AppState, running: list[dict]) -> dict:
    return {
        "port": state.session.port,
        "project": state.session.project_path,
        "running_editors": [
            {"pid": editor.get("pid"), "project": editor.get("project", "")}
            for editor in running
        ],
    }


def _guard_editor_project(state: AppState, api_cls) -> dict | None:
    if not state.session.project_path or sys.platform != "win32":
        return None

    try:
        from cli_anything.unreal.utils.ue_backend import find_running_editors

        running = find_running_editors()
    except Exception as exc:
        raise AppError(
            "EDITOR_PROJECT_VERIFICATION_FAILED",
            "Could not enumerate running Unreal Editor processes to verify the selected project.",
            exit_code=3,
            suggestion="Retry after process discovery is available; no editor request was sent.",
            details={
                "port": state.session.port,
                "project": state.session.project_path,
                "error": str(exc),
            },
        ) from exc

    try:
        listening_pid = api_cls._get_pid_listening_on_port(state.session.port)
        listening_pid = int(listening_pid) if listening_pid is not None else None
    except (OSError, TypeError, ValueError) as exc:
        raise AppError(
            "EDITOR_PROJECT_VERIFICATION_FAILED",
            f"Could not identify the process owning editor port {state.session.port}.",
            exit_code=3,
            suggestion="Retry after port ownership can be verified; no editor request was sent.",
            details={
                **_project_mismatch_details(state, running),
                "error": str(exc),
            },
        ) from exc

    if listening_pid is None:
        raise AppError(
            "EDITOR_PROJECT_VERIFICATION_FAILED",
            f"Editor port {state.session.port} is reachable, but its owning process is unknown.",
            exit_code=3,
            suggestion="Retry after port ownership can be verified; no editor request was sent.",
            details=_project_mismatch_details(state, running),
        )

    owner = next(
        (editor for editor in running if int(editor.get("pid", 0)) == listening_pid),
        None,
    )
    verification_attempts = 1
    retry_errors: list[str] = []
    if owner is None:
        for delay in _PROJECT_VERIFICATION_RETRY_DELAYS:
            time.sleep(delay)
            verification_attempts += 1
            try:
                running = find_running_editors(
                    timeout=_PROJECT_VERIFICATION_RETRY_TIMEOUT,
                )
            except Exception as exc:
                retry_errors.append(str(exc))
                continue
            try:
                retry_listening_pid = api_cls._get_pid_listening_on_port(
                    state.session.port,
                )
                listening_pid = (
                    int(retry_listening_pid)
                    if retry_listening_pid is not None
                    else None
                )
            except (OSError, TypeError, ValueError) as exc:
                retry_errors.append(str(exc))
                continue
            if listening_pid is None:
                continue
            owner = next(
                (
                    editor
                    for editor in running
                    if int(editor.get("pid", 0)) == listening_pid
                ),
                None,
            )
            if owner is not None:
                break

    if owner is None and listening_pid is None:
        details = {
            **_project_mismatch_details(state, running),
            "verification_attempts": verification_attempts,
        }
        if retry_errors:
            details["retry_errors"] = retry_errors
        raise AppError(
            "EDITOR_PROJECT_VERIFICATION_FAILED",
            f"Editor port {state.session.port} is reachable, but its owning process is unknown.",
            exit_code=3,
            suggestion="Retry after port ownership can be verified; no editor request was sent.",
            details=details,
        )
    if owner is None:
        details = {
            **_project_mismatch_details(state, running),
            "listener_pid": listening_pid,
            "verification_attempts": verification_attempts,
        }
        if retry_errors:
            details["retry_errors"] = retry_errors
        raise AppError(
            "EDITOR_PROJECT_VERIFICATION_FAILED",
            f"Port {state.session.port} belongs to PID {listening_pid}, but that process could not be verified as Unreal Editor.",
            exit_code=3,
            suggestion="Retry after process discovery is available; no editor request was sent.",
            details=details,
        )
    if not _same_project_path(owner.get("project", ""), state.session.project_path):
        raise AppError(
            "EDITOR_PROJECT_NOT_RUNNING",
            f"Editor HTTP API on port {state.session.port} belongs to another project.",
            exit_code=3,
            details={
                **_project_mismatch_details(state, running),
                "listener_pid": listening_pid,
                "listener_project": owner.get("project", ""),
            },
        )
    return owner


def _discover_online_editor_port(
    state: AppState,
    *,
    fail_if_ambiguous: bool = False,
) -> int | None:
    """Return one unambiguous live editor port when the selected port is stale."""
    if state.port_is_explicit:
        return None

    try:
        from cli_anything.unreal.commands.editor import _scan_editor_status_instances

        instances = _scan_editor_status_instances(
            state,
            "30010-30020",
            include_bridge_status=False,
        )
    except Exception:
        return None

    online_by_port: dict[int, dict] = {}
    for instance in instances:
        if instance.get("status") != "online" or instance.get("port") is None:
            continue
        if state.session.project_path and not _same_project_path(
            instance.get("project_path"),
            state.session.project_path,
        ):
            continue
        try:
            port = int(instance["port"])
        except (TypeError, ValueError):
            continue
        online_by_port.setdefault(port, instance)
    if len(online_by_port) == 1:
        return next(iter(online_by_port))
    if fail_if_ambiguous and len(online_by_port) > 1:
        live_editors = []
        for port, instance in sorted(online_by_port.items()):
            live_editors.append({
                "pid": instance.get("pid"),
                "port": port,
                "project_path": instance.get("project_path"),
            })
        if state.session.project_path:
            suggestion = "Pass --port <port> to select one matching editor."
        else:
            suggestion = "Pass --project <path-to.uproject> or --port <port> to select one editor."
        raise AppError(
            "EDITOR_TARGET_AMBIGUOUS",
            f"Multiple live editors match while selected port {state.session.port} is offline.",
            exit_code=3,
            suggestion=suggestion,
            details={
                "selected_port": state.session.port,
                "project": state.session.project_path,
                "live_editors": live_editors,
            },
        )
    return None


def require_editor(
    state: AppState,
    *,
    timeout: int | float | None = None,
    accept_listener: bool = False,
):
    from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

    ensure_inferred_project(state)
    if state.session.project_path:
        from cli_anything.unreal.core.confirmations import raise_if_editor_blocked

        # Mailbox-only fast path. Do this before any health probe so every
        # editor-dependent command reports the actionable confirmation code.
        raise_if_editor_blocked(state.session.project_path)
    deadline = time.monotonic() + timeout if timeout is not None else None

    def remaining_timeout() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def editor_available(api) -> bool:
        remaining = remaining_timeout()
        if accept_listener:
            listener_timeout = 0.5 if remaining is None else min(0.5, remaining)
            return api.is_listening(timeout=listener_timeout)
        if remaining is None:
            return api.is_alive()
        return api.is_alive(timeout=remaining)

    api = UEEditorAPI(port=state.session.port)
    api.project_path = state.session.project_path
    api_alive = editor_available(api)
    if not api_alive:
        live_port = _discover_online_editor_port(state, fail_if_ambiguous=True)
        if live_port is not None:
            live_api = UEEditorAPI(port=live_port)
            live_api.project_path = state.session.project_path
            if editor_available(live_api):
                state.session.port = live_port
                api = live_api
                api_alive = True
    if not api_alive:
        if state.session.project_path:
            from cli_anything.unreal.core.confirmations import raise_if_editor_blocked

            raise_if_editor_blocked(state.session.project_path, include_windows=True)
        listener_reachable = False
        try:
            listener_reachable = bool(api.is_listening(timeout=0.5))
        except Exception:
            pass
        if listener_reachable:
            listener_pid = None
            try:
                listener_pid = UEEditorAPI._get_pid_listening_on_port(
                    state.session.port,
                    timeout=0.5,
                )
            except Exception:
                pass
            raise AppError(
                "EDITOR_UNREACHABLE",
                f"Editor HTTP API not responding on port {state.session.port}.",
                exit_code=4,
                suggestion=(
                    "A process is still listening on this port. Wait, then run editor status. "
                    "If the existing editor remains unresponsive, close or restart that session; "
                    "do not launch another editor alongside it."
                ),
                details={
                    "port": state.session.port,
                    "listener_reachable": True,
                    "listener_pid": listener_pid,
                },
            )
        raise AppError(
            "EDITOR_UNREACHABLE",
            f"Editor HTTP API not responding on port {state.session.port}.",
            exit_code=4,
            suggestion="Launch the editor with: editor launch --project <path-to-.uproject>",
        )
    verified_owner = None
    try:
        verified_owner = _guard_editor_project(state, UEEditorAPI)
    except AppError as initial_guard_error:
        if state.port_is_explicit or not state.session.project_path:
            raise
        live_port = _discover_online_editor_port(state, fail_if_ambiguous=True)
        if live_port is None or int(live_port) == int(state.session.port):
            raise initial_guard_error
        live_api = UEEditorAPI(port=live_port)
        live_api.project_path = state.session.project_path
        if not editor_available(live_api):
            raise initial_guard_error
        state.session.port = live_port
        api = live_api
        verified_owner = _guard_editor_project(state, UEEditorAPI)
    if verified_owner:
        api._verified_editor_pid = verified_owner.get("pid")
        api._verified_editor_cmdline = verified_owner.get("cmdline")
    return api


def register_commands(cli_group: click.Group):
    from cli_anything.unreal.commands.project import project_group
    from cli_anything.unreal.commands.asset import asset_group
    from cli_anything.unreal.commands.build import build_group
    from cli_anything.unreal.commands.scene import scene_group
    from cli_anything.unreal.commands.material import material_group
    from cli_anything.unreal.commands.blueprint import blueprint_group
    from cli_anything.unreal.commands.umg import umg_group
    from cli_anything.unreal.commands.screenshot import screenshot_group
    from cli_anything.unreal.commands.editor import editor_close, editor_group, editor_status
    from cli_anything.unreal.commands.confirmation import confirmation_group
    from cli_anything.unreal.commands.session import session_group
    from cli_anything.unreal.commands.skills import register as register_skills
    from cli_anything.unreal.commands.repl import register as register_repl
    from cli_anything.unreal.commands.remote import remote_group

    cli_group.add_command(project_group)
    cli_group.add_command(asset_group)
    cli_group.add_command(build_group)
    cli_group.add_command(scene_group)
    cli_group.add_command(material_group)
    cli_group.add_command(blueprint_group)
    cli_group.add_command(umg_group)
    cli_group.add_command(screenshot_group)
    cli_group.add_command(editor_group)
    cli_group.add_command(editor_close, "close")
    cli_group.add_command(editor_status, "status")
    cli_group.add_command(confirmation_group)
    cli_group.add_command(preflight_cmd)
    cli_group.add_command(session_group)
    cli_group.add_command(remote_group)
    register_skills(cli_group)
    register_repl(cli_group)
