use std::env;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent, Wry};
use auto_launch::{AutoLaunch, AutoLaunchBuilder, MacOSLaunchMode, WindowsEnableMode};
use tauri_plugin_updater::UpdaterExt;

const DEFAULT_PORT: u16 = 17331;
// Windows open on a bundled "starting" page and move to the service once it answers.
const STARTUP_WARNING_AFTER: Duration = Duration::from_secs(60);
const WATCH_INTERVAL: Duration = Duration::from_secs(15);
const RESTART_BUDGET: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(600);
const UPDATE_FIRST_CHECK: Duration = Duration::from_secs(45);
const UPDATE_CHECK_EVERY: Duration = Duration::from_secs(6 * 60 * 60);

pub struct HudPinState {
    pub pinned: AtomicBool,
}

#[cfg(target_os = "windows")]
static ORIG_HUD_WNDPROC: AtomicIsize = AtomicIsize::new(0);
#[cfg(target_os = "windows")]
static HUD_DRAGGING: AtomicBool = AtomicBool::new(false);

fn configured_port() -> u16 {
    env::var("BURNRATE_PORT")
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(DEFAULT_PORT)
}

fn port_reachable(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

/// Where the service runs from.
#[derive(Clone)]
enum Backend {
    /// The frozen service an installed app ships (packaging/build_service.py).
    Bundled(PathBuf),
    /// A checkout's Python environment: development and script installs.
    Python { bin: PathBuf, root: PathBuf },
}

impl Backend {
    fn is_bundled(&self) -> bool {
        matches!(self, Backend::Bundled(_))
    }

    /// `module` runs under Python; the frozen service takes the same arguments directly.
    fn command(&self, module: &str, args: &[&str]) -> Command {
        let mut cmd = match self {
            Backend::Bundled(exe) => {
                let mut cmd = Command::new(exe);
                if let Some(dir) = exe.parent() {
                    cmd.current_dir(dir);
                }
                cmd
            }
            Backend::Python { bin, root } => {
                let mut cmd = Command::new(bin);
                cmd.current_dir(root).args(["-m", module]);
                cmd
            }
        };
        cmd.args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        apply_no_window(&mut cmd);
        cmd
    }
}

fn bundled_service(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let name = if cfg!(target_os = "windows") { "burnrate-service.exe" } else { "burnrate-service" };
    let exe = resource_dir?.join("burnrate-service").join(name);
    exe.is_file().then_some(exe)
}

fn resolve_backend(resource_dir: Option<PathBuf>) -> Backend {
    // BURNRATE_ROOT points a build at a checkout on purpose.
    if env::var_os("BURNRATE_ROOT").is_none() {
        if let Some(exe) = bundled_service(resource_dir.as_deref()) {
            return Backend::Bundled(exe);
        }
    }
    let (bin, root) = resolve_python_and_root();
    Backend::Python { bin, root }
}

/// BURNRATE's data folder, as spend_app.config.default_app_dir finds it.
fn data_root() -> PathBuf {
    if let Some(root) = env::var_os("BURNRATE_DATA_ROOT").filter(|v| !v.is_empty()) {
        return PathBuf::from(root);
    }
    let base = env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| env::var_os("XDG_DATA_HOME").map(PathBuf::from))
        .or_else(|| {
            env::var_os("HOME").map(|home| {
                if cfg!(target_os = "windows") {
                    PathBuf::from(home).join("AppData").join("Local")
                } else {
                    PathBuf::from(home).join(".local").join("share")
                }
            })
        })
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("BURNRATE")
}

#[cfg(target_os = "windows")]
mod child_job {
    use std::os::windows::io::AsRawHandle;
    use std::sync::OnceLock;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    static JOB: OnceLock<usize> = OnceLock::new();

    /// The service and its collector worker end with this app, even after a crash.
    pub fn adopt(child: &std::process::Child) {
        let job = *JOB.get_or_init(|| unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return 0;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            job as usize
        });
        if job != 0 {
            unsafe {
                AssignProcessToJobObject(job as _, child.as_raw_handle() as _);
            }
        }
    }
}

#[cfg(not(target_os = "windows"))]
mod child_job {
    pub fn adopt(_child: &std::process::Child) {}
}

/// Starts the service when nothing answers on its port, and restarts it when it dies or stops answering.
pub struct BackendSupervisor {
    port: u16,
    backend: Mutex<Option<Backend>>,
    service: Mutex<Option<Child>>,
    sync_child: Mutex<Option<Child>>,
    stopping: AtomicBool,
    restarts: Mutex<Vec<Instant>>,
}

impl BackendSupervisor {
    fn new(port: u16) -> Self {
        Self {
            port,
            backend: Mutex::new(None),
            service: Mutex::new(None),
            sync_child: Mutex::new(None),
            stopping: AtomicBool::new(false),
            restarts: Mutex::new(Vec::new()),
        }
    }

    pub fn stop_all(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        for slot in [&self.service, &self.sync_child] {
            if let Ok(mut guard) = slot.lock() {
                if let Some(mut child) = guard.take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    }

    fn stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    fn backend(&self) -> Option<Backend> {
        self.backend.lock().ok().and_then(|backend| backend.clone())
    }

    fn is_bundled(&self) -> bool {
        self.backend().map(|backend| backend.is_bundled()).unwrap_or(false)
    }

    fn spawn_service(&self) {
        let Some(backend) = self.backend() else { return };
        if self.stopping() {
            return;
        }
        let port = self.port.to_string();
        if let Ok(child) = backend.command("spend_app.service", &["--port", &port]).spawn() {
            child_job::adopt(&child);
            if let Ok(mut guard) = self.service.lock() {
                if let Some(mut old) = guard.replace(child) {
                    let _ = old.kill();
                    let _ = old.wait();
                }
            }
        }
    }

    /// The older sync-only setup: BURNRATE_SYNC_HUB and BURNRATE_SYNC_TOKEN in the environment.
    fn spawn_sync(&self) {
        let (Ok(hub_url), Ok(token)) = (env::var("BURNRATE_SYNC_HUB"), env::var("BURNRATE_SYNC_TOKEN")) else {
            return;
        };
        let (hub_url, token) = (hub_url.trim().to_string(), token.trim().to_string());
        let Some(backend) = self.backend() else { return };
        if hub_url.is_empty() || token.is_empty() {
            return;
        }
        let args = ["sync", "--hub", hub_url.as_str(), "--token", token.as_str(), "--interval", "5"];
        if let Ok(child) = backend.command("spend_app", &args).spawn() {
            child_job::adopt(&child);
            if let Ok(mut guard) = self.sync_child.lock() {
                *guard = Some(child);
            }
        }
    }

    fn owns_service(&self) -> bool {
        self.service.lock().map(|guard| guard.is_some()).unwrap_or(false)
    }

    fn service_exited(&self) -> bool {
        self.service
            .lock()
            .map(|mut guard| match guard.as_mut() {
                Some(child) => !matches!(child.try_wait(), Ok(None)),
                None => false,
            })
            .unwrap_or(false)
    }

    /// At most RESTART_BUDGET restarts in RESTART_WINDOW, so a service that cannot start does not loop.
    fn may_restart(&self) -> bool {
        let Ok(mut times) = self.restarts.lock() else { return false };
        let now = Instant::now();
        times.retain(|at| now.duration_since(*at) < RESTART_WINDOW);
        if times.len() >= RESTART_BUDGET {
            return false;
        }
        times.push(now);
        true
    }
}

fn service_url(port: u16, path: &str) -> Option<tauri::Url> {
    tauri::Url::parse(&format!("http://127.0.0.1:{port}{path}")).ok()
}

fn on_service(window: &tauri::WebviewWindow, port: u16) -> bool {
    window
        .url()
        .map(|url| url.host_str() == Some("127.0.0.1") && url.port() == Some(port))
        .unwrap_or(false)
}

/// Moves windows still on the starting page to the service.
fn open_service_windows(app: &AppHandle, port: u16) {
    for (label, path) in [("hud", "/hud?glass=1"), ("main", "/?app=1")] {
        if let (Some(window), Some(url)) = (app.get_webview_window(label), service_url(port, path)) {
            if !on_service(&window, port) {
                let _ = window.navigate(url);
            }
        }
    }
}

fn show_start_failure(app: &AppHandle, port: u16) {
    let log = data_root().join("logs").join("dashboard.log");
    let quoted = serde_json::to_string(&log.to_string_lossy()).unwrap_or_else(|_| "null".into());
    for label in ["hud", "main"] {
        if let Some(window) = app.get_webview_window(label) {
            if !on_service(&window, port) {
                let _ = window.eval(&format!("window.burnrateStartFailed && window.burnrateStartFailed({quoted})"));
            }
        }
    }
}

fn supervise(app: AppHandle) {
    thread::spawn(move || {
        let supervisor = app.state::<BackendSupervisor>();
        let port = supervisor.port;
        if !port_reachable(port) {
            supervisor.spawn_service();
        }
        supervisor.spawn_sync();

        let started = Instant::now();
        let mut warned = false;
        while !port_reachable(port) {
            if supervisor.stopping() {
                return;
            }
            if !warned && started.elapsed() >= STARTUP_WARNING_AFTER {
                warned = true;
                show_start_failure(&app, port);
            }
            // A service that died while starting is tried again, within the restart budget.
            if supervisor.service_exited() && supervisor.may_restart() {
                supervisor.spawn_service();
            }
            thread::sleep(Duration::from_millis(250));
        }
        open_service_windows(&app, port);

        let mut misses = 0u32;
        loop {
            thread::sleep(WATCH_INTERVAL);
            if supervisor.stopping() {
                return;
            }
            if port_reachable(port) {
                misses = 0;
                continue;
            }
            misses += 1;
            let owned = supervisor.owns_service();
            // Ours exited, ours has not answered for a minute, or nothing serves the port at all.
            let restart = supervisor.service_exited() || (owned && misses >= 4) || (!owned && misses >= 2);
            if restart && supervisor.may_restart() {
                supervisor.spawn_service();
                misses = 0;
            }
        }
    });
}

/// An update found by the background check, and the tray item that installs it.
pub struct PendingUpdate {
    update: Mutex<Option<tauri_plugin_updater::Update>>,
    menu: Mutex<Option<MenuItem<Wry>>>,
}

fn set_update_menu_text(app: &AppHandle, text: &str) {
    if let Ok(menu) = app.state::<PendingUpdate>().menu.lock() {
        if let Some(item) = menu.as_ref() {
            let _ = item.set_text(text);
        }
    }
}

fn check_for_update(app: &AppHandle) -> Option<String> {
    let updater = app.updater().ok()?;
    let update = tauri::async_runtime::block_on(updater.check()).ok()??;
    let version = update.version.clone();
    if let Ok(mut pending) = app.state::<PendingUpdate>().update.lock() {
        *pending = Some(update);
    }
    set_update_menu_text(app, &format!("Install update {version} and restart"));
    let _ = app.emit_to("hud", "burnrate://update-available", &version);
    Some(version)
}

fn schedule_update_checks(app: AppHandle) {
    thread::spawn(move || {
        thread::sleep(UPDATE_FIRST_CHECK);
        loop {
            check_for_update(&app);
            thread::sleep(UPDATE_CHECK_EVERY);
        }
    });
}

fn updates_enabled(app: &AppHandle) -> bool {
    app.state::<BackendSupervisor>().is_bundled()
        && env::var("BURNRATE_UPDATES").map(|v| v.trim() != "0").unwrap_or(true)
}

#[tauri::command]
fn pending_update(state: State<'_, PendingUpdate>) -> Option<String> {
    state
        .update
        .lock()
        .ok()
        .and_then(|update| update.as_ref().map(|update| update.version.clone()))
}

#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    // A copy, so the update stays offered if this attempt fails.
    let update = app
        .state::<PendingUpdate>()
        .update
        .lock()
        .map_err(|e| e.to_string())?
        .clone()
        .ok_or_else(|| "No update is waiting".to_string())?;
    let version = update.version.clone();
    set_update_menu_text(&app, &format!("Installing update {version}…"));
    // On Windows the installer takes over and this process exits; elsewhere the app restarts.
    if let Err(error) = update.download_and_install(|_, _| {}, || {}).await {
        set_update_menu_text(&app, &format!("Update {version} failed, click to retry: {error}"));
        return Err(error.to_string());
    }
    app.restart();
}

/// Start at sign-in for this user only: never the machine-wide Run key, even
/// when the app runs elevated (the autostart plugin tries that first).
fn launcher(app: &AppHandle) -> Option<AutoLaunch> {
    let exe = env::current_exe().ok()?.display().to_string();
    #[cfg(target_os = "linux")]
    let exe = app
        .env()
        .appimage
        .and_then(|path| path.to_str().map(String::from))
        .unwrap_or(exe);
    AutoLaunchBuilder::new()
        .set_app_name(&app.package_info().name)
        .set_app_path(&exe)
        .set_macos_launch_mode(MacOSLaunchMode::LaunchAgent)
        .set_windows_enable_mode(WindowsEnableMode::CurrentUser)
        .build()
        .ok()
}

fn starts_at_sign_in(app: &AppHandle) -> bool {
    launcher(app).and_then(|launcher| launcher.is_enabled().ok()).unwrap_or(false)
}

fn set_start_at_sign_in(app: &AppHandle, enabled: bool) -> bool {
    launcher(app)
        .map(|launcher| if enabled { launcher.enable().is_ok() } else { launcher.disable().is_ok() })
        .unwrap_or(false)
}

/// An installed app starts at sign-in unless the person turned that off; this applies the default once.
fn apply_autostart_default(app: &AppHandle) {
    let Ok(dir) = app.path().app_config_dir() else { return };
    let marker = dir.join("autostart-default-applied");
    if marker.exists() || !set_start_at_sign_in(app, true) {
        return; // a failed attempt is tried again next launch
    }
    let _ = std::fs::create_dir_all(&dir);
    let _ = std::fs::write(&marker, b"1");
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

#[cfg(target_os = "windows")]
#[repr(C)]
struct StyleStruct {
    style_old: u32,
    style_new: u32,
}

#[cfg(target_os = "windows")]
unsafe extern "system" fn hud_subclass_wndproc(
    hwnd: isize,
    msg: u32,
    wparam: usize,
    lparam: isize,
) -> isize {
    #[link(name = "user32")]
    extern "system" {
        fn CallWindowProcW(
            lpPrevWndFunc: isize,
            hWnd: isize,
            Msg: u32,
            wParam: usize,
            lParam: isize,
        ) -> isize;
    }
    const WM_STYLECHANGING: u32 = 0x007C;
    const WM_NCCALCSIZE: u32 = 0x0083;
    const WM_NCPAINT: u32 = 0x0085;
    const WM_NCACTIVATE: u32 = 0x0086;
    const GWL_STYLE_IDX: isize = -16;
    const WS_POPUP: u32 = 0x8000_0000;
    const WS_VISIBLE: u32 = 0x1000_0000;
    const WS_CLIPSIBLINGS: u32 = 0x0400_0000;
    const WS_CLIPCHILDREN: u32 = 0x0200_0000;
    const STRIP_MASK: u32 = 0x00CF_0000 | 0x0080_0000 | 0x0040_0000;

    match msg {
        WM_NCCALCSIZE => {
            // Returning 0 makes the client area 100% of the window rect (0px non-client borders).
            return 0;
        }
        WM_NCPAINT => {
            return 0;
        }
        WM_NCACTIVATE => {
            return 1;
        }
        WM_STYLECHANGING => {
            if (wparam as isize) == GWL_STYLE_IDX && lparam != 0 {
                let ss = &mut *(lparam as *mut StyleStruct);
                let vis = ss.style_new & WS_VISIBLE;
                ss.style_new = (ss.style_new & !STRIP_MASK) | WS_POPUP | WS_CLIPSIBLINGS | WS_CLIPCHILDREN | vis;
            }
        }
        _ => {}
    }

    let orig = ORIG_HUD_WNDPROC.load(Ordering::SeqCst);
    if orig != 0 {
        CallWindowProcW(orig, hwnd, msg, wparam, lparam)
    } else {
        0
    }
}

#[cfg(target_os = "windows")]
fn enforce_win32_topmost(window: &tauri::WebviewWindow, pinned: bool) {
    if let Ok(hwnd) = window.hwnd() {
        #[link(name = "user32")]
        extern "system" {
            fn GetWindowLongW(hWnd: isize, nIndex: i32) -> i32;
            fn SetWindowLongW(hWnd: isize, nIndex: i32, dwNewLong: i32) -> i32;
            fn GetWindowLongPtrW(hWnd: isize, nIndex: i32) -> isize;
            fn SetWindowLongPtrW(hWnd: isize, nIndex: i32, dwNewLong: isize) -> isize;
            fn GetWindowRect(hWnd: isize, lpRect: *mut [i32; 4]) -> i32;
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
        const GWLP_WNDPROC: i32 = -4;
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
        const DWMWA_BORDER_COLOR: u32 = 34;
        // The window is transparent and the page draws a rounded glass card,
        // so DWM must neither clip the corners nor outline the window rect.
        const DWMWCP_DONOTROUND: i32 = 1;
        const DWMWA_COLOR_NONE: i32 = 0xFFFF_FFFEu32 as i32;
        const HWND_TOPMOST: isize = -1;
        const HWND_NOTOPMOST: isize = -2;
        const SWP_NOSIZE: u32 = 0x0001;
        const SWP_NOMOVE: u32 = 0x0002;
        const SWP_NOACTIVATE: u32 = 0x0010;
        const SWP_FRAMECHANGED: u32 = 0x0020;
        const SWP_SHOWWINDOW: u32 = 0x0040;
        unsafe {
            let h = hwnd.0 as isize;
            let mut needs_frame_refresh = false;
            let subclass_fn = hud_subclass_wndproc as *const () as usize as isize;
            let cur_proc = GetWindowLongPtrW(h, GWLP_WNDPROC);
            if cur_proc != 0 && cur_proc != subclass_fn {
                ORIG_HUD_WNDPROC.store(cur_proc, Ordering::SeqCst);
                SetWindowLongPtrW(h, GWLP_WNDPROC, subclass_fn);
                needs_frame_refresh = true;
            }

            let target_style = (WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN) as i32;
            let cur_style = GetWindowLongW(h, GWL_STYLE);
            if cur_style != target_style {
                SetWindowLongW(h, GWL_STYLE, target_style);
                needs_frame_refresh = true;
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
                needs_frame_refresh = true;
            }
            let corner_pref: i32 = DWMWCP_DONOTROUND;
            DwmSetWindowAttribute(
                h,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                &corner_pref as *const i32,
                std::mem::size_of::<i32>() as u32,
            );
            let border_color: i32 = DWMWA_COLOR_NONE;
            DwmSetWindowAttribute(
                h,
                DWMWA_BORDER_COLOR,
                &border_color as *const i32,
                std::mem::size_of::<i32>() as u32,
            );

            if needs_frame_refresh {
                let mut r = [0i32; 4];
                if GetWindowRect(h, &mut r as *mut [i32; 4]) != 0 {
                    let w = (r[2] - r[0]).max(240);
                    let ht = (r[3] - r[1]).max(140);
                    SetWindowPos(
                        h,
                        if pinned { HWND_TOPMOST } else { HWND_NOTOPMOST },
                        r[0],
                        r[1],
                        w + 1,
                        ht,
                        SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                    );
                    SetWindowPos(
                        h,
                        if pinned { HWND_TOPMOST } else { HWND_NOTOPMOST },
                        r[0],
                        r[1],
                        w,
                        ht,
                        SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                    );
                    return;
                }
            }

            SetWindowPos(
                h,
                if pinned { HWND_TOPMOST } else { HWND_NOTOPMOST },
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
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
        const SWP_FRAMECHANGED: u32 = 0x0020;
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
                SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            );
        }
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn resize_hud_window(window: &tauri::WebviewWindow, width: f64, height: f64, pinned: bool) -> Result<(), String> {
    use tauri::{LogicalSize, Size};
    let _ = window.set_min_size(Some(Size::Logical(LogicalSize::new(260.0, 160.0))));
    window
        .set_size(Size::Logical(LogicalSize::new(width, height)))
        .map_err(|e| e.to_string())?;
    let _ = apply_hud_always_on_top(window, pinned);
    Ok(())
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
            if HUD_DRAGGING.swap(true, Ordering::SeqCst) {
                return Ok(());
            }
            let h = hwnd.0 as isize;
            thread::spawn(move || {
                #[link(name = "user32")]
                extern "system" {
                    fn GetCursorPos(lpPoint: *mut [i32; 2]) -> i32;
                    fn GetWindowRect(hWnd: isize, lpRect: *mut [i32; 4]) -> i32;
                    fn GetAsyncKeyState(vKey: i32) -> i16;
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
                const VK_LBUTTON: i32 = 0x01;
                const SWP_NOSIZE: u32 = 0x0001;
                const SWP_NOZORDER: u32 = 0x0004;
                const SWP_NOACTIVATE: u32 = 0x0010;
                unsafe {
                    let mut start_pt = [0i32; 2];
                    let mut start_rect = [0i32; 4];
                    if GetCursorPos(&mut start_pt as *mut [i32; 2]) != 0
                        && GetWindowRect(h, &mut start_rect as *mut [i32; 4]) != 0
                    {
                        let mut last_x = start_rect[0];
                        let mut last_y = start_rect[1];
                        while (GetAsyncKeyState(VK_LBUTTON) as u16 & 0x8000) != 0 {
                            let mut cur_pt = [0i32; 2];
                            if GetCursorPos(&mut cur_pt as *mut [i32; 2]) != 0 {
                                let nx = start_rect[0] + (cur_pt[0] - start_pt[0]);
                                let ny = start_rect[1] + (cur_pt[1] - start_pt[1]);
                                if nx != last_x || ny != last_y {
                                    last_x = nx;
                                    last_y = ny;
                                    SetWindowPos(
                                        h,
                                        0,
                                        nx,
                                        ny,
                                        0,
                                        0,
                                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                                    );
                                }
                            }
                            thread::sleep(Duration::from_millis(4));
                        }
                    }
                }
                HUD_DRAGGING.store(false, Ordering::SeqCst);
            });
            return Ok(());
        }
        #[cfg(not(target_os = "windows"))]
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
        let port = app.state::<BackendSupervisor>().port;
        if let Some(url) = service_url(port, "/setup") {
            let _ = window.navigate(url);
        }
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

#[cfg(target_os = "windows")]
fn set_windows_app_id() {
    #[link(name = "shell32")]
    extern "system" {
        fn SetCurrentProcessExplicitAppUserModelID(app_id: *const u16) -> i32;
    }
    let app_id: Vec<u16> = "dev.burnrate.desktop\0".encode_utf16().collect();
    unsafe {
        let _ = SetCurrentProcessExplicitAppUserModelID(app_id.as_ptr());
    }
}

#[cfg(not(target_os = "windows"))]
fn set_windows_app_id() {}

#[cfg(target_os = "windows")]
fn apply_win32_window_icons(window: &tauri::WebviewWindow) {
    if let Ok(hwnd) = window.hwnd() {
        #[link(name = "user32")]
        extern "system" {
            fn SendMessageW(hWnd: isize, Msg: u32, wParam: usize, lParam: isize) -> isize;
            fn SetClassLongPtrW(hWnd: isize, nIndex: i32, dwNewLong: isize) -> isize;
        }
        #[link(name = "shell32")]
        extern "system" {
            fn ExtractIconW(hInst: isize, lpszExeFileName: *const u16, nIconIndex: u32) -> isize;
        }
        const WM_GETICON: u32 = 0x007F;
        const WM_SETICON: u32 = 0x0080;
        const ICON_SMALL: usize = 0;
        const ICON_BIG: usize = 1;
        const GCL_HICON: i32 = -14;
        unsafe {
            let h = hwnd.0 as isize;
            let mut hicon = 0isize;
            if let Ok(exe) = env::current_exe() {
                let mut wide: Vec<u16> = exe.to_string_lossy().encode_utf16().collect();
                wide.push(0);
                hicon = ExtractIconW(0, wide.as_ptr(), 0);
                if hicon <= 1 {
                    hicon = 0;
                }
            }
            if hicon == 0 {
                hicon = SendMessageW(h, WM_GETICON, ICON_SMALL, 0);
            }
            if hicon != 0 {
                SendMessageW(h, WM_SETICON, ICON_BIG, hicon);
                SendMessageW(h, WM_SETICON, ICON_SMALL, hicon);
                SetClassLongPtrW(h, GCL_HICON, hicon);
            }
        }
    }
}

#[cfg(not(target_os = "windows"))]
fn apply_win32_window_icons(_window: &tauri::WebviewWindow) {}

pub fn run() {
    set_windows_app_id();
    release_webview_lock_if_needed();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(HudPinState {
            pinned: AtomicBool::new(true),
        })
        .manage(BackendSupervisor::new(configured_port()))
        .manage(PendingUpdate {
            update: Mutex::new(None),
            menu: Mutex::new(None),
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
            open_setup_wizard,
            pending_update,
            install_update
        ])
        .setup(|app| {
            let default_icon = app.default_window_icon().cloned();
            if let Some(main_win) = app.get_webview_window("main") {
                if let Some(ref ic) = default_icon {
                    let _ = main_win.set_icon(ic.clone());
                }
                apply_win32_window_icons(&main_win);
            }
            if let Some(hud_win) = app.get_webview_window("hud") {
                if let Some(ref ic) = default_icon {
                    let _ = hud_win.set_icon(ic.clone());
                }
                let _ = apply_hud_always_on_top(&hud_win, true);
                apply_win32_window_icons(&hud_win);
            }

            let backend = resolve_backend(app.path().resource_dir().ok());
            let installed = backend.is_bundled();
            if let Ok(mut slot) = app.state::<BackendSupervisor>().backend.lock() {
                *slot = Some(backend);
            }
            supervise(app.handle().clone());

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
            let setup_wizard_item = MenuItem::with_id(
                app,
                "setup_wizard",
                "Run Setup Wizard…",
                true,
                None::<&str>,
            )?;
            let sep = PredefinedMenuItem::separator(app)?;
            let sep_installed = PredefinedMenuItem::separator(app)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit BURNRATE", true, None::<&str>)?;

            // Start-at-sign-in and updates belong to an installed app, not a checkout.
            let autostart_item = if installed {
                apply_autostart_default(app.handle());
                let enabled = starts_at_sign_in(app.handle());
                Some(CheckMenuItem::with_id(app, "autostart", "Start BURNRATE at sign-in", true, enabled, None::<&str>)?)
            } else {
                None
            };
            let update_item = if updates_enabled(app.handle()) {
                let item = MenuItem::with_id(app, "update", "Check for updates", true, None::<&str>)?;
                if let Ok(mut slot) = app.state::<PendingUpdate>().menu.lock() {
                    *slot = Some(item.clone());
                }
                schedule_update_checks(app.handle().clone());
                Some(item)
            } else {
                None
            };

            let menu = Menu::with_items(app, &[&toggle_hud_item, &pin_hud_item, &setup_wizard_item])?;
            if autostart_item.is_some() || update_item.is_some() {
                menu.append(&sep_installed)?;
            }
            if let Some(item) = autostart_item.as_ref() {
                menu.append(item)?;
            }
            if let Some(item) = update_item.as_ref() {
                menu.append(item)?;
            }
            menu.append(&sep)?;
            menu.append(&quit_item)?;

            let autostart_toggle = autostart_item.clone();
            let mut tray_builder = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("BURNRATE · Live Multi-Computer Spend & Sidecar HUD")
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "toggle_hud" => {
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud(app.clone(), state);
                    }
                    "pin_hud" => {
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud_pin(app.clone(), state);
                    }
                    "setup_wizard" => {
                        let _ = open_setup_wizard(app.clone());
                    }
                    "autostart" => {
                        let enable = !starts_at_sign_in(app);
                        set_start_at_sign_in(app, enable);
                        if let Some(item) = autostart_toggle.as_ref() {
                            let _ = item.set_checked(starts_at_sign_in(app));
                        }
                    }
                    "update" => {
                        let app = app.clone();
                        let waiting = app
                            .state::<PendingUpdate>()
                            .update
                            .lock()
                            .map(|update| update.is_some())
                            .unwrap_or(false);
                        if waiting {
                            // A failure shows on this menu item, which then retries.
                            tauri::async_runtime::spawn(async move {
                                let _ = install_update(app).await;
                            });
                        } else {
                            thread::spawn(move || {
                                set_update_menu_text(&app, "Checking for updates…");
                                if check_for_update(&app).is_none() {
                                    set_update_menu_text(&app, "BURNRATE is up to date");
                                }
                            });
                        }
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
