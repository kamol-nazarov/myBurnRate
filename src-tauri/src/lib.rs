use std::env;
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, State, WindowEvent};

pub struct HudPinState {
    pub pinned: AtomicBool,
}

pub struct BackendSupervisor {
    pub hub_child: Mutex<Option<Child>>,
    pub sync_child: Mutex<Option<Child>>,
}

impl BackendSupervisor {
    pub fn stop_all(&self) {
        if let Ok(mut guard) = self.hub_child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        if let Ok(mut guard) = self.sync_child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn hub_port_reachable(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

fn resolve_python_and_root() -> (PathBuf, PathBuf) {
    let mut candidate_roots = Vec::new();
    if let Ok(exe) = env::current_exe() {
        // Handle .../src-tauri/target/{release,debug}/burnrate-desktop.exe
        if let Some(repo_from_exe) = exe
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
        {
            candidate_roots.push(repo_from_exe.to_path_buf());
        }
        if let Some(exe_dir) = exe.parent() {
            candidate_roots.push(exe_dir.to_path_buf());
        }
    }
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    if cwd.ends_with("src-tauri") {
        if let Some(parent) = cwd.parent() {
            candidate_roots.push(parent.to_path_buf());
        }
    }
    candidate_roots.push(cwd.clone());

    for root in &candidate_roots {
        #[cfg(target_os = "windows")]
        let venv_pyw = root.join(".venv").join("Scripts").join("pythonw.exe");
        #[cfg(target_os = "windows")]
        if venv_pyw.is_file() {
            return (venv_pyw, root.clone());
        }
        #[cfg(target_os = "windows")]
        let venv_py = root.join(".venv").join("Scripts").join("python.exe");
        #[cfg(not(target_os = "windows"))]
        let venv_py = root.join(".venv").join("bin").join("python");
        if venv_py.is_file() {
            return (venv_py, root.clone());
        }
    }

    let fallback_root = candidate_roots.into_iter().next().unwrap_or(cwd);
    (PathBuf::from("python"), fallback_root)
}

#[cfg(target_os = "windows")]
fn apply_no_window(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    cmd.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn apply_no_window(_cmd: &mut Command) {}

fn ensure_hub_and_sync_processes() -> (Option<Child>, Option<Child>) {
    let port: u16 = env::var("BURNRATE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(17331);
    let (python_bin, repo_root) = resolve_python_and_root();

    let mut hub_child = None;
    if !hub_port_reachable(port) {
        let mut cmd = Command::new(&python_bin);
        cmd.current_dir(&repo_root)
            .args([
                "-m",
                "spend_app",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                &port.to_string(),
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        apply_no_window(&mut cmd);

        if let Ok(child) = cmd.spawn() {
            hub_child = Some(child);
            let deadline = Instant::now() + Duration::from_secs(8);
            while Instant::now() < deadline && !hub_port_reachable(port) {
                thread::sleep(Duration::from_millis(120));
            }
        }
    }

    let mut sync_child = None;
    if let (Ok(hub_url), Ok(token)) = (
        env::var("BURNRATE_SYNC_HUB"),
        env::var("BURNRATE_SYNC_TOKEN"),
    ) {
        if !hub_url.trim().is_empty() && !token.trim().is_empty() {
            let mut cmd = Command::new(&python_bin);
            cmd.current_dir(&repo_root)
                .args([
                    "-m",
                    "spend_app",
                    "sync",
                    "--hub",
                    hub_url.trim(),
                    "--token",
                    token.trim(),
                    "--interval",
                    "5",
                ])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            apply_no_window(&mut cmd);
            if let Ok(child) = cmd.spawn() {
                sync_child = Some(child);
            }
        }
    }

    (hub_child, sync_child)
}

#[tauri::command]
fn toggle_hud(app: AppHandle) -> Result<bool, String> {
    let window = app
        .get_webview_window("hud")
        .ok_or_else(|| "Sidecar HUD window not found".to_string())?;
    let visible = window.is_visible().unwrap_or(false);
    if visible {
        window.hide().map_err(|e| e.to_string())?;
        Ok(false)
    } else {
        window.show().map_err(|e| e.to_string())?;
        let _ = window.set_focus();
        Ok(true)
    }
}

#[tauri::command]
fn show_hud(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("hud") {
        window.show().map_err(|e| e.to_string())?;
        let _ = window.set_focus();
    }
    Ok(())
}

#[tauri::command]
fn hide_hud(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("hud") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn toggle_hud_pin(app: AppHandle, state: State<'_, HudPinState>) -> Result<bool, String> {
    let next = !state.pinned.load(Ordering::SeqCst);
    if let Some(window) = app.get_webview_window("hud") {
        window.set_always_on_top(next).map_err(|e| e.to_string())?;
    }
    state.pinned.store(next, Ordering::SeqCst);
    Ok(next)
}

#[tauri::command]
fn show_dashboard(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.show().map_err(|e| e.to_string())?;
        let _ = window.unminimize();
        window.set_focus().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn open_setup_wizard(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval("window.location.href = '/setup';");
        window.show().map_err(|e| e.to_string())?;
        let _ = window.unminimize();
        window.set_focus().map_err(|e| e.to_string())?;
    }
    Ok(())
}

pub fn run() {
    let (hub_child, sync_child) = ensure_hub_and_sync_processes();

    let app = tauri::Builder::default()
        .manage(HudPinState {
            pinned: AtomicBool::new(true),
        })
        .manage(BackendSupervisor {
            hub_child: Mutex::new(hub_child),
            sync_child: Mutex::new(sync_child),
        })
        .invoke_handler(tauri::generate_handler![
            toggle_hud,
            show_hud,
            hide_hud,
            toggle_hud_pin,
            show_dashboard,
            open_setup_wizard
        ])
        .setup(|app| {
            let toggle_hud_item = MenuItem::with_id(
                app,
                "toggle_hud",
                "Toggle Sidecar HUD",
                true,
                None::<&str>,
            )?;
            let pin_hud_item = MenuItem::with_id(
                app,
                "pin_hud",
                "Toggle HUD Always on Top",
                true,
                None::<&str>,
            )?;
            let show_main_item = MenuItem::with_id(
                app,
                "show_dashboard",
                "Open Full Dashboard",
                true,
                None::<&str>,
            )?;
            let setup_wizard_item = MenuItem::with_id(
                app,
                "setup_wizard",
                "Run Setup Wizard…",
                true,
                None::<&str>,
            )?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit BURNRATE", true, None::<&str>)?;

            let menu = Menu::with_items(
                app,
                &[
                    &toggle_hud_item,
                    &pin_hud_item,
                    &show_main_item,
                    &setup_wizard_item,
                    &sep,
                    &quit_item,
                ],
            )?;

            let mut tray_builder = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("BURNRATE · Live Multi-Computer Spend & Sidecar HUD")
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "toggle_hud" => {
                        let _ = toggle_hud(app.clone());
                    }
                    "pin_hud" => {
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud_pin(app.clone(), state);
                    }
                    "show_dashboard" => {
                        let _ = show_dashboard(app.clone());
                    }
                    "setup_wizard" => {
                        let _ = open_setup_wizard(app.clone());
                    }
                    "quit" => {
                        app.state::<BackendSupervisor>().stop_all();
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let _ = toggle_hud(tray.app_handle().clone());
                    }
                });

            if let Some(icon) = app.default_window_icon().cloned() {
                if let Some(main_win) = app.get_webview_window("main") {
                    let _ = main_win.set_icon(icon.clone());
                }
                if let Some(hud_win) = app.get_webview_window("hud") {
                    let _ = hud_win.set_icon(icon.clone());
                }
                tray_builder = tray_builder.icon(icon);
            }
            if let Some(main_win) = app.get_webview_window("main") {
                if env::var("BURNRATE_OPEN_SETUP").ok().as_deref() == Some("1") {
                    if let Ok(url) = "http://127.0.0.1:17331/setup".parse() {
                        let _ = main_win.navigate(url);
                    }
                }
                let _ = main_win.show();
                let _ = main_win.unminimize();
                let _ = main_win.set_focus();
            }
            let _tray = tray_builder.build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let label = window.label();
                if label == "main" || label == "hud" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building BURNRATE Tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            app_handle.state::<BackendSupervisor>().stop_all();
        }
    });
}
