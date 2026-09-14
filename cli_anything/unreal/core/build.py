"""Build system wrapper for Unreal Engine."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from cli_anything.unreal.utils.ue_backend import (
    BuildProcessProbeError,
    _build_output_encoding,
    find_engine_root,
    find_generate_project_files,
    find_running_build_processes,
    get_editor_binary_prefix,
    get_engine_version,
    kill_build_processes,
    run_build,
    run_uat,
)


_UNSAFE_PACKAGE_VALUE_CHARS = frozenset('"&|<>\0\r\n')
_GAME_TARGET_PATTERN = re.compile(
    r"\bType\s*=\s*TargetType\s*\.\s*Game\s*;"
)
_EDITOR_TARGET_PATTERN = re.compile(
    r"\bType\s*=\s*TargetType\s*\.\s*Editor\s*;"
)
_MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PE_BUILD_PRODUCT_TYPES = frozenset({"dynamiclibrary", "executable"})
_PE_BUILD_PRODUCT_SUFFIXES = frozenset({".dll", ".exe"})
_MAX_REPORTED_INVALID_BUILD_PRODUCTS = 20
_MAX_REPORTED_MISSING_RUNTIME_DEPENDENCIES = 20
_MAX_REPORTED_MISSING_INCLUDES = 20
_BUILD_STATE_PROCESS_PROBE_TIMEOUT_SECONDS = 3
_ENGINE_PROVENANCE_TIMEOUT_SECONDS = 3

_MSVC_MISSING_INCLUDE_PATTERN = re.compile(
    r"^(?P<referenced_by>.+?)"
    r"\((?P<line>\d+)(?:,(?P<column>\d+))?\):\s*"
    r"fatal error C1083:\s*(?P<message>.+)$",
    re.IGNORECASE,
)
_MSVC_MISSING_INCLUDE_MESSAGE_PATTERN = re.compile(
    r"cannot open include file|无法打开包括文件",
    re.IGNORECASE,
)
_QUOTED_INCLUDE_PATTERN = re.compile(
    r"""["'“‘](?P<include>[^"'“”‘’]+)["'”’]"""
)
_PLUGIN_LOAD_FAILURE_PATTERN = re.compile(
    r"\bPlugin\s+['\"](?P<plugin>[^'\"]+)['\"]\s+failed to load\b",
    re.IGNORECASE,
)
_PLUGIN_LOAD_MODULE_PATTERN = re.compile(
    r"\bmodule\s+['\"](?P<module>[^'\"]+)['\"]\s+could not be found\b",
    re.IGNORECASE,
)
_COOK_FAILURE_PATTERN = re.compile(
    r"\bCook failed\b|\bError_UnknownCookFailure\b",
    re.IGNORECASE,
)
_UBT_CONFLICTING_INSTANCE_PATTERN = re.compile(
    r"\bResult:\s*Failed\s*\(ConflictingInstance\)\s*$",
    re.IGNORECASE,
)
_UBT_CONFLICTING_MUTEX_PATTERN = re.compile(
    r"\bA conflicting instance of "
    r"(?P<mutex>Global\\UnrealBuildTool_Mutex_[0-9a-f]+) "
    r"is already running\.",
    re.IGNORECASE,
)
_UBT_CONFLICTING_LEGACY_PATTERN = re.compile(
    r"\bA conflicting instance of UnrealBuildTool is already running\.\s*$",
    re.IGNORECASE,
)
_MSVC_VIRTUAL_MEMORY_EXHAUSTION_PATTERN = re.compile(
    r"\berror C3859\b|"
    r"(?:system returned (?:error )?code|\u7cfb\u7edf\u8fd4\u56de\u4ee3\u7801)\s*:?\s*1455\b|"
    r"(?:paging file.{0,80}too small|\u9875\u9762\u6587\u4ef6.{0,80}\u592a\u5c0f)",
    re.IGNORECASE,
)
_WINDOWS_PAGEFILE_ERROR_PATTERN = re.compile(
    r"(?:system returned (?:error )?code|\u7cfb\u7edf\u8fd4\u56de\u4ee3\u7801)\s*:?\s*1455\b|"
    r"\bc1xx\b[^\r\n]{0,120}\b1455\b",
    re.IGNORECASE,
)
_UBT_PARALLEL_ACTION_LIMIT_PATTERN = re.compile(
    r"limiting max parallel actions to\s+(?P<limit>\d+)",
    re.IGNORECASE,
)
_BK_DIST_JOB_COUNT_PATTERN = re.compile(
    r"Building\s+(?P<actions>\d+)\s+actions with\s+(?P<jobs>\d+)\s+jobs",
    re.IGNORECASE,
)
_GRADLE_LOOPBACK_FAILURE_PATTERN = re.compile(
    r"java\.io\.IOException:\s*Unable to establish loopback connection",
    re.IGNORECASE,
)
_GRADLE_PACKAGE_CONTEXT_PATTERN = re.compile(
    r"\bMaking \.apk with Gradle\b|\brungradle\.bat\b",
    re.IGNORECASE,
)
_GRADLE_TASK_PATTERN = re.compile(
    r"rungradle\.bat[\"']?\s+(?P<task>:[^\s]+)",
    re.IGNORECASE,
)

_TARGET_LEXICAL_NOISE_PATTERN = re.compile(
    r"//[^\r\n]*|/\*.*?\*/|"
    r'@"(?:""|[^"])*"|'
    r'"(?:\\.|[^"\\\r\n])*"|'
    r"'(?:\\.|[^'\\\r\n])*'",
    re.DOTALL,
)


def validate_package_uat_value(
    value: str,
    *,
    label: str,
    require_option: bool = False,
    require_value: bool = False,
) -> str:
    """Validate a package option before it reaches the Windows UAT wrapper."""
    if require_value and not value:
        raise ValueError(f"{label} must not be empty")
    if require_option and not value.startswith("-"):
        raise ValueError(
            f"{label} must start with '-' (for example --uat-arg=-pak)"
        )
    if any(char in _UNSAFE_PACKAGE_VALUE_CHARS for char in value):
        raise ValueError(
            f"Unsafe {label}: literal quotes, shell control characters, and "
            "NUL/CR/LF are not allowed"
        )
    return value


def validate_cook_package(value: str) -> str:
    """Validate one package before combining UE's ``+``-separated list."""
    value = validate_package_uat_value(
        value,
        label="cook package",
        require_value=True,
    )
    if "+" in value:
        raise ValueError("Cook package must not contain '+'")
    return value


def validate_cook_ini_override(value: str) -> str:
    """Validate one ini override before adding the native prefix."""
    value = validate_package_uat_value(
        value,
        label="ini override",
        require_value=True,
    )
    if value.lower().startswith("-ini:"):
        raise ValueError("ini override must omit the '-ini:' prefix")
    return value


def _is_dangling_link(path: Path) -> bool:
    """Return whether a symlink or Windows junction exists but its target does not."""
    return os.path.lexists(path) and not os.path.exists(path)


def _find_dangling_package_paths(
    uproject_path: str,
    platform: str,
    output_dir: str,
) -> list[str]:
    project_dir = Path(uproject_path).parent
    writable_paths = (
        project_dir / "Saved",
        project_dir / "Saved" / "Shaders",
        project_dir / "Saved" / "Cooked" / platform,
        project_dir / "Saved" / "StagedBuilds" / platform,
        project_dir / "DerivedDataCache",
        project_dir / "Intermediate" / platform,
        Path(output_dir),
    )

    dangling: list[str] = []
    checked: set[str] = set()
    for path in writable_paths:
        if not path.is_absolute():
            path = Path.cwd() / path
        for candidate in (path, *path.parents):
            key = os.path.normcase(os.path.abspath(candidate))
            if key in checked:
                continue
            checked.add(key)
            if _is_dangling_link(candidate):
                dangling.append(str(candidate))
    return dangling


def _sanitize_target_source(source: str) -> str:
    def preserve_newlines(value: str) -> str:
        return "".join(char if char in "\r\n" else " " for char in value)

    source = _TARGET_LEXICAL_NOISE_PATTERN.sub(
        lambda match: preserve_newlines(match.group(0)),
        source,
    )
    conditional_depth = 0
    sanitized_lines = []
    for line in source.splitlines(keepends=True):
        directive = re.match(r"^\s*#\s*(if|endif)\b", line)
        if directive:
            if directive.group(1) == "if":
                conditional_depth += 1
            elif conditional_depth:
                conditional_depth -= 1
            sanitized_lines.append(preserve_newlines(line))
        elif conditional_depth:
            sanitized_lines.append(preserve_newlines(line))
        else:
            sanitized_lines.append(line)
    return "".join(sanitized_lines)


def _resolve_game_target(uproject_path: str) -> tuple[str | None, str | None]:
    project_path = Path(uproject_path)
    source_dir = project_path.parent / "Source"
    game_targets = []
    if source_dir.is_dir():
        for target_file in sorted(source_dir.rglob("*.Target.cs")):
            try:
                source = target_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return None, f"Could not read target file {target_file}: {exc}"
            if _GAME_TARGET_PATTERN.search(_sanitize_target_source(source)):
                game_targets.append(target_file.name.removesuffix(".Target.cs"))

    game_targets = sorted(set(game_targets))
    if len(game_targets) > 1:
        return None, (
            "Multiple Game targets found; cannot choose one for compile: "
            + ", ".join(game_targets)
        )
    if game_targets:
        return game_targets[0], None
    return project_path.stem, None


def _find_project_editor_target(
    uproject_path: str,
) -> tuple[str | None, str | None]:
    project_path = Path(uproject_path)
    source_dir = project_path.parent / "Source"
    editor_targets = []
    if source_dir.is_dir():
        for target_file in sorted(source_dir.rglob("*.Target.cs")):
            try:
                source = target_file.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError as exc:
                return None, f"Could not read target file {target_file}: {exc}"
            if _EDITOR_TARGET_PATTERN.search(_sanitize_target_source(source)):
                editor_targets.append(
                    target_file.name.removesuffix(".Target.cs")
                )

    editor_targets = sorted(set(editor_targets))
    if len(editor_targets) > 1:
        return None, (
            "Multiple Editor targets found; cannot choose one for module compile: "
            + ", ".join(editor_targets)
        )
    if editor_targets:
        return editor_targets[0], None
    return None, None


def _resolve_editor_target(uproject_path: str) -> tuple[str | None, str | None]:
    project_target, target_error = _find_project_editor_target(uproject_path)
    if project_target or target_error:
        return project_target, target_error
    project_path = Path(uproject_path)
    return project_path.stem + "Editor", None


def validate_module_name(value: str) -> str:
    if not _MODULE_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"Invalid module name {value!r}; expected a C++ identifier such as Renderer"
        )
    return value


def _find_module_hot_reload_state_files(
    uproject_path: str,
    target: str,
    platform: str,
    config: str,
) -> list[Path]:
    """Find UBT state that a module-only build must preserve."""
    build_root = Path(uproject_path).parent / "Intermediate" / "Build" / platform
    if not build_root.is_dir():
        return []

    matches = []
    for state_file in build_root.rglob("HotReloadState.bin"):
        relative_parts = state_file.relative_to(build_root).parts
        if len(relative_parts) < 3:
            continue
        if (
            relative_parts[-3].casefold() == target.casefold()
            and relative_parts[-2].casefold() == config.casefold()
        ):
            matches.append(state_file)
    return sorted(matches)


def _check_already_building(uproject_path: str) -> dict | None:
    processes = find_running_build_processes(uproject_path)
    if not processes:
        return None
    return {
        "status": "error",
        "error": "Build already in progress for this project.",
        "running_processes": processes,
    }


def _extract_msvc_missing_includes(diagnostics: list[str]) -> list[dict]:
    """Return structured evidence for MSVC C1083 missing-include failures."""
    missing_includes = []
    seen = set()
    for diagnostic in diagnostics:
        match = _MSVC_MISSING_INCLUDE_PATTERN.match(diagnostic)
        if not match:
            continue
        message = match.group("message")
        if not _MSVC_MISSING_INCLUDE_MESSAGE_PATTERN.search(message):
            continue
        include_match = _QUOTED_INCLUDE_PATTERN.search(message)
        if include_match:
            include = include_match.group("include").strip()
        else:
            include_match = re.search(
                r"(?:include file|包括文件)\s*[:：]\s*"
                r"(?P<include>.+?)\s*[:：]\s*",
                message,
                re.IGNORECASE,
            )
            if not include_match:
                continue
            include = include_match.group("include").strip()
        if not include:
            continue

        referenced_by = match.group("referenced_by").strip()
        line = int(match.group("line"))
        column_text = match.group("column")
        key = (include.casefold(), referenced_by.casefold(), line, column_text)
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "include": include,
            "referenced_by": referenced_by,
            "line": line,
        }
        if column_text is not None:
            entry["column"] = int(column_text)
        missing_includes.append(entry)
    return missing_includes


def _engine_source_control_provenance(engine_root: str) -> dict:
    """Read bounded Git provenance for a source engine without changing it."""

    def run_git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", engine_root, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_ENGINE_PROVENANCE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    commit = run_git("rev-parse", "--verify", "HEAD")
    if not commit:
        return {"status": "unavailable"}
    branch = run_git("branch", "--show-current")
    provenance = {
        "status": "available",
        "type": "git",
        "commit": commit,
    }
    if branch:
        provenance["branch"] = branch
    else:
        provenance["detached_head"] = True
    return provenance


def _missing_include_compatibility_context(
    uproject_path: str | None,
    engine_root: str | None,
) -> dict:
    """Describe factual project/engine context without claiming mismatch."""
    context = {
        "status": "unverified",
        "reason": (
            "Compiler output proves that an include could not be resolved; "
            "it does not prove a project/engine revision mismatch."
        ),
    }
    if uproject_path:
        project = {"path": uproject_path}
        try:
            descriptor = json.loads(
                Path(uproject_path).read_text(encoding="utf-8-sig")
            )
        except (OSError, json.JSONDecodeError):
            descriptor = None
        if isinstance(descriptor, dict):
            association = descriptor.get("EngineAssociation")
            if association:
                project["engine_association"] = str(association)
        context["project"] = project
    if engine_root:
        engine = {
            "root": engine_root,
            "source_control": _engine_source_control_provenance(engine_root),
        }
        version = get_engine_version(engine_root)
        if version:
            engine["version"] = version
        context["engine"] = engine
    return context


def _build_failure_diagnostics(
    log_file: str | None,
    *,
    uproject_path: str | None = None,
    engine_root: str | None = None,
    uat_command: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Extract bounded, factual diagnostics from a failed UAT/UBT log."""
    if not log_file:
        return {}
    path = Path(log_file)
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
            text = handle.read().decode(_build_output_encoding(), errors="replace")
    except OSError:
        return {}

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    missing_receipts = [
        line for line in lines if "Stage Failed. Missing receipt '" in line
    ]
    if missing_receipts:
        cook_completed = "********** COOK COMMAND COMPLETED **********" in lines
        return {
            "failure_kind": "missing_staging_receipt",
            "diagnostics": list(dict.fromkeys(missing_receipts))[-20:],
            "completed_phases": ["cook"] if cook_completed else [],
            "error": (
                "Cook completed, but UAT failed while preparing staging receipts. "
                if cook_completed else "UAT failed while preparing staging receipts. "
            ) + "See log_file for details.",
        }
    gradle_loopback_lines = [
        line for line in lines if _GRADLE_LOOPBACK_FAILURE_PATTERN.search(line)
    ]
    if (
        gradle_loopback_lines
        and _GRADLE_PACKAGE_CONTEXT_PATTERN.search(text)
    ):
        command_args = [str(arg) for arg in (uat_command or ())]
        iterative_cook = any(
            arg.casefold() in {"-iterate", "-iterativecooking"}
            for arg in command_args
        ) or re.search(r"(?:^|\s)-(?:iterate|iterativecooking)(?:\s|$)", text, re.IGNORECASE) is not None
        completed_phases = [
            phase
            for phase, marker in (
                ("build", "BUILD COMMAND COMPLETED"),
                ("cook", "COOK COMMAND COMPLETED"),
                ("stage", "STAGE COMMAND COMPLETED"),
            )
            if marker in text
        ]
        reuse_successful_outputs = iterative_cook and "cook" in completed_phases
        if reuse_successful_outputs:
            retry_guidance = (
                "Retry the same ue-cli build package command; its existing "
                "--uat-arg=-iterate can reuse unchanged cooked outputs."
            )
        else:
            retry_guidance = (
                "Retry the same ue-cli build package command with "
                "--uat-arg=-iterate to reuse unchanged cooked outputs when "
                "the previous Cook completed."
            )
        result = {
            "code": "BUILD_GRADLE_LOOPBACK_FAILED",
            "failure_kind": "gradle_loopback_connection",
            "failure_scope": "local_environment",
            "phase": "package",
            "external_tool": "Gradle",
            "transient": True,
            "retry_safe": True,
            "retry_uses_iterative_cook": iterative_cook,
            "reuse_successful_outputs": reuse_successful_outputs,
            "completed_phases": completed_phases,
            "error": (
                "Gradle could not establish its local loopback connection "
                "while assembling the Android package."
            ),
            "diagnostic": gradle_loopback_lines[-1],
            "suggestion": (
                retry_guidance
                + " If the loopback error repeats, stop stale Gradle daemons "
                "and inspect local firewall, antivirus, proxy, and loopback "
                "policy before retrying."
            ),
        }
        gradle_tasks = [
            match.group("task")
            for match in _GRADLE_TASK_PATTERN.finditer(text)
        ]
        if gradle_tasks:
            result["gradle_task"] = gradle_tasks[-1]
        return result

    conflict_result_lines = [
        line for line in lines if _UBT_CONFLICTING_INSTANCE_PATTERN.search(line)
    ]
    conflict_mutex_matches = []
    for line in lines:
        match = _UBT_CONFLICTING_MUTEX_PATTERN.search(line)
        if match:
            conflict_mutex_matches.append((line, match))
    conflict_legacy_lines = [
        line for line in lines if _UBT_CONFLICTING_LEGACY_PATTERN.search(line)
    ]
    if conflict_result_lines or conflict_mutex_matches or conflict_legacy_lines:
        diagnostic = (
            conflict_mutex_matches[-1][0]
            if conflict_mutex_matches
            else (
                conflict_legacy_lines[-1]
                if conflict_legacy_lines
                else conflict_result_lines[-1]
            )
        )
        result = {
            "code": "BUILD_CONFLICTING_INSTANCE",
            "failure_kind": "ubt_mutex_conflict",
            "error": (
                "UnrealBuildTool could not start because another instance is "
                "using this engine's global mutex."
            ),
            "diagnostic": diagnostic,
            "conflicting_process_hint": (
                "Another UnrealBuildTool process using the same engine owns the "
                "global build mutex."
            ),
            "suggestion": (
                "Wait for the other UnrealBuildTool process to finish, or stop it "
                "if you own it, then retry the build."
            ),
        }
        if conflict_mutex_matches:
            result["mutex_name"] = conflict_mutex_matches[-1][1].group("mutex")
        return result

    plugin_load_failures = []
    for line in lines:
        match = _PLUGIN_LOAD_FAILURE_PATTERN.search(line)
        if match:
            plugin_load_failures.append((line, match))
    if plugin_load_failures:
        primary_diagnostic, primary_match = plugin_load_failures[-1]
        diagnostics = list(dict.fromkeys(
            line for line, _match in plugin_load_failures
        ))[-20:]
        result = {
            "code": "BUILD_PLUGIN_LOAD_FAILED",
            "failure_kind": "plugin_load_failure",
            "diagnostic": primary_diagnostic,
            "diagnostics": diagnostics,
            "plugin": primary_match.group("plugin"),
            "suggestion": (
                "Verify that the plugin is enabled for this target and that its "
                "module exists and is built for the selected Unreal Engine before "
                "retrying the build."
            ),
        }
        module_match = _PLUGIN_LOAD_MODULE_PATTERN.search(primary_diagnostic)
        if module_match:
            result["module"] = module_match.group("module")
        if _COOK_FAILURE_PATTERN.search(text):
            result["phase"] = "cook"
        return result

    compiler_diagnostics = []
    for line in lines:
        if re.search(
            r"\bfatal error\b|\berror (?:C|LNK)\d+\b|:\s*error:",
            line,
            re.IGNORECASE,
        ):
            compiler_diagnostics.append(line)
    compiler_diagnostics = list(dict.fromkeys(compiler_diagnostics))[-20:]
    if compiler_diagnostics:
        missing_includes = _extract_msvc_missing_includes(compiler_diagnostics)
        if missing_includes:
            missing_includes = missing_includes[-_MAX_REPORTED_MISSING_INCLUDES:]
            return {
                "code": "BUILD_MISSING_INCLUDE",
                "failure_kind": "missing_include",
                "diagnostics": compiler_diagnostics,
                "missing_include_count": len(missing_includes),
                "missing_includes": missing_includes,
                "compatibility": _missing_include_compatibility_context(
                    uproject_path,
                    engine_root,
                ),
                "suggestion": (
                    "Verify that the project source revision matches the engine "
                    "branch/commit, then restore or update the missing include "
                    "and rebuild."
                ),
            }
        if _MSVC_VIRTUAL_MEMORY_EXHAUSTION_PATTERN.search(text):
            result = {
                "code": "BUILD_RESOURCE_EXHAUSTED",
                "failure_kind": "compiler_virtual_memory_exhaustion",
                "resource": "virtual_memory",
                "error": (
                    "The compiler exhausted Windows virtual memory while "
                    "creating a precompiled header."
                ),
                "diagnostics": compiler_diagnostics,
                "suggestion": (
                    "Reduce the selected build executor's parallel job limit, "
                    "ensure the Windows paging file has enough free space, then "
                    "retry the full compile."
                ),
            }
            if _WINDOWS_PAGEFILE_ERROR_PATTERN.search(text):
                result["system_error_code"] = 1455

            parallel_limit_matches = list(
                _UBT_PARALLEL_ACTION_LIMIT_PATTERN.finditer(text)
            )
            if parallel_limit_matches:
                result["ubt_parallel_action_limit"] = int(
                    parallel_limit_matches[-1].group("limit")
                )

            bk_job_matches = list(_BK_DIST_JOB_COUNT_PATTERN.finditer(text))
            if bk_job_matches and re.search(
                r"bk[_-](?:dist|ubt)|failed to compile with bk tools",
                text,
                re.IGNORECASE,
            ):
                match = bk_job_matches[-1]
                result.update({
                    "executor": "bk_dist",
                    "action_count": int(match.group("actions")),
                    "executor_job_count": int(match.group("jobs")),
                    "suggestion": (
                        "Reduce bk_dist's own job limit or disable bk_dist so "
                        "UBT's parallel-action limit controls execution; ensure "
                        "the Windows paging file has enough free space, then "
                        "retry the full compile."
                    ),
                })
                if "ubt_parallel_action_limit" in result:
                    result["parallel_limit_exceeded"] = (
                        result["executor_job_count"]
                        > result["ubt_parallel_action_limit"]
                    )
            return result
        return {
            "failure_kind": "compiler_diagnostics",
            "diagnostics": compiler_diagnostics,
        }

    if not re.search(r"failed to compile with bk tools|bk-ubt-tool", text, re.IGNORECASE):
        return {}

    result = {
        "failure_kind": "distributed_executor_failed_without_diagnostic",
        "executor": "bk_dist",
        "diagnostic": (
            "bk_dist failed without emitting a compiler diagnostic; inspect the "
            "actions JSON or rerun through the project's normal build path for the failing action."
        ),
    }
    action_matches = re.findall(
        r"(?:Parallel executor to run|Building)\s+(\d+)\s+action",
        text,
        re.IGNORECASE,
    )
    if action_matches:
        result["action_count"] = int(action_matches[-1])
    exit_matches = re.findall(r"exit code:\s*(-?\d+)", text, re.IGNORECASE)
    if exit_matches:
        result["executor_exit_code"] = int(exit_matches[-1])
    actions_matches = re.findall(
        r"(?:--actions_json_file\s+|failed to run actions with json file:\s*)([^\s\"']+|\"[^\"]+\"|'[^']+')",
        text,
        re.IGNORECASE,
    )
    if actions_matches:
        result["actions_json_file"] = actions_matches[-1].strip("\"'")
    return result


def _normalize_result(
    result: dict,
    action: str,
    *,
    uproject_path: str | None = None,
    engine_root: str | None = None,
) -> dict:
    out = {
        "status": "ok" if result["returncode"] == 0 else "error",
        "returncode": result["returncode"],
        "duration_seconds": result.get("duration_seconds", 0.0),
        "log_file": result.get("log_file", ""),
    }
    if result.get("command"):
        out["uat_command"] = result["command"]
    if result["returncode"] != 0:
        out["error"] = result.get(
            "error",
            f"{action} failed (exit {result['returncode']}). See log_file for details.",
        )
        out.update(_build_failure_diagnostics(
            result.get("log_file"),
            uproject_path=uproject_path,
            engine_root=engine_root,
            uat_command=result.get("command"),
        ))
    return out


def _validate_pe_image(path: Path) -> str | None:
    """Return a concise reason when a Windows build product is not a PE image."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            dos_header = handle.read(64)
            if len(dos_header) < 64:
                return f"file is too small for a DOS header ({size} bytes)"
            if dos_header[:2] != b"MZ":
                return "missing DOS MZ signature"

            pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > size - 26:
                return f"invalid PE header offset {pe_offset}"

            handle.seek(pe_offset)
            coff_header = handle.read(24)
            if len(coff_header) != 24 or coff_header[:4] != b"PE\0\0":
                return "missing PE signature"

            machine = int.from_bytes(coff_header[4:6], "little")
            section_count = int.from_bytes(coff_header[6:8], "little")
            optional_header_size = int.from_bytes(coff_header[20:22], "little")
            characteristics = int.from_bytes(coff_header[22:24], "little")
            if machine == 0:
                return "COFF machine is zero"
            if section_count == 0:
                return "COFF section count is zero"
            if optional_header_size < 64:
                return "PE optional header is missing"

            optional_header_end = pe_offset + 24 + optional_header_size
            if optional_header_end > size:
                return "optional header extends past end of file"
            section_table_end = optional_header_end + section_count * 40
            if section_table_end > size:
                return "section table extends past end of file"

            optional_header = handle.read(optional_header_size)
            optional_magic = int.from_bytes(optional_header[:2], "little")
            if optional_magic not in (0x10B, 0x20B):
                return f"invalid PE optional header magic 0x{optional_magic:04x}"
            section_alignment = int.from_bytes(optional_header[32:36], "little")
            file_alignment = int.from_bytes(optional_header[36:40], "little")
            size_of_image = int.from_bytes(optional_header[56:60], "little")
            size_of_headers = int.from_bytes(optional_header[60:64], "little")
            if section_alignment == 0 or file_alignment == 0:
                return "PE alignment is zero"
            if size_of_headers < section_table_end:
                return "SizeOfHeaders does not include the section table"
            if size_of_headers > size:
                return "SizeOfHeaders extends past end of file"
            if size_of_image < size_of_headers:
                return "SizeOfImage is smaller than SizeOfHeaders"
            if path.suffix.casefold() == ".dll" and not characteristics & 0x2000:
                return "COFF DLL characteristic is missing"

            section_table = handle.read(section_count * 40)
            for index in range(section_count):
                section = section_table[index * 40:(index + 1) * 40]
                raw_size = int.from_bytes(section[16:20], "little")
                raw_offset = int.from_bytes(section[20:24], "little")
                if raw_size and (
                    raw_offset == 0
                    or raw_offset > size
                    or raw_size > size - raw_offset
                ):
                    return f"section {index} raw data extends past end of file"
    except FileNotFoundError:
        return "file is missing"
    except OSError as exc:
        return f"could not read file: {exc}"
    return None


def _resolve_receipt_product_path(
    value: str,
    *,
    project_dir: Path,
    engine_root: Path,
) -> Path | None:
    replacements = {
        "$(ProjectDir)": str(project_dir),
        "$(EngineDir)": str(engine_root / "Engine"),
    }
    for prefix, root in replacements.items():
        if value.startswith(prefix):
            return Path(root + value[len(prefix):])
    if "$(" in value:
        return None
    path = Path(value)
    return path if path.is_absolute() else project_dir / path


def _invalid_build_receipt(receipt_path: Path, reason: str) -> dict:
    return {
        "status": "error",
        "code": "INVALID_BUILD_OUTPUT",
        "failure_kind": "invalid_build_receipt",
        "receipt_file": str(receipt_path),
        "error": f"Compile exited 0, but the Editor target receipt is invalid: {reason}",
    }


def _load_editor_target_receipt(
    project_dir: Path,
    target: str,
    config: str,
) -> tuple[Path | None, dict | None, dict | None]:
    """Load the receipt path selected by UE's target/config naming rules."""
    bin_dir = project_dir / "Binaries" / "Win64"
    candidates = []
    if config.casefold() == "development":
        default_path = bin_dir / f"{target}.target"
        if default_path.is_file():
            candidates.append(default_path)
    candidates.extend(bin_dir.glob(f"{target}-Win64-{config}*.target"))
    candidates = sorted(
        dict.fromkeys(candidates),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )

    first_error = None
    for receipt_path in candidates:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if first_error is None:
                first_error = _invalid_build_receipt(receipt_path, str(exc))
            continue
        if not isinstance(receipt, dict):
            if first_error is None:
                first_error = _invalid_build_receipt(
                    receipt_path,
                    "root value is not an object",
                )
            continue
        if str(receipt.get("TargetName", "")).casefold() != target.casefold():
            continue
        if str(receipt.get("TargetType", "")).casefold() != "editor":
            continue
        if str(receipt.get("Platform", "")).casefold() != "win64":
            continue
        if str(receipt.get("Configuration", "")).casefold() != config.casefold():
            continue
        return receipt_path, receipt, None
    return None, None, first_error


def _find_engine_plugin_module_products(
    uproject_path: str,
    engine_root: str,
    target: str,
    config: str,
    modules: list[str] | tuple[str, ...],
) -> tuple[Path | None, dict[str, list[str]]]:
    """Find requested module products owned by Engine plugins in the receipt."""
    project_dir = Path(uproject_path).parent
    try:
        receipt_path, receipt, receipt_error = _load_editor_target_receipt(
            project_dir,
            target,
            config,
        )
    except OSError:
        return None, {}
    if receipt_error or receipt_path is None or receipt is None:
        return receipt_path, {}

    products = receipt.get("BuildProducts")
    if not isinstance(products, list):
        return receipt_path, {}

    engine_path = Path(engine_root)
    engine_plugins_prefix = (
        str(engine_path / "Engine" / "Plugins")
        .replace("/", "\\")
        .rstrip("\\")
        .casefold()
    )
    requested_modules = {module.casefold(): module for module in modules}
    matches: dict[str, list[str]] = {}
    for product in products:
        if not isinstance(product, dict):
            continue
        product_type = str(product.get("Type", "")).casefold()
        raw_path = str(product.get("Path", ""))
        if (
            product_type != "dynamiclibrary"
            or Path(raw_path).suffix.casefold() != ".dll"
        ):
            continue
        path = _resolve_receipt_product_path(
            raw_path,
            project_dir=project_dir,
            engine_root=engine_path,
        )
        if path is None:
            continue
        path_text = str(path)
        normalized_path = path_text.replace("/", "\\").casefold()
        if not normalized_path.startswith(engine_plugins_prefix + "\\"):
            continue

        binary_tokens = set(Path(raw_path).stem.casefold().split("-"))
        for module_key, module in requested_modules.items():
            if module_key in binary_tokens:
                matches.setdefault(module, []).append(path_text)
    return receipt_path, matches


def inspect_win64_editor_runtime_dependencies(
    uproject_path: str,
    engine_root: str | None,
    config: str = "Development",
) -> dict:
    """Inspect runtime dependencies declared by the current Editor receipt."""
    target, target_error = _resolve_editor_target(uproject_path)
    if target_error or not target:
        return {
            "status": "unavailable",
            "reason": "editor_target_unresolved",
            "message": target_error or "Could not resolve the Editor target.",
        }

    resolved_engine_root = engine_root or find_engine_root(uproject_path)
    if not resolved_engine_root:
        return {
            "status": "unavailable",
            "reason": "engine_root_unresolved",
            "message": "Could not resolve the engine root for output inspection.",
        }

    project_dir = Path(uproject_path).parent
    receipt_path, receipt, receipt_error = _load_editor_target_receipt(
        project_dir,
        target,
        config,
    )
    if receipt_error:
        return {
            "status": "unavailable",
            "reason": "invalid_editor_target_receipt",
            "receipt_file": receipt_error.get("receipt_file"),
            "message": "Could not read a valid Editor target receipt for output inspection.",
        }
    if receipt_path is None or receipt is None:
        return {
            "status": "unavailable",
            "reason": "editor_target_receipt_not_found",
            "expected_receipt_directory": str(project_dir / "Binaries" / "Win64"),
            "message": "No matching Editor target receipt exists for output inspection.",
        }

    runtime_dependencies = receipt.get("RuntimeDependencies")
    if not isinstance(runtime_dependencies, list):
        return {
            "status": "unavailable",
            "reason": "runtime_dependencies_not_declared",
            "receipt_file": str(receipt_path),
            "message": "Editor target receipt does not declare RuntimeDependencies.",
        }

    missing_paths: list[str] = []
    unresolved_entries: list[dict] = []
    seen_paths: set[str] = set()
    engine_path = Path(resolved_engine_root)
    for index, dependency in enumerate(runtime_dependencies):
        if not isinstance(dependency, dict):
            unresolved_entries.append({
                "index": index,
                "reason": "entry is not an object",
            })
            continue
        raw_path = dependency.get("Path")
        if not isinstance(raw_path, str) or not raw_path:
            unresolved_entries.append({
                "index": index,
                "reason": "Path is missing or is not a string",
            })
            continue
        path = _resolve_receipt_product_path(
            raw_path,
            project_dir=project_dir,
            engine_root=engine_path,
        )
        if path is None:
            unresolved_entries.append({
                "index": index,
                "path": raw_path,
                "reason": "unsupported path macro",
            })
            continue
        path_text = str(path)
        path_key = path_text.replace("/", "\\").casefold()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        if not path.exists():
            missing_paths.append(path_text)

    recovery_command = (
        f'ue-cli --project "{uproject_path}" build compile '
        f"--platform Win64 --config {config}"
    )
    result = {
        "status": "ok",
        "receipt_file": str(receipt_path),
        "checked_runtime_dependency_count": len(seen_paths),
        "unresolved_runtime_dependency_count": len(unresolved_entries),
    }
    if unresolved_entries:
        result["unresolved_runtime_dependencies"] = unresolved_entries[
            :_MAX_REPORTED_MISSING_RUNTIME_DEPENDENCIES
        ]
    if not missing_paths:
        return result

    missing_count = len(missing_paths)
    result.update({
        "status": "incomplete",
        "code": "BUILD_CANCELLED_OUTPUTS_INCOMPLETE",
        "failure_kind": "missing_runtime_dependencies",
        "missing_runtime_dependency_count": missing_count,
        "missing_runtime_dependencies": missing_paths[
            :_MAX_REPORTED_MISSING_RUNTIME_DEPENDENCIES
        ],
        "missing_runtime_dependencies_truncated": (
            missing_count > _MAX_REPORTED_MISSING_RUNTIME_DEPENDENCIES
        ),
        "recovery_command": recovery_command,
        "message": (
            f"Cancellation completed, but {missing_count} runtime dependency "
            f"path(s) declared by {receipt_path.name} are missing. The Editor "
            "target may not launch; run a full Editor build to restore it."
        ),
    })
    return result


def _validate_win64_editor_build_products(
    uproject_path: str,
    engine_root: str,
    config: str,
    modules: list[str] | tuple[str, ...] | None = None,
    *,
    require_requested_module_product: bool = True,
) -> dict:
    """Validate PE products declared by UE's generated Editor target receipt.

    ``modules`` also enables RequiredResource manifest checks. Some project
    receipts do not enumerate project-plugin DLLs, so callers that validate a
    plugin separately can validate all receipt PE products without requiring a
    matching module product.
    """
    target, target_error = _resolve_editor_target(uproject_path)
    if target_error or not target:
        return {}

    project_dir = Path(uproject_path).parent
    receipt_path, receipt, receipt_error = _load_editor_target_receipt(
        project_dir,
        target,
        config,
    )
    if receipt_error:
        return receipt_error
    if receipt_path is None or receipt is None:
        if modules:
            return {
                "status": "error",
                "code": "INVALID_BUILD_OUTPUT",
                "failure_kind": "missing_editor_target_receipt",
                "expected_receipt_directory": str(project_dir / "Binaries" / "Win64"),
                "error": (
                    "Compile exited 0, but the module-targeted build left no "
                    "Editor target receipt. The target may not be launchable; "
                    "run a full build compile to restore launch metadata."
                ),
            }
        return {}

    products = receipt.get("BuildProducts")
    if not isinstance(products, list):
        return _invalid_build_receipt(
            receipt_path,
            "BuildProducts is not an array",
        )

    invalid_products = []
    missing_module_manifests = []
    validated_count = 0
    engine_path = Path(engine_root)
    requested_modules = {module.casefold() for module in (modules or ())}
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            return _invalid_build_receipt(
                receipt_path,
                f"BuildProducts[{index}] is not an object",
            )
        product_type = str(product.get("Type", ""))
        raw_path = str(product.get("Path", ""))
        if (
            requested_modules
            and product_type.casefold() == "requiredresource"
            and Path(raw_path).suffix.casefold() == ".modules"
        ):
            path = _resolve_receipt_product_path(
                raw_path,
                project_dir=project_dir,
                engine_root=engine_path,
            )
            if path is None:
                return _invalid_build_receipt(
                    receipt_path,
                    f"unsupported path macro in BuildProducts[{index}]: {raw_path}",
                )
            if not path.is_file():
                missing_module_manifests.append(str(path))
            continue
        if product_type.casefold() not in _PE_BUILD_PRODUCT_TYPES:
            continue
        if Path(raw_path).suffix.casefold() not in _PE_BUILD_PRODUCT_SUFFIXES:
            continue
        if requested_modules and require_requested_module_product:
            binary_tokens = set(Path(raw_path).stem.casefold().split("-"))
            if requested_modules.isdisjoint(binary_tokens):
                continue

        path = _resolve_receipt_product_path(
            raw_path,
            project_dir=project_dir,
            engine_root=engine_path,
        )
        if path is None:
            return _invalid_build_receipt(
                receipt_path,
                f"unsupported path macro in BuildProducts[{index}]: {raw_path}",
            )
        validated_count += 1
        reason = _validate_pe_image(path)
        if reason:
            invalid_products.append({
                "path": str(path),
                "type": product_type,
                "reason": reason,
            })

    if missing_module_manifests:
        missing_count = len(missing_module_manifests)
        return {
            "status": "error",
            "code": "INVALID_BUILD_OUTPUT",
            "failure_kind": "missing_editor_module_manifests",
            "receipt_file": str(receipt_path),
            "missing_module_manifest_count": missing_count,
            "missing_module_manifests": missing_module_manifests[
                :_MAX_REPORTED_INVALID_BUILD_PRODUCTS
            ],
            "error": (
                f"Compile exited 0, but {missing_count} Editor module manifest(s) "
                "declared by the target receipt are missing. The target is not "
                "launchable; run a full build compile to restore launch metadata."
            ),
        }

    if validated_count == 0:
        if requested_modules and require_requested_module_product:
            reason = (
                "BuildProducts contains no PE file for requested module(s): "
                + ", ".join(sorted(modules or ()))
            )
        else:
            reason = "BuildProducts contains no resolvable PE files"
        return _invalid_build_receipt(
            receipt_path,
            reason,
        )
    if not invalid_products:
        return {}

    invalid_count = len(invalid_products)
    return {
        "status": "error",
        "code": "INVALID_BUILD_OUTPUT",
        "failure_kind": "invalid_pe_build_product",
        "receipt_file": str(receipt_path),
        "validated_pe_products": validated_count,
        "invalid_build_product_count": invalid_count,
        "invalid_build_products": invalid_products[
            :_MAX_REPORTED_INVALID_BUILD_PRODUCTS
        ],
        "error": (
            f"Compile exited 0, but {invalid_count} PE build product(s) declared "
            f"by {receipt_path.name} are missing or malformed. The target is not "
            "launchable; inspect invalid_build_products and rebuild through the "
            "project's normal local build path."
        ),
    }


def _normalize_compile_result(
    result: dict,
    *,
    uproject_path: str,
    engine_root: str,
    config: str,
    platform: str,
    modules: list[str] | tuple[str, ...] | None = None,
) -> dict:
    out = _normalize_result(
        result,
        "Compile",
        uproject_path=uproject_path,
        engine_root=engine_root,
    )
    if out["status"] != "ok" or platform.casefold() != "win64":
        return out
    validation = _validate_win64_editor_build_products(
        uproject_path,
        engine_root,
        config,
        modules,
    )
    if validation.get("status") == "error":
        out.update(validation)
    return out


def compile_project(
    uproject_path: str,
    config: str = "Development",
    platform: str = "Win64",
    engine_root: str | None = None,
    log_file: str | None = None,
    on_start=None,
    modules: list[str] | tuple[str, ...] | None = None,
    use_engine_editor_target_if_missing: bool = False,
) -> dict:
    try:
        modules = [validate_module_name(value) for value in (modules or ())]
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    if modules and platform.lower() != "win64":
        return {
            "status": "error",
            "error": "Module-targeted compile is supported only for Win64 Editor targets.",
        }

    already = _check_already_building(uproject_path)
    if already:
        return already

    engine_root = engine_root or find_engine_root(uproject_path)
    if not engine_root:
        return {"status": "error", "error": "Could not find engine root"}

    if modules:
        target_source = "project"
        if use_engine_editor_target_if_missing:
            target, target_error = _find_project_editor_target(uproject_path)
            if not target and not target_error:
                target = get_editor_binary_prefix(engine_root)
                target_source = "engine"
        else:
            target, target_error = _resolve_editor_target(uproject_path)
        if target_error:
            return {"status": "error", "error": target_error}
        receipt_path, engine_plugin_products = _find_engine_plugin_module_products(
            uproject_path,
            engine_root,
            target,
            config,
            modules,
        )
        if engine_plugin_products:
            unsupported_modules = sorted(engine_plugin_products)
            recovery_command = (
                f'ue-cli --project "{uproject_path}" build compile '
                f"--platform {platform} --config {config}"
            )
            return {
                "status": "error",
                "code": "ENGINE_PLUGIN_MODULE_UNSUPPORTED",
                "failure_kind": "unsupported_engine_plugin_module",
                "modules": unsupported_modules,
                "module_products": engine_plugin_products,
                "receipt_file": str(receipt_path),
                "recovery_command": recovery_command,
                "error": (
                    "Module-targeted compile does not support Engine plugin "
                    "module(s): " + ", ".join(unsupported_modules) + ". UnrealBuildTool "
                    "may omit their output actions from the project Editor target."
                ),
                "suggestion": (
                    "Run the full Editor target build without --module: "
                    + recovery_command
                ),
            }
        try:
            hot_reload_state_files = _find_module_hot_reload_state_files(
                uproject_path,
                target,
                platform,
                config,
            )
        except OSError as exc:
            return {
                "status": "error",
                "code": "HOT_RELOAD_STATE_PROBE_FAILED",
                "error": (
                    "Could not inspect existing UBT hot-reload state before the "
                    f"module-targeted build: {exc}"
                ),
            }
        extra_args = [f"-Project={uproject_path}"]
        if hot_reload_state_files:
            # UBT's disabled-hot-reload setup deletes previous temporary metadata
            # before -Module filters output actions. Keeping hot reload enabled
            # reapplies that state and rewrites the affected manifests instead.
            extra_args.append("-ForceHotReload")
        extra_args.extend(f"-Module={module}" for module in modules)
        extra_args.append("-WaitMutex")
        result = run_build(
            engine_root,
            target,
            platform,
            config,
            extra_args=extra_args,
            log_file=log_file,
            log_label="compile",
            project_dir=str(Path(uproject_path).parent),
            on_start=on_start,
        )
        if target_source == "engine":
            normalized = _normalize_result(
                result,
                "Compile",
                uproject_path=uproject_path,
                engine_root=engine_root,
            )
            normalized["editor_target"] = target
            normalized["editor_target_source"] = target_source
            return normalized
        return _normalize_compile_result(
            result,
            uproject_path=uproject_path,
            engine_root=engine_root,
            config=config,
            platform=platform,
            modules=modules,
        )

    if platform.lower() != "win64":
        target, target_error = _resolve_game_target(uproject_path)
        if target_error:
            return {"status": "error", "error": target_error}
        result = run_build(
            engine_root,
            target,
            platform,
            config,
            extra_args=[f"-Project={uproject_path}", "-WaitMutex"],
            log_file=log_file,
            log_label="compile",
            project_dir=str(Path(uproject_path).parent),
            on_start=on_start,
        )
        return _normalize_compile_result(
            result,
            uproject_path=uproject_path,
            engine_root=engine_root,
            config=config,
            platform=platform,
            modules=modules,
        )

    args = [
        f"-project={uproject_path}",
        f"-platform={platform}",
        f"-clientconfig={config}",
        "-build",
        "-noP4",
        "-WaitForUATMutex",
    ]
    result = run_uat(
        engine_root,
        "BuildCookRun",
        args,
        log_file=log_file,
        log_label="compile",
        project_dir=str(Path(uproject_path).parent),
        on_start=on_start,
    )
    return _normalize_compile_result(
        result,
        uproject_path=uproject_path,
        engine_root=engine_root,
        config=config,
        platform=platform,
        modules=modules,
    )


def cook_content(
    uproject_path: str,
    platform: str = "Win64",
    engine_root: str | None = None,
    log_file: str | None = None,
    on_start=None,
    *,
    packages: list[str] | tuple[str, ...] | None = None,
    output_dir: str | None = None,
    ini_overrides: list[str] | tuple[str, ...] | None = None,
) -> dict:
    try:
        packages = [validate_cook_package(value) for value in (packages or ())]
        if output_dir is not None:
            output_dir = validate_package_uat_value(
                output_dir,
                label="cook output directory",
                require_value=True,
            )
        ini_overrides = [
            validate_cook_ini_override(value)
            for value in (ini_overrides or ())
        ]
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    already = _check_already_building(uproject_path)
    if already:
        return already

    engine_root = engine_root or find_engine_root(uproject_path)
    if not engine_root:
        return {"status": "error", "error": "Could not find engine root"}

    args = [
        f"-project={uproject_path}",
        f"-platform={platform}",
        "-cook",
        "-noP4",
        # UAT Package prepares deployment contexts even without -package.
        # Skip it explicitly so cook-only runs do not require Game receipts.
        "-skippackage",
    ]
    if packages:
        args.append(
            "-AdditionalCookerOptions=-Package=" + "+".join(packages)
        )
    else:
        args.append("-allmaps")
    if output_dir:
        args.append(f"-CookOutputDir={output_dir}")
    args.extend(f"-ini:{override}" for override in ini_overrides)
    result = run_uat(
        engine_root,
        "BuildCookRun",
        args,
        log_file=log_file,
        log_label="cook",
        project_dir=str(Path(uproject_path).parent),
        on_start=on_start,
    )
    return _normalize_result(result, "Cook")


def package_project(
    uproject_path: str,
    platform: str = "Win64",
    config: str = "Development",
    output_dir: str | None = None,
    engine_root: str | None = None,
    log_file: str | None = None,
    on_start=None,
    *,
    maps: list[str] | tuple[str, ...] | None = None,
    cook_flavor: str | None = None,
    uat_args: list[str] | tuple[str, ...] | None = None,
) -> dict:
    try:
        maps = [
            validate_package_uat_value(value, label="map")
            for value in (maps or ())
        ]
        if cook_flavor is not None:
            cook_flavor = validate_package_uat_value(
                cook_flavor,
                label="cook flavor",
            )
        uat_args = [
            validate_package_uat_value(
                value,
                label="UAT argument",
                require_option=True,
            )
            for value in (uat_args or ())
        ]
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    already = _check_already_building(uproject_path)
    if already:
        return already

    path = Path(uproject_path)
    output_dir = output_dir or str(path.parent / "Packaged")
    dangling_paths = _find_dangling_package_paths(
        uproject_path,
        platform,
        output_dir,
    )
    if dangling_paths:
        return {
            "status": "error",
            "code": "PACKAGE_DANGLING_LINK",
            "error": "Package preflight found a dangling symlink or Windows junction.",
            "failure_kind": "dangling_link",
            "dangling_paths": dangling_paths,
            "suggestion": (
                "Restore each link target or replace the link with a real directory, "
                "then retry the package command."
            ),
        }

    engine_root = engine_root or find_engine_root(uproject_path)
    if not engine_root:
        return {"status": "error", "error": "Could not find engine root"}

    args = [
        f"-project={uproject_path}",
        f"-platform={platform}",
        f"-clientconfig={config}",
        "-build",
        "-cook",
        "-stage",
        "-package",
        "-archive",
        f"-archivedirectory={output_dir}",
        "-noP4",
    ]
    if maps:
        args.append("-map=" + "+".join(maps))
    if cook_flavor:
        args.append(f"-cookflavor={cook_flavor}")
    if uat_args:
        args.extend(uat_args)
    result = run_uat(
        engine_root,
        "BuildCookRun",
        args,
        log_file=log_file,
        log_label="package",
        project_dir=str(path.parent),
        on_start=on_start,
    )
    out = _normalize_result(result, "Package")
    out["output_dir"] = output_dir
    return out


def build_status(uproject_path: str) -> dict:
    path = Path(uproject_path)
    project_dir = path.parent
    project_name = path.stem
    binaries_dir = project_dir / "Binaries"
    intermediate_dir = project_dir / "Intermediate"

    status = {
        "project": project_name,
        "has_binaries": binaries_dir.is_dir(),
        "has_intermediate": intermediate_dir.is_dir(),
        "platforms": {},
    }

    if binaries_dir.is_dir():
        for platform_dir in binaries_dir.iterdir():
            if not platform_dir.is_dir():
                continue
            binaries = list(platform_dir.glob("*.dll")) + list(platform_dir.glob("*.exe"))
            newest = None
            newest_time = 0.0
            for binary in binaries:
                mtime = binary.stat().st_mtime
                if mtime > newest_time:
                    newest = binary.name
                    newest_time = mtime
            status["platforms"][platform_dir.name] = {
                "binary_count": len(binaries),
                "newest_binary": newest,
                "newest_time": newest_time,
            }

    saved_dir = project_dir / "Saved" / "Logs"
    if saved_dir.is_dir():
        log_files = sorted(saved_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        status["recent_logs"] = [
            {"name": log.name, "size": log.stat().st_size}
            for log in log_files[:5]
        ]

    return status


def generate_project_files(uproject_path: str, engine_root: str | None = None) -> dict:
    engine_root = engine_root or find_engine_root(uproject_path)
    if not engine_root:
        return {"status": "error", "error": "Could not find engine root"}

    project_dir = str(Path(uproject_path).parent)
    gen_bat = find_generate_project_files(engine_root)
    if gen_bat:
        from cli_anything.unreal.utils.ue_backend import _allocate_log_path, _run_subprocess

        log_file = _allocate_log_path(project_dir, "genproj")
        result = _run_subprocess([gen_bat, f"-project={uproject_path}", "-game", "-engine"], log_file=log_file)
    else:
        result = run_uat(
            engine_root,
            "GenerateProjectFiles",
            [f"-project={uproject_path}", "-game", "-engine"],
            log_label="genproj",
            project_dir=project_dir,
        )
    return _normalize_result(result, "Generate project files")


def stop_build(uproject_path: str) -> dict:
    from cli_anything.unreal.core.tasks import (
        FINAL_TASK_STATUSES,
        active_build_tasks,
        cancel_task,
        reconcile_task_cancellation,
    )

    task_results = []
    for task in active_build_tasks(uproject_path):
        cancelled = cancel_task(task["task_id"])
        if cancelled is None:
            continue
        cancel_result = cancelled.get("cancel_result", {})
        task_result = {
            "task_id": cancelled["task_id"],
            "status": cancelled.get("status"),
            "pid": cancelled.get("pid"),
            "worker_pid": cancelled.get("worker_pid"),
            "killed": cancel_result.get("killed", []),
            "remaining": cancel_result.get("remaining", []),
        }
        if cancel_result.get("processes"):
            task_result["processes"] = cancel_result["processes"]
        if cancelled.get("error"):
            task_result["error"] = cancelled["error"]
        task_results.append(task_result)

    tracked_tasks_cancelled = bool(task_results) and all(
        task["status"] in FINAL_TASK_STATUSES and not task["remaining"]
        for task in task_results
    )
    if tracked_tasks_cancelled:
        result = {
            "status": "skipped",
            "killed": [],
            "remaining": [],
            "process_probe": {
                "status": "skipped",
                "reason": "tracked_tasks_cancelled",
            },
        }
    else:
        try:
            result = kill_build_processes(
                uproject_path,
                query_timeout=_BUILD_STATE_PROCESS_PROBE_TIMEOUT_SECONDS,
                fail_on_probe_error=True,
            )
        except BuildProcessProbeError as exc:
            result = {
                "status": "partial",
                "killed": [],
                "remaining": [],
                "process_probe": {
                    "status": "failed",
                    "message": str(exc),
                    "details": exc.details,
                },
            }
    final_scan_killed = result.get("killed", [])
    for task in task_results:
        reconciled = reconcile_task_cancellation(task["task_id"], final_scan_killed)
        if reconciled is None:
            continue
        cancel_result = reconciled.get("cancel_result", {})
        task.update({
            "status": reconciled.get("status"),
            "killed": cancel_result.get("killed", []),
            "remaining": cancel_result.get("remaining", []),
        })
        if cancel_result.get("processes"):
            task["processes"] = cancel_result["processes"]
        else:
            task.pop("processes", None)
        if reconciled.get("error"):
            task["error"] = reconciled["error"]
        else:
            task.pop("error", None)

    killed = list(result.get("killed", []))
    remaining = list(result.get("remaining", []))
    for task in task_results:
        killed.extend(task["killed"])
        remaining.extend(task["remaining"])
    killed = list(dict.fromkeys(killed))
    killed_set = set(killed)
    remaining = [
        pid for pid in dict.fromkeys(remaining)
        if pid not in killed_set
    ]

    if result.get("status") == "partial" or remaining or any(
        task["status"] not in FINAL_TASK_STATUSES for task in task_results
    ):
        status = "partial"
    elif task_results or killed:
        status = "ok"
    else:
        status = result["status"]

    response = {
        "status": status,
        "killed": killed,
        "remaining": remaining,
    }
    if task_results:
        response["tasks"] = task_results
    if "process_probe" in result:
        response["process_probe"] = result["process_probe"]
    return response


def is_building(uproject_path: str) -> dict:
    from cli_anything.unreal.core.tasks import active_build_tasks

    tasks = []
    for task in active_build_tasks(uproject_path):
        evidence = {
            key: task[key]
            for key in (
                "task_id",
                "command",
                "status",
                "worker_pid",
                "pid",
                "log_file",
            )
            if key in task
        }
        tasks.append(evidence)

    # A non-final ue-cli task already answers the busy-state question. Avoid
    # delaying its JSON response on the much slower Windows CIM process scan.
    if tasks:
        processes = []
        process_probe = {
            "status": "skipped",
            "reason": "active_task_state",
        }
    else:
        processes = find_running_build_processes(
            uproject_path,
            include_cmdline=False,
            query_timeout=_BUILD_STATE_PROCESS_PROBE_TIMEOUT_SECONDS,
            fail_on_error=True,
        )
        process_probe = {"status": "ok"}

    kinds: dict[str, int] = {}
    for process in processes:
        name = process.get("name", "")
        kinds[name] = kinds.get(name, 0) + 1

    result = {
        "building": bool(processes or tasks),
        "count": len(processes),
        "kinds": kinds,
        "processes": processes,
        "active_task_count": len(tasks),
        "active_tasks": tasks,
        "process_probe": process_probe,
    }

    saved_logs = Path(uproject_path).parent / "Saved" / "Logs"
    if saved_logs.is_dir():
        cli_logs = sorted(saved_logs.glob("cli_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if cli_logs:
            result["latest_log"] = str(cli_logs[0])
    return result
