# Editor Lifecycle & Python Scripting

## Editor Lifecycle - Required Flow

When editor needed, follow exact sequence:

```
Step 1: editor status
  Matching instance online? Proceed to your task.
  No matching online instance? Continue to Step 2.

Step 2: preflight (or editor preflight)
  Engine/project ready? editor launch (launch prepares editor integration files)
  BuildId mismatch? Continue to Step 3.

Step 3: editor close (if running) -> build compile -> editor launch
  This is the ONLY correct way to fix module version mismatches.

Step 4: editor status (verify)
  Matching instance online? Proceed.
  `launching`/`unreachable`? Poll the reported status command and wait.
  Still `offline` after the stale grace? Report the error to the user.
```

Key points:
- `preflight` (`editor preflight` also works) is strictly read-only. It reports engine/project, Remote Control, and bridge readiness but never changes `.uproject`, config, or plugin files. It is an editor-startup check, not a prerequisite for `build cook` or `build package`.
- `editor enable-remote` is the explicit editor-automation mutation command. It enables RemoteControl, PythonScriptPlugin, and EditorScriptingUtilities, then configures Remote Control. `editor launch` may perform the same preparation and deploy/enable CliAnythingBridge because launching the controlled editor requires them.
- Source engine automation plugin has source but no Editor DLLs -> close matching editors, run returned `remote_control_recovery.build_command` in PowerShell, run `setup_command`, then retry original launch. The UBT `-Plugin=` build works while plugin is still disabled; ue-cli does not enable an uncompiled module.
- `editor launch` starts an interactive editor by default. Pass `--unattended` only when UE dialogs must be suppressed; `--no-unattended` explicitly preserves interactivity. It waits up to 30 seconds for the API, then returns `launching` with a task id if startup is still in progress. `--timeout` controls the background startup deadline and does not extend the foreground shell wait. Do not use `sleep`.
- `editor launch --no-remote` is an explicit direct-process fallback when automation dependencies are unavailable and no controlled command is needed. It skips Remote Control config/plugin changes, port checks, bridge deployment/compilation, and API/map verification. A completed task reports `launched`, `editor_readiness_verified=false`, and `map_verified=false`; close it in Unreal UI, or use `editor close --force` only with discard authorization.
- Async: `--no-wait`, then `editor status <task_id>` or `task status <task_id>`.
- DLL locked build fail -> `editor close`, then compile. If `editor plugin-upgrade` reports `LNK1104` with `locked_file`, close/kill all UnrealEditor processes for that project and retry the reported command. `plugin-upgrade` waits for matching editor processes to exit before compiling, then relaunches with the normal interactive/windowed default. Another stale process can still hold third-party plugin DLLs.
- `build compile --platform Win64` fails fast with `EDITOR_RUNNING_LOCKS_DLLS` when the same project editor is running. This avoids wasting minutes before UBT reaches a locked `UnrealEditor-*.dll` link step.
- CliAnythingBridge missing/stale module -> keep plugin enabled; `editor launch` deploys/enables it and precompiles before starting UE. Do not disable it to bypass startup.
- Do not create or load maps at module top level inside `editor run-script` with `EditorLoadingAndSavingUtils.new_blank_map`/`load_map` or `EditorLevelLibrary.load_level`; ue-cli blocks these known-crashy world teardown paths. This includes duplicating the active World and immediately loading that duplicate. Reusable helper functions may contain offline/commandlet map-load branches as long as they are not called at top level. Use `editor new-blank-level` for an unsaved transient world, `editor new-level /Game/Path/Level` for a persistent asset, or `editor open-level /Game/Path/Level` for an existing asset, then run setup separately. A separate run-script automatically uses the active transient editor world. `editor new-blank-level` rejects dirty maps unless `--discard-dirty-map` explicitly authorizes loss. To discard changes and reload the currently active map from disk, run `editor open-level /Game/Path/Level --reload --discard-unsaved`; the two explicit flags restart the editor on that verified map and discard all unsaved packages. Poll the returned launch task when startup remains pending. `editor open-level` rejects an already-loaded, non-active target World before `LoadLevel`. After duplicating an active World, save the duplicate without loading it, close the editor, then start a fresh lifetime with `editor launch --map /Game/Path/Level` before continuing setup. `editor launch --map` and `editor open-level` inputs must include their Unreal mount root (`/Game/...`); bare names are ambiguous and rejected. Level commands verify the active editor world after transition and try to recover if the HTTP bridge resets.
- User says editor running -> still verify with Step 1.
- `unreachable` means the editor process is alive but Remote Control may be busy during PIE/loading/startup. Retry the reported `editor status` command; do not relaunch or kill yet.
- `launching` means an active `editor.launch` task owns the process. Poll `editor status <task_id>`.
- `offline` with `next_command` means the process stayed unreachable past the stale grace or has a clearer failure; use its `next_command`.
- `editor launch` kills zombie `UnrealEditor.exe` (no API). API-alive `ALREADY_RUNNING` blocks.

### Example

```bash
# 1. Preflight - catches build mismatches before they cause hangs
ue-cli --project F:\MyGame\MyGame.uproject preflight

# 2. If BuildId mismatch, compile first
ue-cli --project F:\MyGame\MyGame.uproject build compile

# 3. Launch editor (blocks until ready)
ue-cli --project F:\MyGame\MyGame.uproject editor launch --map /Game/Maps/MyMap

# 4. Verify
ue-cli editor status
```

### Async Launch

```bash
# Async (returns immediately, poll for progress)
ue-cli editor launch --no-wait
# -> {"task_id": "t-abc123", "status": "submitted", "suggested_poll_interval_seconds": 5}

# Check launch progress
ue-cli editor status <task_id>
# Or use generic task commands:
ue-cli task status <task_id>
ue-cli task cancel <task_id>
```

### Status Values

`editor status` without task id returns result array. Each item has:
- `status`: `online` if Remote Control reachable; `launching` when an active launch task owns an unreachable process; `unreachable` for temporary Remote Control loss while the process is alive; `offline` only after stale grace or clear failure
- `pid`: UnrealEditor pid
- `port`: Remote Control port
- `project_path`: uproject path
- `bridge_version` / `bundled_version` / `plugin_match`: online bridge plugin health. `plugin_match` can be `null` if the version probe timed out or the editor is busy.
- `message` / `suggestion` / command hints: recovery guidance. For `unreachable`, retry status; for `launching`, poll the task; for stale `offline`, relaunch/close guidance may be provided. Online bridge mismatches expose `no_restart_command` for Remote Control Python validation and `upgrade_command` for bridge-backed commands.

With top-level `--project`, `editor status` filters to that project by default. Use `editor status --all` only when you need to inspect editors for other projects too.

If an online item has `plugin_match: false`, inspect its capability fields. `degraded_mode=remote_control_only`, `remote_control_commands_available=true`, and `run_script_no_save_available=true` mean Remote Control Python execution remains available without a restart. Run `upgrade_command` only when a bridge-backed command is needed and restarting is acceptable. Do not force recompile before every launch; `editor launch` deploys bridge source and recompiles only when plugin load failure requires it.

`editor status <task_id>` returns async task progress. After a launch timeout, it also reconciles the task to completed only when the same project/PID is online and any requested map matches; use the returned `next_command` instead of relaunching blindly.

## Close

```bash
ue-cli editor close
```

`editor close` does not report `closed` only because the Remote Control API stopped responding. It closes a clean editor by default; dirty packages return `EDITOR_DIRTY_PACKAGES` with their paths and leave the editor running. Use `--save-dirty` only when saving every reported dirty package is intended, or `--force` only when discarding them is authorized. On Windows it also waits for matching same-project `UnrealEditor.exe` processes to exit, and terminates a stale lock holder when needed so immediate `build compile` does not hit locked editor/plugin DLLs.

## Active Confirmation Polling

Use this only for an agent-owned interactive editor. It does not create a background listener. The Agent queries the local mailbox when needed.

```powershell
# Arm before a risky or potentially prompting operation. Re-run to refresh TTL.
ue-cli --project "F:/MyGame/MyGame.uproject" confirmation enable --ttl 900

# Query without Remote Control; this still works while UE's GameThread is blocked.
ue-cli --project "F:/MyGame/MyGame.uproject" confirmation list

# Use only an exact choice returned for a source=bridge, answerable=true item.
ue-cli --project "F:/MyGame/MyGame.uproject" confirmation answer <id> --choice no

# Return unresolved standard dialogs to normal editor UI before human handoff.
ue-cli --project "F:/MyGame/MyGame.uproject" confirmation disable
```

Call `confirmation list` in these cases:

- Any editor-dependent command returns `EDITOR_BLOCKED_BY_CONFIRMATION` or `EDITOR_BLOCKED_BY_DIALOG`; follow its `next_command`.
- A Remote Control command times out or reports unreachable while the matching UnrealEditor process remains alive.
- A destructive, overwrite, import, save, plugin, map-transition, or long-running operation has not produced expected progress.
- Before retrying a command with unknown delivery, closing the editor, or declaring the editor hung.

The lease must be enabled before the dialog occurs. Bridge interception covers standard `FMessageDialog` calls after the bridge installs its post-engine-init hook. Startup recovery, custom Slate windows, platform file pickers, and third-party dialogs may appear as `source=window`, `answerable=false`; inspect those in editor UI. If closing is the requested outcome and discarding state is explicitly authorized, `editor close --force` may terminate verified processes matching the selected project without answering the window. For **Restore Packages** after a session the Agent started and closed, follow [the startup-recovery rule](safety.md#restore-packages-after-an-agent-managed-restart) to handle the window without repeating an already authorized recovery or discard decision. A lease expiry or `confirmation disable` removes hidden interception and sends the unresolved standard dialog to normal editor UI.

Inspect the title, message, and choices; answer according to the task's existing authorization, without blindly choosing `yes`. The triggering command may already have executed side effects. After answering, verify editor/project state before retrying it.

## Python Scripting Patterns

Use `editor run-script` when no CLI command covers operation. Use `-c` only for short one-liners; for multiline Python, especially in PowerShell, pipe code to `editor run-script -` or pass a `.py` file so shell argv splitting cannot corrupt code or indentation.

### Result Convention

Set `result` dict to return structured data. Missing -> `{"status": "ok"}`. Exception -> `{"error": "...", "error_type": "...", "traceback": "..."}`.

```bash
# Inline Python via -c - result variable is captured
ue-cli editor run-script -c "result = {'actors': 42}"

# Multiline Python via stdin - avoids PowerShell argv splitting
@'
value = 41
result = {'actors': value + 1}
'@ | ue-cli editor run-script -

# Script file - same result capture, auto-save
ue-cli editor run-script build_scene.py --timeout 60

# Read-only script - skip auto-save
ue-cli editor run-script query.py --no-save
```

For long scripts that save or modify many assets, pass a larger `--timeout`
(for example `--timeout 300`). If the project log is usable, a timeout returns
a read-only observation `task_id`; poll `task status` or `task wait` without
resending the script. `executing` means delivery is confirmed but Unreal's game
thread is still inside the wrapper. Without a usable log, `EDITOR_SCRIPT_TIMEOUT`
keeps completion unknown, so run `editor status` and inspect the Output Log.
`--no-save` only disables ue-cli's post-script save. Script calls and Unreal APIs
can still save; `EditorLevelLibrary.new_level('/Temp/Name')`, for example, may
write `Saved/Name.umap`.

### Safe Static Mesh UV Access

`editor run-script` executes in the Unreal Editor process. `--no-save` disables
automatic package saves; it cannot turn a native Unreal `check()` into a Python
exception. Never discover UV channels by incrementing `channel_index` in
`StaticMeshDescription.get_vertex_instance_uv(...)` until an exception occurs.
An out-of-range channel can terminate the editor.

Query the StaticMesh before reading its MeshDescription, then validate the
channel index explicitly. UE5 uses `StaticMeshEditorSubsystem`; UE4.26 uses the
legacy `EditorStaticMeshLibrary`. UE4.26 exposes the safe count query but not
`StaticMesh.get_static_mesh_description`, so report that read path as
unsupported:

```python
import unreal

mesh = unreal.load_asset("/Game/Meshes/SM_Example.SM_Example")
lod_index = 0
channel_index = 1

if hasattr(unreal, "StaticMeshEditorSubsystem"):
    static_mesh_editor = unreal.get_editor_subsystem(
        unreal.StaticMeshEditorSubsystem
    )
else:
    static_mesh_editor = unreal.EditorStaticMeshLibrary

channel_count = static_mesh_editor.get_num_uv_channels(mesh, lod_index)
if not 0 <= channel_index < channel_count:
    raise ValueError(
        f"UV channel {channel_index} out of range; mesh has {channel_count} channels"
    )

result = {
    "uv_channel_count": channel_count,
    "mesh_description_uv_read_supported": hasattr(
        mesh, "get_static_mesh_description"
    ),
}
if result["mesh_description_uv_read_supported"]:
    description = mesh.get_static_mesh_description(lod_index)
    uv = description.get_vertex_instance_uv(
        unreal.VertexInstanceID(0), channel_index
    )
    result["uv"] = str(uv)
```

### Console Commands

Use `editor exec` for UE console commands:

```bash
ue-cli editor exec "stat unit"
ue-cli editor exec "r.DumpRenderTargetPoolMemory"
```

`editor exec` returns bounded captured output in `log_output`/`log_text`. `omitted_line_count` reports lines excluded by filtering or the fixed inline limit; `log_file` keeps complete diagnostics. Marker-delimited capture waits for the matching end marker within `--log-wait`, including delayed file flushes. Automation runs retain lifecycle/result lines instead of unrelated discovery noise and wait up to 300 seconds by default for `Automation Test Queue Empty`; use `--log-wait <seconds>` to override that bound. Other console commands keep the 1-second log-capture default.

`LiveCoding.Compile` is different: Unreal completes it asynchronously in the separate `LiveCodingConsole`. `editor exec LiveCoding.Compile` therefore submits the request but exits non-zero with `LIVECODING_RESULT_UNOBSERVABLE`; never use it as a compile-success check. On Windows UE5, use `editor live-coding-compile --timeout 600` to invoke `LiveCoding.CompileSync` and wait for a structured success, no-changes, failure, or cancellation result while the editor remains open. A timeout or disconnect stays unknown and is never retried; an editor crash includes process and fatal-log evidence when available. UE4.26 lacks the synchronous engine API and returns `LIVECODING_SYNC_UNSUPPORTED` before dispatch.

Negative CVar values are valid:

```bash
ue-cli editor cvar set r.Shadow.Virtual.ResolutionLodBiasDirectional -4
ue-cli editor cvar set r.Shadow.Virtual.ResolutionLodBiasDirectional -- -4
ue-cli editor cvar get r.VSync --timeout 10
```

`editor cvar get NAME` uses a 10-second total timeout by default. It verifies the selected editor's TCP listener, then spends the remaining budget on the actual CVar query instead of a duplicate functional readiness call. A busy PIE/game thread returns `CVAR_GET_TIMEOUT` instead of waiting on a second fallback request. It also fails instead of returning a misleading success when UE returns an empty value for a missing or unverified CVar. If the bridge plugin is old and the value is empty, upgrade it:

```bash
ue-cli editor plugin-upgrade
```

## Viewport Bookmarks

Console attempts have executed without moving camera:

```bash
ue-cli editor exec "BOOKMARK JUMPTO=1"
ue-cli editor exec "JumpToBookmark1"
```

Read active Level Viewport camera with the CLI command. UE Python differs by engine branch; do not call `LevelEditorSubsystem.get_level_viewport_camera_info()` directly.

```bash
ue-cli --output json --project "F:/path/to/Project.uproject" editor viewport camera
```

Use dedicated Windows-only bookmark command. It finds UE main window, foregrounds it, focuses viewport, sends numeric key `0`-`9`, then compares active viewport camera before/after.

```bash
ue-cli --output json --project "F:/path/to/Project.uproject" editor viewport bookmark jump --index 1
```

Success returns index, window, before/after camera. `BOOKMARK_JUMP_UNCHANGED` means likely focus fail, missing bookmark, changed shortcut, or wrong window.

## Viewport Game View

`editor exec ToggleGameView` can report command execution without changing the Level Viewport. Use the dedicated state API instead:

```bash
# Query current state
ue-cli --output json --project "F:/path/to/Project.uproject" editor viewport game-view

# Set or toggle, with verified before/after state
ue-cli --output json --project "F:/path/to/Project.uproject" editor viewport game-view on
ue-cli --output json --project "F:/path/to/Project.uproject" editor viewport game-view off
ue-cli --output json --project "F:/path/to/Project.uproject" editor viewport game-view toggle
```

## RenderDoc Frame Capture

Capture GPU frame for offline shader/draw-call analysis.

### Prerequisites

1. **RenderDoc plugin loaded.** Project `DefaultEngine.ini`:
   ```ini
   [Plugins]
   +EnabledPlugins=RenderDoc
   ```
   Or enable via UE Editor -> Edit -> Plugins -> RenderDoc. Verify:
   ```bash
   ue-cli editor exec "renderdoc.captureframe"
   # If the plugin is missing, the command silently does nothing.
   ```

2. **Windowed editor required.** Not `-nullrhi`; RenderDoc needs real RHI.

### Capture a Frame

```bash
# 1. Ensure editor is online
ue-cli editor status

# 2. Capture the next frame
ue-cli editor exec "renderdoc.captureframe"
```

Capture saves `.rdc` under `<ProjectDir>/Saved/RenderDocCaptures/` (example `F:\MyProject\Saved\RenderDocCaptures\2026.05.11-11.01.53_capture.rdc`). RenderDoc UI may open if installed.

### Typical Workflow

```bash
# 1. Open the target map
ue-cli editor launch --map /Game/Maps/MyMap

# 2. (Optional) Tweak rendering settings before capture
ue-cli editor cvar set r.ShadowQuality 3
ue-cli editor cvar set r.AntiAliasingMethod 2
ue-cli editor cvar set r.Shadow.Virtual.ResolutionLodBiasDirectional -4

# 3. Capture a GPU frame
ue-cli editor exec "renderdoc.captureframe"

# 4. (Optional) Take a viewport screenshot for visual reference alongside the .rdc
ue-cli screenshot capture --filename before_capture
```

### Analyzing the Capture

Use `rdc-cli` skill if available, or RenderDoc app. Common tasks:
- **Shader debugging**: step pixel/vertex shaders.
- **Draw call inspection**: expensive draws, overdraw, redundant state.
- **Texture/RT verification**: inspect intermediate render targets.
- **Performance profiling**: GPU timings per draw/pass.

### Android Packaged Apps

For UE Android packaged captures, use `rdc-cli` Android loader workflow, not editor-only `renderdoc.captureframe`.

Known UE issue: Adreno + RenderDoc Android loader + `VK_KHR_buffer_device_address` can fail `vkAllocateMemory` with `VK_ERROR_INVALID_OPAQUE_CAPTURE_ADDRESS`. Workaround: comment user-identified `ADD_CUSTOM_EXTENSION` block in `Engine/Source/Runtime/VulkanRHI/Private/VulkanExtensions.cpp` (`FVulkanKHRBufferDeviceAddressExtension` through related ray-tracing/vendor diagnostic extensions), then rebuild/package Development. Keep evidence + cleanup in `rdc-cli` `references/android-loader-and-ue5.md`.

### Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Command executes but no capture | RenderDoc plugin not loaded | Check `DefaultEngine.ini` has `+EnabledPlugins=RenderDoc`; restart editor |
| Capture fails with D3D error | `-nullrhi` or headless mode | Remove `-nullrhi`; use windowed mode |
| Can't find `.rdc` | Unknown capture dir | Check `<ProjectDir>/Saved/RenderDocCaptures/` |
| RenderDoc UI doesn't open | RenderDoc not installed | Install from [renderdoc.org](https://renderdoc.org) |

### Synchronous Results and Deferred Callbacks

`editor run-script` is synchronous: CLI waits for Python main thread, returns result, disconnects.

1. **Deferred callbacks can outlive the command.** Registered functions retain their invocation globals while Unreal retains the callback.
2. **Store and unregister callback handles.** Errors raised after the command returns appear only in the editor log and cannot change the completed CLI result.
3. **Prefer split scripts for observable multi-frame work.** Use one `editor run-script` per frame-bound step when the caller must verify each result.

### Inline Python Auto-Mode

`editor run-script -c` executes inline Python with full result capture:
- Uses `exec_python_file`, captures result as JSON.
- Auto-saves dirty packages unless `--no-save`.
- Errors return message + traceback, not silent timeout.

## UE Python API - Class Lookup

UE Python functions live on Library/Subsystem helpers, not usually objects themselves. Start here, then `editor api-discover ClassName -q keyword`.

| I want to... | Start with |
|--------------|-----------|
| Spawn/delete/duplicate actors | `EditorActorSubsystem` |
| Move/rotate/scale actors | `EditorLevelLibrary` |
| Save packages | `EditorLoadingAndSavingUtils` |
| Open/create levels | `editor open-level` / `editor new-level` (`LevelEditorSubsystem`) |
| Load/save/delete/rename assets | `EditorAssetLibrary` |
| Create material nodes, connect pins | `MaterialEditingLibrary` |
| Create new asset | `AssetToolsHelpers.get_asset_tools()` -> `create_asset()` |
| Render targets, draw to texture | `KismetRenderingLibrary` |
| Mesh LODs, collision | `StaticMeshEditorSubsystem` |
| Viewport camera | `LevelEditorSubsystem` |
| Sub-levels, streaming | `EditorLevelUtils` |
| Sequencer playback/keys | `LevelSequenceEditorBlueprintLibrary` |

**Pitfalls** - wrong guesses:
- `spawn_actor` -> `EditorActorSubsystem.spawn_actor_from_class`, not `EditorLevelLibrary`
- `create_material` -> `get_asset_tools().create_asset()`, not `MaterialEditingLibrary`
- `set_material(slot, mat)` -> call on `MeshComponent`, not `StaticMesh` asset
- `set_location` -> `Actor.set_actor_location(vector)`; UE uses `set_actor_*`
- viewport realtime -> exposure varies by engine version. Run `editor api-discover LevelEditorSubsystem -q realtime`; when it lists `EditorSetViewportRealtime` / `editor_set_viewport_realtime`, call `unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_set_viewport_realtime(True)`. Fall back to console/CVars only when reflection confirms the function is unavailable.

**When you can't find the class**: pass live actor/asset path:
```bash
ue-cli editor api-discover "/Game/Maps/L.L:PersistentLevel.MyActor_0"
```

## Editor-Specific Error Patterns

| Error | Cause | Fix |
|-------|-------|-----|
| Connection refused | Editor not running | Follow lifecycle above |
| Timeout | Editor busy: shaders/loading | Run `editor status`; if reachable, wait 10-15s and retry |
| `EDITOR_BLOCKED_BY_CONFIRMATION` | Standard UE dialog is waiting in Bridge mailbox | Run returned `confirmation list`, inspect, then `confirmation answer` with an allowed choice |
| `EDITOR_BLOCKED_BY_DIALOG` | Startup/custom/non-brokered window blocks UE | Run returned `confirmation list`; resolve it in editor UI, or use `editor close --force` only when closing and discarding state are explicitly authorized |
| "modules built with different engine version" | Binary/engine mismatch | `editor preflight` -> `build compile` -> `editor launch` |
| Screenshot fails | Editor window not visible/minimized | Foreground editor, retry |


## Bridge Version Mismatch

If `editor status` reports `plugin_match=false`, the editor has already loaded an older or missing `CliAnythingBridge` DLL. UE cannot safely hot-reload that C++ bridge. This blocks bridge-backed features, not Remote Control itself.

For a validation script that does not mutate editor state, use the reported `no_restart_command` or run:

```powershell
ue-cli --output json --project "F:\MyGame\MyGame.uproject" editor run-script --no-save -
```

`editor run-script` uses Remote Control Python execution and does not require the bundled and loaded bridge versions to match. `--no-save` only disables ue-cli's automatic dirty-package save; it does not sandbox the script or prevent explicit mutations/saves. Run `upgrade_command` later when bridge-backed commands are needed and restarting is acceptable.
