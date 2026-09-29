use tauri::{Manager, RunEvent};
use std::sync::Mutex;
use tauri_plugin_shell::process::CommandChild;

struct BackendProcess(Mutex<Option<CommandChild>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  let app = tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .setup(|app| {
      use tauri_plugin_shell::ShellExt;
      let sidecar_command = app.shell().sidecar("admin-backend").unwrap();
      let (_receiver, child) = sidecar_command
        .spawn()
        .expect("Failed to spawn sidecar");

      app.manage(BackendProcess(Mutex::new(Some(child))));

      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .build(tauri::generate_context!())
    .expect("error while building tauri application");

  app.run(|app_handle, event| match event {
      RunEvent::ExitRequested { .. } => {
          let state: tauri::State<BackendProcess> = app_handle.state();
          if let Some(child) = state.0.lock().unwrap().take() {
              let _ = child.kill();
          }
      }
      _ => {}
  });
}
