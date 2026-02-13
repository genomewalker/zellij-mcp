#!/usr/bin/env python3
"""Zellij MCP Server - Full control of Zellij from Claude Code."""

import json
import subprocess
import os
import re
import asyncio
import time
import hashlib
import threading
import pty
import signal
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("zellij-mcp")

# Tools to HIDE from tools/list (still callable, just not listed)
# Goal: Expose only ~15 essential tools to save context tokens
HIDDEN_TOOLS = {
    # Agents (specialized)
    "agent_output", "agent_session", "spawn_agents", "stop_agent", "list_spawned_agents",
    # Jobs (specialized)
    "job_status", "job_submit",
    # SSH (specialized)
    "ssh_connect", "ssh_run",
    # REPL (specialized)
    "repl_execute", "repl_interrupt",
    # Named panes (advanced)
    "create_named_pane", "destroy_named_pane", "focus_pane_by_name", "list_named_panes",
    # Layout (advanced)
    "dump_layout", "swap_layout", "stack_panes",
    # Moving (advanced)
    "move_pane", "move_pane_backwards", "move_tab",
    # Scrolling (advanced)
    "scroll", "edit_scrollback",
    # Toggles (advanced)
    "toggle_embed_or_floating", "toggle_floating", "toggle_fullscreen",
    "toggle_pane_frames", "toggle_sync_tab",
    # Resizing (advanced)
    "resize_pane",
    # Rename (advanced)
    "rename_pane", "rename_session", "rename_tab",
    "undo_rename_pane", "undo_rename_tab",
    # Focus navigation (advanced)
    "focus_next_pane", "focus_previous_pane",
    # Waiting (advanced)
    "wait_for_idle", "wait_for_output",
    # Misc (advanced)
    "pipe", "send_keys", "write_chars", "search_pane", "tail_pane",
    "list_clients", "edit_file", "launch_plugin", "query_tab_names",
    "session_attach", "session_map",
}


# =============================================================================
# WORKSPACE TABS - Organized workspaces within current session
# =============================================================================
# NOTE: Previously used agent session isolation, but Zellij doesn't support
# creating panes in detached sessions. Now uses tab-based organization instead.
# Tabs like "agent-work", "shepherd", etc. keep agent work separate while
# staying in the attached session where pane operations actually work.

AGENT_SESSION = "zellij-agent"  # DEPRECATED: Kept for backwards compatibility
DEFAULT_WORKSPACE_TAB = "agent-work"  # Default tab for autonomous operations


def get_active_sessions() -> list[str]:
    """Get list of active zellij session names."""
    try:
        result = subprocess.run(
            ["zellij", "list-sessions", "-n"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Parse session name (first word before space/bracket)
            sessions = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    # Extract just the session name (first token)
                    name = line.strip().split()[0] if line.strip() else ""
                    if name:
                        sessions.append(name)
            return sessions
    except Exception:
        pass
    return []


_agent_session_lock = threading.Lock()

# Per-session locks for focus operations to prevent race conditions
_focus_locks: dict[str, threading.Lock] = {}
_focus_locks_lock = threading.Lock()


def get_focus_lock(session: str = None) -> threading.Lock:
    """Get or create a lock for focus operations on a session."""
    key = session or os.environ.get("ZELLIJ_SESSION_NAME", "_default")
    with _focus_locks_lock:
        if key not in _focus_locks:
            _focus_locks[key] = threading.Lock()
        return _focus_locks[key]


def ensure_agent_session() -> bool:
    """Ensure the agent session exists, create if needed. Thread-safe."""
    with _agent_session_lock:
        sessions = get_active_sessions()
        if AGENT_SESSION in sessions:
            return True

        # Create the agent session in detached mode
        try:
            result = subprocess.run(
                ["zellij", "-s", AGENT_SESSION, "options", "--detached"],
                capture_output=True, text=True, timeout=10
            )
            # Also try attach --create which works better
            if result.returncode != 0:
                subprocess.Popen(
                    ["zellij", "attach", "--create", AGENT_SESSION],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                time.sleep(1)  # Give it time to start
            # Re-check after creation
            return AGENT_SESSION in get_active_sessions()
        except Exception:
            return False


def get_agent_session() -> str:
    """Get the agent session name, ensuring it exists."""
    ensure_agent_session()
    return AGENT_SESSION


# =============================================================================
# PANE BRIDGE PLUGIN - Focus-free pane operations
# =============================================================================

def get_plugin_path() -> Optional[str]:
    """Get the path to the pane-bridge WASM plugin if installed."""
    paths = [
        os.path.expanduser("~/.local/share/zellij-mcp/plugins/zellij-pane-bridge.wasm"),
        os.path.join(os.path.dirname(__file__), "zellij-pane-bridge.wasm"),
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return None


_plugin_path_cache: Optional[str] = None
_plugin_available: Optional[bool] = None


def is_plugin_available() -> bool:
    """Check if the pane-bridge plugin is available."""
    global _plugin_path_cache, _plugin_available
    if _plugin_available is None:
        _plugin_path_cache = get_plugin_path()
        _plugin_available = _plugin_path_cache is not None
    return _plugin_available


def plugin_command(cmd: str, payload: dict = None, timeout: float = 5.0, session: str = None) -> dict:
    """Execute a command via the pane-bridge plugin.

    Returns dict with success/error/data fields.

    Note: zellij pipe outputs data but doesn't exit on its own. We use the
    shell `timeout` command to force termination after getting the output.

    Args:
        cmd: Plugin command name
        payload: JSON payload to send
        timeout: Timeout in seconds
        session: Target session (default: current session)
    """
    if not is_plugin_available():
        return {"success": False, "error": "pane-bridge plugin not installed"}

    plugin_url = f"file://{_plugin_path_cache}"
    payload_json = json.dumps(payload) if payload else "{}"

    try:
        # Escape single quotes in payload for shell safety
        escaped_payload = payload_json.replace("'", "'\"'\"'")

        # Add session targeting if specified
        session_flag = f"-s '{session}'" if session else ""

        # IMPORTANT: zellij pipe expects payload as positional arg after --, NOT via stdin
        # The stdin pipe method only works for streaming mode, not one-shot commands
        shell_cmd = f"timeout {int(timeout)} zellij {session_flag} pipe -p '{plugin_url}' -n {cmd} -- '{escaped_payload}'"

        result = subprocess.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=timeout + 2  # Extra buffer for Python timeout
        )

        # returncode 124 means timeout killed it (expected), but we got output
        stdout = result.stdout.strip()
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {"success": True, "raw": stdout}

        # No output - check if there was an error
        if result.stderr:
            return {"success": False, "error": result.stderr.strip()}

        return {"success": False, "error": "No output from plugin"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Plugin command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def plugin_write_to_pane(pane_id: int, chars: str, session: str = None) -> dict:
    """Write characters to a pane by ID without focus stealing."""
    return plugin_command("write", {"pane_id": pane_id, "chars": chars}, session=session)


def plugin_list_panes(session: str = None) -> dict:
    """Get list of all panes via plugin."""
    return plugin_command("list", session=session)


def plugin_get_protected(session: str = None) -> dict:
    """Get the protected (Claude) pane ID."""
    return plugin_command("get_protected", session=session)


def plugin_find_pane_id(name: str, session: str = None) -> Optional[int]:
    """Find a pane ID by name/title/command using the plugin.

    Returns the pane ID if found, None otherwise.
    """
    result = plugin_list_panes(session=session)
    if not result.get("success") or not result.get("data"):
        return None

    name_lower = name.lower()
    for pane in result.get("data", []):
        title = pane.get("title", "").lower()
        command = (pane.get("command") or "").lower()

        if name_lower in title or name_lower in command:
            return pane.get("id")

    return None


# =============================================================================
# DAEMON COMMUNICATION - Focus-free pane reading via attached daemon
# =============================================================================
# The daemon runs INSIDE a Zellij session and can use dump-screen reliably.
# It listens on a Unix socket for commands from the MCP server.

import socket as socket_module


def get_daemon_socket_path(session: str = None) -> str:
    """Get the daemon socket path for a session."""
    session_name = session or os.environ.get("ZELLIJ_SESSION_NAME", "default")
    return f"/tmp/zellij-daemon-{session_name}.sock"


def is_daemon_running(session: str = None) -> bool:
    """Check if the daemon is running for a session."""
    socket_path = get_daemon_socket_path(session)
    return os.path.exists(socket_path)


def daemon_request(request: dict, session: str = None, timeout: float = 10.0) -> dict:
    """Send a request to the daemon and return the response."""
    socket_path = get_daemon_socket_path(session)
    if not os.path.exists(socket_path):
        return {"success": False, "error": "Daemon not running"}

    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.send(json.dumps(request).encode('utf-8'))
        response = sock.recv(65536).decode('utf-8')
        return json.loads(response)
    except socket_module.timeout:
        return {"success": False, "error": "Daemon request timed out"}
    except ConnectionRefusedError:
        return {"success": False, "error": "Daemon connection refused"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        sock.close()


def daemon_read_pane(pane_id: int, full: bool = False, tail: int = None,
                     session: str = None) -> dict:
    """Read pane content via the daemon (focus-free)."""
    return daemon_request({
        "cmd": "read",
        "pane_id": pane_id,
        "full": full,
        "tail": tail
    }, session=session)


def daemon_write_pane(pane_id: int, chars: str, session: str = None) -> dict:
    """Write to a pane via the daemon."""
    return daemon_request({
        "cmd": "write",
        "pane_id": pane_id,
        "chars": chars
    }, session=session)


def daemon_list_panes(session: str = None) -> dict:
    """List panes via the daemon."""
    return daemon_request({"cmd": "list"}, session=session)


def daemon_status(session: str = None) -> dict:
    """Get daemon status."""
    return daemon_request({"cmd": "status"}, session=session)


_daemon_start_attempted: dict[str, bool] = {}  # Track per-session to avoid repeated attempts


def start_daemon(session: str = None) -> dict:
    """Start the daemon in a hidden pane within the session.

    The daemon must run INSIDE the Zellij session for dump-screen to work.
    """
    session_name = session or os.environ.get("ZELLIJ_SESSION_NAME")
    if not session_name:
        return {"success": False, "error": "No session specified"}

    # Check if already running
    if is_daemon_running(session):
        return {"success": True, "message": "Daemon already running"}

    # Find the daemon script
    daemon_script = os.path.join(os.path.dirname(__file__), "zellij-daemon.py")
    if not os.path.exists(daemon_script):
        return {"success": False, "error": f"Daemon script not found: {daemon_script}"}

    socket_path = get_daemon_socket_path(session)

    # Create a hidden pane in the session to run the daemon
    # Use a floating pane that we immediately minimize
    args = ["action", "new-pane", "--floating", "--name", "zellij-daemon",
            "--", "python3", daemon_script, "--socket", socket_path]
    result = run_zellij(*args, session=session)

    if not result.get("success"):
        return result

    # Wait for daemon to start
    for _ in range(10):
        time.sleep(0.3)
        if is_daemon_running(session):
            # Hide the daemon pane by toggling floating
            zellij_action("toggle-floating-panes", session=session)
            return {"success": True, "message": "Daemon started", "socket": socket_path}

    return {"success": False, "error": "Daemon failed to start within timeout"}


def ensure_daemon(session: str = None) -> bool:
    """Ensure daemon is running, auto-start if needed. Returns True if available."""
    session_key = _resolve_session_key(session)

    # Already running?
    if is_daemon_running(session):
        return True

    # Already tried and failed this session? Don't spam retries
    if _daemon_start_attempted.get(session_key):
        return False

    # Try to start
    _daemon_start_attempted[session_key] = True
    result = start_daemon(session)
    return result.get("success", False)


# =============================================================================
# SESSION MANAGER - Native cross-session control via pty attachments
# =============================================================================

@dataclass
class SessionAttachment:
    """Tracks a headless pty attachment to a Zellij session."""
    session: str
    pid: int
    master_fd: int
    created_at: float = field(default_factory=time.time)


class SessionManager:
    """Manages headless attachments and daemons across Zellij sessions.

    For cross-session control, we need:
    1. A pty-based "headless client" attached to the target session
    2. A daemon running inside that session for focus-free operations
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._attachments: dict[str, SessionAttachment] = {}

    def _is_current_session(self, session: str) -> bool:
        """Check if session is the current attached session."""
        current = os.environ.get("ZELLIJ_SESSION_NAME")
        return session == current or (not session and current)

    def _is_attachment_alive(self, attachment: SessionAttachment) -> bool:
        """Check if the headless attachment process is still running."""
        try:
            os.kill(attachment.pid, 0)  # Signal 0 = check if process exists
            return True
        except OSError:
            return False

    def headless_attach(self, session: str) -> dict:
        """Create a headless pty attachment to a Zellij session.

        This creates a pseudo-terminal and forks a process that runs
        'zellij attach <session>', making Zellij think a client is connected.
        """
        with self._lock:
            # Already attached?
            if session in self._attachments:
                att = self._attachments[session]
                if self._is_attachment_alive(att):
                    return {"success": True, "message": "Already attached", "pid": att.pid}
                else:
                    # Clean up dead attachment
                    self._cleanup_attachment(att)
                    del self._attachments[session]

            # Check if session exists
            active = get_active_sessions()
            if session not in active:
                return {"success": False, "error": f"Session '{session}' not found"}

            try:
                # Create pseudo-terminal
                master_fd, slave_fd = pty.openpty()

                pid = os.fork()
                if pid == 0:
                    # Child process - becomes the headless Zellij client
                    try:
                        os.setsid()  # New session, detach from parent
                        os.dup2(slave_fd, 0)  # stdin
                        os.dup2(slave_fd, 1)  # stdout
                        os.dup2(slave_fd, 2)  # stderr
                        os.close(master_fd)
                        os.close(slave_fd)
                        os.execvp("zellij", ["zellij", "attach", session])
                    except Exception:
                        os._exit(1)
                else:
                    # Parent process
                    os.close(slave_fd)

                    # Wait briefly for attachment to establish
                    time.sleep(0.5)

                    # Verify it's still running
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        os.close(master_fd)
                        return {"success": False, "error": "Attachment process died immediately"}

                    attachment = SessionAttachment(
                        session=session,
                        pid=pid,
                        master_fd=master_fd
                    )
                    self._attachments[session] = attachment

                    return {"success": True, "pid": pid, "session": session}

            except Exception as e:
                return {"success": False, "error": str(e)}

    def detach(self, session: str) -> dict:
        """Detach from a session, cleaning up the headless attachment."""
        with self._lock:
            if session not in self._attachments:
                return {"success": True, "message": "Not attached"}

            att = self._attachments[session]
            self._cleanup_attachment(att)
            del self._attachments[session]

            return {"success": True, "message": f"Detached from {session}"}

    def _cleanup_attachment(self, attachment: SessionAttachment):
        """Clean up a headless attachment."""
        try:
            os.close(attachment.master_fd)
        except OSError:
            pass
        try:
            os.kill(attachment.pid, signal.SIGTERM)
            # Give it a moment to exit gracefully
            time.sleep(0.1)
            try:
                os.kill(attachment.pid, signal.SIGKILL)
            except OSError:
                pass
        except OSError:
            pass

    def ensure_session_ready(self, session: str) -> bool:
        """Ensure a session is ready for daemon operations.

        For current session: just return True (already have client).
        For other sessions: create headless attachment if needed.
        """
        if self._is_current_session(session):
            return True

        if not session:
            return True  # Will use current session

        # Check if already attached
        with self._lock:
            if session in self._attachments:
                if self._is_attachment_alive(self._attachments[session]):
                    return True

        # Need to attach
        result = self.headless_attach(session)
        return result.get("success", False)

    def list_attachments(self) -> list[dict]:
        """List all active headless attachments."""
        with self._lock:
            result = []
            dead = []
            for session, att in self._attachments.items():
                if self._is_attachment_alive(att):
                    result.append({
                        "session": session,
                        "pid": att.pid,
                        "age_seconds": time.time() - att.created_at
                    })
                else:
                    dead.append(session)

            # Clean up dead attachments
            for session in dead:
                self._cleanup_attachment(self._attachments[session])
                del self._attachments[session]

            return result

    def cleanup_all(self):
        """Clean up all attachments (call on shutdown)."""
        with self._lock:
            for att in self._attachments.values():
                self._cleanup_attachment(att)
            self._attachments.clear()


# Global session manager
session_manager = SessionManager()


def ensure_session_daemon(session: str = None) -> bool:
    """Ensure both session attachment and daemon are ready.

    This is the main entry point for cross-session operations.
    """
    # First ensure we can talk to the session (pty attachment for remote sessions)
    if not session_manager.ensure_session_ready(session):
        return False

    # Then ensure daemon is running in that session
    return ensure_daemon(session)


# =============================================================================
# UTILITIES
# =============================================================================

def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text for clean LLM consumption."""
    # CSI sequences (colors, cursor movement, etc.)
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    # OSC sequences (title, hyperlinks, etc.)
    text = re.sub(r'\x1b\].*?\x07', '', text)
    # Other escape sequences
    text = re.sub(r'\x1b[PX^_].*?\x1b\\', '', text)
    return text


# Key name to byte sequence mapping for send_keys
KEY_SEQUENCES: dict[str, list[int]] = {
    # Control characters
    "ctrl+a": [1], "ctrl+b": [2], "ctrl+c": [3], "ctrl+d": [4],
    "ctrl+e": [5], "ctrl+f": [6], "ctrl+g": [7], "ctrl+h": [8],
    "ctrl+i": [9], "ctrl+j": [10], "ctrl+k": [11], "ctrl+l": [12],
    "ctrl+m": [13], "ctrl+n": [14], "ctrl+o": [15], "ctrl+p": [16],
    "ctrl+q": [17], "ctrl+r": [18], "ctrl+s": [19], "ctrl+t": [20],
    "ctrl+u": [21], "ctrl+v": [22], "ctrl+w": [23], "ctrl+x": [24],
    "ctrl+y": [25], "ctrl+z": [26],
    # Special keys
    "tab": [9], "enter": [13], "return": [13], "escape": [27], "esc": [27],
    "backspace": [127], "delete": [27, 91, 51, 126],
    "space": [32],
    # Arrow keys
    "up": [27, 91, 65], "down": [27, 91, 66],
    "right": [27, 91, 67], "left": [27, 91, 68],
    # Navigation
    "home": [27, 91, 72], "end": [27, 91, 70],
    "pageup": [27, 91, 53, 126], "pagedown": [27, 91, 54, 126],
    "insert": [27, 91, 50, 126],
    # Function keys
    "f1": [27, 79, 80], "f2": [27, 79, 81], "f3": [27, 79, 82], "f4": [27, 79, 83],
    "f5": [27, 91, 49, 53, 126], "f6": [27, 91, 49, 55, 126],
    "f7": [27, 91, 49, 56, 126], "f8": [27, 91, 49, 57, 126],
    "f9": [27, 91, 50, 48, 126], "f10": [27, 91, 50, 49, 126],
    "f11": [27, 91, 50, 51, 126], "f12": [27, 91, 50, 52, 126],
}

def calculate_grid_direction(pane_count: int) -> str:
    """Calculate optimal split direction for grid layout.

    Strategy: Alternate between right and down to create balanced grid.
    - 0 panes: first pane, no split needed
    - 1 pane:  split right  → 2 columns
    - 2 panes: split down   → 2x2 grid start
    - 3 panes: split right  → balance
    - 4 panes: split down   → 2x3 or 3x2
    ...

    This creates layouts like:
    1: [A]
    2: [A][B]
    3: [A][B]
       [C]
    4: [A][B]
       [C][D]
    """
    if pane_count <= 0:
        return "right"  # First split
    # Alternate: odd count → right, even count → down
    return "right" if pane_count % 2 == 1 else "down"


# REPL prompt patterns for auto-detection
REPL_PROMPTS: dict[str, str] = {
    "ipython": r"In \[\d+\]:\s*$",
    "python": r">>>\s*$",
    "r": r">\s*$",
    "julia": r"julia>\s*$",
    "bash": r"[\$#]\s*$",
    "zsh": r"[%#]\s*$",
    "default": r"[\$#>%]\s*$",
}


# =============================================================================
# LAYOUT CACHE - Reduces redundant subprocess calls
# =============================================================================

def _resolve_session_key(session: str = None) -> str:
    """Resolve session to a stable cache key, avoiding _default bleeding."""
    if session:
        return session
    # Try to get from environment if not specified
    env_session = os.environ.get("ZELLIJ_SESSION_NAME")
    if env_session:
        return env_session
    return "_default"


class LayoutCache:
    """Cache for dump-layout results with TTL to reduce subprocess calls."""

    def __init__(self, ttl: float = 0.5):
        self._cache: dict[str, tuple[float, str]] = {}  # session -> (timestamp, layout)
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, session: str = None) -> Optional[str]:
        """Get cached layout if still valid."""
        key = _resolve_session_key(session)
        with self._lock:
            if key in self._cache:
                ts, layout = self._cache[key]
                if time.time() - ts < self._ttl:
                    return layout
        return None

    def set(self, layout: str, session: str = None):
        """Cache a layout result."""
        key = _resolve_session_key(session)
        with self._lock:
            self._cache[key] = (time.time(), layout)

    def invalidate(self, session: str = None):
        """Invalidate cache for a session (call after mutations)."""
        key = _resolve_session_key(session)
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_all(self):
        """Invalidate all cached layouts."""
        with self._lock:
            self._cache.clear()


layout_cache = LayoutCache()


# =============================================================================
# SESSION STATE - In-memory registry for panes, SSH sessions, jobs
# =============================================================================

@dataclass
class PaneInfo:
    """Registered pane information."""
    name: str
    tab: str
    command: Optional[str] = None
    cwd: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    repl_type: Optional[str] = None
    pane_index: Optional[int] = None  # Index within tab at creation time
    floating: bool = False  # Whether pane is floating
    pane_id: Optional[int] = None  # Zellij pane ID for daemon communication


@dataclass
class SSHSession:
    """Registered SSH session."""
    name: str
    host: str
    pane_name: str
    connected_at: float = field(default_factory=time.time)


@dataclass
class TrackedJob:
    """Tracked HPC job."""
    job_id: str
    scheduler: str  # "slurm" or "pbs"
    ssh_name: str
    script: str
    submitted_at: float = field(default_factory=time.time)
    status: str = "PENDING"


@dataclass
class SpawnedAgent:
    """Tracked spawned agent."""
    name: str
    pane_name: str
    task: str
    prompt: str
    model: str = "claude-sonnet-4-22k-0514"
    status: str = "running"  # running, completed, failed
    spawned_at: float = field(default_factory=time.time)
    tab: str = "agents"


class SessionState:
    """Server-side state that persists across tool calls within a session. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self.panes: dict[str, PaneInfo] = {}
        self.ssh_sessions: dict[str, SSHSession] = {}
        self.tracked_jobs: dict[str, TrackedJob] = {}
        self.pane_groups: dict[str, list[str]] = {}
        self.pane_cursors: dict[str, int] = {}  # For tail_pane incremental reads
        self.spawned_agents: dict[str, SpawnedAgent] = {}  # For task-based agent tracking

    def register_pane(self, name: str, tab: str, command: str = None,
                      cwd: str = None, repl_type: str = None,
                      pane_index: int = None, floating: bool = False,
                      pane_id: int = None) -> PaneInfo:
        """Register a named pane. Thread-safe."""
        pane = PaneInfo(name=name, tab=tab, command=command, cwd=cwd,
                        repl_type=repl_type, pane_index=pane_index, floating=floating,
                        pane_id=pane_id)
        with self._lock:
            self.panes[name] = pane
        return pane

    def unregister_pane(self, name: str) -> bool:
        """Remove a pane from registry. Thread-safe."""
        with self._lock:
            if name in self.panes:
                del self.panes[name]
                if name in self.pane_cursors:
                    del self.pane_cursors[name]
                return True
            return False

    def get_pane(self, name: str) -> Optional[PaneInfo]:
        """Get registered pane info. Thread-safe."""
        with self._lock:
            return self.panes.get(name)


# Global state instance
state = SessionState()


def parse_layout_panes(layout_text: str) -> list[dict]:
    """Parse KDL layout to extract pane info with enhanced metadata."""
    panes = []
    current_tab = None
    current_tab_index = 0
    tab_focused = False
    pane_index_in_tab = 0
    floating_depth = 0  # Track brace depth for floating_panes block
    current_pane = None  # Track current pane block for multi-line parsing
    pane_brace_depth = 0  # Track brace depth within pane block
    swap_layout_depth = 0  # Track depth inside swap_* template sections

    for line in layout_text.split('\n'):
        stripped = line.strip()

        # Skip swap_tiled_layout and swap_floating_layout sections (templates, not real panes)
        if stripped.startswith('swap_tiled_layout') or stripped.startswith('swap_floating_layout'):
            swap_layout_depth = 1
            swap_layout_depth += stripped.count('{') - 1
            continue
        if swap_layout_depth > 0:
            swap_layout_depth += stripped.count('{')
            swap_layout_depth -= stripped.count('}')
            if swap_layout_depth <= 0:
                swap_layout_depth = 0
            continue

        # Skip new_tab_template section (template, not real panes)
        if stripped.startswith('new_tab_template'):
            swap_layout_depth = 1
            swap_layout_depth += stripped.count('{') - 1
            continue

        # Track floating panes section with brace depth
        # Note: must check startswith, not 'in', to avoid matching 'hide_floating_panes=true'
        if stripped.startswith('floating_panes') and current_pane is None:
            floating_depth = 1
            floating_depth += stripped.count('{') - 1
            continue
        if floating_depth > 0 and current_pane is None:
            # Check if this is a pane start within floating_panes
            if re.match(r'\s*pane\b', stripped):
                pass  # Let it fall through to pane handling
            else:
                floating_depth += stripped.count('{')
                floating_depth -= stripped.count('}')
                if floating_depth <= 0:
                    floating_depth = 0
                continue

        in_floating = floating_depth > 0

        # Match tab lines
        tab_match = re.search(r'tab\s+name="([^"]+)".*?(focus=true)?', line)
        if tab_match and current_pane is None:
            current_tab = tab_match.group(1)
            current_tab_index += 1
            tab_focused = tab_match.group(2) is not None
            pane_index_in_tab = 0
            continue

        # Handle content inside a pane block
        if current_pane is not None:
            pane_brace_depth += stripped.count('{')
            pane_brace_depth -= stripped.count('}')

            # Extract args from inside pane block
            args_match = re.search(r'args\s+(.+)', stripped)
            if args_match:
                # Parse the args string - format: "arg1" "arg2" "arg3"
                args_str = args_match.group(1)
                current_pane["args"] = args_str

            # Check for nested pane declarations (e.g., inside split_direction blocks)
            # These are real panes that need to be captured
            nested_pane_match = re.match(r'\s*pane\b', stripped)
            if nested_pane_match and 'borderless=true' not in stripped:
                # This is a nested pane - create a new pane_info for it
                nested_info = {
                    "tab": current_tab,
                    "tab_index": current_tab_index,
                    "tab_focused": tab_focused,
                    "pane_index": pane_index_in_tab,
                    "floating": in_floating,
                }
                pane_index_in_tab += 1

                # Extract command from nested pane
                cmd_match = re.search(r'command="([^"]+)"', stripped)
                if cmd_match:
                    nested_info["command"] = cmd_match.group(1)

                # Extract name from nested pane
                name_match = re.search(r'name="([^"]+)"', stripped)
                if name_match:
                    nested_info["name"] = name_match.group(1)

                # Extract size
                size_match = re.search(r'size="([^"]+)"', stripped)
                if size_match:
                    nested_info["size"] = size_match.group(1)

                # Check if focused
                nested_info["focused"] = "focus=true" in stripped and tab_focused

                panes.append(nested_info)

            # Check if pane block ended
            if pane_brace_depth <= 0:
                panes.append(current_pane)
                current_pane = None
                pane_brace_depth = 0
            continue

        # Match pane lines (any pane, not just named ones)
        if re.match(r'\s*pane\b', stripped):
            pane_info = {
                "tab": current_tab,
                "tab_index": current_tab_index,
                "tab_focused": tab_focused,
                "pane_index": pane_index_in_tab,
                "floating": in_floating,
            }
            pane_index_in_tab += 1

            # Extract command
            cmd_match = re.search(r'command="([^"]+)"', line)
            if cmd_match:
                pane_info["command"] = cmd_match.group(1)

            # Extract name
            name_match = re.search(r'name="([^"]+)"', line)
            if name_match:
                pane_info["name"] = name_match.group(1)

            # Extract size
            size_match = re.search(r'size="([^"]+)"', line)
            if size_match:
                pane_info["size"] = size_match.group(1)

            # Check if focused
            pane_info["focused"] = "focus=true" in line and tab_focused

            # Extract cwd if present
            cwd_match = re.search(r'cwd="([^"]+)"', line)
            if cwd_match:
                pane_info["cwd"] = cwd_match.group(1)

            # Check if pane has a block (contains '{')
            if '{' in stripped:
                pane_brace_depth = stripped.count('{') - stripped.count('}')
                if pane_brace_depth > 0:
                    current_pane = pane_info
                    continue

            # Simple pane without block
            panes.append(pane_info)

    return panes


def find_pane_by_name(panes: list[dict], name: str, registered_pane: PaneInfo = None) -> Optional[dict]:
    """Find a pane by name, command, args marker, or registered index."""
    name_lower = name.lower()
    marker = f"ZELLIJ_PANE_NAME={name}"
    marker_lower = marker.lower()

    for p in panes:
        pane_name = p.get("name", "").lower()
        pane_cmd = p.get("command", "").lower()
        pane_args = p.get("args", "").lower()

        # Match by name or command (existing behavior)
        if name_lower in pane_name or name_lower in pane_cmd:
            return p
        # Match by ZELLIJ_PANE_NAME marker in args
        if marker_lower in pane_args:
            return p

    # If not found by name/command, try registered pane index
    if registered_pane and registered_pane.pane_index is not None:
        reg_tab = registered_pane.tab
        reg_index = registered_pane.pane_index
        is_floating = registered_pane.floating

        # Filter panes by tab and floating status
        for p in panes:
            p_tab = p.get("tab")
            p_floating = p.get("floating", False)
            p_index = p.get("pane_index", -1)

            # Match: same tab (or "current"), same floating status, same index
            if (p_tab == reg_tab or reg_tab == "current") and \
               p_floating == is_floating and p_index == reg_index:
                return p

    return None


async def wait_for_prompt(
    pane_name: str,
    pattern: str,
    timeout: float,
    session: str = None,
    poll_interval: float = 1.0,
    check_lines: int = 5,
) -> dict:
    """
    Wait for a prompt pattern to appear in pane output.

    Common helper for run_in_pane, repl_execute, ssh_run, repl_interrupt.
    Returns dict with success, completed, output, and elapsed.
    """
    async def do_read():
        return zellij_action("dump-screen", "/dev/stdout", capture=True, session=session)

    start_time = time.time()
    regex = re.compile(pattern)
    output = ""

    while time.time() - start_time < timeout:
        if pane_name:
            read_result = await with_pane_focus(pane_name, do_read, session=session)
        else:
            read_result = await do_read()

        if read_result.get("success"):
            content = strip_ansi(read_result.get("stdout", ""))
            output = content
            # Check last few lines for prompt
            last_lines = '\n'.join(content.split('\n')[-check_lines:])
            if regex.search(last_lines):
                return {
                    "success": True,
                    "completed": True,
                    "output": output,
                    "elapsed": round(time.time() - start_time, 2),
                }

        await asyncio.sleep(poll_interval)

    return {
        "success": True,
        "completed": False,
        "output": output,
        "elapsed": round(time.time() - start_time, 2),
        "timeout": True,
    }


async def with_pane_focus(pane_name: str, action_fn: Callable, session: str = None) -> dict:
    """
    Execute action_fn while the named pane is focused, then restore focus.

    This is the core pattern for pane-targeted operations in zellij,
    which doesn't support pane-id targeting for most actions.

    Args:
        pane_name: Name of the pane to focus
        action_fn: Async or sync callable that performs the action
        session: Target zellij session

    Returns:
        Result dict from action_fn, with focus restoration info
    """
    # Acquire per-session lock to prevent concurrent focus operations
    focus_lock = get_focus_lock(session)
    with focus_lock:
        return await _with_pane_focus_impl(pane_name, action_fn, session)


async def _with_pane_focus_impl(pane_name: str, action_fn: Callable, session: str = None) -> dict:
    """Internal implementation of with_pane_focus (called under lock)."""
    # Get current layout to find original focus and target pane
    layout_result = zellij_action("dump-layout", capture=True, session=session)
    if not layout_result.get("success"):
        return {"success": False, "error": "Failed to get layout", "details": layout_result}

    panes = parse_layout_panes(layout_result.get("stdout", ""))

    # Look up registered pane info for index-based matching
    registered_pane = state.panes.get(pane_name)
    target_pane = find_pane_by_name(panes, pane_name, registered_pane)

    if not target_pane:
        return {"success": False, "error": f"Pane '{pane_name}' not found",
                "available": [p.get("name") or p.get("command") for p in panes]}

    # Find the currently focused pane/tab for restoration
    original_tab = None
    for p in panes:
        if p.get("focused"):
            original_tab = p.get("tab")
            break

    # Switch to target tab if needed
    if not target_pane.get("tab_focused") and target_pane.get("tab"):
        tab_result = zellij_action("go-to-tab-name", target_pane["tab"], session=session)
        if not tab_result.get("success"):
            return {"success": False, "error": "Failed to switch tab", "details": tab_result}

    # Navigate to the pane within the tab using focus cycling
    # For detached sessions, we use pane index to determine how many cycles needed
    if not target_pane.get("focused"):
        # Count panes in the target tab (excluding plugins like tab-bar, status-bar)
        tab_panes = [p for p in panes if p.get("tab") == target_pane.get("tab")
                     and p.get("command") and "plugin" not in str(p.get("command", ""))]

        target_index = target_pane.get("pane_index", 0)

        # In detached sessions, nothing is "focused" in the layout
        # We just cycle to the pane by index
        # First, reset to a known state by cycling through all panes once
        num_panes = len(tab_panes) if tab_panes else 1

        # Cycle to target pane (use modulo to handle wrapping)
        cycles_needed = target_index % num_panes if num_panes > 0 else 0

        for _ in range(cycles_needed):
            zellij_action("focus-next-pane", session=session)

        layout_cache.invalidate(session)

    # Execute the action
    try:
        if asyncio.iscoroutinefunction(action_fn):
            result = await action_fn()
        else:
            result = action_fn()
    except Exception as e:
        result = {"success": False, "error": str(e)}

    # Restore original tab focus (pane focus within tab is best-effort)
    if original_tab and original_tab != target_pane.get("tab"):
        zellij_action("go-to-tab-name", original_tab, session=session)

    return result


def run_zellij(*args: str, capture: bool = False, session: str = None) -> dict[str, Any]:
    """Run a zellij command, optionally targeting a specific session."""
    cmd = ["zellij"]
    if session:
        cmd.extend(["-s", session])
    cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if capture:
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        return {"success": result.returncode == 0}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# Actions that mutate layout and should invalidate cache
LAYOUT_MUTATING_ACTIONS = {
    "new-pane", "close-pane", "new-tab", "close-tab", "rename-pane",
    "rename-tab", "move-pane", "move-pane-backwards", "toggle-floating-panes",
    "toggle-pane-embed-or-floating", "focus-next-pane", "focus-previous-pane",
    "move-focus", "go-to-tab", "go-to-tab-name", "go-to-next-tab", "go-to-previous-tab",
}


def zellij_action(*args: str, capture: bool = False, session: str = None) -> dict[str, Any]:
    """Run a zellij action command, optionally targeting a specific session."""
    action_name = args[0] if args else ""

    # Use cache for dump-layout reads
    if action_name == "dump-layout" and capture:
        cached = layout_cache.get(session)
        if cached is not None:
            return {"success": True, "stdout": cached, "stderr": ""}

    result = run_zellij("action", *args, capture=capture, session=session)

    # Cache dump-layout results
    if action_name == "dump-layout" and capture and result.get("success"):
        layout_cache.set(result.get("stdout", ""), session)

    # Invalidate cache after layout-mutating actions
    if action_name in LAYOUT_MUTATING_ACTIONS:
        layout_cache.invalidate(session)

    return result


# Common schema for session targeting
SESSION_PARAM = {"session": {"type": "string", "description": "Target session name (default: current)"}}


DIRECTION_ENUM = {"type": "string", "enum": ["down", "right", "up", "left"]}
MODE_ENUM = {"type": "string", "enum": ["locked", "pane", "tab", "resize", "move", "search", "session", "normal"]}


def with_session(tools: list[Tool]) -> list[Tool]:
    """Add session parameter to all tool schemas and filter hidden tools."""
    # Filter hidden tools to save context tokens
    filtered = [t for t in tools if t.name not in HIDDEN_TOOLS]
    for tool in filtered:
        tool.inputSchema["properties"] = {
            **tool.inputSchema.get("properties", {}),
            **SESSION_PARAM,
        }
    return filtered


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all Zellij control tools."""
    return with_session([
        # === PANE MANAGEMENT ===
        Tool(
            name="new_pane",
            description="Open a new pane in Zellij",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {**DIRECTION_ENUM, "description": "Direction for new pane"},
                    "floating": {"type": "boolean", "description": "Create floating pane"},
                    "in_place": {"type": "boolean", "description": "Open in place (replace current)"},
                    "command": {"type": "string", "description": "Command to run"},
                    "name": {"type": "string", "description": "Pane name"},
                    "cwd": {"type": "string", "description": "Working directory"},
                    "close_on_exit": {"type": "boolean", "description": "Close pane when command exits"},
                    "start_suspended": {"type": "boolean", "description": "Start suspended"},
                },
            },
        ),
        Tool(
            name="close_pane",
            description="Close the focused pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="focus_pane",
            description="Move focus to pane in direction",
            inputSchema={
                "type": "object",
                "properties": {"direction": {**DIRECTION_ENUM, "description": "Direction to move focus"}},
                "required": ["direction"],
            },
        ),
        Tool(
            name="focus_next_pane",
            description="Move focus to the next pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="focus_previous_pane",
            description="Move focus to the previous pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="move_pane",
            description="Move the focused pane in a direction",
            inputSchema={
                "type": "object",
                "properties": {"direction": {**DIRECTION_ENUM, "description": "Direction to move pane"}},
            },
        ),
        Tool(
            name="move_pane_backwards",
            description="Rotate pane location backwards",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="resize_pane",
            description="Resize the focused pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {**DIRECTION_ENUM, "description": "Border to resize"},
                    "increase": {"type": "boolean", "description": "Increase size (default true)", "default": True},
                },
                "required": ["direction"],
            },
        ),
        Tool(
            name="rename_pane",
            description="Rename the focused pane",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "New pane name"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="undo_rename_pane",
            description="Remove custom pane name",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="toggle_floating",
            description="Toggle floating panes visibility",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="toggle_fullscreen",
            description="Toggle fullscreen for focused pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="toggle_embed_or_floating",
            description="Toggle between embedded and floating for focused pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="toggle_pane_frames",
            description="Toggle pane frames in the UI",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="toggle_sync_tab",
            description="Toggle sending commands to all panes in tab",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="stack_panes",
            description="Stack multiple panes together",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Pane IDs (e.g., terminal_1, plugin_1)",
                    }
                },
                "required": ["pane_ids"],
            },
        ),
        # === TAB MANAGEMENT ===
        Tool(
            name="new_tab",
            description="Create a new tab",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name"},
                    "layout": {"type": "string", "description": "Layout file path"},
                    "cwd": {"type": "string", "description": "Working directory"},
                },
            },
        ),
        Tool(
            name="close_tab",
            description="Close the current tab",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="focus_tab",
            description="Switch to a tab by index or direction",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Tab index (1-based)"},
                    "name": {"type": "string", "description": "Tab name"},
                    "direction": {"type": "string", "enum": ["next", "previous"], "description": "Relative navigation"},
                },
            },
        ),
        Tool(
            name="move_tab",
            description="Move the current tab left or right",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["left", "right"], "description": "Direction to move"},
                },
                "required": ["direction"],
            },
        ),
        Tool(
            name="rename_tab",
            description="Rename the current tab",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "New tab name"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="undo_rename_tab",
            description="Remove custom tab name",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="query_tab_names",
            description="Get names of all tabs",
            inputSchema={"type": "object", "properties": {}},
        ),
        # === SCROLLING ===
        Tool(
            name="scroll",
            description="Scroll in the focused pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                    "amount": {
                        "type": "string",
                        "enum": ["line", "half_page", "page", "top", "bottom"],
                        "description": "Scroll amount (default: line)",
                    },
                },
                "required": ["direction"],
            },
        ),
        # === TEXT/COMMAND ===
        Tool(
            name="write_chars",
            description="Send characters to the focused pane",
            inputSchema={
                "type": "object",
                "properties": {"chars": {"type": "string", "description": "Characters to send"}},
                "required": ["chars"],
            },
        ),
        Tool(
            name="clear_pane",
            description="Clear all buffers for the focused pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="read_pane",
            description="Read content from any pane by name. Returns cleaned text suitable for LLM processing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane name (default: focused pane)"},
                    "full": {"type": "boolean", "description": "Include full scrollback history"},
                    "tail": {"type": "integer", "description": "Return only the last N lines"},
                    "strip_ansi": {"type": "boolean", "description": "Strip ANSI codes (default: true)", "default": True},
                },
            },
        ),
        Tool(
            name="list_panes",
            description="List all panes in the current session with their names, commands, and focus state",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="focus_pane_by_name",
            description="Focus a pane by its name (searches all tabs)",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pane name to focus"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="write_to_pane",
            description="Send characters to a specific named pane without changing visible focus",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane name"},
                    "chars": {"type": "string", "description": "Characters to send"},
                    "press_enter": {"type": "boolean", "description": "Append newline after chars", "default": False},
                },
                "required": ["pane_name", "chars"],
            },
        ),
        Tool(
            name="send_keys",
            description="Send special key sequences to a pane (ctrl+c, tab, arrows, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane (default: focused)"},
                    "keys": {"type": "string", "description": "Key spec: ctrl+c, ctrl+d, tab, enter, escape, up, down, left, right, etc."},
                    "repeat": {"type": "integer", "description": "Repeat the key N times", "default": 1},
                },
                "required": ["keys"],
            },
        ),
        Tool(
            name="search_pane",
            description="Search a pane's content for a regex pattern, returns matching lines",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane (default: focused)"},
                    "pattern": {"type": "string", "description": "Python regex pattern to search"},
                    "context": {"type": "integer", "description": "Lines of context around matches", "default": 0},
                    "full": {"type": "boolean", "description": "Search full scrollback", "default": True},
                },
                "required": ["pattern"],
            },
        ),
        # === MONITORING ===
        Tool(
            name="wait_for_output",
            description="Wait for a regex pattern to appear in pane output. Essential for waiting for command completion.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane (default: focused)"},
                    "pattern": {"type": "string", "description": "Regex pattern to wait for"},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 30},
                    "poll_interval": {"type": "number", "description": "Seconds between polls", "default": 1.0},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="wait_for_idle",
            description="Wait until pane output stops changing (command finished producing output)",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane (default: focused)"},
                    "stable_seconds": {"type": "number", "description": "How long output must be stable", "default": 3.0},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 60},
                    "poll_interval": {"type": "number", "description": "Seconds between polls", "default": 1.0},
                },
            },
        ),
        Tool(
            name="tail_pane",
            description="Get only new output since last read (incremental monitoring)",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane (default: focused)"},
                    "reset": {"type": "boolean", "description": "Reset cursor to current position", "default": False},
                },
            },
        ),
        # === WORKSPACE MANAGEMENT ===
        Tool(
            name="agent_session",
            description="Manage workspace tabs for agent operations. Creates 'agent-work' tab if needed. DEPRECATED: Session isolation doesn't work in Zellij; now uses tab-based workspaces.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "create", "destroy"],
                               "description": "Action: status (default), create workspace tab, or destroy workspace tab", "default": "status"},
                },
            },
        ),
        Tool(
            name="spawn_agents",
            description="Spawn multiple Claude agents in parallel, each working on a task. Creates panes in a dedicated tab.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "List of tasks for agents",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Task/agent name (used as pane name)"},
                                "prompt": {"type": "string", "description": "Task prompt for the agent"},
                                "model": {"type": "string", "description": "Model to use (default: claude-sonnet-4-22k-0514)"},
                                "cwd": {"type": "string", "description": "Working directory for agent"},
                            },
                            "required": ["name", "prompt"],
                        },
                    },
                    "tab": {"type": "string", "description": "Tab name for agents (default: agents)", "default": "agents"},
                    "dangerously_skip_permissions": {"type": "boolean", "description": "Run with --dangerously-skip-permissions", "default": False},
                },
                "required": ["tasks"],
            },
        ),
        Tool(
            name="list_spawned_agents",
            description="List all spawned agents and their status",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="agent_output",
            description="Read output from a spawned agent's pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Agent name"},
                    "tail": {"type": "integer", "description": "Last N lines (default: 50)", "default": 50},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="stop_agent",
            description="Stop a running agent (sends Ctrl+C)",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Agent name to stop"},
                },
                "required": ["name"],
            },
        ),
        # === COMPOUND OPERATIONS (run in agent session by default) ===
        Tool(
            name="run_in_pane",
            description="Run command in a pane (agent session by default). No focus stealing - safe for autonomous use.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "Target pane name in agent session"},
                    "command": {"type": "string", "description": "Command to execute"},
                    "wait": {"type": "boolean", "description": "Wait for completion", "default": True},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 30},
                    "capture": {"type": "boolean", "description": "Return output", "default": True},
                    "prompt_pattern": {"type": "string", "description": "Regex for shell prompt", "default": "[\\$#>]\\s*$"},
                },
                "required": ["pane_name", "command"],
            },
        ),
        Tool(
            name="create_named_pane",
            description="Create pane in agent session (isolated workspace). No focus stealing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique pane name"},
                    "command": {"type": "string", "description": "Command to run (e.g., ipython3, R)"},
                    "tab": {"type": "string", "description": "Tab name (creates if needed)"},
                    "direction": {**DIRECTION_ENUM, "description": "Split direction"},
                    "floating": {"type": "boolean", "description": "Create as floating"},
                    "cwd": {"type": "string", "description": "Working directory"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="destroy_named_pane",
            description="Close a named pane in agent session",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pane name to close"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="list_named_panes",
            description="List all registered panes in agent session",
            inputSchema={"type": "object", "properties": {}},
        ),
        # === REPL ===
        Tool(
            name="repl_execute",
            description="Execute code in an interactive REPL (IPython, R, Julia) and capture output",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "REPL pane name"},
                    "code": {"type": "string", "description": "Code to execute (can be multi-line)"},
                    "repl_type": {"type": "string", "enum": ["ipython", "python", "r", "julia", "bash", "auto"],
                                  "description": "REPL type for prompt detection", "default": "auto"},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 60},
                },
                "required": ["pane_name", "code"],
            },
        ),
        Tool(
            name="repl_interrupt",
            description="Send Ctrl+C to interrupt a running command in a REPL",
            inputSchema={
                "type": "object",
                "properties": {
                    "pane_name": {"type": "string", "description": "REPL pane name"},
                    "wait_for_prompt": {"type": "boolean", "description": "Wait for prompt to return", "default": True},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 10},
                },
                "required": ["pane_name"],
            },
        ),
        # === SSH/HPC ===
        Tool(
            name="ssh_connect",
            description="Open an SSH connection in a named pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for this SSH session"},
                    "host": {"type": "string", "description": "SSH host (user@host or config name)"},
                    "tab": {"type": "string", "description": "Tab to create pane in"},
                    "port": {"type": "integer", "description": "SSH port"},
                    "identity_file": {"type": "string", "description": "Path to SSH key"},
                },
                "required": ["name", "host"],
            },
        ),
        Tool(
            name="ssh_run",
            description="Execute a command on a remote host via existing SSH session",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "SSH session name"},
                    "command": {"type": "string", "description": "Command to run"},
                    "wait": {"type": "boolean", "description": "Wait for completion", "default": True},
                    "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 30},
                },
                "required": ["name", "command"],
            },
        ),
        Tool(
            name="job_submit",
            description="Submit an HPC job (SLURM/PBS) and track it",
            inputSchema={
                "type": "object",
                "properties": {
                    "ssh_name": {"type": "string", "description": "SSH session to use"},
                    "script": {"type": "string", "description": "Path to job script"},
                    "scheduler": {"type": "string", "enum": ["slurm", "pbs"], "default": "slurm"},
                    "extra_args": {"type": "string", "description": "Additional sbatch/qsub args"},
                },
                "required": ["ssh_name", "script"],
            },
        ),
        Tool(
            name="job_status",
            description="Check status of tracked HPC jobs",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Specific job ID (or check all)"},
                    "ssh_name": {"type": "string", "description": "SSH session to use"},
                },
            },
        ),
        # === EDIT ===
        Tool(
            name="edit_file",
            description="Open a file in a new pane with default editor",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "line": {"type": "integer", "description": "Line number to jump to"},
                    "floating": {"type": "boolean", "description": "Open in floating pane"},
                    "in_place": {"type": "boolean", "description": "Open in place"},
                    "direction": DIRECTION_ENUM,
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="edit_scrollback",
            description="Open pane scrollback in default editor",
            inputSchema={"type": "object", "properties": {}},
        ),
        # === SESSION ===
        Tool(
            name="list_sessions",
            description="List all Zellij sessions",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_clients",
            description="List connected clients in current session",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="session_info",
            description="Get current session information",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="rename_session",
            description="Rename the current session",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "New session name"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="session_map",
            description="Generate visual ASCII map of all sessions, tabs, and panes",
            inputSchema={
                "type": "object",
                "properties": {
                    "compact": {"type": "boolean", "description": "Compact view (less detail)", "default": False},
                },
            },
        ),
        Tool(
            name="session_attach",
            description="Manage cross-session control. Creates headless pty attachments to other sessions for daemon-based operations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "attach", "detach", "detach_all"],
                        "description": "Action: list attachments, attach to session, detach from session, or detach all",
                        "default": "list",
                    },
                    "session": {
                        "type": "string",
                        "description": "Target session name (required for attach/detach)",
                    },
                },
            },
        ),
        # === LAYOUT ===
        Tool(
            name="dump_layout",
            description="Dump current layout to stdout",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="swap_layout",
            description="Swap to next or previous layout",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["next", "previous"], "description": "Direction"},
                },
            },
        ),
        # === MODE ===
        Tool(
            name="switch_mode",
            description="Switch input mode for all clients",
            inputSchema={
                "type": "object",
                "properties": {"mode": {**MODE_ENUM, "description": "Input mode"}},
                "required": ["mode"],
            },
        ),
        # === PLUGINS ===
        Tool(
            name="launch_plugin",
            description="Launch a Zellij plugin",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Plugin URL or path"},
                    "floating": {"type": "boolean", "description": "Launch as floating"},
                    "in_place": {"type": "boolean", "description": "Launch in place"},
                    "skip_cache": {"type": "boolean", "description": "Skip plugin cache"},
                    "configuration": {"type": "object", "description": "Plugin configuration"},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="pipe",
            description="Send data to plugins via pipe",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pipe name"},
                    "payload": {"type": "string", "description": "Data payload"},
                    "plugin": {"type": "string", "description": "Target plugin URL"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Additional arguments"},
                },
            },
        ),
    ])


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a Zellij control tool."""
    result: dict[str, Any]
    session = arguments.pop("session", None)  # Extract session for all tools

    # === PANE MANAGEMENT ===
    if name == "new_pane":
        command = arguments.get("command")
        start_suspended = arguments.get("start_suspended", False)

        # Use 'zellij run' when command specified (unless explicitly suspended)
        # 'zellij action new-pane' creates suspended panes in detached sessions
        if command and not start_suspended:
            args = ["run"]
            if arguments.get("floating"):
                args.append("--floating")
            if arguments.get("direction"):
                args.extend(["--direction", arguments["direction"]])
            if arguments.get("cwd"):
                args.extend(["--cwd", arguments["cwd"]])
            if arguments.get("name"):
                args.extend(["--name", arguments["name"]])
            if arguments.get("close_on_exit"):
                args.append("--close-on-exit")
            # Complex commands need shell wrapping
            if any(c in command for c in [' ', '|', '&', ';', '>', '<', '$', '`']):
                args.extend(["--", "bash", "-c", command])
            else:
                args.extend(["--", command])
            result = run_zellij(*args, session=session)
        else:
            # No command or explicitly suspended - use new-pane
            args = ["new-pane"]
            if arguments.get("floating"):
                args.append("--floating")
            if arguments.get("in_place"):
                args.append("--in-place")
            if arguments.get("direction"):
                args.extend(["--direction", arguments["direction"]])
            if arguments.get("cwd"):
                args.extend(["--cwd", arguments["cwd"]])
            if arguments.get("name"):
                args.extend(["--name", arguments["name"]])
            if arguments.get("close_on_exit"):
                args.append("--close-on-exit")
            if start_suspended:
                args.append("--start-suspended")
            if command:
                if any(c in command for c in [' ', '|', '&', ';', '>', '<', '$', '`']):
                    args.extend(["--", "bash", "-c", command])
                else:
                    args.extend(["--", command])
            result = zellij_action(*args, session=session)

    elif name == "close_pane":
        # PROTECTION: Check if focused pane is Claude before closing
        layout_result = zellij_action("dump-layout", capture=True, session=session)
        if layout_result.get("success"):
            panes = parse_layout_panes(layout_result.get("stdout", ""))
            focused = next((p for p in panes if p.get("focused")), None)
            if focused and "claude" in (focused.get("name", "") + focused.get("command", "")).lower():
                result = {"success": False, "error": "Cannot close Claude pane - this would terminate the session"}
            else:
                result = zellij_action("close-pane", session=session)
        else:
            result = zellij_action("close-pane", session=session)

    elif name == "focus_pane":
        result = zellij_action("move-focus", arguments["direction"], session=session)

    elif name == "focus_next_pane":
        result = zellij_action("focus-next-pane", session=session)

    elif name == "focus_previous_pane":
        result = zellij_action("focus-previous-pane", session=session)

    elif name == "move_pane":
        if arguments.get("direction"):
            result = zellij_action("move-pane", arguments["direction"], session=session)
        else:
            result = zellij_action("move-pane", session=session)

    elif name == "move_pane_backwards":
        result = zellij_action("move-pane-backwards", session=session)

    elif name == "resize_pane":
        action = "increase" if arguments.get("increase", True) else "decrease"
        result = zellij_action("resize", action, arguments["direction"], session=session)

    elif name == "rename_pane":
        result = zellij_action("rename-pane", arguments["name"], session=session)

    elif name == "undo_rename_pane":
        result = zellij_action("undo-rename-pane", session=session)

    elif name == "toggle_floating":
        result = zellij_action("toggle-floating-panes", session=session)

    elif name == "toggle_fullscreen":
        result = zellij_action("toggle-fullscreen", session=session)

    elif name == "toggle_embed_or_floating":
        result = zellij_action("toggle-pane-embed-or-floating", session=session)

    elif name == "toggle_pane_frames":
        result = zellij_action("toggle-pane-frames", session=session)

    elif name == "toggle_sync_tab":
        result = zellij_action("toggle-active-sync-tab", session=session)

    elif name == "stack_panes":
        result = zellij_action("stack-panes", *arguments["pane_ids"], session=session)

    # === TAB MANAGEMENT ===
    elif name == "new_tab":
        args = ["new-tab"]
        if arguments.get("name"):
            args.extend(["--name", arguments["name"]])
        if arguments.get("layout"):
            args.extend(["--layout", arguments["layout"]])
        if arguments.get("cwd"):
            args.extend(["--cwd", arguments["cwd"]])
        result = zellij_action(*args, session=session)

    elif name == "close_tab":
        result = zellij_action("close-tab", session=session)

    elif name == "focus_tab":
        if "index" in arguments:
            result = zellij_action("go-to-tab", str(arguments["index"]), session=session)
        elif "name" in arguments:
            result = zellij_action("go-to-tab-name", arguments["name"], session=session)
        elif arguments.get("direction") == "next":
            result = zellij_action("go-to-next-tab", session=session)
        elif arguments.get("direction") == "previous":
            result = zellij_action("go-to-previous-tab", session=session)
        else:
            result = {"success": False, "error": "Specify index, name, or direction"}

    elif name == "move_tab":
        result = zellij_action("move-tab", arguments["direction"], session=session)

    elif name == "rename_tab":
        result = zellij_action("rename-tab", arguments["name"], session=session)

    elif name == "undo_rename_tab":
        result = zellij_action("undo-rename-tab", session=session)

    elif name == "query_tab_names":
        result = zellij_action("query-tab-names", capture=True, session=session)

    # === SCROLLING ===
    elif name == "scroll":
        direction = arguments["direction"]
        amount = arguments.get("amount", "line")

        if amount == "top" and direction == "up":
            result = zellij_action("scroll-to-top", session=session)
        elif amount == "bottom" and direction == "down":
            result = zellij_action("scroll-to-bottom", session=session)
        elif amount == "half_page":
            cmd = "half-page-scroll-up" if direction == "up" else "half-page-scroll-down"
            result = zellij_action(cmd, session=session)
        elif amount == "page":
            cmd = "page-scroll-up" if direction == "up" else "page-scroll-down"
            result = zellij_action(cmd, session=session)
        else:  # line
            cmd = "scroll-up" if direction == "up" else "scroll-down"
            result = zellij_action(cmd, session=session)

    # === TEXT/COMMAND ===
    elif name == "write_chars":
        result = zellij_action("write-chars", arguments["chars"], session=session)


    elif name == "clear_pane":
        result = zellij_action("clear", session=session)

    elif name == "read_pane":
        # Panes are now in current session (tab-based workspaces)
        pane_name = arguments.get("pane_name")

        do_strip = arguments.get("strip_ansi", True)
        tail_lines = arguments.get("tail")

        # Try to get pane_id for daemon communication
        pane_id = None
        if pane_name:
            registered_pane = state.get_pane(pane_name)
            if registered_pane and registered_pane.pane_id:
                pane_id = registered_pane.pane_id
            elif is_plugin_available():
                # Try to find pane_id via plugin
                pane_id = plugin_find_pane_id(pane_name, session=session)
                # Update registered pane with discovered pane_id
                if pane_id and registered_pane:
                    registered_pane.pane_id = pane_id

        # Try daemon first for focus-free reads (auto-start if needed, cross-session support)
        daemon_result = None
        if pane_id and ensure_session_daemon(session):
            daemon_result = daemon_read_pane(
                pane_id,
                full=arguments.get("full", False),
                tail=tail_lines,
                session=session
            )

        if daemon_result and daemon_result.get("success"):
            content = daemon_result.get("content", "")
            if do_strip:
                content = strip_ansi(content)
            result = {"success": True, "content": content, "method": "daemon",
                      "pane_id": pane_id}
        else:
            # Fall back to dump-screen method (requires focus)
            async def do_read():
                args = ["dump-screen", "/dev/stdout"]
                if arguments.get("full"):
                    args.append("--full")
                return zellij_action(*args, capture=True, session=session)

            if pane_name:
                if pane_id is not None:
                    # Focus via plugin, then read
                    focus_result = plugin_command("focus", {"pane_id": pane_id}, session=session)
                    if focus_result.get("success"):
                        result = await do_read()
                        result["method"] = "plugin_focus"
                    else:
                        result = await with_pane_focus(pane_name, do_read, session=session)
                        result["method"] = "focus_fallback"
                else:
                    # Pane not found via plugin, try focus method
                    result = await with_pane_focus(pane_name, do_read, session=session)
                    result["method"] = "focus"
            else:
                result = await do_read()

            # Post-process output
            if result.get("success") and result.get("stdout"):
                content = result["stdout"]
                if do_strip:
                    content = strip_ansi(content)
                if tail_lines:
                    lines = content.split('\n')
                    content = '\n'.join(lines[-tail_lines:])
                result["content"] = content

    elif name == "list_panes":
        layout_result = zellij_action("dump-layout", capture=True, session=session)
        if layout_result.get("success"):
            panes = parse_layout_panes(layout_result.get("stdout", ""))
            result = {"success": True, "panes": panes}
        else:
            result = layout_result

    elif name == "focus_pane_by_name":
        target_name = arguments["name"].lower()
        layout_result = zellij_action("dump-layout", capture=True, session=session)
        if not layout_result.get("success"):
            result = layout_result
        else:
            panes = parse_layout_panes(layout_result.get("stdout", ""))
            target_pane = find_pane_by_name(panes, arguments["name"])

            if not target_pane:
                result = {"success": False, "error": f"Pane '{arguments['name']}' not found", "available": panes}
            elif target_pane.get("focused"):
                result = {"success": True, "message": "Pane already focused"}
            else:
                if not target_pane.get("tab_focused"):
                    tab_result = zellij_action("go-to-tab-name", target_pane["tab"], session=session)
                    if not tab_result.get("success"):
                        result = tab_result
                    else:
                        result = {"success": True, "message": f"Switched to tab '{target_pane['tab']}' containing pane", "pane": target_pane}
                else:
                    result = {"success": True, "message": "Pane is in current tab - use direction keys to navigate", "pane": target_pane}

    elif name == "write_to_pane":
        # Panes are in current session (tab-based workspaces)

        pane_name = arguments["pane_name"]
        chars = arguments["chars"]
        if arguments.get("press_enter"):
            chars += "\n"

        # Try plugin first (no focus stealing)
        if is_plugin_available():
            pane_id = plugin_find_pane_id(pane_name, session=session)
            if pane_id is not None:
                result = plugin_write_to_pane(pane_id, chars, session=session)
                if result.get("success"):
                    result["method"] = "plugin"
                else:
                    # Plugin failed, fall back to focus method
                    async def do_write():
                        return zellij_action("write-chars", chars, session=session)
                    result = await with_pane_focus(pane_name, do_write, session=session)
                    result["method"] = "focus_fallback"
            else:
                # Pane not found via plugin, try focus method
                async def do_write():
                    return zellij_action("write-chars", chars, session=session)
                result = await with_pane_focus(pane_name, do_write, session=session)
                result["method"] = "focus"
        else:
            # Plugin not available, use focus method
            async def do_write():
                return zellij_action("write-chars", chars, session=session)
            result = await with_pane_focus(pane_name, do_write, session=session)
            result["method"] = "focus"

    elif name == "send_keys":
        # Panes are in current session (tab-based workspaces)
        pane_name = arguments.get("pane_name")

        keys = arguments["keys"].lower()
        repeat = arguments.get("repeat", 1)

        if keys not in KEY_SEQUENCES:
            result = {"success": False, "error": f"Unknown key: {keys}",
                      "available": list(KEY_SEQUENCES.keys())}
        else:
            byte_seq = KEY_SEQUENCES[keys] * repeat
            byte_args = [str(b) for b in byte_seq]

            async def do_send():
                return zellij_action("write", *byte_args, session=session)

            if pane_name:
                # Try plugin first (no focus stealing)
                if is_plugin_available():
                    pane_id = plugin_find_pane_id(pane_name)
                    if pane_id is not None:
                        result = plugin_command("write_bytes", {"pane_id": pane_id, "bytes": byte_seq})
                        if result.get("success"):
                            result["method"] = "plugin"
                        else:
                            result = await with_pane_focus(pane_name, do_send, session=session)
                            result["method"] = "focus_fallback"
                    else:
                        result = await with_pane_focus(pane_name, do_send, session=session)
                        result["method"] = "focus"
                else:
                    result = await with_pane_focus(pane_name, do_send, session=session)
                    result["method"] = "focus"
            else:
                result = await do_send()

    elif name == "search_pane":
        # Panes are in current session (tab-based workspaces)
        pane_name = arguments.get("pane_name")

        pattern = arguments["pattern"]
        context = arguments.get("context", 0)

        async def do_read():
            args = ["dump-screen", "/dev/stdout"]
            if arguments.get("full", True):
                args.append("--full")
            return zellij_action(*args, capture=True, session=session)

        if pane_name:
            read_result = await with_pane_focus(pane_name, do_read, session=session)
        else:
            read_result = await do_read()

        if not read_result.get("success"):
            result = read_result
        else:
            content = strip_ansi(read_result.get("stdout", ""))
            lines = content.split('\n')
            matches = []
            try:
                regex = re.compile(pattern, re.IGNORECASE)
                for i, line in enumerate(lines):
                    if regex.search(line):
                        start = max(0, i - context)
                        end = min(len(lines), i + context + 1)
                        matches.append({
                            "line_number": i + 1,
                            "match": line,
                            "context": lines[start:end] if context > 0 else None,
                        })
                result = {"success": True, "matches": matches, "count": len(matches)}
            except re.error as e:
                result = {"success": False, "error": f"Invalid regex: {e}"}

    # === MONITORING ===
    elif name == "wait_for_output":
        # Panes are in current session (tab-based workspaces)
        pane_name = arguments.get("pane_name")

        pattern = arguments["pattern"]
        timeout = arguments.get("timeout", 30)
        poll_interval = arguments.get("poll_interval", 1.0)

        async def do_read():
            args = ["dump-screen", "/dev/stdout"]
            return zellij_action(*args, capture=True, session=session)

        start_time = time.time()
        try:
            regex = re.compile(pattern)
        except re.error as e:
            result = {"success": False, "error": f"Invalid regex: {e}"}
        else:
            matched = False
            match_text = None
            last_content = ""

            while time.time() - start_time < timeout:
                if pane_name:
                    read_result = await with_pane_focus(pane_name, do_read, session=session)
                else:
                    read_result = await do_read()

                if read_result.get("success"):
                    content = strip_ansi(read_result.get("stdout", ""))
                    last_content = content
                    match = regex.search(content)
                    if match:
                        matched = True
                        match_text = match.group(0)
                        break

                await asyncio.sleep(poll_interval)

            elapsed = time.time() - start_time
            result = {
                "success": True,
                "matched": matched,
                "match": match_text,
                "elapsed": round(elapsed, 2),
                "output": last_content[-2000:] if len(last_content) > 2000 else last_content,
            }

    elif name == "wait_for_idle":
        # Panes are in current session (tab-based workspaces)
        pane_name = arguments.get("pane_name")

        stable_seconds = arguments.get("stable_seconds", 3.0)
        timeout = arguments.get("timeout", 60)
        poll_interval = arguments.get("poll_interval", 1.0)

        async def do_read():
            args = ["dump-screen", "/dev/stdout"]
            return zellij_action(*args, capture=True, session=session)

        start_time = time.time()
        last_hash = None
        stable_since = None
        # Initialize result for safety (handles case where all reads fail)
        result = {"success": False, "error": "Timeout waiting for idle", "timeout": True}

        while time.time() - start_time < timeout:
            if pane_name:
                read_result = await with_pane_focus(pane_name, do_read, session=session)
            else:
                read_result = await do_read()

            if read_result.get("success"):
                content = strip_ansi(read_result.get("stdout", ""))
                current_hash = hashlib.md5(content.encode()).hexdigest()

                if current_hash == last_hash:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= stable_seconds:
                        result = {
                            "success": True,
                            "idle": True,
                            "elapsed": round(time.time() - start_time, 2),
                            "output": content[-2000:],
                        }
                        break
                else:
                    stable_since = None
                    last_hash = current_hash

            await asyncio.sleep(poll_interval)
        else:
            result = {
                "success": True,
                "idle": False,
                "elapsed": timeout,
                "message": "Timeout waiting for idle",
            }

    elif name == "tail_pane":
        # Panes are in current session (tab-based workspaces)
        pane_name = arguments.get("pane_name")
        reset = arguments.get("reset", False)

        # Try to get pane_id for daemon communication
        pane_id = None
        registered_pane = state.get_pane(pane_name) if pane_name else None
        if registered_pane and registered_pane.pane_id:
            pane_id = registered_pane.pane_id
        elif pane_name and is_plugin_available():
            pane_id = plugin_find_pane_id(pane_name, session=session)
            if pane_id and registered_pane:
                registered_pane.pane_id = pane_id

        # Try daemon first for focus-free reads (auto-start if needed, cross-session support)
        daemon_content = None
        method = "dump-screen"
        if pane_id and ensure_session_daemon(session):
            daemon_result = daemon_read_pane(pane_id, full=True, session=session)
            if daemon_result.get("success"):
                daemon_content = strip_ansi(daemon_result.get("content", ""))
                method = "daemon"

        if daemon_content is not None:
            # Use daemon content
            lines = daemon_content.split('\n')
            current_count = len(lines)
            cursor_key = f"{_resolve_session_key(session)}:{pane_name}"

            if reset:
                state.pane_cursors[cursor_key] = current_count
                result = {"success": True, "reset": True, "line_count": current_count,
                          "method": method}
            else:
                cursor = state.pane_cursors.get(cursor_key, 0)
                new_lines = lines[cursor:] if cursor < current_count else []
                state.pane_cursors[cursor_key] = current_count
                result = {
                    "success": True,
                    "new_output": '\n'.join(new_lines),
                    "lines_read": len(new_lines),
                    "method": method,
                }
        else:
            # Fall back to dump-screen method
            async def do_read():
                args = ["dump-screen", "/dev/stdout", "--full"]
                return zellij_action(*args, capture=True, session=session)

            if pane_name:
                read_result = await with_pane_focus(pane_name, do_read, session=session)
            else:
                read_result = await do_read()
                pane_name = "_focused"  # Use distinct key for focused pane

            if not read_result.get("success"):
                result = read_result
            else:
                content = strip_ansi(read_result.get("stdout", ""))
                lines = content.split('\n')
                current_count = len(lines)

                # Use session-qualified key to avoid collisions between same pane names in different sessions
                cursor_key = f"{_resolve_session_key(session)}:{pane_name}"

                if reset:
                    state.pane_cursors[cursor_key] = current_count
                    result = {"success": True, "reset": True, "line_count": current_count}
                else:
                    cursor = state.pane_cursors.get(cursor_key, 0)
                    new_lines = lines[cursor:] if cursor < current_count else []
                    state.pane_cursors[cursor_key] = current_count
                    result = {
                        "success": True,
                        "new_output": '\n'.join(new_lines),
                        "lines_read": len(new_lines),
                    }

    # === COMPOUND OPERATIONS ===
    elif name == "run_in_pane":
        # Panes are in current session (tab-based workspaces)

        pane_name = arguments["pane_name"]
        command = arguments["command"]
        wait = arguments.get("wait", True)
        timeout = arguments.get("timeout", 30)
        capture = arguments.get("capture", True)
        prompt_pattern = arguments.get("prompt_pattern", r"[\$#>]\s*$")

        # Write the command
        async def do_write():
            return zellij_action("write-chars", command + "\n", session=session)

        write_result = await with_pane_focus(pane_name, do_write, session=session)
        if not write_result.get("success"):
            result = write_result
        elif not wait:
            result = {"success": True, "message": "Command sent", "wait": False}
        else:
            # Wait for prompt using shared helper
            await asyncio.sleep(0.5)  # Initial delay
            wait_result = await wait_for_prompt(
                pane_name, prompt_pattern, timeout, session=session, check_lines=5
            )
            result = {
                "success": True,
                "completed": wait_result.get("completed", False),
                "elapsed": wait_result.get("elapsed", 0),
            }
            if capture:
                result["output"] = wait_result.get("output", "")

    elif name == "create_named_pane":
        # Use current session by default - pane creation doesn't work in detached sessions
        # User can explicitly specify session if needed
        # Invalidate cache since we may mutate layout
        layout_cache.invalidate(session)

        pane_name = arguments["name"]
        command = arguments.get("command")
        tab = arguments.get("tab")
        direction = arguments.get("direction")
        floating = arguments.get("floating", False)
        cwd = arguments.get("cwd")

        result = None  # Initialize to catch unexpected states

        # Check if pane already exists
        existing = state.get_pane(pane_name)
        if existing:
            # Verify it still exists in layout
            layout_result = zellij_action("dump-layout", capture=True, session=session)
            if layout_result.get("success"):
                panes = parse_layout_panes(layout_result.get("stdout", ""))
                found = find_pane_by_name(panes, pane_name)
                if found:
                    result = {"success": True, "exists": True, "pane": existing.__dict__}
                else:
                    state.unregister_pane(pane_name)
                    existing = None
            else:
                # Layout check failed, can't verify - assume stale and recreate
                state.unregister_pane(pane_name)
                existing = None

        if not existing:
            # Create the tab if needed
            if tab:
                layout_result = zellij_action("dump-layout", capture=True, session=session)
                tab_exists = False
                if layout_result.get("success"):
                    if f'name="{tab}"' in layout_result.get("stdout", ""):
                        tab_exists = True
                if not tab_exists:
                    zellij_action("new-tab", "--name", tab, session=session)
                else:
                    zellij_action("go-to-tab-name", tab, session=session)

            # Smart grid layout: calculate direction if not specified
            if not direction and not floating:
                # Count existing panes in session
                layout_result = zellij_action("dump-layout", capture=True, session=session)
                if layout_result.get("success"):
                    panes = parse_layout_panes(layout_result.get("stdout", ""))
                    direction = calculate_grid_direction(len(panes))

            # Count panes before creation (to determine new pane's index)
            # Use the target tab name (we know what tab we're creating in)
            target_tab_name = tab if tab else None
            pre_layout = zellij_action("dump-layout", capture=True, session=session)
            pre_pane_count = 0
            if pre_layout.get("success"):
                pre_panes = parse_layout_panes(pre_layout.get("stdout", ""))
                # If no explicit tab, find the focused tab
                if not target_tab_name:
                    for p in pre_panes:
                        if p.get("tab_focused"):
                            target_tab_name = p.get("tab")
                            break
                    if not target_tab_name and pre_panes:
                        target_tab_name = pre_panes[0].get("tab")
                # Count panes in TARGET tab (excluding plugins)
                tab_panes = [p for p in pre_panes
                             if p.get("tab") == target_tab_name
                             and not (p.get("command") and "plugin" in str(p.get("command", "")))]
                pre_pane_count = len(tab_panes)

            # Create the pane using 'action new-pane' (works in detached sessions)
            args = ["action", "new-pane", "--name", pane_name]
            if floating:
                args.append("--floating")
            if direction:
                args.extend(["--direction", direction])
            if cwd:
                args.extend(["--cwd", cwd])
            if command:
                args.extend(["--", command])
            create_result = run_zellij(*args, session=session)

            if create_result.get("success"):
                # Wait briefly for pane to initialize
                time.sleep(0.3)

                # Panes created with a command start suspended in Zellij.
                # Send Enter to start the command, then wait for shell to initialize.
                if command:
                    # Send Enter to unsuspend the pane (starts the command)
                    zellij_action("write", "13", session=session)  # 13 = Enter key
                    time.sleep(0.5)  # Wait for command to start

                # Try to get the pane ID for daemon communication
                pane_id = None
                if is_plugin_available():
                    pane_id = plugin_find_pane_id(pane_name, session=session)

                # New pane index = pre_pane_count (0-indexed)
                new_pane_index = pre_pane_count

                # Register in state with index for later lookup
                pane_info = state.register_pane(
                    name=pane_name,
                    tab=tab or target_tab_name or "current",
                    command=command,
                    cwd=cwd,
                    pane_index=new_pane_index,
                    floating=floating or False,
                    pane_id=pane_id,
                )
                result = {"success": True, "created": True, "pane": pane_info.__dict__,
                          "direction": direction}
                # Invalidate cache after pane creation
                layout_cache.invalidate(session)
            else:
                result = create_result

        # Safety check for unexpected state
        if result is None:
            result = {"success": False, "error": "Unexpected state in create_named_pane"}

    elif name == "destroy_named_pane":
        # Use current session - panes are now created in workspace tabs
        # Invalidate cache since we're mutating layout
        layout_cache.invalidate(session)

        pane_name = arguments["name"]

        # PROTECTION: Never close the Claude pane
        if "claude" in pane_name.lower():
            result = {"success": False, "error": "Cannot close Claude pane - this would terminate the session"}
        else:
            async def do_close():
                return zellij_action("close-pane", session=session)

            close_result = await with_pane_focus(pane_name, do_close, session=session)

            # Check if close actually succeeded - unregister only on success
            if not close_result.get("success"):
                result = close_result
            else:
                state.unregister_pane(pane_name)
                result = {"success": True, "closed": pane_name}
                layout_cache.invalidate(session)

    elif name == "list_named_panes":
        # Use current session by default (same as create_named_pane)

        # Get live layout
        layout_result = zellij_action("dump-layout", capture=True, session=session)
        live_panes = []
        if layout_result.get("success"):
            live_panes = parse_layout_panes(layout_result.get("stdout", ""))

        # Reconcile with registry
        pane_list = []
        for pane_name_key, pane in state.panes.items():
            found = find_pane_by_name(live_panes, pane_name_key, pane)
            pane_list.append({
                "name": pane_name_key,
                "tab": pane.tab,
                "command": pane.command,
                "alive": found is not None,
                "focused": found.get("focused") if found else False,
            })

        result = {"success": True, "panes": pane_list}

    # === REPL ===
    elif name == "repl_execute":
        # Panes are in current session (tab-based workspaces)

        pane_name = arguments["pane_name"]
        code = arguments["code"]
        repl_type = arguments.get("repl_type", "auto")
        timeout = arguments.get("timeout", 60)

        # Auto-detect REPL type from pane command
        if repl_type == "auto":
            pane_info = state.get_pane(pane_name)
            if pane_info and pane_info.command:
                cmd = pane_info.command.lower()
                if "ipython" in cmd:
                    repl_type = "ipython"
                elif "python" in cmd:
                    repl_type = "python"
                elif cmd in ("r", "rscript"):
                    repl_type = "r"
                elif "julia" in cmd:
                    repl_type = "julia"
                else:
                    repl_type = "default"
            else:
                repl_type = "default"

        prompt_pattern = REPL_PROMPTS.get(repl_type, REPL_PROMPTS["default"])

        # Send the code
        async def do_write():
            # For multi-line code, send as-is with trailing newlines
            text = code.strip() + "\n"
            if '\n' in code:
                text += "\n"  # Extra newline to close blocks
            return zellij_action("write-chars", text, session=session)

        write_result = await with_pane_focus(pane_name, do_write, session=session)
        if not write_result.get("success"):
            result = write_result
        else:
            # Wait for prompt using shared helper
            await asyncio.sleep(0.5)
            wait_result = await wait_for_prompt(
                pane_name, prompt_pattern, timeout, session=session, check_lines=3
            )
            result = {
                "success": True,
                "completed": wait_result.get("completed", False),
                "output": wait_result.get("output", ""),
                "repl_type": repl_type,
            }

    elif name == "repl_interrupt":
        # Panes are in current session (tab-based workspaces)

        pane_name = arguments["pane_name"]
        should_wait = arguments.get("wait_for_prompt", True)
        timeout = arguments.get("timeout", 10)

        # Send Ctrl+C
        async def do_interrupt():
            return zellij_action("write", "3", session=session)  # ASCII 3 = Ctrl+C

        int_result = await with_pane_focus(pane_name, do_interrupt, session=session)

        if not int_result.get("success"):
            result = int_result
        elif not should_wait:
            result = {"success": True, "interrupted": True}
        else:
            # Wait for prompt using shared helper
            await asyncio.sleep(0.3)
            prompt_result = await wait_for_prompt(
                pane_name, REPL_PROMPTS["default"], timeout, session=session, check_lines=3, poll_interval=0.5
            )
            result = {"success": True, "interrupted": True, "prompt_returned": prompt_result.get("completed", False)}

    # === SSH/HPC ===
    elif name == "ssh_connect":
        # Panes are in current session (tab-based workspaces)

        ssh_name = arguments["name"]
        host = arguments["host"]
        tab = arguments.get("tab")
        port = arguments.get("port")
        identity = arguments.get("identity_file")

        # Build SSH command as argument list (zellij expects separate args after --)
        ssh_args = ["ssh"]
        if port:
            ssh_args.extend(["-p", str(port)])
        if identity:
            ssh_args.extend(["-i", identity])
        ssh_args.append(host)
        ssh_cmd = " ".join(ssh_args)  # For display/registry

        # Create pane with SSH command using 'zellij run' to avoid suspended panes
        # 'zellij action new-pane' creates suspended panes in detached sessions
        args = ["run", "--name", ssh_name, "--"] + ssh_args

        if tab:
            layout_result = zellij_action("dump-layout", capture=True, session=session)
            if not layout_result.get("success"):
                # Layout check failed, try to create tab anyway
                zellij_action("new-tab", "--name", tab, session=session)
            elif f'name="{tab}"' not in layout_result.get("stdout", ""):
                zellij_action("new-tab", "--name", tab, session=session)
            else:
                zellij_action("go-to-tab-name", tab, session=session)

        create_result = run_zellij(*args, session=session)
        if create_result.get("success"):
            # Rename pane to ensure name persists in layout
            zellij_action("rename-pane", ssh_name, session=session)
            state.register_pane(name=ssh_name, tab=tab or "current", command=ssh_cmd)
            state.ssh_sessions[ssh_name] = SSHSession(
                name=ssh_name, host=host, pane_name=ssh_name
            )
            result = {"success": True, "connected": ssh_name, "host": host}
        else:
            result = create_result

    elif name == "ssh_run":
        # Panes are in current session (tab-based workspaces)

        ssh_name = arguments["name"]
        command = arguments["command"]
        wait = arguments.get("wait", True)
        timeout = arguments.get("timeout", 30)

        # Validate SSH session exists
        if ssh_name not in state.ssh_sessions:
            result = {"success": False, "error": f"SSH session '{ssh_name}' not found. Use ssh_connect first."}
        else:
            # Execute command in SSH pane
            async def do_write():
                return zellij_action("write-chars", command + "\n", session=session)

            write_result = await with_pane_focus(ssh_name, do_write, session=session)
            if not write_result.get("success"):
                result = write_result
            elif not wait:
                result = {"success": True, "sent": True}
            else:
                # Wait for prompt using shared helper
                await asyncio.sleep(0.5)
                wait_result = await wait_for_prompt(
                    ssh_name, r"[\$#>]\s*$", timeout, session=session, check_lines=1
                )
                result = {
                    "success": True,
                    "output": wait_result.get("output", ""),
                    "elapsed": wait_result.get("elapsed", 0),
                }

    elif name == "job_submit":
        # Panes are in current session (tab-based workspaces)

        ssh_name = arguments["ssh_name"]
        script = arguments["script"]
        scheduler = arguments.get("scheduler", "slurm")
        extra_args = arguments.get("extra_args", "")

        if scheduler == "slurm":
            cmd = f"sbatch {extra_args} {script}".strip()
            job_pattern = r"Submitted batch job (\d+)"
        else:  # pbs
            cmd = f"qsub {extra_args} {script}".strip()
            job_pattern = r"(\d+\.[\w.-]+)"

        # Run the command
        async def do_write():
            return zellij_action("write-chars", cmd + "\n", session=session)

        write_result = await with_pane_focus(ssh_name, do_write, session=session)
        if not write_result.get("success"):
            result = write_result
        else:
            await asyncio.sleep(2.0)

            async def do_read():
                return zellij_action("dump-screen", "/dev/stdout", capture=True, session=session)

            read_result = await with_pane_focus(ssh_name, do_read, session=session)

            if read_result.get("success"):
                content = strip_ansi(read_result.get("stdout", ""))
                match = re.search(job_pattern, content)
                if match:
                    job_id = match.group(1)
                    state.tracked_jobs[job_id] = TrackedJob(
                        job_id=job_id, scheduler=scheduler, ssh_name=ssh_name, script=script
                    )
                    result = {"success": True, "job_id": job_id, "output": content[-500:]}
                else:
                    result = {"success": False, "error": "Could not parse job ID", "output": content[-500:]}
            else:
                result = read_result

    elif name == "job_status":
        # Panes are in current session (tab-based workspaces)

        job_id = arguments.get("job_id")
        ssh_name = arguments.get("ssh_name")

        jobs_to_check = []
        if job_id:
            if job_id in state.tracked_jobs:
                jobs_to_check.append(state.tracked_jobs[job_id])
            elif ssh_name:
                # Untracked job with provided ssh_name - create temporary tracker
                jobs_to_check.append(TrackedJob(job_id=job_id, scheduler="slurm",
                                                ssh_name=ssh_name, script=""))
            else:
                # Can't check untracked job without ssh_name
                result = {"success": False, "error": f"Job '{job_id}' not found in tracker. Provide ssh_name to check untracked jobs."}
                jobs_to_check = []  # Skip the loop
        else:
            jobs_to_check = list(state.tracked_jobs.values())

        statuses = []
        for job in jobs_to_check:
            target_ssh = ssh_name or job.ssh_name
            if not target_ssh:
                statuses.append({"job_id": job.job_id, "error": "No SSH session specified"})
                continue

            if job.scheduler == "slurm":
                cmd = f"sacct -j {job.job_id} --format=JobID,State,Elapsed,ExitCode -n 2>/dev/null || squeue -j {job.job_id} -o '%i %T' --noheader"
            else:
                cmd = f"qstat {job.job_id}"

            async def do_write():
                return zellij_action("write-chars", cmd + "\n", session=session)

            await with_pane_focus(target_ssh, do_write, session=session)
            await asyncio.sleep(1.5)

            async def do_read():
                return zellij_action("dump-screen", "/dev/stdout", capture=True, session=session)

            read_result = await with_pane_focus(target_ssh, do_read, session=session)
            if read_result.get("success"):
                content = strip_ansi(read_result.get("stdout", ""))
                # Parse status from output
                lines = content.strip().split('\n')[-10:]
                statuses.append({"job_id": job.job_id, "output": '\n'.join(lines)})
            else:
                statuses.append({"job_id": job.job_id, "error": "Failed to read"})

        # Set result only if we processed jobs (error case already set result above)
        if jobs_to_check or statuses:
            result = {"success": True, "jobs": statuses}

    # === EDIT ===
    elif name == "edit_file":
        args = ["edit", arguments["path"]]
        if arguments.get("line"):
            args.extend(["--line", str(arguments["line"])])
        if arguments.get("floating"):
            args.append("--floating")
        if arguments.get("in_place"):
            args.append("--in-place")
        if arguments.get("direction"):
            args.extend(["--direction", arguments["direction"]])
        result = zellij_action(*args, session=session)

    elif name == "edit_scrollback":
        result = zellij_action("edit-scrollback", session=session)

    # === SESSION ===
    elif name == "list_sessions":
        result = run_zellij("list-sessions", capture=True)

    elif name == "list_clients":
        result = zellij_action("list-clients", capture=True, session=session)

    elif name == "session_info":
        sess_name = session or os.environ.get("ZELLIJ_SESSION_NAME", "unknown")
        result = {"success": True, "session": sess_name, "pane_id": os.environ.get("ZELLIJ_PANE_ID", "unknown")}

    elif name == "rename_session":
        result = zellij_action("rename-session", arguments["name"], session=session)

    elif name == "session_map":
        compact = arguments.get("compact", False)
        current_session = os.environ.get("ZELLIJ_SESSION_NAME", "")

        # Get all sessions
        sessions_result = run_zellij("list-sessions", capture=True)
        if not sessions_result.get("success"):
            result = sessions_result
        else:
            # Parse session names from output
            session_names = []
            for line in sessions_result.get("stdout", "").split('\n'):
                line = strip_ansi(line).strip()
                if line:
                    # Format: "session-name [Created Xs ago] (current)"
                    name_match = re.match(r'^(\S+)', line)
                    if name_match:
                        session_names.append({
                            "name": name_match.group(1),
                            "current": "(current)" in line,
                        })

            # Build map for each session
            session_maps = []
            for sess in session_names:
                sess_name = sess["name"]
                layout_result = zellij_action("dump-layout", capture=True, session=sess_name)
                if not layout_result.get("success"):
                    continue

                layout = layout_result.get("stdout", "")

                # Parse cwd
                cwd_match = re.search(r'cwd\s+"([^"]+)"', layout)
                cwd = cwd_match.group(1) if cwd_match else ""
                # Shorten cwd for display
                if len(cwd) > 50:
                    cwd = "..." + cwd[-47:]

                # Parse tabs and their panes
                tabs = []
                current_tab = None

                for line in layout.split('\n'):
                    # Match tab lines
                    tab_match = re.search(r'tab\s+name="([^"]+)".*?(focus=true)?', line)
                    if tab_match and 'swap_' not in line and 'new_tab_template' not in layout[max(0, layout.find(line)-50):layout.find(line)]:
                        # Check this isn't inside swap_tiled_layout or new_tab_template
                        line_pos = layout.find(line)
                        before = layout[:line_pos]
                        if 'swap_tiled_layout' in before.split('tab')[-1] if 'tab' in before else '':
                            continue
                        if before.count('{') - before.count('}') > 2:  # Deep nesting = template
                            continue

                        if current_tab:
                            tabs.append(current_tab)
                        current_tab = {
                            "name": tab_match.group(1),
                            "focused": tab_match.group(2) is not None,
                            "panes": [],
                            "floating": [],
                        }
                        continue

                    if current_tab:
                        # Match pane with command
                        cmd_match = re.search(r'pane\s+command="([^"]+)"', line)
                        if cmd_match and 'plugin' not in line:
                            pane_info = cmd_match.group(1)
                            # Check for name
                            name_match = re.search(r'name="([^"]+)"', line)
                            if name_match:
                                pane_info = f'"{name_match.group(1)}" ({pane_info})'
                            if 'start_suspended' in line:
                                pane_info += " [suspended]"
                            if 'floating_panes' in layout[max(0, layout.find(line)-200):layout.find(line)]:
                                current_tab["floating"].append(pane_info)
                            else:
                                current_tab["panes"].append(pane_info)
                        # Match named pane without command
                        elif 'pane' in line and 'plugin' not in line and 'size=' not in line.split('pane')[0]:
                            name_match = re.search(r'name="([^"]+)"', line)
                            if name_match:
                                pane_info = f'"{name_match.group(1)}"'
                                if 'floating_panes' in layout[max(0, layout.find(line)-200):layout.find(line)]:
                                    current_tab["floating"].append(pane_info)
                                else:
                                    current_tab["panes"].append(pane_info)

                if current_tab:
                    tabs.append(current_tab)

                session_maps.append({
                    "name": sess_name,
                    "current": sess["current"],
                    "cwd": cwd,
                    "tabs": tabs,
                })

            # ANSI colors
            O = "\033[38;5;208m"  # orange
            G = "\033[38;5;82m"   # green
            D = "\033[38;5;240m"  # dim
            B = "\033[1m"         # bold
            R = "\033[0m"         # reset

            def parse_layout_tree(layout_str: str) -> dict:
                """Parse layout string into a tree structure for a tab."""
                lines_iter = iter(layout_str.split('\n'))

                def extract_attrs(line: str) -> dict:
                    """Extract attributes from a pane line."""
                    attrs = {"command": None, "name": None, "size": None, "focused": False,
                             "split": "horizontal"}
                    if 'split_direction="vertical"' in line:
                        attrs["split"] = "vertical"
                    if 'focus=true' in line and 'hide_floating' not in line:
                        attrs["focused"] = True
                    cmd = re.search(r'command="([^"]+)"', line)
                    if cmd:
                        attrs["command"] = cmd.group(1)
                    name = re.search(r'name="([^"]+)"', line)
                    if name and 'tab ' not in line:
                        attrs["name"] = name.group(1)
                    size = re.search(r'size="?(\d+%?)"?', line)
                    if size:
                        attrs["size"] = size.group(1)
                    return attrs

                def parse_pane(initial_attrs=None):
                    """Recursively parse pane structure."""
                    pane = {"type": "pane", "children": []}
                    if initial_attrs:
                        pane.update(initial_attrs)
                    else:
                        pane.update({"name": None, "command": None, "split": "horizontal",
                                     "size": None, "focused": False})

                    for line in lines_iter:
                        stripped = line.strip()
                        if not stripped:
                            continue

                        # End of block
                        if stripped == '}':
                            return pane

                        # Skip plugins and their content
                        if 'plugin' in stripped:
                            brace_count = stripped.count('{') - stripped.count('}')
                            while brace_count > 0:
                                next_line = next(lines_iter, '')
                                brace_count += next_line.count('{') - next_line.count('}')
                            continue

                        # Skip non-pane lines (args, start_suspended, etc.)
                        if not stripped.startswith('pane'):
                            continue

                        # Skip borderless panes (plugin containers like tab-bar, status-bar)
                        if 'borderless=true' in stripped:
                            if '{' in stripped:
                                brace_count = 1
                                while brace_count > 0:
                                    next_line = next(lines_iter, '')
                                    brace_count += next_line.count('{') - next_line.count('}')
                            continue

                        # Skip floating_panes section
                        if 'floating_panes' in stripped:
                            if '{' in stripped:
                                brace_count = 1
                                while brace_count > 0:
                                    next_line = next(lines_iter, '')
                                    brace_count += next_line.count('{') - next_line.count('}')
                            continue

                        # Pane with braces - recursive parse
                        if '{' in stripped:
                            attrs = extract_attrs(stripped)
                            child = parse_pane(attrs)
                            # Keep panes with children, commands, names, or percentage sizes
                            if child.get("children") or child.get("command") or child.get("name"):
                                pane["children"].append(child)
                            elif child.get("size") and '%' in str(child.get("size", "")):
                                # Percentage-sized pane without command = shell
                                pane["children"].append(child)
                        else:
                            # Leaf pane without braces (e.g., "pane size="50%"")
                            attrs = extract_attrs(stripped)
                            leaf = {"type": "pane", "children": [], **attrs}
                            pane["children"].append(leaf)

                    return pane

                return parse_pane()

            def render_layout(pane: dict, width: int, height: int, x: int = 0, y: int = 0) -> list:
                """Render pane tree into a 2D grid. Returns list of (x, y, w, h, label, focused)."""
                cells = []
                children = pane.get("children", [])

                if not children:
                    # Leaf pane - show it
                    label = pane.get("command") or pane.get("name") or "shell"
                    label = label.split('/')[-1][:12]
                    cells.append((x, y, width, height, label, pane.get("focused", False)))
                else:
                    # Container - split children based on direction
                    is_vertical = pane.get("split") == "vertical"  # vertical = side by side
                    n = len(children)

                    # Calculate sizes from percentages or divide equally
                    sizes = []
                    for c in children:
                        sz = c.get("size")
                        if sz and sz.endswith('%'):
                            sizes.append(int(sz[:-1]))
                        elif sz and sz.isdigit():
                            sizes.append(int(sz))
                        else:
                            sizes.append(0)

                    # Normalize sizes - distribute remaining to unspecified
                    total = sum(sizes)
                    unspec = sizes.count(0)
                    if total < 100 and unspec > 0:
                        remaining = 100 - total
                        for i in range(len(sizes)):
                            if sizes[i] == 0:
                                sizes[i] = remaining // unspec

                    # Render children
                    if is_vertical:
                        # Horizontal arrangement (side by side)
                        cx = x
                        for i, child in enumerate(children):
                            cw = max(1, (width * sizes[i]) // 100) if sum(sizes) > 0 else width // n
                            if i == n - 1:  # Last child takes remaining space
                                cw = width - (cx - x)
                            cells.extend(render_layout(child, cw, height, cx, y))
                            cx += cw
                    else:
                        # Vertical arrangement (stacked)
                        cy = y
                        for i, child in enumerate(children):
                            ch = max(1, (height * sizes[i]) // 100) if sum(sizes) > 0 else height // n
                            if i == n - 1:
                                ch = height - (cy - y)
                            cells.extend(render_layout(child, width, ch, x, cy))
                            cy += ch

                return cells

            def draw_grid(cells: list, width: int, height: int) -> list:
                """Draw cells as ASCII grid with proper box characters."""
                # Initialize grid and metadata
                grid = [[' ' for _ in range(width)] for _ in range(height)]
                colors = [[None for _ in range(width)] for _ in range(height)]

                # Sort cells by area (draw smaller on top)
                cells = sorted(cells, key=lambda c: c[2] * c[3], reverse=True)

                for cx, cy, cw, ch, label, focused in cells:
                    if cw < 3 or ch < 1:
                        continue

                    # Draw horizontal borders
                    for i in range(cx, min(cx + cw, width)):
                        if cy < height:
                            grid[cy][i] = '─'
                            if focused:
                                colors[cy][i] = O
                        if cy + ch - 1 < height and ch > 1:
                            grid[cy + ch - 1][i] = '─'
                            if focused:
                                colors[cy + ch - 1][i] = O

                    # Draw vertical borders
                    for j in range(cy, min(cy + ch, height)):
                        if cx < width:
                            grid[j][cx] = '│'
                            if focused:
                                colors[j][cx] = O
                        if cx + cw - 1 < width:
                            grid[j][cx + cw - 1] = '│'
                            if focused:
                                colors[j][cx + cw - 1] = O

                    # Corners
                    if cy < height and cx < width:
                        grid[cy][cx] = '┌'
                    if cy < height and cx + cw - 1 < width:
                        grid[cy][cx + cw - 1] = '┐'
                    if cy + ch - 1 < height and cx < width:
                        grid[cy + ch - 1][cx] = '└'
                    if cy + ch - 1 < height and cx + cw - 1 < width:
                        grid[cy + ch - 1][cx + cw - 1] = '┘'

                    # Label (centered)
                    label_y = cy + max(1, ch // 2) if ch > 1 else cy
                    max_label_len = cw - 2
                    if max_label_len > 0:
                        disp_label = label[:max_label_len]
                        label_x = cx + 1 + (max_label_len - len(disp_label)) // 2
                        for k, char in enumerate(disp_label):
                            if label_x + k < width:
                                grid[label_y][label_x + k] = char
                                if focused:
                                    colors[label_y][label_x + k] = O

                # Fix overlapping corners
                for y in range(height):
                    for x in range(width):
                        c = grid[y][x]
                        # Check neighbors for T-junctions
                        has_up = y > 0 and grid[y-1][x] in '│┌┐├┤┬┴┼'
                        has_down = y < height-1 and grid[y+1][x] in '│└┘├┤┬┴┼'
                        has_left = x > 0 and grid[y][x-1] in '─┌└├┬┴┼'
                        has_right = x < width-1 and grid[y][x+1] in '─┐┘┤┬┴┼'

                        if c in '┌┐└┘─│':
                            if has_up and has_down and has_left and has_right:
                                grid[y][x] = '┼'
                            elif has_up and has_down and has_right:
                                grid[y][x] = '├'
                            elif has_up and has_down and has_left:
                                grid[y][x] = '┤'
                            elif has_left and has_right and has_down:
                                grid[y][x] = '┬'
                            elif has_left and has_right and has_up:
                                grid[y][x] = '┴'

                # Build output with colors
                lines = []
                for y in range(height):
                    line = ""
                    for x in range(width):
                        if colors[y][x]:
                            line += colors[y][x] + grid[y][x] + R
                        else:
                            line += grid[y][x]
                    lines.append(line)

                return lines

            lines = []

            for i, sess in enumerate(session_maps):
                name = sess["name"]
                is_agent = name == "zellij-agent"
                is_current = sess["current"]

                # Session header
                badge = f" {O}[AGENT]{R}" if is_agent else ""
                badge_len = 8 if is_agent else 0
                status = f"{G}●{R}" if is_current else f"{D}○{R}"

                header = f"{status} {B}{name}{R}{badge}"
                header_len = 2 + len(name) + badge_len

                lines.append(f"{O}╔{'═' * 62}╗{R}")
                lines.append(f"{O}║{R} {header}{' ' * (60 - header_len)} {O}║{R}")
                lines.append(f"{O}╚{'═' * 62}╝{R}")

                # Get full layout for this session
                layout_result = zellij_action("dump-layout", capture=True, session=name)
                layout_str = layout_result.get("stdout", "") if layout_result.get("success") else ""

                # Parse tabs from layout
                for tab in sess["tabs"]:
                    tname = tab["name"]
                    is_focused = tab["focused"]

                    focus = f"{O}►{R} " if is_focused else "  "
                    tab_status = f"{G}active{R}" if is_focused else f"{D}idle{R}"
                    lines.append(f"  {focus}{D}[{R}{tname}{D}]{R} {tab_status}")

                    if not compact:
                        # Extract tab section from layout
                        tab_pattern = rf'tab name="{re.escape(tname)}"[^{{]*\{{'
                        tab_match = re.search(tab_pattern, layout_str)

                        if tab_match:
                            # Find the matching closing brace
                            start = tab_match.end()
                            brace_count = 1
                            end = start
                            while brace_count > 0 and end < len(layout_str):
                                if layout_str[end] == '{':
                                    brace_count += 1
                                elif layout_str[end] == '}':
                                    brace_count -= 1
                                end += 1

                            tab_content = layout_str[start:end-1]

                            # Parse and render
                            tree = parse_layout_tree(tab_content)
                            cells = render_layout(tree, 60, 6, 0, 0)

                            if cells:
                                grid_lines = draw_grid(cells, 60, 6)
                                for gl in grid_lines:
                                    lines.append(f"  {D}{gl}{R}")

                        # Floating panes
                        if tab["floating"]:
                            fl_names = ", ".join(f[:15] for f in tab["floating"][:3])
                            if len(tab["floating"]) > 3:
                                fl_names += f" +{len(tab['floating'])-3}"
                            lines.append(f"  {O}~{R} floating: {fl_names}")

                lines.append("")

            # Legend
            lines.append(f"{O}●{R} current  {D}○{R} idle  {O}►{R} focused tab  {O}~{R} floating")

            result = {
                "success": True,
                "map": "\n".join(lines),
                "sessions": session_maps,
            }

    elif name == "session_attach":
        action = arguments.get("action", "list")
        target_session = arguments.get("session")

        if action == "list":
            attachments = session_manager.list_attachments()
            result = {
                "success": True,
                "attachments": attachments,
                "count": len(attachments),
            }
        elif action == "attach":
            if not target_session:
                result = {"success": False, "error": "Session name required for attach"}
            else:
                result = session_manager.headless_attach(target_session)
                if result.get("success"):
                    # Also start daemon in the newly attached session
                    daemon_result = start_daemon(target_session)
                    result["daemon"] = daemon_result
        elif action == "detach":
            if not target_session:
                result = {"success": False, "error": "Session name required for detach"}
            else:
                result = session_manager.detach(target_session)
        elif action == "detach_all":
            session_manager.cleanup_all()
            result = {"success": True, "message": "All attachments cleaned up"}
        else:
            result = {"success": False, "error": f"Unknown action: {action}"}

    # === LAYOUT ===
    elif name == "dump_layout":
        result = zellij_action("dump-layout", capture=True, session=session)


    elif name == "swap_layout":
        direction = arguments.get("direction", "next")
        if direction == "next":
            result = zellij_action("next-swap-layout", session=session)
        else:
            result = zellij_action("previous-swap-layout", session=session)

    # === MODE ===
    elif name == "switch_mode":
        result = zellij_action("switch-mode", arguments["mode"], session=session)

    # === PLUGINS ===
    elif name == "launch_plugin":
        args = ["launch-plugin", arguments["url"]]
        if arguments.get("floating"):
            args.append("--floating")
        if arguments.get("in_place"):
            args.append("--in-place")
        if arguments.get("skip_cache"):
            args.append("--skip-plugin-cache")
        if arguments.get("configuration"):
            for k, v in arguments["configuration"].items():
                args.extend(["--configuration", f"{k}={v}"])
        result = zellij_action(*args, session=session)

    elif name == "pipe":
        args = ["pipe"]
        if arguments.get("name"):
            args.extend(["--name", arguments["name"]])
        if arguments.get("payload"):
            args.extend(["--payload", arguments["payload"]])
        if arguments.get("plugin"):
            args.extend(["--plugin", arguments["plugin"]])
        if arguments.get("args"):
            args.extend(["--args", *arguments["args"]])
        result = zellij_action(*args, session=session)

    # === WORKSPACE MANAGEMENT ===
    elif name == "agent_session":
        # DEPRECATED: Agent session isolation doesn't work (Zellij can't create
        # panes in detached sessions). Now uses tab-based workspaces instead.
        action = arguments.get("action", "status")

        if action == "create":
            # Instead of creating a separate session, create a workspace tab
            workspace_tab = DEFAULT_WORKSPACE_TAB
            layout_result = zellij_action("dump-layout", capture=True, session=session)
            tab_exists = False
            if layout_result.get("success"):
                if f'name="{workspace_tab}"' in layout_result.get("stdout", ""):
                    tab_exists = True
            if not tab_exists:
                zellij_action("new-tab", "--name", workspace_tab, session=session)
            result = {
                "success": True,
                "workspace_tab": workspace_tab,
                "message": f"Workspace tab '{workspace_tab}' ready in current session",
                "note": "Session isolation deprecated - using tab-based workspaces now"
            }
        elif action == "status":
            sessions = get_active_sessions()
            # Check for workspace tabs in current session
            layout_result = zellij_action("dump-layout", capture=True, session=session)
            workspace_tabs = []
            if layout_result.get("success"):
                layout = layout_result.get("stdout", "")
                # Find all tab names
                for match in re.finditer(r'tab name="([^"]+)"', layout):
                    workspace_tabs.append(match.group(1))
            result = {
                "success": True,
                "workspace_tabs": workspace_tabs,
                "all_sessions": sessions,
                "note": "Using tab-based workspaces (session isolation deprecated)"
            }
        elif action == "destroy":
            # Close the workspace tab if it exists
            workspace_tab = DEFAULT_WORKSPACE_TAB
            zellij_action("go-to-tab-name", workspace_tab, session=session)
            time.sleep(0.2)
            zellij_action("close-tab", session=session)
            result = {
                "success": True,
                "closed_tab": workspace_tab,
                "note": "Closed workspace tab (session isolation deprecated)"
            }
        else:
            result = {"success": False, "error": f"Unknown action: {action}"}

    elif name == "spawn_agents":
        # Spawn multiple Claude agents in parallel in current session (tab-based workspaces)
        tasks = arguments.get("tasks", [])
        tab = arguments.get("tab", "agents")
        skip_permissions = arguments.get("dangerously_skip_permissions", False)

        if not tasks:
            result = {"success": False, "error": "No tasks provided"}
        else:
            # Create tab for agents if needed
            layout_result = zellij_action("dump-layout", capture=True, session=session)
            if layout_result.get("success") and f'name="{tab}"' not in layout_result.get("stdout", ""):
                zellij_action("new-tab", "--name", tab, session=session)
            else:
                zellij_action("go-to-tab-name", tab, session=session)

            spawned = []
            for i, task in enumerate(tasks):
                task_name = task.get("name", f"agent-{i}")
                prompt = task.get("prompt", "")
                model = task.get("model", "claude-sonnet-4-22k-0514")
                cwd = task.get("cwd")

                # Build claude command
                claude_args = ["claude", "--model", model, "--print"]
                if skip_permissions:
                    claude_args.append("--dangerously-skip-permissions")

                # Escape prompt for shell
                escaped_prompt = prompt.replace("'", "'\"'\"'")
                claude_cmd = " ".join(claude_args) + f" '{escaped_prompt}'"

                # Determine direction for grid layout
                direction = calculate_grid_direction(i)

                # Create pane with claude command
                pane_name = f"agent-{task_name}"
                args = ["run", "--name", pane_name]
                if direction and i > 0:
                    args.extend(["--direction", direction])
                if cwd:
                    args.extend(["--cwd", cwd])
                args.extend(["--", "bash", "-c", claude_cmd])

                create_result = run_zellij(*args, session=session)

                if create_result.get("success"):
                    # Register the agent
                    agent = SpawnedAgent(
                        name=task_name,
                        pane_name=pane_name,
                        task=task_name,
                        prompt=prompt,
                        model=model,
                        tab=tab,
                    )
                    with state._lock:
                        state.spawned_agents[task_name] = agent

                    # Also register as pane for read operations
                    state.register_pane(pane_name, tab, command="claude")

                    spawned.append({
                        "name": task_name,
                        "pane": pane_name,
                        "model": model,
                        "prompt": prompt[:100] + "..." if len(prompt) > 100 else prompt,
                    })

            result = {
                "success": True,
                "spawned": len(spawned),
                "agents": spawned,
                "tab": tab,
                "session": session,
            }
            layout_cache.invalidate(session)

    elif name == "list_spawned_agents":
        with state._lock:
            agents_list = []
            for name, agent in state.spawned_agents.items():
                agents_list.append({
                    "name": agent.name,
                    "pane": agent.pane_name,
                    "task": agent.task,
                    "model": agent.model,
                    "status": agent.status,
                    "spawned_at": agent.spawned_at,
                    "tab": agent.tab,
                    "prompt": agent.prompt[:80] + "..." if len(agent.prompt) > 80 else agent.prompt,
                })
        result = {"success": True, "agents": agents_list, "count": len(agents_list)}

    elif name == "agent_output":
        agent_name = arguments["name"]
        tail_lines = arguments.get("tail", 50)

        with state._lock:
            agent = state.spawned_agents.get(agent_name)

        if not agent:
            result = {"success": False, "error": f"Agent '{agent_name}' not found"}
        else:
            # Panes are in current session (tab-based workspaces)
            async def do_read():
                args = ["dump-screen", "/dev/stdout", "--full"]
                return zellij_action(*args, capture=True, session=session)

            read_result = await with_pane_focus(agent.pane_name, do_read, session=session)

            if read_result.get("success"):
                content = strip_ansi(read_result.get("stdout", ""))
                lines = content.split('\n')
                if tail_lines:
                    lines = lines[-tail_lines:]
                result = {
                    "success": True,
                    "agent": agent_name,
                    "output": '\n'.join(lines),
                    "lines": len(lines),
                }
            else:
                result = read_result

    elif name == "stop_agent":
        agent_name = arguments["name"]

        with state._lock:
            agent = state.spawned_agents.get(agent_name)

        if not agent:
            result = {"success": False, "error": f"Agent '{agent_name}' not found"}
        else:
            # Panes are in current session (tab-based workspaces)
            async def do_interrupt():
                return zellij_action("write", "3", session=session)  # Ctrl+C

            interrupt_result = await with_pane_focus(agent.pane_name, do_interrupt, session=session)

            if interrupt_result.get("success"):
                with state._lock:
                    if agent_name in state.spawned_agents:
                        state.spawned_agents[agent_name].status = "stopped"
                result = {"success": True, "stopped": agent_name}
            else:
                result = interrupt_result

    else:
        result = {"success": False, "error": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
