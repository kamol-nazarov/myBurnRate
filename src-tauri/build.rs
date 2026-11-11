// The dashboard, setup, and HUD pages come from the local service
// (http://127.0.0.1), which Tauri treats as a remote origin: a remote page can
// call an app command only when build.rs lists it and capabilities/default.json
// grants it.
const COMMANDS: &[&str] = &[
    "toggle_hud",
    "show_hud",
    "hide_hud",
    "get_hud_pin",
    "set_hud_pin",
    "toggle_hud_pin",
    "set_hud_size",
    "start_hud_drag",
    "start_main_drag",
    "show_dashboard",
    "minimize_main",
    "toggle_maximize_main",
    "hide_main",
    "open_setup_wizard",
    "pending_update",
    "install_update",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
