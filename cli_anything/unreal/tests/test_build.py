"""Tests for test_build.py — Uses synthetic data only, no UE editor required."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


TEST574_UPROJECT = r"F:\Test574\Test574.uproject"

class TestBuild:
    """Tests for core/build.py — verifies command assembly."""

    def test_build_status(self, temp_project):
        from cli_anything.unreal.core.build import build_status

        status = build_status(temp_project["uproject"])
        assert status["project"] == "TestProject"
        assert status["has_binaries"] is True
        assert "Win64" in status["platforms"]

    def test_compile_no_engine(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=None):
            result = compile_project(temp_project["uproject"])
            assert result["status"] == "error"
            assert "engine root" in result["error"].lower()

    def test_cook_no_engine(self, temp_project):
        from cli_anything.unreal.core.build import cook_content

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=None):
            result = cook_content(temp_project["uproject"])
            assert result["status"] == "error"

    def test_package_no_engine(self, temp_project):
        from cli_anything.unreal.core.build import package_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=None):
            result = package_project(temp_project["uproject"])
            assert result["status"] == "error"

    def test_generate_no_engine(self, temp_project):
        from cli_anything.unreal.core.build import generate_project_files

        with patch("cli_anything.unreal.core.build.find_engine_root", return_value=None):
            result = generate_project_files(temp_project["uproject"])
            assert result["status"] == "error"


# ═══════════════════════════════════════════════════════════════════════
#  Test ue_http_api.py (mocked)
# ═══════════════════════════════════════════════════════════════════════


class TestBuildSuccessPaths:
    """Tests for compile/cook/package success paths via mocked run_uat."""

    def _mock_engine_root(self):
        return r"F:\RX_ENGINE_5.7"

    @staticmethod
    def _write_editor_receipt(
        project_dir: Path,
        products,
        *,
        config: str = "Development",
        filename: str = "TestProjectEditor.target",
        runtime_dependencies=None,
    ) -> Path:
        receipt = project_dir / "Binaries" / "Win64" / filename
        data = {
            "TargetName": "TestProjectEditor",
            "Platform": "Win64",
            "Configuration": config,
            "TargetType": "Editor",
            "BuildProducts": products,
        }
        if runtime_dependencies is not None:
            data["RuntimeDependencies"] = runtime_dependencies
        receipt.write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        return receipt

    @staticmethod
    def _minimal_pe_image() -> bytes:
        image = bytearray(1024)
        image[:2] = b"MZ"
        image[0x3C:0x40] = (0x80).to_bytes(4, "little")
        image[0x80:0x84] = b"PE\0\0"
        image[0x84:0x86] = (0x8664).to_bytes(2, "little")
        image[0x86:0x88] = (1).to_bytes(2, "little")
        image[0x94:0x96] = (0xF0).to_bytes(2, "little")
        image[0x96:0x98] = (0x2022).to_bytes(2, "little")
        image[0x98:0x9A] = (0x20B).to_bytes(2, "little")
        image[0xB0:0xB8] = (0x140000000).to_bytes(8, "little")
        image[0xB8:0xBC] = (0x1000).to_bytes(4, "little")
        image[0xBC:0xC0] = (0x200).to_bytes(4, "little")
        image[0xD0:0xD4] = (0x2000).to_bytes(4, "little")
        image[0xD4:0xD8] = (0x200).to_bytes(4, "little")
        image[0x104:0x108] = (16).to_bytes(4, "little")
        image[0x188:0x190] = b".text\0\0\0"
        image[0x190:0x194] = (1).to_bytes(4, "little")
        image[0x194:0x198] = (0x1000).to_bytes(4, "little")
        image[0x198:0x19C] = (0x200).to_bytes(4, "little")
        image[0x19C:0x1A0] = (0x200).to_bytes(4, "little")
        image[0x1AC:0x1B0] = (0x60000020).to_bytes(4, "little")
        return bytes(image)

    def test_compile_success(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 0, "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
                 "duration_seconds": 12.3,
             }) as mock_run:
            result = compile_project(temp_project["uproject"])
            assert result["status"] == "ok"
            assert result["returncode"] == 0
            assert result["log_file"].endswith("cli_compile.log")
            assert result["duration_seconds"] == 12.3
            # stdout/stderr must not leak back into the result
            assert "stdout" not in result
            assert "stderr" not in result
            assert "-utf8output" not in mock_run.call_args.args[2]
            assert "-WaitForUATMutex" in mock_run.call_args.args[2]

    def test_compile_rejects_corrupt_pe_from_editor_target_receipt(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = (
            project_dir
            / "Plugins"
            / "SGFramework"
            / "Binaries"
            / "Win64"
            / "UnrealEditor-SGFramework.dll"
        )
        product.parent.mkdir(parents=True)
        product.write_bytes(b"\0" * (2 * 1024 * 1024))
        receipt = self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Plugins/SGFramework/Binaries/Win64/"
                "UnrealEditor-SGFramework.dll",
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={
                "returncode": 0,
                "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
                "duration_seconds": 12.3,
            },
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert result["code"] == "INVALID_BUILD_OUTPUT"
        assert result["returncode"] == 0
        assert result["failure_kind"] == "invalid_pe_build_product"
        assert result["receipt_file"] == str(receipt)
        assert result["validated_pe_products"] == 1
        assert result["invalid_build_products"] == [{
            "path": str(product),
            "type": "DynamicLibrary",
            "reason": "missing DOS MZ signature",
        }]

    def test_compile_accepts_valid_pe_from_editor_target_receipt(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "TestProjectEditor.dll"
        product.write_bytes(self._minimal_pe_image())
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/TestProjectEditor.dll",
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={
                "returncode": 0,
                "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
                "duration_seconds": 12.3,
            },
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "ok"
        assert result["returncode"] == 0
        assert "invalid_build_products" not in result

    def test_cancel_output_inspection_reports_missing_runtime_dependencies(
        self, temp_project, tmp_path
    ):
        from cli_anything.unreal.core.build import (
            inspect_win64_editor_runtime_dependencies,
        )

        project_dir = Path(temp_project["dir"])
        engine_root = tmp_path / "EngineRoot"
        existing = (
            engine_root
            / "Engine"
            / "Binaries"
            / "ThirdParty"
            / "Existing.dll"
        )
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"present")
        receipt = self._write_editor_receipt(
            project_dir,
            [],
            runtime_dependencies=[
                {
                    "Path": "$(EngineDir)/Binaries/ThirdParty/Existing.dll",
                    "Type": "NonUFS",
                },
                {
                    "Path": "$(EngineDir)/Binaries/ThirdParty/tbbmalloc.dll",
                    "Type": "NonUFS",
                },
                {
                    "Path": "$(ProjectDir)/Content/Resources/usd.schema.json",
                    "Type": "NonUFS",
                },
                {
                    "Path": "$(EngineDir)/Binaries/ThirdParty/tbbmalloc.dll",
                    "Type": "NonUFS",
                },
            ],
        )

        result = inspect_win64_editor_runtime_dependencies(
            temp_project["uproject"],
            str(engine_root),
        )

        assert result["status"] == "incomplete"
        assert result["code"] == "BUILD_CANCELLED_OUTPUTS_INCOMPLETE"
        assert result["failure_kind"] == "missing_runtime_dependencies"
        assert result["receipt_file"] == str(receipt)
        assert result["checked_runtime_dependency_count"] == 3
        assert result["missing_runtime_dependency_count"] == 2
        assert result["missing_runtime_dependencies"] == [
            str(
                engine_root
                / "Engine"
                / "Binaries"
                / "ThirdParty"
                / "tbbmalloc.dll"
            ),
            str(project_dir / "Content" / "Resources" / "usd.schema.json"),
        ]
        assert result["recovery_command"] == (
            f'ue-cli --project "{temp_project["uproject"]}" build compile '
            "--platform Win64 --config Development"
        )

    @pytest.mark.parametrize(
        ("config", "filename"),
        [
            ("Shipping", "TestProjectEditor-Win64-Shippingx64.target"),
            ("Development", "TestProjectEditor-Win64-Developmentx64.target"),
        ],
    )
    def test_compile_finds_configured_or_architecture_suffixed_receipt(
        self, temp_project, config, filename
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "Broken.dll"
        product.write_bytes(b"\0" * 512)
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/Broken.dll",
                "Type": "DynamicLibrary",
            }],
            config=config,
            filename=filename,
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                config=config,
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert result["code"] == "INVALID_BUILD_OUTPUT"
        assert result["receipt_file"].endswith(filename)

    def test_compile_rejects_truncated_pe_headers(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "Truncated.dll"
        product.write_bytes(self._minimal_pe_image()[:154])
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/Truncated.dll",
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert result["invalid_build_products"][0]["reason"] == (
            "optional header extends past end of file"
        )

    def test_compile_rejects_truncated_pe_section_data(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "TruncatedSection.dll"
        product.write_bytes(self._minimal_pe_image()[:0x300])
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/TruncatedSection.dll",
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert result["invalid_build_products"][0]["reason"] == (
            "section 0 raw data extends past end of file"
        )

    def test_compile_prefers_newer_matching_architecture_receipt(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        valid = project_dir / "Binaries" / "Win64" / "Valid.dll"
        corrupt = project_dir / "Binaries" / "Win64" / "Corrupt.dll"
        valid.write_bytes(self._minimal_pe_image())
        corrupt.write_bytes(b"\0" * 512)
        default_receipt = self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/Valid.dll",
                "Type": "DynamicLibrary",
            }],
        )
        os.utime(default_receipt, (1, 1))
        architecture_receipt = self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/Corrupt.dll",
                "Type": "DynamicLibrary",
            }],
            filename="TestProjectEditor-Win64-Developmentx64.target",
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert result["receipt_file"] == str(architecture_receipt)

    def test_compile_rejects_invalid_build_products_schema(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        self._write_editor_receipt(project_dir, None)

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert result["code"] == "INVALID_BUILD_OUTPUT"
        assert result["failure_kind"] == "invalid_build_receipt"

    def test_compile_error_returncode(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 1, "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
                 "duration_seconds": 5.0,
             }):
            result = compile_project(temp_project["uproject"])
            assert result["status"] == "error"
            assert result["returncode"] == 1
            assert "log_file" in result["error"]

    def test_compile_win64_modules_uses_build_bat_editor_target(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        products = []
        for module in ("Renderer", "RHI"):
            product = (
                project_dir
                / "Binaries"
                / "Win64"
                / f"UnrealEditor-{module}.dll"
            )
            product.write_bytes(self._minimal_pe_image())
            products.append({
                "Path": f"$(ProjectDir)/Binaries/Win64/UnrealEditor-{module}.dll",
                "Type": "DynamicLibrary",
            })
        self._write_editor_receipt(project_dir, products)

        build_result = {
            "returncode": 0,
            "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
            "duration_seconds": 3.0,
        }
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value=build_result,
        ) as mock_run_build, patch(
            "cli_anything.unreal.core.build.run_uat",
        ) as mock_run_uat:
            result = compile_project(
                temp_project["uproject"],
                platform="Win64",
                engine_root=self._mock_engine_root(),
                modules=("Renderer", "RHI"),
            )

        mock_run_build.assert_called_once_with(
            self._mock_engine_root(),
            "TestProjectEditor",
            "Win64",
            "Development",
            extra_args=[
                f'-Project={temp_project["uproject"]}',
                "-Module=Renderer",
                "-Module=RHI",
                "-WaitMutex",
            ],
            log_file=None,
            log_label="compile",
            project_dir=str(Path(temp_project["uproject"]).parent),
            on_start=None,
        )
        mock_run_uat.assert_not_called()
        assert result["status"] == "ok"

    def test_compile_module_can_use_engine_editor_target_for_blueprint_project(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = tmp_path / "BlueprintOnly"
        project_dir.mkdir()
        uproject = project_dir / "BlueprintOnly.uproject"
        uproject.write_text(
            '{"FileVersion": 3, "EngineAssociation": "4.26"}',
            encoding="utf-8",
        )
        engine_root = self._mock_engine_root()
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.get_editor_binary_prefix",
            return_value="UE4Editor",
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ) as mock_run_build:
            result = compile_project(
                str(uproject),
                engine_root=engine_root,
                modules=("CliAnythingBridge",),
                use_engine_editor_target_if_missing=True,
            )

        mock_run_build.assert_called_once_with(
            engine_root,
            "UE4Editor",
            "Win64",
            "Development",
            extra_args=[
                f"-Project={uproject}",
                "-Module=CliAnythingBridge",
                "-WaitMutex",
            ],
            log_file=None,
            log_label="compile",
            project_dir=str(project_dir),
            on_start=None,
        )
        assert result["status"] == "ok"
        assert result["editor_target"] == "UE4Editor"
        assert result["editor_target_source"] == "engine"

    def test_compile_rejects_engine_plugin_module_before_ubt(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": (
                    "$(EngineDir)/Plugins/Editor/AssetReferenceRestrictions/"
                    "Binaries/Win64/UnrealEditor-AssetReferenceRestrictions.dll"
                ),
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
        ) as mock_run_build:
            result = compile_project(
                temp_project["uproject"],
                platform="Win64",
                config="Development",
                engine_root=self._mock_engine_root(),
                modules=("AssetReferenceRestrictions",),
            )

        mock_run_build.assert_not_called()
        assert result["status"] == "error"
        assert result["code"] == "ENGINE_PLUGIN_MODULE_UNSUPPORTED"
        assert result["failure_kind"] == "unsupported_engine_plugin_module"
        assert result["modules"] == ["AssetReferenceRestrictions"]
        assert result["module_products"] == {
            "AssetReferenceRestrictions": [
                str(
                    Path(self._mock_engine_root())
                    / "Engine"
                    / "Plugins"
                    / "Editor"
                    / "AssetReferenceRestrictions"
                    / "Binaries"
                    / "Win64"
                    / "UnrealEditor-AssetReferenceRestrictions.dll"
                )
            ]
        }
        assert "--module" not in result["recovery_command"]
        assert result["recovery_command"] == (
            f'ue-cli --project "{temp_project["uproject"]}" build compile '
            "--platform Win64 --config Development"
        )

    def test_compile_allows_engine_core_module_to_reach_ubt(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(EngineDir)/Binaries/Win64/UnrealEditor-Renderer.dll",
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 6, "log_file": "compile.log"},
        ) as mock_run_build:
            result = compile_project(
                temp_project["uproject"],
                platform="Win64",
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        mock_run_build.assert_called_once()
        assert result["status"] == "error"
        assert result.get("code") != "ENGINE_PLUGIN_MODULE_UNSUPPORTED"

    def test_compile_module_preserves_existing_hot_reload_state(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "UnrealEditor-Renderer.dll"
        product.write_bytes(self._minimal_pe_image())
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/UnrealEditor-Renderer.dll",
                "Type": "DynamicLibrary",
            }],
        )
        state_file = (
            project_dir
            / "Intermediate"
            / "Build"
            / "Win64"
            / "x64"
            / "TestProjectEditor"
            / "Development"
            / "HotReloadState.bin"
        )
        state_file.parent.mkdir(parents=True)
        state_file.write_bytes(b"synthetic state")

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ) as mock_run_build:
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "ok"
        assert mock_run_build.call_args.kwargs["extra_args"] == [
            f'-Project={temp_project["uproject"]}',
            "-ForceHotReload",
            "-Module=Renderer",
            "-WaitMutex",
        ]

    def test_compile_module_rejects_missing_editor_receipt(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "error"
        assert result["code"] == "INVALID_BUILD_OUTPUT"
        assert result["failure_kind"] == "missing_editor_target_receipt"

    def test_compile_module_rejects_missing_module_manifest(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "UnrealEditor-Renderer.dll"
        product.write_bytes(self._minimal_pe_image())
        missing_manifest = (
            project_dir
            / "Plugins"
            / "Example"
            / "Binaries"
            / "Win64"
            / "UnrealEditor.modules"
        )
        self._write_editor_receipt(
            project_dir,
            [
                {
                    "Path": "$(ProjectDir)/Binaries/Win64/UnrealEditor-Renderer.dll",
                    "Type": "DynamicLibrary",
                },
                {
                    "Path": (
                        "$(ProjectDir)/Plugins/Example/Binaries/Win64/"
                        "UnrealEditor.modules"
                    ),
                    "Type": "RequiredResource",
                },
            ],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "error"
        assert result["code"] == "INVALID_BUILD_OUTPUT"
        assert result["failure_kind"] == "missing_editor_module_manifests"
        assert result["missing_module_manifests"] == [str(missing_manifest)]

    def test_compile_module_ignores_unrelated_invalid_target_product(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        renderer = project_dir / "Binaries" / "Win64" / "UnrealEditor-Renderer.dll"
        unrelated = project_dir / "Binaries" / "Win64" / "UnrealEditor-Broken.dll"
        renderer.write_bytes(self._minimal_pe_image())
        unrelated.write_bytes(b"\0" * 512)
        self._write_editor_receipt(
            project_dir,
            [
                {
                    "Path": "$(ProjectDir)/Binaries/Win64/UnrealEditor-Renderer.dll",
                    "Type": "DynamicLibrary",
                },
                {
                    "Path": "$(ProjectDir)/Binaries/Win64/UnrealEditor-Broken.dll",
                    "Type": "DynamicLibrary",
                },
            ],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "ok"

    def test_compile_module_validates_config_suffixed_binary(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = (
            project_dir
            / "Binaries"
            / "Win64"
            / "UnrealEditor-Renderer-Win64-DebugGame.dll"
        )
        product.write_bytes(b"\0" * 512)
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/"
                "UnrealEditor-Renderer-Win64-DebugGame.dll",
                "Type": "DynamicLibrary",
            }],
            config="DebugGame",
            filename="TestProjectEditor-Win64-DebugGamex64.target",
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                config="DebugGame",
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "error"
        assert result["invalid_build_products"][0]["path"] == str(product)

    def test_compile_module_rejects_receipt_without_requested_product(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["dir"])
        product = project_dir / "Binaries" / "Win64" / "UnrealEditor-RHI.dll"
        product.write_bytes(self._minimal_pe_image())
        self._write_editor_receipt(
            project_dir,
            [{
                "Path": "$(ProjectDir)/Binaries/Win64/UnrealEditor-RHI.dll",
                "Type": "DynamicLibrary",
            }],
        )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value={"returncode": 0, "log_file": "compile.log"},
        ):
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "error"
        assert result["failure_kind"] == "invalid_build_receipt"
        assert "Renderer" in result["error"]

    @pytest.mark.parametrize("module", ["Renderer -Clean", "../Renderer", ""])
    def test_compile_modules_rejects_unsafe_names(self, temp_project, module):
        from cli_anything.unreal.core.build import compile_project

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch("cli_anything.unreal.core.build.run_build") as mock_run:
            result = compile_project(
                temp_project["uproject"],
                engine_root=self._mock_engine_root(),
                modules=(module,),
            )

        assert result["status"] == "error"
        assert "module" in result["error"].lower()
        mock_run.assert_not_called()

    def test_compile_modules_rejects_non_win64_platform(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch("cli_anything.unreal.core.build.run_build") as mock_run:
            result = compile_project(
                temp_project["uproject"],
                platform="Android",
                engine_root=self._mock_engine_root(),
                modules=("Renderer",),
            )

        assert result["status"] == "error"
        assert "win64" in result["error"].lower()
        mock_run.assert_not_called()

    def test_compile_failure_reports_bk_dist_evidence(self, tmp_path):
        from cli_anything.unreal.core.build import _normalize_result

        actions_file = tmp_path / "bk_actions_tRXFhsLpTb.json"
        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "\n".join([
                "Using Parallel executor to run 87 action(s)",
                f"bk-ubt-tool.exe --actions_json_file {actions_file}",
                "UBTTool: Building 87 actions with 288 jobs...exit code:2,error:<nil>",
                f"failed to run actions with json file: {actions_file}",
                "failed to compile with bk tools",
                "Result: Failed (OtherCompilationError)",
            ]),
            encoding="utf-8",
        )

        result = _normalize_result(
            {"returncode": 6, "log_file": str(log_file)},
            "Compile",
        )

        assert result["status"] == "error"
        assert result["failure_kind"] == "distributed_executor_failed_without_diagnostic"
        assert result["executor"] == "bk_dist"
        assert result["action_count"] == 87
        assert result["executor_exit_code"] == 2
        assert result["actions_json_file"] == str(actions_file)
        assert "without emitting a compiler diagnostic" in result["diagnostic"]

    def test_compile_failure_classifies_ubt_mutex_conflict_before_uat_exit_label(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_compile_result

        mutex_name = (
            r"Global\UnrealBuildTool_Mutex_"
            "cc1e4ec248d9b3ed35259397698050712f573880"
        )
        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "\n".join([
                f"A conflicting instance of {mutex_name} is already running.",
                "Result: Failed (ConflictingInstance)",
                "Took 0.65s to run dotnet.exe, ExitCode=10",
                (
                    "AutomationTool exiting with ExitCode=10 "
                    "(Error_SDKNotFound)"
                ),
                "BUILD FAILED",
            ]),
            encoding="utf-8",
        )

        result = _normalize_compile_result(
            {"returncode": 10, "log_file": str(log_file)},
            uproject_path=str(tmp_path / "Test.uproject"),
            engine_root=str(tmp_path / "Engine"),
            config="Development",
            platform="Win64",
        )

        assert result["status"] == "error"
        assert result["code"] == "BUILD_CONFLICTING_INSTANCE"
        assert result["failure_kind"] == "ubt_mutex_conflict"
        assert result["mutex_name"] == mutex_name
        assert result["diagnostic"].startswith("A conflicting instance")
        assert "another instance" in result["error"].lower()
        assert "SDK" not in result["error"]
        assert "other UnrealBuildTool process" in result["suggestion"]

    def test_compile_exit_10_without_conflict_evidence_remains_generic(self, tmp_path):
        from cli_anything.unreal.core.build import _normalize_compile_result

        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "AutomationTool exiting with ExitCode=10 (Error_SDKNotFound)\n",
            encoding="utf-8",
        )

        result = _normalize_compile_result(
            {"returncode": 10, "log_file": str(log_file)},
            uproject_path=str(tmp_path / "Test.uproject"),
            engine_root=str(tmp_path / "Engine"),
            config="Development",
            platform="Win64",
        )

        assert result["status"] == "error"
        assert "code" not in result
        assert result["error"].startswith("Compile failed (exit 10)")

    def test_compile_failure_classifies_legacy_ubt_mutex_conflict(self, tmp_path):
        from cli_anything.unreal.core.build import _normalize_compile_result

        diagnostic = (
            "ERROR: A conflicting instance of UnrealBuildTool is already running."
        )
        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "\n".join([
                diagnostic,
                "Result: Failed (OtherCompilationError)",
                "BUILD FAILED",
            ]),
            encoding="utf-8",
        )

        result = _normalize_compile_result(
            {"returncode": 6, "log_file": str(log_file)},
            uproject_path=str(tmp_path / "Test.uproject"),
            engine_root=str(tmp_path / "Engine"),
            config="Development",
            platform="Win64",
        )

        assert result["code"] == "BUILD_CONFLICTING_INSTANCE"
        assert result["failure_kind"] == "ubt_mutex_conflict"
        assert result["diagnostic"] == diagnostic
        assert "mutex_name" not in result

    def test_compile_failure_returns_real_compiler_diagnostics(self, tmp_path):
        from cli_anything.unreal.core.build import _normalize_result

        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "Source.cpp(42): error C2065: 'Missing': undeclared identifier\n"
            "LINK : fatal error LNK1104: cannot open file 'Locked.dll'\n",
            encoding="utf-8",
        )

        result = _normalize_result(
            {"returncode": 6, "log_file": str(log_file)},
            "Compile",
        )

        assert result["failure_kind"] == "compiler_diagnostics"
        assert len(result["diagnostics"]) == 2
        assert "error C2065" in result["diagnostics"][0]
        assert "fatal error LNK1104" in result["diagnostics"][1]

    def test_compile_failure_classifies_bk_dist_virtual_memory_exhaustion(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_result

        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "\n".join([
                (
                    "Requested 1.5 GB memory per action, 14.8 GB available: "
                    "limiting max parallel actions to 10"
                ),
                "c1xx: error C3859: \u672a\u80fd\u521b\u5efa PCH \u7684\u865a\u62df\u5185\u5b58",
                "c1xx: note: \u7cfb\u7edf\u8fd4\u56de\u4ee3\u7801 1455: \u9875\u9762\u6587\u4ef6\u592a\u5c0f\uff0c\u65e0\u6cd5\u5b8c\u6210\u64cd\u4f5c\u3002",
                "c1xx: fatal error C1076: \u7f16\u8bd1\u5668\u9650\u5236: \u8fbe\u5230\u5185\u90e8\u5806\u9650\u5236",
                "UBTTool: Building 4317 actions with 288 jobs...exit code:2,error:<nil>",
                "[bk_dist] status: failed to compile with bk tools",
            ]),
            encoding="utf-8",
        )

        result = _normalize_result(
            {"returncode": 6, "log_file": str(log_file)},
            "Compile",
        )

        assert result["code"] == "BUILD_RESOURCE_EXHAUSTED"
        assert result["failure_kind"] == "compiler_virtual_memory_exhaustion"
        assert result["resource"] == "virtual_memory"
        assert result["system_error_code"] == 1455
        assert result["executor"] == "bk_dist"
        assert result["ubt_parallel_action_limit"] == 10
        assert result["executor_job_count"] == 288
        assert result["parallel_limit_exceeded"] is True
        assert "C3859" in result["diagnostics"][0]
        assert "C1076" in result["diagnostics"][1]
        assert "bk_dist" in result["suggestion"]

    def test_c1076_without_virtual_memory_evidence_remains_generic(self, tmp_path):
        from cli_anything.unreal.core.build import _normalize_result

        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "Source.cpp(7): fatal error C1076: compiler limit: "
            "internal heap limit reached\n",
            encoding="utf-8",
        )

        result = _normalize_result(
            {"returncode": 6, "log_file": str(log_file)},
            "Compile",
        )

        assert result["failure_kind"] == "compiler_diagnostics"
        assert "code" not in result

    def test_package_cook_plugin_failure_takes_priority_over_old_errors(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_result

        plugin_error = (
            "LogPluginManager: Error: Plugin 'AssetReferenceRestrictions' failed "
            "to load because module 'AssetReferenceRestrictions' could not be found."
        )
        log_file = tmp_path / "cli_package.log"
        log_file.write_text(
            "\n".join([
                "Source.cpp(42): error C2065: 'OldCompileError': undeclared identifier",
                (
                    "[0806/120000.000:ERROR:tcp_socket_win.cc(123)] "
                    "bind() returned an error"
                ),
                plugin_error,
                "UnrealEditor-Cmd.exe, ExitCode=1",
                "Cook failed.",
                (
                    "AutomationTool exiting with ExitCode=25 "
                    "(Error_UnknownCookFailure)"
                ),
            ]),
            encoding="utf-8",
        )

        result = _normalize_result(
            {"returncode": 25, "log_file": str(log_file)},
            "Package",
        )

        assert result["status"] == "error"
        assert result["code"] == "BUILD_PLUGIN_LOAD_FAILED"
        assert result["failure_kind"] == "plugin_load_failure"
        assert result["phase"] == "cook"
        assert result["plugin"] == "AssetReferenceRestrictions"
        assert result["module"] == "AssetReferenceRestrictions"
        assert result["diagnostic"] == plugin_error
        assert result["diagnostics"] == [plugin_error]

    def test_package_failure_classifies_gradle_loopback_and_iterative_retry(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_result

        diagnostic = "java.io.IOException: Unable to establish loopback connection"
        log_file = tmp_path / "cli_package.log"
        log_file.write_text(
            "\n".join([
                "********** BUILD COMMAND COMPLETED **********",
                "********** COOK COMMAND COMPLETED **********",
                "********** STAGE COMMAND COMPLETED **********",
                "********** PACKAGE COMMAND STARTED **********",
                "Making .apk with Gradle...",
                diagnostic,
                (
                    'cmd.exe failed with args /c "F:\\Game\\Intermediate\\Android\\'
                    'arm64\\gradle\\rungradle.bat" :app:assembleDebug'
                ),
                "AutomationTool exiting with ExitCode=1 (Error_Unknown)",
                "BUILD FAILED",
            ]),
            encoding="utf-8",
        )

        result = _normalize_result(
            {
                "returncode": 1,
                "log_file": str(log_file),
                "command": [
                    r"F:\Engine\Build\BatchFiles\RunUAT.bat",
                    "BuildCookRun",
                    "-build",
                    "-cook",
                    "-stage",
                    "-package",
                    "-iterate",
                ],
            },
            "Package",
        )

        assert result["status"] == "error"
        assert result["returncode"] == 1
        assert result["code"] == "BUILD_GRADLE_LOOPBACK_FAILED"
        assert result["failure_kind"] == "gradle_loopback_connection"
        assert result["failure_scope"] == "local_environment"
        assert result["phase"] == "package"
        assert result["external_tool"] == "Gradle"
        assert result["transient"] is True
        assert result["retry_safe"] is True
        assert result["retry_uses_iterative_cook"] is True
        assert result["reuse_successful_outputs"] is True
        assert result["completed_phases"] == ["build", "cook", "stage"]
        assert result["diagnostic"] == diagnostic
        assert result["gradle_task"] == ":app:assembleDebug"
        assert "existing --uat-arg=-iterate" in result["suggestion"]
        assert "firewall" in result["suggestion"]

    def test_loopback_text_without_gradle_package_context_remains_generic(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_result

        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(
            "java.io.IOException: Unable to establish loopback connection\n",
            encoding="utf-8",
        )

        result = _normalize_result(
            {"returncode": 1, "log_file": str(log_file)},
            "Compile",
        )

        assert result["status"] == "error"
        assert "code" not in result
        assert result["error"].startswith("Compile failed (exit 1)")

    def test_compile_failure_classifies_missing_include_with_engine_context(
        self,
        temp_project,
        mock_engine_root,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_compile_result

        diagnostic = (
            r"F:\Project\Source\Game\Public\OcclusionRevealComponent.h"
            "(7,1): fatal error C1083: "
            "\u65e0\u6cd5\u6253\u5f00\u5305\u62ec\u6587\u4ef6: "
            "\u201cLocalOcclusionInvalidation.h\u201d: No such file or directory"
        )
        log_file = tmp_path / "cli_compile.log"
        log_file.write_bytes((diagnostic + "\n").encode("mbcs"))

        with patch(
            "cli_anything.unreal.core.build._engine_source_control_provenance",
            return_value={
                "status": "available",
                "type": "git",
                "branch": "NL_Master",
                "commit": "780f6e1652acca9b8eda1c965724ab910e3feab2",
            },
        ):
            result = _normalize_compile_result(
                {"returncode": 6, "log_file": str(log_file)},
                uproject_path=temp_project["uproject"],
                engine_root=mock_engine_root,
                config="Development",
                platform="Win64",
            )

        assert result["code"] == "BUILD_MISSING_INCLUDE"
        assert result["failure_kind"] == "missing_include"
        assert result["missing_include_count"] == 1
        assert result["missing_includes"] == [{
            "include": "LocalOcclusionInvalidation.h",
            "referenced_by": (
                r"F:\Project\Source\Game\Public\OcclusionRevealComponent.h"
            ),
            "line": 7,
            "column": 1,
        }]
        assert result["compatibility"]["status"] == "unverified"
        assert result["compatibility"]["project"]["engine_association"] == "5.7"
        assert result["compatibility"]["engine"]["version"] == "5.7.0"
        assert (
            result["compatibility"]["engine"]["source_control"]["branch"]
            == "NL_Master"
        )
        assert (
            result["compatibility"]["engine"]["source_control"]["commit"]
            == "780f6e1652acca9b8eda1c965724ab910e3feab2"
        )
        assert "project source revision" in result["suggestion"]

    def test_c1083_non_include_failure_remains_generic_compiler_diagnostic(
        self,
        tmp_path,
    ):
        from cli_anything.unreal.core.build import _normalize_result

        diagnostic = (
            "Source.cpp(7): fatal error C1083: "
            "Cannot open compiler generated file: 'Source.obj': Permission denied"
        )
        log_file = tmp_path / "cli_compile.log"
        log_file.write_text(diagnostic + "\n", encoding="utf-8")

        result = _normalize_result(
            {"returncode": 6, "log_file": str(log_file)},
            "Compile",
        )

        assert result["failure_kind"] == "compiler_diagnostics"
        assert "code" not in result

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows compiler encoding behavior")
    def test_compile_failure_decodes_localized_msvc_diagnostics(self, tmp_path):
        from cli_anything.unreal.core.build import _normalize_result

        log_file = tmp_path / "cli_compile.log"
        diagnostic = (
            "SDOC.cpp(717,4): error C2065: LogRenderer: "
            "\u672a\u58f0\u660e\u7684\u6807\u8bc6\u7b26"
        )
        log_file.write_bytes((diagnostic + "\n").encode("mbcs"))

        result = _normalize_result(
            {"returncode": 6, "log_file": str(log_file)},
            "Compile",
        )

        assert result["failure_kind"] == "compiler_diagnostics"
        assert result["diagnostics"] == [diagnostic]
        assert "\ufffd" not in result["diagnostics"][0]

    def test_compile_android_uses_build_bat_game_target(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        build_result = {
            "returncode": 0,
            "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
            "duration_seconds": 12.3,
        }
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value=build_result,
            create=True,
        ) as mock_run_build, patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value=build_result,
        ) as mock_run_uat:
            result = compile_project(
                temp_project["uproject"],
                platform="Android",
                engine_root=self._mock_engine_root(),
            )

        project_dir = str(Path(temp_project["uproject"]).parent)
        mock_run_build.assert_called_once_with(
            self._mock_engine_root(),
            "TestProject",
            "Android",
            "Development",
            extra_args=[
                f'-Project={temp_project["uproject"]}',
                "-WaitMutex",
            ],
            log_file=None,
            log_label="compile",
            project_dir=project_dir,
            on_start=None,
        )
        mock_run_uat.assert_not_called()
        assert result["status"] == "ok"

    def test_compile_android_uses_unique_custom_game_target(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        project_dir = Path(temp_project["uproject"]).parent
        (project_dir / "Source" / "CustomMobile.Target.cs").write_text(
            "public class CustomMobileTarget : TargetRules {\n"
            "    public CustomMobileTarget(TargetInfo Target) : base(Target) {\n"
            "        Type = TargetType.Game;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        build_result = {
            "returncode": 0,
            "log_file": "compile.log",
            "duration_seconds": 1.0,
        }
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value=build_result,
            create=True,
        ) as mock_run_build, patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value=build_result,
        ) as mock_run_uat:
            result = compile_project(
                temp_project["uproject"],
                platform="Android",
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "ok"
        assert mock_run_build.call_args.args[1] == "CustomMobile"
        mock_run_uat.assert_not_called()

    def test_compile_android_ignores_inactive_and_lexical_fake_game_targets(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import compile_project

        source_dir = Path(temp_project["uproject"]).parent / "Source"
        (source_dir / "FakeEditor.Target.cs").write_text(
            "// Type = TargetType.Game;\n"
            'const string Normal = "Type = TargetType.Game;";\n'
            'const string Verbatim = @"Type = TargetType.Game;";\n'
            "const char Fake = 'Type = TargetType.Game;';\n"
            "/* Type = TargetType.Game; */\n"
            "#if false\n"
            "Type = TargetType.Game;\n"
            "#if NESTED\n"
            "Type = TargetType.Game;\n"
            "#endif\n"
            "#endif\n",
            encoding="utf-8",
        )
        (source_dir / "RealMobile.Target.cs").write_text(
            "public class RealMobileTarget : TargetRules {\n"
            "    Type = TargetType.Game;\n"
            "}\n",
            encoding="utf-8",
        )
        build_result = {
            "returncode": 0,
            "log_file": "compile.log",
            "duration_seconds": 1.0,
        }
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            return_value=build_result,
        ) as mock_run_build, patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value=build_result,
        ) as mock_run_uat:
            result = compile_project(
                temp_project["uproject"],
                platform="Android",
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "ok"
        assert mock_run_build.call_args.args[1] == "RealMobile"
        mock_run_uat.assert_not_called()

    def test_compile_android_rejects_multiple_game_targets(self, temp_project):
        from cli_anything.unreal.core.build import compile_project

        source_dir = Path(temp_project["uproject"]).parent / "Source"
        for name in ("ClientGame", "CustomMobile"):
            (source_dir / f"{name}.Target.cs").write_text(
                f"public class {name}Target : TargetRules {{\n"
                "    Type = TargetType.Game;\n"
                "}\n",
                encoding="utf-8",
            )

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_build",
            create=True,
        ) as mock_run_build, patch(
            "cli_anything.unreal.core.build.run_uat",
        ) as mock_run_uat:
            result = compile_project(
                temp_project["uproject"],
                platform="Android",
                engine_root=self._mock_engine_root(),
            )

        assert result["status"] == "error"
        assert "multiple game targets" in result["error"].lower()
        assert "ClientGame" in result["error"]
        assert "CustomMobile" in result["error"]
        mock_run_build.assert_not_called()
        mock_run_uat.assert_not_called()

    def test_cook_success(self, temp_project):
        from cli_anything.unreal.core.build import cook_content

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 0, "log_file": r"F:\Test\Saved\Logs\cli_cook.log",
                 "duration_seconds": 30.0,
             }) as mock_run:
            result = cook_content(temp_project["uproject"])
            assert result["status"] == "ok"
            assert result["returncode"] == 0
            assert result["log_file"].endswith("cli_cook.log")
            assert "-utf8output" not in mock_run.call_args.args[2]
            assert "-allmaps" in mock_run.call_args.args[2]
            assert "-skippackage" in mock_run.call_args.args[2]

    @pytest.mark.parametrize("cook_completed", [True, False])
    def test_missing_staging_receipt_preserves_cook_evidence(self, tmp_path, cook_completed):
        from cli_anything.unreal.core.build import _normalize_result

        log_file = tmp_path / "cook.log"
        receipt_error = "Stage Failed. Missing receipt 'Test.target'. Check that this target has been built."
        log_file.write_text(
            "LogInit: Display: Success - 0 error(s), 0 warning(s)\n"
            + ("********** COOK COMMAND COMPLETED **********\n" if cook_completed else "")
            + receipt_error + "\nAutomationTool exiting with ExitCode=103\n",
            encoding="utf-8",
        )

        result = _normalize_result({"returncode": 103, "log_file": str(log_file)}, "Cook")

        assert result["status"] == "error"
        assert result["returncode"] == 103
        assert result["failure_kind"] == "missing_staging_receipt"
        assert result["completed_phases"] == (["cook"] if cook_completed else [])
        assert result["diagnostics"] == [receipt_error]
        assert ("Cook completed" in result["error"]) is cook_completed

    def test_cook_native_options(self, temp_project):
        """Targeted cook inputs must reach their native UAT/Cooker options."""
        from cli_anything.unreal.core.build import cook_content

        command = ["RunUAT.bat", "BuildCookRun", "-cook"]
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.find_engine_root",
            return_value=self._mock_engine_root(),
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={
                "returncode": 0,
                "log_file": r"F:\Test\Saved\Logs\cli_cook.log",
                "duration_seconds": 30.0,
                "command": command,
            },
        ) as mock_run:
            result = cook_content(
                temp_project["uproject"],
                platform="Android",
                packages=["/Game/Foo/A", "/Game/Foo/B"],
                output_dir=r"F:\Cook Output",
                ini_overrides=[
                    "Engine:[Section]:Key=Value",
                    "Game:[Other]:Flag=True",
                ],
            )

        uat_args = mock_run.call_args.args[2]
        assert "-allmaps" not in uat_args
        assert "-skippackage" in uat_args
        assert (
            "-AdditionalCookerOptions=-Package=/Game/Foo/A+/Game/Foo/B"
            in uat_args
        )
        assert r"-CookOutputDir=F:\Cook Output" in uat_args
        assert "-ini:Engine:[Section]:Key=Value" in uat_args
        assert "-ini:Game:[Other]:Flag=True" in uat_args
        assert result["uat_command"] == command

    def test_cook_rejects_embedded_plus_in_package(self, temp_project):
        """One package value cannot contain the separator used by UE."""
        from cli_anything.unreal.core.build import cook_content

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
        ) as mock_run:
            result = cook_content(
                temp_project["uproject"],
                packages=["/Game/Foo/A+/Game/Foo/B"],
            )

        assert result["status"] == "error"
        assert "must not contain '+'" in result["error"]
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"packages": [""]}, "cook package must not be empty"),
            ({"output_dir": ""}, "cook output directory must not be empty"),
            ({"ini_overrides": [""]}, "ini override must not be empty"),
            (
                {"ini_overrides": ["-ini:Engine:[Section]:Key=Value"]},
                "omit the '-ini:' prefix",
            ),
        ],
        ids=["empty_package", "empty_output_dir", "empty_ini", "prefixed_ini"],
    )
    def test_cook_rejects_invalid_native_option_values(
        self, temp_project, kwargs, message
    ):
        """Core callers get the same native-option validation as Click."""
        from cli_anything.unreal.core.build import cook_content

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.find_engine_root",
            return_value=self._mock_engine_root(),
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={
                "returncode": 0,
                "log_file": r"F:\Test\Saved\Logs\cli_cook.log",
                "duration_seconds": 1.0,
            },
        ) as mock_run:
            result = cook_content(temp_project["uproject"], **kwargs)

        assert result["status"] == "error"
        assert message in result["error"]
        mock_run.assert_not_called()

    def test_package_success(self, temp_project):
        from cli_anything.unreal.core.build import package_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 0, "log_file": r"F:\Test\Saved\Logs\cli_package.log",
                 "duration_seconds": 60.0,
             }) as mock_run:
            result = package_project(temp_project["uproject"])
            assert result["status"] == "ok"
            assert "output_dir" in result
            assert result["log_file"].endswith("cli_package.log")
            assert "-utf8output" not in mock_run.call_args.args[2]

    def test_package_rejects_dangling_writable_path_before_uat(self, temp_project):
        from cli_anything.unreal.core.build import package_project

        dangling = str(Path(temp_project["uproject"]).parent / "Saved" / "Shaders")
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build._find_dangling_package_paths",
            return_value=[dangling],
        ), patch(
            "cli_anything.unreal.core.build.find_engine_root",
        ) as mock_engine, patch(
            "cli_anything.unreal.core.build.run_uat",
        ) as mock_run:
            result = package_project(
                temp_project["uproject"],
                platform="Android",
            )

        assert result == {
            "status": "error",
            "code": "PACKAGE_DANGLING_LINK",
            "error": "Package preflight found a dangling symlink or Windows junction.",
            "failure_kind": "dangling_link",
            "dangling_paths": [dangling],
            "suggestion": (
                "Restore each link target or replace the link with a real directory, "
                "then retry the package command."
            ),
        }
        mock_engine.assert_not_called()
        mock_run.assert_not_called()

    def test_package_targeted_android_uat_args(self, temp_project):
        """Targeted package options must reach BuildCookRun as argv."""
        from cli_anything.unreal.core.build import package_project

        maps = [
            "/Game/Maps/Oregon_Main",
            "/Game/Maps/Oregon_Sub",
        ]
        extra_args = [
            "-pak",
            "-iostore",
            "-compressed",
            "-prereqs",
            "-nodebuginfo",
            "-unversionedcookedcontent",
            "-SkipCookingEditorContent",
            "-ini:Engine:[/Script/Engine.RendererSettings]:r.SDOC.Enable=1",
        ]
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.find_engine_root",
            return_value=self._mock_engine_root(),
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={
                "returncode": 0,
                "log_file": r"F:\Test\Saved\Logs\cli_package.log",
                "duration_seconds": 60.0,
                "command": ["RunUAT.bat", "BuildCookRun", "-package"],
            },
        ) as mock_run:
            result = package_project(
                temp_project["uproject"],
                platform="Android",
                output_dir="D:/Out",
                maps=maps,
                cook_flavor="ASTC",
                uat_args=extra_args,
            )

        uat_args = mock_run.call_args.args[2]
        assert "-platform=Android" in uat_args
        assert "-map=/Game/Maps/Oregon_Main+/Game/Maps/Oregon_Sub" in uat_args
        assert "-cookflavor=ASTC" in uat_args
        for arg in extra_args:
            assert arg in uat_args
        assert result["uat_command"] == [
            "RunUAT.bat",
            "BuildCookRun",
            "-package",
        ]

    def test_package_preserves_legacy_positional_parameters(self, temp_project):
        """New package controls must not shift existing positional arguments."""
        from cli_anything.unreal.core.build import package_project

        on_start = MagicMock()
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
            return_value={
                "returncode": 0,
                "log_file": "package.log",
                "duration_seconds": 1.0,
                "command": ["RunUAT.bat", "BuildCookRun"],
            },
        ) as mock_run:
            result = package_project(
                temp_project["uproject"],
                "Android",
                "Development",
                "D:/Out",
                "F:/Engine",
                "D:/package.log",
                on_start,
            )

        assert result["status"] == "ok"
        assert mock_run.call_args.args[0] == "F:/Engine"
        assert mock_run.call_args.kwargs["log_file"] == "D:/package.log"
        assert mock_run.call_args.kwargs["on_start"] is on_start

    def test_package_rejects_unsafe_uat_args_in_core(self, temp_project):
        """Direct core callers must not bypass argv safety validation."""
        from cli_anything.unreal.core.build import package_project

        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.find_engine_root",
            return_value=self._mock_engine_root(),
        ), patch(
            "cli_anything.unreal.core.build.run_uat",
        ) as mock_run:
            result = package_project(
                temp_project["uproject"],
                uat_args=['-x=" & echo PWNED & rem "'],
            )

        assert result["status"] == "error"
        assert "unsafe" in result["error"].lower()
        mock_run.assert_not_called()

    def test_package_default_output_dir(self, temp_project):
        from cli_anything.unreal.core.build import package_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 0, "log_file": "", "duration_seconds": 0.0,
             }):
            result = package_project(temp_project["uproject"])
            assert result["output_dir"].endswith("Packaged")

    def test_package_custom_output_dir(self, temp_project):
        from cli_anything.unreal.core.build import package_project

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]), \
             patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 0, "log_file": "", "duration_seconds": 0.0,
             }):
            result = package_project(temp_project["uproject"], output_dir="D:/Out")
            assert result["output_dir"] == "D:/Out"

    def test_stop_build_calls_kill(self, temp_project):
        from cli_anything.unreal.core.build import stop_build

        with patch("cli_anything.unreal.core.build.kill_build_processes", return_value={
            "killed": [100, 200], "remaining": [], "status": "ok",
        }):
            result = stop_build(temp_project["uproject"])
            assert result["status"] == "ok"
            assert 100 in result["killed"]

    def test_is_building_calls_find(self, temp_project):
        from cli_anything.unreal.core.build import is_building

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[
            {"pid": 100, "name": "MSBuild.exe", "cmdline": "", "project": ""},
        ]):
            result = is_building(temp_project["uproject"])
            assert result["building"] is True

    def test_generate_project_files_uat_fallback(self, temp_project):
        """generate_project_files uses UAT fallback when bat not found."""
        from cli_anything.unreal.core.build import generate_project_files

        with patch("cli_anything.unreal.core.build.find_engine_root", return_value=self._mock_engine_root()), \
             patch("cli_anything.unreal.core.build.find_generate_project_files", return_value=None), \
             patch("cli_anything.unreal.core.build.run_uat", return_value={
                 "returncode": 0, "log_file": r"F:\Test\Saved\Logs\cli_genproj.log",
                 "duration_seconds": 4.0,
             }):
            result = generate_project_files(temp_project["uproject"])
            assert result["status"] == "ok"
            assert result["log_file"].endswith("cli_genproj.log")


# ═══════════════════════════════════════════════════════════════════════
#  Test build stop / is-building / no-timeout (new features)
# ═══════════════════════════════════════════════════════════════════════


class TestBuildStopAndDetect:
    """Tests for build stop, is-building, and timeout removal."""

    def test_compile_no_timeout_param(self, temp_project):
        """compile_project() no longer accepts timeout."""
        from cli_anything.unreal.core.build import compile_project
        import inspect

        sig = inspect.signature(compile_project)
        assert "timeout" not in sig.parameters

    def test_cook_no_timeout_param(self, temp_project):
        """cook_content() no longer accepts timeout."""
        from cli_anything.unreal.core.build import cook_content
        import inspect

        sig = inspect.signature(cook_content)
        assert "timeout" not in sig.parameters

    def test_package_no_timeout_param(self, temp_project):
        """package_project() no longer accepts timeout."""
        from cli_anything.unreal.core.build import package_project
        import inspect

        sig = inspect.signature(package_project)
        assert "timeout" not in sig.parameters

    def test_generate_no_timeout_param(self):
        """generate_project_files() no longer accepts timeout."""
        from cli_anything.unreal.core.build import generate_project_files
        import inspect

        sig = inspect.signature(generate_project_files)
        assert "timeout" not in sig.parameters

    def test_is_building_no_processes(self, temp_project):
        """is_building returns False when no build processes are running."""
        from cli_anything.unreal.core.build import is_building

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]):
            result = is_building(temp_project["uproject"])
            assert result["building"] is False
            assert result["processes"] == []

    def test_is_building_with_processes(self, temp_project):
        """is_building returns True when build processes are detected."""
        from cli_anything.unreal.core.build import is_building

        mock_procs = [
            {"pid": 1234, "name": "MSBuild.exe", "cmdline": "MSBuild.exe project.vcxproj", "project": temp_project["uproject"]},
        ]
        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=mock_procs):
            result = is_building(temp_project["uproject"])
            assert result["building"] is True
            assert len(result["processes"]) == 1

    def test_is_building_with_active_task_but_no_detectable_processes(
        self, temp_project
    ):
        """Persistent task ownership must cover dotnet/bk process scan gaps."""
        from cli_anything.unreal.core.build import is_building

        active_task = {
            "task_id": "t-active-build",
            "command": "build.compile",
            "status": "running",
            "worker_pid": 98136,
            "pid": 55856,
            "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
        }
        with patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
        ) as process_probe, patch(
            "cli_anything.unreal.core.tasks.active_build_tasks",
            return_value=[active_task],
        ):
            result = is_building(temp_project["uproject"])

        assert result["building"] is True
        assert result["count"] == 0
        assert result["active_task_count"] == 1
        assert result["active_tasks"] == [{
            "task_id": "t-active-build",
            "command": "build.compile",
            "status": "running",
            "worker_pid": 98136,
            "pid": 55856,
            "log_file": r"F:\Test\Saved\Logs\cli_compile.log",
        }]
        assert result["process_probe"] == {
            "status": "skipped",
            "reason": "active_task_state",
        }
        process_probe.assert_not_called()

    def test_is_building_requires_conclusive_process_probe_without_tasks(
        self, temp_project
    ):
        from cli_anything.unreal.core.build import is_building

        with patch(
            "cli_anything.unreal.core.tasks.active_build_tasks",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ) as process_probe:
            result = is_building(temp_project["uproject"])

        assert result["building"] is False
        assert result["process_probe"] == {"status": "ok"}
        process_probe.assert_called_once_with(
            temp_project["uproject"],
            include_cmdline=False,
            query_timeout=3,
            fail_on_error=True,
        )

    def test_reconcile_task_state_fails_when_tracked_processes_exited(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A dead worker/build pair must not leave a task permanently running."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update(status="running", worker_pid=62224, pid=107964)
        tasks.save_task(task)

        with patch.object(
            tasks,
            "_query_process_info",
            side_effect=lambda pid: {
                "query_ok": True,
                "found": False,
                "pid": pid,
            },
        ) as process_query:
            result = tasks.reconcile_task_state(task["task_id"])

        assert result["status"] == "failed"
        assert result["error"]["code"] == "TASK_WORKER_EXITED"
        assert result["reconciliation"]["outcome"] == "failed"
        assert [
            item["state"] for item in result["reconciliation"]["processes"]
        ] == ["exited", "exited"]
        assert tasks.task_progress(result)["reconciliation"] == result["reconciliation"]
        assert [call.args[0] for call in process_query.call_args_list] == [
            62224,
            107964,
        ]

    def test_reconcile_task_state_keeps_unknown_worker_active(
        self, temp_project, tmp_path, monkeypatch
    ):
        """An inconclusive worker probe must never become a false failure."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update(status="running", worker_pid=62225, pid=107965)
        tasks.save_task(task)
        with patch.object(
            tasks,
            "_query_process_info",
            side_effect=lambda pid: (
                {
                    "query_ok": False,
                    "found": False,
                    "pid": pid,
                    "error": "CIM timed out",
                }
                if pid == 62225
                else {"query_ok": True, "found": False, "pid": pid}
            ),
        ):
            result = tasks.reconcile_task_state(task["task_id"])

        assert result["status"] == "running"
        assert "reconciliation" not in result

    def test_active_build_tasks_reconciles_dead_task(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Busy-state lookup removes a stale task and persists its failure."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task(
            "build.package",
            {"project_path": temp_project["uproject"]},
        )
        task.update(status="running", worker_pid=62226, pid=107966)
        tasks.save_task(task)
        with patch.object(
            tasks,
            "_query_process_info",
            side_effect=lambda pid: {
                "query_ok": True,
                "found": False,
                "pid": pid,
            },
        ):
            active = tasks.active_build_tasks(temp_project["uproject"])

        assert active == []
        saved = tasks.load_task(task["task_id"])
        assert saved["status"] == "failed"
        assert saved["error"]["code"] == "TASK_WORKER_EXITED"

    def test_is_building_rechecks_processes_after_stale_task_reconciliation(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A reconciled task no longer blocks a conclusive idle result."""
        from cli_anything.unreal.core import tasks
        from cli_anything.unreal.core.build import is_building

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update(status="running", worker_pid=62227, pid=107967)
        tasks.save_task(task)
        with patch.object(
            tasks,
            "_query_process_info",
            side_effect=lambda pid: {
                "query_ok": True,
                "found": False,
                "pid": pid,
            },
        ), patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            return_value=[],
        ) as process_probe:
            result = is_building(temp_project["uproject"])

        assert result["building"] is False
        assert result["active_task_count"] == 0
        assert result["process_probe"] == {"status": "ok"}
        process_probe.assert_called_once()

    def test_compile_rejects_if_already_building(self, temp_project):
        """compile_project returns error when a build is already running."""
        from cli_anything.unreal.core.build import compile_project

        mock_procs = [
            {"pid": 1234, "name": "MSBuild.exe", "cmdline": "MSBuild.exe project.vcxproj", "project": temp_project["uproject"]},
        ]
        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=mock_procs):
            result = compile_project(temp_project["uproject"])
            assert result["status"] == "error"
            assert "already in progress" in result["error"].lower()

    def test_cook_rejects_if_already_building(self, temp_project):
        """cook_content returns error when a build is already running."""
        from cli_anything.unreal.core.build import cook_content

        mock_procs = [
            {"pid": 1234, "name": "MSBuild.exe", "cmdline": "MSBuild.exe project.vcxproj", "project": temp_project["uproject"]},
        ]
        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=mock_procs):
            result = cook_content(temp_project["uproject"])
            assert result["status"] == "error"
            assert "already in progress" in result["error"].lower()

    def test_package_rejects_if_already_building(self, temp_project):
        """package_project returns error when a build is already running."""
        from cli_anything.unreal.core.build import package_project

        mock_procs = [
            {"pid": 1234, "name": "MSBuild.exe", "cmdline": "MSBuild.exe project.vcxproj", "project": temp_project["uproject"]},
        ]
        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=mock_procs):
            result = package_project(temp_project["uproject"])
            assert result["status"] == "error"
            assert "already in progress" in result["error"].lower()

    def test_stop_build_no_processes(self, temp_project):
        """stop_build returns 'none' when no processes to kill."""
        from cli_anything.unreal.core.build import stop_build

        with patch("cli_anything.unreal.core.build.kill_build_processes", return_value={
            "killed": [], "remaining": [], "status": "none",
        }):
            result = stop_build(temp_project["uproject"])
            assert result["status"] == "none"
            assert result["killed"] == []

    def test_stop_build_success(self, temp_project):
        """stop_build kills processes and returns 'ok'."""
        from cli_anything.unreal.core.build import stop_build

        with patch("cli_anything.unreal.core.build.kill_build_processes", return_value={
            "killed": [1234, 5678], "remaining": [], "status": "ok",
        }):
            result = stop_build(temp_project["uproject"])
            assert result["status"] == "ok"
            assert 1234 in result["killed"]

    def test_stop_build_partial(self, temp_project):
        """stop_build returns 'partial' when some processes survive."""
        from cli_anything.unreal.core.build import stop_build

        with patch("cli_anything.unreal.core.build.kill_build_processes", return_value={
            "killed": [1234], "remaining": [9999], "status": "partial",
        }):
            result = stop_build(temp_project["uproject"])
            assert result["status"] == "partial"
            assert 9999 in result["remaining"]

    def test_stop_build_probe_timeout_is_explicit_partial_result(
        self, temp_project
    ):
        """An inconclusive final scan must not look like no processes exist."""
        from cli_anything.unreal.core.build import stop_build
        from cli_anything.unreal.utils.ue_backend import BuildProcessProbeError

        failure = BuildProcessProbeError(
            "Windows build-process query timed out after 3 seconds.",
            details={"reason": "timeout", "timeout_seconds": 3},
        )
        with patch(
            "cli_anything.unreal.core.build.kill_build_processes",
            side_effect=failure,
        ):
            result = stop_build(temp_project["uproject"])

        assert result["status"] == "partial"
        assert result["process_probe"] == {
            "status": "failed",
            "message": "Windows build-process query timed out after 3 seconds.",
            "details": {"reason": "timeout", "timeout_seconds": 3},
        }

    def test_stop_build_cancels_matching_async_task(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Project stop must use task ownership when process scanning finds nothing."""
        from cli_anything.unreal.core.build import stop_build
        from cli_anything.unreal.core.tasks import create_task, load_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 48788, "pid": 98612})
        save_task(task)

        killed = []

        def fake_kill(pid):
            killed.append(pid)
            return {
                "ok": True,
                "pid": pid,
                "method": "taskkill" if pid == 48788 else "taskkill_already_exited",
                "already_exited": pid == 98612,
            }

        def fake_process_info(pid):
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 1 if pid == 48788 else 48788,
                "name": "python.exe" if pid == 48788 else "powershell.exe",
                "cmdline": (
                    f"python -m cli_anything.unreal _task-worker run {task['task_id']}"
                    if pid == 48788 else "powershell.exe -EncodedCommand AAA="
                ),
            }

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        with patch(
            "cli_anything.unreal.core.build.kill_build_processes",
            return_value=no_processes,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ), patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            side_effect=fake_kill,
        ):
            result = stop_build(temp_project["uproject"])

        saved = load_task(task["task_id"])
        assert killed == [48788, 98612]
        assert saved["status"] == "cancelled"
        assert saved["cancelled"] is True
        assert result["status"] == "ok"
        assert result["killed"] == [48788]
        assert result["remaining"] == []
        assert result["tasks"][0]["task_id"] == task["task_id"]
        assert result["tasks"][0]["status"] == "cancelled"

    def test_stop_build_reconciles_task_when_final_scan_kills_remaining_pid(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A successful final scan must clear task-level cancellation failures."""
        from cli_anything.unreal.core.build import stop_build
        from cli_anything.unreal.core.tasks import create_task, load_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({
            "status": "running",
            "cancelled": False,
            "cancel_result": {"killed": [], "remaining": [49001]},
        })
        save_task(task)

        with patch(
            "cli_anything.unreal.core.tasks.active_build_tasks",
            return_value=[task],
        ), patch(
            "cli_anything.unreal.core.tasks.cancel_task",
            side_effect=lambda task_id: load_task(task_id),
        ), patch(
            "cli_anything.unreal.core.build.kill_build_processes",
            return_value={"killed": [49001], "remaining": [], "status": "ok"},
        ):
            result = stop_build(temp_project["uproject"])

        saved = load_task(task["task_id"])
        assert result["status"] == "ok"
        assert result["remaining"] == []
        assert result["tasks"][0]["status"] == "cancelled"
        assert result["tasks"][0]["remaining"] == []
        assert saved["status"] == "cancelled"
        assert saved["cancelled"] is True
        assert saved["cancel_result"]["remaining"] == []
        assert saved["cancel_result"]["killed"] == [49001]


    def test_stop_build_does_not_kill_reused_task_pids(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A stale running task must not kill unrelated processes reusing its PIDs."""
        from cli_anything.unreal.core.build import stop_build
        from cli_anything.unreal.core.tasks import create_task, load_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 48788, "pid": 98612})
        save_task(task)

        def fake_process_info(pid):
            if pid == 48788:
                return {
                    "query_ok": True,
                    "found": True,
                    "pid": pid,
                    "parent_pid": 1684,
                    "name": "TiWorker.exe",
                    "cmdline": "TiWorker.exe -Embedding",
                }
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 1234,
                "name": "python.exe",
                "cmdline": "python unrelated.py",
            }

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        with patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
            create=True,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result"
        ) as kill, patch(
            "cli_anything.unreal.core.build.kill_build_processes",
            return_value=no_processes,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ):
            result = stop_build(temp_project["uproject"])

        kill.assert_not_called()
        saved = load_task(task["task_id"])
        assert saved["status"] == "cancelled"
        assert result["status"] == "ok"
        assert result["killed"] == []
        assert all(
            process.get("ownership_mismatch")
            for process in saved["cancel_result"]["processes"]
        )

    def test_save_task_keeps_previous_record_when_write_is_interrupted(
        self, tmp_path, monkeypatch
    ):
        """Task updates must replace atomically instead of truncating live JSON."""
        from cli_anything.unreal.core.tasks import create_task, load_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("build.compile", {"project_path": "P.uproject"})
        original_write = Path.write_text

        def interrupted_write(path, data, *args, **kwargs):
            if task["task_id"] in path.name:
                original_write(path, "{", encoding="utf-8")
                raise OSError("interrupted task write")
            return original_write(path, data, *args, **kwargs)

        updated = dict(task, status="running")
        with patch.object(Path, "write_text", new=interrupted_write):
            with pytest.raises(OSError, match="interrupted task write"):
                save_task(updated)

        assert load_task(task["task_id"])["status"] == "submitted"

    def test_save_task_retries_transient_windows_replace_permission_error(
        self, tmp_path, monkeypatch
    ):
        """Windows sharing violations must not turn a live task into INTERNAL_ERROR."""
        from contextlib import nullcontext

        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("editor.launch", {"project_path": "P.uproject"})
        original_replace = tasks.os.replace
        attempts = []

        def sharing_violation_then_replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) < 3:
                raise PermissionError(13, "Permission denied", str(destination))
            return original_replace(source, destination)

        task["status"] = "running"
        with patch.object(tasks.sys, "platform", "win32"), patch.object(
            tasks,
            "_task_lock",
            side_effect=lambda task_id: nullcontext(),
        ), patch.object(
            tasks.os,
            "replace",
            side_effect=sharing_violation_then_replace,
        ), patch.object(tasks.time, "sleep") as sleep:
            tasks.save_task(task)

        assert len(attempts) == 3
        assert sleep.call_count == 2
        assert tasks.load_task(task["task_id"])["status"] == "running"

    def test_load_task_uses_task_lock(self, tmp_path, monkeypatch):
        """Readers must share the writer lock so os.replace is safe on Windows."""
        from contextlib import contextmanager

        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("editor.launch", {"project_path": "P.uproject"})
        lock_events = []

        @contextmanager
        def recording_lock(task_id):
            lock_events.append(("enter", task_id))
            yield
            lock_events.append(("exit", task_id))

        with patch.object(tasks, "_task_lock", side_effect=recording_lock):
            loaded = tasks.load_task(task["task_id"])

        assert loaded["task_id"] == task["task_id"]
        assert lock_events == [
            ("enter", task["task_id"]),
            ("exit", task["task_id"]),
        ]

    def test_load_task_lock_timeout_is_bounded(self, tmp_path, monkeypatch):
        """Status callers can bound reads while a worker owns the task lock."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        with patch.object(tasks.sys, "platform", "win32"), patch(
            "msvcrt.locking",
            side_effect=OSError("locked"),
        ), patch.object(
            tasks.time,
            "monotonic",
            side_effect=[100.0, 100.2],
        ):
            with pytest.raises(tasks.TaskLockTimeout) as exc_info:
                tasks.load_task("t-blocked", timeout=0.1)

        assert exc_info.value.task_id == "t-blocked"
        assert exc_info.value.timeout == 0.1

    def test_submit_task_keeps_live_worker_after_task_record_permission_error(
        self, tmp_path, monkeypatch
    ):
        """A started worker remains valid if its PID write is briefly blocked."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        worker = MagicMock(pid=41652)
        with patch.object(tasks.subprocess, "Popen", return_value=worker), patch.object(
            tasks,
            "update_task_fields",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            task = tasks.submit_task(
                "editor.launch",
                {"project_path": "P.uproject"},
            )

        assert task["status"] == "submitted"
        assert task["worker_pid"] == 41652
        assert tasks.load_task(task["task_id"])["task_id"] == task["task_id"]

    def test_spawn_worker_persists_native_process_identity(
        self, tmp_path, monkeypatch
    ):
        """Cancellation can verify the worker later without querying CIM."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("build.compile", {"project_path": "P.uproject"})
        worker = MagicMock(pid=41653)
        identity = {
            "pid": 41653,
            "creation_time": 123456789,
            "image_path": r"D:\Python\python.exe",
            "identity_source": "win32_process_times",
        }
        with patch.object(tasks.subprocess, "Popen", return_value=worker), patch.object(
            tasks,
            "_capture_windows_process_identity",
            return_value=identity,
        ):
            tasks.spawn_worker(task["task_id"])

        saved = tasks.load_task(task["task_id"])
        assert saved["worker_pid"] == 41653
        assert saved["worker_process_identity"] == identity

    def test_spawn_worker_retries_without_breakaway_on_access_denied(
        self, tmp_path, monkeypatch
    ):
        """Restrictive caller jobs still allow a worker inherited into the job."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("editor.launch", {"project_path": "P.uproject"})
        access_denied = PermissionError(13, "Access is denied")
        access_denied.winerror = 5
        worker = MagicMock(pid=41655)

        with patch.object(tasks.sys, "platform", "win32"), patch.object(
            tasks.subprocess,
            "Popen",
            side_effect=[access_denied, worker],
        ) as popen, patch.object(
            tasks,
            "_capture_windows_process_identity",
            return_value=None,
        ):
            worker_pid = tasks.spawn_worker(task["task_id"])

        breakaway_flag = tasks.subprocess.CREATE_BREAKAWAY_FROM_JOB
        first_flags = popen.call_args_list[0].kwargs["creationflags"]
        fallback_flags = popen.call_args_list[1].kwargs["creationflags"]
        assert worker_pid == 41655
        assert first_flags & breakaway_flag
        assert fallback_flags == first_flags & ~breakaway_flag
        saved = tasks.load_task(task["task_id"])
        assert saved["worker_pid"] == 41655
        assert saved["worker_spawn"]["fallback_used"] is True
        assert saved["worker_spawn"]["initial_attempt"]["error"]["winerror"] == 5
        assert tasks.task_progress(saved)["worker_spawn"] == saved["worker_spawn"]

    def test_spawn_worker_reports_both_errors_when_breakaway_fallback_fails(
        self, tmp_path, monkeypatch
    ):
        """Both process-creation attempts remain available as structured diagnostics."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("editor.launch", {"project_path": "P.uproject"})
        initial_error = PermissionError(13, "Access is denied")
        initial_error.winerror = 5
        fallback_error = PermissionError(13, "Fallback access is denied")
        fallback_error.winerror = 5

        with patch.object(tasks.sys, "platform", "win32"), patch.object(
            tasks.subprocess,
            "Popen",
            side_effect=[initial_error, fallback_error],
        ) as popen:
            with pytest.raises(tasks.TaskWorkerSpawnError) as exc_info:
                tasks.spawn_worker(task["task_id"])

        assert popen.call_count == 2
        assert exc_info.value.code == "TASK_WORKER_SPAWN_FAILED"
        assert exc_info.value.details["fallback_attempted"] is True
        assert [attempt["mode"] for attempt in exc_info.value.attempts] == [
            "with_breakaway",
            "without_breakaway",
        ]
        saved = tasks.load_task(task["task_id"])
        assert saved["status"] == "failed"
        assert saved["error"] == exc_info.value.as_task_error()

    def test_spawn_worker_does_not_retry_unrelated_windows_error(
        self, tmp_path, monkeypatch
    ):
        """Only access denied from breakaway creation gets the compatibility retry."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("editor.launch", {"project_path": "P.uproject"})
        sharing_error = PermissionError(13, "File is in use")
        sharing_error.winerror = 32

        with patch.object(tasks.sys, "platform", "win32"), patch.object(
            tasks.subprocess,
            "Popen",
            side_effect=sharing_error,
        ) as popen:
            with pytest.raises(tasks.TaskWorkerSpawnError) as exc_info:
                tasks.spawn_worker(task["task_id"])

        assert popen.call_count == 1
        assert exc_info.value.details["fallback_attempted"] is False
        assert exc_info.value.attempts[0]["error"]["winerror"] == 32

    def test_build_task_persists_native_root_process_identity(
        self, temp_project, tmp_path, monkeypatch
    ):
        """The build root gets its own PID-reuse-safe creation token."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        identity = {
            "pid": 41654,
            "creation_time": 987654321,
            "image_path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "identity_source": "win32_process_times",
        }

        def fake_compile(**kwargs):
            kwargs["on_start"](MagicMock(pid=41654))
            return {"status": "ok", "log_file": "build.log"}

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=fake_compile,
        ), patch.object(
            tasks,
            "_capture_windows_process_identity",
            return_value=identity,
        ):
            tasks._run_build_task(
                task,
                "compile_project",
                estimated_total_seconds=600,
            )

        saved = tasks.load_task(task["task_id"])
        assert saved["pid"] == 41654
        assert saved["build_process_identity"] == identity

    def test_wait_for_task_keeps_polling_after_task_record_permission_error(
        self, tmp_path, monkeypatch
    ):
        """Foreground wait must continue while an existing worker task is updating."""
        from cli_anything.unreal.core import tasks

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = tasks.create_task("editor.launch", {"project_path": "P.uproject"})
        completed = dict(task, status="completed", result={"status": "online"})

        with patch.object(
            tasks,
            "load_task",
            side_effect=[PermissionError(13, "Permission denied"), completed],
        ), patch.object(tasks.time, "sleep") as sleep:
            result = tasks.wait_for_task(task["task_id"], timeout=5)

        assert result == completed
        sleep.assert_called_once_with(0.5)

    def test_create_task_normalizes_project_path_for_cross_cwd_stop(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Task ownership must survive compile and stop running from different cwd."""
        from cli_anything.unreal.core.tasks import (
            active_build_tasks,
            create_task,
            save_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        project = Path(temp_project["uproject"])
        monkeypatch.chdir(project.parent)
        task = create_task("build.compile", {"project_path": project.name})
        task["status"] = "running"
        save_task(task)

        monkeypatch.chdir(tmp_path)
        matches = active_build_tasks(str(project))

        assert [item["task_id"] for item in matches] == [task["task_id"]]
        assert Path(matches[0]["payload"]["project_path"]).is_absolute()

    def test_cancel_task_reconciles_failed_kill_with_later_scan_success(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A PID killed by the final scan must not remain in remaining."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 41001, "pid": 41002})
        save_task(task)

        def fake_process_info(pid):
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 1 if pid == 41001 else 41001,
                "name": "python.exe" if pid == 41001 else "powershell.exe",
                "cmdline": (
                    f"python -m cli_anything.unreal _task-worker run {task['task_id']}"
                    if pid == 41001 else "powershell.exe -EncodedCommand AAA="
                ),
            }

        def fake_kill(pid):
            if pid == 41001:
                return {"ok": False, "pid": pid, "error": "first kill failed"}
            return {"ok": True, "pid": pid}

        scan_result = {"killed": [41001], "remaining": [], "status": "ok"}
        with patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
            create=True,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            side_effect=fake_kill,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=scan_result,
        ):
            result = cancel_task(task["task_id"])

        assert result["status"] == "cancelled"
        assert result["cancel_result"]["killed"] == [41002, 41001]
        assert result["cancel_result"]["remaining"] == []

    def test_cancel_task_persists_incomplete_runtime_dependency_warning(
        self, temp_project, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import (
            cancel_task,
            create_task,
            save_task,
            task_progress,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {
                "project_path": temp_project["uproject"],
                "engine_root": r"F:\Engine",
                "platform": "Win64",
                "build_config": "Development",
            },
        )
        task["status"] = "running"
        save_task(task)
        integrity = {
            "status": "incomplete",
            "code": "BUILD_CANCELLED_OUTPUTS_INCOMPLETE",
            "message": "Two runtime dependencies are missing.",
            "recovery_command": "ue-cli full-build",
        }

        with patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value={"killed": [], "remaining": [], "status": "none"},
        ), patch(
            "cli_anything.unreal.core.build."
            "inspect_win64_editor_runtime_dependencies",
            return_value=integrity,
        ) as inspect:
            result = cancel_task(task["task_id"])

        assert result["status"] == "cancelled"
        assert result["output_integrity"] == integrity
        assert task_progress(result)["output_integrity"] == integrity
        inspect.assert_called_once_with(
            temp_project["uproject"],
            r"F:\Engine",
            "Development",
        )

    def test_cancel_task_rechecks_root_identity_after_worker_exit(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A root PID reused after Job close must not be killed."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 42001, "pid": 42002})
        save_task(task)
        root_queries = {"count": 0}

        def fake_process_info(pid):
            if pid == 42001:
                return {
                    "query_ok": True,
                    "found": True,
                    "pid": pid,
                    "parent_pid": 1,
                    "name": "python.exe",
                    "cmdline": f"python _task-worker run {task['task_id']}",
                    "creation_date": "worker-created",
                }
            root_queries["count"] += 1
            if root_queries["count"] == 1:
                return {
                    "query_ok": True,
                    "found": True,
                    "pid": pid,
                    "parent_pid": 42001,
                    "name": "powershell.exe",
                    "cmdline": "powershell.exe -EncodedCommand AAA=",
                    "creation_date": "build-created",
                }
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 999,
                "name": "unrelated.exe",
                "cmdline": "unrelated.exe",
                "creation_date": "reused-created",
            }

        killed = []

        def fake_kill(pid):
            killed.append(pid)
            return {"ok": True, "pid": pid}

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        with patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            side_effect=fake_kill,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ):
            result = cancel_task(task["task_id"])

        assert killed == [42001]
        assert root_queries["count"] >= 2
        assert result["status"] == "cancelled"
        root_result = next(
            item for item in result["cancel_result"]["processes"]
            if item["role"] == "build"
        )
        assert root_result["ownership_mismatch"] is True

    def test_cancel_task_rechecks_worker_identity_immediately_before_kill(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A worker PID reused after initial discovery must not be killed."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 42501, "pid": 42502})
        save_task(task)
        worker_queries = {"count": 0}

        def fake_process_info(pid):
            if pid == 42501:
                worker_queries["count"] += 1
                if worker_queries["count"] == 1:
                    return {
                        "query_ok": True,
                        "found": True,
                        "pid": pid,
                        "parent_pid": 1,
                        "name": "python.exe",
                        "cmdline": f"python _task-worker run {task['task_id']}",
                        "creation_date": "worker-created",
                    }
                return {
                    "query_ok": True,
                    "found": True,
                    "pid": pid,
                    "parent_pid": 999,
                    "name": "unrelated.exe",
                    "cmdline": "unrelated.exe",
                    "creation_date": "reused-created",
                }
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 42501,
                "name": "powershell.exe",
                "cmdline": "powershell.exe -EncodedCommand AAA=",
                "creation_date": "build-created",
            }

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        with patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result"
        ) as kill, patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ):
            result = cancel_task(task["task_id"])

        kill.assert_not_called()
        assert worker_queries["count"] >= 2
        assert result["status"] == "cancelled"

    def test_cancel_task_drops_failed_pid_that_exited_before_final_check(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A failed first kill is not remaining when the owned PID has exited."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 43001})
        save_task(task)
        queries = {"count": 0}

        def fake_process_info(pid):
            queries["count"] += 1
            if queries["count"] == 1:
                return {
                    "query_ok": True,
                    "found": True,
                    "pid": pid,
                    "parent_pid": 1,
                    "name": "python.exe",
                    "cmdline": f"python _task-worker run {task['task_id']}",
                    "creation_date": "worker-created",
                }
            return {"query_ok": True, "found": False, "pid": pid}

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        with patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            return_value={"ok": False, "pid": 43001, "error": "transient"},
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ):
            result = cancel_task(task["task_id"])

        assert queries["count"] >= 2
        assert result["status"] == "cancelled"
        assert result["cancel_result"]["remaining"] == []

    def test_cancel_task_keeps_pid_when_initial_identity_query_failed(
        self, temp_project, tmp_path, monkeypatch
    ):
        """An incomplete first snapshot cannot prove a live PID was reused."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({"status": "running", "worker_pid": 43501})
        save_task(task)
        queries = {"count": 0}

        def fake_process_info(pid):
            queries["count"] += 1
            if queries["count"] == 1:
                return {
                    "query_ok": False,
                    "found": False,
                    "pid": pid,
                    "error": "CIM temporarily unavailable",
                }
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 1,
                "name": "python.exe",
                "cmdline": f"python _task-worker run {task['task_id']}",
                "creation_date": "worker-created",
            }

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        with patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ):
            result = cancel_task(task["task_id"])

        assert result["status"] == "running"
        assert result["cancelled"] is False
        assert result["cancel_result"]["remaining"] == [43501]

    def test_cancel_task_uses_recorded_identity_without_cim(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A matching creation token is sufficient for safe direct termination."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({
            "status": "running",
            "worker_pid": 43601,
            "pid": 43602,
            "worker_process_identity": {
                "pid": 43601,
                "creation_time": 1001,
            },
            "build_process_identity": {
                "pid": 43602,
                "creation_time": 1002,
            },
        })
        save_task(task)

        def native_identity(pid):
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "creation_time": 1001 if pid == 43601 else 1002,
                "identity_source": "win32_process_times",
            }

        with patch(
            "cli_anything.unreal.utils.ue_backend._windows_process_identity",
            side_effect=native_identity,
        ), patch(
            "cli_anything.unreal.core.tasks._query_process_info",
        ) as cim_query, patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            side_effect=lambda pid: {"ok": True, "pid": pid},
        ) as kill, patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
        ) as process_scan:
            result = cancel_task(task["task_id"])

        assert result["status"] == "cancelled"
        assert result["cancel_result"]["remaining"] == []
        assert result["stop_result"]["reason"] == "tracked_process_identities"
        assert [call.args[0] for call in kill.call_args_list] == [43601, 43602]
        cim_query.assert_not_called()
        process_scan.assert_not_called()

    def test_cancel_task_reports_native_kill_failure_without_cim_scan(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A failed taskkill returns diagnostics promptly even if CIM is unusable."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.package",
            {"project_path": temp_project["uproject"]},
        )
        task.update({
            "status": "running",
            "worker_pid": 43611,
            "worker_process_identity": {
                "pid": 43611,
                "creation_time": 1011,
            },
        })
        save_task(task)
        native_identity = {
            "query_ok": True,
            "found": True,
            "pid": 43611,
            "creation_time": 1011,
            "identity_source": "win32_process_times",
        }
        failed_kill = {
            "ok": False,
            "pid": 43611,
            "method": "taskkill",
            "timeout": True,
            "error": "taskkill timed out",
        }

        with patch(
            "cli_anything.unreal.utils.ue_backend._windows_process_identity",
            return_value=native_identity,
        ), patch(
            "cli_anything.unreal.core.tasks._query_process_info",
        ) as cim_query, patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            return_value=failed_kill,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
        ) as process_scan:
            result = cancel_task(task["task_id"])

        assert result["status"] == "running"
        assert result["error"]["code"] == "TASK_CANCEL_FAILED"
        assert result["cancel_result"]["remaining"] == [43611]
        assert result["cancel_result"]["processes"][0]["error"] == "taskkill timed out"
        cim_query.assert_not_called()
        process_scan.assert_not_called()

    def test_cancel_task_skips_reused_pid_with_recorded_identity(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A changed creation token prevents termination of an unrelated PID."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({
            "status": "running",
            "worker_pid": 43701,
            "worker_process_identity": {
                "pid": 43701,
                "creation_time": 2001,
            },
        })
        save_task(task)
        reused = {
            "query_ok": True,
            "found": True,
            "pid": 43701,
            "creation_time": 9999,
            "identity_source": "win32_process_times",
        }

        with patch(
            "cli_anything.unreal.utils.ue_backend._windows_process_identity",
            return_value=reused,
        ), patch(
            "cli_anything.unreal.core.tasks._query_process_info",
        ) as cim_query, patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
        ) as kill:
            result = cancel_task(task["task_id"])

        assert result["status"] == "cancelled"
        process = result["cancel_result"]["processes"][0]
        assert process["ownership_mismatch"] is True
        assert process["process"]["identity_matches"] is False
        cim_query.assert_not_called()
        kill.assert_not_called()

    def test_cancel_task_falls_back_to_cim_when_native_identity_fails(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Legacy CIM verification remains available if Win32 access fails."""
        from cli_anything.unreal.core.tasks import cancel_task, create_task, save_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task.update({
            "status": "running",
            "worker_pid": 43801,
            "worker_process_identity": {
                "pid": 43801,
                "creation_time": 3001,
            },
        })
        save_task(task)
        native_failure = {
            "query_ok": False,
            "found": True,
            "pid": 43801,
            "error": "access denied",
        }
        cim_identity = {
            "query_ok": True,
            "found": True,
            "pid": 43801,
            "parent_pid": 1,
            "name": "python.exe",
            "cmdline": f"python _task-worker run {task['task_id']}",
            "creation_date": "worker-created",
        }

        with patch(
            "cli_anything.unreal.utils.ue_backend._windows_process_identity",
            return_value=native_failure,
        ), patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            return_value=cim_identity,
        ) as cim_query, patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            return_value={"ok": True, "pid": 43801},
        ) as kill:
            result = cancel_task(task["task_id"])

        assert result["status"] == "cancelled"
        assert cim_query.call_count == 2
        kill.assert_called_once_with(43801)

    def test_update_task_fields_preserves_concurrent_metadata(
        self, tmp_path, monkeypatch
    ):
        """Field updates must merge instead of replacing another process's fields."""
        from cli_anything.unreal.core.tasks import (
            create_task,
            load_task,
            transition_task,
            update_task_fields,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("build.compile", {"project_path": "P.uproject"})

        update_task_fields(task["task_id"], worker_pid=44001)
        transition_task(task["task_id"], status="running", started_at=123.0)

        saved = load_task(task["task_id"])
        assert saved["worker_pid"] == 44001
        assert saved["status"] == "running"
        assert saved["started_at"] == 123.0

        with pytest.raises(ValueError, match="transition_task"):
            update_task_fields(task["task_id"], status="failed")

    def test_transition_task_terminal_state_rejects_late_worker_result(
        self, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import create_task, transition_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("editor.launch", {"project_path": "P.uproject"})
        transition_task(task["task_id"], status="running", phase="spawning")
        cancelled = transition_task(
            task["task_id"],
            status="cancelled",
            phase="exited",
            error=None,
            cancelled=True,
        )

        late = transition_task(
            task["task_id"],
            status="completed",
            phase="online",
            result_patch={"late_worker": True},
        )

        assert cancelled["status"] == "cancelled"
        assert late["status"] == "cancelled"
        assert late["phase"] == "exited"
        assert "late_worker" not in late.get("result", {})

    def test_transition_task_cancel_request_wins_over_completion(
        self, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import (
            _request_task_cancel,
            create_task,
            transition_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("editor.launch", {"project_path": "P.uproject"})
        transition_task(task["task_id"], status="running", phase="waiting_remote_control")
        _request_task_cancel(task["task_id"])

        result = transition_task(
            task["task_id"],
            status="completed",
            phase="online",
            result_patch={"status": "online"},
        )

        assert result["status"] == "cancelled"
        assert result["phase"] == "exited"
        assert result["cancelled"] is True
        assert "result" not in result

    def test_transition_task_timeout_recovery_requires_expected_state(
        self, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import create_task, transition_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("editor.launch", {"project_path": "P.uproject"})
        transition_task(task["task_id"], status="running", phase="waiting_remote_control")
        transition_task(task["task_id"], status="timeout", phase="blocked")

        rejected = transition_task(task["task_id"], status="completed", phase="online")
        recovered = transition_task(
            task["task_id"],
            expected_statuses={"timeout"},
            status="completed",
            phase="online",
            result_patch={"reconciled": True},
            error=None,
        )

        assert rejected["status"] == "timeout"
        assert rejected["phase"] == "blocked"
        assert recovered["status"] == "completed"
        assert recovered["phase"] == "online"
        assert recovered["result"]["reconciled"] is True

    def test_task_progress_exposes_phase_separately_from_status(
        self, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import create_task, task_progress, transition_task

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("editor.launch", {"project_path": "P.uproject"})
        task = transition_task(
            task["task_id"],
            status="running",
            phase="deploying_bridge",
        )

        progress = task_progress(task)
        assert progress["status"] == "running"
        assert progress["phase"] == "deploying_bridge"

    def test_finalize_build_task_respects_failed_cancel_state(
        self, tmp_path, monkeypatch
    ):
        """Worker finalization must not overwrite a lock-protected cancel failure."""
        from cli_anything.unreal.core.tasks import (
            _finalize_build_task,
            _request_task_cancel,
            create_task,
            load_task,
            transition_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("build.compile", {"project_path": "P.uproject"})
        _request_task_cancel(task["task_id"])
        transition_task(
            task["task_id"],
            status="running",
            cancel_requested=True,
            cancelled=False,
            cancel_result={"killed": [], "remaining": [45001]},
        )

        result = _finalize_build_task(
            task["task_id"],
            {"status": "ok", "log_file": "build.log"},
        )

        saved = load_task(task["task_id"])
        assert result["status"] == "completed"
        assert result["cancelled"] is False
        assert saved["status"] == "completed"
        assert saved["cancel_result"]["remaining"] == [45001]

    def test_finalize_build_task_does_not_overwrite_terminal_failure(
        self, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import (
            _finalize_build_task,
            create_task,
            load_task,
            transition_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("build.compile", {"project_path": "P.uproject"})
        transition_task(task["task_id"], status="running")
        failed = transition_task(
            task["task_id"],
            status="failed",
            error={"code": "TASK_WORKER_EXITED", "message": "worker exited"},
        )

        late = _finalize_build_task(
            task["task_id"],
            {"status": "ok", "log_file": "late.log"},
        )

        assert late == failed
        saved = load_task(task["task_id"])
        assert saved["status"] == "failed"
        assert saved["error"]["code"] == "TASK_WORKER_EXITED"
        assert "result" not in saved

    def test_finalize_build_task_preserves_specific_failure_code(
        self, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import (
            _finalize_build_task,
            create_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task("build.compile", {"project_path": "P.uproject"})

        result = _finalize_build_task(
            task["task_id"],
            {
                "status": "error",
                "code": "INVALID_BUILD_OUTPUT",
                "error": "Compile produced an invalid DLL.",
            },
        )

        assert result["status"] == "failed"
        assert result["error"] == {
            "code": "INVALID_BUILD_OUTPUT",
            "message": "Compile produced an invalid DLL.",
        }

    def test_run_build_task_forwards_targeted_package_options(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Async package workers must preserve targeted UAT options."""
        from cli_anything.unreal.core.tasks import (
            _run_build_task,
            create_task,
            save_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.package",
            {
                "project_path": temp_project["uproject"],
                "platform": "Android",
                "build_config": "Development",
                "output_dir": "D:/Out",
                "maps": ["/Game/Maps/Oregon_Main"],
                "cook_flavor": "ASTC",
                "uat_args": ["-pak", "-iostore"],
            },
        )
        task["status"] = "running"
        save_task(task)

        with patch(
            "cli_anything.unreal.core.build.package_project",
            return_value={"status": "ok", "log_file": "package.log"},
        ) as mock_package:
            result = _run_build_task(
                task,
                "package_project",
                estimated_total_seconds=1200,
            )

        assert result["status"] == "completed"
        kwargs = mock_package.call_args.kwargs
        assert kwargs["maps"] == ["/Game/Maps/Oregon_Main"]
        assert kwargs["cook_flavor"] == "ASTC"
        assert kwargs["uat_args"] == ["-pak", "-iostore"]

    def test_run_build_task_forwards_targeted_cook_options(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Async cook workers must preserve native cook inputs."""
        from cli_anything.unreal.core.tasks import (
            _run_build_task,
            create_task,
            save_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.cook",
            {
                "project_path": temp_project["uproject"],
                "platform": "Android",
                "packages": ["/Game/Foo/A", "/Game/Foo/B"],
                "output_dir": r"F:\Cook Output",
                "ini_overrides": ["Engine:[Section]:Key=Value"],
            },
        )
        task["status"] = "running"
        save_task(task)

        with patch(
            "cli_anything.unreal.core.build.cook_content",
            return_value={"status": "ok", "log_file": "cook.log"},
        ) as mock_cook:
            result = _run_build_task(
                task,
                "cook_content",
                estimated_total_seconds=300,
            )

        assert result["status"] == "completed"
        kwargs = mock_cook.call_args.kwargs
        assert kwargs["packages"] == ["/Game/Foo/A", "/Game/Foo/B"]
        assert kwargs["output_dir"] == r"F:\Cook Output"
        assert kwargs["ini_overrides"] == ["Engine:[Section]:Key=Value"]

    def test_run_build_task_forwards_compile_modules(
        self, temp_project, tmp_path, monkeypatch
    ):
        from cli_anything.unreal.core.tasks import (
            _run_build_task,
            create_task,
            save_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {
                "project_path": temp_project["uproject"],
                "platform": "Win64",
                "build_config": "Development",
                "modules": ["Renderer", "RHI"],
            },
        )
        task["status"] = "running"
        save_task(task)

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            return_value={"status": "ok", "log_file": "compile.log"},
        ) as mock_compile:
            result = _run_build_task(
                task,
                "compile_project",
                estimated_total_seconds=600,
            )

        assert result["status"] == "completed"
        assert mock_compile.call_args.kwargs["modules"] == [
            "Renderer",
            "RHI",
        ]

    def test_run_build_task_does_not_overwrite_cancelled_status(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A worker returning after cancellation must not change it to failed."""
        from cli_anything.unreal.core.tasks import (
            _run_build_task,
            create_task,
            load_task,
            save_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {
                "project_path": temp_project["uproject"],
                "build_config": "Development",
                "platform": "Win64",
            },
        )
        task["status"] = "running"
        save_task(task)

        def finish_after_cancel(**kwargs):
            live = load_task(task["task_id"])
            live["status"] = "cancelled"
            live["cancel_requested"] = True
            live["cancelled"] = True
            save_task(live)
            return {"status": "error", "error": "process exited during cancel"}

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=finish_after_cancel,
        ):
            result = _run_build_task(
                task,
                "compile_project",
                estimated_total_seconds=600,
            )

        assert result["status"] == "cancelled"
        assert result["cancelled"] is True
        assert "error" not in result


    def test_run_build_task_preserves_cancel_when_build_raises(
        self, temp_project, tmp_path, monkeypatch
    ):
        """A build exception after cancellation must still finish as cancelled."""
        from cli_anything.unreal.core.tasks import (
            _run_build_task,
            create_task,
            load_task,
            save_task,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        task["status"] = "running"
        save_task(task)

        def raise_after_cancel(**kwargs):
            live = load_task(task["task_id"])
            live["cancel_requested"] = True
            save_task(live)
            raise RuntimeError("build teardown failed")

        with patch(
            "cli_anything.unreal.core.build.compile_project",
            side_effect=raise_after_cancel,
        ):
            result = _run_build_task(
                task,
                "compile_project",
                estimated_total_seconds=600,
            )

        assert result["status"] == "cancelled"
        assert result["cancelled"] is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
    def test_stop_build_closes_async_worker_job_and_descendants(
        self, temp_project, tmp_path, monkeypatch
    ):
        """Stopping the task worker must release its kill-on-close Job Object."""
        from cli_anything.unreal.core.build import stop_build
        from cli_anything.unreal.core.tasks import create_task, save_task
        from cli_anything.unreal.utils.ue_backend import (
            _kill_process_tree,
            _windows_process_identity,
        )

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task = create_task(
            "build.compile",
            {"project_path": temp_project["uproject"]},
        )
        child_pid_file = tmp_path / "child.pid"
        root_pid_file = tmp_path / "root.pid"
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import os, pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "time.sleep(60)\n",
            encoding="ascii",
        )
        worker_script = tmp_path / "worker.py"
        worker_script.write_text(
            "import pathlib, sys\n"
            "from cli_anything.unreal.utils.ue_backend import _run_subprocess\n"
            f"def on_start(proc): pathlib.Path({str(root_pid_file)!r}).write_text(str(proc.pid))\n"
            f"_run_subprocess([sys.executable, {str(child_script)!r}, "
            f"{str(child_pid_file)!r}], log_file={str(tmp_path / 'worker.log')!r}, "
            "heartbeat_seconds=0, on_start=on_start)\n",
            encoding="ascii",
        )

        worker = subprocess.Popen(
            [
                sys.executable,
                str(worker_script),
                "_task-worker",
                "run",
                task["task_id"],
            ],
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid = None
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not (
                child_pid_file.exists() and root_pid_file.exists()
            ):
                time.sleep(0.1)
            assert child_pid_file.exists(), "build child did not start"
            assert root_pid_file.exists(), "build root did not start"
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
            root_pid = int(root_pid_file.read_text(encoding="ascii"))

            task.update({
                "status": "running",
                "worker_pid": worker.pid,
                "pid": root_pid,
                "worker_process_identity": _windows_process_identity(worker.pid),
                "build_process_identity": _windows_process_identity(root_pid),
            })
            save_task(task)

            result = stop_build(temp_project["uproject"])

            worker.wait(timeout=10)
            assert result["status"] == "ok"
            assert result["remaining"] == []
            assert result["tasks"][0]["status"] == "cancelled"
            assert _windows_process_identity(child_pid)["found"] is False
        finally:
            if worker.poll() is None:
                _kill_process_tree(worker.pid)
            if child_pid is not None:
                _kill_process_tree(child_pid)

    def test_run_uat_returns_resolved_command(self, tmp_path):
        """UAT results should expose the exact argv used for reproduction."""
        from cli_anything.unreal.utils.ue_backend import run_uat

        resolved = [
            r"F:\Engine\Build\BatchFiles\RunUAT.bat",
            "BuildCookRun",
            "-pak",
        ]
        with patch(
            "cli_anything.unreal.utils.ue_backend.find_uat",
            return_value=resolved[0],
        ), patch(
            "cli_anything.unreal.utils.ue_backend._run_subprocess",
            return_value={
                "returncode": 0,
                "log_file": str(tmp_path / "uat.log"),
                "duration_seconds": 1.0,
            },
        ) as mock_run:
            result = run_uat(
                r"F:\Engine",
                "BuildCookRun",
                ["-pak"],
                log_file=str(tmp_path / "uat.log"),
            )

        assert result["command"] == resolved
        assert mock_run.call_args.args[0] == resolved

    def test_run_uat_no_timeout_param(self):
        """run_uat() no longer accepts timeout parameter."""
        from cli_anything.unreal.utils.ue_backend import run_uat
        import inspect

        sig = inspect.signature(run_uat)
        assert "timeout" not in sig.parameters

    def test_run_build_no_timeout_param(self):
        """run_build() no longer accepts timeout parameter."""
        from cli_anything.unreal.utils.ue_backend import run_build
        import inspect

        sig = inspect.signature(run_build)
        assert "timeout" not in sig.parameters

    def test_run_subprocess_no_timeout_param(self):
        """_run_subprocess() no longer accepts timeout parameter."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess
        import inspect

        sig = inspect.signature(_run_subprocess)
        assert "timeout" not in sig.parameters

    def test_kill_process_tree(self):
        """_kill_process_tree calls taskkill with /F /T /PID."""
        from cli_anything.unreal.utils.ue_backend import _kill_process_tree

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _kill_process_tree(1234)
            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "taskkill" in call_args
            assert "/F" in call_args
            assert "/T" in call_args
            assert "/PID" in call_args
            assert "1234" in call_args
            kwargs = mock_run.call_args.kwargs
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is False

    def test_find_running_build_processes_no_match(self):
        """find_running_build_processes returns [] when no processes match."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = find_running_build_processes("F:/Test/Test.uproject")
            assert result == []

    def test_find_running_build_processes_strict_timeout_is_not_empty_success(self):
        """A conclusive probe must expose a CIM timeout to its caller."""
        from cli_anything.unreal.utils.ue_backend import (
            BuildProcessProbeError,
            find_running_build_processes,
        )

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("powershell", 5),
        ):
            with pytest.raises(BuildProcessProbeError) as exc_info:
                find_running_build_processes(
                    "F:/Test/Test.uproject",
                    query_timeout=5,
                    fail_on_error=True,
                )

        assert exc_info.value.details == {
            "reason": "timeout",
            "timeout_seconds": 5,
        }

    def test_find_running_build_processes_with_match(self):
        """find_running_build_processes parses PowerShell JSON output."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        ps_output = json.dumps([
            {"ProcessId": 100, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe /p:Project=F:\\Test\\Test.uproject"},
            {"ProcessId": 200, "Name": "cl.exe", "CommandLine": "cl.exe some.cpp"},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ps_output)
            result = find_running_build_processes()
            assert len(result) == 2
            assert result[0]["pid"] == 100
            assert result[0]["name"] == "MSBuild.exe"

    def test_find_running_build_processes_filter_by_project(self):
        """find_running_build_processes filters by .uproject path."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        ps_output = json.dumps([
            {"ProcessId": 100, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe -project=F:\\Test574\\Test574.uproject"},
            {"ProcessId": 200, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe -project=F:\\Other\\Other.uproject"},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ps_output)
            result = find_running_build_processes("F:\\Test574\\Test574.uproject")
            assert len(result) == 1
            assert result[0]["pid"] == 100

    def test_find_running_build_processes_skips_idle_msbuild_daemons(self):
        """find_running_build_processes skips Rider/VS idle MSBuild node-reuse daemons."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        ps_output = json.dumps([
            {"ProcessId": 100, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe /noautoresponse /nologo /nodemode:1 /nodeReuse:false"},
            {"ProcessId": 200, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe /noautoresponse /nologo /nodemode:1 /nodeReuse:false /low:false"},
            {"ProcessId": 300, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe /nr:true"},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ps_output)
            result = find_running_build_processes("F:\\Test574\\Test574.uproject")
            assert len(result) == 0

    def test_find_running_build_processes_no_false_positive_for_other_project(self):
        """find_running_build_processes returns [] when only other projects are building."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        ps_output = json.dumps([
            {"ProcessId": 100, "Name": "UnrealBuildTool.exe", "CommandLine": "UnrealBuildTool.exe -project=F:\\Other\\Other.uproject"},
            {"ProcessId": 200, "Name": "cl.exe", "CommandLine": "cl.exe some.cpp"},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ps_output)
            result = find_running_build_processes("F:\\Test574\\Test574.uproject")
            assert len(result) == 0

    def test_find_running_build_processes_keeps_associated_processes_when_project_matches(self):
        """When a project matches, associated processes (cl.exe without .uproject) are kept."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        ps_output = json.dumps([
            {"ProcessId": 100, "Name": "MSBuild.exe", "CommandLine": "MSBuild.exe -project=F:\\Test574\\Test574.uproject"},
            {"ProcessId": 200, "Name": "cl.exe", "CommandLine": "cl.exe some.cpp"},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ps_output)
            result = find_running_build_processes("F:\\Test574\\Test574.uproject")
            assert len(result) == 2
            pids = {p["pid"] for p in result}
            assert 100 in pids
            assert 200 in pids

    def test_kill_build_processes_all_killed(self):
        """kill_build_processes kills all found processes."""
        from cli_anything.unreal.utils.ue_backend import kill_build_processes

        mock_procs = [
            {"pid": 100, "name": "MSBuild.exe", "cmdline": "", "project": ""},
        ]
        with patch("cli_anything.unreal.utils.ue_backend.find_running_build_processes", side_effect=[
            mock_procs,  # first call: find processes
            [],          # after kill + sleep: re-check (empty)
        ]), patch("cli_anything.unreal.utils.ue_backend._kill_process_tree", return_value=True), \
             patch("time.sleep"):
            result = kill_build_processes()
            assert result["status"] == "ok"
            assert 100 in result["killed"]
            assert result["remaining"] == []

    def test_kill_build_processes_none_running(self):
        """kill_build_processes returns 'none' when no processes found."""
        from cli_anything.unreal.utils.ue_backend import kill_build_processes

        with patch("cli_anything.unreal.utils.ue_backend.find_running_build_processes", return_value=[]):
            result = kill_build_processes()
            assert result["status"] == "none"
            assert result["killed"] == []

    def test_run_subprocess_kills_tree_on_timeout(self, tmp_path):
        """_run_subprocess kills the process tree when the safety timeout is hit."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        # The inner poll-wait loop now catches TimeoutExpired repeatedly
        # until the safety deadline is reached — exactly one expiry is
        # enough when we force the safety timeout down to 0.
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="test", timeout=1),
            None,  # post-kill wait() succeeds
        ]
        mock_proc.returncode = -2

        log_path = tmp_path / "t.log"
        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("cli_anything.unreal.utils.ue_backend._SAFETY_TIMEOUT", 0), \
             patch("cli_anything.unreal.utils.ue_backend._kill_process_tree", return_value=True) as mock_kill, \
             patch("cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job", return_value=123), \
             patch("cli_anything.unreal.utils.ue_backend._resume_suspended_process", return_value=True, create=True), \
             patch("cli_anything.unreal.utils.ue_backend._release_kill_on_close_job", return_value=True):
            # Disable heartbeats so the poll loop doesn't spin waiting for
            # a beat boundary.
            result = _run_subprocess(
                ["echo", "test"], log_file=str(log_path),
                heartbeat_seconds=0,
            )
            assert result["returncode"] == -2
            assert result["log_file"] == str(log_path)
            assert "timed out" in result["error"].lower()
            # Verify _kill_process_tree was called with the PID
            mock_kill.assert_called_once_with(9999)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
    @pytest.mark.parametrize(
        ("returncode", "child_should_run"),
        [(7, False), (0, True)],
    )
    def test_run_subprocess_job_controls_descendants(
        self,
        tmp_path,
        returncode,
        child_should_run,
    ):
        """Failed builds kill descendants; successful builds preserve them."""
        from cli_anything.unreal.utils.ue_backend import (
            _kill_process_tree,
            _run_subprocess,
        )

        child_pid_path = tmp_path / "child.pid"
        parent_script = tmp_path / "spawn_child_then_exit.py"
        parent_script.write_text(
            "import pathlib, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
            f"raise SystemExit({returncode})\n",
            encoding="utf-8",
        )

        result = _run_subprocess(
            [sys.executable, str(parent_script)],
            log_file=str(tmp_path / "failed-build.log"),
            heartbeat_seconds=0,
        )
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        def child_is_running():
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
            ]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            handle = kernel32.OpenProcess(0x00100000, False, child_pid)
            if not handle:
                error = ctypes.get_last_error()
                if error == 87:
                    return False
                raise ctypes.WinError(error)
            try:
                wait_result = kernel32.WaitForSingleObject(handle, 0)
                if wait_result == 258:
                    return True
                if wait_result == 0:
                    return False
                raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel32.CloseHandle(handle)

        try:
            if not child_should_run:
                deadline = time.time() + 3
                while child_is_running() and time.time() < deadline:
                    time.sleep(0.1)
            assert result["returncode"] == returncode
            assert child_is_running() is child_should_run
        finally:
            if child_is_running():
                _kill_process_tree(child_pid)

    def test_run_subprocess_success(self, tmp_path):
        """_run_subprocess returns result dict on success."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock()
        mock_proc.pid = 1234
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0

        log_path = tmp_path / "t.log"
        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job", return_value=123), \
             patch("cli_anything.unreal.utils.ue_backend._resume_suspended_process", return_value=True, create=True), \
             patch("cli_anything.unreal.utils.ue_backend._release_kill_on_close_job", return_value=True):
            result = _run_subprocess(["echo", "hello"], log_file=str(log_path))
            assert result["returncode"] == 0
            assert result["log_file"] == str(log_path)
            assert "duration_seconds" in result
            # Output must not leak back
            assert "stdout" not in result
            assert "stderr" not in result

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows argv safety")
    def test_run_subprocess_rejects_literal_quote_argv(self, tmp_path):
        """The batch wrapper must reject quote-based command injection."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock(pid=1234, returncode=0)
        mock_proc.wait.return_value = None
        with patch("subprocess.Popen", return_value=mock_proc) as popen, patch(
            "cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job",
            return_value=123,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._resume_suspended_process",
            return_value=True,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._release_kill_on_close_job",
            return_value=True,
        ):
            result = _run_subprocess(
                ["RunUAT.bat", '-x=" & echo PWNED & rem "'],
                log_file=str(tmp_path / "unsafe.log"),
            )

        assert result["returncode"] == -1
        assert "unsafe" in result["error"].lower()
        popen.assert_not_called()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console encoding behavior")
    def test_run_subprocess_uses_hidden_native_console(self, tmp_path):
        """Detached build workers must align UBT with native tool encoding."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock(pid=1234, returncode=0)
        mock_proc.wait.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as popen, \
             patch(
                 "cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job",
                 return_value=123,
             ), patch(
                 "cli_anything.unreal.utils.ue_backend._resume_suspended_process",
                 return_value=True,
             ), patch(
                 "cli_anything.unreal.utils.ue_backend._release_kill_on_close_job",
                 return_value=True,
            ):
            result = _run_subprocess(
                [r"F:\Custom Engine\Build.bat", "Target", "Win64", "Development"],
                log_file=str(tmp_path / "utf8-console.log"),
            )

        assert result["returncode"] == 0
        launch_command = popen.call_args.args[0]
        assert isinstance(launch_command, list)
        assert launch_command[0].lower().endswith("powershell.exe")
        assert "-EncodedCommand" in launch_command
        assert popen.call_args.kwargs["shell"] is False
        assert popen.call_args.kwargs["creationflags"] & subprocess.CREATE_NEW_CONSOLE
        startupinfo = popen.call_args.kwargs["startupinfo"]
        assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert startupinfo.wShowWindow == subprocess.SW_HIDE

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows command argument behavior")
    def test_run_subprocess_preserves_cmd_metacharacters(self, tmp_path):
        """Build paths and options must not be reinterpreted by cmd.exe."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        dump_script = tmp_path / "dump_args.py"
        dump_script.write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
            encoding="ascii",
        )
        batch = tmp_path / "forward_args.bat"
        batch.write_text(
            f'@echo off\n"{sys.executable}" "{dump_script}" %*\n',
            encoding="ascii",
        )
        expected = [
            "a&b",
            "%PATH%",
            "space value",
            "caret^pipe|value",
            "bang!value",
            "paren(value)<redir>",
        ]
        log_path = tmp_path / "forwarded.log"

        result = _run_subprocess(
            [str(batch), *expected],
            log_file=str(log_path),
            heartbeat_seconds=0,
        )

        assert result["returncode"] == 0
        assert json.loads(log_path.read_text(encoding="utf-8").strip()) == expected

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console encoding behavior")
    def test_run_subprocess_preserves_localized_linker_output(self, tmp_path):
        """A CP936 parent must not corrupt localized MSVC-style child output."""
        import ctypes

        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        original_code_page = kernel32.GetConsoleOutputCP()
        if not original_code_page:
            pytest.skip("Test process has no Windows console")

        script = tmp_path / "localized_linker_output.py"
        script.write_text(
            "import ctypes, os\n"
            "cp = ctypes.windll.kernel32.GetConsoleOutputCP()\n"
            "encoding = f'cp{cp}' if cp else 'mbcs'\n"
            "line = '\\u6b63\\u5728\\u521b\\u5efa\\u5e93 Engine.lib "
            "\\u548c\\u5bf9\\u8c61 Engine.exp\\n'\n"
            "os.write(1, line.encode(encoding))\n",
            encoding="ascii",
        )
        log_path = tmp_path / "localized-linker.log"

        assert kernel32.SetConsoleOutputCP(936)
        try:
            result = _run_subprocess(
                [sys.executable, str(script)],
                log_file=str(log_path),
                heartbeat_seconds=0,
            )
        finally:
            kernel32.SetConsoleOutputCP(original_code_page)

        assert result["returncode"] == 0
        assert log_path.read_text(encoding="mbcs") == (
            "\u6b63\u5728\u521b\u5efa\u5e93 Engine.lib \u548c\u5bf9\u8c61 Engine.exp\n"
        )
        assert b"\xef\xbf\xbd" not in log_path.read_bytes()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console encoding behavior")
    def test_run_subprocess_preserves_cp936_msvc_diagnostics_through_ubt(self, tmp_path):
        """UBT and ue-cli must preserve localized MSVC bytes in native logs."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        compiler = tmp_path / "fake_compiler.py"
        compiler.write_text(
            "import os\n"
            "line = 'SDOC.cpp(717,4): error C2065: LogRenderer: "
            "\\u672a\\u58f0\\u660e\\u7684\\u6807\\u8bc6\\u7b26\\n'\n"
            "os.write(1, line.encode('cp936'))\n",
            encoding="ascii",
        )
        ubt = tmp_path / "fake_ubt.py"
        ubt.write_text(
            "import ctypes, os, subprocess, sys\n"
            "proc = subprocess.run([sys.executable, sys.argv[1]], stdout=subprocess.PIPE)\n"
            "cp = ctypes.windll.kernel32.GetConsoleOutputCP()\n"
            "encoding = f'cp{cp}' if cp else 'mbcs'\n"
            "text = proc.stdout.decode(encoding, errors='replace')\n"
            "os.write(1, text.encode(encoding, errors='replace'))\n"
            "raise SystemExit(proc.returncode)\n",
            encoding="ascii",
        )
        log_path = tmp_path / "localized-msvc.log"

        result = _run_subprocess(
            [sys.executable, str(ubt), str(compiler)],
            log_file=str(log_path),
            heartbeat_seconds=0,
        )

        assert result["returncode"] == 0
        assert log_path.read_text(encoding="mbcs") == (
            "SDOC.cpp(717,4): error C2065: LogRenderer: \u672a\u58f0\u660e\u7684\u6807\u8bc6\u7b26\n"
        )
        assert b"\xef\xbf\xbd" not in log_path.read_bytes()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
    def test_run_subprocess_success_disarms_kill_job(self, tmp_path):
        """Successful builds preserve descendants before releasing the Job Object."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock()
        mock_proc.pid = 1234
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as popen, \
             patch(
                 "cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job",
                 return_value=123,
             ), patch(
                 "cli_anything.unreal.utils.ue_backend._resume_suspended_process",
                 return_value=True,
                 create=True,
             ) as resume_process, patch(
                 "cli_anything.unreal.utils.ue_backend._release_kill_on_close_job",
                 return_value=True,
                 create=True,
             ) as release_job:
            _run_subprocess(
                ["echo", "hello"],
                log_file=str(tmp_path / "success-job.log"),
            )

        creationflags = popen.call_args.kwargs["creationflags"]
        assert creationflags & 0x00000004
        resume_process.assert_called_once_with(1234)
        release_job.assert_called_once_with(123, preserve_processes=True)
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
    def test_run_subprocess_fails_closed_when_job_attach_fails(self, tmp_path):
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock(pid=1234, returncode=0)
        mock_proc.wait.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job", return_value=None):
            result = _run_subprocess(
                ["echo", "hello"],
                log_file=str(tmp_path / "attach-failed.log"),
            )

        assert result["returncode"] == -1
        assert "Job Object" in result["error"]
        mock_proc.kill.assert_called_once()
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
    def test_run_subprocess_fails_closed_when_resume_fails(self, tmp_path):
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock(pid=1234, returncode=None)
        mock_proc.wait.return_value = None
        on_start = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job", return_value=123), \
             patch("cli_anything.unreal.utils.ue_backend._resume_suspended_process", return_value=False), \
             patch("cli_anything.unreal.utils.ue_backend._release_kill_on_close_job", return_value=True) as release_job:
            result = _run_subprocess(
                ["echo", "hello"],
                log_file=str(tmp_path / "resume-failed.log"),
                on_start=on_start,
            )

        assert result["returncode"] == -1
        assert "resume build process" in result["error"]
        release_job.assert_called_once_with(123, preserve_processes=False)
        mock_proc.kill.assert_not_called()
        mock_proc.wait.assert_called_once()
        on_start.assert_not_called()
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
    def test_run_subprocess_reports_disarm_failure(self, tmp_path):
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        mock_proc = MagicMock(pid=1234, returncode=0)
        mock_proc.wait.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("cli_anything.unreal.utils.ue_backend._attach_kill_on_close_job", return_value=123), \
             patch("cli_anything.unreal.utils.ue_backend._resume_suspended_process", return_value=True, create=True), \
             patch("cli_anything.unreal.utils.ue_backend._release_kill_on_close_job", return_value=False):
            result = _run_subprocess(
                ["echo", "hello"],
                log_file=str(tmp_path / "disarm-failed.log"),
            )

        assert result["returncode"] == -1
        assert "release build Job Object" in result["error"]

    def test_run_subprocess_file_not_found(self, tmp_path):
        """_run_subprocess handles FileNotFoundError gracefully."""
        from cli_anything.unreal.utils.ue_backend import _run_subprocess

        log_path = tmp_path / "t.log"
        with patch("subprocess.Popen", side_effect=FileNotFoundError("not found")):
            result = _run_subprocess(["nonexistent_command"], log_file=str(log_path))
            assert result["returncode"] == -1
            assert "not found" in result["error"]


# ═══════════════════════════════════════════════════════════════════════
#  Test build CLI commands (stop, is-building, no --timeout)
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCLI:
    """Tests for build CLI — stop, is-building, and --timeout removal."""

    @staticmethod
    def _parse_json_output(output: str) -> dict:
        """Extract JSON from CLI output that may have skin.info() text before it."""
        # Find the first '{' and parse from there
        idx = output.find("{")
        if idx == -1:
            return json.loads(output)
        return json.loads(output[idx:])

    def test_build_compile_has_no_wait_and_timeout(self, temp_project):
        """build compile now has --no-wait and --timeout options."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["build", "compile", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--no-wait" in result.output
        assert "--timeout" in result.output
        assert "--module" in result.output
        assert "--log-tail-lines" not in result.output

    def test_build_compile_accepts_command_project_option(self, temp_project):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        captured = {}

        def fake_submit_task(command, payload):
            captured["command"] = command
            captured["payload"] = payload
            return {"task_id": "compile-task"}

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.build.submit_task", side_effect=fake_submit_task):
            result = runner.invoke(cli, [
                "--output", "json",
                "build", "compile",
                "--project", temp_project["uproject"],
                "--platform", "Android",
                "--config", "Development",
                "--no-wait",
            ])

        assert result.exit_code == 0, result.output
        data = self._parse_json_output(result.output)
        assert data["status"] == "success"
        assert data["result"]["task_id"] == "compile-task"
        assert captured["command"] == "build.compile"
        assert captured["payload"]["project_path"] == temp_project["uproject"]
        assert captured["payload"]["platform"] == "Android"
        assert captured["payload"]["build_config"] == "Development"

    def test_build_compile_interrupt_cancels_owned_task_before_abort(
        self, temp_project
    ):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        cancelled_task = {
            "task_id": "t-interrupted",
            "command": "build.compile",
            "status": "cancelled",
            "cancelled": True,
        }

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.build.find_running_editors",
            return_value=[],
        ), patch(
            "cli_anything.unreal.commands.build.submit_task",
            return_value={"task_id": "t-interrupted"},
        ), patch(
            "cli_anything.unreal.commands.build._wait_for_task_with_log_stream",
            side_effect=KeyboardInterrupt,
        ), patch(
            "cli_anything.unreal.commands.build.cancel_task",
            return_value=cancelled_task,
        ) as cancel:
            result = runner.invoke(cli, [
                "--project", temp_project["uproject"],
                "build", "compile",
            ])

        assert result.exit_code == 1
        assert "Aborted!" in result.output
        cancel.assert_called_once_with("t-interrupted")

    def test_build_compile_interrupt_reports_cancellation_failure(
        self, temp_project
    ):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        failed_task = {
            "task_id": "t-interrupted",
            "command": "build.compile",
            "status": "running",
            "error": {
                "code": "TASK_CANCEL_FAILED",
                "message": "Build task cancellation left processes running.",
            },
            "cancel_result": {"remaining": [1234]},
        }

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.build.find_running_editors",
            return_value=[],
        ), patch(
            "cli_anything.unreal.commands.build.submit_task",
            return_value={"task_id": "t-interrupted"},
        ), patch(
            "cli_anything.unreal.commands.build._wait_for_task_with_log_stream",
            side_effect=KeyboardInterrupt,
        ), patch(
            "cli_anything.unreal.commands.build.cancel_task",
            return_value=failed_task,
        ) as cancel:
            result = runner.invoke(cli, [
                "--output", "json",
                "--project", temp_project["uproject"],
                "build", "compile",
            ])

        assert result.exit_code == 4
        data = self._parse_json_output(result.output)
        assert data["code"] == "TASK_CANCEL_FAILED"
        assert data["details"]["cancel_result"]["remaining"] == [1234]
        cancel.assert_called_once_with("t-interrupted")

    def test_build_compile_accepts_repeatable_module_option(self, temp_project):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        captured = {}

        def fake_submit_task(command, payload):
            captured["command"] = command
            captured["payload"] = payload
            return {"task_id": "compile-task"}

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.build.find_running_editors",
            return_value=[],
        ), patch(
            "cli_anything.unreal.commands.build.submit_task",
            side_effect=fake_submit_task,
        ):
            result = runner.invoke(cli, [
                "--output", "json",
                "--project", temp_project["uproject"],
                "build", "compile",
                "--platform", "Win64",
                "--module", "Renderer",
                "--module", "RHI",
                "--no-wait",
            ])

        assert result.exit_code == 0, result.output
        assert captured["command"] == "build.compile"
        assert captured["payload"]["modules"] == ["Renderer", "RHI"]

    def test_build_compile_rejects_explicit_target_with_replacement(self, temp_project):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.build.submit_task") as mock_submit:
            result = runner.invoke(cli, [
                "--output", "json",
                "build", "compile",
                "--project", temp_project["uproject"],
                "--target", "TestProjectEditor",
                "--platform", "Win64",
                "--configuration", "Development",
            ])

        assert result.exit_code == 2, result.output
        data = self._parse_json_output(result.output)
        assert data["code"] == "BUILD_TARGET_INFERRED"
        assert data["details"]["provided_target"] == "TestProjectEditor"
        replacement = data["details"]["replacement_command"]
        assert f'--project "{temp_project["uproject"]}"' in replacement
        assert "build compile --platform Win64 --config Development" in replacement
        assert replacement in data["suggestion"]
        mock_submit.assert_not_called()

    def test_build_compile_accepts_configuration_alias(self, temp_project):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        captured = {}

        def fake_submit_task(command, payload):
            captured["command"] = command
            captured["payload"] = payload
            return {"task_id": "compile-task"}

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.build.find_running_editors",
            return_value=[],
        ), patch(
            "cli_anything.unreal.commands.build.submit_task",
            side_effect=fake_submit_task,
        ):
            result = runner.invoke(cli, [
                "--output", "json",
                "--project", temp_project["uproject"],
                "build", "compile",
                "--platform", "Win64",
                "--configuration", "Shipping",
                "--no-wait",
            ])

        assert result.exit_code == 0, result.output
        assert captured["command"] == "build.compile"
        assert captured["payload"]["build_config"] == "Shipping"

    def test_build_compile_surfaces_invalid_build_output_failure(
        self, temp_project
    ):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        invalid_product = str(
            Path(temp_project["dir"])
            / "Plugins"
            / "Broken"
            / "Binaries"
            / "Win64"
            / "UnrealEditor-Broken.dll"
        )
        final_task = {
            "task_id": "t-invalid-output",
            "command": "build.compile",
            "status": "failed",
            "log_file": "compile.log",
            "result": {
                "status": "error",
                "code": "INVALID_BUILD_OUTPUT",
                "error": "Compile produced an invalid DLL.",
                "receipt_file": "TestProjectEditor.target",
                "invalid_build_products": [{
                    "path": invalid_product,
                    "type": "DynamicLibrary",
                    "reason": "missing DOS MZ signature",
                }],
            },
            "error": {
                "code": "INVALID_BUILD_OUTPUT",
                "message": "Compile produced an invalid DLL.",
            },
        }

        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.build.find_running_editors",
            return_value=[],
        ), patch(
            "cli_anything.unreal.commands.build.submit_task",
            return_value={"task_id": "t-invalid-output"},
        ), patch(
            "cli_anything.unreal.commands.build._wait_for_task_with_log_stream",
            return_value=final_task,
        ):
            result = runner.invoke(cli, [
                "--output", "json",
                "--project", temp_project["uproject"],
                "build", "compile",
            ])

        assert result.exit_code == 3
        data = self._parse_json_output(result.output)
        assert data["status"] == "error"
        assert data["code"] == "INVALID_BUILD_OUTPUT"
        assert data["details"]["result"]["receipt_file"] == (
            "TestProjectEditor.target"
        )
        assert data["details"]["result"]["invalid_build_products"][0][
            "path"
        ] == invalid_product

    def test_build_compile_blocks_when_matching_editor_online(self, temp_project):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_api = MagicMock()
        mock_api.is_alive.return_value = True

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.build.sys.platform", "win32"), \
             patch("cli_anything.unreal.commands.build.find_running_editors", return_value=[
                 {"pid": 777, "project": temp_project["uproject"]},
             ]), \
             patch("cli_anything.unreal.commands.build.UEEditorAPI", return_value=mock_api), \
             patch("cli_anything.unreal.commands.build.submit_task") as mock_submit:
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "compile",
                "--platform", "Win64",
                "--config", "Development",
                "--no-wait",
            ])

        assert result.exit_code == 3
        data = self._parse_json_output(result.output)
        assert data["code"] == "EDITOR_RUNNING_LOCKS_DLLS"
        assert data["details"]["online"] is True
        assert data["details"]["running_editors"][0]["pid"] == 777
        assert "editor close" in data["suggestion"]
        mock_submit.assert_not_called()

    def test_build_compile_allows_other_project_editor(self, temp_project):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_api = MagicMock()
        mock_api.is_alive.return_value = True

        runner = CliRunner()
        with patch("cli_anything.unreal.commands.build.sys.platform", "win32"), \
             patch("cli_anything.unreal.commands.build.find_running_editors", return_value=[
                 {"pid": 777, "project": "F:/Other/Other.uproject"},
             ]), \
             patch("cli_anything.unreal.commands.build.UEEditorAPI", return_value=mock_api), \
             patch("cli_anything.unreal.commands.build.submit_task", return_value={"task_id": "compile-task"}):
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "compile",
                "--platform", "Win64",
                "--config", "Development",
                "--no-wait",
            ])

        assert result.exit_code == 0, result.output
        data = self._parse_json_output(result.output)
        assert data["status"] == "success"
        assert data["result"]["task_id"] == "compile-task"

    def test_build_wait_streams_log_file_to_stderr(self, tmp_path, capsys):
        from cli_anything.unreal.commands.build import _wait_for_task_with_log_stream

        log_file = tmp_path / "compile.log"
        log_file.write_text("first\n", encoding="utf-8")
        calls = {"count": 0}

        def fake_load_task(task_id):
            assert task_id == "t-log"
            calls["count"] += 1
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write("second\n" if calls["count"] == 1 else "third\n")
            status = "running" if calls["count"] == 1 else "completed"
            return {"task_id": task_id, "command": "build.compile", "status": status}

        with patch("cli_anything.unreal.commands.build.load_task", side_effect=fake_load_task):
            task = _wait_for_task_with_log_stream("t-log", timeout=5, log_file=str(log_file))

        assert task["status"] == "completed"
        assert capsys.readouterr().err.replace("\r\n", "\n") == "first\nsecond\nthird\n"

    @pytest.mark.parametrize("warning_code", ["D8002", "D9002"])
    def test_build_wait_folds_repeated_msvc_command_line_warnings_without_rewriting_log(
        self,
        tmp_path,
        capsys,
        warning_code,
    ):
        from cli_anything.unreal.commands.build import _wait_for_task_with_log_stream

        warning = f"cl : command line warning {warning_code} : repeated command-line issue\n"
        log_file = tmp_path / "compile.log"
        log_file.write_text(warning, encoding="ascii")
        calls = {"count": 0}

        def fake_load_task(task_id):
            calls["count"] += 1
            with log_file.open("a", encoding="ascii") as fh:
                fh.write(warning)
                if calls["count"] == 2:
                    fh.write("build complete\n")
            status = "running" if calls["count"] == 1 else "completed"
            return {"task_id": task_id, "command": "build.compile", "status": status}

        with patch("cli_anything.unreal.commands.build.load_task", side_effect=fake_load_task):
            task = _wait_for_task_with_log_stream("t-msvc-warning", timeout=5, log_file=str(log_file))

        streamed = capsys.readouterr().err.replace("\r\n", "\n")
        assert task["status"] == "completed"
        assert streamed.count(warning) == 1
        assert "[ue-cli] folded 2 repeated MSVC command-line warning lines" in streamed
        assert str(log_file) in streamed
        assert "build complete\n" in streamed
        assert log_file.read_text(encoding="ascii").count(warning) == 3

    def test_build_wait_does_not_fold_plain_text_that_mentions_d8_warning(self, tmp_path, capsys):
        from cli_anything.unreal.commands.build import _wait_for_task_with_log_stream

        note = "note: documentation mentions warning D8002 for reference\n"
        log_file = tmp_path / "compile.log"
        log_file.write_text(note + note, encoding="ascii")

        with patch(
            "cli_anything.unreal.commands.build.load_task",
            return_value={"task_id": "t-note", "command": "build.compile", "status": "completed"},
        ):
            task = _wait_for_task_with_log_stream("t-note", timeout=5, log_file=str(log_file))

        streamed = capsys.readouterr().err.replace("\r\n", "\n")
        assert task["status"] == "completed"
        assert streamed == note + note

    def test_build_wait_flushes_fold_summary_when_task_disappears(self, tmp_path, capsys):
        from cli_anything.unreal.commands.build import _wait_for_task_with_log_stream

        warning = "cl : command line warning D8002 : repeated command-line issue\n"
        tail = "worker task record disappeared"
        log_file = tmp_path / "compile.log"
        log_file.write_text(warning + warning + tail, encoding="ascii")

        with patch("cli_anything.unreal.commands.build.load_task", return_value=None):
            task = _wait_for_task_with_log_stream("t-missing", timeout=5, log_file=str(log_file))

        streamed = capsys.readouterr().err.replace("\r\n", "\n")
        assert task is None
        assert streamed.count(warning) == 1
        assert tail in streamed
        assert "[ue-cli] folded 1 repeated MSVC command-line warning lines" in streamed

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows compiler encoding behavior")
    def test_build_wait_streams_split_localized_bytes_without_replacement(self, tmp_path, capsys):
        from cli_anything.unreal.commands.build import _wait_for_task_with_log_stream

        first = "\u7b2c\u4e00\n".encode("mbcs")
        second = "\u7b2c\u4e8c\n".encode("mbcs")
        third = "\u7b2c\u4e09\n".encode("mbcs")
        log_file = tmp_path / "compile.log"
        log_file.write_bytes(first[:1])
        calls = {"count": 0}

        def fake_load_task(task_id):
            calls["count"] += 1
            with log_file.open("ab") as fh:
                if calls["count"] == 1:
                    fh.write(first[1:] + second)
                else:
                    fh.write(third)
            status = "running" if calls["count"] == 1 else "completed"
            return {"task_id": task_id, "command": "build.compile", "status": status}

        with patch("cli_anything.unreal.commands.build.load_task", side_effect=fake_load_task):
            task = _wait_for_task_with_log_stream("t-localized", timeout=5, log_file=str(log_file))

        assert task["status"] == "completed"
        assert capsys.readouterr().err.replace("\r\n", "\n") == "\u7b2c\u4e00\n\u7b2c\u4e8c\n\u7b2c\u4e09\n"

    def test_build_cook_has_no_wait_and_timeout(self, temp_project):
        """build cook now has --no-wait and --timeout options."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["build", "cook", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--no-wait" in result.output
        assert "--timeout" in result.output
        assert "--package" in result.output
        assert "--output-dir" in result.output
        assert "--ini" in result.output
        assert "--log-tail-lines" not in result.output

    def test_build_cook_native_options_reach_task_payload(self, temp_project):
        """Cook CLI must preserve native inputs for the shared worker."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        captured = {}

        def fake_submit(command, payload):
            captured["command"] = command
            captured["payload"] = payload
            return {"task_id": "t-targeted-cook"}

        with patch(
            "cli_anything.unreal.commands.build.submit_task",
            side_effect=fake_submit,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json",
                "--project", temp_project["uproject"],
                "build", "cook",
                "--platform", "Android",
                "--package", "/Game/Foo/A",
                "--package", "/Game/Foo/B",
                "--output-dir", r"F:\Cook Output",
                "--ini", "Engine:[Section]:Key=Value",
                "--ini", "Game:[Other]:Flag=True",
                "--no-wait",
            ])

        assert result.exit_code == 0, result.output
        assert captured["command"] == "build.cook"
        assert captured["payload"]["packages"] == (
            "/Game/Foo/A",
            "/Game/Foo/B",
        )
        assert captured["payload"]["output_dir"] == r"F:\Cook Output"
        assert captured["payload"]["ini_overrides"] == (
            "Engine:[Section]:Key=Value",
            "Game:[Other]:Flag=True",
        )

    @pytest.mark.parametrize(
        ("option", "value", "message"),
        [
            ("--package", "/Game/Foo/A+/Game/Foo/B", "must not contain '+'"),
            ("--package", "", "cook package must not be empty"),
            ("--output-dir", 'F:\\Cook" & echo PWNED', "Unsafe cook output directory"),
            ("--output-dir", "", "cook output directory must not be empty"),
            ("--ini", 'Engine:[Section]:Key=" & echo PWNED', "Unsafe ini override"),
            ("--ini", "", "ini override must not be empty"),
            (
                "--ini",
                "-ini:Engine:[Section]:Key=Value",
                "omit the '-ini:' prefix",
            ),
        ],
        ids=[
            "package_separator",
            "empty_package",
            "output_dir",
            "empty_output_dir",
            "ini",
            "empty_ini",
            "prefixed_ini",
        ],
    )
    def test_build_cook_rejects_unsafe_native_values(
        self, temp_project, option, value, message
    ):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        result = CliRunner().invoke(cli, [
            "--project", temp_project["uproject"],
            "build", "cook",
            option, value,
            "--no-wait",
        ])

        assert result.exit_code == 2
        assert message in result.output

    def test_build_package_targeted_options_reach_task_payload(
        self, temp_project
    ):
        """Package CLI should preserve reproducibility options for the worker."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        captured = {}

        def fake_submit(command, payload):
            captured["command"] = command
            captured["payload"] = payload
            return {"task_id": "t-targeted-package"}

        with patch(
            "cli_anything.unreal.commands.build.submit_task",
            side_effect=fake_submit,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json",
                "--project", temp_project["uproject"],
                "build", "package",
                "--platform", "Android",
                "--map", "/Game/Maps/Oregon_Main",
                "--map", "/Game/Maps/Oregon_Sub",
                "--cook-flavor", "ASTC",
                "--uat-arg=-pak",
                "--uat-arg=-iostore",
                "--uat-arg=-ini:Engine:[Section]:Key=Value",
                "--uat-arg=-AdditionalCookerOptions=-CookProcessCount=1",
                "--no-wait",
            ])

        assert result.exit_code == 0, result.output
        assert captured["command"] == "build.package"
        assert captured["payload"]["maps"] == (
            "/Game/Maps/Oregon_Main",
            "/Game/Maps/Oregon_Sub",
        )
        assert captured["payload"]["cook_flavor"] == "ASTC"
        assert captured["payload"]["uat_args"] == (
            "-pak",
            "-iostore",
            "-ini:Engine:[Section]:Key=Value",
            "-AdditionalCookerOptions=-CookProcessCount=1",
        )

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("--uat-arg", '-x=" & echo PWNED & rem "'),
            ("--map", '/Game/Maps/Oregon" & echo PWNED & rem "'),
            ("--cook-flavor", 'ASTC" & echo PWNED & rem "'),
        ],
        ids=["uat_arg", "map", "cook_flavor"],
    )
    def test_build_package_rejects_unsafe_freeform_values(
        self, temp_project, option, value
    ):
        """All free-form package values must reject command-shell injection."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        with patch(
            "cli_anything.unreal.commands.build.submit_task",
            return_value={"task_id": "must-not-submit"},
        ) as submit:
            result = CliRunner().invoke(cli, [
                "--project", temp_project["uproject"],
                "build", "package",
                option, value,
                "--no-wait",
            ])

        assert result.exit_code == 2
        assert "unsafe" in result.output.lower()
        submit.assert_not_called()

    def test_build_package_rejects_non_option_uat_arg(self, temp_project):
        """Additional UAT argv must be explicit option-style values."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        result = CliRunner().invoke(cli, [
            "--project", temp_project["uproject"],
            "build", "package",
            "--uat-arg", "pak",
            "--no-wait",
        ])

        assert result.exit_code == 2
        assert "must start with '-'" in result.output

    def test_build_package_has_no_wait_and_timeout(self, temp_project):
        """build package now has --no-wait and --timeout options."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["build", "package", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--no-wait" in result.output
        assert "--timeout" in result.output
        assert "--map" in result.output
        assert "--cook-flavor" in result.output
        assert "--uat-arg" in result.output
        assert "AdditionalCookerOptions" in result.output
        assert "--log-tail-lines" not in result.output

    def test_build_stop_cli(self, temp_project):
        """build stop command works and calls stop_build."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        with patch("cli_anything.unreal.core.build.kill_build_processes", return_value={
            "killed": [1234], "remaining": [], "status": "ok",
        }):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "stop",
            ])
            assert result.exit_code == 0
            data = self._parse_json_output(result.output)
            assert data["status"] == "success"
            assert data["result"]["status"] == "ok"
            assert 1234 in data["result"]["killed"]

    def test_build_cancel_failure_is_structured_nonzero_json(self):
        """A surviving tracked PID must be visible to automation immediately."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        failed_task = {
            "task_id": "t-cancel-failed",
            "command": "build.package",
            "status": "running",
            "worker_pid": 51001,
            "pid": 51002,
            "error": {
                "code": "TASK_CANCEL_FAILED",
                "message": "Build task cancellation left processes running.",
            },
            "cancel_result": {
                "killed": [],
                "remaining": [51001, 51002],
                "processes": [
                    {"ok": False, "pid": 51001, "error": "taskkill timed out"},
                ],
            },
        }
        with patch(
            "cli_anything.unreal.core.tasks.cancel_task",
            return_value=failed_task,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json", "build", "cancel", "t-cancel-failed",
            ])

        assert result.exit_code == 4
        data = self._parse_json_output(result.output)
        assert data["status"] == "error"
        assert data["code"] == "TASK_CANCEL_FAILED"
        assert data["details"]["cancel_result"]["remaining"] == [51001, 51002]
        assert data["details"]["cancel_result"]["processes"][0]["error"] == "taskkill timed out"

    def test_build_cancel_incomplete_outputs_is_structured_nonzero_json(self):
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        cancelled_task = {
            "task_id": "t-cancel-incomplete",
            "command": "build.compile",
            "status": "cancelled",
            "cancelled": True,
            "cancel_result": {
                "killed": [51101],
                "remaining": [],
                "processes": [],
            },
            "output_integrity": {
                "status": "incomplete",
                "code": "BUILD_CANCELLED_OUTPUTS_INCOMPLETE",
                "message": "Two runtime dependencies are missing.",
                "missing_runtime_dependency_count": 2,
                "missing_runtime_dependencies": [
                    r"F:\Engine\Binaries\Win64\tbbmalloc.dll",
                    r"F:\Engine\Binaries\Win64\libfbxsdk.dll",
                ],
                "recovery_command": (
                    'ue-cli --project "F:\\Game\\Game.uproject" build compile '
                    "--platform Win64 --config Development"
                ),
            },
        }
        with patch(
            "cli_anything.unreal.core.tasks.cancel_task",
            return_value=cancelled_task,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json", "build", "cancel", "t-cancel-incomplete",
            ])

        assert result.exit_code == 4
        data = self._parse_json_output(result.output)
        assert data["status"] == "error"
        assert data["code"] == "BUILD_CANCELLED_OUTPUTS_INCOMPLETE"
        assert data["details"]["status"] == "cancelled"
        assert data["details"]["output_integrity"][
            "missing_runtime_dependency_count"
        ] == 2
        assert data["suggestion"].startswith("Run: ue-cli --project")

    def test_build_stop_partial_is_structured_nonzero_json(self, temp_project):
        """Project stop propagates task/process diagnostics instead of success."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        partial = {
            "status": "partial",
            "killed": [52001],
            "remaining": [52002],
            "tasks": [{
                "task_id": "t-stop-failed",
                "status": "running",
                "remaining": [52002],
                "processes": [{"pid": 52002, "error": "access denied"}],
            }],
        }
        with patch(
            "cli_anything.unreal.core.build.stop_build",
            return_value=partial,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "stop",
            ])

        assert result.exit_code == 4
        data = self._parse_json_output(result.output)
        assert data["status"] == "error"
        assert data["code"] == "TASK_CANCEL_FAILED"
        assert data["details"] == partial

    def test_generic_task_cancel_failure_is_structured_nonzero_json(self):
        """The shared task-cancel surface follows the same failure contract."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        failed_task = {
            "task_id": "t-generic-failed",
            "command": "build.compile",
            "status": "running",
            "error": {
                "code": "TASK_CANCEL_FAILED",
                "message": "Build task cancellation left processes running.",
            },
            "cancel_result": {
                "killed": [],
                "remaining": [53001],
                "processes": [{"pid": 53001, "error": "query failed"}],
            },
        }
        with patch(
            "cli_anything.unreal.unreal_cli.cancel_task",
            return_value=failed_task,
        ):
            result = CliRunner().invoke(cli, [
                "--output", "json", "task", "cancel", "t-generic-failed",
            ])

        assert result.exit_code == 4
        data = self._parse_json_output(result.output)
        assert data["status"] == "error"
        assert data["code"] == "TASK_CANCEL_FAILED"
        assert data["details"]["cancel_result"]["remaining"] == [53001]

    def test_task_status_reports_reconciled_worker_exit(self):
        """Generic task status must observe the reconciled terminal record."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        failed_task = {
            "task_id": "t-stale-build",
            "command": "build.compile",
            "status": "failed",
            "worker_pid": 62224,
            "pid": 107964,
            "error": {
                "code": "TASK_WORKER_EXITED",
                "message": "Background build worker exited.",
            },
            "reconciliation": {
                "reason": "tracked_processes_exited",
                "outcome": "failed",
                "processes": [],
            },
        }
        with patch(
            "cli_anything.unreal.unreal_cli.reconcile_task_state",
            return_value=failed_task,
        ) as reconcile:
            result = CliRunner().invoke(cli, [
                "--output", "json", "task", "status", "t-stale-build",
            ])

        assert result.exit_code == 0
        data = self._parse_json_output(result.output)
        assert data["status"] == "failed"
        assert data["error"]["code"] == "TASK_WORKER_EXITED"
        assert data["reconciliation"]["outcome"] == "failed"
        reconcile.assert_called_once_with("t-stale-build")

    def test_build_status_reports_reconciled_worker_exit(self, temp_project):
        """Project build status by task ID uses the same reconciliation."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        failed_task = {
            "task_id": "t-stale-build",
            "command": "build.compile",
            "status": "failed",
            "error": {
                "code": "TASK_WORKER_EXITED",
                "message": "Background build worker exited.",
            },
        }
        with patch(
            "cli_anything.unreal.commands.build.reconcile_task_state",
            return_value=failed_task,
        ) as reconcile:
            result = CliRunner().invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "status", "t-stale-build",
            ])

        assert result.exit_code == 0
        data = self._parse_json_output(result.output)
        assert data["result"]["status"] == "failed"
        assert data["result"]["error"]["code"] == "TASK_WORKER_EXITED"
        reconcile.assert_called_once_with("t-stale-build")

    def test_build_compile_no_wait_then_stop_cancels_task(
        self, temp_project, tmp_path, monkeypatch
    ):
        """The documented async compile/stop sequence must cancel its task."""
        from click.testing import CliRunner

        from cli_anything.unreal.core.tasks import create_task, save_task
        from cli_anything.unreal.unreal_cli import cli

        monkeypatch.setenv("UE_CLI_TASK_DIR", str(tmp_path / "tasks"))
        task_holder = {}
        killed = []

        def fake_submit(command, payload):
            task = create_task(command, payload)
            task.update({"status": "running", "worker_pid": 41001, "pid": 41002})
            task_holder["task"] = task
            return save_task(task)

        def fake_kill(pid):
            killed.append(pid)
            return {
                "ok": True,
                "pid": pid,
                "already_exited": pid == 41002,
            }

        def fake_process_info(pid):
            task_id = task_holder["task"]["task_id"]
            return {
                "query_ok": True,
                "found": True,
                "pid": pid,
                "parent_pid": 1 if pid == 41001 else 41001,
                "name": "python.exe" if pid == 41001 else "powershell.exe",
                "cmdline": (
                    f"python -m cli_anything.unreal _task-worker run {task_id}"
                    if pid == 41001 else "powershell.exe -EncodedCommand AAA="
                ),
            }

        no_processes = {"killed": [], "remaining": [], "status": "none"}
        runner = CliRunner()
        with patch(
            "cli_anything.unreal.commands.build._guard_compile_against_editor_locks"
        ), patch(
            "cli_anything.unreal.commands.build.submit_task",
            side_effect=fake_submit,
        ), patch(
            "cli_anything.unreal.core.build.kill_build_processes",
            return_value=no_processes,
        ), patch(
            "cli_anything.unreal.utils.ue_backend.kill_build_processes",
            return_value=no_processes,
        ), patch(
            "cli_anything.unreal.core.tasks._query_process_info",
            side_effect=fake_process_info,
        ), patch(
            "cli_anything.unreal.utils.ue_backend._kill_process_tree_result",
            side_effect=fake_kill,
        ):
            submitted = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "compile", "--no-wait",
            ])
            stopped = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "stop",
            ])

        assert submitted.exit_code == 0
        task_id = self._parse_json_output(submitted.output)["result"]["task_id"]
        assert stopped.exit_code == 0
        result = self._parse_json_output(stopped.output)["result"]
        assert result["status"] == "ok"
        assert result["remaining"] == []
        assert result["tasks"][0]["task_id"] == task_id
        assert result["tasks"][0]["status"] == "cancelled"
        assert killed == [41001, 41002]

    def test_build_is_building_cli_false(self, temp_project):
        """build is-building returns building=false when no processes."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "is-building",
            ])
            assert result.exit_code == 0
            data = self._parse_json_output(result.output)
            assert data["result"]["building"] is False

    def test_build_is_building_cli_true(self, temp_project):
        """build is-building returns building=true when processes detected."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        mock_procs = [
            {"pid": 1234, "name": "MSBuild.exe", "cmdline": "MSBuild.exe project.vcxproj", "project": temp_project["uproject"]},
        ]
        with patch("cli_anything.unreal.core.build.find_running_build_processes", return_value=mock_procs):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "is-building",
            ])
            assert result.exit_code == 0
            data = self._parse_json_output(result.output)
            assert data["result"]["building"] is True

    def test_build_is_building_cli_probe_timeout_is_structured_error(
        self, temp_project
    ):
        """A timed-out state probe must emit JSON and exit non-zero."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli
        from cli_anything.unreal.utils.ue_backend import BuildProcessProbeError

        error = BuildProcessProbeError(
            "Windows build-process query timed out after 5 seconds.",
            details={"reason": "timeout", "timeout_seconds": 5},
        )
        runner = CliRunner()
        with patch(
            "cli_anything.unreal.core.tasks.active_build_tasks",
            return_value=[],
        ), patch(
            "cli_anything.unreal.core.build.find_running_build_processes",
            side_effect=error,
        ):
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "is-building",
            ])

        assert result.exit_code == 4
        data = self._parse_json_output(result.output)
        assert data["status"] == "error"
        assert data["code"] == "BUILD_STATE_PROBE_FAILED"
        assert data["details"] == {
            "reason": "timeout",
            "timeout_seconds": 5,
        }

    def test_build_stop_none_running(self, temp_project):
        """build stop when nothing is running returns status=none."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        with patch("cli_anything.unreal.core.build.kill_build_processes", return_value={
            "killed": [], "remaining": [], "status": "none",
        }):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", "json", "--project", temp_project["uproject"],
                "build", "stop",
            ])
            assert result.exit_code == 0
            data = self._parse_json_output(result.output)
            assert data["result"]["status"] == "none"


# ═══════════════════════════════════════════════════════════════════════
#  Real E2E tests with F:\Test574
# ═══════════════════════════════════════════════════════════════════════

TEST574_UPROJECT = r"F:\Test574\Test574.uproject"


@pytest.mark.skipif(
    not Path(TEST574_UPROJECT).exists(),
    reason="F:\\Test574\\Test574.uproject not available"
)

class TestBuildE2E:
    """Real end-to-end tests using F:\\Test574 project."""

    def test_build_status_real(self):
        """build status against real project."""
        from cli_anything.unreal.core.build import build_status

        result = build_status(TEST574_UPROJECT)
        assert result["project"] == "Test574"
        assert "platforms" in result

    def test_is_building_real(self):
        """is_building returns state or an explicit host probe failure."""
        from cli_anything.unreal.core.build import is_building
        from cli_anything.unreal.utils.ue_backend import BuildProcessProbeError

        try:
            result = is_building(TEST574_UPROJECT)
        except BuildProcessProbeError as exc:
            assert exc.details["reason"] in {
                "timeout",
                "query_failed",
                "invalid_result",
            }
            return
        assert "building" in result
        assert "processes" in result
        assert isinstance(result["building"], bool)

    @staticmethod
    def _parse_json_output(output: str) -> dict:
        idx = output.find("{")
        if idx == -1:
            return json.loads(output)
        return json.loads(output[idx:])

    def test_build_is_building_cli_real(self):
        """build is-building CLI against real project."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", "json", "--project", TEST574_UPROJECT,
            "build", "is-building",
        ])
        data = self._parse_json_output(result.output)
        if result.exit_code == 0:
            assert data["status"] == "success"
            assert "building" in data["result"]
        else:
            assert result.exit_code == 4
            assert data["status"] == "error"
            assert data["code"] == "BUILD_STATE_PROBE_FAILED"

    def test_build_status_cli_real(self):
        """build status CLI against real project."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", "json", "--project", TEST574_UPROJECT,
            "build", "status",
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["result"]["project"] == "Test574"

    def test_find_running_build_processes_real(self):
        """find_running_build_processes on real system."""
        from cli_anything.unreal.utils.ue_backend import find_running_build_processes

        # Without project filter
        all_procs = find_running_build_processes()
        assert isinstance(all_procs, list)

        # With project filter
        filtered = find_running_build_processes(TEST574_UPROJECT)
        assert isinstance(filtered, list)

    def test_build_stop_cli_real(self):
        """Real stop returns success or an explicit host/process failure."""
        from click.testing import CliRunner
        from cli_anything.unreal.unreal_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", "json", "--project", TEST574_UPROJECT,
            "build", "stop",
        ])
        data = self._parse_json_output(result.output)
        if result.exit_code == 0:
            assert data["status"] == "success"
            assert data["result"]["status"] in ("none", "ok")
        else:
            assert result.exit_code == 4
            assert data["status"] == "error"
            assert data["code"] == "TASK_CANCEL_FAILED"
            assert data["details"]["status"] == "partial"
            assert data["details"].get("remaining") or (
                data["details"].get("process_probe", {}).get("status") == "failed"
            )
