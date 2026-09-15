"""File-backed confirmation broker shared by ue-cli and the editor bridge."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from cli_anything.unreal.errors import UeCliError


DEFAULT_LEASE_SECONDS = 900
MINIMUM_BRIDGE_VERSION = (1, 32)
PROTOCOL_VERSION = 1
CONFIRMATION_ID_PATTERN = re.compile(r"[0-9a-fA-F]{32}\Z")


def _normalize_path(path: str | Path) -> str:
    if not path:
        return ""
    return str(Path(path).resolve()).replace("/", "\\").casefold()


def confirmation_bridge_supported(version: str | None) -> bool:
    if not version:
        return False
    try:
        parts = tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return False
    return parts >= MINIMUM_BRIDGE_VERSION


def _confirmation_root(project_path: str | Path) -> Path:
    return (
        Path(project_path).resolve().parent
        / "Saved"
        / "CliAnything"
        / "Confirmations"
    )


def _pid_dir(project_path: str | Path, pid: int) -> Path:
    return _confirmation_root(project_path) / str(int(pid))


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _matching_editors(project_path: str | Path) -> list[dict]:
    from cli_anything.unreal.utils.ue_backend import find_running_editors

    wanted = _normalize_path(project_path)
    by_pid: dict[int, dict] = {}
    for editor in find_running_editors():
        try:
            pid = int(editor.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or _normalize_path(editor.get("project") or "") != wanted:
            continue
        by_pid[pid] = dict(editor)
    return [by_pid[pid] for pid in sorted(by_pid)]


def _resolve_editor_pid(project_path: str | Path, pid: int | None) -> int:
    editors = _matching_editors(project_path)
    live_pids = [int(editor["pid"]) for editor in editors]
    if pid is not None:
        pid = int(pid)
        if pid not in live_pids:
            raise UeCliError(
                "EDITOR_PROCESS_NOT_FOUND",
                f"PID {pid} is not a running Unreal Editor for this project.",
                exit_code=3,
                details={"pid": pid, "project": str(Path(project_path).resolve()), "live_pids": live_pids},
            )
        return pid
    if not live_pids:
        raise UeCliError(
            "EDITOR_NOT_RUNNING",
            "No running Unreal Editor was found for this project.",
            exit_code=3,
            suggestion="Launch or attach to the editor, then retry confirmation enable.",
        )
    if len(live_pids) > 1:
        raise UeCliError(
            "EDITOR_TARGET_AMBIGUOUS",
            "Multiple running editors use this project.",
            exit_code=3,
            suggestion="Pass --pid <editor-pid>.",
            details={"project": str(Path(project_path).resolve()), "live_pids": live_pids},
        )
    return live_pids[0]


def _lease_state(project_path: str | Path, pid: int, now: float) -> dict:
    lease_path = _pid_dir(project_path, pid) / "lease.json"
    lease = _read_json(lease_path)
    expires_at = float((lease or {}).get("expires_at") or 0.0)
    enabled = bool(
        lease
        and lease.get("enabled") is True
        and int(lease.get("pid") or 0) == int(pid)
        and expires_at > now
    )
    return {
        "pid": int(pid),
        "enabled": enabled,
        "expires_at": expires_at or None,
        "remaining_seconds": max(0, int(expires_at - now)) if expires_at else 0,
        "lease_id": (lease or {}).get("lease_id"),
    }


def _process_is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, int(pid))
        if not process:
            return False
        kernel32.CloseHandle(process)
        return True
    try:
        os.kill(int(pid), 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _active_brokered_confirmations(
    project_path: str | Path,
    now: float | None = None,
) -> list[dict]:
    """Read the local mailbox without process enumeration or Remote Control."""

    process_dirs = _candidate_pid_dirs(project_path, None)
    if not process_dirs:
        return []
    current_time = time.time() if now is None else float(now)
    confirmations: list[dict] = []
    for process_dir in process_dirs:
        process_pid = int(process_dir.name)
        if not _process_is_alive(process_pid):
            continue
        if not _lease_state(project_path, process_pid, current_time)["enabled"]:
            continue
        for pending_path in sorted(process_dir.glob("pending-*.json")):
            pending = _read_json(pending_path)
            if not pending:
                continue
            confirmations.append({
                **pending,
                "id": str(pending.get("id") or pending_path.stem.removeprefix("pending-")),
                "pid": process_pid,
                "source": "bridge",
                "answerable": bool(pending.get("choices")),
                "stale": False,
            })
    return confirmations


def enable_confirmation_broker(
    project_path: str | Path,
    *,
    pid: int | None = None,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    now: float | None = None,
) -> dict:
    """Arm broker interception for one live editor for a bounded lease."""

    resolved_pid = _resolve_editor_pid(project_path, pid)
    current_time = time.time() if now is None else float(now)
    lease_id = uuid.uuid4().hex
    expires_at = current_time + int(ttl_seconds)
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "enabled": True,
        "lease_id": lease_id,
        "pid": resolved_pid,
        "project_path": str(Path(project_path).resolve()),
        "created_at": current_time,
        "expires_at": expires_at,
    }
    _atomic_write_json(_pid_dir(project_path, resolved_pid) / "lease.json", payload)
    return {
        "status": "enabled",
        "mode": "active_polling",
        "pid": resolved_pid,
        "lease_id": lease_id,
        "expires_at": expires_at,
        "ttl_seconds": int(ttl_seconds),
        "next_command": f"ue-cli --project \"{Path(project_path).resolve()}\" confirmation list",
    }


def _candidate_pid_dirs(project_path: str | Path, pid: int | None) -> list[Path]:
    root = _confirmation_root(project_path)
    if pid is not None:
        return [root / str(int(pid))]
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: int(path.name),
    )


def list_confirmations(
    project_path: str | Path,
    *,
    pid: int | None = None,
    now: float | None = None,
) -> dict:
    """List brokered dialogs plus detectable non-brokered UE windows."""

    from cli_anything.unreal.utils.ue_backend import detect_ue_dialogs

    current_time = time.time() if now is None else float(now)
    live_editors = _matching_editors(project_path)
    live_pids = {int(editor["pid"]) for editor in live_editors}
    if pid is not None:
        live_pids &= {int(pid)}

    confirmations: list[dict] = []
    leases: list[dict] = []
    for process_dir in _candidate_pid_dirs(project_path, pid):
        process_pid = int(process_dir.name)
        leases.append(_lease_state(project_path, process_pid, current_time))
        for pending_path in sorted(process_dir.glob("pending-*.json")):
            pending = _read_json(pending_path)
            if not pending:
                continue
            pending_id = str(pending.get("id") or pending_path.stem.removeprefix("pending-"))
            choices = [str(choice) for choice in pending.get("choices") or []]
            stale = process_pid not in live_pids
            confirmations.append({
                **pending,
                "id": pending_id,
                "pid": process_pid,
                "source": "bridge",
                "answerable": not stale and bool(choices),
                "stale": stale,
            })

    for process_pid in sorted(live_pids):
        if process_pid not in {int(item["pid"]) for item in leases}:
            leases.append(_lease_state(project_path, process_pid, current_time))
        for dialog in detect_ue_dialogs(process_id=process_pid):
            hwnd = int(dialog.get("hwnd") or 0)
            confirmations.append({
                "id": f"window-{process_pid}-{hwnd}",
                "pid": process_pid,
                "source": "window",
                "title": str(dialog.get("title") or ""),
                "hwnd": hwnd,
                "answerable": False,
                "reason": (
                    "Detected window is not a brokered FMessageDialog. "
                    "Inspect it in the editor; ue-cli will not auto-click it."
                ),
            })

    confirmations.sort(key=lambda item: (float(item.get("created_at") or 0.0), str(item.get("id") or "")))
    leases.sort(key=lambda item: int(item["pid"]))
    return {
        "status": "ok",
        "project_path": str(Path(project_path).resolve()),
        "confirmations": confirmations,
        "count": len(confirmations),
        "answerable_count": sum(bool(item.get("answerable")) for item in confirmations),
        "leases": leases,
        "live_editor_pids": sorted(live_pids),
    }


def raise_if_editor_blocked(
    project_path: str | Path,
    *,
    include_windows: bool = False,
) -> None:
    """Raise the public blocked-editor contract when a dialog is pending."""

    brokered = _active_brokered_confirmations(project_path)
    windows: list[dict] = []
    if include_windows:
        result = list_confirmations(project_path)
        active = [
            item
            for item in result["confirmations"]
            if not item.get("stale")
        ]
        windows = [item for item in active if item.get("source") == "window"]
    project = str(Path(project_path).resolve())
    list_command = f'ue-cli --project "{project}" confirmation list'
    if brokered:
        raise UeCliError(
            "EDITOR_BLOCKED_BY_CONFIRMATION",
            "Editor execution is blocked while waiting for a brokered confirmation.",
            exit_code=4,
            suggestion=f"Inspect it first: {list_command}",
            details={
                "delivery_state": "waiting_confirmation",
                "confirmations": brokered,
                "next_command": list_command,
                "answer_command": (
                    f'ue-cli --project "{project}" confirmation answer '
                    "<confirmation_id> --choice <choice>"
                ),
            },
        )
    if windows:
        raise UeCliError(
            "EDITOR_BLOCKED_BY_DIALOG",
            "Editor execution appears blocked by a non-brokered dialog window.",
            exit_code=4,
            suggestion=f"Inspect it first: {list_command}",
            details={
                "delivery_state": "blocked_by_dialog",
                "confirmations": windows,
                "next_command": list_command,
                "answerable_by_cli": False,
            },
        )


def _find_pending(project_path: str | Path, confirmation_id: str) -> tuple[Path, dict] | None:
    for process_dir in _candidate_pid_dirs(project_path, None):
        pending_path = process_dir / f"pending-{confirmation_id}.json"
        pending = _read_json(pending_path)
        if pending:
            return pending_path, pending
    return None


def _normalize_choice(choice: str) -> str:
    normalized = choice.strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {"yesall": "yes_all", "noall": "no_all"}
    return aliases.get(normalized, normalized)


def answer_confirmation(
    project_path: str | Path,
    confirmation_id: str,
    choice: str,
    *,
    wait_seconds: float = 2.0,
    now: float | None = None,
) -> dict:
    """Submit one explicit answer through the bridge's out-of-band mailbox."""

    if confirmation_id.startswith("window-"):
        raise UeCliError(
            "CONFIRMATION_NOT_ANSWERABLE",
            "This item is a detected editor window, not a brokered FMessageDialog.",
            exit_code=3,
            suggestion=(
                "Inspect the window in the editor. For Restore Packages after a session "
                "the Agent started and closed, verify the project, window, and packages, "
                "then use UI automation according to the existing recovery or discard decision. "
                "Ask if ownership or the choice is unclear; do not auto-click third-party dialogs."
            ),
        )
    if CONFIRMATION_ID_PATTERN.fullmatch(confirmation_id) is None:
        raise UeCliError(
            "CONFIRMATION_ID_INVALID",
            "Confirmation id must be the 32-digit hexadecimal id reported by confirmation list.",
            exit_code=2,
            suggestion="Run confirmation list and copy one active source=bridge id exactly.",
        )
    found = _find_pending(project_path, confirmation_id)
    if found is None:
        raise UeCliError(
            "CONFIRMATION_NOT_FOUND",
            f"Pending confirmation not found: {confirmation_id}",
            exit_code=3,
            suggestion="Run confirmation list and use an active bridge confirmation id.",
        )
    pending_path, pending = found
    process_pid = int(pending.get("pid") or pending_path.parent.name)
    submitted_at = time.time() if now is None else float(now)
    if not _process_is_alive(process_pid):
        raise UeCliError(
            "CONFIRMATION_STALE",
            f"Editor PID {process_pid} is no longer running.",
            exit_code=3,
            suggestion="Run confirmation list and ignore stale mailbox entries.",
        )
    if not _lease_state(project_path, process_pid, submitted_at)["enabled"]:
        raise UeCliError(
            "CONFIRMATION_LEASE_EXPIRED",
            "The confirmation lease expired; the dialog is returning to editor UI.",
            exit_code=3,
            suggestion="Inspect the editor window before starting another broker lease.",
        )
    normalized_choice = _normalize_choice(choice)
    allowed = [_normalize_choice(str(item)) for item in pending.get("choices") or []]
    if normalized_choice not in allowed:
        raise UeCliError(
            "CONFIRMATION_INVALID_CHOICE",
            f"Choice '{choice}' is not valid for confirmation {confirmation_id}.",
            exit_code=2,
            details={"confirmation_id": confirmation_id, "allowed_choices": allowed},
        )

    response_path = pending_path.parent / f"response-{confirmation_id}.json"
    if response_path.exists():
        raise UeCliError(
            "CONFIRMATION_ALREADY_ANSWERED",
            f"An answer is already pending for confirmation {confirmation_id}.",
            exit_code=3,
        )
    _atomic_write_json(response_path, {
        "protocol_version": PROTOCOL_VERSION,
        "confirmation_id": confirmation_id,
        "pid": process_pid,
        "choice": normalized_choice,
        "submitted_at": submitted_at,
    })

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while pending_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    resolved = not pending_path.exists()
    return {
        "status": "resolved" if resolved else "submitted",
        "confirmation_id": confirmation_id,
        "pid": process_pid,
        "choice": normalized_choice,
        "resolved": resolved,
        "message": (
            "The editor consumed the answer."
            if resolved
            else "Answer submitted; run confirmation list to verify consumption."
        ),
    }


def disable_confirmation_broker(
    project_path: str | Path,
    *,
    pid: int | None = None,
) -> dict:
    """Disarm one editor; a currently parked dialog falls back to normal UI."""

    resolved_pid = _resolve_editor_pid(project_path, pid)
    process_dir = _pid_dir(project_path, resolved_pid)
    pending_count = len(list(process_dir.glob("pending-*.json"))) if process_dir.is_dir() else 0
    lease_path = process_dir / "lease.json"
    removed = False
    try:
        lease_path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    return {
        "status": "disabled",
        "pid": resolved_pid,
        "lease_removed": removed,
        "pending_fallback_count": pending_count,
        "message": (
            "Broker disabled; pending standard dialogs will fall back to editor UI."
        ),
    }
