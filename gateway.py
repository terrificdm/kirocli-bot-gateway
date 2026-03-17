"""Gateway: connects chat adapters to Kiro CLI via ACP protocol.

Platform-agnostic gateway that works with any ChatAdapter implementation.
Each platform gets its own Kiro CLI instance for fault isolation.
workspace_mode only affects session working directories, not Kiro CLI instances.
"""

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

from adapters.base import ChatAdapter, ChatType, IncomingMessage, CardHandle
from acp_client import ACPClient, PromptResult, PermissionRequest
from config import Config

log = logging.getLogger(__name__)

# Permission request timeout (seconds)
_PERMISSION_TIMEOUT = 60


def format_response(result: PromptResult) -> str:
    """Format Kiro's response with tool call info."""
    parts = []

    # Show tool calls
    for tc in result.tool_calls:
        icon = {"fs": "📄", "edit": "📝", "terminal": "⚡", "other": "🔧"}.get(tc.kind, "🔧")
        if result.stop_reason == "refusal" and tc.status != "completed":
            status_icon = "🚫"
        else:
            status_icon = {"completed": "✅", "failed": "❌"}.get(tc.status, "⏳")
        line = f"{icon} {tc.title} {status_icon}"
        parts.append(line)

    if parts:
        parts.append("")

    if result.stop_reason == "refusal":
        if result.text:
            parts.append(result.text)
        else:
            parts.append("🚫 Operation cancelled")
        parts.append("")
        parts.append("💬 You can continue the conversation")
    elif result.text:
        parts.append(result.text)

    return "\n".join(parts) if parts else "(No response)"


@dataclass
class ChatContext:
    """Context for a chat conversation."""
    chat_id: str
    platform: str
    session_id: str | None = None
    mode_id: str = ""  # Remember agent selection across session_load


class Gateway:
    """Platform-agnostic gateway between chat adapters and Kiro CLI.
    
    Each platform gets its own Kiro CLI instance for:
    - Fault isolation (one crash doesn't affect others)
    - Independent idle timeout
    - Platform-specific working directories
    
    workspace_mode affects session working directories:
    - fixed: all sessions share the same directory
    - per_chat: each session gets its own subdirectory
    """

    def __init__(self, config: Config, adapters: list[ChatAdapter]):
        self._config = config
        self._adapters = adapters
        self._adapter_map: dict[str, ChatAdapter] = {a.platform_name: a for a in adapters}
        
        # Per-platform ACP clients: platform -> ACPClient
        self._acp_clients: dict[str, ACPClient] = {}
        self._acp_lock = threading.Lock()
        
        # Per-platform last activity time: platform -> timestamp
        self._last_activity: dict[str, float] = {}
        
        # Chat context: "platform:chat_id" -> ChatContext
        self._contexts: dict[str, ChatContext] = {}
        self._contexts_lock = threading.Lock()
        
        # Processing state: "platform:chat_id" -> True if processing
        self._processing: dict[str, bool] = {}
        self._processing_lock = threading.Lock()
        
        # Pending messages for debounce + collect: key -> [(text, images)]
        self._pending_messages: dict[str, list[tuple[str, list | None]]] = {}
        self._pending_lock = threading.Lock()
        # Reply target: key -> message_id (for group chat reply, feishu only)
        self._reply_targets: dict[str, str] = {}
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._DEBOUNCE_BY_PLATFORM = {
            "discord": config.debounce_discord,
            "feishu": config.debounce_feishu,
        }
        self._DEBOUNCE_DEFAULT = config.debounce_default
        self._PENDING_CAP = config.pending_cap
        
        # Pending permission requests: "platform:chat_id" -> (event, result_holder)
        self._pending_permissions: dict[str, tuple[threading.Event, list]] = {}
        self._pending_permissions_lock = threading.Lock()
        
        # Active card handles: "platform:chat_id" -> CardHandle (for permission UI reuse)
        self._active_cards: dict[str, CardHandle] = {}
        
        # session_id -> "platform:chat_id" mapping
        self._session_to_key: dict[str, str] = {}
        
        # Idle checker
        self._idle_checker_stop = threading.Event()
        self._idle_checker_thread: threading.Thread | None = None

    def _make_key(self, platform: str, chat_id: str) -> str:
        """Create unique key for platform:chat_id combination."""
        return f"{platform}:{chat_id}"

    def start(self):
        """Start the gateway and all adapters."""
        log.info("[Gateway] Starting with per-platform Kiro CLI instances (workspace_mode=%s)", 
                 self._config.kiro.workspace_mode)

        # Start idle checker
        self._idle_checker_stop.clear()
        self._idle_checker_thread = threading.Thread(target=self._idle_checker_loop, daemon=True)
        self._idle_checker_thread.start()

        # Setup graceful shutdown
        def shutdown(sig, frame):
            log.info("[Gateway] Shutting down...")
            self._idle_checker_stop.set()
            # Cancel all debounce timers
            with self._pending_lock:
                for timer in self._debounce_timers.values():
                    timer.cancel()
                self._debounce_timers.clear()
            self._stop_all_acp()
            for adapter in self._adapters:
                adapter.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Start adapters
        if not self._adapters:
            log.error("[Gateway] No adapters configured")
            return

        # Setup slash command handler for Discord adapter
        for adapter in self._adapters:
            if adapter.platform_name == "discord" and hasattr(adapter, "set_slash_handler"):
                adapter.set_slash_handler(self._handle_slash_command)
                log.info("[Gateway] Slash command handler set for Discord")

        # Start all but last adapter in threads
        for adapter in self._adapters[:-1]:
            log.info("[Gateway] Starting %s adapter in thread...", adapter.platform_name)
            t = threading.Thread(
                target=adapter.start,
                args=(self._on_message,),
                daemon=True,
            )
            t.start()

        # Start last adapter in main thread (blocking)
        last_adapter = self._adapters[-1]
        log.info("[Gateway] Starting %s adapter (blocking)...", last_adapter.platform_name)
        last_adapter.start(self._on_message)

    def _start_acp(self, platform: str) -> ACPClient:
        """Start ACP client for a specific platform if not running."""
        with self._acp_lock:
            if platform in self._acp_clients and self._acp_clients[platform].is_running():
                return self._acp_clients[platform]
            
            log.info("[Gateway] [%s] Starting kiro-cli...", platform)
            acp = ACPClient(cli_path=self._config.kiro.path)
            
            # Get cwd based on workspace_mode:
            # - fixed mode: pass platform cwd (loads project-level .kiro/ config)
            # - per_chat mode: pass None (loads global ~/.kiro/ config)
            cwd = self._config.get_kiro_cwd(platform)
            acp.start(cwd=cwd)
            # Use default argument to capture platform value (avoid closure issue)
            if not self._config.kiro.auto_approve:
                acp.on_permission_request(lambda req, p=platform: self._handle_permission(req, p))
            else:
                log.info("[Gateway] [%s] Auto-approve enabled, skipping permission handler", platform)
            
            self._acp_clients[platform] = acp
            self._last_activity[platform] = time.time()
            
            # Clear sessions for this platform
            with self._contexts_lock:
                keys_to_remove = [k for k in self._contexts if k.startswith(f"{platform}:")]
                for k in keys_to_remove:
                    ctx = self._contexts.pop(k, None)
                    if ctx and ctx.session_id:
                        self._session_to_key.pop(ctx.session_id, None)
            
            mode = self._config.get_workspace_mode(platform)
            log.info("[Gateway] [%s] kiro-cli started (mode=%s, cwd=%s)", platform, mode, cwd)
            return acp

    def _stop_acp(self, platform: str):
        """Stop ACP client for a specific platform."""
        with self._acp_lock:
            acp = self._acp_clients.pop(platform, None)
            self._last_activity.pop(platform, None)
            
        if acp is not None:
            log.info("[Gateway] [%s] Stopping kiro-cli...", platform)
            acp.stop()
            
            # Clear sessions for this platform
            with self._contexts_lock:
                keys_to_remove = [k for k in self._contexts if k.startswith(f"{platform}:")]
                for k in keys_to_remove:
                    ctx = self._contexts.pop(k, None)
                    if ctx and ctx.session_id:
                        self._session_to_key.pop(ctx.session_id, None)
            
            log.info("[Gateway] [%s] kiro-cli stopped", platform)

    def _stop_all_acp(self):
        """Stop all ACP clients."""
        with self._acp_lock:
            platforms = list(self._acp_clients.keys())
        for platform in platforms:
            self._stop_acp(platform)

    def _ensure_acp(self, platform: str) -> ACPClient:
        """Ensure ACP client is running for a platform."""
        acp = self._start_acp(platform)
        with self._acp_lock:
            self._last_activity[platform] = time.time()
        return acp

    def _get_acp(self, platform: str) -> ACPClient | None:
        """Get ACP client for a platform if running."""
        with self._acp_lock:
            acp = self._acp_clients.get(platform)
            if acp and acp.is_running():
                return acp
        return None

    def _idle_checker_loop(self):
        """Background thread for per-platform idle timeout."""
        idle_timeout = self._config.kiro.idle_timeout
        if idle_timeout <= 0:
            log.info("[Gateway] Idle timeout disabled")
            return
        
        while not self._idle_checker_stop.wait(timeout=30):
            platforms_to_stop = []
            
            with self._acp_lock:
                now = time.time()
                for platform, last in self._last_activity.items():
                    idle_time = now - last
                    if idle_time > idle_timeout:
                        if platform in self._acp_clients and self._acp_clients[platform].is_running():
                            log.info("[Gateway] [%s] Idle timeout (%.0fs)", platform, idle_time)
                            platforms_to_stop.append(platform)
            
            # Stop outside the lock
            for platform in platforms_to_stop:
                self._stop_acp(platform)

    def _get_adapter(self, platform: str) -> ChatAdapter | None:
        """Get adapter by platform name."""
        return self._adapter_map.get(platform)

    def _send_text(self, platform: str, chat_id: str, text: str, reply_to: str = ""):
        """Send text message via appropriate adapter."""
        adapter = self._get_adapter(platform)
        if adapter:
            adapter.send_text(chat_id, text, reply_to=reply_to)

    def _send_text_nowait(self, platform: str, chat_id: str, text: str):
        """Send text message without blocking (for command responses).
        
        Falls back to send_text if adapter doesn't support nowait.
        """
        adapter = self._get_adapter(platform)
        if adapter:
            if hasattr(adapter, 'send_text_nowait'):
                adapter.send_text_nowait(chat_id, text)
            else:
                adapter.send_text(chat_id, text)

    def _send_card(self, platform: str, chat_id: str, content: str, title: str = "", reply_to: str = "") -> CardHandle | None:
        """Send card via appropriate adapter."""
        adapter = self._get_adapter(platform)
        if adapter:
            return adapter.send_card(chat_id, content, title, reply_to=reply_to)
        return None

    def _update_card(self, platform: str, handle: CardHandle, content: str, title: str = "") -> bool:
        """Update card via appropriate adapter."""
        adapter = self._get_adapter(platform)
        if adapter:
            return adapter.update_card(handle, content, title)
        return False

    def _handle_permission(self, request: PermissionRequest, platform: str) -> str | None:
        """Handle permission request from Kiro."""
        session_id = request.session_id
        key = self._session_to_key.get(session_id)
        if not key:
            log.warning("[Gateway] [%s] No chat found for session %s, auto-denying", platform, session_id)
            return "deny"

        _, chat_id = key.split(":", 1)
        
        msg = f"🔐 **Kiro requests permission:**\n\n"
        msg += f"📋 {request.title}\n\n"
        msg += "Reply: **y**(allow) / **n**(deny) / **t**(trust)\n"
        msg += f"⏱️ Auto-deny in {_PERMISSION_TIMEOUT}s"

        # Prefer updating the active card (Feishu) over sending a new message (Discord)
        card = self._active_cards.get(key)
        if card:
            self._update_card(platform, card, msg)
        else:
            self._send_text(platform, chat_id, msg)
        log.info("[Gateway] [%s] Sent permission request: %s", platform, request.title)

        evt = threading.Event()
        result_holder: list = []

        with self._pending_permissions_lock:
            self._pending_permissions[key] = (evt, result_holder)

        try:
            if evt.wait(timeout=_PERMISSION_TIMEOUT):
                if result_holder:
                    decision = result_holder[0]
                    log.info("[Gateway] [%s] User decision: %s", platform, decision)
                    # Send new card below user's reply for the result
                    if card:
                        new_card = self._send_card(platform, chat_id, "🤔 Processing...")
                        if new_card:
                            self._active_cards[key] = new_card
                    return decision
            
            # Timeout
            if card:
                self._update_card(platform, card, "⏱️ Timeout, auto-denied")
            else:
                self._send_text(platform, chat_id, "⏱️ Timeout, auto-denied")
            log.warning("[Gateway] [%s] Permission timed out: %s", platform, request.title)
            return "deny"
        finally:
            with self._pending_permissions_lock:
                self._pending_permissions.pop(key, None)

    def _on_message(self, msg: IncomingMessage):
        """Handle incoming message from any adapter."""
        platform = msg.raw.get("_platform", "")
        if not platform:
            log.warning("[Gateway] Message missing _platform in raw data")
            if self._adapters:
                platform = self._adapters[0].platform_name
            else:
                return
        
        chat_id = msg.chat_id
        text = msg.text.strip()
        text_lower = text.lower()
        images = msg.images
        key = self._make_key(platform, chat_id)

        if images:
            log.info("[Gateway] [%s] Received %d image(s)", key, len(images))

        # Check for permission response
        with self._pending_permissions_lock:
            pending = self._pending_permissions.get(key)
        
        if pending:
            evt, result_holder = pending
            if text_lower in ('y', 'yes', 'ok'):
                result_holder.append("allow_once")
                evt.set()
                return
            elif text_lower in ('n', 'no'):
                result_holder.append("deny")
                evt.set()
                return
            elif text_lower in ('t', 'trust', 'always'):
                result_holder.append("allow_always")
                evt.set()
                return
            else:
                self._send_text_nowait(platform, chat_id, "⚠️ Please reply y/n/t")
                return

        # Cancel command
        if text_lower in ("cancel", "stop"):
            self._handle_cancel(platform, chat_id, key)
            return

        # Commands (/ prefix)
        if text.startswith("/"):
            self._handle_command(platform, chat_id, key, text)
            return

        # Store in pending buffer for debounce + collect
        # Save last message_id for group chat reply (feishu + discord)
        if msg.chat_type == ChatType.GROUP:
            raw_msg_id = msg.raw.get("message_id", "")
            if raw_msg_id:
                self._reply_targets[key] = raw_msg_id
        with self._pending_lock:
            if key not in self._pending_messages:
                self._pending_messages[key] = []
            pending = self._pending_messages[key]
            if len(pending) >= self._PENDING_CAP:
                self._send_text_nowait(platform, chat_id,
                                       f"⚠️ Too many pending messages (max {self._PENDING_CAP})")
                return
            pending.append((text, images))

        with self._processing_lock:
            is_busy = self._processing.get(key, False)

        if is_busy:
            # Currently processing — send typing indicator, loop will drain pending
            adapter = self._adapter_map.get(platform)
            if adapter:
                adapter.send_typing(chat_id)
        else:
            # Idle — send immediate typing feedback, then start/reset debounce
            adapter = self._adapter_map.get(platform)
            if adapter:
                adapter.send_typing(chat_id)
            self._reset_debounce(platform, chat_id, key)

    def _handle_cancel(self, platform: str, chat_id: str, key: str):
        """Handle cancel command.
        
        Uses _send_text_nowait to avoid deadlocking Discord's event loop
        (this is called synchronously from the adapter's message handler).
        """
        # Cancel debounce timer and clear pending messages
        pending_cleared = 0
        with self._pending_lock:
            timer = self._debounce_timers.pop(key, None)
            if timer:
                timer.cancel()
            pending_cleared = len(self._pending_messages.pop(key, []))
        
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None

        if not session_id:
            if pending_cleared:
                self._send_text_nowait(platform, chat_id, f"🗑️ Cleared {pending_cleared} queued message(s)")
            else:
                self._send_text_nowait(platform, chat_id, "❌ No active session")
            return

        acp = self._get_acp(platform)
        if not acp:
            if pending_cleared:
                self._send_text_nowait(platform, chat_id, f"🗑️ Cleared {pending_cleared} queued message(s)")
            else:
                self._send_text_nowait(platform, chat_id, "❌ Kiro is not running")
            return

        try:
            acp.session_cancel(session_id)
            msg = "⏹️ Cancel request sent"
            if pending_cleared:
                msg += f"\n🗑️ Cleared {pending_cleared} queued message(s)"
            self._send_text_nowait(platform, chat_id, msg)
        except Exception as e:
            log.error("[Gateway] [%s] Cancel failed: %s", key, e)
            self._send_text_nowait(platform, chat_id, f"❌ Cancel failed: {e}")

    def _handle_command(self, platform: str, chat_id: str, key: str, text: str):
        """Handle slash commands."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/agent":
            self._handle_agent_command(platform, chat_id, key, arg)
        elif cmd == "/model":
            self._handle_model_command(platform, chat_id, key, arg)
        elif cmd == "/help":
            self._handle_help_command(platform, chat_id)
        else:
            self._send_text_nowait(platform, chat_id, f"❓ Unknown command: {cmd}\n💡 Send /help for available commands")

    def _handle_agent_command(self, platform: str, chat_id: str, key: str, mode_arg: str):
        """Handle /agent command (text-based)."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None

        acp = self._get_acp(platform)
        response = self._get_agent_response(acp, session_id, mode_arg)
        self._send_text_nowait(platform, chat_id, response)

    def _handle_model_command(self, platform: str, chat_id: str, key: str, model_arg: str):
        """Handle /model command (text-based)."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None

        acp = self._get_acp(platform)
        response = self._get_model_response(acp, session_id, model_arg)
        self._send_text_nowait(platform, chat_id, response)

    def _handle_help_command(self, platform: str, chat_id: str):
        """Show help."""
        self._send_text_nowait(platform, chat_id, self._get_help_text())

    def _handle_slash_command(self, platform: str, chat_id: str, cmd: str, args: str) -> str | None:
        """Handle slash command from Discord adapter.
        
        Returns the response text to be sent as interaction followup.
        This is called synchronously from the adapter.
        """
        key = self._make_key(platform, chat_id)
        
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None
        
        acp = self._get_acp(platform)
        
        if cmd == "help":
            return self._get_help_text()
        
        if cmd == "agent":
            return self._get_agent_response(acp, session_id, args)
        
        if cmd == "model":
            return self._get_model_response(acp, session_id, args)
        
        return f"❓ Unknown command: /{cmd}"
    
    def _get_help_text(self) -> str:
        """Get help text for slash commands."""
        return """📚 **Available Commands:**

**Agent:**
• /agent - List available agents
• /agent agent_name - Switch agent

**Model:**
• /model - List available models
• /model model_name - Switch model

**Other:**
• /help - Show this help"""
    
    def _get_agent_response(self, acp: ACPClient | None, session_id: str | None, args: str) -> str:
        """Get agent command response."""
        if not session_id:
            return "❌ No session yet. Send a message first."
        
        if not acp:
            return "❌ Kiro is not running"
        
        if not args:
            # List agents
            modes_data = acp.get_session_modes(session_id)
            if not modes_data:
                return "❓ No agent info available"
            
            current_mode = modes_data.get("currentModeId", "")
            available_modes = modes_data.get("availableModes", [])
            
            if not available_modes:
                return "❓ No agents available"
            
            lines = ["📋 **Available agents:**", ""]
            for mode in available_modes:
                mode_id = mode.get("id", "unknown")
                mode_name = mode.get("name", mode_id)
                marker = "▶️" if mode_id == current_mode else "•"
                lines.append(f"{marker} **{mode_name}**")
            
            lines.append("")
            lines.append("💡 Use /agent agent_name to switch")
            return "\n".join(lines)
        else:
            # Switch agent
            valid_ids = set()
            modes_data = acp.get_session_modes(session_id)
            if modes_data:
                for m in modes_data.get("availableModes", []):
                    if m.get("id"):
                        valid_ids.add(m["id"])
                    if m.get("name"):
                        valid_ids.add(m["name"])
            
            if valid_ids and args not in valid_ids:
                return f"❌ Invalid agent: {args}\n\n💡 Use /agent to see available agents"
            
            try:
                acp.session_set_mode(session_id, args)
                # Save mode selection for restoration after session_load
                key = self._session_to_key.get(session_id)
                if key:
                    with self._contexts_lock:
                        ctx = self._contexts.get(key)
                        if ctx:
                            ctx.mode_id = args
                return f"✅ Switched to agent: **{args}**"
            except Exception as e:
                return f"❌ Switch failed: {e}"
    
    def _get_model_response(self, acp: ACPClient | None, session_id: str | None, args: str) -> str:
        """Get model command response."""
        if not session_id:
            return "❌ No session yet. Send a message first."
        
        if not acp:
            return "❌ Kiro is not running"
        
        if not args:
            # List models
            options = acp.get_model_options(session_id)
            current_model = acp.get_current_model(session_id)
            
            if not options:
                if current_model:
                    return f"📊 **Current model:** {current_model}\n\n(No other models available)"
                return "❓ No model info available"
            
            lines = ["📋 **Available Models:**", ""]
            for opt in options:
                if isinstance(opt, dict):
                    model_id = opt.get("modelId", "") or opt.get("id", "")
                    model_name = opt.get("name", model_id)
                else:
                    model_id = str(opt)
                    model_name = model_id
                
                if model_id:
                    marker = "▶️" if model_id == current_model else "•"
                    if model_id == model_name:
                        lines.append(f"{marker} {model_id}")
                    else:
                        lines.append(f"{marker} {model_id} - {model_name}")
            
            lines.append("")
            if current_model:
                lines.append(f"**Current:** {current_model}")
            lines.append("💡 Use /model model_name to switch")
            return "\n".join(lines)
        else:
            # Switch model
            options = acp.get_model_options(session_id)
            valid_ids = set()
            if options:
                for opt in options:
                    if isinstance(opt, dict):
                        mid = opt.get("modelId", "") or opt.get("id", "")
                        if mid:
                            valid_ids.add(mid)
                    else:
                        valid_ids.add(str(opt))
            
            if valid_ids and args not in valid_ids:
                return f"❌ Invalid model: {args}\n\n💡 Use /model to see available models"
            
            try:
                acp.session_set_model(session_id, args)
                return f"✅ Switched to model: **{args}**"
            except Exception as e:
                return f"❌ Switch failed: {e}"

    def _reset_debounce(self, platform: str, chat_id: str, key: str):
        """Start or reset the debounce timer for a chat.
        
        Cancels any existing timer and starts a new one. When the timer fires,
        all pending messages are merged and processed as a single turn.
        """
        with self._pending_lock:
            old_timer = self._debounce_timers.get(key)
            if old_timer:
                old_timer.cancel()
            debounce_sec = self._DEBOUNCE_BY_PLATFORM.get(platform, self._DEBOUNCE_DEFAULT)
            timer = threading.Timer(
                debounce_sec,
                self._debounce_fire,
                args=(platform, chat_id, key),
            )
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _debounce_fire(self, platform: str, chat_id: str, key: str):
        """Called when debounce timer expires. Starts processing in a new thread."""
        with self._pending_lock:
            self._debounce_timers.pop(key, None)
        threading.Thread(
            target=self._process_message,
            args=(platform, chat_id, key),
            daemon=True,
        ).start()

    @staticmethod
    def _merge_messages(messages: list[tuple[str, list | None]]) -> tuple[str, list | None]:
        """Merge multiple pending messages into a single prompt.
        
        Single message is returned as-is. Multiple messages have their text
        joined with newlines and images concatenated.
        """
        if len(messages) == 1:
            return messages[0]

        texts = [text for text, _ in messages if text]
        all_images: list = []
        for _, images in messages:
            if images:
                all_images.extend(images)

        merged_text = "\n".join(texts)
        return merged_text, all_images or None

    def _process_message(self, platform: str, chat_id: str, key: str):
        """Process pending messages with collect semantics."""
        with self._processing_lock:
            if self._processing.get(key):
                return  # Another thread is already processing; it will drain pending
            self._processing[key] = True

        try:
            self._process_message_loop(platform, chat_id, key)
        finally:
            with self._processing_lock:
                self._processing[key] = False
            # Race condition fix: if new messages arrived while we were finishing,
            # kick off another debounce so they don't get stuck in pending.
            with self._pending_lock:
                if self._pending_messages.get(key):
                    self._reset_debounce(platform, chat_id, key)

    def _process_message_loop(self, platform: str, chat_id: str, key: str):
        """Drain and process pending messages in a loop.
        
        Each iteration merges all currently pending messages into one prompt.
        After processing, checks for new messages that arrived during the run.
        """
        while True:
            with self._pending_lock:
                messages = self._pending_messages.pop(key, [])
            if not messages:
                break

            text, images = self._merge_messages(messages)
            if len(messages) > 1:
                log.info("[Gateway] [%s] Merged %d messages into one prompt", key, len(messages))
            self._process_single_message(platform, chat_id, key, text, images)

    def _process_single_message(self, platform: str, chat_id: str, key: str, text: str, images: list[tuple[str, str]] | None = None):
        """Process a single message."""
        card_handle = None
        adapter = self._adapter_map.get(platform)
        
        # Streaming state
        _stream_lock = threading.Lock()
        _last_stream_update = [0.0]
        _STREAM_INTERVAL = 1.0  # seconds between card updates (Feishu rate limit safe)
        
        def _on_stream(chunk: str, accumulated: str):
            """Called from ACP read thread on each text chunk."""
            # Use _active_cards to get the current card (may change after permission approval)
            current_card = self._active_cards.get(key)
            if not current_card:
                return
            now = time.time()
            with _stream_lock:
                elapsed = now - _last_stream_update[0]
                if elapsed >= _STREAM_INTERVAL:
                    _last_stream_update[0] = now
                else:
                    return
            # Update card outside lock
            try:
                self._update_card(platform, current_card, accumulated + " ▌")
            except Exception as e:
                log.debug("[Gateway] [%s] Stream update error: %s", key, e)
        
        try:
            reply_to = self._reply_targets.pop(key, "")
            card_handle = self._send_card(platform, chat_id, "🤔 Thinking...", reply_to=reply_to)
            # Keep reply_to for platforms where send_card returns None (e.g., Discord)
            if not card_handle and reply_to:
                self._reply_targets[key] = reply_to
            
            # Store card handle for permission UI reuse
            if card_handle:
                self._active_cards[key] = card_handle
            
            # Start typing loop for platforms that don't use card updates (e.g., Discord)
            # Discord's send_card already sends one typing indicator, the loop continues it
            if adapter and not card_handle:
                adapter.start_typing_loop(chat_id)

            try:
                acp = self._ensure_acp(platform)
            except Exception as e:
                log.error("[Gateway] [%s] Failed to start kiro-cli: %s", platform, e)
                error_msg = f"❌ Failed to start Kiro: {e}"
                if card_handle:
                    self._update_card(platform, card_handle, error_msg)
                else:
                    self._send_text(platform, chat_id, error_msg)
                return

            session_id = self._get_or_create_session(platform, chat_id, key, acp)
            self._session_to_key[session_id] = key

            # Send to Kiro (with streaming for card-based platforms)
            stream_cb = _on_stream if card_handle else None
            max_retries = 3
            last_error: Exception | None = None
            for attempt in range(max_retries):
                try:
                    result = acp.session_prompt(session_id, text, images=images, on_stream=stream_cb)
                    break
                except RuntimeError as e:
                    last_error = e
                    error_str = str(e)
                    if "ValidationException" in error_str or "Internal error" in error_str:
                        if attempt < max_retries - 1:
                            log.warning("[Gateway] [%s] Transient error (attempt %d/%d): %s", platform, attempt + 1, max_retries, e)
                            time.sleep(1)
                            continue
                    raise
            else:
                raise last_error

            # Update activity
            with self._acp_lock:
                self._last_activity[platform] = time.time()

            response = format_response(result)
            final_card = self._active_cards.get(key) or card_handle
            if final_card:
                self._update_card(platform, final_card, response)
            else:
                final_reply_to = self._reply_targets.pop(key, "")
                self._send_text(platform, chat_id, response, reply_to=final_reply_to)

        except Exception as e:
            log.exception("[Gateway] [%s] Error: %s", platform, e)
            error_msg = str(e)
            if "cancelled" in error_msg.lower():
                error_text = "⏹️ Operation cancelled"
            else:
                error_text = f"❌ Error: {e}"
            
            error_card = self._active_cards.get(key) or card_handle
            if error_card:
                self._update_card(platform, error_card, error_text)
            else:
                self._send_text(platform, chat_id, error_text)
            
            with self._contexts_lock:
                self._contexts.pop(key, None)
            
            # Check if this platform's ACP died
            with self._acp_lock:
                acp = self._acp_clients.get(platform)
                if acp is not None and not acp.is_running():
                    log.warning("[Gateway] [%s] kiro-cli died, will restart on next message", platform)
                    self._acp_clients.pop(platform, None)
                    self._last_activity.pop(platform, None)
        
        finally:
            # Clean up active card reference
            self._active_cards.pop(key, None)
            # Always stop typing loop when done
            if adapter and not card_handle:
                adapter.stop_typing_loop(chat_id)

    def _get_or_create_session(self, platform: str, chat_id: str, key: str, acp: ACPClient) -> str:
        """Get or create ACP session for a chat."""
        # Get working directory based on workspace_mode (fixed or per_chat)
        work_dir = self._config.get_session_cwd(platform, chat_id)
        os.makedirs(work_dir, exist_ok=True)

        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx and ctx.session_id:
                # Session already in memory — just reuse it, no need to load.
                # session/load is only needed after kiro-cli restart to restore
                # from disk, but _start_acp clears _contexts on restart so we
                # never reach here with a stale session_id.
                log.info("[Gateway] [%s] Reusing session %s", key, ctx.session_id)
                return ctx.session_id

        session_id, modes = acp.session_new(work_dir)
        log.info("[Gateway] [%s] Created session %s (cwd: %s)", key, session_id, work_dir)

        with self._contexts_lock:
            self._contexts[key] = ChatContext(
                chat_id=chat_id,
                platform=platform,
                session_id=session_id,
            )
        self._session_to_key[session_id] = key
        return session_id
