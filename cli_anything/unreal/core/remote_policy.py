"""Project scoping for the remote CLI transport.

This is a trusted-development interface, not a sandbox: editor Python, build
scripts and plugins retain the host user's permissions. These checks prevent
accidental context switching and transport recursion, not malicious UE code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import time

import click


class RemoteError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class ParsedCommand:
    argv: list[str]
    command_path: tuple[str, ...]
    params: dict
    observation: bool = False
    output_mode: str = "json"


_CONTROL_COMMANDS = {
    ("task", "status"), ("task", "wait"), ("task", "cancel"),
    ("build", "status"), ("build", "cancel"), ("build", "stop"),
    ("build", "is-building"), ("editor", "status"), ("editor", "cancel"),
    ("status",), ("project", "info"), ("session", "status"),
    ("session", "history"), ("preflight",), ("editor", "preflight"),
    ("confirmation", "list"), ("confirmation", "answer"),
}
_FORBIDDEN_OPTIONS = {"--project", "--port", "--remote", "--local", "--all", "--scan-range", "--pid"}


def same_path(first: str, second: str) -> bool:
    return str(Path(first).resolve()).replace("\\", "/").casefold() == str(Path(second).resolve()).replace("\\", "/").casefold()


def parse_remote_command(argv: list[str]) -> ParsedCommand:
    """Validate with the live Click parser without invoking any callbacks.

Every ordinary leaf command is transported automatically. Only transport,
interactive/install entrypoints and context-overriding options are excluded.
The child CLI still performs its full normal validation and error reporting.
"""
    from cli_anything.unreal.unreal_cli import cli

    if not isinstance(argv, list) or not argv or len(argv) > 512:
        raise RemoteError("INVALID_ARGV", "argv must contain 1 to 512 command arguments.")
    if any(not isinstance(value, str) or "\0" in value or len(value) > 262144 for value in argv):
        raise RemoteError("INVALID_ARGV", "Command arguments must be bounded strings without NUL bytes.")
    if sum(len(value.encode("utf-8")) for value in argv) > 1048576:
        raise RemoteError("INVALID_ARGV", "Command arguments exceed 1 MiB.")
    command = cli
    # The same root settings as normal Click dispatch are essential here.
    # get_params() lazily caches help options on shared command objects; a
    # context without the configured -h alias would corrupt later local help.
    context = click.Context(cli, info_name="ue-cli", **cli.context_settings)
    try:
        root_params, remaining, _ = cli.make_parser(context).parse_args(list(argv))
    except click.ClickException as exc:
        raise RemoteError("INVALID_ARGV", exc.format_message()) from exc
    if set(root_params) - {"output_mode"}:
        raise RemoteError("REMOTE_CONTEXT_FIXED", "Only --output may be supplied as a root option; the host fixes project and connection context.")
    output_mode = root_params.get("output_mode", "json")
    if output_mode not in {"json", "text"}:
        raise RemoteError("INVALID_ARGV", "--output must be json or text.")
    command_argv = list(remaining)
    command_path = []
    values = {}
    help_requested = False
    try:
        while isinstance(command, click.Group):
            if not remaining:
                raise RemoteError("COMMAND_REQUIRED", "Choose a complete CLI command.")
            name = remaining.pop(0)
            if name.startswith("_") or name in {"remote", "repl", "install-skills"}:
                raise RemoteError("REMOTE_COMMAND_FORBIDDEN", "Interactive, installation and transport entrypoints cannot execute remotely.")
            child = command.get_command(context, name)
            if child is None or child.hidden:
                raise RemoteError("UNKNOWN_COMMAND", f"Unknown or unavailable command: {name}")
            command = child
            command_path.append(name)
            context = click.Context(command, parent=context, info_name=name, **command.context_settings)
            raw, remaining, _order = command.make_parser(context).parse_args(remaining)
            for param in command.get_params(context):
                if isinstance(param, click.Option) and param.name in raw and set(param.opts) & _FORBIDDEN_OPTIONS:
                    raise RemoteError("REMOTE_CONTEXT_FIXED", f"Option {param.opts[0]} cannot override the host's fixed project context.")
            help_requested = bool(raw.get("help"))
            values = {}
            for param in command.params:
                raw_value = raw.get(param.name, param.get_default(context))
                # Click 8.3+ represents an omitted optional value with UNSET.
                # This boundary intentionally skips callbacks and prompts.
                if raw_value is getattr(click.core, "UNSET", None):
                    raw_value = None
                if raw_value is None and param.required and not help_requested:
                    raise click.MissingParameter(ctx=context, param=param)
                value = param.type_cast_value(context, raw_value)
                if param.required and param.value_is_missing(value) and not help_requested:
                    raise click.MissingParameter(ctx=context, param=param)
                values[param.name] = value
            context.params = values
            if help_requested:
                if remaining:
                    raise RemoteError("INVALID_ARGV", "Help requests must not contain additional commands.")
                break
        if remaining:
            raise RemoteError("INVALID_ARGV", "Unexpected trailing command arguments.")
    except click.ClickException as exc:
        raise RemoteError("INVALID_ARGV", exc.format_message()) from exc
    path = tuple(command_path)
    return ParsedCommand(command_argv, path, values, help_requested or path in _CONTROL_COMMANDS, output_mode)


def require_project_task(task_id: str, project_path: str) -> dict:
    from cli_anything.unreal.core.tasks import load_task

    # Task paths in the local CLI are trusted input. The network boundary must
    # reject separators before asking that subsystem to load a file.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", task_id):
        raise RemoteError("INVALID_TASK_ID", "Invalid task identifier.")
    task = load_task(task_id, timeout=2.0)
    if task is None:
        raise RemoteError("TASK_NOT_FOUND", "Task does not exist.", 404)
    owner = str(task.get("payload", {}).get("project_path") or "")
    if not owner or not same_path(owner, project_path):
        raise RemoteError("TASK_PROJECT_MISMATCH", "Task belongs to another project.", 403)
    return task


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RemoteError("TASK_STATE_UNAVAILABLE", "Timed out checking the project's active task state.", 409)
    return remaining


def _task_snapshots(deadline: float) -> list[dict]:
    """Read atomic published records, without taking thousands of file locks.

The general task snapshot iterator intentionally skips unreadable records.
Admission must instead reject an incomplete scan, because the missing record
could own a running build. No task state or cancellation files are modified.
"""
    from cli_anything.unreal.core.tasks import task_data_path

    records = []
    for path in task_data_path("remote-admission").parent.glob("*.json"):
        _remaining(deadline)
        try:
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > 32 * 1024 * 1024:
                    raise ValueError("Oversized task record")
                data = handle.read(size + 1)
            record = json.loads(data)
            if not isinstance(record, dict):
                raise ValueError("Invalid task record")
        except (OSError, ValueError) as exc:
            raise RemoteError("TASK_STATE_UNAVAILABLE", "Cannot read a complete published task snapshot.", 409) from exc
        _remaining(deadline)
        # Non-task metadata (for example editor_remote_unreachable.json) is
        # stored in this same directory by the native task subsystem.
        if "task_id" in record:
            if not isinstance(record.get("payload", {}), dict):
                raise RemoteError("TASK_STATE_UNAVAILABLE", "Invalid published task payload.", 409)
            records.append(record)
    return records


def _project_editor_processes(project_path: str, deadline: float) -> list[dict] | None:
    """Return verified same-project processes; None means unknown, never none.

find_running_editors() returns [] on discovery errors for interactive status.
That fallback cannot prove absence at an admission boundary. Use an explicit
success envelope and retain its existing command-line project parser instead.
"""
    from cli_anything.unreal.utils.ue_backend import _extract_uproject_from_cmdline

    if sys.platform != "win32":
        return None
    command = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$items=@(Get-CimInstance Win32_Process -Filter "
        "\"Name like '%UnrealEditor%' OR Name like '%UE4Editor%'\"); "
        "@{ok=$true; processes=@($items | Select-Object ProcessId,CommandLine,"
        "@{Name='StartedAt';Expression={([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds()/1000.0}})} "
        "| ConvertTo-Json -Compress -Depth 4"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
            timeout=min(5.0, _remaining(deadline)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _remaining(deadline)
        envelope = json.loads(result.stdout.lstrip("\ufeff"))
        if result.returncode or envelope.get("ok") is not True or not isinstance(envelope.get("processes"), list):
            return None
        matched = []
        for process in envelope["processes"]:
            command_line = process.get("CommandLine")
            project = _extract_uproject_from_cmdline(command_line) if isinstance(command_line, str) else ""
            # An editor whose project cannot be determined may be ours.
            if not project:
                return None
            if same_path(project, project_path):
                started = process.get("StartedAt")
                matched.append({"pid": process.get("ProcessId"), "started_at": float(started) if isinstance(started, (float, int)) and started > 0 else None})
        return matched
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return None


def active_project_tasks(project_path: str) -> list[dict]:
    from cli_anything.unreal.core.tasks import (
        BUILD_TASK_COMMANDS, EDITOR_OBSERVATION_TASK_COMMANDS,
        FINAL_TASK_STATUSES, _probe_task_process,
    )

    deadline = time.monotonic() + 15.0
    active = []
    observations = []
    for task in _task_snapshots(deadline):
        if task.get("status") in FINAL_TASK_STATUSES:
            continue
        owner = str(task.get("payload", {}).get("project_path") or "")
        if not owner or not same_path(owner, project_path):
            continue
        _remaining(deadline)
        if task.get("command") in EDITOR_OBSERVATION_TASK_COMMANDS:
            observations.append(task)
            continue
        if task.get("worker_pid"):
            worker = _probe_task_process(task, "worker")
            _remaining(deadline)
            if worker.get("state") in {"exited", "pid_reused"}:
                if task.get("command") in BUILD_TASK_COMMANDS:
                    build = _probe_task_process(task, "build")
                    _remaining(deadline)
                    if build.get("state") in {"exited", "pid_reused", "not_started"}:
                        continue
                elif task.get("command") == "editor.launch":
                    continue
        # Missing or uncertain worker evidence remains blocking, including
        # newly submitted tasks whose worker PID is not yet published.
        active.append(task)
    if observations:
        processes = _project_editor_processes(project_path, deadline)
        for task in observations:
            if processes == []:
                continue
            created = task.get("created_at")
            if processes is not None and isinstance(created, (float, int)) and created > 0:
                if all(process.get("started_at") is not None and process["started_at"] > created for process in processes):
                    # The request predates every current same-project editor;
                    # it cannot still be executing in a newly started process.
                    continue
            # A timeout observation is not proof of completion. Preserve the
            # lock when its editor is alive or process ownership is unknown.
            active.append(task)
    _remaining(deadline)
    return active
