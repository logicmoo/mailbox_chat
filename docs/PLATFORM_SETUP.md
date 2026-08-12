# Platform setup

This relay never stores platform secrets in `listeners.json`. Put tokens in
`config/.env` (or the process environment), then reference their environment
variable names from listener entries.

## Discord

The implemented bidirectional adapter requires a Discord application with a
bot user. A webhook URL is sufficient only for outbound-only posting and is
not yet the transport used by this adapter.

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Follow Discord's [bot getting-started guide](https://docs.discord.com/developers/quick-start/getting-started)
   to create/reset the bot token. Store it as `DISCORD_BOT_TOKEN`.
3. In Installation/OAuth2, generate an install URL containing the `bot` scope,
   then install it into the desired server. Discord documents bot install URLs
   and permissions in [OAuth2 and Permissions](https://docs.discord.com/developers/platform/oauth2-and-permissions).
4. Grant only the needed channel permissions: View Channel, Read Message
   History, Send Messages, and Attach Files.
5. Enable Discord Developer Mode, copy each channel ID, place the IDs in
   `DISCORD_CHANNEL_IDS`, and enable the `discord-primary` listener.

The bot token is a password. Do not put it in an install URL, listener file,
mailbox record, or Git commit. Discord notes that webhooks are simpler when an
integration only needs to push messages; see its
[Bots & Companion Apps guidance](https://docs.discord.com/developers/platform/bots).

## Slack

1. Create a Slack app and bot installation using Slack's
   [authentication guide](https://api.slack.com/authentication).
2. Add the bot scopes required by the configured conversations:
   `chat:write`, `files:write`, and the applicable history scopes such as
   `channels:history`, `groups:history`, `im:history`, or `mpim:history`.
   Slack explains conversation visibility and token-specific scope behavior in
   [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).
3. Install/reinstall the app into the workspace and store the resulting
   `xoxb-...` bot token as `SLACK_BOT_TOKEN`. Slack's
   [OAuth installation guide](https://api.slack.com/authentication/oauth-v2)
   covers multi-workspace installations.
4. Invite the bot to every private or public channel it must read, then copy
   channel IDs into `SLACK_CHANNEL_IDS` and enable the Slack listener.

Outbound messages use [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postmessage).
Each listener can name a different `token_env` and `presence_id`, allowing the
relay to represent multiple bot presences or workspaces simultaneously.

## Matrix / Element

The adapter speaks the Matrix Client-Server API; Element is one convenient
client for creating and administering the Matrix account.

1. Create a dedicated Matrix account on the chosen homeserver.
2. Obtain an access token for that account and store it as
   `MATRIX_ACCESS_TOKEN`. The Matrix bot introduction describes the
   [dedicated-account and access-token setup](https://matrix.org/docs/older/matrix-bot-sdk-intro/).
3. Invite/join the account to each room it should relay. The account must be a
   room member; Matrix distinguishes human-friendly aliases from the room IDs
   required by message APIs.
4. Put the homeserver URL in `MATRIX_HOMESERVER`, the `!room:server` IDs in
   `MATRIX_ROOM_IDS`, and enable the Matrix listener.

The implementation uses `/sync`, room message events, and media uploads from
the official [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/).

## Telegram

1. Create a bot with BotFather and store its token only as
   `TELEGRAM_BOT_TOKEN` in `config/.env`.
2. Add the bot to each group, supergroup, or channel it must relay. Grant only
   the permissions required to read and send the intended content.
3. Put the numeric chat IDs or `@channelusername` values in
   `TELEGRAM_ALLOWED_CHAT_IDS`, then enable `telegram-primary` in
   `config/listeners.json`.
4. Set `include_direct_messages` only when private conversations are explicitly
   in scope.

The adapter uses the official [Telegram Bot API](https://core.telegram.org/bots/api).
It long-polls `getUpdates`, calls `getChat` to cache readable labels for opaque
chat IDs, sends text with `sendMessage`, uploads attachments with `sendDocument`,
and preserves `message_thread_id` for forum topics.

## WhatsApp Business

Configure a Meta WhatsApp Business phone-number ID and conversation allowlist
on `whatsapp-business-primary`. Provide the system-user access token, webhook
verification token, and app secret through `config/.env`. Register the public
HTTPS callback as `/v1/webhooks/whatsapp`. Meta template and 24-hour conversation
rules still apply to outbound business messages.

## Facebook Messenger

Configure a Facebook Page ID and permitted PSIDs on
`facebook-messenger-primary`. Provide the Page access token, webhook verification
token, and app secret through `config/.env`. Register the public HTTPS callback
as `/v1/webhooks/facebook-messenger` and grant the Page messaging permissions
required by the deployment.

## IRC

Set `IRC_SERVER`, `IRC_CHANNELS`, and optional password/NickServ settings. IRC
does not provide native attachment upload, so configure
`MAILBOX_RELAY_PUBLIC_URL`; the adapter publishes managed files through the
relay's `/v1/attachments/` endpoint.

## Mattermost

Create a bot account in the Mattermost system console, add it to the desired
channels, and set `MM_URL`, `MM_BOT_TOKEN`, `MM_CHANNEL_ID`, and optional
`MM_CHANNEL_IDS`. The token belongs only in `config/.env`.

## Channel-to-channel controllers

Routes under `routes` in `config/listeners.json` choose one controller:

- `relay_agent` sends a `channel_route_request` to any mailbox identity.
- `presence_controller` immediately emits mailbox delivery requests using the
  destination listener/presence. It also does not require an agent entry.

Use a relay agent when routing requires reasoning, moderation, translation, or
approval. Use a presence controller for deterministic channel mirroring.

Trusted speakers listed in a listener's `trusted_admins` may manage routes from
chat using the portable ASCII command prefix:

```text
!relay routes
!relay attach slack-primary C0123456789 presence
!relay attach matrix-primary !room:example.org agent:moderating-router
!relay detach runtime-discord-primary-slack-primary-ab12cd34
```

The default `!relay` prefix works in IRC and every implemented chat adapter.
Override it with `MAILBOX_RELAY_COMMAND_PREFIX` or a listener-specific
`command_prefix`. Mailbox identities are open identifiers and require no
separate registration file.
