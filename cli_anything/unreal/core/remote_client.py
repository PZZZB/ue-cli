"""Transport ordinary ue-cli argv to a project-bound remote ue-cli process."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import sys
import time
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import click

from cli_anything.unreal.core.remote_config import RemoteError, config_path, get_profile, validate_url


PROTOCOL_VERSION = 1
_MAX_RESPONSE = 32 * 1024 * 1024
_MAX_ARTIFACT = 256 * 1024 * 1024
_LOCAL_COMMANDS = {"remote", "install-skills", "_task-worker"}
_ROOT_VALUE_OPTIONS = {"--output", "--project", "--port"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def split_routing_args(argv: list[str]) -> tuple[list[str], str | None, bool, str | None]:
    """Only root options are routing flags; command values remain byte-for-byte."""
    forwarded: list[str] = []
    remote = None
    local = False
    command = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--local":
            local = True
        elif arg == "--remote" or arg.startswith("--remote="):
            if arg == "--remote":
                index += 1
                if index >= len(argv):
                    raise RemoteError("REMOTE_ARGUMENT_INVALID", "--remote requires a profile name.", exit_code=2)
                remote = argv[index]
            else:
                remote = arg.partition("=")[2]
            if not remote:
                raise RemoteError("REMOTE_ARGUMENT_INVALID", "--remote requires a profile name.", exit_code=2)
        elif arg in _ROOT_VALUE_OPTIONS:
            forwarded.append(arg)
            index += 1
            if index < len(argv):
                forwarded.append(argv[index])
        elif arg == "--":
            forwarded.extend(argv[index:])
            command = argv[index + 1] if index + 1 < len(argv) else None
            break
        elif not arg.startswith("-"):
            command = arg
            forwarded.extend(argv[index:])
            break
        else:
            forwarded.append(arg)
        index += 1
    if remote is not None and local:
        raise RemoteError("REMOTE_ARGUMENT_INVALID", "--remote and --local cannot be combined.", exit_code=2)
    return forwarded, remote, local, command


def routing_profile(argv: list[str]) -> tuple[list[str], dict | None]:
    forwarded, remote, local, command = split_routing_args(argv)
    root_args = forwarded[:forwarded.index(command)] if command in forwarded else forwarded
    if (local or command in _LOCAL_COMMANDS or command is None or
            any(arg in {"--help", "-h", "--version", "--list-commands"} for arg in root_args)):
        return forwarded, None
    profile = get_profile(remote)
    if command == "repl" and profile is not None:
        raise RemoteError("REMOTE_REPL_UNSUPPORTED", "Remote interactive sessions are unsupported; invoke individual ue-cli commands.", exit_code=2)
    return forwarded, profile


class RemoteClient:
    def __init__(self, profile: dict, *, timeout=30, poll_interval=0.2):
        if profile is None:
            raise RemoteError("REMOTE_NOT_FOUND", "No remote connection was selected; use remote add or remote use first.")
        self.profile = profile
        self.url = validate_url(profile["url"])
        self.timeout = timeout
        self.poll_interval = poll_interval
        try:
            self.token = Path(profile["token_file"]).read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise RemoteError("REMOTE_TOKEN_UNAVAILABLE", "Cannot read the remote token file.") from exc
        if not self.token or any(char in self.token for char in "\r\n"):
            raise RemoteError("REMOTE_TOKEN_INVALID", "Remote token file must contain one nonempty token.")
        # Never send VM control credentials through the user's HTTP proxy, or a redirect.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, payload=None, *, binary=False):
        if not path.startswith("/v1/") or urlsplit(path).netloc or ".." in path:
            raise RemoteError("REMOTE_PROTOCOL_ERROR", "Server supplied an invalid API path.")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                limit = _MAX_ARTIFACT if binary else _MAX_RESPONSE
                data = response.read(limit + 1)
        except HTTPError as exc:
            code = "REMOTE_AUTH_FAILED" if exc.code in {401, 403} else "REMOTE_HTTP_ERROR"
            message = f"Remote service returned HTTP {exc.code}; no local command was executed."
            try:
                error_body = json.loads(exc.read(_MAX_RESPONSE))
            except (ValueError, OSError, AttributeError):
                error_body = None
            if isinstance(error_body, dict) and error_body.get("protocol_version") == PROTOCOL_VERSION:
                error = error_body.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
                    code, message = error["code"], error["message"]
            raise RemoteError(code, message,
                              details={"http_status": exc.code}) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise RemoteError("REMOTE_UNAVAILABLE", "Remote service is unreachable; no local command was executed.",
                              details={"exception_type": type(exc).__name__}) from exc
        if len(data) > limit:
            raise RemoteError("REMOTE_PROTOCOL_ERROR", "Remote response exceeded the client size limit.")
        if binary:
            return data
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise RemoteError("REMOTE_PROTOCOL_ERROR", "Remote service returned invalid JSON.") from exc
        if not isinstance(result, dict) or result.get("protocol_version") != PROTOCOL_VERSION:
            raise RemoteError("REMOTE_PROTOCOL_MISMATCH", "Remote service does not support protocol version 1.")
        return result

    def health(self) -> dict:
        return self.request("GET", "/v1/health")

    def execute(self, argv: list[str], stdin: str | None = None) -> dict:
        payload = {"protocol_version": PROTOCOL_VERSION, "argv": argv}
        if stdin is not None:
            payload["stdin"] = stdin
        try:
            job = self.request("POST", "/v1/commands", payload)
        except RemoteError as exc:
            if exc.code == "REMOTE_UNAVAILABLE":
                exc.details = {**(exc.details or {}), "command_may_still_be_running": True,
                               "submission_receipt_lost": True}
            raise
        return self._poll(job)

    def _get_job(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
            raise RemoteError("REMOTE_ARGUMENT_INVALID", "Invalid remote request ID.", exit_code=2)
        try:
            return self.request("GET", f"/v1/commands/{request_id}")
        except RemoteError as exc:
            exc.details = {**(exc.details or {}), "request_id": request_id, "command_may_still_be_running": True}
            name = self.profile.get("name")
            connection = f" --connection {name}" if name else ""
            exc.suggestion = f"Resume the same request without resubmitting: ue-cli remote result {request_id}{connection}"
            raise

    def follow(self, request_id: str) -> dict:
        """Resume polling a prior transport request without re-executing its command."""
        return self._poll(self._get_job(request_id))

    def _poll(self, job: dict) -> dict:
        emitted_stderr = ""
        while True:
            stderr = job.get("stderr", "")
            if not isinstance(stderr, str) or not stderr.startswith(emitted_stderr):
                raise RemoteError("REMOTE_PROTOCOL_ERROR", "Remote stderr stream was malformed.")
            if len(stderr) > len(emitted_stderr):
                click.echo(stderr[len(emitted_stderr):], nl=False, err=True)
                emitted_stderr = stderr
            status = job.get("status")
            if status in {"completed", "failed", "timeout", "cancelled"}:
                if not isinstance(job.get("exit_code"), int) or not isinstance(job.get("stdout"), str):
                    raise RemoteError("REMOTE_PROTOCOL_ERROR", "Remote command result was incomplete.")
                return job
            request_id = job.get("request_id")
            if status not in {"running", "queued"} or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
                raise RemoteError("REMOTE_PROTOCOL_ERROR", "Remote command state was malformed.")
            time.sleep(self.poll_interval)
            job = self._get_job(request_id)


def _path_key(value: str) -> str:
    if os.name == "nt":
        # Resolve VMware shared-folder junctions before comparing the guest alias.
        value = str(Path(value).expanduser().resolve())
    if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _project_roots(profile: dict, health: dict) -> tuple[str | None, str | None]:
    local = profile.get("local_project")
    if local and local.lower().endswith(".uproject"):
        local = ntpath.dirname(local) if "\\" in local else str(Path(local).parent)
    return local, health.get("project_root")


def _map_argument(value: str, local_root: str | None, host_root: str | None) -> str:
    if not local_root or not host_root or value.startswith("/Game/") or value.startswith("/Engine/"):
        return value
    # Only path-shaped arguments participate; arbitrary code and labels are untouched.
    if not (os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")):
        return value
    root_key, value_key = _path_key(local_root), _path_key(value)
    separator = "\\" if "\\" in root_key else os.sep
    if value_key == root_key:
        return host_root
    if value_key.startswith(root_key.rstrip("/\\") + separator):
        suffix = value_key[len(root_key):].lstrip("/\\")
        return str(PureWindowsPath(host_root) / suffix) if "\\" in host_root or re.match(r"^[A-Za-z]:", host_root) else str(Path(host_root) / suffix)
    return value


def _shared_script_path(script: Path, local_root: str | None, host_root: str | None) -> str | None:
    """Keep file execution semantics when the script is already host-accessible."""
    if not local_root or not host_root:
        return None
    root_key, script_key = _path_key(local_root), _path_key(str(script))
    separator = "\\" if "\\" in root_key else os.sep
    if script_key.startswith(root_key.rstrip("/\\") + separator):
        return _map_argument(str(script), local_root, host_root)
    return None


def prepare_command(argv: list[str], profile: dict, health: dict) -> tuple[list[str], str | None, str | None]:
    """Translate a shared project, upload script input, retain local screenshot output."""
    args = list(argv)
    local_root, host_root = _project_roots(profile, health)
    _, _, _, command = split_routing_args(args)
    command_index = args.index(command) if command is not None else len(args)
    index = 0
    while index < command_index:
        arg = args[index]
        if arg == "--project" or arg.startswith("--project="):
            equal = arg.startswith("--project=")
            value = arg.partition("=")[2] if equal else args[index + 1] if index + 1 < command_index else ""
            expected = health.get("project_path")
            mapped = _map_argument(value, local_root, host_root)
            if not expected or _path_key(mapped) not in {_path_key(expected), _path_key(host_root or expected)}:
                raise RemoteError("REMOTE_PROJECT_MISMATCH", "The supplied project does not match this remote's fixed project.")
            count = 1 if equal else 2
            del args[index:index + count]
            command_index -= count
            continue
        index += 1
    stdin = None
    output_path = None
    command_parts = args[command_index:command_index + 2]
    if command_parts == ["editor", "run-script"]:
        index = command_index + 2
        while index < len(args):
            arg = args[index]
            if arg in {"-c", "--code", "--timeout"}:
                index += 2
                continue
            if arg.startswith("--") or (arg.startswith("-c") and arg != "-"):
                index += 1
                continue
            if arg == "-":
                stdin = sys.stdin.read()
            else:
                try:
                    script = Path(arg).expanduser().resolve()
                    if not script.is_file():
                        raise FileNotFoundError(arg)
                    shared_path = _shared_script_path(script, local_root, host_root)
                    if shared_path is not None:
                        # The original file path supplies __file__ and sibling
                        # imports through the host's normal script runner.
                        args[index] = shared_path
                    else:
                        stdin = script.read_text(encoding="utf-8-sig")
                        args[index] = "-"
                except (OSError, UnicodeError) as exc:
                    raise RemoteError("REMOTE_SCRIPT_UNAVAILABLE", f"Cannot read local Python script: {arg}") from exc
            break
    if command_parts == ["screenshot", "capture"]:
        filename = "screenshot"
        for filename_index in range(command_index + 2, len(args)):
            if args[filename_index] == "--filename" and filename_index + 1 < len(args):
                filename = args[filename_index + 1]
            elif args[filename_index].startswith("--filename="):
                filename = args[filename_index].partition("=")[2]
        index = command_index + 2
        while index < len(args):
            arg = args[index]
            if arg == "--path" or arg.startswith("--path="):
                equal = arg.startswith("--path=")
                value_index = index if equal else index + 1
                if value_index >= len(args):
                    raise RemoteError("REMOTE_ARGUMENT_INVALID", "--path requires an output path.", exit_code=2)
                output_path = arg.partition("=")[2] if equal else args[value_index]
                local_path = Path(output_path).expanduser()
                # Host receives a basename only and rewrites it into its managed artifact directory.
                if local_path.is_dir() or output_path.endswith(("/", "\\")):
                    # Capture saves raw PNG plus its optional compressed JPG.
                    stem = Path(filename).stem or "screenshot"
                    name = stem + (".png" if "--no-compress" in args else ".jpg")
                else:
                    name = local_path.name
                args[value_index] = "--path=" + name if equal else name
                break
            index += 1
    # Avoid changing Python source passed through --code, which can contain path literals.
    skip_next = False
    for index in range(command_index + 2, len(args)):
        if skip_next:
            skip_next = False
            continue
        if args[index] in {"--code", "-c", "--path"}:
            skip_next = True
            continue
        if args[index].startswith(("--code=", "--path=")):
            continue
        if args[index].startswith("--") and "=" in args[index]:
            option, _, value = args[index].partition("=")
            args[index] = option + "=" + _map_argument(value, local_root, host_root)
        else:
            args[index] = _map_argument(args[index], local_root, host_root)
    if not any(arg == "--output" or arg.startswith("--output=") for arg in args[:command_index]):
        args[:0] = ["--output", "text" if sys.stdout.isatty() else "json"]
    return args, stdin, output_path


def _replace_paths(value, replacements: dict[str, str]):
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_paths(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, replacements) for key, item in value.items()}
    return value


def download_artifacts(client: RemoteClient, job: dict, output_path=None) -> str:
    stdout = job["stdout"]
    artifacts = job.get("artifacts") or []
    if job.get("stdout_truncated") or (job.get("output_truncated") and "stdout_truncated" not in job):
        raise RemoteError("REMOTE_OUTPUT_TRUNCATED", "The remote command output exceeded the service limit; inspect the host before retrying.",
                          details={"request_id": job.get("request_id"), "command_exit_code": job.get("exit_code")})
    if job.get("stderr_truncated"):
        click.echo("Remote progress output was truncated; inspect the host log_file for complete build logs.", err=True)
    if job.get("artifact_error"):
        raise RemoteError("REMOTE_ARTIFACT_UNAVAILABLE", str(job["artifact_error"]),
                          details={"request_id": job.get("request_id"), "command_exit_code": job.get("exit_code")})
    if not artifacts:
        return _render_result(stdout, job)
    request_id = job.get("request_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
        raise RemoteError("REMOTE_PROTOCOL_ERROR", "Invalid remote artifact request ID.")
    directory = config_path().parent / "remote-artifacts" / client.profile.get("name", "remote") / request_id
    directory.mkdir(parents=True, exist_ok=True)
    replacements = {}
    for artifact in artifacts:
        name = artifact.get("name", "")
        if not name or name in {".", ".."} or "/" in name or "\\" in name or ":" in name:
            raise RemoteError("REMOTE_PROTOCOL_ERROR", "Invalid remote artifact filename.")
        data = client.request("GET", artifact["download_path"], binary=True)
        if len(data) != artifact.get("size") or hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
            raise RemoteError("REMOTE_ARTIFACT_INVALID", "Remote artifact checksum or size did not match.")
        destination = directory / name
        if output_path:
            requested = Path(output_path).expanduser().absolute()
            if requested.is_dir() or str(output_path).endswith(("/", "\\")):
                destination = requested / name
            elif requested.suffix.lower() == Path(name).suffix.lower():
                destination = requested
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        replacements[artifact["host_path"]] = str(destination)
    try:
        parsed = json.loads(stdout)
    except (ValueError, TypeError):
        for host_path, local_path in replacements.items():
            stdout = stdout.replace(host_path, local_path)
        return stdout
    rewritten = json.dumps(_replace_paths(parsed, replacements), indent=2, ensure_ascii=False) + "\n"
    return _render_result(rewritten, job)


def _render_result(stdout: str, job: dict) -> str:
    """Screenshot JSON is transported internally, then rendered as requested."""
    if job.get("effective_output") != "json" or job.get("requested_output") != "text":
        return stdout
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return stdout
    if not isinstance(payload, dict) or payload.get("status") != "success" or "result" not in payload:
        return stdout
    from cli_anything.unreal.utils.output import format_text_output
    return format_text_output(payload["result"])


def run_remote(profile: dict, argv: list[str]) -> int:
    client = RemoteClient(profile)
    health = client.health()
    args, stdin, output_path = prepare_command(argv, profile, health)
    job = client.execute(args, stdin)
    click.echo(download_artifacts(client, job, output_path), nl=False)
    return job["exit_code"]
