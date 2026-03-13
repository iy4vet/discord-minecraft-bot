# Minecraft Server Discord Bot — v3.5

A Discord bot that manages one or more Minecraft servers via slash commands, with two-way chat integration.

## Features

- **Multi-server support** — manage multiple Minecraft servers from a single bot instance.
- **Automatic shutdown** — empty servers are stopped after a configurable grace period.
- **Two-way chat** — Discord messages are relayed to Minecraft via RCON and Minecraft chat appears in Discord via webhooks. Each server gets its own thread.
- **Proxy support** — optional BungeeCord / Velocity proxy management.
- **Address checking** — the bot can ping your server and auto-detect address changes.
- **Automatic migration** — upgrades from v1.0 `.env` configs all the way to v3.5 YAML.
- **PyInstaller-ready** — pre-built binaries are available for Windows, macOS, and Linux (x64 and ARM64).

## Commands

### General

| Command | Description |
|---|---|
| `/start <server> [delay]` | Start a Minecraft server. Optional `delay` (1–300s) postpones auto-shutdown; `-1` disables it. |
| `/stop` | Stop all running servers (only if empty). |
| `/delay <seconds> <server>` | Pause the auto-shutdown thread (1–300s, or `-1` to cancel). |
| `/info` | Display address, proxy status, and online players for every server. |
| `/checkaddress <server>` | Ping the stored address and auto-update if a new one is found. |
| `/help` | Show command help. |

### Operator only (users listed in `bot_op`)

| Command | Description |
|---|---|
| `/setaddress <address> <port> <server>` | Set a new server address after verifying reachability. |
| `/cmd <command> <server>` | Execute an arbitrary Minecraft command via RCON. |
| `/reset` | Delete and regenerate the temporary server's world. |

## Installation

### 1. Create a Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/) and create a new application.
2. Under **Bot**, create a bot and copy the token.
3. Under **OAuth2 → URL Generator**, select `bot` + `applications.commands`, then **Administrator** permission.
4. Use the generated URL to invite the bot to your server.

### 2. Set up your Minecraft server(s)

Ensure each server's `server.properties` contains:

```properties
enable-rcon=true
rcon.password=YOUR_PASSWORD
server-ip=0.0.0.0
server-port=25565
rcon.port=25575
```

### 3. Configure the bot

Copy `config_examples/config.yaml.example` to `bot.yaml` in the bot's directory and fill in your values. See the example for documentation of every field.

**Minimal example:**

```yaml
version: v3.5
bot_token: YOUR_TOKEN_HERE
server_address: your.server.ip
server_port: 25565
server_title: My Minecraft Server
bot_op:
  - 123456789012345678
discord:
  "987654321098765432":
    channels: {}
minecraft:
  MyServer:
    start_script: java -Xmx2G -Xms512M -jar server.jar
```

### 4. Run the bot

**From source (Python 3.10+):**

```bash
pip install -r requirements.txt
python main.py
```

**From a pre-built binary:**

Download the appropriate binary from [Releases](https://github.com/MinecraftServerDiscordBot/minecraft-server-discord-bot-python/releases) and run it in the same directory as your `bot.yaml` and server folders.

## Upgrading from older versions

The bot automatically migrates configurations from v1.0 (`.env`), v2.0/v2.4, and v3.0 to v3.5. A backup of the original file is created before any changes are made. Simply place the new `main.py` (or binary) alongside your existing config and run it.

## Project structure

```
main.py               Entry point
mcbot/
  __init__.py          Package init
  migration.py         Chained configuration migration (v1.0 → v3.5)
config_examples/
  config.yaml.example  v3.5 YAML configuration template
  .env.example         Legacy v1.0 .env template
.github/workflows/
  build.yml            CI/CD: PyInstaller builds for 6 targets
requirements.txt       Python dependencies
```

## Important notes

- Minecraft chat relay parses `<player> message` from server stdout. Mods that alter the chat format may break this.
- RCON must be enabled for all bot functionality.
- The bot requires the **Message Content** intent (enabled in the Developer Portal and in code).

## Licence

[GNU GPL v3](LICENSE)
