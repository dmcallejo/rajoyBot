# RSS-to-Telegram

`rss-to-tg` is a small, self-hosted Telegram bot that watches RSS/Atom feeds and publishes new articles to Telegram channels. Feed definitions and delivery history are stored in SQLAlchemy models, so the default SQLite database can be switched to MySQL/MariaDB through one environment variable.

## Features

- Each Telegram user can add, edit, pause, resume, list, and delete their own named feed-to-channel configurations.
- Users who configure the same feed URL share one poll; each new article is delivered to every enabled destination for that feed.
- The shared feed is polled at the shortest interval requested by its enabled configurations.
- Article deduplication by RSS/Atom GUID (with a stable link fallback).
- Conditional HTTP requests using ETag and Last-Modified headers.
- Readable HTML posts with a short summary, a `Read more` link, and Telegram's normal link preview.
- Failed Telegram deliveries remain retryable on the next poll.
- First-run protection: existing feed entries are recorded but not posted by default, preventing a channel flood.
- User data is scoped by Telegram user ID, and management commands work only in private chats.
- SQLite by default, MySQL/MariaDB support, and Docker files included.

## Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Add the bot to every destination channel as an administrator with permission to post messages.
3. Copy `.env.example` to `.env`, fill in the bot token, and start the bot.

Any Telegram user can create personal configurations. To use a destination channel, the user must be a channel administrator and the bot must be an administrator with permission to post. Commands and configuration details are available only in a private chat with the bot.

For public channels, use `@channel_username` when adding a feed. For private channels, use the numeric channel ID (normally beginning with `-100`).

## Run locally

```bash
cp .env.example .env
# edit .env
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
python -m rss_to_tg
```

The SQLite file is created under `data/`. Tables are created automatically on startup.

## Run with Docker

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
docker compose logs -f rss-to-tg
```

To use MariaDB instead of SQLite:

```bash
docker compose -f compose.yaml -f compose.mariadb.yaml up -d --build
```

Set `MARIADB_PASSWORD` and `MARIADB_ROOT_PASSWORD` in the shell or in `.env` before starting a real deployment. The application accepts `mysql://`, `mariadb://`, or `mysql+aiomysql://` URLs and normalizes the first two to the async driver.

## Bot commands

| Command | Purpose |
| --- | --- |
| `/start`, `/help` | Show help |
| `/addfeed` | Add a feed interactively |
| `/addfeed NAME URL CHANNEL [MINUTES]` | Add a named configuration directly |
| `/feeds` | List your configurations and recent feed errors |
| `/editfeed ID` | Change name, URL, channel, interval, or enabled state interactively |
| `/togglefeed ID on\|off` | Pause or resume a configuration |
| `/deletefeed ID` | Delete a configuration with confirmation |
| `/stats` | Show your configuration and delivery counters |
| `/cancel` | Cancel an active add/edit prompt |

The scheduler checks for due shared feeds every `POLL_TICK_SECONDS`. If several users configure the same URL, the bot makes one HTTP poll and fans new articles out to their enabled channels. The shortest enabled configuration interval controls when that shared feed is polled.

## Configuration

`BOT_TOKEN` is required. The remaining settings are optional; see `.env.example` for defaults. `POST_EXISTING_ON_FIRST_RUN=true` posts up to `MAX_ARTICLES_PER_POLL` existing entries to each active configuration during the first successful poll of a shared feed.

The application creates tables on startup and migrates existing single-user feed rows into named configurations. Back up an existing database before deploying a schema change.
