# Material Editing & Shader Workflows

## AI Progressive Material Editing Workflow

Edit materials progressively to avoid context overflow. Prefer HLSL Custom nodes when useful.

### 1. Locate or Create

```bash
ue-cli material list --path /Game/Materials/
# Or create via Python script:
ue-cli editor run-script create_mat.py
```

### 2. Inspect Properties & Topology

```bash
# Full info: nodes, parameters, textures, connections, Custom node code
ue-cli material info /Game/M_Water

# Lightweight connection graph with orphan detection
ue-cli material get-graph /Game/M_Water
```

### 3. HLSL Custom Node (Preferred Approach)

Visual graph is awkward for AI. HLSL in Custom node is often simpler/safer:

```bash
# Add a Custom node with HLSL from a file
ue-cli material add-node /Game/M_Water \
    --type MaterialExpressionCustom --code-file my_shader.hlsl

# Connect to an output pin
ue-cli material connect /Game/M_Water \
    --from MaterialExpressionCustom_0 --to __material_output__ --to-input BaseColor

# Recompile -> {"status":"ok"} or {"status":"error","compile_errors":[...]}
ue-cli material recompile /Game/M_Water
```

Update HLSL on existing Custom node via `editor run-script`:
```python
import unreal
mat = unreal.load_asset("/Game/M_Water")
node = unreal.MaterialEditingLibrary.get_material_property_input_node(
    mat, unreal.MaterialProperty.MP_BASE_COLOR)
if node:
    with open("my_shader.hlsl") as f:
        node.set_editor_property("code", f.read())
```

Then recompile:
```bash
ue-cli material recompile /Game/M_Water
```

Custom node HLSL variable names come from the node's input list, not from
MaterialInstance parameter names or UI display text. Rename them with:

```bash
ue-cli material rename-custom-input /Game/M_Water \
    --node MaterialExpressionCustom_0 --from OutlineWidth --to OutlineWidthPx
```

Custom node input display labels are not stable through UE Python across all
engine versions. Treat `inputs[].input_name` and the Custom HLSL code as the
source of truth. MaterialInstance parameters are separate; artists can still see
and tune MI names such as `OutlineWidthPx` even if a Custom node display label
does not persist.

### 4. Alternative: Standard Nodes

For simple changes:

```bash
# Add a Constant3Vector node
ue-cli material add-node /Game/M_Water \
    --type MaterialExpressionConstant3Vector --pos-x -200 --pos-y 0

# Connect to BaseColor
ue-cli material connect /Game/M_Water \
    --from Constant3Vector_0 --to __material_output__ --to-input BaseColor

# Set a scalar parameter on a MaterialInstance
ue-cli material set-param /Game/MI_Water \
    --name Roughness --value 0.5 --type scalar

# Recompile
ue-cli material recompile /Game/M_Water
```

`set-param` verifies the effective scalar, vector, or texture value after the write. Trust
`applied` / `readback_match`; Unreal's raw `set_return` remains diagnostic-only and can be
false after a successful write. A mismatch returns `MATERIAL_PARAM_READBACK_MISMATCH` and
does not save the asset.

**Connect to material output:** `--to __material_output__` + `--to-input` material property: `BaseColor`, `Metallic`, `Roughness`, `Normal`, `Emissive`, `Opacity`, `WorldPositionOffset`, etc.

### 5. Setting Node Properties

Always `editor api-discover` node type before setting props:

```bash
# Step 1: Discover what properties the node type has
ue-cli editor api-discover unreal.MaterialExpressionConstant3Vector
# -> Shows: constant (LinearColor), desc (str), etc.

# Step 2: Use editor run-script with the correct type
```

**Simple properties (int, float, str, enum)** on new nodes can use `add-node --set`:
```bash
ue-cli material add-node /Game/M \
    --type MaterialExpressionPanner --set speed_x=0.15
```
Python-style `snake_case` property names resolve to native reflected fields. If any requested
property cannot be applied, `add-node` returns `MATERIAL_NODE_PROPERTIES_UNAPPLIED`, removes
the partial node, and does not save the material.

**Any property on existing node**: use `editor run-script` + `get_material_property_input_node`:
```python
import unreal
mat = unreal.load_asset("/Game/M_Water")
node = unreal.MaterialEditingLibrary.get_material_property_input_node(
    mat, unreal.MaterialProperty.MP_BASE_COLOR)
if node:
    node.set_editor_property("constant", unreal.LinearColor(0.9, 0.3, 0.1, 1.0))
```

**Complex properties at creation**: create + set in one script:
```python
import unreal
mat = unreal.load_asset("/Game/M_Water")
expr = unreal.MaterialEditingLibrary.create_material_expression(
    mat, unreal.MaterialExpressionConstant3Vector, -200, 0)
expr.set_editor_property("constant", unreal.LinearColor(0.2, 0.5, 0.8, 1.0))
unreal.MaterialEditingLibrary.connect_material_property(
    expr, "", unreal.MaterialProperty.MP_BASE_COLOR)
```

**Progressive Node Discovery:** 400+ `MaterialExpression` classes; do targeted search:
```python
import unreal
# Search for nodes related to "Noise"
print([cls for cls in dir(unreal) if "Noise" in cls and cls.startswith("MaterialExpression")])
```
Then `editor api-discover unreal.MaterialExpressionNoise`.

## HLSL / Shader Source Discovery

For Custom HLSL, discover available cbuffers/structs (`cbuffer View`, `FPrimitiveConstants`, `FMaterialPixelParameters`). They vary by engine/material.

**Two complementary commands:**

### `material hlsl-code` - Lightweight, No Recompile

Returns `/Engine/Generated/Material.ush` with `FMaterialPixelParameters` + material structs. No `cbuffer View`.
Supports Material and MaterialInstanceConstant assets. MaterialFunction assets return `MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET`.
Requires Bridge 1.40. Both source exporters preserve effective static parameters,
including inherited instance overrides. UE5 follows active editor preview; UE4
reports `platform_source=host_rhi`. Check `checked_platform` in the response.
`hlsl-code` uses the current-quality interface resource and fails if unavailable;
`shader-source` extracts at High quality. Neither silently substitutes base defaults.

```bash
ue-cli material hlsl-code /Game/M_Custom
# -> {"source": "plugin", "lines": 5250, "file": "F:/Project/Saved/CliAnything/M_Custom.ush"}
```

### `material shader-source` - Full Source with cbuffers

Refreshes changed engine shader files, triggers a synchronous recompile, then returns compiled `.usf` files with cbuffers/structs including `cbuffer View`, `FPrimitiveConstants`. Success includes `"shader_cache_refresh": "changed"`; a refresh or extraction that yields no shaders fails instead of returning stale validation evidence.

```bash
ue-cli material shader-source /Game/M_Custom
# -> {"source": "plugin", "shader_count": 12, "shaders": [...], "output_dir": "..."}
```

### Typical Shader Development Workflow

```bash
# 1. Get lightweight HLSL context (fast)
ue-cli material hlsl-code /Game/M_Custom

# 2. Get full shader source with cbuffers (triggers recompile)
ue-cli material shader-source /Game/M_Custom

# 3. Read shader files to discover available resources (cbuffer View members, etc.)

# 4. Write Custom HLSL code using discovered resources

# 5. Recompile and verify
ue-cli material recompile /Game/M_Custom
```

## Actor -> Material -> Shader Investigation

Trace visible actor to shader:

```bash
ue-cli scene list -q "MyActor"
ue-cli scene get-material "<actor_path>"
ue-cli material info /Game/SomeMaterial
ue-cli material hlsl-code /Game/SomeMaterial
```

## Diagnostics

```bash
ue-cli material get-errors /Game/M_Water
ue-cli material analyze /Game/M_Water
```

Full command reference: `commands.md` -> `material`.

## Material-Specific Notes

- **`MEL.recompile_material()` is void** - cannot detect compile failures. Verify with `material recompile` or `material get-errors`. No exception != success.
- `Material.expressions` protected in UE5.7+. Read nodes with `material info`; edit via `add-node`, `connect`, `delete-node`.
- Custom node internal HLSL variable names are `inputs[].input_name`; use `material rename-custom-input` for persistent HLSL variable renames. Do not rely on display-label-only edits.
- Existing node prop edits: use `MaterialEditingLibrary.get_material_property_input_node()`; avoid `find_object` and direct `expressions`.
- `material shader-source` first runs the engine's changed-shader refresh, then sync recompiles the extraction resource to guarantee current `.usf`/`.ush` source; complex materials may take time.
