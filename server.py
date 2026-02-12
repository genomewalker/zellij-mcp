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
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("zellij-mcp")


# =============================================================================
# AGENT SESSION - Isolated workspace for autonomous operations
# =============================================================================

AGENT_SESSION = "zellij-agent"  # Dedicated session for agent work


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
                      cwd: str = None, repl_type: str = None) -> PaneInfo:
        """Register a named pane. Thread-safe."""
        pane = PaneInfo(name=name, tab=tab, command=command, cwd=cwd, repl_type=repl_type)
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

    for line in layout_text.split('\n'):
        stripped = line.strip()

        # Track floating panes section with brace depth
        if 'floating_panes' in stripped:
            floating_depth = 1
            # Count opening braces on this line
            floating_depth += stripped.count('{') - 1  # -1 because we start at 1
            continue
        if floating_depth > 0:
            floating_depth += stripped.count('{')
            floating_depth -= stripped.count('}')
            if floating_depth <= 0:
                floating_depth = 0
                continue

        in_floating = floating_depth > 0

        # Match tab lines
        tab_match = re.search(r'tab\s+name="([^"]+)".*?(focus=true)?', line)
        if tab_match:
            current_tab = tab_match.group(1)
            current_tab_index += 1
            tab_focused = tab_match.group(2) is not None
            pane_index_in_tab = 0
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

            # Include all panes to allow index-based navigation
            panes.append(pane_info)

    return panes


def find_pane_by_name(panes: list[dict], name: str) -> Optional[dict]:
    """Find a pane by name or command (case-insensitive partial match)."""
    name_lower = name.lower()
    for p in panes:
        pane_name = p.get("name", "").lower()
        pane_cmd = p.get("command", "").lower()
        if name_lower in pane_name or name_lower in pane_cmd:
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
    target_pane = find_pane_by_name(panes, pane_name)

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
    """Add session parameter to all tool schemas."""
    for tool in tools:
        tool.inputSchema["properties"] = {
            **tool.inputSchema.get("properties", {}),
            **SESSION_PARAM,
        }
    return tools


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
        # === AGENT SESSION ===
        Tool(
            name="agent_session",
            description="Manage the isolated agent session (zellij-agent). Creates if needed. Returns session info.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "create", "destroy"],
                               "description": "Action: status (default), create, or destroy", "default": "status"},
                },
            },
        ),
        Tool(
            name="spawn_agents",
            description="Spawn multiple Claude agents in parallel, each working on a task. Creates panes in agent session.",
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
        # Use agent session by default when targeting named panes
        pane_name = arguments.get("pane_name")
        if pane_name:
            session = session or get_agent_session()

        do_strip = arguments.get("strip_ansi", True)
        tail_lines = arguments.get("tail")

        async def do_read():
            args = ["dump-screen", "/dev/stdout"]
            if arguments.get("full"):
                args.append("--full")
            return zellij_action(*args, capture=True, session=session)

        if pane_name:
            result = await with_pane_focus(pane_name, do_read, session=session)
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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

        pane_name = arguments["pane_name"]
        chars = arguments["chars"]
        if arguments.get("press_enter"):
            chars += "\n"

        async def do_write():
            return zellij_action("write-chars", chars, session=session)

        result = await with_pane_focus(pane_name, do_write, session=session)

    elif name == "send_keys":
        # Use agent session by default when targeting named panes
        pane_name = arguments.get("pane_name")
        if pane_name:
            session = session or get_agent_session()

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
                result = await with_pane_focus(pane_name, do_send, session=session)
            else:
                result = await do_send()

    elif name == "search_pane":
        # Use agent session by default when targeting named panes
        pane_name = arguments.get("pane_name")
        if pane_name:
            session = session or get_agent_session()

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
        # Use agent session by default when targeting named panes
        pane_name = arguments.get("pane_name")
        if pane_name:
            session = session or get_agent_session()

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
        # Use agent session by default when targeting named panes
        pane_name = arguments.get("pane_name")
        if pane_name:
            session = session or get_agent_session()

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
        pane_name = arguments.get("pane_name")
        # Use agent session by default when targeting named panes
        if pane_name:
            session = session or get_agent_session()

        reset = arguments.get("reset", False)

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()
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

            # Create the pane
            # Use 'zellij run' when command specified to avoid suspended panes in detached sessions
            # 'zellij action new-pane' creates suspended panes, 'zellij run' starts immediately
            if command:
                args = ["run", "--name", pane_name]
                if floating:
                    args.append("--floating")
                if direction:
                    args.extend(["--direction", direction])
                if cwd:
                    args.extend(["--cwd", cwd])
                # Complex commands need shell wrapping
                if any(c in command for c in [' ', '|', '&', ';', '>', '<', '$', '`']):
                    args.extend(["--", "bash", "-c", command])
                else:
                    args.extend(["--", command])
                create_result = run_zellij(*args, session=session)
            else:
                # No command - use new-pane for an empty shell
                args = ["action", "new-pane", "--name", pane_name]
                if floating:
                    args.append("--floating")
                if direction:
                    args.extend(["--direction", direction])
                if cwd:
                    args.extend(["--cwd", cwd])
                create_result = run_zellij(*args, session=session)
            if create_result.get("success"):
                # Rename pane to ensure name persists in layout
                zellij_action("rename-pane", pane_name, session=session)
                # Register in state
                pane_info = state.register_pane(
                    name=pane_name,
                    tab=tab or "current",
                    command=command,
                    cwd=cwd,
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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()
        # Invalidate cache since we're mutating layout
        layout_cache.invalidate(session)

        pane_name = arguments["name"]

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
        # Use agent session by default for named pane operations
        session = session or get_agent_session()

        # Get live layout
        layout_result = zellij_action("dump-layout", capture=True, session=session)
        live_panes = []
        if layout_result.get("success"):
            live_panes = parse_layout_panes(layout_result.get("stdout", ""))

        # Reconcile with registry
        pane_list = []
        for name, pane in state.panes.items():
            found = find_pane_by_name(live_panes, name)
            pane_list.append({
                "name": name,
                "tab": pane.tab,
                "command": pane.command,
                "alive": found is not None,
                "focused": found.get("focused") if found else False,
            })

        result = {"success": True, "panes": pane_list}

    # === REPL ===
    elif name == "repl_execute":
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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
        # Use agent session by default to avoid stealing focus
        session = session or get_agent_session()

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

            W = 68  # inner width

            def row(content: str, visible_len: int) -> str:
                """Build a row with proper padding."""
                pad = W - visible_len
                return f"{D}│{R} {content}{' ' * pad} {D}│{R}"

            def sep(char: str = "─", left: str = "├", right: str = "┤") -> str:
                return f"{D}{left}{char * W}{right}{R}"

            lines = []
            lines.append(f"{D}┌{'─' * W}┐{R}")
            lines.append(row(f"{O}{B}SESSION MAP{R}", 11))
            lines.append(sep())

            for i, sess in enumerate(session_maps):
                name = sess["name"]
                is_agent = name == "zellij-agent"
                is_current = sess["current"]

                # Build session line: "  name [AGENT]          ● ACTIVE"
                left = f"  {B}{name}{R}"
                left_len = 2 + len(name)

                if is_agent:
                    left += f" {O}[AGENT]{R}"
                    left_len += 8

                if is_current:
                    right = f"{G}● ACTIVE{R}"
                    right_len = 8
                else:
                    right = f"{D}○ idle{R}"
                    right_len = 6

                mid_pad = W - left_len - right_len
                lines.append(f"{D}│{R}{left}{' ' * mid_pad}{right}{D}│{R}")

                # CWD
                if sess["cwd"]:
                    cwd = sess["cwd"]
                    max_cwd = W - 8
                    if len(cwd) > max_cwd:
                        cwd = "..." + cwd[-(max_cwd - 3):]
                    cwd_line = f"    {D}└─{R} {cwd}"
                    lines.append(row(cwd_line, 7 + len(cwd)))

                # Tabs
                for tab in sess["tabs"]:
                    is_focused = tab["focused"]
                    tname = tab["name"]

                    if is_focused:
                        tab_line = f"    {O}►{R} [{tname}]"
                        tab_len = 7 + len(tname)
                    else:
                        tab_line = f"      [{tname}]"
                        tab_len = 8 + len(tname)

                    lines.append(row(tab_line, tab_len))

                    if not compact:
                        panes = tab["panes"] if tab["panes"] else ["shell"]
                        for j, pane in enumerate(panes[:5]):
                            max_pane = W - 14
                            p = pane[:max_pane] if len(pane) > max_pane else pane
                            pane_line = f"        {D}├─{R} {p}"
                            lines.append(row(pane_line, 12 + len(p)))

                        if len(panes) > 5:
                            more = f"+{len(panes) - 5} more"
                            lines.append(row(f"        {D}└─{R} {more}", 12 + len(more)))

                        if tab["floating"]:
                            n = len(tab["floating"])
                            fl_line = f"        {O}~{R} {n} floating"
                            lines.append(row(fl_line, 12 + len(str(n)) + 9))

                if i < len(session_maps) - 1:
                    lines.append(sep())

            lines.append(f"{D}└{'─' * W}┘{R}")
            lines.append("")
            lines.append(f"{O}►{R} focused  {G}●{R} current  {O}~{R} floating")

            result = {
                "success": True,
                "map": "\n".join(lines),
                "sessions": session_maps,
            }

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

    # === AGENT SESSION ===
    elif name == "agent_session":
        action = arguments.get("action", "status")

        if action == "create":
            success = ensure_agent_session()
            if success:
                result = {
                    "success": True,
                    "session": AGENT_SESSION,
                    "message": f"Agent session '{AGENT_SESSION}' is ready"
                }
            else:
                result = {
                    "success": False,
                    "error": f"Failed to create agent session '{AGENT_SESSION}'"
                }
        elif action == "status":
            sessions = get_active_sessions()
            exists = AGENT_SESSION in sessions
            result = {
                "success": True,
                "session": AGENT_SESSION,
                "exists": exists,
                "all_sessions": sessions
            }
        elif action == "destroy":
            # Kill the agent session
            try:
                subprocess.run(
                    ["zellij", "kill-session", AGENT_SESSION],
                    capture_output=True, timeout=10
                )
                result = {"success": True, "destroyed": AGENT_SESSION}
            except Exception as e:
                result = {"success": False, "error": str(e)}
        else:
            result = {"success": False, "error": f"Unknown action: {action}"}

    elif name == "spawn_agents":
        # Spawn multiple Claude agents in parallel
        session = get_agent_session()
        tasks = arguments.get("tasks", [])
        tab = arguments.get("tab", "agents")
        skip_permissions = arguments.get("dangerously_skip_permissions", False)

        if not tasks:
            result = {"success": False, "error": "No tasks provided"}
        else:
            # Ensure agent session exists
            ensure_agent_session()

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
            session = get_agent_session()

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
            session = get_agent_session()

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
