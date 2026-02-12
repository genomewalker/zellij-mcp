#!/usr/bin/env bash
# Install zellij-mcp for Claude Code
#
# Usage:
#   Fresh install:  curl -fsSL https://raw.githubusercontent.com/genomewalker/zellij-mcp/main/scripts/install.sh | bash
#   From repo:      ./scripts/install.sh
#   Update:         cd ~/.local/share/zellij-mcp && git pull && ./scripts/install.sh

set -euo pipefail

REPO_URL="https://github.com/genomewalker/zellij-mcp.git"
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

# Determine if we're running from within a repo or standalone
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || SCRIPT_DIR=""
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/../server.py" ]]; then
    # Running from within the repo
    SOURCE_DIR="$(dirname "$SCRIPT_DIR")"
    echo "Installing from local repo: $SOURCE_DIR"

    # If source is not the install dir, copy/sync the repo
    if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
        mkdir -p "$INSTALL_DIR"

        # Copy all repo files (excluding .git for dev installs, build artifacts)
        rsync -a --delete \
            --exclude '.git' \
            --exclude 'zellij-pane-bridge/target' \
            --exclude '__pycache__' \
            --exclude '*.pyc' \
            "$SOURCE_DIR/" "$INSTALL_DIR/"

        # Initialize as git repo for updates if not already
        if [[ ! -d "$INSTALL_DIR/.git" ]]; then
            cd "$INSTALL_DIR"
            git init -q
            git remote add origin "$REPO_URL" 2>/dev/null || git remote set-url origin "$REPO_URL"
            git fetch -q origin main 2>/dev/null || true
            git branch -M main 2>/dev/null || true
        fi
    fi
else
    # Fresh install - clone from GitHub
    echo "Cloning from GitHub..."

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        echo "Existing installation found, updating..."
        cd "$INSTALL_DIR"
        git fetch origin main
        git reset --hard origin/main
    else
        rm -rf "$INSTALL_DIR"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    fi
fi

# Create plugins directory
mkdir -p "$WASM_DIR"

# Install the pane-bridge plugin
WASM_SOURCE="${INSTALL_DIR}/zellij-pane-bridge.wasm"
WASM_DEST="${WASM_DIR}/zellij-pane-bridge.wasm"

if [[ -f "$WASM_SOURCE" ]]; then
    echo "Installing pane-bridge plugin..."
    cp "$WASM_SOURCE" "$WASM_DEST"
    chmod +x "$WASM_DEST"
    echo "  -> $WASM_DEST"
else
    echo "Warning: zellij-pane-bridge.wasm not found."
    echo "  Some features (focus-free pane writing) will be unavailable."
    echo "  To build: cd $INSTALL_DIR/zellij-pane-bridge && cargo build --release --target wasm32-wasip1"
fi

# Ensure server.py is executable
chmod +x "$INSTALL_DIR/server.py"

# Register MCP server with Claude Code
echo "Registering MCP server..."
claude mcp remove zellij-mcp 2>/dev/null || true
claude mcp add --transport stdio --scope user zellij-mcp -- python3 "$INSTALL_DIR/server.py"

echo ""
echo "Done! Restart Claude Code to use zellij-mcp tools."
echo ""
echo "Installed to: $INSTALL_DIR"
echo "  - server.py (MCP server)"
if [[ -f "$WASM_DEST" ]]; then
    echo "  - plugins/zellij-pane-bridge.wasm (focus-free pane control)"
fi
echo ""
echo "To update later: cd $INSTALL_DIR && git pull && ./scripts/install.sh"
echo ""
echo "Tools available via ToolSearch: new_pane, write_to_pane, read_pane, etc."
