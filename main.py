#!/usr/bin/env python3
"""
Minecraft Server Discord Bot — v3.5

Manages one or more Minecraft servers through Discord slash commands,
with two-way chat integration via webhooks and RCON.

https://github.com/iy4vet
https://github.com/MinecraftServerDiscordBot/
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import enum
import pathlib
import re
import socket
import sys
from datetime import datetime, timezone
from io import BytesIO
from shutil import rmtree
from typing import Any, Optional
from urllib.request import urlopen

import disnake
import jproperties
import yaml
from disnake.ext import commands
from mcrcon import MCRcon

from mcbot.migration import ensure_config


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_base_path() -> pathlib.Path:
    """Return the base directory for configuration and server files.

    When running inside a PyInstaller bundle, ``sys._MEIPASS`` points at the
    temporary extraction directory — but user data (configs, server folders)
    lives alongside the executable, so we always use the executable's parent
    directory in that case.

    For normal Python execution we use the current working directory.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundled executable — use the directory containing the exe.
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path.cwd().resolve()


def _validate_working_directory(base: pathlib.Path) -> None:
    """Guard against running from the filesystem root or a system directory."""
    if base == pathlib.Path(base.anchor):
        print(
            "ERROR: The bot must not be run from the filesystem root.\n"
            "Please move it to a dedicated directory and restart."
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ServerState(enum.Enum):
    """Lifecycle state of a Minecraft server process."""
    UP = 0
    DOWN = 1
    STARTING = 2
    STOPPING = 3


class PingResponse(enum.Enum):
    """Result of a socket-level server reachability check."""
    SUCCESS = 0
    INACTIVE = 1
    STARTING = 2
    STOPPING = 3
    FAILURE = 4


class MessageSource(enum.Enum):
    """Origin of a chat message (for logging)."""
    DISCORD = 0
    MINECRAFT = 1


class RconResponse(enum.Enum):
    """Sentinel RCON responses and their user-facing counterparts."""
    NOTRUNNING = "NOTRUNNING"
    TRANSITION = "TRANSITION"
    STOPPING = "Stopping"
    RUNNING = "There are"
    FRONT_NOTRUNNING = "The server is not running."
    FRONT_TRANSITION = "The server is transitioning. Trying again…"
    FRONT_STARTING = "The server is starting up. Trying again…"
    FRONT_STOPPING = (
        "The server is shutting down. It cannot be controlled at this time."
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ServerConfig:
    """Tracks runtime state and properties of a single Minecraft server."""

    # Runtime state
    server_running: ServerState = ServerState.DOWN
    server_process: Optional[asyncio.subprocess.Process] = None
    shutdown_pause: Optional[int] = None

    # Properties read from server.properties / bot.yaml
    internal_ip: str = ""
    internal_port: int = 0
    external_ip: str = ""
    external_port: int = 0
    rcon_port: int = 0
    rcon_password: str = ""
    uses_proxy: bool = False


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager:
    """Loads and manages bot configuration (``bot.yaml``) and per-server
    properties (``server.properties``)."""

    def __init__(self, base_dir: pathlib.Path) -> None:
        self.base_dir: pathlib.Path = base_dir
        self.bot_config: dict[str, Any] = {}
        self.server_config: dict[str, ServerConfig] = {}
        self.servers_active: set[str] = set()
        self.proxy_running: ServerState = ServerState.DOWN
        self.proxy_process: Optional[asyncio.subprocess.Process] = None

        self.load_bot_config()
        self.load_server_config()

    # -- YAML I/O -----------------------------------------------------------

    def load_bot_config(self) -> None:
        """Read ``bot.yaml`` from *base_dir*."""
        config_path = self.base_dir / "bot.yaml"
        with config_path.open("r", encoding="utf-8") as fh:
            self.bot_config = yaml.safe_load(fh) or {}

    def update_bot_config(self) -> None:
        """Write the current ``bot_config`` dict back to ``bot.yaml``."""
        config_path = self.base_dir / "bot.yaml"
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.dump(self.bot_config, fh)

    def dump_server_config(self) -> None:
        """Serialise runtime server configuration to ``server.yaml``."""
        snapshot: dict[str, ServerConfig] = copy.deepcopy(self.server_config)
        dump_path = self.base_dir / "server.yaml"
        with dump_path.open("w", encoding="utf-8") as fh:
            yaml.dump(
                {name: dataclasses.asdict(cfg) for name, cfg in snapshot.items()},
                fh,
            )

    # -- server.properties --------------------------------------------------

    def load_server_config(self) -> None:
        """Parse ``server.properties`` for every directory listed in
        ``bot_config["minecraft"]`` and populate *server_config*."""
        errors: list[str] = []

        for directory in self.bot_config.get("minecraft", {}).keys():
            lower = directory.lower()
            if lower == "logs":
                errors.append(
                    f"A server is named '{directory}' — this collides with the log "
                    "directory. Please rename it and restart."
                )
                continue
            if lower == "proxy":
                errors.append(
                    f"A server is named '{directory}' — this collides with the proxy "
                    "directory. Please rename it and restart."
                )
                continue

            props_path = self.base_dir / directory / "server.properties"
            reader = jproperties.Properties()
            with props_path.open("rb") as fh:
                reader.load(fh)

            required_keys = [
                "server-ip",
                "server-port",
                "rcon.password",
                "rcon.port",
                "enable-rcon",
            ]
            props: dict[str, str] = {}
            for key in required_keys:
                entry = reader.get(key)
                props[key] = entry.data if entry else ""

            missing = [k for k in required_keys if not props[k]]
            if missing:
                errors.append(
                    f"Missing properties in {props_path}: {', '.join(missing)}"
                )

            try:
                server_port = int(props["server-port"])
                rcon_port = int(props["rcon.port"])
                if server_port <= 0 or rcon_port <= 0:
                    raise ValueError
            except (ValueError, KeyError):
                errors.append(
                    f"'server-port' or 'rcon.port' is not a valid positive "
                    f"integer in {props_path}."
                )
                continue

            if props.get("enable-rcon") != "true":
                errors.append(
                    f"'enable-rcon' is not 'true' in {props_path}. "
                    "RCON is required for the bot."
                )

            uses_proxy = self.bot_config.get("proxy", {}).get("enabled", False)
            server_block = self.bot_config.get("minecraft", {}).get(directory, {})
            if "proxy" in server_block:
                uses_proxy = server_block["proxy"]

            if directory in self.server_config:
                cfg = self.server_config[directory]
                cfg.internal_ip = props["server-ip"]
                cfg.internal_port = server_port
                cfg.rcon_password = props["rcon.password"]
                cfg.rcon_port = rcon_port
                cfg.uses_proxy = uses_proxy
            else:
                self.server_config[directory] = ServerConfig(
                    internal_ip=props["server-ip"],
                    internal_port=server_port,
                    rcon_password=props["rcon.password"],
                    rcon_port=rcon_port,
                    uses_proxy=uses_proxy,
                )

        if errors:
            for err in errors:
                print(f"CONFIG ERROR: {err}")
            input("Press Enter to exit…")
            raise SystemExit(0)

    # -- helpers ------------------------------------------------------------

    def get_external_addr(self, server: str) -> tuple[str, int]:
        """Return ``(ip, port)`` for a server's external address and update
        ``server_config`` accordingly."""
        ip: str = ""
        port: int = 0
        srv_block = self.bot_config.get("minecraft", {}).get(server, {})

        ip = srv_block.get("server_address", self.bot_config.get("server_address", ""))

        if (
            self.bot_config.get("proxy", {}).get("enabled", False)
            and self.server_config[server].uses_proxy
        ):
            port = int(self.bot_config.get("server_port", 0))
        else:
            port = int(srv_block.get("server_port", self.bot_config.get("server_port", 0)))

        self.server_config[server].external_ip = ip
        self.server_config[server].external_port = port
        return ip, port


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    """Simple file + console logger for bot events."""

    def __init__(self, config_manager: ConfigManager) -> None:
        self._cm: ConfigManager = config_manager

    async def console(self, text: str) -> None:
        now = datetime.now(tz=timezone.utc)
        print(f"[{now}]: {text}")
        path = self._cm.base_dir / "logs" / "console"
        path.mkdir(parents=True, exist_ok=True)
        log_file = path / f"{str(now.date())}.txt"
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now}]: {text}\n")

    async def chat(
        self,
        server: str,
        message: str,
        author: str,
        source: MessageSource,
    ) -> None:
        tag = "MC" if source == MessageSource.MINECRAFT else "DC"
        now = datetime.now(tz=timezone.utc)
        path = self._cm.base_dir / "logs" / "chat" / server
        path.mkdir(parents=True, exist_ok=True)
        log_file = path / f"{str(now.date())}.txt"
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now}]: [{tag}] {author}: {message}\n")

    async def inter(self, cmd: str, user: str, params: Any) -> None:
        now = datetime.now(tz=timezone.utc)
        path = self._cm.base_dir / "logs" / "inter"
        path.mkdir(parents=True, exist_ok=True)
        log_file = path / f"{str(now.date())}.txt"
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now}]: user:{user}\t\tcommand:{cmd}\t\tparams:{params}\n")


# ---------------------------------------------------------------------------
# WebhookHandler
# ---------------------------------------------------------------------------

class WebhookHandler:
    """Routes messages from Minecraft → Discord via channel webhooks."""

    def __init__(
        self,
        config_manager: ConfigManager,
        logger: Logger,
        bot: commands.Bot,
    ) -> None:
        self._cm: ConfigManager = config_manager
        self._logger: Logger = logger
        self.bot: commands.Bot = bot

    async def webhook_send(
        self,
        server: str,
        content: str,
        username: str,
        avatar_url: str,
        embed: Optional[disnake.Embed] = None,
        files: Optional[list[tuple[bytes, str, bool]]] = None,
        crosstalk: int = 0,
    ) -> None:
        """Send *content* (or *embed*) to every configured Discord channel."""
        if not embed and content:
            await self._logger.chat(
                server,
                content,
                username,
                MessageSource.MINECRAFT
                if avatar_url == self._cm.bot_config.get("mc_url")
                else MessageSource.DISCORD,
            )

        for channel_id in self._cm.bot_config.get("discord", {}).keys():
            discord_files: list[disnake.File] = []
            if files:
                for file_data, filename, is_spoiler in files:
                    fp = BytesIO(file_data)
                    fp.seek(0)
                    discord_files.append(
                        disnake.File(fp=fp, filename=filename, spoiler=is_spoiler)
                    )

            channel = await self.bot.fetch_channel(int(channel_id))
            if not isinstance(channel, disnake.TextChannel):
                continue
            webhooks = await channel.webhooks()

            thread_target: Optional[disnake.Thread] = None
            if crosstalk == 0:
                thread_target = await self._resolve_thread(channel, channel_id, server)

            for webhook in webhooks:
                if webhook.token:
                    try:
                        kwargs: dict[str, Any] = {
                            "content": str(content) if content else None,
                            "username": username,
                            "avatar_url": avatar_url,
                            "embed": embed,
                            "files": discord_files,
                        }
                        if thread_target:
                            kwargs["thread"] = thread_target
                        await webhook.send(**kwargs)
                    except disnake.errors.HTTPException:
                        pass
                    break

    async def _resolve_thread(
        self,
        channel: disnake.TextChannel,
        channel_id: str,
        server: str,
    ) -> Optional[disnake.Thread]:
        """Look up (and un-archive) the thread for *server* in *channel*."""
        try:
            thread_id = self._cm.bot_config["discord"][channel_id]["channels"][server]
            thread = channel.get_thread(thread_id)
            if not thread:
                fetched = await channel.guild.fetch_channel(thread_id)
                if isinstance(fetched, disnake.Thread):
                    thread = fetched
            if thread and thread.archived:
                await thread.edit(archived=False)
            return thread
        except (KeyError, AttributeError, disnake.errors.NotFound):
            return None


# ---------------------------------------------------------------------------
# MessageHandler
# ---------------------------------------------------------------------------

class MessageHandler:
    """Bi-directional relay between Discord messages and Minecraft RCON
    *tellraw*."""

    def __init__(
        self,
        config_manager: ConfigManager,
        logger: Logger,
        server_manager: ServerManager,
        webhook_handler: WebhookHandler,
    ) -> None:
        self._cm: ConfigManager = config_manager
        self._logger: Logger = logger
        self._sm: ServerManager = server_manager
        self._wh: WebhookHandler = webhook_handler

    # -- Minecraft → Discord ------------------------------------------------

    async def handle_minecraft_chat(self, server: str, line: str) -> None:
        """Parse ``<User> message`` from server stdout and relay to Discord."""
        try:
            split = line.split("<", 1)[1].split("> ", 1)
            username = split[0]
            message = split[1]

            # Resolve @name#0000 style mentions.
            pings = re.findall(r"[@]\S+[#]\d{4}", message)
            for ping in pings:
                name = ping.split("@")[1].split("#")[0]
                discriminator = ping.split("@")[1].split("#")[1]
                member = disnake.utils.get(
                    self._wh.bot.get_all_members(),
                    name=name,
                    discriminator=discriminator,
                )
                if member:
                    message = message.replace(ping, f"<@{member.id}>")

            await self._wh.webhook_send(
                server, message, username, self._cm.bot_config.get("mc_url", "")
            )
        except (IndexError, AttributeError):
            pass

    # -- Discord → Minecraft ------------------------------------------------

    async def handle_discord_message(self, message: disnake.Message) -> None:
        """Route a Discord message to the correct Minecraft server."""
        if message.author.bot:
            return

        server_name: Optional[str] = None
        crosstalk: int = -1

        for channel_id in self._cm.bot_config.get("discord", {}).keys():
            try:
                channels_cfg = self._cm.bot_config["discord"][channel_id]["channels"]
                if isinstance(message.channel, disnake.Thread):
                    if message.channel.name in channels_cfg:
                        if channels_cfg[message.channel.name] == message.channel.id:
                            server_name = message.channel.name
                            crosstalk = 0
                            break
                for srv, tid in channels_cfg.items():
                    if tid == message.channel.id:
                        server_name = srv
                        crosstalk = 0
                        break
            except (KeyError, AttributeError):
                pass

        if server_name is None:
            try:
                if (
                    str(message.channel.id) in self._cm.bot_config.get("discord", {}).keys()
                    and self._cm.bot_config.get("crosstalk", False)
                ):
                    crosstalk = 1
            except (KeyError, AttributeError):
                pass

        if crosstalk == -1:
            return

        if crosstalk == 0 and server_name:
            clean_msg = message.clean_content.replace('"', '\\"')
            final_msg = f"{{{message.author}}} {clean_msg}"

            max_len = 256
            header_len = len(f"{{{message.author}}} ")
            if len(final_msg) > max_len:
                resp = (
                    f"Message is too long! Your message can be up to "
                    f"{max_len - header_len} characters long."
                )
                error_msg = await message.channel.send(resp)
                await asyncio.sleep(5)
                try:
                    await error_msg.delete()
                    await message.delete()
                except disnake.errors.HTTPException:
                    pass
                return

            resp = self._sm.rcon(server_name, f'tellraw @a "{final_msg}"')
            if resp == RconResponse.NOTRUNNING.value:
                error_msg = await message.channel.send(RconResponse.FRONT_NOTRUNNING.value)
                await asyncio.sleep(5)
                try:
                    await error_msg.delete()
                    await message.delete()
                except disnake.errors.HTTPException:
                    pass
                return
            if resp == RconResponse.TRANSITION.value:
                error_msg = await message.channel.send(RconResponse.FRONT_TRANSITION.value)
                await asyncio.sleep(5)
                try:
                    await error_msg.delete()
                    await message.delete()
                except disnake.errors.HTTPException:
                    pass
                return
            if resp:
                error_msg = await message.channel.send(resp)
                await asyncio.sleep(5)
                try:
                    await error_msg.delete()
                    await message.delete()
                except disnake.errors.HTTPException:
                    pass
                return

        file_props: list[tuple[bytes, str, bool]] = []
        net_size = 0
        for attachment in message.attachments:
            if attachment.size >= 8_388_608:
                await message.channel.send(
                    f"File `{attachment.filename}` was skipped! Bots cannot send "
                    f"files greater than 8 MB regardless of server boosting. "
                    f"This file was `{attachment.size}` bytes."
                )
            elif net_size + attachment.size >= 8_388_608:
                await message.channel.send(
                    f"File `{attachment.filename}` was skipped! Total attachment "
                    f"size would exceed 8 MB (`{net_size + attachment.size}` bytes). "
                    f"Current total: `{net_size}` bytes."
                )
            else:
                file_props.append(
                    (await attachment.read(), attachment.filename, attachment.is_spoiler())
                )
                net_size += attachment.size

        try:
            await message.delete()
        except disnake.errors.NotFound:
            pass

        webhook_server = server_name or getattr(message.channel, "name", "unknown")
        await self._wh.webhook_send(
            webhook_server,
            message.content,
            message.author.name,
            str(message.author.avatar.url) if message.author.avatar else "",
            files=file_props,
            crosstalk=crosstalk,
        )


# ---------------------------------------------------------------------------
# ServerManager
# ---------------------------------------------------------------------------

class ServerManager:
    """Manages Minecraft server processes — start, stop, RCON, ping."""

    def __init__(self, config_manager: ConfigManager, logger: Logger) -> None:
        self._cm: ConfigManager = config_manager
        self._logger: Logger = logger

    # -- RCON ---------------------------------------------------------------

    def rcon(self, server: str, cmd: str) -> str:
        """Send an RCON command and return the response string."""
        cfg: ServerConfig = self._cm.server_config[server]
        if cfg.server_running not in (ServerState.UP, ServerState.STARTING):
            return RconResponse.NOTRUNNING.value
        mcr = MCRcon(cfg.internal_ip, cfg.rcon_password, port=cfg.rcon_port)
        try:
            mcr.connect()
            resp = mcr.command(cmd)
            mcr.disconnect()
            return resp or ""
        except ConnectionRefusedError:
            return RconResponse.TRANSITION.value
        except UnicodeDecodeError:
            return (
                "Something went wrong whilst fetching the response. "
                "Perhaps the server is too old or the command doesn't exist?"
            )

    # -- Ping ---------------------------------------------------------------

    def ping(self, server: str, ip: str, port: int) -> PingResponse:
        """Check whether a server is reachable at *ip*:*port*."""
        cfg = self._cm.server_config[server]
        proxy_active = self._cm.bot_config.get("proxy", {}).get("enabled", False)

        if not (proxy_active and cfg.uses_proxy):
            if cfg.server_running == ServerState.DOWN:
                return PingResponse.INACTIVE
            if cfg.server_running == ServerState.STARTING:
                return PingResponse.STARTING
            if cfg.server_running == ServerState.STOPPING:
                return PingResponse.STOPPING
            if not self.rcon(server, "list").startswith(RconResponse.RUNNING.value):
                return PingResponse.STARTING

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((ip, port))
            sock.close()
            return PingResponse.SUCCESS if result == 0 else PingResponse.FAILURE
        except socket.gaierror:
            return PingResponse.FAILURE

    # -- Background threads -------------------------------------------------

    async def _monitor_thread(self, server: str) -> None:
        """Wait for a server to finish starting, then watch until it stops."""
        cfg = self._cm.server_config[server]

        while cfg.server_running == ServerState.STARTING:
            rcon_resp = self.rcon(server, "list")
            if rcon_resp.startswith(RconResponse.RUNNING.value):
                cfg.server_running = ServerState.UP
                await self._logger.console(f"{server} up and running.")
                break
            if rcon_resp == RconResponse.NOTRUNNING.value:
                cfg.server_running = ServerState.DOWN
                await self._logger.console(f"{server} failed to start.")
                return
            await asyncio.sleep(5)

        while cfg.server_running == ServerState.UP:
            await asyncio.sleep(30)

        if cfg.server_running == ServerState.DOWN:
            await self._logger.console(f"{server} already down.")
            return

        while cfg.server_running == ServerState.STOPPING:
            rcon_resp = self.rcon(server, "list")
            if rcon_resp == RconResponse.NOTRUNNING.value:
                cfg.server_running = ServerState.DOWN
                await self._logger.console(f"{server} stopped and down.")
                return
            await asyncio.sleep(5)

    async def _shutdown_thread(self, server: str) -> None:
        """Automatically stop an empty server after a grace period."""
        cfg = self._cm.server_config[server]

        # Wait until server is actually accepting RCON
        while True:
            rcon_resp = self.rcon(server, "list")
            if rcon_resp.startswith(RconResponse.RUNNING.value):
                break
            if rcon_resp == RconResponse.NOTRUNNING.value:
                return
            await asyncio.sleep(5)

        await asyncio.sleep(300)  # Initial 5-minute grace period

        while cfg.server_running == ServerState.UP:
            if cfg.shutdown_pause:
                if cfg.shutdown_pause == -1:
                    cfg.shutdown_pause = None
                    return
                pause = cfg.shutdown_pause
                cfg.shutdown_pause = None
                await asyncio.sleep(pause)

            for _ in range(2):
                rcon_resp = self.rcon(server, "list")
                if not rcon_resp.startswith("There are 0"):
                    break
                await asyncio.sleep(180)
            else:
                self.rcon(server, "execute unless entity @a run stop")
                cfg.server_running = ServerState.STOPPING
                return

            await asyncio.sleep(180)

    # -- Server process management ------------------------------------------

    async def server_thread(
        self,
        server: str,
        user: disnake.User | disnake.Member,
        webhook_handler: WebhookHandler,
        message_handler: MessageHandler,
    ) -> None:
        """Launch a Minecraft server process and relay its stdout."""
        cfg = self._cm.server_config[server]
        cfg.server_running = ServerState.STARTING
        self._cm.servers_active.add(server)

        asyncio.create_task(self._monitor_thread(server))
        await self._logger.console(f"{server} starting…")

        start_embed = disnake.Embed(
            title=self._cm.bot_config.get("server_title", "Minecraft Server"),
            description=f"🖥️ **{server}** started by {user.mention}",
            colour=disnake.Colour.green(),
            timestamp=datetime.now(tz=timezone.utc),
        )
        start_embed.set_author(
            name=str(user),
            icon_url=str(user.avatar.url) if user.avatar else "",
        )
        bot_avatar = (
            str(webhook_handler.bot.user.avatar.url)
            if webhook_handler.bot.user and webhook_handler.bot.user.avatar
            else ""
        )
        await webhook_handler.webhook_send(
            server, "", "MC Bot", bot_avatar, embed=start_embed
        )

        server_dir = self._cm.base_dir / server
        start_script = self._cm.bot_config["minecraft"][server]["start_script"]
        proc = await asyncio.create_subprocess_shell(
            start_script,
            stdout=asyncio.subprocess.PIPE,
            cwd=str(server_dir),
        )
        cfg.server_process = proc

        while proc.stdout:
            data = await proc.stdout.readline()
            if not data:
                break
            line = data.decode("latin1").rstrip()
            asyncio.create_task(self._logger.console(f"{server}: {line}"))
            asyncio.create_task(message_handler.handle_minecraft_chat(server, line))

        cfg.server_running = ServerState.DOWN
        cfg.server_process = None
        self._cm.servers_active.discard(server)
        await self._logger.console(f"{server} stopped.")

        stop_embed = disnake.Embed(
            title=self._cm.bot_config.get("server_title", "Minecraft Server"),
            description=f"🖥️ **{server}** stopped",
            colour=disnake.Colour.red(),
            timestamp=datetime.now(tz=timezone.utc),
        )
        await webhook_handler.webhook_send(
            server, "", "MC Bot", bot_avatar, embed=stop_embed
        )

    async def proxy_thread(self) -> None:
        """Launch and monitor the proxy process."""
        self._cm.proxy_running = ServerState.STARTING
        await self._logger.console("Proxy: starting…")

        proxy_dir: Optional[pathlib.Path] = None
        for child in self._cm.base_dir.iterdir():
            if child.is_dir() and child.name.lower() == "proxy":
                proxy_dir = child
                break

        if proxy_dir is None:
            await self._logger.console("Proxy: no proxy directory found!")
            self._cm.proxy_running = ServerState.DOWN
            return

        start_script = self._cm.bot_config["proxy"]["start_script"]
        proc = await asyncio.create_subprocess_shell(
            start_script,
            stdout=asyncio.subprocess.PIPE,
            cwd=str(proxy_dir),
        )
        self._cm.proxy_process = proc
        self._cm.proxy_running = ServerState.UP

        while proc.stdout:
            data = await proc.stdout.readline()
            if not data:
                break
            line = data.decode("utf-8").rstrip()
            asyncio.create_task(self._logger.console(f"Proxy: {line}"))

        self._cm.proxy_running = ServerState.DOWN
        self._cm.proxy_process = None
        await self._logger.console("Proxy: stopped.")


# ---------------------------------------------------------------------------
# MinecraftBot
# ---------------------------------------------------------------------------

class MinecraftBot(commands.Bot):
    """The main Discord bot; registers slash commands and event handlers."""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

        intents = disnake.Intents.all()
        super().__init__(
            command_prefix="$",
            intents=intents,
            command_sync_flags=commands.CommandSyncFlags.default(),
        )

        self.logger = Logger(config_manager)
        self.server_manager = ServerManager(config_manager, self.logger)
        self.webhook_handler = WebhookHandler(config_manager, self.logger, self)
        self.message_handler = MessageHandler(
            config_manager,
            self.logger,
            self.server_manager,
            self.webhook_handler,
        )
        self._register_commands()

    # -- Slash commands -----------------------------------------------------

    def _register_commands(self) -> None:
        """Create and bind all slash commands."""
        server_choices = list(self.config_manager.bot_config.get("minecraft", {}).keys())

        # /start
        @self.slash_command(description="Starts the Minecraft server")
        async def start(
            inter: disnake.ApplicationCommandInteraction,
            server: str = commands.Param(choices=server_choices),
            delay: Optional[int] = commands.Param(
                default=None,
                description="Delay shutdown thread (1–300 seconds, -1 to disable)",
            ),
        ) -> None:
            self.config_manager.server_config[server].shutdown_pause = None
            await self.logger.inter("start", str(inter.author), {"server": server, "delay": delay})

            if (
                self.config_manager.bot_config.get("proxy", {}).get("enabled", False)
                and self.config_manager.proxy_running == ServerState.DOWN
            ):
                asyncio.create_task(self.server_manager.proxy_thread())

            if server not in self.config_manager.bot_config.get("minecraft", {}):
                await inter.response.send_message(
                    "Invalid server name. Use /info for a list of servers.",
                    ephemeral=True,
                )
                return

            await inter.response.defer()

            cfg = self.config_manager.server_config[server]
            if cfg.server_running != ServerState.DOWN:
                await inter.edit_original_message(content=f"{server} is already running!")
                return

            max_concurrent = self.config_manager.bot_config.get("server_max_concurrent", -1)
            if max_concurrent != -1 and len(self.config_manager.servers_active) >= max_concurrent:
                await inter.edit_original_message(
                    content=f"Maximum concurrent server limit ({max_concurrent}) reached. "
                    "Try `/stop` to free up some."
                )
                return

            if delay is not None:
                is_op = inter.author.id in self.config_manager.bot_config.get("bot_op", [])
                if is_op:
                    if delay <= 0 and delay != -1:
                        await inter.edit_original_message(
                            content=f"Invalid delay value {delay}. Use -1 to disable, or a positive value."
                        )
                        return
                else:
                    if delay > 300 or delay <= 0:
                        await inter.edit_original_message(
                            content=f"Delay must be between 1 and 300 seconds. You entered {delay}."
                        )
                        return

            asyncio.create_task(
                self.server_manager.server_thread(
                    server, inter.author, self.webhook_handler, self.message_handler
                )
            )

            if delay is not None:
                await inter.edit_original_message(
                    content=f"Starting {server}. Delaying automatic shutdown by an "
                    f"additional {delay} second(s).\nRemember that the automatic "
                    "shutdown thread starts 5 minutes after the server is joinable without this delay."
                )
                await asyncio.sleep(delay)
            else:
                await inter.edit_original_message(content=f"Starting {server}.")

            if delay != -1:
                asyncio.create_task(self.server_manager._shutdown_thread(server))

        # /stop
        @self.slash_command(description="Attempts to stop all servers")
        async def stop(inter: disnake.ApplicationCommandInteraction) -> None:
            await self.logger.inter("stop", str(inter.author), {})
            await inter.response.defer()

            lines: list[str] = []
            for srv in self.config_manager.bot_config.get("minecraft", {}).keys():
                res = self.server_manager.rcon(srv, "execute unless entity @a run stop")
                if not res:
                    res = f"Could not stop: {self.server_manager.rcon(srv, 'list')}"
                lines.append(f"{srv}: `{res}`")
            await inter.edit_original_message(content="\n".join(lines))

        # /delay
        @self.slash_command(description="Pauses the automatic shutdown thread")
        async def delay(
            inter: disnake.ApplicationCommandInteraction,
            delay: int = commands.Param(description="Seconds to delay shutdown (1–300, -1 to cancel)"),
            server: str = commands.Param(choices=server_choices),
        ) -> None:
            await self.logger.inter("delay", str(inter.author), {"server": server, "delay": delay})

            if server not in self.config_manager.bot_config.get("minecraft", {}):
                await inter.response.send_message(
                    "Invalid server name. Use /info for a list.", ephemeral=True
                )
                return

            await inter.response.defer()

            cfg = self.config_manager.server_config[server]
            if cfg.server_running != ServerState.UP:
                await inter.edit_original_message(
                    content="This server isn't running. No shutdown thread to pause."
                )
                return

            is_op = inter.author.id in self.config_manager.bot_config.get("bot_op", [])
            if is_op:
                if delay <= 0 and delay != -1:
                    await inter.edit_original_message(
                        content=f"Invalid delay value {delay}. Use -1 to cancel, or a positive value."
                    )
                    return
            else:
                if delay <= 0 or delay > 300:
                    await inter.edit_original_message(
                        content=f"Delay must be between 1 and 300 seconds. You entered {delay}."
                    )
                    return

            if cfg.shutdown_pause:
                await inter.edit_original_message(
                    content=f"Existing pause of {cfg.shutdown_pause}s. Adding {delay}s "
                    f"→ total wait: {cfg.shutdown_pause + delay}s."
                )
                cfg.shutdown_pause += delay
            else:
                cfg.shutdown_pause = delay
                await inter.edit_original_message(
                    content=f"Pausing automatic shutdown for {delay} second(s)."
                )

        # /info
        @self.slash_command(description="Gets all servers' information")
        async def info(inter: disnake.ApplicationCommandInteraction) -> None:
            await self.logger.inter("info", str(inter.author), {})
            await inter.response.defer()

            lines: list[str] = []
            for srv in self.config_manager.bot_config.get("minecraft", {}).keys():
                address, port = self.config_manager.get_external_addr(srv)
                cfg = self.config_manager.server_config[srv]
                status = (
                    self.server_manager.rcon(srv, "list")
                    if cfg.server_running == ServerState.UP
                    else "Server is not running."
                )
                lines.append(
                    f"**{srv}:**\n"
                    f"    Server address: `{address}:{port}`.\n"
                    f"    Using proxy: `{cfg.uses_proxy}`.\n"
                    f"    {status}"
                )

            if self.config_manager.bot_config.get("proxy", {}).get("enabled", False):
                lines.append(
                    "\n_A proxy setup is present. Connect to proxy-enabled servers "
                    "using the same address and navigate using `/server [name]` in "
                    "Minecraft. Non-proxy servers require their own address._"
                )
            await inter.edit_original_message(content="\n".join(lines))

        # /checkaddress
        @self.slash_command(description="Checks server address")
        async def checkaddress(
            inter: disnake.ApplicationCommandInteraction,
            server: str = commands.Param(choices=server_choices),
        ) -> None:
            await self.logger.inter("checkaddress", str(inter.author), {"server": server})

            if (
                self.config_manager.bot_config.get("proxy", {}).get("enabled", False)
                and self.config_manager.proxy_running == ServerState.DOWN
            ):
                asyncio.create_task(self.server_manager.proxy_thread())

            if server not in self.config_manager.bot_config.get("minecraft", {}):
                await inter.response.send_message("Invalid server name.", ephemeral=True)
                return

            await inter.response.defer()
            await inter.edit_original_message(content="Pinging current address…")

            address, port = self.config_manager.get_external_addr(server)
            ping_result = self.server_manager.ping(server, address, port)

            if ping_result == PingResponse.SUCCESS:
                await inter.edit_original_message(
                    content=f"✅ Successfully pinged server at `{address}:{port}`.\n"
                    "Ensure your Minecraft client uses this address."
                )
            elif ping_result == PingResponse.INACTIVE:
                await inter.edit_original_message(
                    content="Server is not running. Please start it first."
                )
            elif ping_result == PingResponse.STARTING:
                await inter.edit_original_message(
                    content="Server is starting up. Please wait and try again."
                )
            elif ping_result == PingResponse.STOPPING:
                await inter.edit_original_message(content="Server is shutting down.")
            else:
                await inter.edit_original_message(
                    content="Current address failed. Fetching external IP…"
                )
                try:
                    ext_ip = urlopen("https://ident.me").read().decode("utf8")  # noqa: S310
                    ping_result = self.server_manager.ping(server, ext_ip, port)
                    if ping_result == PingResponse.SUCCESS:
                        srv_cfg = self.config_manager.server_config[server]
                        if srv_cfg.uses_proxy:
                            self.config_manager.bot_config["server_address"] = ext_ip
                        else:
                            self.config_manager.bot_config.setdefault("minecraft", {}).setdefault(server, {})["server_address"] = ext_ip
                        self.config_manager.update_bot_config()
                        await inter.edit_original_message(
                            content=f"✅ Found new working address: `{ext_ip}:{port}`"
                        )
                    else:
                        ops = " ".join(
                            f"<@{oid}>"
                            for oid in self.config_manager.bot_config.get("bot_op", [])
                        )
                        await inter.edit_original_message(
                            content=f"❌ Could not find a working address. Please ask {ops} to check the configuration."
                        )
                except Exception as exc:
                    await inter.edit_original_message(
                        content=f"Error fetching external IP: {exc}"
                    )

        # /help
        @self.slash_command(description="Get help on all commands")
        async def help(inter: disnake.ApplicationCommandInteraction) -> None:
            await self.logger.inter("help", str(inter.author), {})
            help_text = (
                "**Available Commands:**\n\n"
                "`/start <server> [delay]` — Start a Minecraft server\n"
                "  • `delay`: Optional delay for auto-shutdown (1–300s, -1 to disable)\n\n"
                "`/stop` — Stop all running servers\n\n"
                "`/delay <delay> <server>` — Pause auto-shutdown for a server\n"
                "  • `delay`: Seconds to delay (1–300, -1 to cancel)\n\n"
                "`/info` — Display information about all servers\n\n"
                "`/checkaddress <server>` — Check if a server address is reachable\n\n"
                "`/setaddress <address> <port> <server>` — Set server address (OP only)\n\n"
                "`/cmd <command> <server>` — Execute Minecraft command (OP only)\n\n"
                "`/reset` — Reset temporary server's world (OP only)\n\n"
                "`/help` — Display this help message"
            )
            await inter.response.send_message(help_text)

        # /setaddress
        @self.slash_command(description="Sets a new address for the Minecraft server")
        async def setaddress(
            inter: disnake.ApplicationCommandInteraction,
            address: str = commands.Param(description="New server address"),
            port: int = commands.Param(description="New server port"),
            server: str = commands.Param(choices=server_choices),
        ) -> None:
            await self.logger.inter(
                "setaddress", str(inter.author),
                {"address": address, "port": port, "server": server},
            )
            if inter.author.id not in self.config_manager.bot_config.get("bot_op", []):
                await inter.response.send_message(
                    "You don't have permission to use this command!", ephemeral=True
                )
                return
            if server not in self.config_manager.bot_config.get("minecraft", {}):
                await inter.response.send_message("Invalid server name.", ephemeral=True)
                return

            await inter.response.defer()

            ping_result = self.server_manager.ping(server, address, port)
            if ping_result == PingResponse.SUCCESS:
                srv_cfg = self.config_manager.server_config[server]
                if srv_cfg.uses_proxy:
                    self.config_manager.bot_config["server_address"] = address
                    self.config_manager.bot_config["server_port"] = port
                else:
                    self.config_manager.bot_config.setdefault("minecraft", {}).setdefault(server, {})
                    self.config_manager.bot_config["minecraft"][server]["server_address"] = address
                    self.config_manager.bot_config["minecraft"][server]["server_port"] = port
                self.config_manager.update_bot_config()
                await inter.edit_original_message(content=f"✅ Address updated to `{address}:{port}`")
            elif ping_result == PingResponse.INACTIVE:
                await inter.edit_original_message(
                    content="Server is not running. Start it to verify the address."
                )
            elif ping_result in (PingResponse.STARTING, PingResponse.STOPPING):
                await inter.edit_original_message(
                    content="Server is transitioning. Please wait and try again."
                )
            else:
                await inter.edit_original_message(
                    content=f"❌ Address `{address}:{port}` is not reachable."
                )

        # /cmd
        @self.slash_command(description="Executes a Minecraft command on the server")
        async def cmd(
            inter: disnake.ApplicationCommandInteraction,
            command: str = commands.Param(description="Command to execute"),
            server: str = commands.Param(choices=server_choices),
        ) -> None:
            await self.logger.inter("cmd", str(inter.author), {"command": command, "server": server})
            if inter.author.id not in self.config_manager.bot_config.get("bot_op", []):
                await inter.response.send_message(
                    "You don't have permission to use this command!", ephemeral=True
                )
                return
            if server not in self.config_manager.bot_config.get("minecraft", {}):
                await inter.response.send_message("Invalid server name.", ephemeral=True)
                return
            await inter.response.defer()
            result = self.server_manager.rcon(server, command)
            await inter.edit_original_message(content=f"RCON: {result}")

        # /reset
        @self.slash_command(description="Delete temporary server's world")
        async def reset(inter: disnake.ApplicationCommandInteraction) -> None:
            await self.logger.inter("reset", str(inter.author), {})
            if inter.author.id not in self.config_manager.bot_config.get("bot_op", []):
                await inter.response.send_message(
                    "You don't have permission to use this command!", ephemeral=True
                )
                return

            await inter.response.defer()

            server = "someotherserver"
            if server not in self.config_manager.bot_config.get("minecraft", {}):
                await inter.edit_original_message(
                    content="Server 'someotherserver' not found in configuration!"
                )
                return

            cfg = self.config_manager.server_config[server]
            if cfg.server_running != ServerState.DOWN:
                await inter.edit_original_message(content="Stopping server…")
                while cfg.server_running != ServerState.DOWN:
                    self.server_manager.rcon(server, "stop")
                    await asyncio.sleep(5)
            else:
                await inter.edit_original_message(content="Server isn't running…")

            await inter.edit_original_message(content="Deleting world files…")
            server_dir = self._cm_base / server
            for world_name in ("world", "world_nether", "world_the_end"):
                world_path = server_dir / world_name
                if world_path.exists():
                    rmtree(world_path)

            await inter.edit_original_message(content="✅ World reset complete!")

    # -- Events -------------------------------------------------------------

    async def on_ready(self) -> None:
        await self.logger.console(f"Logged in as {self.user}")
        await self.change_presence(activity=disnake.Game("/help for help"))

        for channel_id in self.config_manager.bot_config.get("discord", {}).keys():
            channel = await self.fetch_channel(int(channel_id))
            if not isinstance(channel, disnake.TextChannel):
                continue
            webhooks = await channel.webhooks()

            if not any(w.token for w in webhooks):
                await channel.create_webhook(name="MCBotChat")

            for server in self.config_manager.bot_config.get("minecraft", {}).keys():
                await self._ensure_thread(channel, channel_id, server)

        for server in self.config_manager.bot_config.get("minecraft", {}).keys():
            srv_block = self.config_manager.bot_config["minecraft"][server]
            if "proxy" not in srv_block:
                srv_block["proxy"] = self.config_manager.bot_config.get("proxy", {}).get("enabled", False)

        self.config_manager.update_bot_config()

        if (
            self.config_manager.bot_config.get("proxy", {}).get("enabled", False)
            and self.config_manager.proxy_running == ServerState.DOWN
        ):
            asyncio.create_task(self.server_manager.proxy_thread())

    async def _ensure_thread(
        self,
        channel: disnake.TextChannel,
        channel_id: str,
        server: str,
    ) -> None:
        """Ensure a chat thread exists for *server* in *channel*."""
        try:
            discord_cfg = self.config_manager.bot_config["discord"][channel_id]
            thread_id = discord_cfg.get("channels", {}).get(server)
            if thread_id:
                thread = channel.get_thread(thread_id)
                if not thread:
                    fetched = await channel.guild.fetch_channel(thread_id)
                    if isinstance(fetched, disnake.Thread):
                        thread = fetched
                if thread and thread.archived:
                    await thread.edit(archived=False)
            else:
                await self._create_thread(channel, channel_id, server)
        except (TypeError, KeyError, AttributeError, disnake.errors.NotFound) as exc:
            if isinstance(exc, TypeError):
                self.config_manager.bot_config["discord"][channel_id] = {}
            await self.logger.console(f"Error setting up thread for {server}: {exc}")
            try:
                await self._create_thread(channel, channel_id, server)
            except Exception as inner:
                await self.logger.console(f"Failed to create thread for {server}: {inner}")

    async def _create_thread(
        self,
        channel: disnake.TextChannel,
        channel_id: str,
        server: str,
    ) -> None:
        """Create a new chat thread for *server*."""
        msg = await channel.send(f"Creating thread for {server} chat integration.")
        thread = await channel.create_thread(
            name=server,
            reason=f"Creating thread for {server} chat integration.",
            message=msg,
        )
        discord_cfg = self.config_manager.bot_config["discord"].setdefault(channel_id, {})
        discord_cfg.setdefault("channels", {})[server] = thread.id

    async def on_message(self, message: disnake.Message) -> None:
        await self.message_handler.handle_discord_message(message)
        await self.process_commands(message)

    @property
    def _cm_base(self) -> pathlib.Path:
        return self.config_manager.base_dir


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    base = resolve_base_path()
    _validate_working_directory(base)

    # Run configuration migration before anything else.
    ensure_config(base)

    config_manager = ConfigManager(base)
    bot = MinecraftBot(config_manager)
    bot.run(config_manager.bot_config["bot_token"])


if __name__ == "__main__":
    main()
