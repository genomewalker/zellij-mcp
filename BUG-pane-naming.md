# Bug: create_named_pane does not properly name panes

**Discovered**: 2026-02-12 during shepherd skill testing
**Severity**: Medium (affects pane targeting workflows)

## Problem

When using `create_named_pane`, the pane is created but the name is not set. The `list_named_panes` tool shows `alive: false` for all created panes because `find_pane_by_name` cannot match them in the layout dump.

## Steps to Reproduce

```bash
# Create a named pane via zellij CLI
zellij -s cc-soul action new-pane --name "test-pane" --floating

# Check if name appears in layout
zellij -s cc-soul action dump-layout | grep "test-pane"
# Returns nothing - name not set
```

## Root Cause Analysis

The `create_named_pane` function in `server.py`:
1. Creates a new pane via `new-pane` action
2. Then runs `rename-pane` action to set the name
3. BUT `rename-pane` renames the *currently focused* pane
4. In detached sessions or when focus changes, the wrong pane gets renamed

## Relevant Code

```python
# server.py line ~1600
# The issue is that new-pane doesn't guarantee focus on the new pane
# before rename-pane is called
```

## Potential Fix

Option 1: Use `--name` flag directly with `new-pane` (if zellij supports it natively)
Option 2: Focus the new pane explicitly before renaming
Option 3: Track pane by command/cwd instead of name for identification
