# Design: multi-network identity, agents, routing, lifecycle

Status: DESIGN (no code). History is disposable (look-ahead rewrites) — we design the
target model and regenerate the store to match; message logs need not be preserved.

## Problem
Moving from "one bot per adapter" to "many bots across many networks" broke every
id/assumption that presumed singularity: `-primary`, one `self.irc`, one bridge agent.
`-primary` is the visible symptom; the real work is a composite identity model.

## Core entities (proposed definitions)
- Principal (person/actor) = a real identity, stable across all networks, keyed by something
  like an email (e.g. logicmoo@gmail.com). ONE principal, MANY presences.
- Presence = a per-server USER identity (that principal's account on one network):
  Nephrael@QuakeNet, @douglas.miles@Mattermost(+special id), doug.miles@Element.
  Presences exist for BOTH our operated bots AND external humans we observe.
- Binding = presence -> principal link, via email match or manual bind. This is the identity
  graph; it lives in the identifier directory (server_registry_identifiers).
- Connector = OUR credentials/config to OPERATE a presence we control (a login we can drive).
  External humans have presences but no connector. A presence we operate has a connector.
- Presence ownership is PUPPETING, many-to-many (NOT 1:1 ownership):
  * A presence can have NO agent at all (e.g. min.botnick: a login on SNET Mattermost with no
    agent behind it).
  * MANY agents can puppet ("pretend to be") the SAME presence (a handful of local agents all
    speak as min.botnick). Matches relay field "puppeted_by".
  * An agent can puppet many presences; a presence can be puppeted by many agents.
- Agent = a logical actor we run. It may puppet 0..N presences (internal-only agents puppet
  none). Presences are first-class and exist independent of agents.
- Mailbox = a cursor-consumed local stream (agent inboxes, event log, relay outbox, queue).
- Channel = an external place a presence is on and we are handling.
- Relay = wiring a mailbox <-> external mailbox via a specific presence (puppeted by agents).

### Identity graph (the new layer)
principal (logicmoo@gmail.com)
  ├─ presence irc/quakenet.org/Nephrael
  ├─ presence mattermost/chat.singularitynet.io/douglas.miles (+platform id)
  └─ presence element/<hs>/doug.miles
Our bots are the same shape, e.g. principal "jllykifsh-bot" -> presence irc/quakenet.org/jllykifsh.
Cross-network resolution = "given a presence, find the principal, then its other presences."
Binding key precedence: explicit manual bind > verified email > (later) heuristics.

### Cross-network delivery (the point of the principal layer)
A message to ANY presence routes to that presence's PRINCIPAL, then is delivered out on the
principal's ACTIVE/HOME presence. Example: someone messages Nephrael@QuakeNet -> resolves to
principal logicmoo@gmail.com -> surfaces on Mattermost as @douglas.miles (his active presence).
Consequences:
- Mailbox is keyed by PRINCIPAL (resolves fork G). Presences are inbound sources and outbound
  delivery routes, not separate inboxes.
- Each principal has an active/home presence (where they currently read). Configurable, and
  possibly auto-updated to wherever they were last active.
- Each stored message records BOTH the origin presence (who/where it came from) and the
  delivery presence (where we surfaced it), so replies can route back correctly.
- Reply path is the mirror: principal replies from Mattermost -> we send as the right presence
  on the origin network (as Nephrael on QuakeNet) via that presence's connector.

## Adapter capability matrix (researched, cited 2026)
Ghosting/virtual-presence sorts into three FIDELITY tiers — the key design axis:
- FULL: message is a real, indistinguishable user (no bot badge). Matrix appservice ghosts;
  Discourse admin post-as-user.
- DISPLAY: per-message name+avatar override, but still bot-attributed/badged. Discord webhooks,
  Slack (webhook / chat:write.customize), Mattermost webhooks (always BOT badge), Viber
  sender.name/avatar, LINE sender.name/iconUrl, Messenger Personas.
- NONE: one fixed identity per account/connection. IRC (one real nick per TCP conn), Telegram,
  WhatsApp Cloud (business number), WhatsApp personal.

| adapter | auth | ghost tier | ghost mechanism / permission gate | multi | threads | attach | DM cold-init | discovery |
|---|---|---|---|---|---|---|---|---|
| matrix | access token / **appservice** registration | FULL | `?user_id=` masquerade within AS `users` namespace; **homeserver-admin** installs AS registration | ✅ unlimited ghosts | ✅ m.thread | ✅ | ✅ (invite/DM room) | rooms/members |
| discord | bot token (+ intents) / webhook token | DISPLAY | webhook per-msg username+avatar; needs **Manage Webhooks**; webhooks CANNOT DM | ✅ | ✅ | ✅ | ❌ (bot needs shared guild) | guilds/mailboxes/members |
| discourse | Api-Key + Api-Username | FULL | admin key + `Api-Username: <user>` posts AS that user (no badge); **admin key**; user must exist (can pre-create) | ✅ (one key, N users) | forum topics/posts | ✅ (uploads) | n/a (forum) | categories/users |
| slack | bot token xoxb (+ app token/Socket Mode) | DISPLAY | webhook or `chat:write.customize` name+icon (bot-attributed) | ✅ | ✅ thread_ts | ✅ | limited | mailboxes/users |
| mattermost | bot PAT / incoming webhook | DISPLAY | webhook `override_username`/`icon` — gated by **EnablePostUsernameOverride/IconOverride** (admin, default OFF); always **BOT badge**; REST bot posts have NO per-post override | ✅ | ✅ root_id | ✅ | admin-granted | mailboxes/users |
| irc | services: **Q** (QuakeNet) / **SASL** (Libera) | NONE | none (one real nick per TCP conn); "<name> text" prefixing only | ✅ = N conns (QuakeNet 5/IP) | ❌ | ❌ (URLs/DCC) | ✅ PRIVMSG | LIST/NAMES/WHO |
| telegram | bot token (BotFather) | NONE | none (`author_signature` cosmetic only) | ✅ N tokens | ✅ message_thread_id | ✅ 50MB up | ❌ user must /start | ❌ |
| viber | X-Viber-Auth-Token | DISPLAY | per-msg `sender.name`+`sender.avatar` (no extra perm); **new bots commercial-only since 2024** | ✅ | ❌ | ✅ URL media | ❌ user subscribes first | ❌ no subscriber list |
| line | mailbox access token (+ secret) | DISPLAY | per-msg `sender.name`(≤20) + `sender.iconUrl` | ✅ N mailboxes | ❌ | ✅ (2MB body; URL media) | ❌ user must friend | ⚠️ member list = verified/premium |
| whatsapp (cloud) | Meta app + phone_number_id + system token | NONE | none — fixed business number; **24h window + templates** | ✅ per phone number (2→20) | ❌ | ✅ 100MB | ❌ (opt-in/template) | ❌ |
| whatsapp_personal | QR web-companion pairing (**UNOFFICIAL, ToS risk**) | NONE | none (one real account) | 1/account | ⚠️ | ✅ | within contacts | partial (chats/contacts) |
| facebook_messenger | Page token (app review) | DISPLAY | **Personas API** (custom persona name+avatar); **24h window** | ✅ personas per Page | ❌ | ✅ | ❌ (opt-in/24h) | ❌ |

Key per-network IRC facts: QuakeNet = Q auth (`/msg Q@CServe.quakenet.org AUTH`), `+x` cloak, **no SASL, NO msgid** (dedupe must be heuristic), `#` only, 5 conns/IP. Libera = Solanum, **SASL PLAIN/EXTERNAL(CertFP)**, **has msgid + server-time + echo-message** (clean dedupe), `##` secondary namespace, bots should be identified+voiced (ozone/`$~a`).

### Proposed expanded catalog schema (code-level ADAPTER_CAPABILITIES)
```
"<adapter>": {
  "presence": "single|multiple",
  "ghost": { "tier": "full|display|none",
             "mechanism": "appservice|webhook|admin_post_as|personas|sender_override|none",
             "permission": "homeserver_admin|manage_webhooks|admin_api_key|server_setting|none" },
  "threads": true|false,
  "attachments": true|false,
  "dm_cold_initiate": true|false,      // can we DM a user who hasn't contacted us?
  "discovery": { "mailboxes": bool, "users": bool },
  "auth_methods": ["..."],
  "official": true|false,              // false = unofficial/ToS-risk (whatsapp_personal)
  "notes": "msgid? windows? badges?"
}
```
This makes /v1/adapters advertise not just "what exists" but HOW a presence can be realized
(real login vs which ghost mechanism) and what permission the operator must obtain.

## Presence realization: real login vs projected "ghost" (adapter-dependent)
A presence can be realized two ways, depending on adapter capability + admin permission:
- REAL login: an actual account we hold creds for (IRC nick, MM bot token). relay_via_bot.
- PROJECTED / ghost: a user that isn't really on the platform, rendered by a HOST connector with
  a puppeting power the mailbox/server admin granted — Matrix application services, Discord
  webhooks. relay_via_webhook / relay_via_appservice / relay_via_puppet.
Consequences:
- One host connector + permission can project MANY distinct-looking virtual presences with NO
  per-ghost account (great for multi-persona like several codex agents each appearing as their
  own user). IRC CANNOT do this (needs a real nick per presence); MM webhooks/override are limited.
- So presence-realization is an adapter-type CAPABILITY (add to catalog, e.g.
  "virtual_presence": true for matrix/discord) PLUS a runtime PERMISSION GATE (appservice
  namespace / webhook URL granted by the admin). Without the grant, fall back to relay_via_bot.
- Presence config gains a realization hint: { "realized_by": "login" | "ghost",
  "via": "<host-connector-id>", "relay_kind": "relay_via_webhook" }. Ghost presences have no
  own connection; they reference the host connector that projects them.

## Adapter CATALOG vs configured connectors (discoverability)
"What adapters exist" is a CODE-DERIVED catalog, separate from configured connectors/presences:
- Catalog = SUPPORTED_MAILBOX_TYPES + ADAPTER_CAPABILITIES (mailbox_relay.py), served at
  GET /v1/adapters. Lists all 12 implemented adapters (incl. whatsapp, whatsapp_personal) with
  capability flags (presence/threads/attachments/interaction/official). This is "what's possible".
- Connectors/presences = the LIVE subset we actually operate. "What's configured".
Pruning placeholder connectors does NOT hide availability — whatsapp is still in the catalog.
To message a new platform (e.g. a WhatsApp acct): consult catalog -> it's supported -> create a
presence/connector for it on demand (with creds) -> then message. Capabilities are ADAPTER-TYPE
level (from the catalog); connectors/presences just name their adapter and inherit them (don't
duplicate capability flags per connector).

## Mechanical worker configs (what the non-LLM daemon needs)
The dumb worker does only: LIVENESS (stay connected/joined), INGEST (inbound -> mailbox, dedupe),
FLUSH (send already-addressed outbound; run pure mechanical mirrors). It makes no decisions.
Below are the config record shapes it reads. Creds are ALWAYS env refs, never inline.

### A. Presence config (operated) — the login it must keep alive
An operated presence folds in its connection (connector = the "how to log in" of a presence;
1:1 for operated presences). Observed human presences have no `connection` and aren't operated.
```
{
  "id": "irc-quakenet-org-presence-jllykifsh",
  "kind": "presence",
  "adapter": "irc", "instance": "quakenet.org",
  "external_identifier": "jllykifsh", "display": "jllykifsh",
  "operated": true,
  "connection": {                         // present iff operated (this IS the connector)
    "server": "irc.quakenet.org", "port": 6667, "tls": false,
    "nick": "jllykifsh",
    "auth": { "method": "quakenet_q",     // per-network: quakenet_q | sasl_plain | nickserv | bot_token
              "account_env": "QUAKENET_Q_ACCOUNT", "password_env": "QUAKENET_Q_PASSWORD",
              "cloak": true },
    "keepalive": { "ping_interval_s": 60, "reconnect_backoff_s": [1,2,5,15,60,120] }
  },
  "availability": { "desired": "online" },// what we make the bot APPEAR as (set, not read)
  "join": ["irc-quakenet-org-mailbox-logicmoo"]  // mailboxes to stay in (or derive from membership)
}
```
Mattermost operated presence differs only in `connection`:
```
"connection": { "base_url": "https://chat.singularitynet.io",
                "auth": { "method": "bot_token", "token_env": "MM_TOKEN_MIN_BOTNICK" },
                "poll": { "interval_s": 2 } }         // MM has no socket; poll to ingest
```
NOTE fork: this replaces/renames today's `server_registry_connectors` with per-presence records
(server_registry_presences). Operated presence = old connector. (Fork I.)

### B. Channel config — where ingest lands, how to dedupe, exact wire name
```
{
  "id": "irc-quakenet-org-mailbox-logicmoo",
  "kind": "mailbox",
  "adapter": "irc", "instance": "quakenet.org",
  "external_identifier": "##logicmoo",    // EXACT wire name (sigil preserved; used to JOIN/POST)
  "display": "##logicmoo",
  "mailbox": "irc-quakenet-org-mailbox-logicmoo",   // ingested inbound lands here (one per mailbox)
  "members_operated": ["irc-quakenet-org-presence-jllykifsh",
                       "irc-quakenet-org-presence-prologmud"],  // our bots in it (sibling set)
  "dedupe": { "strategy": "msgid_or_heuristic", "window_s": 10 }
}
```
`members_operated` = the network-scoped "these nicks are us" set the ingest uses for sibling
recognition + loop governor (it does NOT ban sibling chatter).

### C. Relay config — ONLY for pure mechanical mirrors (no cognition)
For agent-authored outbound (items already carry target+presence), the flush needs NO relay
config — it routes by the item's own fields. Relays exist only for static verbatim mirroring
(mailbox/mailbox A -> external mailbox B) that must run with no LLM at all:
```
{
  "id": "mirror-events-to-logicmoo",
  "kind": "relay", "enabled": true,
  "source_mailbox": "server_events_log",
  "destination": { "presence": "irc-quakenet-org-presence-jllykifsh",
                   "mailbox": "irc-quakenet-org-mailbox-logicmoo" },
  "cursor": "mirror-events-to-logicmoo",     // consumption pointer (stored in agents-registry cursors)
  "transform": "verbatim",
  "governors": { "rate_per_min": 20, "max_hops": 8 }
}
```

### D. Outbox drain + routing (no per-item config; by item fields)
The worker drains configured outbox mailbox(es) and, per item, selects the presence to send
through using the item's own fields (connector/presence id -> instance -> default). Config is
just the list of outboxes + defaults:
```
{ "outboxes": ["server_outbound_relay_agent_to_mailbox"],
  "default_presence_by_mailbox": { "irc-quakenet-org-mailbox-logicmoo": "irc-quakenet-org-presence-jllykifsh" } }
```
An item with no presence uses default_presence_by_mailbox -> else sole operated member -> else
it is left unsent (a cognition/addressing gap, waits).

### E. Daemon/global settings + governors
```
{
  "poll_interval_s": 2,
  "reconnect_backoff_s": [1,2,5,15,60,120],
  "governors": { "max_hops": 8, "default_rate_per_min": 60, "dedupe_window_s": 10 },
  "operate_presences": ["irc-quakenet-org-presence-jllykifsh",
                        "irc-libera-chat-presence-prologmud",
                        "mm-chat-singularitynet-io-presence-min-botnick"],
  "drain_outboxes": ["server_outbound_relay_agent_to_mailbox"]
}
```

### F. Secrets
Never inline. Referenced by env var (`*_env`) resolved from config_dir/.env or the environment:
tokens (MM), Q account/password (QuakeNet), SASL creds (Libera). The worker only needs the refs.

### What the worker does NOT need
No reply logic, no claim/disown, no self-ID, no provenance decisions, no principal/active-presence
resolution. Those are LLM-only. Unaddressed inbound simply accumulates in mailboxes until an LLM
claims it.

## Do we need a server adapter with no LLM? Yes — but only a thin MECHANICAL one
LLM agents are on-demand/ephemeral; external reality is continuous. Split into two layers:
- MECHANICAL (persistent server worker; required when no LLM is around) — a DUMB executor:
  * Liveness: keep presences connected, reconnect, answer PING, stay in mailboxes. (IRC presence
    IS the live socket; you can't lazily reconnect per message without leaving the room.)
  * Ingest: pull inbound into the right mailbox, with dedupe.
  * Flush already-addressed outbound: if an item has target+text+presence, just send it. No
    decisions. Inbound that needs a REPLY is NOT answered — it waits in the mailbox for an LLM.
- COGNITIVE (on-demand LLM agents; never required to be running): claim/disown, compose,
  self-identify, choose provenance, cross-network routing choices, direct presence control.
Reconciliation: "adapter optional" is true for COGNITION/direct control (an LLM can bypass it and
drive a presence itself, even author provenance); a persistent worker is still needed for
MECHANICAL liveness+delivery+ingest. This worker already exists: the ChannelRelay daemon
(server.py); the multi-IRC work is its liveness/routing across servers. It is the floor, not the brain.

## The adapter/bridge agent is OPTIONAL (direct presence control)
Driving a presence (connect / send / poll via its connector) is a CAPABILITY, not the exclusive
job of a bridge agent. So the adapter agent need not exist:
- Agents monitor the outbox (server_outbound_relay_agent_to_mailbox) directly.
- When a message is unclaimed ("no one cares"), an agent takes DIRECT CONTROL of a presence
  (e.g. min.botnick) and performs the platform send itself using that presence's connector.
- A bridge/adapter agent, if present, is just a default worker doing the same thing; if absent,
  agents self-serve. Consistent with "allow & enable anything" + "intelligence in the agents".
Implication for code: the adapter is a reusable CAPABILITY (mattermost/irc send+poll bound to a
connector) that any agent can call, plus an OPTIONAL long-running agent that polls on their behalf.
The presence+connector is the drivable resource; "who drives it" is open (any agent).
Provenance is AGENT-AUTHORED: an agent that performs a direct send can stamp the record with
adapter-style provenance (adapter/connector/presence/sent-via) so records look uniform whether a
real adapter agent or a codex agent did it. No separate adapter identity is required to attribute
records; provenance fields are agent-fillable.

## Guiding principle: allow & enable anything (permissive by default)
The system enables everything by default and imposes NO hard prohibitions:
- Any agent may puppet any presence; multiple agents may share a presence; a presence may have
  no agent. Sibling bots MAY talk to each other (a feature, not just a loop to kill).
- Cross-network delivery, multi-reply, multiple bots in one mailbox: all allowed.
Safety is achieved with OPT-IN GOVERNORS, never bans:
- Loop safety: per-message hop/loop counter + relay_trace (already present) with an optional max;
  drop/park only when a configurable limit is exceeded.
- Dedupe: collapse duplicate ingests (see below) — a safety governor, not a restriction.
- Optional rate limits / suppression policies the operator can turn on.
Default posture = on/allowed; the operator opts into constraints.

## Intelligence lives in the agents (minimal plumbing)
Reply-routing / self-identification for SHARED presences is NOT a rigid protocol. The system
provides the (agent, presence) outbound path and fans inbound replies to the puppeting agents
via claim/disown. The codex agents themselves:
- self-identify in-band ("I'm your UI codex agent, speaking via min.botnick; tag me UI-CODEX"),
- ask for/relay an ack to confirm the mailbox works,
- recognize in natural language when a reply is meant for them (claim), else disown.
No marker grammar or correlation parser is required. Storing an established (agent, presence,
user) correlation is an OPTIONAL assist for auto-routing, not mandatory machinery. Where the
platform offers threads (Mattermost root_id), use them as a free correlation key; IRC has none,
so agents lean on natural-language self-ID + claim/disown.

## Terminology collision: "presence" — KEEP IT (availability is an attribute)
A presence is a bot/user ACCOUNT on one server, and it CARRIES availability state
(online / away / appear-offline) plus its own STORAGE AREA (session/credentials handle,
per-server cursors, per-server knowledge). So Matrix "presence=availability" isn't a clash:
availability is just one attribute of our presence. Fork H is RESOLVED: keep "presence".

Presence storage vs principal mailbox (distinct):
- Presence storage = per-account state: availability, session, per-server seen-markers, and
  anything the account needs "to know" on that specific server.
- Principal mailbox = where cross-network messages converge for delivery to the person.

Agents and presences (0..N):
- An agent MAY own zero presences (internal-only agents that never log into any server, e.g.
  worker/pool/server agents). Others own one or many presences across networks.

## Decisions (defaults chosen; forks flagged)

### 1. Identity / naming (kills -primary)
Connector id = self-describing address `adapter/instance/identity`, instance = network host
(lowercased), identity = login nick / bot user:
- `irc/quakenet.org/jllykifsh`
- `irc/libera.chat/prologmud`
- `mattermost/chat.singularitynet.io/<botuser>`
Scales to multiple nicks per network (the reason `irc-quakenet` is already fragile).
Placeholder/disabled template connectors (discord/matrix/...) either get dropped from the
seed or keyed `adapter/<instance>/<identity>` when actually configured — no `-primary`.
FORK A: RESOLVED (user 2026-08-22 "i like the 2 slashed IDs") — SPLIT by id class:
  - Presence/connector ids = SLASH address form `adapter/instance/identity`
    (irc/quakenet.org/jllykifsh, irc/libera.chat/prologmud,
    mattermost/chat.singularitynet.io/<botuser>). Safe because these are ENTRIES inside
    server_registry_*.json (dict keys), never files on disk.
  - Channel/mailbox ids = KEEP the hyphen mailbox_resource_id() slug (former Fork D):
    irc-quakenet-org-logicmoo, mm-chat-singularitynet-io-... Each is its OWN file
    (<id>.json) so it cannot contain '/'.
  So slash for registry-entry ids, hyphen-slug for file-backed mailbox/mailbox ids.

### 2. Presence vs connector vs principal
Presence = a per-server user identity (bot OR human). Connector = only exists for presences
WE operate (our credentials to drive that login); external humans are presences with no
connector. Principal binds presences across networks (email-keyed). Our agents are
principals too; a bridge agent's bot logins are its presences.

### 3c. Multiple presences in one mailbox (e.g. two of our bots in ##logicmoo)
A mailbox is ONE object/mailbox no matter how many presences sit in it. What multiplies is
MEMBERSHIP: mailbox <-> presence is many-to-many, tracked per pair
(joined?, our modes op/voice, last-seen). Consequences (all handled permissively):
- Inbound dedupe: if two of our bots both witness a mailbox line, ingest it ONCE into the
  mailbox mailbox. Prefer IRCv3 msgid; QuakeNet may lack it, so fall back to heuristic key
  (mailbox, source-nick, text, ~time-window). Dedupe is a safety governor, not a block.
- Sibling recognition: every adapter must know ALL our presences on a network (network-scoped
  "these nicks are us") so a sibling bot's output isn't blindly re-ingested as external. But per
  the permissive principle, sibling-to-sibling chat is ALLOWED; recognition feeds the loop
  governor (hop count), it does not ban the exchange.
- Outbound has TWO axes: (a) which AGENT composes (claim/disown, may be several) and (b) which
  PRESENCE utters it on the wire. A reply is an (agent, presence) pair; puppeting links
  agent->presence. So agentA speaks as jllykifsh and agentB as prologmud -> the room sees two
  bots answer. Composes with min.botnick (many agents -> one presence).
- Default speaker: if an outbound to a mailbox names no presence, use a configured default
  presence for that mailbox (else the only member; else error).

### 3. Agent topology
Agents PUPPET presences (many-to-many). "Ownership" is NOT a system concept — it exists only
in the operator's/configurator's head. The system knows only: puppeting links + per-message
claim/disown. A "1-to-1 owned" presence is just a configuration where a single local agent is
the only one that claims it (emergent, not an enforced relation — no ownership field).
ONE `irc-bridge-agent` can puppet multiple presences (jllykifsh@quakenet, prologmud@libera);
a shared presence (min.botnick) is puppeted by several agents; some presences have no agent and
some agents have no presence. The adapter agent routes to the right server per presence.
POSSIBLE RENAME: irc-bridge-agent -> irc-adapter-agent (user's wording).

### 3b. Dynamic puppeting (claim / disown per message)
Puppeting is decided per-message by CONTENT; there is no ownership relation to consult:
- An inbound message to a presence is offered to the agents that puppet it; each CLAIMS or
  DISOWNS based on what's being asked. Disown is per-message, not permanent.
- A presence configured with a single puppeting agent that always claims LOOKS 1-to-1, but
  that's just configuration (the operator's mental "ownership"), not a system field.
Design implication — inbound to a shared presence is a dispatch:
  message -> presence's candidate agents -> (claim | disown) -> EVERY claimer replies as presence.
Multiple claimers do NOT arbitrate to one winner: each claiming agent replies AS the presence,
so one inbound can yield several outbound messages under the same presence (min.botnick speaks
multiple times). Internally record which agent produced each reply (attribution); externally it
is all the one presence.
Open: fallback when ALL disown (queue, default agent, or drop with a notice). Optional: cap or
order multiple replies; whether agents can see each other's claims before replying.

### 4. Channel ids MUST be network-qualified
Two networks can both have `#test`. Use `mailbox_resource_id(adapter, instance, identifier)`
(already includes instance) for every mailbox/mailbox id, e.g. `irc-quakenet-org-logicmoo`.
Cursors keyed by these ids are therefore network-safe.

### 5. Routing (outbound mailbox -> right network+bot+mailbox)
Rule: the relay that emits an outbound record STAMPS `connector_id`; the router honors
connector_id first, then endpoint instance, then first. endpoint_address alone (instance,
no identity) can't disambiguate two nicks on one network, so connector_id is authoritative.
(Adapter-per-connector + connector_id/instance routing already built in mailbox_relay.)

### 6. Cursor ownership
Relay/bridge cursors live under the owning bridge agent's registry entry
(entries[irc-bridge-agent].cursors = {mailbox_resource_id: {next, consumed}}), one map
spanning all its presences' mailboxes. Consistent with mm relays' cursors under the mm bridge.

### 7. Lifecycle (join/list/leave/mailboxes/mailboxes)
- list [--on adapter/instance]  -> discovery on that presence; all mailboxes, joined or not.
- join adapter/instance/#chan   -> full onboarding: select/ensure connector, add #chan to
  its mailbox_ids, create the local mailbox (network-qualified id), subscribe relevant
  agents + init cursors, and tell the live adapter to JOIN. (matches the "join does it all".)
- mailboxes                      -> mailboxes we hold a presence on and are handling.
- mailboxes                     -> all cursor-consumed local streams.
- leave/part                    -> PART + unsubscribe (+ optionally stop monitoring).

### 8. Regeneration (since history is disposable)
Define a small SEED spec (agents, connectors/presences, relays, subscriptions). A builder
regenerates server_registry_{agents,connectors,mailboxes,relays} from the seed and drops
message-history mailbox files. Registries kept; logs discarded.

## Open forks to confirm
A. RESOLVED (user "i like the 2 slashed IDs"): presence/connector ids use the SLASH
   address form `adapter/instance/identity` (registry entries, never files);
   mailbox/mailbox ids keep the hyphen mailbox_resource_id() slug (they are files on
   disk). Slash for entry ids, hyphen-slug for file-backed ids. See Fork I section.
B. Agent topology: one irc bridge agent owning many presences (recommended) vs one per persona.
C. Rename irc-bridge-agent -> irc-adapter-agent? (+ mattermost-bridge-agent -> mattermost-adapter-agent?)
D. Instance granularity: bare network host (quakenet.org) vs full (irc.quakenet.org)?
   [RESOLVED for resource-id slugs: keep the plain adapter slug and POP a leading host label that
   equals the slug/canonical name, then prepend the slug -> irc.quakenet.org=>irc-quakenet-org,
   mm.snet=>mm-snet, chat.singularitynet.io kept=>mm-chat-singularitynet-io. Endpoint ADDRESSES
   still use the full host: irc/irc.quakenet.org/<id>. CANONICAL_TYPES unchanged.]
E. Do we keep the disabled template connectors at all, or seed only real ones on demand?
F. Principal key: email (logicmoo@gmail.com) as the canonical id, or an opaque principal id
   with email as one of several bindable keys? How do we handle a presence with no known email
   (create a provisional principal, bind later)?
G. Do presences (esp. external humans) get their own mailbox, or only principals do? i.e. is a
   mailbox keyed by presence or by principal?  [RESOLVED: per principal]
H. Rename our "presence"? [RESOLVED: keep "presence"; availability is an attribute of it]
I. Replace server_registry_connectors with per-presence records (server_registry_presences),
   folding operated-presence connection/creds into the presence? (operated presence = old connector)

## mm/instance/<name>: user vs mailbox disambiguation (user question 2026-08-22)
QUESTION: "some command need to use mm/instance/test but how do we know if its the user or mailbox?"
CURRENT CODE (answer):
  - The address alone is AMBIGUOUS: EndpointAddress.inferred_type returns "unknown" for mattermost
    (endpoint_address.py:98). IRC disambiguates by sigil (#/&/+/! => mailbox, else user); MM has none.
  - Kind comes from COMMAND CONTEXT: each MM subcommand passes explicit kind= to _id()
    (kind="mailbox" for mailbox args, kind="user"/_user_id for user args; mattermost_adapter.py:551,741,664).
  - IdentifierDirectory stores a `kind` per entry; _id() filters lookups by it. If a name matches BOTH
    (or is ambiguous), it raises "ambiguous Mattermost name; use mm/INSTANCE/ID" (:543) and you must use
    the opaque 26-char id (:532 treats a 26-char alnum as already-resolved).
  - FACT: in MM everything posted to is a mailbox_id; a DM is a mailbox of type "D". Messaging a user =
    username -> user_id -> get/create direct mailbox -> mailbox_id (_send_outbound always uses mailbox_id, :331).
GAP: typed commands are fine (arg slot implies kind); a GENERIC address field (relay target, bare
  send-to) has no kind and stays ambiguous.
OPTIONS (address-syntax decision — user steers, NOT chosen yet):
  A (RECOMMENDED): adopt MM's native sigils in the address — mm/instance/@test = user (=> DM mailbox),
    mm/instance/~test or bare mm/instance/test = mailbox. Self-describing, mirrors IRC, extends
    inferred_type. Would add MM handling to EndpointAddress.inferred_type + resolution.
  B: keep command-context kind only; generic addresses error when ambiguous (status quo).
  C: resolution rule (try mailbox then user; or directory kind), error if both exist.
STATUS: analysis recorded; awaiting user's pick before any code.

## Registry filename convention (user 2026-08-22): CONFIRMED, keep `server_registry_<kind>`
User: "i love them.. but they need to indicate the kind of thing they register in their names."
The 5 registry files (mailbox_store.py:28-32) already comply: server_registry_{agents,mailboxes,
connectors,identifiers,relays} — each names the KIND it registers. RULE: every registry file is
`server_registry_<kind>` and must name the kind of thing registered. No renames needed.
The new Fork I registry therefore MUST be `server_registry_presences` (kind = presences).

## Fork I — resolution + staged plan (presences are first-class, not just connections)
USER STEER (2026-08-22): "presences are where we add lots of things not just connections."
So server_registry_presences is a first-class, GROWING registry; the old connector is merely the
`connection` sub-block of an OPERATED presence. Resolution: YES, replace server_registry_connectors
with server_registry_presences.

Presence record (superset; connection only when operated; room to grow):
```
{
  "id": "<presence-id>",            // SLASH form: adapter/instance/identity
                                    //   e.g. irc/quakenet.org/jllykifsh (registry entry, not a file)
  "kind": "presence",
  "adapter": "irc", "instance": "quakenet.org",
  "external_identifier": "jllykifsh", "display": "jllykifsh",
  "operated": true,                 // false = observed human/other bot (no connection block)
  "principal": null,                // Fork F: bind presence -> person (email/opaque) when known
  "puppeteers": [],                 // agents that may puppet (claim/disown per msg); [] allowed
  "availability": { "desired": "online" },
  "join": ["<mailbox-id>", ...],
  "connection": { ...creds via *_env... },   // present iff operated (this IS the old connector)
  // GROWTH SLOTS (per user steer): roles/permissions, ghost/projection policy, per-presence
  //   governors (rate/hops), storage handle, capabilities overrides, notes, etc.
}
```

Migration from today's 3 connectors -> operated presences (SLASH ids):
  irc/libera.chat/prologmud, irc/quakenet.org/jllykifsh, mattermost/chat.singularitynet.io/<botuser>.
  connector.{server,port,tls,nickname,token_env,base_url,...} -> presence.connection;
  connector.mailbox_ids -> presence.join (mailbox/mailbox ids stay hyphen-slug);
  connector.bridge_agent -> presence.puppeteers (Fork C may rename *-bridge-agent ->
  *-adapter-agent); adapter/instance carried up to the presence.

BLOCKERS (do NOT implement until resolved — these affect stored field values):
  - Fork A: RESOLVED — presence/connector ids = slash form adapter/instance/identity;
    mailbox/mailbox ids stay hyphen-slug (files). NOT a blocker.
  - Fork C: *-bridge-agent -> *-adapter-agent rename affects puppeteers values.
  - Fork F: principal key (email vs opaque) affects the `principal` field.
  A/C/F note: only C and F remain open. Neither blocks the STRUCTURE (registry, shim,
  migration) — they only fix two field VALUES (puppeteers ids, principal key), which can be
  filled/backfilled once chosen. So stages 1-2 below are technically unblocked.

Staged implementation (structure unblocked; C/F only fix field values), lowest-risk first:
  1. Add mailbox_store.PRESENCES_REGISTRY = "server_registry_presences"; write migrated records.
  2. Add load_presences(); keep load_connectors()/connectors_for() as a THIN COMPAT SHIM that
     projects presences back to the legacy connector dict shape, so the ~15 adapters +
     mailbox_relay/server/subscriptions/chat_admin/discovery_admin keep running UNCHANGED.
  3. Grow presence-native fields + a presences CLI/REST surface incrementally.
  4. (Later, optional) migrate consumers off the shim to a presence-native API; retire connectors.
Rationale for shim-first: connectors_for/load_connectors is consumed by every adapter; a big-bang
cutover is high-risk and violates "one step, nothing more". Shim keeps 223 tests green throughout.
STATUS: design recorded; Fork A resolved (slash entry ids / hyphen file ids). C/F only
affect two field values, not structure.
