//! Zellij Pane Bridge Plugin - The Agentic Companion
//!
//! Enables full pane control by ID without focus stealing.
//! Designed for AI agents that need to manage multiple panes autonomously.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};
use zellij_tile::prelude::*;

#[derive(Default)]
struct State {
    panes: HashMap<usize, Vec<PaneInfo>>,
    tabs: Vec<TabInfo>,
    command_results: HashMap<u32, CommandResult>,
    /// The pane that was focused when we received the first command - this is Claude's pane
    protected_pane_id: Option<u32>,
}

#[derive(Clone, Serialize)]
struct CommandResult {
    pane_id: u32,
    exit_code: Option<i32>,
    exited: bool,
}

#[derive(Deserialize)]
#[serde(tag = "cmd")]
enum Command {
    // === WRITE ===
    #[serde(rename = "write")]
    Write { pane_id: u32, chars: String },

    #[serde(rename = "write_bytes")]
    WriteBytes { pane_id: u32, bytes: Vec<u8> },

    // === READ ===
    #[serde(rename = "list")]
    List,

    #[serde(rename = "query")]
    Query { name: String },

    #[serde(rename = "list_tabs")]
    ListTabs,

    // === PANE CONTROL ===
    #[serde(rename = "focus")]
    Focus { pane_id: u32 },

    #[serde(rename = "close")]
    Close {
        pane_id: u32,
        #[serde(default)]
        force: bool,
    },

    #[serde(rename = "hide")]
    Hide {
        pane_id: u32,
        #[serde(default)]
        force: bool,
    },

    #[serde(rename = "show")]
    Show { pane_id: u32 },

    #[serde(rename = "clear")]
    Clear { pane_id: u32 },

    #[serde(rename = "fullscreen")]
    Fullscreen { pane_id: u32 },

    #[serde(rename = "rename_pane")]
    RenamePane { pane_id: u32, name: String },

    #[serde(rename = "move")]
    Move { pane_id: u32, direction: String },

    #[serde(rename = "resize")]
    Resize { pane_id: u32, direction: String },

    #[serde(rename = "toggle_floating")]
    TogglePaneFloating { pane_id: u32 },

    // === COMMAND EXECUTION ===
    #[serde(rename = "run")]
    Run {
        command: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default)]
        cwd: Option<String>,
        #[serde(default)]
        floating: bool,
    },

    #[serde(rename = "rerun")]
    Rerun { pane_id: u32 },

    #[serde(rename = "command_status")]
    CommandStatus { pane_id: u32 },

    // === TAB OPERATIONS ===
    #[serde(rename = "new_tab")]
    NewTab {
        #[serde(default)]
        name: Option<String>,
        #[serde(default)]
        cwd: Option<String>,
    },

    #[serde(rename = "close_tab")]
    CloseTab {
        index: u32,
        #[serde(default)]
        force: bool,
    },

    #[serde(rename = "focus_tab")]
    FocusTab {
        name: String,
        #[serde(default)]
        create: bool,
    },

    #[serde(rename = "goto_tab")]
    GotoTab { index: u32 },

    // === SESSION ===
    #[serde(rename = "session_info")]
    SessionInfo,

    #[serde(rename = "detach")]
    Detach,

    // === PROTECTION ===
    #[serde(rename = "protect")]
    Protect { pane_id: u32 },

    #[serde(rename = "get_protected")]
    GetProtected,
}

#[derive(Serialize)]
struct Response {
    success: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<serde_json::Value>,
}

#[derive(Serialize)]
struct PaneSummary {
    id: u32,
    is_plugin: bool,
    title: String,
    is_focused: bool,
    is_floating: bool,
    is_fullscreen: bool,
    is_suppressed: bool,
    tab_index: usize,
    command: Option<String>,
    exit_status: Option<i32>,
    exited: bool,
    rows: usize,
    cols: usize,
}

#[derive(Serialize)]
struct TabSummary {
    index: usize,
    name: String,
    active: bool,
    is_fullscreen: bool,
    is_sync: bool,
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, _configuration: BTreeMap<String, String>) {
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::WriteToStdin,
            PermissionType::ChangeApplicationState,
            PermissionType::ReadCliPipes,
            PermissionType::OpenTerminalsOrPlugins,
            PermissionType::RunCommands,
        ]);

        subscribe(&[
            EventType::PaneUpdate,
            EventType::TabUpdate,
            EventType::CommandPaneOpened,
            EventType::CommandPaneExited,
        ]);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PaneUpdate(pane_manifest) => {
                self.panes = pane_manifest.panes;
            }
            Event::TabUpdate(tabs) => {
                self.tabs = tabs;
            }
            Event::CommandPaneOpened(pane_id, _context) => {
                self.command_results.insert(pane_id, CommandResult {
                    pane_id,
                    exit_code: None,
                    exited: false,
                });
            }
            Event::CommandPaneExited(pane_id, exit_code, _context) => {
                self.command_results.insert(pane_id, CommandResult {
                    pane_id,
                    exit_code,
                    exited: true,
                });
            }
            _ => {}
        }
        false
    }

    fn pipe(&mut self, pipe_message: PipeMessage) -> bool {
        // On first command, detect and protect the focused pane (where Claude is running)
        if self.protected_pane_id.is_none() {
            self.detect_protected_pane();
        }

        let pipe_name = pipe_message.name.clone();
        let payload = pipe_message.payload.clone().unwrap_or_default();
        let response = self.handle_command(&pipe_name, &payload);

        if let PipeSource::Cli(pipe_id) = pipe_message.source {
            let response_json = serde_json::to_string(&response)
                .unwrap_or_else(|e| format!(r#"{{"success":false,"error":"{}"}}"#, e));
            cli_pipe_output(&pipe_id, &response_json);
            unblock_cli_pipe_input(&pipe_id);
        }
        false
    }

    fn render(&mut self, _rows: usize, _cols: usize) {
        println!("Zellij Pane Bridge - Agentic Companion v0.3.0");
        if let Some(pid) = self.protected_pane_id {
            println!("Protected pane: {}", pid);
        }
    }
}

impl State {
    fn handle_command(&mut self, name: &str, payload: &str) -> Response {
        // Inject the pipe name as the "cmd" field for serde deserialization
        let json_with_cmd = if payload.trim().is_empty() || payload == "{}" {
            format!(r#"{{"cmd":"{}"}}"#, name)
        } else {
            // Parse payload, inject cmd field, re-serialize
            match serde_json::from_str::<serde_json::Value>(payload) {
                Ok(mut v) => {
                    if let Some(obj) = v.as_object_mut() {
                        obj.insert("cmd".to_string(), serde_json::Value::String(name.to_string()));
                    }
                    serde_json::to_string(&v).unwrap_or_else(|_| payload.to_string())
                }
                Err(_) => format!(r#"{{"cmd":"{}"}}"#, name),
            }
        };

        let cmd_result: Result<Command, serde_json::Error> = serde_json::from_str(&json_with_cmd);

        match cmd_result {
            Ok(cmd) => self.execute_command(cmd),
            Err(e) => Response {
                success: false,
                error: Some(format!("Invalid command '{}': {} (payload: {})", name, e, json_with_cmd)),
                data: None,
            },
        }
    }

    fn detect_protected_pane(&mut self) {
        // Find the currently focused terminal pane - this is where Claude is running
        for pane_list in self.panes.values() {
            for p in pane_list {
                if p.is_focused && !p.is_plugin {
                    self.protected_pane_id = Some(p.id);
                    return;
                }
            }
        }
    }

    fn is_protected_pane(&self, pane_id: u32) -> bool {
        // Check if this is the protected pane (Claude's pane)
        if self.protected_pane_id == Some(pane_id) {
            return true;
        }

        // Also check for panes with "claude" in name/command as fallback
        for pane_list in self.panes.values() {
            for p in pane_list {
                if p.id == pane_id {
                    let title_lower = p.title.to_lowercase();
                    return title_lower.contains("claude") ||
                           title_lower.contains("anthropic") ||
                           p.terminal_command.as_ref()
                               .map(|c| c.to_lowercase().contains("claude"))
                               .unwrap_or(false);
                }
            }
        }
        false
    }

    fn execute_command(&mut self, cmd: Command) -> Response {
        match cmd {
            // === WRITE ===
            Command::Write { pane_id, chars } => {
                write_chars_to_pane_id(&chars, PaneId::Terminal(pane_id));
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"written": chars.len(), "pane_id": pane_id})),
                }
            }

            Command::WriteBytes { pane_id, bytes } => {
                write_to_pane_id(bytes.clone(), PaneId::Terminal(pane_id));
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"written": bytes.len(), "pane_id": pane_id})),
                }
            }

            // === READ ===
            Command::List => {
                let panes: Vec<PaneSummary> = self.panes.iter()
                    .flat_map(|(tab_idx, pane_list)| {
                        pane_list.iter().map(move |p| PaneSummary {
                            id: p.id,
                            is_plugin: p.is_plugin,
                            title: p.title.clone(),
                            is_focused: p.is_focused,
                            is_floating: p.is_floating,
                            is_fullscreen: p.is_fullscreen,
                            is_suppressed: p.is_suppressed,
                            tab_index: *tab_idx,
                            command: p.terminal_command.clone(),
                            exit_status: p.exit_status,
                            exited: p.exited,
                            rows: p.pane_content_rows,
                            cols: p.pane_content_columns,
                        })
                    })
                    .collect();

                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::to_value(panes).unwrap_or_default()),
                }
            }

            Command::Query { name } => {
                let name_lower = name.to_lowercase();
                let matches: Vec<PaneSummary> = self.panes.iter()
                    .flat_map(|(tab_idx, pane_list)| {
                        pane_list.iter()
                            .filter(|p| {
                                p.title.to_lowercase().contains(&name_lower) ||
                                p.terminal_command.as_ref()
                                    .map(|c| c.to_lowercase().contains(&name_lower))
                                    .unwrap_or(false)
                            })
                            .map(move |p| PaneSummary {
                                id: p.id,
                                is_plugin: p.is_plugin,
                                title: p.title.clone(),
                                is_focused: p.is_focused,
                                is_floating: p.is_floating,
                                is_fullscreen: p.is_fullscreen,
                                is_suppressed: p.is_suppressed,
                                tab_index: *tab_idx,
                                command: p.terminal_command.clone(),
                                exit_status: p.exit_status,
                                exited: p.exited,
                                rows: p.pane_content_rows,
                                cols: p.pane_content_columns,
                            })
                    })
                    .collect();

                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::to_value(matches).unwrap_or_default()),
                }
            }

            Command::ListTabs => {
                let tabs: Vec<TabSummary> = self.tabs.iter()
                    .map(|t| TabSummary {
                        index: t.position,
                        name: t.name.clone(),
                        active: t.active,
                        is_fullscreen: t.is_fullscreen_active,
                        is_sync: t.is_sync_panes_active,
                    })
                    .collect();

                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::to_value(tabs).unwrap_or_default()),
                }
            }

            // === PANE CONTROL ===
            Command::Focus { pane_id } => {
                focus_terminal_pane(pane_id, true);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"focused": pane_id})),
                }
            }

            Command::Close { pane_id, force } => {
                if !force && self.is_protected_pane(pane_id) {
                    Response {
                        success: false,
                        error: Some("Cannot close Claude pane - this would terminate the agent (use force:true to override)".to_string()),
                        data: Some(serde_json::json!({"protected_pane": pane_id})),
                    }
                } else {
                    close_terminal_pane(pane_id);
                    Response {
                        success: true,
                        error: None,
                        data: Some(serde_json::json!({"closed": pane_id})),
                    }
                }
            }

            Command::Hide { pane_id, force } => {
                if !force && self.is_protected_pane(pane_id) {
                    Response {
                        success: false,
                        error: Some("Cannot hide Claude pane - this would disrupt the agent (use force:true to override)".to_string()),
                        data: Some(serde_json::json!({"protected_pane": pane_id})),
                    }
                } else {
                    hide_pane_with_id(PaneId::Terminal(pane_id));
                    Response {
                        success: true,
                        error: None,
                        data: Some(serde_json::json!({"hidden": pane_id})),
                    }
                }
            }

            Command::Show { pane_id } => {
                show_pane_with_id(PaneId::Terminal(pane_id), false);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"shown": pane_id})),
                }
            }

            Command::Clear { pane_id } => {
                clear_screen_for_pane_id(PaneId::Terminal(pane_id));
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"cleared": pane_id})),
                }
            }

            Command::Fullscreen { pane_id } => {
                toggle_pane_id_fullscreen(PaneId::Terminal(pane_id));
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"toggled_fullscreen": pane_id})),
                }
            }

            Command::RenamePane { pane_id, name } => {
                rename_terminal_pane(pane_id, &name);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"renamed": pane_id, "name": name})),
                }
            }

            Command::Move { pane_id, direction } => {
                let dir = match direction.to_lowercase().as_str() {
                    "up" => Direction::Up,
                    "down" => Direction::Down,
                    "left" => Direction::Left,
                    "right" => Direction::Right,
                    _ => Direction::Right,
                };
                move_pane_with_pane_id_in_direction(PaneId::Terminal(pane_id), dir);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"moved": pane_id, "direction": direction})),
                }
            }

            Command::Resize { pane_id, direction } => {
                let resize = match direction.to_lowercase().as_str() {
                    "increase" | "up" | "right" => Resize::Increase,
                    "decrease" | "down" | "left" => Resize::Decrease,
                    _ => Resize::Increase,
                };
                let strategy = ResizeStrategy::new(resize, None);
                resize_pane_with_id(strategy, PaneId::Terminal(pane_id));
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"resized": pane_id, "direction": direction})),
                }
            }

            Command::TogglePaneFloating { pane_id } => {
                toggle_pane_embed_or_eject_for_pane_id(PaneId::Terminal(pane_id));
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"toggled_floating": pane_id})),
                }
            }

            // === COMMAND EXECUTION ===
            Command::Run { command, args, cwd, floating } => {
                let cmd = CommandToRun {
                    path: command.clone().into(),
                    args: args.clone(),
                    cwd: cwd.clone().map(|s| s.into()),
                };
                let context = BTreeMap::new();

                if floating {
                    open_command_pane_floating(cmd, None, context);
                } else {
                    open_command_pane(cmd, context);
                }

                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({
                        "command": command,
                        "args": args,
                        "floating": floating,
                        "cwd": cwd
                    })),
                }
            }

            Command::Rerun { pane_id } => {
                rerun_command_pane(pane_id);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"rerun": pane_id})),
                }
            }

            Command::CommandStatus { pane_id } => {
                if let Some(result) = self.command_results.get(&pane_id) {
                    Response {
                        success: true,
                        error: None,
                        data: Some(serde_json::to_value(result).unwrap_or_default()),
                    }
                } else {
                    Response {
                        success: true,
                        error: None,
                        data: Some(serde_json::json!({
                            "pane_id": pane_id,
                            "exited": false,
                            "exit_code": null
                        })),
                    }
                }
            }

            // === TAB OPERATIONS ===
            Command::NewTab { name, cwd } => {
                new_tab(name.as_deref(), cwd.as_deref());
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"created_tab": name, "cwd": cwd})),
                }
            }

            Command::CloseTab { index, force } => {
                // Check if this tab contains a Claude pane
                if !force {
                    if let Some(panes) = self.panes.get(&(index as usize)) {
                        for p in panes {
                            if self.is_protected_pane(p.id) {
                                return Response {
                                    success: false,
                                    error: Some("Cannot close tab containing Claude pane (use force:true to override)".to_string()),
                                    data: Some(serde_json::json!({"protected_tab": index, "claude_pane_id": p.id})),
                                };
                            }
                        }
                    }
                }
                close_tab_with_index(index as usize);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"closed_tab": index})),
                }
            }

            Command::FocusTab { name, create } => {
                if create {
                    focus_or_create_tab(&name);
                } else {
                    go_to_tab_name(&name);
                }
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"focused_tab": name, "create": create})),
                }
            }

            Command::GotoTab { index } => {
                go_to_tab(index);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"goto_tab": index})),
                }
            }

            // === SESSION ===
            Command::SessionInfo => {
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({
                        "tabs_count": self.tabs.len(),
                        "panes_count": self.panes.values().map(|v| v.len()).sum::<usize>(),
                        "tabs": self.tabs.iter().map(|t| &t.name).collect::<Vec<_>>(),
                        "protected_pane_id": self.protected_pane_id,
                    })),
                }
            }

            Command::Detach => {
                detach();
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"message": "Detaching"})),
                }
            }

            // === PROTECTION ===
            Command::Protect { pane_id } => {
                self.protected_pane_id = Some(pane_id);
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({"protected_pane_id": pane_id})),
                }
            }

            Command::GetProtected => {
                Response {
                    success: true,
                    error: None,
                    data: Some(serde_json::json!({
                        "protected_pane_id": self.protected_pane_id,
                        "auto_detected": self.protected_pane_id.is_some()
                    })),
                }
            }
        }
    }
}
