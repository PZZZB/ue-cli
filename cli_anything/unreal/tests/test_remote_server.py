"""Remote transport tests use synthetic projects and mocked UE processes."""

from __future__ import annotations

import io
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import click
import pytest

from cli_anything.unreal.core import remote_policy, remote_server
from cli_anything.unreal.core.remote_policy import RemoteError, parse_remote_command
from cli_anything.unreal.core.remote_server import RemoteCommandService, RemoteServerConfig, create_server


@pytest.fixture
def config(tmp_path, monkeypatch):
    engine = tmp_path / "EngineRoot"
    (engine / "Engine" / "Build").mkdir(parents=True)
    project = tmp_path / "Project" / "Test.uproject"
    project.parent.mkdir()
    project.write_text(json.dumps({"EngineAssociation": str(engine)}), encoding="utf-8")
    token = tmp_path / "token.txt"
    token.write_text("test-token-" + "a" * 48, encoding="utf-8")
    monkeypatch.setattr(remote_server, "active_project_tasks", lambda _: [])
    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "native-tasks"))
    return RemoteServerConfig(str(project), str(engine), str(token), port=0, state_directory=str(tmp_path / "state"))


class FakeProcess:
    def __init__(self, stdout=b'{"ok":true}\n', stderr=b"", returncode=0, gate=None):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.stdin = io.BytesIO()
        self.returncode = returncode
        self.gate = gate

    def wait(self, timeout=None):
        if self.gate and not self.gate.wait(timeout=timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self):
        self.returncode = -9
        if self.gate:
            self.gate.set()


def finished(service, request_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = service.get(request_id)
        if value["finished_at"] is not None:
            return value
        time.sleep(0.005)
    raise AssertionError("Mock request did not complete")


def submit(service, argv, **kwargs):
    return service.submit({"protocol_version": 1, "argv": argv, **kwargs})


@pytest.mark.parametrize("argv", [
    ["remote", "serve"], ["repl"], ["install-skills"], ["_task-worker", "run", "fake"],
    ["--project", "other.uproject", "editor", "status"],
    ["--port", "30011", "editor", "status"],
    ["editor", "status", "--all"], ["status", "--all"],
    ["editor", "status", "--scan-range", "1-65535"],
    ["build", "compile", "--project=other.uproject"],
    ["project", "info", "--project", "other.uproject"],
    ["confirmation", "list", "--pid", "123"],
    ["does-not-exist"], ["build", "compile", "--bogus"],
    ["editor", "status", "a", "b"],
    ["--output", "bogus", "editor", "status"], [],
])
def test_rejects_context_override_and_unknown_commands(argv):
    with pytest.raises(RemoteError):
        parse_remote_command(argv)


def test_live_click_parser_supports_future_commands_without_mapping(monkeypatch):
    from cli_anything.unreal.unreal_cli import cli
    invoked = []

    @click.command("future-feature")
    @click.option("--count", type=click.IntRange(1, 3), required=True)
    def future_feature(count):
        invoked.append(count)

    monkeypatch.setitem(cli.commands, "future-feature", future_feature)
    parsed = parse_remote_command(["future-feature", "--count", "2"])
    assert parsed.params["count"] == 2
    assert invoked == []
    with pytest.raises(RemoteError):
        parse_remote_command(["future-feature", "--count", "4"])
    with pytest.raises(RemoteError):
        parse_remote_command(["future-feature"])


def test_click_unset_is_not_a_path_or_string():
    parsed = parse_remote_command(["--output", "text", "editor", "status"])
    assert parsed.output_mode == "text"
    assert parsed.params["project_path"] is None
    assert parsed.params["task_id"] is None
    assert parse_remote_command(["project", "config", "get", "Engine"]).params["section"] is None


def test_remote_parser_preserves_local_short_help_alias(monkeypatch):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    editor = cli.commands["editor"]
    script = editor.commands["run-script"]
    for command in (cli, editor, script):
        monkeypatch.setattr(command, "_help_option", None)
    parse_remote_command(["editor", "run-script", "--no-save", "-"])
    result = CliRunner().invoke(cli, ["editor", "run-script", "-h"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "-h, --help" in result.output


def test_auth_requires_exact_peer_and_token(config):
    service = RemoteCommandService(config)
    token = Path(config.token_file).read_text()
    assert service.authorized("127.0.0.1", "Bearer " + token)
    assert not service.authorized("192.168.168.10", "Bearer " + token)
    assert not service.authorized("127.0.0.1", "Bearer incorrect")
    assert not service.authorized("127.0.0.1", token)


def test_listener_rejects_second_server_on_same_address(config):
    first = create_server(config)
    second = None
    try:
        port = first.server_address[1]
        if os.name == "nt":
            assert not first.allow_reuse_address
            assert first.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
        with pytest.raises(OSError):
            second = create_server(replace(config, port=port))
        # Reproduce the old Windows service's socket option too: it must not
        # be able to claim a port owned by the exclusive listener.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competing:
            competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competing.bind((config.bind, port))
    finally:
        if second is not None:
            second.server_close()
        first.server_close()


def test_cli_argv_stdout_stderr_exit_preserved(config, monkeypatch):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess(stdout=b'{"error":"test"}\n', stderr=b"compiler log\n", returncode=7)

    monkeypatch.setattr(remote_server.subprocess, "Popen", popen)
    service = RemoteCommandService(config)
    response = submit(service, ["--output", "text", "project", "info"])
    final = finished(service, response["request_id"])
    assert final["stdout"] == '{"error":"test"}\n'
    assert final["stderr"] == "compiler log\n"
    assert final["exit_code"] == 7 and final["status"] == "failed"
    argv, kwargs = calls[0]
    assert argv[-6:] == ["--output", "text", "--project", config.project_path, "project", "info"]
    assert "--local" in argv
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(Path(config.project_path).parent)
    assert "test-token" not in repr(calls)


def test_pythonw_supervisor_runs_console_child(config, monkeypatch):
    monkeypatch.setattr(remote_server.sys, "executable", "C:/Python/pythonw.exe")
    calls = []
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda argv, **kw: calls.append(argv) or FakeProcess())
    service = RemoteCommandService(config)
    finished(service, submit(service, ["project", "info"])["request_id"])
    assert Path(calls[0][0]).name == "python.exe"


def test_only_one_mutating_request_but_status_can_run(config, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda *a, **kw: FakeProcess(gate=gate))
    service = RemoteCommandService(config)
    first = submit(service, ["build", "compile"])
    with pytest.raises(RemoteError, match="Another command") as exc:
        submit(service, ["editor", "launch"])
    assert exc.value.status == 409
    status = submit(service, ["editor", "status"])
    gate.set()
    assert finished(service, first["request_id"])["status"] == "completed"
    assert finished(service, status["request_id"])["status"] == "completed"


def test_native_no_wait_task_blocks_new_effects(config, monkeypatch):
    monkeypatch.setattr(remote_server, "active_project_tasks", lambda _: [{"task_id": "native"}])
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda *a, **kw: FakeProcess())
    service = RemoteCommandService(config)
    with pytest.raises(RemoteError) as exc:
        submit(service, ["editor", "launch"])
    assert exc.value.code == "PROJECT_TASK_ACTIVE"
    assert finished(service, submit(service, ["editor", "status"])["request_id"])["exit_code"] == 0


def test_native_task_ids_scoped_to_project(config, monkeypatch):
    from cli_anything.unreal.core import tasks
    monkeypatch.setattr(tasks, "load_task", lambda *a, **kw: {"payload": {"project_path": str(Path(config.project_path).parent / "Other.uproject")}})
    service = RemoteCommandService(config)
    with pytest.raises(RemoteError) as exc:
        submit(service, ["task", "cancel", "other"])
    assert exc.value.code == "TASK_PROJECT_MISMATCH"
    with pytest.raises(RemoteError) as exc:
        submit(service, ["task", "status", "../secret"])
    assert exc.value.code == "INVALID_TASK_ID"


def test_task_admission_ignores_other_projects_without_writing_state(config, monkeypatch):
    from cli_anything.unreal.core import tasks
    entries = [
        {"task_id": "ours", "command": "build.compile", "worker_pid": 123, "status": "running", "payload": {"project_path": config.project_path}},
        {"task_id": "foreign", "command": "build.compile", "worker_pid": 124, "status": "running", "payload": {"project_path": "Other.uproject"}},
    ]
    monkeypatch.setattr(remote_policy, "_task_snapshots", lambda deadline: entries)
    probed = []
    monkeypatch.setattr(tasks, "_probe_task_process", lambda task, role: probed.append((task["task_id"], role)) or {"state": "exited"})
    monkeypatch.setattr(tasks, "reconcile_task_state", lambda *a: pytest.fail("Admission must not modify historical tasks"))
    assert remote_policy.active_project_tasks(config.project_path) == []
    assert probed == [("ours", "worker"), ("ours", "build")]


@pytest.mark.parametrize("processes,blocked", [
    ([], False),
    ([{"pid": 42, "started_at": 2000.0}], False),
    ([{"pid": 42, "started_at": 500.0}], True),
    ([{"pid": 42, "started_at": None}], True),
    (None, True),
])
def test_observation_requires_its_original_editor_to_be_gone(config, monkeypatch, processes, blocked):
    from cli_anything.unreal.core import tasks
    observer = {
        "task_id": "t-historical", "command": "editor.run-script", "status": "running",
        "phase": "awaiting_delivery_evidence", "worker_pid": None, "pid": None,
        "created_at": 1000.0, "updated_at": 9999.0,
        "payload": {"project_path": config.project_path, "log_file": "Project.log", "log_start": 200,
                    "begin_marker": "begin", "end_marker": "end", "source_kind": "stdin"},
        "result": {"delivery_state": "unknown", "completion_state": "unknown"},
    }
    before = json.dumps(observer)
    monkeypatch.setattr(remote_policy, "_task_snapshots", lambda deadline: [observer])
    monkeypatch.setattr(remote_policy, "_project_editor_processes", lambda *a: processes)
    monkeypatch.setattr(tasks, "reconcile_task_state", lambda *a: pytest.fail("Do not reconcile observations during admission"))
    assert bool(remote_policy.active_project_tasks(config.project_path)) is blocked
    assert json.dumps(observer) == before


@pytest.mark.parametrize("worker_state,build_state,blocked", [
    ("running", "not_started", True), ("unknown", "exited", True),
    ("exited", "running", True), ("exited", "unknown", True),
    ("exited", "exited", False), ("pid_reused", "not_started", False),
])
def test_build_admission_keeps_live_or_uncertain_processes(config, monkeypatch, worker_state, build_state, blocked):
    from cli_anything.unreal.core import tasks
    task = {"task_id": "t-build", "command": "build.compile", "worker_pid": 123, "pid": 456,
            "status": "running", "payload": {"project_path": config.project_path}}
    monkeypatch.setattr(remote_policy, "_task_snapshots", lambda deadline: [task])
    monkeypatch.setattr(tasks, "_probe_task_process", lambda task, role: {"state": worker_state if role == "worker" else build_state})
    assert bool(remote_policy.active_project_tasks(config.project_path)) is blocked


def test_snapshot_admission_is_strict_and_bounded(config):
    from cli_anything.unreal.core.tasks import task_data_path
    record = task_data_path("one.json")
    record.write_text("{broken", encoding="utf-8")
    with pytest.raises(RemoteError, match="complete published task"):
        remote_policy._task_snapshots(time.monotonic() + 5)
    record.write_text("{}", encoding="utf-8")
    with pytest.raises(RemoteError, match="Timed out"):
        remote_policy._task_snapshots(time.monotonic() - 1)


def test_strict_process_inventory_distinguishes_other_projects(config, monkeypatch):
    monkeypatch.setattr(remote_policy.sys, "platform", "win32")
    inventory = {
        "ok": True, "processes": [
            {"ProcessId": 1, "CommandLine": 'UnrealEditor.exe "D:/Other1/P.uproject"', "StartedAt": 500},
            {"ProcessId": 2, "CommandLine": 'UnrealEditor.exe "D:/Other2/P.uproject"', "StartedAt": 600},
        ],
    }
    monkeypatch.setattr(remote_policy.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(inventory), ""))
    monkeypatch.setattr("cli_anything.unreal.utils.ue_backend._extract_uproject_from_cmdline", lambda value: value.split('"')[1])
    assert remote_policy._project_editor_processes(config.project_path, time.monotonic() + 5) == []
    inventory["processes"].append({"ProcessId": 3, "CommandLine": f'UnrealEditor.exe "{config.project_path}"', "StartedAt": 700})
    assert remote_policy._project_editor_processes(config.project_path, time.monotonic() + 5) == [{"pid": 3, "started_at": 700.0}]
    inventory["processes"][0]["CommandLine"] = None
    assert remote_policy._project_editor_processes(config.project_path, time.monotonic() + 5) is None


def test_strict_process_query_failure_is_unknown(config, monkeypatch):
    monkeypatch.setattr(remote_policy.sys, "platform", "win32")
    monkeypatch.setattr(remote_policy.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "query failed"))
    assert remote_policy._project_editor_processes(config.project_path, time.monotonic() + 5) is None


def test_engine_change_rejected_before_execution(config, monkeypatch):
    service = RemoteCommandService(config)
    monkeypatch.setattr("cli_anything.unreal.utils.ue_backend.find_engine_root", lambda _: "C:/wrong")
    with pytest.raises(RemoteError) as exc:
        submit(service, ["project", "info"])
    assert exc.value.code == "REMOTE_ENGINE_MISMATCH"


def test_stdin_only_script_and_exact_bytes(config, monkeypatch):
    received = []

    class Input(io.BytesIO):
        def close(self):
            received.append(self.getvalue())
            super().close()

    def popen(*a, **kw):
        process = FakeProcess()
        process.stdin = Input()
        return process

    monkeypatch.setattr(remote_server.subprocess, "Popen", popen)
    service = RemoteCommandService(config)
    code = "result = {'text': '中文'}\n"
    result = submit(service, ["editor", "run-script", "--no-save", "-"], stdin=code)
    assert finished(service, result["request_id"])["exit_code"] == 0
    assert received == [code.encode("utf-8")]
    with pytest.raises(RemoteError):
        submit(service, ["project", "info"], stdin="unrelated")


def test_bounded_stream_preserves_valid_unicode(config, monkeypatch):
    monkeypatch.setattr(remote_server, "MAX_STREAM_BYTES", 5)
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda *a, **kw: FakeProcess(stdout="中文测试".encode()))
    service = RemoteCommandService(config)
    final = finished(service, submit(service, ["project", "info"])["request_id"])
    assert final["output_truncated"]
    assert final["stdout_truncated"] and not final["stderr_truncated"]
    assert final["stdout"] == "中"


def test_large_build_log_does_not_mark_stdout_truncated(config, monkeypatch):
    monkeypatch.setattr(remote_server, "MAX_STREAM_BYTES", 20)
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda *a, **kw: FakeProcess(stderr=b"a" * 40))
    service = RemoteCommandService(config)
    final = finished(service, submit(service, ["build", "compile"])["request_id"])
    assert final["stderr_truncated"] and not final["stdout_truncated"]
    assert final["exit_code"] == 0 and final["stdout"] == '{"ok":true}\n'


def test_default_screenshot_keeps_jpeg_semantics(config, monkeypatch):
    paths = []

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index("--path") + 1]))
        return FakeProcess()

    monkeypatch.setattr(remote_server.subprocess, "Popen", popen)
    service = RemoteCommandService(config)
    finished(service, submit(service, ["screenshot", "capture"])["request_id"])
    assert paths[0].suffix == ".jpg"


def test_screenshot_filename_preserved_without_directory_access(config, monkeypatch):
    paths = []

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index("--path") + 1]))
        return FakeProcess()

    monkeypatch.setattr(remote_server.subprocess, "Popen", popen)
    service = RemoteCommandService(config)
    finished(service, submit(service, ["screenshot", "capture", "--filename", "MyShot"])["request_id"])
    assert paths[0].name == "MyShot.jpg"
    assert paths[0].is_relative_to(Path(config.state_directory))


def test_screenshot_is_managed_and_ready_before_terminal(config, monkeypatch):
    captured = []

    def popen(argv, **kwargs):
        target = Path(argv[argv.index("--path") + 1])
        target.write_bytes(b"PNG-test")
        captured.append(argv)
        return FakeProcess(stdout=json.dumps({"read_this": str(target), "path_raw": str(target)}).encode())

    monkeypatch.setattr(remote_server.subprocess, "Popen", popen)
    service = RemoteCommandService(config)
    request = submit(service, ["--output", "text", "screenshot", "capture", "--path", "C:/unexpected/path.png", "--filename", "../../bad"])
    final = finished(service, request["request_id"])
    assert final["status"] == "completed"
    assert final["requested_output"] == "text" and final["effective_output"] == "json"
    assert len(final["artifacts"]) == 1
    metadata = final["artifacts"][0]
    path, _ = service.artifact(metadata["id"])
    assert path.read_bytes() == b"PNG-test"
    assert path.is_relative_to(Path(config.state_directory))
    assert "C:/unexpected/path.png" not in captured[0]
    assert "../../bad" not in captured[0]
    with pytest.raises(RemoteError):
        service.artifact("../../token.txt")


def test_screenshot_result_cannot_download_arbitrary_host_file(config, monkeypatch):
    secret = Path(config.token_file)
    renamed = secret.with_suffix(".png")
    renamed.write_text("not-an-artifact")
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda *a, **kw: FakeProcess(stdout=json.dumps({"read_this": str(renamed)}).encode()))
    service = RemoteCommandService(config)
    final = finished(service, submit(service, ["screenshot", "capture"])["request_id"])
    assert final["artifacts"] == []


def test_http_auth_protocol_and_request_validation(config, monkeypatch):
    monkeypatch.setattr(remote_server.subprocess, "Popen", lambda *a, **kw: FakeProcess())
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:" + str(server.server_address[1])
    opener = build_opener(ProxyHandler({}))
    token = Path(config.token_file).read_text()

    def request(path, body=None, auth=True, origin=None):
        headers = {"Authorization": "Bearer " + token} if auth else {}
        if origin:
            headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        return opener.open(Request(url + path, data=data, headers=headers), timeout=3)

    try:
        with pytest.raises(HTTPError) as exc:
            request("/v1/health", auth=False)
        assert exc.value.code == 403
        assert json.load(request("/v1/health"))["project_path"] == config.project_path
        with pytest.raises(HTTPError) as exc:
            request("/v1/health", origin="https://example.com")
        assert exc.value.code == 403
        with pytest.raises(HTTPError) as exc:
            request("/v1/commands", {"protocol_version": 2, "argv": ["project", "info"]})
        assert exc.value.code == 409
        created = json.load(request("/v1/commands", {"protocol_version": 1, "argv": ["project", "info"]}))
        final = finished(server.service, created["request_id"])
        assert json.load(request("/v1/commands/" + created["request_id"]))["stdout"] == final["stdout"]
        with pytest.raises(HTTPError) as exc:
            request("/v1/artifacts/../token.txt")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_synthetic_project_runs_real_cli_subprocess(config):
    # Reads only generated .uproject / engine metadata; no UE process starts.
    service = RemoteCommandService(config)
    final = finished(service, submit(service, ["project", "info"])["request_id"])
    assert final["exit_code"] == 0, final
    result = json.loads(final["stdout"])
    assert isinstance(result, dict) and not result.get("error")
    assert "Test" in final["stdout"]
