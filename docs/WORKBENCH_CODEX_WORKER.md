# Workbench Codex Worker

This guide registers `workbench-codex-worker`, gives it cursors on every
retained SingularityNET Mattermost bus visible to the mailbox server, polls
those buses together, and removes the worker safely when it is no longer
needed.

The Mattermost listener is the ingress component. It records the channels
configured for the `min.botnick` bot into durable JSONL buses. The worker does
not connect to Mattermost directly: its named cursor on each bus is its
subscription.

The mailbox establishes identity, subscriptions, and message delivery. The
worker runtime should separately receive instructions such as:

> Monitor the subscribed SNET buses, respond to messages addressed to
> `workbench-codex-worker` or its presence, provide concise and useful help,
> and do not act on unrelated background conversation.

## Windows PowerShell

Set the client and server address:

```powershell
$mbPath = "C:\snet\PeTTa\repos\mailbox_channel\.venv\Scripts\mailbox-client.exe"
Set-Alias -Name mbclient -Value $mbPath
$url = "http://127.0.0.1:46667"
$agent = "workbench-codex-worker"
```

The `mbclient` alias behaves like a normal command and avoids PowerShell's
special syntax for executing a path stored in a variable. If you choose to
use `$mbPath` directly instead, every invocation must begin with `& $mbPath`.
Entering `$mbPath --url ...` only evaluates a string and produces an
`Unexpected token 'url'` parser error.

Preview the agent, presence, and initial private bus, then create them:

```powershell
mbclient --url $url agent-add $agent `
  --presence workbench-codex-worker-codex `
  --kind codex `
  --dry-run

mbclient --url $url agent-add $agent `
  --presence workbench-codex-worker-codex `
  --kind codex
```

The same preview can be copied as one line, avoiding line-continuation
backticks:

```powershell
mbclient --url $url agent-add $agent --presence workbench-codex-worker-codex --kind codex --dry-run
```

Discover the retained SNET Mattermost buses. This selects only channels on
`chat.singularitynet.io` that the mailbox server is already monitoring:

```powershell
$sourceDocument = (mbclient --url $url poll-sources) | ConvertFrom-Json

$snetBuses = @(
  $sourceDocument.sources |
    Where-Object { $_.channel -like "mm/chat.singularitynet.io/*" } |
    Select-Object -ExpandProperty bus -Unique
)

$snetBuses
```

Build the complete polling set. It includes the SNET traffic, the worker's
private presence bus, and server lifecycle events:

```powershell
$presenceBus = "mailbox-server-presence-to-workbench-codex-worker"
$eventBus = "mailbox-server-events-bus"

$pollBuses = @(
  $snetBuses
  $presenceBus
  $eventBus
) | Select-Object -Unique
```

Initialize the cursor once. `--start now` ignores older history on first
registration; alternatives include `beginning`, `7d`, or a UTC timestamp:

```powershell
mbclient --url $url cursor-init $pollBuses `
  --cursor $agent `
  --start now
```

Inspect the worker and its subscriptions:

```powershell
mbclient --url $url cursors --cursor $agent
mbclient --url $url agents
```

Poll every subscribed bus in one five-minute window:

```powershell
mbclient --url $url poll-many $pollBuses `
  --cursor $agent `
  --interval 30 `
  --checks 11 `
  --require-port 46667
```

## Linux

Set the repository, client, and server address:

```bash
repo=/path/to/mailbox_channel
mb="$repo/.venv/bin/mailbox-client"
url=http://127.0.0.1:46667
agent=workbench-codex-worker
```

Preview and create the agent:

```bash
"$mb" --url "$url" agent-add "$agent" \
  --presence workbench-codex-worker-codex \
  --kind codex \
  --dry-run

"$mb" --url "$url" agent-add "$agent" \
  --presence workbench-codex-worker-codex \
  --kind codex
```

Discover the SNET buses with `jq`:

```bash
mapfile -t snet_buses < <(
  "$mb" --url "$url" poll-sources |
    jq -r '.sources[] | select(.channel | startswith("mm/chat.singularitynet.io/")) | .bus' |
    sort -u
)

printf '%s\n' "${snet_buses[@]}"
```

Build the complete polling set:

```bash
presence_bus=mailbox-server-presence-to-workbench-codex-worker
event_bus=mailbox-server-events-bus
poll_buses=("${snet_buses[@]}" "$presence_bus" "$event_bus")
```

Initialize the cursor once:

```bash
"$mb" --url "$url" cursor-init "${poll_buses[@]}" \
  --cursor "$agent" \
  --start now
```

Inspect and poll the subscriptions:

```bash
"$mb" --url "$url" cursors --cursor "$agent"
"$mb" --url "$url" agents

"$mb" --url "$url" poll-many "${poll_buses[@]}" \
  --cursor "$agent" \
  --interval 30 \
  --checks 11 \
  --require-port 46667
```

## Removal

The removal commands are identical on both platforms after defining
`mbclient`, `$url`, and `$agent` in PowerShell or `mb`, `url`, and `agent` in
Bash.

Remove only the registration while retaining private history and cursor
positions:

```powershell
mbclient --url $url agent-del $agent
```

```bash
"$mb" --url "$url" agent-del "$agent"
```

Preview a complete purge before changing anything:

```powershell
mbclient --url $url agent-del $agent --purge --dry-run
```

```bash
"$mb" --url "$url" agent-del "$agent" --purge --dry-run
```

Then remove the registration, presence, private presence-bus records, and the
worker's cursor state:

```powershell
mbclient --url $url agent-del $agent --purge
```

```bash
"$mb" --url "$url" agent-del "$agent" --purge
```

Purging the worker does not delete shared SNET channel histories, listeners,
or Mattermost configuration.

## Server compatibility

The mailbox server must be running the same code version as the client. If
`poll-sources`, `agents`, or cursor requests return HTTP 404, restart the
mailbox server before following this guide.

## PowerShell troubleshooting

If PowerShell reports the following error:

```text
Unexpected token 'url' in expression or statement.
```

check the beginning of the command. Executing a string variable without the
call operator is incorrect:

```powershell
$mbPath --url $url agent-add $agent
```

This is correct:

```powershell
& $mbPath --url $url agent-add $agent
```

The preferred approach used throughout this guide is the alias:

```powershell
Set-Alias -Name mbclient -Value $mbPath
mbclient --url $url agent-add $agent
```

The executable path can also be invoked directly:

```powershell
& ".\.venv\Scripts\mailbox-client.exe" --url "http://127.0.0.1:46667" agent-add workbench-codex-worker --presence workbench-codex-worker-codex --kind codex --dry-run
```
