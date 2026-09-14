# ue-cli

`ue-cli` controls Unreal Engine 4.26 and Unreal Engine 5.x editors from a shell. It provides structured commands for project inspection, editor lifecycle, materials, Blueprints, scenes, screenshots, CVars, Python, and UAT/UBT builds.

## Requirements

- Python 3.10+
- Git, required by the GitHub install command below
- Unreal Engine 4.26 or 5.x and a `.uproject`
- Loadable Epic `RemoteControl`, `PythonScriptPlugin`, and `EditorScriptingUtilities` engine plugins for controlled commands; `editor launch` enables and configures them for the selected project unless `--no-remote` is explicit
- Windows for the supported end-to-end workflow; editor discovery, launch, and UAT/UBT integration currently use Windows-specific paths and tools

A coding agent is optional. Every command can also be run manually.

## Install

The package is not currently published on PyPI. `pip install ue-cli` will fail. Install the current source from GitHub:

```powershell
python -m pip install "git+https://github.com/PZZZB/ue-cli.git"
ue-cli --version
```

For local development:

```powershell
git clone https://github.com/PZZZB/ue-cli.git
Set-Location ue-cli
python -m pip install -e ".[dev]"
ue-cli --version
```

If `ue-cli` is not found after installation, run it as:

```powershell
python -m cli_anything.unreal --version
```

## First Run

Use forward slashes in Windows paths. Quote paths so projects under directories containing spaces also work:

```powershell
$Project = "F:/path/to/MyProject.uproject"

# Read project metadata. Unreal Editor is not required.
ue-cli --output json --project "$Project" project info

# Read-only startup checks. This does not modify project files.
ue-cli --output json --project "$Project" preflight
```

If engine discovery fails for a custom source build, set its root and rerun `preflight`:

```powershell
$env:UE_ENGINE_ROOT = "F:/path/to/UnrealEngine"
```

Start the controlled editor:

```powershell
ue-cli --output json --project "$Project" editor launch
```

`editor start` is an exact alias for `editor launch` and accepts the same options.

First launch may:

- enable `RemoteControl`, `PythonScriptPlugin`, `EditorScriptingUtilities`, and `CliAnythingBridge` in the `.uproject`;
- create or update Remote Control config (`Config/DefaultRemoteControl.ini` on UE5, plus UE4's `Config/DefaultWebRemoteControl.ini` when launching);
- deploy `Plugins/CliAnythingBridge`;
- compile only the `CliAnythingBridge` Editor module when its binary is missing or stale, then repair and validate its `UnrealEditor.modules` metadata. A recovered targeted build reports `compile_result.status: ok` with `output_recovered: true`; its transient failure remains diagnostic history under `initial_output_validation`. Bridge upgrades preserve the previous plugin until validation passes and restore it with structured rollback details if deployment or compilation fails. If the remaining Editor target output is incomplete, launch stops with a structured full-build reason and recovery command instead of starting a full project build automatically.

For a source engine whose required Epic automation plugin has source but no Editor DLLs, launch stays read-only at the unsafe step and returns `remote_control_recovery`. Run its exact PowerShell `build_command`, then `setup_command`, then retry the original launch. The generated UBT command uses `-Plugin=` so the disabled source plugin can be compiled before ue-cli enables it.

When only a basic UnrealEditor process is needed, `editor launch --no-remote` explicitly skips Remote Control config, plugin enablement, bridge deployment, bridge compilation, port checks, and API readiness. It still requires engine/project preflight to pass, preserves existing same-project editors, and verifies that the spawned process remains alive for a short bounded observation. Success is `status=launched`, not `online`; `editor_readiness_verified=false` and `map_verified=false` stay explicit. Close this unautomated editor in its UI, or use `editor close --force` only when discarding editor state is authorized. `-NullRHI` is allowed only with this direct mode because no WebRemoteControl endpoint is expected.

Commit or back up the project before first launch if these project-file changes need review.

`editor launch` returns `online` when ready. Slow startup may return a `launching` payload with a `task_id`; poll that task:

```powershell
ue-cli --output json --project "$Project" editor status <task_id>
```

If crash recovery opens **Restore Packages**, normal launch stays `running` in `waiting_user_action` until you choose in Unreal. To explicitly discard that recovery opportunity for this launch, pass `editor launch --skip-restore`. CliAnythingBridge applies Unreal's native decline policy before the recovery prompt; it does not click UI, restore autosaves, or enable global unattended mode. The option requires controlled launch and cannot be combined with `--no-remote`.

Or block until any asynchronous task finishes, with an optional caller-side timeout:

```powershell
ue-cli --output json task wait <task_id> --timeout 300
```

`task wait` exits 0 for a completed task, 3 for a failed/task-timeout result, and 4 for cancellation or caller wait timeout. A caller timeout never cancels the task; output keeps the current task status and adds `wait.status=timeout` plus a follow-up command.

`build cook` skips UAT package preparation, so cook-only runs do not require a Game target receipt. If UAT reports a missing staging receipt, failure details retain `completed_phases: ["cook"]` only when the log explicitly confirms Cook completed; the overall operation remains failed.

For running `build cook` and `build package` tasks, `task status` inspects a bounded log suffix for Unreal's explicit `Cooker has been blocked from saving ...` warning. It keeps `status=running`, reports `stalled` plus a structured `diagnostic` with duration, package/object names, log path, and a user-controlled cancellation command. Default stall threshold is 600 seconds; set `UE_CLI_COOK_STALL_THRESHOLD_SECONDS` to override it. ue-cli never auto-cancels the task.

`build package --uat-arg` appends one argument to UAT `BuildCookRun`; it does
not append arbitrary arguments directly to Cook Commandlet. Put cooker-only
switches in one `-AdditionalCookerOptions=` value. In PowerShell, quote the
whole value without adding inner literal quotes:

```powershell
ue-cli --project F:\Game\Game.uproject build package `
  --uat-arg='-AdditionalCookerOptions=-NoDefaultMaps -CookProcessCount=1'
```

Task records keep lifecycle `status` separate from the current `phase` (for example, `running` plus `waiting_remote_control`). Terminal states are monotonic: a late worker result cannot replace cancellation or another final outcome. A timed-out launch, or a launch still marked `running/waiting_remote_control`, can become completed only after `editor status` verifies the same project, recorded process identity, and port owner are online. Requested maps must also match.

Launch-timeout diagnostics distinguish the OS TCP listener from an active connection and HTTP route health. `port_listening` and `listener_pid` report TCP-table ownership when available, while `tcp_connect_succeeded` reports the active connection probe. A Windows listener owned by the launched editor that does not accept a connection returns `failure_kind=api_listener_unresponsive` instead of `api_not_listening`.

Before an offline or starting process snapshot blocks a Windows launch, ue-cli revalidates PID existence. A confirmed exit is ignored; a live or inconclusive PID remains preserved.
Interrupting a foreground build command with Ctrl+C synchronously cancels its owned task and process tree before ue-cli exits. If safe cancellation cannot be confirmed, ue-cli returns `TASK_CANCEL_FAILED` with remaining-process diagnostics instead of only printing `Aborted!`.

If crash recovery blocks startup, the launch task stays `running` with phase `waiting_user_action`, matching UnrealEditor PID/window evidence, and a poll command. Choose **Restore Selected** or **Skip Restore** in that existing editor; the same launch task resumes and can complete. Its configured launch timeout still applies. ue-cli never auto-clicks this dialog.

For agent-owned interactive sessions, ue-cli can broker standard UE confirmation dialogs through a local mailbox. Arm it before an operation that may prompt:

```powershell
ue-cli --output json --project "$Project" confirmation enable --ttl 900
ue-cli --output json --project "$Project" confirmation list
ue-cli --output json --project "$Project" confirmation answer <id> --choice no
ue-cli --output json --project "$Project" confirmation disable
```

The Agent queries explicitly; no background listener is created. Editor-dependent commands return `EDITOR_BLOCKED_BY_CONFIRMATION` with a `next_command` when a standard brokered dialog is pending, including when the current request triggers it and otherwise would time out. `EDITOR_BLOCKED_BY_DIALOG` reports a detected startup/custom window that cannot be answered by CLI. Only `source=bridge`, `answerable=true` items accept `confirmation answer`. When discarding editor state is explicitly authorized, `editor close --force` can terminate verified processes matching the selected project even while such a non-brokered startup window is open; it never clicks the window. The bounded lease must exist before the dialog; expiry or `disable` returns unresolved standard dialogs to normal editor UI. Never auto-click **Restore Packages**; use explicit launch-time `--skip-restore` when discarding recovery is authorized.

Controlled launch requires WebRemoteControl, which Unreal does not start under `-NullRHI`. `editor launch` rejects that extra argument before creating a task or starting UnrealEditor; explicit `--no-remote` direct launch allows it. When launch receives `--extra-arg=-abslog=PATH`, task status and startup diagnostics report and inspect that explicit log file.

If startup reports a missing registered virtual shader source or Windows `STATUS_ENTRYPOINT_NOT_FOUND`, `editor launch` returns `EDITOR_ENGINE_BINARY_SOURCE_MISMATCH` or `EDITOR_ENGINE_BINARY_ENTRYPOINT_MISMATCH`. These signatures can follow a custom Engine branch switch that leaves stale or mixed DLLs. Use the returned `recovery_command` to compile the full Editor target without `--module`, then retry launch.

If startup reports `Plugin '<name>' failed to load`, `editor launch` returns `EDITOR_PLUGIN_LOAD_FAILED` with the named plugin, module when available, and original log diagnostic. Only a failure naming `CliAnythingBridge` can trigger its targeted rebuild; ue-cli first terminates the editor process it spawned so the Bridge DLL is not locked. An unrelated plugin failure never triggers Bridge cleanup or compilation.

If Unreal Editor exits before Remote Control startup with a `FileSystemCacheStoreMaintainer` crash stack, `editor launch` returns `EDITOR_EXTERNAL_DDC_CRASH` with `failure_kind=external_editor_ddc_crash`. This identifies an Unreal Engine DerivedDataCache failure rather than a Bridge failure, confirms no editor automation command was dispatched, and recommends one retry. If it repeats, preserve the reported log and CrashReportClient artifacts for Engine/DDC diagnosis.

After status becomes `online`, run a read-only editor query:

```powershell
ue-cli --output json --project "$Project" material list
```

Each shell invocation is a new process. Repeat `--project` on project-scoped commands when running outside the project tree. Commands that talk to an editor infer the nearest unique `.uproject` from the current directory or its parents, so they cannot silently fall through to another project's editor. An explicit `--project` wins; `--port` selects the port but keeps the inferred project identity check. Use `editor status --all` to inspect every project. Project selection remains sticky inside the interactive `ue-cli` REPL.

If an already-running editor was started without Remote Control preparation, run this command, close that editor, then launch it again:

```powershell
ue-cli --output json --project "$Project" editor enable-remote
```

## Install Agent Skills

Skill installation is optional. By default, this command detects installed clients and writes only their matching global targets:

```powershell
ue-cli install-skills
```

| Target | Installed when detected |
| --- | --- |
| `%USERPROFILE%\.agents\skills\ue-cli` | Codex, Cursor, GitHub Copilot, Windsurf, or OpenCode |
| `%USERPROFILE%\.claude\skills\ue-cli` | Claude Code |
| `%USERPROFILE%\.codebuddy\agents\ue-cli` | CodeBuddy |
| `%USERPROFILE%\.gemini\skills\ue-cli` | Gemini CLI |

The shared `.agents` location follows the Agent Skills convention. If no supported client or initialized skill directory is detected, the command reports every target as `skipped` and creates no skill target. Run a newly installed client once so its profile directory exists, or explicitly install every built-in target:

```powershell
ue-cli install-skills --all-targets
```

Existing `ue-cli` target directories are replaced, including custom `--target` directories. Other skills beside the `ue-cli` directory are not changed.

For an agent with a different skill-directory convention, install directly to its exact `ue-cli` directory. The final directory name must be `ue-cli`; this prevents accidentally replacing an entire skills root:

```powershell
ue-cli install-skills --target "C:/path/expected/by/your-agent/ue-cli"
```

`--target` is repeatable. Restart or reload the agent after installation.

## Prompt an Agent

With the skill installed:

```text
Use the ue-cli skill with project F:/path/to/MyProject.uproject.
Analyze /Game/MyMaterial, fix confirmed issues, then capture before/after screenshots.
```

Without the skill:

```text
Use ue-cli with project F:/path/to/MyProject.uproject.
Run ue-cli --help and command-specific --help before choosing commands.
```

## Common Commands

Global options such as `--output`, `--project`, and `--port` must appear before the subcommand.

```powershell
$Project = "F:/path/to/MyProject.uproject"

ue-cli --help
ue-cli --list-commands
ue-cli --output json --project "$Project" editor status
ue-cli --output json --project "$Project" editor api-discover MaterialEditingLibrary -q connect
ue-cli --output json --project "$Project" project config set DefaultEngine.ini /Script/Engine.RendererSettings r.DefaultFeature.MotionBlur False
ue-cli --output json --project "$Project" material analyze /Game/MyMaterial
ue-cli --output json --project "$Project" material get-param /Game/MyMaterialInstance --name Roughness
ue-cli --output json --project "$Project" material shader-source /Game/MyMaterial
ue-cli --output json --project "$Project" screenshot capture --path "F:/output/material_check.png"
ue-cli --output json --project "$Project" screenshot capture --path "F:/output/stat_evidence.png" --include-ui
ue-cli --output json --project "$Project" scene property "<StaticMeshComponentPath>" "LODData[0].PaintedVertices"
ue-cli --output json --project "$Project" scene property "<PostProcessVolumePath>" "Settings.WeightedBlendables.Array"
ue-cli --output json --project "$Project" editor run-script --no-save -c 'result={"label":"quoted value"}'
ue-cli --output json --project "$Project" editor cvar get r.VSync --timeout 10
ue-cli --output json --project "$Project" editor live-coding-compile --timeout 600
```

Use Unreal virtual paths such as `/Game/MyMaterial` for assets, not filesystem paths to `.uasset` files.
For `project config get` and `project config set`, `CONFIG_NAME` accepts a bare category such as `Engine`, a config stem such as `DefaultEngine`, or a standard filename such as `DefaultEngine.ini`. These forms resolve to the same project config when it exists; paths and non-`.ini` extensions are rejected.
`asset duplicate` saves the duplicated package before reporting success, including World assets stored as `.umap`. A duplicate that cannot be saved returns nonzero `ASSET_DUPLICATE_FAILED` with `saved=false` instead of reporting a transient in-memory success.
`asset rename` waits up to 120 seconds by default for reference-heavy moves; override this with `--timeout <seconds>`. If the HTTP response times out, ue-cli safely checks source and destination existence. A conclusive moved state returns confirmed success. An inconclusive check returns nonzero `ASSET_RENAME_TIMEOUT`, `completion_state=unknown`, `retry_safe=false`, plus source/destination verification commands; do not retry until those checks prove the source still exists and destination does not.
`editor api-discover` cross-checks reflected functions against the live UE Python wrapper. It resolves standard `Kismet*` reflection names through their shorter Python aliases in both directions (for example, `KismetSystemLibrary` and `SystemLibrary` use `unreal.SystemLibrary`). Detailed function items report `python_callable`, plus `python_name` and `python_path` when a matching binding exists. Filtered summaries list reflection-only entries in `python_unavailable_functions`; class-level `python_exposed` does not imply every reflected function is callable.
Bridge 1.34 lets `scene property` read StaticMeshComponent instance-paint fields through `LODData[N].PaintedVertices` and `LODData[N].OverrideVertexColors`. Those fields are native, non-reflected data; writes remain unsupported.
`scene property` reads `Settings.WeightedBlendables.Array` on PostProcessVolume actors as structured `weight` and object-path entries through Unreal Python. This nested expression is read-only.
`material get-param` returns the effective scalar, vector, texture, or static-switch value, including values inherited from parent material instances or materials. `material set-param` reports success only after effective-value readback matches the request; use `applied` / `readback_match` as the authoritative outcome. Unreal's raw `set_return` is retained for diagnostics with `set_return_authoritative=false` because supported engine implementations can return false after applying the value.
`material info` supports Material, MaterialFunction, and MaterialInstanceConstant assets. Bridge 1.29 reads these assets directly through Remote Control, including graph edges, outputs, textures, and instance parameters, without creating Python expression wrappers. A stale bridge returns `MATERIAL_INFO_BRIDGE_REQUIRED` with an upgrade command.

`material get-errors` supports Material, MaterialInstanceConstant, and MaterialFunction assets. Bridge 1.33 checks MaterialFunction errors through a transient preview material without saving or modifying the function asset. A stale bridge returns `MATERIAL_FUNCTION_ERRORS_BRIDGE_UPGRADE_REQUIRED` with an upgrade command.
`material hlsl-code` supports Material and MaterialInstanceConstant assets. MaterialFunction assets return `MATERIAL_HLSL_CODE_UNSUPPORTED_ASSET`; they are not misreported as a missing Bridge.
`material analyze` and `material get-stats` reject MaterialInstanceConstant assets with `MATERIAL_ANALYZE_UNSUPPORTED_CLASS` and `MATERIAL_STATS_UNSUPPORTED_CLASS`, respectively; parent-graph counts are not reported as effective compiled-instance statistics.
Material inspection commands never save packages. Bridge 1.32 runs graph mutations in native C++ and saves only their explicitly targeted package, leaving unrelated dirty work untouched. Material expression UObjects never cross into Python. Bridge 1.35 resolves Python-style `snake_case` names passed to `material add-node --set` (for example, `parameter_name` and `default_value`) to native reflected fields. If any requested property or Custom input cannot be applied, it returns `MATERIAL_NODE_PROPERTIES_UNAPPLIED`, removes any partial node, and does not save the asset. When `material connect` targets a `MaterialExpressionSetMaterialAttributes` node, it safely creates the requested attribute input before connecting it; for example, use `--to-input WorldPositionOffset`. Do not write `AttributeSetTypes` or `Inputs` directly: Unreal keeps them as parallel arrays, and an independent `set_editor_property` write can crash the editor. `material add-node --set` rejects those raw properties with `MATERIAL_SET_ATTRIBUTES_UNSAFE_PROPERTY`.
`material info`, `material analyze`, and `material get-graph` recognize Material Attributes connections as material outputs and trace their upstream graph.
`material shader-source` refreshes changed engine `.usf`/`.ush` files before synchronous extraction. Success reports `shader_cache_refresh=changed`; an empty extraction returns `MATERIAL_SHADER_SOURCE_FAILED` instead of stale success.
`material dump-hlsl` rejects an inactive requested shader platform before recompiling, preserves the material package's original dirty flag, and returns a nonzero structured error when no dump or matching shader stage is available. Material instances resolve through their actual shader-map material: UE5 uses the exact hashed debug group so another static permutation is not selected, while UE4.26 uses its base-material dump directory. Successful instance results include `shader_map_material`, `shader_dump_name`, and `shader_debug_group`. UE5 viewport previews are recognized as active platforms; use `--platform vulkan_sm5_android_preview` or `--platform vulkan_es31_android_preview` for Android Vulkan preview dumps. After recompilation it waits up to 10 seconds for dump files by default; use `--wait-timeout <seconds>` only when a large material is still compiling. Missing-dump details include the exact search root and candidate count.
On Windows PowerShell, `editor run-script -c` preserves double-quoted Python string literals that native argument parsing would otherwise strip. Use stdin (`editor run-script -`) or a `.py` file for multiline code.
A `.py` file passed to `editor run-script` executes with `__name__ == "__main__"`, an absolute `__file__`, and its containing directory temporarily at `sys.path[0]`. Guarded entrypoints and sibling imports therefore work like direct Python file execution; the editor's prior `sys.path` is restored afterward.
Each `editor run-script` invocation receives fresh globals. Deferred Unreal callbacks retain that invocation's globals after the command returns; store their handles and unregister them when finished because later callback failures appear only in the editor log.
`editor run-script` waits 30 seconds by default. Set `--timeout <seconds>` for long asset scans. When the project log is available, a timeout creates a read-only observation task from unique wrapper begin/end markers. `EDITOR_SCRIPT_IN_PROGRESS` confirms delivery and includes a `task_id`; `EDITOR_SCRIPT_TIMEOUT` can also include the handle while delivery evidence remains unknown. Poll with `ue-cli task status <task_id>` or `ue-cli task wait <task_id> --timeout <seconds>` without resending the script. The task exposes `awaiting_delivery_evidence`, `executing`, or terminal `exited` phase and returns the late structured script result. Observation tasks cannot be cancelled because cancellation could not safely stop Python already executing on Unreal's game thread. Without a usable project log, `EDITOR_SCRIPT_TIMEOUT` keeps `completion_state=unknown` and retains the existing manual log/status guidance. File-backed scripts also receive an exact `details.retry_command` with a doubled timeout (at least 60 seconds), for use only after the prior execution is known terminal and safe to repeat.
`--no-save` skips ue-cli's post-script dirty-package save only. It cannot stop the script or an Unreal API from saving. For example, `EditorLevelLibrary.new_level('/Temp/Name')` may write `Saved/Name.umap`; use a unique name and treat that file as explicit script output.
If the editor connection resets, `EDITOR_CONNECTION_LOST` keeps script delivery unknown and unsafe to replay. When the selected editor exits or writes new fatal/assert lines during the call, details include the editor PID, process status or exit code when available, configured log path, and a bounded `fatal_log_tail`.
Top-level map transitions through `EditorLoadingAndSavingUtils.new_blank_map`, `EditorLoadingAndSavingUtils.load_map`, or `EditorLevelLibrary.load_level` are blocked with `UNSAFE_RUN_SCRIPT_OPERATION`. These calls can retain a World through Python or project subsystem references and terminate the editor with `World Memory Leaks`, including when a script duplicates the active World and immediately loads that duplicate. Use `editor new-blank-level` for an unsaved `/Temp/Untitled_*` world, `editor new-level` for a persistent asset, or `editor open-level` for an existing asset. `editor new-blank-level` calls native `NewBlankMap(false)` through Remote Control, verifies the active transient world, then lets the next separate `editor run-script --no-save` bind to that active world normally. It rejects dirty map packages unless `--discard-dirty-map` explicitly authorizes losing them; it never saves the blank world. `editor open-level --timeout <seconds>` waits up to 300 seconds by default for the synchronous `LoadLevel` request, so large maps can select a larger bound. If it expires, `EDITOR_OPEN_LEVEL_TIMEOUT` reports unknown completion and tells callers not to retry or launch a duplicate editor while the existing process may still be loading. To reload the currently active map from disk, use `editor open-level /Game/Path/Level --reload --discard-unsaved`. Both flags are required because this restarts the project editor, discards all unsaved editor packages, and relaunches directly on the verified active map instead of performing a crash-prone in-process teardown. Startup may return a pollable `launching` task; use its `next_command` until completion. This restart path works on UE4.26 and UE5. UE4.26 does not expose `LevelEditorSubsystem.LoadLevel`, so ordinary unloaded targets return `EDITOR_OPEN_LEVEL_UNSUPPORTED` before dispatch, with a safe `editor close` plus `editor launch --map` workflow. `editor open-level` rejects an already-loaded, non-active target World with `EDITOR_OPEN_LEVEL_UNSAFE_LOADED_WORLD` before dispatching `LoadLevel`. This state commonly follows `asset duplicate` on a World. Save the duplicate without loading it, close the editor, then start a fresh editor with `editor launch --map /Game/Path/Level` before continuing setup.
`editor run-script` executes inside Unreal Editor, so `--no-save` does not sandbox native engine assertions. In particular, do not probe `StaticMeshDescription.get_vertex_instance_uv(...)` with increasing channel indices and catch Python exceptions: an out-of-range channel can trigger an Unreal `check()` and terminate the editor. Query `StaticMeshEditorSubsystem.get_num_uv_channels(static_mesh, lod_index)` first on UE5, or `EditorStaticMeshLibrary.get_num_uv_channels(...)` on UE4.26, then require `0 <= channel_index < channel_count` before reading the MeshDescription. UE4.26 can report the count but does not expose `StaticMesh.get_static_mesh_description`; treat that read path as unsupported instead of guessing another binding.
`editor cvar get` confirms the selected editor's TCP listener, then uses the remaining timeout for the requested CVar. It does not spend the budget on a duplicate functional readiness call. Query timeouts report the actual request stage; verified missing CVars return `CVAR_NOT_FOUND`.
`editor exec --timeout <seconds>` waits 15 seconds by default and never redispatches a command after an ambiguous transport failure. When the project log is available, a timeout creates a read-only observation task from the command's unique begin/end log markers. `EDITOR_EXEC_IN_PROGRESS` confirms delivery and includes a `task_id`; `EDITOR_EXEC_DELIVERY_UNKNOWN` can also include that handle while delivery evidence is still pending. Poll with `ue-cli task status <task_id>` or `ue-cli task wait <task_id> --timeout <seconds>` without resending the command. The observation task reports terminal Unreal Python errors and cannot be cancelled because cancellation could not safely stop the already-dispatched editor command. Without a usable project log, delivery remains unknown; inspect editor state and logs before retrying because a non-idempotent command may already have run. Inline command logs are bounded; `omitted_line_count` reports omitted lines and `log_file` retains complete diagnostics. Marker-delimited file capture waits for the matching end marker within `--log-wait`, so a briefly delayed log flush does not produce false empty output. `Automation RunTests` waits up to 300 seconds by default for `Automation Test Queue Empty`; pass `--log-wait <seconds>` to override that bound. Other console commands retain the 1-second log-capture default. Automation commands lower Unreal's runtime-only interactive-FPS gate to 1 FPS without saving project configuration, so background agent runs do not require human window focus. Engines such as UE4.26 that do not expose this setting still dispatch the Automation command without the runtime FPS override.
On Windows UE5, `editor live-coding-compile` invokes Unreal's synchronous `LiveCoding.CompileSync`, waits for `success`, `no_changes`, `failed`, or `cancelled`, and returns that terminal state. A timeout remains unknown and is never retried. Editor exits include PID and bounded fatal-log evidence when available. UE4 returns `LIVECODING_SYNC_UNSUPPORTED` before dispatch because UE4.26 has no synchronous Live Coding result API.

## Multiple Editors

Discover all running instances:

```powershell
ue-cli --output json editor status --all
```

Select one editor by project or port:

```powershell
ue-cli --output json --project "F:/ProjectA/ProjectA.uproject" editor status
ue-cli --output json --port 30011 material list
```

When `--port` is omitted, ue-cli uses the selected project's engine-specific Remote Control config or one unambiguous live editor. UE5 reads `DefaultRemoteControl.ini`; UE4 reads `DefaultWebRemoteControl.ini`. `editor launch` persists the selected port to that same file before spawning the editor, so readiness and Unreal bind the same port. Multiple matching editors produce a structured ambiguity error instead of choosing silently. A directory containing multiple `.uproject` files also requires explicit `--project`. For every project-bound editor request on Windows, the listening-port PID must be known and its process command line must match that project. A transient missing process-discovery result receives two short bounded retries; unknown or mismatched ownership still fails before sending the request.

`editor status` applies a 15-second discovery deadline by default; the root `status` alias accepts the same `--timeout <seconds>` option. An exhausted deadline returns `EDITOR_STATUS_TIMEOUT` with the `blocking_phase` and the task snapshot being scanned, when known. Project-scoped status filters unrelated Unreal processes before per-process task and Bridge enrichment; use `--all` to inspect every project. Launch discovery reads atomically published task snapshots before locking only matching launch candidates, so an unrelated busy task cannot hide a known live launch. When a matching launch task is locked, status retains `launching` and marks its last published snapshot as potentially stale.

`editor status` reports `unreachable` when `/remote/info` answers but the functional editor health probe fails, instead of claiming the session is online or that the Bridge is missing. If an editor command finds the port still listening while the API is unresponsive, `EDITOR_UNREACHABLE` recommends waiting, checking status, then closing or restarting that existing session rather than launching a second editor.

`status` is a root alias for `editor status`; it accepts the same `--all`, `--scan-range`, `--timeout`, `--project`, and optional task ID arguments.

`close` is a root alias for `editor close`; it preserves the same project selection and `--save-dirty` / `--force` safety behavior.

`editor close` targets the verified editor for the selected project. Without `--project`, it captures the process owning the selected Remote Control port. By default it closes only a clean editor; dirty map/content packages return `EDITOR_DIRTY_PACKAGES` with their paths, and nothing is saved or closed. Use `--save-dirty` to explicitly move dirty `/Temp/` maps to deterministic `/Game/__UeCliAutoSave_<name>` assets, save every remaining dirty package, require Unreal to confirm the save, then close. The separate `--force` escape hatch allows data loss and offline/stale process termination and should be used only when the request already authorizes that outcome. `--save-dirty` and `--force` cannot be combined. After `taskkill`, ue-cli uses a bounded native recheck of the exact target PID, so a child-process error does not turn a confirmed editor exit into `EDITOR_CLOSE_FAILED`. Unknown/offline state and same-project stale peers remain machine-readable failures because their dirty state cannot be checked safely.

Graceful `editor close` requests the normal Slate/MainFrame shutdown so open asset editors close before editor subsystems are destroyed (UE 4.26 and 5.x). It captures the target process handle and current log offset before shutdown. New fatal log evidence or an observed nonzero unforced exit returns `EDITOR_CLOSE_CRASHED` instead of successful `closed`; historical log errors are excluded. Successful responses include `shutdown_evidence` with available process/log observations; unavailable exit-code evidence does not prove a clean exit.

## Output Contract

Non-interactive callers receive JSON by default. `--output json` forces it. JSON mode writes one final machine-readable payload to stdout; progress, heartbeats, and diagnostics go to stderr.

Runners should either stream stdout live or replay captured stdout once, never both.

Failed cook/package results prioritize terminal plugin-load failures over compiler-like lines from earlier phases. These failures return `code=BUILD_PLUGIN_LOAD_FAILED`, `failure_kind=plugin_load_failure`, the primary `diagnostic`, plugin/module names when available, and `phase=cook` when the UAT log identifies Cook as the failing phase.

Android packaging failures containing Gradle's `java.io.IOException: Unable to establish loopback connection` return `code=BUILD_GRADLE_LOOPBACK_FAILED`, `failure_kind=gradle_loopback_connection`, `failure_scope=local_environment`, and retain the failed Gradle task. The result remains an error. It marks the failure as transient and safe to retry; when the failed command already used `--uat-arg=-iterate` after a completed Cook, the retry guidance explicitly preserves that option so unchanged cooked outputs can be reused. Repeated failures should be investigated in local Gradle daemons, firewall, antivirus, proxy, and loopback policy.

When UBT cannot start because another process owns the same engine's global build mutex, `build compile` returns `code=BUILD_CONFLICTING_INSTANCE`, `failure_kind=ubt_mutex_conflict`, the original UBT diagnostic, and a wait-or-stop recovery hint. UE5 results also include `mutex_name` when UBT reports it. This classification uses explicit UBT conflict evidence rather than AutomationTool's overloaded exit code, which may be labeled `Error_SDKNotFound` even when no SDK problem occurred.

MSVC PCH failures caused by exhausted Windows virtual memory return `code=BUILD_RESOURCE_EXHAUSTED`, `failure_kind=compiler_virtual_memory_exhaustion`, compiler diagnostics, and pagefile error code 1455 when present. When a `bk_dist` log reports its job count and UBT's lower memory-based action limit, the result preserves both values and directs the operator to reduce the executor's own job limit or disable it; ue-cli does not claim that UBT's local limit controls a custom distributed executor.

`build compile` infers the Editor target from `--project`; do not pass `--target`. If an explicit target is supplied, ue-cli returns `BUILD_TARGET_INFERRED` with the accepted replacement command. `--config` and `--configuration` are equivalent.

`build compile --module NAME` is a focused project or engine-core module build. If the current Editor target receipt identifies `NAME` as an Engine plugin module, ue-cli rejects the command before UBT with `ENGINE_PLUGIN_MODULE_UNSUPPORTED` and provides a full `build compile` recovery command. Project-targeted UBT can omit Engine plugin output actions and otherwise fail with `Unable to find output items for module`.

## Features

- **Project management:** parse `.uproject`, inspect `.ini`, list content assets
- **Build system:** compile, cook, and package through UAT/UBT
- **Material analysis:** list, inspect, analyze, and edit materials
- **Blueprint and scene tools:** inspect and edit Blueprints, actors, and components
- **Screenshots:** capture the viewport, include UI overlays, compare images, run CVar A/B checks
- **Editor control:** launch, status, console commands, CVars, and Python scripts

## Architecture

Three communication tiers:

1. **UAT/UBT subprocess:** build, cook, and package without a running editor.
2. **Remote Control HTTP:** query and edit editor state through the selected HTTP port.
3. **Editor Python injection:** execute Unreal Python through Remote Control when no dedicated subcommand exists.

## Known Engine Bugs

UE Remote Control and Python have version-specific automation bugs. See [ENGINE_BUGS.md](ENGINE_BUGS.md) for known issues, workarounds, and engine-source fixes.

## Problems and Support

Report reproducible ue-cli problems at [PZZZB/ue-cli issues](https://github.com/PZZZB/ue-cli/issues). Include ue-cli version, Unreal version, exact command, expected behavior, actual behavior, minimal reproduction, and sanitized logs.
