"""Tests for test_material.py — Uses synthetic data only, no UE editor required."""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


class TestMaterials:
    """Tests for core/materials.py — mocked editor API."""

    def _make_mock_api(self, assets=None, describe=None, properties=None):
        """Helper to create a mock API with common defaults."""
        mock_api = MagicMock()
        mock_api.search_assets.return_value = assets or {"Assets": []}
        mock_api.describe_object.return_value = describe or {"Properties": [], "Functions": []}
        mock_api.get_property.return_value = properties or {"error": "not found"}
        return mock_api

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_get_material_info_with_nodes(self, mock_exec_script):
        """Test that material info merges node data from Python script."""
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "TestMat", "Path": "/Game/TestMat.TestMat",
                            "Class": "/Script/Engine.Material",
                            "Metadata": {}}]
            },
            describe={
                "Properties": [{"Name": "BlendMode", "Type": "EBlendMode"}],
                "Functions": [],
            },
        )

        # Simulate Python script returning full node data
        mock_exec_script.return_value = {
            "name": "TestMat",
            "path": "/Game/TestMat",
            "class": "Material",
            "blend_mode": "BlendMode.BLEND_Opaque",
            "material_domain": "MaterialDomain.MD_Surface",
            "shading_model": "ShadingModel.MSM_DefaultLit",
            "two_sided": False,
            "nodes": [
                {"type": "MaterialExpressionTextureSampleParameter2D", "name": "BaseColor_Tex", "desc": "Base Color"},
                {"type": "MaterialExpressionConstant3Vector", "name": "Tint_Color", "desc": "Tint"},
                {"type": "MaterialExpressionMultiply", "name": "Multiply_0", "desc": ""},
                {"type": "MaterialExpressionTextureSample", "name": "Normal_Tex", "desc": "Normal Map"},
            ],
            "node_count": 4,
            "textures": [
                {"name": "T_BaseColor", "path": "/Game/Textures/T_BaseColor", "node_type": "MaterialExpressionTextureSampleParameter2D", "size_x": 2048, "size_y": 2048},
                {"name": "T_Normal", "path": "/Game/Textures/T_Normal", "node_type": "MaterialExpressionTextureSample", "size_x": 2048, "size_y": 2048},
            ],
            "texture_sample_count": 2,
        }

        result = get_material_info(mock_api, "/Game/TestMat")

        # Verify nodes are present
        assert "nodes" in result
        assert len(result["nodes"]) == 4
        assert result["node_count"] == 4
        # Verify node types
        node_types = [n["type"] for n in result["nodes"]]
        assert "MaterialExpressionTextureSampleParameter2D" in node_types
        assert "MaterialExpressionMultiply" in node_types
        # Verify textures merged
        assert "textures" in result
        assert len(result["textures"]) == 2
        assert result["texture_sample_count"] == 2
        # Verify material properties merged
        assert result["blend_mode"] == "BlendMode.BLEND_Opaque"
        assert result["shading_model"] == "ShadingModel.MSM_DefaultLit"
        assert result["two_sided"] is False

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_get_material_info_material_instance(self, mock_exec_script):
        """Test material info for MaterialInstanceConstant with parameters."""
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "MI_Test", "Path": "/Game/MI_Test.MI_Test",
                            "Class": "/Script/Engine.MaterialInstanceConstant",
                            "Metadata": {}}]
            },
            describe={"Properties": [{"Name": "Parent", "Type": "UMaterialInterface*"}], "Functions": []},
        )

        mock_exec_script.return_value = {
            "name": "MI_Test",
            "path": "/Game/MI_Test",
            "class": "MaterialInstanceConstant",
            "parent": "/Game/M_Master.M_Master",
            "scalar_parameters": [
                {"name": "Roughness", "value": 0.5},
                {"name": "Metallic", "value": 1.0},
            ],
            "vector_parameters": [
                {"name": "BaseColor", "value": {"r": 0.8, "g": 0.2, "b": 0.1, "a": 1.0}},
            ],
            "texture_parameters": [
                {"name": "DiffuseTexture", "texture": "/Game/Textures/T_Diffuse"},
            ],
        }

        result = get_material_info(mock_api, "/Game/MI_Test")

        assert result["parent"] == "/Game/M_Master.M_Master"
        assert len(result["scalar_parameters"]) == 2
        assert result["scalar_parameters"][0]["name"] == "Roughness"
        assert result["scalar_parameters"][0]["value"] == 0.5
        assert len(result["vector_parameters"]) == 1
        assert result["vector_parameters"][0]["value"]["r"] == 0.8
        assert len(result["texture_parameters"]) == 1
        assert result["texture_parameters"][0]["texture"] == "/Game/Textures/T_Diffuse"

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_get_material_info_reads_material_function_graph(self, mock_exec_script):
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api(assets={"Assets": [{
            "Name": "MF_Test",
            "Path": "/Game/MF_Test.MF_Test",
            "Class": "/Script/Engine.MaterialFunction",
            "Metadata": {},
        }]})
        mock_exec_script.return_value = {
            "name": "MF_Test",
            "path": "/Game/MF_Test.MF_Test",
            "class": "MaterialFunction",
            "nodes": [
                {"name": "Input", "type": "MaterialExpressionFunctionInput"},
                {"name": "Output", "type": "MaterialExpressionFunctionOutput"},
            ],
            "node_count": 2,
            "edges": [{"from_node": "Input", "to_node": "Output", "to_input_index": 0}],
            "function_inputs": ["Input"],
            "function_outputs": ["Output"],
        }

        result = get_material_info(mock_api, "/Game/MF_Test")

        assert result["class"] == "MaterialFunction"
        assert result["node_count"] == 2
        assert result["edges"][0]["from_node"] == "Input"
        assert result["function_inputs"] == ["Input"]
        assert result["function_outputs"] == ["Output"]

    def test_get_material_info_uses_object_path_for_native_bridge(self):
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api()
        mock_api.call_function.return_value = {"ReturnValue": json.dumps({
            "name": "MI_Drone",
            "nodes": [],
            "node_count": 0,
            "path": "/Game/Drone/MI_Drone.MI_Drone",
            "class": "MaterialInstanceConstant",
        })}

        result = get_material_info(mock_api, "/Game/Drone/MI_Drone")

        assert result["name"] == "MI_Drone"
        load_call, bridge_call = mock_api.call_function.call_args_list
        assert load_call.args[1] == "LoadAsset"
        assert load_call.args[2] == {"AssetPath": "/Game/Drone/MI_Drone"}
        assert bridge_call.args == (
            "/Script/CliAnythingBridge.Default__CliAnythingBridgeLibrary",
            "GetMaterialInfo",
            {"Asset": "/Game/Drone/MI_Drone.MI_Drone"},
        )
        assert bridge_call.kwargs == {"timeout": 30}

    @patch("cli_anything.unreal.core.script_runner.run_python_code")
    def test_material_hlsl_code_uses_resolver_for_package_path(self, mock_run):
        from cli_anything.unreal.core.materials import get_material_hlsl_code

        mock_run.return_value = {
            "material": "/Game/Drone/MA_Glow.MA_Glow",
            "file": "F:/Proj/Saved/CliAnything/MA_Glow.ush",
            "lines": 12,
            "source": "plugin",
        }

        result = get_material_hlsl_code(MagicMock(), "/Game/Drone/MA_Glow")

        assert result["source"] == "plugin"
        script = mock_run.call_args.args[1]
        assert "mat, loaded_asset_path, tried_asset_paths = _cli_load_material" in script
        assert 'material_candidates = ["/Game/Drone/MA_Glow", "/Game/Drone/MA_Glow.MA_Glow"]' in script
        assert '"material": loaded_asset_path' in script
        assert 'bridge = getattr(unreal, "CliAnythingBridgeLibrary", None)' in script
        assert '"code": "MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET"' in script
        compile(script, "<material-hlsl-script>", "exec")

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_material_hlsl_code_missing_bridge_has_upgrade_guidance(self, mock_exec):
        from cli_anything.unreal.core.materials import get_material_hlsl_code

        mock_exec.return_value = {
            "error": "CliAnythingBridgeLibrary is unavailable in this editor",
            "bridge_missing": True,
        }

        result = get_material_hlsl_code(MagicMock(), "/Game/M_Test")

        assert "not loaded" in result["error"]
        assert "plugin-upgrade" in result["error"]

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_material_hlsl_code_type_error_is_not_misreported_as_missing_bridge(self, mock_exec):
        from cli_anything.unreal.core.materials import get_material_hlsl_code

        bridge_error = (
            "CliAnythingBridgeLibrary: Failed to convert parameter 'material' when calling "
            "function 'CliAnythingBridgeLibrary.GetMaterialHLSLCode'"
        )
        mock_exec.return_value = {"error": bridge_error, "error_type": "TypeError"}

        result = get_material_hlsl_code(MagicMock(), "/Game/MF_Test")

        assert result == {"error": bridge_error, "error_type": "TypeError"}
        assert "not loaded" not in result["error"]

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_material_hlsl_code_cli_preserves_unsupported_asset_code(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "error": "Unsupported asset class for material hlsl-code: MaterialFunction",
            "code": "MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET",
            "asset_class": "MaterialFunction",
            "supported_classes": ["Material", "MaterialInstanceConstant"],
        }

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.material.require_editor",
            return_value=MagicMock(),
        ), patch("cli_anything.unreal.commands.material.require_project"):
            result = runner.invoke(cli, [
                "--output", "json",
                "material", "hlsl-code", "/Game/MF_Test",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["code"] == "MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET"
        assert data["details"]["asset_class"] == "MaterialFunction"

    @patch("cli_anything.unreal.core.script_runner.run_python_code")
    def test_material_shader_source_script_is_valid_python(self, mock_run):
        from cli_anything.unreal.core.materials import get_material_shader_source

        mock_run.return_value = {
            "material": "/Game/Drone/MA_Glow.MA_Glow",
            "shader_count": 0,
            "shaders": [],
            "output_dir": "F:/Proj/Saved/CliAnything/MA_Glow_shaders",
            "source": "plugin",
        }

        get_material_shader_source(MagicMock(), "/Game/Drone/MA_Glow")

        script = mock_run.call_args.args[1]
        assert "mat, loaded_asset_path, tried_asset_paths = _cli_load_material" in script
        assert '"material": loaded_asset_path' in script
        assert '"shader_cache_refresh": "changed"' in script
        assert "if not shaders:" in script
        compile(script, "<material-shader-source-script>", "exec")

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_material_shader_source_cli_promotes_empty_extraction_error(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "error": "Shader source extraction returned no shaders after refreshing changed shader files.",
            "material": "/Game/M_Test.M_Test",
            "output_dir": "F:/Proj/Saved/CliAnything/M_Test_shaders",
        }

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.material.require_editor",
            return_value=MagicMock(),
        ), patch("cli_anything.unreal.commands.material.require_project"):
            result = runner.invoke(cli, [
                "--output", "json",
                "material", "shader-source", "/Game/M_Test",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "MATERIAL_SHADER_SOURCE_FAILED"
        assert data["details"]["material"] == "/Game/M_Test.M_Test"

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_dump_hlsl_rejects_inactive_platform_before_recompile(self, mock_exec, tmp_path):
        from cli_anything.unreal.core.materials import get_material_hlsl

        (tmp_path / "Saved" / "ShaderDebugInfo" / "PCD3D_SM6").mkdir(parents=True)
        mock_exec.return_value = {"active_platform": "PCD3D_SM6"}
        mock_api = MagicMock()

        result = get_material_hlsl(
            mock_api,
            "/Game/M_Test",
            project_dir=str(tmp_path),
            platform="sm5",
        )

        assert result["code"] == "SHADER_PLATFORM_NOT_ACTIVE"
        assert result["requested_platform"] == "PCD3D_SM5"
        assert result["active_platform"] == "PCD3D_SM6"
        assert result["available_platforms"] == ["PCD3D_SM6"]
        mock_api.get_cvar.assert_not_called()
        mock_api.exec_console.assert_not_called()
        from cli_anything.unreal.core.script_runner import SavePolicy

        assert mock_exec.call_args.kwargs["save_policy"] is SavePolicy.NEVER

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_dump_hlsl_resolves_android_preview_platform_alias(self, mock_exec, tmp_path):
        from cli_anything.unreal.core.materials import get_material_hlsl

        mock_exec.return_value = {"active_platform": "PCD3D_SM6"}
        mock_api = MagicMock()

        result = get_material_hlsl(
            mock_api,
            "/Game/M_Test",
            project_dir=str(tmp_path),
            platform="vulkan_sm5_android_preview",
        )

        assert result["code"] == "SHADER_PLATFORM_NOT_ACTIVE"
        assert result["requested_platform"] == "VULKAN_SM5_ANDROID_Preview"
        assert result["active_platform"] == "PCD3D_SM6"
        mock_api.get_cvar.assert_not_called()

    @patch("cli_anything.unreal.core.materials.time.sleep")
    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_dump_hlsl_recompile_preserves_dirty_state(self, mock_exec, mock_sleep, tmp_path):
        from cli_anything.unreal.core.materials import get_material_hlsl

        dump_dir = (
            tmp_path
            / "Saved"
            / "ShaderDebugInfo"
            / "PCD3D_SM6"
            / "M_Test_ABC"
            / "Default"
            / "FLocalVertexFactory"
            / "TBasePassPSFNoLightMapPolicy"
        )

        def run_script(_api, script_template, **kwargs):
            if "get_active_shader_platform" in script_template:
                return {"active_platform": "PCD3D_SM6"}
            dump_dir.mkdir(parents=True)
            (dump_dir / "M_Test.usf").write_text(
                "void CalcPixelMaterialInputs()\n{\n}\n",
                encoding="utf-8",
            )
            return {
                "status": "ok",
                "package_dirty_before": False,
                "package_dirty_after_recompile": True,
                "package_dirty_restored": True,
            }

        mock_exec.side_effect = run_script
        mock_api = MagicMock()
        mock_api.get_cvar.return_value = "0"

        result = get_material_hlsl(
            mock_api,
            "/Game/M_Test",
            project_dir=str(tmp_path),
            platform="sm6",
        )

        assert result["platform"] == "PCD3D_SM6"
        assert result["shader_count"] == 1
        assert "CalcPixelMaterialInputs" in result["material_code"]
        assert len(mock_exec.call_args_list) == 2
        from cli_anything.unreal.core.script_runner import SavePolicy

        assert all(
            call.kwargs["save_policy"] is SavePolicy.NEVER
            for call in mock_exec.call_args_list
        )
        mock_api.set_cvar.assert_any_call("r.DumpShaderDebugInfo", "1")
        mock_api.set_cvar.assert_any_call("r.DumpShaderDebugInfo", "0")
        mock_api.exec_console.assert_not_called()

    @patch("cli_anything.unreal.core.materials.time.sleep")
    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_dump_hlsl_material_instance_uses_exact_shader_map_directory(
        self,
        mock_exec,
        mock_sleep,
        tmp_path,
    ):
        from cli_anything.unreal.core.materials import get_material_hlsl

        debug_base = tmp_path / "Saved" / "ShaderDebugInfo" / "PCD3D_SM6"
        wrong_dir = debug_base / "M_Base_WRONG" / "Default" / "FLocalVertexFactory" / "TBasePassPS"
        exact_dir = debug_base / "M_Base_EXACT" / "Default" / "FLocalVertexFactory" / "TBasePassPS"
        wrong_dir.mkdir(parents=True)
        exact_dir.mkdir(parents=True)
        (wrong_dir / "Wrong.usf").write_text("wrong", encoding="utf-8")
        (exact_dir / "Exact.usf").write_text(
            "void CalcPixelMaterialInputs()\n{\n}\n",
            encoding="utf-8",
        )
        mock_exec.side_effect = [
            {"active_platform": "PCD3D_SM6"},
            {
                "status": "ok",
                "material": "/Game/MI_Test.MI_Test",
                "requested_asset_class": "MaterialInstanceConstant",
                "shader_map_material": "/Game/M_Base.M_Base",
                "shader_dump_name": "M_Base",
                "shader_debug_group": "M_Base_EXACT/Default",
                "resolved_from_material_instance": True,
                "package_dirty_restored": True,
            },
        ]
        mock_api = MagicMock()
        mock_api.get_cvar.return_value = "0"

        result = get_material_hlsl(
            mock_api,
            "/Game/MI_Test",
            project_dir=str(tmp_path),
            platform="sm6",
            wait_timeout=0,
        )

        assert Path(result["dump_dir"]).name == "M_Base_EXACT"
        assert result["material"] == "/Game/MI_Test.MI_Test"
        assert result["shader_map_material"] == "/Game/M_Base.M_Base"
        assert result["shader_dump_name"] == "M_Base"
        assert result["shader_debug_group"] == "M_Base_EXACT/Default"
        assert result["resolved_from_material_instance"] is True
        assert "CalcPixelMaterialInputs" in result["material_code"]
        assert mock_sleep.call_args_list == [call(0.5), call(2)]

    @patch("cli_anything.unreal.core.materials.time.sleep")
    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_dump_hlsl_material_instance_uses_ue4_base_material_directory(
        self,
        mock_exec,
        mock_sleep,
        tmp_path,
    ):
        from cli_anything.unreal.core.materials import get_material_hlsl

        dump_dir = (
            tmp_path
            / "Saved"
            / "ShaderDebugInfo"
            / "PCD3D_SM5"
            / "M_Base"
            / "Default"
            / "FLocalVertexFactory"
            / "TBasePassPS"
        )
        dump_dir.mkdir(parents=True)
        (dump_dir / "Base.usf").write_text(
            "void CalcPixelMaterialInputs()\n{\n}\n",
            encoding="utf-8",
        )
        mock_exec.side_effect = [
            {"active_platform": "PCD3D_SM5"},
            {
                "status": "ok",
                "shader_map_material": "/Game/M_Base.M_Base",
                "shader_dump_name": "M_Base",
                "shader_debug_group": "",
                "resolved_from_material_instance": True,
                "package_dirty_restored": True,
            },
        ]
        mock_api = MagicMock()
        mock_api.get_cvar.return_value = "0"

        result = get_material_hlsl(
            mock_api,
            "/Game/MI_Test",
            project_dir=str(tmp_path),
            platform="sm5",
            wait_timeout=0,
        )

        assert Path(result["dump_dir"]).name == "M_Base"
        assert result["shader_map_material"] == "/Game/M_Base.M_Base"
        assert result["resolved_from_material_instance"] is True
        assert mock_sleep.call_args_list == [call(0.5), call(2)]

    @patch("cli_anything.unreal.core.materials.time.monotonic")
    @patch("cli_anything.unreal.core.materials.time.sleep")
    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_dump_hlsl_missing_dump_respects_wait_timeout(
        self,
        mock_exec,
        mock_sleep,
        mock_monotonic,
        tmp_path,
    ):
        from cli_anything.unreal.core.materials import get_material_hlsl

        mock_exec.side_effect = [
            {"active_platform": "PCD3D_SM6"},
            {"status": "ok", "package_dirty_restored": True},
        ]
        mock_monotonic.side_effect = [100.0, 100.0, 101.0, 102.0]
        mock_api = MagicMock()
        mock_api.get_cvar.return_value = "0"
        empty_candidate = (
            tmp_path
            / "Saved"
            / "ShaderDebugInfo"
            / "PCD3D_SM6"
            / "M_Test_EMPTY"
        )
        empty_candidate.mkdir(parents=True)

        result = get_material_hlsl(
            mock_api,
            "/Game/M_Test",
            project_dir=str(tmp_path),
            platform="sm6",
            wait_timeout=2.0,
        )

        assert result["code"] == "SHADER_DUMP_NOT_FOUND"
        assert result["wait_timeout_seconds"] == 2.0
        assert Path(result["search_root"]).parts[-3:] == (
            "Saved",
            "ShaderDebugInfo",
            "PCD3D_SM6",
        )
        assert result["searched_directory_count"] == 1
        assert result["candidate_count"] == 1
        assert result["candidate_directories"] == ["M_Test_EMPTY"]
        assert mock_sleep.call_args_list == [call(0.5), call(1.0), call(1.0)]
        mock_api.set_cvar.assert_any_call("r.DumpShaderDebugInfo", "0")

    @patch("cli_anything.unreal.core.materials.get_material_hlsl")
    def test_dump_hlsl_cli_promotes_missing_dump_error(self, mock_hlsl, tmp_path):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_hlsl.return_value = {
            "error": "No shader dump found for 'M_Test' on platform 'PCD3D_SM6'.",
            "code": "SHADER_DUMP_NOT_FOUND",
            "available_platforms": ["PCD3D_SM6", "VM"],
        }
        output_path = tmp_path / "missing.usf"

        with patch(
            "cli_anything.unreal.commands.material.require_editor",
            return_value=MagicMock(),
        ), patch("cli_anything.unreal.commands.material.require_project"):
            result = CliRunner().invoke(cli, [
                "--output", "json",
                "material", "dump-hlsl", "/Game/M_Test",
                "--output", str(output_path),
                "--wait-timeout", "2.5",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "SHADER_DUMP_NOT_FOUND"
        assert data["details"]["available_platforms"] == ["PCD3D_SM6", "VM"]
        assert not output_path.exists()
        assert mock_hlsl.call_args.kwargs["wait_timeout"] == 2.5

    @patch("cli_anything.unreal.core.materials.get_material_hlsl")
    def test_dump_hlsl_cli_reports_output_write_failure(self, mock_hlsl, tmp_path):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_hlsl.return_value = {
            "material": "/Game/M_Test.M_Test",
            "platform": "PCD3D_SM6",
            "shader_count": 1,
            "shaders": [{"pass": "TBasePassPS", "code": "shader code"}],
            "material_code": "material code",
        }
        output_path = tmp_path / "missing-directory" / "dump.usf"

        with patch(
            "cli_anything.unreal.commands.material.require_editor",
            return_value=MagicMock(),
        ), patch("cli_anything.unreal.commands.material.require_project"):
            result = CliRunner().invoke(cli, [
                "--output", "json",
                "material", "dump-hlsl", "/Game/M_Test",
                "--output", str(output_path),
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "SHADER_DUMP_WRITE_FAILED"

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_get_material_info_reports_bridge_failure(self, mock_exec_script):
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api()
        mock_exec_script.return_value = {
            "error": "Native material inspection requires CliAnythingBridge 1.29 or newer.",
            "code": "MATERIAL_INFO_BRIDGE_REQUIRED",
        }

        result = get_material_info(mock_api, "/Game/TestMat")

        assert result["code"] == "MATERIAL_INFO_BRIDGE_REQUIRED"
        assert "CliAnythingBridge 1.29" in result["error"]

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_get_material_info_raises_when_editor_drops_during_detail(self, mock_exec_script):
        """A transport failure must not become a successful empty inspection."""
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api()
        mock_exec_script.side_effect = ConnectionError(
            "HTTPConnectionPool: WinError 10061 connection actively refused"
        )

        with pytest.raises(ConnectionError, match="WinError 10061"):
            get_material_info(mock_api, "/Engine/EngineDebugMaterials/GeomMaterial")

    def test_material_bridge_call_raises_when_editor_drops(self):
        from cli_anything.unreal.core.materials import _call_material_info_bridge

        mock_api = self._make_mock_api()
        mock_api.call_function.return_value = {
            "error": "HTTPConnectionPool: WinError 10061 connection actively refused",
        }
        mock_api.is_alive.return_value = False

        with pytest.raises(ConnectionError, match="WinError 10061"):
            _call_material_info_bridge(mock_api, "/Game/TestMat")

    def test_material_info_loads_asset_before_native_bridge_call(self):
        from cli_anything.unreal.core.materials import _call_material_info_bridge

        mock_api = self._make_mock_api()
        mock_api.call_function.side_effect = [
            {"ReturnValue": "/Game/TestMat.TestMat"},
            {"ReturnValue": json.dumps({
                "status": "ok",
                "name": "TestMat",
                "class": "Material",
                "node_count": 0,
            })},
        ]

        result = _call_material_info_bridge(mock_api, "/Game/TestMat")

        assert result["status"] == "ok"
        load_call, bridge_call = mock_api.call_function.call_args_list
        assert load_call.args[1] == "LoadAsset"
        assert load_call.args[2] == {"AssetPath": "/Game/TestMat"}
        assert bridge_call.args[1] == "GetMaterialInfo"
        assert bridge_call.args[2] == {"Asset": "/Game/TestMat.TestMat"}

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_analyze_material_structure(self, mock_exec_script):
        """Test that analyze_material returns correct structure."""
        from cli_anything.unreal.core.materials import analyze_material

        mock_exec_script.return_value = {"error": "timeout"}

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "TestMat", "Path": "/Game/TestMat.TestMat",
                            "Class": "/Script/Engine.Material",
                            "Metadata": {"BlendMode": "BLEND_Opaque", "ShadingModel": "MSM_DefaultLit"}}]
            },
            describe={
                "Properties": [{"Name": "BlendMode", "Type": "EBlendMode"}],
                "Functions": [],
            },
        )

        from cli_anything.unreal.errors import UeCliError

        with pytest.raises(UeCliError) as exc_info:
            analyze_material(mock_api, "/Game/TestMat")

        assert exc_info.value.code == "MATERIAL_ANALYZE_FAILED"
        assert exc_info.value.message == "timeout"

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_analyze_cli_rejects_material_instance(self, mock_exec_script):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.return_value = {
            "name": "MI_Test",
            "path": "/Game/MI_Test.MI_Test",
            "class": "MaterialInstanceConstant",
            "parent": "/Game/M_Master.M_Master",
            "scalar_parameters": [{"name": "Roughness", "value": 0.5}],
        }

        with patch(
            "cli_anything.unreal.commands.material.require_editor",
            return_value=self._make_mock_api(),
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json", "material", "analyze", "/Game/MI_Test",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "MATERIAL_ANALYZE_UNSUPPORTED_CLASS"
        assert data["details"]["asset_class"] == "MaterialInstanceConstant"
        assert data["details"]["parent"] == "/Game/M_Master.M_Master"
        assert data["details"]["supported_classes"] == ["Material"]
        assert "material info" in data["suggestion"]

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_analyze_material_high_textures(self, mock_exec_script):
        """Test detection of high texture sample count."""
        from cli_anything.unreal.core.materials import analyze_material

        mock_exec_script.return_value = {
            "node_count": 50,
            "texture_sample_count": 18,
            "textures": [{"name": f"T_{i}", "path": f"/Game/T_{i}", "node_type": "MaterialExpressionTextureSample"} for i in range(18)],
            "nodes": [{"type": "MaterialExpressionTextureSample", "name": f"TS_{i}"} for i in range(18)],
        }

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "HeavyMat", "Path": "/Game/HeavyMat.HeavyMat",
                            "Class": "/Script/Engine.Material",
                            "Metadata": {"BlendMode": "BLEND_Opaque"}}]
            },
            describe={"Properties": [], "Functions": []},
        )

        result = analyze_material(mock_api, "/Game/HeavyMat")
        assert "issues" in result
        assert "stats" in result
        assert result["stats"]["texture_sample_count"] == 18
        # Should detect >16 texture samples as an issue
        assert any("exceeds" in issue.lower() or "texture sample" in issue.lower()
                    for issue in result["issues"])

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_analyze_material_attributes_is_connected_output(self, mock_exec_script):
        """A Material Attributes master must not be reported as output-less."""
        from cli_anything.unreal.core.materials import analyze_material

        mock_exec_script.return_value = {
            "name": "M_Attributes",
            "path": "/Game/M_Attributes",
            "class": "Material",
            "use_material_attributes": True,
            "node_count": 1,
            "nodes": [
                {"type": "MaterialExpressionMaterialFunctionCall", "name": "SurfaceFn"},
            ],
            "material_outputs": {
                "MaterialAttributes": {
                    "node": "SurfaceFn",
                    "node_type": "MaterialExpressionMaterialFunctionCall",
                    "output": "MaterialAttributes",
                },
            },
            "edges": [],
            "textures": [],
            "texture_sample_count": 0,
        }
        mock_api = self._make_mock_api(
            assets={
                "Assets": [{
                    "Name": "M_Attributes",
                    "Path": "/Game/M_Attributes.M_Attributes",
                    "Class": "/Script/Engine.Material",
                    "Metadata": {},
                }],
            },
        )

        result = analyze_material(mock_api, "/Game/M_Attributes")

        assert result["info"]["use_material_attributes"] is True
        assert result["stats"]["connected_outputs"] == ["MaterialAttributes"]
        assert not any("No material output connections" in warning for warning in result["warnings"])

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_analyze_material_high_node_count(self, mock_exec_script):
        """Test detection of very high node count."""
        from cli_anything.unreal.core.materials import analyze_material

        mock_exec_script.return_value = {
            "node_count": 250,
            "texture_sample_count": 4,
            "textures": [],
            "nodes": [{"type": "MaterialExpressionAdd", "name": f"Add_{i}"} for i in range(250)],
        }

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "ComplexMat", "Path": "/Game/ComplexMat.ComplexMat",
                            "Class": "/Script/Engine.Material",
                            "Metadata": {}}]
            },
            describe={"Properties": [], "Functions": []},
        )

        result = analyze_material(mock_api, "/Game/ComplexMat")
        assert any("node count" in issue.lower() for issue in result["issues"])

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_analyze_material_missing_texture(self, mock_exec_script):
        """Test detection of missing texture references."""
        from cli_anything.unreal.core.materials import analyze_material

        mock_exec_script.return_value = {
            "node_count": 3,
            "texture_sample_count": 2,
            "textures": [
                {"name": "T_Good", "path": "/Game/T_Good", "node_type": "MaterialExpressionTextureSample"},
                {"name": None, "path": None, "node_type": "MaterialExpressionTextureSample"},
            ],
            "nodes": [],
        }

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "BrokenMat", "Path": "/Game/BrokenMat.BrokenMat",
                            "Class": "/Script/Engine.Material",
                            "Metadata": {}}]
            },
            describe={"Properties": [], "Functions": []},
        )

        result = analyze_material(mock_api, "/Game/BrokenMat")
        assert any("missing texture" in issue.lower() for issue in result["issues"])

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_analyze_material_error(self, mock_exec_script):
        """Test handling of material not found."""
        from cli_anything.unreal.core.materials import analyze_material
        from cli_anything.unreal.errors import UeCliError

        mock_exec_script.return_value = {"error": "timeout"}

        mock_api = self._make_mock_api(
            describe={"errorMessage": "Object not found"},
        )

        with pytest.raises(UeCliError) as exc_info:
            analyze_material(mock_api, "/Game/Missing")

        assert exc_info.value.code == "MATERIAL_ANALYZE_FAILED"
        assert exc_info.value.message == "timeout"

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_texture_list_with_nodes(self, mock_exec_script):
        """Test texture list merges node textures and parameter textures."""
        from cli_anything.unreal.core.materials import get_material_texture_list

        mock_exec_script.return_value = {
            "textures": [
                {"name": "T_Diffuse", "path": "/Game/T_Diffuse", "node_type": "MaterialExpressionTextureSample"},
            ],
            "texture_sample_count": 1,
            "texture_parameters": [
                {"name": "DetailTexture", "texture": "/Game/T_Detail"},
            ],
        }

        mock_api = self._make_mock_api(
            assets={
                "Assets": [{"Name": "M_Test", "Path": "/Game/M_Test.M_Test",
                            "Class": "/Script/Engine.Material", "Metadata": {}}]
            },
            describe={"Properties": [], "Functions": []},
        )

        result = get_material_texture_list(mock_api, "/Game/M_Test")
        assert "textures" in result
        assert len(result["textures"]) == 2  # 1 node texture + 1 parameter texture

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_info_cli(self, mock_exec_script):
        """Test material info via CLI with --json output."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.return_value = {
            "nodes": [
                {"type": "MaterialExpressionConstant", "name": "Const_0"},
            ],
            "node_count": 1,
        }

        runner = CliRunner()
        # This will fail to connect to editor — but we patch the whole chain
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_api = self._make_mock_api(
                assets={
                    "Assets": [{"Name": "M_Test", "Path": "/Game/M_Test.M_Test",
                                "Class": "/Script/Engine.Material", "Metadata": {}}]
                },
                describe={"Properties": [{"Name": "BlendMode", "Type": "EBlendMode"}], "Functions": []},
            )
            mock_editor.return_value = mock_api

            result = runner.invoke(cli, [
                "--output", "json", "material", "info", "/Game/M_Test",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            assert "nodes" in data["result"]
            assert data["result"]["node_count"] == 1

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_stats_cli_rejects_material_instance(self, mock_exec_script):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.return_value = {
            "name": "MI_Test",
            "path": "/Game/MI_Test.MI_Test",
            "class": "MaterialInstanceConstant",
            "parent": "/Game/M_Master.M_Master",
        }
        mock_api = self._make_mock_api(assets={"Assets": [{
            "Name": "MI_Test",
            "Path": "/Game/MI_Test.MI_Test",
            "Class": "/Script/Engine.MaterialInstanceConstant",
            "Metadata": {},
        }]})

        with patch(
            "cli_anything.unreal.commands.material.require_editor",
            return_value=mock_api,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json", "material", "get-stats", "/Game/MI_Test",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "MATERIAL_STATS_UNSUPPORTED_CLASS"
        assert data["details"]["asset_class"] == "MaterialInstanceConstant"
        assert data["details"]["parent"] == "/Game/M_Master.M_Master"
        assert "material info" in data["suggestion"]

    def test_material_info_is_direct_remote_control_read(self):
        from cli_anything.unreal.core.materials import get_material_info

        mock_api = self._make_mock_api()
        mock_api.call_function.return_value = {
            "ReturnValue": json.dumps({"nodes": [], "node_count": 0}),
        }

        result = get_material_info(mock_api, "/Game/M_Test")

        assert result["node_count"] == 0
        load_call, bridge_call = mock_api.call_function.call_args_list
        assert load_call.args[1] == "LoadAsset"
        assert bridge_call.args == (
            "/Script/CliAnythingBridge.Default__CliAnythingBridgeLibrary",
            "GetMaterialInfo",
            {"Asset": "/Game/M_Test.M_Test"},
        )
        assert bridge_call.kwargs == {"timeout": 30}

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_info_cli_reports_editor_unreachable_after_connection_drop(self, mock_exec_script):
        """The CLI exposes a mid-query disconnect as a top-level protocol error."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.side_effect = ConnectionError(
            "HTTPConnectionPool: WinError 10061 connection actively refused"
        )
        mock_api = self._make_mock_api(assets={"Assets": []})
        mock_api.is_alive.return_value = False

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor", return_value=mock_api):
            result = runner.invoke(cli, [
                "--output", "json", "material", "info",
                "/Engine/EngineDebugMaterials/GeomMaterial",
            ])

        assert result.exit_code == 4
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "EDITOR_UNREACHABLE"
        assert "WinError 10061" in data["message"]

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_info_cli_reads_material_function(self, mock_exec_script):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.return_value = {
            "name": "MF_Test",
            "path": "/Game/MF_Test.MF_Test",
            "class": "MaterialFunction",
            "nodes": [{"name": "Output", "type": "MaterialExpressionFunctionOutput"}],
            "node_count": 1,
            "edges": [],
            "function_inputs": [],
            "function_outputs": ["Output"],
        }
        mock_api = self._make_mock_api(assets={"Assets": [{
            "Name": "MF_Test",
            "Path": "/Game/MF_Test.MF_Test",
            "Class": "/Script/Engine.MaterialFunction",
            "Metadata": {},
        }]})

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor", return_value=mock_api):
            result = runner.invoke(cli, [
                "--output", "json", "material", "info", "/Game/MF_Test",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["result"]["node_count"] == 1
        assert data["result"]["function_outputs"] == ["Output"]

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_graph_cli_reads_material_function_topology(self, mock_exec_script):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.return_value = {
            "name": "MF_Test",
            "path": "/Game/MF_Test.MF_Test",
            "class": "MaterialFunction",
            "nodes": [
                {"name": "Input", "type": "MaterialExpressionFunctionInput"},
                {"name": "Output", "type": "MaterialExpressionFunctionOutput"},
            ],
            "node_count": 2,
            "edges": [{"from_node": "Input", "to_node": "Output", "to_input_index": 0}],
            "function_inputs": ["Input"],
            "function_outputs": ["Output"],
        }
        mock_api = self._make_mock_api(assets={"Assets": [{
            "Name": "MF_Test",
            "Path": "/Game/MF_Test.MF_Test",
            "Class": "/Script/Engine.MaterialFunction",
            "Metadata": {},
        }]})

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor", return_value=mock_api):
            result = runner.invoke(cli, [
                "--output", "json", "material", "get-graph", "/Game/MF_Test",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["result"]["connected_nodes"] == ["Input", "Output"]
        assert data["result"]["orphan_nodes"] == []

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_material_info_cli_reports_stale_bridge_for_material_function(self, mock_exec_script):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec_script.return_value = {
            "error": "Native material inspection requires CliAnythingBridge 1.29 or newer.",
            "code": "MATERIAL_INFO_BRIDGE_REQUIRED",
            "material": "/Game/MF_Test.MF_Test",
            "asset_class": "MaterialFunction",
            "suggestion": "Run 'editor plugin-upgrade', then retry material info.",
        }
        mock_api = self._make_mock_api(assets={"Assets": []})

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor", return_value=mock_api):
            result = runner.invoke(cli, [
                "--output", "json", "material", "info", "/Game/MF_Test",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["code"] == "MATERIAL_INFO_BRIDGE_REQUIRED"
        assert "plugin-upgrade" in data["suggestion"]


# ═══════════════════════════════════════════════════════════════════════
#  Test material connections (core/materials.py get_material_connections)
# ═══════════════════════════════════════════════════════════════════════


class TestMaterialConnections:
    """Tests for get_material_connections — BFS connected/orphan logic."""

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_connections_with_edges(self, mock_exec_script):
        """Nodes reachable from material outputs via edges are connected."""
        from cli_anything.unreal.core.materials import get_material_connections

        mock_api = MagicMock()
        # A -> B -> MaterialOutput.BaseColor, C is orphan
        mock_exec_script.return_value = {
            "name": "TestMat", "path": "/Game/TestMat", "class": "Material",
            "nodes": [
                {"type": "MaterialExpressionConstant", "name": "A"},
                {"type": "MaterialExpressionMultiply", "name": "B"},
                {"type": "MaterialExpressionConstant", "name": "C_Orphan"},
            ],
            "node_count": 3,
            "material_outputs": {
                "BaseColor": {"node": "B", "node_type": "MaterialExpressionMultiply", "output": ""},
            },
            "edges": [
                {"from_node": "A", "to_node": "B", "to_input_index": 0},
            ],
            "textures": [], "texture_sample_count": 0,
        }

        result = get_material_connections(mock_api, "/Game/TestMat")

        assert set(result["connected_nodes"]) == {"A", "B"}
        assert result["orphan_nodes"] == ["C_Orphan"]
        assert result["orphan_count"] == 1
        assert len(result["edges"]) == 1

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_connections_material_attributes_reaches_function_chain(self, mock_exec_script):
        """Material Attributes is a real material-output seed."""
        from cli_anything.unreal.core.materials import get_material_connections

        mock_api = MagicMock()
        mock_exec_script.return_value = {
            "name": "M_Attributes", "path": "/Game/M_Attributes", "class": "Material",
            "use_material_attributes": True,
            "nodes": [
                {"type": "MaterialExpressionMaterialFunctionCall", "name": "SurfaceFn"},
                {"type": "MaterialExpressionMaterialFunctionCall", "name": "LayerFn"},
                {"type": "MaterialExpressionConstant", "name": "Unused"},
            ],
            "node_count": 3,
            "material_outputs": {
                "MaterialAttributes": {
                    "node": "SurfaceFn",
                    "node_type": "MaterialExpressionMaterialFunctionCall",
                    "output": "MaterialAttributes",
                },
            },
            "edges": [
                {"from_node": "LayerFn", "to_node": "SurfaceFn", "to_input_index": 0},
            ],
            "textures": [], "texture_sample_count": 0,
        }

        result = get_material_connections(mock_api, "/Game/M_Attributes")

        assert set(result["connected_nodes"]) == {"LayerFn", "SurfaceFn"}
        assert result["orphan_nodes"] == ["Unused"]
        assert "MaterialAttributes" in result["material_outputs"]

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_connections_custom_output_node(self, mock_exec_script):
        """Custom output nodes (e.g. SLW) are treated as seeds."""
        from cli_anything.unreal.core.materials import get_material_connections

        mock_api = MagicMock()
        # D -> SLWOutput (custom output), no standard material_outputs
        mock_exec_script.return_value = {
            "name": "M_SLW", "path": "/Game/M_SLW", "class": "Material",
            "nodes": [
                {"type": "MaterialExpressionConstant3Vector", "name": "D"},
                {"type": "MaterialExpressionSingleLayerWaterMaterialOutput", "name": "SLWOutput"},
                {"type": "MaterialExpressionConstant", "name": "E_Orphan"},
            ],
            "node_count": 3,
            "material_outputs": {},
            "edges": [
                {"from_node": "D", "to_node": "SLWOutput", "to_input_index": 0},
            ],
            "textures": [], "texture_sample_count": 0,
        }

        result = get_material_connections(mock_api, "/Game/M_SLW")

        assert set(result["connected_nodes"]) == {"D", "SLWOutput"}
        assert result["orphan_nodes"] == ["E_Orphan"]

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_connections_deep_chain(self, mock_exec_script):
        """Multi-hop chains are fully traversed."""
        from cli_anything.unreal.core.materials import get_material_connections

        mock_api = MagicMock()
        # Tex -> Custom -> Multiply -> Output.WPO
        mock_exec_script.return_value = {
            "name": "M", "path": "/Game/M", "class": "Material",
            "nodes": [
                {"type": "MaterialExpressionTextureSample", "name": "Tex"},
                {"type": "MaterialExpressionCustom", "name": "Custom"},
                {"type": "MaterialExpressionMultiply", "name": "Mul"},
            ],
            "node_count": 3,
            "material_outputs": {
                "WorldPositionOffset": {"node": "Mul", "node_type": "MaterialExpressionMultiply", "output": ""},
            },
            "edges": [
                {"from_node": "Tex", "to_node": "Custom", "to_input_index": 0},
                {"from_node": "Custom", "to_node": "Mul", "to_input_index": 0},
            ],
            "textures": [], "texture_sample_count": 0,
        }

        result = get_material_connections(mock_api, "/Game/M")

        assert set(result["connected_nodes"]) == {"Tex", "Custom", "Mul"}
        assert result["orphan_nodes"] == []

    @patch("cli_anything.unreal.core.materials._call_material_info_bridge")
    def test_connections_no_edges_fallback(self, mock_exec_script):
        """When no edges data, only material_outputs seeds are connected."""
        from cli_anything.unreal.core.materials import get_material_connections

        mock_api = MagicMock()
        mock_exec_script.return_value = {
            "name": "M", "path": "/Game/M", "class": "Material",
            "nodes": [
                {"type": "MaterialExpressionConstant", "name": "X"},
                {"type": "MaterialExpressionConstant", "name": "Y"},
            ],
            "node_count": 2,
            "material_outputs": {
                "BaseColor": {"node": "X", "node_type": "MaterialExpressionConstant", "output": ""},
            },
            "textures": [], "texture_sample_count": 0,
        }

        result = get_material_connections(mock_api, "/Game/M")

        assert result["connected_nodes"] == ["X"]
        assert result["orphan_nodes"] == ["Y"]


# ═══════════════════════════════════════════════════════════════════════
#  Test material editing (core/materials.py editing functions)
# ═══════════════════════════════════════════════════════════════════════


class TestMaterialEditing:
    """Tests for material editing functions — mocked script execution."""

    def test_material_asset_path_candidates_normalizes_object_and_subobject_paths(self):
        from cli_anything.unreal.core.materials import _material_asset_path_candidates

        assert _material_asset_path_candidates("/Game/Env/M_Test") == [
            "/Game/Env/M_Test",
            "/Game/Env/M_Test.M_Test",
        ]
        assert _material_asset_path_candidates("/Game/Env/M_Test.M_Test") == [
            "/Game/Env/M_Test.M_Test",
            "/Game/Env/M_Test",
        ]
        assert _material_asset_path_candidates("/Game/Env/M_Test.M_Test:SubObject") == [
            "/Game/Env/M_Test.M_Test",
            "/Game/Env/M_Test",
        ]

    def test_native_edit_calls_bridge_then_saves_only_target(self):
        from cli_anything.unreal.core.materials import _call_material_edit_bridge

        api = MagicMock()
        api.call_function.side_effect = [
            {"ReturnValue": json.dumps({"status": "ok", "action": "recompile"})},
            {"ReturnValue": True},
        ]

        result = _call_material_edit_bridge(
            api,
            "RecompileMaterial",
            "/Game/Env/M_Test.M_Test",
        )

        assert result["saved"] is True
        assert result["saved_packages"] == ["/Game/Env/M_Test"]
        bridge_call, save_call = api.call_function.call_args_list
        assert bridge_call.args[1] == "RecompileMaterial"
        assert bridge_call.args[2] == {
            "Material": "/Game/Env/M_Test.M_Test",
        }
        assert bridge_call.kwargs["generate_transaction"] is True
        assert save_call.args[1] == "SaveAsset"
        assert save_call.args[2] == {
            "AssetToSave": "/Game/Env/M_Test",
            "bOnlyIfIsDirty": False,
        }

    def test_native_edit_reports_target_save_failure(self):
        from cli_anything.unreal.core.materials import _call_material_edit_bridge

        api = MagicMock()
        api.call_function.side_effect = [
            {"ReturnValue": json.dumps({"status": "ok", "action": "connect"})},
            {"ReturnValue": False},
        ]

        result = _call_material_edit_bridge(
            api,
            "ConnectMaterialExpressions",
            "/Game/M_Test",
        )

        assert result["code"] == "MATERIAL_SAVE_FAILED"
        assert result["edit_result"]["status"] == "ok"

    def test_native_edit_does_not_save_unapplied_node_properties(self):
        from cli_anything.unreal.core.materials import _call_material_edit_bridge

        api = MagicMock()
        api.call_function.return_value = {
            "ReturnValue": json.dumps({
                "error": "Requested material node properties could not be applied; no asset was saved.",
                "code": "MATERIAL_NODE_PROPERTIES_UNAPPLIED",
                "property_errors": ["missing_name: property not found"],
                "node_created": False,
                "saved": False,
            }),
        }

        result = _call_material_edit_bridge(
            api,
            "AddMaterialExpression",
            "/Game/M_Test",
        )

        assert result["code"] == "MATERIAL_NODE_PROPERTIES_UNAPPLIED"
        assert result["saved"] is False
        assert api.call_function.call_count == 1

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_add_node(self, mock_exec):
        from cli_anything.unreal.core.materials import add_material_node

        mock_exec.return_value = {
            "status": "ok",
            "action": "add_node",
            "material": "/Game/M_Test",
            "node": {"name": "Constant3Vector_0", "type": "MaterialExpressionConstant3Vector"},
        }

        api = MagicMock()
        result = add_material_node(api, "/Game/M_Test", "MaterialExpressionConstant3Vector", pos_x=100, pos_y=-200)

        assert result["status"] == "ok"
        assert result["node"]["type"] == "MaterialExpressionConstant3Vector"
        # Verify the native bridge receives typed values, not Python source.
        call_kwargs = mock_exec.call_args
        assert "MaterialExpressionConstant3Vector" in str(call_kwargs)

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_add_node_invalid_class(self, mock_exec):
        from cli_anything.unreal.core.materials import add_material_node

        mock_exec.return_value = {
            "error": "Failed to create expression. Class 'unreal.FakeClass' may not exist."
        }

        api = MagicMock()
        result = add_material_node(api, "/Game/M_Test", "FakeClass")
        assert "error" in result

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_rename_custom_input(self, mock_exec):
        from cli_anything.unreal.core.materials import rename_custom_input

        mock_exec.return_value = {
            "status": "ok",
            "action": "rename_custom_input",
            "material": "/Game/M_Test",
            "node": "Custom_0",
            "old_name": "OutlineWidth",
            "new_name": "OutlineWidthPx",
            "inputs_after": ["OutlineWidthPx", "Softness"],
            "code_updated": True,
        }

        api = MagicMock()
        result = rename_custom_input(
            api,
            "/Game/M_Test",
            "Custom_0",
            "OutlineWidth",
            "OutlineWidthPx",
        )

        assert result["status"] == "ok"
        assert result["new_name"] == "OutlineWidthPx"
        assert mock_exec.call_args.args[1] == "RenameMaterialCustomInput"
        assert mock_exec.call_args.args[3] == {
            "NodeName": "Custom_0",
            "OldName": "OutlineWidth",
            "NewName": "OutlineWidthPx",
            "bUpdateCode": True,
        }

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_rename_custom_input_without_code_update(self, mock_exec):
        from cli_anything.unreal.core.materials import rename_custom_input

        mock_exec.return_value = {
            "status": "ok",
            "action": "rename_custom_input",
            "code_updated": False,
        }

        api = MagicMock()
        result = rename_custom_input(
            api,
            "/Game/M_Test",
            "Custom_0",
            "Softness",
            "SoftnessPx",
            update_code=False,
        )

        assert result["status"] == "ok"
        assert mock_exec.call_args.args[3]["bUpdateCode"] is False

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_delete_node(self, mock_exec):
        from cli_anything.unreal.core.materials import delete_material_node

        mock_exec.return_value = {
            "status": "ok",
            "action": "delete_node",
            "material": "/Game/M_Test",
            "deleted_node": "Constant3Vector_0",
        }

        api = MagicMock()
        result = delete_material_node(api, "/Game/M_Test", "Constant3Vector_0")
        assert result["status"] == "ok"
        assert result["deleted_node"] == "Constant3Vector_0"

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_delete_node_not_found(self, mock_exec):
        from cli_anything.unreal.core.materials import delete_material_node

        mock_exec.return_value = {
            "error": "Node not found: BadName",
            "available_nodes": ["Constant3Vector_0", "TextureSample_0"],
        }

        api = MagicMock()
        result = delete_material_node(api, "/Game/M_Test", "BadName")
        assert "error" in result
        assert "available_nodes" in result

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_connect_nodes(self, mock_exec):
        from cli_anything.unreal.core.materials import connect_material_nodes

        mock_exec.return_value = {
            "status": "ok",
            "action": "connect",
            "from": "Constant3Vector_0",
            "to": "MaterialOutput.BaseColor",
        }

        api = MagicMock()
        result = connect_material_nodes(
            api, "/Game/M_Test",
            "Constant3Vector_0", "", "__material_output__", "BaseColor",
        )
        assert result["status"] == "ok"
        assert "BaseColor" in result["to"]

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_connect_between_expressions(self, mock_exec):
        from cli_anything.unreal.core.materials import connect_material_nodes

        mock_exec.return_value = {
            "status": "ok",
            "action": "connect",
            "from": "Multiply_0",
            "from_output": "",
            "to": "TextureSample_0",
            "to_input": "UVs",
        }

        api = MagicMock()
        result = connect_material_nodes(
            api, "/Game/M_Test",
            "Multiply_0", "", "TextureSample_0", "UVs",
        )
        assert result["status"] == "ok"
        assert result["to"] == "TextureSample_0"

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_disconnect_nodes(self, mock_exec):
        from cli_anything.unreal.core.materials import disconnect_material_nodes

        mock_exec.return_value = {
            "status": "ok",
            "action": "disconnect",
            "from": "Constant3Vector_0",
            "to": "MaterialOutput.BaseColor",
        }

        api = MagicMock()
        result = disconnect_material_nodes(
            api, "/Game/M_Test",
            "Constant3Vector_0", "", "__material_output__", "BaseColor",
        )
        assert result["status"] == "ok"
        assert mock_exec.call_args.args[1] == "DisconnectMaterialOutput"
        assert mock_exec.call_args.args[3] == {"PropertyName": "BaseColor"}

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_disconnect_between_expressions_uses_bridge(self, mock_exec):
        from cli_anything.unreal.core.materials import disconnect_material_nodes

        mock_exec.return_value = {
            "status": "ok",
            "action": "disconnect",
            "from": "Constant_0",
            "to": "Multiply_0",
            "to_input": "A",
        }

        api = MagicMock()
        result = disconnect_material_nodes(
            api, "/Game/M_Test",
            "Constant_0", "", "Multiply_0", "A",
        )

        assert result["status"] == "ok"
        assert mock_exec.call_args.args[1] == "DisconnectMaterialExpression"
        assert mock_exec.call_args.args[3] == {
            "ToNode": "Multiply_0",
            "ToInputName": "A",
        }

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_set_param_scalar(self, mock_exec):
        from cli_anything.unreal.core.materials import set_material_param

        mock_exec.return_value = {
            "status": "ok",
            "action": "set_param",
            "material": "/Game/MI_Test",
            "param": "Roughness",
            "type": "scalar",
            "value": 0.5,
        }

        api = MagicMock()
        result = set_material_param(api, "/Game/MI_Test", "Roughness", "0.5", "scalar")
        assert result["status"] == "ok"
        assert result["value"] == 0.5

    @patch("cli_anything.unreal.core.script_runner.run_python_code")
    def test_set_param_saves_material_instance_package(self, mock_run):
        from cli_anything.unreal.core.materials import set_material_param

        mock_run.return_value = {"status": "ok", "action": "set_param"}

        set_material_param(MagicMock(), "/Game/MI_Test", "Roughness", "0.5", "scalar")

        script = mock_run.call_args.args[1]
        assert "_cli_load_material" in script
        assert "set_return" in script
        assert "set_return_authoritative" in script
        assert "readback_match" in script
        assert "applied" in script
        assert "MATERIAL_PARAM_READBACK_MISMATCH" in script
        assert "MATERIAL_PARAM_NOT_FOUND" in script
        assert "get_material_instance_scalar_parameter_value" in script
        assert "get_material_instance_vector_parameter_value" in script
        assert "get_material_instance_texture_parameter_value" in script
        assert "math.isclose" in script
        assert "save_loaded_asset(mat" not in script
        assert "save_dirty_packages(False, True)" not in script
        compile(script, "<material-set-param-script>", "exec")

        from cli_anything.unreal.core.script_runner import SavePolicy

        assert mock_run.call_args.kwargs["save_policy"] is SavePolicy.TARGET_PACKAGES
        assert mock_run.call_args.kwargs["target_packages"] == ["/Game/MI_Test"]

    @patch("cli_anything.unreal.core.script_runner.run_python_code")
    def test_get_param_uses_material_resolver(self, mock_run):
        from cli_anything.unreal.core.materials import get_material_param

        mock_run.return_value = {"status": "ok", "action": "get_param"}

        get_material_param(MagicMock(), "/Game/Env/MI_Test.MI_Test", "Roughness")

        script = mock_run.call_args.args[1]
        assert 'material_path = "/Game/Env/MI_Test.MI_Test"' in script
        assert 'material_candidates = ["/Game/Env/MI_Test.MI_Test", "/Game/Env/MI_Test"]' in script
        assert "mat, loaded_asset_path, tried_asset_paths = _cli_load_material" in script
        assert "unreal.EditorAssetLibrary.load_asset(material_path)" not in script
        assert '"material": loaded_asset_path' in script
        assert "mel.get_scalar_parameter_names(mat)" in script
        assert "mel.get_material_instance_scalar_parameter_value(mat, scalar_name)" in script
        assert "mel.get_vector_parameter_names(mat)" in script
        assert "mel.get_material_instance_vector_parameter_value(mat, vector_name)" in script
        assert "mel.get_texture_parameter_names(mat)" in script
        assert "mel.get_material_instance_texture_parameter_value(mat, texture_name)" in script
        assert "mel.get_static_switch_parameter_names(mat)" in script
        assert "mel.get_material_instance_static_switch_parameter_value(mat, static_switch_name)" in script
        assert 'mat.get_editor_property("scalar_parameter_values")' not in script
        compile(script, "<material-get-param-script>", "exec")

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_set_param_vector(self, mock_exec):
        from cli_anything.unreal.core.materials import set_material_param

        mock_exec.return_value = {
            "status": "ok",
            "action": "set_param",
            "material": "/Game/MI_Test",
            "param": "BaseColor",
            "type": "vector",
            "value": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0},
        }

        api = MagicMock()
        result = set_material_param(
            api, "/Game/MI_Test", "BaseColor",
            '{"r":1,"g":0,"b":0,"a":1}', "vector",
        )
        assert result["status"] == "ok"
        assert result["value"]["r"] == 1.0

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_set_param_texture(self, mock_exec):
        from cli_anything.unreal.core.materials import set_material_param

        mock_exec.return_value = {
            "status": "ok",
            "action": "set_param",
            "material": "/Game/MI_Test",
            "param": "DiffuseTexture",
            "type": "texture",
            "value": "/Game/Textures/T_Diffuse",
        }

        api = MagicMock()
        result = set_material_param(
            api, "/Game/MI_Test", "DiffuseTexture",
            "/Game/Textures/T_Diffuse", "texture",
        )
        assert result["status"] == "ok"

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_set_param_on_non_mi(self, mock_exec):
        from cli_anything.unreal.core.materials import set_material_param

        mock_exec.return_value = {
            "error": "Asset is not a MaterialInstanceConstant (set-param only works on MI): /Game/M_Test"
        }

        api = MagicMock()
        result = set_material_param(api, "/Game/M_Test", "Roughness", "0.5", "scalar")
        assert "error" in result
        assert "MaterialInstanceConstant" in result["error"]

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_recompile(self, mock_exec):
        from cli_anything.unreal.core.materials import recompile_material

        mock_exec.return_value = {
            "status": "ok",
            "action": "recompile",
            "material": "/Game/M_Test",
        }

        api = MagicMock()
        result = recompile_material(api, "/Game/M_Test")
        assert result["status"] == "ok"
        mock_exec.assert_called_once_with(
            api,
            "RecompileMaterial",
            "/Game/M_Test",
            timeout=120,
        )

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_recompile_uses_material_object_path(self, mock_edit):
        from cli_anything.unreal.core.materials import recompile_material

        mock_edit.return_value = {"status": "ok", "action": "recompile"}
        api = MagicMock()

        recompile_material(api, "/Game/Env/M_Test.M_Test")

        mock_edit.assert_called_once_with(
            api,
            "RecompileMaterial",
            "/Game/Env/M_Test.M_Test",
            timeout=120,
        )

    @patch("cli_anything.unreal.core.script_runner.run_python_code")
    def test_get_errors_uses_material_resolver(self, mock_run):
        from cli_anything.unreal.core.materials import get_material_errors

        mock_run.return_value = {"errors": [], "has_errors": False}

        get_material_errors(MagicMock(), "/Game/Env/M_Test.M_Test")

        script = mock_run.call_args.args[1]
        assert "_cli_load_material" in script
        assert "bridge.get_material_compile_status(mat)" in script
        assert '"material": loaded_asset_path' in script

    @patch("cli_anything.unreal.core.materials.ensure_plugin_deployed")
    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_get_errors_does_not_deploy_bridge(self, mock_exec, mock_deploy):
        from cli_anything.unreal.core.materials import get_material_errors

        mock_exec.return_value = {"errors": [], "has_errors": False, "source": "plugin"}

        result = get_material_errors(MagicMock(), "/Game/M_Test", project_dir="F:/RXGame")

        assert result["source"] == "plugin"
        mock_deploy.assert_not_called()

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_recompile_not_found(self, mock_exec):
        from cli_anything.unreal.core.materials import recompile_material

        mock_exec.return_value = {"error": "Material not found: /Game/Missing"}

        api = MagicMock()
        result = recompile_material(api, "/Game/Missing")
        assert "error" in result

    # ── CLI command tests ──────────────────────────────────────────────

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_add_node_cli(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "status": "ok",
            "action": "add_node",
            "material": "/Game/M_Test",
            "node": {"name": "Constant_0", "type": "MaterialExpressionConstant"},
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "add-node", "/Game/M_Test",
                "--type", "MaterialExpressionConstant",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            assert data["result"]["status"] == "ok"
            assert data["result"]["node"]["type"] == "MaterialExpressionConstant"

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_add_node_cli_rejects_unapplied_properties_without_saving(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "error": "Requested material node properties could not be applied; no asset was saved.",
            "code": "MATERIAL_NODE_PROPERTIES_UNAPPLIED",
            "property_errors": ["missing_name: property not found"],
            "node_created": False,
            "saved": False,
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "add-node", "/Game/M_Test",
                "--type", "MaterialExpressionScalarParameter",
                "--set", "missing_name=DepthBiasMultiplier",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "MATERIAL_NODE_PROPERTIES_UNAPPLIED"
        assert data["details"]["saved"] is False

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_rename_custom_input_cli(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "status": "ok",
            "action": "rename_custom_input",
            "material": "/Game/M_Test",
            "node": "Custom_0",
            "old_name": "OutlineWidth",
            "new_name": "OutlineWidthPx",
            "code_updated": True,
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "rename-custom-input", "/Game/M_Test",
                "--node", "Custom_0",
                "--from", "OutlineWidth",
                "--to", "OutlineWidthPx",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            assert data["result"]["status"] == "ok"
            assert data["result"]["new_name"] == "OutlineWidthPx"

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_connect_cli(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "status": "ok",
            "action": "connect",
            "from": "Constant3Vector_0",
            "to": "MaterialOutput.BaseColor",
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "connect", "/Game/M_Test",
                "--from", "Constant3Vector_0",
                "--to", "__material_output__",
                "--to-input", "BaseColor",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            assert data["result"]["status"] == "ok"
            assert "BaseColor" in data["result"]["to"]

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_set_param_cli(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "status": "ok",
            "action": "set_param",
            "param": "Roughness",
            "type": "scalar",
            "value": 0.8,
            "readback_value": 0.800000011920929,
            "readback_match": True,
            "applied": True,
            "verification": "effective_parameter_readback",
            "set_return": False,
            "set_return_authoritative": False,
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "set-param", "/Game/MI_Test",
                "--name", "Roughness",
                "--value", "0.8",
                "--type", "scalar",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            assert data["result"]["status"] == "ok"
            assert data["result"]["applied"] is True
            assert data["result"]["readback_match"] is True
            assert data["result"]["set_return"] is False
            assert data["result"]["set_return_authoritative"] is False

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_set_param_cli_readback_mismatch_is_top_level_error(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "status": "error",
            "error": "Material parameter readback did not match requested value; asset was not saved.",
            "code": "MATERIAL_PARAM_READBACK_MISMATCH",
            "action": "set_param",
            "param": "Roughness",
            "type": "scalar",
            "value": 0.8,
            "readback_value": 0.5,
            "readback_match": False,
            "applied": False,
            "verification": "effective_parameter_readback",
            "set_return": False,
            "set_return_authoritative": False,
            "saved": False,
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "set-param", "/Game/MI_Test",
                "--name", "Roughness",
                "--value", "0.8",
                "--type", "scalar",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "MATERIAL_PARAM_READBACK_MISMATCH"
        assert data["details"]["readback_match"] is False
        assert data["details"]["applied"] is False
        assert data["details"]["saved"] is False

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_get_param_cli_error_is_top_level_error(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "error": "Material not found: /Game/Missing",
            "tried": ["/Game/Missing", "/Game/Missing.Missing"],
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "get-param", "/Game/Missing",
                "--name", "Roughness",
            ])

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "MATERIAL_PARAM_FAILED"
        assert data["message"] == "Material not found: /Game/Missing"
        assert data["details"]["tried"] == ["/Game/Missing", "/Game/Missing.Missing"]

    @patch("cli_anything.unreal.core.materials._call_material_edit_bridge")
    def test_recompile_cli(self, mock_exec):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_exec.return_value = {
            "status": "ok",
            "action": "recompile",
            "material": "/Game/M_Test",
        }

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.material.require_editor") as mock_editor:
            mock_editor.return_value = MagicMock()
            result = runner.invoke(cli, [
                "--output", "json", "material", "recompile", "/Game/M_Test",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            assert data["result"]["status"] == "ok"


# ═══════════════════════════════════════════════════════════════════════
#  Test screenshot.py (mocked)
# ═══════════════════════════════════════════════════════════════════════


class TestMaterialErrorsPlugin:
    """Tests for get_material_errors — plugin-based path."""

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    @patch("cli_anything.unreal.core.materials.ensure_plugin_deployed")
    def test_plugin_returns_errors(self, mock_deploy, mock_exec):
        """Plugin path returns compile errors."""
        from cli_anything.unreal.core.materials import get_material_errors

        mock_deploy.return_value = {"deployed": True, "action": "already_up_to_date"}
        mock_exec.return_value = {
            "errors": ["Type mismatch on BaseColor input"],
            "warnings": [],
            "material": "/Game/M_Test",
            "has_errors": True,
            "source": "plugin",
        }

        result = get_material_errors(MagicMock(), "/Game/M_Test", project_dir="/tmp/proj")
        assert result["has_errors"] is True
        assert len(result["errors"]) == 1
        assert result["source"] == "plugin"

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    @patch("cli_anything.unreal.core.materials.ensure_plugin_deployed")
    def test_plugin_no_errors(self, mock_deploy, mock_exec):
        """Plugin path returns empty errors for clean material."""
        from cli_anything.unreal.core.materials import get_material_errors

        mock_deploy.return_value = {"deployed": True, "action": "already_up_to_date"}
        mock_exec.return_value = {
            "resources": [{"resource_present": True, "compilation_finished": True, "shader_map_valid": True}],
            "errors": [],
            "warnings": [],
            "material": "/Game/M_Clean",
            "has_errors": False,
            "source": "plugin",
        }

        result = get_material_errors(MagicMock(), "/Game/M_Clean", project_dir="/tmp/proj")
        assert result["has_errors"] is False
        assert result["errors"] == []

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    @patch("cli_anything.unreal.core.materials.ensure_plugin_deployed")
    def test_plugin_not_loaded_returns_error(self, mock_deploy, mock_exec):
        """Returns error message when plugin is deployed but not loaded."""
        from cli_anything.unreal.core.materials import get_material_errors

        mock_deploy.return_value = {"deployed": True, "action": "already_up_to_date"}
        mock_exec.return_value = {
            "error": "CliAnythingBridgeLibrary is unavailable in this editor",
            "bridge_missing": True,
        }

        result = get_material_errors(MagicMock(), "/Game/M_Test", project_dir="/tmp/proj")
        assert "error" in result
        assert "not loaded" in result["error"]
        assert "plugin-upgrade" in result["error"]
        assert "recompile" in result["error"]

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    def test_material_function_type_error_is_not_misreported_as_missing_plugin(self, mock_exec):
        """A bridge call type error must retain its real failure classification."""
        from cli_anything.unreal.core.materials import get_material_errors

        bridge_error = (
            "CliAnythingBridgeLibrary: Failed to convert parameter 'material' when calling "
            "function 'CliAnythingBridgeLibrary.GetMaterialCompileErrors'"
        )
        mock_exec.return_value = {"error": bridge_error}

        result = get_material_errors(MagicMock(), "/Game/MF_Test", project_dir="/tmp/proj")

        assert result == {"error": bridge_error}
        assert "not loaded" not in result["error"]

    @patch("cli_anything.unreal.core.materials._exec_material_script")
    @patch("cli_anything.unreal.core.materials.ensure_plugin_deployed")
    def test_get_errors_ignores_deploy_failure(self, mock_deploy, mock_exec):
        """Read-only get-errors never touches plugin deployment state."""
        from cli_anything.unreal.core.materials import get_material_errors

        mock_deploy.return_value = {"deployed": False, "error": "Source not found"}
        mock_exec.return_value = {"errors": [], "warnings": [], "has_errors": False, "source": "plugin"}

        result = get_material_errors(MagicMock(), "/Game/M_Test", project_dir="/tmp/proj")
        assert result["source"] == "plugin"
        mock_deploy.assert_not_called()

@pytest.mark.parametrize(
    "resource, state, complete, succeeded, has_errors",
    [
        ({"resource_present": False}, "resource_absent", False, None, None),
        ({"resource_present": True, "compilation_finished": False,
          "shader_map_valid": True}, "not_finished", False, None, None),
        ({"resource_present": True, "compilation_finished": True,
          "shader_map_valid": False}, "shader_map_unavailable", False, None, None),
        ({"resource_present": True, "compilation_finished": False,
          "shader_map_valid": False, "errors": ["bad shader"]},
         "not_finished", False, None, True),
        ({"resource_present": True, "compilation_finished": True,
          "shader_map_valid": True}, "complete", True, True, False),
        ({"resource_present": True, "compilation_finished": True,
          "shader_map_valid": False, "errors": ["bad shader"]},
         "errors", True, False, True),
    ],
)
def test_compile_resource_verdict(resource, state, complete, succeeded, has_errors):
    from cli_anything.unreal.core.materials import get_material_errors

    evidence = {"resources": [resource], "errors": resource.get("errors", []),
                "checked_platform": "VULKAN_ES3_1_ANDROID_Preview"}
    with patch("cli_anything.unreal.core.materials._exec_material_script", return_value=evidence):
        result = get_material_errors(MagicMock(), "/Game/M_Test")
    assert result["resources"][0]["compile_state"] == state
    assert result["compilation_complete"] is complete
    assert result["compile_succeeded"] is succeeded
    assert result["has_errors"] is has_errors
    assert result["checked_platform"] == "VULKAN_ES3_1_ANDROID_Preview"


def test_compile_empty_resources_never_certify_success():
    from cli_anything.unreal.core.materials import get_material_errors

    with patch("cli_anything.unreal.core.materials._exec_material_script", return_value={"errors": []}):
        result = get_material_errors(MagicMock(), "/Game/M_Test")
    assert result["compilation_complete"] is False
    assert result["compile_succeeded"] is None
    assert result["has_errors"] is None


def test_compile_mixed_resources_remain_unknown_with_observed_errors():
    from cli_anything.unreal.core.materials import get_material_errors

    evidence = {"errors": ["bad shader"], "resources": [
        {"resource_present": True, "compilation_finished": True, "errors": ["bad shader"]},
        {"resource_present": False},
    ]}
    with patch("cli_anything.unreal.core.materials._exec_material_script", return_value=evidence):
        result = get_material_errors(MagicMock(), "/Game/M_Test")
    assert result["has_errors"] is True
    assert result["compile_succeeded"] is None
    assert result["compilation_complete"] is False


@pytest.mark.parametrize("exporter", ["hlsl", "shader"])
@pytest.mark.parametrize("version", ["1.39", "unknown"])
def test_shader_export_rejects_old_or_unknown_bridge(exporter, version):
    import types
    from cli_anything.unreal.core import materials

    class Material:
        pass

    mat = Material()
    bridge = MagicMock()
    bridge.get_plugin_version.return_value = version
    fake_unreal = types.SimpleNamespace(MaterialInterface=Material, CliAnythingBridgeLibrary=bridge)
    template = (materials._PLUGIN_GET_HLSL_CODE_SCRIPT if exporter == "hlsl"
                else materials._PLUGIN_GET_SHADER_SOURCE_SCRIPT)
    namespace = {"_cli_load_material": lambda *args: (mat, "/Game/MI_Test", [])}
    with patch.dict("sys.modules", {"unreal": fake_unreal}):
        exec(template.format(material_path="/Game/MI_Test", material_path_candidates_json="[]",
                             mat_name="MI_Test"), namespace)
    assert namespace["result"]["code"] == "MATERIAL_SHADER_EXPORT_BRIDGE_UPGRADE_REQUIRED"
    assert namespace["result"]["loaded_version"] == version
    bridge.get_material_hlsl_code.assert_not_called()
    bridge.get_material_shader_source.assert_not_called()


@pytest.mark.parametrize("exporter", ["hlsl", "shader"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("engine, platform, source", [
    ("5.7.4", "VULKAN_ES3_1_ANDROID_Preview", "active_editor"),
    ("4.26.2", "PCD3D_SM5", "host_rhi"),
])
def test_shader_export_preserves_interface_and_platform(tmp_path, exporter, empty, engine, platform, source):
    import types
    from cli_anything.unreal.core import materials

    class Material:
        pass

    mat = Material()
    bridge = MagicMock()
    bridge.get_plugin_version.return_value = "1.40"
    bridge.get_active_shader_platform.return_value = platform

    def extract_hlsl(requested, path):
        assert requested is mat
        Path(path).write_text("instance source\n", encoding="utf-8")
        return [] if empty else [path]

    def extract_shaders(requested, path):
        assert requested is mat
        return [] if empty else ["BasePass\t" + path + "/BasePass.usf\t10"]

    bridge.get_material_hlsl_code.side_effect = extract_hlsl
    bridge.get_material_shader_source.side_effect = extract_shaders
    fake_unreal = types.SimpleNamespace(
        MaterialInterface=Material, CliAnythingBridgeLibrary=bridge,
        Paths=types.SimpleNamespace(project_saved_dir=lambda: str(tmp_path)),
        SystemLibrary=types.SimpleNamespace(get_engine_version=lambda: engine),
    )
    template = (materials._PLUGIN_GET_HLSL_CODE_SCRIPT if exporter == "hlsl"
                else materials._PLUGIN_GET_SHADER_SOURCE_SCRIPT)
    namespace = {"_cli_load_material": lambda *args: (mat, "/Game/MI_Test", [])}
    with patch.dict("sys.modules", {"unreal": fake_unreal}):
        exec(template.format(material_path="/Game/MI_Test", material_path_candidates_json="[]",
                             mat_name="MI_Test"), namespace)
    result = namespace["result"]
    assert ("error" in result) is empty
    assert result["checked_platform"] == platform
    assert result["platform_source"] == source
    assert result["resource_scope"] == "material_interface"
    if not empty:
        assert result["material"] == "/Game/MI_Test"
        if exporter == "shader":
            assert result["quality_level"] == "High"


def test_compile_status_script_rejects_legacy_bridge():
    import types
    from cli_anything.unreal.core.materials import _PLUGIN_GET_ERRORS_SCRIPT

    class Material:
        pass

    mat = Material()
    fake_unreal = types.SimpleNamespace(
        MaterialInterface=Material, MaterialFunctionInterface=type("Function", (), {}),
        CliAnythingBridgeLibrary=types.SimpleNamespace(),
    )
    namespace = {"_cli_load_material": lambda *args: (mat, "/Game/M_Test", [])}
    script = _PLUGIN_GET_ERRORS_SCRIPT.format(
        material_path="/Game/M_Test", material_path_candidates_json="[]"
    )
    with patch.dict("sys.modules", {"unreal": fake_unreal}):
        exec(script, namespace)
    assert namespace["result"]["code"] == "MATERIAL_ERRORS_BRIDGE_UPGRADE_REQUIRED"
    assert namespace["result"]["required_version"] == "1.39"
