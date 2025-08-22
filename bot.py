import os
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# ----------------- Config & Logging -----------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
HYPIXEL_API_KEY = os.getenv("HYPIXEL_API_KEY")
POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "30")))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TRACKING_FILE = os.path.join(DATA_DIR, "tracking.json")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("hypixel-tracker")


# ----------------- Storage -----------------
class JsonStorage:
    def __init__(self, file_path: str):
        self._file_path = file_path
        self._lock = asyncio.Lock()
        # Internal structure
        # {
        #   "accounts": {
        #       uuid: {
        #           "ign": str,
        #           "last_login_ms": Optional[int],
        #           "watchers": [ {"guild_id": int, "channel_id": int, "user_id": int} ]
        #       }
        #   },
        #   "ign_to_uuid": { ign_lower: uuid }
        # }
        self._data: Dict[str, Any] = {"accounts": {}, "ign_to_uuid": {}}

    async def load(self) -> None:
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        if os.path.exists(self._file_path):
            try:
                with open(self._file_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error("Failed to load storage: %s", e)
                self._data = {"accounts": {}, "ign_to_uuid": {}}
        else:
            await self._save_locked()

    async def _save_locked(self) -> None:
        # assumes caller holds lock
        tmp_path = self._file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._file_path)

    async def save(self) -> None:
        async with self._lock:
            await self._save_locked()

    async def add_watcher(self, uuid: str, ign: str, guild_id: int, channel_id: int, user_id: int) -> Tuple[bool, int]:
        async with self._lock:
            accounts = self._data.setdefault("accounts", {})
            ign_to_uuid = self._data.setdefault("ign_to_uuid", {})
            ign_lower = ign.lower()
            ign_to_uuid[ign_lower] = uuid

            if uuid not in accounts:
                accounts[uuid] = {
                    "ign": ign,
                    "last_login_ms": None,
                    "watchers": [],
                }
            else:
                # keep latest ign casing if provided
                accounts[uuid]["ign"] = ign

            watcher = {"guild_id": guild_id, "channel_id": channel_id, "user_id": user_id}
            if watcher not in accounts[uuid]["watchers"]:
                accounts[uuid]["watchers"].append(watcher)
                await self._save_locked()
                return True, len(accounts[uuid]["watchers"])  # added
            else:
                return False, len(accounts[uuid]["watchers"])  # already present

    async def remove_watcher(self, uuid: str, guild_id: int, channel_id: int, user_id: int) -> bool:
        async with self._lock:
            accounts = self._data.get("accounts", {})
            if uuid not in accounts:
                return False
            watchers: List[Dict[str, int]] = accounts[uuid].get("watchers", [])
            before = len(watchers)
            watchers = [w for w in watchers if not (w["guild_id"] == guild_id and w["channel_id"] == channel_id and w["user_id"] == user_id)]
            accounts[uuid]["watchers"] = watchers
            removed = len(watchers) != before
            if removed and len(watchers) == 0:
                # cleanup when no watchers left
                accounts.pop(uuid, None)
                # also cleanup ign mappings referencing this uuid
                igns_to_remove = [ign for ign, u in self._data.get("ign_to_uuid", {}).items() if u == uuid]
                for ign_key in igns_to_remove:
                    self._data["ign_to_uuid"].pop(ign_key, None)
            if removed:
                await self._save_locked()
            return removed

    async def remove_all_for_user(self, guild_id: int, channel_id: int, user_id: int) -> int:
        async with self._lock:
            accounts = self._data.get("accounts", {})
            uuids_to_delete: List[str] = []
            total_removed = 0
            for uuid, entry in accounts.items():
                watchers: List[Dict[str, int]] = entry.get("watchers", [])
                before = len(watchers)
                watchers = [w for w in watchers if not (w["guild_id"] == guild_id and w["channel_id"] == channel_id and w["user_id"] == user_id)]
                removed_here = before - len(watchers)
                if removed_here > 0:
                    total_removed += removed_here
                    entry["watchers"] = watchers
                if len(entry["watchers"]) == 0:
                    uuids_to_delete.append(uuid)
            for uuid in uuids_to_delete:
                accounts.pop(uuid, None)
                igns_to_remove = [ign for ign, u in self._data.get("ign_to_uuid", {}).items() if u == uuid]
                for ign_key in igns_to_remove:
                    self._data["ign_to_uuid"].pop(ign_key, None)
            if total_removed > 0 or uuids_to_delete:
                await self._save_locked()
            return total_removed

    async def set_last_login(self, uuid: str, last_login_ms: Optional[int]) -> None:
        async with self._lock:
            if uuid in self._data.get("accounts", {}):
                self._data["accounts"][uuid]["last_login_ms"] = last_login_ms
                await self._save_locked()

    async def get_last_login(self, uuid: str) -> Optional[int]:
        async with self._lock:
            entry = self._data.get("accounts", {}).get(uuid)
            return None if entry is None else entry.get("last_login_ms")

    async def get_accounts(self) -> Dict[str, Dict[str, Any]]:
        async with self._lock:
            # shallow copy is fine
            return dict(self._data.get("accounts", {}))

    async def get_uuid_by_ign(self, ign: str) -> Optional[str]:
        async with self._lock:
            return self._data.get("ign_to_uuid", {}).get(ign.lower())


# ----------------- Hypixel Client -----------------
class HypixelClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key
        self._user_agent = os.getenv("HTTP_USER_AGENT", "discord-hypixel-tracker/1.0")

    @staticmethod
    def _strip_uuid_dashes(uuid_str: str) -> str:
        return uuid_str.replace("-", "").lower()

    async def get_uuid_for_ign(self, ign: str) -> Optional[str]:
        # Try Mojang API to resolve IGN -> UUID
        url = f"https://api.mojang.com/users/profiles/minecraft/{ign}"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
            async with self._session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_id = data.get("id")  # no dashes
                    if isinstance(raw_id, str) and len(raw_id) == 32:
                        return raw_id.lower()
                elif resp.status == 204 or resp.status == 404:
                    return None
        except Exception as e:
            logger.warning("Mojang API resolve failed for %s: %s", ign, e)

        # Fallback: playerdb.co
        try:
            alt = f"https://playerdb.co/api/player/minecraft/{ign}"
            timeout = aiohttp.ClientTimeout(total=10)
            headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
            async with self._session.get(alt, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success") and data.get("data", {}).get("player", {}).get("id"):
                        return self._strip_uuid_dashes(data["data"]["player"]["id"])
        except Exception as e:
            logger.warning("Fallback UUID resolve failed for %s: %s", ign, e)
        return None

    async def get_last_login_ms(self, uuid_no_dashes: str) -> Optional[int]:
        url = f"https://api.hypixel.net/v2/player?uuid={uuid_no_dashes}"
        headers = {"API-Key": self._api_key, "User-Agent": self._user_agent, "Accept": "application/json"}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 403:
                    # Retry using query param auth as a fallback
                    try:
                        body = await resp.text()
                        logger.warning("Hypixel API 403 via header auth: %s", body[:200])
                    except Exception:
                        pass
                    alt_url = f"https://api.hypixel.net/v2/player?uuid={uuid_no_dashes}&key={self._api_key}"
                    alt_headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
                    async with self._session.get(alt_url, headers=alt_headers, timeout=timeout) as resp2:
                        if resp2.status != 200:
                            logger.warning("Hypixel API fallback returned status %s", resp2.status)
                            return None
                        data = await resp2.json()
                        if not data.get("success", True):
                            logger.warning("Hypixel API fallback error: %s", data)
                            return None
                        player = data.get("player")
                        if not player:
                            return None
                        last_login = player.get("lastLogin")
                        if isinstance(last_login, int):
                            return last_login
                        return None
                if resp.status != 200:
                    logger.warning("Hypixel API returned status %s", resp.status)
                    return None
                data = await resp.json()
                if not data.get("success", True):
                    logger.warning("Hypixel API returned error: %s", data)
                    return None
                player = data.get("player")
                if not player:
                    return None
                # lastLogin is epoch ms according to Hypixel API
                last_login = player.get("lastLogin")
                if isinstance(last_login, int):
                    return last_login
                return None
        except Exception as e:
            logger.warning("Hypixel API request failed: %s", e)
            return None

    async def get_key_info(self) -> Tuple[bool, str]:
        url = "https://api.hypixel.net/v2/key"
        headers = {"API-Key": self._api_key, "User-Agent": self._user_agent, "Accept": "application/json"}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 403:
                    return False, "Forbidden by Hypixel API. The API key is invalid/disabled, or the IP is blocked."
                if resp.status != 200:
                    return False, f"Unexpected status {resp.status}"
                data = await resp.json()
                if not data.get("success", True):
                    return False, "Key check failed."
                record = data.get("record") or {}
                owner = record.get("owner") or record.get("ownerUuid") or "unknown"
                return True, f"Key is valid. Owner: {owner}"
        except Exception as e:
            return False, f"Key check request failed: {e}"


# ----------------- Tracker -----------------
@dataclass
class Notification:
    guild_id: int
    channel_id: int
    user_id: int
    ign: str
    uuid: str
    last_login_ms: int


class LoginTracker:
    def __init__(self, storage: JsonStorage, hypixel_client: HypixelClient):
        self._storage = storage
        self._hypixel = hypixel_client
        self._poll_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(10)

    async def start(self):
        self._stop_event.clear()
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._stop_event.set()
        if self._poll_task:
            await self._poll_task

    async def track(self, ign: str, guild_id: int, channel_id: int, user_id: int) -> Tuple[bool, str]:
        uuid = await self._hypixel.get_uuid_for_ign(ign)
        if not uuid:
            return False, "Minecraft IGN not found."
        # Normalize IGN by fetching latest from Mojang? We'll keep provided IGN casing.
        added, count = await self._storage.add_watcher(uuid, ign, guild_id, channel_id, user_id)
        if added:
            # initialize last login if unknown to avoid immediate false-positive
            if await self._storage.get_last_login(uuid) is None:
                current = await self._hypixel.get_last_login_ms(uuid)
                await self._storage.set_last_login(uuid, current)
            return True, f"Tracking {ign} (watchers: {count})."
        else:
            return True, f"Already tracking {ign}."

    async def untrack(self, ign: str, guild_id: int, channel_id: int, user_id: int) -> Tuple[bool, str]:
        uuid = await self._storage.get_uuid_by_ign(ign)
        if not uuid:
            # Try resolving live just in case
            uuid = await self._hypixel.get_uuid_for_ign(ign)
            if not uuid:
                return False, "IGN not found in tracking."
        removed = await self._storage.remove_watcher(uuid, guild_id, channel_id, user_id)
        if removed:
            return True, f"Stopped tracking {ign}."
        else:
            return False, f"You were not tracking {ign} in this channel."

    async def untrack_all_for_user(self, guild_id: int, channel_id: int, user_id: int) -> int:
        return await self._storage.remove_all_for_user(guild_id, channel_id, user_id)

    async def list_for_user(self, guild_id: int, channel_id: int, user_id: int) -> List[str]:
        accounts = await self._storage.get_accounts()
        igns: List[str] = []
        for uuid, entry in accounts.items():
            for watcher in entry.get("watchers", []):
                if watcher["guild_id"] == guild_id and watcher["channel_id"] == channel_id and watcher["user_id"] == user_id:
                    igns.append(entry.get("ign", uuid))
                    break
        igns.sort()
        return igns

    async def _poll_one(self, uuid: str, ign: str) -> List[Notification]:
        async with self._semaphore:
            latest = await self._hypixel.get_last_login_ms(uuid)
        if latest is None:
            return []
        previous = await self._storage.get_last_login(uuid)
        if previous is None:
            await self._storage.set_last_login(uuid, latest)
            return []
        if latest != previous:
            # Update and produce notifications
            await self._storage.set_last_login(uuid, latest)
            accounts = await self._storage.get_accounts()
            entry = accounts.get(uuid)
            if not entry:
                return []
            notifications: List[Notification] = []
            for watcher in entry.get("watchers", []):
                notifications.append(
                    Notification(
                        guild_id=watcher["guild_id"],
                        channel_id=watcher["channel_id"],
                        user_id=watcher["user_id"],
                        ign=entry.get("ign", ign),
                        uuid=uuid,
                        last_login_ms=latest,
                    )
                )
            return notifications
        return []

    async def _poll_loop(self):
        await asyncio.sleep(3)  # small delay after startup
        logger.info("LoginTracker polling loop started (interval=%ss)", POLL_SECONDS)
        while not self._stop_event.is_set():
            try:
                accounts = await self._storage.get_accounts()
                if not accounts:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                tasks = [self._poll_one(uuid, entry.get("ign", uuid)) for uuid, entry in accounts.items()]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                notifications: List[Notification] = []
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("Poll task failed: %s", result)
                        continue
                    notifications.extend(result)
                # Dispatch notifications via an event callback
                if notifications:
                    await self.on_notifications(notifications)
            except Exception as e:
                logger.exception("Error in poll loop: %s", e)
            await asyncio.sleep(POLL_SECONDS)

    async def on_notifications(self, notifications: List[Notification]):
        # To be set by the bot at runtime
        pass


# ----------------- Discord Bot Setup -----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)

aiohttp_session: Optional[aiohttp.ClientSession] = None
storage = JsonStorage(TRACKING_FILE)
hypixel_client: Optional[HypixelClient] = None
login_tracker: Optional[LoginTracker] = None


@bot.event
async def on_ready():
    global login_tracker
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
    try:
        await bot.tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.error("Failed to sync commands: %s", e)
    if login_tracker:
        await login_tracker.start()


# Helper to format epoch ms to relative time
from datetime import datetime, timezone

def format_relative_time_ms(epoch_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except Exception:
        return "unknown time"


@bot.tree.command(name="track", description="Track a Minecraft IGN's Hypixel last login and mention you when it changes.")
@app_commands.describe(ign="Minecraft IGN to track")
async def track_cmd(interaction: discord.Interaction, ign: str):
    await interaction.response.defer(ephemeral=True)
    assert login_tracker is not None
    try:
        ok, msg = await login_tracker.track(ign, interaction.guild_id, interaction.channel_id, interaction.user.id)
        await interaction.followup.send(content=msg, ephemeral=True)
    except Exception as e:
        logger.exception("/track failed: %s", e)
        await interaction.followup.send(content="Failed to track that IGN.", ephemeral=True)


@bot.tree.command(name="untrack", description="Stop tracking a Minecraft IGN for yourself in this channel.")
@app_commands.describe(ign="Minecraft IGN to stop tracking")
async def untrack_cmd(interaction: discord.Interaction, ign: str):
    await interaction.response.defer(ephemeral=True)
    assert login_tracker is not None
    try:
        ok, msg = await login_tracker.untrack(ign, interaction.guild_id, interaction.channel_id, interaction.user.id)
        await interaction.followup.send(content=msg, ephemeral=True)
    except Exception as e:
        logger.exception("/untrack failed: %s", e)
        await interaction.followup.send(content="Failed to untrack that IGN.", ephemeral=True)


@bot.tree.command(name="list", description="List IGNs you are tracking in this channel.")
async def list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    assert login_tracker is not None
    try:
        igns = await login_tracker.list_for_user(interaction.guild_id, interaction.channel_id, interaction.user.id)
        if not igns:
            await interaction.followup.send(content="You are not tracking any IGNs in this channel.", ephemeral=True)
        else:
            await interaction.followup.send(content="Tracking: " + ", ".join(igns), ephemeral=True)
    except Exception as e:
        logger.exception("/list failed: %s", e)
        await interaction.followup.send(content="Failed to list IGNs.", ephemeral=True)


@bot.tree.command(name="untrackall", description="Stop tracking all IGNs you requested in this channel.")
async def untrack_all_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    assert login_tracker is not None
    try:
        removed = await login_tracker.untrack_all_for_user(interaction.guild_id, interaction.channel_id, interaction.user.id)
        if removed > 0:
            await interaction.followup.send(content=f"Stopped tracking {removed} subscription(s).", ephemeral=True)
        else:
            await interaction.followup.send(content="You had no active tracking here.", ephemeral=True)
    except Exception as e:
        logger.exception("/untrackall failed: %s", e)
        await interaction.followup.send(content="Failed to untrack.", ephemeral=True)


@bot.tree.command(name="apitest", description="Check Hypixel API key validity and connectivity.")
async def apitest_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    global hypixel_client
    if hypixel_client is None:
        await interaction.followup.send(content="Client not ready.", ephemeral=True)
        return
    ok, msg = await hypixel_client.get_key_info()
    await interaction.followup.send(content=msg, ephemeral=True)


async def send_notifications(notifications: List[Notification]):
    for note in notifications:
        try:
            channel = bot.get_channel(note.channel_id)
            if channel is None:
                # fetch to ensure availability
                channel = await bot.fetch_channel(note.channel_id)
            if isinstance(channel, discord.abc.Messageable):
                content = (
                    f"<@{note.user_id}> {note.ign} logged into Hypixel (last login updated {format_relative_time_ms(note.last_login_ms)})."
                )
                await channel.send(content=content, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.Forbidden:
            logger.warning("Missing permissions to send message in channel %s", note.channel_id)
        except Exception as e:
            logger.warning("Failed to send notification to channel %s: %s", note.channel_id, e)


async def main():
    global aiohttp_session, hypixel_client, login_tracker

    aiohttp_session = aiohttp.ClientSession()
    if not HYPIXEL_API_KEY:
        logger.error("HYPIXEL_API_KEY is not set. Generate one with '/api new' in Hypixel and set it in .env.")
        await aiohttp_session.close()
        return
    hypixel_client = HypixelClient(aiohttp_session, HYPIXEL_API_KEY)

    await storage.load()
    login_tracker = LoginTracker(storage, hypixel_client)
    # bind callback
    async def on_notes(notes: List[Notification]):
        await send_notifications(notes)
    login_tracker.on_notifications = on_notes  # type: ignore

    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set. Please set it in environment or .env file.")
        await aiohttp_session.close()
        return

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        if login_tracker:
            await login_tracker.stop()
        if aiohttp_session:
            await aiohttp_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass