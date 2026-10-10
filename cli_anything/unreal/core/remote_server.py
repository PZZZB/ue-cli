"""Authenticated HTTP transport for executing the existing local CLI.

Run this as the interactive developer account when commands may launch UE.
Tokens authorize trusted host execution, including Python and build scripts;
the fixed project and peer filters are context guards, not a code sandbox.
HTTP is intended for a private host-only link or an authenticated tunnel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import uuid

from cli_anything.unreal._version import __version__
from cli_anything.unreal.core.remote_policy import (
    ParsedCommand, RemoteError, active_project_tasks, parse_remote_command,
    require_project_task, same_path,
)


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_STDIN_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACTS = 32
_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class RemoteServerConfig:
    project_path: str
    expected_engine: str
    token_file: str
    bind: str = "127.0.0.1"
    port: int = 17891
    allowed_clients: tuple[str, ...] = ("127.0.0.1",)
    state_directory: str = ""
    max_commands: int = 64
    command_timeout: float = 86400
    retention_seconds: float = 86400


@dataclass
class _Command:
    request_id: str
    parsed: ParsedCommand
    created_at: float = field(default_factory=time.time)
    status: str = "running"
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    output_truncated: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    exit_code: int | None = None
    finished_at: float | None = None
    artifacts: list[dict] = field(default_factory=list)
    artifact_error: str | None = None

    def snapshot(self) -> dict:
        result = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": self.request_id, "status": self.status,
            "stdout": self.stdout, "stderr": self.stderr,
            "exit_code": self.exit_code, "output_truncated": self.output_truncated,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "artifacts": list(self.artifacts), "created_at": self.created_at,
            "finished_at": self.finished_at,
            "requested_output": self.parsed.output_mode,
            "effective_output": "json" if self.parsed.command_path[0] == "screenshot" and not self.parsed.observation else self.parsed.output_mode,
        }
        if self.artifact_error:
            result["artifact_error"] = self.artifact_error
        return result


def _assert_engine(config: RemoteServerConfig) -> None:
    from cli_anything.unreal.utils.ue_backend import find_engine_root

    actual = find_engine_root(config.project_path)
    if not actual or not same_path(actual, config.expected_engine):
        raise RemoteError("REMOTE_ENGINE_MISMATCH", "The fixed project's engine no longer matches expected_engine.", 409)


def _normalize_config(config: RemoteServerConfig) -> RemoteServerConfig:
    project = Path(config.project_path).resolve(strict=True)
    if not project.is_file() or project.suffix.casefold() != ".uproject":
        raise ValueError("project_path must name one existing .uproject file.")
    engine = Path(config.expected_engine).resolve(strict=True)
    if not engine.is_dir():
        raise ValueError("expected_engine must name an existing engine directory.")
    address = ipaddress.ip_address(config.bind)
    if address.version != 4 or address.is_unspecified or not (address.is_loopback or address.is_private):
        raise ValueError("bind must be an explicit private or loopback IPv4 address.")
    if not 0 <= config.port <= 65535:
        raise ValueError("port must be between 0 and 65535.")
    if not config.allowed_clients:
        raise ValueError("At least one exact allowed client address is required.")
    clients = tuple(str(ipaddress.ip_address(value)) for value in config.allowed_clients)
    if any(ipaddress.ip_address(value).is_unspecified for value in clients):
        raise ValueError("Wildcard allowed client addresses are not supported.")
    if not 1 <= config.max_commands <= 256 or config.command_timeout <= 0 or config.retention_seconds <= 0:
        raise ValueError("Invalid command or retention limits.")
    config.project_path = str(project)
    config.expected_engine = str(engine)
    config.bind = str(address)
    config.allowed_clients = clients
    config.state_directory = str(Path(config.state_directory or Path.home() / ".ue-cli" / "remote-server").resolve())
    _assert_engine(config)
    return config


def _replace_option(argv: list[str], option: str, value: str) -> list[str]:
    result = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == option:
            index += 2
            continue
        if item.startswith(option + "="):
            index += 1
            continue
        result.append(item)
        index += 1
    # Screenshot options must precede the optional option terminator.
    insert_at = result.index("--") if "--" in result else len(result)
    return result[:insert_at] + [option, value] + result[insert_at:]


class RemoteCommandService:
    """Own request lifetime; workers execute real CLI argv without a shell."""

    def __init__(self, config: RemoteServerConfig):
        self.config = _normalize_config(config)
        token = Path(config.token_file).read_text(encoding="utf-8-sig").strip()
        if len(token) < 32 or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError("Token file must contain one random token of 32 to 4096 characters.")
        self._token = token.encode("utf-8")
        self._lock = threading.RLock()
        self._commands: dict[str, _Command] = {}
        self._artifact_paths: dict[str, Path] = {}
        self._mutating_request: str | None = None
        self._closed = False
        self._artifacts_root = Path(config.state_directory) / "artifacts"
        self._artifacts_root.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"ue-cli.remote.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = RotatingFileHandler(Path(config.state_directory) / "server.log", maxBytes=1048576, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._logger.addHandler(handler)
        self._logger.info("Remote service initialized; protocol %s", PROTOCOL_VERSION)
        # A restart expires transport request IDs. Retain no orphaned captures
        # beyond the configured lifetime or bounded number of request folders.
        folders = sorted((path for path in self._artifacts_root.iterdir() if _ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink()), key=lambda path: path.stat().st_mtime, reverse=True)
        for index, folder in enumerate(folders):
            if index >= config.max_commands or time.time() - folder.stat().st_mtime > config.retention_seconds:
                shutil.rmtree(folder)

    def authorized(self, peer: str, authorization: str) -> bool:
        if peer not in self.config.allowed_clients or not authorization.startswith("Bearer "):
            return False
        return hmac.compare_digest(authorization[7:].encode("utf-8"), self._token)

    def health(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION, "status": "ok",
            "ue_cli_version": __version__, "project_path": self.config.project_path,
            "project_root": str(Path(self.config.project_path).parent),
            "expected_engine": self.config.expected_engine,
            "capabilities": ["argv", "stdin", "poll", "artifacts"],
        }

    def _prune(self) -> None:
        now = time.time()
        expired = [item for item in self._commands.values() if item.finished_at and now - item.finished_at > self.config.retention_seconds]
        if len(self._commands) >= self.config.max_commands:
            finished = sorted((item for item in self._commands.values() if item.finished_at), key=lambda item: item.finished_at)
            expired.extend(finished[:len(self._commands) - self.config.max_commands + 1])
        for item in expired:
            if item.request_id not in self._commands:
                continue
            self._commands.pop(item.request_id)
            for artifact in item.artifacts:
                self._artifact_paths.pop(artifact["id"], None)
            folder = self._artifacts_root / item.request_id
            # Only server-generated UUID directories below the fixed root.
            if _ID.fullmatch(item.request_id) and folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)

    def submit(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"protocol_version", "argv", "stdin"}:
            raise RemoteError("INVALID_REQUEST", "Expected protocol_version, argv and optional stdin only.")
        if type(payload.get("protocol_version")) is not int or payload["protocol_version"] != PROTOCOL_VERSION:
            raise RemoteError("PROTOCOL_MISMATCH", "Remote protocol version must be 1.", 409)
        parsed = parse_remote_command(payload.get("argv"))
        stdin = payload.get("stdin")
        if stdin is not None:
            if not isinstance(stdin, str) or len(stdin.encode("utf-8")) > MAX_STDIN_BYTES:
                raise RemoteError("INVALID_STDIN", "stdin must be a UTF-8 string no larger than 1 MiB.")
            if parsed.command_path != ("editor", "run-script") or parsed.params.get("script_path") != "-":
                raise RemoteError("INVALID_STDIN", "stdin is supported only by editor run-script -.")
        _assert_engine(self.config)
        task_id = parsed.params.get("task_id")
        if task_id:
            require_project_task(task_id, self.config.project_path)
        with self._lock:
            if self._closed:
                raise RemoteError("SERVER_STOPPING", "The remote server is stopping.", 503)
            self._prune()
            if len(self._commands) >= self.config.max_commands:
                raise RemoteError("REMOTE_BUSY", "Too many active requests; poll existing requests.", 429)
            if not parsed.observation:
                if self._mutating_request:
                    raise RemoteError("REMOTE_BUSY", f"Another command is running: {self._mutating_request}", 409)
                # The CLI may return early with --no-wait. Its persistent native
                # task remains authoritative after this HTTP request completes.
                try:
                    active = active_project_tasks(self.config.project_path)
                except Exception as exc:
                    raise RemoteError("TASK_STATE_UNAVAILABLE", "Cannot verify the project's active task state.", 409) from exc
                if active:
                    raise RemoteError("PROJECT_TASK_ACTIVE", f"Project task is still active: {active[0]['task_id']}. Poll or cancel it before another command.", 409)
            item = _Command(uuid.uuid4().hex, parsed)
            self._commands[item.request_id] = item
            if not parsed.observation:
                self._mutating_request = item.request_id
            threading.Thread(target=self._execute, args=(item, stdin), daemon=True, name=f"ue-remote-{item.request_id[:8]}").start()
            return item.snapshot()

    def get(self, request_id: str) -> dict:
        if not _ID.fullmatch(request_id):
            raise RemoteError("REQUEST_NOT_FOUND", "Unknown request identifier.", 404)
        with self._lock:
            item = self._commands.get(request_id)
            if item is None:
                raise RemoteError("REQUEST_NOT_FOUND", "Request expired or the server restarted; do not automatically redispatch.", 404)
            return item.snapshot()

    def artifact(self, artifact_id: str) -> tuple[Path, dict]:
        if not _ID.fullmatch(artifact_id):
            raise RemoteError("ARTIFACT_NOT_FOUND", "Unknown artifact.", 404)
        with self._lock:
            path = self._artifact_paths.get(artifact_id)
            if path is None or not path.is_file() or path.is_symlink():
                raise RemoteError("ARTIFACT_NOT_FOUND", "Artifact expired or does not exist.", 404)
            resolved = path.resolve()
            if not resolved.is_relative_to(self._artifacts_root.resolve()) or resolved.stat().st_size > MAX_ARTIFACT_BYTES:
                raise RemoteError("ARTIFACT_UNAVAILABLE", "Artifact is outside its managed storage or exceeds the size limit.", 409)
            return resolved, {"size": resolved.stat().st_size, "mime_type": mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"}

    def _append(self, item: _Command, stream: str, text: str) -> None:
        encoded = text.encode("utf-8")
        with self._lock:
            count_name = stream + "_bytes"
            available = MAX_STREAM_BYTES - getattr(item, count_name)
            if len(encoded) > available:
                item.output_truncated = True
                setattr(item, stream + "_truncated", True)
            kept = encoded[:max(0, available)].decode("utf-8", errors="ignore")
            setattr(item, stream, getattr(item, stream) + kept)
            setattr(item, count_name, MAX_STREAM_BYTES if len(encoded) > available else getattr(item, count_name) + len(kept.encode("utf-8")))

    def _read_stream(self, item: _Command, stream: str, pipe) -> None:
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                self._append(item, stream, decoder.decode(chunk))
            self._append(item, stream, decoder.decode(b"", final=True))
        finally:
            pipe.close()

    def _execute(self, item: _Command, stdin: str | None) -> None:
        try:
            argv = list(item.parsed.argv)
            directory = self._artifacts_root / item.request_id
            if item.parsed.command_path == ("screenshot", "capture") and not item.parsed.observation:
                directory.mkdir(parents=True, exist_ok=False)
                requested = item.parsed.params.get("output_path")
                filename = requested or str(item.parsed.params.get("filename") or "screenshot")
                # Preserve useful screenshot names while keeping the output in
                # the host's managed directory on both Windows and POSIX.
                basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
                suffix = Path(basename).suffix.casefold() if requested else ""
                if suffix not in {".png", ".jpg", ".jpeg"}:
                    suffix = ".png" if item.parsed.params.get("no_compress") else ".jpg"
                stem = Path(basename).stem if Path(basename).suffix.casefold() in {".png", ".jpg", ".jpeg"} else basename
                stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(". ")[:100] or "screenshot"
                if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", stem):
                    stem = "_" + stem
                argv = _replace_option(argv, "--path", str(directory / (stem + suffix)))
                argv = _replace_option(argv, "--filename", stem)
            executable = Path(sys.executable)
            # pythonw has no usable stdio. Commands need a console interpreter
            # with redirected pipes even when the persistent service is hidden.
            if executable.name.casefold() == "pythonw.exe":
                executable = executable.with_name("python.exe")
            output_mode = "json" if item.parsed.command_path[0] == "screenshot" and not item.parsed.observation else item.parsed.output_mode
            command = [str(executable), "-u", "-m", "cli_anything.unreal", "--local", "--output", output_mode, "--project", self.config.project_path, *argv]
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            # Preserve native task discovery, including tasks created outside
            # this service. --local prevents the user's remote default looping.
            process = subprocess.Popen(command, cwd=str(Path(self.config.project_path).parent), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            readers = [threading.Thread(target=self._read_stream, args=(item, name, getattr(process, name)), daemon=True) for name in ("stdout", "stderr")]
            for reader in readers:
                reader.start()
            def write_stdin():
                try:
                    if stdin is not None:
                        process.stdin.write(stdin.encode("utf-8"))
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    process.stdin.close()
            writer = threading.Thread(target=write_stdin, daemon=True)
            writer.start()
            timed_out = False
            try:
                process.wait(timeout=self.config.command_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                timed_out = True
                self._append(item, "stderr", "\nRemote CLI wait timed out. Native UE tasks may continue; inspect task/editor status before retrying.\n")
            for reader in readers:
                reader.join(timeout=10)
            if process.returncode == 0 and item.parsed.command_path[0] == "screenshot" and not item.parsed.observation:
                self._collect_artifacts(item)
            with self._lock:
                item.exit_code = process.returncode
                item.status = "timeout" if timed_out else ("completed" if process.returncode == 0 else "failed")
            self._logger.info("Request %s ended: %s (exit %s)", item.request_id, item.status, item.exit_code)
        except Exception as exc:
            # Exception strings may contain user code or argv. Keep only their
            # class and correlation ID in the persistent service log.
            self._logger.error("Request %s failed: %s", item.request_id, type(exc).__name__)
            self._append(item, "stderr", "Remote command execution failed. Check host service configuration and logs.\n")
            with self._lock:
                item.status, item.exit_code = "failed", 1
        finally:
            with self._lock:
                item.finished_at = time.time()
                if self._mutating_request == item.request_id:
                    self._mutating_request = None

    def _collect_artifacts(self, item: _Command) -> None:
        try:
            result = json.loads(item.stdout)
        except (ValueError, TypeError):
            item.artifact_error = "Screenshot output is not complete JSON."
            return
        directory = self._artifacts_root / item.request_id
        directory.mkdir(parents=True, exist_ok=True)
        screenshot_root = Path(self.config.project_path).parent / "Saved" / "Screenshots"
        roots = (directory.resolve(), screenshot_root.resolve())
        seen = set()
        total = 0

        def visit(value):
            nonlocal total
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and Path(value).suffix.casefold() in {".png", ".jpg", ".jpeg"}:
                original = Path(value)
                path = original.resolve()
                if not any(path.is_relative_to(root) for root in roots) or not path.is_file() or path.is_symlink() or original.is_symlink():
                    return
                if str(path) in seen:
                    return
                seen.add(str(path))
                size = path.stat().st_size
                if size > MAX_ARTIFACT_BYTES or total + size > MAX_ARTIFACT_BYTES * 4 or len(item.artifacts) >= MAX_ARTIFACTS:
                    item.artifact_error = "Some screenshot artifacts exceed transfer limits."
                    return
                artifact_id = uuid.uuid4().hex
                destination = directory / (artifact_id + path.suffix.casefold())
                # Copy an immutable request-specific snapshot. The endpoint
                # never accepts a caller-provided filesystem path.
                with path.open("rb") as source:
                    data = source.read(MAX_ARTIFACT_BYTES + 1)
                if len(data) > MAX_ARTIFACT_BYTES:
                    item.artifact_error = "Screenshot changed beyond the transfer limit."
                    return
                destination.write_bytes(data)
                metadata = {"id": artifact_id, "name": path.name, "host_path": value, "download_path": f"/v1/artifacts/{artifact_id}", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}
                with self._lock:
                    self._artifact_paths[artifact_id] = destination
                    item.artifacts.append(metadata)
                total += len(data)
        visit(result)
        if not item.artifacts and not item.artifact_error:
            item.artifact_error = "Screenshot command produced no downloadable image in its managed output locations."


class RemoteHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR permits another process to bind an existing
    # listener, so requests can reach either service. Unix reuse only handles
    # old connections and remains useful during supervised restarts.
    allow_reuse_address = os.name != "nt"

    def __init__(self, service: RemoteCommandService):
        self.service = service
        super().__init__((service.config.bind, service.config.port), _Handler)

    def server_bind(self):
        if os.name == "nt":
            # Set before bind; even a second process enabling SO_REUSEADDR
            # cannot take over this address while the original service lives.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        with self.service._lock:
            self.service._closed = True
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "ue-cli-remote/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, _format, *args):
        # Avoid recording authorization, script text or project paths in access
        # logs. The caller can supervise process exit and authenticated health.
        pass

    def _json(self, status: int, result: dict):
        data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: RemoteError):
        self._json(exc.status, {"protocol_version": PROTOCOL_VERSION, "error": {"code": exc.code, "message": str(exc)}})

    def _authorize(self):
        if not self.server.service.authorized(self.client_address[0], self.headers.get("Authorization", "")):
            raise RemoteError("UNAUTHORIZED", "Authentication or client address rejected.", 403)
        origin = self.headers.get("Origin")
        if origin is not None:
            raise RemoteError("BROWSER_ORIGIN_FORBIDDEN", "Browser-origin requests are not supported.", 403)
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
            raise RemoteError("INVALID_PATH", "Use an exact API path without a query.")
        return parsed.path

    def do_GET(self):
        try:
            path = self._authorize()
            if path == "/v1/health":
                self._json(200, self.server.service.health())
            elif path.startswith("/v1/commands/"):
                self._json(200, self.server.service.get(path.removeprefix("/v1/commands/")))
            elif path.startswith("/v1/artifacts/"):
                file_path, metadata = self.server.service.artifact(path.removeprefix("/v1/artifacts/"))
                self.send_response(200)
                self.send_header("Content-Type", metadata["mime_type"])
                self.send_header("Content-Length", str(metadata["size"]))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with file_path.open("rb") as handle:
                    remaining = metadata["size"]
                    while remaining:
                        chunk = handle.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                raise RemoteError("NOT_FOUND", "Unknown API endpoint.", 404)
        except RemoteError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self.server.service._logger.error("GET failed: %s", type(exc).__name__)
            self._error(RemoteError("REMOTE_INTERNAL_ERROR", "The host could not complete this request.", 500))

    def do_POST(self):
        try:
            path = self._authorize()
            if path != "/v1/commands":
                raise RemoteError("NOT_FOUND", "Unknown API endpoint.", 404)
            if self.headers.get("Transfer-Encoding"):
                raise RemoteError("INVALID_REQUEST", "Chunked request bodies are not supported.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise RemoteError("INVALID_REQUEST", "A bounded Content-Length is required.") from exc
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise RemoteError("REQUEST_TOO_LARGE", "Request body must be no larger than 2 MiB.", 413)
            body = self.rfile.read(length)
            if len(body) != length:
                raise RemoteError("INVALID_REQUEST", "Incomplete request body.")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise RemoteError("INVALID_REQUEST", "Expected a UTF-8 JSON body.") from exc
            result = self.server.service.submit(payload)
            self._json(202, result)
        except RemoteError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self.server.service._logger.error("POST failed: %s", type(exc).__name__)
            self._error(RemoteError("REMOTE_INTERNAL_ERROR", "The host could not complete this request; do not automatically redispatch.", 500))


def create_server(config: RemoteServerConfig) -> RemoteHTTPServer:
    return RemoteHTTPServer(RemoteCommandService(config))


def serve(config: RemoteServerConfig) -> None:
    server = create_server(config)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
