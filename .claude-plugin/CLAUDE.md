# zellij-mcp

Autonomous Zellij control for Claude Code. 56 tools for pane management, output monitoring, REPL interaction, and SSH/HPC workflows.

## Key Features

- **Pane targeting** - Work with any pane by name, not just focused
- **Output monitoring** - Wait for patterns, tail new output, search history
- **REPL interaction** - Execute code in IPython/R/Julia, capture output
- **SSH/HPC** - Manage SSH sessions, submit SLURM/PBS jobs
- **Autonomous workflows** - Run commands and wait for completion

## Quick Start

```python
# Create a named pane
create_named_pane(name="analysis", command="ipython3", tab="work")

# Run code and capture output
repl_execute(pane_name="analysis", code="import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df.shape)")

# Wait for completion
wait_for_output(pane_name="analysis", pattern=r"In \[\d+\]:")
```

## Tools by Category

### Core I/O (Pane Targeting)

| Tool | Description |
|------|-------------|
| `read_pane` | Read content from any pane by name. Params: `pane_name`, `full`, `tail`, `strip_ansi` |
| `write_to_pane` | Send text to a named pane. Params: `pane_name`, `chars`, `press_enter` |
| `send_keys` | Send special keys (ctrl+c, arrows, etc). Params: `pane_name`, `keys`, `repeat` |
| `search_pane` | Search pane content with regex. Params: `pane_name`, `pattern`, `context` |
| `write_chars` | Send characters to focused pane. Params: `chars` |

### Monitoring

| Tool | Description |
|------|-------------|
| `wait_for_output` | Wait for regex pattern to appear. Params: `pane_name`, `pattern`, `timeout` |
| `wait_for_idle` | Wait until output stops changing. Params: `pane_name`, `stable_seconds`, `timeout` |
| `tail_pane` | Get only new output since last read. Params: `pane_name`, `reset` |

### Compound Operations

| Tool | Description |
|------|-------------|
| `run_in_pane` | Run command and wait for completion. Params: `pane_name`, `command`, `wait`, `timeout`, `capture` |
| `create_named_pane` | Create pane with name (idempotent). Params: `name`, `command`, `tab`, `direction`, `floating`, `cwd` |
| `destroy_named_pane` | Close named pane. Params: `name` |
| `list_named_panes` | List all registered panes with status |

### REPL Interaction

| Tool | Description |
|------|-------------|
| `repl_execute` | Execute code in REPL and capture output. Params: `pane_name`, `code`, `repl_type`, `timeout` |
| `repl_interrupt` | Send Ctrl+C to interrupt. Params: `pane_name`, `wait_for_prompt` |

### SSH/HPC

| Tool | Description |
|------|-------------|
| `ssh_connect` | Open SSH session in named pane. Params: `name`, `host`, `tab`, `port`, `identity_file` |
| `ssh_run` | Execute command on remote host. Params: `name`, `command`, `wait`, `timeout` |
| `job_submit` | Submit SLURM/PBS job and track. Params: `ssh_name`, `script`, `scheduler`, `extra_args` |
| `job_status` | Check tracked job status. Params: `job_id`, `ssh_name` |

### Pane Management

| Tool | Description |
|------|-------------|
| `new_pane` | Create pane. Params: `direction`, `floating`, `command`, `name`, `cwd` |
| `close_pane` | Close focused pane |
| `focus_pane` | Move focus by direction. Params: `direction` |
| `focus_pane_by_name` | Focus pane by name. Params: `name` |
| `focus_next_pane` / `focus_previous_pane` | Cycle focus |
| `move_pane` | Move pane location. Params: `direction` |
| `resize_pane` | Resize pane. Params: `direction`, `increase` |
| `rename_pane` | Set pane name. Params: `name` |
| `toggle_floating` | Toggle floating panes visibility |
| `toggle_fullscreen` | Toggle fullscreen |
| `clear_pane` | Clear pane buffers |

### Tab Management

| Tool | Description |
|------|-------------|
| `new_tab` | Create tab. Params: `name`, `layout`, `cwd` |
| `close_tab` | Close current tab |
| `focus_tab` | Switch tab. Params: `index`, `name`, `direction` |
| `move_tab` | Move tab. Params: `direction` |
| `rename_tab` | Set tab name. Params: `name` |
| `query_tab_names` | List all tab names |

### Session & Layout

| Tool | Description |
|------|-------------|
| `list_sessions` | List all Zellij sessions |
| `list_panes` | List all panes with metadata |
| `session_info` | Current session info |
| `dump_layout` | Export current layout |
| `swap_layout` | Cycle layouts |

## Workflow Examples

### Bioinformatics Pipeline
```python
# Connect to HPC
ssh_connect(name="hpc", host="user@cluster.edu", tab="remote")

# Submit job
job_submit(ssh_name="hpc", script="pipeline.sh", scheduler="slurm")

# Check status
job_status()
```

### Interactive Analysis
```python
# Create IPython pane
create_named_pane(name="ipython", command="ipython3")

# Run analysis
repl_execute(pane_name="ipython", code="""
import pandas as pd
df = pd.read_csv('data.csv')
print(df.describe())
""")

# Interrupt if stuck
repl_interrupt(pane_name="ipython")
```

### Parallel Command Execution
```python
# Create panes for different tasks
create_named_pane(name="build", command="bash", tab="dev")
create_named_pane(name="test", command="bash", tab="dev")

# Run commands
run_in_pane(pane_name="build", command="make build", wait=False)
run_in_pane(pane_name="test", command="make test", wait=True, timeout=120)
```

### Monitor Long-Running Process
```python
# Start process
run_in_pane(pane_name="worker", command="./long_job.sh", wait=False)

# Monitor progress
tail_pane(pane_name="worker", reset=True)
# ... later ...
tail_pane(pane_name="worker")  # Get new output

# Wait for completion
wait_for_output(pane_name="worker", pattern="Job completed|Error", timeout=3600)
```

## Special Keys for send_keys

```
ctrl+a through ctrl+z
tab, enter, escape, backspace, delete, space
up, down, left, right
home, end, pageup, pagedown, insert
f1 through f12
```

Example: `send_keys(pane_name="editor", keys="ctrl+c")`

## Cross-Session Support

All tools accept `session` parameter to target other Zellij sessions:

```python
list_sessions()
read_pane(session="other-session", pane_name="worker")
```
