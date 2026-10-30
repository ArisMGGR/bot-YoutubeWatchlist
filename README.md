# YtWatchlistBot

Watches **your YouTube subscriptions** and sends you a **Telegram** (or
**Discord**) message
whenever one of them posts a **new video or livestream** (including
scheduled/upcoming streams). YouTube Shorts are filtered out automatically. Each
message has a 💾 Save button that adds the video to a **playlist in your own
YouTube account**, and a 🗑 Delete button.

There's no channel list to maintain — the bot reads the channels you're
subscribed to on YouTube, so subscribing/unsubscribing there is all it takes to
change what you're notified about. An optional `exceptions.txt` lets you opt out
of individual channels, or of just their videos or just their livestreams.

It runs as a **single always-on process** — one program does the periodic
channel checks *and* answers the buttons and chat commands. Point it at a bot
token, connect your YouTube account, and leave it running (e.g. on a home
server / NAS / Raspberry Pi).

**Telegram or Discord?** Both backends do the same things (notifications, 💾
Save, 🗑 Delete, the same commands) and share all the checking logic — pick one
with `CHAT_PLATFORM` in `conf.env`, or just let
[`manual-setup/setup_bot.py`](#guided-setup-python-manual-setupsetup_botpy) ask you. Telegram is the
default.

## Quickstart

Two ways to set this up. Both end with the same `conf.env`, `venv/` and
`youtube_token.json` — pick whichever you prefer.

### The guided way (recommended)

One script does everything: virtualenv, dependencies, a walk-through of every
setting, the bot-token check, the YouTube connection, and (on Linux) the
systemd service.

```bash
python manual-setup/setup_bot.py        # python3 on Linux / macOS
```

It asks which chat app you want (Telegram or Discord) and installs only that
one's library. Have your **bot token** ready before you start — see
[Create your bot](#1-create-your-bot-telegram-or-discord) for how to get one on
either platform. Full walkthrough of what it asks:
[Guided setup](#guided-setup-python-manual-setupsetup_botpy).

It's safe to re-run: it reuses an existing `venv/` and won't touch a `conf.env`
you already have unless you tell it to (and keeps a `conf.env.bak` if you do).

### The manual way

Run these in order, from the project folder (the setup scripts and templates
live in `manual-setup/`; everything they create lands in the project folder).
Each step links to details below.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2a. Telegram — get a bot token from @BotFather
cp manual-setup/conf.env.example conf.env
#     then edit conf.env and set TELEGRAM_BOT_TOKEN=...

# 2b. …or Discord — create a bot in the Developer Portal and invite it
#     then edit conf.env and set:
#       CHAT_PLATFORM=discord
#       DISCORD_BOT_TOKEN=...

# 3. Connect your YouTube account (this is your watchlist + the 💾 Save button)
#    put your OAuth client as client_secret.json here, then:
python manual-setup/authorize_youtube.py        # opens a browser, writes youtube_token.json

# 4. (Optional) opt out of some subscriptions
cp manual-setup/exceptions.txt.example exceptions.txt
#    then edit exceptions.txt — mute a channel, or keep only its videos / livestreams

# 5. Start the bot (the same command for either platform)
python ytwatchlistbot.py

# 6. Telegram: open your bot and send  /start
#    Discord:  run  /start  in the channel you want notifications in
```

That's it — you'll get a "set up" message, then a notification for each new
video from the channels you're subscribed to, and 💾 Save adds it to your
YouTube playlist. To keep it running after you close the terminal, install it as
a service (see [Install it as a service](#install-it-as-a-service-recommended)).

Detailed walkthrough of each step is below:
[guided setup](#guided-setup-python-manual-setupsetup_botpy)
· [bot token](#1-create-your-bot-telegram-or-discord)
· [YouTube connection](#2-connect-your-youtube-account-your-watchlist--the--save-button)
· [opting out](#4-optional-opt-out-of-some-subscriptions)
· [running it](#running-it).


## How it works

1. **One process, always on** — `ytwatchlistbot.py` listens to Telegram (or
   `discordbot.py` to Discord) for button taps and commands, and on a timer
   (every `CHECK_INTERVAL_MINUTES`, default 30) checks your channels in the
   background. The check itself lives in `checker.py` and is identical on both
   platforms.
2. **Watchlist = your subscriptions** — once per check the bot reads the list of
   channels your connected YouTube account is subscribed to (via OAuth, the same
   login the 💾 Save button uses). Subscribe/unsubscribe on YouTube and the
   watchlist follows automatically.
3. **Opt-outs** — `exceptions.txt` lets you silence a subscribed channel
   entirely, or keep only its regular videos or only its livestreams.
4. **Discovery** — reads each watched channel's public RSS feed
   (`youtube.com/feeds/videos.xml`) to find new uploads.
5. **Archive** — `archive.json` records every video ID it has notified you
   about (or deliberately skipped) and every channel it has already
   initialised, keeping the newest 50 ids per channel. This means:
   - You never get spammed with a channel's back-catalogue.
   - A newly subscribed channel is seeded silently (you only hear about
     *future* uploads from it).
6. **Video vs. livestream** — if you provide a YouTube Data API key it labels
   each item precisely (`New video` / `LIVE now` / `Scheduled livestream` /
   `New livestream`) and includes stream start times. Without a key it falls
   back to reading the watch page, which still distinguishes live content.
7. **Shorts are skipped** — YouTube Shorts are detected (via their `/shorts/`
   URL) and silently ignored.
8. **Freshness guard** — YouTube sometimes resurfaces an old upload in a
   channel's RSS feed. Videos released more than `MAX_VIDEO_AGE_DAYS` ago
   (default 14) are skipped so you aren't pinged about weeks-old content.

## Chat commands & buttons

Every notification carries two inline buttons:

- **💾 Save** — adds the video to a playlist in **your YouTube account** (named
  `Telegram Watchlist` by default, auto-created the first time). It shows up in
  your YouTube app and on youtube.com like any other playlist. Tapping it marks
  the message `✅ Saved`; saving is idempotent — tapping twice never duplicates.
  Requires the one-time YouTube connection below.
- **🗑 Delete** — removes that message from the chat (on Telegram, bots can
  delete their own messages for up to 48 hours; on Discord the bot can always
  delete its own messages, so no extra permission is needed).

Send the bot:

- **`/start`** — a short how-to (and, if you didn't set `TELEGRAM_CHAT_ID`, this
  is how it learns where to notify you). Only the *first* chat to send it is
  registered; a `/start` from any other chat is refused, so a stranger who
  finds your bot can't redirect it. To move it yourself, set
  `TELEGRAM_CHAT_ID` (or clear `notify_chat_id` in `archive.json`) and restart.
  In a group or channel, every member reads the notifications, but the
  commands and buttons answer only the chat's admins.
- **`/check`** — check the channels right now instead of waiting for the timer.
- **`/except`** — opt a channel out, right from the chat. The bot asks for the
  channel (send a name, @handle, URL, or `UC…` id), matches it against your
  subscriptions, then shows buttons: **🔕 Mute**, **▶️ Only videos**, or
  **📺 Only livestreams**. If the name doesn't match a subscription it says so;
  `/cancel` aborts. This writes the same `exceptions.txt` you can also edit by
  hand.
- **`/authorize`** — connect (or reconnect) your YouTube account **from the
  chat**, no server access needed. The bot posts a short code and a
  `google.com/device` link; open the link on any device, enter the code, sign
  in and approve, and the bot picks the token up itself, confirms which channel
  it's connected as, and runs a check straight away. If YouTube is already
  connected it shows the current channel first and asks before replacing it;
  a ✖️ Cancel button stops the wait (the code expires on its own after ~30
  minutes). It needs `client_secret.json` on the server, like the script does,
  and uses the same device flow as `python manual-setup/authorize_youtube.py --device` — so
  the *"TVs and Limited Input devices"* note below applies here too.
- **`/help`** — how it works.

**On Discord** these are the same five commands, as slash commands, with two
differences: `/start` registers the channel you run it in (rather than a private
chat), and `/except` takes both answers in one go — pick the channel and the
rule from the command's own options —
`/except channel: Some Channel rule: 🔕 Mute the channel` — instead of Telegram's
back-and-forth. Replies to `/check`, `/help`, `/except` and `/authorize` are
ephemeral, so only you see them (and only you see the authorization code); the
"YouTube connected" confirmation is posted in the channel.

> **Why not the actual "Watch Later" list?** YouTube's API can't add to the
> special *Watch Later* or *History* playlists — Google disabled that in 2016,
> for every app. A normal playlist in the same account is the closest possible
> equivalent and works identically.

## Setup

Either run the guided script below and answer its questions, or do steps 1–4 by
hand. The script performs exactly these steps — it doesn't do anything you
couldn't do yourself.

### Guided setup: `python manual-setup/setup_bot.py`

The script walks the whole thing end to end. It only uses the Python standard
library, so you run it *before* any dependencies exist — with your system
Python:

```bash
python manual-setup/setup_bot.py            # Windows
python3 manual-setup/setup_bot.py           # Linux / macOS
```

What it does, in order:

| Step | What happens | What it asks you |
| --- | --- | --- |
| 1 | Checks Python ≥ 3.9 and that it's sitting in the project's `manual-setup/` folder | — |
| 2 | Picks the chat platform | **Telegram or Discord** |
| 3 | Creates `venv/` and installs dependencies | — (only the library for the platform you picked; `--all-backends` installs both) |
| 4 | Walks through **every** setting and writes `conf.env` | bot token (required), chat/channel id, check interval, playlist name, API key, age limit, retry/pacing, file paths |
| 5 | Verifies the token with Telegram/Discord | keep it anyway, if the platform rejects it |
| 6 | Offers the one-time YouTube connection | whether to run `manual-setup/authorize_youtube.py` now, and whether to create `exceptions.txt` |
| 7 | Offers the systemd service (Linux only) | whether to run `manual-setup/deploy/install-service.sh` with `sudo` |

Notes:

- **Have your bot token ready** — get one from
  [@BotFather](https://t.me/BotFather) (Telegram) or the
  [Developer Portal](https://discord.com/developers/applications) (Discord).
  See [Create your bot](#1-create-your-bot-telegram-or-discord).
- At step 4, **Enter accepts the default** shown in brackets and `-` clears it
  (useful on a re-run to drop a value you no longer want). Anything left at its
  default is written to `conf.env` commented out, so the file documents the
  default without pinning it.
- At step 5 the token goes only to Telegram/Discord itself, to confirm the bot's
  name back to you. A typo is caught here instead of at first run.
- Step 6 needs `client_secret.json` in place first (see
  [Connect your YouTube account](#2-connect-your-youtube-account-your-watchlist--the--save-button)).
  If it isn't there, the script tells you what to create and skips ahead — run
  `venv/bin/python manual-setup/authorize_youtube.py` once you have it.
- The venv is `venv/` on purpose: `manual-setup/deploy/install-service.sh` finds
  `venv/bin/python3` there and points the service at it automatically.

**Re-running it** is safe and is the normal way to change things later: an
existing `venv/` is reused (dependencies refreshed), and an existing `conf.env`
is shown to you — with tokens masked — and left alone unless you opt in. If you
do rewrite it, your current values become the defaults and the old file is kept
as `conf.env.bak`. Switching platforms on a re-run is a supported path: it
installs the other library and offers to flip `CHAT_PLATFORM` for you.

When it finishes it prints the exact command to start the bot, using the venv's
Python.

### 1. Create your bot (Telegram or Discord)

Pick one. The bot behaves the same either way; the only difference is where the
messages land.

> Using [the guided script](#guided-setup-python-manual-setupsetup_botpy)? You only need the
> **token** from this step — it writes `conf.env` for you, so skip the
> `conf.env` lines below.

#### Telegram
1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you — that's the only required secret.
3. In `conf.env`: `TELEGRAM_BOT_TOKEN=...` (and optionally `TELEGRAM_CHAT_ID`,
   though `/start` teaches it that by itself).

#### Discord
1. Go to the [Developer Portal](https://discord.com/developers/applications) →
   **New Application**, name it, then **Bot** → **Reset Token** and copy the
   token.
2. Invite it to your server: **OAuth2 → URL Generator**, tick the scopes `bot`
   and `applications.commands`, then the permissions **Send Messages** and
   **Embed Links**. Open the generated URL at the bottom and pick your server.
3. In `conf.env`:

   ```ini
   CHAT_PLATFORM=discord
   DISCORD_BOT_TOKEN=your-token
   # optional: makes the slash commands appear instantly instead of within an hour
   DISCORD_GUILD_ID=your-server-id
   # optional: pin the channel instead of learning it from /start
   # DISCORD_CHANNEL_ID=123456789012345678
   ```

   (`CHAT_PLATFORM` can be left out if `DISCORD_BOT_TOKEN` is the only token you
   set.)
4. Start it the same way as always — `python ytwatchlistbot.py` hands over to
   the Discord client automatically (`python discordbot.py` works too).
5. In the channel you want notifications in, run **`/start`**.

> No privileged intents are needed — the bot never reads message content, so
> there's nothing to enable in the portal's *Bot → Privileged Gateway Intents*
> section. It answers only in its notification channel (a `/start` elsewhere
> is refused unless it's in the same server and you have the *Manage Server*
> permission there — that's how you move it to a new channel), and the 💾/🗑
> buttons keep working after a restart. Other members of that channel can
> read the notifications, but `/authorize`, `/except` and the buttons need the
> same *Manage Server* permission (you have it: adding a bot to a server
> requires it).

> **Slash commands not showing up?** A global registration can take up to an
> hour to propagate. Set `DISCORD_GUILD_ID` to your server's id (right-click the
> server → *Copy Server ID*, with Developer Mode on) and restart — they appear
> immediately.

### 2. Connect your YouTube account (your watchlist + the 💾 Save button)
This step is **required**: the bot reads your subscriptions through this
connection (and Save uses it to add videos to a playlist in *your* account).
Authorize the bot once:

1. In the [Google Cloud Console](https://console.cloud.google.com/): create a
   project and **enable YouTube Data API v3**
   (*APIs & Services → Library*).
2. *APIs & Services → Credentials → Create credentials → **OAuth client ID***,
   application type **Desktop app**. Download its JSON and save it next to the
   code as **`client_secret.json`**.
3. **Publish the app:** *APIs & Services → OAuth consent screen* (in the
   newer console: *Google Auth Platform → Audience*) → **Publish app**, so
   its status is *In production*. Google's verification isn't needed for your
   own use: when you authorize, Google warns that it hasn't verified the app —
   click *Advanced → Go to … (unsafe)* to continue. **Don't leave it in
   "Testing"**: Google then expires the bot's token after 7 days, and the bot
   asks you to re-authorize every week. (If you keep it in Testing anyway, add
   your Google account under *Test users*.)
4. Run the one-time authorize step:
   ```bash
   pip install -r requirements.txt
   python manual-setup/authorize_youtube.py
   ```
   On a machine with a browser it opens one; approve access. It writes
   **`youtube_token.json`**, which the bot uses (and refreshes) from then on.

*Headless server / over SSH?* Just run it there — with no browser to open it
switches to a **copy-paste flow**: it prints a URL, you open it on any device
(laptop/phone) and approve, then paste the resulting code back into the SSH
session. (Force this mode with `python manual-setup/authorize_youtube.py --console` if it
isn't auto-detected.) Alternatively, run `manual-setup/authorize_youtube.py` on your laptop
and copy the resulting `youtube_token.json` (and `client_secret.json`) to the
server next to the code.

*No copy-paste at all:* run `python manual-setup/authorize_youtube.py --device`. It prints a
short code and a `google.com/device` URL; open it on any device, enter the code,
approve, and the SSH session **waits and grabs the token itself** — nothing to
paste back. Google may require an OAuth client of type *"TVs and Limited Input
devices"* for this; if it rejects your current one, the script tells you to
create that client and point `YOUTUBE_DEVICE_CLIENT_SECRET_FILE` at its JSON.

*No server access at all:* once the bot is running, send it **`/authorize`**
in Telegram or Discord. It runs that same device flow from the chat — code +
link, approve on any device, done — and then runs a check. Only
`client_secret.json` has to be on the server. (See
[Chat commands](#chat-commands--buttons).)

> **Which account's subscriptions?** The bot watches the subscriptions of
> whichever channel you pick at Google's "Choose a channel" screen during
> authorization — the same one saves go to. Connect the account whose
> subscriptions you want to be notified about.

> **Multiple channels / Brand Account?** A single Google login can own several
> channels. During authorization Google shows a **"Choose a channel"** screen —
> pick the exact channel you want saves to go to; the token binds to that
> channel. The script then prints `Connected as channel: <name>` so you can
> confirm. Picked the wrong one? Delete `youtube_token.json` **and**
> `youtube_state.json` and run `python manual-setup/authorize_youtube.py` again.

> **If the token later expires or is revoked**, the bot can no longer read your
> subscriptions and checks stop finding anything. It messages you — on whichever
> platform you're using — to say YouTube needs re-authorizing (repeated at most
> every 6 hours, and always in reply to a manual `/check`). Send `/authorize`
> to reconnect from the chat (or run `python manual-setup/authorize_youtube.py` on the
> server, then `/check`). If this happens every 7 days, your OAuth app is still
> in "Testing": publish it (step 3 above), then authorize once more — tokens
> issued while it was in Testing keep their 7-day limit.

### 3. (Optional) Get a YouTube Data API key
A separate, read-only key that improves video/livestream labels and adds
scheduled start times. Create an **API key** in the same project and put it in
`YOUTUBE_API_KEY`. Leave it blank to use the no-key fallback.

### 4. (Optional) Opt out of some subscriptions
By default you're notified about **every** channel you're subscribed to. To
mute some — or to keep only their videos or only their livestreams — copy the
template (`cp manual-setup/exceptions.txt.example exceptions.txt`) and add lines to your
local `exceptions.txt`. One rule per line:

```
@SomeNoisyChannel                  # mute this channel entirely
@NewsNetwork = videos              # only regular videos (skip 24/7 livestreams)
@MyFavoriteStreamer = livestreams  # only livestreams/premieres (skip uploads)
```

The channel can be an `@handle`, a channel URL, or a raw `UC...` id. Leave the
file empty (or delete it) to be notified about everything. Changes take effect
on the next check — no restart needed.

Prefer not to edit files? Use **`/except`** in the chat — Telegram asks for the
channel and then shows opt-out buttons, Discord takes both as command options.
Either way it updates `exceptions.txt` for you.

## Running it

Meant for a machine that's always on — a home server, a NAS, a Raspberry Pi.

If you used [`manual-setup/setup_bot.py`](#guided-setup-python-manual-setupsetup_botpy), it printed the
start command for you — it's the venv's Python:

```bash
./venv/bin/python ytwatchlistbot.py          # Linux / macOS
.\venv\Scripts\python.exe ytwatchlistbot.py   # Windows
```

Set it up by hand instead? Then:

```bash
pip install -r requirements.txt
cp manual-setup/conf.env.example conf.env      # then paste your bot token
python ytwatchlistbot.py
```

`ytwatchlistbot.py` is the entry point for **both** platforms — with
`CHAT_PLATFORM=discord` it hands over to the Discord client before it loads
anything Telegram-specific, so a Discord-only install (no `python-telegram-bot`)
runs fine.

Then send **`/start`** so it knows where to message you: in Telegram, open your
bot and send it; on Discord, run it in the channel you want notifications in.
The **first check** seeds the archive silently and sends a single "I'm set up"
message; from then on you only get pinged about new uploads. State
(`archive.json`, `youtube_state.json`) lives next to the code on this machine.

Make sure you've done the [YouTube connection](#2-connect-your-youtube-account-your-watchlist--the--save-button)
step first, or Save will reply that YouTube isn't connected.

### Install it as a service (recommended)
The [`manual-setup/deploy/`](manual-setup/deploy/) folder has a ready-to-use **systemd** unit, an
auto-installer, and a step-by-step guide in
[`manual-setup/deploy/README.md`](manual-setup/deploy/README.md). The quickest way — run it with `sudo`
from your normal login and it detects the user, repo path, and python for you,
then enables and starts the service:

```bash
sudo bash manual-setup/deploy/install-service.sh
```

To remove it later:

```bash
sudo bash manual-setup/deploy/uninstall-service.sh
```

(Prefer to do it by hand? The manual copy-and-edit steps are in
[`manual-setup/deploy/README.md`](manual-setup/deploy/README.md).)

> Run only **one** instance per bot token — two processes polling the same bot
> get a Telegram "Conflict" error.

### Logs

Alongside the console output, every run writes to a plain-text log file under a
`logs/` folder next to the code. There's **one file per day**, named by date
(`logs/2026-08-11.log`); all the runs/checks on that day append to it, and the
next day starts a fresh file. Each line records what happened — new videos
notified, status messages, and any errors:

```
2026-08-11 09:00:12  INFO     ytwatchlistbot  New video notified: Some Channel — A title (dQw4w9WgXcQ)
2026-08-11 09:00:13  INFO     ytwatchlistbot  Check (scheduled) done: watched 42 channel(s), sent 1, seeded 0.
```

The folder is created automatically and is gitignored. Change its location with
the `LOG_DIR` environment variable.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `CHAT_PLATFORM` | — | `telegram` (default) or `discord`. Unset → whichever token you set |
| `TELEGRAM_BOT_TOKEN` | telegram | Your Telegram bot token (or a `token.txt` file) |
| `TELEGRAM_CHAT_ID` | — | Where messages go. Unset → learned from your first `/start` (later `/start`s from other chats are refused) |
| `DISCORD_BOT_TOKEN` | discord | Your Discord bot token (or a `discord_token.txt` file) |
| `DISCORD_CHANNEL_ID` | — | Channel to post in. Unset → learned from your first `/start` |
| `DISCORD_GUILD_ID` | — | Register slash commands to this server (instant instead of ~1h) |
| `CHECK_INTERVAL_MINUTES` | — | How often to check channels (default `30`) |
| `YOUTUBE_PLAYLIST_NAME` | — | Playlist the 💾 Save button fills (default `Telegram Watchlist`) |
| `YOUTUBE_API_KEY` | — | Better live/video detection + start times |
| `EXCEPTIONS_FILE` | — | Per-channel opt-out list path (default `exceptions.txt`) |
| `ARCHIVE_FILE` | — | Archive path (default `archive.json`) |
| `LOG_DIR` | — | Folder for the daily `.log` files (default `logs/`) |
| `MAX_VIDEO_AGE_DAYS` | — | Skip videos released more than this many days ago (default `14`, `0` disables) |
| `FEED_FETCH_RETRIES` / `FEED_FETCH_BACKOFF` | — | Retries per feed on a transient error and the backoff base in seconds (defaults `3` / `1`) |
| `FEED_FETCH_PACING_SECONDS` | — | Pause between channel feeds (default `0.5`, `0` disables) |

Advanced: `YOUTUBE_CLIENT_SECRET_FILE`, `YOUTUBE_TOKEN_FILE`,
`YOUTUBE_PLAYLIST_STATE_FILE` and `YOUTUBE_DEVICE_CLIENT_SECRET_FILE` override
where the OAuth files live (defaults are next to the code). Every file/folder
setting that is given as a relative path is resolved against the code folder,
never against the directory the bot was started from.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests in `tests/` are offline: network calls and the chat platforms are
stubbed, so they run in a couple of seconds anywhere.

## License

[MIT](LICENSE).

## Files

Everything the bot needs at runtime is in the project root. Everything you only
touch while setting it up is in `manual-setup/`.

**The bot**

| File | What it is |
| --- | --- |
| `ytwatchlistbot.py` | The Telegram bot (and the entry point for either platform) |
| `discordbot.py` | The Discord bot — same features, slash commands |
| `checker.py` | The periodic check itself, shared by both bots (no chat code) |
| `discovery.py` | Reads feeds and works out what's genuinely new (no chat code) |
| `youtube.py` | Channel resolution, RSS feeds, live-vs-video classification |
| `youtube_playlist.py` | Adds 💾-saved videos to your YouTube playlist (OAuth) |
| `youtube_auth.py` | Google's device flow, shared by `manual-setup/authorize_youtube.py --device` and `/authorize` |
| `archive.py` | Reads/writes `archive.json` state |
| `config.py` | Loads settings from env / `conf.env` / `token.txt` |
| `logging_setup.py` | Writes a daily `.log` file under `logs/` (one per day) |
| `requirements.txt` | Dependencies (`setup_bot.py` installs only the chat library you pick) |
| `tests/` | Offline unit tests (`python -m pytest`) |

**`manual-setup/` — setup scripts and templates**

| File | What it is |
| --- | --- |
| `setup_bot.py` | Interactive setup: venv, dependencies, `conf.env`, OAuth, service |
| `authorize_youtube.py` | One-time script to grant YouTube access (the `/authorize` command does the same from chat) |
| `conf.env.example` | Template for `conf.env` — copy it to the project root and edit |
| `exceptions.txt.example` | Template for per-channel opt-outs — copy to `exceptions.txt` in the project root |
| `deploy/` | systemd unit template, `install-service.sh` / `uninstall-service.sh`, and a [service guide](manual-setup/deploy/README.md) |

**Your local files (all gitignored, live in the project root)**

| File | What it is |
| --- | --- |
| `conf.env` | Your settings and bot token (**secret**) |
| `exceptions.txt` | Your optional per-channel opt-outs (mute / videos-only / livestreams-only) |
| `client_secret.json` | Google OAuth client (you provide it, **secret**) |
| `youtube_token.json` | YouTube token from `manual-setup/authorize_youtube.py` (**secret**) |
| `archive.json` | Seen videos + learned notify chat/channel (runtime state) |
| `youtube_state.json` | Cached id of your saved-videos playlist |
| `venv/` | The virtualenv `setup_bot.py` creates |
| `logs/` | Daily run logs, one `.log` per day |
