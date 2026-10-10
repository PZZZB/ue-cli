# Remote host execution

Use the ordinary `ue-cli` skill commands in remote mode. The client sends their
arguments to the host's original parser and command implementations. No separate
MCP operation mapping is required. The host has Unreal and its build tools; the
client only needs Python and ue-cli.

## Select and inspect a connection

```powershell
ue-cli remote list
ue-cli remote health
ue-cli --remote host-game editor status
```

`remote use NAME` selects the default. With a default, `ue-cli editor status`
and other UE commands run remotely. `remote use --clear` removes the default.
`--local` is an explicit local override; never use it to work around a remote
connection error. Management, root help/version and `install-skills` run locally.
Remote failure must remain a failure, not silently execute in the VM.

Validate the health result's host project/engine before changing anything. A
configured `local_project` maps the shared client's absolute file arguments to
the fixed host root. The host refuses project/port overrides or task IDs owned
by a different project. It serializes commands that operate on the editor and
checks for native tasks already in progress. Normal tool safety, lifecycle,
save/discard and single-controller ownership rules still apply.

## Builds and progress

```powershell
ue-cli build compile --platform Win64 --config Development --no-wait
ue-cli task status <task_id>
ue-cli task wait <task_id> --timeout 600
```

Native task states/IDs and exit codes retain their original meaning. A transport
request receipt is not a completed build. Compile the complete Editor target
unless the user explicitly asks for a limited diagnostic. Compilation does not
authorize launching the editor.

If transport polling fails after receipt, continue that same request with:

```powershell
ue-cli remote result REQUEST_ID --connection host-game
```

Do not redispatch an uncertain build or launch. If the receipt itself was lost,
inspect native task/editor state on the host. A server restart invalidates its
transport receipts but does not erase native task records or imply UE stopped.

## Scripts and screenshots

`editor run-script -` sends UTF-8 stdin. A script file inside the configured
shared project keeps file mode: its client path maps to the host path, preserving
the normal runner's `__file__` and sibling imports. Relative script paths resolve
from the client's working directory. A standalone client script outside the
shared project is read locally and transmitted as stdin, so it has no file-based
`__file__` or automatic sibling import directory. Dependencies are not uploaded;
put scripts and their dependencies in the shared project when they need file
context. UE executes both forms on the host. Use `--no-save` deliberately.
Other file arguments inside the configured shared project map to the host;
arbitrary files outside it are not automatically uploaded. Paths that remain
absolute refer to the host. UE asset paths such as `/Game/Maps/Test` stay unchanged.

```powershell
ue-cli screenshot capture --path C:/Temp/viewport.png --no-compress --include-ui
```

Screenshot `--path` is a client destination. The service stores and returns only
managed image artifacts; the client checks their size/hash and rewrites result
paths to downloaded files. Inspect the returned client image for visual proof.
Without an explicit path, downloads go below `~/.ue-cli/remote-artifacts`.

## Trust and setup

Run `ue-cli --local --project <host.uproject> remote serve --help` for service
options. Run the service in the host user's interactive desktop session when
launching UE. Restrict the listening interface/client IPs with the OS firewall,
use a private isolated network or a secure tunnel, and keep token files outside
shared projects. `remote add NAME --url URL --token-file PATH --local-project PATH
--default` configures the client without putting token values in arguments.

The fixed project and command checks prevent accidental context switching.
Authorized UE Python, build scripts and plugins retain the host user's powers;
this is not a sandbox for untrusted code. Host command networking is separate
from the VM's inference/proxy route. Keep both ue-cli packages on a compatible
remote protocol version.
