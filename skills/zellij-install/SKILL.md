---
name: zellij-install
description: Install or update zellij-mcp for Claude Code with focus-free pane control
execution: direct
---

# Zellij MCP Installation

Install or update zellij-mcp for Claude Code. Includes the pane-bridge plugin for focus-free operations.

## Instructions

Guide the user through installing or updating zellij-mcp.

### Step 1: Check Prerequisites

Run these checks:

```bash
# Check zellij
zellij --version

# Check python3
python3 --version

# Check claude CLI
claude --version
```

Report any missing dependencies:
- **zellij**: Install from https://zellij.dev/documentation/installation
- **python3**: Install from system package manager
- **claude**: Install Claude Code CLI

### Step 2: Determine Installation Type

Use AskUserQuestion:

```
Questions:
  - question: "How would you like to install zellij-mcp?"
    header: "Install"
    options:
      - label: "Fresh Install (Recommended)"
        description: "Clone repo and run install script"
      - label: "Update Existing"
        description: "Pull latest and reinstall"
      - label: "Development Mode"
        description: "Link to local repo for development"
```

### Step 3: Execute Installation

**Fresh Install:**
```bash
# Clone to user's preferred location (suggest ~/.local/share)
cd ~/.local/share
git clone https://github.com/genomewalker/zellij-mcp
cd zellij-mcp
./scripts/install.sh
```

**Update Existing:**
```bash
cd ~/.local/share/zellij-mcp  # or wherever installed
git pull
./scripts/install.sh
```

**Development Mode:**
Ask where the repo is located, then:
```bash
cd /path/to/zellij-mcp
./scripts/install.sh
```

### Step 4: Verify Installation

Check that everything is installed:

```bash
# Check MCP registration
claude mcp list | grep zellij

# Check plugin installed
ls -la ~/.local/share/zellij-mcp/plugins/zellij-pane-bridge.wasm
```

### Step 5: Report Results

Tell the user:
1. Installation location: `~/.local/share/zellij-mcp/`
2. Plugin location: `~/.local/share/zellij-mcp/plugins/zellij-pane-bridge.wasm`
3. **Important**: Restart Claude Code to use the new tools
4. Test with: `ToolSearch query="+zellij"` then `list_panes`

### Features Installed

- **57 MCP tools** for pane management, monitoring, REPL, SSH/HPC
- **Pane-bridge plugin** for focus-free write operations
- **Claude pane protection** prevents accidental self-termination

### Troubleshooting

If installation fails:

1. **MCP package missing**: `pip3 install --user mcp`
2. **Permission denied**: Check write access to `~/.local/share/`
3. **Plugin not working**: Ensure Zellij version >= 0.40.0

### Building Plugin from Source (optional)

If user needs to rebuild the WASM plugin:

```bash
# Requires Rust toolchain
cd zellij-pane-bridge
rustup target add wasm32-wasip1
cargo build --release --target wasm32-wasip1
cp target/wasm32-wasip1/release/zellij-pane-bridge.wasm ..
./scripts/install.sh
```
