"""Tests for test_plugin_bridge.py — Uses synthetic data only, no UE editor required."""

import json
import os
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestPluginBridge:
    """Tests for core/plugin_bridge.py — deploy and detect logic."""

    def test_bundled_descriptor_matches_cpp_reported_version(self):
        """The descriptor VersionName must match GetPluginVersion()."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        descriptor = json.loads((plugin_dir / "CliAnythingBridge.uplugin").read_text(encoding="utf-8"))
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        match = re.search(r"GetPluginVersion\(\)\s*\{[^}]*TEXT\(\"([^\"]+)\"\)", cpp, re.DOTALL)

        assert match is not None
        assert match.group(1) == descriptor["VersionName"]

    def test_setup_includes_bundled_bridge_source(self):
        """Packaged installs must include the bridge source used for deployment."""
        setup_py = Path(__file__).resolve().parents[3] / "setup.py"
        setup_text = setup_py.read_text(encoding="utf-8")

        assert "bridge_plugin/CliAnythingBridge/*.uplugin" in setup_text
        assert "bridge_plugin/CliAnythingBridge/Source/CliAnythingBridge/*.cs" in setup_text
        assert "bridge_plugin/CliAnythingBridge/Source/CliAnythingBridge/Public/*.h" in setup_text
        assert "bridge_plugin/CliAnythingBridge/Source/CliAnythingBridge/Private/*.cpp" in setup_text

    def test_ensure_plugin_deployed_fresh_install(self, tmp_path):
        """First deploy copies plugin source to project Plugins/."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed, get_bundled_version

        project_dir = str(tmp_path)
        result = ensure_plugin_deployed(project_dir)

        assert result["deployed"] is True
        assert result["action"] == "fresh_install"
        assert result["version"] == get_bundled_version()

        plugin_dir = tmp_path / "Plugins" / "CliAnythingBridge"
        assert (plugin_dir / "CliAnythingBridge.uplugin").exists()
        assert (plugin_dir / "Source" / "CliAnythingBridge" / "CliAnythingBridge.Build.cs").exists()
        assert (plugin_dir / "Source" / "CliAnythingBridge" / "Public" / "CliAnythingBridgeLibrary.h").exists()
        assert (plugin_dir / "Source" / "CliAnythingBridge" / "Private" / "CliAnythingBridgeLibrary.cpp").exists()
        assert (plugin_dir / "Source" / "CliAnythingBridge" / "Private" / "CliAnythingBridgeModule.cpp").exists()
        descriptor = json.loads((plugin_dir / "CliAnythingBridge.uplugin").read_text(encoding="utf-8"))
        assert descriptor["EnabledByDefault"] is False

    def test_ensure_plugin_deployed_already_up_to_date(self, tmp_path):
        """Second deploy is a no-op when versions match."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)  # first install
        result = ensure_plugin_deployed(project_dir)  # second call

        assert result["deployed"] is True
        assert result["action"] == "already_up_to_date"

    def test_ensure_plugin_deployed_normalizes_old_enabled_by_default(self, tmp_path):
        """Old deployed descriptors must not keep loading before the bridge is compiled."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)
        uplugin = tmp_path / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
        data = json.loads(uplugin.read_text(encoding="utf-8"))
        data["EnabledByDefault"] = True
        uplugin.write_text(json.dumps(data), encoding="utf-8")

        result = ensure_plugin_deployed(project_dir)

        assert result["action"] == "already_up_to_date"
        assert result["descriptor_normalized"] is True
        data = json.loads(uplugin.read_text(encoding="utf-8"))
        assert data["EnabledByDefault"] is False

    def test_ensure_plugin_deployed_version_update(self, tmp_path):
        """Plugin is updated when bundled version is newer."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed, get_bundled_version

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)

        # Tamper with the deployed version to simulate an old install
        uplugin = tmp_path / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
        data = json.loads(uplugin.read_text())
        data["VersionName"] = "0.1"
        uplugin.write_text(json.dumps(data))

        result = ensure_plugin_deployed(project_dir)
        assert result["deployed"] is True
        assert "updated" in result["action"]
        assert result["version"] == get_bundled_version()

    def test_transactional_update_restores_previous_plugin(self, tmp_path):
        """A failed bridge build can restore the exact pre-upgrade plugin tree."""
        from cli_anything.unreal.core.plugin_bridge import (
            ensure_plugin_deployed,
            get_bundled_version,
            rollback_plugin_deployment,
        )

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)
        plugin_dir = tmp_path / "Plugins" / "CliAnythingBridge"
        uplugin = plugin_dir / "CliAnythingBridge.uplugin"
        data = json.loads(uplugin.read_text(encoding="utf-8"))
        data["VersionName"] = "1.20"
        uplugin.write_text(json.dumps(data), encoding="utf-8")
        working_dll = plugin_dir / "Binaries" / "Win64" / "UnrealEditor-CliAnythingBridge.dll"
        working_dll.parent.mkdir(parents=True)
        working_dll.write_bytes(b"working-1.20")

        deploy = ensure_plugin_deployed(project_dir, preserve_existing=True)
        transaction_root = Path(deploy["upgrade_transaction"]["transaction_root"])

        assert deploy["action"] == f"updated_1.20_to_{get_bundled_version()}"
        assert deploy["rollback_available"] is True
        assert not working_dll.exists()
        assert transaction_root.is_dir()

        rollback = rollback_plugin_deployment(deploy)

        assert rollback["status"] == "restored"
        assert rollback["restored"] is True
        assert rollback["previous_version"] == "1.20"
        assert working_dll.read_bytes() == b"working-1.20"
        restored = json.loads(uplugin.read_text(encoding="utf-8"))
        assert restored["VersionName"] == "1.20"
        assert not transaction_root.exists()

    def test_transactional_update_discards_backup_after_success(self, tmp_path):
        from cli_anything.unreal.core.plugin_bridge import (
            ensure_plugin_deployed,
            finalize_plugin_deployment,
            get_bundled_version,
        )

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)
        uplugin = tmp_path / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
        data = json.loads(uplugin.read_text(encoding="utf-8"))
        data["VersionName"] = "1.20"
        uplugin.write_text(json.dumps(data), encoding="utf-8")

        deploy = ensure_plugin_deployed(project_dir, preserve_existing=True)
        transaction_root = Path(deploy["upgrade_transaction"]["transaction_root"])
        commit = finalize_plugin_deployment(deploy)

        assert commit == {
            "status": "committed",
            "committed": True,
            "previous_version": "1.20",
            "version": get_bundled_version(),
        }
        assert not transaction_root.exists()
        deployed = json.loads(uplugin.read_text(encoding="utf-8"))
        assert deployed["VersionName"] == get_bundled_version()

    def test_deployment_refreshes_packaged_source_mtime(self, tmp_path):
        """UBT must see copied Build.cs as newer than a cached rules assembly."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed

        bundled_dir = tmp_path / "Bundled" / "CliAnythingBridge"
        source_dir = bundled_dir / "Source" / "CliAnythingBridge"
        source_dir.mkdir(parents=True)
        (bundled_dir / "CliAnythingBridge.uplugin").write_text(
            json.dumps({"VersionName": "9.9", "EnabledByDefault": False}),
            encoding="utf-8",
        )
        build_cs = source_dir / "CliAnythingBridge.Build.cs"
        build_cs.write_text("// synthetic", encoding="utf-8")
        packaged_mtime = time.time() - 3600
        os.utime(build_cs, (packaged_mtime, packaged_mtime))

        project_dir = tmp_path / "Project"
        project_dir.mkdir()
        with patch(
            "cli_anything.unreal.core.plugin_bridge._BUNDLED_PLUGIN_DIR",
            bundled_dir,
        ):
            result = ensure_plugin_deployed(str(project_dir))

        deployed_build_cs = (
            project_dir
            / "Plugins"
            / "CliAnythingBridge"
            / "Source"
            / "CliAnythingBridge"
            / "CliAnythingBridge.Build.cs"
        )
        assert result["deployed"] is True
        assert deployed_build_cs.stat().st_mtime > packaged_mtime + 3000

    def test_transactional_copy_failure_restores_previous_plugin(self, tmp_path):
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)
        uplugin = tmp_path / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
        data = json.loads(uplugin.read_text(encoding="utf-8"))
        data["VersionName"] = "1.20"
        uplugin.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "cli_anything.unreal.core.plugin_bridge.shutil.copytree",
            side_effect=OSError("synthetic copy failure"),
        ):
            result = ensure_plugin_deployed(project_dir, preserve_existing=True)

        assert result["deployed"] is False
        assert result["rollback"]["status"] == "restored"
        restored = json.loads(uplugin.read_text(encoding="utf-8"))
        assert restored["VersionName"] == "1.20"

    def test_ensure_plugin_deployed_locked_update_is_pending(self, tmp_path):
        """Online editors can lock bridge DLLs; deploy should report pending update instead of raising."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed, get_bundled_version

        project_dir = str(tmp_path)
        ensure_plugin_deployed(project_dir)

        uplugin = tmp_path / "Plugins" / "CliAnythingBridge" / "CliAnythingBridge.uplugin"
        data = json.loads(uplugin.read_text())
        data["VersionName"] = "0.1"
        uplugin.write_text(json.dumps(data))

        locked = PermissionError(5, "Access is denied", str(uplugin))
        with patch("cli_anything.unreal.core.plugin_bridge.shutil.rmtree", side_effect=locked), \
             patch("cli_anything.unreal.core.plugin_bridge.shutil.copytree") as copytree:
            result = ensure_plugin_deployed(project_dir)

        assert result["deployed"] is True
        assert result["action"] == "update_pending_locked"
        assert result["version"] == "0.1"
        assert result["bundled_version"] == get_bundled_version()
        assert result["retry_suggested"] is True
        copytree.assert_not_called()

    def test_get_plugin_binary_status_missing_binary(self, tmp_path):
        """Deployed bridge source is not launch-ready until its editor DLL exists."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed, get_plugin_binary_status

        ensure_plugin_deployed(str(tmp_path))

        result = get_plugin_binary_status(str(tmp_path))

        assert result["ready"] is False
        assert result["reason"] == "missing_binary"
        assert result["dll_path"].endswith("UnrealEditor-CliAnythingBridge.dll")

    def test_get_plugin_binary_status_uses_ue4_prefix(self, tmp_path):
        """UE4 projects use UE4Editor-* binary names."""
        from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed, get_plugin_binary_status

        engine_root = tmp_path / "UE4Engine"
        bin_dir = engine_root / "Engine" / "Binaries" / "Win64"
        bin_dir.mkdir(parents=True)
        (bin_dir / "UE4Editor.exe").write_text("fake", encoding="utf-8")
        ensure_plugin_deployed(str(tmp_path))

        result = get_plugin_binary_status(str(tmp_path), engine_root=str(engine_root))

        assert result["editor_binary_prefix"] == "UE4Editor"
        assert result["dll_path"].endswith("UE4Editor-CliAnythingBridge.dll")

    def test_get_plugin_binary_status_uses_manifest_mapped_binary(self, tmp_path):
        """Hot-reload suffixes in the modules manifest must select the real DLL."""
        from cli_anything.unreal.core.plugin_bridge import (
            ensure_plugin_deployed,
            get_plugin_binary_status,
        )

        engine_root = tmp_path / "EngineRoot"
        engine_bin = engine_root / "Engine" / "Binaries" / "Win64"
        engine_bin.mkdir(parents=True)
        (engine_bin / "UnrealEditor.exe").write_text("fake", encoding="utf-8")
        (engine_bin / "UnrealEditor.modules").write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )
        ensure_plugin_deployed(str(tmp_path))
        plugin_bin = tmp_path / "Plugins" / "CliAnythingBridge" / "Binaries" / "Win64"
        plugin_bin.mkdir(parents=True)
        dll = plugin_bin / "UnrealEditor-CliAnythingBridge-1234.dll"
        dll.write_bytes(b"MZ")
        (plugin_bin / "UnrealEditor.modules").write_text(
            json.dumps({
                "BuildId": "engine-build",
                "Modules": {"CliAnythingBridge": dll.name},
            }),
            encoding="utf-8",
        )

        result = get_plugin_binary_status(
            str(tmp_path),
            engine_root=str(engine_root),
        )

        assert result["ready"] is True
        assert result["dll_path"] == str(dll)

    def test_repair_bridge_modules_manifest_uses_engine_build_id(self, tmp_path):
        from cli_anything.unreal.core.plugin_bridge import repair_bridge_modules_manifest

        engine_root = tmp_path / "EngineRoot"
        engine_bin = engine_root / "Engine" / "Binaries" / "Win64"
        engine_bin.mkdir(parents=True)
        (engine_bin / "UnrealEditor.exe").write_text("fake", encoding="utf-8")
        (engine_bin / "UnrealEditor.modules").write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )
        plugin_bin = tmp_path / "Plugins" / "CliAnythingBridge" / "Binaries" / "Win64"
        plugin_bin.mkdir(parents=True)
        dll = plugin_bin / "UnrealEditor-CliAnythingBridge.dll"
        dll.write_bytes(b"MZ")

        result = repair_bridge_modules_manifest(
            str(tmp_path),
            str(engine_root),
        )

        assert result["status"] == "ok"
        assert result["action"] == "created"
        modules = json.loads(
            (plugin_bin / "UnrealEditor.modules").read_text(encoding="utf-8")
        )
        assert modules == {
            "BuildId": "engine-build",
            "Modules": {"CliAnythingBridge": dll.name},
        }

    def test_compile_bridge_plugin_repairs_missing_manifest_after_targeted_build(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.plugin_bridge import (
            compile_bridge_plugin,
            ensure_plugin_deployed,
        )

        project_dir = tmp_path / "Project"
        project_dir.mkdir()
        uproject = project_dir / "Project.uproject"
        uproject.write_text('{"FileVersion": 3}', encoding="utf-8")
        ensure_plugin_deployed(str(project_dir))
        engine_root = tmp_path / "EngineRoot"
        engine_bin = engine_root / "Engine" / "Binaries" / "Win64"
        engine_bin.mkdir(parents=True)
        (engine_bin / "UnrealEditor.exe").write_text("fake", encoding="utf-8")
        (engine_bin / "UnrealEditor.modules").write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )

        def targeted_compile(*args, **kwargs):
            plugin_bin = (
                project_dir
                / "Plugins"
                / "CliAnythingBridge"
                / "Binaries"
                / "Win64"
            )
            plugin_bin.mkdir(parents=True)
            (plugin_bin / "UnrealEditor-CliAnythingBridge.dll").write_bytes(b"MZ")
            return {
                "status": "ok",
                "returncode": 0,
                "editor_target": "UnrealEditor",
                "editor_target_source": "engine",
            }

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=targeted_compile,
        ) as mock_compile, patch(
            "cli_anything.unreal.core.build._validate_win64_editor_build_products",
            return_value={},
        ) as mock_validate:
            result = compile_bridge_plugin(
                str(uproject),
                engine_root=str(engine_root),
            )

        assert result["status"] == "ok"
        assert result["build_scope"] == "bridge_module"
        assert result["full_editor_build_started"] is False
        assert result["full_editor_build_required"] is False
        assert result["metadata_repair"]["action"] == "created"
        _, kwargs = mock_compile.call_args
        assert kwargs["modules"] == ["CliAnythingBridge"]
        assert kwargs["platform"] == "Win64"
        assert kwargs["use_engine_editor_target_if_missing"] is True
        mock_validate.assert_not_called()

    def test_compile_bridge_plugin_marks_repaired_output_as_recovered(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.plugin_bridge import (
            compile_bridge_plugin,
            ensure_plugin_deployed,
        )

        project_dir = tmp_path / "Project"
        project_dir.mkdir()
        uproject = project_dir / "Project.uproject"
        uproject.write_text('{"FileVersion": 3}', encoding="utf-8")
        source_dir = project_dir / "Source"
        source_dir.mkdir()
        (source_dir / "ProjectEditor.Target.cs").write_text(
            "Type = TargetType.Editor;",
            encoding="utf-8",
        )
        ensure_plugin_deployed(str(project_dir))
        engine_root = tmp_path / "EngineRoot"
        engine_bin = engine_root / "Engine" / "Binaries" / "Win64"
        engine_bin.mkdir(parents=True)
        (engine_bin / "UnrealEditor.exe").write_text("fake", encoding="utf-8")
        (engine_bin / "UnrealEditor.modules").write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )

        def targeted_compile(*args, **kwargs):
            plugin_bin = (
                project_dir
                / "Plugins"
                / "CliAnythingBridge"
                / "Binaries"
                / "Win64"
            )
            plugin_bin.mkdir(parents=True)
            (plugin_bin / "UnrealEditor-CliAnythingBridge.dll").write_bytes(b"MZ")
            return {
                "status": "error",
                "returncode": 0,
                "code": "INVALID_BUILD_OUTPUT",
                "failure_kind": "missing_editor_module_manifests",
                "missing_module_manifests": [
                    str(plugin_bin / "UnrealEditor.modules"),
                ],
            }

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=targeted_compile,
        ), patch(
            "cli_anything.unreal.core.build._validate_win64_editor_build_products",
            return_value={},
        ) as mock_validate:
            result = compile_bridge_plugin(
                str(uproject),
                engine_root=str(engine_root),
            )

        assert result["status"] == "ok"
        assert result["metadata_repair"]["action"] == "created"
        assert result["output_validation"] == {"status": "ok"}
        assert result["bridge_binary_status"]["ready"] is True
        assert result["compile_result"]["status"] == "ok"
        assert result["compile_result"]["output_recovered"] is True
        assert result["compile_result"]["recovered_by"] == (
            "metadata_repair_and_output_validation"
        )
        initial_validation = result["compile_result"]["initial_output_validation"]
        assert initial_validation["status"] == "error"
        assert initial_validation["code"] == "INVALID_BUILD_OUTPUT"
        assert initial_validation["failure_kind"] == "missing_editor_module_manifests"
        mock_validate.assert_called_once_with(
            str(uproject),
            str(engine_root),
            "Development",
            ["CliAnythingBridge"],
            require_requested_module_product=False,
        )

    def test_compile_bridge_plugin_accepts_project_receipt_without_bridge_product(
        self,
        tmp_path,
    ):
        """Project receipts need not enumerate a separately verified plugin DLL."""
        from cli_anything.unreal.core.plugin_bridge import (
            compile_bridge_plugin,
            ensure_plugin_deployed,
        )

        project_dir = tmp_path / "Project"
        project_dir.mkdir()
        uproject = project_dir / "Project.uproject"
        uproject.write_text('{"FileVersion": 3}', encoding="utf-8")
        source_dir = project_dir / "Source"
        source_dir.mkdir()
        (source_dir / "ProjectEditor.Target.cs").write_text(
            "Type = TargetType.Editor;",
            encoding="utf-8",
        )
        ensure_plugin_deployed(str(project_dir))

        engine_root = tmp_path / "EngineRoot"
        engine_bin = engine_root / "Engine" / "Binaries" / "Win64"
        engine_bin.mkdir(parents=True)
        (engine_bin / "UnrealEditor.exe").write_bytes(b"engine")
        (engine_bin / "UnrealEditor.modules").write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )

        project_bin = project_dir / "Binaries" / "Win64"
        project_bin.mkdir(parents=True)
        project_editor = project_bin / "ProjectEditor.exe"
        project_editor.write_bytes(b"project-editor")
        project_modules = project_bin / "UnrealEditor.modules"
        project_modules.write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )
        (project_bin / "ProjectEditor.target").write_text(
            json.dumps({
                "TargetName": "ProjectEditor",
                "TargetType": "Editor",
                "Platform": "Win64",
                "Configuration": "Development",
                "BuildProducts": [
                    {"Path": str(project_editor), "Type": "Executable"},
                    {"Path": str(project_modules), "Type": "RequiredResource"},
                ],
            }),
            encoding="utf-8",
        )

        def targeted_compile(*args, **kwargs):
            plugin_bin = (
                project_dir
                / "Plugins"
                / "CliAnythingBridge"
                / "Binaries"
                / "Win64"
            )
            plugin_bin.mkdir(parents=True)
            (plugin_bin / "UnrealEditor-CliAnythingBridge.dll").write_bytes(b"MZ")
            return {
                "status": "error",
                "returncode": 0,
                "code": "INVALID_BUILD_OUTPUT",
                "failure_kind": "invalid_build_receipt",
                "error": (
                    "Compile exited 0, but the Editor target receipt is invalid: "
                    "BuildProducts contains no PE file for requested module(s): "
                    "CliAnythingBridge"
                ),
            }

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=targeted_compile,
        ), patch(
            "cli_anything.unreal.core.build._validate_pe_image",
            return_value=None,
        ):
            result = compile_bridge_plugin(
                str(uproject),
                engine_root=str(engine_root),
            )

        assert result["status"] == "ok"
        assert result["full_editor_build_required"] is False
        assert result["metadata_repair"]["action"] == "created"
        assert result["output_validation"] == {"status": "ok"}
        assert result["bridge_binary_status"]["ready"] is True
        assert result["compile_result"]["status"] == "ok"
        assert result["compile_result"]["output_recovered"] is True
        assert result["compile_result"]["initial_output_validation"][
            "failure_kind"
        ] == "invalid_build_receipt"

    def test_compile_bridge_plugin_stops_before_full_build_when_target_invalid(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.plugin_bridge import (
            compile_bridge_plugin,
            ensure_plugin_deployed,
        )

        project_dir = tmp_path / "Project"
        project_dir.mkdir()
        uproject = project_dir / "Project.uproject"
        uproject.write_text('{"FileVersion": 3}', encoding="utf-8")
        source_dir = project_dir / "Source"
        source_dir.mkdir()
        (source_dir / "ProjectEditor.Target.cs").write_text(
            "Type = TargetType.Editor;",
            encoding="utf-8",
        )
        ensure_plugin_deployed(str(project_dir))
        engine_root = tmp_path / "EngineRoot"
        engine_bin = engine_root / "Engine" / "Binaries" / "Win64"
        engine_bin.mkdir(parents=True)
        (engine_bin / "UnrealEditor.exe").write_text("fake", encoding="utf-8")
        (engine_bin / "UnrealEditor.modules").write_text(
            json.dumps({"BuildId": "engine-build", "Modules": {}}),
            encoding="utf-8",
        )

        def targeted_compile(*args, **kwargs):
            plugin_bin = (
                project_dir
                / "Plugins"
                / "CliAnythingBridge"
                / "Binaries"
                / "Win64"
            )
            plugin_bin.mkdir(parents=True)
            (plugin_bin / "UnrealEditor-CliAnythingBridge.dll").write_bytes(b"MZ")
            return {"status": "ok", "returncode": 0}

        invalid = {
            "status": "error",
            "code": "INVALID_BUILD_OUTPUT",
            "failure_kind": "missing_editor_module_manifests",
        }
        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=targeted_compile,
        ), patch(
            "cli_anything.unreal.core.build._validate_win64_editor_build_products",
            return_value=invalid,
        ):
            result = compile_bridge_plugin(
                str(uproject),
                engine_root=str(engine_root),
            )

        assert result["status"] == "error"
        assert result["code"] == "BRIDGE_TARGETED_BUILD_INCOMPLETE"
        assert result["full_editor_build_started"] is False
        assert result["full_editor_build_required"] is True
        assert result["fallback_reason"] == "missing_editor_module_manifests"
        assert "--module" not in result["recovery_command"]

    def test_is_plugin_loaded_true(self):
        """is_plugin_loaded returns True when probe script succeeds."""
        from cli_anything.unreal.core.plugin_bridge import is_plugin_loaded

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.run_python_code") as mock_run:
            mock_run.return_value = {"loaded": True}
            assert is_plugin_loaded(mock_api) is True

    def test_is_plugin_loaded_false(self):
        """is_plugin_loaded returns False when plugin class not found."""
        from cli_anything.unreal.core.plugin_bridge import is_plugin_loaded

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.run_python_code") as mock_run:
            mock_run.return_value = {"loaded": False}
            assert is_plugin_loaded(mock_api) is False

    def test_is_plugin_loaded_error(self):
        """is_plugin_loaded returns False on communication error."""
        from cli_anything.unreal.core.plugin_bridge import is_plugin_loaded

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.run_python_code") as mock_run:
            mock_run.side_effect = Exception("connection refused")
            assert is_plugin_loaded(mock_api) is False

    def test_get_bundled_version(self):
        """get_bundled_version returns the bundled plugin version."""
        from cli_anything.unreal.core.plugin_bridge import get_bundled_version

        version = get_bundled_version()
        assert version is not None
        assert version == "1.39"

    def test_static_mesh_lod_property_reader_uses_native_vertex_paint_data(self):
        """Bridge exposes LOD fields omitted from Unreal reflection."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        header = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        assert "GetStaticMeshComponentLODProperty" in header
        assert "LODInfo.PaintedVertices" in cpp
        assert "LODInfo.OverrideVertexColors" in cpp

    def test_material_compile_errors_support_material_functions(self):
        """Bridge routes MaterialFunction assets through transient preview materials."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        header = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        assert "GetMaterialCompileErrors(UObject* Asset)" in header
        assert "Cast<UMaterialFunctionInterface>(Asset)" in cpp
        assert "MaterialFunction->GetPreviewMaterial()" in cpp

    def test_set_material_attributes_uses_safe_parallel_array_path(self):
        """SetMaterialAttributes IDs and inputs must never be edited independently."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        add_body = cpp.split(
            "FString UCliAnythingBridgeLibrary::AddMaterialExpression",
            1,
        )[1].split(
            "FString UCliAnythingBridgeLibrary::DeleteMaterialExpression",
            1,
        )[0]
        connect_body = cpp.split(
            "FString UCliAnythingBridgeLibrary::ConnectMaterialExpressions",
            1,
        )[1].split(
            "FString UCliAnythingBridgeLibrary::DisconnectMaterialExpression",
            1,
        )[0]

        assert 'MaterialExpressionSetMaterialAttributes.h' in cpp
        assert "CreateOrGetInputAttribute(Property)" in cpp
        assert "AttributeSetTypes.Add(AttributeId)" in cpp
        assert "MATERIAL_SET_ATTRIBUTES_UNSAFE_PROPERTY" in add_body
        assert add_body.index("MATERIAL_SET_ATTRIBUTES_UNSAFE_PROPERTY") < add_body.index("CreateMaterialExpression")
        assert "CreateOrGetSetMaterialAttributeInput" in connect_body
        assert "set_material_attribute_input" in connect_body

    def test_add_material_expression_resolves_aliases_and_rolls_back_errors(self):
        """Native add-node honors Python-style aliases and never keeps partial nodes."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        add_body = cpp.split(
            "FString UCliAnythingBridgeLibrary::AddMaterialExpression",
            1,
        )[1].split(
            "FString UCliAnythingBridgeLibrary::DeleteMaterialExpression",
            1,
        )[0]

        assert "NormalizeReflectedPropertyName" in cpp
        assert "FindMaterialExpressionProperty" in add_body
        assert "MATERIAL_NODE_PROPERTIES_UNAPPLIED" in cpp
        assert "DeleteMaterialExpression(Material, Expression)" in add_body
        assert "property_warnings" not in add_body

    def test_confirmation_broker_hooks_ue4_and_ue5_modal_delegates(self):
        """The out-of-band mailbox must work while Remote Control is blocked."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        module_cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeModule.cpp"
        ).read_text(encoding="utf-8")
        build_cs = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "CliAnythingBridge.Build.cs"
        ).read_text(encoding="utf-8")

        assert "FCoreDelegates::ModalErrorMessage" in module_cpp
        assert "FCoreDelegates::ModalMessageDialog" in module_cpp
        assert "ENGINE_MINOR_VERSION >= 3" in module_cpp
        assert "FCoreDelegates::OnPostEngineInit" in module_cpp
        assert 'TEXT("lease.json")' in module_cpp
        assert 'TEXT("pending-")' in module_cpp
        assert 'TEXT("response-")' in module_cpp
        assert "FPlatformProcess::Sleep(0.05f)" in module_cpp
        assert '"Json"' in build_cs

    def test_skip_restore_uses_native_package_recovery_policy_before_editor_init(self):
        """Explicit skip uses UE recovery API, not window automation or unattended mode."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        module_cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeModule.cpp"
        ).read_text(encoding="utf-8")

        assert 'TEXT("CliAnythingSkipRestorePackages")' in module_cpp
        assert "DisableRestorePromptAndDeclinePackageRecovery" in module_cpp
        assert "HandlePostEngineInit" in module_cpp
        assert "OnPostEngineInit.AddStatic(&HandlePostEngineInit)" in module_cpp

    def test_disconnect_helpers_defer_post_edit_to_single_recompile(self):
        """Bridge mutation must not duplicate RecompileMaterial notifications."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        expression_body = cpp.split(
            "FString UCliAnythingBridgeLibrary::DisconnectMaterialExpression",
            1,
        )[1].split(
            "FString UCliAnythingBridgeLibrary::GetTextureSourceInfo",
            1,
        )[0]
        output_body = cpp.split(
            "FString UCliAnythingBridgeLibrary::DisconnectMaterialOutput",
            1,
        )[1].split(
            "FString UCliAnythingBridgeLibrary::RecompileMaterial",
            1,
        )[0]

        for body in (expression_body, output_body):
            assert "PostEditChange(" not in body
            assert "RecompileEditedMaterial(Material)" in body

    def test_disconnect_helpers_use_native_mutation_on_ue57(self):
        """Native graph edits must not leave a Python-only UE 5.7 branch."""
        from cli_anything.unreal.core.plugin_bridge import _BUNDLED_PLUGIN_DIR

        cpp = (
            _BUNDLED_PLUGIN_DIR
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        assert "MATERIAL_DISCONNECT_UNSAFE_ENGINE" not in cpp
        assert "DisconnectMaterialExpression(UMaterial* Material" in cpp
        assert "DisconnectMaterialOutput(UMaterial* Material" in cpp

    def test_bridge_shader_dump_recompile_restores_package_dirty_state(self):
        """Diagnostic shader dumps must not leave a clean material package dirty."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        header = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        function = cpp.split(
            "FString UCliAnythingBridgeLibrary::RecompileMaterialShadersForDump",
            1,
        )[1].split("TArray<FString> UCliAnythingBridgeLibrary::GetMaterialHLSLCode", 1)[0]

        assert "GetActiveShaderPlatform" in header
        assert "RecompileMaterialShadersForDump" in header
        assert "Package->IsDirty()" in function
        assert "Material->PreEditChange(nullptr)" in function
        assert "Material->PostEditChange()" in function
        assert "Package->SetDirtyFlag(bDirtyBefore)" in function
        assert "package_dirty_restored" in function
        assert "Material->GetMaterialResource(ActivePlatform)" in function
        assert "Material->GetMaterialResource(GMaxRHIFeatureLevel)" in function
        assert "MaterialResource->GetFriendlyName()" in function
        assert "MaterialResource->GetDebugGroupName()" in function
        assert "shader_map_material" in function
        assert "shader_dump_name" in function
        assert "shader_debug_group" in function

    def test_bridge_shader_platform_uses_active_editor_preview_on_ue5(self):
        """UE5 shader dumps follow editor preview platform, not host RHI."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        function = cpp.split(
            "FString UCliAnythingBridgeLibrary::GetActiveShaderPlatform()",
            1,
        )[1].split(
            "FString UCliAnythingBridgeLibrary::RecompileMaterialShadersForDump",
            1,
        )[0]

        assert "#if ENGINE_MAJOR_VERSION >= 5" in function
        assert "GEditor->GetActiveShaderPlatform()" in function
        assert "FDataDrivenShaderPlatformInfo::GetName(ActivePlatform)" in function
        assert "LegacyShaderPlatformToShaderFormat(GMaxRHIShaderPlatform)" in function

    def test_bridge_shader_source_refreshes_changed_files_before_extraction(self):
        """Shader extraction must invalidate changed engine shader sources first."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")
        function = cpp.split(
            "TArray<FString> UCliAnythingBridgeLibrary::GetMaterialShaderSource",
            1,
        )[1].split("// Escape a string", 1)[0]

        refresh = 'HandleRecompileShadersCommand(TEXT("Changed"), *GLog)'
        assert refresh in function
        assert function.index(refresh) < function.index("ExtractResource->CacheShaders")

    def test_bridge_composed_viewport_capture_uses_slate_screenshot(self):
        """HUD-inclusive capture must read the composed Slate viewport region."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        header = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        assert "bool bIncludeUI = false" in header
        assert "FSlateApplication::Get().TakeScreenshot" in cpp
        assert "if (bIncludeUI)" in cpp

    def test_bridge_declares_umg_helpers(self):
        """UMG authoring helpers live in the bridge because WidgetTree fields are protected."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        header = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        for name in ("SetWidgetBlueprintRoot", "AddWidgetToCanvas", "GetWidgetBlueprintTree"):
            assert name in header
            assert name in cpp

    def test_bridge_declares_native_material_edit_surface(self):
        """Graph edits stay in C++ so Python never wraps expression UObjects."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        header = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        for name in (
            "AddMaterialExpression",
            "DeleteMaterialExpression",
            "RenameMaterialCustomInput",
            "ConnectMaterialExpressions",
            "DisconnectMaterialExpression",
            "ConnectMaterialOutput",
            "DisconnectMaterialOutput",
            "RecompileMaterial",
        ):
            assert name in header
            assert name in cpp

    def test_bridge_declares_struct_info_helper(self):
        """api-discover needs C++ reflection for UScriptStruct types such as CustomInput."""
        root = Path(__file__).parent.parent / "bridge_plugin" / "CliAnythingBridge"
        header = (root / "Source" / "CliAnythingBridge" / "Public" / "CliAnythingBridgeLibrary.h").read_text(encoding="utf-8")
        cpp = (root / "Source" / "CliAnythingBridge" / "Private" / "CliAnythingBridgeLibrary.cpp").read_text(encoding="utf-8")

        assert "GetStructInfo" in header
        assert "GetStructInfo" in cpp
        assert "UScriptStruct" in header
        assert "TFieldIterator<FProperty> It(Struct" in cpp

    def test_bridge_declares_texture_source_helper(self):
        """TextureSource inspection needs C++ because UE Python hides Texture2D.Source."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        header = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        assert "GetTextureSourceInfo" in header
        assert "GetTextureSourceInfo" in cpp
        assert "LockMipReadOnly" in cpp

    def test_bridge_declares_active_viewport_screenshot_helper(self):
        """Viewport capture must redraw and read only the active FSceneViewport."""
        root = Path(__file__).parent.parent / "bridge_plugin" / "CliAnythingBridge"
        header = (
            root
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            root
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        assert "TakeActiveViewportScreenshot" in header
        assert "GetViewports()" in cpp
        assert "SceneViewport->Draw()" in cpp
        assert "SceneViewport->ReadPixels" in cpp

    def test_bridge_declares_umg_image_helper(self):
        """WidgetBlueprint Image slot/brush editing needs bridge access to WidgetTree."""
        plugin_dir = Path(__file__).resolve().parents[1] / "bridge_plugin" / "CliAnythingBridge"
        header = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Public"
            / "CliAnythingBridgeLibrary.h"
        ).read_text(encoding="utf-8")
        cpp = (
            plugin_dir
            / "Source"
            / "CliAnythingBridge"
            / "Private"
            / "CliAnythingBridgeLibrary.cpp"
        ).read_text(encoding="utf-8")

        assert "SetWidgetImageProperties" in header
        assert "SetWidgetImageProperties" in cpp
        assert "SetBrushResourceObject" in cpp

    def test_get_loaded_plugin_version(self):
        """get_loaded_plugin_version queries the running editor."""
        from cli_anything.unreal.core.plugin_bridge import get_loaded_plugin_version

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.run_python_code") as mock_run:
            mock_run.return_value = {"version": "1.3"}
            assert get_loaded_plugin_version(mock_api) == "1.3"

    def test_get_loaded_plugin_version_not_loaded(self):
        """get_loaded_plugin_version returns None when plugin not loaded."""
        from cli_anything.unreal.core.plugin_bridge import get_loaded_plugin_version

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.run_python_code") as mock_run:
            mock_run.return_value = {"version": None}
            assert get_loaded_plugin_version(mock_api) is None

    def test_get_loaded_plugin_version_raises_transport_error_when_requested(self):
        """Health probes distinguish transport failure from an unloaded plugin."""
        from cli_anything.unreal.core.plugin_bridge import get_loaded_plugin_version

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.run_python_code") as mock_run:
            mock_run.return_value = {"error": "Read timed out"}
            with pytest.raises(RuntimeError, match="Read timed out"):
                get_loaded_plugin_version(mock_api, raise_on_error=True)

    def test_check_plugin_version_match(self):
        """check_plugin_version returns match=True when versions agree."""
        from cli_anything.unreal.core.plugin_bridge import check_plugin_version, get_bundled_version

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version") as mock_loaded, \
             patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed") as mock_deploy:
            mock_loaded.return_value = get_bundled_version()
            mock_deploy.return_value = {"deployed": True, "action": "already_up_to_date"}
            result = check_plugin_version(mock_api, "/tmp/project")
            assert result["match"] is True
            assert result["action_needed"] == "none"

    def test_check_plugin_version_mismatch(self):
        """check_plugin_version detects version mismatch."""
        from cli_anything.unreal.core.plugin_bridge import check_plugin_version, get_bundled_version

        mock_api = MagicMock()
        with patch("cli_anything.unreal.core.plugin_bridge.get_loaded_plugin_version") as mock_loaded, \
             patch("cli_anything.unreal.core.plugin_bridge.ensure_plugin_deployed") as mock_deploy:
            mock_loaded.return_value = "1.3"
            mock_deploy.return_value = {
                "deployed": True,
                "action": f"updated_1.3_to_{get_bundled_version()}",
                "version": get_bundled_version(),
            }
            result = check_plugin_version(mock_api, "/tmp/project")
            assert result["match"] is False
            assert result["action_needed"] == "recompile"
            assert result["loaded_version"] == "1.3"
            assert result["bundled_version"] == get_bundled_version()


# ═══════════════════════════════════════════════════════════════════════
#  Test _ensure_plugin_enabled and _is_plugin_enabled_in_uproject
# ═══════════════════════════════════════════════════════════════════════


class TestPluginEnableInUproject:
    """Tests for ue_backend._ensure_plugin_enabled and _is_plugin_enabled_in_uproject."""

    def test_ensure_plugin_enabled_auto_enables_bridge(self, tmp_path):
        """_ensure_plugin_enabled enables CliAnythingBridge in .uproject."""
        from cli_anything.unreal.utils.ue_backend import _ensure_plugin_enabled

        project_dir = tmp_path / "TestProj"
        project_dir.mkdir()
        uproject = project_dir / "TestProj.uproject"
        uproject.write_text(json.dumps({"FileVersion": 3}), encoding="utf-8")

        changed = _ensure_plugin_enabled(str(project_dir), "CliAnythingBridge")
        assert changed is True

        data = json.loads(uproject.read_text(encoding="utf-8"))
        plugin_names = [p["Name"] for p in data["Plugins"]]
        assert "CliAnythingBridge" in plugin_names
        assert next(p for p in data["Plugins"] if p["Name"] == "CliAnythingBridge")["Enabled"] is True

    def test_ensure_plugin_enabled_no_change_when_already_enabled(self, tmp_path):
        """_ensure_plugin_enabled returns False when plugin already enabled."""
        from cli_anything.unreal.utils.ue_backend import _ensure_plugin_enabled

        project_dir = tmp_path / "TestProj"
        project_dir.mkdir()
        uproject = project_dir / "TestProj.uproject"
        uproject.write_text(json.dumps({
            "FileVersion": 3,
            "Plugins": [{"Name": "CliAnythingBridge", "Enabled": True}],
        }), encoding="utf-8")

        changed = _ensure_plugin_enabled(str(project_dir), "CliAnythingBridge")
        assert changed is False

    def test_ensure_plugin_enabled_enables_disabled_entry(self, tmp_path):
        """_ensure_plugin_enabled changes Enabled from False to True."""
        from cli_anything.unreal.utils.ue_backend import _ensure_plugin_enabled

        project_dir = tmp_path / "TestProj"
        project_dir.mkdir()
        uproject = project_dir / "TestProj.uproject"
        uproject.write_text(json.dumps({
            "FileVersion": 3,
            "Plugins": [{"Name": "CliAnythingBridge", "Enabled": False}],
        }), encoding="utf-8")

        changed = _ensure_plugin_enabled(str(project_dir), "CliAnythingBridge")
        assert changed is True

        data = json.loads(uproject.read_text(encoding="utf-8"))
        assert next(p for p in data["Plugins"] if p["Name"] == "CliAnythingBridge")["Enabled"] is True

    def test_is_plugin_enabled_in_uproject_read_only(self, tmp_path):
        """_is_plugin_enabled_in_uproject checks without modifying."""
        from cli_anything.unreal.utils.ue_backend import _is_plugin_enabled_in_uproject

        project_dir = tmp_path / "TestProj"
        project_dir.mkdir()
        uproject = project_dir / "TestProj.uproject"

        # Not enabled
        uproject.write_text(json.dumps({"FileVersion": 3}), encoding="utf-8")
        assert _is_plugin_enabled_in_uproject(str(project_dir), "CliAnythingBridge") is False

        # Explicitly disabled
        uproject.write_text(json.dumps({
            "FileVersion": 3,
            "Plugins": [{"Name": "CliAnythingBridge", "Enabled": False}],
        }), encoding="utf-8")
        assert _is_plugin_enabled_in_uproject(str(project_dir), "CliAnythingBridge") is False

        # Enabled
        uproject.write_text(json.dumps({
            "FileVersion": 3,
            "Plugins": [{"Name": "CliAnythingBridge", "Enabled": True}],
        }), encoding="utf-8")
        assert _is_plugin_enabled_in_uproject(str(project_dir), "CliAnythingBridge") is True


# ═══════════════════════════════════════════════════════════════════════
#  Test get_material_errors with plugin path
# ═══════════════════════════════════════════════════════════════════════

