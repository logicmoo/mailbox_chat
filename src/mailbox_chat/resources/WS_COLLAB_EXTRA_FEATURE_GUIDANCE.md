# ws_collab extra feature guidance

This guide covers the higher-value features exposed by the `mailbox_chat`
ws_collab-compatible surface at `/ws_collab` and `/ws_collab/v1`.

## 1) Use mailbox-specific streams instead of one global conversation

Create purpose-specific mailboxes so automation, status chatter, and user-facing
conversation are isolated:

```powershell
curl -s -X POST http://127.0.0.1:46667/ws_collab/v1/mailbox/create `
  -H "Content-Type: application/json" `
  -d "{\"id\":\"ops_status\",\"purpose\":\"adapter and runtime status\"}"
```

Then publish/send directly to that mailbox:

```json
{"type":"publish","stream":"ops_status","event_type":"status","data":{"text":"relay healthy"}}
```

## 2) Make writes idempotent for retries and reconnects

When clients can retry, always set `idempotency_key` on publish/send to prevent
duplicate records:

```json
{
  "type": "publish",
  "stream": "conversation",
  "event_type": "message",
  "data": {"text": "deployment done"},
  "idempotency_key": "deploy-2026-01-01T00:00:00Z-step-4"
}
```

## 3) Track per-agent progress with mailbox cursors

Use cursor state so each agent can resume from where it left off:

1. Initialize cursor:
   `POST /ws_collab/v1/mailbox/cursor {"mailbox":"conversation","agent":"worker-a","start":"beginning"}`
2. Inspect lag:
   `GET /ws_collab/v1/mailbox/cursor?mailbox=conversation&agent=worker-a`
3. Clear/reset when needed:
   `DELETE /ws_collab/v1/mailbox/cursor?mailbox=conversation&agent=worker-a`

This keeps consumers independent and avoids "everyone shares one offset."

## 4) Prefer WebSocket for live updates + replay

Recommended connect flow:

1. Connect to `/ws_collab/ws` (or `/ws_collab/v1/ws`).
2. Wait for `auth_ok`.
3. Send:

```json
{"type":"subscribe","streams":["conversation"],"cursors":{},"filters":{}}
```

4. Handle `event` frames, and rely on `caught_up` to know replay completion.
5. Use `publish` + `ack_id` for request/ack correlation.

The server supports reconnect-friendly replay via stream cursors.

## 5) Use mailbox config + subscriptions for operational policy

- `POST /ws_collab/v1/mailbox/mailbox-config` for per-mailbox tuning metadata.
- `POST /ws_collab/v1/mailbox/subscription` to record agent-mailbox membership.
- `GET /ws_collab/v1/mailbox/agents` to discover active/known identities.

Treat these as control-plane state rather than message content.

## 6) Useful patterns

- **Human + bots mixed**: keep `conversation` for shared chat, and route bot-only
  telemetry to `ops_status`.
- **Long-running jobs**: write progress updates to a dedicated mailbox and let UI
  clients tail with `GET /ws_collab/v1/streams/<mailbox>/tail?count=...`.
- **Edits/corrections**: use `POST /ws_collab/v1/mailbox/record` and include the
  prior message id in `id`.
