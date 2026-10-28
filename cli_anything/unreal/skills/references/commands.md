# CLI Command Reference

Complete `ue-cli` command reference. Non-TTY output defaults JSON; terminal can force with `--output json`.

Workflow examples live in sibling workflow docs.

## editor - Editor Control

| Command | Description | Requires Editor |
|---------|-------------|:-:|
| `editor status [--project PATH] [--all] [--scan-range START-END] [TASK_ID]` | No TASK_ID: list Unreal Editor processes/result array. `--project` is accepted here or as top-level `ue-cli --project PATH editor status`; it filters to that project unless `--all` is set. Online entries include bridge plugin version fields; mismatch entries include `no_restart_command` for Remote Control Python validation and `upgrade_command` for bridge-backed features when project is known. Offline entries include recovery hints. With TASK_ID: async progress | - |
| `preflight` / `editor preflight` | Read-only editor-startup check for engine, project, Remote Control, and bridge readiness. It never modifies project files and is not required before `build cook` or `build package` | No |
| `editor launch [--map /Game/PATH] [--no-wait] [--timeout N] [--unattended/--no-unattended] [--skip-restore] [--no-remote]` | Prepare Remote Control/bridge project files when needed, then launch editor; waits up to 30 seconds for readiness and kills zombies. `editor start` is an exact alias with the same options. Launches an interactive editor by default; `--unattended` suppresses all UE dialogs, while explicit `--skip-restore` declines only startup package recovery through CliAnythingBridge without UI clicking. `--skip-restore` discards that recovery opportunity and requires controlled launch. Explicit `--no-remote` skips automation setup and reports only bounded process-start verification (`launched`, never `online` or map-verified). Map package paths must include their mount root; bare names such as `Oregon_Main` are rejected. If controlled startup is still running, the command returns a pollable `launching` task while `--timeout` remains the background startup deadline. | No |
| `editor close` | Close a clean editor; dirty packages fail with paths unless `--save-dirty` explicitly saves them (`--force` explicitly discards) | No |
| `editor new-level PATH [--template PATH]` | Safely create/open new level; verifies the active editor world and attempts recovery if the HTTP bridge resets during transition | Yes |
| `editor new-blank-level [--discard-dirty-map] [--timeout SEC]` | Create an unsaved transient `/Temp/Untitled_*` level through native Remote Control, verify it is active, then run setup in a separate `run-script --no-save`. Rejects dirty map packages unless discard is explicit | Yes |
| `editor open-level /Game/PATH [--timeout SEC] [--reload --discard-unsaved]` | Open an existing level via `LevelEditorSubsystem.LoadLevel`, then verify the active editor world. The synchronous request waits up to 300 seconds by default; timeout is configurable for large maps. `EDITOR_OPEN_LEVEL_TIMEOUT` means completion is unknown, so do not retry or launch another editor until status and active world are verified. To reload the active map from disk, both `--reload` and `--discard-unsaved` are required; ue-cli verifies the target is active, closes the editor while discarding every unsaved package, then launches a fresh editor directly on that map. Poll the returned launch task when startup is still pending. This restart path supports UE4.26 and UE5. Ordinary UE4.26 unloaded targets return `EDITOR_OPEN_LEVEL_UNSUPPORTED` because that engine lacks `LevelEditorSubsystem`. Rejects an already-loaded, non-active target World with `EDITOR_OPEN_LEVEL_UNSAFE_LOADED_WORLD`; close and use `editor launch --map` for that state. The path must include its Unreal mount root | Yes |
| `editor save-level` | Safely save current level; editor disconnects return top-level `EDITOR_CONNECTION_LOST` | Yes |
| `editor exec [--timeout SEC] [--log-wait SEC] COMMAND` | Run UE console command and return captured project Output Log text. Dispatch waits 15 seconds by default. A transport timeout never redispatches: when the project log is available, unique begin/end markers provide a read-only `task_id` for `task status` / `task wait`; `EDITOR_EXEC_IN_PROGRESS` means delivery is confirmed, while `EDITOR_EXEC_DELIVERY_UNKNOWN` may retain pending evidence. Observation tasks surface terminal Python errors and cannot safely cancel the already-dispatched Unreal command. Marker-delimited capture waits for the matching end marker within `--log-wait`, including delayed file flushes. `Automation RunTests` waits up to 300 seconds by default for `Automation Test Queue Empty`; `--log-wait` overrides the bound. Other commands retain a 1-second log-capture default. Separate-process output is not observable: `LiveCoding.Compile` returns `LIVECODING_RESULT_UNOBSERVABLE` after submission so it cannot be mistaken for compile success | Yes |
| `editor live-coding-compile [--timeout SEC] [--log-wait SEC]` | Windows UE5: run `LiveCoding.CompileSync`, wait for its terminal result, and report success/no-changes/failure/cancellation. Timeouts and disconnects stay unknown and are never retried. UE4 returns `LIVECODING_SYNC_UNSUPPORTED` before dispatch | Yes (UE5) |
| `editor viewport camera [--timeout SEC]` | Read active Level Viewport camera using UE-version-compatible fallback | Yes |
| `editor viewport game-view [on\|off\|toggle]` | Read or change active Level Viewport game-view state; changes return verified before/after values | Yes |
| `editor viewport bookmark jump --index N [--timeout SEC]` | Jump Level Viewport bookmark 0-9; Windows only | Yes |
| `editor run-script [PATH|-] [-c CODE] [--timeout N] [--no-save]` | Execute Python file, stdin (`-`), or short inline code with result capture. File execution temporarily puts the script directory at `sys.path[0]` for sibling imports, then restores the prior path. With a usable project log, read timeouts return a non-cancellable observation `task_id`; poll `task status`/`task wait` without replaying. Otherwise `EDITOR_SCRIPT_TIMEOUT` keeps completion unknown. `--no-save` skips ue-cli's post-script save, not saves performed by the script or Unreal APIs. Connection resets return `EDITOR_CONNECTION_LOST`; when available, details include editor process-exit evidence and a bounded fatal/assert log tail. Both outcomes may have partially executed and are unsafe to replay blindly. Blocks known-crashy top-level map transitions via `EditorLoadingAndSavingUtils.new_blank_map`/`load_map` and `EditorLevelLibrary.load_level`; use `editor new-blank-level` for a transient blank world. Helper definitions for offline modes are allowed when not called at module top level. | Yes |
| `editor cvar get NAME [--timeout N]` / `editor cvar set NAME VALUE` | Get/set console variable; `get` has a bounded total timeout and errors on missing/unverified empty CVars, negative values are supported (`... set r.X -4` or `... set r.X -- -4`) | Yes |
| `editor enable-remote` | Explicitly enable RemoteControl, PythonScriptPlugin, and EditorScriptingUtilities in `.uproject`, then create/update `DefaultRemoteControl.ini` | No |
| `editor api-discover TARGET [-q QUERY] [-d NAMES] [--timeout N]` | Discover UE class/struct/API. TARGET: class, struct, asset path, loaded UObject/subobject path, actor path | Yes |
| `editor cancel TASK_ID` | Cancel async editor launch | - |
| `editor plugin-version` | Compare bundled vs loaded plugin | Yes (loaded) |
| `editor plugin-upgrade` | Deploy -> compile -> restart plugin upgrade; relaunch uses the normal interactive/windowed editor default | No |

### api-discover Usage

`api-discover` belongs under `editor`, not top-level.

**TARGET auto-detection**: class name, struct name (for UE Python structs such as `CustomInput`), asset path, loaded UObject/subobject path, actor path:

```bash
# Class name - direct lookup:
ue-cli editor api-discover DirectionalLight
ue-cli editor api-discover unreal.MaterialEditingLibrary -q connect

# Native reflection name and UE Python alias both resolve:
ue-cli editor api-discover KismetSystemLibrary -d GetConsoleVariableIntValue
ue-cli editor api-discover SystemLibrary -d GetConsoleVariableIntValue

# Asset path (/Game/...) - auto-detects class from the live asset:
ue-cli editor api-discover /Game/Materials/M_Water

# Loaded UObject/subobject path - resolved with unreal.find_object first:
ue-cli editor api-discover "/Game/UI/WBP.WBP_C:WidgetTree.Image_Dot"

# Actor path (contains PersistentLevel) - auto-detects class from scene actor:
ue-cli editor api-discover /Game/Maps/L.L:PersistentLevel.Light_0

# Drill into details with -d:
ue-cli editor api-discover DirectionalLight -d bHidden,Intensity
ue-cli editor api-discover /Game/Materials/M_Water -d BlendMode,ShadingModel

# WRONG - these will fail:
ue-cli api-discover DirectionalLight   # not top-level, needs 'editor' prefix
```

**Progressive disclosure**: Details panel style.

1. **Overview** (default): property/function **names** only.
   ```json
   {"properties": ["Constant", "Desc"], "functions": ["ConnectMaterialProperty"]}
   ```

2. **Detail** (`-d NAMES`): full info for named items: tooltips, categories, params, returns, read/write.
   ```bash
   editor api-discover unreal.MaterialEditingLibrary -d ConnectMaterialProperty
   ```
   ```json
   {"items": [{"kind": "function", "name": "ConnectMaterialProperty",
     "detail": {"tooltip": "Connect a material expression output to...",
       "return_type": "bool",
       "params": [{"name": "FromExpression", "type": "UMaterialExpression*"}, ...]}}]}
   ```

**Data sources**: C++ reflection via `CliAnythingBridgeLibrary.get_class_info()` is primary. Targeted `-q`/`-d` queries also merge matching live Python wrapper symbols omitted by reflection; these results are marked `python_only` with `source: python_binding`.

Standard `Kismet*` reflection classes and their shorter UE Python aliases resolve in either direction. Discovery returns the native class plus the callable alias path, such as `unreal.SystemLibrary.get_console_variable_int_value`.

## confirmation - Active Dialog Polling

These commands use a project-local mailbox, not Remote Control, so `list`, `answer`, and `disable` remain usable while a brokered dialog blocks UE. Enable the bounded lease before an operation can prompt. `enable` requires loaded Bridge 1.32 or newer.

| Command | Description |
|---------|-------------|
| `confirmation enable [--pid PID] [--ttl SEC]` | Arm standard `FMessageDialog` interception for one live project editor; default TTL is 900 seconds |
| `confirmation list [--pid PID]` | List brokered confirmations and detectable UE windows. Only `source=bridge`, `answerable=true` entries support CLI answers |
| `confirmation answer CONFIRMATION_ID --choice CHOICE [--wait SEC]` | Submit one exact reported choice through the out-of-band mailbox |
| `confirmation disable [--pid PID]` | End the lease; any unresolved brokered dialog falls back to normal editor UI |

Editor-dependent commands detect an active brokered dialog before dispatch and return `EDITOR_BLOCKED_BY_CONFIRMATION` with `next_command`. If a request itself triggers the dialog, the same code replaces its transport timeout. A detected non-brokered window returns `EDITOR_BLOCKED_BY_DIALOG`; `confirmation list` reports it but does not auto-click it. With explicit authorization to discard editor state, `editor close --force` can terminate verified processes matching the selected project while such a startup window is open.

## project - Project Management

| Command | Description |
|---------|-------------|
| `project info [--project PATH]` | Show `.uproject` info |
| `project config list` | List config files |
| `project config get CONFIG_NAME [--section SEC]` | Read config |
| `project config set CONFIG_NAME SECTION KEY VALUE` | Set config value |
| `project generate` | Generate Visual Studio project files |

`CONFIG_NAME` accepts a bare category (`Engine`), a config stem (`DefaultEngine`), or a standard filename (`DefaultEngine.ini`). These forms resolve to the same project config when it exists. Paths and non-`.ini` extensions are rejected.

## asset - Asset Operations

All asset commands require editor.

| Command | Description |
|---------|-------------|
| `asset list [-q QUERY] [--class CLASS] [--path PATH] [--limit N]` | Search assets via Asset Registry. `--class Blueprint` includes Blueprint-family assets such as `WidgetBlueprint` and `AnimBlueprint` |
| `asset exists ASSET_PATH` | Check existence |
| `asset property ASSET_PATH PROP[=VALUE]` | Get/set property |
| `asset delete ASSET_PATH [--force]` | Delete with ref detection. Accepts package paths like `/Game/A` and full object paths like `/Game/A.A`; package paths are normalized before deletion |
| `asset refs ASSET_PATH` | List referencers |
| `asset texture-source ASSET_PATH` | Read Texture2D source size/format and basic alpha/value stats through the bridge plugin |
| `asset duplicate SRC DEST [--force]` | Duplicate and explicitly save the destination package before success; save failure returns `ASSET_DUPLICATE_FAILED` |
| `asset rename SRC DEST [--timeout N]` | Rename/move asset. Waits 120 seconds by default. A response timeout is post-checked; confirmed moved state succeeds, otherwise `ASSET_RENAME_TIMEOUT` reports unknown completion plus safe existence checks and must not be retried blindly |

## build - Build System

Build commands do not require editor.

Synchronous `build compile` / `build cook` / `build package` stream the live UAT/UBT log to stderr while waiting, similar to UE `Build.bat`. Repeated MSVC command-line warnings show once plus a folded count; the `log_file` retains the complete unmodified output. JSON stdout stays one final payload with `log_file`.
For non-Win64 platforms, `build compile` calls UE `Build.bat` directly for the project's detected Game target, falling back to the `.uproject` name when no Game `Target.cs` is present.
For project repair, normal validation, and final completion on Win64, run `build compile` without `--module`. This builds the detected `<Project>Editor` target and its required dependencies. A large compile is expected and correct; do not replace it with a Game, plugin, or other isolated module merely to reduce work.
The Editor target is inferred from `--project`; do not pass `--target`. If one is supplied, `BUILD_TARGET_INFERRED` includes the accepted replacement command. `--config` and `--configuration` are equivalent.
After a successful Win64 compile, ue-cli validates the PE files declared by UE's generated Editor target receipt. A missing or malformed DLL/EXE returns `INVALID_BUILD_OUTPUT` with the receipt and affected paths instead of reporting a false success.
After cancelling a Win64 Editor compile, ue-cli checks every `RuntimeDependencies` path declared by the current Editor target receipt. Missing files return nonzero `BUILD_CANCELLED_OUTPUTS_INCOMPLETE` with counts, examples, and a full `build compile` recovery command.
Use repeated `--module NAME` only when the user explicitly requests a focused diagnostic. ue-cli still calls `Build.bat` for the detected Editor target, constrained by `-Module=NAME`; this is not final Editor-target validation. Follow it with full `build compile` before launching the editor or reporting build success.


On Windows, `build compile --platform Win64` refuses to start while an UnrealEditor process for the same project is running, because editor/plugin DLLs are commonly locked and link fails with `LNK1104`. Run `editor close` first, then compile.

If MSVC reports `C3859` or Windows pagefile error 1455, inspect `BUILD_RESOURCE_EXHAUSTED`. Reduce the active executor's own parallel-job limit and ensure the paging file has enough free space. For `bk_dist`, UBT's local memory-based parallel-action limit may not control the external executor; use its own job setting or disable it before retrying.

If Android package assembly fails with Gradle's `java.io.IOException: Unable to establish loopback connection`, inspect `BUILD_GRADLE_LOOPBACK_FAILED`. The failure remains nonzero and is classified as a transient local-environment problem. Retry the same package command with `--uat-arg=-iterate`; if it was already present and Cook completed, ue-cli reports that unchanged cooked outputs can be reused. For repeated failures, stop stale Gradle daemons and inspect local firewall, antivirus, proxy, and loopback policy.

| Command | Description |
|---------|-------------|
| `build compile [--project PATH] [--config/--configuration C] [--platform P] [--module NAME]... [--no-wait] [--timeout N]` | Compile C++; Win64 defaults to the inferred full Editor target. Repeated modules are explicit focused diagnostics only |
| `build cook [--project PATH] [--platform P] [--package PACKAGE]... [--output-dir DIR] [--ini OVERRIDE]... [--no-wait] [--timeout N]` | Cook content through UAT; package seeds, cook output, and ini overrides map to native UE options |
| `build package [--project PATH] [--platform P] [--config C] [--output-dir DIR] [--map MAP]... [--cook-flavor F] [--uat-arg=-ARG]... [--no-wait] [--timeout N]` | Reproducible BuildCookRun package pipeline; final result includes `uat_command` |
| `build stop [--project PATH]` | Cancel project-owned async build tasks, then kill any remaining MSBuild/UBT tree |
| `build is-building [--project PATH]` | Check compile/build running |
| `build status [--project PATH] [TASK_ID]` | Check artifacts/logs or async progress |
| `build cancel TASK_ID` | Cancel async build |

`build package --uat-arg` appends one argument to UAT `BuildCookRun`; it does
not append arbitrary arguments directly to Cook Commandlet. Put cooker-only
switches in one `-AdditionalCookerOptions=` value. In PowerShell, quote the
whole value without adding inner literal quotes:

```powershell
ue-cli --project F:\Game\Game.uproject build package `
  --uat-arg='-AdditionalCookerOptions=-NoDefaultMaps -CookProcessCount=1'
```

Targeted cook example:

```powershell
ue-cli --project F:\Game\Game.uproject build cook --platform Android `
  --package /Game/Foo/A --package /Game/Foo/B `
  --output-dir "F:\Cook Output" `
  --ini "Engine:[Section]:Key=Value"
```

`--package` supplies package seeds to CookCommandlet; UE may also cook their
dependencies and roots selected by project configuration. `build cook
--output-dir` is the cooked-content root. The separate `build package
--output-dir` option remains the archive destination.

Targeted Android example (repeat `--uat-arg=...` for long-tail BuildCookRun switches and per-run ini overrides):

```powershell
ue-cli --project F:\Game\Game.uproject build package --platform Android --config Development `
  --map /Game/Maps/Oregon_Main --cook-flavor ASTC `
  --uat-arg=-pak --uat-arg=-iostore --uat-arg=-compressed --uat-arg=-prereqs `
  --uat-arg=-nodebuginfo --uat-arg=-unversionedcookedcontent `
  --uat-arg=-SkipCookingEditorContent `
  --uat-arg="-ini:Engine:[Section]:Key=Value"
```

## scene - Scene/Level Queries

All scene commands require editor.

| Command | Description |
|---------|-------------|
| `scene list [--class CLASS] [-q QUERY] [--field name\|label\|path\|all] [--exact]` | List actors in current level; outputs `name`, World Outliner `label`, `path`, `class`; `-q` searches name/label/path by default |
| `scene property ACTOR_PATH PROP[=VALUE]` | Get/set property; reads PostProcessVolume `Settings.WeightedBlendables.Array`; Bridge 1.34+ reads `LODData[N].PaintedVertices` and `LODData[N].OverrideVertexColors` on StaticMeshComponents |
| `scene get-transform ACTOR_PATH` | Get transform |
| `scene list-components ACTOR_PATH` | List actor components |
| `scene get-material ACTOR_PATH [--index N]` | Get material from actor mesh |

## material - Material Viewing & Editing

All material commands require editor.

### Viewing
| Command | Description |
|---------|-------------|
| `material list [--path /Game/]` | List materials |
| `material info MATERIAL_PATH` | Nodes, params, textures, connections, Custom node code |
| `material get-graph MATERIAL_PATH` | Mermaid topology + orphan detection |
| `material get-stats MATERIAL_PATH` | Compilation stats |
| `material get-errors MATERIAL_PATH` | Compilation errors for Material, MaterialInstanceConstant, or MaterialFunction (Bridge 1.33+) |
| `material list-textures MATERIAL_PATH` | Referenced textures |
| `material analyze MATERIAL_PATH` | Common issue detector |
| `material dump-hlsl MATERIAL_PATH --output PATH [--platform P] [--shader-type T] [--wait-timeout SECONDS] [--full]` | Compiled HLSL via `r.DumpShaderDebugInfo`; resolves material instances to the exact UE5 shader debug group or UE4.26 base-material directory; UE5 Android preview aliases include `vulkan_sm5_android_preview` and `vulkan_es31_android_preview`; waits 10s by default |
| `material hlsl-code MATERIAL_PATH` | Material/MaterialInstance HLSL source; MaterialFunction returns `MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET` |
| `material shader-source MATERIAL_PATH` | Compiled `.usf` sources with cbuffers/structs |

### Editing
| Command | Description |
|---------|-------------|
| `material add-node PATH --type CLASS [--pos-x X] [--pos-y Y] [--set KEY=VALUE] [--add-input NAME] [--code-file PATH]` | Add expression node |
| `material delete-node PATH --node NAME` | Delete expression node |
| `material rename-custom-input PATH --node NODE --from OLD --to NEW [--no-update-code]` | Rename Custom node input/HLSL variable |
| `material connect PATH --from NODE [--from-output PIN] --to NODE --to-input PIN` | Connect nodes |
| `material disconnect PATH --from NODE [--from-output PIN] --to NODE --to-input PIN` | Disconnect nodes |
| `material set-param PATH --name N --value V --type scalar\|vector\|texture` | Set and readback-verify MaterialInstance param (`applied` / `readback_match`) |
| `material get-param PATH --name N` | Get MaterialInstance param |
| `material recompile PATH [--save]` | Force shader recompile; no disk save by default. May leave package dirty in memory. `--save` saves only the target package. |

## blueprint - Blueprint Viewing & Editing

All blueprint commands require editor.

| Command | Description |
|---------|-------------|
| `blueprint list [--path /Game/]` | List blueprints |
| `blueprint info BLUEPRINT_PATH` | Graphs, nodes, variables |
| `blueprint add-function PATH --name NAME` | Add function graph |
| `blueprint delete-function PATH --name NAME` | Remove function graph |
| `blueprint add-variable PATH --name NAME --type TYPE` | Add member variable |
| `blueprint delete-variable PATH --name NAME` | Delete member variable |
| `blueprint delete-unused-variables PATH` | Remove unused variables |
| `blueprint rename-graph PATH --old OLD --new NEW` | Rename graph |
| `blueprint compile PATH` | Compile blueprint |

## umg - Widget Blueprint Authoring

All UMG commands require editor and the CliAnythingBridge plugin.

| Command | Description |
|---------|-------------|
| `umg create WIDGET_PATH [--root-class CLASS] [--root-name NAME] [--force] [--variable]` | Create a Widget Blueprint with a root widget; default root is `CanvasPanel` |
| `umg add-widget WIDGET_PATH --type CLASS --name NAME [--parent CANVAS] [--text TEXT] [--x X] [--y Y] [--w W] [--h H] [--z Z] [--variable]` | Add a child widget to a CanvasPanel and set its Canvas slot. `WIDGET_PATH` accepts package, object, generated-class, or WidgetTree subobject paths |
| `umg tree WIDGET_PATH` | Inspect the design-time WidgetTree, including root, child widgets, slots, TextBlock text, and Image brush metadata. `WIDGET_PATH` accepts package, object, generated-class, or WidgetTree subobject paths |
| `umg live-tree TARGET [--limit N]` | Inspect live UUserWidget instances by instance name/path or generated class name. Outputs child widget names/classes, CanvasPanelSlot layout, Image brush resource, and cached geometry sizes without reading protected BindWidget/WidgetTree fields |
| `umg set-image WIDGET_PATH --name NAME [--texture PATH] [--image-size W H] [--x X] [--y Y] [--w W] [--h H] [--z Z]` | Edit an existing Image widget brush resource, Brush ImageSize, and CanvasPanelSlot layout. `--image-size` updates the brush size without changing the resource or Canvas slot size |

## screenshot - Screenshot

All screenshot commands require editor.

| Command | Description |
|---------|-------------|
| `screenshot capture [--path PATH] [--filename NAME] [--no-compress] [--include-ui]` | Redraw and capture a fresh frame from the active Level Viewport at its live resolution. Default capture reads scene pixels only; `--include-ui` captures the composed viewport region including stat HUD and Slate/Canvas overlays. Requires the bundled bridge version shown by `editor status`. `--path` may be a full output file path or an existing directory. Directory paths combine with `--filename`; `.png` writes the raw PNG there, `.jpg/.jpeg` writes the compressed image there and keeps raw PNG beside it |
| `screenshot capture-sequence [-n N] [-i SEC] [--no-compress]` | Capture sequence with bounded per-frame waits |

## session - Undo/Redo

| Command | Description |
|---------|-------------|
| `session status` | Current session info |
| `session undo` | Undo last change |
| `session redo` | Redo last undone change |
| `session history` | Show undo history |

## task - Background Task Management

Generic async task polling/cancel.

| Command | Description |
|---------|-------------|
| `task status TASK_ID` | Check progress/result |
| `task wait TASK_ID [--timeout SEC]` | Block until final state; caller timeout reports `TASK_WAIT_TIMEOUT` without cancelling the task |
| `task cancel TASK_ID` | Cancel pending/running task |

## install-skills - IDE Skill Installation

| Command | Description |
|---------|-------------|
| `install-skills [--target PATH]` | Install skill docs into IDE dirs or custom path |

## Multi-Instance Support

Multiple editors: discover with `editor status --all`, target with `--port`.

```bash
ue-cli editor status --all
ue-cli --port 30010 editor status
ue-cli --port 30011 material list
```

Offline result items can have `next_command`. For stale editor process:

```bash
ue-cli --project "F:\MyGame\MyGame.uproject" editor launch
```

For online bridge mismatch, Remote Control commands remain available. A non-mutating validation can run without restart:

```bash
ue-cli --output json --project "F:\MyGame\MyGame.uproject" editor run-script --no-save -
```

`--no-save` skips ue-cli's automatic dirty-package save; it does not sandbox script side effects. Upgrade only when bridge-backed commands are needed:

```bash
ue-cli --project "F:\MyGame\MyGame.uproject" editor plugin-upgrade
```

`editor launch` kills stale matching editors and starts reachable editor.
