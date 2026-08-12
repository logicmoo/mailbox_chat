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
- [Register a relay token](#register-a-relay-token)
- [Platform-side setup](#platform-side-setup)
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
  - [Viber](#viber--implemented-adapter)
  - [LINE](#line--implemented-adapter)
- Mailbox interfaces
  - [CLI versus REST capabilities](#agent-mailbox-cli-versus-direct-rest)
  - [REST](#rest-mailbox--implemented)
  - [WebSocket chat](#websocket-chat--implemented)
  - [JSONL](#jsonl-mailbox--implemented)
- [OpenAI Codex integration](#openai-codex-installation-and-mailbox-integration--implemented)
- [Compatible agent runtimes](#compatible-agent-runtimes)
- [Channel-to-channel bridges](#channel-to-channel-bridges)

This document is the operational contract for configuring mailbox listeners,
chat-platform adapters, and channel-to-channel bridges. The daemon owns
`127.0.0.1:46667`; a successful `GET /health` means an instance is already
running on this machine.

Adapter failures are delivered to configured bridge agents and mailbox
recipients as `chat_server_status` messages. Their structured `diagnostic`
object lets an agent such as Codex explain the exception type, error message,
failed operation, and whether the relay will retry automatically. Credentials
and authorization headers are never intentionally included in these events.
The accompanying `service_context` includes safe listener IDs, channel IDs,
directions, current enabled/connected state, and the bounded retry policy.

The port is configurable. Precedence is `--port`, then `MAILBOX_RELAY_PORT`,
then default `46667`. The host follows `--host`, `MAILBOX_RELAY_HOST`, then
safe default `127.0.0.1`:

```powershell
.\mailbox-relay-server.cmd --port 47667
python server.py --host 127.0.0.1 --port 47667
$env:MAILBOX_RELAY_PORT='47667'
python server.py
```

The configuration syntax recognizes an all-interface request:

```powershell
python server.py --host 0.0.0.0 --port 46667
$env:MAILBOX_RELAY_HOST='0.0.0.0'
.\mailbox-relay-server.cmd
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
directory owns `.env`, `relays.json`, and `mailboxes.json`. This permits shared
or read-only configuration with machine-local data, or several isolated proxy
instances without state overlap.

## Configuration boundaries

- `config/relays.json` contains non-secret routing configuration.
- `config/.env` contains tokens, passwords, server URLs, and deployment identifiers.
- `mailbox/messages.jsonl`, `mailbox/attachments/`, and `mailbox/cursors/` are runtime data.
- `mailbox/runtime/identifier-directory.sqlite3` durably retains system-scoped
  UUID, contact, channel, group, and readable-name mappings between runs.
- Attachment storage defaults to 1 GiB per file and 25 GiB total; configure
  `--max-attachment-mb` and `--max-attachment-storage-mb` on the relay server.
- `messages.jsonl` defaults to a 5 GiB limit and each SQLite database to 1 GiB;
  configure `--max-jsonl-mb` and `--max-sqlite-mb` on the relay server.
- Never place credentials in listener records, messages, workflow resources,
  logs, screenshots, or committed files.
- A listener is not operational until its adapter appears in `GET /v1/adapters`
  under `supported`. Entries under `planned` document future contracts only.

## Register a relay token

Run token registration on the relay host, never through an unauthenticated
remote endpoint:

```powershell
mailbox-relay-token register
```

This generates a strong random value and atomically writes it to
`config/.env` as `MAILBOX_RELAY_TOKEN`. The value is printed once. Copy it into
the authorized client's secret environment as `AGENT_MAILBOX_TOKEN`, then
restart the relay. Confirm registration without revealing the secret:

```powershell
mailbox-relay-token status
```

For a non-default configuration directory, pass `--config-dir PATH` to both
the token command and relay daemon. Token rotation uses the same `register`
command and requires redistributing the new value to clients. Do not commit
`config/.env`, paste tokens into listener configuration, or expose them in
logs. Internet-facing relays require HTTPS because Bearer tokens otherwise
travel as plaintext.

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
- [x] Viber
- [x] Telegram
- [x] LINE
- [x] Matrix protocol (including Element clients)
- [x] Discourse forums

Matrix is the adapter/protocol name. Element is one supported Matrix client,
so listener entries use `"adapter": "matrix"` rather than `"element"`.

Discourse listener entries use `"adapter": "discourse"`. Their channel IDs
represent topic, category, or tag scopes; inbound delivery should use webhooks
and outbound delivery uses the Discourse REST API. Configure signed post
webhooks at `/v1/webhooks/discourse`; topic IDs map to channels and post numbers
map to reply threads. Outbound messages can reply or create a new topic.

Agent integrations:

- [x] OpenAI Codex CLI/Desktop through `agent_mailbox.py` and REST
- [x] Generic workflow and agent clients
- [x] OmegaClaw/MeTTaClaw-compatible mailbox identities
- [ ] Client/server authentication

Check an adapter only after inbound and outbound delivery, authentication,
thread/source preservation where supported, echo suppression, failure records,
and deterministic adapter tests are implemented. Configuration examples alone
do not count as implementation.

## Deep compatibility invariant

Everything crosses the durable mailbox. The mailbox envelope—not Mattermost,
IRC, Codex, or any agent framework—is the compatibility layer:

```text
external channel <-> adapter <-> endpoint agent <-> JSONL/REST mailbox
                                               <->
JSONL/REST mailbox <-> endpoint agent <-> adapter <-> external channel
```

Adapters never call other adapters directly. Agents never need platform SDKs
to communicate with one another. Every participant only reads and writes the
stable envelope. This preserves replayability, offline delivery, per-recipient
cursors, audit evidence, and compatibility between Windows, WSL, Codex,
agent runtimes and future systems.

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

The daemon reloads `relays.json` while evaluating routes. Normal routing
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
  "mailbox_recipients": ["local-agent", "automation-agent"],
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
through Element or another Matrix client. Account, token, room, and encryption
requirements are documented under [Platform-side setup](#matrix--element-setup).

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
  "groups_enabled": false,
  "preserve_threads": false
}
```

Expected secrets: `WHATSAPP_ACCESS_TOKEN` and webhook verification/signing
secrets. The adapter accepts signature-verified Cloud API webhooks at
`/v1/webhooks/whatsapp`, preserves contact and message identifiers, sends text,
uploads documents, and sends uploaded media by ID. Eligible WhatsApp Business
Groups API accounts may set `groups_enabled` and use group IDs as channel IDs;
group messages preserve both the group ID and participant ID. This does not
provide access to ordinary personal-account groups.

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
  "mailbox_recipients": ["local-agent"],
  "preserve_threads": false
}
```

Expected secrets: `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_VERIFY_TOKEN`, and
`FACEBOOK_APP_SECRET`. Inbound delivery requires verified, signature-checked
Meta webhooks at `/v1/webhooks/facebook-messenger`; outbound delivery uses the
Messenger Send API for text and file attachments. User profile names are
resolved through Graph and cached by source system and identifier.

## Viber — implemented adapter

```json
{
  "id": "viber-bot",
  "adapter": "viber",
  "enabled": true,
  "direction": "bidirectional",
  "token_env": "VIBER_AUTH_TOKEN",
  "channel_ids": ["$VIBER_ALLOWED_CONVERSATIONS"],
  "bridge_agent": "viber-bridge-agent",
  "mailbox_recipients": ["customer-agent"],
  "include_direct_messages": true,
  "preserve_threads": false,
  "bot_name": "Mailbox Relay"
}
```

Expected secret: `VIBER_AUTH_TOKEN`. Viber posts callbacks to
`/v1/webhooks/viber`; the relay verifies `X-Viber-Content-Signature` with the
matching bot token. Inbound user IDs and names are retained in the identifier
directory. Outbound text uses Viber's Bot API, while files up to Viber's 50 MiB
limit use the relay's public attachment URLs. Register an HTTPS webhook with a
trusted certificate; Viber does not accept self-signed webhook certificates.

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

## LINE — implemented adapter

```json
{
  "id": "line-official-account",
  "adapter": "line",
  "enabled": true,
  "direction": "bidirectional",
  "token_env": "LINE_CHANNEL_ACCESS_TOKEN",
  "secret_env": "LINE_CHANNEL_SECRET",
  "channel_ids": ["$LINE_ALLOWED_SOURCE_IDS"],
  "bridge_agent": "line-bridge-agent",
  "mailbox_recipients": ["customer-agent"],
  "include_direct_messages": true,
  "preserve_threads": true
}
```

Expected secrets: `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET`.
Allowed source IDs may represent users, groups, or rooms and must not be
confused with credentials. Signed webhooks arrive at `/v1/webhooks/line`.
Inbound images, video, audio, and files are downloaded through the managed
attachment quota. Text and public attachment links can be pushed to users,
group chats, and multi-person rooms; images use native LINE image messages.

## Platform-side setup

Never store platform secrets in `relays.json`. Put tokens and signing secrets
in `config/.env` or the process environment, and reference only their environment
variable names from listener entries.

### Mattermost setup

Create a bot account in the Mattermost system console, add it to the required
channels, then set `MM_URL`, `MM_BOT_TOKEN`, `MM_CHANNEL_ID`, and optional
`MM_CHANNEL_IDS`. Keep the token only in `config/.env` or a secret manager.

### Discord setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Follow Discord's [bot guide](https://docs.discord.com/developers/quick-start/getting-started), create the bot user, and store its token as `DISCORD_BOT_TOKEN`.
3. Generate an installation URL with the `bot` scope and install it into the server. See [OAuth2 and permissions](https://docs.discord.com/developers/platform/oauth2-and-permissions).
4. Grant only View Channel, Read Message History, Send Messages, and Attach Files where needed.
5. Enable Developer Mode, copy the channel IDs into `DISCORD_CHANNEL_IDS`, and enable the listener.

A webhook URL is only sufficient for outbound posting and is not the transport
used by this bidirectional adapter. Never place a bot token in an install URL,
listener file, mailbox message, or commit.

### Slack setup

1. Create a Slack app and bot installation using Slack's [authentication guide](https://api.slack.com/authentication).
2. Add `chat:write`, `files:write`, and the applicable `channels:history`, `groups:history`, `im:history`, or `mpim:history` scopes. See [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).
3. Install or reinstall the app and store its `xoxb-...` token as `SLACK_BOT_TOKEN`. See [OAuth installation](https://api.slack.com/authentication/oauth-v2).
4. Invite the bot to each required channel, copy channel IDs into `SLACK_CHANNEL_IDS`, and enable the listener.

Outbound messages use [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postmessage).
Separate listeners may use different `token_env` and `presence_id` values for
multiple bot presences or workspaces.

### Matrix / Element setup

1. Create a dedicated Matrix account on the selected homeserver.
2. Obtain its access token and store it as `MATRIX_ACCESS_TOKEN`. See the [Matrix bot introduction](https://matrix.org/docs/older/matrix-bot-sdk-intro/).
3. Invite or join the account to every relayed room.
4. Set `MATRIX_HOMESERVER`, put the canonical `!room:server` IDs—not display aliases—in `MATRIX_ROOM_IDS`, and enable the listener.

The implementation uses `/sync`, room-message events, and media uploads from
the [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/).

### Telegram setup

1. Create a bot with BotFather and store its token as `TELEGRAM_BOT_TOKEN`.
2. Add it to each required group, supergroup, or channel with the minimum read/send permissions.
3. Put numeric chat IDs or `@channelusername` values in `TELEGRAM_ALLOWED_CHAT_IDS` and enable the listener.
4. Enable `include_direct_messages` only when private conversations are explicitly in scope.

The adapter uses the [Telegram Bot API](https://core.telegram.org/bots/api),
long-polls `getUpdates`, resolves chat labels with `getChat`, and preserves forum
topic `message_thread_id` values.

### WhatsApp Business setup

Configure a Meta WhatsApp Business phone-number ID and conversation allowlist.
Store the system-user access token, webhook verification token, and app secret
in `config/.env`; register the public HTTPS callback at
`/v1/webhooks/whatsapp`. Meta template and 24-hour conversation rules apply.

Accounts approved for the restricted WhatsApp Business Groups API may set
`groups_enabled: true` and use approved group IDs as `channel_ids`. Ordinary
WhatsApp groups require the separate personal companion.

### Personal WhatsApp and ordinary groups

The optional `companions/whatsapp-personal` process uses `whatsapp-web.js`,
persistent `LocalAuth`, and linked-device QR login. It is not an official Meta
API. Set `WHATSAPP_PERSONAL_COMPANION_TOKEN`,
`WHATSAPP_PERSONAL_WEBHOOK_SECRET`, and optionally
`WHATSAPP_PERSONAL_SESSION_DIR`; run `npm install` in the companion directory,
start `whatsapp-personal-relay`, scan the QR code, and query authenticated
`GET /chats` for stable `@g.us` group IDs.

The companion binds to `127.0.0.1:46668`, protects its API with a Bearer token,
and signs callbacks to `/v1/webhooks/whatsapp-personal`. WhatsApp Web changes or
account restrictions can break this unofficial integration.

### Facebook Messenger setup

Configure a Facebook Page ID and permitted PSIDs. Store the Page access token,
webhook verification token, and app secret in `config/.env`; register the public
HTTPS callback at `/v1/webhooks/facebook-messenger` and grant the required Page
messaging permissions.

### Viber setup

Enable the listener, store the commercial bot token as `VIBER_AUTH_TOKEN`, and
configure `VIBER_ALLOWED_CONVERSATIONS` or `include_direct_messages`. Register:

```bash
curl -X POST https://chatapi.viber.com/pa/set_webhook \
  -H "X-Viber-Auth-Token: $VIBER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://relay.example.com/v1/webhooks/viber","event_types":["subscribed","unsubscribed","failed"],"send_name":true}'
```

Viber requires publicly trusted HTTPS, signs callbacks with
`X-Viber-Content-Signature`, and limits outbound files to 50 MiB.

### LINE setup

Create a LINE Official Account and Messaging API channel. Store its long-lived
token as `LINE_CHANNEL_ACCESS_TOKEN` and secret as `LINE_CHANNEL_SECRET`.
Enable the listener, configure `LINE_ALLOWED_SOURCE_IDS`, permit the Official
Account to join group and multi-person chats, and register this webhook with
redelivery enabled:

```text
https://relay.example.com/v1/webhooks/line
```

### Discourse setup

Enable the listener and set `DISCOURSE_URL`, `DISCOURSE_API_KEY`, and
`DISCOURSE_WEBHOOK_SECRET`. Configure a Discourse post webhook pointing to
`https://relay.example.com/v1/webhooks/discourse` with the same secret. Topic
IDs are channel IDs and post numbers are thread/reply IDs. The API-key user
must be able to read configured topics and create posts.

### IRC setup

Set `IRC_SERVER`, `IRC_CHANNELS`, and optional password or NickServ settings.
IRC has no native attachment upload, so configure `MAILBOX_RELAY_PUBLIC_URL`;
the adapter publishes managed files through `/v1/attachments/`.

## `agent-mailbox` CLI versus direct REST

With `--url`, `agent-mailbox` provides the common mailbox workflow over REST.
Direct REST additionally exposes administrative, integration, and live-chat
surfaces that the CLI does not currently wrap.

| Capability | `agent-mailbox` | Direct REST | Notes and limitations |
|---|---:|---:|---|
| Send messages | Yes | Yes | CLI wraps `POST /v1/messages`. |
| Receive or peek at messages | Yes | Yes | CLI wraps `GET /v1/messages` and controls cursor advancement. |
| Named cursors and acknowledgements | Yes | Yes | Explicit acknowledgement uses `POST /v1/ack`. |
| Poll and continuously follow | Yes | Client must implement | CLI repeatedly calls REST at the requested interval. |
| Count unread messages | Yes | Client must calculate | CLI peeks and counts the returned records locally. |
| Filter with `--where`, `--since`, and `--limit` | Yes | Client must calculate | These conveniences are applied by the CLI after retrieval. |
| JSON and JSONL output | Yes | JSON | CLI can render the REST response as JSONL. |
| Readable diagnostic text | Yes | No | `--format text` summarizes structured server diagnostics. |
| Bearer-token authentication | Yes | Yes | CLI accepts `--token` or `AGENT_MAILBOX_TOKEN`. |
| Server and adapter status | Partial | Yes | CLI `status` wraps `/v1/status`; direct REST also exposes `/v1/adapters`. |
| Listener inspection | No | Yes | Use `GET /v1/listeners`. |
| Route inspection and mutation | Separate command | Yes | Use `mailbox-relay-route`, or `GET/POST /v1/routes`. |
| Identifier/UUID directory | No | Yes | Use `GET/POST /v1/identifiers`. |
| Identifier-resolution requests | No | Yes | Use `GET/POST /v1/identifier-resolution-requests`. |
| WebSocket chat | No | Yes | Connect to `/v1/chat/ws`. |
| Platform webhook ingestion | No | Yes | Platform adapters expose their webhook routes on the server. |
| Attachment download | No dedicated command | Yes | Public attachments are served through the attachment endpoint. |
| Attachment upload from a remote client | Limited | Limited | CLI currently sends local paths; this only works when the server can access the same filesystem. A binary or multipart upload endpoint is still needed. |
| Read message text from a local file | Yes | Client must implement | `--input PATH` reads UTF-8 text on the CLI machine before sending. |
| Monitor a required TCP port | Yes | No | `--require-port` checks the CLI machine, not the remote relay host. |
| Operate without a running server | Yes, with `--dir` | No | Direct JSONL mode lacks live adapter, listener, route, and connection-state APIs. |

Prefer REST when several processes or machines share one relay: the server
centralizes storage and cursor changes. Direct JSONL mode is useful for a local,
serverless mailbox, but concurrent programs must not make unsafe independent
writes to its files.

## REST mailbox — implemented

REST clients address mailbox identities directly and do not need a polled chat
listener. Send to `POST /v1/messages`:

```json
{
  "from": "workflow-runner",
  "to": "local-agent",
  "type": "message",
  "text": "The workflow completed",
  "correlation_id": "run-123"
}
```

Receive through `GET /v1/messages?recipient=local-agent`. Receiving
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
trusted-speaker special-console-client --to local-agent
```

`mailbox-chat` remains a compatibility alias. From an uninstalled checkout, use
`python -m mailbox_channel_relay_bridging_proxy.console_client special-console-client --to local-agent`.
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
python agent_mailbox.py send local-agent "Please inspect this run" `
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

For recurring mailbox polling, customize
[`AUTOMATION_PROMPT.md`](../src/mailbox_channel_relay_bridging_proxy/resources/AUTOMATION_PROMPT.md)
and create the recurring task in Codex Desktop. Copying the file does not create
the automation. For workspace bootstrap, use
[`INSTALL_WITH_CODEX.md`](../src/mailbox_channel_relay_bridging_proxy/resources/INSTALL_WITH_CODEX.md).
The running relay also serves both documents at `/AUTOMATION_PROMPT.md` and
`/INSTALL_WITH_CODEX.md`.

### Windows

After installing and signing in to Codex, install this proxy in PowerShell:

```powershell
cd C:\path\to\mailbox_channel_relay_bridging_proxy
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\.env.example config\.env
.\mailbox-relay-server.cmd
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

## Compatible agent runtimes

Applications with JSONL or HTTP clients can use stable, transport-neutral
mailbox recipient names. OmegaClaw-compatible deployments commonly use
identities such as `omegaclaw-core` and `omegaclaw-min`; MeTTaClaw-compatible
deployments may use a deployment-specific `mettaclaw-*` identity. These are
consumer conventions, not identities built into the relay.

Poll through REST or point an existing JSONL adapter at `AGENT_MAILBOX_DIR`.
To send to a chat channel, address the message to `channel-relay` and include
`channel_type` plus `channel_id`. Forwarders should preserve
`thread_id`/`root_id`, `source_id`, attachments, and workflow/run correlation
fields. Python-and-HTTP clients do not require another application's repository
or a shared filesystem mount.

An external service manager may expose transport-neutral lifecycle endpoints
for applications that need to discover or control the relay daemon:

```text
GET  http://127.0.0.1:8000/api/system/services
POST http://127.0.0.1:8000/api/system/services/channel-relay/start
POST http://127.0.0.1:8000/api/system/services/channel-relay/stop
POST http://127.0.0.1:8000/api/system/services/channel-relay/restart
```

Port 8000 in this example belongs to that external service manager, not to the
mailbox relay. The relay's default REST port is 46667.

Example:

```powershell
agent-mailbox --url http://127.0.0.1:46667 `
  send omegaclaw-core 'Please inspect this run' --sender worker-1
```

## Channel-to-channel bridges

A bridge combines two agent-mediated listener endpoints. The endpoint agents
must preserve `source_id`, `channel_type`, `channel_id`, `thread_id`, `root_id`,
attachments, and correlation fields whenever the destination supports them.
Every adapter must suppress its own outbound echoes to prevent relay loops.

Routes in `config/relays.json` select one controller:

- `relay_agent` sends a `channel_route_request` to a mailbox identity for
  reasoning, moderation, translation, or approval.
- `presence_controller` emits deterministic mailbox delivery requests through
  the selected destination listener and presence without requiring an agent.

Trusted speakers listed in a listener's `trusted_admins` may manage routes from
chat with the portable ASCII command prefix:

```text
!relay routes
!relay attach slack-primary C0123456789 presence
!relay attach matrix-primary !room:example.org agent:moderating-router
!relay detach runtime-discord-primary-slack-primary-ab12cd34
```

The default `!relay` prefix works in IRC and every implemented chat adapter.
Override it with `MAILBOX_RELAY_COMMAND_PREFIX` or a listener-specific
`command_prefix`. Mailbox identities are open identifiers and do not require a
separate registration file.

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
