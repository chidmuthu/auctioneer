"""Auctioneer Discord bot — start auction threads and place bids."""
import asyncio
import logging
import os
import time

import discord
from discord.ext.commands import Bot
from dotenv import load_dotenv

from db import (
    complete_auction,
    get_active_auctions_for_reminders,
    init_db,
)
from cogs import auction
from cogs.auction import _update_pinned_auctions_list, _seconds_until_expiry
from sheets import append_completed_auction, deduct_pom

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("auctioneer")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

BID_EXPIRY_HOURS = 24
REMINDER_CHECK_INTERVAL_SEC = 300  # 5 minutes
COMPLETION_CHECK_INTERVAL_SEC = 60  # 1 minutes
EMBED_UPDATE_INTERVAL_SEC = 60  # 1 minutes - update embed time left

# Configurable reminder thresholds (hours before expiry)
REMINDER_THRESHOLDS = [6, 1]  # Send reminders at 6h and 1h remaining

# In-memory tracking: {(thread_id, last_bid_at): set_of_thresholds_sent}
# When a new bid is placed (last_bid_at changes), the key changes so reminders reset
_reminders_sent: dict[tuple[int, int], set[float]] = {}



async def _check_expiry_reminders(bot: Bot) -> None:
    """Post expiry warnings in auction threads at configurable thresholds; run in a loop."""
    await bot.wait_until_ready()
    logger.info("[Reminders] Background task started")
    while not bot.is_closed():
        try:
            auctions = await get_active_auctions_for_reminders()
            logger.debug(f"[Reminders] Checking {len(auctions)} active auctions")
            for a in auctions:
                thread_id = a["thread_id"]
                last_bid_at = a["last_bid_at"]
                player_name = a["player_name"]
                seconds_left = _seconds_until_expiry(last_bid_at)
                hours_left = seconds_left / 3600
                
                # Key: (thread_id, last_bid_at) — changes when new bid placed
                key = (thread_id, last_bid_at)
                sent_for_this_bid = _reminders_sent.setdefault(key, set())
                
                thread = bot.get_channel(thread_id)
                if thread is None:
                    try:
                        thread = await bot.fetch_channel(thread_id)
                    except discord.HTTPException:
                        continue
                if not isinstance(thread, discord.Thread):
                    continue
                
                # Check each threshold (e.g. 6h, 1h)
                for threshold_hours in REMINDER_THRESHOLDS:
                    if threshold_hours in sent_for_this_bid:
                        continue  # Already sent for this bid
                    # Send if we're in the window: (threshold - 0.5h) < hours_left <= threshold
                    lower = threshold_hours - 0.5
                    if lower < hours_left <= threshold_hours:
                        try:
                            if threshold_hours >= 2:
                                msg = (
                                    f"⏰ **{player_name}** — about **{int(threshold_hours)} hours** "
                                    f"left on this bid. No new bid in 24h wins!"
                                )
                            else:
                                msg = (
                                    f"⏰ **{player_name}** — about **{int(threshold_hours)} hour** "
                                    f"left on this bid! Last chance to outbid."
                                )
                            await thread.send(msg)
                            sent_for_this_bid.add(threshold_hours)
                            logger.info(f"[Reminders] Sent {int(threshold_hours)}h reminder for '{player_name}'")
                        except discord.HTTPException as e:
                            logger.warning(f"[Reminders] Failed to send reminder for '{player_name}': {e}")
        except Exception as e:
            logger.exception("[Reminders] Check failed: %s", e)
        await asyncio.sleep(REMINDER_CHECK_INTERVAL_SEC)


async def _check_auction_completions(bot: Bot) -> None:
    """Check for expired auctions (24h with no new bid), lock threads, announce winners; run in a loop."""
    await bot.wait_until_ready()
    logger.info("[Completions] Background task started")
    while not bot.is_closed():
        try:
            auctions = await get_active_auctions_for_reminders()
            logger.debug(f"[Completions] Checking {len(auctions)} active auctions")
            for a in auctions:
                thread_id = a["thread_id"]
                channel_id = a["channel_id"]
                last_bid_at = a["last_bid_at"]
                player_name = a["player_name"]
                current_bid = a["current_bid"]
                current_bidder_id = a.get("current_bidder_id")
                current_bidder_name = a.get("current_bidder_name")
                seconds_left = _seconds_until_expiry(last_bid_at)
                
                # If expired (0 seconds left), complete the auction
                if seconds_left <= 0:
                    # Mark as completed in DB
                    completed = await complete_auction(thread_id)
                    if completed is None:
                        continue
                    
                    # Get thread and lock it
                    thread = bot.get_channel(thread_id)
                    if thread is None:
                        try:
                            thread = await bot.fetch_channel(thread_id)
                        except discord.HTTPException:
                            continue
                    if not isinstance(thread, discord.Thread):
                        continue
                    
                    
                    
                    # Post winner announcement in the thread
                    try:
                        winner_mention = f"{current_bidder_name}" if current_bidder_name else "Unknown"
                        await thread.send(
                            f"🎉 **Auction complete!** {winner_mention} wins **{player_name}** for **{current_bid}**. "
                            "This thread is now archived and locked."
                        )
                    except discord.HTTPException:
                        pass

                    # Lock the thread (archive + lock)
                    try:
                        await thread.edit(archived=True, locked=True)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not lock thread {thread_id}: {e}")
                    
                    # Post announcement in the main channel
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        try:
                            channel = await bot.fetch_channel(channel_id)
                        except discord.HTTPException:
                            continue
                    try:
                        winner_mention = f"<@{current_bidder_id}>" if current_bidder_id else current_bidder_name or "Unknown"
                        await channel.send(
                            f"🏆 **Auction {thread.name} closed:** {winner_mention} wins for **{current_bid}**!"
                        )
                    except discord.HTTPException:
                        pass
                    
                    # Update Google Sheets: append result and deduct POM from winner
                    try:
                        await append_completed_auction(
                            player_name=player_name,
                            winner_discord_id=current_bidder_id,
                            winner_name=current_bidder_name,
                            winning_bid=current_bid,
                        )
                        if current_bidder_id:
                            ok = await deduct_pom(current_bidder_id, current_bid)
                            if not ok:
                                logger.warning(
                                    f"[Completions] Could not deduct POM for winner {current_bidder_name} "
                                    f"(user {current_bidder_id}) - update sheet manually"
                                )
                                try:
                                    winner_mention = f"<@{current_bidder_id}>" if current_bidder_id else current_bidder_name or "Unknown"
                                    await channel.send(
                                        f"⚠️ **POM deduction failed** — could not deduct {current_bid} from {winner_mention}'s balance. "
                                        "Please update the POM Balance sheet manually."
                                    )
                                except discord.HTTPException:
                                    pass
                    except Exception as e:
                        logger.exception(f"[Completions] Google Sheets update failed: {e}")

                    # Update the pinned active auctions list (remove this completed auction)
                    try:
                        await _update_pinned_auctions_list(channel, bot)
                    except Exception as e:
                        logger.warning(f"[Completions] Could not update pinned list: {e}")
                    
                    logger.info(f"[Completions] Auction complete: '{player_name}' won by {current_bidder_name} for {current_bid}")
        except Exception as e:
            logger.exception("[Completions] Check failed: %s", e)
        await asyncio.sleep(COMPLETION_CHECK_INTERVAL_SEC)


async def _update_auction_embeds(bot: Bot) -> None:
    """Periodically update the 'time left' in auction thread embeds; run in a loop."""
    await bot.wait_until_ready()
    logger.info("[EmbedUpdater] Background task started")
    while not bot.is_closed():
        try:
            from cogs.auction import _auction_embed
            auctions = await get_active_auctions_for_reminders()
            logger.debug(f"[EmbedUpdater] Updating embeds for {len(auctions)} active auctions")
            updated_count = 0
            for a in auctions:
                thread_id = a["thread_id"]
                player_name = a["player_name"]
                thread = bot.get_channel(thread_id)
                if thread is None:
                    try:
                        thread = await bot.fetch_channel(thread_id)
                    except discord.HTTPException:
                        continue
                if not isinstance(thread, discord.Thread):
                    continue
                # Find the last message from the bot with an embed
                try:
                    async for msg in thread.history(limit=20):
                        if msg.author.id == bot.user.id and msg.embeds:
                            embed = _auction_embed(a)
                            await msg.edit(embed=embed)
                            updated_count += 1
                            break
                except discord.HTTPException as e:
                    logger.warning(f"[EmbedUpdater] Failed to update embed for '{player_name}': {e}")
            if updated_count > 0:
                logger.info(f"[EmbedUpdater] Updated {updated_count} embed(s)")
        except Exception as e:
            logger.exception("[EmbedUpdater] Check failed: %s", e)
        await asyncio.sleep(EMBED_UPDATE_INTERVAL_SEC)


# Bot has .tree for slash commands
# members intent needed for /discord-ids to list server members
intents = discord.Intents.default()
intents.members = True
bot = Bot(intents=intents, command_prefix="/")


@bot.event
async def on_ready():
    await init_db()
    await auction.setup(bot)
    guild = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
    if guild:
        bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    bot.loop.create_task(_check_expiry_reminders(bot))
    bot.loop.create_task(_check_auction_completions(bot))
    bot.loop.create_task(_update_auction_embeds(bot))
    logger.info("Auctioneer ready: %s", bot.user)


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
