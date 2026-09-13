# RSS-to-Telegram

`rss-to-tg` is a small, self-hosted Telegram bot that watches RSS/Atom feeds and publishes new articles to Telegram channels. Feed definitions and delivery history are stored in SQLAlchemy models, so the default SQLite database can be switched to MySQL/MariaDB through one environment variable.

## Features

- Add, edit, pause, resume, list, and delete feeds from Telegram.
- One feed can be connected to multiple channels by adding it more than once.
- Per-feed polling intervals, with configurable global limits.
- Article deduplication by RSS/Atom GUID (with a stable link fallback).
- Conditional HTTP requests using ETag and Last-Modified headers.
- Readable HTML posts with a short summary, a `Read more` link, and Telegram's normal link preview.
- Failed Telegram deliveries remain retryable on the next poll.
- First-run protection: existing feed entries are recorded but not posted by default, preventing a channel flood.
- Admin allow-list, SQLite by default, MySQL/MariaDB support, and Docker files included.

## Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Add the bot to every destination channel as an administrator with permission to post messages.
3. Copy your Telegram user ID into `ADMIN_USER_IDS`. Multiple IDs may be comma-separated.
4. Copy `.env.example` to `.env`, fill in the values, and start the bot.

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
| `/addfeed URL CHANNEL [MINUTES]` | Add a feed directly |
| `/feeds` | List feeds and recent errors |
| `/editfeed ID` | Change URL, channel, interval, or enabled state interactively |
| `/togglefeed ID on\|off` | Pause or resume a feed |
| `/deletefeed ID` | Delete a feed and confirm with an inline button |
| `/stats` | Show feed and article counters |
| `/cancel` | Cancel an active add/edit prompt |

The scheduler checks for due feeds every `POLL_TICK_SECONDS`. Each feed is only fetched once its own interval has elapsed, so intervals do not need to be whole multiples of the scheduler tick.

## Configuration

`BOT_TOKEN` and `ADMIN_USER_IDS` are required. The remaining settings are optional; see `.env.example` for defaults. `POST_EXISTING_ON_FIRST_RUN=true` posts up to `MAX_ARTICLES_PER_POLL` existing entries during the first successful poll of a feed.

This starter uses `create_all` for a simple deployment. If the schema is changed for an existing production database, add a migration tool such as Alembic before deploying that change.

