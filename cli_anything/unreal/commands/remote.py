"""Named remote connections and the authenticated host service."""

from __future__ import annotations

import hashlib
import ipaddress
from pathlib import Path

import click

from cli_anything.unreal.commands import AppError, AppState, handle_error, output, require_project


@click.group("remote")
def remote_group():
    """Configure connections or serve one host project. Management always runs locally."""


@remote_group.command("add")
@click.argument("name")
@click.option("--url", required=True, help="Host service URL, without credentials.")
@click.option("--token-file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Private UTF-8 file containing only the bearer token.")
@click.option("--local-project", type=click.Path(path_type=Path),
              help="Optional client project directory or .uproject for explicit path validation.")
@click.option("--default", "make_default", is_flag=True, help="Use this connection for subsequent UE commands.")
@handle_error
@click.pass_obj
def add_cmd(state: AppState, name, url, token_file, local_project, make_default):
    """Save a named connection. The token value is never included in CLI arguments."""
    from cli_anything.unreal.core.remote_config import add_profile, select_profile

    add_profile(name, url, str(token_file.resolve()),
                local_project=str(local_project.resolve()) if local_project else None)
    if make_default:
        select_profile(name)
    output({"name": name, "url": url, "default": make_default}, state)


@remote_group.command("list")
@handle_error
@click.pass_obj
def list_cmd(state: AppState):
    """Show configured connections without reading or printing token values."""
    from cli_anything.unreal.core.remote_config import list_profiles

    output(list_profiles(), state)


@remote_group.command("use")
@click.argument("name", required=False)
@click.option("--clear", is_flag=True, help="Remove the default; keep all saved connections.")
@handle_error
@click.pass_obj
def use_cmd(state: AppState, name, clear):
    """Select the default remote connection, or clear it with --clear."""
    from cli_anything.unreal.core.remote_config import select_profile

    if bool(name) == bool(clear):
        raise AppError("INVALID_ARGUMENT", "Specify a connection name or --clear.", exit_code=2)
    select_profile(None if clear else name)
    output({"default": None if clear else name}, state)


@remote_group.command("health")
@click.argument("name", required=False)
@handle_error
@click.pass_obj
def health_cmd(state: AppState, name):
    """Check protocol compatibility and the host's fixed project without running UE."""
    from cli_anything.unreal.core.remote_client import RemoteClient
    from cli_anything.unreal.core.remote_config import get_profile

    output(RemoteClient(get_profile(name)).health(), state)


@remote_group.command("result")
@click.argument("request_id")
@click.option("--connection", help="Saved connection name; defaults to the selected connection.")
@handle_error
@click.pass_obj
def result_cmd(state: AppState, request_id, connection):
    """Resume waiting for a transport request without executing its command again."""
    from cli_anything.unreal.core.remote_client import RemoteClient, download_artifacts
    from cli_anything.unreal.core.remote_config import get_profile

    client = RemoteClient(get_profile(connection))
    result = client.follow(request_id)
    click.echo(download_artifacts(client, result), nl=False)
    raise SystemExit(result["exit_code"])


@remote_group.command("serve")
@click.option("--bind", default="127.0.0.1", show_default=True, help="Exact local IP address to bind.")
@click.option("--port", "listen_port", type=click.IntRange(1, 65535), default=17891, show_default=True)
@click.option("--token-file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--allow-client", multiple=True, help="Allowed client IP; repeatable. Required for a non-loopback bind.")
@click.option("--expected-engine", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Pin the engine directory; defaults to the project's resolved engine.")
@click.option("--state-dir", type=click.Path(path_type=Path), help="Private request history and managed artifact directory.")
@handle_error
@click.pass_obj
def serve_cmd(state: AppState, bind, listen_port, token_file, allow_client, expected_engine, state_dir):
    """Serve the --project context using the original ue-cli parser and commands.

    Use an isolated network or a secure authenticated tunnel. Authorized clients
    can execute UE scripts and trusted build code as the host user; this service
    is not a sandbox. Run in the logged-in desktop session to launch visible UE.
    """
    from cli_anything.unreal.core.remote_server import RemoteServerConfig, serve
    from cli_anything.unreal.core.remote_policy import RemoteError

    require_project(state)
    project = Path(state.session.project_path).resolve()
    engine = expected_engine or state.session.engine_root
    if not engine:
        raise AppError("ENGINE_REQUIRED", "Cannot pin the project engine; pass --expected-engine.", exit_code=2)
    if state_dir is None:
        identity = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:16]
        state_dir = Path.home() / ".ue-cli" / "remote-server" / identity
    if not allow_client:
        try:
            address = ipaddress.ip_address(bind)
        except ValueError as exc:
            raise AppError("INVALID_ARGUMENT", "--bind must be an IP address.", exit_code=2) from exc
        if address.is_loopback:
            allow_client = (bind,)
    config = RemoteServerConfig(
        project_path=str(project), expected_engine=str(Path(engine).resolve()),
        token_file=str(token_file.resolve()), bind=bind, port=listen_port,
        allowed_clients=tuple(allow_client), state_directory=str(state_dir.resolve()),
    )
    try:
        serve(config)
    except RemoteError as exc:
        raise AppError(exc.code, str(exc), exit_code=2) from exc
    except (OSError, ValueError) as exc:
        raise AppError("REMOTE_SERVER_CONFIG", str(exc), exit_code=2) from exc
