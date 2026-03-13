"""
Configuration migration module.

Detects the current configuration version and applies chained upgrades:
  v1.0 (bot.env)           -> v2.0 (bot.yaml, old structure)
  v2.0 (unversioned yaml)  -> v2.4 (v3.0-like structure, no version field)
  v2.4 (v3.0-like, no ver) -> v3.0 (versioned yaml)
  v3.0                     -> v3.5 (adds crosstalk field)

Each step calls the next, so running against a v1.0 config will walk all
the way through to v3.5 automatically.

Called from the main application startup; not intended for standalone use.
"""

from __future__ import annotations

import pathlib
import shutil
from typing import Any, Optional

import yaml

# Ordered list of all known versions (oldest to newest).
VERSIONS: list[str] = ["v1.0", "v2.0", "v2.4", "v3.0", "v3.5"]
LATEST: str = VERSIONS[-1]


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def _is_v3_structure(config: dict[str, Any]) -> bool:
    """Return True if *config* resembles the v3.0 YAML layout (even without a version field)."""
    try:
        # v3.0 discord section uses dict-of-dicts with a "channels" key.
        discord: Any = config.get("discord", {})
        if isinstance(discord, dict):
            for channel_cfg in discord.values():
                if isinstance(channel_cfg, dict) and "channels" in channel_cfg:
                    return True
        # v3.0 proxy section has "enabled".
        proxy: Any = config.get("proxy", {})
        if isinstance(proxy, dict) and "enabled" in proxy:
            return True
        # v3.0 minecraft servers have "proxy" field.
        minecraft: Any = config.get("minecraft", {})
        if isinstance(minecraft, dict):
            for srv_cfg in minecraft.values():
                if isinstance(srv_cfg, dict) and "proxy" in srv_cfg:
                    return True
    except Exception:
        pass
    return False


def detect_version(
    base_dir: pathlib.Path,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Detect the configuration version present in *base_dir*.

    Returns ``(version_string, config_dict)`` or ``("unknown", None)``
    if no recognisable configuration is found.
    """
    print("\n=== Detecting configuration version ===")
    yaml_path: pathlib.Path = base_dir / "bot.yaml"
    env_path: pathlib.Path = base_dir / "bot.env"

    if yaml_path.exists():
        try:
            config: dict[str, Any] = yaml.safe_load(
                yaml_path.read_text(encoding="utf-8")
            )
            version: Optional[str] = config.get("version")
            if version in VERSIONS:
                print(f"  Detected {version} configuration.")
                return version, config
            if version is None:
                if _is_v3_structure(config):
                    print("  Detected v2.4 configuration (v3.0-like, no version field).")
                    return "v2.4", config
                print("  Detected v2.0 configuration (old YAML, no version field).")
                return "v2.0", config
            # Unknown future version — treat as up-to-date.
            print(f"  Detected configuration version: {version}")
            return version, config
        except Exception as exc:
            print(f"  Error reading bot.yaml: {exc}")
            return "unknown", None

    if env_path.exists():
        print("  Detected v1.0 configuration (bot.env).")
        return "v1.0", None

    print("  No configuration files found.")
    return "unknown", None


# ---------------------------------------------------------------------------
# Individual upgrade steps
# ---------------------------------------------------------------------------


def _upgrade_v1_to_v2(
    base_dir: pathlib.Path,
    server_name: str = "Server",
) -> dict[str, Any]:
    """v1.0 (bot.env) -> v2.0 (bot.yaml with old structure).

    Moves server files into a subdirectory and builds a v2.0-style YAML.
    Returns the new config dict.
    """
    print("\n--- v1.0 -> v2.0 ---")

    # Lazy-import dotenv so it is only required for legacy upgrades.
    import os
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=str(base_dir / "bot.env"))

    # Read server.properties for the internal port if available.
    server_port_internal: int = 25565
    props_path: pathlib.Path = base_dir / "server.properties"
    if props_path.exists():
        try:
            from jproperties import Properties

            props: Properties = Properties()
            with props_path.open("rb") as fh:
                props.load(fh)
            server_port_internal = int(props.get("server-port", "25565").data)
        except Exception as exc:
            print(f"  Warning: could not read server.properties: {exc}")

    # Create a directory for the server files.
    server_dir: str = server_name
    counter: int = 1
    while (base_dir / server_dir).exists():
        server_dir = f"{server_name}{counter}"
        counter += 1
        if counter > 100:
            raise RuntimeError("Could not create server directory (too many conflicts).")
    (base_dir / server_dir).mkdir()

    # Move server files — keep bot infrastructure in root.
    keep_files: set[str] = {
        "main.py",
        "migration.py",
        "bot.py",
        "bot2.py",
        "bot (env).py",
        "upgrade.py",
        "bot.env",
        "bot.yaml",
        "bot.yaml.example",
        "bot.env.bak",
        "bot.yaml.bak",
        server_dir,
        "legacy",
        "logs",
        "mcbot",
        "config_examples",
        ".git",
        ".gitignore",
        ".github",
        "README.md",
        "CHANGES.md",
        "LICENSE",
        "requirements.txt",
    }
    all_items: set[str] = {p.name for p in base_dir.iterdir()}
    moved: int = 0
    for item in all_items - keep_files:
        try:
            shutil.move(str(base_dir / item), str(base_dir / server_dir / item))
            moved += 1
        except Exception as exc:
            print(f"  Warning: could not move {item}: {exc}")
    print(f"  Moved {moved} items to {server_dir}/")

    # Parse server address.
    server_addr: str = os.getenv("server-address", "127.0.0.1")
    if ":" in server_addr:
        address_ip: str = server_addr.split(":")[0]
        external_port: int = int(server_addr.split(":")[1])
    else:
        address_ip = server_addr
        external_port = 25565

    # Build v2.0 YAML config.
    channel_ids: list[str] = [
        c.strip() for c in os.getenv("chat-channel-id", "").split(",") if c.strip()
    ]
    bot_ops_raw: list[str] = [
        o.strip() for o in os.getenv("server-op", "").split(",") if o.strip()
    ]

    config: dict[str, Any] = {
        "bot_token": os.getenv("bot-token", ""),
        "server_address": address_ip,
        "server_port": (
            external_port if external_port != 25565 else server_port_internal
        ),
        "bot_op": [int(o) for o in bot_ops_raw],
        "discord": channel_ids,
        "minecraft": {
            server_dir: {
                "start_script": os.getenv("start-script", "java -jar server.jar"),
            }
        },
        "bungeecord": 0,
        "mc_url": (
            "https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com/i/"
            "977e8c4f-1c99-46cd-b070-10cd97086c08/d36qrs5-017c3744-8c94-4d47-9633-"
            "d85b991bf2f7.png"
        ),
        "server_max_concurrent": -1,
        "server_title": "A Minecraft Server",
    }

    # Back up bot.env.
    _backup_file(base_dir / "bot.env")

    # Write v2.0 yaml.
    _write_yaml(base_dir, config)
    print("  v1.0 -> v2.0 complete.")
    return config


def _upgrade_v2_to_v24(
    base_dir: pathlib.Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """v2.0 (old unversioned yaml) -> v2.4 (v3.0-like structure without version field)."""
    print("\n--- v2.0 -> v2.4 ---")
    _backup_yaml(base_dir)

    new: dict[str, Any] = {
        "bot_token": config.get("bot_token", ""),
        "server_address": config.get("server_address", "127.0.0.1"),
        "server_port": config.get("server_port", 25565),
        "bot_op": config.get("bot_op", []),
        "mc_url": config.get(
            "mc_url",
            "https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com/i/"
            "977e8c4f-1c99-46cd-b070-10cd97086c08/d36qrs5-017c3744-8c94-4d47-9633-"
            "d85b991bf2f7.png",
        ),
        "server_max_concurrent": config.get("server_max_concurrent", -1),
        "server_title": config.get("server_title", "A Minecraft Server"),
    }

    # Upgrade discord section.
    old_discord: Any = config.get("discord", [])
    minecraft_servers: list[str] = list(config.get("minecraft", {}).keys())
    if isinstance(old_discord, list):
        new["discord"] = {
            str(cid): {"channels": {srv: None for srv in minecraft_servers}}
            for cid in old_discord
        }
    elif isinstance(old_discord, dict):
        new["discord"] = old_discord
    else:
        new["discord"] = {}

    # Upgrade minecraft section.
    old_minecraft: dict[str, Any] = config.get("minecraft", {})
    new["minecraft"] = {}
    for srv_name, srv_cfg in old_minecraft.items():
        entry: dict[str, Any] = {
            "start_script": srv_cfg.get("start_script", "java -jar server.jar"),
            "proxy": srv_cfg.get("proxy", False),
        }
        if "server_port" in srv_cfg:
            entry["server_port"] = srv_cfg["server_port"]
        new["minecraft"][srv_name] = entry

    # Upgrade proxy section.
    old_proxy: Any = config.get("proxy", config.get("bungeecord", 0))
    if isinstance(old_proxy, dict):
        new["proxy"] = {
            "enabled": old_proxy.get("enabled", False),
            "start_script": old_proxy.get("start_script", "java -jar bungeecord.jar"),
        }
    else:
        new["proxy"] = {
            "enabled": bool(old_proxy),
            "start_script": "java -jar bungeecord.jar",
        }

    _write_yaml(base_dir, new)
    print("  v2.0 -> v2.4 complete.")
    return new


def _upgrade_v24_to_v30(
    base_dir: pathlib.Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """v2.4 (v3.0-like without version field) -> v3.0 (adds version field)."""
    print("\n--- v2.4 -> v3.0 ---")
    _backup_yaml(base_dir)
    config["version"] = "v3.0"
    _write_yaml(base_dir, config)
    print("  v2.4 -> v3.0 complete.")
    return config


def _upgrade_v30_to_v35(
    base_dir: pathlib.Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """v3.0 -> v3.5 (adds crosstalk field if missing, bumps version)."""
    print("\n--- v3.0 -> v3.5 ---")
    _backup_yaml(base_dir)
    config.setdefault("crosstalk", False)
    config["version"] = "v3.5"
    _write_yaml(base_dir, config)
    print("  v3.0 -> v3.5 complete.")
    return config


# ---------------------------------------------------------------------------
# Upgrade chain runner
# ---------------------------------------------------------------------------

# Mapping: current version -> next version.
_UPGRADE_CHAIN: dict[str, str] = {
    "v1.0": "v2.0",
    "v2.0": "v2.4",
    "v2.4": "v3.0",
    "v3.0": "v3.5",
}

# Mapping: current version -> upgrade callable.
# v1.0 takes (base_dir, server_name); all others take (base_dir, config).
_UPGRADE_FNS: dict[str, Any] = {
    "v1.0": _upgrade_v1_to_v2,
    "v2.0": _upgrade_v2_to_v24,
    "v2.4": _upgrade_v24_to_v30,
    "v3.0": _upgrade_v30_to_v35,
}


def run_upgrade_chain(
    base_dir: pathlib.Path,
    start_version: str,
    config: Optional[dict[str, Any]],
    server_name: str = "Server",
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Walk the upgrade chain from *start_version* to the latest version.

    Returns ``(success, final_config)``.
    """
    if start_version == LATEST:
        print(f"\n  Already at {LATEST}. No upgrade needed.")
        return True, config

    current: str = start_version
    current_config: Optional[dict[str, Any]] = config

    while current != LATEST:
        if current not in _UPGRADE_CHAIN:
            print(f"\n  Unknown version '{current}'. Cannot upgrade.")
            return False, current_config

        fn = _UPGRADE_FNS[current]
        try:
            if current == "v1.0":
                current_config = fn(base_dir, server_name)
            else:
                current_config = fn(base_dir, current_config)
        except Exception as exc:
            print(f"\n  Upgrade from {current} failed: {exc}")
            return False, current_config

        current = _UPGRADE_CHAIN[current]

    print(f"\n  Successfully upgraded to {LATEST}.")
    return True, current_config


# ---------------------------------------------------------------------------
# Startup entry point
# ---------------------------------------------------------------------------


def ensure_config(base_dir: pathlib.Path) -> dict[str, Any]:
    """Ensure a valid v3.5 configuration exists in *base_dir*.

    If an older configuration is found, it is upgraded in-place.
    The original file(s) are backed up with a ``.bak`` suffix.

    Returns the loaded v3.5 config dict, or raises ``SystemExit`` on failure.
    """
    version, config = detect_version(base_dir)

    if version == "unknown":
        print(
            "\n  No configuration found.\n"
            "  Copy config_examples/config.yaml.example to bot.yaml and fill in your values."
        )
        raise SystemExit(1)

    # If both .yaml and .env exist and we are upgrading from yaml, back up the .env too.
    env_path: pathlib.Path = base_dir / "bot.env"
    yaml_path: pathlib.Path = base_dir / "bot.yaml"
    if yaml_path.exists() and env_path.exists() and version != "v1.0":
        _backup_file(env_path)

    if version == LATEST:
        print(f"  Configuration is at {LATEST}. No upgrade needed.")
        return config  # type: ignore[return-value]

    # For v1.0 upgrades we need to pick a server directory name automatically.
    # In headless/startup mode we cannot prompt interactively, so default to "Server".
    server_name: str = "Server"

    success, final_config = run_upgrade_chain(base_dir, version, config, server_name)
    if not success or final_config is None:
        print("\n  Configuration upgrade failed. Check the messages above.")
        raise SystemExit(1)

    return final_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _backup_file(path: pathlib.Path) -> None:
    """Copy *path* to ``path.bak``, preserving metadata."""
    dst: pathlib.Path = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(str(path), str(dst))
        print(f"  Backed up {path.name} -> {dst.name}")
    except Exception as exc:
        print(f"  Warning: could not back up {path.name}: {exc}")


def _backup_yaml(base_dir: pathlib.Path) -> None:
    """Back up ``bot.yaml`` in *base_dir*."""
    _backup_file(base_dir / "bot.yaml")


def _write_yaml(base_dir: pathlib.Path, config: dict[str, Any]) -> None:
    """Write *config* to ``bot.yaml`` in *base_dir*."""
    (base_dir / "bot.yaml").write_text(
        yaml.dump(config, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
