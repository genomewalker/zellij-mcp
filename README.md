# zellij-mcp

Control [Zellij](https://zellij.dev) terminal multiplexer from [Claude Code](https://claude.ai/claude-code).

## Installation

### Claude Code Marketplace (recommended)

```
/install zellij-mcp
```

### Manual

```bash
git clone https://github.com/genomewalker/zellij-mcp
cd zellij-mcp
./scripts/install.sh
```

## Requirements

- [Zellij](https://zellij.dev/documentation/installation) installed
- Python 3.8+ with `mcp` package
- Claude Code running inside a Zellij session

## Usage

After installation, Claude Code can control your Zellij session:

```
"Open nvim in a floating pane"
"Create a new tab called 'tests'"
"Run npm test in a split pane on the right"
"Send 'git status' to the current pane"
```

## Tools

### Pane Management
- `new_pane` - Create pane (floating, direction, command)
- `close_pane` - Close focused pane
- `focus_pane` - Move focus between panes
- `toggle_floating` / `toggle_fullscreen`
- `rename_pane` / `resize_pane`

### Tab Management
- `new_tab` / `close_tab` / `focus_tab` / `rename_tab`

### Commands
- `write_chars` - Send text to pane
- `write_command` - Execute command (text + Enter)

### Session
- `list_sessions` / `session_info` / `dump_layout`

## Combining with prism-nvim

For full development workflows:

1. **zellij-mcp**: Opens nvim in a pane
2. **prism-nvim**: Controls nvim (edit files, navigate, LSP)
3. **zellij-mcp**: Opens test runner in another pane

```
Claude: "Open nvim with main.ts, add error handling, then run the tests"
→ new_pane(command="nvim src/main.ts")
→ prism: edit_buffer(...)
→ new_pane(direction="down", command="npm test")
```

## License

MIT
