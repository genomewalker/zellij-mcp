# zellij-mcp

Control Zellij terminal multiplexer from Claude Code. All tools support cross-session targeting via the `session` parameter.

## Cross-Session Support

All tools accept an optional `session` parameter to target a different Zellij session:

```
new_pane --session other-session --command "htop"
run_command --session other-session --command "git status"
```

Use `list_sessions` to discover available sessions.

## Tools Reference

### Pane Management

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `new_pane` | Create pane | `direction`, `floating`, `command`, `name`, `cwd`, `in_place`, `close_on_exit` |
| `close_pane` | Close focused pane | |
| `focus_pane` | Move focus | `direction` (down/right/up/left) |
| `focus_next_pane` | Focus next pane | |
| `focus_previous_pane` | Focus previous pane | |
| `move_pane` | Move pane location | `direction` |
| `move_pane_backwards` | Rotate pane backwards | |
| `resize_pane` | Resize pane border | `direction`, `increase` (bool) |
| `rename_pane` | Set pane name | `name` |
| `undo_rename_pane` | Remove custom name | |
| `toggle_floating` | Toggle floating panes | |
| `toggle_fullscreen` | Toggle fullscreen | |
| `toggle_embed_or_floating` | Toggle pane embed/float | |
| `toggle_pane_frames` | Toggle UI frames | |
| `toggle_sync_tab` | Send to all panes in tab | |
| `stack_panes` | Stack panes together | `pane_ids` (array) |

### Tab Management

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `new_tab` | Create tab | `name`, `layout`, `cwd` |
| `close_tab` | Close current tab | |
| `focus_tab` | Switch tab | `index` (1-based), `name`, or `direction` (next/previous) |
| `move_tab` | Move tab | `direction` (left/right) |
| `rename_tab` | Set tab name | `name` |
| `undo_rename_tab` | Remove custom name | |
| `query_tab_names` | List all tab names | |

### Scrolling

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `scroll` | Scroll in pane | `direction` (up/down), `amount` (line/half_page/page/top/bottom) |

### Text/Commands

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `write_chars` | Send characters | `chars` |
| `write_bytes` | Send raw bytes | `bytes` (array) |
| `run_command` | Execute command | `command` (adds Enter) |
| `clear_pane` | Clear pane buffers | |

### Edit

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `edit_file` | Open file in editor pane | `path`, `line`, `floating`, `in_place`, `direction` |
| `edit_scrollback` | Open scrollback in editor | |

### Session

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `list_sessions` | List all sessions | |
| `list_clients` | List connected clients | |
| `session_info` | Current session info | |
| `rename_session` | Rename session | `name` |

### Layout

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `dump_layout` | Export current layout | |
| `dump_screen` | Dump pane to file | `path`, `full` (include scrollback) |
| `swap_layout` | Cycle layouts | `direction` (next/previous) |

### Mode

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `switch_mode` | Change input mode | `mode` (locked/pane/tab/resize/move/search/session/normal) |

### Plugins

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `launch_plugin` | Launch Zellij plugin | `url`, `floating`, `in_place`, `skip_cache`, `configuration` |
| `pipe` | Send data to plugins | `name`, `payload`, `plugin`, `args` |

## Common Workflows

### Open nvim in floating pane
```
new_pane --floating --command "nvim src/main.ts"
```

### Multi-pane development
```
new_pane --direction right --name "server" --command "npm run dev"
new_pane --direction down --name "tests"
run_command --command "npm test --watch"
```

### Cross-session command
```
list_sessions
run_command --session other-session --command "git pull"
```

### With prism-nvim
1. `new_pane --floating --command "nvim"` - Open nvim via zellij-mcp
2. Use prism-nvim tools to edit files in that nvim instance
3. `new_pane --direction down --command "make test"` - Run tests

## Running Inside Zellij (Claude Code in a pane)

When Claude Code runs inside a Zellij pane, follow these rules:

### Always specify your session
```
# Find your session name first
session_info  # Returns {"session": "cc-soul", "pane_id": "0"}

# Then use session parameter on all commands
new_tab --session cc-soul --name "build"
write_chars --session cc-soul --chars "make build"
dump_screen --session cc-soul --path /tmp/output.txt
```

### Never close your own pane
`close_pane` on the focused pane will detach/kill the session. Safe pattern:
```
# Create work in another tab
new_tab --session cc-soul --name "work"

# When done, go back to your tab first
focus_tab --session cc-soul --name "Tab #1"  # or your tab name

# Then close the work tab (NOT your own)
close_tab --session cc-soul  # Only if you're NOT on that tab
```

### dump_screen captures the focused pane
To capture output from another tab:
```bash
# Switch to the target tab, dump, then return
zellij -s cc-soul action go-to-tab-name work && \
  zellij -s cc-soul action dump-screen /tmp/output.txt && \
  zellij -s cc-soul action go-to-tab-name "Tab #1"
```

### Recommended workflow for running commands
```
# 1. Create a dedicated tab
new_tab --session cc-soul --name "task"

# 2. Type and execute command
write_chars --session cc-soul --chars "pdu ~"
write_chars --session cc-soul --chars "\n"  # Press Enter

# 3. Wait, then capture output
# (use Bash: sleep 10)
dump_screen --session cc-soul --path /tmp/result.txt --full true

# 4. Read the output file
# (use Read tool)
```

## Natural Language Mapping

| User says | Tool |
|-----------|------|
| "open a new pane" | `new_pane` |
| "split right with htop" | `new_pane --direction right --command "htop"` |
| "floating pane" | `new_pane --floating` |
| "close this pane" | `close_pane` |
| "focus left/right/up/down" | `focus_pane --direction <dir>` |
| "fullscreen" | `toggle_fullscreen` |
| "new tab called X" | `new_tab --name "X"` |
| "go to tab 2" | `focus_tab --index 2` |
| "run X" / "execute X" | `run_command --command "X"` |
| "send X to other session" | `run_command --session other --command "X"` |
| "list sessions" | `list_sessions` |
| "scroll up/down" | `scroll --direction up/down` |
| "scroll to top" | `scroll --direction up --amount top` |
| "clear the screen" | `clear_pane` |
