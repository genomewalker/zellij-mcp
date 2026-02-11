#!/usr/bin/env bash
# Install zellij-mcp for Claude Code

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"

echo "Installing zellij-mcp..."

# Check dependencies
if ! command -v zellij &>/dev/null; then
    echo "Error: zellij not found. Install it first: https://zellij.dev/documentation/installation"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found"
    exit 1
fi

if ! command -v claude &>/dev/null; then
    echo "Error: claude CLI not found"
    exit 1
fi

# Check for mcp package
if ! python3 -c "import mcp" &>/dev/null; then
    echo "Installing mcp package..."
    pip3 install --user mcp
fi

# Make server executable
chmod +x "$PLUGIN_DIR/server.py"

# Register MCP server with Claude Code
echo "Registering MCP server..."
claude mcp add --transport stdio --scope user zellij-mcp -- python3 "$PLUGIN_DIR/server.py"

echo ""
echo "Done! Restart Claude Code to use zellij-mcp tools."
echo ""
echo "Tools available via ToolSearch: new_pane, write_chars, focus_tab, etc."
