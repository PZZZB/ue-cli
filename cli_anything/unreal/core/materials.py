"""core/materials.py — Material viewing and analysis.

Provides material listing, inspection, and automated issue detection
for AI Agent analysis workflows. Requires a running UE editor with
Remote Control API plugin (default port 30010).

Uses two approaches:
1. /remote/search/assets — Fast asset search by class (for listing)
2. /remote/object/call + /remote/object/property — Direct UObject queries
3. Python script execution — For complex queries not possible via REST
"""

import json
import time
from pathlib import Path
from typing import Optional

from cli_anything.unreal.core.plugin_bridge import ensure_plugin_deployed
from cli_anything.unreal.core.script_runner import SavePolicy
from cli_anything.unreal.errors import UeCliError, raise_for_legacy_error
from cli_anything.unreal.utils.ue_http_api import UEEditorAPI


def _raise_if_editor_became_unreachable(api: UEEditorAPI, result: dict) -> None:
    """Propagate a failed editor request when the endpoint has disappeared."""
    if result.get("error") and not api.is_alive():
        raise ConnectionError(str(result["error"]))


def _material_asset_path_candidates(material_path: str) -> list[str]:
    """Return likely loadable material asset paths for UE object path forms."""
    base_path = str(material_path).strip().split(":", 1)[0]
    if not base_path:
        return [base_path]

    candidates: list[str] = []

    def add(path: str) -> None:
        if path and path not in candidates:
            candidates.append(path)

    leaf = base_path.rsplit("/", 1)[-1]
    if "." not in leaf:
        add(base_path)
        add(base_path + "." + leaf)
        return candidates

    package_path, object_name = base_path.rsplit(".", 1)
    add(package_path + "." + object_name)
    add(package_path)
    return candidates


_MATERIAL_RESOLVER = '''
def _cli_load_material(asset_path, asset_candidates):
    tried = []

    def _is_package(obj):
        try:
            return obj.__class__.__name__ == "Package"
        except Exception:
            return False

    def _try_load(candidate):
        if not candidate or candidate in tried:
            return None, None
        tried.append(candidate)
        try:
            mat = unreal.EditorAssetLibrary.load_asset(candidate)
            if mat is not None and not _is_package(mat):
                return mat, candidate
        except Exception:
            pass
        try:
            mat = unreal.load_asset(candidate)
            if mat is not None and not _is_package(mat):
                return mat, candidate
        except Exception:
            pass
        try:
            mat = unreal.load_object(None, candidate)
            if mat is not None and not _is_package(mat):
                return mat, candidate
        except Exception:
            pass
        try:
            data = unreal.EditorAssetLibrary.find_asset_data(candidate)
            if data and data.is_valid():
                mat = data.get_asset()
                if mat is not None and not _is_package(mat):
                    return mat, candidate
        except Exception:
            pass
        return None, None

    for candidate in asset_candidates:
        mat, loaded_path = _try_load(candidate)
        if mat is not None:
            return mat, loaded_path, tried

    try:
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        wanted_package_names = set()
        wanted_asset_names = set()
        parent_paths = []
        for candidate in asset_candidates:
            package_name = str(candidate).split(":", 1)[0]
            leaf = package_name.rsplit("/", 1)[-1]
            if "." in leaf:
                object_name = package_name.rsplit(".", 1)[1]
                package_name = package_name.rsplit(".", 1)[0]
                wanted_asset_names.add(object_name)
            else:
                wanted_asset_names.add(leaf)
            wanted_package_names.add(package_name)
            parent_path = package_name.rsplit("/", 1)[0] or "/Game"
            if parent_path not in parent_paths:
                parent_paths.append(parent_path)

            for data in registry.get_assets_by_package_name(package_name, False):
                object_path = str(data.package_name) + "." + str(data.asset_name)
                mat, loaded_path = _try_load(object_path)
                if mat is not None:
                    return mat, loaded_path, tried

        for parent_path in parent_paths:
            for data in registry.get_assets_by_path(parent_path, False, False):
                if str(data.package_name) in wanted_package_names or str(data.asset_name) in wanted_asset_names:
                    object_path = str(data.package_name) + "." + str(data.asset_name)
                    mat, loaded_path = _try_load(object_path)
                    if mat is not None:
                        return mat, loaded_path, tried
    except Exception:
        pass

    return None, None, tried
'''


# ── Shared helper snippet for property type coercion ─────────────────

# ── Script templates for material editing ─────────────────────────────
_SCRIPT_GET_PARAM = '''
import unreal
import json

material_path = "{material_path}"
material_candidates = {material_path_candidates_json}
param_name = "{param_name}"

mat, loaded_asset_path, tried_asset_paths = _cli_load_material(material_path, material_candidates)
if mat is None:
    result = {{"error": "Material not found: " + material_path, "tried": tried_asset_paths}}
elif not isinstance(mat, unreal.MaterialInstanceConstant):
    result = {{"error": "Asset is not a MaterialInstanceConstant (get-param only works on MI): " + loaded_asset_path}}
else:
    mel = unreal.MaterialEditingLibrary
    try:
        found = False
        val = None
        param_type = None
        requested_name = param_name.casefold()

        # MaterialEditingLibrary enumerates the complete parameter hierarchy,
        # unlike the instance value arrays, which contain local overrides only.
        scalar_names = [str(name) for name in mel.get_scalar_parameter_names(mat)]
        scalar_name = next((name for name in scalar_names if name.casefold() == requested_name), None)
        if scalar_name is not None:
            val = mel.get_material_instance_scalar_parameter_value(mat, scalar_name)
            param_type = "scalar"
            found = True
        
        # Check vector
        if not found:
            vector_names = [str(name) for name in mel.get_vector_parameter_names(mat)]
            vector_name = next((name for name in vector_names if name.casefold() == requested_name), None)
            if vector_name is not None:
                c = mel.get_material_instance_vector_parameter_value(mat, vector_name)
                val = {{"r": c.r, "g": c.g, "b": c.b, "a": c.a}}
                param_type = "vector"
                found = True

        # Check texture
        if not found:
            texture_names = [str(name) for name in mel.get_texture_parameter_names(mat)]
            texture_name = next((name for name in texture_names if name.casefold() == requested_name), None)
            if texture_name is not None:
                tex = mel.get_material_instance_texture_parameter_value(mat, texture_name)
                val = str(tex.get_path_name()) if tex else None
                param_type = "texture"
                found = True

        # Check static switch
        if not found:
            static_switch_names = [str(name) for name in mel.get_static_switch_parameter_names(mat)]
            static_switch_name = next((name for name in static_switch_names if name.casefold() == requested_name), None)
            if static_switch_name is not None:
                val = bool(mel.get_material_instance_static_switch_parameter_value(mat, static_switch_name))
                param_type = "static_switch"
                found = True

        if found:
            result = {{"status": "ok", "action": "get_param", "material": loaded_asset_path, "param": param_name, "type": param_type, "value": val}}
        else:
            result = {{"error": "Parameter not found: " + param_name}}
            
    except Exception as e:
        result = {{"error": "get_param failed: " + str(e)}}
'''

_SCRIPT_SET_PARAM = '''
import unreal
import json
import math

material_path = "{material_path}"
material_candidates = {material_path_candidates_json}
param_name = "{param_name}"
param_type = "{param_type}"
param_value_raw = """{param_value}"""

mat, loaded_asset_path, tried_asset_paths = _cli_load_material(material_path, material_candidates)
if mat is None:
    result = {{"error": "Material not found: " + material_path, "tried": tried_asset_paths}}
elif not isinstance(mat, unreal.MaterialInstanceConstant):
    result = {{"error": "Asset is not a MaterialInstanceConstant (set-param only works on MI): " + loaded_asset_path}}
else:
    mel = unreal.MaterialEditingLibrary
    try:
        set_return = None
        parameter_names = []
        if param_type == "scalar":
            parameter_names = [str(name) for name in mel.get_scalar_parameter_names(mat)]
        elif param_type == "vector":
            parameter_names = [str(name) for name in mel.get_vector_parameter_names(mat)]
        elif param_type == "texture":
            parameter_names = [str(name) for name in mel.get_texture_parameter_names(mat)]
        else:
            result = {{"error": "Unknown param_type: " + param_type + ". Use scalar, vector, or texture."}}

        resolved_param_name = next(
            (name for name in parameter_names if name.casefold() == param_name.casefold()),
            None,
        )
        if param_type in ("scalar", "vector", "texture") and resolved_param_name is None:
            result = {{
                "error": "Parameter not found: " + param_name,
                "code": "MATERIAL_PARAM_NOT_FOUND",
                "action": "set_param",
                "material": loaded_asset_path,
                "param": param_name,
                "type": param_type,
                "available_parameters": parameter_names,
                "applied": False,
                "readback_match": False,
                "saved": False,
            }}
        if param_type == "scalar":
            if resolved_param_name is not None:
                val = float(param_value_raw)
                set_return = mel.set_material_instance_scalar_parameter_value(mat, resolved_param_name, val)
                readback_value = float(mel.get_material_instance_scalar_parameter_value(mat, resolved_param_name))
                readback_match = math.isclose(readback_value, val, rel_tol=1e-6, abs_tol=1e-6)
                result = {{
                    "status": "ok",
                    "action": "set_param",
                    "material": loaded_asset_path,
                    "param": param_name,
                    "resolved_param": resolved_param_name,
                    "type": "scalar",
                    "value": val,
                    "readback_value": readback_value,
                    "readback_match": readback_match,
                    "applied": readback_match,
                    "verification": "effective_parameter_readback",
                    "readback_tolerance": 1e-6,
                    "set_return": set_return,
                    "set_return_authoritative": False,
                }}
        elif param_type == "vector":
            if resolved_param_name is not None:
                parts = json.loads(param_value_raw)
                value = {{
                    "r": float(parts.get("r", 0)),
                    "g": float(parts.get("g", 0)),
                    "b": float(parts.get("b", 0)),
                    "a": float(parts.get("a", 1)),
                }}
                color = unreal.LinearColor(**value)
                set_return = mel.set_material_instance_vector_parameter_value(mat, resolved_param_name, color)
                readback_color = mel.get_material_instance_vector_parameter_value(mat, resolved_param_name)
                readback_value = {{
                    "r": float(readback_color.r),
                    "g": float(readback_color.g),
                    "b": float(readback_color.b),
                    "a": float(readback_color.a),
                }}
                readback_match = all(
                    math.isclose(readback_value[channel], value[channel], rel_tol=1e-6, abs_tol=1e-6)
                    for channel in ("r", "g", "b", "a")
                )
                result = {{
                    "status": "ok",
                    "action": "set_param",
                    "material": loaded_asset_path,
                    "param": param_name,
                    "resolved_param": resolved_param_name,
                    "type": "vector",
                    "value": value,
                    "readback_value": readback_value,
                    "readback_match": readback_match,
                    "applied": readback_match,
                    "verification": "effective_parameter_readback",
                    "readback_tolerance": 1e-6,
                    "set_return": set_return,
                    "set_return_authoritative": False,
                }}
        elif param_type == "texture":
            if resolved_param_name is not None:
                tex = unreal.EditorAssetLibrary.load_asset(param_value_raw)
                if tex is None:
                    result = {{"error": "Texture not found: " + param_value_raw}}
                else:
                    set_return = mel.set_material_instance_texture_parameter_value(mat, resolved_param_name, tex)
                    readback_texture = mel.get_material_instance_texture_parameter_value(mat, resolved_param_name)
                    requested_path = tex.get_path_name()
                    readback_value = readback_texture.get_path_name() if readback_texture is not None else None
                    readback_match = readback_value == requested_path
                    result = {{
                        "status": "ok",
                        "action": "set_param",
                        "material": loaded_asset_path,
                        "param": param_name,
                        "resolved_param": resolved_param_name,
                        "type": "texture",
                        "value": param_value_raw,
                        "requested_asset": requested_path,
                        "readback_value": readback_value,
                        "readback_match": readback_match,
                        "applied": readback_match,
                        "verification": "effective_parameter_readback",
                        "set_return": set_return,
                        "set_return_authoritative": False,
                    }}

        if isinstance(result, dict) and result.get("status") == "ok" and not result.get("readback_match"):
            result["status"] = "error"
            result["error"] = "Material parameter readback did not match requested value; asset was not saved."
            result["code"] = "MATERIAL_PARAM_READBACK_MISMATCH"
            result["saved"] = False
        elif isinstance(result, dict) and result.get("status") == "ok":
            mat.modify()
    except Exception as e:
        result = {{"error": "set_param failed: " + str(e)}}
'''



# ── Public API ────────────────────────────────────────────────────────

def list_materials(
    api: UEEditorAPI,
    content_path: str = "/Game/",
    project_dir: str | None = None,
) -> dict:
    """List all materials in the project via Remote Control search API.

    Args:
        api: Connected UEEditorAPI instance.
        content_path: Content path to search (e.g., "/Game").
        project_dir: Project directory (unused, kept for API compat).

    Returns:
        {"materials": [{"path": str, "name": str, "class": str, "metadata": dict}, ...]}
    """
    # Normalize path — search API wants no trailing slash
    pkg_path = content_path.rstrip("/")
    if not pkg_path:
        pkg_path = "/Game"

    result = api.search_assets(
        query="",
        class_names=["/Script/Engine.Material", "/Script/Engine.MaterialInstanceConstant"],
        package_paths=[pkg_path],
        recursive=True,
    )

    if "error" in result:
        return result

    assets = result.get("Assets", [])
    materials = []
    for asset in assets:
        materials.append({
            "path": asset.get("Path", ""),
            "name": asset.get("Name", ""),
            "class": asset.get("Class", ""),
            "metadata": asset.get("Metadata", {}),
        })

    return {"materials": materials}


_BRIDGE_CDO = "/Script/CliAnythingBridge.Default__CliAnythingBridgeLibrary"
_EDITOR_ASSET_LIBRARY_CDO = (
    "/Script/EditorScriptingUtilities.Default__EditorAssetLibrary"
)


def _material_object_path(material_path: str) -> str:
    candidates = _material_asset_path_candidates(material_path)
    return next(
        (path for path in candidates if "." in path.rsplit("/", 1)[-1]),
        candidates[0],
    )


def _call_material_bridge(
    api: UEEditorAPI,
    function_name: str,
    parameters: dict,
    *,
    missing_code: str,
    required_version: str,
    timeout: int = 30,
    generate_transaction: bool = False,
) -> dict:
    call_kwargs = {"timeout": timeout}
    if generate_transaction:
        call_kwargs["generate_transaction"] = True
    response = api.call_function(
        _BRIDGE_CDO,
        function_name,
        parameters,
        **call_kwargs,
    )
    _raise_if_editor_became_unreachable(api, response)

    raw = response.get("ReturnValue")
    if not isinstance(raw, str):
        return {
            "error": (
                f"{function_name} requires CliAnythingBridge "
                f"{required_version} or newer."
            ),
            "code": missing_code,
            "detail": response.get("error", response),
            "suggestion": "Run 'editor plugin-upgrade', then retry.",
        }
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "error": f"{function_name} returned invalid JSON: {exc}",
            "code": "MATERIAL_BRIDGE_INVALID_RESPONSE",
        }
    if not isinstance(result, dict):
        return {
            "error": f"{function_name} returned non-object JSON.",
            "code": "MATERIAL_BRIDGE_INVALID_RESPONSE",
        }
    return result


def _material_package_path(material_path: str) -> str:
    leaf = material_path.rsplit("/", 1)[-1]
    return material_path.rsplit(".", 1)[0] if "." in leaf else material_path


def _call_material_edit_bridge(
    api: UEEditorAPI,
    function_name: str,
    material_path: str,
    parameters: dict | None = None,
    *,
    timeout: int = 120,
) -> dict:
    call_parameters = {"Material": _material_object_path(material_path)}
    call_parameters.update(parameters or {})
    result = _call_material_bridge(
        api,
        function_name,
        call_parameters,
        missing_code="MATERIAL_EDIT_BRIDGE_REQUIRED",
        required_version="1.30",
        timeout=timeout,
        generate_transaction=True,
    )
    if result.get("status") not in {"ok", "error"}:
        return result

    package_path = _material_package_path(material_path)
    save_response = api.call_function(
        _EDITOR_ASSET_LIBRARY_CDO,
        "SaveAsset",
        {"AssetToSave": package_path, "bOnlyIfIsDirty": False},
        timeout=60,
    )
    _raise_if_editor_became_unreachable(api, save_response)
    if save_response.get("ReturnValue") is not True:
        return {
            "error": (
                "Material edit completed in memory, but the target asset "
                f"could not be saved: {package_path}"
            ),
            "code": "MATERIAL_SAVE_FAILED",
            "material": package_path,
            "edit_result": result,
            "detail": save_response.get("error", save_response),
        }
    result["saved"] = True
    result["saved_packages"] = [package_path]
    return result


def _call_material_info_bridge(api: UEEditorAPI, material_path: str) -> dict:
    load_response = api.call_function(
        _EDITOR_ASSET_LIBRARY_CDO,
        "LoadAsset",
        {"AssetPath": _material_package_path(material_path)},
        timeout=30,
    )
    _raise_if_editor_became_unreachable(api, load_response)

    result = _call_material_bridge(
        api,
        "GetMaterialInfo",
        {"Asset": _material_object_path(material_path)},
        missing_code="MATERIAL_INFO_BRIDGE_REQUIRED",
        required_version="1.29",
    )
    result.setdefault("material", material_path)
    return result


def get_material_info(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """Get detailed material data through the native bridge.

    Remote Control resolves the asset path directly into the bridge call, so
    Python never creates wrappers for material expression UObjects.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path (e.g., "/Game/NewMaterial").
        project_dir: Kept for public API compatibility.

    Returns:
        Dict with material properties, nodes, parameters, etc.
    """
    return _call_material_info_bridge(api, material_path)


def get_material_stats(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """Get material compilation statistics.

    Gets info and extracts stats-relevant fields.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        project_dir: Project directory.

    Returns:
        Dict with shader stats.
    """
    info = get_material_info(api, material_path, project_dir)
    raise_for_legacy_error(info, default_code="MATERIAL_STATS_FAILED")

    asset_class = info.get("class", "")
    if asset_class == "MaterialInstanceConstant":
        return {
            "error": (
                "Material compilation statistics are unavailable for "
                "MaterialInstanceConstant because ue-cli cannot inspect its "
                "compiled static permutation."
            ),
            "code": "MATERIAL_STATS_UNSUPPORTED_CLASS",
            "material": info.get("path") or material_path,
            "asset_class": asset_class,
            "parent": info.get("parent"),
            "supported_classes": ["Material"],
            "suggestion": (
                "Run 'material info' for effective instance parameters, or run "
                "'material get-stats <parent-material>' for parent graph statistics."
            ),
        }

    return {
        "path": material_path,
        "name": info.get("name", ""),
        "node_count": info.get("node_count", 0),
        "texture_sample_count": info.get("texture_sample_count", 0),
        "blend_mode": info.get("blend_mode", info.get("BlendMode", "")),
        "shading_model": info.get("shading_model", info.get("ShadingModel", "")),
        "material_domain": info.get("material_domain", info.get("MaterialDomain", "")),
    }


_PLUGIN_GET_ERRORS_SCRIPT = r'''import unreal

material_path = "{material_path}"
material_candidates = {material_path_candidates_json}
mat, loaded_asset_path, tried_asset_paths = _cli_load_material(material_path, material_candidates)
if mat is None:
    result = {{"error": "Material not found: " + material_path, "tried": tried_asset_paths}}
else:
    bridge = getattr(unreal, "CliAnythingBridgeLibrary", None)
    if bridge is None:
        result = {{
            "error": "CliAnythingBridgeLibrary is unavailable in this editor",
            "bridge_missing": True,
        }}
    elif isinstance(mat, (unreal.MaterialFunctionInterface, unreal.MaterialInterface)):
        if not hasattr(bridge, "get_material_compile_status"):
            result = {{
                "error": "Compile status requires Bridge 1.39. Run 'editor plugin-upgrade', then retry material get-errors.",
                "code": "MATERIAL_ERRORS_BRIDGE_UPGRADE_REQUIRED",
                "required_version": "1.39",
            }}
        else:
            import json
            result = json.loads(bridge.get_material_compile_status(mat))
            result.update({{
                "warnings": [],
                "material": loaded_asset_path,
                "asset_class": mat.get_class().get_name(),
                "source": "plugin",
            }})
    else:
        result = {{
            "error": "Unsupported asset class for material get-errors: " + mat.get_class().get_name(),
            "code": "MATERIAL_ERRORS_UNSUPPORTED_ASSET",
            "asset_class": mat.get_class().get_name(),
        }}
'''

_SHADER_EXPORT_CONTEXT_SCRIPT = r'''import unreal

def _cli_shader_export_context(bridge):
    if bridge is None:
        return {{"error": "CliAnythingBridgeLibrary is unavailable in this editor", "bridge_missing": True}}
    version = str(bridge.get_plugin_version())
    try:
        version_parts = tuple(int(part) for part in version.split("."))
    except ValueError:
        version_parts = ()
    if version_parts < (1, 40):
        return {{
            "error": "Shader export requires Bridge 1.40. Run 'editor plugin-upgrade', then retry.",
            "code": "MATERIAL_SHADER_EXPORT_BRIDGE_UPGRADE_REQUIRED",
            "required_version": "1.40",
            "loaded_version": version,
        }}
    return {{
        "checked_platform": str(bridge.get_active_shader_platform()),
        "platform_source": "host_rhi" if unreal.SystemLibrary.get_engine_version().startswith("4.") else "active_editor",
        "resource_scope": "material_interface",
    }}
'''

_PLUGIN_GET_HLSL_CODE_SCRIPT = _SHADER_EXPORT_CONTEXT_SCRIPT + r'''import unreal
import os

material_path = "{material_path}"
material_candidates = {material_path_candidates_json}
mat, loaded_asset_path, tried_asset_paths = _cli_load_material(material_path, material_candidates)
if mat is None:
    result = {{"error": "Material not found: " + material_path, "tried": tried_asset_paths}}
else:
    bridge = getattr(unreal, "CliAnythingBridgeLibrary", None)
    if bridge is None:
        result = {{
            "error": "CliAnythingBridgeLibrary is unavailable in this editor",
            "bridge_missing": True,
        }}
    elif not isinstance(mat, unreal.MaterialInterface):
        result = {{
            "error": "Unsupported asset class for material hlsl-code: " + mat.get_class().get_name(),
            "code": "MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET",
            "asset_class": mat.get_class().get_name(),
            "supported_classes": ["Material", "MaterialInstanceConstant"],
        }}
    else:
        result = _cli_shader_export_context(bridge)
    if mat is not None and "error" not in result:
        export_context = result
        # Construct output path under project Saved/CliAnything/
        _saved = unreal.Paths.project_saved_dir()
        output_path = os.path.join(_saved, "CliAnything", "{mat_name}.ush")
        output_path = output_path.replace("\\", "/")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        ret = bridge.get_material_hlsl_code(mat, output_path)
        if ret:
            with open(output_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = len(f.readlines())
            result = {{
                "material": loaded_asset_path,
                "file": output_path,
                "lines": lines,
                "source": "plugin",
                **export_context,
            }}
        else:
            result = {{"error": "No HLSL source for the requested material interface and platform. Its resource may be unavailable or translation failed.", **export_context}}
'''

_PLUGIN_GET_SHADER_SOURCE_SCRIPT = _SHADER_EXPORT_CONTEXT_SCRIPT + r'''import unreal
import os

material_path = "{material_path}"
material_candidates = {material_path_candidates_json}
mat, loaded_asset_path, tried_asset_paths = _cli_load_material(material_path, material_candidates)
if mat is None:
    result = {{"error": "Material not found: " + material_path, "tried": tried_asset_paths}}
else:
    bridge = getattr(unreal, "CliAnythingBridgeLibrary", None)
    result = _cli_shader_export_context(bridge)
if mat is not None and "error" not in result:
    export_context = result
    # Construct output dir under project Saved/CliAnything/
    _saved = unreal.Paths.project_saved_dir()
    output_dir = os.path.join(_saved, "CliAnything", "{mat_name}_shaders")
    output_dir = output_dir.replace("\\", "/")
    os.makedirs(output_dir, exist_ok=True)
    entries = bridge.get_material_shader_source(mat, output_dir)
    shaders = []
    for entry in entries:
        parts = entry.split("\t")
        if len(parts) >= 3:
            shaders.append({{"name": parts[0], "file": parts[1], "lines": int(parts[2])}})
        elif len(parts) >= 2:
            shaders.append({{"name": parts[0], "file": parts[1]}})
    if not shaders:
        result = {{
            "error": "Shader source extraction returned no shaders after refreshing changed shader files.",
            "material": loaded_asset_path,
            "output_dir": output_dir,
            **export_context,
        }}
    else:
        result = {{
            "material": loaded_asset_path,
            "shader_count": len(shaders),
            "shaders": shaders,
            "output_dir": output_dir,
            "shader_cache_refresh": "changed",
            "source": "plugin",
            "quality_level": "High",
            **export_context,
        }}
'''


def get_material_errors(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """Get current material compile errors via the CliAnythingBridge plugin.

    Reads the error state from FMaterialResource::GetCompileErrors() without
    triggering a recompile. Call ``recompile_material()`` first if you need
    fresh errors after making changes.

    Requires the bridge plugin to be compiled and loaded in the editor.
    This read-only command never deploys or rewrites the project plugin while
    the editor may hold the bridge DLL locked.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path (e.g. /Game/MyMaterial).
        project_dir: Project directory used only for upgrade guidance.

    Returns:
        Compile errors, checked platform, per-quality resources, and nullable verdicts
        or {"error": "..."} if plugin not available.
    """
    result = _exec_material_script(
        api,
        _PLUGIN_GET_ERRORS_SCRIPT,
        timeout=15.0,
        save_policy=SavePolicy.NEVER,
        material_path=material_path,
    )

    if "error" not in result:
        resources = result.get("resources", [])
        for resource in resources:
            if not resource.get("resource_present"):
                state = "resource_absent"
            elif not resource.get("compilation_finished"):
                state = "not_finished"
            elif resource.get("errors"):
                state = "errors"
            elif resource.get("shader_map_valid"):
                state = "complete"
            else:
                state = "shader_map_unavailable"
            resource["compile_state"] = state
        result["compilation_complete"] = bool(resources) and all(
            r["compile_state"] in ("complete", "errors") for r in resources
        )
        errors = result.get("errors", [])
        result["has_errors"] = True if errors else (
            False if result["compilation_complete"] else None
        )
        result["compile_succeeded"] = (
            not bool(errors) if result["compilation_complete"] else None
        )

    if result.get("bridge_missing") is True:
        plugin_dir = f"{project_dir}/Plugins/CliAnythingBridge" if project_dir else "<project>/Plugins/CliAnythingBridge"
        return {
            "error": (
                "Bridge plugin not loaded in this editor. "
                f"Project plugin path: {plugin_dir}. "
                "Run 'editor plugin-upgrade' to deploy/recompile/restart, "
                "then retry material get-errors."
            )
        }

    return result


def get_material_hlsl_code(
    api: UEEditorAPI,
    material_path: str,
    output_path: str | None = None,
    project_dir: str | None = None,
) -> dict:
    """Get the material's HLSL expression source code via the Bridge plugin.

    This calls FMaterialResource::GetMaterialExpressionSource() which returns
    /Engine/Generated/Material.ush — the material's translated HLSL including
    FMaterialPixelParameters struct and all custom node code.

    Output is written to <project>/Saved/CliAnything/<MaterialName>.ush by default.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Material or MaterialInstanceConstant content path
            (e.g. /Game/M_Test). MaterialFunction is unsupported.
        output_path: Optional custom output path. Defaults to project Saved dir.
        project_dir: Project directory for auto-deploying the plugin.

    Returns:
        {"material": str, "file": str, "lines": int, "source": "plugin"}
    """
    if project_dir:
        deploy_result = ensure_plugin_deployed(project_dir)
        if not deploy_result["deployed"]:
            return {"error": deploy_result.get("error", "Plugin deployment failed")}

    mat_name = material_path.rsplit("/", 1)[-1].split(".")[0]

    result = _exec_material_script(
        api,
        _PLUGIN_GET_HLSL_CODE_SCRIPT,
        timeout=60.0,
        save_policy=SavePolicy.NEVER,
        material_path=material_path,
        mat_name=mat_name,
    )

    if result.get("bridge_missing") is True:
        plugin_dir = f"{project_dir}/Plugins/CliAnythingBridge" if project_dir else "<project>/Plugins/CliAnythingBridge"
        return {
            "error": (
                "Bridge plugin not loaded. "
                f"Plugin source has been deployed to {plugin_dir}. "
                "Run 'editor plugin-upgrade' to compile and activate, "
                "or manually recompile the project and restart the editor."
            )
        }

    return result


def get_material_shader_source(
    api: UEEditorAPI,
    material_path: str,
    output_dir: str | None = None,
    project_dir: str | None = None,
) -> dict:
    """Get the compiled shader source (.usf files) for a material via the Bridge plugin.

    This forces a synchronous recompile with bExtractShaderSource=true,
    then writes each compiled shader (BasePassPS, BasePassVS, LumenCardPS, etc.)
    to a .usf file. Output goes to <project>/Saved/CliAnything/<MaterialName>_shaders/ by default.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path (e.g. /Game/M_Test).
        output_dir: Optional custom output directory. Defaults to project Saved dir.
        project_dir: Project directory for auto-deploying the plugin.

    Returns:
        {"material": str, "shader_count": int, "shaders": [...], "output_dir": str}
    """
    if project_dir:
        deploy_result = ensure_plugin_deployed(project_dir)
        if not deploy_result["deployed"]:
            return {"error": deploy_result.get("error", "Plugin deployment failed")}

    mat_name = material_path.rsplit("/", 1)[-1].split(".")[0]

    result = _exec_material_script(
        api,
        _PLUGIN_GET_SHADER_SOURCE_SCRIPT,
        timeout=120.0,
        save_policy=SavePolicy.NEVER,
        material_path=material_path,
        mat_name=mat_name,
    )

    if "error" in result and "CliAnythingBridgeLibrary" in result.get("error", ""):
        plugin_dir = f"{project_dir}/Plugins/CliAnythingBridge" if project_dir else "<project>/Plugins/CliAnythingBridge"
        return {
            "error": (
                "Bridge plugin not loaded. "
                f"Plugin source has been deployed to {plugin_dir}. "
                "Run 'editor plugin-upgrade' to compile and activate, "
                "or manually recompile the project and restart the editor."
            )
        }

    return result


def get_material_texture_list(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """List all textures referenced by a material.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        project_dir: Project directory.

    Returns:
        {"textures": [...]}
    """
    info = get_material_info(api, material_path, project_dir)
    raise_for_legacy_error(info, default_code="MATERIAL_TEXTURE_LIST_FAILED")

    textures = info.get("textures", [])
    tex_params = info.get("texture_parameters", [])

    # Merge texture parameters into list
    all_textures = list(textures)
    for tp in tex_params:
        all_textures.append({
            "parameter_name": tp.get("name", ""),
            "path": tp.get("texture"),
            "name": tp.get("texture", "").split(".")[-1] if tp.get("texture") else None,
        })

    return {"textures": all_textures, "material": material_path}


def get_material_connections(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """Get the full material node connection graph.

    Builds a complete topology from node-to-node edges (via
    ``get_inputs_for_material_expression``) and material output pin
    connections. A node is "connected" if it's reachable (directly or
    transitively) from any material output; otherwise it's "orphan".

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path (e.g., "/Game/M_Water").
        project_dir: Project directory for temp files.

    Returns:
        {
            "material": str,
            "material_outputs": {"BaseColor": {"node": ..}, ..},
            "edges": [{"from_node": str, "to_node": str, "to_input_index": int}, ..],
            "nodes": [{"name": str, "type": str, ..}, ..],
            "node_count": int,
            "connected_nodes": [str, ..],
            "orphan_nodes": [str, ..],
        }
    """
    info = get_material_info(api, material_path, project_dir)
    raise_for_legacy_error(info, default_code="MATERIAL_GRAPH_FAILED")

    mat_outputs = info.get("material_outputs", {})
    nodes = info.get("nodes", [])
    edges = info.get("edges", [])

    # Build reverse adjacency: node → set of nodes that feed into it
    # We need forward lookup: from a "sink" node, find all upstream nodes
    upstream = {}  # node_name → set of upstream node_names
    for e in edges:
        upstream.setdefault(e["to_node"], set()).add(e["from_node"])

    # Seed: nodes directly connected to material output pins
    seeds = {v["node"] for v in mat_outputs.values() if isinstance(v, dict)}

    # Also treat custom output nodes as seeds (SLW, Strata, etc.)
    for n in nodes:
        t = n.get("type", "")
        if "Output" in t and t != "MaterialExpressionCustomOutput":
            seeds.add(n["name"])

    # BFS backwards through edges to find all transitively connected nodes
    connected = set()
    queue = list(seeds)
    while queue:
        name = queue.pop()
        if name in connected:
            continue
        connected.add(name)
        for src in upstream.get(name, []):
            if src not in connected:
                queue.append(src)

    all_names = {n["name"] for n in nodes}
    orphan_names = sorted(all_names - connected)

    return {
        "material": material_path,
        "material_outputs": mat_outputs,
        "edges": edges,
        "nodes": nodes,
        "node_count": len(nodes),
        "connected_nodes": sorted(connected),
        "orphan_nodes": orphan_names,
        "orphan_count": len(orphan_names),
    }


def analyze_material(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """Comprehensive material analysis — detect common issues.

    Checks:
    - Instruction count warnings
    - Too many texture samples (>16)
    - Missing texture references
    - Blend mode / transparency mismatch
    - High node count
    - Large texture dimensions

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        project_dir: Project directory.

    Returns:
        {"issues": [...], "warnings": [...], "stats": {...}, "info": {...}}
    """
    issues = []
    warnings = []

    # Get full material info
    info = get_material_info(api, material_path, project_dir)
    raise_for_legacy_error(info, default_code="MATERIAL_ANALYZE_FAILED")

    asset_class = info.get("class", "")
    if asset_class == "MaterialInstanceConstant":
        suggestion = (
            "Run 'material info' for effective instance parameters, or run "
            "'material analyze <parent-material>' for parent graph analysis."
        )
        raise UeCliError(
            code="MATERIAL_ANALYZE_UNSUPPORTED_CLASS",
            message=(
                "Material graph analysis is unavailable for "
                "MaterialInstanceConstant because ue-cli cannot inspect its "
                "compiled static permutation."
            ),
            exit_code=3,
            suggestion=suggestion,
            details={
                "material": info.get("path") or material_path,
                "asset_class": asset_class,
                "parent": info.get("parent"),
                "supported_classes": ["Material"],
            },
        )

    # ── Analysis rules ────────────────────────────────────────────────

    # Check texture sample count
    tex_count = info.get("texture_sample_count", 0)
    if tex_count > 16:
        issues.append(
            f"Texture sample count ({tex_count}) exceeds recommended maximum of 16"
        )
    elif tex_count > 12:
        warnings.append(
            f"Texture sample count ({tex_count}) approaching limit (max 16)"
        )
    elif tex_count > 8:
        warnings.append(
            f"Texture sample count ({tex_count}) approaching mobile limit"
        )

    # Check total node count
    node_count = info.get("node_count", 0)
    if node_count > 200:
        issues.append(f"Very high node count ({node_count}) - consider simplifying")
    elif node_count > 100:
        warnings.append(f"High node count ({node_count})")

    # Check for missing textures
    for tex in info.get("textures", []):
        if tex.get("path") is None or tex.get("name") is None:
            issues.append(f"Missing texture reference in node: {tex.get('node_type', 'unknown')}")

    # Check texture sizes
    for tex in info.get("textures", []):
        size_x = tex.get("size_x", 0)
        size_y = tex.get("size_y", 0)
        if size_x > 4096 or size_y > 4096:
            warnings.append(
                f"Large texture '{tex.get('name', '?')}': {size_x}x{size_y} "
                f"(consider downscaling for performance)"
            )

    # Check blend mode
    blend_mode = info.get("blend_mode", info.get("BlendMode", ""))
    if "Translucent" in str(blend_mode) and tex_count > 4:
        warnings.append(
            "Translucent material with many texture samples may cause overdraw issues"
        )

    # Check material output connections (agent's most requested analysis)
    mat_outputs = info.get("material_outputs", {})
    if mat_outputs:
        connected_names = {v["node"] for v in mat_outputs.values() if isinstance(v, dict)}
        all_names = {n["name"] for n in info.get("nodes", [])}
        orphan_names = all_names - connected_names
        if orphan_names and len(orphan_names) > node_count * 0.5 and node_count > 5:
            warnings.append(
                f"{len(orphan_names)}/{node_count} nodes not directly connected to "
                f"any material output (may be intermediate or unused)"
            )
    elif "material_outputs_error" not in info and node_count > 0:
        warnings.append(
            "No material output connections detected — material may produce no visible output"
        )

    # Check for duplicate Custom nodes (common when rebuilding materials)
    custom_nodes = [n for n in info.get("nodes", []) if n.get("type") == "MaterialExpressionCustom"]
    if len(custom_nodes) > 5:
        warnings.append(
            f"High number of Custom HLSL nodes ({len(custom_nodes)}) — "
            "review for duplicates from prior edits"
        )

    stats = {
        "texture_sample_count": tex_count,
        "node_count": node_count,
        "texture_count": len(info.get("textures", [])),
        "blend_mode": str(blend_mode),
        "shading_model": info.get("shading_model", info.get("ShadingModel", "")),
        "material_domain": info.get("material_domain", info.get("MaterialDomain", "")),
        "connected_outputs": list(mat_outputs.keys()) if mat_outputs else [],
        "custom_node_count": len(custom_nodes),
    }

    return {
        "material": material_path,
        "issues": issues,
        "warnings": warnings,
        "stats": stats,
        "info": info,
    }


# ── Material editing (via MaterialEditingLibrary) ────────────────────

def add_material_node(
    api: UEEditorAPI,
    material_path: str,
    expression_class: str,
    pos_x: int = 0,
    pos_y: int = 0,
    set_props: list[tuple[str, str]] | None = None,
    add_input_names: list[str] | None = None,
    project_dir: str | None = None,
) -> dict:
    """Add a new material expression node.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path (e.g., "/Game/M_Test").
        expression_class: UE expression class name
            (e.g., "MaterialExpressionConstant3Vector").
        pos_x: Node X position in the material graph.
        pos_y: Node Y position in the material graph.
        set_props: List of (key, value) pairs to set on the node via set_editor_property.
        add_input_names: List of input names to add (for MaterialExpressionCustom).
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", "node": {"name": str, "type": str}} or {"error": str}
    """
    return _call_material_edit_bridge(
        api,
        "AddMaterialExpression",
        material_path,
        {
            "ExpressionClass": expression_class,
            "PosX": pos_x,
            "PosY": pos_y,
            "Properties": dict(set_props or []),
            "InputNames": add_input_names or [],
        },
    )


def delete_material_node(
    api: UEEditorAPI,
    material_path: str,
    node_name: str,
    project_dir: str | None = None,
) -> dict:
    """Delete a material expression node by name.

    Use ``material info`` to find node names first.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        node_name: Name of the expression to delete (e.g., "Constant3Vector_0").
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", "deleted_node": str} or {"error": str}
    """
    return _call_material_edit_bridge(
        api,
        "DeleteMaterialExpression",
        material_path,
        {"NodeName": node_name},
    )


def rename_custom_input(
    api: UEEditorAPI,
    material_path: str,
    node_name: str,
    old_name: str,
    new_name: str,
    update_code: bool = True,
    project_dir: str | None = None,
) -> dict:
    """Rename a MaterialExpressionCustom input variable.

    Custom node HLSL variable names come from ``inputs[].input_name``.
    MaterialInstance parameter names and node display text do not rename
    those variables. This helper updates the real Custom input name and,
    by default, rewrites whole-word HLSL references from old_name to new_name.
    """
    return _call_material_edit_bridge(
        api,
        "RenameMaterialCustomInput",
        material_path,
        {
            "NodeName": node_name,
            "OldName": old_name,
            "NewName": new_name,
            "bUpdateCode": bool(update_code),
        },
    )


def connect_material_nodes(
    api: UEEditorAPI,
    material_path: str,
    from_node: str,
    from_output: str,
    to_node: str,
    to_input: str,
    project_dir: str | None = None,
) -> dict:
    """Connect two material expression nodes.

    To connect to a material output pin (BaseColor, Normal, etc.),
    use ``to_node="__material_output__"`` and ``to_input="BaseColor"``.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        from_node: Source expression name.
        from_output: Source output pin name (often "" for single-output nodes).
        to_node: Target expression name, or "__material_output__".
        to_input: Target input pin name, or material property name.
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", "action": "connect", ...} or {"error": str}
    """
    function_name = (
        "ConnectMaterialOutput"
        if to_node == "__material_output__"
        else "ConnectMaterialExpressions"
    )
    parameters = {
        "FromNode": from_node,
        "FromOutputName": from_output,
    }
    if to_node == "__material_output__":
        parameters["PropertyName"] = to_input
    else:
        parameters.update({"ToNode": to_node, "ToInputName": to_input})
    return _call_material_edit_bridge(
        api,
        function_name,
        material_path,
        parameters,
    )


def disconnect_material_nodes(
    api: UEEditorAPI,
    material_path: str,
    from_node: str,
    from_output: str,
    to_node: str,
    to_input: str,
    project_dir: str | None = None,
) -> dict:
    """Disconnect material expression nodes.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        from_node: Source expression name.
        from_output: Source output pin name.
        to_node: Target expression name, or "__material_output__".
        to_input: Target input pin name, or material property name.
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", "action": "disconnect", ...} or {"error": str}
    """
    function_name = (
        "DisconnectMaterialOutput"
        if to_node == "__material_output__"
        else "DisconnectMaterialExpression"
    )
    parameters = (
        {"PropertyName": to_input}
        if to_node == "__material_output__"
        else {"ToNode": to_node, "ToInputName": to_input}
    )
    result = _call_material_edit_bridge(
        api,
        function_name,
        material_path,
        parameters,
    )
    if result.get("status") == "ok":
        result.setdefault("from", from_node)
        result.setdefault("from_output", from_output)
        result.setdefault("to", to_node)
        result.setdefault("to_input", to_input)
    return result



def get_material_param(
    api: UEEditorAPI,
    material_path: str,
    param_name: str,
    project_dir: str | None = None,
) -> dict:
    """Get a parameter value on a MaterialInstanceConstant.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path to a MaterialInstanceConstant.
        param_name: Parameter name (e.g., "Roughness").
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", "value": ...} or {"error": str}
    """
    return _exec_material_script(
        api,
        _SCRIPT_GET_PARAM,
        project_dir=project_dir,
        save_policy=SavePolicy.NEVER,
        material_path=material_path,
        param_name=param_name,
    )


def set_material_param(
    api: UEEditorAPI,
    material_path: str,
    param_name: str,
    param_value: str,
    param_type: str = "scalar",
    project_dir: str | None = None,
) -> dict:
    """Set a parameter value on a MaterialInstanceConstant.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path to a MaterialInstanceConstant.
        param_name: Parameter name (e.g., "Roughness").
        param_value: Value as string. For scalar: "0.5".
            For vector: '{"r":1,"g":0,"b":0,"a":1}'.
            For texture: "/Game/Textures/T_Diffuse".
        param_type: "scalar", "vector", or "texture".
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", ...} or {"error": str}
    """
    return _exec_material_script(
        api,
        _SCRIPT_SET_PARAM,
        project_dir=project_dir,
        save_policy=SavePolicy.TARGET_PACKAGES,
        target_packages=[material_path],
        material_path=material_path,
        param_name=param_name,
        param_value=param_value,
        param_type=param_type,
    )


def recompile_material(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
) -> dict:
    """Recompile a material.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path.
        project_dir: Project directory for temp files.

    Returns:
        {"status": "ok", "action": "recompile"} or {"error": str}
    """
    return _call_material_edit_bridge(
        api,
        "RecompileMaterial",
        material_path,
        timeout=120,
    )


# ── Shader / HLSL code ──────────────────────────────────────────────

# Map user-friendly platform names to UE ShaderDebugInfo directory names
SHADER_PLATFORMS = {
    "sm6":       "PCD3D_SM6",
    "sm5":       "PCD3D_SM5",
    "vulkan":    "VULKAN_SM5",
    "vulkan_sm5": "VULKAN_SM5",
    "vulkan_android": "VULKAN_SM5_ANDROID",
    "vulkan_sm5_android": "VULKAN_SM5_ANDROID",
    "vulkan_sm5_android_preview": "VULKAN_SM5_ANDROID_Preview",
    "vulkan_es31": "VULKAN_ES3_1_ANDROID",
    "vulkan_es31_android_preview": "VULKAN_ES3_1_ANDROID_Preview",
    "opengl_es31": "OPENGL_ES3_1_ANDROID",
    "metal":     "METAL_SM5",
    "metal_sm5": "METAL_SM5",
}

SHADER_DUMP_WAIT_TIMEOUT_SECONDS = 10.0


_PLUGIN_GET_ACTIVE_SHADER_PLATFORM_SCRIPT = r'''import unreal

bridge = getattr(unreal, "CliAnythingBridgeLibrary", None)
if bridge is None:
    result = {{
        "error": "CliAnythingBridge is not loaded; shader-dump platform validation is unavailable.",
        "code": "MATERIAL_SHADER_DUMP_BRIDGE_UNAVAILABLE",
    }}
else:
    active_platform = str(bridge.get_active_shader_platform())
    if active_platform:
        result = {{"active_platform": active_platform}}
    else:
        result = {{
            "error": "CliAnythingBridge could not determine the active shader platform.",
            "code": "MATERIAL_SHADER_DUMP_BRIDGE_UNAVAILABLE",
        }}
'''


_PLUGIN_RECOMPILE_SHADER_DUMP_SCRIPT = r'''import unreal
import json

material_path = "{material_path}"
material_candidates = {material_path_candidates_json}
mat, loaded_asset_path, tried_asset_paths = _cli_load_material(material_path, material_candidates)
if mat is None:
    result = {{
        "error": "Material not found: " + material_path,
        "code": "MATERIAL_NOT_FOUND",
        "tried": tried_asset_paths,
    }}
else:
    bridge = getattr(unreal, "CliAnythingBridgeLibrary", None)
    if bridge is None:
        result = {{
            "error": "CliAnythingBridge is not loaded; package-safe shader recompilation is unavailable.",
            "code": "MATERIAL_SHADER_DUMP_BRIDGE_UNAVAILABLE",
        }}
    else:
        raw_result = str(bridge.recompile_material_shaders_for_dump(mat))
        try:
            result = json.loads(raw_result)
        except Exception:
            result = {{
                "error": "CliAnythingBridge returned an invalid shader-recompile result.",
                "code": "MATERIAL_SHADER_RECOMPILE_FAILED",
                "bridge_result": raw_result,
            }}
        result.setdefault("material", loaded_asset_path)
        result.setdefault("requested_asset_class", mat.get_class().get_name())
        shader_map_material = mat
        if isinstance(mat, unreal.MaterialInstanceConstant):
            try:
                base_material = mat.get_base_material()
                if base_material is not None:
                    shader_map_material = base_material
            except Exception:
                pass
        result.setdefault("shader_map_material", shader_map_material.get_path_name())
        result.setdefault("shader_dump_name", shader_map_material.get_name())
        result.setdefault("resolved_from_material_instance", shader_map_material != mat)
'''


def get_material_hlsl(
    api: UEEditorAPI,
    material_path: str,
    project_dir: str | None = None,
    platform: str = "sm6",
    shader_type: str = "pixel",
    wait_timeout: float = SHADER_DUMP_WAIT_TIMEOUT_SECONDS,
) -> dict:
    """Get the compiled HLSL/USF shader code for a material.

    First checks if a shader dump already exists in ShaderDebugInfo.
    If not, enables r.DumpShaderDebugInfo and triggers RecompileShaders
    to generate one. The CVar is saved and restored afterwards.

    Args:
        api: Connected UEEditorAPI instance.
        material_path: Content path (e.g., "/Game/TestVP/M_TestPP").
        project_dir: Project directory (to find ShaderDebugInfo).
        platform: Shader platform: "sm6" (default), "sm5", "vulkan", etc.
        shader_type: "pixel" (PS), "vertex" (VS), "all", or specific pass name.
        wait_timeout: Seconds to wait for dump files after recompilation returns.

    Returns:
        {
            "material": str,
            "platform": str,
            "shaders": [{"pass": str, "type": str, "file": str, "lines": int}, ...],
            "material_code": str,  # Extracted CalcPixelMaterialInputs section
        }
    """
    if not project_dir:
        return {"error": "project_dir required to locate ShaderDebugInfo"}

    # Resolve platform name
    platform_dir_name = SHADER_PLATFORMS.get(platform.lower(), platform)

    # Normalize material path
    if "." in material_path:
        mat_name = material_path.split(".")[-1]
    else:
        mat_name = material_path.rsplit("/", 1)[-1]
        material_path = f"{material_path}.{mat_name}"

    debug_base = Path(project_dir) / "Saved" / "ShaderDebugInfo" / platform_dir_name

    # ── Step 1: Check if dump already exists ───────────────────────
    dump_dir = _find_shader_dump_dir(debug_base, mat_name)
    if dump_dir and not any(dump_dir.rglob("*.usf")):
        dump_dir = None
    recompile_result = None

    # ── Step 2: If no dump, trigger one ────────────────────────────
    if not dump_dir:
        platform_result = _exec_material_script(
            api,
            _PLUGIN_GET_ACTIVE_SHADER_PLATFORM_SCRIPT,
            project_dir=project_dir,
            timeout=10.0,
            save_policy=SavePolicy.NEVER,
        )
        if "error" in platform_result:
            platform_result.setdefault("code", "MATERIAL_SHADER_DUMP_BRIDGE_UNAVAILABLE")
            return platform_result

        active_platform = str(platform_result.get("active_platform", ""))
        if active_platform.casefold() != platform_dir_name.casefold():
            return {
                "error": (
                    f"Requested shader platform '{platform_dir_name}' is not active; "
                    f"the running editor uses '{active_platform}'. No recompile was started."
                ),
                "code": "SHADER_PLATFORM_NOT_ACTIVE",
                "requested_platform": platform_dir_name,
                "active_platform": active_platform,
                "available_platforms": _available_shader_dump_platforms(project_dir),
            }

        # Save original CVar
        old_value = api.get_cvar("r.DumpShaderDebugInfo")

        try:
            api.set_cvar("r.DumpShaderDebugInfo", "1")
            time.sleep(0.5)

            recompile_result = _exec_material_script(
                api,
                _PLUGIN_RECOMPILE_SHADER_DUMP_SCRIPT,
                project_dir=project_dir,
                timeout=30.0,
                save_policy=SavePolicy.NEVER,
                material_path=material_path,
            )
            if "error" in recompile_result:
                recompile_result.setdefault("code", "MATERIAL_SHADER_RECOMPILE_FAILED")
                return recompile_result

            shader_dump_name = str(recompile_result.get("shader_dump_name") or mat_name)
            shader_debug_group = str(recompile_result.get("shader_debug_group") or "")
            exact_dump_dir_name = _shader_debug_group_root(shader_debug_group)

            # Shader compilation may finish after PostEditChange returns. Keep
            # that grace period short by default and let callers extend it for
            # unusually large materials instead of appearing hung for 2 min.
            wait_timeout = max(0.0, float(wait_timeout))
            deadline = time.monotonic() + wait_timeout
            while True:
                dump_dir = _find_shader_dump_dir(
                    debug_base,
                    shader_dump_name,
                    exact_dir_name=exact_dump_dir_name,
                )
                if dump_dir:
                    # Verify .usf files exist
                    usf_files = list(dump_dir.rglob("*.usf"))
                    if usf_files:
                        # Wait a bit more to ensure all files are written
                        time.sleep(2)
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))

        finally:
            # Restore CVar
            restore_val = str(old_value) if old_value and old_value != "0" else "0"
            api.set_cvar("r.DumpShaderDebugInfo", restore_val)

    dump_has_usf = bool(
        dump_dir
        and dump_dir.exists()
        and any(dump_dir.rglob("*.usf"))
    )
    if not dump_has_usf:
        shader_dump_name = (
            str(recompile_result.get("shader_dump_name") or mat_name)
            if recompile_result
            else mat_name
        )
        shader_debug_group = (
            str(recompile_result.get("shader_debug_group") or "")
            if recompile_result
            else ""
        )
        exact_dump_dir_name = _shader_debug_group_root(shader_debug_group)
        search_details = _shader_dump_search_details(debug_base, shader_dump_name)
        return {
            "error": f"No shader dump found for '{mat_name}' on platform '{platform_dir_name}'. "
                     f"Waited {wait_timeout:g} seconds after recompilation. "
                     "Increase --wait-timeout only if shader compilation is still active.",
            "code": "SHADER_DUMP_NOT_FOUND",
            "available_platforms": _available_shader_dump_platforms(project_dir),
            "recompile": recompile_result,
            "wait_timeout_seconds": wait_timeout,
            "shader_map_material": (
                recompile_result.get("shader_map_material") if recompile_result else material_path
            ),
            "shader_dump_name": shader_dump_name,
            "shader_debug_group": shader_debug_group or None,
            "exact_dump_directory": exact_dump_dir_name or None,
            **search_details,
        }

    # ── Step 3: Read shader files ──────────────────────────────────
    result = _read_shader_dump(
        dump_dir,
        mat_name,
        material_path,
        platform_dir_name,
        shader_type,
        project_dir,
    )
    if recompile_result:
        result["shader_map_material"] = recompile_result.get(
            "shader_map_material",
            material_path,
        )
        result["shader_dump_name"] = recompile_result.get("shader_dump_name", mat_name)
        result["shader_debug_group"] = recompile_result.get("shader_debug_group") or None
        result["resolved_from_material_instance"] = bool(
            recompile_result.get("resolved_from_material_instance", False)
        )
    return result


def _shader_debug_group_root(shader_debug_group: str) -> str:
    """Return ShaderDebugInfo's top-level directory from an engine debug group."""
    normalized = str(shader_debug_group or "").replace("\\", "/").strip("/")
    return normalized.split("/", 1)[0] if normalized else ""


def _find_shader_dump_dir(
    debug_base: Path,
    mat_name: str,
    *,
    exact_dir_name: str = "",
) -> Optional[Path]:
    """Find the shader dump directory for a material.

    Dump dirs are named like: MaterialName_hexhash

    Returns:
        Path to dump directory, or None.
    """
    if not debug_base.is_dir():
        return None
    if exact_dir_name:
        exact_dir = debug_base / exact_dir_name
        return exact_dir if exact_dir.is_dir() else None
    for d in debug_base.iterdir():
        if d.is_dir() and (d.name == mat_name or d.name.startswith(f"{mat_name}_")):
            return d
    return None


def _shader_dump_search_details(debug_base: Path, mat_name: str) -> dict:
    """Return bounded diagnostics for a failed material dump lookup."""
    details = {
        "search_root": str(debug_base),
        "searched_directory_count": 0,
        "candidate_count": 0,
        "candidate_directories": [],
    }
    try:
        if not debug_base.is_dir():
            return details
        directories = [path for path in debug_base.iterdir() if path.is_dir()]
    except OSError as exc:
        details["search_error"] = str(exc)
        return details

    candidates = sorted(
        path.name
        for path in directories
        if path.name == mat_name or path.name.startswith(f"{mat_name}_")
    )
    details["searched_directory_count"] = len(directories)
    details["candidate_count"] = len(candidates)
    details["candidate_directories"] = candidates[:20]
    return details


def _available_shader_dump_platforms(project_dir: str) -> list[str]:
    shader_debug_root = Path(project_dir) / "Saved" / "ShaderDebugInfo"
    if not shader_debug_root.is_dir():
        return []
    return sorted(d.name for d in shader_debug_root.iterdir() if d.is_dir())


def _read_shader_dump(
    dump_dir: Path,
    mat_name: str,
    material_path: str,
    platform_dir_name: str,
    shader_type: str,
    project_dir: str,
) -> dict:
    """Read shader dump files and extract material code."""

    shaders = []
    type_filter = shader_type.lower()

    for usf_file in sorted(dump_dir.rglob("*.usf")):
        rel = usf_file.relative_to(dump_dir)
        parts = list(rel.parts)

        # Directory structure: Default/VertexFactory/ShaderType/hash/file.usf
        # ShaderType is the one that contains PS/VS (e.g. TBasePassPSFNoLightMapPolicy)
        shader_class = ""
        for p in reversed(parts):
            if "PS" in p or "VS" in p or "GS" in p or "CS" in p:
                shader_class = p
                break
        if not shader_class and len(parts) >= 3:
            shader_class = parts[-3] if len(parts) >= 4 else parts[-2]

        is_ps = "PS" in shader_class and "VS" not in shader_class
        is_vs = "VS" in shader_class and "PS" not in shader_class

        if type_filter == "pixel" and not is_ps:
            continue
        elif type_filter == "vertex" and not is_vs:
            continue

        shaders.append({
            "pass": shader_class,
            "type": "PS" if is_ps else ("VS" if is_vs else "Other"),
            "file": str(usf_file),
            "lines": sum(1 for _ in open(usf_file, encoding="utf-8", errors="replace")),
        })

    # Extract material-specific code from best PS shader
    # Prefer BasePass PS (has the full material code), then PostProcess, then any PS
    material_code = ""
    first_ps = None
    for priority in ["TBasePassPS", "FPostProcessMaterial", "PS"]:
        for s in shaders:
            if s["type"] == "PS" and priority in s["pass"]:
                first_ps = s
                break
        if first_ps:
            break
    if not first_ps:
        first_ps = next((s for s in shaders if s["type"] == "PS"), None)
    if not first_ps and shaders:
        first_ps = shaders[0]

    if first_ps:
        full_code = Path(first_ps["file"]).read_text(encoding="utf-8", errors="replace")
        first_ps["code"] = full_code
        material_code = _extract_material_code(full_code)

    # Available platforms
    available_platforms = []
    shader_debug_root = Path(project_dir) / "Saved" / "ShaderDebugInfo"
    if shader_debug_root.is_dir():
        available_platforms = [d.name for d in shader_debug_root.iterdir() if d.is_dir()]

    return {
        "material": material_path,
        "platform": platform_dir_name,
        "available_platforms": available_platforms,
        "dump_dir": str(dump_dir),
        "shader_count": len(shaders),
        "shaders": shaders,
        "material_code": material_code,
    }


def _extract_material_code(hlsl_code: str) -> str:
    """Extract the material-graph-generated section from full HLSL code.

    Looks for CalcPixelMaterialInputs() which contains the compiled
    material node graph.

    Args:
        hlsl_code: Full .usf file content.

    Returns:
        Extracted material code section, or empty string.
    """
    lines = hlsl_code.split("\n")

    # Find CalcPixelMaterialInputs or CalcMaterialParameters
    start_idx = -1
    for i, line in enumerate(lines):
        if "void CalcPixelMaterialInputs" in line or "void CalcMaterialParameters" in line:
            start_idx = i
            break

    if start_idx < 0:
        return ""

    # Find the matching closing brace
    brace_depth = 0
    end_idx = start_idx
    for i in range(start_idx, len(lines)):
        brace_depth += lines[i].count("{") - lines[i].count("}")
        if brace_depth == 0 and i > start_idx:
            end_idx = i
            break

    if end_idx <= start_idx:
        # Fallback: take 200 lines from start
        end_idx = min(start_idx + 200, len(lines) - 1)

    return "\n".join(lines[start_idx:end_idx + 1])


# ── Internal helpers ──────────────────────────────────────────────────

def _exec_material_script(
    api: UEEditorAPI,
    script_template: str,
    project_dir: str | None = None,
    timeout: float = 30.0,
    save: bool | None = None,
    save_policy: SavePolicy | str | None = None,
    target_packages: list[str] | None = None,
    **kwargs,
) -> dict:
    """Execute a material query Python script in the editor and read results.

    Formats *script_template* with **kwargs, then executes via
    ``script_runner.run_python_code`` (which uses
    ``ExecutePythonCommandEx`` under the hood).

    Args:
        api: Connected UEEditorAPI instance.
        script_template: Python script template with {placeholders}.
        project_dir: Unused — kept for backwards compatibility.
        timeout: HTTP request timeout in seconds.
        save: Backwards-compatible boolean save flag.
        save_policy: Explicit package persistence policy. Material operations
            default to read-only; mutation callers must name target packages.
        target_packages: Packages owned by a targeted mutation.
        **kwargs: Template variables.

    Returns:
        Parsed JSON result from the script.
    """
    from cli_anything.unreal.core.script_runner import run_python_code

    material_path = kwargs.get("material_path")
    if isinstance(material_path, str) and material_path.startswith("/"):
        kwargs.setdefault("material_path_json", json.dumps(material_path, ensure_ascii=False))
        kwargs.setdefault(
            "material_path_candidates_json",
            json.dumps(_material_asset_path_candidates(material_path), ensure_ascii=False),
        )
    script_content = _MATERIAL_RESOLVER + "\n" + script_template.format(**kwargs)
    if save_policy is None and save is None:
        save_policy = SavePolicy.NEVER
    return run_python_code(
        api,
        script_content,
        timeout=timeout,
        save=save,
        save_policy=save_policy,
        target_packages=target_packages,
    )
