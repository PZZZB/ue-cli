"""Named remote connections. Credentials live in separate private token files."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from cli_anything.unreal.errors import UeCliError


class RemoteError(UeCliError):
    """A remote operation failed without falling back to local execution."""


def config_path(path=None) -> Path:
    return Path(path or os.environ.get("UE_CLI_REMOTE_CONFIG") or
                Path.home() / ".ue-cli" / "remotes.json").expanduser()


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise RemoteError("REMOTE_CONFIG_INVALID", "Remote names must contain 1-64 letters, numbers, dots, underscores or hyphens.")


def validate_url(url: str) -> str:
    try:
        value = urlsplit(url)
        port = value.port
    except (ValueError, TypeError) as exc:
        raise RemoteError("REMOTE_CONFIG_INVALID", "Invalid remote URL.") from exc
    if (value.scheme not in {"http", "https"} or not value.hostname or
            value.username is not None or value.password is not None or
            value.query or value.fragment or value.path not in {"", "/"}):
        raise RemoteError("REMOTE_CONFIG_INVALID", "Remote URL must be an HTTP(S) origin without credentials, query or path.")
    if port is not None and port < 1:
        raise RemoteError("REMOTE_CONFIG_INVALID", "Invalid remote port.")
    return url.rstrip("/")


def load_config(path=None) -> dict:
    target = config_path(path)
    if not target.exists():
        return {"version": 1, "default": None, "profiles": {}}
    try:
        value = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RemoteError("REMOTE_CONFIG_INVALID", f"Cannot read remote configuration: {target}") from exc
    if (not isinstance(value, dict) or value.get("version") != 1 or
            not isinstance(value.get("profiles"), dict)):
        raise RemoteError("REMOTE_CONFIG_INVALID", "Unsupported remote configuration format.")
    for name, profile in value["profiles"].items():
        _validate_name(name)
        if not isinstance(profile, dict) or not isinstance(profile.get("token_file"), str):
            raise RemoteError("REMOTE_CONFIG_INVALID", f"Invalid remote profile: {name}")
        validate_url(profile.get("url"))
        if profile.get("local_project") is not None and not isinstance(profile["local_project"], str):
            raise RemoteError("REMOTE_CONFIG_INVALID", f"Invalid local project for remote: {name}")
    default = value.get("default")
    if default is not None and default not in value["profiles"]:
        raise RemoteError("REMOTE_CONFIG_INVALID", "Default remote does not name an existing profile.")
    return value


def save_config(data: dict, path=None) -> None:
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".remotes-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def add_profile(name: str, url: str, token_file: str, local_project=None, path=None) -> dict:
    _validate_name(name)
    profile = {"url": validate_url(url), "token_file": str(Path(token_file).expanduser().resolve())}
    if local_project:
        profile["local_project"] = str(Path(local_project).expanduser().resolve())
    data = load_config(path)
    data["profiles"][name] = profile
    save_config(data, path)
    return {"name": name, **profile}


def select_profile(name: str | None, path=None) -> dict:
    data = load_config(path)
    if name is not None and name not in data["profiles"]:
        raise RemoteError("REMOTE_NOT_FOUND", f"Remote profile not found: {name}")
    data["default"] = name
    save_config(data, path)
    return {"default": name}


def list_profiles(path=None) -> list[dict]:
    data = load_config(path)
    return [{"name": name, **profile, "default": name == data.get("default")}
            for name, profile in sorted(data["profiles"].items())]


def get_profile(name=None, path=None) -> dict | None:
    data = load_config(path)
    chosen = name if name is not None else data.get("default")
    if chosen is None:
        return None
    if chosen not in data["profiles"]:
        raise RemoteError("REMOTE_NOT_FOUND", f"Remote profile not found: {chosen}")
    profile = dict(data["profiles"][chosen])
    token_path = Path(profile["token_file"]).expanduser()
    if not token_path.is_absolute():
        token_path = config_path(path).parent / token_path
    profile["token_file"] = str(token_path)
    profile["name"] = chosen
    return profile
