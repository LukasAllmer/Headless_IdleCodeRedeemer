# The email source

Codename Entertainment mails a code to every Idle Champions newsletter
subscriber. This source reads those mails and queues the codes.

## The one thing to understand first

**A newsletter code is single-use, and it belongs to the account that received
it.** Redeeming it on a different account consumes it on the wrong one and
leaves you with nothing on the right one.

So every mailbox is tied to exactly one game account, and codes found in it are
only ever offered to that account:

```
mailbox "main-mail" ──▶ account "main"    only main sees its codes
mailbox "alt-mail"  ──▶ account "alt"     only alt sees its codes
Discord / manual    ──▶ every account     public codes, unchanged
```

`icr code list` shows this in the **For** column: `all` for a public code, an
account name for a personal one.

If you have three accounts and want codes for all three, subscribe three email
addresses and register three mailboxes.

## Setup

### 1. Subscribe

Sign the address up for the Idle Champions newsletter, once per game account.
Codes arrive from `newsletters@codenameentertainment.com`.

### 2. Register the mailbox

Two kinds are supported. Pick by provider.

#### Ordinary IMAP (Gmail, Fastmail, self-hosted, most things)

```bash
icr mailbox add \
  --name main-mail \
  --account main \
  --host imap.gmail.com \
  --username player@gmail.com
```

You will be prompted for the password. Omitting `--password` on purpose keeps it
out of your shell history; pass it explicitly only in a script.

**Almost every provider needs an app-specific password, not your login
password.** Gmail calls it an [App Password][gmail-app-pw] and requires 2FA to be
on first. If login is rejected, this is usually why.

Common hosts:

| Provider | Host | Port |
| --- | --- | --- |
| Gmail | `imap.gmail.com` | 993 |
| Fastmail | `imap.fastmail.com` | 993 |
| Proton (via Bridge) | `127.0.0.1` | 1143 |
| iCloud | `imap.mail.me.com` | 993 |

#### Microsoft (Outlook, Hotmail, Live, Office 365)

Microsoft turned off IMAP basic authentication — for work and school accounts in
2022, and for **personal** Outlook.com/Hotmail/Live accounts around October 2024.
An app password now gets an immediate `NO LOGIN`. There is no password route
left; these must use OAuth2, which needs a client id from somewhere.

Getting one has a catch that trips most people up, so read the next section
before the registration walkthrough.

##### If you cannot register an app (personal accounts)

App registration lives in Microsoft Entra ID, and **a personal Microsoft account
has no Entra directory**. Getting one means signing up for the free Azure tier,
which demands a payment method — it does not charge you, but it is a hard gate,
and it is where personal-account users usually give up.

Two ways past it, neither needing Azure:

**Use Thunderbird's public client id.** Thunderbird ships a secret-less client id
registered for exactly this scope, and the open-source mail world leans on it for
this exact problem — mutt's own `mutt_oauth2.py`, mbsync and getmail all point at
it. No registration, no card:

```bash
ICR_EMAIL_OAUTH_CLIENT_ID=9e5f94bc-e8a4-4e73-b8be-63364c29d753
```

Then add and authorize the mailbox as below; skip the registration steps.

Two things to know. The consent screen will say **"Thunderbird"**, because that is
whose identity you are borrowing — it is your mailbox, your sign-in and read-only
IMAP, but it is not your app. And Microsoft has been tightening how it classifies
third-party clients, so this may stop working; if the device-code flow is refused
you get a clear `AADSTS` error rather than a silent failure.

**Or sidestep Microsoft entirely.** The code is *mailed to* the address; where it
is stored afterwards does not matter. A forwarding rule in Outlook.com into a
mailbox that still takes an app password (Gmail, Fastmail) means `icr` never
talks to Microsoft at all. Register the destination mailbox instead. Note the
mailbox is what binds codes to a game account, so two subscriptions forwarded
into one destination cannot be told apart.

If this is a **work or school** Microsoft 365 account rather than @outlook.com,
none of this applies: registering an app in your organisation's existing tenant
is free and needs no subscription. Use the walkthrough below.

##### Registering your own app

**a. Register an app** at [portal.azure.com][azure] → *Microsoft Entra ID* → *App
registrations* → *New registration*.

- Name: anything.
- Supported account types: *Accounts in any organizational directory and
  personal Microsoft accounts* — the last part matters for @outlook.com.
- Redirect URI: leave empty.

Then, in the new app:

- *Authentication* → *Advanced settings* → **Allow public client flows: Yes**.
  Without this the device-code sign-in is refused.
- *API permissions* → *Add a permission* → *APIs my organization uses* → search
  `Office 365 Exchange Online` → *Delegated permissions* → **IMAP.AccessAsUser.All**.
- Copy the **Application (client) ID** from *Overview*.

**b. Tell `icr` about it**, in your `.env` next to `docker-compose.yml`:

```bash
ICR_EMAIL_ENABLED=true
ICR_EMAIL_OAUTH_CLIENT_ID=00000000-0000-0000-0000-000000000000
```

**c. Add and authorize the mailbox:**

```bash
icr mailbox add --name alt-mail --account alt --microsoft --username alt@outlook.com
icr mailbox authorize alt-mail
```

`authorize` prints a short code. Open `microsoft.com/devicelogin` on any device,
enter it, sign in. Nothing needs a browser on the server. The refresh token that
comes back is stored in the database and renewed automatically from then on.

### 3. Turn the source on and check it

```bash
ICR_EMAIL_ENABLED=true
```

```bash
icr mailbox test main-mail        # connects, reports what it sees, stores nothing
icr mailbox list                  # per-mailbox status and last error
icr poll                          # actually queue the codes
icr code list
```

`icr mailbox test` is the one to run when something is wrong. It uses the same
credentials and the same search the scheduled poll does, so if it works, polling
works.

## How it works

Every poll, for each enabled mailbox:

1. Refresh the OAuth2 token if it is a Microsoft mailbox.
2. List every folder and select each one that can be selected — **all folders**,
   not just the inbox, because people file the newsletter away and providers
   route it to Spam or a tab.
3. Search each folder for mail from the configured senders that arrived after the
   last message read from it.
4. Pull codes out of the plain-text body, or the HTML with tags stripped if there
   is no text part. The subject line is scanned too.
5. Insert them against that mailbox's account.

Nothing is marked as read, moved, flagged or deleted. This only ever reads.

### Read position

The cursor is a **UID per folder**, stored as JSON under one `kv` key per
mailbox. UIDs ascend within a folder and are never reused, so a poll can ask the
server for exactly what arrived since last time and a message is fetched and
parsed once rather than on every poll.

A UID only means something under the folder's `UIDVALIDITY`, so that is stored
next to it. If the server bumps it — a folder recreated, a mailbox migrated —
the position is void and that folder is re-read over the last
`ICR_EMAIL_RESCAN_DAYS` days. Codes already extracted are still on file, so only
recent mail needs covering.

Dates are only ever a fallback, for a folder with no position yet. IMAP's date
search has no time component (RFC 3501), so a date bound can do no better than
"everything from that day on" — which is why it is not used for ordinary polling.

The first poll of a new mailbox reads **everything**, which can take a minute on
a large account. Set `ICR_EMAIL_INITIAL_SCAN_DAYS` if you would rather it did
not. A folder holding more than `ICR_EMAIL_MAX_MESSAGES_PER_FOLDER` messages is
read oldest-first up to the cap and continues from there on the next poll, so a
big backlog catches up over several polls rather than stranding the older half.

A mailbox upgraded from a version that stored a single date per mailbox keeps
that date as its opening window, then switches to UIDs. It does not re-read its
history.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `ICR_EMAIL_ENABLED` | `false` | Turns the source on |
| `ICR_EMAIL_SENDERS` | `newsletters@codenameentertainment.com` | Comma-separated; empty scans everything |
| `ICR_EMAIL_INITIAL_SCAN_DAYS` | `0` | First scan depth; 0 is the whole mailbox |
| `ICR_EMAIL_RESCAN_DAYS` | `2` | Recovery window after a `UIDVALIDITY` break; not used by ordinary polls |
| `ICR_EMAIL_TIMEOUT_SECONDS` | `60` | IMAP socket timeout |
| `ICR_EMAIL_MAX_MESSAGES_PER_FOLDER` | `200` | Messages per folder per poll; the rest follows next poll |
| `ICR_EMAIL_OAUTH_CLIENT_ID` | — | Azure client id, Microsoft mailboxes only |
| `ICR_EMAIL_OAUTH_TENANT` | `common` | Azure tenant |

**Leave the sender filter alone.** The code pattern is deliberately loose and is
matched case-insensitively, so scanning every message in a mailbox turns any
twelve-character token in any newsletter into a redemption attempt. Each one
costs an API call and is recorded as `invalid` — harmless individually, tedious
by the hundred.

## Troubleshooting

Run `icr mailbox list` first; the last error is on the row.

| What you see | What it means |
| --- | --- |
| `rejected the login ... app-specific password` | The password is wrong, or it is your account password where the provider wants an app password |
| `Could not reach HOST:993` | Typo in the host, wrong port, or no route out of the container |
| `has not been authorized yet` | A Microsoft mailbox with no token — run `icr mailbox authorize NAME` |
| `AADSTS700082: The refresh token has expired` | Nobody has signed in for 90 days — run `icr mailbox authorize NAME` again |
| `AADSTS7000218` / `must contain client_assertion` | *Allow public client flows* is off in the app registration. With a borrowed client id it means that app does not permit the device-code flow — you need your own registration, or the forwarding workaround |
| `AADSTS50059: No tenant-identifying information` | The client id is wrong or empty |
| `NO LOGIN` from `outlook.office365.com` with a password | Expected — Microsoft removed basic auth. Use `--microsoft`, not `--password` |
| `no mailboxes are configured` | `ICR_EMAIL_ENABLED=true` with nothing added yet |
| Connects fine, finds nothing | The newsletter may not be in this mailbox, or the sender address has changed — try `icr mailbox test NAME --days 0` |

To re-scan a mailbox from the beginning:

```bash
icr source reset email     # clears every mailbox's cursor
icr poll
```

Codes already in the database are not re-added; this only makes the source look
again at mail it had passed.

## Security

The mailbox password or refresh token is stored **in plaintext in the SQLite
database**, the same as the game account hashes, with the same reasoning: on a
single-user box, encrypting at rest only moves the problem unless the key lives
somewhere else. Treat `icr.sqlite3` as a credential file and back it up
accordingly.

Two things follow:

- Use an app-specific password wherever the provider offers one. It is scoped to
  IMAP and revocable on its own, so it is not your account password.
- The token grants `IMAP.AccessAsUser.All` — read access to that mailbox, and
  nothing else. It cannot send.

Passwords and tokens are registered with the log redaction filter at startup, so
they are scrubbed from log records and tracebacks.

Mailboxes are CLI-only. The web UI lists them read-only and never accepts a
credential, because it is unauthenticated on loopback by default.

[gmail-app-pw]: https://support.google.com/accounts/answer/185833
[azure]: https://portal.azure.com/
