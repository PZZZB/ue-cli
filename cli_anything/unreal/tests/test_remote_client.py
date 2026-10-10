"""Remote routing, fail-closed transport and artifact transfer without Unreal."""

import hashlib
import json
import sys
from io import BytesIO, StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from click.testing import CliRunner

from cli_anything.unreal.core import remote_client as client_module
from cli_anything.unreal.core.remote_client import (
    RemoteClient, download_artifacts, prepare_command, routing_profile, split_routing_args,
)
from cli_anything.unreal.core.remote_config import (
    RemoteError, add_profile, get_profile, list_profiles, load_config, select_profile, validate_url,
)
from cli_anything.unreal.unreal_cli import cli


@pytest.fixture
def remote_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("UE_CLI_REMOTE_CONFIG", str(tmp_path / "remotes.json"))
    token = tmp_path / "token.txt"
    token.write_text("test-remote-secret\n", encoding="utf-8")
    add_profile("host-test", "http://127.0.0.1:17891", str(token), local_project="C:\\Projects\\RXGame_3")
    return get_profile("host-test")


def test_config_roundtrip_default_and_private_token_reference(remote_profile):
    assert get_profile() is None
    select_profile("host-test")
    assert get_profile()["name"] == "host-test"
    assert list_profiles()[0]["default"] is True
    assert "test-remote-secret" not in json.dumps(load_config())
    select_profile(None)
    assert get_profile() is None
    with pytest.raises(RemoteError, match="not found"):
        select_profile("absent")


@pytest.mark.parametrize("url", ["http://user:secret@127.0.0.1", "file:///tmp/x", "http://localhost/path", "http://localhost?token=x", "http://localhost#x", "http://localhost:bad"])
def test_config_rejects_credential_or_ambiguous_urls(url):
    with pytest.raises(RemoteError):
        validate_url(url)


def test_remote_dispatch_precedes_local_project_discovery(remote_profile, monkeypatch):
    calls = []
    monkeypatch.setattr(client_module, "run_remote", lambda profile, argv: calls.append((profile, argv)) or 0)
    result = CliRunner().invoke(cli, ["--remote", "host-test", "--project", "Q:/absent.uproject", "editor", "status"])
    assert result.exit_code == 0, result.output
    assert calls[0][1] == ["--project", "Q:/absent.uproject", "editor", "status"]


def test_default_remote_dispatch_and_explicit_local(remote_profile, monkeypatch):
    select_profile("host-test")
    calls = []
    monkeypatch.setattr(client_module, "run_remote", lambda profile, argv: calls.append(argv) or 0)
    assert CliRunner().invoke(cli, ["editor", "status"]).exit_code == 0
    assert calls == [["editor", "status"]]
    result = CliRunner().invoke(cli, ["--local", "--project", "Q:/missing.uproject", "editor", "status"])
    assert result.exit_code == 3
    assert "PROJECT_NOT_FOUND" in result.output
    assert len(calls) == 1


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["--list-commands"], ["remote", "list"], ["install-skills", "--help"], ["_task-worker", "--help"]])
def test_management_help_and_hidden_worker_always_local(remote_profile, monkeypatch, argv):
    select_profile("host-test")
    monkeypatch.setattr(client_module, "run_remote", lambda *a: pytest.fail("local command dispatched remotely"))
    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output


def test_remote_serve_preserves_root_project_and_subcommand_port(remote_profile):
    select_profile("host-test")
    args = ["--project", "D:/RXGame_3/RXGame.uproject", "remote", "serve", "--port", "17891"]
    forwarded, profile = routing_profile(args)
    assert profile is None
    assert forwarded == args


def test_cli_preserves_failure_and_never_falls_back(remote_profile, monkeypatch):
    def fail(*args):
        raise RemoteError("REMOTE_UNAVAILABLE", "Endpoint offline.")
    monkeypatch.setattr(client_module, "run_remote", fail)
    result = CliRunner().invoke(cli, ["--remote", "host-test", "editor", "status"])
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "REMOTE_UNAVAILABLE"


def test_unknown_profile_fails_closed(remote_profile):
    result = CliRunner().invoke(cli, ["--remote", "missing", "editor", "status"])
    assert result.exit_code != 0
    assert json.loads(result.output)["code"] == "REMOTE_NOT_FOUND"


def test_conflicting_flags_fail_and_script_values_are_untouched():
    with pytest.raises(RemoteError):
        split_routing_args(["--local", "--remote", "host", "editor", "status"])
    original = ["--remote=x", "editor", "run-script", "-c", "print('--local --remote=x')"]
    assert split_routing_args(original) == (original[1:], "x", False, "editor")


def test_command_specific_help_is_remote(remote_profile, monkeypatch):
    calls = []
    monkeypatch.setattr(client_module, "run_remote", lambda profile, argv: calls.append(argv) or 0)
    result = CliRunner().invoke(cli, ["--remote", "host-test", "future-command", "--help"])
    assert result.exit_code == 0
    assert calls == [["future-command", "--help"]]


def test_explicit_remote_repl_never_enters_local_session(remote_profile):
    result = CliRunner().invoke(cli, ["--remote", "host-test", "repl"])
    assert result.exit_code == 2
    assert json.loads(result.output)["code"] == "REMOTE_REPL_UNSUPPORTED"


def test_transport_disables_environment_proxy_and_redirects(remote_profile, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid:1234")
    client = RemoteClient(remote_profile)
    assert not any(type(handler).__name__ == "ProxyHandler" for handler in client.opener.handlers)
    redirect = next(handler for handler in client.opener.handlers if isinstance(handler, client_module._NoRedirect))
    assert redirect.redirect_request(None, None, 302, None, None, "http://untrusted.invalid") is None


def test_transport_propagates_protocol_and_authorization(remote_profile, monkeypatch):
    requests = []
    class Response(BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    client = RemoteClient(remote_profile)
    def open_request(request, timeout):
        requests.append(request)
        return Response(b'{"protocol_version":1,"status":"ok"}')
    monkeypatch.setattr(client.opener, "open", open_request)
    assert client.health()["status"] == "ok"
    assert requests[0].get_header("Authorization") == "Bearer test-remote-secret"


@pytest.mark.parametrize("error,code", [(URLError("offline"), "REMOTE_UNAVAILABLE"), (HTTPError("http://local", 401, "no", {}, None), "REMOTE_AUTH_FAILED"), (HTTPError("http://local", 302, "redirect", {}, None), "REMOTE_HTTP_ERROR")])
def test_transport_error_does_not_expose_token(remote_profile, monkeypatch, error, code):
    client = RemoteClient(remote_profile)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(RemoteError) as raised:
        client.health()
    assert raised.value.code == code
    assert "test-remote-secret" not in str(raised.value)


def test_protocol_mismatch_rejected(remote_profile, monkeypatch):
    client = RemoteClient(remote_profile)
    monkeypatch.setattr(client.opener, "open", lambda *args, **kwargs: BytesIO(b'{"protocol_version":2}'))
    with pytest.raises(RemoteError) as raised:
        client.health()
    assert raised.value.code == "REMOTE_PROTOCOL_MISMATCH"


def test_structured_service_errors_remain_diagnostic(remote_profile, monkeypatch):
    client = RemoteClient(remote_profile)
    def fail(*args, **kwargs):
        raise HTTPError("http://local", 409, "Conflict", {}, BytesIO(json.dumps({
            "protocol_version": 1, "error": {"code": "PROJECT_TASK_ACTIVE", "message": "Poll task abc."}
        }).encode()))
    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(RemoteError) as raised:
        client.health()
    assert raised.value.code == "PROJECT_TASK_ACTIVE"
    assert raised.value.message == "Poll task abc."


def test_job_polling_emits_incremental_stderr_preserves_stdout_and_exit(remote_profile, monkeypatch, capsys):
    client = RemoteClient(remote_profile, poll_interval=0)
    calls = []
    jobs = iter([
        {"request_id": "abc", "status": "running", "stderr": "build\n"},
        {"request_id": "abc", "status": "running", "stderr": "build\nprogress\n"},
        {"request_id": "abc", "status": "failed", "stderr": "build\nprogress\n", "stdout": "exact\n", "exit_code": 3},
    ])
    def request(method, path, payload=None):
        calls.append((method, path, payload))
        return next(jobs)
    monkeypatch.setattr(client, "request", request)
    final = client.execute(["editor", "run-script", "-"], "result = 42")
    assert final["stdout"] == "exact\n"
    assert final["exit_code"] == 3
    assert capsys.readouterr().err == "build\nprogress\n"
    assert calls[0][2]["stdin"] == "result = 42"
    assert calls[1][1] == "/v1/commands/abc"


def test_lost_poll_preserves_request_receipt(remote_profile, monkeypatch):
    client = RemoteClient(remote_profile, poll_interval=0)
    def request(method, path, payload=None):
        if method == "POST":
            return {"request_id": "abc", "status": "running"}
        raise RemoteError("REMOTE_UNAVAILABLE", "Offline")
    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RemoteError) as raised:
        client.execute(["editor", "status"])
    assert raised.value.details["request_id"] == "abc"
    assert raised.value.details["command_may_still_be_running"] is True
    assert raised.value.suggestion.endswith("ue-cli remote result abc --connection host-test")


def test_follow_existing_request_never_reposts(remote_profile, monkeypatch):
    client = RemoteClient(remote_profile, poll_interval=0)
    calls = []
    def request(method, path, payload=None):
        calls.append((method, path))
        return {"request_id": "abc", "status": "completed", "stdout": "{}", "exit_code": 0}
    monkeypatch.setattr(client, "request", request)
    assert client.follow("abc")["exit_code"] == 0
    assert calls == [("GET", "/v1/commands/abc")]


HEALTH = {"project_path": "D:\\RXGame_3\\RXGame.uproject", "project_root": "D:\\RXGame_3"}


def test_shared_project_path_validated_and_removed(remote_profile):
    args, stdin, path = prepare_command(["--project", "C:\\Projects\\RXGame_3\\RXGame.uproject", "editor", "status"], remote_profile, HEALTH)
    assert args == ["--output", "json", "editor", "status"]
    assert stdin is None and path is None
    with pytest.raises(RemoteError, match="does not match"):
        prepare_command(["--project", "C:\\Other\\Other.uproject", "editor", "status"], remote_profile, HEALTH)


def test_local_script_is_uploaded_through_stdin(remote_profile, tmp_path):
    script = tmp_path / "scene.py"
    script.write_text("result = {'中文': 1}\n", encoding="utf-8")
    args, stdin, _ = prepare_command(["editor", "run-script", "--no-save", str(script)], remote_profile, HEALTH)
    assert args[-1] == "-"
    assert stdin == "result = {'中文': 1}\n"


def test_shared_script_preserves_file_mode_and_sibling_import_context(remote_profile, tmp_path):
    shared = tmp_path / "shared"
    scripts = shared / "Scripts"
    scripts.mkdir(parents=True)
    script = scripts / "inspect_scene.py"
    script.write_text("from sibling import value\nresult = {'file': __file__, 'value': value}\n", encoding="utf-8")
    (scripts / "sibling.py").write_text("value = 42\n", encoding="utf-8")
    profile = {**remote_profile, "local_project": str(shared)}
    args, stdin, _ = prepare_command(["editor", "run-script", "--no-save", str(script)], profile, HEALTH)
    assert args[-1].lower() == "d:\\rxgame_3\\scripts\\inspect_scene.py"
    assert stdin is None


def test_relative_shared_script_uses_guest_working_directory(remote_profile, tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    scripts = shared / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "scene.py").write_text("result = __file__\n", encoding="utf-8")
    monkeypatch.chdir(scripts)
    profile = {**remote_profile, "local_project": str(shared / "RXGame.uproject")}
    args, stdin, _ = prepare_command(["editor", "run-script", "scene.py"], profile, HEALTH)
    assert args[-1].lower() == "d:\\rxgame_3\\scripts\\scene.py"
    assert stdin is None


def test_shared_script_stays_file_mode_when_guest_and_host_paths_match(remote_profile, tmp_path):
    script = tmp_path / "scene.py"
    script.write_text("result = __file__\n", encoding="utf-8")
    profile = {**remote_profile, "local_project": str(tmp_path)}
    health = {"project_root": str(tmp_path), "project_path": str(tmp_path / "Game.uproject")}
    args, stdin, _ = prepare_command(["editor", "run-script", str(script)], profile, health)
    assert Path(args[-1]) == script
    assert stdin is None


def test_script_outside_shared_root_does_not_inherit_file_mapping(remote_profile, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    adjacent = tmp_path / "shared-other"
    adjacent.mkdir()
    script = adjacent / "scene.py"
    script.write_text("result = 42\n", encoding="utf-8")
    profile = {**remote_profile, "local_project": str(shared)}
    args, stdin, _ = prepare_command(["editor", "run-script", str(script)], profile, HEALTH)
    assert args[-1] == "-"
    assert stdin == "result = 42\n"


def test_missing_shared_script_fails_before_remote_submission(remote_profile, tmp_path):
    profile = {**remote_profile, "local_project": str(tmp_path)}
    with pytest.raises(RemoteError) as raised:
        prepare_command(["editor", "run-script", str(tmp_path / "absent.py")], profile, HEALTH)
    assert raised.value.code == "REMOTE_SCRIPT_UNAVAILABLE"


def test_inline_code_and_unreal_paths_not_rewritten(remote_profile):
    code = "result = r'C:\\Projects\\RXGame_3\\file'"
    args, stdin, _ = prepare_command(["editor", "run-script", "-c", code], remote_profile, HEALTH)
    assert args[-1] == code
    assert stdin is None
    args, _, _ = prepare_command(["editor", "open-level", "/Game/Map"], remote_profile, HEALTH)
    assert args[-1] == "/Game/Map"


def test_stdin_script_forwarded(remote_profile, monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("result = 42\n"))
    _, stdin, _ = prepare_command(["editor", "run-script", "-"], remote_profile, HEALTH)
    assert stdin == "result = 42\n"


def test_screenshot_retains_local_output_only(remote_profile, tmp_path):
    requested = tmp_path / "result.png"
    args, _, output = prepare_command(["screenshot", "capture", "--path", str(requested)], remote_profile, HEALTH)
    assert output == str(requested)
    assert args[-1] == "result.png"


def test_screenshot_directory_preserves_filename(remote_profile, tmp_path):
    args, _, output = prepare_command(["screenshot", "capture", "--path", str(tmp_path), "--filename", "MyShot"], remote_profile, HEALTH)
    assert output == str(tmp_path)
    assert args[args.index("--path") + 1] == "MyShot.jpg"


@pytest.mark.parametrize("value,expected", [
    ({"one": 1, "nested": {"x": "中文"}}, 'one: 1\nnested: {\n  "x": "中文"\n}\n'),
    ([{"x": 1}, "hello"], '{"x": 1}\nhello\n'),
    ({}, ""), ([], ""), (None, "None\n"), ("line\n", "line\n\n"),
])
def test_shared_text_formatter_matches_cli_presentation(value, expected, capsys):
    from cli_anything.unreal.commands import AppState, output
    from cli_anything.unreal.utils.output import format_text_output
    assert format_text_output(value) == expected
    state = AppState()
    state.json_output = False
    output(value, state)
    assert capsys.readouterr().out == expected


def test_download_integrity_and_recursive_result_path_rewrite(remote_profile, tmp_path, monkeypatch):
    client = RemoteClient(remote_profile)
    image = b"fake image"
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: image)
    artifact = {"name": "scene.png", "download_path": "/v1/artifacts/test", "host_path": "D:\\Managed\\scene.png",
                "size": len(image), "sha256": hashlib.sha256(image).hexdigest()}
    job = {"request_id": "job123", "artifacts": [artifact], "stdout": json.dumps({"result": {"default_path": artifact["host_path"]}})}
    target = tmp_path / "image.png"
    result = json.loads(download_artifacts(client, job, str(target)))
    assert result["result"]["default_path"] == str(target)
    assert target.read_bytes() == image
    artifact["sha256"] = "bad"
    with pytest.raises(RemoteError, match="checksum"):
        download_artifacts(client, job)


def test_screenshot_internal_json_restores_text_format(remote_profile):
    client = RemoteClient(remote_profile)
    job = {"stdout": json.dumps({"status": "success", "result": {"default_path": "C:/shot.png"}}),
           "requested_output": "text", "effective_output": "json"}
    assert download_artifacts(client, job) == "default_path: C:/shot.png\n"


def test_artifact_error_cannot_look_successful(remote_profile):
    client = RemoteClient(remote_profile)
    job = {"stdout": "{}", "artifact_error": "Capture file missing", "request_id": "abc", "exit_code": 0}
    with pytest.raises(RemoteError) as raised:
        download_artifacts(client, job)
    assert raised.value.code == "REMOTE_ARTIFACT_UNAVAILABLE"


def test_file_argument_maps_but_adjacent_root_does_not(remote_profile):
    args, _, _ = prepare_command(["assets", "import", "C:\\Projects\\RXGame_3\\Source\\Thing.FBX"], remote_profile, HEALTH)
    assert args[-1].lower() == "d:\\rxgame_3\\source\\thing.fbx"
    args, _, _ = prepare_command(["assets", "import", "C:\\Projects\\RXGame_30\\Thing.FBX"], remote_profile, HEALTH)
    assert args[-1] == "C:\\Projects\\RXGame_30\\Thing.FBX"
    args, _, _ = prepare_command(["assets", "import", "--source=C:\\Projects\\RXGame_3\\Source\\Thing.FBX"], remote_profile, HEALTH)
    assert args[-1].lower() == "--source=d:\\rxgame_3\\source\\thing.fbx"


def test_truncated_command_output_is_not_reported_as_success(remote_profile):
    client = RemoteClient(remote_profile)
    with pytest.raises(RemoteError) as raised:
        download_artifacts(client, {"stdout": "{", "output_truncated": True, "exit_code": 0, "request_id": "abc"})
    assert raised.value.code == "REMOTE_OUTPUT_TRUNCATED"


def test_truncated_progress_retains_final_result(remote_profile, capsys):
    client = RemoteClient(remote_profile)
    text = '{"status":"success","result":{"log_file":"D:/build.log"}}\n'
    returned = download_artifacts(client, {"stdout": text, "output_truncated": True, "stdout_truncated": False,
                                         "stderr_truncated": True, "exit_code": 0, "request_id": "abc"})
    assert returned == text
    assert "progress output was truncated" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-directory alias handling")
def test_guest_directory_alias_resolves_same_shared_project(remote_profile, tmp_path):
    actual = tmp_path / "shared"
    actual.mkdir()
    (actual / "RXGame.uproject").write_text("{}", encoding="utf-8")
    alias = tmp_path / "guest-alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a Windows symlink requires developer mode or privilege")
    profile = {**remote_profile, "local_project": str(actual)}
    args, _, _ = prepare_command(["--project", str(alias / "RXGame.uproject"), "editor", "status"], profile, HEALTH)
    assert args == ["--output", "json", "editor", "status"]


def test_remote_cli_returns_exact_command_exit(remote_profile, monkeypatch):
    monkeypatch.setattr(client_module, "run_remote", lambda *args: 7)
    result = CliRunner().invoke(cli, ["--remote", "host-test", "editor", "status"])
    assert result.exit_code == 7
    assert cli.main(["--remote", "host-test", "editor", "status"], standalone_mode=False) == 7
