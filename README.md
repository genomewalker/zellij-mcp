# zellij-mcp

**Terminal orchestration for Claude Code. 57 tools. Tab-based workspaces.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zellij](https://img.shields.io/badge/Zellij-0.40+-orange?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyTDIgN2wxMCA1IDEwLTV6Ii8+PC9zdmc+)](https://zellij.dev)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-MCP-blue)](https://docs.anthropic.com/en/docs/claude-code)
[![Docs](https://img.shields.io/badge/Docs-Website-blue)](https://genomewalker.github.io/zellij-mcp/)
[![Version](https://img.shields.io/github/v/release/genomewalker/zellij-mcp?label=version)](https://github.com/genomewalker/zellij-mcp/releases)

---

## Why zellij-mcp?

**Focus-free pane control.** Native Zellij plugin writes to panes without stealing focus. Your cursor stays where it is while Claude works in the background.

**Claude pane protection.** Auto-detects the agent's pane and prevents accidental closure. No more killing yourself mid-task.

**Tab-based workspaces.** Agent work is organized into dedicated tabs (e.g., "agents", "analysis", "hpc"). Switch tabs to observe agent progress, or stay in your own tab.

**Named pane targeting.** Address any pane by name, not just the focused one. `create_named_pane("worker")`, `read_pane("worker")`, `run_in_pane("worker", "make test")`.

**Complete terminal control.** 57 MCP tools for panes, tabs, output monitoring, REPL interaction, SSH connections, and HPC job management.

---

## Install

### Via Claude Code Plugin System (Recommended)

```
/plugin genomewalker/zellij-mcp
```

Then **restart Claude Code**.

### Manual Install

```bash
curl -fsSL https://raw.githubusercontent.com/genomewalker/zellij-mcp/main/scripts/install.sh | bash
```

To update: `cd ~/.local/share/zellij-mcp && git pull && ./scripts/install.sh`

---

## Quick Start

```
"create a pane called worker"
"run npm test in worker"
"wait for the tests to finish"
"show me the output"
```

---

## Key Features

### Tab-Based Workspaces

Agent operations are organized into dedicated tabs within your session:

```python
create_named_pane(name="analysis", command="ipython3", tab="work")
# → Created in "work" tab, switch tabs to observe progress
```

### Named Pane Operations

Target panes by name across tabs and sessions:

```python
create_named_pane(name="tests", command="bash", tab="dev")
run_in_pane(pane_name="tests", command="pytest -v")
wait_for_output(pane_name="tests", pattern="passed|failed")
read_pane(pane_name="tests")
```

### Output Monitoring

```python
wait_for_output(pane_name="worker", pattern=r"Done|Error", timeout=300)
wait_for_idle(pane_name="worker", stable_seconds=3)
tail_pane(pane_name="worker")  # Get new output since last read
```

### REPL Interaction

```python
create_named_pane(name="ipython", command="ipython3")
repl_execute(pane_name="ipython", code="import pandas as pd\ndf.describe()")
repl_interrupt(pane_name="ipython")  # Ctrl+C
```

### SSH & HPC Workflows

```python
ssh_connect(name="hpc", host="user@cluster.edu")
ssh_run(name="hpc", command="squeue -u $USER")
job_submit(ssh_name="hpc", script="analysis.sh", scheduler="slurm")
job_status(job_id="12345")
```

### Agent Spawning

```python
spawn_agents(agents=[
    {"name": "researcher", "task": "Find API documentation"},
    {"name": "implementer", "task": "Write the integration"}
])
list_spawned_agents()
agent_output(name="researcher")
```

---

## Tools Reference

### Pane Management (12 tools)
`new_pane` `close_pane` `focus_pane` `focus_next_pane` `focus_previous_pane` `move_pane` `move_pane_backwards` `resize_pane` `rename_pane` `toggle_floating` `toggle_fullscreen` `toggle_embed_or_floating`

### Named Panes (7 tools)
`create_named_pane` `destroy_named_pane` `list_named_panes` `focus_pane_by_name` `read_pane` `write_to_pane` `run_in_pane`

### Monitoring (5 tools)
`wait_for_output` `wait_for_idle` `tail_pane` `search_pane` `send_keys`

### REPL (2 tools)
`repl_execute` `repl_interrupt`

### SSH & HPC (4 tools)
`ssh_connect` `ssh_run` `job_submit` `job_status`

### Tab Management (8 tools)
`new_tab` `close_tab` `focus_tab` `move_tab` `rename_tab` `query_tab_names` `go_to_next_tab` `go_to_previous_tab`

### Session & Layout (6 tools)
`list_sessions` `list_panes` `session_info` `dump_layout` `swap_layout` `session_map`

### Workspace & Agent Management (5 tools)
`agent_session` `spawn_agents` `list_spawned_agents` `agent_output` `stop_agent`

### Other (8 tools)
`write_chars` `clear_pane` `scroll` `edit_scrollback` `switch_mode` `stack_panes` `launch_plugin` `pipe`

---

## Cross-Session Control

All tools support `session` parameter to target other Zellij sessions:

```python
list_sessions()
read_pane(session="other-session", pane_name="worker")
run_in_pane(session="dev", pane_name="tests", command="make check")
```

---

## Combining with prism.nvim

The complete development workflow:

```
1. zellij-mcp: "open nvim in a floating pane"
2. prism.nvim: "go to line 42, add error handling"
3. zellij-mcp: "run tests in a pane below"
4. zellij-mcp: "wait for tests and show results"
```

---

## Pane Bridge Plugin

The installer includes a native Zellij plugin (`zellij-pane-bridge.wasm`) that enables:

- **Focus-free writes** - Send text to any pane without changing focus
- **Claude pane protection** - Auto-detects and protects the agent's pane from closure
- **Direct pane control** - Close, hide, show panes by ID

The MCP server automatically uses the plugin when available, falling back to focus-based operations if not installed.

---

## Requirements

- [Zellij](https://zellij.dev/documentation/installation) 0.40+
- Python 3.8+ with `mcp` package
- Claude Code

---

MIT · [Website](https://genomewalker.github.io/zellij-mcp/) · [GitHub](https://github.com/genomewalker/zellij-mcp) · [Issues](https://github.com/genomewalker/zellij-mcp/issues)
