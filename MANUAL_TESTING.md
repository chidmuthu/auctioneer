# Manual testing: run the bot in a Discord test server

Step-by-step guide to set up and test Auctioneer.

---

## 1. Create a test Discord server

- In Discord, click **+** on the left → **Create a server**.
- Choose **Create My Own** → **For me and my friends** → name it (e.g. "Auctioneer test").
- Use one text channel (e.g. **#general**) for auctions.

---

## 2. Create the bot in the Developer Portal

1. Open [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** (e.g. "Auctioneer").
2. Go to **Bot** → **Add Bot**.
3. Under **Token**, copy the token (this is `DISCORD_TOKEN`).
4. Under **Privileged Gateway Intents**, enable **Server Members Intent** (required for `/discord-ids`).

---

## 3. Invite the bot to your server

1. In your application → **OAuth2** → **URL Generator**.
2. **SCOPES**: check **bot** and **applications.commands**.
3. **BOT PERMISSIONS**: check
   - Send Messages
   - Create Public Threads
   - Send Messages in Threads
   - Embed Links
   - Manage Messages (for pinning)
   - Manage Threads (for locking and removing members)
4. Copy the **Generated URL**, open it in a browser, choose your test server, and authorize.

---

## 4. Google Sheet setup

1. Create a Google Sheet.
2. **Tab: POM Balance** — add header row and columns:
   - `ID` — Discord user ID (use `/discord-ids` to get these)
   - `Name` — display name
   - `POM Balance` — starting balance (e.g. 500)

3. **Tab: Completed Auctions** — add header row (bot will append rows):
   - `player_name`, `winner_discord_id`, `winner_name`, `winning_bid`, `completed_at`

4. Create a Google Cloud service account:
   - [Google Cloud Console](https://console.cloud.google.com/) → Create project → APIs & Services → Enable **Google Sheets API**.
   - IAM & Admin → Service Accounts → Create service account → Create JSON key.
   - Download the JSON key file.

5. Share the Google Sheet with the service account email (Editor access).

---

## 5. Get your server ID

- In Discord, enable **Developer Mode**: User Settings → Advanced → Developer Mode = On.
- Right‑click your test server icon → **Copy Server ID** (this is `DISCORD_GUILD_ID`).

---

## 6. Configure and run the bot

```bash
cd auctioneer
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit **`.env`**:

```
DISCORD_TOKEN=paste_your_bot_token_here
DISCORD_GUILD_ID=paste_your_server_id_here
GOOGLE_CREDENTIALS_PATH=path/to/your-service-account-key.json
GOOGLE_SPREADSHEET_ID=your_sheet_id_from_url
```

Run:

```bash
python bot.py
```

You should see: `Auctioneer ready: Auctioneer#1234`. The bot appears as **Online** in Discord.

---

## 7. Populate the POM Balance sheet

1. In Discord, run `/discord-ids` in your channel (ephemeral — only you see it).
2. Copy the user IDs and add rows to the **POM Balance** tab in your sheet.
3. Run `/balances` to refresh the pinned balances list.

---

## 8. Test the commands

### Start an auction

1. In a **channel** (not a thread), run `/auction start`.
2. `player_name`: e.g. `Jackson Holliday`
3. `initial_bid`: e.g. `50` (stored as current bid)
4. The bot creates a thread and posts an embed. The pinned **Active Auctions** message updates.

### Place a bid

1. Open the auction thread.
2. Run `/bid 75` (higher than 50).
3. The bot posts the new high bid and notifies everyone in the thread.
4. Try `/bid 50` — error: must be higher than current bid.
5. Try bidding again as the current high bidder — error: cannot raise your own bid.
6. Try `/bid` in the main channel — error: not an active auction.

### POM validation

1. Ensure your POM Balance row has a low balance (e.g. 10).
2. Start an auction with initial bid 50 — error: not enough POM.
3. Or bid 100 when you only have 50 available — error: not enough POM.

### Refresh pinned lists

1. Run `/auctions` — updates pinned Active Auctions and posts a followup.
2. Run `/balances` — updates pinned POM Balances and posts a followup.

### Auction completion (optional)

- For quick tests, reduce `BID_EXPIRY_HOURS` in `bot.py` and `cogs/auction.py` to `0.05` (3 minutes).
- After 24h (or 3 min in test) with no new bid: thread locks, winner announced, POM deducted, Completed Auctions row added.
- Check the Google Sheet for the new Completed Auctions row and updated POM Balance.

---

## 9. Troubleshooting

| Issue | Check |
|-------|-------|
| Slash commands don't appear | Restart the bot; wait a few minutes for Discord to sync. |
| `/discord-ids` hangs | Enable **Server Members Intent** in the Developer Portal. |
| POM balance errors | Ensure user is in POM Balance sheet with correct `ID` (Discord user ID). |
| Google Sheet errors | Verify credentials path, sheet ID, and that the sheet is shared with the service account. |
| Can't pin messages | Ensure bot has **Manage Messages** permission. |
