"""Tests for test_backend.py — Uses synthetic data only, no UE editor required."""

import json
import inspect
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestBackend:
    """Tests for utils/ue_backend.py."""

    def test_validate_engine_root(self, mock_engine_root):
        from cli_anything.unreal.utils.ue_backend import _validate_engine_root

        assert _validate_engine_root(mock_engine_root) is True
        assert _validate_engine_root("/nonexistent/path") is False

    def test_find_running_editors_bounds_combined_process_queries(self):
        from cli_anything.unreal.utils import ue_backend

        with patch.object(ue_backend.sys, "platform", "win32"), patch.object(
            ue_backend.time,
            "monotonic",
            side_effect=[100.0, 100.0, 102.0],
        ), patch.object(
            ue_backend.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("powershell", 2.0),
        ) as run:
            result = ue_backend.find_running_editors(timeout=2.0)

        assert result == []
        assert run.call_count == 1
        assert run.call_args.kwargs["timeout"] == pytest.approx(2.0)

    def test_find_editor_exe(self, mock_engine_root):
        from cli_anything.unreal.utils.ue_backend import find_editor_exe

        exe = find_editor_exe(mock_engine_root)
        assert exe is not None
        assert "UnrealEditor.exe" in exe

    def test_find_editor_exe_supports_ue4_cmd_binary(self, tmp_path):
        from cli_anything.unreal.utils.ue_backend import find_editor_exe

        bin_dir = tmp_path / "Engine" / "Binaries" / "Win64"
        bin_dir.mkdir(parents=True)
        (bin_dir / "UE4Editor-Cmd.exe").write_text("fake", encoding="utf-8")

        exe = find_editor_exe(str(tmp_path))

        assert exe is not None
        assert exe.endswith("UE4Editor-Cmd.exe")

    def test_check_engine_build_supports_ue4_binary_names(self, tmp_path):
        from cli_anything.unreal.utils.ue_backend import check_engine_build

        bin_dir = tmp_path / "Engine" / "Binaries" / "Win64"
        bin_dir.mkdir(parents=True)
        (bin_dir / "UE4Editor.exe").write_bytes(b"0" * 200_000)
        (bin_dir / "UE4Editor.modules").write_text(
            json.dumps({"BuildId": "ue4-build", "Modules": {"Core": "UE4Editor-Core.dll"}}),
            encoding="utf-8",
        )
        build_dir = tmp_path / "Engine" / "Build"
        build_dir.mkdir(parents=True)
        (build_dir / "Build.version").write_text(
            json.dumps({"MajorVersion": 4, "MinorVersion": 26, "PatchVersion": 1}),
            encoding="utf-8",
        )

        result = check_engine_build(str(tmp_path))

        assert result["ready"] is True
        assert result["build_id"] == "ue4-build"
        assert result["details"]["editor_binary_prefix"] == "UE4Editor"
        assert result["errors"] == []

    def test_check_project_build_supports_ue4_binary_names(self, tmp_path):
        from cli_anything.unreal.utils.ue_backend import check_project_build

        project_dir = tmp_path / "UE4Game"
        source_dir = project_dir / "Source" / "UAGame"
        source_dir.mkdir(parents=True)
        (source_dir / "UAGame.cpp").write_text("// cpp", encoding="utf-8")
        (source_dir / "UAGame.h").write_text("// h", encoding="utf-8")
        uproject = project_dir / "UAGame.uproject"
        uproject.write_text(
            json.dumps({"FileVersion": 3, "Modules": [{"Name": "UAGame", "Type": "Runtime"}]}),
            encoding="utf-8",
        )
        bin_dir = project_dir / "Binaries" / "Win64"
        bin_dir.mkdir(parents=True)
        (bin_dir / "UE4Editor.modules").write_text(
            json.dumps({"BuildId": "ue4-build", "Modules": {"UAGame": "UE4Editor-UAGame.dll"}}),
            encoding="utf-8",
        )
        dll = bin_dir / "UE4Editor-UAGame.dll"
        dll.write_bytes(b"0" * 10)
        os.utime(dll, (time.time() + 10, time.time() + 10))

        result = check_project_build(str(uproject), engine_build_id="ue4-build", editor_binary_prefix="UE4Editor")

        assert result["ready"] is True
        assert result["needs_compile"] is False
        assert result["details"]["editor_binary_prefix"] == "UE4Editor"
        assert result["errors"] == []

    def test_find_uat(self, mock_engine_root):
        from cli_anything.unreal.utils.ue_backend import find_uat

        uat = find_uat(mock_engine_root)
        assert uat is not None
        assert "RunUAT" in uat

    def test_find_build_bat(self, mock_engine_root):
        from cli_anything.unreal.utils.ue_backend import find_build_bat

        bat = find_build_bat(mock_engine_root)
        assert bat is not None
        assert "Build.bat" in bat

    def test_get_engine_version(self, mock_engine_root):
        from cli_anything.unreal.utils.ue_backend import get_engine_version

        version = get_engine_version(mock_engine_root)
        assert version == "5.7.0"

    def test_find_engine_root_env_var(self, mock_engine_root):
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        with patch.dict(os.environ, {"UE_ENGINE_ROOT": mock_engine_root}):
            root = find_engine_root()
            assert root == mock_engine_root

    def test_find_engine_root_no_engine(self):
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        with patch.dict(os.environ, {}, clear=True):
            # Should not crash even if no engine found
            root = find_engine_root("/nonexistent.uproject")

    def test_find_engine_root_version_hklm(self, tmp_path):
        """EngineAssociation '5.7' resolves via HKLM subkey name."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        uproject = tmp_path / "Test.uproject"
        uproject.write_text('{"EngineAssociation": "5.7"}', encoding="utf-8")

        mock_install_dir = r"C:\Program Files\Epic Games\UE_5.7"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hklm", return_value=mock_install_dir),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hkcu", return_value=None),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_from_registry", return_value=None),
        ):
            root = find_engine_root(str(uproject))
            assert root == mock_install_dir

    def test_find_engine_root_guid_hkcu(self, tmp_path):
        """EngineAssociation '{GUID}' resolves via HKCU Builds."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        guid = "{F9E7804A-46B1-30B0-1C7B-4B99E6AAB63F}"
        uproject = tmp_path / "Test.uproject"
        uproject.write_text(f'{{"EngineAssociation": "{guid}"}}', encoding="utf-8")

        mock_install_dir = r"F:\RX_ENGINE_5.7"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hklm", return_value=None),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hkcu", return_value=mock_install_dir),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_from_registry", return_value=None),
        ):
            root = find_engine_root(str(uproject))
            assert root == mock_install_dir

    def test_find_engine_root_path_assoc(self, tmp_path):
        """EngineAssociation that is a directory path is used directly."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        engine_dir = tmp_path / "MyEngine"
        engine_dir.mkdir()
        (engine_dir / "Engine" / "Build").mkdir(parents=True)

        uproject = tmp_path / "Test.uproject"
        uproject.write_text(f'{{"EngineAssociation": "{str(engine_dir).replace(chr(92), "/")}"}}', encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            root = find_engine_root(str(uproject))
            assert root is not None

    def test_find_engine_by_association_hklm_direct(self):
        """_find_engine_in_hklm opens subkey by version name directly."""
        from cli_anything.unreal.utils.ue_backend import _find_engine_in_hklm

        mock_winreg = MagicMock()
        mock_subkey = MagicMock()
        mock_subkey.__enter__ = lambda s: s
        mock_subkey.__exit__ = MagicMock(return_value=False)
        mock_winreg.OpenKey.return_value = mock_subkey
        mock_winreg.HKEY_LOCAL_MACHINE = MagicMock()
        mock_winreg.QueryValueEx.return_value = (r"C:\Program Files\Epic Games\UE_5.7", MagicMock())

        with patch.dict("sys.modules", {"winreg": mock_winreg}), \
             patch("cli_anything.unreal.utils.ue_backend._validate_engine_root", return_value=True):
            result = _find_engine_in_hklm("5.7")
            assert result == r"C:\Program Files\Epic Games\UE_5.7"
            # Verify it opened the version subkey directly
            mock_winreg.OpenKey.assert_called()

    def test_find_engine_by_association_hkcu_guid(self):
        """_find_engine_in_hkcu looks up GUID by value name."""
        from cli_anything.unreal.utils.ue_backend import _find_engine_in_hkcu

        guid = "{F9E7804A-46B1-30B0-1C7B-4B99E6AAB63F}"
        mock_winreg = MagicMock()
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = MagicMock()
        mock_winreg.QueryValueEx.return_value = (r"F:\RX_ENGINE_5.7", MagicMock())

        with patch.dict("sys.modules", {"winreg": mock_winreg}), \
             patch("cli_anything.unreal.utils.ue_backend._validate_engine_root", return_value=True):
            result = _find_engine_in_hkcu(guid)
            assert result == r"F:\RX_ENGINE_5.7"
            mock_winreg.QueryValueEx.assert_called_with(mock_key, guid)

    def test_find_engine_by_association_hklm_takes_priority(self, tmp_path):
        """When both HKLM and HKCU match, HKLM wins."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        uproject = tmp_path / "Test.uproject"
        uproject.write_text('{"EngineAssociation": "5.7"}', encoding="utf-8")

        hklm_dir = r"C:\Program Files\Epic Games\UE_5.7"
        hkcu_dir = r"F:\RX_ENGINE_5.7"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hklm", return_value=hklm_dir),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hkcu", return_value=hkcu_dir),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_from_registry", return_value=None),
        ):
            root = find_engine_root(str(uproject))
            assert root == hklm_dir

    def test_find_engine_by_association_hkcu_fallback(self, tmp_path):
        """When HKLM has no match, falls back to HKCU Build.version scan."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        uproject = tmp_path / "Test.uproject"
        uproject.write_text('{"EngineAssociation": "5.7"}', encoding="utf-8")

        hkcu_dir = r"F:\RX_ENGINE_5.7"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hklm", return_value=None),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hkcu", return_value=hkcu_dir),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_from_registry", return_value=None),
        ):
            root = find_engine_root(str(uproject))
            assert root == hkcu_dir

    def test_find_engine_by_association_no_match(self, tmp_path):
        """No matching engine returns None."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        uproject = tmp_path / "Test.uproject"
        uproject.write_text('{"EngineAssociation": "99.9"}', encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hklm", return_value=None),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_in_hkcu", return_value=None),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_from_registry", return_value=None),
        ):
            root = find_engine_root(str(uproject))
            assert root is None

    def test_find_engine_root_no_uproject_uses_registry(self):
        """Without uproject, falls back to registry."""
        from cli_anything.unreal.utils.ue_backend import find_engine_root

        mock_dir = r"C:\Program Files\Epic Games\UE_5.7"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cli_anything.unreal.utils.ue_backend._find_engine_from_registry", return_value=mock_dir),
        ):
            root = find_engine_root()
            assert root == mock_dir

    def test_read_engine_version(self, tmp_path):
        """_read_engine_version reads MajorVersion.MinorVersion from Build.version."""
        from cli_anything.unreal.utils.ue_backend import _read_engine_version

        engine = tmp_path / "Engine"
        build_dir = engine / "Build"
        build_dir.mkdir(parents=True)
        (build_dir / "Build.version").write_text(
            '{"MajorVersion": 5, "MinorVersion": 7, "PatchVersion": 0}', encoding="utf-8"
        )
        assert _read_engine_version(str(tmp_path)) == "5.7"

    def test_read_engine_version_missing_file(self, tmp_path):
        """_read_engine_version returns None if Build.version is missing."""
        from cli_anything.unreal.utils.ue_backend import _read_engine_version

        assert _read_engine_version(str(tmp_path)) is None

    def test_find_engine_in_hkcu_version_scan(self):
        """_find_engine_in_hkcu scans entries by Build.version when assoc is not GUID."""
        from cli_anything.unreal.utils.ue_backend import _find_engine_in_hkcu

        mock_winreg = MagicMock()
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = MagicMock()

        # First entry: version doesn't match
        # Second entry: version matches
        mock_winreg.EnumValue.side_effect = [
            ("{GUID1}", r"F:\Engine1", 1),
            ("{GUID2}", r"F:\Engine2", 1),
            OSError,  # end enumeration
        ]

        def mock_read_version(path):
            return "5.5" if "Engine1" in path else "5.7"

        with patch.dict("sys.modules", {"winreg": mock_winreg}), \
             patch("cli_anything.unreal.utils.ue_backend._validate_engine_root", return_value=True), \
             patch("cli_anything.unreal.utils.ue_backend._read_engine_version", side_effect=mock_read_version):
            result = _find_engine_in_hkcu("5.7")
            assert result == r"F:\Engine2"

    def test_find_engine_from_registry_checks_both_hives(self):
        """_find_engine_from_registry checks HKLM first, then HKCU."""
        from cli_anything.unreal.utils.ue_backend import _find_engine_from_registry

        mock_winreg = MagicMock()
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_LOCAL_MACHINE = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = MagicMock()

        # HKLM has no entries (EnumKey raises OSError immediately)
        mock_winreg.EnumKey.side_effect = OSError
        # HKCU has one entry
        mock_winreg.EnumValue.side_effect = [
            ("{GUID1}", r"F:\CustomEngine", 1),
            OSError,
        ]

        with patch.dict("sys.modules", {"winreg": mock_winreg}), \
             patch("cli_anything.unreal.utils.ue_backend._validate_engine_root", return_value=True):
            result = _find_engine_from_registry()
            assert result == r"F:\CustomEngine"


# ═══════════════════════════════════════════════════════════════════════
#  Test session.py
# ═══════════════════════════════════════════════════════════════════════


class TestSession:
    """Tests for core/session.py."""

    def test_new_session(self):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        assert not sess.is_loaded
        assert sess.port == 30010

    def test_load_project(self, temp_project):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        sess.load_project(temp_project["uproject"])
        assert sess.is_loaded
        assert sess.project_name == "TestProject"
        assert sess.project_dir == temp_project["dir"]

    def test_load_project_not_found(self):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        with pytest.raises(FileNotFoundError):
            sess.load_project("/nonexistent.uproject")

    def test_snapshot_and_undo(self, temp_project):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        sess.load_project(temp_project["uproject"])

        sess.snapshot("change 1")
        sess._state["test_key"] = "value1"

        sess.snapshot("change 2")
        sess._state["test_key"] = "value2"

        # Undo change 2
        result = sess.undo()
        assert result is not None
        assert result["description"] == "change 2"
        assert sess._state.get("test_key") == "value1"

        # Undo change 1
        result = sess.undo()
        assert result is not None
        assert "test_key" not in sess._state

    def test_redo(self, temp_project):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        sess.load_project(temp_project["uproject"])

        sess.snapshot("change 1")
        sess._state["key"] = "v1"

        sess.undo()
        assert "key" not in sess._state

        result = sess.redo()
        assert result is not None
        # After redo, state should be back

    def test_undo_empty(self):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        assert sess.undo() is None

    def test_redo_empty(self):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        assert sess.redo() is None

    def test_status(self, temp_project):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        sess.load_project(temp_project["uproject"])
        status = sess.status()
        assert status["project"] == "TestProject"
        assert status["undo_available"] == 0

    def test_save_and_load_session(self, temp_project, tmp_path):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        sess.load_project(temp_project["uproject"])
        sess.port = 30015

        save_path = str(tmp_path / "session.json")
        sess.save_session(save_path)

        sess2 = Session()
        sess2.load_session(save_path)
        assert sess2.project_name == "TestProject"
        assert sess2.port == 30015

    def test_max_undo(self, temp_project):
        from cli_anything.unreal.core.session import Session, MAX_UNDO

        sess = Session()
        sess.load_project(temp_project["uproject"])

        for i in range(MAX_UNDO + 10):
            sess.snapshot(f"change {i}")

        assert len(sess._undo_stack) == MAX_UNDO

    def test_history(self, temp_project):
        from cli_anything.unreal.core.session import Session

        sess = Session()
        sess.load_project(temp_project["uproject"])

        sess.snapshot("first")
        sess.snapshot("second")
        sess.snapshot("third")

        history = sess.list_history()
        assert len(history) == 3
        assert history[0]["description"] == "third"
        assert history[2]["description"] == "first"

    def test_resolve_available_port_free(self, temp_project):
        """When desired port is free, return it unchanged."""
        from cli_anything.unreal.utils.ue_backend import resolve_available_port

        # Use a port that's almost certainly free
        result = resolve_available_port(temp_project["dir"], 39999)
        assert result == 39999

    def test_resolve_available_port_occupied(self, temp_project):
        """When desired port is occupied, find next free and persist to ini."""
        import socket
        from cli_anything.unreal.utils.ue_backend import read_rc_port, resolve_available_port

        # Occupy a port with a real socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        occupied_port = sock.getsockname()[1]
        sock.listen(1)
        try:
            result = resolve_available_port(temp_project["dir"], occupied_port)
            assert result == occupied_port + 1
            # Verify the ini was updated
            persisted = read_rc_port(temp_project["dir"])
            assert persisted == occupied_port + 1
        finally:
            sock.close()

    def test_ue4_remote_control_port_uses_web_remote_control_config(self, temp_project):
        from cli_anything.unreal.utils.ue_backend import (
            _write_rc_port,
            check_remote_control_config,
            read_rc_port,
        )

        config_file = _write_rc_port(
            temp_project["dir"],
            30022,
            editor_binary_prefix="UE4Editor",
        )

        assert Path(config_file).name == "DefaultWebRemoteControl.ini"
        content = Path(config_file).read_text(encoding="utf-8")
        assert "[/Script/WebRemoteControl.WebRemoteControlSettings]" in content
        assert "RemoteControlHttpServerPort=30022" in content
        assert read_rc_port(temp_project["dir"], "UE4Editor") == 30022
        (Path(temp_project["dir"]) / "Config" / "DefaultRemoteControl.ini").write_text(
            "[/Script/RemoteControlCommon.RemoteControlSettings]\n"
            "bAllowConsoleCommandRemoteExecution=True\n"
            "bEnableRemotePythonExecution=True\n",
            encoding="utf-8",
        )
        assert check_remote_control_config(
            temp_project["dir"],
            "UE4Editor",
        )["port"] == 30022

    def test_ue5_remote_control_port_keeps_remote_control_config(self, temp_project):
        from cli_anything.unreal.utils.ue_backend import _write_rc_port

        config_file = _write_rc_port(
            temp_project["dir"],
            30021,
            editor_binary_prefix="UnrealEditor",
        )

        assert Path(config_file).name == "DefaultRemoteControl.ini"
        content = Path(config_file).read_text(encoding="utf-8")
        assert "[/Script/RemoteControlCommon.RemoteControlSettings]" in content
        assert "RemoteControlHttpServerPort=30021" in content

        before = Path(config_file).read_bytes()
        assert _write_rc_port(temp_project["dir"], 30021, "UnrealEditor") == config_file
        assert Path(config_file).read_bytes() == before

    def test_extract_uproject_from_quoted_editor_cmdline(self):
        from cli_anything.unreal.utils.ue_backend import _extract_uproject_from_cmdline

        cmdline = (
            r'"C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe" '
            r'"F:\Projects\Space Game\Space Game.uproject" -log'
        )

        assert _extract_uproject_from_cmdline(cmdline) == r"F:\Projects\Space Game\Space Game.uproject"

    def test_extract_uproject_from_project_arg_cmdline(self):
        from cli_anything.unreal.utils.ue_backend import _extract_uproject_from_cmdline

        cmdline = (
            r'"C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe" '
            r'-Project="F:\Projects\Space Game\Space Game.uproject" -WaitMutex'
        )

        assert _extract_uproject_from_cmdline(cmdline) == r"F:\Projects\Space Game\Space Game.uproject"


# ═══════════════════════════════════════════════════════════════════════
#  Test build.py (command assembly, no real build)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel identity only")
def test_windows_process_identity_is_stable_for_current_process():
    from cli_anything.unreal.utils.ue_backend import _windows_process_identity

    first = _windows_process_identity(os.getpid())
    second = _windows_process_identity(os.getpid())

    assert first["query_ok"] is True
    assert first["found"] is True
    assert first["creation_time"] > 0
    assert first["creation_time"] == second["creation_time"]
    assert Path(first["image_path"]).name.lower().startswith("python")


def test_kill_process_tree_uses_native_fallback_after_taskkill_timeout():
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    native_result = {
        "ok": True,
        "pid": 49273,
        "method": "TerminateProcess",
    }
    with patch(
        "cli_anything.unreal.utils.ue_backend.subprocess.run",
        side_effect=subprocess.TimeoutExpired("taskkill", 10),
    ), patch(
        "cli_anything.unreal.utils.ue_backend._terminate_windows_process_result",
        return_value=native_result,
    ) as native_fallback:
        result = _kill_process_tree_result(49273)

    assert result["ok"] is True
    assert result["taskkill_timeout"] is True
    assert result["native_fallback"] == native_result
    assert result["retry_suggested"] is False
    native_fallback.assert_called_once_with(49273)


def test_kill_process_tree_result_reports_access_denied():
    from cli_anything.unreal.utils import ue_backend
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    proc = subprocess.CompletedProcess(
        ["taskkill", "/F", "/T", "/PID", "49272"],
        5,
        stdout=b"",
        stderr="ERROR: Access is denied.".encode("utf-8"),
    )

    with patch.object(ue_backend.sys, "platform", "win32"), \
         patch("cli_anything.unreal.utils.ue_backend.subprocess.run", return_value=proc), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=True):
        result = _kill_process_tree_result(49272)

    assert result["ok"] is False
    assert result["pid"] == 49272
    assert result["method"] == "taskkill"
    assert result["returncode"] == 5
    assert result["access_denied"] is True
    assert result["retry_suggested"] is False
    assert "Access is denied" in result["stderr"]
    assert "administrator" in result["suggestion"].lower()


def test_kill_process_tree_result_treats_missing_pid_after_taskkill_as_success():
    from cli_anything.unreal.utils import ue_backend
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    proc = subprocess.CompletedProcess(
        ["taskkill", "/F", "/T", "/PID", "91916"],
        255,
        stdout=b"",
        stderr=b"ERROR: There is no running instance of the task.",
    )

    with patch.object(ue_backend.sys, "platform", "win32"), \
         patch("cli_anything.unreal.utils.ue_backend.subprocess.run", return_value=proc), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=False):
        result = _kill_process_tree_result(91916)

    assert result["ok"] is True
    assert result["already_exited"] is True
    assert result["pid"] == 91916
    assert result["returncode"] == 255
    assert result["method"] == "taskkill_already_exited"
    assert result["process_exists_after_taskkill"] is False


def test_kill_process_tree_result_prefers_taskkill_missing_pid_over_stale_exists_probe():
    from cli_anything.unreal.utils import ue_backend
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    proc = subprocess.CompletedProcess(
        ["taskkill", "/F", "/T", "/PID", "35788"],
        255,
        stdout=b"SUCCESS: The process with PID 60660 has been terminated.",
        stderr=b"ERROR: The process with PID 35788 could not be terminated. "
               b"Reason: There is no running instance of the task.",
    )

    with patch.object(ue_backend.sys, "platform", "win32"), \
         patch(
             "cli_anything.unreal.utils.ue_backend.subprocess.run",
             return_value=proc,
         ), \
         patch(
             "cli_anything.unreal.utils.ue_backend._windows_process_exists",
             return_value=True,
         ), \
         patch("cli_anything.unreal.utils.ue_backend.time.sleep"):
        result = _kill_process_tree_result(35788)

    assert result["ok"] is True
    assert result["already_exited"] is True
    assert result["pid_state_race"] is True
    assert result["method"] == "taskkill_already_exited"
    assert result["process_exists_after_taskkill"] is True


def test_kill_process_tree_result_accepts_parent_exit_after_child_error():
    from cli_anything.unreal.utils import ue_backend
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    proc = subprocess.CompletedProcess(
        ["taskkill", "/F", "/T", "/PID", "55656"],
        255,
        stdout=b"SUCCESS: The process with PID 55656 has been terminated.",
        stderr=(
            b"ERROR: The process with PID 46812 (child process of PID 55656) "
            b"could not be terminated. Reason: The operation attempted is not supported."
        ),
    )

    with patch.object(ue_backend.sys, "platform", "win32"), \
         patch(
             "cli_anything.unreal.utils.ue_backend.subprocess.run",
             return_value=proc,
         ), \
         patch(
             "cli_anything.unreal.utils.ue_backend._windows_process_exists",
             side_effect=[True, False],
         ) as process_exists, \
         patch("cli_anything.unreal.utils.ue_backend.time.sleep") as sleep:
        result = _kill_process_tree_result(55656)

    assert result["ok"] is True
    assert result["pid"] == 55656
    assert result["returncode"] == 255
    assert result["method"] == "taskkill_already_exited"
    assert result["process_exists_after_taskkill"] is False
    assert result["post_taskkill_confirmation_attempts"] == 1
    assert result["post_taskkill_confirmation_seconds"] == 0.2
    assert "PID 46812" in result["stderr"]
    assert "not supported" in result["stderr"]
    assert process_exists.call_count == 2
    sleep.assert_called_once_with(0.2)


def test_kill_process_tree_result_accepts_success_after_confirmation_race():
    from cli_anything.unreal.utils import ue_backend
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    proc = subprocess.CompletedProcess(
        ["taskkill", "/F", "/T", "/PID", "91916"],
        0,
        stdout=b"SUCCESS",
        stderr=b"",
    )

    with patch.object(ue_backend.sys, "platform", "win32"), \
         patch(
             "cli_anything.unreal.utils.ue_backend.subprocess.run",
             return_value=proc,
         ), \
         patch(
             "cli_anything.unreal.utils.ue_backend._windows_process_exists",
             return_value=True,
         ) as process_exists, \
         patch("cli_anything.unreal.utils.ue_backend.time.sleep") as sleep:
        result = _kill_process_tree_result(91916)

    assert result["ok"] is True
    assert result["already_exited"] is False
    assert result["process_exists_after_taskkill"] is True
    assert result["pid_state_race"] is True
    assert result["kill_confirmed_by_taskkill"] is True
    assert result["retry_suggested"] is False
    assert process_exists.call_count == 6
    assert sleep.call_count == 5


def test_kill_process_tree_result_clears_ok_before_access_denied_branch():
    from cli_anything.unreal.utils import ue_backend
    from cli_anything.unreal.utils.ue_backend import _kill_process_tree_result

    proc = subprocess.CompletedProcess(
        ["taskkill", "/F", "/T", "/PID", "91916"],
        0,
        stdout=b"",
        stderr=b"ERROR: Access is denied.",
    )

    with patch.object(ue_backend.sys, "platform", "win32"), \
         patch("cli_anything.unreal.utils.ue_backend.subprocess.run", return_value=proc), \
         patch("cli_anything.unreal.utils.ue_backend._windows_process_exists", return_value=True):
        result = _kill_process_tree_result(91916)

    assert result["ok"] is False
    assert result["access_denied"] is True
    assert result["process_exists_after_taskkill"] is True
    assert "administrator" in result["suggestion"].lower()


class TestHTTPAPI:
    """Tests for utils/ue_http_api.py — mocked HTTP calls."""

    def test_api_init(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI(port=30015)
        assert api.port == 30015
        assert api.base_url == "http://localhost:30015"

    def test_get_pid_listening_on_port_decodes_cp936_netstat(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        output = (
            "活动连接\r\n\r\n"
            "  TCP    127.0.0.1:30020        0.0.0.0:0"
            "              LISTENING       174352\r\n"
        ).encode("cp936")
        proc = subprocess.CompletedProcess(
            ["netstat", "-ano", "-p", "tcp"],
            0,
            stdout=output,
            stderr=b"",
        )
        with patch(
            "cli_anything.unreal.utils.ue_http_api.subprocess.run",
            return_value=proc,
        ) as mock_run:
            assert UEEditorAPI._get_pid_listening_on_port(30020) == 174352

        assert mock_run.call_args.kwargs["text"] is False

    def test_scan_editor_ports_uses_short_http_timeout(self):
        from cli_anything.unreal.utils.ue_http_api import scan_editor_ports

        calls = []

        def fake_get(_url, timeout):
            calls.append(timeout)
            raise TimeoutError("slow")

        with patch("cli_anything.unreal.utils.ue_http_api.requests.get", side_effect=fake_get):
            assert scan_editor_ports(port_range=(30010, 30012)) == []

        assert calls
        assert max(calls) <= 0.5

    def test_bring_to_foreground_does_not_resize_window(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        source = inspect.getsource(UEEditorAPI.bring_to_foreground)

        assert "SetForegroundWindow" in source
        assert "BringWindowToTop" in source
        assert "SetWindowPos" not in source
        assert "MonitorFromWindow" not in source

    def test_set_window_rect_returns_win32_result(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        source = inspect.getsource(UEEditorAPI.set_window_rect)

        assert "return bool(" in source
        assert "user32.SetWindowPos(" in source

    def test_select_editor_window_prefers_main_unreal_editor_title(self):
        from cli_anything.unreal.utils.ue_http_api import _select_editor_window_hwnd

        candidates = [
            {
                "hwnd": 1,
                "title": "Message Log",
                "class_name": "UnrealWindow",
                "visible": True,
                "area": 2_000_000,
                "pid_rank": 0,
            },
            {
                "hwnd": 2,
                "title": "TestProject - Unreal Editor",
                "class_name": "UnrealWindow",
                "visible": True,
                "area": 100_000,
                "pid_rank": 0,
            },
        ]

        assert _select_editor_window_hwnd(candidates) == 2

    def test_select_editor_window_prefers_target_project_pid_over_other_editor(self):
        from cli_anything.unreal.utils.ue_http_api import _select_editor_window_hwnd

        candidates = [
            {
                "hwnd": 10,
                "pid": 574,
                "title": "Test574 - Unreal Editor",
                "class_name": "UnrealWindow",
                "visible": True,
                "area": 2_000_000,
                "pid_rank": 1,
                "project_rank": 1,
            },
            {
                "hwnd": 20,
                "pid": 222,
                "title": "RXGame - Unreal Editor",
                "class_name": "UnrealWindow",
                "visible": True,
                "area": 800_000,
                "pid_rank": 1,
                "project_rank": 0,
            },
        ]

        assert _select_editor_window_hwnd(candidates) == 20

    def test_window_project_rank_matches_project_pid_or_title(self):
        from cli_anything.unreal.utils.ue_http_api import _window_project_rank

        project = r"F:\RXGame_2\RXGame.uproject"

        assert _window_project_rank(
            pid=222,
            title="Other - Unreal Editor",
            project_path=project,
            project_pids={222},
        ) == 0
        assert _window_project_rank(
            pid=333,
            title="RXGame - Unreal Editor",
            project_path=project,
            project_pids=set(),
        ) == 0
        assert _window_project_rank(
            pid=574,
            title="Test574 - Unreal Editor",
            project_path=project,
            project_pids=set(),
        ) == 1

    def test_require_editor_stamps_project_path_for_window_selection(self):
        from cli_anything.unreal.commands import AppState, require_editor

        created = []

        class FakeAPI:
            def __init__(self, port):
                self.port = port
                self.project_path = None
                created.append(self)

            def is_alive(self):
                return True

        state = AppState()
        state.session.port = 30011
        state.session.project_path = r"F:\RXGame_2\RXGame.uproject"

        with patch("cli_anything.unreal.utils.ue_http_api.UEEditorAPI", FakeAPI), \
             patch("cli_anything.unreal.commands._guard_editor_project", return_value=None):
            api = require_editor(state)

        assert api is created[0]
        assert api.port == 30011
        assert api.project_path == r"F:\RXGame_2\RXGame.uproject"

    def test_select_editor_window_accepts_visible_titleless_unreal_window(self):
        from cli_anything.unreal.utils.ue_http_api import _select_editor_window_hwnd

        candidates = [
            {
                "hwnd": 1,
                "title": "TestProject - Unreal Editor",
                "class_name": "UnrealWindow",
                "visible": False,
                "area": 2_000_000,
                "pid_rank": 0,
            },
            {
                "hwnd": 2,
                "title": "",
                "class_name": "Chrome_WidgetWin_0",
                "visible": True,
                "area": 1_000_000,
                "pid_rank": 0,
            },
            {
                "hwnd": 3,
                "title": "",
                "class_name": "UnrealWindow",
                "visible": True,
                "area": 100_000,
                "pid_rank": 0,
            },
            {
                "hwnd": 4,
                "title": "Message Log",
                "class_name": "UnrealWindow",
                "visible": True,
                "area": 80_000,
                "pid_rank": 0,
            },
        ]

        assert _select_editor_window_hwnd(candidates) == 3

    def test_is_alive_false(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI(port=19999)  # unlikely to be in use
        assert api.is_alive() is False

    @patch("socket.create_connection")
    def test_is_listening_true(self, mock_connect):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()

        assert api.is_listening(timeout=0.25) is True
        mock_connect.assert_called_once_with(
            ("localhost", 30010),
            timeout=0.25,
        )

    @patch("socket.create_connection", side_effect=OSError("connection refused"))
    def test_is_listening_false(self, mock_connect):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()

        assert api.is_listening(timeout=0.25) is False
        mock_connect.assert_called_once()

    @patch("requests.get")
    def test_is_alive_true(self, mock_get):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_get.return_value = MagicMock(status_code=200)
        api = UEEditorAPI()
        assert api.is_alive() is True

    @patch("socket.create_connection")
    @patch("requests.get")
    def test_is_alive_total_timeout_stops_before_fallback(
        self,
        mock_get,
        mock_connect,
    ):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_get.side_effect = TimeoutError("remote info busy")
        api = UEEditorAPI()
        with patch(
            "cli_anything.unreal.utils.ue_http_api.time.monotonic",
            side_effect=[100.0, 100.0, 101.1],
        ):
            assert api.is_alive(timeout=1.0) is False

        assert mock_get.call_args.kwargs["timeout"] == 1.0
        mock_connect.assert_not_called()

    @patch("socket.create_connection")
    @patch("requests.put")
    @patch("requests.get")
    def test_is_alive_falls_back_to_read_only_object_call(
        self,
        mock_get,
        mock_put,
        mock_connect,
    ):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_get.side_effect = TimeoutError("remote info busy")
        mock_put.return_value = MagicMock(status_code=200)

        api = UEEditorAPI()

        assert api.is_alive() is True
        mock_connect.assert_called_once_with(
            ("localhost", 30010),
            timeout=0.5,
        )
        mock_put.assert_called_once_with(
            "http://localhost:30010/remote/object/call",
            json={
                "objectPath": "/Script/Engine.Default__KismetSystemLibrary",
                "functionName": "GetConsoleVariableStringValue",
                "parameters": {"VariableName": "t.MaxFPS"},
                "generateTransaction": False,
            },
            timeout=10,
        )

    @patch("socket.create_connection")
    @patch("requests.put")
    @patch("requests.get")
    def test_is_alive_skips_object_probe_without_tcp_listener(
        self,
        mock_get,
        mock_put,
        mock_connect,
    ):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_get.side_effect = ConnectionError("connection refused")
        mock_connect.side_effect = OSError("connection refused")

        api = UEEditorAPI()

        assert api.is_alive() is False
        mock_put.assert_not_called()

    @patch("requests.put")
    def test_exec_console(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{}'
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None
        mock_put.return_value = mock_response

        api = UEEditorAPI()
        result = api.exec_console("stat fps")
        assert "error" not in result

    @patch("requests.put")
    def test_get_cvar(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"ReturnValue": 1}'
        mock_response.json.return_value = {"ReturnValue": 1}
        mock_response.raise_for_status.return_value = None
        mock_put.return_value = mock_response

        api = UEEditorAPI()
        val = api.get_cvar("r.VSync", timeout=4)
        assert val == "1"
        assert mock_put.call_args.kwargs["timeout"] == 4

    def test_get_cvar_info_uses_bridge_metadata(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()
        with patch.object(api, "exec_python_ex") as mock_exec:
            def fake_exec(script, *, timeout=None):
                import re

                marker = re.search(r'_marker = "([^"]+)"', script).group(1)
                return {
                    "LogOutput": [
                        {
                            "Output": (
                                f'{marker}{{"name":"r.Test",'
                                '"exists":false,"value":""}'
                            )
                        }
                    ]
                }

            mock_exec.side_effect = fake_exec
            info = api.get_cvar_info("r.Test")

        assert info["name"] == "r.Test"
        assert info["exists"] is False
        assert info["value"] == ""

    def test_get_cvar_info_marks_empty_fallback_unverified(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()
        with patch.object(api, "exec_python_ex", return_value={"LogOutput": []}), \
             patch.object(
                 api,
                 "_get_cvar_response",
                 return_value={"ReturnValue": ""},
             ):
            info = api.get_cvar_info("r.MaybeMissing")

        assert info["name"] == "r.MaybeMissing"
        assert info["exists"] is None
        assert info["value"] == ""
        assert info["verification"] == "kismet_only"

    def test_get_cvar_info_marks_nonempty_fallback_existing(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()
        with patch.object(api, "exec_python_ex", return_value={"LogOutput": []}), \
             patch.object(
                 api,
                 "_get_cvar_response",
                 return_value={"ReturnValue": "1"},
             ):
            info = api.get_cvar_info("r.VSync")

        assert info["name"] == "r.VSync"
        assert info["exists"] is True
        assert info["value"] == "1"
        assert info["verification"] == "kismet_only"

    def test_get_cvar_info_falls_back_when_bridge_is_old(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()
        with patch.object(api, "exec_python_ex") as mock_exec, \
             patch.object(
                 api,
                 "_get_cvar_response",
                 return_value={"ReturnValue": "1"},
             ):
            def fake_exec(script, *, timeout=None):
                import re

                marker = re.search(r'_marker = "([^"]+)"', script).group(1)
                return {
                    "LogOutput": [
                        {
                            "Output": (
                                f'{marker}{{"name":"r.VSync","exists":null,'
                                '"value":"","verification":"bridge_unavailable",'
                                '"error":"missing function"}'
                            )
                        }
                    ]
                }

            mock_exec.side_effect = fake_exec
            info = api.get_cvar_info("r.VSync")

        assert info["name"] == "r.VSync"
        assert info["exists"] is True
        assert info["value"] == "1"
        assert info["bridge_error"] == "missing function"

    def test_get_cvar_info_returns_primary_timeout_without_fallback(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()
        with patch.object(
            api,
            "exec_python_ex",
            return_value={
                "error": (
                    "HTTPConnectionPool(host='localhost', port=30010): "
                    "Read timed out. (read timeout=5)"
                )
            },
        ), patch.object(api, "_get_cvar_response") as mock_fallback:
            info = api.get_cvar_info("r.SDOC.Enable", timeout=5)

        assert "Read timed out" in info["error"]
        assert info["verification"] == "request_failed"
        assert info["request_stage"] == "bridge_metadata"
        mock_fallback.assert_not_called()

    def test_get_cvar_info_uses_one_total_timeout_for_fallback(self):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        api = UEEditorAPI()
        with patch(
            "cli_anything.unreal.utils.ue_http_api.time.monotonic",
            side_effect=[100.0, 100.0, 106.0],
        ), patch.object(
            api,
            "exec_python_ex",
            return_value={"LogOutput": []},
        ), patch.object(api, "_get_cvar_response") as mock_fallback:
            info = api.get_cvar_info("r.SDOC.Enable", timeout=5)

        assert "timed out after 5 seconds" in info["error"]
        assert info["request_stage"] == "kismet_fallback"
        mock_fallback.assert_not_called()

    def test_scan_editor_ports_empty(self):
        from cli_anything.unreal.utils.ue_http_api import scan_editor_ports

        # Scan a very unlikely port range
        instances = scan_editor_ports(port_range=(19990, 19991))
        assert instances == []


# ═══════════════════════════════════════════════════════════════════════
#  Test materials.py (mocked API)
# ═══════════════════════════════════════════════════════════════════════


def _write_remote_control_project(
    project_dir: Path,
    *,
    remote_enabled: bool,
    python_enabled: bool = True,
    editor_scripting_enabled: bool = True,
) -> Path:
    project_dir.mkdir()
    uproject = project_dir / "RemoteOnly.uproject"
    uproject.write_text(json.dumps({
        "FileVersion": 3,
        "EngineAssociation": "5.7",
        "Plugins": [
            {"Name": "RemoteControl", "Enabled": remote_enabled},
            {"Name": "PythonScriptPlugin", "Enabled": python_enabled},
            {
                "Name": "EditorScriptingUtilities",
                "Enabled": editor_scripting_enabled,
            },
        ],
    }), encoding="utf-8")

    config_dir = project_dir / "Config"
    config_dir.mkdir()
    (config_dir / "DefaultRemoteControl.ini").write_text(
        "[/Script/RemoteControlCommon.RemoteControlSettings]\n"
        "bRestrictServerAccess=True\n"
        "bAllowConsoleCommandRemoteExecution=True\n"
        "bEnableRemotePythonExecution=True\n"
        "AllowedOrigin=\"*\"\n"
        "RemoteControlHttpServerPort=30010\n",
        encoding="utf-8",
    )
    return uproject


def test_check_remote_control_config_requires_enabled_plugin(tmp_path):
    """RemoteControl ini alone is not enough; .uproject plugin must be enabled."""
    from cli_anything.unreal.utils.ue_backend import check_remote_control_config

    project_dir = tmp_path / "RemoteOnly"
    _write_remote_control_project(project_dir, remote_enabled=False)

    result = check_remote_control_config(str(project_dir))

    assert result["configured"] is False
    assert any("RemoteControl plugin is not enabled" in issue for issue in result["issues"])


def test_check_remote_control_config_requires_python_script_plugin(tmp_path):
    """Remote Python settings still need PythonScriptPlugin loaded by the editor."""
    from cli_anything.unreal.utils.ue_backend import check_remote_control_config

    project_dir = tmp_path / "RemoteOnly"
    _write_remote_control_project(
        project_dir,
        remote_enabled=True,
        python_enabled=False,
    )

    result = check_remote_control_config(str(project_dir))

    assert result["configured"] is False
    assert any("PythonScriptPlugin is not enabled" in issue for issue in result["issues"])


def test_check_remote_control_config_requires_editor_scripting_utilities(tmp_path):
    """Asset commands need EditorAssetLibrary from EditorScriptingUtilities."""
    from cli_anything.unreal.utils.ue_backend import check_remote_control_config

    project_dir = tmp_path / "RemoteOnly"
    _write_remote_control_project(
        project_dir,
        remote_enabled=True,
        editor_scripting_enabled=False,
    )

    result = check_remote_control_config(str(project_dir))

    assert result["configured"] is False
    assert any("EditorScriptingUtilities is not enabled" in issue for issue in result["issues"])


def test_ensure_remote_control_unavailable_error_is_command_neutral(tmp_path):
    from cli_anything.unreal.utils.ue_backend import ensure_remote_control_config

    with patch("cli_anything.unreal.utils.ue_backend._check_plugin_loadable", return_value={
        "available": False,
        "reason": "module_missing",
    }):
        result = ensure_remote_control_config(str(tmp_path), engine_root="F:/MockEngine")

    assert result["status"] == "unavailable"
    assert "preflight" not in result["error"].lower()
    assert result["changes"] == []


def test_ensure_remote_control_enables_both_required_plugins(tmp_path):
    from cli_anything.unreal.utils.ue_backend import (
        _is_plugin_enabled_in_uproject,
        ensure_remote_control_config,
    )

    project_dir = tmp_path / "Automation"
    project_dir.mkdir()
    (project_dir / "Automation.uproject").write_text(
        json.dumps({"FileVersion": 3, "Plugins": []}),
        encoding="utf-8",
    )

    with patch(
        "cli_anything.unreal.utils.ue_backend._check_plugin_loadable",
        return_value={"available": True, "reason": "test"},
    ) as mock_loadable:
        result = ensure_remote_control_config(
            str(project_dir),
            engine_root="F:/MockEngine",
        )

    assert result["status"] == "created"
    assert _is_plugin_enabled_in_uproject(str(project_dir), "RemoteControl") is True
    assert _is_plugin_enabled_in_uproject(str(project_dir), "PythonScriptPlugin") is True
    assert [call.args[1] for call in mock_loadable.call_args_list] == [
        "RemoteControl",
        "PythonScriptPlugin",
        "EditorScriptingUtilities",
    ]


def test_ensure_remote_control_does_not_partially_enable_plugins(tmp_path):
    from cli_anything.unreal.utils.ue_backend import ensure_remote_control_config

    project_dir = tmp_path / "Automation"
    project_dir.mkdir()
    uproject = project_dir / "Automation.uproject"
    uproject.write_text(
        json.dumps({"FileVersion": 3, "Plugins": []}),
        encoding="utf-8",
    )
    before = uproject.read_bytes()

    with patch(
        "cli_anything.unreal.utils.ue_backend._check_plugin_loadable",
        side_effect=[
            {"available": True, "plugin": "RemoteControl", "reason": "test"},
            {
                "available": False,
                "plugin": "PythonScriptPlugin",
                "reason": "module_missing",
            },
        ],
    ):
        result = ensure_remote_control_config(
            str(project_dir),
            engine_root="F:/MockEngine",
        )

    assert result["status"] == "unavailable"
    assert "PythonScriptPlugin plugin is not available" in result["error"]
    assert result["changes"] == []
    assert uproject.read_bytes() == before
    assert not (project_dir / "Config").exists()


def test_plugin_loadability_checks_ue4_uncooked_only_modules(tmp_path):
    from cli_anything.unreal.utils.ue_backend import _check_plugin_loadable

    project_dir = tmp_path / "Project"
    project_dir.mkdir()
    engine_root = tmp_path / "UE4Engine"
    plugin_dir = (
        engine_root
        / "Engine"
        / "Plugins"
        / "Experimental"
        / "PythonScriptPlugin"
    )
    source_dir = plugin_dir / "Source" / "PythonScriptPlugin"
    source_dir.mkdir(parents=True)
    (plugin_dir / "PythonScriptPlugin.uplugin").write_text(
        json.dumps({
            "FileVersion": 3,
            "Modules": [
                {"Name": "PythonScriptPlugin", "Type": "UncookedOnly"},
            ],
        }),
        encoding="utf-8",
    )
    (source_dir / "PythonScriptPlugin.Build.cs").write_text(
        "// source only",
        encoding="utf-8",
    )

    result = _check_plugin_loadable(
        str(project_dir),
        "PythonScriptPlugin",
        engine_root=str(engine_root),
        editor_binary_prefix="UE4Editor",
    )

    assert result["available"] is False
    assert result["reason"] == "source_only_modules_uncompiled"
    assert result["modules"] == ["PythonScriptPlugin"]


def test_preflight_is_read_only_when_remote_control_plugin_is_disabled(tmp_path):
    """preflight reports RemoteControl work without changing the project."""
    from cli_anything.unreal.utils.ue_backend import _is_plugin_enabled_in_uproject, preflight_check

    project_dir = tmp_path / "RemoteOnly"
    uproject = _write_remote_control_project(project_dir, remote_enabled=False)
    before = uproject.read_bytes()

    with patch("cli_anything.unreal.utils.ue_backend.check_engine_build", return_value={
        "ready": True,
        "build_id": "engine-build",
        "errors": [],
        "warnings": [],
        "details": {},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.check_project_build", return_value={
             "ready": True,
             "needs_compile": False,
             "errors": [],
             "warnings": [],
             "details": {},
         }), \
         patch("cli_anything.unreal.utils.ue_backend._check_plugin_loadable", return_value={
             "available": True,
             "plugin": "RemoteControl",
             "reason": "test",
          }), \
         patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed", return_value={
             "deployed": True,
             "action": "already_up_to_date",
         }) as mock_deploy:
        result = preflight_check(str(uproject), engine_root="F:/MockEngine")

    assert uproject.read_bytes() == before
    assert _is_plugin_enabled_in_uproject(str(project_dir), "RemoteControl") is False
    assert result["read_only"] is True
    assert result["remote_control"]["auto_fixed"] is False
    assert result["remote_control"]["configured"] is False
    assert "editor enable-remote" in result["remote_control"]["suggestion"]
    assert not any("Fixed:" in warning for warning in result["project"]["warnings"])
    mock_deploy.assert_not_called()


def test_preflight_does_not_create_remote_control_config(tmp_path):
    from cli_anything.unreal.utils.ue_backend import preflight_check

    project_dir = tmp_path / "RemoteMissingConfig"
    uproject = _write_remote_control_project(project_dir, remote_enabled=True)
    config_file = project_dir / "Config" / "DefaultRemoteControl.ini"
    config_file.unlink()
    before = uproject.read_bytes()

    with patch("cli_anything.unreal.utils.ue_backend.check_engine_build", return_value={
        "ready": True,
        "build_id": "engine-build",
        "errors": [],
        "warnings": [],
        "details": {},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.check_project_build", return_value={
             "ready": True,
             "needs_compile": False,
             "errors": [],
             "warnings": [],
             "details": {},
         }), \
         patch("cli_anything.unreal.utils.ue_backend._check_plugin_loadable", return_value={
             "available": True,
             "plugin": "RemoteControl",
             "reason": "test",
         }):
        result = preflight_check(str(uproject), engine_root="F:/MockEngine")

    assert uproject.read_bytes() == before
    assert not config_file.exists()
    assert result["read_only"] is True
    assert result["remote_control"]["auto_fixed"] is False
    assert result["remote_control"]["configured"] is False


def test_preflight_does_not_normalize_existing_bridge_descriptor(tmp_path):
    from cli_anything.unreal.utils.ue_backend import preflight_check

    project_dir = tmp_path / "BridgeDescriptor"
    uproject = _write_remote_control_project(project_dir, remote_enabled=True)
    project_data = json.loads(uproject.read_text(encoding="utf-8"))
    project_data["Plugins"].append({"Name": "CliAnythingBridge", "Enabled": True})
    uproject.write_text(json.dumps(project_data), encoding="utf-8")
    descriptor = project_dir / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        json.dumps({"VersionName": "1.18", "EnabledByDefault": True}),
        encoding="utf-8",
    )
    before = descriptor.read_bytes()

    with patch("cli_anything.unreal.utils.ue_backend.check_engine_build", return_value={
        "ready": True,
        "build_id": "engine-build",
        "errors": [],
        "warnings": [],
        "details": {},
    }), \
         patch("cli_anything.unreal.utils.ue_backend.check_project_build", return_value={
             "ready": True,
             "needs_compile": False,
             "errors": [],
             "warnings": [],
             "details": {},
         }), \
         patch("cli_anything.unreal.utils.ue_backend._check_plugin_loadable", return_value={
             "available": True,
             "plugin": "RemoteControl",
             "reason": "test",
         }), \
         patch("cli_anything.unreal.core.plugin_bridge.get_bundled_version", return_value="1.18"), \
         patch("cli_anything.unreal.core.plugin_bridge.get_plugin_binary_status", return_value={
             "ready": True,
             "reason": "ok",
         }):
        result = preflight_check(str(uproject), engine_root="F:/MockEngine")

    assert descriptor.read_bytes() == before
    assert result["read_only"] is True
    assert result["bridge_plugin"]["ready"] is True


def test_preflight_does_not_enable_unavailable_remote_control_plugin(tmp_path):
    """UE4/custom engines can lack RemoteControl modules; preflight must not brick startup."""
    from cli_anything.unreal.utils.ue_backend import _is_plugin_enabled_in_uproject, preflight_check

    project_dir = tmp_path / "UE4Game"
    project_dir.mkdir()
    uproject = project_dir / "UAGame.uproject"
    uproject.write_text(
        json.dumps({"FileVersion": 3, "EngineAssociation": "4.26", "Plugins": []}),
        encoding="utf-8",
    )
    engine_root = tmp_path / "UE4Engine"
    bin_dir = engine_root / "Engine" / "Binaries" / "Win64"
    bin_dir.mkdir(parents=True)
    (bin_dir / "UE4Editor.exe").write_bytes(b"0" * 200_000)
    (bin_dir / "UE4Editor.modules").write_text(
        json.dumps({"BuildId": "ue4-build", "Modules": {"Core": "UE4Editor-Core.dll"}}),
        encoding="utf-8",
    )
    build_dir = engine_root / "Engine" / "Build"
    build_dir.mkdir(parents=True)
    (build_dir / "Build.version").write_text(
        json.dumps({"MajorVersion": 4, "MinorVersion": 26, "PatchVersion": 1}),
        encoding="utf-8",
    )

    with patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed", return_value={
        "deployed": True,
        "action": "already_up_to_date",
    }):
        result = preflight_check(str(uproject), engine_root=str(engine_root))

    assert _is_plugin_enabled_in_uproject(str(project_dir), "RemoteControl") is False
    assert not (project_dir / "Config" / "DefaultRemoteControl.ini").exists()
    assert result["remote_control"]["auto_fixed"] is False
    assert result["remote_control"]["fix_result"]["status"] == "unavailable"
    assert "RemoteControl plugin is not available" in result["remote_control"]["fix_result"]["error"]
    assert result["ready"] is False
    assert result["bridge_plugin"]["auto_fixable"] is True
    assert "CliAnythingBridge plugin source is not deployed" in result["bridge_plugin"]["issues"]
    assert not (project_dir / "Plugins" / "CliAnythingBridge").exists()


def test_preflight_does_not_enable_ue4_source_only_remote_control(tmp_path):
    """UE4 RemoteControl source without compiled editor DLLs is not safe to auto-enable."""
    from cli_anything.unreal.utils.ue_backend import _is_plugin_enabled_in_uproject, preflight_check

    project_dir = tmp_path / "UE4Game"
    project_dir.mkdir()
    uproject = project_dir / "UAGame.uproject"
    uproject.write_text(
        json.dumps({"FileVersion": 3, "EngineAssociation": "4.26", "Plugins": []}),
        encoding="utf-8",
    )
    engine_root = tmp_path / "UE4Engine"
    bin_dir = engine_root / "Engine" / "Binaries" / "Win64"
    bin_dir.mkdir(parents=True)
    (bin_dir / "UE4Editor.exe").write_bytes(b"0" * 200_000)
    (bin_dir / "UE4Editor.modules").write_text(
        json.dumps({"BuildId": "ue4-build", "Modules": {"Core": "UE4Editor-Core.dll"}}),
        encoding="utf-8",
    )
    build_dir = engine_root / "Engine" / "Build"
    build_dir.mkdir(parents=True)
    (build_dir / "Build.version").write_text(
        json.dumps({"MajorVersion": 4, "MinorVersion": 26, "PatchVersion": 1}),
        encoding="utf-8",
    )
    batch_dir = build_dir / "BatchFiles"
    batch_dir.mkdir()
    build_script = batch_dir / "Build.bat"
    build_script.write_text("@echo off\n", encoding="utf-8")
    plugin_dir = engine_root / "Engine" / "Plugins" / "VirtualProduction" / "RemoteControl"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "RemoteControl.uplugin").write_text(
        json.dumps({
            "FileVersion": 3,
            "Modules": [
                {"Name": "RemoteControl", "Type": "Runtime"},
                {"Name": "WebRemoteControl", "Type": "Runtime"},
                {"Name": "RemoteControlUI", "Type": "Editor"},
            ],
        }),
        encoding="utf-8",
    )
    for module_name in ("RemoteControl", "WebRemoteControl", "RemoteControlUI"):
        source_dir = plugin_dir / "Source" / module_name
        source_dir.mkdir(parents=True)
        (source_dir / f"{module_name}.Build.cs").write_text("// source only", encoding="utf-8")

    before = uproject.read_text(encoding="utf-8")
    result = preflight_check(str(uproject), engine_root=str(engine_root))
    after = uproject.read_text(encoding="utf-8")

    assert before == after
    assert _is_plugin_enabled_in_uproject(str(project_dir), "RemoteControl") is False
    assert result["ready"] is False
    assert result["remote_control"]["auto_fixed"] is False
    assert result["remote_control"]["fix_result"]["status"] == "unavailable"
    assert result["remote_control"]["fix_result"]["details"]["reason"] == "source_only_modules_uncompiled"
    recovery = result["remote_control"]["fix_result"]["details"]["recovery"]
    assert recovery == {
        "kind": "compile_source_plugin",
        "shell": "powershell",
        "build_command": (
            f'& "{build_script}" UE4Editor Win64 Development '
            f'-Project="{uproject}" -Plugin="{plugin_dir / "RemoteControl.uplugin"}" '
            "-NoUBTMakefiles -NoHotReload -WaitMutex"
        ),
        "setup_command": f'ue-cli --project "{uproject}" editor enable-remote',
        "retry_command": f'ue-cli --project "{uproject}" editor launch',
    }
    assert recovery["build_command"] in result["remote_control"]["fix_result"]["suggestion"]
    assert not any("enable-remote" in issue for issue in result["remote_control"]["issues"])
    assert not (project_dir / "Config" / "DefaultRemoteControl.ini").exists()


def test_plugin_loadability_source_only_has_ue5_recovery_command(tmp_path):
    from cli_anything.unreal.utils.ue_backend import _check_plugin_loadable

    project_dir = tmp_path / "Game"
    project_dir.mkdir()
    uproject = project_dir / "Game.uproject"
    uproject.write_text(json.dumps({"FileVersion": 3}), encoding="utf-8")
    engine_root = tmp_path / "UE5Engine"
    build_script = engine_root / "Engine" / "Build" / "BatchFiles" / "Build.bat"
    build_script.parent.mkdir(parents=True)
    build_script.write_text("@echo off\n", encoding="utf-8")
    plugin_dir = engine_root / "Engine" / "Plugins" / "VirtualProduction" / "RemoteControl"
    source_dir = plugin_dir / "Source" / "RemoteControl"
    source_dir.mkdir(parents=True)
    descriptor = plugin_dir / "RemoteControl.uplugin"
    descriptor.write_text(
        json.dumps({
            "FileVersion": 3,
            "Modules": [{"Name": "RemoteControl", "Type": "Runtime"}],
        }),
        encoding="utf-8",
    )
    (source_dir / "RemoteControl.Build.cs").write_text("// source only", encoding="utf-8")

    result = _check_plugin_loadable(
        str(project_dir),
        "RemoteControl",
        engine_root=str(engine_root),
        editor_binary_prefix="UnrealEditor",
        uproject_path=str(uproject),
    )

    assert result["available"] is False
    assert result["reason"] == "source_only_modules_uncompiled"
    assert result["recovery"]["build_command"] == (
        f'& "{build_script}" UnrealEditor Win64 Development '
        f'-Project="{uproject}" -Plugin="{descriptor}" '
        "-NoUBTMakefiles -NoHotReload -WaitMutex"
    )


def test_preflight_rejects_already_enabled_but_unavailable_remote_control(tmp_path):
    """A previously auto-enabled RemoteControl entry is still unsafe if engine modules are missing."""
    from cli_anything.unreal.utils.ue_backend import preflight_check

    project_dir = tmp_path / "RemoteOnly"
    uproject = _write_remote_control_project(project_dir, remote_enabled=True)
    engine_root = tmp_path / "UE4Engine"
    bin_dir = engine_root / "Engine" / "Binaries" / "Win64"
    bin_dir.mkdir(parents=True)
    (bin_dir / "UE4Editor.exe").write_bytes(b"0" * 200_000)
    (bin_dir / "UE4Editor.modules").write_text(
        json.dumps({"BuildId": "ue4-build", "Modules": {"Core": "UE4Editor-Core.dll"}}),
        encoding="utf-8",
    )
    build_dir = engine_root / "Engine" / "Build"
    build_dir.mkdir(parents=True)
    (build_dir / "Build.version").write_text(
        json.dumps({"MajorVersion": 4, "MinorVersion": 26, "PatchVersion": 1}),
        encoding="utf-8",
    )

    result = preflight_check(str(uproject), engine_root=str(engine_root))

    assert result["ready"] is False
    assert result["remote_control"]["configured"] is False
    assert result["remote_control"]["fix_result"]["status"] == "unavailable"
    assert any("RemoteControl plugin is not available" in issue for issue in result["remote_control"]["issues"])


class TestHTTPAPIAssets:
    """Tests for the asset-related methods on UEEditorAPI."""

    @patch("requests.put")
    def test_does_asset_exist_true(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ReturnValue": True}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        assert api.does_asset_exist("/Game/M_Test") is True

    @patch("requests.put")
    def test_does_asset_exist_false(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ReturnValue": False}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        assert api.does_asset_exist("/Game/Missing") is False

    @patch("requests.put")
    def test_delete_asset_success(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ReturnValue": True}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        assert api.delete_asset("/Game/M_Old") is True

    @patch("requests.put")
    def test_delete_asset_failure(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ReturnValue": False}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        assert api.delete_asset("/Game/Missing") is False

    @patch("requests.put")
    def test_find_asset_referencers(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ReturnValue": ["/Game/MI_Child", "/Game/Maps/L1"]}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        refs = api.find_asset_referencers("/Game/M_Test")
        assert len(refs) == 2
        assert "/Game/MI_Child" in refs

    @patch("requests.put")
    def test_find_asset_referencers_empty(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ReturnValue": []}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        refs = api.find_asset_referencers("/Game/M_Unused")
        assert refs == []

    @patch("requests.put")
    def test_collect_garbage(self, mock_put):
        from cli_anything.unreal.utils.ue_http_api import UEEditorAPI

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        api = UEEditorAPI()
        result = api.collect_garbage()
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════
#  Test scene CLI commands
# ═══════════════════════════════════════════════════════════════════════



@pytest.mark.parametrize('initial', ['', 'bAllowAnyRemoteFunctionCall=False\n', ';bAllowAnyRemoteFunctionCall=True\n', '[Other]\nbAllowAnyRemoteFunctionCall=True\n'])
def test_remote_function_permission_detects_and_repairs_gate(tmp_path, initial):
    from cli_anything.unreal.utils.ue_backend import (
        check_remote_control_config, ensure_remote_control_config,
    )

    project = tmp_path / 'Project'
    _write_remote_control_project(project, remote_enabled=True)
    config = project / 'Config/DefaultRemoteControl.ini'
    config.write_text(config.read_text() + initial, encoding='utf-8')
    engine = tmp_path / 'EngineRoot'
    plugin = engine / 'Engine/Plugins/VirtualProduction/RemoteControl'
    plugin.mkdir(parents=True)
    (plugin / 'RemoteControl.uplugin').write_text('{}')
    header = plugin / 'Source/RemoteControlCommon/Public/RemoteControlSettings.h'
    header.parent.mkdir(parents=True)
    header.write_text('bool bAllowAnyRemoteFunctionCall = false;')
    before = config.read_bytes()
    result = check_remote_control_config(str(project), engine_root=str(engine))
    assert not result['configured']
    assert any('restricts Remote Control function calls' in issue for issue in result['issues'])
    assert config.read_bytes() == before
    with patch('cli_anything.unreal.utils.ue_backend._check_plugin_loadable', return_value={'available': True}):
        repaired = ensure_remote_control_config(str(project), engine_root=str(engine))
        assert repaired['status'] == 'updated'
        assert check_remote_control_config(str(project), engine_root=str(engine))['configured']
        after = config.read_bytes()
        assert ensure_remote_control_config(str(project), engine_root=str(engine))['status'] == 'ok'
        assert config.read_bytes() == after
    if '[Other]' in initial:
        assert '[Other]\nbAllowAnyRemoteFunctionCall=True' in config.read_text()


@pytest.mark.parametrize('supported', [False, True])
def test_remote_function_permission_new_config_and_older_engine(tmp_path, supported):
    from cli_anything.unreal.utils.ue_backend import ensure_remote_control_config

    project = tmp_path / 'Project'
    _write_remote_control_project(project, remote_enabled=True)
    config = project / 'Config/DefaultRemoteControl.ini'
    config.unlink()
    with patch('cli_anything.unreal.utils.ue_backend._requires_remote_function_permission', return_value=supported):
        result = ensure_remote_control_config(str(project))
    assert result['status'] == 'created'
    assert ('bAllowAnyRemoteFunctionCall=True' in config.read_text()) is supported
