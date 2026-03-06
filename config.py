"""Configuration management for kirocli-bot-gateway."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

log = logging.getLogger(__name__)


def _parse_workspace_mode(value: str | None, default: str = "per_chat") -> str:
    """Parse and validate workspace_mode value."""
    if not value:
        return default
    value = value.lower().strip()
    return value if value in ("fixed", "per_chat") else default


@dataclass
class FeishuConfig:
    """Feishu adapter configuration."""
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    bot_name: str = ""
    kiro_cwd: str = ""  # Platform-specific working directory (optional)
    workspace_mode: str = ""  # Platform-specific mode (optional, fallback to global)


# =============================================================================
# Discord Policy Configuration (OpenClaw-style)
# =============================================================================

@dataclass
class DiscordChannelPolicy:
    """Policy for a specific Discord channel."""
    allow: bool = False
    require_mention: bool | None = None  # None = inherit from guild
    users: list[str] = field(default_factory=list)  # Per-channel user allowlist


@dataclass
class DiscordGuildPolicy:
    """Policy for a specific Discord guild (server)."""
    require_mention: bool = True
    users: list[str] = field(default_factory=list)  # Per-guild user allowlist
    channels: dict[str, DiscordChannelPolicy] = field(default_factory=dict)


@dataclass
class DiscordDmPolicy:
    """Policy for Discord DMs."""
    enabled: bool = True
    policy: str = "allowlist"  # "open" | "allowlist" | "disabled"
    allow_from: list[str] = field(default_factory=list)  # User IDs allowed to DM


@dataclass
class DiscordPolicy:
    """Complete Discord access policy (OpenClaw-style).
    
    Structure mirrors OpenClaw's channels.discord configuration:
    - dm: DM access control
    - group_policy: "open" | "allowlist" | "disabled"
    - guilds: Per-guild rules with optional "*" wildcard for defaults
    """
    dm: DiscordDmPolicy = field(default_factory=DiscordDmPolicy)
    group_policy: str = "allowlist"  # "open" | "allowlist" | "disabled"
    guilds: dict[str, DiscordGuildPolicy] = field(default_factory=dict)
    allow_bots: bool = False  # Whether to respond to other bots
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscordPolicy":
        """Parse policy from dict (loaded from JSON)."""
        # Parse DM policy
        dm_data = data.get("dm", {})
        dm = DiscordDmPolicy(
            enabled=dm_data.get("enabled", True),
            policy=dm_data.get("policy", "allowlist"),
            allow_from=dm_data.get("allowFrom", []),
        )
        
        # Parse guilds
        guilds: dict[str, DiscordGuildPolicy] = {}
        for guild_id, guild_data in data.get("guilds", {}).items():
            # Parse channels for this guild
            channels: dict[str, DiscordChannelPolicy] = {}
            for channel_id, channel_data in guild_data.get("channels", {}).items():
                if isinstance(channel_data, dict):
                    channels[channel_id] = DiscordChannelPolicy(
                        allow=channel_data.get("allow", False),
                        require_mention=channel_data.get("requireMention"),
                        users=channel_data.get("users", []),
                    )
                elif isinstance(channel_data, bool):
                    # Shorthand: "channel_id": true
                    channels[channel_id] = DiscordChannelPolicy(allow=channel_data)
            
            guilds[guild_id] = DiscordGuildPolicy(
                require_mention=guild_data.get("requireMention", True),
                users=guild_data.get("users", []),
                channels=channels,
            )
        
        return cls(
            dm=dm,
            group_policy=data.get("groupPolicy", "allowlist"),
            guilds=guilds,
            allow_bots=data.get("allowBots", False),
        )
    
    def check_dm_access(self, user_id: str) -> tuple[bool, str]:
        """Check if a user can DM the bot.
        
        Returns: (allowed, reason)
        """
        if not self.dm.enabled:
            return False, "DM disabled"
        
        if self.dm.policy == "disabled":
            return False, "DM policy disabled"
        
        if self.dm.policy == "open":
            if "*" in self.dm.allow_from or not self.dm.allow_from:
                return True, "DM open"
            if user_id in self.dm.allow_from:
                return True, "User in DM allowlist"
            return False, "User not in DM allowlist (open mode requires allowFrom)"
        
        if self.dm.policy == "allowlist":
            if user_id in self.dm.allow_from:
                return True, "User in DM allowlist"
            return False, "User not in DM allowlist"
        
        return False, f"Unknown DM policy: {self.dm.policy}"
    
    def check_guild_access(self, guild_id: str, channel_id: str, user_id: str) -> tuple[bool, str]:
        """Check if a user can use the bot in a guild channel.
        
        Returns: (allowed, reason)
        """
        if self.group_policy == "disabled":
            return False, "Guild access disabled"
        
        if self.group_policy == "open":
            return True, "Guild access open"
        
        # group_policy == "allowlist"
        # Check for specific guild config, then "*" wildcard
        guild_policy = self.guilds.get(guild_id) or self.guilds.get("*")
        
        if not guild_policy:
            return False, f"Guild {guild_id} not in allowlist"
        
        # Check guild-level user allowlist
        if guild_policy.users and user_id not in guild_policy.users:
            return False, f"User {user_id} not in guild allowlist"
        
        # Check channel allowlist (if channels are specified)
        if guild_policy.channels:
            channel_policy = guild_policy.channels.get(channel_id) or guild_policy.channels.get("*")
            
            if not channel_policy:
                return False, f"Channel {channel_id} not in guild's channel allowlist"
            
            if not channel_policy.allow:
                return False, f"Channel {channel_id} not allowed"
            
            # Check per-channel user allowlist
            if channel_policy.users and user_id not in channel_policy.users:
                return False, f"User {user_id} not in channel allowlist"
        
        return True, "Access granted"
    
    def get_require_mention(self, guild_id: str, channel_id: str) -> bool:
        """Get whether mention is required for a guild/channel."""
        guild_policy = self.guilds.get(guild_id) or self.guilds.get("*")
        
        if not guild_policy:
            return True  # Default: require mention
        
        # Check channel-specific setting
        if guild_policy.channels:
            channel_policy = guild_policy.channels.get(channel_id) or guild_policy.channels.get("*")
            if channel_policy and channel_policy.require_mention is not None:
                return channel_policy.require_mention
        
        return guild_policy.require_mention


@dataclass
class DiscordConfig:
    """Discord adapter configuration."""
    enabled: bool = False
    bot_token: str = ""
    kiro_cwd: str = ""  # Platform-specific working directory (optional)
    workspace_mode: str = ""  # Platform-specific mode (optional, fallback to global)
    policy: DiscordPolicy = field(default_factory=DiscordPolicy)


@dataclass
class KiroConfig:
    """Kiro CLI configuration."""
    path: str = "kiro"
    default_cwd: str = ""  # Default working directory if platform doesn't specify
    idle_timeout: int = 300  # seconds
    workspace_mode: str = "per_chat"  # Global default: "fixed" or "per_chat"
    auto_approve: bool = False  # Auto-approve all permission requests from Kiro


@dataclass
class Config:
    """Main configuration."""
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    kiro: KiroConfig = field(default_factory=KiroConfig)
    log_level: str = "INFO"
    debounce_discord: float = 1.5   # seconds, debounce window for Discord
    debounce_feishu: float = 1.0    # seconds, debounce window for Feishu
    debounce_default: float = 1.5   # seconds, fallback for other platforms
    pending_cap: int = 20           # max pending messages per chat

    def get_workspace_mode(self, platform: str) -> str:
        """Get workspace_mode for a platform (platform-specific or global default)."""
        if platform == "feishu" and self.feishu.workspace_mode:
            return self.feishu.workspace_mode
        elif platform == "discord" and self.discord.workspace_mode:
            return self.discord.workspace_mode
        return self.kiro.workspace_mode

    def get_kiro_cwd(self, platform: str) -> str | None:
        """Get base working directory for kiro-cli startup.
        
        Returns None in per_chat mode (use global ~/.kiro/ config).
        Returns platform cwd in fixed mode (use project-level .kiro/ config).
        """
        mode = self.get_workspace_mode(platform)
        if mode == "per_chat":
            # Don't pass cwd, let kiro-cli use global config
            return None
        
        # fixed mode: use platform-specific or default cwd
        if platform == "feishu" and self.feishu.kiro_cwd:
            return self.feishu.kiro_cwd
        elif platform == "discord" and self.discord.kiro_cwd:
            return self.discord.kiro_cwd
        return self.kiro.default_cwd or os.getcwd()

    def get_session_cwd(self, platform: str, chat_id: str) -> str:
        """Get working directory for a specific chat session.
        
        In 'fixed' mode: all chats share the platform's base directory.
        In 'per_chat' mode: each chat gets its own subdirectory under default_cwd.
        """
        mode = self.get_workspace_mode(platform)
        
        if mode == "per_chat":
            # Use default_cwd as base for per_chat subdirectories
            if platform == "feishu" and self.feishu.kiro_cwd:
                base = self.feishu.kiro_cwd
            elif platform == "discord" and self.discord.kiro_cwd:
                base = self.discord.kiro_cwd
            else:
                base = self.kiro.default_cwd or os.getcwd()
            
            # Sanitize chat_id for use in path
            safe_chat_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in chat_id)
            return os.path.join(base, safe_chat_id)
        
        # fixed mode: all sessions share the same directory
        if platform == "feishu" and self.feishu.kiro_cwd:
            return self.feishu.kiro_cwd
        elif platform == "discord" and self.discord.kiro_cwd:
            return self.discord.kiro_cwd
        return self.kiro.default_cwd or os.getcwd()


def _load_discord_policy(config_dir: str) -> DiscordPolicy:
    """Load Discord access policy.
    
    Priority:
    1. discord_policy.json exists → use it (fine-grained control)
    2. DISCORD_ADMIN_USER_ID set → build allowlist from env vars
    3. Neither → permissive default (DM disabled, guilds open with requireMention)
    """
    policy_file = Path(config_dir) / "discord_policy.json"
    
    # Priority 1: JSON file
    if policy_file.exists():
        try:
            with open(policy_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            policy = DiscordPolicy.from_dict(data)
            log.info("Loaded Discord policy from %s", policy_file)
            return policy
        except Exception as e:
            log.error("Failed to load discord_policy.json: %s", e)
            return DiscordPolicy()
    
    # Priority 2: Build from env vars
    admin_user_ids = [x.strip() for x in os.getenv("DISCORD_ADMIN_USER_ID", "").split(",") if x.strip()]
    guild_ids = [x.strip() for x in os.getenv("DISCORD_GUILD_ID", "").split(",") if x.strip()]
    require_mention = os.getenv("DISCORD_REQUIRE_MENTION", "true").lower() in ("true", "1", "yes")
    
    if admin_user_ids:
        log.info("No discord_policy.json, building policy from env "
                 "(admins=%s, guilds=%s, mention=%s)",
                 admin_user_ids, guild_ids or ["*"], require_mention)
        
        dm = DiscordDmPolicy(
            enabled=True,
            policy="allowlist",
            allow_from=admin_user_ids,
        )
        
        guilds: dict[str, DiscordGuildPolicy] = {}
        
        if guild_ids:
            for gid in guild_ids:
                guilds[gid] = DiscordGuildPolicy(
                    require_mention=require_mention,
                    users=admin_user_ids,
                    channels={"*": DiscordChannelPolicy(allow=True)},
                )
        else:
            # No specific guild: admin users in any guild
            guilds["*"] = DiscordGuildPolicy(
                require_mention=require_mention,
                users=admin_user_ids,
            )
        
        return DiscordPolicy(
            dm=dm,
            group_policy="allowlist",
            guilds=guilds,
            allow_bots=False,
        )
    
    # Priority 3: No JSON, no admin user → permissive default
    log.info("No discord_policy.json and no DISCORD_ADMIN_USER_ID set")
    log.info("Using default policy: DM disabled, guilds open with @mention required")
    log.info("Tip: Set DISCORD_ADMIN_USER_ID in .env for DM access and user allowlist, "
             "or create discord_policy.json for fine-grained control")
    
    return DiscordPolicy(
        dm=DiscordDmPolicy(enabled=False),
        group_policy="open",
        guilds={"*": DiscordGuildPolicy(require_mention=True)},
        allow_bots=False,
    )


def load_config() -> Config:
    """Load configuration from environment variables."""
    load_dotenv()
    
    # Determine config directory (where .env is located)
    config_dir = os.getcwd()

    feishu = FeishuConfig(
        enabled=os.getenv("FEISHU_ENABLED", "true").lower() in ("true", "1", "yes"),
        app_id=os.getenv("FEISHU_APP_ID", ""),
        app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        bot_name=os.getenv("FEISHU_BOT_NAME", ""),
        kiro_cwd=os.getenv("FEISHU_KIRO_CWD", ""),
        workspace_mode=_parse_workspace_mode(os.getenv("FEISHU_WORKSPACE_MODE"), ""),
    )

    # Load Discord policy from JSON file
    discord_policy = _load_discord_policy(config_dir)
    
    discord = DiscordConfig(
        enabled=os.getenv("DISCORD_ENABLED", "false").lower() in ("true", "1", "yes"),
        bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        kiro_cwd=os.getenv("DISCORD_KIRO_CWD", ""),
        workspace_mode=_parse_workspace_mode(os.getenv("DISCORD_WORKSPACE_MODE"), ""),
        policy=discord_policy,
    )

    kiro = KiroConfig(
        path=os.getenv("KIRO_PATH", "kiro-cli"),
        default_cwd=os.getenv("KIRO_CWD", os.getcwd()),
        idle_timeout=int(os.getenv("KIRO_IDLE_TIMEOUT", "300")),
        workspace_mode=_parse_workspace_mode(os.getenv("KIRO_WORKSPACE_MODE"), "per_chat"),
        auto_approve=os.getenv("KIRO_AUTO_APPROVE", "false").lower() in ("true", "1", "yes"),
    )

    return Config(
        feishu=feishu,
        discord=discord,
        kiro=kiro,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        debounce_discord=float(os.getenv("DEBOUNCE_DISCORD", "1.5")),
        debounce_feishu=float(os.getenv("DEBOUNCE_FEISHU", "1.0")),
        debounce_default=float(os.getenv("DEBOUNCE_DEFAULT", "1.5")),
        pending_cap=int(os.getenv("PENDING_CAP", "20")),
    )
