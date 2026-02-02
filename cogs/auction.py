"""Slash commands: /auction start, /bid."""
import logging
import os
import time

import discord
from discord import app_commands

from db import (
    create_auction,
    get_active_auctions_by_channel,
    get_auction_by_thread,
    get_committed_pom_for_user,
    place_bid,
    register_existing_auction,
)
from sheets import get_all_pom_balances, get_pom_balance, DISCORD_USER_ID_COLUMN, NAME_COLUMN, POM_BALANCE_COLUMN

logger = logging.getLogger("auctioneer")
BID_EXPIRY_HOURS = 24
AUCTION_CHANNEL_ID = os.getenv("DISCORD_AUCTION_CHANNEL_ID")

# In-memory cache: channel_id -> message_id for our pinned list messages (best-effort; cleared on restart).
_pinned_list_message_ids: dict[int, int] = {}
_pinned_balances_message_ids: dict[int, int] = {}


async def _require_auction_channel(interaction: discord.Interaction) -> bool:
    """Return False if auction channel is set and this is not that channel (sends error)."""
    if AUCTION_CHANNEL_ID is None:
        return True
    channel_id = interaction.channel.id if interaction.channel else None
    return channel_id == AUCTION_CHANNEL_ID


async def _get_pom_availability(user_id: int) -> tuple[int, int] | None:
    """
    Returns (balance, committed) for a user, or None if user not in POM Balance sheet.
    available = balance - committed.
    """
    balance = await get_pom_balance(user_id)
    if balance is None:
        return None
    committed = await get_committed_pom_for_user(user_id)
    return (balance, committed)


def _active_auctions_embed_title() -> str:
    """Embed title we use for the pinned auctions list (for finding it in pins)."""
    return "📌 Active Auctions"


def _balances_embed_title() -> str:
    """Embed title we use for the pinned balances list (for finding it in pins)."""
    return "📌 POM Balances"


def _build_active_auctions_list(auctions: list[dict]) -> discord.Embed:
    """Build an embed listing active auction threads as links."""
    embed = discord.Embed(
        title=_active_auctions_embed_title(),
        description="Click a link to open the thread and bid with `/bid <amount>`.",
        color=discord.Color.blue(),
    )
    if not auctions:
        embed.add_field(name="—", value="No active auctions in this channel.", inline=False)
        return embed
    lines = []
    for a in auctions:
        time_left = _format_time_left(_seconds_until_expiry(a['last_bid_at']))
        lines.append(f"• <#{a['thread_id']}> (bid: **{a['current_bid']}** by {a['current_bidder_name']}) {time_left} left")
    embed.add_field(name="Prospects", value="\n".join(lines), inline=False)
    embed.set_footer(text="Use /auctions to refresh this list • For a new auction: /auction start")
    return embed


def _build_balances_embed(rows: list[dict]) -> discord.Embed:
    """Build an embed listing POM balances from the sheet."""
    embed = discord.Embed(
        title=_balances_embed_title(),
        description="From POM Balance sheet (source of truth)",
        color=discord.Color.blue(),
    )
    if not rows:
        embed.add_field(name="—", value="No rows in POM Balance sheet.", inline=False)
    else:
        lines = []
        for r in rows:
            uid = r.get(DISCORD_USER_ID_COLUMN, "?")
            name = r.get(NAME_COLUMN, "?")
            bal = r.get(POM_BALANCE_COLUMN, -1)
            lines.append(f"• <@{uid}> **{name}** — {bal} POM")
        embed.add_field(name="Balances", value="\n".join(lines) or "—", inline=False)
    embed.set_footer(text="Use /balances to refresh this list")
    return embed


async def _update_pinned_message(
    channel: discord.abc.GuildChannel,
    embed: discord.Embed,
    cache: dict[int, int],
    expected_embed_title: str,
    bot: discord.Client,
) -> bool:
    """
    Best-effort update or create a pinned message in this channel.
    Tries: (1) in-memory cached message_id, (2) search channel pins for our message by embed title, (3) create new.
    Returns True if the pinned message was updated/created.
    """
    if isinstance(channel, discord.Thread):
        return False
    # 1) Try cached message_id
    msg_id = cache.get(channel.id)
    if msg_id is not None:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return True
        except discord.NotFound:
            cache.pop(channel.id, None)
    # 2) Search pins for our message (e.g. after restart or if someone deleted and we lost cache)
    try:
        pins = await channel.pins()
        for msg in pins:
            if msg.author != bot.user:
                continue
            if msg.embeds and msg.embeds[0].title == expected_embed_title:
                await msg.edit(embed=embed)
                cache[channel.id] = msg.id
                return True
    except discord.DiscordException:
        pass
    # 3) Create new and pin
    msg = await channel.send(embed=embed)
    await msg.pin()
    cache[channel.id] = msg.id
    return True


async def _update_pinned_auctions_list(channel: discord.abc.GuildChannel, bot: discord.Client) -> discord.Embed | None:
    """Update or create the pinned 'active auctions' message in this channel."""
    auctions = await get_active_auctions_by_channel(channel.id)
    embed = _build_active_auctions_list(auctions)
    await _update_pinned_message(
        channel, embed, _pinned_list_message_ids, _active_auctions_embed_title(), bot
    )
    return embed


async def _update_pinned_balances_list(channel: discord.abc.GuildChannel, bot: discord.Client) -> discord.Embed | None:
    """Update or create the pinned 'POM balances' message in this channel."""
    try:
        rows = await get_all_pom_balances()
    except Exception as e:
        logger.exception("Failed to fetch POM balances for pinned list: %s", e)
        return None
    embed = _build_balances_embed(rows)
    await _update_pinned_message(
        channel, embed, _pinned_balances_message_ids, _balances_embed_title(), bot
    )
    return embed


def _seconds_until_expiry(last_bid_at_epoch: int) -> float:
    """Seconds until this bid expires (last_bid_at + 24h)."""
    expiry = last_bid_at_epoch + (BID_EXPIRY_HOURS * 3600)
    now = int(time.time())
    return max(0, expiry - now)


def _format_time_left(seconds: float) -> str:
    """Format seconds as 'Xh Ym' or 'Xm' or 'Expired'."""
    if seconds <= 0:
        return "Expired"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _embed_color_for_time_left(seconds_left: float) -> discord.Color:
    """Green 12+h, yellow 3–12h, red 0–3h."""
    hours = seconds_left / 3600
    if hours >= 12:
        return discord.Color.green()
    if hours >= 3:
        return discord.Color.gold()
    return discord.Color.red()


def _auction_embed(
    auction: dict,
    title: str = "Current bid",
    *,
    time_left_seconds: float | None = None,
) -> discord.Embed:
    """Build an embed for auction status with optional time left and dynamic color."""
    if time_left_seconds is None and auction.get("last_bid_at") is not None:
        time_left_seconds = _seconds_until_expiry(auction["last_bid_at"])
    if time_left_seconds is not None:
        color = _embed_color_for_time_left(time_left_seconds)
        time_str = _format_time_left(time_left_seconds)
    else:
        color = discord.Color.gold()
        time_str = f"{BID_EXPIRY_HOURS}h"
    embed = discord.Embed(
        title=title,
        description=f"**{auction['player_name']}**",
        color=color,
    )
    embed.add_field(name="Current bid", value=f"**{auction['current_bid']}**", inline=True)
    if auction.get("current_bidder_name"):
        embed.add_field(name="High bidder", value=auction["current_bidder_name"], inline=True)
    else:
        embed.add_field(name="High bidder", value="—", inline=True)
    embed.add_field(name="Time left", value=time_str, inline=True)
    embed.set_footer(text="Bid with /bid <amount> • 24h with no new bid wins")
    return embed


class AuctionCog(discord.app_commands.Group):
    """Auction commands."""

    @app_commands.command(name="start", description="Start an auction thread for a player with an initial bid")
    @app_commands.describe(
        player_name="Name of the prospect/player",
        initial_bid="First bid amount (stored as current bid)",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        player_name: str,
        initial_bid: app_commands.Range[int, 1, 1_000_000],
    ):
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("This command can only be used in a server channel.", ephemeral=True)
            return
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Start the auction in the main channel, not inside a thread. I'll create a thread for this player.",
                ephemeral=True,
            )
            return
        if not await _require_auction_channel(interaction):
            return

        # POM budget check for the starter (they become the initial bidder)
        try:
            result = await _get_pom_availability(interaction.user.id)
            if result is None:
                await interaction.response.send_message(
                    "You're not in the POM Balance sheet. Contact an admin to add you.",
                    ephemeral=True,
                )
                return
            balance, committed = result
            available = balance - committed
            if initial_bid > available:
                await interaction.response.send_message(
                    f"You don't have enough POM to start at {initial_bid}. Available: **{available}**. (balance: {balance}, committed: {committed})",
                    ephemeral=True,
                )
                return
        except Exception as e:
            logger.exception("POM balance check failed: %s", e)
            await interaction.response.send_message(
                "Could not verify your POM balance. Try again or contact an admin.",
                ephemeral=True,
            )
            return

        # Create thread from a message: send message, then create thread on it
        await interaction.response.defer(ephemeral=False)
        try:
            thread_name = player_name[:100]  # Discord thread name limit
            msg = await interaction.channel.send(
                f"Auction started for **{player_name}** — current bid: **{initial_bid}**. "
                "Bid in the thread below with `/bid <amount>`."
            )
            thread = await msg.create_thread(name=thread_name)  
        except discord.HTTPException as e:
            await interaction.followup.send(f"Could not create thread: {e}", ephemeral=True)
            return

        auction = await create_auction(
            thread_id=thread.id,
            channel_id=interaction.channel.id,
            guild_id=interaction.guild.id,
            player_name=player_name,
            current_bid=initial_bid,
            current_bidder_id=interaction.user.id,
            current_bidder_name=interaction.user.display_name,
        )
        if auction is None:
            await interaction.followup.send(
                "Thread was created but something went wrong registering the auction. Please try again or contact an admin.",
                ephemeral=True,
            )
            return

        embed = _auction_embed(auction, title=f"Auction: {player_name}")
        await thread.send(embed=embed)
        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            pass  # User may already be in thread or permission issue
        # Update pinned "active auctions" list in this channel
        try:
            await _update_pinned_auctions_list(interaction.channel, interaction.client)
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"Created auction thread for **{player_name}** — <#{thread.id}>", ephemeral=True)

    @app_commands.command(
        name="register",
        description="Register this thread as an existing auction so the bot tracks it (reminders, completion, /bid).",
    )
    @app_commands.describe(
        player_name="Name of the prospect/player",
        current_bid="Current high bid amount",
        high_bidder="Discord user who has the current high bid (must be in this server)",
        hours_remaining="Hours until this bid expires (0–24)",
    )
    async def register(
        self,
        interaction: discord.Interaction,
        player_name: str,
        current_bid: app_commands.Range[int, 1, 1_000_000],
        high_bidder: discord.Member,
        hours_remaining: app_commands.Range[float, 0, 24],
    ):
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Run `/auction register` **inside the auction thread** you want the bot to track.",
                ephemeral=True,
            )
            return
        thread = interaction.channel
        if AUCTION_CHANNEL_ID is not None and thread.parent_id != AUCTION_CHANNEL_ID:
            await interaction.response.send_message(
                f"This thread is not under the designated auction channel <#{AUCTION_CHANNEL_ID}>. Register only threads in that channel.",
                ephemeral=True,
            )
            return
        existing = await get_auction_by_thread(thread.id)
        if existing is not None:
            await interaction.response.send_message(
                "This thread is already registered as an auction. The bot is already tracking it.",
                ephemeral=True,
            )
            return
        bidder_id = high_bidder.id
        bidder_name = high_bidder.display_name
        now = int(time.time())
        # last_bid_at so that expiry = now + hours_remaining
        last_bid_at = now + int((hours_remaining - BID_EXPIRY_HOURS) * 3600)
        created_at = last_bid_at  # treat as "bid started" time for display
        auction = await register_existing_auction(
            thread_id=thread.id,
            channel_id=thread.parent_id or thread.id,
            guild_id=interaction.guild.id,
            player_name=player_name,
            current_bid=current_bid,
            current_bidder_id=bidder_id,
            current_bidder_name=bidder_name,
            created_at=created_at,
            last_bid_at=last_bid_at,
        )
        if auction is None:
            await interaction.response.send_message(
                "This thread could not be registered (it may already exist in the database).",
                ephemeral=True,
            )
            return
        embed = _auction_embed(auction, title=f"Auction: {player_name} (registered)")
        await interaction.response.send_message(
            content="This thread is now registered. The bot will track reminders, expiry, and `/bid` here.",
            embed=embed,
            ephemeral=False,
        )
        parent = thread.parent
        if parent:
            try:
                await _update_pinned_auctions_list(parent, interaction.client)
            except discord.HTTPException:
                pass


async def bid_command(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000]):
    """Place a bid. Only valid inside an auction thread; bid must be higher than current bid."""
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    thread_id = interaction.channel.id
    auction = await get_auction_by_thread(thread_id)
    if auction is None:
        await interaction.response.send_message(
            "This thread is not an active auction. Use `/bid` only inside a thread created by `/auction start`.",
            ephemeral=True,
        )
        return
    if auction["status"] == "completed":
        await interaction.response.send_message(
            f"This auction is completed and was won by {auction['current_bidder_name']} for {auction['current_bid']}. Use `/bid` only inside an active auction thread.",
            ephemeral=True,
        )
        return
    if AUCTION_CHANNEL_ID is not None and auction["channel_id"] != AUCTION_CHANNEL_ID:
        await interaction.response.send_message(
            f"This auction is not in the designated auction channel <#{AUCTION_CHANNEL_ID}>. Bidding is only allowed in threads under that channel.",
            ephemeral=True,
        )
        return
    if amount <= auction["current_bid"]:
        await interaction.response.send_message(
            f"Your bid **{amount}** must be **higher** than the current bid of **{auction['current_bid']}**.",
            ephemeral=True,
        )
        return
    if auction.get("current_bidder_id") == interaction.user.id:
        await interaction.response.send_message(
            "You can't raise your own bid. Wait for someone else to outbid you.",
            ephemeral=True,
        )
        return

    # POM budget check (Google Sheet is source of truth)
    try:
        result = await _get_pom_availability(interaction.user.id)
        if result is None:
            await interaction.response.send_message(
                "You're not in the POM Balance sheet. Contact an admin to add you.",
                ephemeral=True,
            )
            return
        balance, committed = result
        available = balance - committed
        if amount > available:
            await interaction.response.send_message(
                f"You don't have enough POM. Available: **{available}** (balance: {balance}, committed to other bids: {committed}).",
                ephemeral=True,
            )
            return
    except Exception as e:
        logger.exception("POM balance check failed: %s", e)
        await interaction.response.send_message(
            "Could not verify your POM balance. Try again or contact an admin.",
            ephemeral=True,
        )
        return

    updated = await place_bid(
        thread_id,
        amount=amount,
        bidder_id=interaction.user.id,
        bidder_name=interaction.user.display_name,
    )
    if updated is None:
        await interaction.response.send_message("Could not update bid. Try again.", ephemeral=True)
        return

    try:
        await interaction.channel.add_user(interaction.user)
    except discord.HTTPException:
        pass  # User may already be in thread
    # Notify everyone in the thread with a mention so they get a notification
    embed = _auction_embed(updated, title=f"New high bid: {amount}")
    await interaction.response.send_message(
        content=f"New bid from {interaction.user.mention}!",
        embed=embed,
    )


async def _require_main_channel(interaction: discord.Interaction) -> bool:
    """Return False if not guild/channel or in thread (sends error)."""
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("This command can only be used in a server channel.", ephemeral=True)
        return False
    if isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message(
            "Use this command in the main channel (where auction threads live), not inside a thread.",
            ephemeral=True,
        )
        return False
    return True


async def auctions_command(interaction: discord.Interaction):
    """Post the active auctions list and refresh the pinned message."""
    if not await _require_main_channel(interaction):
        return
    if not await _require_auction_channel(interaction):
        return
    await interaction.response.defer(ephemeral=False)
    try:
        embed = await _update_pinned_auctions_list(interaction.channel, interaction.client)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Could not update pinned list: {e}", ephemeral=True)
        return
    await interaction.followup.send(
        content="**Active auctions** — list updated. See pinned message above for quick links.",
        embed=embed,
    )


async def balances_command(interaction: discord.Interaction):
    """Show POM balances from the Google Sheet and refresh the pinned message."""
    if not await _require_main_channel(interaction):
        return
    if not await _require_auction_channel(interaction):
        return
    await interaction.response.defer(ephemeral=False)
    try:
        embed = await _update_pinned_balances_list(interaction.channel, interaction.client)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Could not update pinned list: {e}", ephemeral=True)
        return
    await interaction.followup.send(
        content="**POM balances** — list updated. See pinned message above for quick reference.",
        embed=embed,
    )


async def discord_ids_command(interaction: discord.Interaction):
    """List Discord user IDs for all server members (for adding to POM Balance sheet)."""
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    members = [m for m in interaction.guild.members if not m.bot]
    if len(members) == 0:
        await interaction.followup.send("No members found.", ephemeral=True)
        return
    members.sort(key=lambda m: m.display_name.lower())
    lines = []
    for m in members:
        lines.append(f"`{m.id}` — {m.display_name}")
    embed = discord.Embed(
        title="Discord User IDs",
        description="Copy these for the POM Balance sheet (discord_user_id column)",
        color=discord.Color.blue(),
    )
    if not lines:
        embed.add_field(name="—", value="No members found.", inline=False)
    else:
        embed.add_field(name="ID — Display Name", value="\n".join(lines[:50]), inline=False)
        if len(lines) > 50:
            embed.set_footer(text=f"Showing first 50 of {len(lines)} members")
    logger.info("[discord-ids] Sending response with %d lines", len(lines))
    await interaction.followup.send(embed=embed, ephemeral=True)
    logger.info("[discord-ids] Done")


async def setup(bot: discord.Client):
    """Register auction commands on the bot."""
    bot.tree.add_command(AuctionCog(name="auction"))
    bot.tree.add_command(
        app_commands.Command(name="bid", description="Place a bid (use in an auction thread)", callback=bid_command)
    )
    bot.tree.add_command(
        app_commands.Command(
            name="auctions",
            description="Show active auction threads and refresh the pinned list in this channel",
            callback=auctions_command,
        )
    )
    bot.tree.add_command(
        app_commands.Command(
            name="balances",
            description="Show POM balances from the Google Sheet",
            callback=balances_command,
        )
    )
    bot.tree.add_command(
        app_commands.Command(
            name="discord-ids",
            description="List Discord user IDs for all server members (for POM Balance sheet)",
            callback=discord_ids_command,
        )
    )
