"""commands/material.py — Material viewing, editing & analysis commands."""

import re

import click

from cli_anything.unreal.commands import AppError, AppState, handle_error, output, require_editor, require_project
from cli_anything.unreal.errors import raise_for_legacy_error


def _validate_custom_code(code: str) -> list[str]:
    """Validate HLSL code for UE5 Custom node and return warning messages."""
    warnings = []

    # Detect #define macros — UE substitutes input param names inside macros
    define_matches = re.findall(r'#define\s+\w+\([^)]*\)', code)
    if define_matches:
        warnings.append(
            f"#define macros with parameters detected ({len(define_matches)} found). "
            "UE5 Custom node substitutes input param names by text, which breaks macros. "
            "Use inline code instead of macros."
        )

    # Detect GCC statement expressions ({ ... })
    if re.search(r'\(\s*\{', code):
        warnings.append(
            "GCC statement expressions ({ ... }) detected. "
            "These are NOT valid HLSL. Use inline statements only."
        )

    # Detect function definitions inside code
    func_matches = re.findall(r'\b(?:float|void|int|bool|float[234]|int[234])\s+\w+\s*\(', code)
    # Filter out UE built-in intrinsics and common HLSL functions
    intrinsics = {'lerp', 'clamp', 'saturate', 'frac', 'sin', 'cos', 'tan', 'atan',
                  'atan2', 'pow', 'sqrt', 'abs', 'min', 'max', 'dot', 'cross', 'normalize',
                  'length', 'distance', 'reflect', 'refract', 'mul', 'transpose', 'determinant',
                  'tex2D', 'tex2Dlod', 'tex2Dgrad', 'Sample', 'SampleLevel', 'Load', 'Gather'}
    user_funcs = [f for f in func_matches
                  if not any(f.startswith(t) for t in [f'{i}(' for i in intrinsics])]
    if user_funcs:
        warnings.append(
            f"Function definitions detected ({len(user_funcs)} found). "
            "UE5 Custom node code is already inside a function body — "
            "nested function definitions are not allowed in HLSL. "
            "Inline all logic as sequential statements."
        )

    return warnings


@click.group("material")
def material_group():
    """Material viewing, editing & analysis (add/connect/delete nodes, set params, recompile)."""
    pass


@material_group.command("list")
@click.option("--path", "content_path", default="/Game/", help="Content path to search")
@handle_error
@click.pass_obj
def material_list(state: AppState, content_path):
    """List all materials in the project."""
    from cli_anything.unreal.core.materials import list_materials

    api = require_editor(state)
    result = list_materials(api, content_path, state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_LIST_FAILED")
    output(result, state)


@material_group.command("info")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_info(state: AppState, material_path):
    """Show detailed material information (nodes, parameters, textures)."""
    from cli_anything.unreal.core.materials import get_material_info

    api = require_editor(state)
    result = get_material_info(api, material_path, state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_INFO_FAILED")
    output(result, state)


@material_group.command("get-stats")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_stats(state: AppState, material_path):
    """Show material compilation statistics (instruction counts)."""
    from cli_anything.unreal.core.materials import get_material_stats

    api = require_editor(state)
    result = get_material_stats(api, material_path, state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_STATS_FAILED")
    output(result, state)


@material_group.command("get-errors")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_errors(state: AppState, material_path):
    """Read base-material errors and compile readiness (active UE5 preview; UE4 host RHI)."""
    from cli_anything.unreal.core.materials import get_material_errors

    api = require_editor(state)
    result = get_material_errors(api, material_path, state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_ERRORS_FAILED")
    output(result, state)


@material_group.command("list-textures")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_textures(state: AppState, material_path):
    """List all textures referenced by a material."""
    from cli_anything.unreal.core.materials import get_material_texture_list

    api = require_editor(state)
    result = get_material_texture_list(api, material_path, state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_TEXTURE_LIST_FAILED")
    output(result, state)


@material_group.command("get-graph")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_graph(state: AppState, material_path):
    """Show material node topology as a Mermaid graph.

    Produces a visually readable graph mapping which node feeds each
    material output pin (BaseColor, Normal, WorldPositionOffset, etc.)
    and standard internal node connections.

    Example: material get-graph /Game/M_Water
    """
    from cli_anything.unreal.core.materials import get_material_connections

    api = require_editor(state)
    result = get_material_connections(api, material_path, state.session.project_dir)
    output(result, state)


@material_group.command("analyze")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_analyze(state: AppState, material_path):
    """Analyze material for common issues (Agent core feature)."""
    from cli_anything.unreal.core.materials import analyze_material

    api = require_editor(state)
    result = analyze_material(api, material_path, state.session.project_dir)
    output(result, state)


@material_group.command("dump-hlsl")
@click.argument("material_path")
@click.option("--output", "output_path", required=True,
              help="File path to write the HLSL code to. Required because output is massive.")
@click.option("--platform", default="sm6",
              help=(
                  "Shader platform: sm6 (default), sm5, vulkan, vulkan_es31, "
                  "vulkan_sm5_android_preview, vulkan_es31_android_preview, "
                  "opengl_es31, metal"
              ))
@click.option("--shader-type", default="pixel",
              type=click.Choice(["pixel", "vertex", "all"]),
              help="Which shader stage to return")
@click.option(
    "--wait-timeout",
    default=10.0,
    show_default=True,
    type=click.FloatRange(min=0.0),
    help="Seconds to wait for shader dump files after recompilation returns.",
)
@click.option("--full", is_flag=True, help="Return full .usf file (not just material code)")
@handle_error
@click.pass_obj
def material_hlsl(
    state: AppState,
    material_path,
    output_path,
    platform,
    shader_type,
    wait_timeout,
    full,
):
    """Get compiled HLSL/USF shader code for a material.

    Triggers shader recompile with debug dump, reads the generated code.
    CVar r.DumpShaderDebugInfo is automatically saved and restored.

    Platforms include sm6, sm5, vulkan, Android Vulkan preview, opengl_es31,
    and metal.
    """
    from cli_anything.unreal.core.materials import get_material_hlsl
    import os

    api = require_editor(state)
    require_project(state)

    result = get_material_hlsl(
        api, material_path,
        project_dir=state.session.project_dir,
        platform=platform,
        shader_type=shader_type,
        wait_timeout=wait_timeout,
    )

    if "error" in result:
        details = {
            key: value
            for key, value in result.items()
            if key not in {"code", "error"}
        }
        code = result.get("code", "MATERIAL_HLSL_DUMP_FAILED")
        suggestion = (
            "Request the running editor's active shader platform or relaunch it with the required RHI."
            if code == "SHADER_PLATFORM_NOT_ACTIVE"
            else "Check shader compilation and available_platforms, then retry."
        )
        if code == "SHADER_DUMP_NOT_FOUND":
            suggestion = (
                "Inspect details.search_root; increase --wait-timeout only while shader compilation is active."
            )
        if code == "MATERIAL_SHADER_DUMP_BRIDGE_UNAVAILABLE":
            suggestion = "Run editor plugin-upgrade, then retry after the editor restarts."
        raise AppError(
            code,
            str(result["error"]),
            exit_code=3,
            suggestion=suggestion,
            details=details or None,
        )

    content = ""
    if full and result.get("shaders"):
        first = result["shaders"][0]
        content = (
            f"// Full shader: {first.get('pass', 'Unknown')}\n\n"
            + first.get("code", "")
        )
    else:
        content = result.get("material_code", "")
        if not content and result.get("shaders"):
            content = result["shaders"][0].get("code", "")

    if not content:
        raise AppError(
            "SHADER_STAGE_NOT_FOUND",
            f"No {shader_type} shader source was found in the material dump.",
            exit_code=3,
            suggestion="Retry with --shader-type all or inspect available shader passes.",
            details={
                "material": result.get("material", material_path),
                "platform": result.get("platform"),
                "shader_count": result.get("shader_count", 0),
            },
        )

    try:
        out_path = os.path.abspath(output_path)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        raise AppError(
            "SHADER_DUMP_WRITE_FAILED",
            f"Failed to write output file: {e}",
            exit_code=3,
            details={"output": os.path.abspath(output_path)},
        ) from e

    result["file"] = out_path
    result["lines"] = len(content.splitlines())
    result.pop("material_code", None)
    for shader in result.get("shaders", []):
        shader.pop("code", None)

    output(result, state)


@material_group.command("add-node")
@click.argument("material_path")
@click.option("--type", "expression_class", required=True,
              help="UE expression class (e.g., MaterialExpressionConstant3Vector)")
@click.option("--pos-x", default=0, type=int, help="Node X position in graph")
@click.option("--pos-y", default=0, type=int, help="Node Y position in graph")
@click.option("--set", "set_props", multiple=True, metavar="KEY=VALUE",
              help="Set a property on the node (e.g., --set code='return 1' --set output_type=CMOT_Float4)")
@click.option("--add-input", "add_inputs", multiple=True, metavar="NAME",
              help="Add an input to a Custom node (e.g., --add-input UV --add-input Time)")
@click.option("--code-file", "code_file", default=None,
              help="Read HLSL code from a file and set as 'code' property (for Custom nodes). "
                   "Much easier than --set code='...' for multi-line shaders.")
@handle_error
@click.pass_obj
def material_add_node(state: AppState, material_path, expression_class, pos_x, pos_y,
                      set_props, add_inputs, code_file):
    """Add a new material expression node.

    Requires MaterialEditingLibrary (Python Editor Scripting plugin).

    Use --set KEY=VALUE to set properties on the node after creation.
    Use --add-input NAME to add inputs to Custom nodes.
    Use --code-file PATH to read HLSL code from a file (for Custom nodes,
    much easier than --set code='...' for multi-line shaders).

    Example: material add-node /Game/M_Test --type MaterialExpressionCustom \
      --code-file F:/shaders/blackhole.hlsl --set output_type=CMOT_Float4 \
      --add-input UV --add-input Time
    """
    import os

    from cli_anything.unreal.core.materials import add_material_node

    # Parse --set KEY=VALUE pairs
    parsed_props = []
    for kv in set_props:
        if '=' not in kv:
            click.echo(f"Error: --set requires KEY=VALUE format, got: {kv}", err=True)
            return
        key, _, value = kv.partition('=')
        parsed_props.append((key.strip(), value.strip()))

    # Read --code-file and inject as code property
    if code_file:
        if expression_class != "MaterialExpressionCustom":
            click.echo("Error: --code-file is only for MaterialExpressionCustom nodes", err=True)
            return
        abs_path = os.path.abspath(code_file)
        if not os.path.isfile(abs_path):
            click.echo(f"Error: code file not found: {abs_path}", err=True)
            return
        with open(abs_path, "r", encoding="utf-8") as f:
            code_content = f.read()
        # Remove any existing code= from --set to avoid conflict
        parsed_props = [(k, v) for k, v in parsed_props if k != "code"]
        parsed_props.append(("code", code_content))

    # Custom node code validation
    if expression_class == "MaterialExpressionCustom":
        for key, value in parsed_props:
            if key == "code":
                warnings = _validate_custom_code(value)
                if warnings:
                    click.echo("⚠ Custom node code validation warnings:", err=True)
                    for w in warnings:
                        click.echo(f"  • {w}", err=True)

    api = require_editor(state)
    result = add_material_node(api, material_path, expression_class,
                               pos_x=pos_x, pos_y=pos_y,
                               set_props=parsed_props,
                               add_input_names=list(add_inputs),
                               project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_ADD_NODE_FAILED")
    output(result, state)


@material_group.command("delete-node")
@click.argument("material_path")
@click.option("--node", "node_name", required=True,
              help="Name of the expression node to delete")
@handle_error
@click.pass_obj
def material_delete_node(state: AppState, material_path, node_name):
    """Delete a material expression node by name.

    Use 'material info' to find node names first.

    Example: material delete-node /Game/M_Test --node Constant3Vector_0
    """
    from cli_anything.unreal.core.materials import delete_material_node

    api = require_editor(state)
    result = delete_material_node(api, material_path, node_name,
                                  project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_DELETE_NODE_FAILED")
    output(result, state)


@material_group.command("rename-custom-input")
@click.argument("material_path")
@click.option("--node", "node_name", required=True,
              help="MaterialExpressionCustom node name")
@click.option("--from", "old_name", required=True,
              help="Existing Custom input/HLSL variable name")
@click.option("--to", "new_name", required=True,
              help="New Custom input/HLSL variable name")
@click.option("--no-update-code", is_flag=True,
              help="Rename the input only; do not rewrite HLSL references")
@handle_error
@click.pass_obj
def material_rename_custom_input(state: AppState, material_path, node_name,
                                 old_name, new_name, no_update_code):
    """Rename a Custom node input and its HLSL variable references.

    Example: material rename-custom-input /Game/M_Test --node Custom_0 \
      --from OutlineWidth --to OutlineWidthPx
    """
    from cli_anything.unreal.core.materials import rename_custom_input

    api = require_editor(state)
    result = rename_custom_input(
        api,
        material_path,
        node_name,
        old_name,
        new_name,
        update_code=not no_update_code,
        project_dir=state.session.project_dir,
    )
    raise_for_legacy_error(result, default_code="MATERIAL_RENAME_CUSTOM_INPUT_FAILED")
    output(result, state)


@material_group.command("connect")
@click.argument("material_path")
@click.option("--from", "from_node", required=True, help="Source node name")
@click.option("--from-output", default="", help="Source output pin name (empty for single-output)")
@click.option("--to", "to_node", required=True,
              help="Target node name, or '__material_output__' for material output pins")
@click.option("--to-input", required=True,
              help="Target input pin name, or material property (BaseColor, Normal, etc.)")
@handle_error
@click.pass_obj
def material_connect(state: AppState, material_path, from_node, from_output, to_node, to_input):
    """Connect two material expression nodes.

    To connect to material output (BaseColor, Normal, etc.):
      --to __material_output__ --to-input BaseColor

    Example: material connect /Game/M_Test --from Constant3Vector_0 --to __material_output__ --to-input BaseColor
    """
    from cli_anything.unreal.core.materials import connect_material_nodes

    api = require_editor(state)
    result = connect_material_nodes(api, material_path,
                                    from_node, from_output, to_node, to_input,
                                    project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_CONNECT_FAILED")
    output(result, state)


@material_group.command("disconnect")
@click.argument("material_path")
@click.option("--from", "from_node", required=True, help="Source node name")
@click.option("--from-output", default="", help="Source output pin name")
@click.option("--to", "to_node", required=True,
              help="Target node name, or '__material_output__'")
@click.option("--to-input", required=True,
              help="Target input pin name, or material property name")
@handle_error
@click.pass_obj
def material_disconnect(state: AppState, material_path, from_node, from_output, to_node, to_input):
    """Disconnect material expression nodes.

    Example: material disconnect /Game/M_Test --from Constant3Vector_0 --to __material_output__ --to-input BaseColor
    """
    from cli_anything.unreal.core.materials import disconnect_material_nodes

    api = require_editor(state)
    result = disconnect_material_nodes(api, material_path,
                                       from_node, from_output, to_node, to_input,
                                       project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_DISCONNECT_FAILED")
    output(result, state)


@material_group.command("get-param")
@click.argument("material_path")
@click.option("--name", "param_name", required=True, help="Parameter name")
@handle_error
@click.pass_obj
def material_get_param(state: AppState, material_path, param_name):
    """Get an effective parameter on a MaterialInstanceConstant.

    Values inherited from parent instances or materials are included.

    Example: material get-param /Game/MI_Test --name Roughness
    """
    from cli_anything.unreal.core.materials import get_material_param

    api = require_editor(state)
    result = get_material_param(api, material_path, param_name, project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_PARAM_FAILED")
    output(result, state)


@material_group.command("set-param")
@click.argument("material_path")
@click.option("--name", "param_name", required=True, help="Parameter name")
@click.option("--value", "param_value", required=True,
              help='Value: scalar "0.5", vector \'{"r":1,"g":0,"b":0,"a":1}\', texture "/Game/T_Tex"')
@click.option("--type", "param_type", required=True,
              type=click.Choice(["scalar", "vector", "texture"]),
              help="Parameter type")
@handle_error
@click.pass_obj
def material_set_param(state: AppState, material_path, param_name, param_value, param_type):
    """Set a parameter on a MaterialInstanceConstant.

    Example: material set-param /Game/MI_Test --name Roughness --value 0.5 --type scalar
    """
    from cli_anything.unreal.core.materials import set_material_param

    api = require_editor(state)
    result = set_material_param(api, material_path,
                                param_name, param_value, param_type,
                                project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_PARAM_FAILED")
    output(result, state)


@material_group.command("recompile")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_recompile(state: AppState, material_path):
    """Recompile a material (force shader recompilation).

    Example: material recompile /Game/M_Test
    """
    from cli_anything.unreal.core.materials import recompile_material

    api = require_editor(state)
    result = recompile_material(api, material_path,
                                project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_RECOMPILE_FAILED")
    output(result, state)


@material_group.command("hlsl-code")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_hlsl_code(state: AppState, material_path):
    """Get Material or MaterialInstance HLSL expression source (Material.ush).

    Returns the translated HLSL from FMaterialResource::GetMaterialExpressionSource().
    Contains FMaterialPixelParameters struct and all Custom node code.
    Does NOT contain cbuffer View or Primitive definitions (use shader-source for that).
    MaterialFunction assets return MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET.

    Output: <project>/Saved/CliAnything/<MaterialName>.ush

    Example: material hlsl-code /Game/M_Test
    """
    from cli_anything.unreal.core.materials import get_material_hlsl_code

    api = require_editor(state)
    require_project(state)

    result = get_material_hlsl_code(api, material_path,
                                     project_dir=state.session.project_dir)
    raise_for_legacy_error(result, default_code="MATERIAL_HLSL_CODE_FAILED")
    output(result, state)


@material_group.command("shader-source")
@click.argument("material_path")
@handle_error
@click.pass_obj
def material_shader_source(state: AppState, material_path):
    """Get compiled shader source code (.usf files) for a material.

    Forces a synchronous recompile with source extraction enabled, then writes
    each compiled shader variant (BasePassPS, BasePassVS, LumenCardPS, etc.)
    as a .usf file. These files contain the complete preprocessed shader
    including cbuffer View, FPrimitiveConstants, FMaterialPixelParameters.

    Use this to discover what HLSL resources are available when writing
    Custom node code for this material's configuration.

    Output: <project>/Saved/CliAnything/<MaterialName>_shaders/

    Example: material shader-source /Game/M_Test
    """
    from cli_anything.unreal.core.materials import get_material_shader_source

    api = require_editor(state)
    require_project(state)

    result = get_material_shader_source(api, material_path,
                                         project_dir=state.session.project_dir)
    if "error" in result:
        details = {key: value for key, value in result.items() if key != "error"}
        raise AppError(
            "MATERIAL_SHADER_SOURCE_FAILED",
            str(result["error"]),
            exit_code=3,
            suggestion="Check material compile errors and the editor shader compiler log, then retry.",
            details=details or None,
        )
    output(result, state)
