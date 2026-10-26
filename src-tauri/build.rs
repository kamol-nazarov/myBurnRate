// The dashboard and HUD pages come from the local service (http://127.0.0.1),
// which Tauri treats as a remote origin: a remote page can call an app command
// only when capabilities/default.json allows it. The update commands are
// allowed; window actions (pin, drag, size) go through the service's
// /api/hud/native route instead.
const COMMANDS: &[&str] = &["pending_update", "install_update"];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
