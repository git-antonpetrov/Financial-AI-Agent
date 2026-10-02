use tauri::{Manager, RunEvent};
use std::sync::Mutex;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

struct BackendProcess(Mutex<Option<CommandChild>>);

#[cfg(windows)]
fn attach_child_to_job_object(pid: u32) {
  use std::ptr::null_mut;
  type HANDLE = *mut std::ffi::c_void;
  type BOOL = i32;
  type DWORD = u32;

  #[repr(C)]
  struct IO_COUNTERS {
    read_operation_count: u64,
    write_operation_count: u64,
    other_operation_count: u64,
    read_transfer_count: u64,
    write_transfer_count: u64,
    other_transfer_count: u64,
  }

  #[repr(C)]
  struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
    per_process_user_time_limit: i64,
    per_job_user_time_limit: i64,
    limit_flags: DWORD,
    minimum_working_set_size: usize,
    maximum_working_set_size: usize,
    active_process_limit: DWORD,
    affinity: usize,
    priority_class: DWORD,
    scheduling_class: DWORD,
  }

  #[repr(C)]
  struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    basic_limit_information: JOBOBJECT_BASIC_LIMIT_INFORMATION,
    io_info: IO_COUNTERS,
    process_memory_limit: usize,
    job_memory_limit: usize,
    peak_process_memory_limit: usize,
    peak_job_memory_limit: usize,
  }

  const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: DWORD = 0x2000;
  const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
  const PROCESS_SET_QUOTA: DWORD = 0x0100;
  const PROCESS_TERMINATE: DWORD = 0x0001;

  unsafe extern "system" {
    fn CreateJobObjectW(lpJobAttributes: *mut std::ffi::c_void, lpName: *const u16) -> HANDLE;
    fn SetInformationJobObject(
      hJob: HANDLE,
      JobObjectInformationClass: i32,
      lpJobObjectInformation: *const std::ffi::c_void,
      cbJobObjectInformationLength: DWORD,
    ) -> BOOL;
    fn OpenProcess(dwDesiredAccess: DWORD, bInheritHandle: BOOL, dwProcessId: DWORD) -> HANDLE;
    fn AssignProcessToJobObject(hJob: HANDLE, hProcess: HANDLE) -> BOOL;
    fn CloseHandle(hObject: HANDLE) -> BOOL;
  }

  unsafe {
    let job = CreateJobObjectW(null_mut(), null_mut());
    if !job.is_null() {
      let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
      info.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
      if SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        &info as *const _ as *const _,
        std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as DWORD,
      ) != 0
      {
        let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
        if !process.is_null() {
          AssignProcessToJobObject(job, process);
          CloseHandle(process);
        }
      }
      // Job handle deliberately remains open for the lifetime of parent process.
      // When the parent process terminates, Windows closes the job handle,
      // which automatically kills the child process.
    }
  }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  let app = tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .setup(|app| {
      use tauri_plugin_shell::ShellExt;
      let sidecar_command = app.shell().sidecar("admin-backend").unwrap();
      let (mut receiver, child) = sidecar_command
        .spawn()
        .expect("Failed to spawn sidecar");

      #[cfg(windows)]
      attach_child_to_job_object(child.pid());

      tauri::async_runtime::spawn(async move {
        while let Some(event) = receiver.recv().await {
          match event {
            CommandEvent::Stdout(line) => {
              if let Ok(text) = String::from_utf8(line) {
                print!("{}", text);
              }
            }
            CommandEvent::Stderr(line) => {
              if let Ok(text) = String::from_utf8(line) {
                eprint!("{}", text);
              }
            }
            CommandEvent::Error(err) => {
              eprintln!("[Sidecar Error]: {}", err);
            }
            CommandEvent::Terminated(payload) => {
              println!("[Sidecar Terminated]: code: {:?}", payload.code);
            }
            _ => {}
          }
        }
      });

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
      RunEvent::ExitRequested { .. } | RunEvent::WindowEvent { event: tauri::WindowEvent::Destroyed, .. } => {
          let state: tauri::State<BackendProcess> = app_handle.state();
          if let Some(child) = state.0.lock().unwrap().take() {
              let _ = child.kill();
          }
      }
      _ => {}
  });
}
