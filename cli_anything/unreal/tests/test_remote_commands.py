"""Remote-management CLI behavior, without an editor or network."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli_anything.unreal.unreal_cli import cli


@pytest.fixture
def remote_home(tmp_path, monkeypatch):
    monkeypatch.setenv("UE_CLI_REMOTE_CONFIG", str(tmp_path / "remotes.json"))
    token = tmp_path / "token.txt"
    token.write_text("x" * 48, encoding="utf-8")
    return tmp_path, token


def test_manage_connection_and_default_without_disclosing_token(remote_home):
    directory, token = remote_home
    runner = CliRunner()
    added = runner.invoke(cli, ["remote", "add", "host-game", "--url", "http://127.0.0.1:17891",
                                "--token-file", str(token), "--default"])
    assert added.exit_code == 0, added.output
    assert "x" * 48 not in added.output
    stored = json.loads((directory / "remotes.json").read_text(encoding="utf-8"))
    assert stored["default"] == "host-game"
    assert "x" * 48 not in json.dumps(stored)
    listed = runner.invoke(cli, ["remote", "list"])
    assert listed.exit_code == 0, listed.output
    assert "host-game" in listed.output
    assert "x" * 48 not in listed.output
    cleared = runner.invoke(cli, ["remote", "use", "--clear"])
    assert cleared.exit_code == 0, cleared.output
    assert json.loads((directory / "remotes.json").read_text(encoding="utf-8"))["default"] is None


def test_use_requires_name_or_clear(remote_home):
    result = CliRunner().invoke(cli, ["remote", "use"])
    assert result.exit_code == 2
    assert json.loads(result.output)["code"] == "INVALID_ARGUMENT"


def test_health_uses_selected_connection(remote_home, monkeypatch):
    from cli_anything.unreal.core.remote_config import add_profile, select_profile
    from cli_anything.unreal.core.remote_client import RemoteClient

    _, token = remote_home
    add_profile("host-game", "http://127.0.0.1:17891", str(token))
    select_profile("host-game")
    monkeypatch.setattr(RemoteClient, "health", lambda self: {"protocol_version": 1, "project": "Game"})
    result = CliRunner().invoke(cli, ["remote", "health"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["project"] == "Game"


def test_serve_preserves_root_project_and_subcommand_port(remote_home, monkeypatch):
    from cli_anything.unreal.core import remote_server
    from cli_anything.unreal.utils import ue_backend

    directory, token = remote_home
    project = directory / "Game.uproject"
    project.write_text('{"EngineAssociation":"test"}', encoding="utf-8")
    engine = directory / "EngineRoot"
    engine.mkdir()
    monkeypatch.setattr(ue_backend, "find_engine_root", lambda path: str(engine))
    monkeypatch.setattr(remote_server, "RemoteServerConfig", lambda **kw: SimpleNamespace(**kw))
    captured = []
    monkeypatch.setattr(remote_server, "serve", captured.append)
    result = CliRunner().invoke(cli, ["--project", str(project), "remote", "serve", "--port", "18891",
                                     "--token-file", str(token), "--allow-client", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].project_path == str(project.resolve())
    assert captured[0].port == 18891
    assert captured[0].expected_engine == str(engine.resolve())


def test_result_follows_receipt_without_resubmission(remote_home, monkeypatch):
    from cli_anything.unreal.core.remote_config import add_profile, select_profile
    from cli_anything.unreal.core.remote_client import RemoteClient

    _, token = remote_home
    add_profile("host-game", "http://127.0.0.1:17891", str(token))
    select_profile("host-game")
    seen = []

    def follow(self, request_id):
        seen.append(request_id)
        return {"stdout": '{"status":"success","result":[]}\n', "exit_code": 0, "artifacts": []}

    monkeypatch.setattr(RemoteClient, "follow", follow)
    monkeypatch.setattr(RemoteClient, "execute", lambda *a, **k: pytest.fail("Must not resubmit"))
    result = CliRunner().invoke(cli, ["remote", "result", "a" * 32])
    assert result.exit_code == 0, result.output
    assert seen == ["a" * 32]
    assert json.loads(result.output) == {"status": "success", "result": []}
