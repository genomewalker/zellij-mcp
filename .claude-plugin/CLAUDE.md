# zellij-mcp

Control Zellij terminal multiplexer from Claude Code.

## Tools

### Pane Management
| Tool | Description |
|------|-------------|
| `new_pane` | Open pane (direction, floating, command, name, cwd) |
| `close_pane` | Close focused pane |
| `focus_pane` | Move focus (down/right/up/left) |
| `toggle_floating` | Toggle floating pane mode |
| `toggle_fullscreen` | Toggle fullscreen |
| `rename_pane` | Rename current pane |
| `resize_pane` | Resize pane (direction, amount) |

### Tab Management
| Tool | Description |
|------|-------------|
| `new_tab` | Create tab (name, layout, cwd) |
| `close_tab` | Close current tab |
| `focus_tab` | Switch tab (index or next/previous) |
| `rename_tab` | Rename current tab |

### Commands
| Tool | Description |
|------|-------------|
| `write_chars` | Send characters to focused pane |
| `write_command` | Send command + Enter to focused pane |

### Session
| Tool | Description |
|------|-------------|
| `list_sessions` | List all Zellij sessions |
| `session_info` | Current session info |
| `dump_layout` | Dump current layout |

## Common Workflows

**Open nvim in a new pane:**
```
new_pane --floating --command "nvim"
```

**Run tests in split pane:**
```
new_pane --direction right --command "npm test"
```

**Multi-pane development setup:**
```
new_pane --direction right --name "server" --command "npm run dev"
new_pane --direction down --name "tests"
write_command --command "npm test --watch"
```

## With prism-nvim

When combined with prism-nvim, you can orchestrate full workflows:

1. `new_pane --floating --command "nvim src/main.ts"` - Open nvim
2. Use prism-nvim tools to edit the file
3. `new_pane --direction down --command "npm test"` - Run tests
