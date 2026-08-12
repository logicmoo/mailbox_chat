# Mailbox Channel Relay Bridging Proxy instructions

## Table of contents

- [Configuration and storage](#configuration-boundaries)
  - [Separate mailbox and configuration directories](#separate-mailbox-and-configuration-directories)
  - [Implementation checklist](#implementation-checklist)
- [Architecture and resilience](#deep-compatibility-invariant)
  - [Durable loop prevention](#durable-loop-prevention)
  - [Process resilience](#process-resilience)
  - [Agent-mediated bridges](#agent-mediated-bridge-architecture)
  - [Platform presences](#platform-presence-capability)
- [Listener configuration](#common-listener-fields)
- Implemented platform adapters
  - [Mattermost](#mattermost--implemented)
  - [Discord](#discord--implemented-adapter)
  - [Slack](#slack--implemented-adapter)
  - [IRC](#irc--implemented)
  - [Matrix / Element](#matrix--element--implemented-adapter)
  - [Telegram](#telegram--implemented-adapter)
  - [WhatsApp Business](#whatsapp-business--implemented-adapter)
  - [Facebook Messenger](#facebook-messenger--implemented-adapter)
- Planned platform contracts
  - [Viber](#viber--planned-contract)
  - [LINE](#line--planned-contract)
- Mailbox interfaces
  - [REST](#rest-mailbox--implemented)
  - [WebSocket chat](#websocket-chat--implemented)
  - [JSONL](#jsonl-mailbox--implemented)
- [OpenAI Codex integration](#openai-codex-installation-and-mailbox-integration--implemented)
- [Channel-to-channel bridges](#channel-to-channel-bridges)

This document is the operational contract for configuring mailbox listeners,
chat-platform adapters, and channel-to-channel bridges. The daemon owns
`127.0.0.1:46667`; a successful `GET /health` means an instance is already
running on this machine.

The port is configurable. Precedence is `--port`, then `MAILBOX_RELAY_PORT`,
then default `46667`. The host follows `--host`, `MAILBOX_RELAY_HOST`, then
safe default `127.0.0.1`:

```powershell
.\run.bat --port 47667
python server.py --host 127.0.0.1 --port 47667
$env:MAILBOX_RELAY_PORT='47667'
python server.py
```

The configuration syntax recognizes an all-interface request:

```powershell
python server.py --host 0.0.0.0 --port 46667
$env:MAILBOX_RELAY_HOST='0.0.0.0'
.\run.bat
```

`0.0.0.0` is only a server bind address. Local clients connect to
`http://127.0.0.1:46667`; remote clients use the machine's actual LAN or VPN
address.

Clients must use the matching URL. Runtime PID/status files are separated by
port under `mailbox/runtime/channel-relay-PORT/`, allowing intentional parallel
instances. Authentication is not implemented yet. Console and browser clients
show a service-unavailable message when connection or future authentication
requirements are not satisfied; they do not fabricate a working session.

### Separate mailbox and configuration directories

The non-overlapping defaults are `mailbox/` for mutable data and `config/` for
configuration. Override either independently:

```powershell
python server.py `
  --mailbox-dir D:\relay-data\production `
  --config-dir D:\relay-config\production
```

Equivalent environment variables are `AGENT_MAILBOX_DIR` and
`MAILBOX_RELAY_CONFIG_DIR`. The mailbox directory owns `messages.jsonl`,
`attachments/`, `cursors/`, `runtime/`, and the delivery ledger. The config
directory owns `.env`, `listeners.json`, and `mailboxes.json`. This permits shared
or read-only configuration with machine-local data, or several isolated proxy
instances without state overlap.

## Configuration boundaries

- `config/listeners.json` contains non-secret routing configuration.
- `config/.env` contains tokens, passwords, server URLs, and deployment identifiers.
- `mailbox/messages.jsonl`, `mailbox/attachments/`, and `mailbox/cursors/` are runtime data.
- Never place credentials in listener records, messages, workflow resources,
  logs, screenshots, or committed files.
- A listener is not operational until its adapter appears in `GET /v1/adapters`
  under `supported`. Entries under `planned` document future contracts only.

## Implementation checklist

- [x] JSONL mailbox
- [x] REST mailbox
- [x] WebSocket chat mailbox
- [x] Mattermost
- [x] Discord
- [x] Slack
- [x] IRC
- [x] WhatsApp Business
- [x] Facebook Messenger
- [ ] Viber
- [x] Telegram
- [ ] LINE
- [x] Matrix protocol (including Element clients)
- [ ] Discourse forums

Matrix is the adapter/protocol name. Element is one supported Matrix client,
so listener entries use `"adapter": "matrix"` rather than `"element"`.

Discourse listener entries use `"adapter": "discourse"`. Their channel IDs
represent topic, category, or tag scopes; inbound delivery should use webhooks
and outbound delivery should use the Discourse REST API.

Agent integrations:

- [x] OpenAI Codex CLI/Desktop through `agent_mailbox.py` and REST
- [x] Symbolic Learner Workbench
- [x] OmegaClaw/MeTTaClaw-compatible mailbox identities
- [ ] Client/server authentication

Check an adapter only after inbound and outbound delivery, authentication,
thread/source preservation where supported, echo suppression, failure records,
and deterministic adapter tests are implemented. Configuration examples alone
do not count as implementation.

## Deep compatibility invariant

Everything crosses the durable mailbox. The mailbox envelope—not Mattermost,
IRC, Codex, Workbench, or any agent framework—is the compatibility layer:

```text
external channel <-> adapter <-> endpoint agent <-> JSONL/REST mailbox
                                               <->
JSONL/REST mailbox <-> endpoint agent <-> adapter <-> external channel
```

Adapters never call other adapters directly. Agents never need platform SDKs
to communicate with one another. Every participant only reads and writes the
stable envelope. This preserves replayability, offline delivery, per-recipient
cursors, audit evidence, and compatibility between Windows, WSL, Codex,
Workbench, OmegaClaw, and future systems.

### Durable loop prevention

Every external event receives an immutable `origin_id` derived from its source
adapter, listener, and platform event ID. Relayed mailbox messages retain that
origin even though each mailbox append has a new `id`. The proxy keeps an
atomic SQLite traversal ledger under `runtime/`, keyed by `(origin_id,
endpoint_id)`. An endpoint includes adapter, listener/server, presence, and
channel. A delivery is allowed once per origin and endpoint; cyclic or repeated
routes are recorded as `channel_delivery_suppressed`. Failed reservations are
released so transient errors can be retried. This state survives daemon
restarts and supports concurrent adapter instances without relying on text
similarity or timestamps.

### Process resilience

The daemon does not terminate for adapter exceptions, network failures,
malformed external events, listener reload errors, or status-file write errors.
Its supervisor records the exception in `/health`, resets transient adapter
connections, reloads configuration, and retries with bounded exponential
backoff while REST and WebSocket service remains available on port 46667.

Only an explicit stop signal or an unrecoverable inability to own port 46667 is
a normal terminal condition. Process-level failures outside Python (machine
shutdown, forced termination, interpreter crash, or resource exhaustion) still
require an operating-system service manager if automatic process resurrection
is desired.

## Common listener fields

| Field | Meaning |
|---|---|
| `id` | Unique and stable listener identity |
| `adapter` | Transport such as `mattermost`, `discord`, or `irc` |
| `enabled` | Enables routing through this listener |
| `direction` | `inbound`, `outbound`, or `bidirectional` |
| `channel_ids` | Platform channel IDs; `$NAME` expands an environment variable |
| `bridge_agent` | Endpoint agent responsible for interpreting and forwarding traffic |
| `mailbox_recipients` | Local identities receiving inbound messages |
| `preserve_threads` | Preserve source thread/root IDs when supported |
| `presences` | Optional authorized platform identities exposed by the listener |

The daemon reloads `listeners.json` while evaluating routes. Normal routing
changes therefore do not require a daemon restart. Credential changes may
require reconnecting the affected adapter.

## Agent-mediated bridge architecture

Channel-to-channel traffic is never an unconditional raw copy. Every endpoint
has its own mailbox agent:

```text
Mattermost channel ↔ mattermost-bridge-agent
                   ↕ durable mailbox
IRC channel        ↔ irc-bridge-agent
```

The source adapter mailboxes an inbound envelope to its `bridge_agent`. That
agent applies endpoint-specific policy and addresses a message to the agent on
the other end. The destination agent then sends a `channel_send` envelope to
`channel-relay` with the destination `channel_type` and `channel_id`.

Separate endpoint agents are required because platforms differ in identity,
threading, message length, formatting, attachments, moderation, and rate
limits. They also provide a durable place for loop suppression, translation,
summarization, approval, and access-control policy. `mailbox_recipients` may
contain additional observers, but it does not replace the endpoint agent.

## Platform presence capability

Some platforms support one presence, some support several, and others do not
have a meaningful presence concept. A capable listener may declare authorized
presences explicitly:

```json
"presences": [
  {
    "id": "support-bot",
    "display_name": "Support Assistant",
    "mailbox_agent": "support-agent",
    "credential_profile": "SLACK_SUPPORT"
  },
  {
    "id": "operations-bot",
    "display_name": "Operations Assistant",
    "mailbox_agent": "operations-agent",
    "credential_profile": "SLACK_OPERATIONS"
  }
]
```

`credential_profile` is a non-secret reference; actual tokens remain in `.env`
or a secret manager. Outbound mailbox envelopes select `presence_id`, and
inbound envelopes record the receiving `presence_id`. Each adapter publishes
whether it supports no presence, one presence, or multiple presences and must
reject unsupported selections. Automated identities must remain disclosed and
must follow the platform's authorization rules rather than forging human users.

## Mattermost — implemented

```json
{
  "id": "mattermost-team",
  "adapter": "mattermost",
  "enabled": true,
  "direction": "bidirectional",
  "channel_ids": ["$MM_CHANNEL_ID", "$MM_CHANNEL_IDS"],
  "bridge_agent": "mattermost-bridge-agent",
  "mailbox_recipients": ["symbolic-workbench", "omegaclaw-core"],
  "include_direct_messages": true,
  "preserve_threads": true
}
```

Required `.env` values: `MM_URL`, `MM_BOT_TOKEN`, `MM_CHANNEL_ID`. Optional:
`MM_CHANNEL_IDS` and `MATTERMOST_RELAY_RECIPIENTS` for legacy fallback.

## Discord — implemented adapter

```json
{
  "id": "discord-community",
  "adapter": "discord",
  "enabled": true,
  "direction": "bidirectional",
  "server_id": "$DISCORD_GUILD_ID",
  "channel_ids": ["$DISCORD_CHANNEL_IDS"],
  "bridge_agent": "discord-bridge-agent",
  "mailbox_recipients": ["community-agent"],
  "include_direct_messages": false,
  "preserve_threads": true
}
```

Expected secret: `DISCORD_BOT_TOKEN`.

The adapter polls configured channels through Discord API v10, ignores its own
bot messages, records origin and destination identities in the durable ledger,
emits every inbound post through the mailbox, and supports outbound text and
file attachments. Set `listener_id` on outbound messages when multiple Discord
listeners are enabled.

## Slack — implemented adapter

```json
{
  "id": "slack-workspace",
  "adapter": "slack",
  "enabled": true,
  "direction": "bidirectional",
  "workspace_id": "$SLACK_WORKSPACE_ID",
  "channel_ids": ["$SLACK_CHANNEL_IDS"],
  "bridge_agent": "slack-bridge-agent",
  "presences": [
    {
      "id": "operations-bot",
      "display_name": "Operations Assistant",
      "mailbox_agent": "operations-agent",
      "credential_profile": "SLACK_OPERATIONS"
    },
    {
      "id": "support-bot",
      "display_name": "Support Assistant",
      "mailbox_agent": "support-agent",
      "credential_profile": "SLACK_SUPPORT"
    }
  ],
  "mailbox_recipients": ["operations-agent"],
  "include_direct_messages": true,
  "preserve_threads": true
}
```

Expected secrets: `SLACK_BOT_TOKEN` and, for Socket Mode, `SLACK_APP_TOKEN`.

## Matrix / Element — implemented adapter

The Matrix adapter uses the Client-Server API and works with rooms administered
through Element or another Matrix client. See `PLATFORM_SETUP.md` for account,
token, room, and encryption requirements.

## IRC — implemented

```json
{
  "id": "irc-network",
  "adapter": "irc",
  "enabled": true,
  "direction": "bidirectional",
  "server": "$IRC_SERVER",
  "port": 6697,
  "tls": true,
  "nickname": "mailbox-relay",
  "channel_ids": ["#agents", "#operations"],
  "bridge_agent": "irc-bridge-agent",
  "mailbox_recipients": ["irc-agent"],
  "preserve_threads": false
}
```

Optional secrets: `IRC_PASSWORD`, `IRC_NICKSERV_PASSWORD`, and TLS client
credentials when required by the network.

## WhatsApp Business — implemented adapter

```json
{
  "id": "whatsapp-business",
  "adapter": "whatsapp",
  "enabled": true,
  "direction": "bidirectional",
  "phone_number_id": "$WHATSAPP_PHONE_NUMBER_ID",
  "channel_ids": ["$WHATSAPP_ALLOWED_CONVERSATIONS"],
  "bridge_agent": "whatsapp-bridge-agent",
  "mailbox_recipients": ["customer-agent"],
  "preserve_threads": false
}
```

Expected secrets: `WHATSAPP_ACCESS_TOKEN` and webhook verification/signing
secrets. The adapter accepts signature-verified Cloud API webhooks at
`/v1/webhooks/whatsapp`, preserves contact and message identifiers, sends text,
uploads documents, and sends uploaded media by ID.

## Facebook Messenger — implemented adapter

```json
{
  "id": "facebook-messenger-primary",
  "adapter": "facebook_messenger",
  "enabled": false,
  "direction": "bidirectional",
  "page_id": "$FACEBOOK_PAGE_ID",
  "channel_ids": ["$FACEBOOK_ALLOWED_CONVERSATIONS"],
  "bridge_agent": "facebook-messenger-bridge-agent",
  "mailbox_recipients": ["symbolic-workbench"],
  "preserve_threads": false
}
```

Expected secrets: `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_VERIFY_TOKEN`, and
`FACEBOOK_APP_SECRET`. Inbound delivery requires verified, signature-checked
Meta webhooks at `/v1/webhooks/facebook-messenger`; outbound delivery uses the
Messenger Send API for text and file attachments. User profile names are
resolved through Graph and cached by source system and identifier.

## Viber — planned contract

```json
{
  "id": "viber-bot",
  "adapter": "viber",
  "enabled": true,
  "direction": "bidirectional",
  "channel_ids": ["$VIBER_ALLOWED_CONVERSATIONS"],
  "bridge_agent": "viber-bridge-agent",
  "mailbox_recipients": ["customer-agent"],
  "preserve_threads": false
}
```

Expected secret: `VIBER_AUTH_TOKEN`.

## Telegram — implemented adapter

```json
{
  "id": "telegram-bot",
  "adapter": "telegram",
  "enabled": true,
  "direction": "bidirectional",
  "channel_ids": ["$TELEGRAM_ALLOWED_CHAT_IDS"],
  "bridge_agent": "telegram-bridge-agent",
  "mailbox_recipients": ["community-agent"],
  "include_direct_messages": true,
  "preserve_threads": true
}
```

Expected secret: `TELEGRAM_BOT_TOKEN`. The adapter treats Telegram chat IDs as
opaque strings, preserves numeric message-thread IDs for forum topics, polls
inbound messages and channel posts, and sends outbound text and documents. It
uses `getChat` to glean readable names for configured chat IDs and retains names
learned from inbound updates in the durable identifier directory.

## LINE — planned contract

```json
{
  "id": "line-official-account",
  "adapter": "line",
  "enabled": true,
  "direction": "bidirectional",
  "channel_ids": ["$LINE_ALLOWED_SOURCE_IDS"],
  "bridge_agent": "line-bridge-agent",
  "mailbox_recipients": ["customer-agent"],
  "preserve_threads": false
}
```

Expected secrets: `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET`.
Allowed source IDs may represent users, groups, or rooms and must not be
confused with credentials.

## REST mailbox — implemented

REST clients address mailbox identities directly and do not need a polled chat
listener. Send to `POST /v1/messages`:

```json
{
  "from": "workflow-runner",
  "to": "symbolic-workbench",
  "type": "message",
  "text": "The workflow completed",
  "correlation_id": "run-123"
}
```

Receive through `GET /v1/messages?recipient=symbolic-workbench`. Receiving
advances that identity's durable cursor, so every concurrent consumer needs a
unique stable identity.

## WebSocket chat — implemented

The special client connects to the live mailbox view at:

```text
ws://127.0.0.1:46667/v1/chat/ws?recipient=UNIQUE_IDENTITY
```

The server first sends `{"type":"hello","protocol":"mailbox-chat.v1"}`.
Unread mailbox records arrive as `{"type":"message","message":{...}}` and
advance the same durable recipient cursor used by REST/JSONL receive. Send a
message through the mailbox with:

```json
{
  "action": "send",
  "to": "mattermost-bridge-agent",
  "text": "Hello from the special client",
  "correlation_id": "chat-123"
}
```

Use `{"action":"ping"}` for an application-level health check. Client frames
must be masked JSON text frames and are limited to 1 MiB. Only one active
consumer should use a recipient identity, regardless of whether it connects by
WebSocket, REST, or JSONL.

### Trusted Speaker console client

```powershell
trusted-speaker special-console-client --to symbolic-workbench
```

`mailbox-chat` remains a compatibility alias. From an uninstalled checkout, use
`python -m mailbox_channel_relay_bridging_proxy.console_client special-console-client --to symbolic-workbench`.
Trusted Speaker is the client role/name; `special-console-client` must still be
a unique stable mailbox identity so concurrent clients do not consume the same
cursor.
Commands are `/to ID`, `/ping`, `/help`, and `/quit`.

### Browser demonstration

With the daemon running, open `http://127.0.0.1:46667/page-demo`. The page is served
by the proxy and connects to the same-origin mailbox WebSocket endpoint. It is
a demonstration client; it does not bypass or replace the mailbox.

## JSONL mailbox — implemented

Filesystem clients use the same envelope without a listener entry:

```powershell
python agent_mailbox.py send symbolic-workbench "Please inspect this run" `
  --sender workflow-runner
python agent_mailbox.py receive workflow-runner
```

Supply either a mailbox directory (`AGENT_MAILBOX_DIR`) or the REST daemon
(`AGENT_MAILBOX_URL` or `--url`). Do not have multiple processes consume the
same recipient identity.

## OpenAI Codex installation and mailbox integration — implemented

The checked Codex integration means Codex can use the compatibility mailbox
through the CLI or REST interface. Codex itself is installed separately using
the [official OpenAI Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli).

### Windows

After installing and signing in to Codex, install this proxy in PowerShell:

```powershell
cd C:\path\to\mailbox_channel_relay_bridging_proxy
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\.env.example config\.env
.\run.bat
```

Give every concurrent Codex task a unique mailbox identity:

```powershell
$env:AGENT_MAILBOX_URL='http://127.0.0.1:46667'
python C:\path\to\mailbox_channel_relay_bridging_proxy\agent_mailbox.py `
  poll my-codex-project --interval 30 --checks 10 --require-port 46667
```

Put the selected identity and polling command in the consuming project's
`AGENTS.md` when it should be durable. Do not share a recipient identity across
simultaneous tasks because receiving advances that identity's cursor.

### WSL2

OpenAI's [official WSL instructions](https://learn.chatgpt.com/docs/windows/wsl)
document current Codex support through WSL2 and installing Codex inside Linux:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex

git clone YOUR_PROXY_REPOSITORY_URL ~/code/mailbox_channel_relay_bridging_proxy
cd ~/code/mailbox_channel_relay_bridging_proxy
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

When the daemon also runs inside WSL:

```bash
cp config/.env.example config/.env
.venv/bin/python server.py
export AGENT_MAILBOX_URL=http://127.0.0.1:46667
.venv/bin/python agent_mailbox.py status
```

When the daemon runs on Windows, first try
`curl http://127.0.0.1:46667/health` from WSL2. If local forwarding is not
available, use the Windows host address visible to that WSL distribution and
keep port 46667 protected. Prefer REST across the Windows/WSL boundary instead
of concurrently writing JSONL through `/mnt/c`.

## Channel-to-channel bridges

A bridge combines two agent-mediated listener endpoints. The endpoint agents
must preserve `source_id`, `channel_type`, `channel_id`, `thread_id`, `root_id`,
attachments, and correlation fields whenever the destination supports them.
Every adapter must suppress its own outbound echoes to prevent relay loops.

A future generic adapter entry starts disabled until implementation exists:

```json
{
  "id": "platform-instance",
  "adapter": "platform-name",
  "enabled": false,
  "direction": "bidirectional",
  "channel_ids": ["$PLATFORM_CHANNEL_IDS"],
  "bridge_agent": "platform-bridge-agent",
  "mailbox_recipients": ["local-consumer"],
  "preserve_threads": true
}
```

New adapters must validate their extra fields, authenticate without exposing
secrets through `/v1/listeners`, map inbound events to the stable mailbox
envelope, implement delivery failure records, and add deterministic mocked
tests for inbound, outbound, attachments, threads, and echo suppression.
