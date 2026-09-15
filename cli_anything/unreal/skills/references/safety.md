# Safety Patterns & Destructive Operation Guards

Read before deleting assets, overwriting files, creating `.uasset`, or doing irreversible engine-state changes.

## Forbidden Patterns

Benchmark failures: locks, corruption, modal dialogs, wasted turns.

| Forbidden | Use Instead | Why |
|-----------|-------------|-----|
| `rm /path/to/DLL` or `del *.dll` | `build compile` or `editor close` | Editor locks DLLs; deletion corrupts build |
| `rm /path/to/Asset.uasset` | `asset delete --force` | Bypasses reference tracking; corrupts Content Browser cache |
| `sleep 60 && editor status` | `editor launch` | Blind polling wastes turns; launch waits for API |
| `os.remove()` for .uasset in Python | `EditorAssetLibrary.delete_asset()` | Direct deletion corrupts in-memory asset registry |
| `taskkill` / `kill` editor process | `editor close` | Dirty shutdown leaves locks/corrupt state |
| Editing `.ini` config files directly | `project config set` | CLI preserves UE formatting/reload assumptions |
| `LevelEditorSubsystem.new_level`, `EditorLoadingAndSavingUtils.new_blank_map`/`load_map`, or `save_current_level` in Python | `editor new-blank-level`, `editor new-level`, `editor open-level`, or `editor save-level`; if `open-level` rejects an already-loaded target World, close and use `editor launch --map` | UE world transition / HTTP tick-thread crash risk |
| Probe `StaticMeshDescription.get_vertex_instance_uv(...)` until Python raises | Query `StaticMeshEditorSubsystem.get_num_uv_channels()` on UE5 or `EditorStaticMeshLibrary.get_num_uv_channels()` on UE4.26, then bounds-check | Native out-of-range `check()` can terminate Editor; `--no-save` is not a sandbox |

**General rule:** use CLI for UE operations, except the scoped startup-recovery UI handling below. Direct file manipulation bypasses locks/reference tracking.

## Modal Dialogs Block CLI Execution

Any modal dialog can block Remote Control. For agent-owned interactive work, arm the bounded confirmation broker before risky operations; see `workflows-editor.md` "Active Confirmation Polling". It handles standard `FMessageDialog` only. Do not rely on it for startup recovery, custom Slate, platform, or third-party windows.

| Trigger | Prevention |
|---------|------------|
| `new_level(path)` when level exists | Delete + `collect_garbage()` first |
| `save_asset()` with dirty referencers | Use `--no-save`, handle saves explicitly |
| `import_asset()` name conflict | Delete existing + `collect_garbage()` first |
| `create_asset()` / `duplicate_asset()` target exists | Use overwrite workflow below |
| Any `unreal.EditorDialog` call | Never use in headless scripts |

For confirmation commands, leases, and blocked-window handling, follow [Active Confirmation Polling](workflows-editor.md#active-confirmation-polling). Check the resulting editor state before retrying an operation: side effects may already have occurred.

### Restore Packages after an agent-managed restart

If the Agent started and closed the previous editor session, it may handle **Restore Packages** on the next launch without asking again. Verify the project and current PID/window against the launch record, and inspect the listed packages. Restore needed unsaved work with **Restore Selected**; choose **Skip Restore** only when the task already permits discarding that recovery data. Starting and closing the editor alone does not authorize discarding user changes. Ask only when ownership or the intended choice is unclear, and honor earlier recovery or discard instructions.

When discarding recovery is already authorized before launch, prefer `editor launch --skip-restore`, which uses Unreal's native decline path. For an existing recovery window, use UI automation; `confirmation answer` cannot answer `source=window`, `answerable=false` items. Then poll the existing launch task and verify readiness. Keep normal startup interactive: `--unattended` suppresses dialogs and cannot select a recovery action.

## Asset Overwrite Avoidance

`create_asset` / `duplicate_asset` can pop "Overwrite Existing Object" if target already loaded. Delete, GC, then create:

```python
import unreal
EAL = unreal.EditorAssetLibrary
target = "/Game/MyAsset"
can_create = True
if EAL.does_asset_exist(target):
    if EAL.delete_asset(target):           # Returns True if fully deleted
        unreal.SystemLibrary.collect_garbage()  # Flush old UObject from memory
    else:
        can_create = False                 # Delete failed - do NOT create

if can_create:
    ATH = unreal.AssetToolsHelpers.get_asset_tools()
    new_asset = ATH.create_asset(...)
```

## Silent Failures - C++ Errors Without Python Exceptions

Some UE C++ failures do not raise Python exceptions. If script says success but result wrong, check recent engine errors:

```bash
ue-cli editor run-script -c "import unreal; result = list(unreal.CliAnythingBridgeLibrary.get_recent_engine_errors(10))"
```

## Test Agent - Asset Validation Requirements

For asset verification, disk `.uasset` existence is not enough; corrupted file can still exist.

**First step of any asset test:** load through engine API:

```python
import unreal
path = "/Game/MyNewAsset"
asset = unreal.EditorAssetLibrary.load_asset(path)
if not asset:
    unreal.log_error(f"Asset at {path} exists on disk but FAILED to load. It may be corrupted.")
else:
    unreal.log(f"Successfully loaded asset: {asset.get_name()}")
```

If `load_asset` returns `None`, fail test and report corruption to Dev agent.
