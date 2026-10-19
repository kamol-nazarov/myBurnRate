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
use tauri::{AppHandle, LogicalSize, Manager, RunEvent, Size, State, WindowEvent};

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
    if let Ok(env_root) = env::var("BURNRATE_ROOT") {
        let p = PathBuf::from(env_root.trim());
        if p.is_dir() {
            candidate_roots.push(p);
        }
    }
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
    #[cfg(target_os = "windows")]
    {
        let dev_repo = PathBuf::from(r"C:\Dev\myBurnRateOfficial");
        if dev_repo.is_dir() {
            candidate_roots.push(dev_repo);
        }
    }

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

#[cfg(target_os = "windows")]
fn enforce_win32_topmost(window: &tauri::WebviewWindow, pinned: bool) {
    if let Ok(hwnd) = window.hwnd() {
        #[link(name = "user32")]
        extern "system" {
            fn GetWindowLongW(hWnd: isize, nIndex: i32) -> i32;
            fn SetWindowLongW(hWnd: isize, nIndex: i32, dwNewLong: i32) -> i32;
            fn SetWindowPos(
                hWnd: isize,
                hWndInsertAfter: isize,
                X: i32,
                Y: i32,
                cx: i32,
                cy: i32,
                uFlags: u32,
            ) -> i32;
        }
        #[link(name = "dwmapi")]
        extern "system" {
            fn DwmSetWindowAttribute(
                hwnd: isize,
                dwAttribute: u32,
                pvAttribute: *const i32,
                cbAttribute: u32,
            ) -> i32;
        }
        const GWL_STYLE: i32 = -16;
        const GWL_EXSTYLE: i32 = -20;
        const WS_POPUP: u32 = 0x8000_0000;
        const WS_VISIBLE: u32 = 0x1000_0000;
        const WS_CLIPSIBLINGS: u32 = 0x0400_0000;
        const WS_CLIPCHILDREN: u32 = 0x0200_0000;
        const WS_EX_DLGMODALFRAME: u32 = 0x0000_0001;
        const WS_EX_TOPMOST: u32 = 0x0000_0008;
        const WS_EX_WINDOWEDGE: u32 = 0x0000_0100;
        const WS_EX_CLIENTEDGE: u32 = 0x0000_0200;
        const WS_EX_STATICEDGE: u32 = 0x0002_0000;
        const WS_EX_APPWINDOW: u32 = 0x0004_0000;
        const WS_EX_NOREDIRECTIONBITMAP: u32 = 0x0020_0000;
        const DWMWA_WINDOW_CORNER_PREFERENCE: u32 = 33;
        const DWMWCP_ROUND: i32 = 2;
        const HWND_TOPMOST: isize = -1;
        const HWND_NOTOPMOST: isize = -2;
        const SWP_NOSIZE: u32 = 0x0001;
        const SWP_NOMOVE: u32 = 0x0002;
        const SWP_NOACTIVATE: u32 = 0x0010;
        const SWP_FRAMECHANGED: u32 = 0x0020;
        const SWP_SHOWWINDOW: u32 = 0x0040;
        unsafe {
            let h = hwnd.0 as isize;
            let target_style = (WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN) as i32;
            let cur_style = GetWindowLongW(h, GWL_STYLE);
            let mut flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW;
            if cur_style != target_style {
                SetWindowLongW(h, GWL_STYLE, target_style);
                flags |= SWP_FRAMECHANGED;
            }
            let old_ex = GetWindowLongW(h, GWL_EXSTYLE) as u32;
            let mut new_ex = (old_ex
                & !(WS_EX_DLGMODALFRAME
                    | WS_EX_WINDOWEDGE
                    | WS_EX_CLIENTEDGE
                    | WS_EX_STATICEDGE
                    | WS_EX_NOREDIRECTIONBITMAP))
                | WS_EX_APPWINDOW;
            if pinned {
                new_ex |= WS_EX_TOPMOST;
            } else {
                new_ex &= !WS_EX_TOPMOST;
            }
            if new_ex != old_ex {
                SetWindowLongW(h, GWL_EXSTYLE, new_ex as i32);
                flags |= SWP_FRAMECHANGED;
            }
            let corner_pref: i32 = DWMWCP_ROUND;
            DwmSetWindowAttribute(
                h,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                &corner_pref as *const i32,
                std::mem::size_of::<i32>() as u32,
            );
            SetWindowPos(
                h,
                if pinned { HWND_TOPMOST } else { HWND_NOTOPMOST },
                0,
                0,
                0,
                0,
                flags,
            );
        }
    }
}

#[cfg(not(target_os = "windows"))]
fn enforce_win32_topmost(_window: &tauri::WebviewWindow, _pinned: bool) {}

fn apply_hud_always_on_top(window: &tauri::WebviewWindow, pinned: bool) -> Result<(), String> {
    #[cfg(not(target_os = "windows"))]
    window.set_always_on_top(pinned).map_err(|e| e.to_string())?;
    enforce_win32_topmost(window, pinned);
    Ok(())
}

#[cfg(target_os = "windows")]
fn resize_hud_window(window: &tauri::WebviewWindow, width: f64, height: f64, pinned: bool) -> Result<(), String> {
    enforce_win32_topmost(window, pinned);
    if let Ok(hwnd) = window.hwnd() {
        #[link(name = "user32")]
        extern "system" {
            fn GetDpiForWindow(hwnd: isize) -> u32;
            fn SetWindowPos(
                hWnd: isize,
                hWndInsertAfter: isize,
                X: i32,
                Y: i32,
                cx: i32,
                cy: i32,
                uFlags: u32,
            ) -> i32;
        }
        const HWND_TOPMOST: isize = -1;
        const HWND_NOTOPMOST: isize = -2;
        const SWP_NOMOVE: u32 = 0x0002;
        const SWP_NOACTIVATE: u32 = 0x0010;
        const SWP_SHOWWINDOW: u32 = 0x0040;
        unsafe {
            let h = hwnd.0 as isize;
            let dpi = GetDpiForWindow(h);
            let scale = if dpi > 0 { (dpi as f64) / 96.0 } else { 1.0 };
            let pw = ((width * scale).round() as i32).max(240);
            let ph = ((height * scale).round() as i32).max(140);
            SetWindowPos(
                h,
                if pinned { HWND_TOPMOST } else { HWND_NOTOPMOST },
                0,
                0,
                pw,
                ph,
                SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            );
        }
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn resize_hud_window(window: &tauri::WebviewWindow, width: f64, height: f64, pinned: bool) -> Result<(), String> {
    let _ = window.set_min_size(Some(Size::Logical(LogicalSize::new(260.0, 160.0))));
    window
        .set_size(Size::Logical(LogicalSize::new(width, height)))
        .map_err(|e| e.to_string())?;
    let _ = apply_hud_always_on_top(window, pinned);
    Ok(())
}

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
fn toggle_hud(app: AppHandle, state: State<'_, HudPinState>) -> Result<bool, String> {
    let window = app
        .get_webview_window("hud")
        .ok_or_else(|| "Sidecar HUD window not found".to_string())?;
    let visible = window.is_visible().unwrap_or(false);
    if visible {
        window.hide().map_err(|e| e.to_string())?;
        Ok(false)
    } else {
        window.show().map_err(|e| e.to_string())?;
        let pinned = state.pinned.load(Ordering::SeqCst);
        let _ = apply_hud_always_on_top(&window, pinned);
        let _ = window.set_focus();
        Ok(true)
    }
}

#[tauri::command]
fn show_hud(app: AppHandle, state: State<'_, HudPinState>) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("hud") {
        window.show().map_err(|e| e.to_string())?;
        let pinned = state.pinned.load(Ordering::SeqCst);
        let _ = apply_hud_always_on_top(&window, pinned);
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
fn get_hud_pin(app: AppHandle, state: State<'_, HudPinState>) -> Result<bool, String> {
    let pinned = state.pinned.load(Ordering::SeqCst);
    if let Some(window) = app.get_webview_window("hud") {
        let _ = apply_hud_always_on_top(&window, pinned);
    }
    Ok(pinned)
}

#[tauri::command]
fn set_hud_pin(app: AppHandle, state: State<'_, HudPinState>, pinned: bool) -> Result<bool, String> {
    state.pinned.store(pinned, Ordering::SeqCst);
    if let Some(window) = app.get_webview_window("hud") {
        apply_hud_always_on_top(&window, pinned)?;
    }
    Ok(pinned)
}

#[tauri::command]
fn toggle_hud_pin(app: AppHandle, state: State<'_, HudPinState>) -> Result<bool, String> {
    let next = !state.pinned.load(Ordering::SeqCst);
    state.pinned.store(next, Ordering::SeqCst);
    if let Some(window) = app.get_webview_window("hud") {
        apply_hud_always_on_top(&window, next)?;
    }
    Ok(next)
}

#[tauri::command]
fn set_hud_size(app: AppHandle, state: State<'_, HudPinState>, width: f64, height: f64) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("hud") {
        let w = width.clamp(260.0, 900.0);
        let h = height.clamp(160.0, 1000.0);
        let pinned = state.pinned.load(Ordering::SeqCst);
        resize_hud_window(&window, w, h, pinned)?;
    }
    Ok(())
}

#[tauri::command]
fn start_hud_drag(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("hud") {
        #[cfg(target_os = "windows")]
        if let Ok(hwnd) = window.hwnd() {
            #[link(name = "user32")]
            extern "system" {
                fn ReleaseCapture() -> i32;
                fn PostMessageW(hWnd: isize, Msg: u32, wParam: usize, lParam: isize) -> i32;
            }
            const WM_NCLBUTTONDOWN: u32 = 0x00A1;
            const HTCAPTION: usize = 2;
            unsafe {
                ReleaseCapture();
                PostMessageW(hwnd.0 as isize, WM_NCLBUTTONDOWN, HTCAPTION, 0);
            }
            return Ok(());
        }
        window.start_dragging().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn start_main_drag(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.start_dragging().map_err(|e| e.to_string())?;
    }
    Ok(())
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
fn minimize_main(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.minimize().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn toggle_maximize_main(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_maximized().unwrap_or(false) { window.unmaximize().map_err(|e| e.to_string())?; } else { window.maximize().map_err(|e| e.to_string())?; }
    }
    Ok(())
}

#[tauri::command]
fn hide_main(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.hide().map_err(|e| e.to_string())?;
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

#[cfg(target_os = "windows")]
fn release_webview_lock_if_needed() {
    let my_pid = std::process::id();
    let script = format!(
        "Get-CimInstance Win32_Process -Filter \"Name='burnrate-desktop.exe' OR Name='msedgewebview2.exe'\" -ErrorAction SilentlyContinue | Where-Object {{ ($_.Name -eq 'burnrate-desktop.exe' -and $_.ProcessId -ne {my_pid}) -or ($_.Name -eq 'msedgewebview2.exe' -and $_.CommandLine -like '*burnrate-desktop.exe*') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
    );
    let mut cmd = Command::new("powershell");
    cmd.args([
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        &script,
    ])
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null());
    apply_no_window(&mut cmd);
    let _ = cmd.status();
}

#[cfg(not(target_os = "windows"))]
fn release_webview_lock_if_needed() {}

pub fn run() {
    release_webview_lock_if_needed();
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
            get_hud_pin,
            set_hud_pin,
            toggle_hud_pin,
            set_hud_size,
            start_hud_drag,
            start_main_drag,
            show_dashboard,
            minimize_main,
            toggle_maximize_main,
            hide_main,
            open_setup_wizard
        ])
        .setup(|app| {
            if let Some(hud_win) = app.get_webview_window("hud") {
                let _ = apply_hud_always_on_top(&hud_win, true);
            }

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
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud(app.clone(), state);
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
                        let app = tray.app_handle();
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud(app.clone(), state);
                    }
                });

            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }

            let _tray = tray_builder.build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| match event {
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            WindowEvent::Focused(true) => {
                if window.label() == "hud" {
                    let state = window.app_handle().state::<HudPinState>();
                    let pinned = state.pinned.load(Ordering::SeqCst);
                    if let Some(hud_win) = window.app_handle().get_webview_window("hud") {
                        let _ = apply_hud_always_on_top(&hud_win, pinned);
                    }
                }
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("error while building BURNRATE desktop application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            app_handle.state::<BackendSupervisor>().stop_all();
        }
    });
}
