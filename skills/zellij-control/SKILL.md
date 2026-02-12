---
name: zellij
description: Interactive control center for managing Zellij sessions, tabs, and panes
execution: direct
---

# Zellij Control Center

Interactive control center for managing Zellij sessions, tabs, and panes.

## Instructions

You are the Zellij Control Center. Use ToolSearch to load zellij-mcp tools first, then gather the current state and present options to the user.

### Step 1: Load Tools and Get State

```
ToolSearch query="+zellij list"
```

Then call these tools to get current state:
- `list_sessions` - all zellij sessions
- `list_panes` - panes in current session
- `query_tab_names` - tabs in current session
- `agent_session action="status"` - agent session status

### Step 2: Present Control Center

Use AskUserQuestion to present the main menu with current state summary:

```
Questions:
  - question: "Zellij Control Center - What would you like to do?"
    header: "Action"
    options:
      - label: "Switch Session"
        description: "Attach to a different zellij session"
      - label: "Manage Tabs"
        description: "Create, switch, or close tabs"
      - label: "Manage Panes"
        description: "Create, focus, or close panes"
      - label: "Agent Session"
        description: "Create/destroy isolated agent workspace"
```

### Step 3: Handle User Choice

Based on selection, show appropriate sub-menu:

**Switch Session:**
- List all sessions from `list_sessions`
- Use AskUserQuestion with session names as options
- Inform user how to attach: `zellij attach <session-name>`

**Manage Tabs:**
```
Questions:
  - question: "Tab Management - Current tabs: [list]. What to do?"
    header: "Tabs"
    options:
      - label: "Switch to Tab"
        description: "Focus a different tab"
      - label: "Create Tab"
        description: "Create a new named tab"
      - label: "Close Tab"
        description: "Close the current tab"
```

For "Switch to Tab", show another AskUserQuestion with tab names.
For "Create Tab", ask for the tab name.

**Manage Panes:**
```
Questions:
  - question: "Pane Management - What to do?"
    header: "Panes"
    options:
      - label: "Create Named Pane"
        description: "Create a new pane with a name"
      - label: "Focus Pane"
        description: "Switch to a named pane"
      - label: "List Panes"
        description: "Show all panes with details"
      - label: "Toggle Floating"
        description: "Show/hide floating panes"
```

**Agent Session:**
```
Questions:
  - question: "Agent Session [status]. What to do?"
    header: "Agent"
    options:
      - label: "Create Agent Session"
        description: "Create isolated workspace for autonomous ops"
      - label: "Destroy Agent Session"
        description: "Remove the agent session"
      - label: "List Agent Panes"
        description: "Show panes in agent session"
```

### Step 4: Execute Action

After user makes final selection, execute the appropriate zellij-mcp tool and report the result.

### Important

- Always show current state in question text (e.g., "Current tabs: work, dev, logs")
- Use short, clear option labels
- After completing an action, offer to return to main menu
- For session switching, note that `zellij attach` must be run manually in terminal
