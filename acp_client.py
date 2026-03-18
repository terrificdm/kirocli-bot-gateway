"""ACP client for communicating with kiro-cli via JSON-RPC 2.0 over stdio."""

import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

# Max bytes per stdout line
_BUF_SIZE = 4 * 1024 * 1024


@dataclass
class ToolCallInfo:
    tool_call_id: str = ""
    title: str = ""
    kind: str = ""
    status: str = "pending"
    content: str = ""


@dataclass
class PromptResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop_reason: str = ""


@dataclass
class PermissionRequest:
    """Represents a permission request from Kiro."""
    session_id: str
    tool_call_id: str
    title: str
    options: list  # [{"optionId": "allow_once", "name": "Yes"}, ...]


# Permission handler type: (request) -> "allow_once" | "allow_always" | "deny" | None (timeout)
PermissionHandler = Callable[[PermissionRequest], str | None]


class ACPClient:
    def __init__(self, cli_path: str = "kiro-cli"):
        self._cli_path = cli_path
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._lock = threading.Lock()
        # pending request id -> threading.Event + result holder
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        # session_id -> list of notifications for current prompt
        self._session_updates: dict[str, list[dict]] = {}
        # session_id -> current prompt request id (for cancellation)
        self._active_prompts: dict[str, int] = {}
        # Permission request handler
        self._permission_handler: PermissionHandler | None = None
        # session_id -> available modes (from session/new response)
        self._session_modes: dict[str, dict] = {}
        # session_id -> available models (from session/new response)
        self._session_models: dict[str, dict] = {}
        # session_id -> available commands (from _kiro.dev/commands/available)
        self._session_commands: dict[str, list] = {}
        # Stream callbacks: session_id -> callable(chunk_text: str, accumulated_text: str)
        self._stream_callbacks: dict[str, Callable] = {}
        self._stream_accum: dict[str, list[str]] = {}
        self._running = False

    def on_permission_request(self, handler: PermissionHandler):
        """Register a handler for permission requests.
        
        Handler receives PermissionRequest and should return:
        - "allow_once" to allow this operation
        - "allow_always" to always allow this tool
        - "deny" to deny
        - None if timed out
        """
        self._permission_handler = handler

    # ── Lifecycle ──

    def start(self, cwd: str | None = None):
        """Start kiro-cli acp subprocess and initialize the connection.
        
        Args:
            cwd: Working directory for kiro-cli process. If specified, kiro-cli
                 will read workspace config (.kiro/settings/mcp.json) from this
                 directory instead of the current working directory.
        """
        self._proc = subprocess.Popen(
            [self._cli_path, "acp"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._running = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        result = self._send_request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            "clientInfo": {"name": "kirocli-bot-gateway", "version": "0.1.0"},
        })
        log.info("[ACP] Initialized: %s", json.dumps(result, ensure_ascii=False)[:200])
        return result

    def stop(self):
        """Gracefully stop the subprocess and all its children."""
        self._running = False
        if self._proc and self._proc.poll() is None:
            pid = self._proc.pid
            
            # First, find and kill child processes (kiro-cli-chat, MCP servers)
            self._kill_children(pid)
            
            # Then close stdin and wait for parent to exit
            self._proc.stdin.close()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        log.info("[ACP] Stopped")

    def _kill_children(self, parent_pid: int):
        """Kill all child processes of the given PID."""
        try:
            # Find child PIDs using pgrep
            result = subprocess.run(
                ["pgrep", "-P", str(parent_pid)],
                capture_output=True,
                text=True,
            )
            child_pids = result.stdout.strip().split('\n')
            for pid_str in child_pids:
                if pid_str:
                    child_pid = int(pid_str)
                    # Recursively kill grandchildren first
                    self._kill_children(child_pid)
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                        log.debug("[ACP] Sent SIGTERM to child PID %d", child_pid)
                    except ProcessLookupError:
                        pass  # Already dead
        except Exception as e:
            log.debug("[ACP] Error killing children: %s", e)

    def is_running(self) -> bool:
        return self._running and self._proc is not None and self._proc.poll() is None

    # ── High-level API ──

    def session_new(self, cwd: str) -> tuple[str, dict]:
        """Create a new ACP session, returns (sessionId, modes)."""
        # Note: MCP servers are configured via filesystem, not ACP params:
        # - Global: ~/.kiro/settings/mcp.json (always loaded)
        # - Workspace: {cwd}/.kiro/settings/mcp.json (loaded if exists)
        # Skills work similarly: ~/.kiro/skills/ or {cwd}/.kiro/skills/
        result = self._send_request("session/new", {
            "cwd": cwd,
            "mcpServers": [],  # Required field, but kiro reads from .kiro/ config files
        })
        session_id = result.get("sessionId", "")
        if not session_id:
            raise RuntimeError(f"session/new returned no sessionId: {result}")
        
        # Store available modes for this session
        modes = result.get("modes", {})
        self._session_modes[session_id] = modes
        
        # Store available models for this session
        models = result.get("models", {})
        self._session_models[session_id] = models
        
        log.info("[ACP] New session: %s, modes: %s, models: %s", 
                 session_id, 
                 list(modes.keys()) if modes else "none",
                 models.get("currentModelId", "none"))
        return session_id, modes

    def session_load(self, session_id: str, cwd: str) -> dict:
        """Load an existing session by ID."""
        result = self._send_request("session/load", {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": [],  # Required field, but kiro reads from .kiro/ config files
        })
        
        # Update modes if returned
        modes = result.get("modes", {})
        if modes:
            self._session_modes[session_id] = modes
        
        log.info("[ACP] Loaded session: %s", session_id)
        return result

    def get_session_modes(self, session_id: str) -> dict:
        """Get available modes for a session."""
        return self._session_modes.get(session_id, {})

    def session_set_mode(self, session_id: str, mode_id: str) -> dict:
        """Switch agent mode for a session."""
        result = self._send_request("session/set_mode", {
            "sessionId": session_id,
            "modeId": mode_id,
        })
        # Update local cache
        if session_id in self._session_modes:
            self._session_modes[session_id]["currentModeId"] = mode_id
        log.info("[ACP] Set mode to '%s' for session: %s", mode_id, session_id)
        return result

    def session_set_model(self, session_id: str, model_id: str) -> dict:
        """Change the model for a session."""
        result = self._send_request("session/set_model", {
            "sessionId": session_id,
            "modelId": model_id,  # Kiro uses modelId, not model
        }, timeout=15)
        # Update local cache
        if session_id in self._session_models:
            self._session_models[session_id]["currentModelId"] = model_id
        log.info("[ACP] Set model to '%s' for session: %s", model_id, session_id)
        return result

    def get_command_options(self, session_id: str, partial_command: str) -> list:
        """Get autocomplete options for a partial command (Kiro extension).
        
        Uses _kiro.dev/commands/options to get suggestions.
        Returns list of option strings, or empty list if unavailable.
        """
        try:
            result = self._send_request("_kiro.dev/commands/options", {
                "sessionId": session_id,
                "partialCommand": partial_command,
            }, timeout=10)
            options = result.get("options", [])
            log.info("[ACP] Command options for '%s': %s", partial_command, options)
            return options
        except Exception as e:
            log.warning("[ACP] Failed to get command options: %s", e)
            return []

    def get_model_options(self, session_id: str) -> list:
        """Get available models from session/new response."""
        models_data = self._session_models.get(session_id, {})
        available_models = models_data.get("availableModels", [])
        current_model = models_data.get("currentModelId", "")
        log.info("[ACP] Available models: %s, current: %s", available_models, current_model)
        return available_models
    
    def get_current_model(self, session_id: str) -> str:
        """Get current model ID for a session."""
        models_data = self._session_models.get(session_id, {})
        return models_data.get("currentModelId", "")

    def get_available_commands(self, session_id: str) -> list:
        """Get available commands for a session (from _kiro.dev/commands/available notification)."""
        return self._session_commands.get(session_id, [])

    @staticmethod
    def _detect_image_mime(b64_data: str) -> str | None:
        """Detect real image MIME type from base64 magic bytes.

        Platforms (e.g. Discord) sometimes report wrong content-type.
        Kiro supports: JPEG, PNG, GIF, WebP.
        """
        if b64_data.startswith("iVBORw"):       # PNG  \x89PNG
            return "image/png"
        elif b64_data.startswith("/9j/"):        # JPEG \xFF\xD8\xFF
            return "image/jpeg"
        elif b64_data.startswith("R0lGOD"):      # GIF  GIF8
            return "image/gif"
        elif b64_data.startswith("UklGR"):       # WebP RIFF...WEBP
            return "image/webp"
        return None

    def session_prompt(self, session_id: str, text: str, images: list[tuple[str, str]] | None = None, timeout: float = 300, on_stream: Callable[[str, str], None] | None = None) -> PromptResult:
        """Send a prompt and collect the full response (blocking).
        
        Args:
            session_id: Session ID
            text: Text content
            images: List of (base64_data, mime_type) tuples
            timeout: Timeout in seconds
            on_stream: Optional callback(chunk_text, accumulated_text) called on each text chunk
        """
        # Prepare collection state for this session
        self._session_updates[session_id] = []
        
        # Register stream callback if provided
        if on_stream:
            self._stream_callbacks[session_id] = on_stream
            self._stream_accum[session_id] = []

        # Track active prompt for cancellation
        req_id = self._next_id()
        self._active_prompts[session_id] = req_id

        try:
            # Build prompt content blocks
            prompt_content = []
            
            # Add images first (Kiro supports promptCapabilities.image: true)
            # ACP spec: {"type": "image", "data": "<base64>", "mimeType": "image/jpeg"}
            if images:
                for b64_data, mime_type in images:
                    # Detect real MIME from magic bytes — platforms may report wrong type
                    detected = self._detect_image_mime(b64_data)
                    if detected and detected != mime_type:
                        log.info("[ACP] MIME corrected: %s -> %s", mime_type, detected)
                        mime_type = detected
                    prompt_content.append({
                        "type": "image",
                        "data": b64_data,
                        "mimeType": mime_type,
                    })
                    log.info("[ACP] Adding image: %s, base64 len=%d", mime_type, len(b64_data))
            
            # Add text content
            # NOTE: Kiro requires at least one text block even for image-only messages.
            # Sending only images without text causes "Internal error" from Kiro.
            # Workaround: add "?" as minimal text when user sends image without text.
            if text:
                prompt_content.append({"type": "text", "text": text})
            elif images:
                prompt_content.append({"type": "text", "text": "?"})
            
            # Need at least one content block
            if not prompt_content:
                prompt_content.append({"type": "text", "text": ""})
            
            # Note: Kiro uses "prompt" instead of "content" (differs from ACP spec)
            result = self._send_request_with_id("session/prompt", {
                "sessionId": session_id,
                "prompt": prompt_content,
            }, req_id, timeout=timeout)

            return self._build_prompt_result(session_id, result)
        finally:
            self._active_prompts.pop(session_id, None)
            self._stream_callbacks.pop(session_id, None)
            self._stream_accum.pop(session_id, None)

    def session_cancel(self, session_id: str):
        """Cancel the current operation for a session."""
        if session_id not in self._active_prompts:
            log.warning("[ACP] No active prompt to cancel for session: %s", session_id)
            return

        # Send cancel notification (no response expected)
        msg = {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        }
        data = json.dumps(msg, ensure_ascii=False) + "\n"
        log.info("[ACP] Cancelling session: %s", session_id)
        log.debug("[ACP] >>> %s", data.strip())
        self._proc.stdin.write(data.encode())
        self._proc.stdin.flush()

    # ── Internal: JSON-RPC transport ──

    def _next_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _send_request(self, method: str, params: dict, timeout: float = 300) -> dict:
        return self._send_request_with_id(method, params, self._next_id(), timeout)

    def _send_request_with_id(self, method: str, params: dict, req_id: int, timeout: float = 300) -> dict:
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        data = json.dumps(msg, ensure_ascii=False) + "\n"

        evt = threading.Event()
        holder: list = []  # [result_dict] or [None, error_dict]
        self._pending[req_id] = (evt, holder)

        log.info("[ACP] >>> SENDING: %s", data.strip())
        self._proc.stdin.write(data.encode())
        self._proc.stdin.flush()

        if not evt.wait(timeout=timeout):
            self._pending.pop(req_id, None)
            raise TimeoutError(f"Request {method} (id={req_id}) timed out")

        self._pending.pop(req_id, None)
        if len(holder) == 2 and holder[0] is None:
            err = holder[1]
            raise RuntimeError(f"RPC error {err.get('code')}: {err.get('message')}")
        return holder[0] if holder else {}

    # ── Internal: read loops ──

    def _read_loop(self):
        while self._running:
            try:
                line = self._proc.stdout.readline(_BUF_SIZE)
                if not line:
                    break
                self._handle_line(line.decode(errors="replace").strip())
            except Exception as e:
                if self._running:
                    log.error("[ACP] Read error: %s", e)
                break
        log.info("[ACP] Read loop exited")
        self._running = False

    def _read_stderr(self):
        while self._running:
            try:
                line = self._proc.stderr.readline()
                if not line:
                    break
                log.debug("[ACP stderr] %s", line.decode(errors="replace").strip())
            except Exception:
                break

    def _handle_line(self, line: str):
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.warning("[ACP] Non-JSON line: %s", line[:200])
            return

        log.info("[ACP] <<< RECEIVED: %s", line[:500])

        msg_id = msg.get("id")
        method = msg.get("method")

        # Response to a pending request (has "id" and "result" or "error", no "method")
        if msg_id is not None and method is None and (msg.get("result") is not None or msg.get("error") is not None):
            pending = self._pending.get(msg_id)
            if pending:
                evt, holder = pending
                if msg.get("error"):
                    holder.append(None)
                    holder.append(msg["error"])
                else:
                    holder.append(msg.get("result", {}))
                evt.set()
            return

        # Request from agent (has "id" and "method") - e.g. session/request_permission
        if msg_id is not None and method:
            params = msg.get("params", {})
            if method == "session/request_permission":
                self._handle_permission_request(msg_id, params)
            return

        # Notification (has "method" but no "id")
        if method and msg_id is None:
            params = msg.get("params", {})
            session_id = params.get("sessionId", "")

            if method == "session/update" and session_id:
                update = params.get("update", {})
                updates = self._session_updates.get(session_id)
                if updates is not None:
                    updates.append(update)
                
                # Stream callback for text chunks
                if update.get("sessionUpdate") == "agent_message_chunk":
                    content = update.get("content", {})
                    if isinstance(content, dict) and content.get("type") == "text":
                        chunk_text = content.get("text", "")
                        cb = self._stream_callbacks.get(session_id)
                        accum = self._stream_accum.get(session_id)
                        if cb and accum is not None and chunk_text:
                            accum.append(chunk_text)
                            try:
                                cb(chunk_text, "".join(accum))
                            except Exception as e:
                                log.debug("[ACP] Stream callback error: %s", e)
            
            elif method == "_kiro.dev/commands/available":
                # Store available commands for this session
                commands = params.get("commands", [])
                if session_id and commands:
                    self._session_commands[session_id] = commands
                    log.info("[ACP] Received %d commands for session %s", len(commands), session_id)
                    # Print full command details
                    for cmd in commands:
                        log.info("[ACP] Command: %s", json.dumps(cmd, ensure_ascii=False))

    def _handle_permission_request(self, msg_id, params: dict):
        """Handle permission request from Kiro."""
        session_id = params.get("sessionId", "")
        tool_call = params.get("toolCall", {})
        tool_call_id = tool_call.get("toolCallId", "")
        title = tool_call.get("title", "Unknown operation")
        options = params.get("options", [])

        log.info("[ACP] Permission request for: %s", title)

        # If no handler registered, auto-approve (backward compatible)
        if self._permission_handler is None:
            log.info("[ACP] No permission handler, auto-approving: %s", title)
            self._send_permission_response(msg_id, session_id, "allow_once")
            return

        # Create permission request object
        request = PermissionRequest(
            session_id=session_id,
            tool_call_id=tool_call_id,
            title=title,
            options=options,
        )

        # Call handler in a separate thread to not block the read loop
        def handle_async():
            try:
                decision = self._permission_handler(request)
                if decision:
                    self._send_permission_response(msg_id, session_id, decision)
                else:
                    # Timeout or cancelled - deny
                    log.warning("[ACP] Permission request timed out, denying: %s", title)
                    self._send_permission_response(msg_id, session_id, "deny")
            except Exception as e:
                log.error("[ACP] Permission handler error: %s", e)
                self._send_permission_response(msg_id, session_id, "deny")

        threading.Thread(target=handle_async, daemon=True).start()

    def _send_permission_response(self, msg_id, session_id: str, option_id: str):
        """Send permission response to Kiro."""
        if option_id == "deny":
            # Send cancelled outcome
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "outcome": {"outcome": "cancelled"}
                }
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "outcome": {"outcome": "selected", "optionId": option_id}
                }
            }
        data = json.dumps(response, ensure_ascii=False) + "\n"
        log.debug("[ACP] >>> %s", data.strip())
        self._proc.stdin.write(data.encode())
        self._proc.stdin.flush()

    # ── Internal: result building ──

    def _build_prompt_result(self, session_id: str, rpc_result: dict) -> PromptResult:
        updates = self._session_updates.pop(session_id, [])

        result = PromptResult(stop_reason=rpc_result.get("stopReason", ""))
        text_parts = []
        tool_calls: dict[str, ToolCallInfo] = {}

        for update in updates:
            st = update.get("sessionUpdate", "")
            if st == "agent_message_chunk":
                content = update.get("content", {})
                if isinstance(content, dict) and content.get("type") == "text":
                    text_parts.append(content.get("text", ""))
            elif st == "tool_call":
                tc_id = update.get("toolCallId", "")
                tool_calls[tc_id] = ToolCallInfo(
                    tool_call_id=tc_id,
                    title=update.get("title", ""),
                    kind=update.get("kind", ""),
                    status=update.get("status", "pending"),
                )
            elif st == "tool_call_update":
                tc_id = update.get("toolCallId", "")
                tc = tool_calls.get(tc_id)
                if tc:
                    tc.status = update.get("status", tc.status)
                    # Update title if provided
                    if update.get("title"):
                        tc.title = update.get("title")
                    # Extract text content if present
                    for c in update.get("content", []):
                        if isinstance(c, dict):
                            inner = c.get("content", {})
                            if isinstance(inner, dict) and inner.get("type") == "text":
                                tc.content = inner.get("text", "")

        result.text = "".join(text_parts)
        result.tool_calls = list(tool_calls.values())
        return result
