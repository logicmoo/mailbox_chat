Exit code: 0
Wall time: 1.1 seconds
Output:
# Mailbox Channel Relay Bridging Proxy

Python package/repository name: `mailbox_channel_relay_bridging_proxy`.

Mailbox Channel Relay Bridging Proxy is a standalone, transport-neutral daemon for
relaying messages between mailboxes and chat channels. It can serve automated
workflows, OpenAI Codex tasks, agent runtimes, or ordinary channel-to-channel
bridges without making any of those systems part of the daemon's identity. It
owns loopback port `46667`. If `GET /health` answers, another instance must not
be started on that machine.

The durable JSONL/REST mailbox and the Mattermost, IRC, Discord, Slack,
Matrix/Element, Telegram, WhatsApp Business, and Facebook Messenger adapters
are implemented. The routing envelope is designed for future Viber, Discourse,
LINE, and other adapters,
including direct bidirectional chat-platform bridges.
The REST mailbox remains available when no external chat adapter is configured.

## Start and control

Create an environment and install the project:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\.env.example config\.env
```

Start directly on Windows (the `.env` file is optional for mailbox-only use):

```powershell
run.bat
```

Other applications may discover and control the same external daemon through
its transport-neutral API:

```text
GET  http://127.0.0.1:8000/api/system/services
POST http://127.0.0.1:8000/api/system/services/channel-relay/start
POST http://127.0.0.1:8000/api/system/services/channel-relay/stop
POST http://127.0.0.1:8000/api/system/services/channel-relay/restart
```

The daemon is independent of FastAPI and survives development API reloads.
Runtime PID, status, and logs are under `mailbox/runtime/channel-relay-PORT/`.

## REST mailbox API on port 46667

```text
GET  /health
GET  /v1/status
GET  /v1/adapters
GET  /v1/listeners
GET  /v1/messages?recipient=IDENTITY
POST /v1/messages
GET  /page-demo
```

Example send:

```powershell
$body = @{
  from = 'my-codex-task'
  to = 'channel-relay'
  type = 'channel_send'
  text = 'Workflow completed'
  channel_type = 'mattermost'
  channel_id = 'CHANNEL_ID'
  workflow_run_id = 'run-123'
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:46667/v1/messages `
  -Method Post -ContentType application/json -Body $body
```

Example receive:

```powershell
Invoke-RestMethod 'http://127.0.0.1:46667/v1/messages?recipient=my-codex-task'
```

Receiving advances that recipient's durable cursor. Each concurrent consumer
must therefore use a unique, stable identity.

## `agent_mailbox.py` client

The client supports both direct filesystem access and the REST daemon. Set
`AGENT_MAILBOX_URL` or pass `--url` before the subcommand:

```powershell
$env:AGENT_MAILBOX_URL='http://127.0.0.1:46667'
python agent_mailbox.py status
python agent_mailbox.py send channel-relay 'Hello Mattermost' `
  --sender my-codex-task --channel-type mattermost --channel-id CHANNEL_ID
python agent_mailbox.py receive my-codex-task

python agent_mailbox.py --url http://127.0.0.1:46667 `
  send omegaclaw-core 'Please inspect this run' --sender my-codex-task
```

Download the matching standalone client directly from a running relay:

```powershell
Invoke-WebRequest http://127.0.0.1:46667/agent_mailbox.py -OutFile agent_mailbox.py
```

To make Codex poll the mailbox automatically, customize and paste
[`AUTOMATION_PROMPT.md`](src/mailbox_channel_relay_bridging_proxy/resources/AUTOMATION_PROMPT.md) into a recurring Codex task. It
contains PowerShell, WSL, Linux, REST, token, direct-JSONL, non-overlap, and
required-port instructions. Copying the files alone does not enable a Codex
automation; the user creates the recurring task once in Codex Desktop.

For the shortest installation path, open the target workspace in Codex and
paste the bootstrap prompt from
[`INSTALL_WITH_CODEX.md`](src/mailbox_channel_relay_bridging_proxy/resources/INSTALL_WITH_CODEX.md). It tells that Codex to create
`.codex/mailbox/`, inspect and download the client/template, validate both
PowerShell and WSL/Linux commands, customize the automation prompt, and then
show the user where to enable the recurring task in their own Codex UI.

The live relay also serves both onboarding documents:

```text
http://127.0.0.1:46667/INSTALL_WITH_CODEX.md
http://127.0.0.1:46667/AUTOMATION_PROMPT.md
```

## Trusted Speaker console client

`trusted-speaker UNIQUE_IDENTITY --to DESTINATION` opens the interactive
WebSocket console. The identity remains mandatory and must be unique per active
consumer. `mailbox-chat` is retained as a compatibility alias.

Use `--dir` before the subcommand to select a particular JSONL mailbox for one
invocation. This is useful when one agent participates in several independent
mailbox groups:

```powershell
python agent_mailbox.py --dir C:\snet\group-a\mailbox status
python agent_mailbox.py --dir C:\snet\group-b\mailbox receive my-agent
```

Transport precedence is explicit `--dir` or `--url`, then
`AGENT_MAILBOX_DIR`, then `AGENT_MAILBOX_URL`, then the local `mailbox/`
default. `--dir` and `--url` cannot be combined.

Named groups come from `config/mailboxes.json` (or `--config PATH`):

```powershell
python agent_mailbox.py --mailbox local status
python agent_mailbox.py --config C:\relay\groups.json --mailbox research follow worker-1
```

The full client surface includes:

- transport/identity: `--dir`, `--url`, `--mailbox`, `--config`, `--from`;
- reading: `receive`, `peek`, `poll`, `follow`, `unread-count`, `ack`;
- cursor/query controls: `--cursor`, `--no-advance`, `--ack`, `--since`,
  `--limit`, `--where FIELD=VALUE`, and `--wait`;
- resilience: `--timeout`, `--retry`, `--retry-delay`, and repeatable
  `--require-port` on monitoring commands;
- optional REST authentication: `--token` or `AGENT_MAILBOX_TOKEN`;
- REST inspection: `--curl` may appear anywhere in the command line and prints
  a token-redacted equivalent command and
  performs no network request;
- option termination: `--` stops command-line processing so following text may
  contain switch-looking values, for example
  `agent-mailbox send planner -- "--curl is literal text"`;
- presentation: `--format jsonl|json|text`, `--output`, `--quiet`, and
`--verbose`;
- diagnostics: `status`, `check`, and `--version`.

Use a stable `--cursor` name for each independent consumer. `peek` and
`--no-advance` never persist cursor progress; `ack` advances the selected
cursor through a specific message ID.

## Public attachment links

Set the externally reachable relay origin when IRC or another text-only
adapter must publish mailbox attachments as links:

```powershell
python server.py --host 0.0.0.0 --port 46667 `
  --public-url https://relay.example.com
```

The equivalent environment variable is `MAILBOX_RELAY_PUBLIC_URL`. Managed
files are served below `/v1/attachments/`; paths outside the mailbox's
`attachments/` directory are rejected. IRC automatically appends one public
URL per attachment. Configure the firewall, TLS reverse proxy, and public DNS
for the advertised URL as appropriate.

### Register a server token

Register a strong token locally in `config/.env`:

```powershell
mailbox-relay-token register
```

The command generates a strong token, stores it atomically as
`MAILBOX_RELAY_TOKEN`, and displays it once so it can be copied to authorized
clients. Check registration without revealing the token:

```powershell
mailbox-relay-token status
```

From an uninstalled checkout, use:

```powershell
$env:PYTHONPATH="$PWD\src"
python -m mailbox_channel_relay_bridging_proxy.token_admin register
```

Restart the relay after registering or rotating the token. On each authorized
client, set the displayed value without committing it:

```powershell
$env:AGENT_MAILBOX_TOKEN='<the displayed token>'
agent-mailbox --url https://relay.example.com --from worker-1 `
  send planner 'Finished the task'
```

For automated deployment, an existing secret of at least 32 characters can be
registered with `mailbox-relay-token register --token VALUE`, though passing a
secret on the command line may expose it in shell history. Supplying
`MAILBOX_RELAY_TOKEN` through a service secret manager is preferable.

Then expose the authenticated relay behind TLS:

```powershell
mailbox-channel-relay-proxy --host 0.0.0.0 --port 46667 `
  --public-url https://relay.example.com
```

The server reads `MAILBOX_RELAY_TOKEN`; clients use `AGENT_MAILBOX_TOKEN`.
When no server token is configured, REST mailbox routes
remain unauthenticated for backward-compatible local operation. Put TLS in
front of any Internet-facing deployment so the Bearer token is encrypted in
transit.

This lets OpenAI Codex use the relay with only Python and HTTP access; it does
not need the OmegaClaw repository or a shared filesystem mount.

## OmegaClaw and MeTTaClaw

Use stable transport-neutral recipients such as `omegaclaw-core`,
`omegaclaw-min`, or a deployment-specific `mettaclaw-*` identity. Poll through
REST or point existing JSONL adapters at `AGENT_MAILBOX_DIR`. To send to a chat
channel, address the message to `channel-relay` and include `channel_type` plus
`channel_id`. Preserve `thread_id`/`root_id`, `source_id`, attachments, and
workflow/run correlation fields when forwarding.

## Configuration and security

The daemon binds only `127.0.0.1`. Mattermost credentials are read from the
ignored `config/.env`:

```dotenv
MM_URL=https://mattermost.example
MM_BOT_TOKEN=...
MM_CHANNEL_ID=...
MM_CHANNEL_IDS=optional-second-channel,optional-third-channel
MATTERMOST_RELAY_RECIPIENTS=local-agent
MATTERMOST_RELAY_ENABLED=1
```

Never store tokens in workspace resources, workflow files, mailbox records, or
this README. Remote-machine access should be provided through an authenticated
proxy or tunnel; do not change the daemon to bind publicly without adding
authentication and authorization.

## Adapter status

`GET /v1/adapters` distinguishes installed adapters from planned adapters.
Currently `mattermost`, `irc`, `discord`, `matrix` (including Element clients),
`slack`, `telegram`, `whatsapp`, and `facebook_messenger` are implemented.
Discourse and Viber
are declared roadmap adapters; they are not falsely reported as operational.

## Listener registry

[`config/listeners.json`](config/listeners.json) is the non-secret source of truth for what
the proxy monitors and where inbound messages are mailboxed. Each listener has
an adapter, direction (`inbound`, `outbound`, or `bidirectional`), channel IDs,
and mailbox recipients. A channel ID beginning with `$` expands an environment
variable, so deployment-specific identifiers can stay in `.env`.

Credentials never belong in `listeners.json`. Adding Discord, IRC, or another
adapter means adding its implementation and then declaring its listeners in
the same registry. The daemon reloads the file as it evaluates listener routing,
so ordinary routing edits do not require a restart.

See [`INSTRUCTIONS.md`](docs/INSTRUCTIONS.md) for the implementation checklist,
complete operational contract, and Mattermost, Discord, Slack, IRC, Matrix/Element, Discourse,
WhatsApp, Viber, Telegram, LINE, REST, JSONL, and generic future-adapter examples.
See [`PLATFORM_SETUP.md`](docs/PLATFORM_SETUP.md) for the platform-side application,
bot, token, permission, channel/room ID, and installation steps.

## Message envelope

Required fields are `from`, `to`, `type`, `text`, `id`, and UTC `timestamp`.
Optional routing fields include `channel_type`, `channel_id`, `source_id`,
`thread_id`, `root_id`, `attachments`, `workflow_id`, `workflow_run_id`,
`operation_id`, and `correlation_id`. Unknown fields are preserved so workflow
and agent protocols can evolve independently of channel adapters.
