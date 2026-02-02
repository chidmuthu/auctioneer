# Auctioneer

Discord bot for prospect bidding in fantasy leagues. Start auction threads per player, place bids with POM budget validation, and track results in a Google Sheet.

## Features

- **Auction threads** — Start an auction with `/auction start`; each player gets a dedicated thread
- **Register existing threads** — Use `/auction register` inside a manually created thread so the bot tracks it (reminders, completion, `/bid`)
- **POM budget validation** — Bids checked against Google Sheet balances (source of truth)
- **24-hour expiry** — Auction wins when no new bid for 24 hours; thread locks and winner is announced
- **Pinned lists** — Active auctions and POM balances pinned in the channel for quick reference
- **Google Sheets** — Completed auctions logged; winner POM deducted automatically

## Requirements

- Python 3.10+
- Discord Bot with **Server Members Intent**
- Google Cloud service account with Sheets API
- Google Sheet with POM Balance and Completed Auctions tabs

## Setup

### 1. Discord application

1. Create a [Discord application](https://discord.com/developers/applications) and add a Bot. Copy the token.
2. Under **Privileged Gateway Intents**, enable **Server Members Intent** (for `/discord-ids`).
3. Invite the bot: OAuth2 → URL Generator → scopes: `bot`, `applications.commands`.
4. Bot permissions: **Send Messages**, **Create Public Threads**, **Send Messages in Threads**, **Embed Links**, **Manage Messages**, **Manage Threads**.

### 2. Google Sheet

1. Create a Google Sheet with two tabs.

2. **Tab: POM Balance** — columns (header row):
   - `ID` — Discord user ID (use `/discord-ids` to get these)
   - `Name` — display name
   - `POM Balance` — starting balance (e.g. 500)

3. **Tab: Completed Auctions** — columns (header row; bot appends rows):
   - `player_name`, `winner_discord_id`, `winner_name`, `winning_bid`, `completed_at`

4. Create a Google Cloud service account, enable Sheets API, create JSON key.
5. Share the Google Sheet with the service account email (Editor access).

### 3. Environment and run

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
```

Edit **`.env`**: `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `GOOGLE_CREDENTIALS_PATH`, `GOOGLE_SPREADSHEET_ID`. Optionally set `DISCORD_AUCTION_CHANNEL_ID` to restrict auction commands to one channel.

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/auction start` | Start an auction thread (in a channel). Requires POM balance in sheet. |
| `/auction register` | Register an existing thread as an auction (run inside the thread). Provide player name, current bid, high bidder, hours remaining. |
| `/bid <amount>` | Place a bid (in an auction thread). Must beat current bid; POM validated. You cannot raise your own bid. |
| `/auctions` | Rotate the pinned auctions list (unpin old, post new at bottom, pin). Ephemeral confirmation. |
| `/balances` | Rotate the pinned balances list. Ephemeral confirmation. |
| `/discord-ids` | List Discord user IDs for all server members (for POM Balance sheet). Ephemeral. |

## Behavior

- **Pinned messages** — `/auctions` and `/balances` rotate the pin when run (unpin old, send new at bottom, pin it). A background task also edits the pinned message in place to keep time-left and balances current. Auction start/register/complete update the lists as well.
- **Where to run** — `/auctions` and `/balances` in the main channel; `/auction register` and `/bid` inside a thread.
- **Reminders** — 6h and 1h before expiry, the bot posts warnings in the auction thread.
- **Embed updates** — Time left and color (green → yellow → red) update periodically.
- **Completion** — After 24h with no new bid: thread locked, winner announced, POM deducted, results appended to sheet, members removed from thread.
- **Unknown users** — Users not in the POM Balance sheet cannot bid or start auctions.

## Data

- **SQLite** (`auctioneer.db`): active auctions, pinned message IDs.
- **Google Sheet** (source of truth): POM Balance (budgets), Completed Auctions (results). Balances can be edited manually.

For step-by-step manual testing, see **MANUAL_TESTING.md**.
