// Conductor — Tauri shell around the Python engine (conductor-serve sidecar).
//
// Flow: pick a free port → spawn the sidecar next to our binary → show the
// splash window instantly (native Overlay title bar, traffic lights on the
// cream canvas) → when the engine answers on the port, glide the webview to
// the live dashboard. Closing the window exits the app; the sidecar follows
// via --parent-pid watchdog (and an explicit kill on exit, belt & braces).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, TitleBarStyle, WebviewUrl, WebviewWindowBuilder};

struct Engine(Mutex<Option<Child>>);

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(47747)
}

fn engine_ready(port: u16) -> bool {
    // the HTTP server accepts as soon as it's bound; a TCP connect is enough
    TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}").parse().unwrap(),
        Duration::from_millis(300),
    )
    .is_ok()
}

fn spawn_engine(port: u16) -> std::io::Result<Child> {
    let exe = std::env::current_exe()?;
    let dir = exe.parent().expect("exe has a parent");
    let sidecar = dir.join("conductor-serve");
    Command::new(sidecar)
        .args([
            "--port",
            &port.to_string(),
            "--parent-pid",
            &std::process::id().to_string(),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
}

fn main() {
    let port = free_port();

    tauri::Builder::default()
        .manage(Engine(Mutex::new(None)))
        .setup(move |app| {
            // 1. engine first — it needs a head start while the splash shows
            match spawn_engine(port) {
                Ok(child) => {
                    *app.state::<Engine>().0.lock().unwrap() = Some(child);
                }
                Err(e) => eprintln!("failed to spawn engine: {e}"),
            }

            // 2. splash window with native unified chrome
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Conductor")
                .inner_size(1000.0, 700.0)
                .min_inner_size(760.0, 540.0)
                .title_bar_style(TitleBarStyle::Overlay)
                .hidden_title(true)
                .build()?;

            // 3. glide to the dashboard once the engine answers
            let handle = win.clone();
            std::thread::spawn(move || {
                for _ in 0..300 {
                    if engine_ready(port) {
                        std::thread::sleep(Duration::from_millis(200));
                        let _ = handle.eval(&format!(
                            "window.location.replace('http://127.0.0.1:{port}/?app=1')"
                        ));
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(200));
                }
                let _ = handle.eval(
                    "document.body.innerHTML='<p style=\"font-family:Georgia;font-style:italic;\
                     text-align:center;margin-top:40vh\">the engine never woke up — \
                     check ~/.conductor/.conductor/app.log</p>'",
                );
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                window.app_handle().exit(0); // close = quit (single-window app)
            }
        })
        .build(tauri::generate_context!())
        .expect("error building tauri app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut child) = app.state::<Engine>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
