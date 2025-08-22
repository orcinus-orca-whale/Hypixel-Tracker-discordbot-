# Discord Hypixel Login Tracker Bot

Tracks Hypixel last login for specified Minecraft IGNs and @mentions the requester in the channel as soon as the account logs in (lastLogin changes).

## Features
- Slash commands: `/track`, `/untrack`, `/list`, `/untrackall`
- Per-requester notifications in the channel where tracking was requested
- Persists tracking data in `data/tracking.json`
- Polls Hypixel API on an interval (default 30s; min 10s)

## Prerequisites
- Python 3.10+
- A Discord Bot token
- Hypixel API key

## Setup
1. Clone or open this folder.
2. Create a virtual environment and install requirements:
```bash
cd discord-hypixel-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
3. Create a `.env` file (copy from `.env.example`) and set your Discord token:
```bash
cp .env.example .env
# edit .env and set DISCORD_TOKEN=...
```
4. Invite the bot to your server with applications.commands scope.

## Run
```bash
source .venv/bin/activate
python bot.py
```

## Commands
- `/track ign:<minecraft_ign>`: Start tracking. The bot will send a message in this channel and @mention you when the IGN's `lastLogin` changes on Hypixel.
- `/untrack ign:<minecraft_ign>`: Stop tracking that IGN for you in this channel.
- `/list`: List the IGNs you are tracking in this channel.
- `/untrackall`: Remove all of your tracking subscriptions in this channel.

## Notes
- The bot uses Mojang API (and a fallback) to resolve IGN to UUID, then queries Hypixel API v2 with your API key.
- The poll interval can be set via `POLL_SECONDS` in `.env`. Keep it reasonable to avoid rate-limits.
- The storage file `data/tracking.json` is updated atomically.