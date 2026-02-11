# zellij-mcp Development

## Structure

```
zellij-mcp/
├── .claude-plugin/
│   ├── CLAUDE.md      # User instructions
│   └── plugin.json    # Marketplace metadata
├── server.py          # MCP server (Python)
├── scripts/
│   └── install.sh     # Installation script
├── README.md
└── CLAUDE.md          # This file (development)
```

## Testing

Run the server directly:
```bash
python3 server.py
```

Test with MCP inspector:
```bash
npx @anthropics/mcp-inspector python3 server.py
```

## Adding Tools

1. Add `Tool` definition in `list_tools()`
2. Add handler in `call_tool()`
3. Update `.claude-plugin/CLAUDE.md` with documentation

## Zellij Actions Reference

Full list: `zellij action --help`

Common actions:
- `new-pane` - Create pane
- `close-pane` - Close pane
- `move-focus` - Move focus
- `write-chars` - Send text
- `go-to-tab` - Switch tab
- `rename-tab` / `rename-pane`
- `toggle-floating-panes`
- `dump-layout` - Export layout

## Release

```bash
# Bump version in plugin.json
git tag v0.1.0
git push origin main --tags
```
