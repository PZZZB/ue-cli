"""Shader extraction transport failures must retain completion observation."""

import json
import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.unreal.unreal_cli import cli


@pytest.mark.parametrize("delivered", [True, False])
def test_shader_timeout_observes_late_result_without_redispatch(tmp_path, monkeypatch, delivered):
    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    log_file = tmp_path / "Editor.log"
    log_file.write_text("old log\n", encoding="utf-8")
    markers = {}

    def timeout_response(wrapper, *, timeout):
        assert timeout == 120.0
        for name in ("begin", "end"):
            match = re.search(rf'_cli_observation_{name} = "([^"]+)"', wrapper)
            if match:
                markers[name] = match.group(1)
        if delivered and markers:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"LogPython: {markers['begin']}\n")
        return {"error": "HTTPConnectionPool: Read timed out. (read timeout=120.0)"}

    api = MagicMock()
    api.exec_python_ex.side_effect = timeout_response
    runner = CliRunner()
    with patch("cli_anything.unreal.commands.material.require_editor", return_value=api), \
         patch("cli_anything.unreal.commands.material.require_project"), \
         patch("cli_anything.unreal.commands.editor._resolve_editor_log_file", return_value=log_file):
        response = runner.invoke(cli, ["--output", "json", "material", "shader-source", "/Game/M_Test"])

    data = json.loads(response.output)
    assert response.exit_code == (4 if delivered else 3), data
    assert data["code"] == ("MATERIAL_SHADER_SOURCE_IN_PROGRESS" if delivered else "MATERIAL_SHADER_SOURCE_DELIVERY_UNKNOWN")
    task_id = data["details"]["task_id"]
    assert data["details"]["retry_safe"] is False
    assert runner.invoke(cli, ["task", "cancel", task_id]).exit_code == 4
    with log_file.open("a", encoding="utf-8") as handle:
        if not delivered:
            handle.write(f"LogPython: {markers['begin']}\n")
        handle.write('LogPython: __cli_result__:{"shader_count":156,"source":"plugin"}\n')
        handle.write(f"LogPython: {markers['end']}\n")
    status = runner.invoke(cli, ["task", "status", task_id])
    assert status.exit_code == 0
    task = json.loads(status.output)
    assert task["status"] == "completed"
    assert task["result"]["script_result"]["shader_count"] == 156
    assert api.exec_python_ex.call_count == 1


@pytest.mark.parametrize("outcome", ["success", "error", "missing", "malformed"])
@pytest.mark.parametrize("already_complete", [True, False])
def test_shader_observation_terminal_outcomes(tmp_path, monkeypatch, outcome, already_complete):
    from cli_anything.unreal.core.materials import get_material_shader_source
    from cli_anything.unreal.core.editor_exec import reconcile_editor_observation
    from cli_anything.unreal.errors import UeCliError

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    log_file = tmp_path / "Editor.log"
    # Old, unrelated extraction results must never establish completion.
    log_file.write_text('LogPython: __cli_result__:{"shader_count":999}\n', encoding="utf-8")
    markers = {}

    def finish():
        with log_file.open("a", encoding="utf-8") as handle:
            if outcome != "missing":
                payload = {"shader_count": 7} if outcome == "success" else {"error": "extraction sentinel"}
                raw = "invalid json" if outcome == "malformed" else json.dumps(payload)
                handle.write(f"LogPython: __cli_result__:{raw}\n")
            handle.write(f"LogPython: {markers['end']}\n")

    def response(wrapper, *, timeout):
        assert timeout == 9
        for name in ("begin", "end"):
            markers[name] = re.search(rf'_cli_observation_{name} = "([^"]+)"', wrapper).group(1)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"LogPython: {markers['begin']}\n")
        if already_complete:
            finish()
        return {"error": "Read timed out"}

    api = MagicMock()
    api.exec_python_ex.side_effect = response
    if already_complete and outcome == "success":
        result = get_material_shader_source(api, "/Game/M_Test", timeout=9, log_file=log_file)
        assert result["shader_count"] == 7
        assert result["transport_response_recovered"] is True
    else:
        with pytest.raises(UeCliError) as caught:
            get_material_shader_source(api, "/Game/M_Test", timeout=9, log_file=log_file)
        error = caught.value
        assert error.code == ("MATERIAL_SHADER_SOURCE_FAILED" if already_complete else "MATERIAL_SHADER_SOURCE_IN_PROGRESS")
        if not already_complete:
            finish()
        task = reconcile_editor_observation(error.details["task_id"])
        assert task["status"] == ("completed" if outcome == "success" else "failed")
    assert api.exec_python_ex.call_count == 1


def test_shader_transport_without_log_remains_unknown(tmp_path, monkeypatch):
    from cli_anything.unreal.core.materials import get_material_shader_source
    from cli_anything.unreal.errors import UeCliError

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    api = MagicMock()
    api.exec_python_ex.return_value = {"error": "Connection reset"}
    with pytest.raises(UeCliError) as caught:
        get_material_shader_source(api, "/Game/M_Test")
    assert caught.value.code == "MATERIAL_SHADER_SOURCE_DELIVERY_UNKNOWN"
    assert caught.value.details["task_id"]
    assert "log is unavailable" in caught.value.suggestion


def test_shader_python_timeout_exception_is_not_transport_timeout():
    from cli_anything.unreal.core.materials import get_material_shader_source

    api = MagicMock()
    api.exec_python_ex.return_value = {
        "ReturnValue": True,
        "LogOutput": [{"Output": '__cli_result__:{"error":"timed out","error_type":"TimeoutError"}'}],
    }
    result = get_material_shader_source(api, "/Game/M_Test")
    assert result["error_type"] == "TimeoutError"
    assert "task_id" not in result


def test_shader_timeout_must_be_positive():
    result = CliRunner().invoke(cli, ["material", "shader-source", "/Game/M_Test", "--timeout", "0"])
    assert result.exit_code == 2
