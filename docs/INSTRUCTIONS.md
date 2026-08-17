# Mailbox Channel Relay Bridging Proxy instructions

## Four-kind registry contract

Every top-level managed resource has exactly one of four `kind` values:
`agent`, `connector`, `channel`, or `relay`. Connector platforms use the
separate `adapter` field, while channel subtypes use `channel_type`. Relay
output addresses are fields on relays, not destination resources. Presence
data is optional nested session/account metadata and does not introduce
another managed kind.

## Table of contents

- [Configuration and storage](#configuration-boundaries)
  - [Separate mailbox and configuration directories](#separate-mailbox-and-configuration-directories)
  - [Implementation checklist](#implementation-checklist)
- [Architecture and resilience](#deep-compatibility-invariant)
  - [Durable loop prevention](#durable-loop-prevention)
  - [Process resilience](#process-resilience)
  - [Agent-mediated bridges](#agent-mediated-bridge-architecture)
  - [Platform presences](#platform-presence-capability)
- [Connector configuration](#common-connector-fields)
- [Register a relay token](#register-a-relay-token)
- Implemented platform adapters
  - [Mattermost](#mattermost--implemented)
  - [Discord](#discord--implemented-adapter)
  - [Slack](#slack--implemented-adapter)
  - [Matrix / Element](#matrix--element--implemented-adapter)
  - [IRC](#irc--implemented)
  - [WhatsApp Business](#whatsapp-business--implemented-adapter)
  - [Personal WhatsApp](#personal-whatsapp--implemented-companion)
  - [Facebook Messenger](#facebook-messenger--implemented-adapter)
  - [Viber](#viber--implemented-adapter)
  - [Telegram](#telegram--implemented-adapter)
  - [LINE](#line--implemented-adapter)
  - [Discourse](#discourse--implemented-adapter)
- [Client setup](#client-setup)
- Mailbox interfaces
  - [CLI versus REST capabilities](#mailbox-client-cli-versus-direct-rest)
  - [REST](#rest-mailbox--implemented)
  - [WebSocket chat](#websocket-chat--implemented)
  - [JSONL](#jsonl-mailbox--implemented)
- [OpenAI Codex integration](#openai-codex-installation-and-mailbox-integration--implemented)
- [Compatible agent runtimes](#compatible-agent-runtimes)
- [Channel-to-channel bridges](#channel-to-channel-bridges)

This document is the operational contract for configuring mailbox connectors,
chat-platform adapters, and channel-to-channel bridges. The daemon owns
`127.0.0.1:46667`; a successful `GET /health` means an instance is already
running on this machine.

Adapter failures are delivered to configured bridge agents and mailbox
recipients as `chat_server_status` messages. Their structured `diagnostic`
object lets an agent such as Codex explain the exception type, error message,
failed operation, and whether the relay will retry automatically. Credentials
and authorization headers are never intentionally included in these events.
The accompanying `service_context` includes safe connector IDs, channel IDs,
directions, current enabled/connected state, and the bounded retry policy.

The relay also publishes every adapter lifecycle transition and safe diagnostic
to the built-in local `server_events` channel. Like a Mattermost channel, it
has subscribers: each subscribed agent receives its own mailbox copy and keeps
its own cursor. Subscribe once, then poll the agent's normal identity:

```powershell
mailbox-client subscribe server_events --to symbolic-workbench-codex
mailbox-client follow --to symbolic-workbench-codex --format text --nobuffer
```

Inspect or remove subscriptions with
`mailbox-client subscriptions --to symbolic-workbench-codex` and
`mailbox-client unsubscribe server_events --to symbolic-workbench-codex`.
Do not poll `--to server_events`: it is a publish/subscribe channel name, not a
competing-consumer mailbox identity.

The server saves subscription membership under `subscriptions` in
`config/relays.json` and reloads it after restart:

```json
{
  "subscriptions": [
    {
      "id": "server_events",
      "subscribers": ["symbolic-workbench-codex"]
    },
    {
      "id": "mm/chat.snt/3423423434234",
      "subscribers": ["symbolic-workbench-codex", "omegaclaw-min"]
    }
  ]
}
```

### Stable agents and multiple presences

Use `agent_id` for the durable logical agent. Do not create a new mailbox
identity merely because the same agent is connected through another UI or chat
service. An agent can own any number of presences; three or four simultaneous
presences are normal:

```json
{
  "agents": [{
    "agent_id": "symbolic-workbench-codex",
    "presences": [
      {"presence_id": "symbolic-codex-app"},
      {"presence_id": "symbolic-console"},
      {"presence_id": "symbolic-mailbox"},
      {"presence_id": "symbolic-mm"}
    ]
  }]
}
```

A platform connector associates one live connection with that agent:

```json
{
  "id": "mattermost-primary",
  "adapter": "mattermost",
  "agent_id": "symbolic-workbench-codex",
  "presence_id": "symbolic-mm"
}
```

The connector automatically includes its `agent_id` among inbound mailbox
recipients. `presence_id` records which concrete connection observed or sent a
message; polling, acknowledgements, cursors, and subscriptions continue to use
the stable agent ID. Presence IDs must be globally unique, and a connector's
presence must belong to its declared agent. Older connector-only configuration
continues to work.

Long-running agents can declaratively and idempotently ensure their saved
subscriptions as part of the poll command:

```powershell
mailbox-client poll --to symbolic-workbench-codex `
  --subscribed server_events,mm/chat.snt/3423423434234,mm/chat.snt/2342444444444 `
  --interval 30 --checks 11
```

This means “ensure `symbolic-workbench-codex` is subscribed to these channels,
then retrieve messages addressed to it.” Repeating the command does not create
duplicate subscriptions or deliveries. Ordinary configured
`mailbox_recipients` and saved endpoint subscribers are combined. The older
`mm_CHANNEL_ID` subscription key remains readable for compatibility, but new
configuration and help use `mm/SERVER/ID`.

Routine successful poll cycles are not persisted there; state changes and
actionable diagnostics are. Platform tokens, signing secrets, passwords, and
authorization headers are redacted.

The port is configurable. Precedence is `--port`, then `MAILBOX_RELAY_PORT`,
then default `46667`. The host follows `--host`, `MAILBOX_RELAY_HOST`, then
safe default `127.0.0.1`:

```powershell
.\mailbox-server.cmd --port 47667
.\mailbox-server.cmd --host 127.0.0.1 --port 47667
$env:MAILBOX_RELAY_PORT='47667'
.\mailbox-server.cmd
```

The configuration syntax recognizes an all-interface request:

```powershell
.\mailbox-server.cmd --host 0.0.0.0 --port 46667
$env:MAILBOX_RELAY_HOST='0.0.0.0'
.\mailbox-server.cmd
```

`0.0.0.0` is only a server bind address. Local clients connect to
`http://127.0.0.1:46667`; remote clients use the machine's actual LAN or VPN
address.

Clients must use the matching URL. Runtime PID/status files are separated by
port under `mailbox/runtime/channel-relay-PORT/`, allowing intentional parallel
instances. Configure optional REST Bearer authentication with
`MAILBOX_RELAY_TOKEN`; clients supply the same secret through
`AGENT_MAILBOX_TOKEN`. Console and browser clients show a service-unavailable
or authentication error when requirements are not satisfied; they do not
fabricate a working session.

### Separate mailbox and configuration directories

The non-overlapping defaults are `mailbox/` for mutable data and `config/` for
configuration. Override either independently:

```powershell
.\mailbox-server.cmd `
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
- Never place credentials in connector records, messages, workflow resources,
  logs, screenshots, or committed files.
- A connector is not operational until its adapter appears in `GET /v1/adapters`
  under `supported`. Entries under `planned` document future contracts only.

## Register a relay token

Run token registration on the relay host, never through an unauthenticated
remote endpoint:

```powershell
mailbox-client token register
```

This generates a strong random value and atomically writes it to
`config/.env` as `MAILBOX_RELAY_TOKEN`. The value is printed once. Copy it into
the authorized client's secret environment as `AGENT_MAILBOX_TOKEN`, then
restart the relay. Confirm registration without revealing the secret:

```powershell
mailbox-client token status
```

For a non-default configuration directory, pass `--config-dir PATH` to both
the token command and relay daemon. Token rotation uses the same `register`
command and requires redistributing the new value to clients. Do not commit
`config/.env`, paste tokens into connector configuration, or expose them in
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
so connector entries use `"adapter": "matrix"` rather than `"element"`.

Discourse connector entries use `"adapter": "discourse"`. Their channel IDs
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
adapter, connector, and platform event ID. Relayed mailbox messages retain that
origin even though each mailbox append has a new `id`. The proxy keeps an
atomic SQLite traversal ledger under `runtime/`, keyed by `(origin_id,
endpoint_id)`. An endpoint includes adapter, connector/server, presence, and
channel. A delivery is allowed once per origin and endpoint; cyclic or repeated
routes are recorded as `channel_delivery_suppressed`. Failed reservations are
released so transient errors can be retried. This state survives daemon
restarts and supports concurrent adapter instances without relying on text
similarity or timestamps.

### Process resilience

The daemon does not terminate for adapter exceptions, network failures,
malformed external events, connector reload errors, or status-file write errors.
Its supervisor records the exception in `/health`, resets transient adapter
connections, reloads configuration, and retries with bounded exponential
backoff while REST and WebSocket service remains available on port 46667.

Only an explicit stop signal or an unrecoverable inability to own port 46667 is
a normal terminal condition. Process-level failures outside Python (machine
shutdown, forced termination, interpreter crash, or resource exhaustion) still
require an operating-system service manager if automatic process resurrection
is desired.

## Common connector fields

| Field | Meaning |
|---|---|
| `id` | Unique and stable connector identity |
| `adapter` | Transport such as `mattermost`, `discord`, or `irc` |
| `enabled` | Enables routing through this connector |
| `direction` | `inbound`, `outbound`, or `bidirectional` |
| `channel_ids` | Platform channel IDs; `$NAME` expands an environment variable |
| `bridge_agent` | Endpoint agent responsible for interpreting and forwarding traffic |
| `mailbox_recipients` | Local identities receiving inbound messages |
| `preserve_threads` | Preserve source thread/root IDs when supported |
| `presences` | Optional authorized platform identities exposed by the connector |

The daemon reloads `relays.json` while evaluating connectors and relays. Normal
configuration changes therefore do not require a daemon restart. Credential changes may
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
`outbound_delivery` with the destination `channel_type` and `channel_id`.

Separate endpoint agents are required because platforms differ in identity,
threading, message length, formatting, attachments, moderation, and rate
limits. They also provide a durable place for loop suppression, translation,
summarization, approval, and access-control policy. `mailbox_recipients` may
contain additional observers, but it does not replace the endpoint agent.

## Platform presence capability

Some platforms support one presence, some support several, and others do not
have a meaningful presence concept. A capable connector may declare authorized
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

The platform sections below combine connector configuration, adapter behavior,
and platform-side setup. Never store platform secrets in `relays.json`; put
tokens and signing secrets in `config/.env` or the process environment and
reference only their environment-variable names from connector entries.

## Mattermost — implemented

### Setup

Create a bot account in the Mattermost system console and add it to the
required channels. Put the instance, URL, and channel IDs in `relays.json`;
keep only the token in `config/.env` or a secret manager.

```json
{
  "id": "mattermost-team",
  "adapter": "mattermost",
  "instance": "chat.singularitynet.io",
  "base_url": "https://chat.singularitynet.io",
  "token_env": "MM_BOT_TOKEN",
  "enabled": true,
  "direction": "bidirectional",
  "channel_ids": ["CHANNEL_ID", "ANOTHER_CHANNEL_ID"],
  "bridge_agent": "mattermost-bridge-agent",
  "mailbox_recipients": ["console-default-agent", "automation-agent"],
  "include_direct_messages": true,
  "preserve_threads": true
}
```

Required `.env` value: `MM_BOT_TOKEN` (or the variable named by `token_env`).
`MM_URL`, `MM_CHANNEL_ID`, and `MM_CHANNEL_IDS` remain legacy fallbacks when
the equivalent JSON fields are omitted. `MATTERMOST_RELAY_RECIPIENTS` remains
an optional legacy recipient fallback.

## Discord — implemented adapter

### Setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Follow Discord's [bot guide](https://docs.discord.com/developers/quick-start/getting-started), create the bot user, and store its token as `DISCORD_BOT_TOKEN`.
3. Generate an installation URL with the `bot` scope and install it into the server. See [OAuth2 and permissions](https://docs.discord.com/developers/platform/oauth2-and-permissions).
4. Grant only View Channel, Read Message History, Send Messages, and Attach Files where needed.
5. Enable Developer Mode, copy channel IDs into `DISCORD_CHANNEL_IDS`, and enable the connector.

A webhook URL is only sufficient for outbound posting and is not the transport
used by this bidirectional adapter. Never place a bot token in an install URL,
connector file, mailbox message, or commit.

```json
{
  "id": "discord-community",
  "adapter": "discord",
  "enabled": true,
  "direction": "bidirectional",
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
file attachments. Set `connector_id` on outbound messages when multiple Discord
connectors are enabled.

## Slack — implemented adapter

### Setup

1. Create a Slack app and bot using Slack's [authentication guide](https://api.slack.com/authentication).
2. Add `chat:write`, `files:read`, `files:write`, and the applicable `channels:history`, `groups:history`, `im:history`, or `mpim:history` scopes. See [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).
3. Install or reinstall the app and store its `xoxb-...` token as `SLACK_BOT_TOKEN`. See [OAuth installation](https://api.slack.com/authentication/oauth-v2).
4. Invite the bot to each required channel, copy channel IDs into `SLACK_CHANNEL_IDS`, and enable the connector.

Outbound messages use [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postmessage).
This adapter polls Slack's Web API; it does not use Socket Mode. Create a
separate connector with its own `token_env` and `presence_id` for every bot
identity or workspace.

```json
{
  "id": "slack-workspace",
  "adapter": "slack",
  "enabled": true,
  "direction": "bidirectional",
  "workspace_id": "$SLACK_WORKSPACE_ID",
  "token_env": "SLACK_BOT_TOKEN",
  "channel_ids": ["$SLACK_CHANNEL_IDS"],
  "bridge_agent": "slack-bridge-agent",
  "presence_id": "operations-bot",
  "mailbox_recipients": ["operations-agent"],
  "include_direct_messages": true,
  "preserve_threads": true
}
```

Expected secret: `SLACK_BOT_TOKEN`.

## Matrix / Element — implemented adapter

### Setup

1. Create a dedicated Matrix account on the selected homeserver.
2. Obtain its access token and store it as `MATRIX_ACCESS_TOKEN`. See the [Matrix bot introduction](https://matrix.org/docs/older/matrix-bot-sdk-intro/).
3. Invite or join the account to every relayed room.
4. Set `MATRIX_HOMESERVER`, put canonical `!room:server` IDs—not display aliases—in `MATRIX_ROOM_IDS`, and enable the connector.

The implementation uses `/sync`, room-message events, and media uploads from
the [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/).
It reads plaintext `m.room.message` events; end-to-end encrypted room
decryption is not implemented, so relay rooms must be unencrypted.

```json
{
  "id": "matrix-homeserver",
  "adapter": "matrix",
  "enabled": true,
  "direction": "bidirectional",
  "homeserver": "$MATRIX_HOMESERVER",
  "token_env": "MATRIX_ACCESS_TOKEN",
  "channel_ids": ["$MATRIX_ROOM_IDS"],
  "bridge_agent": "matrix-bridge-agent",
  "mailbox_recipients": ["community-agent"],
  "preserve_threads": true
}
```

The Matrix adapter uses the Client-Server API and works with rooms administered
through Element or another Matrix client.

## IRC — implemented

### Setup

Set `IRC_SERVER`, `IRC_CHANNELS`, and optional `IRC_PASSWORD`.
IRC has no native attachment upload, so configure `MAILBOX_RELAY_PUBLIC_URL`;
the adapter publishes managed files through `/v1/attachments/`.

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

Optional secret: `IRC_PASSWORD`. NickServ identification and TLS client
certificates are not currently implemented by the adapter.

## WhatsApp Business — implemented adapter

### Setup

Configure a Meta WhatsApp Business phone-number ID and conversation allowlist.
Store the system-user access token as `WHATSAPP_ACCESS_TOKEN`, the verification
token as `WHATSAPP_VERIFY_TOKEN`, and the app secret as `WHATSAPP_APP_SECRET`
in `config/.env`; register the public HTTPS callback at
`/v1/webhooks/whatsapp`. Meta template and 24-hour conversation rules apply.
Accounts approved for the restricted Groups API may set `groups_enabled: true`
and use approved group IDs as `channel_ids`.

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

Expected secrets: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, and
`WHATSAPP_APP_SECRET`. The adapter accepts signature-verified Cloud API webhooks at
`/v1/webhooks/whatsapp`, preserves contact and message identifiers, sends text,
uploads documents, and sends uploaded media by ID. Eligible WhatsApp Business
Groups API accounts may set `groups_enabled` and use group IDs as channel IDs;
group messages preserve both the group ID and participant ID. This does not
provide access to ordinary personal-account groups.

## Personal WhatsApp — implemented companion

### Setup

Set `WHATSAPP_PERSONAL_COMPANION_TOKEN`,
`WHATSAPP_PERSONAL_WEBHOOK_SECRET`, and optionally
`WHATSAPP_PERSONAL_SESSION_DIR`. Run `npm install` and then `npm start` in
`companions/whatsapp-personal`, scan the QR code, and query authenticated
`GET /chats` for stable `@g.us` group IDs.

The companion binds to `127.0.0.1:46668`, protects its API with a Bearer token,
and signs callbacks to `/v1/webhooks/whatsapp-personal`. WhatsApp Web changes or
account restrictions can break this unofficial integration.

The optional `companions/whatsapp-personal` process handles ordinary direct and
group chats using `whatsapp-web.js`, persistent `LocalAuth`, and linked-device
QR login. It is not an official Meta API.

## Facebook Messenger — implemented adapter

### Setup

Configure a Facebook Page ID and permitted PSIDs. Store the Page access token
as `FACEBOOK_PAGE_ACCESS_TOKEN`, verification token as `FACEBOOK_VERIFY_TOKEN`,
and app secret as `FACEBOOK_APP_SECRET` in `config/.env`; register the public
HTTPS callback at `/v1/webhooks/facebook-messenger` and grant the required Page
messaging permissions.

```json
{
  "id": "facebook-messenger-primary",
  "adapter": "facebook_messenger",
  "enabled": false,
  "direction": "bidirectional",
  "page_id": "$FACEBOOK_PAGE_ID",
  "channel_ids": ["$FACEBOOK_ALLOWED_CONVERSATIONS"],
  "bridge_agent": "facebook-messenger-bridge-agent",
  "mailbox_recipients": ["console-default-agent"],
  "preserve_threads": false
}
```

Expected secrets: `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_VERIFY_TOKEN`, and
`FACEBOOK_APP_SECRET`. Inbound delivery requires verified, signature-checked
Meta webhooks at `/v1/webhooks/facebook-messenger`; outbound delivery uses the
Messenger Send API for text and file attachments. User profile names are
resolved through Graph and cached by source system and identifier.

## Viber — implemented adapter

### Setup

Enable the connector, store the commercial bot token as `VIBER_AUTH_TOKEN`, and
configure `VIBER_ALLOWED_CONVERSATIONS` or `include_direct_messages`. Register:

```bash
curl -X POST https://chatapi.viber.com/pa/set_webhook \
  -H "X-Viber-Auth-Token: $VIBER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://relay.example.com/v1/webhooks/viber","event_types":["message","conversation_started","subscribed","unsubscribed","failed"],"send_name":true}'
```

Viber requires publicly trusted HTTPS and limits outbound files to 50 MiB.

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

### Setup

1. Create a bot with BotFather and store its token as `TELEGRAM_BOT_TOKEN`.
2. Add it to each required group, supergroup, or channel with minimum read/send permissions.
3. Put numeric chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS` and enable the connector. Use the numeric ID even for public channels because inbound updates identify chats numerically.
4. Enable `include_direct_messages` only when private conversations are explicitly in scope.

The adapter uses the [Telegram Bot API](https://core.telegram.org/bots/api),
long-polls `getUpdates`, resolves labels with `getChat`, and preserves forum
topic `message_thread_id` values.

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

### Setup

Create a LINE Official Account and Messaging API channel. Store its long-lived
token as `LINE_CHANNEL_ACCESS_TOKEN` and secret as `LINE_CHANNEL_SECRET`.
Configure `LINE_ALLOWED_SOURCE_IDS`, permit the account to join group and
multi-person chats, and register this webhook with redelivery enabled:

```text
https://relay.example.com/v1/webhooks/line
```

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

## Discourse — implemented adapter

### Setup

Enable the connector and set `DISCOURSE_URL`, `DISCOURSE_API_KEY`, and
`DISCOURSE_WEBHOOK_SECRET`. Configure a post webhook pointing to
`https://relay.example.com/v1/webhooks/discourse` with the same secret. The
API-key user must be able to read configured topics and create posts. Set the
connector's `api_username` when the key does not belong to the default `system`
user; use `category_ids` to filter inbound posts and `default_category_id` when
creating a new topic.

The Discourse adapter accepts signature-verified post webhooks and sends posts
through the Discourse API. Topic IDs are channel IDs; post numbers are
thread/reply IDs. Outbound messages can reply to a topic or create one when
`topic_title` is supplied.

## Client setup

Every command is available from the repository root and prefers the local
`.venv` when present:

| Purpose | Windows launcher | Linux, macOS, or WSL launcher |
|---|---|---|
| Relay server | `mailbox-server.cmd` | `./mailbox-server` |
| Mailbox client | `mailbox-client.cmd` | `./mailbox-client` |
| Interactive console | `mailbox-console.cmd` | `./mailbox-console` |
| Token administration | `mailbox-client.cmd token` | `./mailbox-client token` |
| Contact administration | `mailbox-client.cmd contacts` | `./mailbox-client contacts` |

After package installation, use the command names directly. Every command has
comprehensive `--help` output.

### `mailbox-client`

`mailbox-client` is the single command for mailbox operations and server
administration.

Identity and endpoint switches have distinct meanings:

| Switch | Meaning |
|---|---|
| `--as IDENTITY` | Stable local mailbox identity performing the operation. |
| `--from ENDPOINT` | External conversation source to subscribe `--as` to. |
| `--to IDENTITY` | Direct mailbox destination or receiving identity. |
| `--to ENDPOINT` | External channel/person destination for `send`. |

Mattermost endpoints use `mm/SERVER/ID`. For example:

```powershell
mailbox-client poll --as symbolic-workbench-codex `
  --from mm/chat.snt/3423423434234 --interval 30 --checks 11

mailbox-client send --as symbolic-workbench-codex `
  --to mm/chat.snt/3423423434234 "Hello channel or person"
```

Mattermost channel and user IDs have the same shape. If the final ID matches a
configured channel, the relay posts there. Otherwise it asks Mattermost to
create or find the bot's direct-message channel with that user and sends the
message privately. The persistent identifier directory remembers platform ID
kind and readable names learned from inbound traffic and resolution requests.

Channel discovery also records a channel's display name and URL slug. Once a
record has been downloaded, all of these forms resolve to the same channel:

```powershell
mailbox-client names c83yjesfejgbmdptwtjqgqis9h
mailbox-client names "Image Perception to Recognizable Memory and ARC3"
mailbox-client names image-perception-to-recognizable-memory-and-arc3
mailbox-client names mm/chat.singularitynet.io/c83yjesfejgbmdptwtjqgqis9h
mailbox-client names --on mm https://chat.singularitynet.io/chat/channels/image-perception-to-recognizable-memory-and-arc3
```

The same inference applies to a downloaded user ID, for example
`mailbox-client whois j4pok4rbqtfytcrcn8d3nhgkto`. Downloaded users also
resolve by username, email, nonblank nickname, and combined first/last name.
A full Mattermost web URL
must contain `/channels/CHANNEL_SLUG`, and its hostname must match the active
Mattermost connection.

For inbound traffic, `--from mm/chat.snt/ID` is an idempotent subscription. A
matching configured channel delivers all its posts; a matching Mattermost user
ID delivers that person's direct/channel posts observed by the connector. The
client still polls the mailbox named by `--as`.

### Endpoint address types

Every chat address has the same `TYPE/INSTANCE/SOURCE_OR_DESTINATION` shape.
Instance `0` always means the configured/default instance, so
`mm/0/CHANNEL_ID` is valid without knowing the Mattermost hostname. The same
shortcut works for every adapter type. Use an explicit instance when selecting
among multiple configured servers or accounts.
Instance `0` is resolved dynamically and is not a duplicate identifier-registry
namespace. Registry rows use the canonical concrete instance, such as
`mm/chat.singularitynet.io`.
The final component is opaque and keeps the platform's native ID:

| Platform | Type | Instance convention | Example source/destination |
|---|---|---|---|
| Mattermost | `mm` | Server DNS name | `mm/chat.snt/4o7...channel-or-user-id` |
| Discord | `discord` | Connector/bot instance | `discord/community/C0123456789` |
| Slack | `slack` | Workspace ID | `slack/T01234567/C01234567` |
| Matrix / Element | `matrix` | Homeserver DNS name | `matrix/matrix.example/!room:example` |
| IRC | `irc` | Network DNS name | `irc/irc.quakenet.org/nepthreal` or `irc/irc.quakenet.org/%23agents` |
| Telegram | `telegram` | Bot/connector instance | `telegram/community-bot/-1001234567890` |
| WhatsApp Business | `wab` | Phone-number ID | `wab/PHONE_NUMBER_ID/15551234567` |
| Personal WhatsApp | `wa` | Companion instance | `wa/local/family@g.us` |
| Facebook Messenger | `facebook` | Facebook Page ID | `facebook/PAGE_ID/PSID` |
| Viber | `viber` | Bot/connector instance | `viber/support-bot/USER_ID` |
| LINE | `line` | Official-account/connector instance | `line/support/GROUP_OR_USER_ID` |
| Discourse | `discourse` | Forum DNS name | `discourse/forum.example/12345` |

Quote addresses containing shell metacharacters. For IRC, percent-encode `#`
as `%23`; the relay treats the decoded/native destination as the adapter's
channel ID. An instance distinguishes accounts, workspaces, homeservers,
networks, pages, bots, or forum servers when several connectors of one platform
are configured. The server retains the complete canonical address in its
subscription set.

The final source/destination component varies by platform. Supply the opaque ID
returned by that platform:

| Platform | User | Group | Channel / room / topic | Thread or reply | Direct message |
|---|---|---|---|---|---|
| Mattermost | `mm/chat.snt/USER_ID` | `mm/chat.snt/GROUP_DM_CHANNEL_ID` | `mm/chat.snt/CHANNEL_ID` | Channel address plus `--thread-id ROOT_POST_ID` | A user address is resolved to the bot/user DM channel. |
| Discord | User IDs are author metadata; creating a DM from one is not implemented | `discord/community/GROUP_DM_CHANNEL_ID` when accessible | `discord/community/CHANNEL_ID` | `discord/community/THREAD_CHANNEL_ID`; a Discord thread is a channel | `discord/community/DM_CHANNEL_ID` |
| Slack | User ID is author metadata, not a send destination | `slack/T01234567/GROUP_DM_ID` (`G...`) | `slack/T01234567/CHANNEL_ID` (`C...`) | Conversation address plus `--thread-id PARENT_MESSAGE_TS` | `slack/T01234567/DM_ID` (`D...`) |
| Matrix / Element | `@alice:example` is author/invite metadata | Group conversations are rooms | `matrix/matrix.example/!ROOM_ID:example` | Room address plus root event ID in `--thread-id` | DMs are rooms; use their `!ROOM_ID:example` |
| IRC | `irc/irc.quakenet.org/nepthreal` | No distinct persistent group-DM object | `irc/irc.quakenet.org/%23agents` | Unsupported | A nickname address such as `irc/irc.quakenet.org/nepthreal` |
| Telegram | `telegram/community-bot/USER_CHAT_ID` | `telegram/community-bot/-GROUP_CHAT_ID` | `telegram/community-bot/-100CHANNEL_ID` | Chat address plus `--thread-id MESSAGE_THREAD_ID` for forum topics | Private chat ID, normally the user's numeric ID |
| WhatsApp Business | `wab/PHONE_NUMBER_ID/15551234567` | `wab/PHONE_NUMBER_ID/GROUP_ID` for approved Groups API accounts | No public channel concept | Unsupported by this adapter | Customer E.164 digits without `+` |
| Personal WhatsApp | `wa/local/15551234567@c.us` | `wa/local/FAMILY_GROUP_ID@g.us` | No public channel concept | Reply metadata is preserved where supplied | A `PHONE@c.us` chat ID |
| Facebook Messenger | `facebook/PAGE_ID/PSID` | Ordinary Facebook group chat is unavailable to this Page adapter | Page conversation identified by PSID | No separate thread address | Page-scoped user ID (`PSID`) |
| Viber | `viber/support-bot/USER_ID` | Group/community addressing is not implemented | Bot conversation identified by Viber user ID | Unsupported | Viber bot subscriber user ID |
| LINE | `line/support/USER_ID` | `line/support/GROUP_ID` | `line/support/ROOM_ID` for a multi-person room | No separate thread address | LINE user ID |
| Discourse | Username is author/API metadata | Categories filter topics but are not chat groups | `discourse/forum.example/TOPIC_ID` | Topic address plus `--thread-id POST_NUMBER` | Private-message topics are not implemented |

Examples:

```powershell
mailbox-client poll --as symbolic-workbench-codex `
  --from mm/chat.snt/CHANNEL_ID

mailbox-client send --as symbolic-workbench-codex `
  --to mm/chat.snt/USER_ID "Private status update"

mailbox-client send --as symbolic-workbench-codex `
  --to slack/T01234567/C01234567 `
  --thread-id 1712345678.123456 "Thread reply"

mailbox-client poll --as symbolic-workbench-codex `
  --from irc/irc.quakenet.org/%23agents
```

`--thread-id` and `--root-id` are envelope metadata, not extra path segments.
This keeps an address stable while preserving the platform's native reply or
thread identifier. Discord is the exception: its threads are channel objects,
so their channel ID is the final address component. [Discord's channel model](https://docs.discord.com/developers/resources/channel)
defines guild channels, DMs, group DMs, and thread channels; Slack replies use
the conversation plus [`thread_ts`](https://api.slack.com/methods/chat.postMessage).

The CLI works through the REST server or directly against a JSONL mailbox:

```powershell
mailbox-client --url http://127.0.0.1:46667 status
mailbox-client --url http://127.0.0.1:46667 send --as symbolic-workbench-codex --to omegaclaw-core-codex "Hello"
mailbox-client --dir C:\relay\mailbox receive --to symbolic-workbench-codex
```

Transport precedence is explicit `--dir`, `--url`, or `--mailbox`; then
`AGENT_MAILBOX_DIR`; then `AGENT_MAILBOX_URL`; then the local `mailbox/`
default. `--dir` and `--url` are mutually exclusive. Named mailboxes come from
`config/mailboxes.json` or `--config PATH`:

```powershell
mailbox-client --mailbox local status
mailbox-client --config C:\relay\groups.json --mailbox research follow --to symbolic-workbench-codex
```

The client supports:

- `send`, `receive`, `peek`, `poll`, `follow`, `unread-count`, `ack`, `status`,
  and `check`;
- `--cursor`, `--no-advance`, `--since`, `--limit`, `--where FIELD=VALUE`, and
  bounded `--wait`;
- `--timeout`, `--retry`, `--retry-delay`, and repeatable `--require-port`;
- `--token` or `AGENT_MAILBOX_TOKEN` for REST authentication;
- `--curl` anywhere before `--` to print a token-redacted equivalent REST call
  without sending it;
- `--input PATH` for UTF-8 message text and repeatable `--attach PATH`;
- `--to` anywhere before `--`, or a positional recipient;
- `--format jsonl|json|text`, `--output`, `--quiet`, `--verbose`, and
  `--nobuffer`;
- `--` to stop option processing so message text may contain switch-like text;
- `--run command.json` to execute a complete JSON command document.

Example command document:

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

Run it with `mailbox-client --run command.json`. For exact argument control use
`{"args":["--dir","mailbox","status"]}`. Keep Bearer tokens in
`AGENT_MAILBOX_TOKEN`, not command documents. Assign a stable cursor to each
independent consumer; `peek` and `--no-advance` do not persist progress, while
`ack` advances through a specific message ID.

Download the matching standalone client from a running relay:

```powershell
Invoke-WebRequest http://127.0.0.1:46667/agent_mailbox.py -OutFile agent_mailbox.py
```

### Trusted Speaker

`mailbox-console UNIQUE_IDENTITY --to DESTINATION` opens the interactive
WebSocket console. The identity is mandatory and must be unique per concurrent
consumer. An HTTP(S) relay URL is converted to WebSocket automatically:

```powershell
mailbox-console speaker-one --url http://127.0.0.1:46667 --to agent-beta
```

It can also use a local mailbox without a server:

```powershell
mailbox-console speaker-one --dir .\mailbox --to agent-beta
```

Interactive commands are
`/as AGENT_ID`, `/from PRESENCE_OR_ENDPOINT`, `/to DESTINATION`,
`/ping`, `/help`, and `/quit`.

## `mailbox-client` CLI versus direct REST

With `--url`, `mailbox-client` provides the common mailbox workflow over REST.
Direct REST additionally exposes administrative, integration, and live-chat
surfaces that the CLI does not currently wrap.

| Capability | `mailbox-client` | Direct REST | Notes and limitations |
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
| Connector inspection | No | Yes | Use `GET /v1/connectors`. |
| Identifier/UUID directory | No | Yes | Use `GET/POST /v1/identifiers`. |
| Identifier-resolution requests | No | Yes | Use `GET/POST /v1/identifier-resolution-requests`. |

The equivalent client and console command family is `mailbox-client registry`
or `/registry`. It supports `remember`, `alias`, `find`, `request`, and
`requests` while
preserving the source system for every UUID or opaque identifier.

Assign a collision-safe manual user nickname with:

```powershell
mailbox-client registry alias mm/chat.singularitynet.io USER_ID patrick.hammer --kind user
```

The same alias may be re-applied to the same ID. The command rejects assigning
it to a different ID in that instance, preventing friendly-name ambiguity.

`mailbox-client list` without `--on` loops through every enabled configured
provider and returns provider-grouped results. `mailbox-client list --on
TYPE/INSTANCE` restricts the operation to one provider. Discovery results are
walked recursively for trustworthy ID/name pairs and the command reports how
many new registry aliases were learned. Relationship IDs without their own
label are kept in metadata but are not incorrectly paired with the containing
object's display name.

For IRC, `mailbox-client discover users --platform irc --channel irc/0/testing`
uses `NAMES` replies to list visible channel members and stores their nicknames,
channel context, and status prefixes in the registry. The same operation is
available as `/discover users ...` inside `mailbox-console`.
| WebSocket chat | No | Yes | Connect to `/v1/chat/ws`. |
| Platform webhook ingestion | No | Yes | Platform adapters expose their webhook routes on the server. |
| Attachment download | No dedicated command | Yes | Public attachments are served through the attachment endpoint. |
| Attachment upload from a remote client | Limited | Limited | CLI currently sends local paths; this only works when the server can access the same filesystem. A binary or multipart upload endpoint is still needed. |
| Read message text from a local file | Yes | Client must implement | `--input PATH` reads UTF-8 text on the CLI machine before sending. |
| Monitor a required TCP port | Yes | No | `--require-port` checks the CLI machine, not the remote relay host. |
| Operate without a running server | Yes, with `--dir` | No | Direct JSONL mode lacks live adapter, connector, route, and connection-state APIs. |

Prefer REST when several processes or machines share one relay: the server
centralizes storage and cursor changes. Direct JSONL mode is useful for a local,
serverless mailbox, but concurrent programs must not make unsafe independent
writes to its files.

## REST mailbox — implemented

REST clients address mailbox identities directly and do not need a polled chat
connector. Send to `POST /v1/messages`:

```json
{
  "from": "workflow-runner",
  "to": "console-default-agent",
  "type": "message",
  "text": "The workflow completed",
  "correlation_id": "run-123"
}
```

Receive through `GET /v1/messages?recipient=console-default-agent`. Receiving
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

### Mailbox console client

```powershell
mailbox-console special-console-client --to console-default-agent
```

From an uninstalled Windows checkout, use
`.\mailbox-console.cmd special-console-client --to console-default-agent`.
`special-console-client` must be a unique stable mailbox identity so concurrent
clients do not consume the same cursor.
Commands are `/as AGENT_ID`, `/from PRESENCE_OR_ENDPOINT`, `/to DESTINATION`,
`/join TYPE/INSTANCE/CHANNEL`, `/console TYPE/INSTANCE/CHANNEL`,
`/leave [TYPE/INSTANCE/CHANNEL]`,
`/url ADDRESS`, `/ws ADDRESS`, `/wss ADDRESS`, `/dir PATH`, `/ping`, `/help`,
and `/quit`. The transport commands reconnect the live console without exiting;
without an argument they display the current transport. Any other slash command invokes the corresponding
`mailbox-client` command, including `/send`, `/poll`, `/subscribe`, `/status`,
`/token` and `/contacts`. Each state command without a value displays its
current setting. `/as` selects the stable sender agent, `/from` selects that
agent's concrete presence or external endpoint, and `/to` selects the receiver.
Changing `/as` does not move the console's receiving connection from the agent
identity used when it connected.

### Browser demonstration

With the daemon running, open `http://127.0.0.1:46667/page-demo`. The page is served
by the proxy and connects to the same-origin mailbox WebSocket endpoint. It is
a demonstration client; it does not bypass or replace the mailbox.

## JSONL mailbox — implemented

Filesystem clients use the same envelope without a connector entry:

```powershell
.\mailbox-client.cmd send --to console-default-agent "Please inspect this run" `
  --sender workflow-runner
.\mailbox-client.cmd receive --to workflow-runner
```

Supply either a mailbox directory (`AGENT_MAILBOX_DIR`) or the REST daemon
(`AGENT_MAILBOX_URL` or `--url`). Do not have multiple processes consume the
same recipient identity.

## OpenAI Codex installation and mailbox integration — implemented

The checked Codex integration means Codex can use the compatibility mailbox
through the CLI or REST interface. Codex itself is installed separately using
the [official OpenAI Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli).

For recurring mailbox polling, customize
[`AUTOMATION_PROMPT.md`](../src/mailbox_channels/resources/AUTOMATION_PROMPT.md)
and create the recurring task in Codex Desktop. Copying the file does not create
the automation. For workspace bootstrap, use
[`INSTALL_WITH_CODEX.md`](../src/mailbox_channels/resources/INSTALL_WITH_CODEX.md).
The running relay also serves both documents at `/AUTOMATION_PROMPT.md` and
`/INSTALL_WITH_CODEX.md`.

### Windows

After installing and signing in to Codex, install this proxy in PowerShell:

```powershell
cd C:\path\to\mailbox_channels
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\.env.example config\.env
.\mailbox-server.cmd
```

Give every concurrent Codex task a unique mailbox identity:

```powershell
$env:AGENT_MAILBOX_URL='http://127.0.0.1:46667'
& C:\path\to\mailbox_channels\mailbox-client.cmd `
  poll --to my-codex-project --interval 30 --checks 10 --require-port 46667
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

git clone YOUR_PROXY_REPOSITORY_URL ~/code/mailbox_channels
cd ~/code/mailbox_channels
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

When the daemon also runs inside WSL:

```bash
cp config/.env.example config/.env
./mailbox-server
```

Leave that terminal running. In a second WSL terminal:

```bash
cd ~/code/mailbox_channels
export AGENT_MAILBOX_URL=http://127.0.0.1:46667
./mailbox-client status
```

When the daemon runs on Windows, start it from PowerShell with
`mailbox-server.cmd`. First try `curl http://127.0.0.1:46667/v1/status`
from WSL2 because current WSL networking may forward Windows localhost. If it
does not, restart the Windows daemon with `--host 0.0.0.0`, find the Windows
host address from WSL with `ip route show default`, and use
`http://<windows-host-address>:46667`. Restrict the Windows firewall rule to
the local/WSL network; binding to `0.0.0.0` otherwise exposes the relay on
every Windows interface. Prefer REST across the Windows/WSL boundary instead
of writing relay JSONL files through `/mnt/c`.

## Compatible agent runtimes

### Example identity map

The examples use three stable identities from the working deployment. Identity
names identify independent mailbox consumers, not merely software products:

| Identity | Kind | Meaning |
|---|---|---|
| `symbolic-workbench-codex` | Codex consumer | Codex task operating in the Symbolic Learner Workbench workspace. |
| `omegaclaw-core-codex` | Codex consumer | Separate Codex task operating in OmegaClaw-Core and coordinating routed work. |
| `omegaclaw-min` | OmegaClaw consumer | OmegaClaw runtime/bridge consumer; this is not a third Codex task. |

For example, the Workbench Codex can send to the OmegaClaw-Core Codex:

```powershell
mailbox-client send --as symbolic-workbench-codex `
  --to omegaclaw-core-codex "Workbench validation completed"
```

Each consumer polls mail addressed to itself:

```powershell
mailbox-client poll --to symbolic-workbench-codex --interval 30 --checks 11
mailbox-client poll --to omegaclaw-core-codex --interval 30 --checks 11
mailbox-client poll --to omegaclaw-min --interval 30 --checks 11
```

After processing a known message, the receiving identity owns the cursor and
therefore appears as `--as` on acknowledgement:

```powershell
mailbox-client ack --as symbolic-workbench-codex MESSAGE_ID
```

Names such as `mattermost-bridge-agent`, `irc-bridge-agent`, and
`channel-relay` elsewhere in this document are infrastructure roles. They are
not extra human-facing agent runtimes: bridge agents mediate a configured
platform connector, while `channel-relay` is the deterministic outbound routing
address.

Applications with JSONL or HTTP clients can use stable, transport-neutral
mailbox recipient names. OmegaClaw-compatible deployments commonly use
identities such as `omegaclaw-core` and `omegaclaw-min`; MeTTaClaw-compatible
deployments may use a deployment-specific `mettaclaw-*` identity. These are
consumer conventions, not identities built into the relay.

Poll through REST with `mailbox-client --url URL ...`. Direct JSONL access is
only appropriate when the consumer and relay intentionally share the same
native filesystem and mailbox directory; do not use it across Windows/WSL
mounts or network shares.
The client translates an external `--to ADAPTER/SERVER/ID` endpoint into the
internal `outbound_delivery` envelope with `channel_type` and `channel_id`.
Low-level REST clients may construct that envelope directly. Forwarders should preserve
`thread_id`/`root_id`, `source_id`, attachments, and workflow/run correlation
fields. Python-and-HTTP clients do not require another application's repository
or a shared filesystem mount.

The relay itself does not expose start, stop, or restart endpoints. If an
external service manager controls it, that manager may define endpoints such
as:

```text
GET  http://127.0.0.1:8000/api/system/services
POST http://127.0.0.1:8000/api/system/services/channel-relay/start
POST http://127.0.0.1:8000/api/system/services/channel-relay/stop
POST http://127.0.0.1:8000/api/system/services/channel-relay/restart
```

These are illustrative external-manager routes, not mailbox-relay API routes.
Port 8000 belongs to that hypothetical manager; the relay's default REST port
is 46667 and its live status endpoint is `GET /v1/status`.

REST client example from PowerShell:

```powershell
mailbox-client --url http://127.0.0.1:46667 send `
  --as symbolic-workbench-codex --to omegaclaw-core-codex 'Please inspect this run'
```

## Channel-to-channel bridges

A bridge combines two agent-mediated connector endpoints. The endpoint agents
must preserve `source_id`, `channel_type`, `channel_id`, `thread_id`, `root_id`,
attachments, and correlation fields whenever the destination supports them.
Every adapter must suppress its own outbound echoes to prevent relay loops.

Cursor-driven relays in `config/relays.json` consume retained channels and send
to complete external destination addresses. Manage them with `relays`,
`relay-add`, and `relay-del`.

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
secrets through `/v1/connectors`, map inbound events to the stable mailbox
envelope, implement delivery failure records, and add deterministic mocked
tests for inbound, outbound, attachments, threads, and echo suppression.
