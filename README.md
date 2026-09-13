# Idle Code Redeemer

Automated Idle Champions code redemption for multiple Steam accounts, running
headless.


Codes are discovered (from Discord, two community code lists, a newsletter
mailbox, or entered by hand), stored once, and redeemed against every account
that may use them. A per-account ledger in SQLite means a code is never submitted
twice for the same account, across restarts.

This replaces a Chrome extension that scraped the Discord web UI.
## AI Notice

There still is a lot of cleanup to be done. The porting work was almost exclusively done by AI.

## How it works

The database is both the source of truth and the work queue. Code sources are
producers that only insert into `codes`; one paced worker drains the outstanding
`(code × account)` matrix. Manual entry and automated discovery take the same
path, so adding a new source later means one module and one registry line.

```
sources (discord, incendar, fandom, email, manual) ──▶ SQLite ──▶ redeemer ──▶ game API
```

A code is normally offered to every account. Newsletter codes are the exception:
they are single-use and belong to the subscriber, so they are pinned to the one
account whose mailbox received them.

Sources overlap heavily — the same code often shows up on Discord, both code
lists and in the newsletter. That costs nothing; the first sighting wins and the
rest are ignored.

**There is one timer: the source poll.** Redemption has none. It runs after a
poll that left work outstanding, and whenever you trigger it (`icr redeem`, or
the web UI). Retries ride the next poll rather than needing a clock of their own.
An idle installation does nothing and logs nothing. The first poll happens
immediately on start, so a restart never sits idle waiting out an interval.

## Install

### Docker (recommended)

```bash
git clone <this repo> && cd Headless_IdleCodeRedeemer
docker compose up -d
```

That is the whole setup. Configuration lives in
[docker-compose.yml](docker-compose.yml) under `environment:` — edit it there
rather than creating a `.env`. Secrets are interpolated from your shell or a
sibling `.env`, so nothing sensitive needs to be committed:

```bash
# .env next to docker-compose.yml, read by compose (not by the app)
ICR_DISCORD_ENABLED=true
ICR_DISCORD_BOT_TOKEN=…
ICR_DISCORD_MIRROR_CHANNEL_ID=…
```

Run CLI commands inside the running container — it shares the database with the
service, so codes you add are picked up on the next tick:

```bash
docker compose exec icr icr account add --name main --support-url "https://…"
docker compose exec icr icr status
docker compose exec icr icr redeem --dry-run
docker compose logs -f
```

Logs go to stdout. The database is a real file on the host — see below.

The image is Alpine-based, ~107 MB, runs as an unprivileged user, and builds
with either BuildKit or the legacy builder.

### Without Docker

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> /opt/icr && cd /opt/icr
uv sync --frozen
cp .env.example .env && chmod 600 .env   # then edit it
```

Environment variables always win over `.env`, which is why the container passes
everything as environment and never ships a `.env`.

## Configure

All configuration is in `.env` (see [.env.example](.env.example) for the full
list with comments). Account credentials are the deliberate exception — they
live in the database, because `instance_id` is mutable state the service
rewrites itself.

### Adding accounts

Open the in-game support page and copy the whole URL from the address bar; it
carries both credentials:

```bash
icr account add --name main --support-url "https://…?user_id=…&device_hash=…"
icr account add --name alt  --user-id 12345 --hash abcdef…   # or explicitly
icr account list
```

### Public code lists

Two community-maintained pages of multi-use codes. No credentials, nothing to
set up — both are on by default:

| Source | Page |
| --- | --- |
| `incendar` | <https://incendar.com/idlechampions_codes.php> |
| `fandom` | <https://idlechampions.fandom.com/wiki/Combinations> |

Turn either off with `ICR_INCENDAR_ENABLED=false` / `ICR_FANDOM_ENABLED=false`.
They are polled on the same interval as every other source, with a conditional
request so an unchanged page costs almost nothing.

Between them these carry the long-lived codes that Discord announcements scroll
past, so expect a burst of redemption attempts the first time you run them —
most will come back `already_redeemed` or `expired`, which is terminal and
costs one call each.

### Discord source

**→ Full walkthrough: [docs/discord-bot.md](docs/discord-bot.md)**

`#combinations` on the Idle Champions Discord is an Announcement channel, so it
can be *followed* into your own guild — Discord cross-posts every new message
there and a bot you control can read the mirror. No user-account automation, no
headless browser.

There is **no bot program to run.** `icr` polls one REST endpoint with a bot
token; creating the bot is Discord-side configuration only. In short:

1. Follow `#combinations` into a channel in your own guild.
2. Create a bot application, enable the **Message Content Intent**, and invite
   it to your guild with `View Channel` + `Read Message History`
   (`permissions=66560`).
3. Set `ICR_DISCORD_ENABLED=true`, `ICR_DISCORD_BOT_TOKEN` and
   `ICR_DISCORD_MIRROR_CHANNEL_ID`.

Two things that catch people out: without the Message Content Intent Discord
returns empty message content and the source silently finds nothing, and
following provides no backfill — only messages posted after you set it up.

### Email source

**→ Full walkthrough: [docs/email-source.md](docs/email-source.md)**

Each game account can subscribe to the Idle Champions newsletter, which mails a
code to every subscriber. Those codes are **single-use and personal**, so each
mailbox is tied to one game account and its codes are never offered to the
others.

Every folder is searched, not just the inbox, and nothing is marked as read.
Plain IMAP and Microsoft (Outlook/Office 365, OAuth2) are both supported.

```bash
icr mailbox add --name main-mail --account main --host imap.gmail.com --username you@gmail.com
icr mailbox test main-mail      # connects and reports, stores nothing
icr mailbox list
```

Microsoft mailboxes need a client id and `icr mailbox authorize NAME`, which
prints a code to enter at microsoft.com/devicelogin — no browser on the server.
Personal @outlook.com accounts can't register an app without an Azure
subscription; [docs/email-source.md](docs/email-source.md) covers the two ways
around that. Set `ICR_EMAIL_ENABLED=true`.

Most providers require an **app-specific password**, not your account password.

## Use

```bash
icr redeem --dry-run          # show what would be attempted, make no requests
icr redeem                    # redeem everything outstanding
icr code add ABCD-EFGH-IJKL   # add codes by hand (paste a whole message if you like)
icr code add XXXX --account main   # single-use code, for one account only
icr poll                      # ask every enabled source for new codes now
icr status                    # accounts, queue depth, recent activity
icr history --account main
icr source list               # sources, whether enabled, how far each has read
icr source reset discord      # re-scan recent messages on the next poll
icr source reset incendar     # (works for any source: fandom, email, …)
icr mailbox list              # newsletter mailboxes and their last error
icr db backup                 # consistent snapshot, safe while running
```

Chest and blacksmith actions are manual only — never scheduled, because they
spend in-game resources:

```bash
icr chest open --account main --chest gold --count 500
icr chest buy  --account main --chest gold --count 10 --yes
icr blacksmith --account main --hero-id 42 --contract large --count 100 --yes
```

## Run as a service

With Docker, `docker compose up -d` already does this. The systemd route below
is for a bare-metal install.

`icr serve` runs the scheduler and the web UI in one process.

```bash
sudo useradd --system --home-dir /opt/icr icr
sudo install -d -o icr -g icr /etc/icr /var/lib/icr /var/log/icr
sudo install -m 600 -o icr -g icr .env /etc/icr/.env
sudo cp deploy/icr.service /etc/systemd/system/
sudo systemctl enable --now icr
journalctl -u icr -f
```

The web UI is at `http://127.0.0.1:8787` by default: dashboard, manual code
entry, the redemption matrix, account management and chest actions.

## Where the database lives, and backing it up

Nothing is stored inside the container. The database is bind-mounted to `./data`
on the host by default, so it survives `docker compose down`, image rebuilds,
and container recreation — and, unlike a named volume, it is not removed by
`docker compose down -v` either:

```
./data/icr.sqlite3        the database
./data/icr.sqlite3-wal    write-ahead log   } SQLite internals --
./data/icr.sqlite3-shm    shared memory     } all three are one database
```

Point it somewhere else with `ICR_DATA_DIR`, e.g. on a mounted backup disk:

```bash
# .env next to docker-compose.yml
ICR_DATA_DIR=/srv/icr-data
```

### Directory ownership

A bind mount keeps the host directory's ownership — Docker does not chown it —
so the container runs as uid 1000 by default. Two things follow:

**If `id -u` is not 1000**, set it in your `.env`:

```bash
ICR_UID=$(id -u)   # write the actual number; compose does not run commands
ICR_GID=$(id -g)
```

**If you point `ICR_DATA_DIR` at a directory that does not exist yet**, create it
first. Docker creates a missing bind-mount source owned by `root`, and the
container then cannot write to it:

```bash
mkdir -p /srv/icr-data      # before the first `docker compose up`
```

(`./data` is committed to the repo for exactly this reason, so the default path
is already owned by whoever cloned it.)

If ownership is wrong, startup says so in one line and names the command to run,
rather than failing with SQLite's unhelpful "unable to open database file". To
recover:

```bash
sudo chown -R $(id -u):$(id -g) data
docker compose restart
```

To use a Docker-managed named volume instead, see the commented block at the
bottom of [docker-compose.yml](docker-compose.yml).

### Backups

```bash
docker compose exec icr icr db backup                    # timestamped, next to the db
docker compose exec icr icr db backup /data/before-upgrade.sqlite3
```

**Do not just copy `icr.sqlite3` while the service is running.** In WAL mode
recently committed data lives in the `-wal` sidecar, so a lone file copy can
give you a stale or torn snapshot. `icr db backup` uses SQLite's online backup
API, which is safe against a live database and produces a single consistent
file.

If you would rather copy by hand, stop the container first
(`docker compose stop`) and take all three files together.

Restoring is a file copy with the service stopped:

```bash
docker compose stop
cp backup.sqlite3 data/icr.sqlite3
rm -f data/icr.sqlite3-wal data/icr.sqlite3-shm
docker compose start
```

A nightly backup via cron on the host:

```cron
0 4 * * * cd /path/to/Headless_IdleCodeRedeemer && docker compose exec -T icr icr db backup >/dev/null
```

The database contains account credentials in plaintext — back it up somewhere
you would be comfortable keeping a password file.

## Security

- **`user_hash` is stored in plaintext in SQLite**, as are mailbox passwords and
  OAuth refresh tokens. File permissions are the mitigation: a dedicated `icr`
  user, `0600` on `.env`, `0750` on the state directory. Encrypting at rest would
  only move the problem unless the key lived elsewhere, which is not worth it on
  a single-user box. Treat the database as a credential file.
- Use an **app-specific password** for any mailbox whose provider offers one. It
  is scoped to IMAP and revocable on its own. Microsoft mailboxes get an OAuth
  token limited to `IMAP.AccessAsUser.All` — read that mailbox, nothing else, and
  no ability to send.
- Logging scrubs account hashes, mailbox credentials and API tokens from every
  record, and query strings containing `hash=`/`user_id=`/`token=` are blanked
  structurally as a second line of defence.
- The web UI binds to loopback by default. Pointing `ICR_WEB_HOST` at a
  non-loopback address without setting `ICR_WEB_AUTH_TOKEN` is refused at
  startup — it can read and write account credentials. Put it behind a reverse
  proxy with TLS if you expose it.
- **`ICR_WEB_INSECURE_BIND` exists only for containers.** A process inside
  Docker must listen on `0.0.0.0` to be reachable at all, so the compose file
  sets it and restricts exposure the layer above, publishing the port as
  `127.0.0.1:8787:8787`. The two go together. If you change the port mapping to
  `8787:8787`, set `ICR_WEB_AUTH_TOKEN` at the same time — otherwise you have
  published an unauthenticated credential store to your whole network.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run mypy
```
