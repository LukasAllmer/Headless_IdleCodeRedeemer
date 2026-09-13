# Discord bot setup

How to give `icr` access to the Idle Champions code announcements.

## There is no bot program to run

This trips people up, so it is worth stating plainly: **a Discord "bot" is an
application and a token, not necessarily a running process.**

`icr` does not connect to Discord's gateway and does not use `discord.py`. It
calls one REST endpoint on a timer:

```
GET https://discord.com/api/v10/channels/{mirror}/messages?after={last_id}
Authorization: Bot <token>
```

That is the entire integration — about 100 lines in
[`src/icr/sources/discord_follow.py`](../src/icr/sources/discord_follow.py).
You still have to *create* a bot application to get a token and to grant it
access to a channel, but there is no second service to deploy, no websocket to
keep alive, and nothing extra to restart. Everything below is configuration in
Discord's UI.

**Why not read the Idle Champions channel directly?** You cannot. Bots can only
read channels in guilds they have been invited to, and you cannot invite a bot
to someone else's Discord. The workaround is Discord's *channel following*
feature, which mirrors an announcement channel into a guild you control.

```
Idle Champions guild            your guild                    icr
#combinations  ──follow──▶  #ic-codes  ──REST poll──▶  codes table
(announcement)              (mirrored via webhook)
```

---

## 1. Follow #combinations into your own guild

Skip if you have already done this.

1. Create a Discord server of your own if you do not have one
   (**+** in the server list → *Create My Own*).
2. Create a text channel in it, e.g. `#ic-codes`.
3. Go to the [Idle Champions Discord](https://discord.com/invite/idlechampions)
   and open **#combinations**.
4. Click **Follow** in the channel header. If there is no Follow button, the
   channel is not an announcement channel and this approach will not work.
5. Pick your server and `#ic-codes` as the destination.

Discord creates a webhook that cross-posts every new `#combinations` message
into your channel from then on.

> **There is no backfill.** Following only mirrors messages posted *after* you
> set it up. Existing codes will not appear. Add any you care about by hand with
> `icr code add`.

Post a test message in `#ic-codes` to confirm you can see it, then wait for a
real cross-post to confirm the follow works.

## 2. Create the bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. **New Application** → give it a name (e.g. `icr`) → **Create**.
3. Open the **Bot** tab in the sidebar.
4. Under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT** and
   save.

**The Message Content Intent is not optional.** Without it Discord returns
messages with an empty `content` field — including over the REST API, not just
the gateway. The poll will succeed, find zero codes, and log nothing alarming.
It is the single most common reason this integration silently does nothing.

You can toggle it yourself as long as the bot is in fewer than 100 servers.
Above that it requires Discord verification, which does not apply here.

## 3. Copy the token

Still on the **Bot** tab: **Reset Token** → **Yes, do it** → **Copy**.

Discord shows the token exactly once. If you lose it, reset again — resetting
invalidates the old one, so anything still using it will start getting 401s.

The token is a password for the bot account. Do not commit it, do not paste it
into a support channel, and do not put it in the compose file directly — use the
interpolation described in step 6.

## 4. Invite the bot to your guild

Build an invite URL with your application's **Application ID** (found on the
**General Information** tab):

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot&permissions=66560
```

`permissions=66560` is exactly two permissions:

| Bit | Permission | Why |
| --- | --- | --- |
| 1024 | View Channel | See the mirror channel exists |
| 65536 | Read Message History | Fetch messages posted before now |

Nothing else is needed. The bot never sends messages, never manages anything,
and never reads any other channel. Do **not** grant Administrator.

Open the URL, pick your server, authorise. The bot appears offline in the member
list forever — that is expected and correct, since nothing ever connects to the
gateway.

If you would rather not hand-build the URL: **OAuth2 → URL Generator**, tick
`bot`, then tick *View Channel* and *Read Message History*.

## 5. Get the mirror channel ID

1. **User Settings → Advanced → Developer Mode** → on.
2. Right-click `#ic-codes` → **Copy Channel ID**.

You want the ID of **your mirror channel**, not the Idle Champions
`#combinations` channel. It is a long number like `1234567890123456789`.

## 6. Configure icr

### Docker

Put the secrets in a `.env` next to `docker-compose.yml`. Compose reads this
file for interpolation; it is not the application's config and is not copied
into the image.

```bash
# .env
ICR_DISCORD_ENABLED=true
ICR_DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.exampleexampleexample
ICR_DISCORD_MIRROR_CHANNEL_ID=1234567890123456789
```

```bash
chmod 600 .env
docker compose up -d
```

The corresponding lines already exist in
[docker-compose.yml](../docker-compose.yml); they read from the environment and
default to disabled.

### Without Docker

Same three variables in the application's own `.env` (see
[.env.example](../.env.example)), then restart the service.

## 7. Verify

```bash
docker compose exec icr icr source list
docker compose exec icr icr poll
```

A working first poll looks like:

```
discord: 3 found, 3 new
```

Then confirm the codes landed and are queued:

```bash
docker compose exec icr icr code list
docker compose exec icr icr status
```

`icr source list` shows how far the source has read:

```
┏━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ Source  ┃ Enabled ┃ Read up to         ┃
┡━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ discord │ yes     │ 1234567890123456789 │
└─────────┴─────────┴────────────────────┘
```

Once this works, nothing further is needed — the service polls every
`ICR_SOURCE_POLL_INTERVAL_SECONDS` (default 300) and redeems on its own.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `discord: 0 found` every time, but the channel has codes | Message Content Intent is off — `content` comes back empty | Enable it (step 2), then `icr source reset discord` and poll again |
| `Discord rejected the bot token (401)` | Token wrong, or was reset after you copied it | Copy a fresh token (step 3) |
| `Discord returned 403 for the mirror channel` | Bot lacks View Channel / Read Message History | Re-invite with `permissions=66560`, or grant them on the channel |
| `Discord channel … not found` (404) | Wrong ID, or bot is not in that guild | Re-copy the channel ID (step 5); confirm the bot is a member |
| Poll succeeds, no new codes ever | Cursor is past everything; nothing new posted since | Normal. Force a re-scan with `icr source reset discord` |
| Codes appear in Discord but not in `icr code list` | Source disabled | `icr source list` — if *Enabled* is `no`, `ICR_DISCORD_ENABLED` is not reaching the container |
| Nothing mirrors into your channel at all | The follow was never set up, or was removed | Redo step 1 |

Raise the log level for detail:

```bash
docker compose exec icr env ICR_LOG_LEVEL=DEBUG icr poll
```

The bot token is redacted from all log output, so pasting logs is safe.

### Re-scanning messages

`icr source reset discord` forgets the source's position so the next poll
re-reads the most recent messages (up to `ICR_DISCORD_FETCH_LIMIT`, max 100).

This is safe: codes already known are not added twice, and codes already
redeemed are not redeemed again — the `codes` table is unique on the code and
the per-account ledger is unique on `(account, code)`.

---

## How it works

- **Cursor.** The highest message ID seen is stored in the `kv` table under
  `discord:last_message_id` and passed as `?after=` on the next poll, so
  restarts do not re-scan and messages are never processed twice.
- **Extraction.** Both `content` and embed fields are scanned, since
  announcement posts sometimes put codes in an embed. The regex is ported from
  the original extension and matches 12- or 16-character codes with optional
  dashes. It is deliberately loose: a false positive costs one API call and is
  recorded as `invalid`, which is cheaper than missing a real code.
- **Failure isolation.** A source that throws is logged and reported, never
  fatal — a Discord outage does not stop manual codes from being redeemed.
- **Rate limits.** A 429 is retried up to three times, honouring Discord's
  `retry_after`.

## Adding other sources later

The source framework is deliberately small. A new source is one module
implementing `CodeSource` — `name`, `enabled()`, `poll()` — plus a `register()`
call. It only returns codes; deduplication, storage and redemption are handled
for it. See [`src/icr/sources/base.py`](../src/icr/sources/base.py).
