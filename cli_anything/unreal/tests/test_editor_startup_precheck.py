import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.mark.parametrize(
    "exit_code,new_fatal,method,expected_exit",
    [(0, False, "process_exit", 0),
     (3, False, "process_exit", 3),
     (0, True, "process_exit", 3),
     (None, True, "process_exit", 3),
     (None, False, "process_exit", 0),
     (1, False, "process_tree_kill", 0),
     (3, True, "process_tree_kill", 3)],
)
def test_editor_close_checks_new_shutdown_evidence(
    mini_project, exit_code, new_fatal, method, expected_exit,
):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    log_file = Path(mini_project).parent / "Saved" / "Logs" / "MiniProject.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("Fatal error: historical crash\nLogInit: online\n", encoding="utf-8")
    api = MagicMock()
    api._verified_editor_pid = 98364
    api._verified_editor_cmdline = f'UnrealEditor.exe "{mini_project}"'
    api.is_alive.return_value = False
    probe = MagicMock()
    probe.snapshot.return_value = {
        "editor_pid": 98364, "process_alive": False,
        **({"process_exit_code": exit_code} if exit_code is not None else {}),
    }

    def shutdown(command, **kwargs):
        assert command == "CLOSE_SLATE_MAINFRAME"
        if new_fatal:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write("Unhandled Exception: EXCEPTION_ACCESS_VIOLATION reading address 0x78\n")
        return {}

    api.exec_console.side_effect = shutdown
    with patch("cli_anything.unreal.commands.editor.require_editor", return_value=api), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 98364, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.core.editor_lifecycle._EditorProcessExitProbe", return_value=probe), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={
             "status": "closed", "method": method,
         }):
        result = CliRunner().invoke(cli, ["--output", "json", "--project", mini_project, "editor", "close"])

    assert result.exit_code == expected_exit, result.output
    data = json.loads(result.output)
    probe.close.assert_called_once()
    api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)
    if expected_exit:
        assert data["code"] == "EDITOR_CLOSE_CRASHED"
        assert data["details"]["stage"] == "editor_shutdown"
        assert "historical crash" not in str(data["details"].get("fatal_log_tail", []))
    else:
        assert data["result"]["status"] == "closed"
        assert "fatal_log_tail" not in data["result"]["shutdown_evidence"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handle contract")
def test_editor_process_exit_probe_preserves_exit_code_after_process_exit():
    from cli_anything.unreal.core.editor_lifecycle import _EditorProcessExitProbe

    process = subprocess.Popen([
        sys.executable,
        "-c",
        "import sys, time; time.sleep(0.2); sys.exit(7)",
    ])
    probe = _EditorProcessExitProbe(process.pid)
    try:
        process.wait(timeout=10)
        snapshot = probe.snapshot()
    finally:
        probe.close()
        if process.poll() is None:
            process.kill()

    assert snapshot["editor_pid"] == process.pid
    assert snapshot["process_alive"] is False
    assert snapshot["process_exit_status"] == "exited"
    assert snapshot["process_exit_code"] == 7
    assert snapshot["process_exit_code_hex"] == "0x00000007"





@pytest.fixture
def mini_project(tmp_path):
    project_dir = tmp_path / "MiniProject"
    project_dir.mkdir()
    uproject = project_dir / "MiniProject.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    return str(uproject)


@pytest.fixture(autouse=True)
def _isolate_synthetic_close_process_identities(request, monkeypatch):
    """Fake editor PIDs must not resolve to unrelated Windows processes."""
    if "editor_close" in request.node.name or "kill_matching_project_editors" in request.node.name:
        monkeypatch.setattr(
            "cli_anything.unreal.utils.ue_backend._windows_process_identity",
            lambda _pid: {"query_ok": True, "found": False},
        )


@pytest.fixture(autouse=True)
def _clean_dirty_state_for_existing_close_tests(request, monkeypatch):
    """Legacy close lifecycle tests operate on an explicitly clean editor."""

    if "editor_close" not in request.node.name:
        return
    import cli_anything.unreal.commands as command_helpers
    from cli_anything.unreal.commands import editor as editor_commands

    if "dirty" not in request.node.name:
        monkeypatch.setattr(
            editor_commands,
            "_query_dirty_editor_packages",
            lambda _api: {"map_packages": [], "content_packages": [], "count": 0},
        )
    if "other_project" not in request.node.name:
        monkeypatch.setattr(
            editor_commands,
            "_guard_editor_project",
            lambda _state, _api_cls: {"pid": 0, "project": "verified-by-test"},
        )
        monkeypatch.setattr(
            command_helpers,
            "_guard_editor_project",
            lambda _state, _api_cls: {"pid": 0, "project": "verified-by-test"},
        )
        monkeypatch.setattr(
            editor_commands,
            "_partition_editor_close_targets",
            lambda targets, _owner_pid: (targets[:1], targets[1:]),
        )


def test_editor_status_offline_api_blocked_includes_log_error(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[{"pid": 1234, "project": mini_project}]), \
         patch("cli_anything.unreal.commands.editor._check_log_errors", return_value="Plugin 'libzstd' failed to load"), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": False,
             "engine": {"errors": ["engine error"], "warnings": []},
             "project": {"errors": ["project error"], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"][0]["status"] == "offline"
    assert data["result"][0]["log_error"] == "Plugin 'libzstd' failed to load"


def test_editor_status_reports_process_discovery_deadline(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch(
        "cli_anything.unreal.commands.editor.time.monotonic",
        side_effect=[100.0, 100.0, 101.1],
    ), patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[],
    ) as find_editors:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status", "--timeout", "1",
        ])

    assert result.exit_code == 4, result.output
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_STATUS_TIMEOUT"
    assert data["details"]["blocking_phase"] == "process_discovery"
    assert data["details"]["timeout_seconds"] == 1.0
    assert find_editors.call_args.kwargs["timeout"] == pytest.approx(1.0)


def test_editor_task_status_reports_blocked_task_id(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import TaskLockTimeout
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch(
        "cli_anything.unreal.commands.editor.load_task",
        side_effect=TaskLockTimeout("t-blocked", 0.1),
    ):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status", "--timeout", "0.1", "t-blocked",
        ])

    assert result.exit_code == 4, result.output
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_STATUS_TIMEOUT"
    assert data["details"]["blocking_phase"] == "task_discovery"
    assert data["details"]["task_id"] == "t-blocked"


def test_editor_status_transient_unreachable_does_not_suggest_relaunch(mini_project, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[{"pid": 64644, "project": mini_project}]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011), \
         patch("cli_anything.unreal.commands.editor._check_log_errors", return_value=None):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0, result.output
    item = json.loads(result.output)["result"][0]
    assert item["status"] == "unreachable"
    assert item["pid"] == 64644
    assert item["port"] == 30011
    assert "temporarily unreachable" in item["message"]
    assert "editor launch" not in item["suggestion"]
    assert item["next_command"] == f'ue-cli --project "{mini_project}" editor status'


def test_editor_status_unreachable_becomes_offline_after_grace(mini_project, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    runner = CliRunner()

    base_patches = [
        patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]),
        patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[{"pid": 64644, "project": mini_project}]),
        patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011),
        patch("cli_anything.unreal.commands.editor._check_log_errors", return_value=None),
    ]
    with base_patches[0], base_patches[1], base_patches[2], base_patches[3], \
         patch("cli_anything.unreal.commands.editor.time.time", return_value=1000.0):
        first = runner.invoke(cli, ["--output", "json", "--project", mini_project, "editor", "status"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["result"][0]["status"] == "unreachable"

    base_patches = [
        patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]),
        patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[{"pid": 64644, "project": mini_project}]),
        patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011),
        patch("cli_anything.unreal.commands.editor._check_log_errors", return_value=None),
    ]
    with base_patches[0], base_patches[1], base_patches[2], base_patches[3], \
         patch("cli_anything.unreal.commands.editor.time.time", return_value=1100.0):
        stale = runner.invoke(cli, ["--output", "json", "--project", mini_project, "editor", "status"])

    assert stale.exit_code == 0, stale.output
    item = json.loads(stale.output)["result"][0]
    assert item["status"] == "offline"
    assert item["unreachable_seconds"] == 100
    assert "editor launch" in item["suggestion"]
    assert item["next_command"] == f'ue-cli --project "{mini_project}" editor launch'


def test_editor_status_rechecks_project_port_before_reporting_unreachable(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", side_effect=[
        [],
        [{"port": 30010, "alive": True, "info": {"ok": True}}],
    ]) as scan_ports, \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=100256), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 100256, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=None), \
         patch("cli_anything.unreal.commands.editor._check_log_errors", return_value=None), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.18"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.18"):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0, result.output
    item = json.loads(result.output)["result"][0]
    assert item["status"] == "online"
    assert item["pid"] == 100256
    assert item["port"] == 30010
    assert "next_command" not in item
    assert scan_ports.call_args_list[-1].kwargs == {
        "port_range": (30010, 30010),
        "timeout": 3.0,
    }


def test_editor_status_offline_ignores_other_project_processes(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 5678, "project": other_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": True,
             "engine": {"errors": [], "warnings": []},
             "project": {"errors": [], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0
    assert result.output.count('"status": "success"') == 1
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"] == []


def test_editor_status_project_filter_skips_unrelated_cook_workers(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import TaskDiscoveryTimeout
    from cli_anything.unreal.unreal_cli import cli

    other_project = str(Path(mini_project).with_name("Other.uproject"))
    cook_workers = [
        {
            "pid": 6000 + index,
            "project": other_project,
            "cmdline": (
                f'UnrealEditor-Cmd.exe "{other_project}" '
                f"-run=cook -cookworker -CookWorkerId={index}"
            ),
        }
        for index in range(4)
    ]

    def exhaust_deadline_on_unrelated_tasks(timeout=None):
        time.sleep(min(0.05, float(timeout or 0.05)))
        raise TaskDiscoveryTimeout("t-unrelated-cook", timeout)

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch(
             "cli_anything.unreal.utils.ue_backend.find_running_editors",
             return_value=cook_workers,
         ), \
         patch(
             "cli_anything.unreal.core.editor_lifecycle.iter_task_snapshots",
             side_effect=exhaust_deadline_on_unrelated_tasks,
         ) as snapshot_scan:
        result = CliRunner().invoke(cli, [
            "--output", "json", "editor", "status",
            "--project", mini_project,
            "--timeout", "0.2",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["result"] == []
    snapshot_scan.assert_not_called()


def test_editor_status_accepts_project_option_after_subcommand(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
             {"pid": 5678, "project": other_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": True,
             "engine": {"errors": [], "warnings": []},
             "project": {"errors": [], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status", "--project", mini_project,
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert len(data["result"]) == 1
    assert data["result"][0]["project_path"] == mini_project


def test_editor_status_all_lists_other_project_processes(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 5678, "project": other_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": True,
             "engine": {"errors": [], "warnings": []},
             "project": {"errors": [], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status", "--all",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "success"
    instance = data["result"][0]
    assert instance["status"] == "unreachable"
    assert instance["pid"] == 5678
    assert instance["project_path"] == other_project
    assert "editor launch" not in instance["suggestion"]


def test_editor_status_all_deduplicates_discovered_editors(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    discovered = [
        {"pid": 1234, "project": mini_project},
        {"pid": 5678, "project": other_project},
    ]

    def fake_owner_pid(port, timeout=3):
        return {30020: 1234, 30021: 5678}[port]

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
            {"port": 30021, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", side_effect=fake_owner_pid), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=discovered + discovered), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=None), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.23"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.23"):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status", "--all",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [
        (item["pid"], item["port"], item["project_path"])
        for item in data["result"]
    ] == [
        (1234, 30020, mini_project),
        (5678, 30021, other_project),
    ]


def test_editor_status_filters_online_port_owner_when_other_project(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=5678), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 5678, "project": other_project},
         ]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": True,
             "engine": {"errors": [], "warnings": []},
             "project": {"errors": [], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"] == []


def test_editor_status_online_includes_matching_bridge_versions(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    item = data["result"][0]
    assert item["bridge_version"] == "1.13"
    assert item["bundled_version"] == "1.13"
    assert item["plugin_match"] is True
    assert "next_command" not in item


def test_editor_status_online_reports_no_restart_path_on_bridge_mismatch(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.19"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.18"):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    item = data["result"][0]
    assert item["bridge_version"] == "1.18"
    assert item["bundled_version"] == "1.19"
    assert item["plugin_match"] is False
    assert item["bridge_status"] == "version_mismatch"
    assert item["degraded_mode"] == "remote_control_only"
    assert item["remote_control_commands_available"] is True
    assert item["run_script_no_save_available"] is True
    assert item["bridge_commands_available"] is False
    assert "next_command" not in item
    assert item["upgrade_command"] == f'ue-cli --project "{mini_project}" editor plugin-upgrade'
    assert item["no_restart_command"] == (
        f'ue-cli --output json --project "{mini_project}" editor run-script --no-save -'
    )
    assert item["restart_required"] is True
    assert item["restart_scope"] == "bridge_commands_only"
    assert "running editor loaded" in item["message"]
    assert "Remote Control remains available" in item["message"]
    assert "does not sandbox" in item["suggestion"]
    assert "plugin-upgrade" in item["suggestion"]


def test_editor_status_online_missing_bridge_reports_remote_control_only_mode(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value=None), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.18"), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[{"pid": 1234, "project": mini_project}]):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0
    item = json.loads(result.output)["result"][0]
    assert item["status"] == "online"
    assert item["bridge_version"] is None
    assert item["bundled_version"] == "1.18"
    assert item["plugin_match"] is False
    assert item["bridge_status"] == "missing_or_unversioned"
    assert item["degraded_mode"] == "remote_control_only"
    assert item["read_only_commands_available"] is True
    assert item["remote_control_commands_available"] is True
    assert item["run_script_no_save_available"] is True
    assert item["bridge_commands_available"] is False
    assert "restart_required" not in item
    assert "next_command" not in item
    assert "upgrade_command" in item
    assert "editor run-script --no-save" in item["suggestion"]
    assert "does not sandbox" in item["suggestion"]


def test_editor_status_online_without_project_still_includes_bridge_versions():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=None), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    item = data["result"][0]
    assert item["project_path"] is None
    assert item["bridge_version"] == "1.13"
    assert item["bundled_version"] == "1.13"
    assert item["plugin_match"] is True
    assert "next_command" not in item


def test_editor_status_failed_bridge_health_probe_is_unreachable(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", side_effect=TimeoutError("busy")):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    item = data["result"][0]
    assert item["status"] == "unreachable"
    assert item["bridge_version"] is None
    assert item["bundled_version"] == "1.13"
    assert item["plugin_match"] is None
    assert item["listener_reachable"] is True
    assert item["health_probe"] == "failed"
    assert item["health_probe_error"] == "busy"
    assert "retry editor status" in item["suggestion"]
    assert "do not launch another editor" in item["suggestion"]
    assert item["next_command"] == f'ue-cli --project "{mini_project}" editor status'


def test_editor_status_bridge_probe_uses_short_timeout(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}

    def fake_loaded(_api, timeout=10.0, raise_on_error=False):
        captured["timeout"] = timeout
        captured["raise_on_error"] = raise_on_error
        return "1.13"

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", side_effect=fake_loaded):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    assert captured["timeout"] <= 5.0
    assert captured["raise_on_error"] is True


def test_editor_status_bridge_probe_preserves_subsecond_timeout():
    from cli_anything.unreal.commands.editor import _add_online_bridge_status

    captured = {}

    class FakeApi:
        def exec_python_ex(self, _code, *, timeout=None):
            captured["timeout"] = timeout
            return {
                "ReturnValue": True,
                "CommandResult": "None",
                "LogOutput": [
                    {
                        "Type": "Info",
                        "Output": '__cli_result__:{"version": "1.37"}',
                    },
                ],
            }

    entry = {
        "status": "online",
        "port": 30011,
        "project_path": None,
    }
    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=FakeApi(),
    ), patch(
        "cli_anything.unreal.core.plugin_bridge.get_bundled_version",
        return_value="1.37",
    ):
        _add_online_bridge_status(entry, timeout=0.25)

    assert captured["timeout"] == pytest.approx(0.25)
    assert entry["status"] == "online"
    assert entry["bridge_version"] == "1.37"
    assert entry["plugin_match"] is True
    assert "health_probe_error" not in entry


def test_editor_status_bridge_probes_online_ports_concurrently():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    calls = []
    calls_lock = threading.Lock()
    both_started = threading.Event()

    def fake_loaded(api, timeout=10.0, raise_on_error=False):
        with calls_lock:
            calls.append(api.port)
            if len(calls) == 2:
                both_started.set()
        if not both_started.wait(0.5):
            return None
        return "1.13"

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
            {"port": 30021, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=None), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", side_effect=fake_loaded):
        start = time.perf_counter()
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status", "--all",
        ])
        elapsed = time.perf_counter() - start

    assert result.exit_code == 0, result.output
    assert sorted(calls) == [30020, 30021]
    assert elapsed < 0.45


def test_editor_status_resolves_online_port_owners_concurrently():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    calls = []
    calls_lock = threading.Lock()
    both_started = threading.Event()

    def fake_pid(port, timeout=3):
        with calls_lock:
            calls.append(port)
            if len(calls) == 2:
                both_started.set()
        if not both_started.wait(0.5):
            return None
        return None

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
            {"port": 30021, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", side_effect=fake_pid), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[]), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"):
        start = time.perf_counter()
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status", "--all",
        ])
        elapsed = time.perf_counter() - start

    assert result.exit_code == 0, result.output
    assert sorted(calls) == [30020, 30021]
    assert elapsed < 0.45


def test_editor_status_lists_all_editor_processes(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
             {"pid": 5678, "project": other_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30030), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": True,
             "engine": {"errors": [], "warnings": []},
             "project": {"errors": [], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "success"
    online, offline = data["result"]
    assert online == {
        "status": "online",
        "pid": 1234,
        "port": 30020,
        "project_path": mini_project,
        "bridge_version": "1.13",
        "bundled_version": "1.13",
        "plugin_match": True,
    }
    assert offline["status"] == "unreachable"
    assert offline["pid"] == 5678
    assert offline["port"] == 30030
    assert offline["project_path"] == other_project
    assert "temporarily unreachable" in offline["message"]
    assert "editor launch" not in offline["suggestion"]
    assert offline["next_command"] == f'ue-cli --project "{other_project}" editor status'


def test_editor_status_marks_offline_process_as_launching_when_task_active(mini_project, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import create_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    task = create_task("editor.launch", {"project_path": mini_project, "port": 30011})
    task["status"] = "running"
    task["pid"] = 16044
    task["result"] = {"pid": 16044, "bridge_binary_status": {"ready": True}}
    save_task(task)

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 16044, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": True,
             "engine": {"errors": [], "warnings": []},
             "project": {"errors": [], "warnings": []},
         }):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    item = data["result"][0]
    assert item["status"] == "launching"
    assert item["pid"] == 16044
    assert item["port"] == 30011
    assert item["task_id"] == task["task_id"]
    assert item["launch_task_status"] == "running"
    assert "editor status " + task["task_id"] in item["next_command"]
    assert "editor launch" not in item["suggestion"]


def test_root_status_retains_cached_launch_state_when_task_discovery_times_out(
    mini_project,
    tmp_path,
    monkeypatch,
):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import TaskLockTimeout, create_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    task = create_task("editor.launch", {"project_path": mini_project, "port": 30011})
    task["status"] = "running"
    task["phase"] = "waiting_remote_control"
    task["pid"] = 16044
    save_task(task)

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 16044, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011), \
         patch(
             "cli_anything.unreal.core.editor_lifecycle.load_task",
             side_effect=TaskLockTimeout(task["task_id"], 0.2),
         ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "status", "--timeout", "0.2",
        ])

    assert result.exit_code == 0, result.output
    item = json.loads(result.output)["result"][0]
    assert item["status"] == "launching"
    assert item["pid"] == 16044
    assert item["task_id"] == task["task_id"]
    assert item["launch_task_status"] == "running"
    assert item["task_state_source"] == "last_published_snapshot"
    assert item["task_state_may_be_stale"] is True
    assert item["task_discovery_timeout"] == {
        "blocking_phase": "task_discovery",
        "task_id": task["task_id"],
        "timeout_seconds": 0.2,
    }
    assert "last published launch state was retained" in item["message"]


def test_root_status_ignores_launch_snapshot_for_different_process(
    mini_project,
    tmp_path,
    monkeypatch,
):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import create_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    task = create_task("editor.launch", {"project_path": mini_project, "port": 30011})
    task["status"] = "running"
    task["pid"] = 99999
    save_task(task)

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 16044, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011), \
         patch(
             "cli_anything.unreal.core.editor_lifecycle.load_task",
             side_effect=AssertionError("mismatched task must not be locked"),
         ) as mismatched_task_read:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "status", "--timeout", "0.2",
        ])

    assert result.exit_code == 0, result.output
    item = json.loads(result.output)["result"][0]
    assert item["status"] == "unreachable"
    assert "task_id" not in item
    mismatched_task_read.assert_not_called()


def test_root_status_skips_unrelated_locked_task_before_active_launch(
    mini_project,
    tmp_path,
    monkeypatch,
):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import TaskLockTimeout, create_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    unrelated = create_task("build.compile", {
        "project_path": str(tmp_path / "Other.uproject"),
    })
    unrelated["status"] = "running"
    save_task(unrelated)
    launch = create_task("editor.launch", {
        "project_path": mini_project,
        "port": 30011,
    })
    launch["status"] = "running"
    launch["phase"] = "waiting_remote_control"
    launch["pid"] = 16044
    save_task(launch)

    def load_matching_task(task_id, timeout=None):
        assert task_id == launch["task_id"]
        return launch

    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 16044, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011), \
         patch(
             "cli_anything.unreal.core.tasks.load_task",
             side_effect=TaskLockTimeout(unrelated["task_id"], 0.2),
         ) as unrelated_task_read, \
         patch(
             "cli_anything.unreal.core.editor_lifecycle.load_task",
             side_effect=load_matching_task,
         ) as task_read:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "status", "--timeout", "0.2",
        ])

    assert result.exit_code == 0, result.output
    item = json.loads(result.output)["result"][0]
    assert item["status"] == "launching"
    assert item["task_id"] == launch["task_id"]
    unrelated_task_read.assert_not_called()
    task_read.assert_called_once()


def test_root_status_reports_snapshot_discovery_deadline(
    mini_project,
    tmp_path,
    monkeypatch,
):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import TaskDiscoveryTimeout
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 16044, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30011), \
         patch(
             "cli_anything.unreal.core.editor_lifecycle.iter_task_snapshots",
             side_effect=TaskDiscoveryTimeout("t-scan", 0.2),
         ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "status", "--timeout", "0.2",
        ])

    assert result.exit_code == 4, result.output
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_STATUS_TIMEOUT"
    assert data["details"]["blocking_phase"] == "task_discovery"
    assert data["details"]["task_id"] == "t-scan"


def test_task_snapshot_scan_honors_deadline(tmp_path, monkeypatch):
    from cli_anything.unreal.core import tasks

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    task = tasks.create_task("editor.launch", {
        "project_path": str(tmp_path / "Mini.uproject"),
    })

    with patch.object(tasks.time, "monotonic", side_effect=[100.0, 100.0, 100.3]):
        with pytest.raises(tasks.TaskDiscoveryTimeout) as exc_info:
            tasks.iter_task_snapshots(timeout=0.2)

    assert exc_info.value.task_id == task["task_id"]
    assert exc_info.value.timeout == 0.2


def test_editor_status_scans_running_project_config_ports_outside_default_range(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", side_effect=[
            [],
            [{"port": 30023, "alive": True, "info": {"ok": True}}],
         ]) as scan_ports, \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=1234), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30023), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"] == [
        {
            "status": "online",
            "pid": 1234,
            "port": 30023,
            "project_path": mini_project,
            "bridge_version": "1.13",
            "bundled_version": "1.13",
            "plugin_match": True,
        },
    ]
    assert [call.kwargs["port_range"] for call in scan_ports.call_args_list] == [
        (30010, 30020),
        (30023, 30023),
    ]


def test_editor_status_does_not_claim_config_port_when_owner_pid_unavailable(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_http_api.scan_editor_ports", return_value=[
            {"port": 30020, "alive": True, "info": {"ok": True}},
         ]), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=None), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend.read_rc_port", return_value=30020), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.13"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", return_value="1.13"):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "status",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "success"
    process_entry = next(item for item in data["result"] if item.get("pid") == 1234)
    unknown_entry = next(item for item in data["result"] if item.get("ownership") == "unknown")
    assert process_entry["status"] in {"offline", "unreachable"}
    assert process_entry["project_path"] == mini_project
    assert unknown_entry["status"] == "online"
    assert unknown_entry["port"] == 30020
    assert unknown_entry["project_path"] is None


def test_editor_list_command_removed():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--output", "json",
        "editor", "list",
    ])

    assert result.exit_code != 0


def test_check_port_in_use_detects_plain_tcp_listener():
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.core.editor_lifecycle import _check_port_in_use

    state = AppState()
    state.session.port = 30020
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", return_value=False), \
         patch("cli_anything.unreal.utils.ue_backend.is_tcp_port_in_use", return_value=True):
        result = _check_port_in_use(30020, state)

    assert result is not None
    assert result["status"] == "port_in_use"
    assert result["port"] == 30020


def test_editor_close_kills_matching_zombie_project_process(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", return_value=False), \
         patch("cli_anything.unreal.commands._discover_online_editor_port", return_value=None), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
             {"pid": 5678, "project": other_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend._kill_process_tree_result", return_value={"ok": True}) as kill_process:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--force",
        ])

    assert result.exit_code == 0, result.output
    kill_process.assert_called_once_with(1234)
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_tree_kill"
    assert data["result"]["closed_processes"] == [{"pid": 1234, "project": mini_project}]


def test_editor_close_force_terminates_project_editor_blocked_by_startup_dialog(
    mini_project,
):
    from click.testing import CliRunner
    from cli_anything.unreal.commands import AppError
    from cli_anything.unreal.unreal_cli import cli

    blocked = AppError(
        "EDITOR_BLOCKED_BY_DIALOG",
        "Editor execution appears blocked by a non-brokered dialog window.",
        exit_code=4,
        details={
            "confirmations": [
                {
                    "pid": 1234,
                    "source": "window",
                    "title": "Missing MiniProject Modules",
                    "answerable": False,
                }
            ]
        },
    )
    with patch(
        "cli_anything.unreal.commands.editor.require_editor",
        side_effect=blocked,
    ), patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[{"pid": 1234, "project": mini_project}],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        return_value={"ok": True},
    ) as kill_process:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--force",
        ])

    assert result.exit_code == 0, result.output
    kill_process.assert_called_once_with(1234)
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_tree_kill"
    assert data["result"]["closed_processes"] == [
        {"pid": 1234, "project": mini_project}
    ]


def test_editor_close_recovers_unique_live_port_before_reporting_offline():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    offline_api = MagicMock()
    offline_api.is_alive.return_value = False
    live_api = MagicMock()
    live_api.is_alive.side_effect = [True, False]
    live_identity = {
        "query_ok": True,
        "found": True,
        "pid": 24356,
        "creation_time": 1001,
        "image_path": r"F:\UE\Engine\Binaries\Win64\UnrealEditor.exe",
        "identity_source": "win32_process_times",
    }
    exited_identity = {
        "query_ok": True,
        "found": False,
        "pid": 24356,
        "identity_source": "win32_process_times",
    }

    def create_api(port):
        if port == 30010:
            return offline_api
        if port == 30011:
            return live_api
        raise AssertionError(f"Unexpected editor port: {port}")

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", side_effect=create_api) as api_cls, \
         patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
             "status": "online",
             "pid": 24356,
             "port": 30011,
             "project_path": r"F:\RXGame_2\RXGame.uproject",
         }]) as scan_status, \
         patch(
             "cli_anything.unreal.utils.ue_backend._windows_process_identity",
             side_effect=[live_identity, exited_identity],
         ):
        api_cls._get_pid_listening_on_port.return_value = 24356
        result = CliRunner().invoke(cli, [
            "--output", "json",
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    assert [call.kwargs["port"] for call in api_cls.call_args_list] == [30010, 30011]
    scan_status.assert_called_once()
    assert scan_status.call_args.args[1] == "30010-30020"
    assert scan_status.call_args.kwargs == {"include_bridge_status": False}
    offline_api.exec_console.assert_not_called()
    live_api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["port"] == 30011
    assert data["result"]["method"] == "process_exit"
    assert data["result"]["target_pids"] == [24356]
    assert data["result"]["pid_evidence"] == [{
        "pid": 24356,
        "project_match": False,
        "identity_query_ok": True,
        "exists": False,
        "identity_matches": False,
    }]


def test_editor_close_without_project_kills_surviving_port_owner():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    live_identity = {
        "query_ok": True,
        "found": True,
        "pid": 180812,
        "creation_time": 1001,
        "image_path": r"F:\UE\Engine\Binaries\Win64\UnrealEditor.exe",
        "identity_source": "win32_process_times",
    }
    exited_identity = {
        "query_ok": True,
        "found": False,
        "pid": 180812,
        "identity_source": "win32_process_times",
    }
    api = MagicMock()
    api.is_alive.side_effect = [True, False]

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=api,
    ) as api_cls, patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_identity",
        side_effect=[live_identity, live_identity, exited_identity],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        return_value={"ok": True, "pid": 180812, "method": "taskkill"},
    ) as kill_process, patch(
        "cli_anything.unreal.commands.editor.time.time",
        side_effect=[0, 0, 0, 11],
    ), patch(
        "cli_anything.unreal.commands.editor.time.sleep",
    ):
        api_cls._get_pid_listening_on_port.return_value = 180812
        result = CliRunner().invoke(cli, [
            "--output", "json",
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)
    kill_process.assert_called_once_with(180812)
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_tree_kill"
    assert data["result"]["closed_processes"] == [{
        "pid": 180812,
        "project": "",
    }]


def test_editor_close_without_project_rejects_unknown_port_owner():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    api = MagicMock()
    api.is_alive.return_value = True
    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=api,
    ) as api_cls:
        api_cls._get_pid_listening_on_port.return_value = None
        result = CliRunner().invoke(cli, [
            "--output", "json",
            "editor", "close",
        ])

    assert result.exit_code == 3
    api.exec_console.assert_not_called()
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_CLOSE_TARGET_UNKNOWN"
    assert data["details"] == {"port": 30010}
    assert "--project" in data["suggestion"]


def test_editor_close_rejects_ambiguous_live_ports():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    offline_api = MagicMock()
    offline_api.is_alive.return_value = False
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=offline_api), \
         patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[
             {"status": "online", "pid": 111, "port": 30011, "project_path": r"F:\One\One.uproject"},
             {"status": "online", "pid": 222, "port": 30012, "project_path": r"F:\Two\Two.uproject"},
         ]):
        result = CliRunner().invoke(cli, [
            "--output", "json",
            "editor", "close",
        ])

    assert result.exit_code == 3
    offline_api.exec_console.assert_not_called()
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_TARGET_AMBIGUOUS"
    assert "--project" in data["suggestion"]
    assert "--port" in data["suggestion"]
    assert [item["port"] for item in data["details"]["live_editors"]] == [30011, 30012]


def test_editor_close_does_not_override_explicit_stale_port():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    offline_api = MagicMock()
    offline_api.is_alive.return_value = False
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=offline_api), \
         patch("cli_anything.unreal.commands.editor._scan_editor_status_instances") as scan_status:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--port", "30010",
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    scan_status.assert_not_called()
    data = json.loads(result.output)
    assert data["result"] == {
        "status": "offline",
        "port": 30010,
        "message": "No editor running on this port.",
    }


def test_kill_matching_project_editors_skips_reused_pid(mini_project):
    from cli_anything.unreal.commands.editor import _kill_matching_project_editors

    original = {"pid": 1234, "project": mini_project}
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    reused = {"pid": 1234, "project": other_project}

    with patch(
        "cli_anything.unreal.commands.editor._find_matching_project_editors",
        side_effect=[
            ([original], [original]),
            ([reused], []),
        ],
    ) as find_matches, patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_exists",
        return_value=False,
    ) as process_exists, patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
    ) as kill_process:
        result = _kill_matching_project_editors(
            mini_project,
            30010,
            success_message="closed",
            failure_message="failed",
        )

    assert result["status"] == "closed"
    assert result["closed_processes"] == [{
        "pid": 1234,
        "project": mini_project,
        "already_exited": True,
        "skipped": True,
    }]
    assert find_matches.call_count == 2
    process_exists.assert_called_once_with(1234)
    kill_process.assert_not_called()


def test_kill_matching_project_editors_fails_when_unmatched_pid_still_exists(mini_project):
    from cli_anything.unreal.commands.editor import _kill_matching_project_editors

    original = {"pid": 1234, "project": mini_project}

    with patch(
        "cli_anything.unreal.commands.editor._find_matching_project_editors",
        side_effect=[
            ([original], [original]),
            ([], []),
        ],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_exists",
        return_value=True,
    ) as process_exists, patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
    ) as kill_process:
        result = _kill_matching_project_editors(
            mini_project,
            30010,
            success_message="closed",
            failure_message="failed",
        )

    assert result["status"] == "failed"
    assert "closed_processes" not in result
    failed = result["failed_processes"][0]
    assert failed["pid"] == 1234
    assert failed["kill_result"]["process_exists_after_rescan"] is True
    assert failed["kill_result"]["retry_suggested"] is True
    assert "still running" in failed["kill_result"]["error"].lower()
    process_exists.assert_called_once_with(1234)
    kill_process.assert_not_called()


def test_kill_matching_project_editors_fails_when_any_target_survives(
    mini_project,
):
    from cli_anything.unreal.commands.editor import _kill_matching_project_editors

    matches = [
        {"pid": 1234, "project": mini_project},
        {"pid": 5678, "project": mini_project},
    ]
    with patch(
        "cli_anything.unreal.commands.editor._find_matching_project_editors",
        return_value=(matches, matches),
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_identity",
        return_value={"query_ok": True, "found": False},
    ), patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        side_effect=[
            {"ok": True, "pid": 1234},
            {
                "ok": False,
                "pid": 5678,
                "error": "Access is denied.",
                "retry_suggested": False,
            },
        ],
    ):
        result = _kill_matching_project_editors(
            mini_project,
            30010,
            success_message="closed",
            failure_message="failed",
        )

    assert result["status"] == "failed"
    assert result["closed_processes"] == [{
        "pid": 1234,
        "project": mini_project,
    }]
    assert result["failed_processes"][0]["pid"] == 5678
    assert result["failed_processes"][0]["kill_result"]["ok"] is False


def test_editor_close_kills_matching_project_process_after_graceful_timeout(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False, True]

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.commands.editor.time.time", side_effect=[0, 0, 31]), \
         patch("cli_anything.unreal.commands.editor.time.sleep"), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend._kill_process_tree_result", return_value={"ok": True}) as kill_process:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    mock_api.call_function.assert_not_called()
    mock_api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)
    kill_process.assert_called_once_with(1234)
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_tree_kill"
    assert "did not close gracefully" in data["result"]["message"]


def test_editor_close_waits_for_process_exit_after_api_closes(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 98364, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={
             "status": "closed",
             "method": "process_exit",
         }) as mock_wait:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    mock_api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)
    mock_wait.assert_called_once()
    assert mock_wait.call_args.args == (mini_project, 30010)
    assert mock_wait.call_args.kwargs["timeout"] == 10
    assert mock_wait.call_args.kwargs["targets"] == [{
        "pid": 98364,
        "project": mini_project,
    }]
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_exit"


def test_editor_close_terminates_stale_peer_without_waiting_for_it(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    active_target = {"pid": 78808, "project": mini_project}
    stale_target = {"pid": 79352, "project": mini_project}
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=mock_api,
    ) as api_class, patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[active_target, stale_target],
    ), patch(
        "cli_anything.unreal.commands.editor._capture_project_editor_targets",
        return_value=[active_target, stale_target],
    ), patch(
        "cli_anything.unreal.commands.editor._wait_for_project_editor_exit",
        return_value={
            "status": "closed",
            "method": "process_exit",
            "target_pids": [78808],
        },
    ) as wait_for_exit, patch(
        "cli_anything.unreal.commands.editor._kill_matching_project_editors",
        return_value={
            "status": "closed",
            "method": "process_tree_kill",
            "closed_processes": [stale_target],
        },
    ) as kill_matching:
        api_class._get_pid_listening_on_port.return_value = 78808
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--force",
        ])

    assert result.exit_code == 0, result.output
    wait_for_exit.assert_called_once_with(
        mini_project,
        30010,
        timeout=10,
        targets=[active_target],
    )
    assert kill_matching.call_args.kwargs["expected_targets"] == [stale_target]
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "graceful_exit_and_stale_process_close"
    assert data["result"]["target_pids"] == [78808, 79352]
    assert data["result"]["graceful_close"]["target_pids"] == [78808]
    assert data["result"]["stale_close"]["closed_processes"] == [stale_target]


def test_editor_close_reports_failed_stale_peer_after_active_exit(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    active_target = {"pid": 78808, "project": mini_project}
    stale_target = {"pid": 79352, "project": mini_project}
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]
    stale_failure = {
        "status": "failed",
        "failed_processes": [{"pid": 79352, "project": mini_project}],
    }

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=mock_api,
    ) as api_class, patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[active_target, stale_target],
    ), patch(
        "cli_anything.unreal.commands.editor._capture_project_editor_targets",
        return_value=[active_target, stale_target],
    ), patch(
        "cli_anything.unreal.commands.editor._wait_for_project_editor_exit",
        return_value={"status": "closed", "method": "process_exit"},
    ), patch(
        "cli_anything.unreal.commands.editor._kill_matching_project_editors",
        return_value=stale_failure,
    ):
        api_class._get_pid_listening_on_port.return_value = 78808
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--force",
        ])

    assert result.exit_code == 3
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_CLOSE_FAILED"
    assert data["details"]["graceful_close"]["status"] == "closed"
    assert data["details"]["stale_close"] == stale_failure


def test_editor_close_kills_original_pid_after_project_metadata_disappears(
    mini_project,
):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    live_identity = {
        "query_ok": True,
        "found": True,
        "pid": 55364,
        "creation_time": 1001,
        "image_path": r"F:\UE\Engine\Binaries\Win64\UnrealEditor.exe",
        "identity_source": "win32_process_times",
    }
    exited_identity = {
        "query_ok": True,
        "found": False,
        "pid": 55364,
        "identity_source": "win32_process_times",
    }
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=mock_api,
    ), patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[{"pid": 55364, "project": mini_project}],
    ) as find_editors, patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_identity",
        side_effect=[live_identity, live_identity, exited_identity],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        return_value={"ok": True, "pid": 55364, "method": "taskkill"},
    ) as kill_process, patch(
        "cli_anything.unreal.commands.editor.time.time",
        side_effect=[0, 0, 0, 0, 61],
    ), patch(
        "cli_anything.unreal.commands.editor.time.sleep",
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    kill_process.assert_called_once_with(55364)
    assert find_editors.call_count == 1
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_tree_kill"
    assert data["result"]["closed_processes"] == [{
        "pid": 55364,
        "project": mini_project,
    }]


def test_editor_close_rejects_false_kill_success_for_original_pid(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    live_identity = {
        "query_ok": True,
        "found": True,
        "pid": 55364,
        "creation_time": 1001,
        "image_path": r"F:\UE\Engine\Binaries\Win64\UnrealEditor.exe",
        "identity_source": "win32_process_times",
    }
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]
    kill_result = {
        "ok": True,
        "pid": 55364,
        "method": "taskkill_already_exited",
        "returncode": 255,
        "process_exists_after_taskkill": True,
        "already_exited": True,
        "pid_state_race": True,
    }

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=mock_api,
    ), patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[{"pid": 55364, "project": mini_project}],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_identity",
        return_value=live_identity,
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_exists",
        return_value=True,
    ) as process_exists, patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        return_value=kill_result,
    ), patch(
        "cli_anything.unreal.commands.editor.time.time",
        side_effect=[0, 0, 0, 0, 61],
    ), patch(
        "cli_anything.unreal.commands.editor.time.sleep",
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 3
    process_exists.assert_called_once_with(55364)
    data = json.loads(result.output)
    assert data["status"] == "error"
    assert data["code"] == "EDITOR_CLOSE_FAILED"
    failed = data["details"]["failed_processes"][0]
    assert failed["pid"] == 55364
    failed_kill = failed["kill_result"]
    assert failed_kill["identity_still_running"] is True
    assert failed_kill["already_exited"] is False
    assert failed_kill["process_exists_after_taskkill"] is True
    assert failed_kill["taskkill_reported_missing"] is True
    assert failed_kill["method"] == "taskkill"
    assert "still running" in failed_kill["error"]


def test_editor_close_accepts_taskkill_missing_race_after_final_exit_probe(
    mini_project,
):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    live_identity = {
        "query_ok": True,
        "found": True,
        "pid": 55364,
        "creation_time": 1001,
        "image_path": r"F:\UE\Engine\Binaries\Win64\UnrealEditor.exe",
        "identity_source": "win32_process_times",
    }
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]
    kill_result = {
        "ok": True,
        "pid": 55364,
        "method": "taskkill_already_exited",
        "returncode": 255,
        "process_exists_after_taskkill": True,
        "already_exited": True,
        "pid_state_race": True,
    }

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=mock_api,
    ), patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        return_value=[{"pid": 55364, "project": mini_project}],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_identity",
        return_value=live_identity,
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_exists",
        return_value=False,
    ) as process_exists, patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        return_value=kill_result,
    ), patch(
        "cli_anything.unreal.commands.editor.time.time",
        side_effect=[0, 0, 0, 0, 61],
    ), patch(
        "cli_anything.unreal.commands.editor.time.sleep",
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    process_exists.assert_called_once_with(55364)
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "process_tree_kill"
    assert data["result"]["closed_processes"] == [{
        "pid": 55364,
        "project": mini_project,
    }]


def test_editor_close_failure_reports_kill_diagnostics(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False]

    kill_detail = {
        "ok": False,
        "pid": 49272,
        "method": "taskkill",
        "returncode": 5,
        "stdout": "",
        "stderr": "ERROR: Access is denied.",
        "access_denied": True,
        "retry_suggested": False,
        "suggestion": "Run from an elevated administrator shell or close the process manually.",
    }

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 49272, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.commands.editor.time.time", side_effect=[0, 0, 0, 61, 122]), \
         patch("cli_anything.unreal.commands.editor.time.sleep"), \
         patch("cli_anything.unreal.utils.ue_backend._kill_process_tree_result", return_value=kill_detail):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 3
    data = json.loads(result.output)
    assert data["status"] == "error"
    assert data["code"] == "EDITOR_CLOSE_FAILED"
    failed = data["details"]["failed_processes"][0]
    assert failed["pid"] == 49272
    assert failed["kill_result"]["access_denied"] is True
    assert failed["kill_result"]["retry_suggested"] is False
    assert "administrator" in data["details"]["suggestion"].lower()


def test_editor_close_returns_when_project_process_exits_but_api_stays_alive(
    mini_project,
):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.return_value = True

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=mock_api,
    ), patch(
        "cli_anything.unreal.commands.editor.time.time",
        return_value=0,
    ), patch(
        "cli_anything.unreal.commands.editor.time.sleep",
    ), patch(
        "cli_anything.unreal.utils.ue_backend.find_running_editors",
        side_effect=[
            [{"pid": 1234, "project": mini_project}],
        ],
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_exists",
        return_value=False,
    ) as process_exists, patch(
        "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
    ) as kill_process:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["result"]["status"] == "closed"
    assert data["result"]["method"] == "project_process_exit"
    assert data["result"]["target_pids"] == [1234]
    assert data["result"]["pid_evidence"] == [
        {"pid": 1234, "project_match": False, "exists": False},
    ]
    process_exists.assert_called_once_with(1234)
    kill_process.assert_not_called()


def test_editor_close_timeout_without_matching_process_returns_error(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, True]

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.commands.editor.time.time", side_effect=[0, 0, 0, 31]), \
         patch("cli_anything.unreal.commands.editor.time.sleep"), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", side_effect=[
             [{"pid": 1234, "project": mini_project}],
             [],
             [],
         ]), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=None):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 3
    data = json.loads(result.output)
    assert data["status"] == "error"
    assert data["code"] == "EDITOR_CLOSE_TIMEOUT"
    assert data["details"]["stage"] == "wait_for_project_process_exit"
    assert data["details"]["target_pids"] == [1234]
    assert data["details"]["last_process_evidence"] == {
        "matching_pids": [],
        "pids": [
            {"pid": 1234, "project_match": False, "exists": None},
        ],
    }
    assert "Editor did not close within 30s." in data["message"]
    assert result.output.count('"status": "error"') == 1
    assert result.output.count('"code": "EDITOR_CLOSE_TIMEOUT"') == 1
    assert result.output.count("Editor did not close within 30s.") == 1


def test_wait_for_project_editor_exit_handles_post_timeout_exit_race(mini_project):
    from cli_anything.unreal.commands.editor import _wait_for_project_editor_exit

    match = {"pid": 41888, "project": mini_project}
    with patch(
        "cli_anything.unreal.commands.editor.time.time",
        side_effect=[0.0, 0.0, 2.0],
    ), patch(
        "cli_anything.unreal.commands.editor.time.sleep",
    ), patch(
        "cli_anything.unreal.commands.editor._find_matching_project_editors",
        side_effect=[([match], [match]), ([], [])],
    ), patch(
        "cli_anything.unreal.commands.editor._kill_matching_project_editors",
        return_value=None,
    ):
        result = _wait_for_project_editor_exit(
            mini_project,
            30011,
            timeout=1.0,
        )

    assert result == {
        "status": "closed",
        "method": "process_exit_after_timeout_race",
    }


def test_editor_close_does_not_quit_other_project_on_same_port(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.return_value = True
    other_project = str(Path(mini_project).with_name("Other.uproject"))

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api) as api_cls, \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1111, "project": mini_project},
             {"pid": 5678, "project": other_project},
         ]):
        api_cls._get_pid_listening_on_port.return_value = 5678
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 3
    mock_api.exec_console.assert_not_called()
    data = json.loads(result.output)
    assert data["status"] == "error"
    assert data["code"] == "EDITOR_PROJECT_NOT_RUNNING"
    assert data["details"]["running_editors"] == [
        {"pid": 1111, "project": mini_project},
        {"pid": 5678, "project": other_project},
    ]


def test_editor_close_force_targets_project_when_other_project_owns_port(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    api = MagicMock()
    api.is_alive.return_value = True
    other_project = str(Path(mini_project).with_name("Other.uproject"))
    running = [
        {"pid": 1111, "project": mini_project},
        {"pid": 5678, "project": other_project},
    ]
    closed = {
        "status": "closed",
        "method": "process_tree_kill",
        "closed_processes": [{"pid": 1111, "project": mini_project}],
    }

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=api) as api_cls, \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=running), \
         patch(
             "cli_anything.unreal.commands.editor._kill_matching_project_editors",
             return_value=closed,
         ) as kill:
        api_cls._get_pid_listening_on_port.return_value = 5678
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--force",
        ])

    assert result.exit_code == 0, result.output
    api.exec_console.assert_not_called()
    assert kill.call_args.args[:2] == (mini_project, 30010)
    assert json.loads(result.output)["result"]["closed_processes"] == [
        {"pid": 1111, "project": mini_project}
    ]


def test_editor_close_refuses_dirty_packages_by_default(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    api = MagicMock()
    api.is_alive.return_value = True
    api.call_function.side_effect = [
        {"OutDirtyPackages": []},
        {"OutDirtyPackages": ["/Game/M_Unsaved"]},
    ]
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=api), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={
             "status": "closed", "method": "process_exit",
         }):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 3, result.output
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_DIRTY_PACKAGES"
    assert data["details"] == {
        "map_packages": [],
        "content_packages": ["/Game/M_Unsaved"],
        "count": 1,
        "decision": "preserve_and_leave_running",
    }
    assert "--save-dirty" in data["suggestion"]
    assert "--force" in data["suggestion"]
    api.exec_console.assert_not_called()
    assert all(
        call.args[1] != "SaveDirtyPackages"
        for call in api.call_function.call_args_list
    )


def test_editor_close_save_dirty_is_explicit(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    api = MagicMock()
    api.is_alive.side_effect = [True, False]
    api.call_function.side_effect = [
        {"OutDirtyPackages": []},
        {"OutDirtyPackages": ["/Game/M_Unsaved"]},
        {"ReturnValue": True},
    ]
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=api), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={
             "status": "closed", "method": "process_exit",
         }):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--save-dirty",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["result"]
    assert data["save_evidence"]["saved_count"] == 1
    assert data["save_evidence"]["content_packages"] == ["/Game/M_Unsaved"]
    assert data["save_evidence"]["policy"] == "save_then_close"
    api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)
    assert api.call_function.call_args_list[-1].args[1] == "SaveDirtyPackages"


def test_editor_close_rejects_conflicting_dirty_policies(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    result = CliRunner().invoke(cli, [
        "--output", "json", "--project", mini_project,
        "editor", "close", "--save-dirty", "--force",
    ])

    assert result.exit_code == 2, result.output
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_CLOSE_OPTION_CONFLICT"


def test_editor_close_dirty_transient_map_autosaves_to_game_path():
    from cli_anything.unreal.commands.editor import _save_dirty_editor_packages_if_needed

    api = MagicMock()
    api.call_function.side_effect = [
        {"OutDirtyPackages": ["/Temp/Untitled_2"]},
        {"OutDirtyPackages": []},
        {"ReturnValue": True},
        {"ReturnValue": "/Game/__UeCliAutoSave_Untitled_2.__UeCliAutoSave_Untitled_2"},
        {"OutDirtyPackages": []},
        {"OutDirtyPackages": []},
    ]

    with patch(
        "cli_anything.unreal.core.script_runner.run_python_code",
        return_value={
            "status": "ok",
            "world": "/Temp/Untitled_2.Untitled",
            "package": "/Temp/Untitled_2",
            "name": "Untitled",
        },
    ):
        evidence = _save_dirty_editor_packages_if_needed(api)

    assert evidence["saved_count"] == 1
    assert evidence["transient_map_saves"] == [{
        "map_package": "/Temp/Untitled_2",
        "world": "/Temp/Untitled_2.Untitled",
        "asset_path": "/Game/__UeCliAutoSave_Untitled_2",
    }]
    save_map_call = api.call_function.call_args_list[2]
    assert save_map_call.args[1] == "SaveMap"
    assert save_map_call.args[2] == {
        "World": "/Temp/Untitled_2.Untitled",
        "AssetPath": "/Game/__UeCliAutoSave_Untitled_2",
    }
    load_map_call = api.call_function.call_args_list[3]
    assert load_map_call.args[1] == "LoadMap"
    assert load_map_call.args[2] == {
        "Filename": "/Game/__UeCliAutoSave_Untitled_2",
    }
    assert all(call.args[1] != "SaveDirtyPackages" for call in api.call_function.call_args_list)


def test_editor_close_dirty_transient_map_reload_failure_preserves_editor():
    from cli_anything.unreal.commands import AppError
    from cli_anything.unreal.commands.editor import _save_dirty_editor_packages_if_needed

    api = MagicMock()
    api.call_function.side_effect = [
        {"OutDirtyPackages": ["/Temp/Untitled_2"]},
        {"OutDirtyPackages": []},
        {"ReturnValue": True},
        {"ReturnValue": ""},
    ]

    with patch(
        "cli_anything.unreal.core.script_runner.run_python_code",
        return_value={
            "status": "ok",
            "world": "/Temp/Untitled_2.Untitled",
            "package": "/Temp/Untitled_2",
            "name": "Untitled",
        },
    ), pytest.raises(AppError) as exc_info:
        _save_dirty_editor_packages_if_needed(api)

    assert exc_info.value.code == "EDITOR_SAVE_BEFORE_CLOSE_FAILED"
    assert exc_info.value.details["function"] == "LoadMap"
    assert exc_info.value.details["parameters"] == {
        "Filename": "/Game/__UeCliAutoSave_Untitled_2",
    }


def test_editor_close_dirty_transient_map_requires_matching_live_world():
    from cli_anything.unreal.commands import AppError
    from cli_anything.unreal.commands.editor import _save_dirty_editor_packages_if_needed

    api = MagicMock()
    api.call_function.side_effect = [
        {"OutDirtyPackages": ["/Temp/Untitled_2"]},
        {"OutDirtyPackages": []},
    ]

    with patch(
        "cli_anything.unreal.core.script_runner.run_python_code",
        return_value={
            "status": "ok",
            "world": "/Temp/Untitled_3.Untitled",
            "package": "/Temp/Untitled_3",
            "name": "Untitled",
        },
    ), pytest.raises(AppError) as exc_info:
        _save_dirty_editor_packages_if_needed(api)

    assert exc_info.value.code == "EDITOR_SAVE_BEFORE_CLOSE_FAILED"
    assert exc_info.value.details == {
        "function": "ResolveEditorWorld",
        "map_package": "/Temp/Untitled_2",
        "response": {
            "status": "ok",
            "world": "/Temp/Untitled_3.Untitled",
            "package": "/Temp/Untitled_3",
            "name": "Untitled",
        },
    }
    assert len(api.call_function.call_args_list) == 2


def test_editor_close_force_is_explicit_and_skips_dirty_query(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    api = MagicMock()
    api.is_alive.side_effect = [True, False]
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=api), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={
             "status": "closed", "method": "process_exit",
         }):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close", "--force",
        ])

    assert result.exit_code == 0, result.output
    api.call_function.assert_not_called()
    api.exec_console.assert_called_once_with("CLOSE_SLATE_MAINFRAME", timeout=1)


def test_editor_close_offline_process_requires_discard_authorization(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    api = MagicMock()
    api.is_alive.return_value = False
    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=api), \
         patch("cli_anything.unreal.commands._discover_online_editor_port", return_value=None), \
         patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 1234, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend._kill_process_tree_result") as kill:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "close",
        ])

    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["code"] == "EDITOR_DIRTY_STATE_UNKNOWN"
    kill.assert_not_called()


def test_dirty_package_query_rejects_null_return_value():
    from cli_anything.unreal.commands import AppError
    from cli_anything.unreal.commands.editor import _dirty_package_names

    with pytest.raises(AppError) as exc_info:
        _dirty_package_names({"ReturnValue": None}, "GetDirtyContentPackages")

    assert exc_info.value.code == "EDITOR_DIRTY_STATE_UNKNOWN"


def test_editor_launch_preflight_failed_includes_startup_precheck(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": False,
        "engine": {"errors": ["engine error"], "warnings": ["engine warning"]},
        "project": {"errors": ["project error"], "warnings": []},
    }), \
         patch("cli_anything.unreal.commands.editor.submit_task", return_value={"task_id": "launch-task"}):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait",
        ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"]["status"] == "submitted"
    assert "task_id" in data["result"]


# 鈹€鈹€ _build_launch_cmd unit tests 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_build_launch_cmd_without_map():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd("UnrealEditor.exe", "MyProject.uproject", None)
    assert cmd == ["UnrealEditor.exe", "MyProject.uproject", "-nosplash"]


def test_build_launch_cmd_with_map():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd("UnrealEditor.exe", "MyProject.uproject", "/Game/Maps/Main")
    assert cmd == ["UnrealEditor.exe", "MyProject.uproject", "/Game/Maps/Main", "-nosplash"]


def test_build_launch_cmd_with_unattended():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd(
        "UnrealEditor.exe",
        "MyProject.uproject",
        None,
        unattended=True,
    )
    assert cmd == ["UnrealEditor.exe", "MyProject.uproject", "-nosplash", "-unattended"]


def test_build_launch_cmd_with_skip_restore():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd(
        "UnrealEditor.exe",
        "MyProject.uproject",
        None,
        skip_restore=True,
    )
    assert cmd == [
        "UnrealEditor.exe",
        "MyProject.uproject",
        "-nosplash",
        "-CliAnythingSkipRestorePackages",
    ]


def test_build_launch_cmd_with_extra_args():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd(
        "UnrealEditor.exe",
        "MyProject.uproject",
        None,
        ["-vulkan", "-ResX=1280", "-ResY=720"],
    )
    assert cmd == [
        "UnrealEditor.exe",
        "MyProject.uproject",
        "-nosplash",
        "-vulkan",
        "-ResX=1280",
        "-ResY=720",
    ]


def test_build_launch_cmd_with_map_and_extra_args():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd(
        "UnrealEditor.exe",
        "MyProject.uproject",
        "/Game/Maps/Main",
        ["-vulkan"],
    )
    assert cmd == [
        "UnrealEditor.exe",
        "MyProject.uproject",
        "/Game/Maps/Main",
        "-nosplash",
        "-vulkan",
    ]


def test_build_launch_cmd_filters_empty_extra_args():
    from cli_anything.unreal.commands.editor import _build_launch_cmd

    cmd = _build_launch_cmd("UnrealEditor.exe", "MyProject.uproject", None, [None, "", "-server"])
    assert cmd == ["UnrealEditor.exe", "MyProject.uproject", "-nosplash", "-server"]


def test_resolve_launch_log_file_uses_case_insensitive_quoted_abslog(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _resolve_launch_log_file

    custom_log = tmp_path / "Custom Logs" / "Startup.log"
    result = _resolve_launch_log_file(
        tmp_path / "Project",
        "Project",
        [f'-ABSLOG="{custom_log}"'],
    )

    assert result == custom_log


def test_resolve_launch_log_file_defaults_to_project_log(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _resolve_launch_log_file

    result = _resolve_launch_log_file(tmp_path, "Project", ["-nosound"])

    assert result == tmp_path / "Saved" / "Logs" / "Project.log"


def test_editor_launch_rejects_nullrhi_before_submitting_task(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    with patch("cli_anything.unreal.commands.editor.submit_task") as mock_submit, \
         patch("cli_anything.unreal.commands.editor._check_already_running") as mock_running:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--extra-arg=-NullRHI",
        ])

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_LAUNCH_NULLRHI_UNSUPPORTED"
    assert data["details"] == {
        "incompatible_argument": "-NullRHI",
        "required_service": "WebRemoteControl",
        "editor_started": False,
    }
    mock_submit.assert_not_called()
    mock_running.assert_not_called()


def test_editor_launch_no_remote_allows_nullrhi_and_marks_payload(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-direct", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--no-remote", "--extra-arg=-NullRHI",
        ])

    assert result.exit_code == 0, result.output
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["no_remote"] is True
    assert captured["payload"]["port"] is None
    assert captured["payload"]["extra_args"] == ["-NullRHI"]


def test_editor_launch_extra_args_propagate_to_payload(mini_project):
    """--extra-arg values must be persisted into the task payload so the worker forwards them."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-xyz", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait",
            "--extra-arg", "-vulkan",
            "--extra-arg", "-ResX=1280",
            "--extra-arg", "-ResY=720",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"]["task_id"] == "task-xyz"
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["extra_args"] == ["-vulkan", "-ResX=1280", "-ResY=720"]
    assert captured["payload"]["unattended"] is False
    assert captured["payload"]["no_remote"] is False


def test_editor_launch_unattended_propagates_to_payload(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-unattended", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--unattended",
        ])

    assert result.exit_code == 0, result.output
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["unattended"] is True


def test_editor_launch_skip_restore_propagates_to_payload(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-skip-restore", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--skip-restore",
        ])

    assert result.exit_code == 0, result.output
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["skip_restore"] is True
    assert captured["payload"]["unattended"] is False


def test_editor_launch_skip_restore_rejects_no_remote(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    with patch("cli_anything.unreal.commands.editor.submit_task") as mock_submit, \
         patch("cli_anything.unreal.commands.editor._check_already_running") as mock_running:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--skip-restore", "--no-remote",
        ])

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_SKIP_RESTORE_REQUIRES_BRIDGE"
    assert data["details"]["editor_started"] is False
    mock_submit.assert_not_called()
    mock_running.assert_not_called()


def test_editor_launch_reports_structured_worker_spawn_failure(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import TaskWorkerSpawnError
    from cli_anything.unreal.unreal_cli import cli

    error = TaskWorkerSpawnError(
        "task-failed",
        [
            {
                "mode": "with_breakaway",
                "creationflags": 16777736,
                "breakaway_requested": True,
                "error": {
                    "type": "PermissionError",
                    "errno": 13,
                    "winerror": 5,
                    "message": "[WinError 5] Access is denied",
                },
            },
            {
                "mode": "without_breakaway",
                "creationflags": 520,
                "breakaway_requested": False,
                "error": {
                    "type": "PermissionError",
                    "errno": 13,
                    "winerror": 5,
                    "message": "[WinError 5] Access is denied",
                },
            },
        ],
    )
    with patch(
        "cli_anything.unreal.commands.editor.submit_task",
        side_effect=error,
    ), patch(
        "cli_anything.unreal.commands.editor._check_already_running",
        return_value=None,
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--unattended",
        ])

    assert result.exit_code == 3
    data = json.loads(result.output)
    assert data["status"] == "error"
    assert data["code"] == "TASK_WORKER_SPAWN_FAILED"
    assert data["details"] == error.details
    assert data["details"]["fallback_attempted"] is True


def test_editor_launch_no_unattended_propagates_to_payload(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}

    def fake_submit_task(command, payload):
        captured["payload"] = payload
        return {"task_id": "task-interactive", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--no-unattended",
        ])

    assert result.exit_code == 0, result.output
    assert captured["payload"]["unattended"] is False


def test_editor_launch_normalizes_absolute_project_umap_to_package_path(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    map_file = Path(mini_project).parent / "Content" / "Maps" / "Oregon_Main.umap"
    map_file.parent.mkdir(parents=True)
    map_file.touch()
    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-map", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--map", str(map_file),
        ])

    assert result.exit_code == 0, result.output
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["map_path"] == "/Game/Maps/Oregon_Main"


def test_editor_launch_rejects_unrooted_map_name_before_submitting_task(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    with patch("cli_anything.unreal.commands.editor.submit_task") as mock_submit, \
         patch("cli_anything.unreal.commands.editor._check_already_running") as mock_running:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--map", "Oregon_Main",
        ])

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["code"] == "INVALID_LEVEL_PATH"
    assert "/Game/" in data["suggestion"]
    assert data["details"]["path"] == "Oregon_Main"
    mock_submit.assert_not_called()
    mock_running.assert_not_called()


def test_editor_launch_rejects_explicit_empty_map_before_submitting_task(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    with patch("cli_anything.unreal.commands.editor.submit_task") as mock_submit:
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait", "--map", "",
        ])

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["code"] == "INVALID_LEVEL_PATH"
    assert data["details"]["path"] == ""
    mock_submit.assert_not_called()


def test_editor_launch_accepts_command_level_project(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    config_dir = Path(mini_project).parent / "Config"
    config_dir.mkdir()
    (config_dir / "DefaultRemoteControl.ini").write_text(
        "[/Script/RemoteControlCommon.RemoteControlSettings]\n"
        "RemoteControlHttpServerPort=30011\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-xyz", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = runner.invoke(cli, [
            "--output", "json",
            "editor", "launch", "--project", mini_project, "--no-wait",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["project_path"] == mini_project
    assert captured["payload"]["port"] == 30011


def test_editor_launch_explicit_port_overrides_command_level_project_config(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    config_dir = Path(mini_project).parent / "Config"
    config_dir.mkdir()
    (config_dir / "DefaultRemoteControl.ini").write_text(
        "[/Script/RemoteControlCommon.RemoteControlSettings]\n"
        "RemoteControlHttpServerPort=30011\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-xyz", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--port", "30012",
            "editor", "launch", "--project", mini_project, "--no-wait",
        ])

    assert result.exit_code == 0, result.output
    assert captured["command"] == "editor.launch"
    assert captured["payload"]["project_path"] == mini_project
    assert captured["payload"]["port"] == 30012


def test_editor_launch_help_lists_command_level_project():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    result = CliRunner().invoke(cli, ["editor", "launch", "--help"])

    assert result.exit_code == 0, result.output
    assert "--project" in result.output
    assert "/Game/" in result.output
    assert "DefaultRemoteControl.ini" in result.output
    assert "--unattended / --no-unattended" in result.output
    assert "interactive editor" in result.output
    assert "--skip-restore" in result.output


def test_editor_enable_remote_help_discloses_project_file_changes():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    result = CliRunner().invoke(cli, ["editor", "enable-remote", "--help"])

    assert result.exit_code == 0, result.output
    assert ".uproject" in result.output
    assert "DefaultRemoteControl.ini" in result.output


def test_editor_launch_no_extra_args_yields_empty_list(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    captured = {}

    def fake_submit_task(command, payload):
        captured["payload"] = payload
        return {"task_id": "task-xyz", "status": "submitted"}

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait",
        ])

    assert result.exit_code == 0, result.output
    assert captured["payload"]["extra_args"] == []
    assert captured["payload"]["unattended"] is False


# 鈹€鈹€ plugin-upgrade relaunch uses _build_launch_cmd 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_editor_launch_preserves_matching_project_process_when_api_owner_differs(mini_project):
    """A live API on another PID must not cause the target editor to be killed."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-xyz", "status": "submitted"}

    other_project = str(Path(mini_project).with_name("Other.uproject"))
    running = [
        {"pid": 60504, "project": mini_project},
        {"pid": 99999, "project": other_project},
    ]

    with patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=running), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=None), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", return_value=True), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port", return_value=99999), \
         patch("cli_anything.unreal.utils.ue_backend.detect_ue_dialogs", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend._kill_process_tree", return_value=True) as kill_proc, \
         patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait",
        ])

    assert result.exit_code == 3, result.output
    kill_proc.assert_not_called()
    assert captured == {}
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_ALREADY_RUNNING_OFFLINE"
    assert data["details"]["decision"] == "preserve_existing_editor"


def test_editor_launch_ignores_matching_process_that_exited_after_discovery(mini_project):
    """A stale process snapshot must not block the next editor launch."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}

    def fake_submit_task(command, payload):
        captured["command"] = command
        captured["payload"] = payload
        return {"task_id": "task-xyz", "status": "submitted"}

    with patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 61980, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=False) as process_exists, \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", return_value=False), \
         patch("cli_anything.unreal.utils.ue_backend.detect_ue_dialogs", return_value=[]), \
         patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait",
        ])

    assert result.exit_code == 0, result.output
    process_exists.assert_called_once_with(61980)
    assert captured["command"] == "editor.launch"


def test_editor_launch_does_not_kill_process_owned_by_active_launch_task(mini_project, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import create_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    task = create_task("editor.launch", {"project_path": mini_project, "port": 30011})
    task["status"] = "running"
    task["pid"] = 16044
    save_task(task)

    runner = CliRunner()
    with patch("cli_anything.unreal.utils.ue_backend.find_running_editors", return_value=[
             {"pid": 16044, "project": mini_project},
         ]), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=True), \
         patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", return_value=False), \
         patch("cli_anything.unreal.utils.ue_backend.detect_ue_dialogs", return_value=[]), \
         patch("cli_anything.unreal.utils.ue_backend._kill_process_tree") as kill_proc, \
         patch("cli_anything.unreal.commands.editor.submit_task") as submit_task:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--no-wait",
        ])

    assert result.exit_code == 3
    data = json.loads(result.output)
    assert data["status"] == "error"
    assert data["code"] == "EDITOR_STARTING"
    assert data["details"]["task_id"] == task["task_id"]
    kill_proc.assert_not_called()
    submit_task.assert_not_called()


def test_editor_launch_without_timeout_uses_bounded_foreground_wait(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    captured = {}

    def fake_submit_task(command, payload):
        captured["payload"] = payload
        return {"task_id": "launch-task", "command": command, "payload": payload}

    def fake_wait_for_task(task_id, timeout):
        captured["wait_timeout"] = timeout
        return {
            "task_id": task_id,
            "command": "editor.launch",
            "status": "completed",
            "result": {"status": "online", "port": 30010},
        }

    with patch("cli_anything.unreal.commands.editor.submit_task", side_effect=fake_submit_task), \
         patch("cli_anything.unreal.commands.editor.wait_for_task", side_effect=fake_wait_for_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch",
        ])

    assert result.exit_code == 0, result.output
    assert captured["payload"]["timeout"] is not None
    assert captured["wait_timeout"] is not None
    assert captured["wait_timeout"] == 30


def test_editor_launch_with_long_timeout_returns_pollable_progress(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    captured = {}
    running_task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "running",
        "suggested_poll_interval_seconds": 5,
    }

    def fake_submit_task(command, payload):
        captured["payload"] = payload
        return {"task_id": "launch-task", "command": command, "payload": payload}

    def fake_wait_for_task(task_id, timeout):
        captured["wait_timeout"] = timeout
        return None

    with patch(
        "cli_anything.unreal.commands.editor.submit_task",
        side_effect=fake_submit_task,
    ), patch(
        "cli_anything.unreal.commands.editor.wait_for_task",
        side_effect=fake_wait_for_task,
    ), patch(
        "cli_anything.unreal.commands.editor.load_task",
        return_value=running_task,
    ), patch(
        "cli_anything.unreal.commands.editor._check_already_running",
        return_value=None,
    ), patch(
        "cli_anything.unreal.commands.editor._scan_editor_status_instances",
        return_value=[],
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--timeout", "600",
        ])

    assert result.exit_code == 0, result.output
    assert captured["payload"]["timeout"] == 600
    assert captured["wait_timeout"] == 30
    data = json.loads(result.output)
    assert data["result"]["status"] == "launching"
    assert data["result"]["task_id"] == "launch-task"
    assert data["result"]["foreground_wait_timeout_seconds"] == 30
    assert data["result"]["next_command"] == (
        f'ue-cli --project "{mini_project}" editor status launch-task'
    )


def test_editor_launch_returns_progress_when_final_task_read_is_blocked(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    submitted_task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "submitted",
        "worker_pid": 41652,
        "suggested_poll_interval_seconds": 5,
    }

    with patch(
        "cli_anything.unreal.commands.editor.submit_task",
        return_value=submitted_task,
    ), patch(
        "cli_anything.unreal.commands.editor.wait_for_task",
        return_value=None,
    ), patch(
        "cli_anything.unreal.commands.editor.load_task",
        side_effect=PermissionError(13, "Permission denied"),
    ), patch(
        "cli_anything.unreal.commands.editor._check_already_running",
        return_value=None,
    ), patch(
        "cli_anything.unreal.commands.editor._scan_editor_status_instances",
        return_value=[],
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--timeout", "1",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["result"]["status"] == "launching"
    assert data["result"]["task_id"] == "launch-task"
    assert data["result"]["worker_pid"] == 41652
    assert data["result"]["next_command"] == (
        f'ue-cli --project "{mini_project}" editor status launch-task'
    )


def test_editor_launch_reload_of_cancelled_task_is_nonzero(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    cancelled = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "cancelled",
        "error": {"code": "TASK_CANCELLED", "message": "Cancelled."},
    }
    with patch(
        "cli_anything.unreal.commands.editor.submit_task",
        return_value={"task_id": "launch-task", "command": "editor.launch"},
    ), patch(
        "cli_anything.unreal.commands.editor.wait_for_task",
        return_value=None,
    ), patch(
        "cli_anything.unreal.commands.editor.load_task",
        return_value=cancelled,
    ), patch(
        "cli_anything.unreal.commands.editor._check_already_running",
        return_value=None,
    ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--timeout", "1",
        ])

    assert result.exit_code == 4
    assert json.loads(result.output)["code"] == "TASK_CANCELLED"


def test_editor_cancel_failure_is_nonzero():
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    failed = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "running",
        "error": {"code": "TASK_CANCEL_FAILED", "message": "Still running."},
    }
    with patch("cli_anything.unreal.commands.editor.cancel_task", return_value=failed):
        result = CliRunner().invoke(cli, [
            "--output", "json", "editor", "cancel", "launch-task",
        ])

    assert result.exit_code == 4
    assert json.loads(result.output)["code"] == "TASK_CANCEL_FAILED"


def test_editor_launch_returns_progress_without_post_wait_editor_scan(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    running_task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "running",
        "pid": 68348,
        "suggested_poll_interval_seconds": 5,
    }

    with patch("cli_anything.unreal.commands.editor.submit_task", return_value={
            "task_id": "launch-task",
            "command": "editor.launch",
        }), \
         patch("cli_anything.unreal.commands.editor.wait_for_task", return_value=None), \
         patch("cli_anything.unreal.commands.editor.load_task", return_value=running_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None), \
         patch("cli_anything.unreal.commands.editor._scan_editor_status_instances") as scan:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--timeout", "120",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "success"
    assert data["result"]["status"] == "launching"
    assert data["result"]["pid"] == 68348
    assert data["result"]["task_id"] == "launch-task"
    assert data["result"]["foreground_wait_timeout_seconds"] == 30
    assert data["result"]["next_command"] == (
        f'ue-cli --project "{mini_project}" editor status launch-task'
    )
    scan.assert_not_called()


def test_editor_launch_does_not_recover_map_launch_without_level_verification(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    requested_map = "/Game/Maps/Oregon_Main"
    timed_out_task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "timeout",
        "payload": {
            "project_path": mini_project,
            "map_path": requested_map,
            "port": 30011,
        },
        "pid": 68348,
        "suggested_poll_interval_seconds": 5,
    }

    with patch("cli_anything.unreal.commands.editor.submit_task", return_value={
             "task_id": "launch-task",
             "command": "editor.launch",
         }), \
         patch("cli_anything.unreal.commands.editor.wait_for_task", return_value=timed_out_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None), \
         patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
             "status": "online",
             "pid": 68348,
             "port": 30011,
             "project_path": mini_project,
             "bridge_version": "1.17",
             "bundled_version": "1.17",
             "plugin_match": True,
         }]), \
         patch(
             "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
             return_value=68348,
         ), \
         patch("cli_anything.unreal.core.scene._verify_current_level", return_value={
             "status": "failed",
             "expected_package": requested_map,
             "active_world": {"package": "/Game/Maps/TestMap"},
         }):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--map", requested_map, "--timeout", "120",
        ])

    assert result.exit_code == 4, result.output
    data = json.loads(result.output)
    assert data["code"] == "EDITOR_LAUNCH_TIMEOUT"
    assert data["details"]["status"] == "timeout"


def test_editor_launch_recovers_map_launch_after_exact_level_verification(mini_project):
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    requested_map = "/Game/Maps/Oregon_Main"
    running_task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "timeout",
        "payload": {
            "project_path": mini_project,
            "map_path": requested_map,
            "port": 30011,
        },
        "pid": 68348,
    }
    verification = {
        "status": "ok",
        "expected_package": requested_map,
        "active_world": {
            "package": requested_map,
            "world": f"{requested_map}.Oregon_Main",
        },
    }

    with patch("cli_anything.unreal.commands.editor.submit_task", return_value={
            "task_id": "launch-task",
            "command": "editor.launch",
        }), \
         patch("cli_anything.unreal.commands.editor.wait_for_task", return_value=running_task), \
         patch("cli_anything.unreal.commands.editor._check_already_running", return_value=None), \
         patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
             "status": "online",
             "pid": 68348,
             "port": 30011,
             "project_path": mini_project,
             "bridge_version": "1.18",
             "bundled_version": "1.18",
             "plugin_match": True,
         }]), \
         patch(
             "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
             return_value=68348,
         ), \
         patch("cli_anything.unreal.core.scene._verify_current_level", return_value=verification):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "launch", "--map", requested_map, "--timeout", "120",
        ])

    assert result.exit_code == 0, result.output
    recovered = json.loads(result.output)["result"]
    assert recovered["status"] == "online"
    assert recovered["pid"] == 68348
    assert recovered["launch_task_status"] == "timeout"
    assert recovered["requested_map"] == requested_map
    assert recovered["map_verification"] == verification


def test_editor_status_reconciles_timed_out_map_launch_when_exact_editor_is_online(
    mini_project,
    tmp_path,
    monkeypatch,
):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import create_task, load_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    requested_map = "/Game/Maps/Oregon_Main"
    task = create_task("editor.launch", {
        "project_path": mini_project,
        "map_path": requested_map,
        "port": 30011,
    })
    task.update({
        "status": "timeout",
        "pid": 68348,
        "editor_process_identity": {
            "pid": 68348,
            "creation_time": 123456,
            "image_path": "F:/MockEngine/UnrealEditor.exe",
        },
        "error": {"code": "TASK_TIMEOUT", "message": "startup timed out"},
        "result": {
            "status": "timeout",
            "failure_kind": "api_route_unhealthy",
            "api_route_healthy": False,
            "next_command": f'ue-cli --project "{mini_project}" editor status {task["task_id"]}',
            "error": "Editor API did not respond within 120s.",
        },
    })
    save_task(task)
    verification = {
        "status": "ok",
        "expected_package": requested_map,
        "active_world": {"package": requested_map},
    }

    with patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
            "status": "online",
            "pid": 68348,
            "port": 30011,
            "project_path": mini_project,
            "bridge_version": "1.18",
            "bundled_version": "1.18",
            "plugin_match": True,
        }]), \
             patch(
                 "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
                 return_value=68348,
             ), \
             patch(
                 "cli_anything.unreal.utils.ue_backend._windows_process_identity",
                 return_value={
                     "query_ok": True,
                     "found": True,
                     "pid": 68348,
                     "creation_time": 123456,
                     "image_path": "F:/MockEngine/UnrealEditor.exe",
                 },
             ), \
             patch("cli_anything.unreal.core.scene._verify_current_level", return_value=verification):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status", task["task_id"],
        ])

    assert result.exit_code == 0, result.output
    progress = json.loads(result.output)["result"]
    assert progress["status"] == "completed"
    assert progress["result"]["status"] == "online"
    assert progress["result"]["launch_task_status"] == "timeout"
    assert progress["result"]["recovered_from"] == "launch_task_status"
    assert progress["result"]["process_identity_verified"] is True
    assert "failure_kind" not in progress["result"]
    assert "api_route_healthy" not in progress["result"]
    assert "next_command" not in progress["result"]
    assert "error" not in progress["result"]
    persisted = load_task(task["task_id"])
    assert persisted["status"] == "completed"
    assert "error" not in persisted


def test_editor_status_reconciles_running_wait_when_exact_editor_is_online(
    mini_project,
    tmp_path,
    monkeypatch,
):
    from click.testing import CliRunner
    from cli_anything.unreal.core.tasks import create_task, load_task, save_task
    from cli_anything.unreal.unreal_cli import cli

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    task = create_task("editor.launch", {
        "project_path": mini_project,
        "port": 30011,
    })
    task.update({
        "status": "running",
        "phase": "waiting_remote_control",
        "pid": 68348,
        "resolved_port": 30011,
        "editor_process_identity": {
            "pid": 68348,
            "creation_time": 123456,
            "image_path": "F:/MockEngine/UnrealEditor.exe",
        },
        "result": {
            "status": "waiting_for_remote_control",
            "startup_phase": "waiting_for_remote_control",
            "api_reachable": True,
        },
    })
    save_task(task)

    with patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
            "status": "online",
            "pid": 68348,
            "port": 30011,
            "project_path": mini_project,
            "bridge_version": "1.37",
            "bundled_version": "1.37",
            "plugin_match": True,
        }]), \
             patch(
                 "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
                 return_value=68348,
             ), \
             patch(
                 "cli_anything.unreal.utils.ue_backend._windows_process_identity",
                 return_value={
                     "query_ok": True,
                     "found": True,
                     "pid": 68348,
                     "creation_time": 123456,
                     "image_path": "F:/MockEngine/UnrealEditor.exe",
                 },
             ):
        result = CliRunner().invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "status", task["task_id"],
        ])

    assert result.exit_code == 0, result.output
    progress = json.loads(result.output)["result"]
    assert progress["status"] == "completed"
    assert progress["phase"] == "online"
    assert progress["result"]["startup_phase"] == "ready"
    assert progress["result"]["launch_task_status"] == "running"
    assert progress["result"]["recovered_from"] == "launch_task_status"
    assert progress["result"]["process_identity_verified"] is True
    persisted = load_task(task["task_id"])
    assert persisted["status"] == "completed"
    assert persisted["phase"] == "online"


def test_editor_launch_recovery_rejects_online_editor_with_different_pid(mini_project):
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.commands.editor import _recover_online_launch_result

    state = AppState()
    state.session.load_project(mini_project)
    task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "timeout",
        "pid": 68348,
        "payload": {"project_path": mini_project, "port": 30011},
    }

    with patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
        "status": "online",
        "pid": 99999,
        "port": 30011,
        "project_path": mini_project,
    }]):
        result = _recover_online_launch_result(state, task["task_id"], task)

    assert result is None


def test_editor_launch_recovery_rejects_port_owned_by_different_pid(mini_project):
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.commands.editor import _recover_online_launch_result

    state = AppState()
    state.session.load_project(mini_project)
    task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "timeout",
        "pid": 68348,
        "payload": {"project_path": mini_project, "port": 30011},
    }

    with patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
            "status": "online",
            "pid": 68348,
            "port": 30011,
            "project_path": mini_project,
        }]), \
         patch(
             "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
             return_value=99999,
         ):
        result = _recover_online_launch_result(state, task["task_id"], task)

    assert result is None


def test_editor_launch_recovery_rejects_reused_pid_identity(mini_project):
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.commands.editor import _recover_online_launch_result

    state = AppState()
    state.session.load_project(mini_project)
    task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "timeout",
        "task_state_version": 2,
        "pid": 68348,
        "resolved_port": 30011,
        "payload": {"project_path": mini_project, "port": 30010},
        "editor_process_identity": {
            "pid": 68348,
            "creation_time": 111,
            "image_path": "F:/MockEngine/UnrealEditor.exe",
        },
    }

    with patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
        "status": "online",
        "pid": 68348,
        "port": 30011,
        "project_path": mini_project,
    }]), patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
        return_value=68348,
    ), patch(
        "cli_anything.unreal.utils.ue_backend._windows_process_identity",
        return_value={
            "query_ok": True,
            "found": True,
            "pid": 68348,
            "creation_time": 222,
            "image_path": "F:/MockEngine/UnrealEditor.exe",
        },
    ):
        result = _recover_online_launch_result(state, task["task_id"], task)

    assert result is None


def test_editor_launch_recovery_requires_task_pid(mini_project):
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.commands.editor import _recover_online_launch_result

    state = AppState()
    state.session.load_project(mini_project)
    task = {
        "task_id": "launch-task",
        "command": "editor.launch",
        "status": "submitted",
        "payload": {"project_path": mini_project, "port": 30011},
    }

    with patch("cli_anything.unreal.commands.editor._scan_editor_status_instances", return_value=[{
        "status": "online",
        "pid": 68348,
        "port": 30011,
        "project_path": mini_project,
    }]):
        result = _recover_online_launch_result(state, task["task_id"], task)

    assert result is None


def test_plugin_upgrade_relaunch_uses_interactive_launch_default(mini_project):
    """plugin-upgrade relaunches windowed without an unattended override."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return mock_proc

    mock_api = MagicMock()
    # 1st call: editor_was_running check -> True
    # 2nd call: post-close wait -> False
    # 3rd call: wait-for-api loop -> True (editor back online)
    mock_api.is_alive.side_effect = [True, False, True]

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.commands.editor._find_matching_project_editors", return_value=([{"pid": 1234, "project": mini_project}], [{"pid": 1234, "project": mini_project}])), \
         patch("cli_anything.unreal.commands.editor.require_editor", return_value=mock_api), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="2.0"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", side_effect=["1.0", "2.0"]), \
         patch("cli_anything.unreal.commands.editor._close_editor_for_project", return_value={"status": "closed"}), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={"status": "closed"}), \
         patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed", return_value={
             "deployed": True, "action": "updated", "version": "2.0", "plugin_dir": "/tmp/plugin"
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={"status": "ok"}), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/Engine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.commands.editor.sp.Popen", side_effect=fake_popen), \
         patch("cli_anything.unreal.commands.editor.time.sleep"):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "plugin-upgrade",
        ])

    assert result.exit_code == 0, result.output
    # The relaunch uses the normal windowed launch command.
    relaunch_calls = [cmd for cmd in popen_calls if cmd and str(cmd[0]).endswith("UnrealEditor.exe")]
    assert len(relaunch_calls) == 1
    relaunch_cmd = relaunch_calls[0]
    assert "-nosplash" in relaunch_cmd
    assert "-unattended" not in relaunch_cmd


def test_plugin_upgrade_uses_editor_close_helper(mini_project):
    """plugin-upgrade should reuse editor close logic instead of console 'exit'."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False, True]

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.commands.editor._find_matching_project_editors", return_value=([{"pid": 1234, "project": mini_project}], [{"pid": 1234, "project": mini_project}])), \
         patch("cli_anything.unreal.commands.editor.require_editor", return_value=mock_api), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="2.0"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", side_effect=["1.0", "2.0"]), \
         patch("cli_anything.unreal.commands.editor._close_editor_for_project", return_value={"status": "closed"}) as mock_close, \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={"status": "closed"}), \
         patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed", return_value={
             "deployed": True, "action": "updated", "version": "2.0", "plugin_dir": "/tmp/plugin"
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={"status": "ok"}), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/Engine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.commands.editor.sp.Popen", return_value=MagicMock(pid=9999)), \
         patch("cli_anything.unreal.commands.editor.time.sleep"):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "plugin-upgrade",
        ])

    assert result.exit_code == 0
    mock_close.assert_called_once()
    mock_api.exec_console.assert_not_called()


# 鈹€鈹€ auto-compile on plugin load failure / skip when OK 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_plugin_upgrade_kills_residual_project_editor_before_compile(mini_project):
    """plugin-upgrade must not compile while same-project editor process still locks DLLs."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.side_effect = [True, False, True]

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.commands.editor._find_matching_project_editors", return_value=([{"pid": 1234, "project": mini_project}], [{"pid": 1234, "project": mini_project}])), \
         patch("cli_anything.unreal.commands.editor.require_editor", return_value=mock_api), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="2.0"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version", side_effect=["1.0", "2.0"]), \
         patch("cli_anything.unreal.commands.editor._close_editor_for_project", return_value={"status": "closed"}), \
         patch("cli_anything.unreal.commands.editor._wait_for_project_editor_exit", return_value={
             "status": "closed",
             "method": "process_tree_kill",
             "closed_processes": [{"pid": 1234, "project": mini_project}],
         }) as mock_drain, \
         patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed", return_value={
             "deployed": True, "action": "updated", "version": "2.0", "plugin_dir": "/tmp/plugin"
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={"status": "ok"}) as mock_compile, \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/Engine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.commands.editor.sp.Popen", return_value=MagicMock(pid=9999)), \
         patch("cli_anything.unreal.commands.editor.time.sleep"):
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "plugin-upgrade",
        ])

    assert result.exit_code == 0, result.output
    mock_drain.assert_called_once_with(mini_project, 30010, timeout=60)
    mock_compile.assert_called_once()


def test_plugin_upgrade_reports_locked_dll_from_compile_log(mini_project, tmp_path):
    """LNK1104 locked DLL failures should identify the locked file and recovery."""
    from click.testing import CliRunner
    from cli_anything.unreal.unreal_cli import cli

    log_file = tmp_path / "cli_compile.log"
    locked = r"F:\RXGame\Plugins\Tencent\UnLua\Binaries\Win64\UnrealEditor-UnLua.dll"
    log_file.write_text(f"LINK : fatal error LNK1104: cannot open file '{locked}'\n", encoding="utf-8")

    runner = CliRunner()
    mock_api = MagicMock()
    mock_api.is_alive.return_value = False

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", return_value=mock_api), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="2.0"), \
         patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed", return_value={
             "deployed": True, "action": "updated", "version": "2.0", "plugin_dir": "/tmp/plugin"
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={
             "status": "error",
             "error": "Compile failed (exit 6). See log_file for details.",
             "log_file": str(log_file),
             "returncode": 6,
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.rollback_plugin_deployment", return_value={
             "status": "restored",
             "restored": True,
             "previous_version": "1.20",
             "failed_version": "2.0",
         }) as mock_rollback:
        result = runner.invoke(cli, [
            "--output", "json", "--project", mini_project,
            "editor", "plugin-upgrade",
        ])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["code"] == "BRIDGE_MODULE_COMPILE_FAILED"
    assert data["details"]["locked_file"] == locked
    assert data["details"]["lock_error"] == "LNK1104"
    assert data["details"]["bridge_rollback"]["status"] == "restored"
    assert "UnrealEditor" in data["suggestion"]
    mock_rollback.assert_called_once()


def test_run_editor_launch_task_auto_compiles_on_plugin_load_failure(tmp_path):
    """Bridge load failure terminates its editor before compile and retry."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "unattended": True,
        "skip_restore": True,
    })

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date", "version": "1.13"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=True), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={"status": "ok"}) as mock_compile, \
         patch("cli_anything.unreal.core.tasks._capture_windows_process_identity", return_value=None), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", side_effect=[
             {"status": "error_dialog", "error": "Plugin 'CliAnythingBridge' failed to load because module 'CliAnythingBridge' could not be found."},
             {"status": "online"},
         ]):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_compile.assert_called_once()
    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once_with(timeout=5)
    assert mock_popen.call_count == 2
    assert all("-unattended" in call.args[0] for call in mock_popen.call_args_list)
    assert all("-CliAnythingSkipRestorePackages" in call.args[0] for call in mock_popen.call_args_list)
    assert result["status"] == "completed"
    assert result["result"].get("recompiled") is True
    assert result["result"]["startup_failure"]["plugin"] == "CliAnythingBridge"
    assert result["result"]["bridge_rebuild_editor_cleanup"]["ok"] is True


def test_run_editor_launch_task_reports_unrelated_plugin_load_failure(tmp_path):
    """An unrelated plugin failure must not trigger a Bridge rebuild."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })
    startup_error = (
        "Plugin 'MFMeshDecal' failed to load because module "
        "'MFMeshDecalEditor' could not be loaded."
    )

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date", "version": "1.34"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={
             "status": "error", "code": "BRIDGE_OUTPUT_CLEAN_FAILED"
         }) as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={
             "status": "error_dialog", "error": startup_error
         }):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "EDITOR_PLUGIN_LOAD_FAILED"
    assert result["error"]["details"]["plugin"] == "MFMeshDecal"
    assert result["error"]["details"]["module"] == "MFMeshDecalEditor"
    assert result["error"]["details"]["diagnostic"] == startup_error
    assert result["error"]["details"]["bridge_rebuild_attempted"] is False
    mock_compile.assert_not_called()
    mock_popen.assert_called_once()


def test_run_editor_launch_task_stops_if_bridge_editor_cannot_be_terminated(tmp_path):
    """Bridge compile must not begin while the spawned editor may hold its DLL."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock(pid=4242)
    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })
    startup_error = (
        "Plugin 'CliAnythingBridge' failed to load because module "
        "'CliAnythingBridge' could not be found."
    )
    cleanup = {"ok": False, "pid": 4242, "still_running": True}

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True, "reason": "ok", "message": "Bridge plugin binary is ready."
         }), \
         patch("cli_anything.unreal.core.tasks._terminate_just_spawned_process", return_value=cleanup), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={
             "status": "error_dialog", "error": startup_error
         }):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "BRIDGE_REBUILD_EDITOR_TERMINATION_FAILED"
    assert result["error"]["details"]["startup_failure"]["diagnostic"] == startup_error
    assert result["error"]["details"]["editor_cleanup"] == cleanup
    mock_compile.assert_not_called()


def test_run_editor_launch_task_precompiles_when_bridge_binary_missing(tmp_path):
    """_run_editor_launch_task should compile the bridge before launching when its DLL is absent."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", side_effect=[
        {
            "ready": False,
            "engine": {"ready": True, "errors": [], "warnings": []},
            "project": {"ready": True, "errors": [], "warnings": []},
            "remote_control": {
                "configured": False,
                "plugin_loadable": {"available": True},
            },
        },
        {
            "ready": True,
            "engine": {"ready": True, "errors": [], "warnings": []},
            "project": {"ready": True, "errors": [], "warnings": []},
            "remote_control": {"configured": True},
        },
    ]), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", side_effect=[
             {
                 "ready": False,
                 "reason": "missing_binary",
                 "message": "Bridge plugin binary is missing.",
             },
             {
                 "ready": True,
                 "reason": "ok",
                 "message": "Bridge plugin binary is ready.",
             },
         ], create=True), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={"status": "ok"}) as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={"status": "online"}):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_compile.assert_called_once()
    assert result["status"] == "completed"
    assert result["result"].get("precompiled_bridge") is True
    assert result["result"].get("compile_reason") == "Bridge plugin binary is missing."


def test_run_editor_launch_task_restores_previous_bridge_on_compile_failure(tmp_path):
    """Auto-upgrade failure reports the build error and restored bridge state."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })
    deploy = {
        "deployed": True,
        "action": "updated_1.20_to_1.23",
        "version": "1.23",
        "upgrade_transaction": {"transaction_id": "synthetic"},
    }
    rollback = {
        "status": "restored",
        "restored": True,
        "previous_version": "1.20",
        "failed_version": "1.23",
    }

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value=deploy), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": False,
             "reason": "missing_binary",
             "message": "Bridge plugin binary is missing.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={
             "status": "error",
             "code": "BUILD_LINK_FAILED",
             "error": "Bridge link failed.",
             "returncode": 6,
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.rollback_plugin_deployment", return_value=rollback) as mock_rollback, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen") as mock_popen:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_rollback.assert_called_once_with(deploy)
    mock_popen.assert_not_called()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "BUILD_LINK_FAILED"
    assert result["error"]["details"]["bridge_rollback"] == rollback
    assert result["result"]["bridge_rollback"] == rollback


def test_run_editor_launch_task_stops_when_bridge_upgrade_is_locked(tmp_path):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })
    deploy = {
        "deployed": True,
        "action": "update_pending_locked",
        "version": "1.20",
        "bundled_version": "1.23",
        "warning": "Bridge plugin is in use and could not be updated.",
    }

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value=deploy), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen") as mock_popen:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_compile.assert_not_called()
    mock_popen.assert_not_called()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "BRIDGE_DEPLOY_LOCKED"
    assert result["result"]["bridge_deploy"] == deploy


def test_run_editor_launch_task_skips_compile_when_plugin_loads_ok(tmp_path):
    """_run_editor_launch_task skips compilation when plugin loads successfully."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", side_effect=[
        {
            "ready": False,
            "engine": {"ready": True, "errors": [], "warnings": []},
            "project": {"ready": True, "errors": [], "warnings": []},
            "remote_control": {
                "configured": False,
                "plugin_loadable": {"available": True},
            },
        },
        {
            "ready": True,
            "engine": {"ready": True, "errors": [], "warnings": []},
            "project": {"ready": True, "errors": [], "warnings": []},
            "remote_control": {"configured": True},
        },
    ]) as mock_preflight, \
         patch("cli_anything.unreal.utils.ue_backend.ensure_remote_control_config", return_value={
             "status": "ok",
             "changes": [],
         }) as mock_prepare_remote, \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={"status": "online"}):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_prepare_remote.assert_called_once()
    assert mock_preflight.call_count == 2
    mock_compile.assert_not_called()
    assert result["status"] == "completed"


def test_run_editor_launch_task_recovers_requested_map_with_open_level(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    requested_map = "/Game/Maps/Oregon_Main"

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "map_path": requested_map,
    })

    verification = {
        "status": "failed",
        "error": "Active editor world did not match requested level.",
        "expected_package": requested_map,
        "active_world": {"package": "/Game/Maps/TestMap_3C"},
    }
    recovery = {
        "status": "ok",
        "success": True,
        "path": requested_map,
        "active_world": {"package": requested_map},
    }
    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={"status": "online", "port": 30010}), \
         patch("cli_anything.unreal.core.scene._verify_current_level", return_value=verification) as mock_verify, \
         patch("cli_anything.unreal.core.scene.open_level", return_value=recovery) as mock_open_level:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_compile.assert_not_called()
    mock_verify.assert_called_once()
    mock_open_level.assert_called_once()
    assert result["status"] == "completed"
    assert result["result"]["status"] == "online"
    assert result["result"]["requested_map"] == requested_map
    assert result["result"]["map_recovered_by_open_level"] is True
    assert result["result"]["map_recovery"]["active_world"]["package"] == requested_map


def test_run_editor_launch_task_recovers_map_after_open_level_connection_reset(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    requested_map = "/Game/Maps/Oregon_Main"

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "map_path": requested_map,
        "timeout": 180,
    })

    wrong_map = {
        "status": "failed",
        "error": "Active editor world did not match requested level.",
        "expected_package": requested_map,
        "active_world": {"package": "/Game/Maps/TestMap_3C"},
    }
    expected_map = {
        "status": "ok",
        "expected_package": requested_map,
        "active_world": {"package": requested_map},
    }
    reset_recovery = {
        "status": "failed",
        "error": "('Connection aborted.', ConnectionResetError(10054, 'remote host closed', None, 10054, None))",
    }

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", side_effect=[
             {"status": "online", "port": 30010},
             {"status": "online", "port": 30010, "recovered_after": "open_level_connection_reset"},
         ]) as mock_wait_for_api, \
         patch("cli_anything.unreal.core.scene._verify_current_level", side_effect=[
             wrong_map,
             expected_map,
         ]) as mock_verify, \
         patch("cli_anything.unreal.core.scene.open_level", return_value=reset_recovery) as mock_open_level:
        result = _run_editor_launch_task(task, estimated_total_seconds=180)

    mock_compile.assert_not_called()
    assert mock_wait_for_api.call_count == 2
    assert mock_verify.call_count == 2
    mock_open_level.assert_called_once()
    assert result["status"] == "completed"
    assert result["result"]["status"] == "online"
    assert result["result"]["map_recovered_after_connection_reset"] is True
    assert result["result"]["map_recovery_post_disconnect_verification"]["active_world"]["package"] == requested_map


def test_run_editor_launch_task_reports_crash_during_map_recovery(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    requested_map = "/Game/Maps/Oregon_Main"

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "map_path": requested_map,
        "timeout": 180,
    })
    log_file = project_dir / "Saved" / "Logs" / "TestProj.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text(
        "World Memory Leaks\n"
        "FPyReferenceCollector::AddReferencedObjects\n"
        "/Script/LevelEditor.LevelEditorSubsystem.LoadLevel\n",
        encoding="utf-8",
    )

    wrong_map = {
        "status": "failed",
        "error": "Active editor world did not match requested level.",
        "expected_package": requested_map,
        "active_world": {"package": "/Game/Maps/TestMap_3C"},
    }
    reset_recovery = {
        "status": "failed",
        "error": "('Connection aborted.', ConnectionResetError(10054, 'remote host closed', None, 10054, None))",
    }
    recovery_crash = {
        "status": "crashed",
        "returncode": 3,
        "log_file": str(log_file),
        "error": "Editor process exited with code 3 before API came online.",
    }

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", side_effect=[
             {"status": "online", "port": 30010},
             recovery_crash,
         ]), \
         patch("cli_anything.unreal.core.scene._verify_current_level", return_value=wrong_map), \
         patch("cli_anything.unreal.core.scene.open_level", return_value=reset_recovery):
        result = _run_editor_launch_task(task, estimated_total_seconds=180)

    mock_compile.assert_not_called()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "EDITOR_CRASHED_DURING_MAP_RECOVERY"
    assert result["result"]["status"] == "map_recovery_crashed"
    assert result["result"]["failure_kind"] == "editor_crash_during_map_recovery"
    assert result["result"]["map_recovery_wait"]["returncode"] == 3
    assert result["result"]["likely_cause"] == "python_world_reference_leak_during_level_transition"
    assert "World Memory Leaks" in result["result"]["log_hints"]


def test_run_editor_launch_task_fails_when_requested_map_is_not_active(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    requested_map = "/Game/Maps/Oregon_Main"

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "map_path": requested_map,
    })

    verification = {
        "status": "failed",
        "error": "Active editor world did not match requested level.",
        "expected_package": requested_map,
        "active_world": {"package": "/Game/Maps/TestMap_3C"},
    }
    recovery = {
        "status": "failed",
        "error": "Open-level did not activate requested map.",
        "active_world": {"package": "/Game/Maps/TestMap_3C"},
    }
    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/Win64/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={"status": "online", "port": 30010}), \
         patch("cli_anything.unreal.core.scene._verify_current_level", return_value=verification) as mock_verify, \
         patch("cli_anything.unreal.core.scene.open_level", return_value=recovery) as mock_open_level:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_compile.assert_not_called()
    mock_verify.assert_called_once()
    mock_open_level.assert_called_once()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "EDITOR_LAUNCH_MAP_MISMATCH"
    assert result["result"]["requested_map"] == requested_map
    assert result["result"]["map_verification"]["active_world"]["package"] == "/Game/Maps/TestMap_3C"
    assert result["result"]["map_recovery"]["status"] == "failed"
    assert result["result"]["next_command"].endswith(f"editor open-level {requested_map}")


def test_run_editor_launch_task_deploys_bridge_for_ue4(tmp_path):
    """UE4 launch deploys and enables the cross-version bridge plugin."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "UE4Proj"
    project_dir.mkdir()
    uproject = project_dir / "UE4Proj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "4.26"}', encoding="utf-8")

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30022,
    })

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": [], "details": {"editor_binary_prefix": "UE4Editor"}},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockUE4"), \
         patch("cli_anything.unreal.utils.ue_backend.get_editor_binary_prefix", return_value="UE4Editor"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockUE4/Binaries/UE4Editor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True,
             "action": "already_up_to_date",
         }) as mock_deploy, \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled") as mock_enable, \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin") as mock_compile, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={"status": "online"}):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_deploy.assert_called_once()
    mock_enable.assert_called_once_with(str(project_dir), "CliAnythingBridge")
    mock_compile.assert_not_called()
    assert result["status"] == "completed"
    assert result["result"]["bridge_deploy"]["action"] == "already_up_to_date"
    assert result["result"]["bridge_binary_status"]["ready"] is True
    port_config = project_dir / "Config" / "DefaultWebRemoteControl.ini"
    assert result["result"]["remote_control_port_config"] == str(port_config)
    assert "RemoteControlHttpServerPort=30022" in port_config.read_text(encoding="utf-8")


def test_run_editor_launch_task_does_not_deploy_ue4_bridge_before_preflight_failure(tmp_path):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    project_dir = tmp_path / "UE4UnavailableRemote"
    project_dir.mkdir()
    uproject = project_dir / "UE4UnavailableRemote.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "4.26"}', encoding="utf-8")
    task = create_task("editor.launch", {"project_path": str(uproject), "port": 30010})

    with patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockUE4"), \
         patch("cli_anything.unreal.utils.ue_backend.get_editor_binary_prefix", return_value="UE4Editor"), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
             "ready": False,
             "engine": {"ready": True, "errors": [], "warnings": []},
             "project": {"ready": True, "errors": [], "warnings": []},
             "remote_control": {
                 "configured": False,
                 "plugin_loadable": {"available": False},
             },
         }), \
         patch("cli_anything.unreal.utils.ue_backend.ensure_remote_control_config", return_value={
             "status": "unavailable",
             "changes": [],
         }) as mock_prepare_remote, \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge") as mock_deploy:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_deploy.assert_not_called()
    mock_prepare_remote.assert_not_called()
    assert result["status"] == "failed"


def test_run_editor_launch_task_no_remote_starts_when_only_automation_is_unavailable(tmp_path):
    import subprocess

    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    project_dir = tmp_path / "UE5DirectLaunch"
    project_dir.mkdir()
    uproject = project_dir / "UE5DirectLaunch.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": None,
        "map_path": "/Game/Maps/Main",
        "timeout": 300,
        "extra_args": ["-NullRHI"],
        "no_remote": True,
    })
    exited_task = create_task("editor.launch", dict(task["payload"]))
    proc = MagicMock()
    proc.pid = 4242
    proc.wait.side_effect = subprocess.TimeoutExpired(["UnrealEditor.exe"], 2.0)
    exited_proc = MagicMock()
    exited_proc.pid = 4343
    exited_proc.wait.return_value = 7
    preflight = {
        "ready": False,
        "engine": {"ready": True, "errors": [], "warnings": []},
        "project": {"ready": True, "errors": [], "warnings": []},
        "remote_control": {
            "configured": False,
            "plugin_loadable": {"available": False},
            "fix_result": {"error": "RemoteControl plugin is not available/loadable for this engine."},
        },
        "bridge_plugin": {
            "ready": False,
            "issues": [
                "CliAnythingBridge plugin source is not deployed",
                "CliAnythingBridge plugin not enabled in .uproject",
            ],
        },
    }

    with patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockUE5"), \
         patch("cli_anything.unreal.utils.ue_backend.get_editor_binary_prefix", return_value="UnrealEditor"), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value=preflight), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockUE5/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.utils.ue_backend.ensure_remote_control_config") as mock_prepare_remote, \
         patch("cli_anything.unreal.utils.ue_backend._write_rc_port") as mock_write_port, \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge") as mock_deploy, \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api") as mock_wait_api, \
         patch("cli_anything.unreal.core.tasks._capture_windows_process_identity", return_value=None), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", side_effect=[proc, exited_proc]) as mock_popen:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)
        exited_result = _run_editor_launch_task(exited_task, estimated_total_seconds=120)

    assert result["status"] == "completed"
    assert result["phase"] == "launched"
    assert result["result"]["status"] == "launched"
    assert result["result"]["automation_mode"] == "not_requested"
    assert result["result"]["remote_control_verified"] is False
    assert result["result"]["bridge_deployment_attempted"] is False
    assert result["result"]["editor_readiness_verified"] is False
    assert result["result"]["map_requested"] == "/Game/Maps/Main"
    assert result["result"]["map_verified"] is False
    assert result["result"]["startup_precheck"]["ready"] is True
    assert "RemoteControl plugin is not available/loadable for this engine." in result["result"]["startup_precheck"]["ignored_automation_issues"]
    assert exited_result["status"] == "failed"
    assert exited_result["phase"] == "exited"
    assert exited_result["error"]["code"] == "EDITOR_DIRECT_LAUNCH_EXITED"
    assert exited_result["result"]["process_alive"] is False
    assert exited_result["result"]["returncode"] == 7
    assert mock_popen.call_args_list[0] == call([
        "F:/MockUE5/Binaries/UnrealEditor.exe",
        str(uproject),
        "/Game/Maps/Main",
        "-nosplash",
        "-NullRHI",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert mock_popen.call_count == 2
    proc.wait.assert_called_once_with(timeout=2.0)
    exited_proc.wait.assert_called_once_with(timeout=2.0)
    mock_prepare_remote.assert_not_called()
    mock_write_port.assert_not_called()
    mock_deploy.assert_not_called()
    mock_wait_api.assert_not_called()


def test_run_editor_launch_task_fails_on_compile_error(tmp_path):
    """Launch reports a targeted bridge build failure without a full-build retry."""
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    mock_proc = MagicMock()
    mock_proc.pid = 4242

    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text('{"FileVersion": 3, "EngineAssociation": "5.7"}', encoding="utf-8")

    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/Binaries/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=True), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.compile_bridge_plugin", return_value={
             "status": "error", "error": "Build failed", "returncode": 1
         }) as mock_compile, \
         patch("cli_anything.unreal.core.tasks._capture_windows_process_identity", return_value=None), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={
             "status": "error_dialog", "error": "Plugin 'CliAnythingBridge' failed to load because module 'CliAnythingBridge' could not be found."
         }) as mock_wait:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    mock_popen.assert_called_once()
    mock_wait.assert_called_once()
    mock_compile.assert_called_once()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "BRIDGE_MODULE_COMPILE_FAILED"


def test_wait_for_api_requires_spawned_process_to_own_port(tmp_path):
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    api = MagicMock()
    api.is_alive.return_value = True
    log_file = tmp_path / "Editor.log"
    log_file.write_text("", encoding="utf-8")
    state = AppState()
    progress = []

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=api,
    ) as api_cls, patch(
        "cli_anything.unreal.core.editor_lifecycle.time.time",
        side_effect=[0.0, 0.0, 0.0, 0.0, 2.0],
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.sleep",
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._restore_packages_blocker",
        return_value=None,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._diagnose_api_unreachable",
        return_value={},
    ) as diagnose, patch(
        "cli_anything.unreal.core.editor_lifecycle._check_log_errors_incremental",
        return_value=(None, 0),
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._check_log_errors",
        return_value=None,
    ):
        api_cls._get_pid_listening_on_port.return_value = 9999
        result = _wait_for_api(
            proc,
            30010,
            1,
            log_file,
            state,
            on_progress=progress.append,
        )

    assert result["status"] == "timeout"
    assert any(item["startup_phase"] == "waiting_for_port_owner" for item in progress)
    assert any(item.get("port_owner_pid") == 9999 for item in progress)
    diagnose.assert_called_once_with(
        log_file,
        30010,
        since_offset=0,
        expected_process_id=4242,
    )


def test_wait_for_api_accepts_verified_spawned_process_owner(tmp_path):
    from cli_anything.unreal.commands import AppState
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    api = MagicMock()
    api.is_alive.return_value = True
    log_file = tmp_path / "Editor.log"
    state = AppState()

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI",
        return_value=api,
    ) as api_cls:
        api_cls._get_pid_listening_on_port.return_value = 4242
        result = _wait_for_api(proc, 30010, 1, log_file, state)

    assert result["status"] == "online"
    assert result["process_id"] == 4242
    assert result["port_owner_pid"] == 4242
    assert result["port_owner_verified"] is True


def test_wait_for_api_timeout_reports_listening_port_with_http_server_log_hints(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    log_dir = tmp_path / "Saved" / "Logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "RXGame.log"
    log_file.write_text(
        "\n".join(
            [
                "LogRemoteControl: Display: Remote Control HTTP server started on port 30010",
                "LogRemoteControl: Display: WebSocket server started on port 30020",
                "LogHttpServerModule: Stopping all listeners...",
                "LogHttpServerModule: All listeners stopped",
                "LogFalconTunnel: StartHttpServer on port 14632",
                "LogHttpServerModule: Starting all listeners...",
                "LogHttpServerModule: All listeners started",
                "LogFalconTunnel: OnServiceConnected: pid:90524, err:connect failed",
            ]
        ),
        encoding="utf-8",
    )
    proc = MagicMock()
    proc.poll.return_value = None
    state = MagicMock()
    state.json_output = True

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", return_value=False), \
         patch("cli_anything.unreal.core.editor_lifecycle._tcp_port_accepts_connection", return_value=True), \
         patch("cli_anything.unreal.core.editor_lifecycle.time.time", side_effect=[100.0, 101.0, 101.0]), \
         patch("cli_anything.unreal.core.editor_lifecycle.time.sleep"):
        result = _wait_for_api(proc, 30010, 1, log_file, state)

    assert result["status"] == "timeout"
    assert result["failure_kind"] == "api_route_unhealthy"
    assert result["port_listening"] is True
    assert result["tcp_connect_succeeded"] is True
    assert result["api_route_healthy"] is False
    assert "likely_cause" not in result
    assert result["http_server_restart_status"] == "completed"
    assert any("All listeners started" in hint for hint in result["log_hints"])
    assert "restart completed" in result["suggestion"]


def test_api_unreachable_distinguishes_os_listener_from_failed_tcp_connect(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _diagnose_api_unreachable

    log_file = tmp_path / "RXGame.log"
    log_file.write_text(
        "\n".join(
            [
                "LogHttpListener: Created new HttpListener on 127.0.0.1:30011",
                "LogHttpServerModule: All listeners started",
            ]
        ),
        encoding="utf-8",
    )

    with patch(
        "cli_anything.unreal.core.editor_lifecycle.sys.platform",
        "win32",
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._tcp_port_accepts_connection",
        return_value=False,
    ), patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
        return_value=4242,
    ):
        result = _diagnose_api_unreachable(
            log_file,
            30011,
            expected_process_id=4242,
        )

    assert result["port_listening"] is True
    assert result["tcp_connect_succeeded"] is False
    assert result["api_route_healthy"] is False
    assert result["listener_pid"] == 4242
    assert result["listener_owned_by_editor"] is True
    assert result["failure_kind"] == "api_listener_unresponsive"
    assert result["likely_cause"] == "tcp_listener_not_accepting_connections"
    assert "LISTENING under PID 4242" in result["cause_hint"]
    assert "starting, busy, or stalled" in result["cause_hint"]


def test_api_unreachable_diagnostics_find_http_server_hints_outside_log_tail(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _diagnose_api_unreachable

    log_file = tmp_path / "RXGame.log"
    log_file.write_text(
        "\n".join(
            [
                "LogHttpServerModule: Stopping all listeners...",
                "LogHttpServerModule: All listeners stopped",
                "LogFalconTunnel: StartHttpServer on port 8262",
                "LogFalconTunnel: StartGatewayServer on port 31010",
                "LogHttpServerModule: Starting all listeners...",
                "LogHttpListener: Error: HttpListener unable to bind to 127.0.0.1:30010",
                "LogHttpListener: Created new HttpListener on 127.0.0.1:31010",
                "LogFalconTunnel: StartWebsocketServer on port 8263",
                "LogNoise: " + ("x" * (200 * 1024)),
            ]
        ),
        encoding="utf-8",
    )

    with patch("cli_anything.unreal.core.editor_lifecycle._tcp_port_accepts_connection", return_value=True):
        result = _diagnose_api_unreachable(log_file, 30010)

    assert result["likely_cause"] == "remote_control_port_bind_failed"
    assert any("FalconTunnel" in hint for hint in result["log_hints"])
    assert any("unable to bind to 127.0.0.1:30010" in hint for hint in result["log_hints"])
    assert "bind failure" in result["suggestion"]
    assert "project-plugin" not in result["suggestion"]


def test_wait_for_api_timeout_keeps_startup_log_window_for_diagnostics(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    log_file = tmp_path / "RXGame.log"
    log_file.write_text("", encoding="utf-8")
    proc = MagicMock()
    proc.poll.return_value = None
    state = MagicMock()
    state.json_output = True

    def write_unhealthy_log():
        log_file.write_text(
            "\n".join(
                [
                    "LogHttpServerModule: Stopping all listeners...",
                    "LogFalconTunnel: StartHttpServer on port 8262",
                    "LogHttpListener: Error: HttpListener unable to bind to 127.0.0.1:30010",
                    "LogNoise: " + ("x" * (200 * 1024)),
                ]
            ),
            encoding="utf-8",
        )
        return False

    with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive", side_effect=write_unhealthy_log), \
         patch("cli_anything.unreal.core.editor_lifecycle._tcp_port_accepts_connection", return_value=True), \
         patch("cli_anything.unreal.core.editor_lifecycle.time.time", side_effect=[100.0, 100.1, 103.1, 103.1, 104.0]), \
         patch("cli_anything.unreal.core.editor_lifecycle.time.sleep"):
        result = _wait_for_api(proc, 30010, 3, log_file, state)

    assert result["status"] == "timeout"
    assert result["likely_cause"] == "remote_control_port_bind_failed"
    assert any("FalconTunnel" in hint for hint in result["log_hints"])


def test_wait_for_api_missing_virtual_shader_reports_full_editor_rebuild(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    project_path = r"F:\CustomEngineGame\Game.uproject"
    log_file = tmp_path / "Game.log"
    log_file.write_text("previous launch\n", encoding="utf-8")
    proc = MagicMock(pid=4242)
    wrote_fatal = False

    def append_fatal_while_process_is_alive():
        nonlocal wrote_fatal
        if not wrote_fatal:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(
                    "Fatal error: [ShaderCore.cpp] [Line: 2823] "
                    "Couldn't find source file of virtual shader path "
                    "'/Engine/Private/VirtualTextureBCUpload.usf'\n"
                )
            wrote_fatal = True
        return None

    proc.poll.side_effect = append_fatal_while_process_is_alive
    state = MagicMock()
    state.json_output = True
    state.session.project_path = project_path

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI"
    ) as api_cls, patch(
        "cli_anything.unreal.core.editor_lifecycle._restore_packages_blocker",
        return_value=None,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.time",
        side_effect=[100.0, 101.0, 103.1],
    ):
        api_cls.return_value.is_alive.return_value = False
        result = _wait_for_api(proc, 30010, 10, log_file, state)

    assert result["status"] == "error_dialog"
    assert result["failure_kind"] == "engine_binary_source_mismatch"
    assert result["likely_cause"] == "stale_or_mixed_engine_binaries"
    assert result["diagnostic_basis"] == "registered_virtual_shader_source_missing"
    assert result["missing_virtual_shader_path"] == (
        "/Engine/Private/VirtualTextureBCUpload.usf"
    )
    assert result["requires_full_editor_rebuild"] is True
    assert result["recovery_command"] == (
        f'ue-cli --project "{project_path}" build compile '
        "--platform Win64 --config Development"
    )
    assert "--module" not in result["recovery_command"]


@pytest.mark.parametrize("returncode", [3221225785, -1073741511])
def test_wait_for_api_entrypoint_not_found_reports_full_editor_rebuild(
    tmp_path,
    returncode,
):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    project_path = r"F:\CustomEngineGame\Game.uproject"
    log_file = tmp_path / "Game.log"
    log_file.write_text("previous launch\n", encoding="utf-8")
    proc = MagicMock(pid=4242)
    proc.poll.return_value = returncode
    state = MagicMock()
    state.json_output = True
    state.session.project_path = project_path

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI"
    ) as api_cls:
        api_cls.return_value.is_alive.return_value = False
        result = _wait_for_api(proc, 30010, 10, log_file, state)

    assert result["status"] == "crashed"
    assert result["failure_kind"] == "engine_binary_entrypoint_mismatch"
    assert result["windows_status"] == "STATUS_ENTRYPOINT_NOT_FOUND"
    assert result["returncode_hex"] == "0xC0000139"
    assert result["requires_full_editor_rebuild"] is True
    assert result["recovery_command"] == (
        f'ue-cli --project "{project_path}" build compile '
        "--platform Win64 --config Development"
    )
    assert "--module" not in result["recovery_command"]


def test_remote_control_diagnostics_ignore_old_and_other_port_bind_failures(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _diagnose_api_unreachable

    log_file = tmp_path / "RXGame.log"
    log_file.write_text(
        "\n".join([
            "LogHttpServerModule: Stopping all listeners...",
            "LogHttpListener: Error: HttpListener unable to bind to 127.0.0.1:30011",
            "LogHttpServerModule: Starting all listeners...",
            "LogHttpServerModule: All listeners started",
            "LogHttpServerModule: Stopping all listeners...",
            "LogFalconTunnel: StartHttpServer on port 8262",
            "LogHttpListener: Error: HttpListener unable to bind to 127.0.0.1:31010",
            "LogHttpListener: Created new HttpListener on 127.0.0.1:30011",
            "LogHttpServerModule: Starting all listeners...",
            "LogHttpServerModule: All listeners started",
        ]),
        encoding="utf-8",
    )

    with patch("cli_anything.unreal.core.editor_lifecycle._tcp_port_accepts_connection", return_value=True):
        result = _diagnose_api_unreachable(log_file, 30011)

    assert result["http_server_restart_status"] == "completed"
    assert "likely_cause" not in result
    assert "restart completed" in result["suggestion"]


def test_remote_control_log_hints_prioritize_target_bind_and_restart_evidence(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _diagnose_api_unreachable

    log_file = tmp_path / "RXGame.log"
    log_file.write_text(
        "\n".join([
            "LogHttpServerModule: Stopping all listeners...",
            *[f"LogFalconTunnel: noisy diagnostic {index}" for index in range(20)],
            "LogHttpListener: Error: HttpListener unable to bind to 127.0.0.1:30011",
            "LogHttpServerModule: Starting all listeners...",
            "LogHttpServerModule: All listeners started",
        ]),
        encoding="utf-8",
    )

    with patch("cli_anything.unreal.core.editor_lifecycle._tcp_port_accepts_connection", return_value=True):
        result = _diagnose_api_unreachable(log_file, 30011)

    assert any("unable to bind to 127.0.0.1:30011" in hint for hint in result["log_hints"])
    assert any("All listeners started" in hint for hint in result["log_hints"])


def test_wait_for_api_crash_reports_progress_and_bounded_log_tail(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    log_file = tmp_path / "RXGame.log"
    log_file.write_text("previous launch\n", encoding="utf-8")
    proc = MagicMock()
    proc.returncode = 1

    def exit_after_writing_log():
        with log_file.open("a", encoding="utf-8") as handle:
            for index in range(10):
                handle.write(f"startup line {index} " + ("x" * 600) + "\n")
        return 1

    proc.poll.side_effect = exit_after_writing_log
    state = MagicMock()
    state.json_output = True
    state.session.project_path = r"F:\RXGame_2\RXGame.uproject"
    progress = []

    with patch(
        "cli_anything.unreal.core.editor_lifecycle.time.time",
        side_effect=[100.0, 101.0, 102.0],
    ):
        result = _wait_for_api(
            proc,
            30011,
            120,
            log_file,
            state,
            on_progress=progress.append,
        )

    assert result["status"] == "crashed"
    assert result["failure_kind"] == "editor_process_exited"
    assert result["startup_phase"] == "waiting_for_remote_control"
    assert result["port"] == 30011
    assert result["process_alive"] is False
    assert result["returncode"] == 1
    assert len(result["log_tail"]) == 8
    assert all(len(line) <= 503 for line in result["log_tail"])
    assert "previous launch" not in result["log_tail"]
    assert result["next_command"] == (
        'ue-cli --project "F:\\RXGame_2\\RXGame.uproject" editor launch'
    )
    assert progress[0]["process_alive"] is False


def test_wait_for_api_classifies_external_filesystem_ddc_crash(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    log_file = tmp_path / "RXGame.log"
    log_file.write_text("previous launch\n", encoding="utf-8")
    proc = MagicMock()

    def exit_after_writing_crash():
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(
                "LogThreadingWindows: Error: Runnable thread "
                "FileSystemCacheStoreMaintainer crashed.\n"
                "Fatal error!\n"
                "UnrealEditor-DerivedDataCache.dll!"
                "UE::DerivedData::FFileSystemCacheStoreMaintainer::CreateContentRoot()\n"
                "UnrealEditor-DerivedDataCache.dll!"
                "UE::DerivedData::FFileSystemCacheStoreMaintainer::Scan()\n"
            )
        return 3

    proc.poll.side_effect = exit_after_writing_crash
    state = MagicMock()
    state.json_output = True
    state.session.project_path = r"F:\RXGame_2\RXGame.uproject"

    with patch(
        "cli_anything.unreal.core.editor_lifecycle.time.time",
        side_effect=[100.0, 101.0, 102.0],
    ):
        result = _wait_for_api(proc, 30011, 120, log_file, state)

    assert result["status"] == "crashed"
    assert result["failure_kind"] == "external_editor_ddc_crash"
    assert result["likely_cause"] == "unreal_engine_filesystem_ddc_maintainer_crash"
    assert result["external_component"] == "Unreal Engine DerivedDataCache"
    assert result["editor_automation_dispatched"] is False
    assert result["retry_safe_after_exit"] is True
    assert "Retry editor launch once" in result["suggestion"]
    assert result["next_command"] == (
        'ue-cli --project "F:\\RXGame_2\\RXGame.uproject" editor launch'
    )


@pytest.mark.parametrize("restore_title", ["Restore Packages", "恢复包"])
def test_detect_ue_dialogs_matches_restore_packages_top_level_for_process(
    monkeypatch,
    restore_title,
):
    import ctypes

    from cli_anything.unreal.utils.ue_backend import detect_ue_dialogs

    class FakeUser32:
        titles = {
            101: restore_title,
            202: "Warning",
        }
        process_ids = {
            101: 4242,
            202: 9999,
        }

        @staticmethod
        def EnumWindows(callback, lparam):
            callback(101, lparam)
            callback(202, lparam)
            return True

        @staticmethod
        def EnumChildWindows(_hwnd, _callback, _lparam):
            return True

        @staticmethod
        def IsWindowVisible(_hwnd):
            return True

        def GetWindowTextLengthW(self, hwnd):
            return len(self.titles[int(hwnd)])

        def GetWindowTextW(self, hwnd, buffer, _length):
            buffer.value = self.titles[int(hwnd)]
            return len(buffer.value)

        def GetWindowThreadProcessId(self, hwnd, pid_pointer):
            pid_pointer._obj.value = self.process_ids[int(hwnd)]
            return 1

    monkeypatch.setattr(
        ctypes,
        "windll",
        type("FakeWindll", (), {"user32": FakeUser32()})(),
    )

    assert detect_ue_dialogs(process_id=4242) == [{
        "title": restore_title,
        "hwnd": 101,
        "process_id": 4242,
    }]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Restore Packages", True),
        ("恢复包", True),
        ("Warning", False),
    ],
)
def test_restore_packages_blocker_recognizes_supported_titles(title, expected):
    from cli_anything.unreal.core.editor_lifecycle import _restore_packages_blocker

    proc = MagicMock(pid=4242)
    with patch(
        "cli_anything.unreal.utils.ue_backend.detect_ue_dialogs",
        return_value=[{"title": title, "hwnd": 101, "process_id": 4242}],
    ):
        result = _restore_packages_blocker(proc)

    assert (result is not None) is expected


def test_wait_for_api_waits_for_restore_packages_then_resumes(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    log_file = tmp_path / "TestProj.log"
    log_file.write_text("", encoding="utf-8")
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    state = MagicMock()
    state.json_output = True
    progress = []
    blocker = {
        "title": "Restore Packages",
        "hwnd": 101,
        "process_id": 4242,
    }

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive",
        side_effect=[False, True],
    ), patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI._get_pid_listening_on_port",
        return_value=4242,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._restore_packages_blocker",
        side_effect=[blocker, None, None],
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.time",
        return_value=100.1,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.monotonic",
        return_value=200.0,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.sleep",
    ):
        result = _wait_for_api(
            proc,
            30021,
            120,
            log_file,
            state,
            on_progress=progress.append,
        )

    assert result["status"] == "online"
    assert result["process_alive"] is True
    assert progress[0]["status"] == "waiting_for_user_action"
    assert progress[0]["blocking_dialog"] == blocker
    assert progress[1]["status"] == "waiting_for_remote_control"
    assert progress[2]["status"] == "waiting_for_remote_control"


def test_wait_for_api_times_out_if_restore_packages_stays_open(tmp_path):
    from cli_anything.unreal.core.editor_lifecycle import _wait_for_api

    log_file = tmp_path / "TestProj.log"
    log_file.write_text("", encoding="utf-8")
    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    state = MagicMock(json_output=True)
    progress = []
    blocker = {
        "title": "Restore Packages",
        "hwnd": 101,
        "process_id": 4242,
    }

    with patch(
        "cli_anything.unreal.utils.ue_http_api.UEEditorAPI.is_alive",
        return_value=False,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._restore_packages_blocker",
        return_value=blocker,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle._diagnose_api_unreachable",
        return_value={},
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.time",
        side_effect=[100.0, 100.1, 100.2, 101.1],
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.monotonic",
        return_value=200.0,
    ), patch(
        "cli_anything.unreal.core.editor_lifecycle.time.sleep",
    ):
        result = _wait_for_api(
            proc,
            30021,
            1,
            log_file,
            state,
            on_progress=progress.append,
        )

    assert result["status"] == "timeout"
    assert result["failure_kind"] == "blocked_by_restore_packages"
    assert result["startup_phase"] == "blocked_by_restore_packages"
    assert result["blocking_dialog"] == blocker
    assert "1s launch timeout" in result["error"]
    assert progress[-1]["status"] == "waiting_for_user_action"


def test_run_editor_launch_task_persists_wait_progress(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    custom_log = tmp_path / "Custom Logs" / "Startup.log"
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "extra_args": [f"-abslog={custom_log}"],
    })
    proc = MagicMock()
    proc.pid = 4242

    def report_progress(_proc, port, _timeout, log_file, _state, on_progress=None):
        assert on_progress is not None
        assert log_file == custom_log
        on_progress({
            "startup_phase": "waiting_for_remote_control",
            "elapsed_seconds": 15,
            "port": port,
            "process_alive": True,
            "log_file": str(log_file),
        })
        return {"status": "online"}

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.tasks._capture_windows_process_identity", return_value={
             "pid": 4242,
             "creation_time": 123456,
             "image_path": "F:/MockEngine/UnrealEditor.exe",
         }), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", side_effect=report_progress):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "completed"
    assert Path(result["log_file"]) == custom_log
    assert Path(result["result"]["log_file"]) == custom_log
    assert result["result"]["startup_phase"] == "waiting_for_remote_control"
    assert result["result"]["elapsed_seconds"] == 15
    assert result["result"]["process_alive"] is True
    assert result["requested_port"] == 30010
    assert result["resolved_port"] == 30010
    assert result["editor_process_identity"]["creation_time"] == 123456


def test_run_editor_launch_task_cancel_wins_over_late_online_result(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import (
        _request_task_cancel,
        _run_editor_launch_task,
        create_task,
    )

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "CancelWins"
    project_dir.mkdir()
    uproject = project_dir / "CancelWins.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
    })
    proc = MagicMock(pid=4242)

    def cancel_then_report_online(*_args, **_kwargs):
        _request_task_cancel(task["task_id"])
        return {"status": "online", "port": 30010}

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.tasks._capture_windows_process_identity", return_value=None), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", side_effect=cancel_then_report_online):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "cancelled"
    assert result["phase"] == "exited"
    assert result["cancelled"] is True
    assert "error" not in result
    assert result["result"].get("status") != "online"


def test_second_project_launch_task_is_rejected_before_spawn(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import (
        _run_editor_launch_task,
        create_task,
        transition_task,
        update_task_fields,
    )

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "OneLaunch"
    project_dir.mkdir()
    uproject = project_dir / "OneLaunch.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    older = create_task("editor.launch", {"project_path": str(uproject), "port": 30010})
    update_task_fields(older["task_id"], worker_pid=41001)
    transition_task(older["task_id"], status="running")
    newer = create_task("editor.launch", {"project_path": str(uproject), "port": 30010})

    with patch("cli_anything.unreal.utils.ue_backend.find_engine_root", return_value="F:/MockEngine"), \
         patch("cli_anything.unreal.core.tasks._probe_task_process", return_value={"state": "running"}), \
         patch("cli_anything.unreal.utils.ue_backend.preflight_check") as preflight, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen") as popen:
        result = _run_editor_launch_task(newer, estimated_total_seconds=120)

    assert result["status"] == "failed"
    assert result["phase"] == "blocked"
    assert result["error"]["code"] == "EDITOR_LAUNCH_ALREADY_ACTIVE"
    assert result["error"]["details"]["active_task_id"] == older["task_id"]
    preflight.assert_not_called()
    popen.assert_not_called()


def test_run_editor_launch_task_rejects_nullrhi_before_preflight(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "extra_args": ["-nullrhi"],
    })

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check") as mock_preflight, \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen") as mock_popen:
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "EDITOR_LAUNCH_NULLRHI_UNSUPPORTED"
    assert result["error"]["details"]["editor_started"] is False
    mock_preflight.assert_not_called()
    mock_popen.assert_not_called()


def test_run_editor_launch_task_timeout_returns_exact_poll_command(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "timeout": 120,
    })
    proc = MagicMock()
    proc.pid = 4242

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value={
             "status": "timeout",
             "port": 30010,
             "process_alive": True,
             "failure_kind": "api_route_unhealthy",
             "error": "Editor API did not respond within 120s on port 30010.",
         }):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    expected = f'ue-cli --project "{uproject}" editor status {task["task_id"]}'
    assert result["status"] == "timeout"
    assert result["result"]["next_command"] == expected
    assert result["error"]["details"]["next_command"] == expected


@pytest.mark.parametrize(
    ("wait_result", "expected_code"),
    [
        (
            {
                "status": "error_dialog",
                "failure_kind": "engine_binary_source_mismatch",
                "error": "Missing registered virtual shader source.",
            },
            "EDITOR_ENGINE_BINARY_SOURCE_MISMATCH",
        ),
        (
            {
                "status": "crashed",
                "failure_kind": "engine_binary_entrypoint_mismatch",
                "error": "Editor exited with STATUS_ENTRYPOINT_NOT_FOUND.",
            },
            "EDITOR_ENGINE_BINARY_ENTRYPOINT_MISMATCH",
        ),
        (
            {
                "status": "crashed",
                "failure_kind": "external_editor_ddc_crash",
                "error": "Unreal Editor crashed in its file-system DDC maintainer.",
            },
            "EDITOR_EXTERNAL_DDC_CRASH",
        ),
    ],
)
def test_run_editor_launch_task_promotes_specific_startup_error_code(
    tmp_path,
    monkeypatch,
    wait_result,
    expected_code,
):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30010,
        "timeout": 120,
    })
    proc = MagicMock(pid=4242)

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"ready": True, "errors": [], "warnings": []},
        "project": {"ready": True, "errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=proc), \
         patch("cli_anything.unreal.core.editor_lifecycle._wait_for_api", return_value=wait_result):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "failed"
    assert result["error"]["code"] == expected_code
    assert result["error"]["details"] == wait_result


def test_run_editor_launch_task_keeps_restore_packages_recoverable(tmp_path, monkeypatch):
    from cli_anything.unreal.core.tasks import _run_editor_launch_task, create_task, load_task

    monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
    project_dir = tmp_path / "TestProj"
    project_dir.mkdir()
    uproject = project_dir / "TestProj.uproject"
    uproject.write_text(
        '{"FileVersion": 3, "EngineAssociation": "5.7"}',
        encoding="utf-8",
    )
    task = create_task("editor.launch", {
        "project_path": str(uproject),
        "port": 30021,
        "timeout": 120,
    })
    proc = MagicMock()
    proc.pid = 4242

    def wait_for_choice_then_resume(
        _proc,
        _port,
        _timeout,
        _log_file,
        _state,
        on_progress=None,
    ):
        assert on_progress is not None
        on_progress({
            "status": "waiting_for_user_action",
            "startup_phase": "blocked_by_restore_packages",
            "blocking_reason": "restore_packages",
            "process_alive": True,
            "blocking_dialog": {
                "title": "Restore Packages",
                "hwnd": 101,
                "process_id": 4242,
            },
        })
        waiting = load_task(task["task_id"])
        assert waiting["status"] == "running"
        assert waiting["phase"] == "waiting_user_action"
        assert waiting["result"]["status"] == "waiting_for_user_action"
        assert waiting["result"]["next_command"].endswith(
            f"editor status {task['task_id']}"
        )
        on_progress({
            "status": "waiting_for_remote_control",
            "startup_phase": "waiting_for_remote_control",
            "process_alive": True,
        })
        resumed = load_task(task["task_id"])
        assert resumed["status"] == "running"
        assert resumed["phase"] == "waiting_remote_control"
        assert "blocking_dialog" not in resumed["result"]
        assert "next_command" not in resumed["result"]
        return {
            "status": "online",
            "startup_phase": "ready",
            "process_alive": True,
            "port": 30021,
        }

    with patch("cli_anything.unreal.utils.ue_backend.preflight_check", return_value={
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.find_editor_exe", return_value="F:/MockEngine/UnrealEditor.exe"), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_already_running", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._check_port_in_use", return_value=None), \
         patch("cli_anything.unreal.core.editor_lifecycle._deploy_bridge", return_value={
             "deployed": True, "action": "already_up_to_date"
         }), \
         patch("cli_anything.unreal.utils.ue_backend._ensure_plugin_enabled", return_value=False), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
             "message": "Bridge plugin binary is ready.",
         }), \
         patch("cli_anything.unreal.core.tasks.subprocess.Popen", return_value=proc), \
         patch(
             "cli_anything.unreal.core.editor_lifecycle._wait_for_api",
             side_effect=wait_for_choice_then_resume,
         ):
        result = _run_editor_launch_task(task, estimated_total_seconds=120)

    assert result["status"] == "completed"
    assert result["phase"] == "online"
    assert result["result"]["status"] == "online"
    assert "error" not in result


def test_summarize_startup_precheck_includes_bridge_plugin_issues():
    """_summarize_startup_precheck includes bridge_plugin issues as warnings."""
    from cli_anything.unreal.core.editor_lifecycle import _summarize_startup_precheck

    check = {
        "ready": True,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
        "bridge_plugin": {
            "ready": False,
            "issues": ["CliAnythingBridge plugin not enabled in .uproject"],
            "auto_fixable": True,
        },
    }
    result = _summarize_startup_precheck(check)
    assert "CliAnythingBridge plugin not enabled in .uproject" in result["warnings"]
    assert not any(warning.startswith("Fixed:") for warning in result["warnings"])


def test_summarize_startup_precheck_surfaces_remote_control_recovery():
    from cli_anything.unreal.core.editor_lifecycle import _summarize_startup_precheck

    recovery = {
        "kind": "compile_source_plugin",
        "shell": "powershell",
        "build_command": '& "F:\\UE\\Engine\\Build\\BatchFiles\\Build.bat" UnrealEditor Win64 Development',
        "setup_command": 'ue-cli --project "F:\\Game\\Game.uproject" editor enable-remote',
        "retry_command": 'ue-cli --project "F:\\Game\\Game.uproject" editor launch',
    }
    check = {
        "ready": False,
        "engine": {"errors": [], "warnings": []},
        "project": {"errors": [], "warnings": []},
        "remote_control": {
            "configured": False,
            "issues": ["RemoteControl unavailable"],
            "fix_result": {
                "error": "RemoteControl plugin is not available/loadable for this engine.",
                "details": {"recovery": recovery},
            },
        },
    }

    result = _summarize_startup_precheck(check)

    assert result["errors"] == ["RemoteControl plugin is not available/loadable for this engine."]
    assert result["remote_control_recovery"] == recovery
