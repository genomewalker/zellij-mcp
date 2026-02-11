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

# Check for mcp package
if ! python3 -c "import mcp" &>/dev/null; then
    echo "Installing mcp package..."
    pip3 install --user mcp
fi

# Make server executable
chmod +x "$PLUGIN_DIR/server.py"

# Add to Claude Code MCP config
MCP_CONFIG="$HOME/.claude/mcp.json"
if [[ -f "$MCP_CONFIG" ]]; then
    # Check if already configured
    if grep -q "zellij-mcp" "$MCP_CONFIG"; then
        echo "zellij-mcp already in MCP config"
    else
        echo "Adding zellij-mcp to MCP config..."
        # Use jq to add the entry
        if command -v jq &>/dev/null; then
            tmp=$(mktemp)
            jq --arg path "$PLUGIN_DIR/server.py" \
               '.mcpServers["zellij-mcp"] = {"command": "python3", "args": [$path]}' \
               "$MCP_CONFIG" > "$tmp" && mv "$tmp" "$MCP_CONFIG"
        else
            echo "Warning: jq not found. Please add manually to $MCP_CONFIG:"
            echo '  "zellij-mcp": {"command": "python3", "args": ["'"$PLUGIN_DIR/server.py"'"]}'
        fi
    fi
else
    echo "Creating MCP config..."
    mkdir -p "$(dirname "$MCP_CONFIG")"
    cat > "$MCP_CONFIG" << EOF
{
  "mcpServers": {
    "zellij-mcp": {
      "command": "python3",
      "args": ["$PLUGIN_DIR/server.py"]
    }
  }
}
EOF
fi

echo "Done! Restart Claude Code to use zellij-mcp tools."
echo ""
echo "Tools available via ToolSearch: new_pane, write_command, focus_tab, etc."
