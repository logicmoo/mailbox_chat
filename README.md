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
Matrix/Element, Telegram, WhatsApp Business, Facebook Messenger, Viber, and LINE
adapters are implemented, including Discourse forums. The routing envelope supports other adapters,
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
mailbox-relay-server.cmd
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

On Windows, an uninstalled checkout includes a repository-local launcher that
works from any current directory:

```powershell
C:\path\to\mailbox_channel_relay_bridging_proxy\agent-mailbox.cmd status
C:\path\to\mailbox_channel_relay_bridging_proxy\agent-mailbox.cmd send agent-beta --input message.txt
```

Linux, macOS, and WSL checkouts include the equivalent POSIX launcher:

```bash
./agent-mailbox status
./agent-mailbox send agent-beta --input message.txt
./agent-mailbox send agent-beta -- '--curl is literal text'
```

After package installation, use the cross-platform `agent-mailbox` command.

## Repository-local commands

Every published command has a launcher in the repository root. From PowerShell:

```powershell
.\mailbox-relay-server.cmd --help
.\agent-mailbox.cmd --help
.\trusted-speaker.cmd --help
.\mailbox-relay-token.cmd --help
.\mailbox-chat.cmd --help
```

From Linux, macOS, or WSL:

```bash
./mailbox-relay-server --help
./agent-mailbox --help
./trusted-speaker --help
./mailbox-relay-token --help
./mailbox-chat --help
```

The launchers work from any current directory and prefer the repository's
`.venv` when it exists. `mailbox-chat` is the compatibility alias for
`trusted-speaker`.

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

Give `--url` the relay's normal HTTP(S) address; Trusted Speaker derives the
WebSocket endpoint automatically. A full `ws://` or `wss://` endpoint is also
accepted:

```bash
trusted-speaker speaker-one --url http://127.0.0.1:46667 --to agent-beta
```

Trusted Speaker can also operate directly on a local JSONL mailbox without a
running relay server:

```bash
trusted-speaker speaker-one --dir ./mailbox --to agent-beta
```

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
  `agent-mailbox send agent-beta -- "--curl is literal text"`;
- file-backed text: `--input PATH` reads the complete UTF-8 file as the message
  text and may appear anywhere before `--`, for example
  `agent-mailbox send agent-beta --input message.txt`;
- command documents: `--run command.json` executes an entire command described
  as JSON, without mixing additional CLI arguments;
- destination: `send` accepts either positional `RECIPIENT` or `--to RECIPIENT`,
  and `--to` may appear anywhere before `--`;
- presentation: `--format jsonl|json|text`, `--output`, `--quiet`, and
  `--verbose`;
- streaming: `--nobuffer` may appear anywhere before `--` and makes stdout and
  stderr line-buffered/write-through for immediate pipe and service-log output;

A complete command document uses normal option names without leading dashes:

```json
{
  "command": "send",
  "url": "https://relay.example.com",
  "recipient": "agent-beta",
  "text": "Finished --curl verification",
  "channel_type": "telegram",
  "channel_id": "123"
}
```

Run it with:

```bash
agent-mailbox --run command.json
```

For exact argument control, use `{"args": ["--dir", "mailbox", "status"]}`.
Do not store Bearer tokens in command documents; use `AGENT_MAILBOX_TOKEN`.
- diagnostics: `status`, `check`, and `--version`.

Use a stable `--cursor` name for each independent consumer. `peek` and
`--no-advance` never persist cursor progress; `ack` advances the selected
cursor through a specific message ID.

## Public attachment links

Set the externally reachable relay origin when IRC or another text-only
adapter must publish mailbox attachments as links:

```powershell
python server.py --host 0.0.0.0 --port 46667 `
  --public-address https://relay.example.com
```

`--public-url` remains an alias. The equivalent environment variable is
`MAILBOX_RELAY_PUBLIC_URL`. Managed
files are served below `/v1/attachments/`; paths outside the mailbox's
`attachments/` directory are rejected. IRC automatically appends one public
URL per attachment. Configure the firewall, TLS reverse proxy, and public DNS
for the advertised URL as appropriate.

Attachment storage is bounded by default to 1 GiB per file and 25 GiB total.
Set different limits when starting the server:

```powershell
mailbox-relay-server --max-attachment-mb 1024 --max-attachment-storage-mb 25600
```

The byte-based environment equivalents are
`MAILBOX_RELAY_MAX_ATTACHMENT_BYTES` and
`MAILBOX_RELAY_MAX_ATTACHMENT_STORAGE_BYTES`. New local attachments and
inbound platform downloads are rejected before either limit is exceeded.

The other durable stores are bounded independently: `messages.jsonl` defaults
to 5 GiB and each relay-owned SQLite database defaults to 1 GiB. Configure
these with `--max-jsonl-mb` and `--max-sqlite-mb`, or the byte-valued
`MAILBOX_RELAY_MAX_JSONL_BYTES` and `MAILBOX_RELAY_MAX_SQLITE_BYTES`
environment variables.

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
  send agent-beta 'Finished the task'
```

For automated deployment, an existing secret of at least 32 characters can be
registered with `mailbox-relay-token register --token VALUE`, though passing a
secret on the command line may expose it in shell history. Supplying
`MAILBOX_RELAY_TOKEN` through a service secret manager is preferable.

Then expose the authenticated relay behind TLS:

```powershell
mailbox-relay-server --host 0.0.0.0 --port 46667 `
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
`slack`, `telegram`, `whatsapp`, `facebook_messenger`, `viber`, and `line` are implemented.
Discourse topics and replies are operational through signed webhooks and its REST API.

## Listener registry

[`config/relays.json`](config/relays.json) is the non-secret source of truth for what
the proxy monitors and where inbound messages are mailboxed. Each listener has
an adapter, direction (`inbound`, `outbound`, or `bidirectional`), channel IDs,
and mailbox recipients. A channel ID beginning with `$` expands an environment
variable, so deployment-specific identifiers can stay in `.env`.

Credentials never belong in `relays.json`. Adding Discord, IRC, or another
adapter means adding its implementation and then declaring its listeners in
the same registry. The daemon reloads the file as it evaluates listener routing,
so ordinary routing edits do not require a restart.

## Import contacts and relay a whole channel

Import WhatsApp contacts from JSON, CSV, or vCard into the durable identifier
directory. Phone numbers are normalized to digits for WhatsApp addressing:

```bash
mailbox-relay-contacts --url http://127.0.0.1:46667 import contacts.vcf --system whatsapp
mailbox-relay-contacts --url http://127.0.0.1:46667 list --system whatsapp
```

To deliver every message from one IRC channel to a WhatsApp conversation, omit
the source `channel_id` to match every channel heard by that listener, or set it
to `#agents` to match only that channel:

```json
{
  "id": "irc-to-douglas-whatsapp",
  "enabled": true,
  "source": {"listener_id": "irc-primary", "channel_id": "#agents"},
  "destinations": [{
    "adapter": "whatsapp",
    "listener_id": "whatsapp-business-primary",
    "channel_id": "15551234567"
  }],
  "controller": {"type": "presence_controller", "presence_id": "whatsapp-business"}
}
```

This sends a separate Business API conversation to that WhatsApp recipient; it
does not claim to join an ordinary personal WhatsApp group.

### Ordinary WhatsApp groups (explicitly unofficial)

`whatsapp_personal` connects an ordinary WhatsApp or WhatsApp Business App
account through a separate WhatsApp Web companion. It supports existing DMs and
groups but is not a Meta-supported automation API and may break or put the
account at risk. Use a non-critical account and accept that risk explicitly.
The pinned upstream browser stack currently reports an unresolved high-severity
archive-extraction advisory during `npm audit`; install and run the companion
only on a controlled machine and reassess the audit when upgrading it.

```powershell
cd companions\whatsapp-personal
npm install
$env:WHATSAPP_PERSONAL_COMPANION_TOKEN = "two-independent-random-secrets"
$env:WHATSAPP_PERSONAL_WEBHOOK_SECRET = "use-a-different-random-secret"
$env:WHATSAPP_PERSONAL_RELAY_URL = "http://127.0.0.1:46667"
..\..\whatsapp-personal-relay.cmd
```

Scan the terminal QR code with Linked Devices. Session state stays in
`companions/whatsapp-personal/.session/` and is ignored by Git. Enable
`whatsapp-personal-primary` in `relays.json`, using the same two secrets for the
relay server and companion. Run only one companion per saved session.

Opaque identifier dictionaries are durable across runs. They are stored in
`mailbox/runtime/identifier-directory.sqlite3`; keep the same `--mailbox-dir`
and include that database in backups when UUID/contact/chat labels must survive
migration to another server.

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
