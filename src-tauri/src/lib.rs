use std::env;
use std::io::Read;
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
const HEALTH_TIMEOUT: Duration = Duration::from_secs(2);
const HEALTH_BODY_LIMIT: u64 = 16 * 1024;
const RESTART_BUDGET: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(600);
const UPDATE_FIRST_CHECK: Duration = Duration::from_secs(45);
const UPDATE_CHECK_EVERY: Duration = Duration::from_secs(6 * 60 * 60);

pub struct HudPinState {
    pub pinned: AtomicBool,
    pub show_in_taskbar: AtomicBool,
}

#[derive(Default, serde::Serialize, serde::Deserialize, Clone, Copy)]
struct HudWindowPrefs {
    x: Option<i32>,
    y: Option<i32>,
    #[serde(default)]
    taskbar: bool,
}

fn hud_prefs_path(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|dir| dir.join("hud-window.json"))
}

fn load_hud_prefs(app: &AppHandle) -> HudWindowPrefs {
    let mut prefs = hud_prefs_path(app)
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<HudWindowPrefs>(&s).ok())
        .unwrap_or_default();
    if let Ok(val) = env::var("BURNRATE_HUD_TASKBAR") {
        let v = val.trim().to_ascii_lowercase();
        prefs.taskbar = matches!(v.as_str(), "1" | "true" | "yes" | "on");
    }
    prefs
}

fn update_hud_prefs(app: &AppHandle, mutate: impl FnOnce(&mut HudWindowPrefs)) {
    let Some(path) = hud_prefs_path(app) else { return };
    let mut prefs = load_hud_prefs(app);
    mutate(&mut prefs);
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(json) = serde_json::to_string(&prefs) {
        let _ = std::fs::write(path, json);
    }
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

#[derive(Debug, PartialEq)]
struct ServiceHealth {
    pid: u32,
    collection_stalled: bool,
}

#[derive(serde::Deserialize)]
struct HealthPayload {
    status: String,
    application: String,
    pid: u32,
    #[serde(rename = "collectorEnabled")]
    collector_enabled: bool,
    ingest: Option<IngestHealth>,
}

#[derive(serde::Deserialize)]
struct IngestHealth {
    // The backend applies its 30-minute first-pass grace and 10-minute
    // completed-pass deadline; a quiet optional provider is not a stall.
    stalled: bool,
}

fn health_client(timeout: Duration) -> Result<reqwest::blocking::Client, reqwest::Error> {
    if rustls::crypto::CryptoProvider::get_default().is_none() {
        let _ = rustls::crypto::ring::default_provider().install_default();
    }
    reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(timeout)
        .timeout(timeout)
        .build()
}

fn service_health(client: &reqwest::blocking::Client, port: u16) -> Option<ServiceHealth> {
    let response = client.get(format!("http://127.0.0.1:{port}/healthz?supervisor=1"))
        .send().ok()?;
    if response.status() != reqwest::StatusCode::OK {
        return None;
    }
    // A malformed or unrelated listener must not allocate an unbounded body.
    let mut body = Vec::new();
    response.take(HEALTH_BODY_LIMIT + 1).read_to_end(&mut body).ok()?;
    if body.len() as u64 > HEALTH_BODY_LIMIT {
        return None;
    }
    let payload: HealthPayload = serde_json::from_slice(&body).ok()?;
    if payload.status != "ok" || payload.application != "burnrate" || payload.pid == 0 {
        return None;
    }
    let collection_stalled = if payload.collector_enabled {
        payload.ingest?.stalled
    } else {
        false
    };
    Some(ServiceHealth { pid: payload.pid, collection_stalled })
}

#[derive(Clone, Copy)]
struct OwnedService {
    pid: u32,
    exited: bool,
    uptime: Duration,
}

fn restart_due(owned: Option<OwnedService>, listener: bool, health: Option<&ServiceHealth>, misses: u32) -> bool {
    match owned {
        Some(service) => {
            if let Some(health) = health {
                // Another BURNRATE process may have taken the port after our
                // child exited. Its heartbeat never authorizes killing it.
                return health.pid == service.pid && health.collection_stalled
                    && service.uptime >= STARTUP_WARNING_AFTER && misses >= 4;
            }
            if service.exited {
                return !listener;
            }
            service.uptime >= STARTUP_WARNING_AFTER && misses >= 4
        }
        // TCP is used only to avoid starting a competing service, never as a
        // health signal. An unrelated/unowned listening process is untouched.
        None => !listener && misses >= 2,
    }
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
mod process_tree {
    use std::io;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use std::os::windows::process::CommandExt;
    use std::process::{Child, Command};
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, THREADENTRY32, TH32CS_SNAPTHREAD,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenThread, ResumeThread, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
    };

    // Each service/sync generation has its own non-inherited handle. Closing
    // it kills only that generation, including grandchildren after parent exit.
    pub struct Tree(OwnedHandle);

    impl Tree {
        fn new() -> io::Result<Self> {
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if job.is_null() { return Err(io::Error::last_os_error()); }
                let handle = OwnedHandle::from_raw_handle(job);
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    job, JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    std::mem::size_of_val(&info) as u32,
                ) == 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(Self(handle))
            }
        }

        pub fn spawn(command: &mut Command) -> io::Result<(Child, Self)> {
            let tree = Self::new()?;
            // Assign before any application code runs: otherwise a fast child
            // can create an untracked collector before job assignment.
            command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
            let mut child = command.spawn()?;
            let result = unsafe {
                if AssignProcessToJobObject(tree.0.as_raw_handle(), child.as_raw_handle()) == 0 {
                    Err(io::Error::last_os_error())
                } else {
                    resume_primary_thread(child.id())
                }
            };
            if let Err(error) = result {
                drop(tree);
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
            Ok((child, tree))
        }

        pub fn terminate(self, child: &mut Child) {
            unsafe { TerminateJobObject(self.0.as_raw_handle(), 1); }
            // Drop the kill-on-close handle before waiting, including when
            // explicit termination failed. The Child handle is our fallback.
            drop(self);
            let _ = child.kill();
        }
    }

    unsafe fn resume_primary_thread(pid: u32) -> io::Result<()> {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snapshot == INVALID_HANDLE_VALUE { return Err(io::Error::last_os_error()); }
        let snapshot = OwnedHandle::from_raw_handle(snapshot);
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of_val(&entry) as u32;
        let mut found = Thread32First(snapshot.as_raw_handle(), &mut entry);
        while found != 0 {
            if entry.th32OwnerProcessID == pid {
                let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                if thread.is_null() { return Err(io::Error::last_os_error()); }
                let thread = OwnedHandle::from_raw_handle(thread);
                if ResumeThread(thread.as_raw_handle()) == u32::MAX {
                    return Err(io::Error::last_os_error());
                }
                return Ok(());
            }
            found = Thread32Next(snapshot.as_raw_handle(), &mut entry);
        }
        Err(io::Error::new(io::ErrorKind::NotFound, "owned child thread unavailable"))
    }

    pub fn exited(child: &mut Child) -> bool {
        !matches!(child.try_wait(), Ok(None))
    }
}

#[cfg(unix)]
mod process_tree {
    use std::io;
    use std::os::unix::process::CommandExt;
    use std::process::{Child, Command};

    pub struct Tree(libc::pid_t);

    // Inspect without reaping so the group leader's PID cannot be reused by
    // an unrelated process before its generation is terminated.
    fn exit_state(pid: u32) -> io::Result<bool> {
        unsafe {
            let mut info: libc::siginfo_t = std::mem::zeroed();
            if libc::waitid(libc::P_PID, pid, &mut info,
                            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT) != 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(info.si_pid() != 0)
        }
    }

    impl Tree {
        pub fn spawn(command: &mut Command) -> io::Result<(Child, Self)> {
            // The child becomes group leader before exec; descendants inherit
            // the group without sharing it with the app or the sync worker.
            command.process_group(0);
            let child = command.spawn()?;
            let tree = Self(child.id() as libc::pid_t);
            Ok((child, tree))
        }

        pub fn terminate(self, child: &mut Child) {
            if exit_state(child.id()).is_ok() {
                unsafe { libc::kill(-self.0, libc::SIGKILL); }
            }
        }
    }

    pub fn exited(child: &mut Child) -> bool {
        exit_state(child.id()).unwrap_or(true)
    }
}

struct ManagedChild {
    child: Child,
    tree: Option<process_tree::Tree>,
    started: Instant,
}

impl ManagedChild {
    fn spawn(command: &mut Command) -> std::io::Result<Self> {
        let (child, tree) = process_tree::Tree::spawn(command)?;
        Ok(Self { child, tree:Some(tree), started:Instant::now() })
    }

    fn stop(&mut self) {
        if let Some(tree) = self.tree.take() {
            tree.terminate(&mut self.child);
            let _ = self.child.wait();
        }
    }

    fn exited(&mut self) -> bool {
        process_tree::exited(&mut self.child)
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) { self.stop(); }
}

/// Starts the service when nothing answers on its port, and restarts it when it dies or stops answering.
pub struct BackendSupervisor {
    port: u16,
    backend: Mutex<Option<Backend>>,
    service: Mutex<Option<ManagedChild>>,
    sync_child: Mutex<Option<ManagedChild>>,
    stopping: AtomicBool,
    restarts: Mutex<Vec<Instant>>,
    pending_main_path: Mutex<String>,
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
            pending_main_path: Mutex::new("/".to_string()),
        }
    }

    pub fn stop_all(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        for slot in [&self.service, &self.sync_child] {
            if let Ok(mut guard) = slot.lock() {
                if let Some(mut child) = guard.take() {
                    child.stop();
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
        let Ok(mut guard) = self.service.lock() else { return };
        // Release our old listener before launching its replacement. Starting
        // first can make the new service fail its own port/mutex check.
        if let Some(mut old) = guard.take() {
            old.stop();
        }
        if self.stopping() {
            return;
        }
        let port = self.port.to_string();
        if let Ok(child) = ManagedChild::spawn(&mut backend.command("spend_app.service", &["--port", &port])) {
            *guard = Some(child);
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
        let Ok(mut guard) = self.sync_child.lock() else { return };
        if self.stopping() {
            return;
        }
        if let Some(mut old) = guard.take() {
            old.stop();
        }
        if self.stopping() {
            return;
        }
        let args = ["sync", "--hub", hub_url.as_str(), "--interval", "5"];
        let mut command = backend.command("spend_app", &args);
        command.env("BURNRATE_SYNC_TOKEN", &token);
        if let Ok(child) = ManagedChild::spawn(&mut command) {
            *guard = Some(child);
        }
    }

    fn owned_service(&self) -> Option<OwnedService> {
        let mut guard = self.service.lock().ok()?;
        let child = guard.as_mut()?;
        Some(OwnedService {
            pid: child.child.id(),
            exited: child.exited(),
            uptime: child.started.elapsed(),
        })
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
    let main_path = app
        .try_state::<BackendSupervisor>()
        .and_then(|s| s.pending_main_path.lock().ok().map(|g| g.clone()))
        .unwrap_or_else(|| "/".to_string());
    for (label, path) in [("hud", "/hud?glass=1"), ("main", main_path.as_str())] {
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
        let Ok(client) = health_client(HEALTH_TIMEOUT) else {
            show_start_failure(&app, port);
            return;
        };
        let started = Instant::now();
        let mut warned = false;
        let mut opened = false;
        let mut misses = 0u32;
        loop {
            if supervisor.stopping() {
                return;
            }
            let health = service_health(&client, port);
            let owned = supervisor.owned_service();
            if health.is_some() && !opened {
                open_service_windows(&app, port);
                opened = true;
            }
            if !opened && !warned && started.elapsed() >= STARTUP_WARNING_AFTER {
                warned = true;
                show_start_failure(&app, port);
            }
            if health.as_ref().is_some_and(|h| !h.collection_stalled
                || owned.is_none_or(|service| service.pid != h.pid)) {
                misses = 0;
            } else {
                misses = misses.saturating_add(1);
            }
            let listener = health.is_some() || port_reachable(port);
            if restart_due(owned, listener, health.as_ref(), misses) && supervisor.may_restart() {
                supervisor.spawn_service();
                misses = 0;
            }
            thread::sleep(if opened { WATCH_INTERVAL } else { Duration::from_millis(250) });
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
    set_update_menu_text(&app, &format!("Installing update {version}\u{2026}"));
    // On Windows the installer takes over and this process exits; elsewhere the app restarts.
    if let Err(error) = update.download_and_install(|_, _| {}, || {}).await {
        set_update_menu_text(&app, &format!("Update {version} failed, click to retry: {error}"));
        return Err(error.to_string());
    }
    app.restart();
}

fn is_autostart_launch() -> bool {
    env::args_os().any(|arg| arg == "--autostart")
}

/// Start at sign-in for this user only: never the machine-wide Run key, even
/// when the app runs elevated (the autostart plugin tries that first).
fn launcher_with_args(app: &AppHandle, args: &[&str]) -> Option<AutoLaunch> {
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
        .set_args(args)
        .set_macos_launch_mode(MacOSLaunchMode::LaunchAgent)
        .set_windows_enable_mode(WindowsEnableMode::CurrentUser)
        .build()
        .ok()
}

fn launcher(app: &AppHandle) -> Option<AutoLaunch> {
    launcher_with_args(app, &["--autostart"])
}

fn starts_at_sign_in(app: &AppHandle) -> bool {
    if launcher(app).and_then(|launcher| launcher.is_enabled().ok()).unwrap_or(false) {
        return true;
    }
    if launcher_with_args(app, &[])
        .and_then(|launcher| launcher.is_enabled().ok())
        .unwrap_or(false)
    {
        let _ = set_start_at_sign_in(app, true);
        return true;
    }
    false
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
    let show_in_taskbar = window
        .app_handle()
        .try_state::<HudPinState>()
        .map(|s| s.show_in_taskbar.load(Ordering::SeqCst))
        .unwrap_or(false);
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
        const WS_EX_TOOLWINDOW: u32 = 0x0000_0080;
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
            let mut new_ex = old_ex
                & !(WS_EX_DLGMODALFRAME
                    | WS_EX_WINDOWEDGE
                    | WS_EX_CLIENTEDGE
                    | WS_EX_STATICEDGE
                    | WS_EX_NOREDIRECTIONBITMAP
                    | WS_EX_APPWINDOW
                    | WS_EX_TOOLWINDOW);
            if show_in_taskbar {
                new_ex |= WS_EX_APPWINDOW;
            } else {
                new_ex |= WS_EX_TOOLWINDOW;
            }
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

fn max_hud_logical_height(window: &tauri::WebviewWindow) -> f64 {
    if let Ok(Some(monitor)) = window.current_monitor() {
        let scale = monitor.scale_factor().max(1.0);
        let work_h = monitor.work_area().size.height as f64 / scale;
        if work_h >= 360.0 {
            return (work_h * 0.85).clamp(320.0, 920.0);
        }
    }
    820.0
}

#[cfg(target_os = "windows")]
fn resize_hud_window(window: &tauri::WebviewWindow, width: f64, height: f64, pinned: bool) -> Result<(), String> {
    enforce_win32_topmost(window, pinned);
    let clamped_height = height.min(max_hud_logical_height(window));
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
            let ph = ((clamped_height * scale).round() as i32).max(140);
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
    let clamped_height = height.min(max_hud_logical_height(window));
    let _ = window.set_min_size(Some(Size::Logical(LogicalSize::new(260.0, 160.0))));
    window
        .set_size(Size::Logical(LogicalSize::new(width, clamped_height)))
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
        let _ = window.unminimize();
        ensure_hud_on_screen(&window);
        window.show().map_err(|e| e.to_string())?;
        let pinned = state.pinned.load(Ordering::SeqCst);
        let _ = apply_hud_always_on_top(&window, pinned);
        let _ = window.set_focus();
        force_win32_foreground(&window);
        Ok(true)
    }
}

#[tauri::command]
fn show_hud(app: AppHandle, state: State<'_, HudPinState>) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("hud") {
        let _ = window.unminimize();
        ensure_hud_on_screen(&window);
        window.show().map_err(|e| e.to_string())?;
        let pinned = state.pinned.load(Ordering::SeqCst);
        let _ = apply_hud_always_on_top(&window, pinned);
        let _ = window.set_focus();
        force_win32_foreground(&window);
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
            let app_for_save = app.clone();
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
                let mut saved_pos: Option<(i32, i32)> = None;
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
                        if last_x != start_rect[0] || last_y != start_rect[1] {
                            saved_pos = Some((last_x, last_y));
                        }
                    }
                }
                HUD_DRAGGING.store(false, Ordering::SeqCst);
                if let Some((x, y)) = saved_pos.filter(|&(x, y)| x > -30000 && y > -30000) {
                    update_hud_prefs(&app_for_save, |p| {
                        p.x = Some(x);
                        p.y = Some(y);
                    });
                }
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

fn sanitize_main_path(raw: Option<&str>) -> String {
    let trimmed = raw.map(str::trim).unwrap_or("");
    if trimmed.is_empty() {
        return "/".to_string();
    }
    if trimmed.starts_with('/') && !trimmed.starts_with("//") {
        return trimmed.to_string();
    }
    if let Ok(parsed) = tauri::Url::parse(trimmed) {
        let mut out = parsed.path().to_string();
        if let Some(q) = parsed.query() {
            out.push('?');
            out.push_str(q);
        }
        if let Some(f) = parsed.fragment() {
            out.push('#');
            out.push_str(f);
        }
        if out.starts_with('/') && !out.starts_with("//") {
            return out;
        }
    }
    "/".to_string()
}

fn ensure_main_window(app: &AppHandle, raw_path: Option<&str>) -> Result<(), String> {
    let clean_path = sanitize_main_path(raw_path);
    let supervisor = app.state::<BackendSupervisor>();
    let port = supervisor.port;
    if let Ok(mut pending) = supervisor.pending_main_path.lock() {
        *pending = clean_path.clone();
    }

    if let Some(window) = app.get_webview_window("main") {
        if port_reachable(port) {
            if let Some(url) = service_url(port, &clean_path) {
                let needs_nav = window
                    .url()
                    .map(|cur| cur.as_str() != url.as_str())
                    .unwrap_or(true);
                if needs_nav {
                    let _ = window.navigate(url);
                }
            }
        }
        let _ = window.unminimize();
        window.show().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        force_win32_foreground(&window);
        return Ok(());
    }

    let initial_url = if port_reachable(port) {
        service_url(port, &clean_path)
            .map(tauri::WebviewUrl::External)
            .unwrap_or_else(|| tauri::WebviewUrl::App("starting.html".into()))
    } else {
        tauri::WebviewUrl::App("starting.html".into())
    };

    let window = tauri::WebviewWindowBuilder::new(app, "main", initial_url)
        .title("BURNRATE \u{00b7} AI Cost Intelligence")
        .inner_size(1340.0, 860.0)
        .min_inner_size(420.0, 560.0)
        .resizable(true)
        .center()
        .visible(true)
        .focused(true)
        .build()
        .map_err(|e| e.to_string())?;

    if let Some(ic) = app.default_window_icon().cloned() {
        let _ = window.set_icon(ic);
    }
    apply_win32_window_icons(&window);
    let _ = window.show();
    let _ = window.set_focus();
    force_win32_foreground(&window);
    Ok(())
}

#[tauri::command]
fn show_dashboard(app: AppHandle, path: Option<String>) -> Result<(), String> {
    ensure_main_window(&app, path.as_deref())
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
        window.destroy().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn open_setup_wizard(app: AppHandle) -> Result<(), String> {
    ensure_main_window(&app, Some("/setup"))
}

fn restore_hud_position(window: &tauri::WebviewWindow, x: i32, y: i32) {
    let monitors = window.available_monitors().unwrap_or_default();
    let on_any_monitor = monitors.iter().any(|m| {
        let area = m.work_area();
        let wx = area.position.x;
        let wy = area.position.y;
        let ww = area.size.width as i32;
        let wh = area.size.height as i32;
        x + 80 >= wx && x <= wx + (ww - 80).max(0) && y + 32 >= wy && y <= wy + (wh - 60).max(0)
    });
    let (target_x, target_y) = if on_any_monitor {
        (x, y)
    } else if let Ok(Some(primary)) = window.primary_monitor().or_else(|_| window.current_monitor()) {
        let area = primary.work_area();
        let wx = area.position.x;
        let wy = area.position.y;
        let ww = area.size.width as i32;
        let wh = area.size.height as i32;
        (
            x.clamp(wx, (wx + ww - 280).max(wx)),
            y.clamp(wy, (wy + wh - 180).max(wy)),
        )
    } else {
        (x, y)
    };
    let _ = window.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(
        target_x, target_y,
    )));
}

fn ensure_hud_on_screen(window: &tauri::WebviewWindow) {
    let prefs = load_hud_prefs(window.app_handle());
    let fallback = window
        .outer_position()
        .ok()
        .map(|p| (p.x, p.y))
        .filter(|&(x, y)| x > -30000 && y > -30000)
        .unwrap_or((24, 72));
    let x = prefs.x.filter(|&v| v > -30000).unwrap_or(fallback.0);
    let y = prefs.y.filter(|&v| v > -30000).unwrap_or(fallback.1);
    restore_hud_position(window, x, y);
}

#[cfg(target_os = "windows")]
fn force_win32_foreground(window: &tauri::WebviewWindow) {
    if let Ok(hwnd) = window.hwnd() {
        #[link(name = "user32")]
        extern "system" {
            fn IsIconic(hWnd: isize) -> i32;
            fn ShowWindow(hWnd: isize, nCmdShow: i32) -> i32;
            fn BringWindowToTop(hWnd: isize) -> i32;
            fn SetForegroundWindow(hWnd: isize) -> i32;
        }
        const SW_SHOW: i32 = 5;
        const SW_RESTORE: i32 = 9;
        unsafe {
            let h = hwnd.0 as isize;
            if IsIconic(h) != 0 {
                let _ = ShowWindow(h, SW_RESTORE);
            } else {
                let _ = ShowWindow(h, SW_SHOW);
            }
            let _ = BringWindowToTop(h);
            let _ = SetForegroundWindow(h);
        }
    }
}

#[cfg(not(target_os = "windows"))]
fn force_win32_foreground(_window: &tauri::WebviewWindow) {}

fn activate_from_shortcut(app: &AppHandle) {
    let pinned = app
        .try_state::<HudPinState>()
        .map(|s| s.pinned.load(Ordering::SeqCst))
        .unwrap_or(true);
    let mut hud_was_visible = false;
    if let Some(hud_win) = app.get_webview_window("hud") {
        hud_was_visible = hud_win.is_visible().unwrap_or(false)
            && !hud_win.is_minimized().unwrap_or(false);
        let _ = hud_win.unminimize();
        ensure_hud_on_screen(&hud_win);
        let _ = hud_win.show();
        let _ = apply_hud_always_on_top(&hud_win, pinned);
        let _ = hud_win.set_focus();
        force_win32_foreground(&hud_win);
    }
    if hud_was_visible || app.get_webview_window("hud").is_none() {
        let _ = ensure_main_window(app, Some("/"));
    }
}

fn sanitize_desktop_tag(raw: &str) -> String {
    let cleaned: String = raw
        .trim()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect();
    if cleaned.is_empty() {
        "Default".to_string()
    } else {
        cleaned
    }
}

#[cfg(target_os = "windows")]
fn current_desktop_tag() -> String {
    #[link(name = "user32")]
    extern "system" {
        fn GetThreadDesktop(dwThreadId: u32) -> isize;
        fn GetUserObjectInformationW(
            hObj: isize,
            nIndex: i32,
            pvInfo: *mut u16,
            nLength: u32,
            lpnLengthNeeded: *mut u32,
        ) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThreadId() -> u32;
    }
    const UOI_NAME: i32 = 2;
    unsafe {
        let hdesk = GetThreadDesktop(GetCurrentThreadId());
        if hdesk == 0 {
            return "Default".to_string();
        }
        let mut buf = [0u16; 128];
        let mut needed = 0u32;
        if GetUserObjectInformationW(
            hdesk,
            UOI_NAME,
            buf.as_mut_ptr(),
            (buf.len() * 2) as u32,
            &mut needed,
        ) == 0
        {
            return "Default".to_string();
        }
        let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
        sanitize_desktop_tag(&String::from_utf16_lossy(&buf[..len]))
    }
}

#[cfg(target_os = "windows")]
static INSTANCE_EXITING: AtomicBool = AtomicBool::new(false);

#[cfg(target_os = "windows")]
struct SharedWinHandle(isize);

#[cfg(target_os = "windows")]
unsafe impl Send for SharedWinHandle {}
#[cfg(target_os = "windows")]
unsafe impl Sync for SharedWinHandle {}

#[cfg(target_os = "windows")]
impl Drop for SharedWinHandle {
    fn drop(&mut self) {
        #[link(name = "kernel32")]
        extern "system" {
            fn CloseHandle(hObject: isize) -> i32;
        }
        if self.0 != 0 {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
}

#[cfg(target_os = "windows")]
struct SingleInstanceGuard {
    mutex: isize,
    activate_event: Option<std::sync::Arc<SharedWinHandle>>,
}

#[cfg(target_os = "windows")]
impl Drop for SingleInstanceGuard {
    fn drop(&mut self) {
        #[link(name = "kernel32")]
        extern "system" {
            fn ReleaseMutex(hMutex: isize) -> i32;
            fn CloseHandle(hObject: isize) -> i32;
            fn SetEvent(hEvent: isize) -> i32;
        }
        INSTANCE_EXITING.store(true, Ordering::SeqCst);
        if let Some(ev) = self.activate_event.as_ref() {
            if ev.0 != 0 {
                unsafe {
                    let _ = SetEvent(ev.0);
                }
            }
        }
        if self.mutex != 0 {
            unsafe {
                let _ = ReleaseMutex(self.mutex);
                let _ = CloseHandle(self.mutex);
            }
        }
    }
}

#[cfg(target_os = "windows")]
unsafe extern "system" fn enum_focus_burnrate_proc(hwnd: isize, _lparam: isize) -> i32 {
    #[link(name = "user32")]
    extern "system" {
        fn GetWindowTextW(hWnd: isize, lpString: *mut u16, nMaxCount: i32) -> i32;
        fn IsIconic(hWnd: isize) -> i32;
        fn ShowWindow(hWnd: isize, nCmdShow: i32) -> i32;
        fn BringWindowToTop(hWnd: isize) -> i32;
        fn SetForegroundWindow(hWnd: isize) -> i32;
    }
    const SW_SHOW: i32 = 5;
    const SW_RESTORE: i32 = 9;
    let mut buf = [0u16; 160];
    let n = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32);
    if n > 0 {
        let title = String::from_utf16_lossy(&buf[..n as usize]);
        if title == "BURNRATE" || title.starts_with("BURNRATE \u{00b7}") {
            let cmd = if IsIconic(hwnd) != 0 { SW_RESTORE } else { SW_SHOW };
            let _ = ShowWindow(hwnd, cmd);
            let _ = BringWindowToTop(hwnd);
            let _ = SetForegroundWindow(hwnd);
        }
    }
    1
}

#[cfg(target_os = "windows")]
fn signal_existing_instance(desktop_tag: &str) {
    #[link(name = "user32")]
    extern "system" {
        fn AllowSetForegroundWindow(dwProcessId: u32) -> i32;
        fn EnumWindows(lpEnumFunc: unsafe extern "system" fn(isize, isize) -> i32, lParam: isize) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenEventW(dwDesiredAccess: u32, bInheritHandle: i32, lpName: *const u16) -> isize;
        fn SetEvent(hEvent: isize) -> i32;
        fn CloseHandle(hObject: isize) -> i32;
    }
    const ASFW_ANY: u32 = 0xFFFF_FFFF;
    const EVENT_MODIFY_STATE: u32 = 0x0002;
    let event_name: Vec<u16> = format!("Local\\BURNRATE-Desktop-Activate-{desktop_tag}\0")
        .encode_utf16()
        .collect();
    unsafe {
        // Grant the already-running BURNRATE shell permission to take foreground focus.
        let _ = AllowSetForegroundWindow(ASFW_ANY);
        let ev = OpenEventW(EVENT_MODIFY_STATE, 0, event_name.as_ptr());
        if ev != 0 {
            let _ = SetEvent(ev);
            let _ = CloseHandle(ev);
        } else {
            let _ = EnumWindows(enum_focus_burnrate_proc, 0);
        }
    }
}

#[cfg(target_os = "windows")]
fn spawn_activation_listener(app: AppHandle, activate_event: Option<std::sync::Arc<SharedWinHandle>>) {
    let Some(ev) = activate_event.filter(|h| h.0 != 0) else {
        return;
    };
    thread::spawn(move || {
        #[link(name = "kernel32")]
        extern "system" {
            fn WaitForSingleObject(hHandle: isize, dwMilliseconds: u32) -> u32;
        }
        const WAIT_OBJECT_0: u32 = 0;
        const INFINITE: u32 = 0xFFFF_FFFF;
        loop {
            let rc = unsafe { WaitForSingleObject(ev.0, INFINITE) };
            if INSTANCE_EXITING.load(Ordering::SeqCst) || rc != WAIT_OBJECT_0 {
                break;
            }
            let app_for_main = app.clone();
            let _ = app.run_on_main_thread(move || {
                activate_from_shortcut(&app_for_main);
            });
        }
    });
}

#[cfg(target_os = "windows")]
fn acquire_single_instance_or_focus() -> Option<SingleInstanceGuard> {
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateMutexW(
            lpMutexAttributes: *const std::ffi::c_void,
            bInitialOwner: i32,
            lpName: *const u16,
        ) -> isize;
        fn CreateEventW(
            lpEventAttributes: *const std::ffi::c_void,
            bManualReset: i32,
            bInitialState: i32,
            lpName: *const u16,
        ) -> isize;
        fn GetLastError() -> u32;
        fn CloseHandle(hObject: isize) -> i32;
    }
    const ERROR_ALREADY_EXISTS: u32 = 183;
    let desktop_tag = current_desktop_tag();
    if desktop_tag.to_ascii_lowercase().starts_with("exebox-")
        && env::var("BURNRATE_ALLOW_SANDBOX_DESKTOP").as_deref() != Ok("1")
    {
        eprintln!(
            "Refusing to run BURNRATE GUI shell on isolated sandbox desktop '{desktop_tag}'; launch on WinSta0\\Default."
        );
        return None;
    }
    INSTANCE_EXITING.store(false, Ordering::SeqCst);
    let mutex_name: Vec<u16> = format!("Local\\BURNRATE-Desktop-Shell-{desktop_tag}\0")
        .encode_utf16()
        .collect();
    let event_name: Vec<u16> = format!("Local\\BURNRATE-Desktop-Activate-{desktop_tag}\0")
        .encode_utf16()
        .collect();
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 1, mutex_name.as_ptr());
        if handle == 0 {
            return Some(SingleInstanceGuard {
                mutex: 0,
                activate_event: None,
            });
        }
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(handle);
            signal_existing_instance(&desktop_tag);
            return None;
        }
        let ev = CreateEventW(std::ptr::null(), 0, 0, event_name.as_ptr());
        let activate_event = (ev != 0).then(|| std::sync::Arc::new(SharedWinHandle(ev)));
        Some(SingleInstanceGuard {
            mutex: handle,
            activate_event,
        })
    }
}

#[cfg(not(target_os = "windows"))]
struct SingleInstanceGuard;

#[cfg(not(target_os = "windows"))]
fn acquire_single_instance_or_focus() -> Option<SingleInstanceGuard> {
    Some(SingleInstanceGuard)
}

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
    let Some(instance_guard) = acquire_single_instance_or_focus() else {
        return;
    };
    #[cfg(target_os = "windows")]
    let activate_event = instance_guard.activate_event.clone();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(HudPinState {
            pinned: AtomicBool::new(true),
            show_in_taskbar: AtomicBool::new(false),
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
        .setup(move |app| {
            #[cfg(target_os = "windows")]
            spawn_activation_listener(app.handle().clone(), activate_event);

            let prefs = load_hud_prefs(app.handle());
            app.state::<HudPinState>()
                .show_in_taskbar
                .store(prefs.taskbar, Ordering::SeqCst);

            let default_icon = app.default_window_icon().cloned();
            if let Some(hud_win) = app.get_webview_window("hud") {
                if let Some(ref ic) = default_icon {
                    let _ = hud_win.set_icon(ic.clone());
                }
                let _ = hud_win.set_skip_taskbar(!prefs.taskbar);
                ensure_hud_on_screen(&hud_win);
                let _ = hud_win.show();
                let _ = apply_hud_always_on_top(&hud_win, true);
                apply_win32_window_icons(&hud_win);
                if !is_autostart_launch() {
                    let _ = hud_win.set_focus();
                    force_win32_foreground(&hud_win);
                }
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
            let hud_taskbar_item = CheckMenuItem::with_id(
                app,
                "hud_taskbar",
                "Show HUD in taskbar",
                true,
                prefs.taskbar,
                None::<&str>,
            )?;
            let open_dashboard_item = MenuItem::with_id(
                app,
                "open_dashboard",
                "Open Full Dashboard",
                true,
                None::<&str>,
            )?;
            let setup_wizard_item = MenuItem::with_id(
                app,
                "setup_wizard",
                "Run Setup Wizard\u{2026}",
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

            let menu = Menu::with_items(
                app,
                &[
                    &toggle_hud_item,
                    &pin_hud_item,
                    &hud_taskbar_item,
                    &open_dashboard_item,
                    &setup_wizard_item,
                ],
            )?;
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
            let hud_taskbar_toggle = hud_taskbar_item.clone();
            let mut tray_builder = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("BURNRATE \u{00b7} Live Multi-Computer Spend & Sidecar HUD")
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "toggle_hud" => {
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud(app.clone(), state);
                    }
                    "pin_hud" => {
                        let state = app.state::<HudPinState>();
                        let _ = toggle_hud_pin(app.clone(), state);
                    }
                    "hud_taskbar" => {
                        let state = app.state::<HudPinState>();
                        let next = !state.show_in_taskbar.load(Ordering::SeqCst);
                        state.show_in_taskbar.store(next, Ordering::SeqCst);
                        update_hud_prefs(app, |p| p.taskbar = next);
                        let _ = hud_taskbar_toggle.set_checked(next);
                        if let Some(hud_win) = app.get_webview_window("hud") {
                            let _ = hud_win.set_skip_taskbar(!next);
                            let pinned = state.pinned.load(Ordering::SeqCst);
                            let _ = apply_hud_always_on_top(&hud_win, pinned);
                        }
                    }
                    "open_dashboard" => {
                        let _ = show_dashboard(app.clone(), None);
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
                                set_update_menu_text(&app, "Checking for updates\u{2026}");
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
                if window.label() == "hud" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
            WindowEvent::Moved(pos) => {
                #[cfg(not(target_os = "windows"))]
                if window.label() == "hud" {
                    let (x, y) = (pos.x, pos.y);
                    update_hud_prefs(window.app_handle(), |p| {
                        p.x = Some(x);
                        p.y = Some(y);
                    });
                }
                #[cfg(target_os = "windows")]
                let _ = pos;
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

    app.run(move |app_handle, event| {
        let _ = &instance_guard;
        if let RunEvent::Exit = event {
            app_handle.state::<BackendSupervisor>().stop_all();
        }
    });
}

#[cfg(test)]
mod supervisor_tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;

    #[test]
    fn desktop_tag_and_main_path_sanitization() {
        assert_eq!(sanitize_desktop_tag("Default"), "Default");
        assert_eq!(sanitize_desktop_tag(""), "Default");
        assert_eq!(sanitize_desktop_tag("WinSta0\\Default"), "WinSta0_Default");
        assert_eq!(sanitize_desktop_tag("exebox-ABC_123"), "exebox-ABC_123");
        assert_eq!(sanitize_main_path(None), "/");
        assert_eq!(sanitize_main_path(Some("/setup")), "/setup");
        assert_eq!(sanitize_main_path(Some("//evil.example")), "/");
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn activation_event_signals_primary_listener() {
        #[link(name = "kernel32")]
        extern "system" {
            fn CreateEventW(
                lpEventAttributes: *const std::ffi::c_void,
                bManualReset: i32,
                bInitialState: i32,
                lpName: *const u16,
            ) -> isize;
            fn WaitForSingleObject(hHandle: isize, dwMilliseconds: u32) -> u32;
        }
        let unique_tag = format!("test-{}-{}", std::process::id(), Instant::now().elapsed().as_nanos());
        let event_name: Vec<u16> = format!("Local\\BURNRATE-Desktop-Activate-{unique_tag}\0")
            .encode_utf16()
            .collect();
        let ev = unsafe { CreateEventW(std::ptr::null(), 0, 0, event_name.as_ptr()) };
        assert_ne!(ev, 0);
        let guard = SharedWinHandle(ev);
        assert_eq!(unsafe { WaitForSingleObject(guard.0, 0) }, 0x0000_0102); // WAIT_TIMEOUT
        signal_existing_instance(&unique_tag);
        assert_eq!(unsafe { WaitForSingleObject(guard.0, 500) }, 0); // WAIT_OBJECT_0
    }

    #[cfg(target_os = "windows")]
    fn helper_command(role: &str) -> Command {
        let mut command = Command::new(env::current_exe().unwrap());
        command.args(["--exact", "supervisor_tests::process_tree_helper", "--ignored"])
            .env("BURNRATE_PROCESS_TREE_TEST_ROLE", role)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        apply_no_window(&mut command);
        command
    }

    // This ignored entry point runs only as a subprocess of the tree test.
    // It never imports provider data or starts the real BURNRATE service.
    #[cfg(target_os = "windows")]
    #[test]
    #[ignore]
    fn process_tree_helper() {
        let Ok(role) = env::var("BURNRATE_PROCESS_TREE_TEST_ROLE") else { return };
        if role == "parent" || role == "exiting-parent" {
            let child = helper_command("leaf").spawn().unwrap();
            std::fs::write(env::var_os("BURNRATE_PROCESS_TREE_TEST_PID_FILE").unwrap(), child.id().to_string()).unwrap();
            if role == "exiting-parent" { return; }
        } else if role != "leaf" {
            return;
        }
        // The owning test always terminates this process's job; a lost test
        // process also closes its non-inherited kill-on-close job handle.
        loop { thread::park(); }
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn terminating_generation_kills_descendants_but_preserves_other_tree() {
        use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
        use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
        use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE};

        let mut other_tree = ManagedChild::spawn(&mut helper_command("leaf")).unwrap();
        for role in ["parent", "exiting-parent"] {
            let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
            let path = env::temp_dir().join(format!("burnrate-process-tree-{}-{stamp}.pid", std::process::id()));
            let mut command = helper_command(role);
            command.env("BURNRATE_PROCESS_TREE_TEST_PID_FILE", &path);
            let mut generation = ManagedChild::spawn(&mut command).unwrap();
            let deadline = Instant::now() + Duration::from_secs(10);
            let descendant_pid = loop {
                if let Ok(text) = std::fs::read_to_string(&path) {
                    if let Ok(pid) = text.parse::<u32>() { break pid; }
                }
                assert!(Instant::now() < deadline, "synthetic parent did not publish its child PID");
                thread::sleep(Duration::from_millis(10));
            };
            let _ = std::fs::remove_file(&path);
            let descendant = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, descendant_pid) };
            assert!(!descendant.is_null(), "synthetic descendant was not running");
            let descendant = unsafe { OwnedHandle::from_raw_handle(descendant) };
            if role == "exiting-parent" {
                while !generation.exited() {
                    assert!(Instant::now() < deadline, "synthetic parent did not exit");
                    thread::sleep(Duration::from_millis(10));
                }
            }
            generation.stop();
            assert_eq!(unsafe { WaitForSingleObject(descendant.as_raw_handle(), 2000) }, WAIT_OBJECT_0,
                       "owned descendant survived generation termination");
            assert!(!other_tree.exited(), "a separate sync/service tree was terminated");
        }
        other_tree.stop();
    }

    fn serve_once(response: String) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 4096];
            let count = stream.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..count]).starts_with("GET /healthz?supervisor=1 "));
            let _ = stream.write_all(response.as_bytes());
        });
        (port, handle)
    }

    fn json_response(value: serde_json::Value) -> String {
        let body = value.to_string();
        format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
    }

    fn payload(stalled: bool) -> serde_json::Value {
        serde_json::json!({"status":"ok", "application":"burnrate", "pid":123,
            "collectorEnabled":true, "ingest":{"stalled":stalled}})
    }

    #[test]
    fn health_checks_application_identity_pid_and_collection_progress() {
        let client = health_client(Duration::from_secs(2)).unwrap();
        for stalled in [false, true] {
            let (port, server) = serve_once(json_response(payload(stalled)));
            assert_eq!(service_health(&client, port), Some(ServiceHealth { pid:123, collection_stalled:stalled }));
            server.join().unwrap();
        }
        for value in [
            serde_json::json!({"status":"ok"}),
            serde_json::json!({"status":"ok","application":"other","pid":123,"collectorEnabled":false}),
            serde_json::json!({"status":"ok","application":"burnrate","pid":0,"collectorEnabled":false}),
            serde_json::json!({"status":"ok","application":"burnrate","pid":123,"collectorEnabled":true}),
        ] {
            let (port, server) = serve_once(json_response(value));
            assert_eq!(service_health(&client, port), None);
            server.join().unwrap();
        }
    }

    #[test]
    fn disabled_collector_does_not_need_a_heartbeat() {
        let client = health_client(Duration::from_secs(2)).unwrap();
        let (port, server) = serve_once(json_response(serde_json::json!({"status":"ok",
            "application":"burnrate","pid":123,"collectorEnabled":false,"ingest":null})));
        assert_eq!(service_health(&client, port), Some(ServiceHealth { pid:123, collection_stalled:false }));
        server.join().unwrap();
    }

    #[test]
    fn listening_socket_and_stalled_response_body_fail_within_deadline() {
        let client = health_client(Duration::from_millis(100)).unwrap();
        for headers in ["", "HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"] {
            let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
            let port = listener.local_addr().unwrap().port();
            let (release, wait) = std::sync::mpsc::channel();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = [0; 4096];
                let _ = stream.read(&mut request);
                let _ = stream.write_all(headers.as_bytes());
                let _ = wait.recv_timeout(Duration::from_secs(3));
            });
            let started = Instant::now();
            assert_eq!(service_health(&client, port), None);
            assert!(started.elapsed() < Duration::from_secs(2));
            release.send(()).unwrap();
            server.join().unwrap();
        }
    }

    #[test]
    fn redirects_and_oversized_bodies_are_not_accepted() {
        let client = health_client(Duration::from_secs(2)).unwrap();
        let (port, server) = serve_once("HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/\r\nContent-Length: 0\r\n\r\n".into());
        assert_eq!(service_health(&client, port), None);
        server.join().unwrap();
        let mut large = payload(false);
        large["padding"] = serde_json::Value::String("x".repeat(HEALTH_BODY_LIMIT as usize));
        let (port, server) = serve_once(json_response(large));
        assert_eq!(service_health(&client, port), None);
        server.join().unwrap();
    }

    #[test]
    fn startup_grace_and_ownership_bound_restarts() {
        let mut owned = OwnedService { pid:123, exited:false, uptime:Duration::from_secs(30) };
        assert!(!restart_due(Some(owned), true, None, 100));
        let stalled = ServiceHealth { pid:123, collection_stalled:true };
        assert!(!restart_due(Some(owned), true, Some(&stalled), 100));
        owned.uptime = STARTUP_WARNING_AFTER;
        assert!(!restart_due(Some(owned), true, None, 3));
        assert!(restart_due(Some(owned), true, None, 4));
        assert!(restart_due(Some(owned), true, Some(&stalled), 4));
        let another = ServiceHealth { pid:456, collection_stalled:true };
        assert!(!restart_due(Some(owned), true, Some(&another), 100));
        assert!(!restart_due(None, true, None, 100));
        assert!(!restart_due(None, true, Some(&stalled), 100));
        assert!(restart_due(None, false, None, 2));
        owned.exited = true;
        assert!(!restart_due(Some(owned), true, None, 100));
        assert!(restart_due(Some(owned), false, None, 1));
    }

    #[test]
    fn healthy_or_warming_collector_and_restart_budget_are_preserved() {
        let owned = OwnedService { pid:123, exited:false, uptime:Duration::from_secs(1200) };
        let warming = ServiceHealth { pid:123, collection_stalled:false };
        assert!(!restart_due(Some(owned), true, Some(&warming), 100));
        let supervisor = BackendSupervisor::new(17331);
        for _ in 0..RESTART_BUDGET {
            assert!(supervisor.may_restart());
        }
        assert!(!supervisor.may_restart());
    }
}
