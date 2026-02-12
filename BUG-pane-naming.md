# Bug: create_named_pane does not properly name panes

**Discovered**: 2026-02-12 during shepherd skill testing
**Severity**: Medium (affects pane targeting workflows)
**Status**: RESOLVED via architectural change (v0.11.0)

## Problem

When using `create_named_pane`, the pane is created but the name is not set. The `list_named_panes` tool shows `alive: false` for all created panes because `find_pane_by_name` cannot match them in the layout dump.

## Root Cause

1. Zellij doesn't support creating panes in detached sessions
2. `rename-pane` renames the *currently focused* pane, not necessarily the newly created one
3. Pane names set via CLI don't persist in layout dumps for detached sessions

## Resolution (v0.11.0)

**Architectural change: Tab-based workspaces instead of session isolation**

- Removed agent session isolation (Zellij limitation: can't create panes in detached sessions)
- All pane operations now work in the current attached session
- Agent work organized into dedicated tabs (e.g., "agents", "analysis", "build")
- Pane targeting uses content markers and registered indices as fallbacks

This resolves the issue because panes are now created in an attached session where:
1. The `--name` flag works with `new-pane`
2. `rename-pane` can reliably target the newly created pane
3. Focus operations work correctly

## Migration

Users relying on session isolation should update their workflows:
```python
# Old (deprecated): Separate session
create_named_pane(name="worker")  # Would go to zellij-agent session

# New: Tab-based organization
create_named_pane(name="worker", tab="work")  # Creates in "work" tab
```
