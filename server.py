#!/usr/bin/env python3
"""Zellij MCP Server - Control Zellij from Claude Code."""

import json
import subprocess
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("zellij-mcp")


def run_zellij(*args: str, capture: bool = False) -> dict[str, Any]:
    """Run a zellij command."""
    cmd = ["zellij"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
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


def zellij_action(*args: str) -> dict[str, Any]:
    """Run a zellij action command."""
    return run_zellij("action", *args)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Zellij control tools."""
    return [
        # Pane management
        Tool(
            name="new_pane",
            description="Open a new pane in Zellij",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["down", "right", "up", "left"],
                        "description": "Direction for the new pane (default: down)",
                    },
                    "floating": {
                        "type": "boolean",
                        "description": "Create a floating pane",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command to run in the new pane",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name for the new pane",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the new pane",
                    },
                },
            },
        ),
        Tool(
            name="close_pane",
            description="Close the currently focused pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="focus_pane",
            description="Move focus to another pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["down", "right", "up", "left"],
                        "description": "Direction to move focus",
                    },
                },
                "required": ["direction"],
            },
        ),
        Tool(
            name="toggle_floating",
            description="Toggle floating pane mode",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="toggle_fullscreen",
            description="Toggle fullscreen for current pane",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="rename_pane",
            description="Rename the current pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "New name for the pane"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="resize_pane",
            description="Resize the current pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["down", "right", "up", "left"],
                        "description": "Direction to resize",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Amount to resize (default: 1)",
                    },
                },
                "required": ["direction"],
            },
        ),
        # Tab management
        Tool(
            name="new_tab",
            description="Create a new tab",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new tab"},
                    "layout": {"type": "string", "description": "Layout file to use"},
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
            description="Switch to another tab",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "Tab index (0-based)",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["next", "previous"],
                        "description": "Relative tab navigation",
                    },
                },
            },
        ),
        Tool(
            name="rename_tab",
            description="Rename the current tab",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "New name for the tab"},
                },
                "required": ["name"],
            },
        ),
        # Text/command interaction
        Tool(
            name="write_chars",
            description="Send text/characters to the focused pane",
            inputSchema={
                "type": "object",
                "properties": {
                    "chars": {
                        "type": "string",
                        "description": "Characters to send to the pane",
                    },
                },
                "required": ["chars"],
            },
        ),
        Tool(
            name="write_command",
            description="Send a command to the focused pane (adds Enter)",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to execute in the pane",
                    },
                },
                "required": ["command"],
            },
        ),
        # Session management
        Tool(
            name="list_sessions",
            description="List all Zellij sessions",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="session_info",
            description="Get info about current Zellij session",
            inputSchema={"type": "object", "properties": {}},
        ),
        # Layout
        Tool(
            name="dump_layout",
            description="Dump current layout to stdout",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a Zellij control tool."""
    result: dict[str, Any]

    if name == "new_pane":
        args = ["new-pane"]
        if arguments.get("floating"):
            args.append("--floating")
        if arguments.get("direction"):
            args.extend(["--direction", arguments["direction"]])
        if arguments.get("cwd"):
            args.extend(["--cwd", arguments["cwd"]])
        if arguments.get("name"):
            args.extend(["--name", arguments["name"]])
        if arguments.get("command"):
            args.append("--")
            args.append(arguments["command"])
        result = zellij_action(*args)

    elif name == "close_pane":
        result = zellij_action("close-pane")

    elif name == "focus_pane":
        result = zellij_action("move-focus", arguments["direction"])

    elif name == "toggle_floating":
        result = zellij_action("toggle-floating-panes")

    elif name == "toggle_fullscreen":
        result = zellij_action("toggle-fullscreen")

    elif name == "rename_pane":
        result = zellij_action("rename-pane", arguments["name"])

    elif name == "resize_pane":
        direction = arguments["direction"]
        amount = arguments.get("amount", 1)
        # Zellij uses resize with direction
        result = zellij_action("resize", "increase", direction)
        # Multiple resizes if amount > 1
        for _ in range(amount - 1):
            zellij_action("resize", "increase", direction)

    elif name == "new_tab":
        args = ["new-tab"]
        if arguments.get("name"):
            args.extend(["--name", arguments["name"]])
        if arguments.get("layout"):
            args.extend(["--layout", arguments["layout"]])
        if arguments.get("cwd"):
            args.extend(["--cwd", arguments["cwd"]])
        result = zellij_action(*args)

    elif name == "close_tab":
        result = zellij_action("close-tab")

    elif name == "focus_tab":
        if "index" in arguments:
            result = zellij_action("go-to-tab", str(arguments["index"] + 1))
        elif arguments.get("direction") == "next":
            result = zellij_action("go-to-next-tab")
        elif arguments.get("direction") == "previous":
            result = zellij_action("go-to-previous-tab")
        else:
            result = {"success": False, "error": "Specify index or direction"}

    elif name == "rename_tab":
        result = zellij_action("rename-tab", arguments["name"])

    elif name == "write_chars":
        result = zellij_action("write-chars", arguments["chars"])

    elif name == "write_command":
        # Send command + Enter
        cmd = arguments["command"] + "\n"
        result = zellij_action("write-chars", cmd)

    elif name == "list_sessions":
        result = run_zellij("list-sessions", capture=True)

    elif name == "session_info":
        # Get ZELLIJ_SESSION_NAME env var
        import os
        session = os.environ.get("ZELLIJ_SESSION_NAME", "unknown")
        result = {"success": True, "session": session}

    elif name == "dump_layout":
        result = zellij_action("dump-layout", capture=True)

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
