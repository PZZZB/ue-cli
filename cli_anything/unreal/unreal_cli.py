"""Click CLI main entry point for ue-cli."""

from __future__ import annotations

import re
import sys

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import click

from cli_anything.unreal._version import __version__
from cli_anything.unreal.commands import (
    AppError,
    AppState,
    emit_json,
    error_payload,
    register_commands,
)
from cli_anything.unreal.core.plugin_bridge import get_bundled_version
from cli_anything.unreal.core import remote_client
from cli_anything.unreal.core.tasks import (
    FINAL_TASK_STATUSES,
    cancel_task,
    reconcile_task_state,
    run_task_worker,
    task_progress,
    transition_task,
    wait_for_task,
)


_BRIDGE_VERSION = get_bundled_version() or "unknown"
_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


COMMAND_SPECS = [
    {
        "name": "build compile",
        "description": "Compile the project's C++ code.",
        "async_supported": True,
        "estimated_duration": "300-900s",
        "parameters": [
            {"name": "--project", "required": True},
            {"name": "--config", "required": False},
            {"name": "--platform", "required": False},
            {"name": "--no-wait", "required": False},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "build cook",
        "description": "Cook content assets for the target platform.",
        "async_supported": True,
        "estimated_duration": "120-600s",
        "parameters": [
            {"name": "--project", "required": True},
            {"name": "--platform", "required": False},
            {"name": "--no-wait", "required": False},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "build package",
        "description": "Run build, cook, stage and package.",
        "async_supported": True,
        "estimated_duration": "600-1800s",
        "parameters": [
            {"name": "--project", "required": True},
            {"name": "--platform", "required": False},
            {"name": "--config", "required": False},
            {"name": "--output-dir", "required": False},
            {"name": "--no-wait", "required": False},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "preflight",
        "description": "Check engine/project build and Remote Control readiness before launching.",
        "async_supported": False,
        "estimated_duration": "<5s",
        "parameters": [
            {"name": "--project", "required": True},
        ],
    },
    {
        "name": "editor launch",
        "description": "Launch the Unreal Editor and wait for Remote Control API readiness.",
        "async_supported": True,
        "estimated_duration": "30-180s",
        "parameters": [
            {"name": "--project", "required": True},
            {"name": "--map", "required": False},
            {"name": "--no-wait", "required": False},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "editor viewport camera",
        "description": "Read the active Level Viewport camera.",
        "async_supported": False,
        "estimated_duration": "<5s",
        "parameters": [
            {"name": "--project", "required": True},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "editor viewport bookmark jump",
        "description": "Jump the Level Viewport camera to a saved bookmark using the numeric shortcut.",
        "async_supported": False,
        "estimated_duration": "<5s",
        "parameters": [
            {"name": "--project", "required": True},
            {"name": "--index", "required": True},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "editor new-level",
        "description": "Create and open a new level via LevelEditorSubsystem.",
        "async_supported": False,
        "estimated_duration": "5-30s",
        "parameters": [
            {"name": "LEVEL_PATH", "required": True},
            {"name": "--template", "required": False},
        ],
    },
    {
        "name": "editor new-blank-level",
        "description": "Create an unsaved transient blank level without Unreal Python world references.",
        "async_supported": False,
        "estimated_duration": "5-30s",
        "parameters": [
            {"name": "--discard-dirty-map", "required": False},
            {"name": "--timeout", "required": False},
        ],
    },
    {
        "name": "editor open-level",
        "description": "Open a level, or explicitly reload the active map through a fresh editor process.",
        "async_supported": True,
        "estimated_duration": "5-300s",
        "parameters": [
            {"name": "LEVEL_PATH", "required": True},
            {"name": "--timeout", "required": False},
            {"name": "--reload", "required": False},
            {"name": "--discard-unsaved", "required": False},
        ],
    },
    {
        "name": "editor save-level",
        "description": "Save the current level via LevelEditorSubsystem.",
        "async_supported": False,
        "estimated_duration": "5-30s",
        "parameters": [],
    },
    {
        "name": "umg create",
        "description": "Create a Widget Blueprint with a root widget.",
        "async_supported": False,
        "estimated_duration": "5-30s",
        "parameters": [
            {"name": "WIDGET_PATH", "required": True},
            {"name": "--root-class", "required": False},
            {"name": "--root-name", "required": False},
            {"name": "--force", "required": False},
            {"name": "--variable", "required": False},
        ],
    },
    {
        "name": "umg add-widget",
        "description": "Add a widget to a CanvasPanel in a Widget Blueprint.",
        "async_supported": False,
        "estimated_duration": "5-30s",
        "parameters": [
            {"name": "WIDGET_PATH", "required": True},
            {"name": "--type", "required": True},
            {"name": "--name", "required": True},
            {"name": "--parent", "required": False},
            {"name": "--text", "required": False},
            {"name": "--x", "required": False},
            {"name": "--y", "required": False},
            {"name": "--w", "required": False},
            {"name": "--h", "required": False},
            {"name": "--z", "required": False},
            {"name": "--variable", "required": False},
        ],
    },
    {
        "name": "umg tree",
        "description": "Inspect the design-time WidgetTree for a Widget Blueprint.",
        "async_supported": False,
        "estimated_duration": "<5s",
        "parameters": [
            {"name": "WIDGET_PATH", "required": True},
        ],
    },
]


def _iter_click_commands(command: click.Command, prefix: str = ""):
    if not isinstance(command, click.Group):
        return
    for name, subcommand in sorted(command.commands.items()):
        if getattr(subcommand, "hidden", False):
            continue
        full_name = f"{prefix} {name}".strip()
        if isinstance(subcommand, click.Group):
            yield from _iter_click_commands(subcommand, full_name)
        else:
            yield full_name, subcommand


def _click_parameter_specs(command: click.Command) -> list[dict]:
    parameters: list[dict] = []
    for param in command.params:
        if isinstance(param, click.Argument):
            name = param.name.upper() if param.name else "ARG"
            parameters.append({"name": name, "required": param.required})
            continue
        if isinstance(param, click.Option):
            option_name = next((opt for opt in param.opts if opt.startswith("--")), param.opts[0])
            parameters.append({"name": option_name, "required": param.required})
    return parameters


def _command_specs() -> list[dict]:
    """Return curated command metadata plus any callable Click command not curated yet."""
    specs_by_name = {spec["name"]: dict(spec) for spec in COMMAND_SPECS}
    for name, command in _iter_click_commands(cli):
        if name in specs_by_name:
            continue
        description = (command.short_help or command.help or "").strip().splitlines()
        specs_by_name[name] = {
            "name": name,
            "description": description[0] if description else "",
            "async_supported": False,
            "estimated_duration": None,
            "parameters": _click_parameter_specs(command),
        }
    return [specs_by_name[name] for name in sorted(specs_by_name)]


def _fix_argv_msys2():
    import os

    if sys.platform != "win32" or not any(os.environ.get(name) for name in ("MSYSTEM", "MSYSTEM_PREFIX", "MSYS")):
        return

    virtual_roots = {"Game", "Engine", "Script"}
    fixed = []
    for arg in sys.argv:
        if (
            len(arg) >= 3
            and arg[0].isalpha()
            and arg[1] == ":"
            and arg[2] in ("/", "\\")
            and not os.path.exists(arg)
            and not os.path.exists(arg.split("*")[0])
        ):
            rest = arg[2:].replace("\\", "/")
            parts = rest.strip("/").split("/")
            msys_prefix_len = 0
            for i in range(len(parts)):
                candidate = arg[0:3] + "/".join(parts[: i + 1])
                if os.path.isdir(candidate):
                    msys_prefix_len = i + 1
                else:
                    break
            remaining = parts[msys_prefix_len:]
            if remaining and remaining[0] in virtual_roots:
                fixed.append("/" + "/".join(remaining))
            else:
                fixed.append(arg)
        else:
            fixed.append(arg)
    sys.argv = fixed


def _parse_run_script_option_suffix(text: str) -> list[str] | None:
    """Parse options that Click permits after ``run-script -c CODE``."""
    tokens = text.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--no-save", "-h", "--help"):
            index += 1
            continue
        if token == "--timeout":
            if index + 1 >= len(tokens):
                return None
            try:
                int(tokens[index + 1])
            except ValueError:
                return None
            index += 2
            continue
        if token.startswith("--timeout="):
            try:
                int(token.partition("=")[2])
            except ValueError:
                return None
            index += 1
            continue
        return None
    return tokens


def _decode_windows_inline_code(text: str) -> str:
    """Decode quote escapes while retaining quotes PowerShell left unescaped."""
    decoded: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            decoded.append(text[index])
            index += 1
            continue

        slash_start = index
        while index < len(text) and text[index] == "\\":
            index += 1
        slash_count = index - slash_start
        if index < len(text) and text[index] == '"':
            decoded.append("\\" * (slash_count // 2))
            decoded.append('"')
            index += 1
        else:
            decoded.append("\\" * slash_count)
    return "".join(decoded)


def _repair_windows_run_script_code_argv(argv: list[str], raw_command_line: str) -> list[str]:
    """Recover PowerShell-stripped quotes in ``editor run-script -c``.

    Windows PowerShell 5.1 does not escape embedded double quotes when it
    serializes a native-process argument. The raw command line still contains
    those quotes, but the C runtime removes them (and can split a quoted Python
    string containing spaces) before Python populates ``sys.argv``.
    """
    command_index = next(
        (
            index
            for index in range(len(argv) - 1)
            if argv[index:index + 2] == ["editor", "run-script"]
        ),
        None,
    )
    if command_index is None:
        return argv

    code_option_index = next(
        (
            index
            for index in range(command_index + 2, len(argv))
            if argv[index] in ("-c", "--code") or argv[index].startswith("--code=")
        ),
        None,
    )
    if code_option_index is None:
        return argv

    command_match = re.search(r"(?<!\S)editor\s+run-script(?=\s|$)", raw_command_line)
    if command_match is None:
        return argv
    option_match = re.search(
        r"(?<!\S)(?:-c|--code)(?:=|\s+)",
        raw_command_line[command_match.end():],
    )
    if option_match is None:
        return argv

    value_start = command_match.end() + option_match.end()
    raw_value = raw_command_line[value_start:]
    suffix: list[str] | None = None
    code_text: str | None = None

    if raw_value.startswith('"'):
        quote_positions = [
            index for index, char in enumerate(raw_value[1:], start=1) if char == '"'
        ]
        for quote_index in reversed(quote_positions):
            candidate_suffix = _parse_run_script_option_suffix(raw_value[quote_index + 1:])
            if candidate_suffix is not None:
                # Two quotes alone are ambiguous and already parse correctly.
                # A nested quote proves that PowerShell discarded Python syntax.
                if len(quote_positions) < 3:
                    return argv
                code_text = raw_value[1:quote_index]
                suffix = candidate_suffix
                break
    else:
        value_match = re.match(r"\S+", raw_value)
        if value_match is not None:
            candidate_suffix = _parse_run_script_option_suffix(raw_value[value_match.end():])
            if candidate_suffix is not None:
                code_text = value_match.group(0)
                suffix = candidate_suffix

    if code_text is None or suffix is None:
        return argv

    repaired_prefix = argv[:code_option_index]
    option = argv[code_option_index]
    repaired_prefix.append("--code" if option.startswith("--code=") else option)
    return repaired_prefix + [_decode_windows_inline_code(code_text)] + suffix


def _fix_argv_windows_run_script_code():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
        raw_command_line = ctypes.windll.kernel32.GetCommandLineW()
    except (AttributeError, OSError):
        return
    if raw_command_line:
        sys.argv = _repair_windows_run_script_code_argv(sys.argv, raw_command_line)


def _default_output_mode() -> str:
    return "text" if sys.stdout.isatty() else "json"


class RemoteRoutingGroup(click.Group):
    """Intercept remote commands before local project and Click command parsing."""

    def main(self, args=None, prog_name=None, complete_var=None, standalone_mode=True, **extra):
        args = list(sys.argv[1:] if args is None else args)
        try:
            forwarded, profile = remote_client.routing_profile(args)
            if profile is not None:
                code = remote_client.run_remote(profile, forwarded)
                if standalone_mode:
                    raise SystemExit(code)
                return code
        except remote_client.RemoteError as exc:
            emit_json(error_payload(exc.code, exc.message, suggestion=exc.suggestion, details=exc.details))
            if standalone_mode:
                raise SystemExit(exc.exit_code)
            return exc.exit_code
        return super().main(args=forwarded, prog_name=prog_name, complete_var=complete_var,
                            standalone_mode=standalone_mode, **extra)


@click.group(cls=RemoteRoutingGroup, invoke_without_command=True, context_settings=_CONTEXT_SETTINGS)
@click.version_option(
    __version__,
    prog_name="ue-cli",
    message=f"%(prog)s, version %(version)s\nCliAnythingBridge bundled version {_BRIDGE_VERSION}",
)
@click.option("--output", "output_mode", type=click.Choice(["json", "text"]), default=None)
@click.option("--project", "project_path", type=click.Path(), help="Path to .uproject file")
@click.option("--port", type=int, default=None, help="Editor Remote Control API port (auto-detected from a unique live editor or project config if omitted)")
@click.option("--list-commands", is_flag=True, help="List CLI commands in a machine-readable format")
@click.option("--remote", "remote_name", default=None, help="Run commands on a named remote host.")
@click.option("--local", "local_only", is_flag=True, help="Execute locally, ignoring the default remote connection.")
@click.pass_context
def cli(ctx, output_mode, project_path, port, list_commands, remote_name, local_only):
    state = AppState()
    state.output_mode = output_mode or _default_output_mode()
    state.json_output = state.output_mode == "json"
    state.port_is_explicit = port is not None
    ctx.obj = state

    if project_path:
        try:
            state.session.load_project(project_path)
        except FileNotFoundError:
            emit_json(error_payload("PROJECT_NOT_FOUND", f"Project not found: {project_path}"))
            raise SystemExit(3)
    state.project_is_explicit = project_path is not None

    if port is not None:
        state.session.port = port
    elif state.session.project_dir:
        from cli_anything.unreal.utils.ue_backend import get_editor_binary_prefix, read_rc_port
        ini_port = read_rc_port(
            state.session.project_dir,
            editor_binary_prefix=get_editor_binary_prefix(state.session.engine_root),
        )
        if ini_port is not None:
            state.session.port = ini_port

    if list_commands:
        emit_json(_command_specs())
        return

    if ctx.invoked_subcommand is None:
        if state.json_output:
            emit_json({"name": "ue-cli", "commands": _command_specs()})
        else:
            from cli_anything.unreal.commands.repl import repl_cmd

            ctx.invoke(repl_cmd)


@cli.group("_task-worker", hidden=True)
def task_worker_group():
    pass


@task_worker_group.command("run")
@click.argument("task_id")
def task_worker_run(task_id):
    try:
        run_task_worker(task_id)
        emit_json({"task_id": task_id, "status": "accepted"})
    except FileNotFoundError:
        emit_json(error_payload("TASK_NOT_FOUND", f"Task not found: {task_id}"))
        raise SystemExit(3)
    except Exception as e:
        transition_task(
            task_id,
            status="failed",
            phase="exited",
            error={"code": "TASK_EXECUTION_FAILED", "message": str(e)},
            result_patch={"exception_type": type(e).__name__},
        )
        emit_json(error_payload("TASK_EXECUTION_FAILED", str(e)))
        raise SystemExit(1)


@cli.group("task")
def task_group():
    """Background task management."""


@task_group.command("status")
@click.argument("task_id")
def task_status_cmd(task_id):
    task = reconcile_task_state(task_id)
    if task is None:
        emit_json(error_payload("TASK_NOT_FOUND", f"Task not found: {task_id}"))
        raise SystemExit(3)
    emit_json(task_progress(task))


@task_group.command("wait")
@click.argument("task_id")
@click.option(
    "--timeout",
    type=click.IntRange(min=0),
    default=None,
    help="Maximum seconds to wait; omit to wait indefinitely.",
)
def task_wait_cmd(task_id, timeout):
    task = reconcile_task_state(task_id)
    if task is None:
        emit_json(error_payload("TASK_NOT_FOUND", f"Task not found: {task_id}"))
        raise SystemExit(3)

    final_task = wait_for_task(task_id, timeout)
    if final_task is None:
        task = reconcile_task_state(task_id)
        if task is None:
            emit_json(error_payload("TASK_NOT_FOUND", f"Task not found: {task_id}"))
            raise SystemExit(3)
        if task.get("status") in FINAL_TASK_STATUSES:
            final_task = task
        else:
            progress = task_progress(task)
            progress["wait"] = {
                "status": "timeout",
                "code": "TASK_WAIT_TIMEOUT",
                "timeout_seconds": timeout,
                "task_continues": True,
            }
            progress["next_command"] = f"ue-cli task status {task_id}"
            emit_json(progress)
            raise SystemExit(4)

    progress = task_progress(final_task)
    emit_json(progress)
    if final_task.get("status") in {"failed", "timeout"}:
        raise SystemExit(3)
    if final_task.get("status") == "cancelled":
        raise SystemExit(4)


@task_group.command("cancel")
@click.argument("task_id")
def task_cancel_cmd(task_id):
    task = cancel_task(task_id)
    if task is None:
        emit_json(error_payload("TASK_NOT_FOUND", f"Task not found: {task_id}"))
        raise SystemExit(3)
    progress = task_progress(task)
    error = progress.get("error", {})
    if error.get("code") == "TASK_CANCEL_UNSUPPORTED":
        emit_json(error_payload(
            "TASK_CANCEL_UNSUPPORTED",
            error.get("message", "Task cancellation is unsupported."),
            suggestion=f"Poll without redispatching: ue-cli task status {task_id}",
            details=progress,
        ))
        raise SystemExit(4)
    if error.get("code") == "TASK_CANCEL_FAILED":
        emit_json(error_payload(
            "TASK_CANCEL_FAILED",
            error.get("message", "Build task cancellation failed."),
            suggestion="Retry cancellation after inspecting the remaining process diagnostics.",
            details=progress,
        ))
        raise SystemExit(4)
    output_integrity = progress.get("output_integrity", {})
    if output_integrity.get("code") == "BUILD_CANCELLED_OUTPUTS_INCOMPLETE":
        emit_json(error_payload(
            "BUILD_CANCELLED_OUTPUTS_INCOMPLETE",
            output_integrity["message"],
            suggestion=f"Run: {output_integrity['recovery_command']}",
            details=progress,
        ))
        raise SystemExit(4)
    emit_json(progress)


register_commands(cli)


def main():
    _fix_argv_windows_run_script_code()
    _fix_argv_msys2()
    try:
        cli()
    except AppError as e:
        emit_json(error_payload(e.code, e.message, suggestion=e.suggestion, details=e.details))
        raise SystemExit(e.exit_code)


if __name__ == "__main__":
    main()
