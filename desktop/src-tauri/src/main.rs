//! Research Manager 桌面版外壳。
//!
//! - 启动时拉起本地 Python 服务（server.py，端口 8765）；已经在跑就直接复用
//! - 主窗口加载 http://localhost:8765，关闭窗口只是隐藏，App 常驻菜单栏
//! - 菜单栏列出关注的会话及状态，点一下打开对应会话
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WindowEvent};

const PORT: u16 = 8765;

struct Server(Mutex<Option<Child>>);

fn server_up() -> bool {
    TcpStream::connect_timeout(&([127, 0, 0, 1], PORT).into(), Duration::from_millis(300)).is_ok()
}

/// 打包后 server.py 在 Resources/core；开发时直接用仓库根目录
fn core_dir(app: &AppHandle) -> PathBuf {
    if let Ok(res) = app.path().resource_dir() {
        let p = res.join("core");
        if p.join("server.py").exists() {
            return p;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn start_server(app: &AppHandle) -> Option<Child> {
    if server_up() {
        return None;
    }
    let home = app.path().home_dir().unwrap_or_else(|_| PathBuf::from("/tmp"));
    let base = home.join(".research-manager");
    let _ = fs::create_dir_all(&base);
    let log = OpenOptions::new().create(true).append(true).open(base.join("server.log")).ok()?;
    // 从 Finder 启动的 App 拿不到终端里的 PATH，补上常见位置，找得到 python3 / claude
    let path = format!(
        "/opt/homebrew/bin:/usr/local/bin:{h}/.local/bin:{h}/.npm-global/bin:{h}/.cargo/bin:{p}",
        h = home.display(),
        p = std::env::var("PATH").unwrap_or_default()
    );
    Command::new("/bin/zsh")
        .args(["-lc", "exec python3 server.py"])
        .current_dir(core_dir(app))
        .env("PATH", path)
        .env("RM_DATA_DIR", base.join("data"))
        .stdout(Stdio::from(log.try_clone().ok()?))
        .stderr(Stdio::from(log))
        .spawn()
        .ok()
}

/// 极简 GET，返回 JSON（本地服务用 HTTP/1.0，读到连接关闭即可）
fn get_json(path: &str) -> Option<serde_json::Value> {
    let mut s = TcpStream::connect_timeout(&([127, 0, 0, 1], PORT).into(), Duration::from_secs(1)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    write!(s, "GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n").ok()?;
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    let body = text.split_once("\r\n\r\n")?.1;
    serde_json::from_str(body).ok()
}

fn ago(mtime: f64) -> String {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let m = ((now - mtime) / 60.0).max(0.0) as u64;
    match m {
        0..=2 => "运行中".into(),
        3..=59 => format!("{m} 分钟前"),
        60..=1439 => format!("{} 小时前", m / 60),
        _ => format!("{} 天前", m / 1440),
    }
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;
    menu.append(&MenuItem::with_id(app, "header", "关注的会话", false, None::<&str>)?)?;
    let sessions = get_json("/api/watched").and_then(|v| v.as_array().cloned()).unwrap_or_default();
    if sessions.is_empty() {
        let hint = if server_up() { "  还没有添加会话" } else { "  本地服务未启动" };
        menu.append(&MenuItem::with_id(app, "none", hint, false, None::<&str>)?)?;
    }
    for s in sessions.iter().take(12) {
        let id = s["id"].as_str().unwrap_or_default();
        let title: String = s["title"].as_str().unwrap_or("会话").chars().take(28).collect();
        let status = ago(s["mtime"].as_f64().unwrap_or(0.0));
        let dot = if status == "运行中" { "●" } else { "○" };
        menu.append(&MenuItem::with_id(app, format!("s:{id}"), format!("{dot} {title}  —  {status}"), true, None::<&str>)?)?;
    }
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "open", "打开主窗口", true, Some("CmdOrCtrl+O"))?)?;
    menu.append(&MenuItem::with_id(app, "settings", "设置…", true, Some("CmdOrCtrl+,"))?)?;
    menu.append(&MenuItem::with_id(app, "quit", "退出 Research Manager", true, Some("CmdOrCtrl+Q"))?)?;
    Ok(menu)
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn open_session(app: &AppHandle, id: &str) {
    show_main(app);
    if let Some(w) = app.get_webview_window("main") {
        // id 只含十六进制和连字符，直接拼进 JS 是安全的
        if id.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
            let _ = w.eval(&format!("window.openSession && openSession('{id}')"));
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let child = start_server(&handle);
            app.manage(Server(Mutex::new(child)));

            let tray = TrayIconBuilder::with_id("main")
                .icon(Image::from_bytes(include_bytes!("../icons/tray.png"))?)
                .icon_as_template(true)
                .tooltip("Research Manager")
                .menu(&build_menu(&handle)?)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "settings" => {
                        show_main(app);
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.eval("window.openSettings && openSettings()");
                        }
                    }
                    "quit" => app.exit(0),
                    id if id.starts_with("s:") => open_session(app, &id[2..]),
                    _ => {}
                })
                .build(app)?;

            // 定时刷新菜单栏里的会话状态
            thread::spawn(move || loop {
                thread::sleep(Duration::from_secs(15));
                let h = handle.clone();
                let tray = tray.clone();
                let _ = handle.run_on_main_thread(move || {
                    if let Ok(menu) = build_menu(&h) {
                        let _ = tray.set_menu(Some(menu));
                    }
                });
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            // 关窗口 = 隐藏，App 继续待在菜单栏
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build app");

    app.run(|app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main(app),
        RunEvent::Exit => {
            if let Some(state) = app.try_state::<Server>() {
                if let Some(mut child) = state.0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        }
        _ => {}
    });
}
