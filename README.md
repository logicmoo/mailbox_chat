Exit code: 0
Wall time: 1.1 seconds
Output:
# Mailbox Channel Relay Bridging Proxy

## Managed resource kinds

The registry has exactly four top-level managed kinds: `agent`, `connector`,
`channel`, and `relay`. Every returned resource carries one of those `kind`
values. Platform details belong in `adapter`; channel details belong in
`channel_type`; a relay's external output is an address on the relay rather
than a separate destination resource. A presence, when needed, is nested
session/account metadata and is never a fifth managed kind.

External channel resources use stable, non-address IDs. For example,
`mm-chat-singularitynet-io-chat-c83yjesfejgbmdptwtjqgqis9h` identifies a
Mattermost channel. Its `aliases` include the external address
`mm/chat.singularitynet.io/c83yjesfejgbmdptwtjqgqis9h` and its readable
channel name. Subscriptions and cursors always use the resource ID; slashed
addresses are reserved for external send/relay targets.

Python package/repository name: `mailbox_channels`.

Mailbox Channel Relay Bridging Proxy is a standalone, transport-neutral daemon for
relaying messages between mailboxes and chat channels. It can serve automated
consumers, agent runtimes, or ordinary channel-to-channel bridges without
making any of those systems part of the daemon's identity. It
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
mailbox-server.cmd
```

Add `--verbose` for adapter startup, connection, failure, and retry messages,
or `--verbose 2` to include successful adapter polls and HTTP request logging.
Level `0` reports errors only. Failed adapters continue retrying with bounded
exponential backoff while verbose output identifies the affected service.
On an interactive terminal, consecutive identical verbose messages are
collapsed into one in-place `last message repeated N times` counter. Redirected
logs emit periodic repeat summaries without terminal control characters.
The same state is available from `/v1/status` as `lastVerboseMessage`,
`lastVerboseMessageRepeatCount`, and `lastVerboseMessageAt`.
Adapter startup, connection, and failure transitions are also delivered to
each connector's bridge agent and mailbox recipients as durable
`chat_server_status` messages from `local-ADAPTER-server`. This lets agents
observe or forward service state without scraping process logs. Failure
messages include a structured `diagnostic` object with the safe exception type
and message, failed operation, enabled state, and automatic-retry flags. A
`service_context` object supplies safe connector IDs, channel IDs, directions,
connection state, and retry policy.

`mailbox-client receive`, `peek`, `poll`, and `follow` retain these objects in
their default JSONL output. With `--format text`, the client prints a compact
diagnostic summary including the service, state, connector/channel IDs, failed
operation, error type/message, and whether another attempt will occur.

The daemon is independent of FastAPI and survives development API reloads.
Runtime PID, status, and logs are under `mailbox/runtime/channel-relay-PORT/`.

## REST mailbox API on port 46667

```text
GET  /health
GET  /v1/status
GET  /v1/adapters
GET  /v1/registry
GET  /v1/registry
GET  /v1/cursors?cursor=AGENT_ID
GET  /v1/messages?recipient=IDENTITY
POST /v1/agents
POST /v1/cursors
POST /v1/messages
GET  /page-demo
GET  /cmd-client/?args=ARGUMENTS
```

`/cmd-client/` is a persistent browser console for the bundled
`mailbox-client`. It automatically targets the relay serving the page, retains
the submitted argument string, and displays stdout, stderr, and the exit code.
For example, open `/cmd-client/?args=-h` for help or enter `status` in its form.
Commands use the same Python entrypoint as `mailbox-client.cmd`, without a
shell or command whitelist. Long-running commands such as `poll` and `follow`
stream output into the page and remain active until they finish or the page's
Stop button terminates them.

Example send:

```powershell
$body = @{
  from = 'worker-1'
  to = 'outbound_delivery'
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
Invoke-RestMethod 'http://127.0.0.1:46667/v1/messages?recipient=worker-1'
```

Receiving advances that recipient's durable cursor. Each concurrent consumer
must therefore use a unique, stable identity.

List every agent mailbox that currently has one or more durable conversation
subscriptions:

```powershell
mailbox-client --url http://127.0.0.1:46667 subscriptions --all
```

That mailbox identity is the stable **agent ID**, not a particular connection.
One agent may have several simultaneous presences (commonly three or four),
such as a Codex task, console client, mailbox poller, and Mattermost bot. Define
them once in `config/relays.json`; connectors bind a platform connection to one
of those presences. All inbound traffic for the bound presence is delivered to
the owning `agent_id`, so the agent keeps one durable mailbox and cursor:

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
  }],
  "connectors": [{
    "id": "mattermost-primary",
    "adapter": "mattermost",
    "agent_id": "symbolic-workbench-codex",
    "presence_id": "symbolic-mm"
  }]
}
```

Presence IDs are globally unique. A connector cannot claim a presence belonging
to a different agent. Existing configurations without `agents` remain valid.

Create an agent, or idempotently add one of its presences, through the registry
API. A newly created agent emits `agent_registered`; a newly created presence
emits `presence_registered`. Both lifecycle records are retained on
`server_events`. Repeating the same registration does not emit a
duplicate event.

```powershell
$registration = @{
  agent_id = 'symbolic-workbench-codex'
  presence_id = 'symbolic-codex-app'
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:46667/v1/agents `
  -Method Post -ContentType application/json -Body $registration
```

The server exposes retained operational channels:

- `server_events` contains server lifecycle and adapter events.
- `agent_to_agent` contains copies of sends whose target is
  a registered agent.
- `agent_to_channel` contains a copy of every ordinary
  send.
- The agent ID itself is its writable mailbox. Messages addressed through one
  of its presences are copied to that mailbox. For example,
  `workspace-codex-agent` owns the `workspace-codex-agent` mailbox.

Addressing a presence does not rewrite the original envelope. If `codex.star`
is registered to `workspace-codex-agent`, the original record still says
`to: "codex.star"`. Its per-agent channel copy adds
`addressed_presence_id: "codex.star"` and
`resolved_agent_id: "workspace-codex-agent"`, allowing the owning agent to
recognize and consume the message without losing the precise address used by
the sender.

An original record and all of its audit copies share `dedupe_id`. Audit copies
also contain `audit_of` (the original record ID) and `audit_recipient` (the
original destination). The audit channels do not copy themselves, preventing
recursive records.

Agents can discover all retained sources, initialize each cursor exactly once
at a caller-selected position, and poll every saved source in one command:

```powershell
mailbox-client --url http://127.0.0.1:46667 channels
mailbox-client --url http://127.0.0.1:46667 agents
mailbox-client --url http://127.0.0.1:46667 agent-add review-agent `
  --presence review-agent-app
mailbox-client --url http://127.0.0.1:46667 agent-add review-agent --dry-run
mailbox-client --url http://127.0.0.1:46667 agent-del review-agent `
  --presence review-agent-codex
mailbox-client --url http://127.0.0.1:46667 agent-del review-agent `
  --purge --dry-run
mailbox-client --url http://127.0.0.1:46667 cursors --cursor symbolic-workbench-codex
mailbox-client --url http://127.0.0.1:46667 cursor-init `
  server_events agent_to_agent `
  agent_to_channel --cursor symbolic-workbench-codex --start now
mailbox-client --url http://127.0.0.1:46667 --as symbolic-workbench-codex `
  poll --subscriptions --interval 30 --checks 11
```

`channels` is a global catalog: it includes monitored external channels,
server/audit channels, and every registered agent mailbox. `agents` lists
all registered messageable agents, including their presences. Each entry also
includes its private `agent_mailbox` and a flat
`subscriptions` list. Every subscription is the saved cursor state for one channel
(`channel`, `cursor`, and byte `offset`); there is no second polling-subscription
layer. Listing agents never advances a subscription. `agent-add`
creates a stable agent and can add one presence. Repeating the same registration
is safe and reports that nothing new was created. Registration also exposes
the agent's mailbox in `channels`. `agent-del` removes one
presence when `--presence` is supplied, or the entire agent otherwise. It
retains private channel history and cursor files unless `--purge` is supplied.
Use `--purge --dry-run` to see exactly which channels, records, and cursor files
would be deleted without changing anything. It refuses removals that would
leave a connector pointing at a missing identity. `cursors`
reports which of those channels have already been initialized for one cursor and
the current byte offset of each. The listing does not hide other agents'
pollable sources.

There is no server-defined age cutoff. Initial cursor positions may be
`beginning`, `now`, an exact timestamp, or a caller-selected relative duration
such as `7d`. Retained history can be queried independently of live cursors
with `mailbox-client history CHANNEL... --since 7d`.

Subscribed channels fan events out to those identities. For
example, subscribe an agent to server diagnostics and continue polling the
agent's own mailbox:

```powershell
mailbox-client subscribe server_events --to symbolic-workbench-codex
mailbox-client poll --to symbolic-workbench-codex
```

An agent can ensure all required subscriptions when it starts:

```powershell
mailbox-client poll --to symbolic-workbench-codex `
  --subscribed server_events,mm/chat.snt/MATTERMOST_CHANNEL_ID
```

These memberships are saved by the server in `config/relays.json` under
`subscriptions[].subscribers` and survive relay restarts.

Mattermost messages are retained directly under their canonical address, such
as `mm/chat.singularitynet.io/opaque-channel-id`. There is no generated alias
or separate mapping. The immutable channel ID keeps the address stable if its
display name changes.

Every inbound Mattermost JSONL event also carries `workspace_id` (the original
Mattermost `team_id`), `team_id`, `workspace_name`, and `channel_name`. The
event is therefore self-describing even when it is read without access to the
subscription or identifier registries.

External Mattermost addresses use `mm/SERVER/ID`; `--as` is the local agent,
`--from` subscribes to an external source, and `--to` sends externally:

```powershell
mailbox-client poll --as symbolic-workbench-codex --from mm/chat.snt/CHANNEL_ID
mailbox-client send --as symbolic-workbench-codex --to mm/chat.snt/PERSON_ID "Hello"
```

Use instance `0` to select the configured/default platform instance without
knowing its server name, for example `mm/0/CHANNEL_ID`. This works for every
platform address type; an explicit instance still selects a particular server
when several are configured.

Inside `mailbox-console`, emulate a presence in any addressable conversation:

```text
/console irc/0/testing
```

`/join irc/0/testing` creates a persistent subscription. `/console` creates a
temporary subscription and automatically unsubscribes that temporary channel
when the console switches elsewhere. Both make the selected conversation the
default source and destination. `/leave` explicitly removes either kind.

List channels visible to the configured IRC account with the IRC `LIST`
protocol and save their names, topics, and visible-user counts in the registry:

```powershell
mailbox-client discover channels --platform irc
mailbox-client discover users --platform irc --channel irc/0/testing
```

From a console connected to the server, use `/discover channels --platform
irc` or `/discover users --platform irc --channel irc/0/testing`. User discovery
uses IRC `NAMES`, retains status prefixes such as operator (`@`) and voice (`+`),
and saves each nickname in the registry with its channel context. Some IRC
networks restrict or throttle full channel lists; use
`--timeout SECONDS` when the network needs longer.

IRC exposes a read-only server/status conversation at `irc/0/status`. Subscribe
or open `/console irc/0/status` to receive server notices, errors, and numeric
status replies without confusing that window with a real `#status` channel.

Create a qualified local channel, or a channel on a configured platform whose
credentials have creation permission:

```powershell
mailbox-client channels create debug-console --title "Debug console"
mailbox-client channels create irc/0/testing
mailbox-client channels create slack/0/agent-testing --topic "Agent testing"
mailbox-client channels create discord/0/agent-testing --container GUILD_ID
mailbox-client channels create mm/0/agent-testing --container TEAM_ID
mailbox-client channels create matrix/0/agent-testing
```

The same family works as `/channels create ...` in the console. Telegram and
WhatsApp Business bot APIs do not expose arbitrary group creation, so those
adapters report the operation as unsupported.

Standard IRC operations are top-level `mailbox-client` commands and are all
shown by `mailbox-client --help`: `ping`, `list`, `names`, `join`, `part`,
`topic`, `nick`, `whois`, `mode`, `invite`, `kick`, `message`, `notice`, and
`raw`. Stateful commands run through the relay's active IRC connection. For
example, use `mailbox-client join #testing` or `mailbox-client whois alice`.
For Mattermost, `join mm/INSTANCE/CHANNEL --subscribe-all` both joins the bot
account and subscribes every registered mailbox agent to the resulting stable
channel resource ID.
For an already-known retained channel, `channel-add ADDRESS --subscribe-all`
adds the external ID to the selected connector's monitored channels, subscribes
every agent, and initializes their cursors at the current mailbox end. A later
`poll --subscriptions --as AGENT` dynamically includes those subscriptions and
returns as soon as an ingested channel message arrives.

Mailbox JSONL maintenance is exposed through authenticated server endpoints.
Pause first with `POST /v1/maintenance/pause`, then use
`POST /v1/maintenance/trim-before` with `before` or `older_than_seconds`, or
`POST /v1/maintenance/trim-to-size` with `max_bytes`. Omit `channels` to trim
the full mailbox, or pass a channel-ID array to trim only those channels.
Every trim creates a timestamped backup and remaps durable cursor offsets.
Finish with `POST /v1/maintenance/resume`.

With no `--on` selector, `mailbox-client list` visits every enabled configured
provider and groups the channel results by provider. An unavailable provider is
reported in its own result instead of hiding successful results from the other
providers. Use `--on TYPE/INSTANCE` to restrict listing to one provider:

```powershell
mailbox-client list
mailbox-client list --on mm/chat.singularitynet.io
```

The same top-level commands operate on Mattermost when an `mm/INSTANCE/ID`
address is supplied. For commands without a channel argument, select the
platform instance with `--on mm/INSTANCE`. The relay maps the operations to
channel listing, membership, headers, bot nickname, user lookup, visibility,
posts, notices, and constrained authenticated `/api/v4/` requests. For example:

```powershell
mailbox-client names mm/0/CHANNEL_ID
mailbox-client names test
mailbox-client names --on mm `
  https://chat.singularitynet.io/chat/channels/image-perception-to-recognizable-memory-and-arc3
mailbox-client whois j4pok4rbqtfytcrcn8d3nhgkto
mailbox-client teams --on mm/0
mailbox-client list --on mm/0 --team engineering
mailbox-client threads engineering --on mm/0
mailbox-client ping --on mm/0
mailbox-client invite USER_ID mm/0/CHANNEL_ID
mailbox-client message mm/0/CHANNEL_ID --input message.txt --format text
mailbox-client raw --on mm/0 POST /api/v4/posts --input post.json --input-format json
```

`--input FILE` chooses the content source, `--input-format text|json` controls
how the file is interpreted, and `--format jsonl|json|text` controls output.
These switches may appear before or after the IRC or Mattermost subcommand.
Mattermost discovery saves teams, channels, direct/group-message channels,
threads, and users in the durable identifier registry. After discovery, a
unique readable alias or bare opaque ID also identifies its platform, so
`mailbox-client names test` and `mailbox-client whois OPAQUE_USER_ID` do not
need `--on`. Qualified addresses remain available, including
`/console mm/0/Town-Square`; subscriptions are saved using the resolved ID.
Downloaded Mattermost users resolve by username, email address, nonblank
nickname, or combined first and last name; for example, both `whois zarathustra`
and `whois zarathustra@singularitynet.io` resolve the same user.
Mattermost browser URLs containing `/channels/CHANNEL_SLUG` are accepted as
channel arguments when Mattermost is selected or inferred. The URL hostname
must match the configured Mattermost server.
Every Mattermost response is also scanned recursively: whenever an object has
an `id` together with `display_name`, `name`, or `username`, all readable
aliases are refreshed under the concrete instance, such as
`mm/chat.singularitynet.io`. `mm/0` remains a routing shortcut but is not stored
as a second registry copy. Relationship fields such as `team_id` and
`creator_id` are retained as metadata; they receive names only from separately
downloaded team or user objects, never from the containing channel's name.
At the end of each Mattermost-backed command, the client reports the number of
new durable aliases found on stderr without corrupting JSON or JSONL stdout.
`mode mm/0/CHANNEL public|private` maps visibility, while `mode mm/0/CHANNEL
+o USER` or `mode mm/0/CHANNEL -o USER` grants or revokes Mattermost
channel-admin membership. `notice mm/0/CHANNEL`
creates a tagged channel post, or an ephemeral user-only post with `--user`.
IRC voice and moderated-channel flags have no safe one-to-one Mattermost
equivalent and are not silently approximated.

Every `TYPE/INSTANCE/ID` resolves to a structured endpoint with common
`platform`, `instance`, `id`, `type`, and `properties` fields. Resource types
include `user`, `channel`, `group`, `thread`, `dm`, and `status`; discovery or
registry metadata supplies platform-specific properties such as topic,
visibility, membership counts, and status prefixes.

All adapters use the same `TYPE/INSTANCE/SOURCE_OR_DESTINATION` form. Canonical
types are `wa`, `wab`, `viber`, `mm`, `discord`, `discourse`, `irc`, `slack`,
`matrix`, `telegram`, `facebook`, and `line`. `wa` means personal WhatsApp;
`wab` means WhatsApp Business. The full address table is in
`docs/INSTRUCTIONS.md`.

## Client commands

Run a UTF-8 command file sequentially with `mailbox-client --batch FILE`.
Each nonblank line contains one complete command after the `mailbox-client`
executable name (the name itself is also accepted). Quoting follows the local
platform command-line rules. Execution stops at the first failed line and
reports its file name and line number.

The interactive console accepts `--on TYPE/INSTANCE[/ID]` at startup and `/on
TYPE/INSTANCE[/ID]` while running. A two-part value selects the platform for
addressless commands such as `/list`. A three-part value also changes the
current sending/source conversation; a qualified address on an individual
command still takes precedence:

```powershell
mailbox-console --as operator --on mm/chat.singularitynet.io
/list
/on mm/chat.singularitynet.io/Town-Hypercube
/on irc/irc.libera.chat
/names irc/irc.libera.chat/%23agents
```

Client installation, repository-local launchers, `mailbox-client`, named
mailboxes, command documents, cursor behavior, Trusted Speaker, REST, JSONL,
WebSocket, and platform setup are consolidated in
[`docs/INSTRUCTIONS.md`](docs/INSTRUCTIONS.md).

## Public attachment links

Set the externally reachable relay origin when IRC or another text-only
adapter must publish mailbox attachments as links:

```powershell
mailbox-server.cmd --host 0.0.0.0 --port 46667 `
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
mailbox-server --max-attachment-mb 1024 --max-attachment-storage-mb 25600
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
mailbox-client token register
```

The command generates a strong token, stores it atomically as
`MAILBOX_RELAY_TOKEN`, and displays it once so it can be copied to authorized
clients. Check registration without revealing the token:

```powershell
mailbox-client token status
```

From an uninstalled checkout on Windows, use the repository launcher:

```powershell
.\mailbox-client token.cmd register
```

Restart the relay after registering or rotating the token. On each authorized
client, set the displayed value without committing it:

```powershell
$env:AGENT_MAILBOX_TOKEN='<the displayed token>'
mailbox-client --url https://relay.example.com send `
  --as symbolic-workbench-codex --to omegaclaw-core-codex 'Finished the task'
```

For automated deployment, an existing secret of at least 32 characters can be
registered with `mailbox-client token register --token VALUE`, though passing a
secret on the command line may expose it in shell history. Supplying
`MAILBOX_RELAY_TOKEN` through a service secret manager is preferable.

Then expose the authenticated relay behind TLS:

```powershell
mailbox-server --host 0.0.0.0 --port 46667 `
  --public-url https://relay.example.com
```

The server reads `MAILBOX_RELAY_TOKEN`; clients use `AGENT_MAILBOX_TOKEN`.
When no server token is configured, REST mailbox routes
remain unauthenticated for backward-compatible local operation. Put TLS in
front of any Internet-facing deployment so the Bearer token is encrypted in
transit.

## Configuration and security

The daemon binds only `127.0.0.1`. Put the non-secret Mattermost connection
and channel configuration in `config/relays.json`:

```json
{
  "id": "mattermost-primary",
  "adapter": "mattermost",
  "instance": "chat.singularitynet.io",
  "base_url": "https://chat.singularitynet.io",
  "token_env": "MM_BOT_TOKEN",
  "channel_ids": ["CHANNEL_ID", "ANOTHER_CHANNEL_ID"]
}
```

Only the secret belongs in ignored `config/.env` or the process environment:

```dotenv
MM_BOT_TOKEN=...
MATTERMOST_RELAY_RECIPIENTS=
MATTERMOST_RELAY_ENABLED=1
```

The former `MM_URL`, `MM_CHANNEL_ID`, and `MM_CHANNEL_IDS` variables remain a
legacy fallback when the JSON connector omits those fields.

Never store tokens in workspace resources, workflow files, mailbox records, or
this README. Remote-machine access should be provided through an authenticated
proxy or tunnel; do not change the daemon to bind publicly without adding
authentication and authorization.

## Adapter status

`GET /v1/adapters` distinguishes installed adapters from planned adapters.
Currently `mattermost`, `irc`, `discord`, `matrix` (including Element clients),
`slack`, `telegram`, `whatsapp`, `facebook_messenger`, `viber`, and `line` are implemented.
Discourse topics and replies are operational through signed webhooks and its REST API.

## Connector registry

[`config/relays.json`](config/relays.json) is the non-secret source of truth for what
the proxy monitors and where inbound messages are mailboxed. Each connector has
an adapter, direction (`inbound`, `outbound`, or `bidirectional`), channel IDs,
and mailbox recipients. A channel ID beginning with `$` expands an environment
variable, so deployment-specific identifiers can stay in `.env`.

Credentials never belong in `relays.json`. Adding Discord, IRC, or another
adapter means adding its implementation and then declaring its connectors in
the same registry.

## Import contacts and relay a whole channel

Import WhatsApp contacts from JSON, CSV, or vCard into the durable identifier
directory. Phone numbers are normalized to digits for WhatsApp addressing:

```bash
mailbox-client contacts --url http://127.0.0.1:46667 import contacts.vcf --system whatsapp
mailbox-client contacts --url http://127.0.0.1:46667 list --system whatsapp
```

To deliver retained IRC traffic to a WhatsApp conversation, add a cursor-driven
relay using complete channel and destination addresses:

```powershell
mailbox-client relay-add irc/irc.example/#agents `
  whatsapp/example/15551234567 --id irc-to-whatsapp
mailbox-client relays
mailbox-client relay-del irc-to-whatsapp
```

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
restarts or migration to another server. Manage them without direct SQLite
access:

```powershell
mailbox-client registry remember discord UUID "Operations room" --kind channel
mailbox-client registry alias mm/chat.singularitynet.io USER_ID patrick.hammer --kind user
mailbox-client registry find --system discord --identifier UUID
mailbox-client registry request discord UUID --resolver get-channel
mailbox-client registry requests --system discord
```

Look up a downloaded identifier without knowing its system:

```powershell
mailbox-client registry find --identifier j4pok4rbqtfytcrcn8d3nhgkto
```

Registry records are stored once under their canonical `TYPE/INSTANCE` source.
Default-instance forms such as `mm/0` are resolved from configuration rather
than duplicated in SQLite. Existing identical `mm/0` duplicates are removed
automatically; legacy-only records are preserved.
Use `registry alias` for a manually chosen friendly name. An alias is
idempotent for its existing ID and rejected if the same case-insensitive name
already belongs to another ID in that platform instance. `registry remember`
remains available for importing ordinary platform-supplied mappings, where
duplicate human display names may be legitimate.

Resolution requests are keyed by source system, identifier, and resolver. An
already-pending request is not issued again unless `--force` is supplied. The
same commands work interactively as `/registry ...` in `mailbox-console`.

See [`INSTRUCTIONS.md`](docs/INSTRUCTIONS.md) for the implementation checklist,
complete operational contract; platform-side application, bot, token,
permission, channel/room ID, and installation steps; and Mattermost, Discord,
Slack, IRC, Matrix/Element, Discourse, WhatsApp, Viber, Telegram, LINE, REST,
JSONL, and generic future-adapter examples.

## Message envelope

Required fields are `from`, `to`, `type`, `text`, `id`, and UTC `timestamp`.
Optional routing fields include `channel_type`, `channel_id`, `source_id`,
`thread_id`, `root_id`, `attachments`, `workflow_id`, `workflow_run_id`,
`operation_id`, and `correlation_id`. Unknown fields are preserved so workflow
and agent protocols can evolve independently of channel adapters.
