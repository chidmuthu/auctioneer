"""SQLite storage for auctions. Thread ID maps to one active auction per player."""
import aiosqlite
import os
import time
from pathlib import Path

# Override with AUCTIONEER_DB_PATH (e.g. ":memory:") for tests
DB_PATH = os.environ.get("AUCTIONEER_DB_PATH") or str(Path(__file__).parent / "auctioneer.db")


async def init_db():
    """Create auctions table if it doesn't exist. Migrates existing tables to drop starting_bid if present."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                thread_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                current_bid INTEGER NOT NULL,
                current_bidder_id INTEGER,
                current_bidder_name TEXT,
                created_at INTEGER NOT NULL,
                last_bid_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        await conn.commit()
        # Migrate existing DB: if auctions has starting_bid, recreate without it
        async with conn.execute("PRAGMA table_info(auctions)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if "starting_bid" in columns:
            await conn.execute("""
                CREATE TABLE auctions_new (
                    thread_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    player_name TEXT NOT NULL,
                    current_bid INTEGER NOT NULL,
                    current_bidder_id INTEGER,
                    current_bidder_name TEXT,
                    created_at INTEGER NOT NULL,
                    last_bid_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
            """)
            await conn.execute("""
                INSERT INTO auctions_new
                SELECT thread_id, channel_id, guild_id, player_name, current_bid,
                       current_bidder_id, current_bidder_name, created_at, last_bid_at, status
                FROM auctions
            """)
            await conn.execute("DROP TABLE auctions")
            await conn.execute("ALTER TABLE auctions_new RENAME TO auctions")
            await conn.commit()
    await init_pinned_list_table()


async def create_auction(
    *,
    thread_id: int,
    channel_id: int,
    guild_id: int,
    player_name: str,
    current_bid: int,
    current_bidder_id: int,
    current_bidder_name: str,
) -> dict | None:
    """Create a new auction for a thread. First bid is stored as current_bid. Returns auction row as dict or None on conflict."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                """
                INSERT INTO auctions (
                    thread_id, channel_id, guild_id, player_name,
                    current_bid, current_bidder_id, current_bidder_name,
                    created_at, last_bid_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (thread_id, channel_id, guild_id, player_name, current_bid, current_bidder_id, current_bidder_name, now, now),
            )
            await conn.commit()
        except aiosqlite.IntegrityError:
            return None
    return await get_auction_by_thread(thread_id)


async def register_existing_auction(
    *,
    thread_id: int,
    channel_id: int,
    guild_id: int,
    player_name: str,
    current_bid: int,
    current_bidder_id: int | None,
    current_bidder_name: str,
    created_at: int,
    last_bid_at: int,
) -> dict | None:
    """
    Register an existing auction thread so the bot tracks it (reminders, completion, embeds).
    Returns auction row as dict or None if thread_id already exists.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                """
                INSERT INTO auctions (
                    thread_id, channel_id, guild_id, player_name,
                    current_bid, current_bidder_id, current_bidder_name,
                    created_at, last_bid_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    thread_id, channel_id, guild_id, player_name,
                    current_bid, current_bidder_id, current_bidder_name,
                    created_at, last_bid_at,
                ),
            )
            await conn.commit()
        except aiosqlite.IntegrityError:
            return None
    return await get_auction_by_thread(thread_id)


async def get_auction_by_thread(thread_id: int) -> dict | None:
    """Get active auction by thread ID. Returns None if not found or not active."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT thread_id, channel_id, guild_id, player_name,
                   current_bid, current_bidder_id, current_bidder_name,
                   created_at, last_bid_at, status
            FROM auctions
            WHERE thread_id = ?
            """,
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)


async def place_bid(
    thread_id: int,
    *,
    amount: int,
    bidder_id: int,
    bidder_name: str,
) -> dict | None:
    """
    Update current bid if amount > current_bid. Returns updated auction dict or None
    (auction not found, not active, or bid too low).
    """
    auction = await get_auction_by_thread(thread_id)
    if auction is None:
        return None
    if amount <= auction["current_bid"]:
        return None
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE auctions
            SET current_bid = ?, current_bidder_id = ?, current_bidder_name = ?, last_bid_at = ?
            WHERE thread_id = ? AND status = 'active'
            """,
            (amount, bidder_id, bidder_name, now, thread_id),
        )
        await conn.commit()
    return await get_auction_by_thread(thread_id)


async def get_committed_pom_for_user(user_id: int) -> int:
    """Sum of current_bid for active auctions where user is high bidder."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(current_bid), 0) FROM auctions
            WHERE status = 'active' AND current_bidder_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def get_active_auctions_for_reminders() -> list[dict]:
    """Return all active auctions with thread_id, last_bid_at, player_name, etc."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT thread_id, channel_id, guild_id, player_name,
                   current_bid, current_bidder_id, current_bidder_name,
                   created_at, last_bid_at, status
            FROM auctions
            WHERE status = 'active'
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_active_auctions_by_channel(channel_id: int) -> list[dict]:
    """Return all active auctions in a channel (for pinned list)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT thread_id, channel_id, guild_id, player_name,
                   current_bid, current_bidder_id, current_bidder_name,
                   created_at, last_bid_at, status
            FROM auctions
            WHERE channel_id = ? AND status = 'active'
            ORDER BY created_at DESC
            """,
            (channel_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def init_pinned_list_table():
    """Create pinned_list_messages and pinned_balances_messages tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pinned_list_messages (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pinned_balances_messages (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)
        await conn.commit()


async def get_pinned_list_message_id(channel_id: int) -> int | None:
    """Return stored message_id for the pinned auctions list in this channel, or None."""
    await init_pinned_list_table()
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT message_id FROM pinned_list_messages WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_pinned_list_message_id(channel_id: int, message_id: int) -> None:
    """Store or update the pinned list message_id for this channel."""
    await init_pinned_list_table()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO pinned_list_messages (channel_id, message_id) VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET message_id = excluded.message_id
            """,
            (channel_id, message_id),
        )
        await conn.commit()


async def get_pinned_balances_message_id(channel_id: int) -> int | None:
    """Return stored message_id for the pinned balances list in this channel, or None."""
    await init_pinned_list_table()
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT message_id FROM pinned_balances_messages WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_pinned_balances_message_id(channel_id: int, message_id: int) -> None:
    """Store or update the pinned balances message_id for this channel."""
    await init_pinned_list_table()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO pinned_balances_messages (channel_id, message_id) VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET message_id = excluded.message_id
            """,
            (channel_id, message_id),
        )
        await conn.commit()


async def complete_auction(thread_id: int) -> dict | None:
    """Mark auction as completed. Returns the completed auction dict or None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE auctions SET status = 'completed' WHERE thread_id = ? AND status = 'active'",
            (thread_id,),
        )
        await conn.commit()
        # Return the completed auction (now with status='completed')
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT thread_id, channel_id, guild_id, player_name,
                   current_bid, current_bidder_id, current_bidder_name,
                   created_at, last_bid_at, status
            FROM auctions
            WHERE thread_id = ?
            """,
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
