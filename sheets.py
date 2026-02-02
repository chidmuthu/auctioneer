"""Google Sheets integration: POM balance (source of truth) and completed auctions log."""
import asyncio
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("auctioneer")

SHEET_POM_BALANCE = "POM Balance"
SHEET_COMPLETED_AUCTIONS = "Completed Auctions"

DISCORD_USER_ID_COLUMN = "ID"
POM_BALANCE_COLUMN = "POM Balance"
NAME_COLUMN = "Name"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_client():
    """Get gspread client with service account credentials."""
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH")
    if not creds_path or not os.path.isfile(creds_path):
        raise FileNotFoundError("GOOGLE_CREDENTIALS_PATH must point to a valid service account JSON file")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_spreadsheet():
    """Open the configured spreadsheet."""
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_SPREADSHEET_ID must be set")
    client = _get_client()
    return client.open_by_key(spreadsheet_id)


def get_pom_balance_sync(discord_user_id: int) -> int | None:
    """
    Get POM balance for a user from the sheet.
    Returns None if user is not in the sheet.
    """
    ss = _get_spreadsheet()
    ws = ss.worksheet(SHEET_POM_BALANCE)
    rows = ws.get_all_records()
    logger.info(f"[Sheets] POM Balance: get_all_records returned {len(rows)} rows")
    discord_id_str = str(discord_user_id)
    for idx, row in enumerate(rows):
        logger.info(f"[Sheets] POM Balance row {idx + 1}: {row}")
        row_id = str(row.get(DISCORD_USER_ID_COLUMN, "")).strip()
        logger.info(f"[Sheets] POM Balance: row_id={row_id}, discord_id_str={discord_id_str}")
        if row_id == discord_id_str:
            logger.info(f"[Sheets] POM Balance: found user {discord_user_id}, balance={row.get(POM_BALANCE_COLUMN)}")
            val = row.get(POM_BALANCE_COLUMN, 0)
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0
    logger.info(f"[Sheets] POM Balance: user {discord_user_id} not found in sheet")
    return None


def get_all_pom_balances_sync() -> list[dict]:
    """Get all rows from POM Balance sheet. Returns list of dicts with discord_user_id, display_name, pom_balance."""
    ss = _get_spreadsheet()
    ws = ss.worksheet(SHEET_POM_BALANCE)
    rows = ws.get_all_records()
    return rows


def append_completed_auction_sync(
    player_name: str,
    winner_discord_id: int | None,
    winner_name: str,
    winning_bid: int,
) -> None:
    """Append a row to the Completed Auctions sheet."""
    ss = _get_spreadsheet()
    ws = ss.worksheet(SHEET_COMPLETED_AUCTIONS)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    row = [
        player_name,
        str(winner_discord_id) if winner_discord_id else "",
        winner_name or "Unknown",
        winning_bid,
        now,
    ]
    logger.info(f"[Sheets] Completed Auctions: appending row {row} (player_name, winner_discord_id, winner_name, winning_bid, completed_at)")
    ws.append_row(row)


def deduct_pom_sync(discord_user_id: int, amount: int) -> bool:
    """
    Deduct POM from user's balance in the sheet.
    Returns False if user not found or insufficient balance.
    """
    ss = _get_spreadsheet()
    ws = ss.worksheet(SHEET_POM_BALANCE)
    rows = ws.get_all_records()
    if not rows:
        logger.info("[Sheets] POM Balance (deduct): sheet is empty")
        return False
    discord_id_str = str(discord_user_id)
    # Column position for update_cell: need 1-based col for pom_balance (gspread uses 1-based)
    column_names = list(rows[0].keys())
    pom_balance_col = column_names.index(POM_BALANCE_COLUMN) + 1 if POM_BALANCE_COLUMN in column_names else 3
    for idx, row in enumerate(rows):
        row_id = str(row.get(DISCORD_USER_ID_COLUMN, "")).strip()
        if row_id != discord_id_str:
            continue
        logger.info(f"[Sheets] POM Balance (deduct): found user at row {idx + 2}, row={row}")
        try:
            current = int(row.get(POM_BALANCE_COLUMN, 0))
        except (TypeError, ValueError):
            current = 0
        if current < amount:
            logger.warning(f"[Sheets] Insufficient POM for user {discord_user_id}: has {current}, need {amount}")
            return False
        new_balance = current - amount
        row_num = idx + 2  # 1-indexed, row 1 is header
        logger.info(f"[Sheets] POM Balance (deduct): updating row {row_num} pom_balance: {current} -> {new_balance}")
        ws.update_cell(row_num, pom_balance_col, new_balance)
        return True
    logger.info(f"[Sheets] POM Balance (deduct): user {discord_user_id} not found")
    return False


# Async wrappers to avoid blocking the event loop
async def get_pom_balance(discord_user_id: int) -> int | None:
    return await asyncio.to_thread(get_pom_balance_sync, discord_user_id)


async def get_all_pom_balances() -> list[dict]:
    return await asyncio.to_thread(get_all_pom_balances_sync)


async def append_completed_auction(
    player_name: str,
    winner_discord_id: int | None,
    winner_name: str,
    winning_bid: int,
) -> None:
    await asyncio.to_thread(
        append_completed_auction_sync,
        player_name,
        winner_discord_id,
        winner_name,
        winning_bid,
    )


async def deduct_pom(discord_user_id: int, amount: int) -> bool:
    return await asyncio.to_thread(deduct_pom_sync, discord_user_id, amount)
