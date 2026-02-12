#!/usr/bin/env bash
# Install zellij-mcp for Claude Code

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="${HOME}/.local/share/zellij-mcp"
WASM_DIR="${INSTALL_DIR}/plugins"

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

# Create install directories
mkdir -p "$INSTALL_DIR"
mkdir -p "$WASM_DIR"

# Install the pane-bridge plugin
WASM_SOURCE="${PLUGIN_DIR}/zellij-pane-bridge.wasm"
WASM_DEST="${WASM_DIR}/zellij-pane-bridge.wasm"

if [[ -f "$WASM_SOURCE" ]]; then
    echo "Installing pane-bridge plugin..."
    cp "$WASM_SOURCE" "$WASM_DEST"
    echo "  -> $WASM_DEST"
else
    echo "Warning: zellij-pane-bridge.wasm not found in repo."
    echo "  Some features (focus-free pane writing) will be unavailable."
    echo "  To build the plugin: cd zellij-pane-bridge && cargo build --release"
fi

# Copy server.py to install location for stability
cp "$PLUGIN_DIR/server.py" "$INSTALL_DIR/server.py"
chmod +x "$INSTALL_DIR/server.py"

# Register MCP server with Claude Code
echo "Registering MCP server..."
claude mcp remove zellij-mcp 2>/dev/null || true
claude mcp add --transport stdio --scope user zellij-mcp -- python3 "$INSTALL_DIR/server.py"

echo ""
echo "Done! Restart Claude Code to use zellij-mcp tools."
echo ""
echo "Installed to: $INSTALL_DIR"
echo "  - server.py"
if [[ -f "$WASM_DEST" ]]; then
    echo "  - plugins/zellij-pane-bridge.wasm (focus-free pane control)"
fi
echo ""
echo "Tools available via ToolSearch: new_pane, write_to_pane, read_pane, etc."
