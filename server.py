#!/usr/bin/env python3
"""Zellij MCP Server - Full control of Zellij from Claude Code."""

import json
import subprocess
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("zellij-mcp")


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


def zellij_action(*args: str, capture: bool = False, session: str = None) -> dict[str, Any]:
    """Run a zellij action command, optionally targeting a specific session."""
    return run_zellij("action", *args, capture=capture, session=session)


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
            name="write_bytes",
            description="Send raw bytes to the focused pane",
            inputSchema={
                "type": "object",
                "properties": {"bytes": {"type": "array", "items": {"type": "integer"}, "description": "Bytes to send"}},
                "required": ["bytes"],
            },
        ),
        Tool(
            name="run_command",
            description="Run a command in the focused pane (sends command + Enter)",
            inputSchema={
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Command to execute"}},
                "required": ["command"],
            },
        ),
        Tool(
            name="clear_pane",
            description="Clear all buffers for the focused pane",
            inputSchema={"type": "object", "properties": {}},
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
        # === LAYOUT ===
        Tool(
            name="dump_layout",
            description="Dump current layout to stdout",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="dump_screen",
            description="Dump focused pane content to a file",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output file path"},
                    "full": {"type": "boolean", "description": "Include scrollback"},
                },
                "required": ["path"],
            },
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
        if arguments.get("start_suspended"):
            args.append("--start-suspended")
        if arguments.get("command"):
            args.append("--")
            args.append(arguments["command"])
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

    elif name == "write_bytes":
        byte_args = [str(b) for b in arguments["bytes"]]
        result = zellij_action("write", *byte_args, session=session)

    elif name == "run_command":
        result = zellij_action("write-chars", arguments["command"] + "\n", session=session)

    elif name == "clear_pane":
        result = zellij_action("clear", session=session)

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

    # === LAYOUT ===
    elif name == "dump_layout":
        result = zellij_action("dump-layout", capture=True, session=session)

    elif name == "dump_screen":
        args = ["dump-screen", arguments["path"]]
        if arguments.get("full"):
            args.append("--full")
        result = zellij_action(*args, session=session)

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

    else:
        result = {"success": False, "error": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
