# zellij-mcp

**Claude controls your terminal. Create panes, run commands, orchestrate workflows.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zellij](https://img.shields.io/badge/Zellij-0.40+-orange?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyTDIgN2wxMCA1IDEwLTV6Ii8+PC9zdmc+)](https://zellij.dev)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-MCP-blue)](https://docs.anthropic.com/en/docs/claude-code)
[![Docs](https://img.shields.io/badge/Docs-Website-blue)](https://genomewalker.github.io/zellij-mcp/)
[![Version](https://img.shields.io/github/v/release/genomewalker/zellij-mcp?label=version)](https://github.com/genomewalker/zellij-mcp/releases)

---

## Why zellij-mcp?

**Orchestrate your terminal.** Claude creates panes, runs commands, and captures output—all from natural language.

**35+ MCP tools.** Panes, tabs, floating windows, scrolling, screen capture, cross-session control.

**Pairs with prism-nvim.** Open nvim in a pane via zellij-mcp, edit with prism-nvim, run tests in another pane.

---

## Install

```bash
git clone https://github.com/genomewalker/zellij-mcp
cd zellij-mcp
./scripts/install.sh
```

Then **restart Claude Code**.

---

## Quick Start

```
"open a floating pane with htop"
"create a tab called tests"
"run npm test in a split on the right"
"send git status to the current pane"
```

---

## Natural Language

| You say | What happens |
|---------|--------------|
| "new pane" | Creates pane |
| "split right" | Pane to the right |
| "floating pane" | Floating window |
| "new tab called X" | Tab named X |
| "go to tab 2" | Switches tab |
| "run npm test" | Executes command |
| "scroll up" | Scrolls pane |
| "fullscreen" | Toggle fullscreen |

---

## Tools

### Pane Management
- `new_pane` - Create pane (floating, direction, command, cwd)
- `close_pane` / `focus_pane` / `resize_pane`
- `toggle_floating` / `toggle_fullscreen`

### Tab Management
- `new_tab` / `close_tab` / `focus_tab` / `rename_tab`

### Commands & Input
- `write_chars` - Send text to pane
- `run_command` - Execute command (text + Enter)
- `dump_screen` - Capture pane output to file

### Session
- `session_info` / `list_sessions` / `dump_layout`

---

## Cross-Session Control

All tools support `--session` to target other Zellij sessions:

```
list_sessions              # See available sessions
run_command --session dev --command "git pull"
dump_screen --session dev --path /tmp/output.txt
```

---

## Safety Guidelines

When Claude Code runs inside a Zellij pane:

1. **Always use `session_info` first** - Know your session name
2. **Pass `--session` on commands** - Target the right session
3. **Never close your own pane** - Only close panes you created
4. **Create work in separate tabs** - Easier to manage

See [CLAUDE.md](.claude-plugin/CLAUDE.md) for detailed guidelines.

---

## Combining with prism-nvim

The ultimate development workflow:

```
1. zellij-mcp: "open nvim in a floating pane"
2. prism-nvim: "go to line 42, add error handling"
3. zellij-mcp: "open a pane below and run the tests"
4. zellij-mcp: "capture the test output"
```

---

## Requirements

- [Zellij](https://zellij.dev/documentation/installation) 0.40+
- Python 3.8+ with `mcp` package
- Claude Code running inside a Zellij session

---

MIT · [Website](https://genomewalker.github.io/zellij-mcp/) · [GitHub](https://github.com/genomewalker/zellij-mcp) · [Issues](https://github.com/genomewalker/zellij-mcp/issues)
