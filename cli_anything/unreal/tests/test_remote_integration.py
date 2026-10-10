"""Real HTTP and child CLI round trips against synthetic, editor-free projects."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from cli_anything.unreal.core.remote_client import RemoteClient, prepare_command
from cli_anything.unreal.core.remote_config import RemoteError, add_profile, get_profile, select_profile
from cli_anything.unreal.core.remote_server import RemoteServerConfig, create_server
from cli_anything.unreal.core.tasks import create_task, save_task


@pytest.fixture
def transport(tmp_path, monkeypatch):
    """Bind an ephemeral loopback listener; never discover or launch Unreal."""
    repo = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", str(repo))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("UE_CLI_REMOTE_CONFIG", str(tmp_path / "remotes.json"))
    engine = tmp_path / "SyntheticEngine"
    (engine / "Engine" / "Build").mkdir(parents=True)
    project = tmp_path / "Game" / "Game.uproject"
    project.parent.mkdir()
    project.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": str(engine)}), encoding="utf-8")
    (project.parent / "Config").mkdir()
    (project.parent / "Config" / "DefaultEngine.ini").write_text("[TransportTest]\nValue=42\n", encoding="utf-8")
    token = tmp_path / "token.txt"
    token.write_text("integration-test-secret-" + "x" * 48, encoding="utf-8")
    server = create_server(RemoteServerConfig(
        project_path=str(project), expected_engine=str(engine), token_file=str(token),
        bind="127.0.0.1", port=0, allowed_clients=("127.0.0.1",),
        state_directory=str(tmp_path / "server"), command_timeout=20,
    ))
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    add_profile("loopback-test", f"http://127.0.0.1:{server.server_port}", str(token), local_project=str(project.parent))
    profile = get_profile("loopback-test")
    try:
        yield RemoteClient(profile, poll_interval=0.01), project, profile
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        # The service is deliberately in-process only for this test.
        for handler in list(server.service._logger.handlers):
            handler.close()
            server.service._logger.removeHandler(handler)


def test_real_http_executes_original_click_command(transport):
    client, project, profile = transport
    health = client.health()
    assert Path(health["project_path"]) == project
    argv, stdin, output = prepare_command(
        ["--project", str(project), "project", "config", "get", "Engine"], profile, health,
    )
    assert stdin is None and output is None
    result = client.execute(argv)
    assert result["status"] == "completed", result
    assert result["exit_code"] == 0, result["stderr"]
    assert json.loads(result["stdout"])["result"]["TransportTest"]["Value"] == "42"


def test_real_cli_default_remote_does_not_recurse(transport, tmp_path):
    _, _, _ = transport
    select_profile("loopback-test")
    # Run outside the synthetic project. Only the server knows the project.
    result = subprocess.run(
        [sys.executable, "-m", "cli_anything.unreal", "project", "config", "list"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=20,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["result"][0]["filename"] == "DefaultEngine.ini"


def test_real_http_preserves_text_help_and_parser_failure(transport):
    client, _, _ = transport
    result = client.execute(["--output", "text", "project", "config", "get", "--help"])
    assert result["exit_code"] == 0, result
    assert "CONFIG_NAME" in result["stdout"]
    with pytest.raises(RemoteError) as error:
        client.execute(["project", "config", "get", "Engine", "--not-an-option"])
    assert error.value.code == "INVALID_ARGV"
    assert "not-an-option" in error.value.message


def test_real_http_native_task_status_stays_in_project(transport, tmp_path):
    client, project, _ = transport
    task = create_task("build.compile", {"project_path": str(project)})
    task.update(status="completed", result={"transport_test": True})
    save_task(task)
    result = client.execute(["task", "status", task["task_id"]])
    assert result["exit_code"] == 0, result
    progress = json.loads(result["stdout"])
    assert progress["task_id"] == task["task_id"]
    assert progress["status"] == "completed"
    assert progress["result"]["transport_test"] is True

    other = create_task("build.compile", {"project_path": str(tmp_path / "Other.uproject")})
    other.update(status="completed")
    save_task(other)
    with pytest.raises(RemoteError) as error:
        client.execute(["task", "status", other["task_id"]])
    assert error.value.code == "TASK_PROJECT_MISMATCH"


def test_real_http_rejects_project_override_and_hidden_worker(transport):
    client, project, _ = transport
    for argv, code in [
        (["--project", str(project), "project", "config", "list"], "REMOTE_CONTEXT_FIXED"),
        (["project", "info", "--project", str(project)], "REMOTE_CONTEXT_FIXED"),
        (["_task-worker", "run", "any-task"], "REMOTE_COMMAND_FORBIDDEN"),
    ]:
        with pytest.raises(RemoteError) as error:
            client.execute(argv)
        assert error.value.code == code


def test_real_http_rejects_invalid_bearer(transport):
    client, _, _ = transport
    client.token = "wrong-token"
    with pytest.raises(RemoteError) as error:
        client.health()
    assert error.value.details["http_status"] == 403
