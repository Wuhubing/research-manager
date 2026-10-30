//! Research Manager 桌面版外壳。
//!
//! - 启动时拉起本地 Python 服务（server.py，端口 8765）；已经在跑就直接复用
//! - 主窗口加载 http://localhost:8765，关闭窗口只是隐藏，App 常驻菜单栏
//! - 菜单栏列出关注的会话及状态，点一下打开对应会话
//! - 全局快捷键 ⌃P：开始录音并弹出菜单栏菜单，再按一次结束；识别出的原话自动复制，并显示在菜单里
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WindowEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

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

/// 极简 POST JSON；timeout 是等待回应的秒数
fn post_json(path: &str, body: &str, timeout: u64) -> Option<serde_json::Value> {
    let mut s = TcpStream::connect_timeout(&([127, 0, 0, 1], PORT).into(), Duration::from_secs(1)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(timeout))).ok()?;
    write!(s, "POST {path} HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
           body.len()).ok()?;
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    serde_json::from_str(text.split_once("\r\n\r\n")?.1).ok()
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

// 菜单用的数据（关注的会话、最近一次录音）。只在后台线程里取，主线程只用这份缓存搭菜单，
// 这样菜单栏不会因为等本地服务而卡顿。
struct MenuData {
    server_up: bool,
    sessions: Vec<serde_json::Value>,
    last_text: String,
    last_error: String,
}

static MENU_DATA: Mutex<MenuData> = Mutex::new(MenuData {
    server_up: false,
    sessions: Vec::new(),
    last_text: String::new(),
    last_error: String::new(),
});

/// 从本地服务取菜单数据（会阻塞，只在后台线程调用）
fn fetch_menu_data() {
    let sessions = get_json("/api/watched").and_then(|v| v.as_array().cloned());
    let last = get_json("/api/quick_record/last").unwrap_or_default();
    let mut d = MENU_DATA.lock().unwrap();
    d.server_up = sessions.is_some();
    d.sessions = sessions.unwrap_or_default();
    d.last_text = last["text"].as_str().unwrap_or_default().to_string();
    d.last_error = last["error"].as_str().unwrap_or_default().to_string();
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let d = MENU_DATA.lock().unwrap();
    let menu = Menu::new(app)?;
    append_quick_area(app, &menu, &d)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "header", "关注的会话", false, None::<&str>)?)?;
    if d.sessions.is_empty() {
        let hint = if d.server_up { "  还没有添加会话" } else { "  本地服务未启动" };
        menu.append(&MenuItem::with_id(app, "none", hint, false, None::<&str>)?)?;
    }
    for s in d.sessions.iter().take(12) {
        let id = s["id"].as_str().unwrap_or_default();
        let title: String = s["title"].as_str().unwrap_or("会话").chars().take(28).collect();
        let status = ago(s["mtime"].as_f64().unwrap_or(0.0));
        let dot = if status == "运行中" { "●" } else { "○" };
        // 上下文超过 70% 提示，超过 85% 加警告
        let pct = s["context"]["pct"].as_u64().unwrap_or(0);
        let ctx = match pct {
            85.. => format!(" · ⚠ 上下文 {pct}%"),
            70..=84 => format!(" · 上下文 {pct}%"),
            _ => String::new(),
        };
        menu.append(&MenuItem::with_id(app, format!("s:{id}"), format!("{dot} {title}  —  {status}{ctx}"), true, None::<&str>)?)?;
    }
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "open", "打开主窗口", true, Some("CmdOrCtrl+O"))?)?;
    menu.append(&MenuItem::with_id(app, "settings", "设置…", true, Some("CmdOrCtrl+,"))?)?;
    menu.append(&MenuItem::with_id(app, "quit", "退出 Research Manager", true, Some("CmdOrCtrl+Q"))?)?;
    Ok(menu)
}

/// 菜单里显示一段长文字：按宽度折行，最多 max_lines 行（菜单项是灰色的说明文字，不可点）
fn append_text(app: &AppHandle, menu: &Menu<tauri::Wry>, id: &str, text: &str, width: usize, max_lines: usize) -> tauri::Result<()> {
    let mut lines: Vec<String> = Vec::new();
    for para in text.lines().filter(|l| !l.trim().is_empty()) {
        let chars: Vec<char> = para.chars().collect();
        for chunk in chars.chunks(width) {
            lines.push(chunk.iter().collect());
        }
    }
    let total = lines.len();
    for (i, line) in lines.into_iter().take(max_lines).enumerate() {
        let more = if i + 1 == max_lines && total > max_lines { " …" } else { "" };
        menu.append(&MenuItem::with_id(app, format!("{id}:{i}"), format!("    {line}{more}"), false, None::<&str>)?)?;
    }
    Ok(())
}

/// 菜单最上面的录音区：录音按钮，以及最近一次识别出的原话（已自动复制）
fn append_quick_area(app: &AppHandle, menu: &Menu<tauri::Wry>, d: &MenuData) -> tauri::Result<()> {
    let key = quick_shortcut(app);
    match QUICK.load(Ordering::SeqCst) {
        RECORDING => {
            menu.append(&MenuItem::with_id(app, "rec_hint", "● 正在录音…", false, None::<&str>)?)?;
            menu.append(&MenuItem::with_id(app, "rec", "■ 结束录音", true, Some(key))?)?;
            return Ok(());
        }
        WORKING => {
            menu.append(&MenuItem::with_id(app, "rec", "识别中…", false, None::<&str>)?)?;
            return Ok(());
        }
        _ => menu.append(&MenuItem::with_id(app, "rec", "录音", true, Some(key))?)?,
    }
    if !d.last_error.is_empty() {
        append_text(app, menu, "err", &format!("上次失败：{}", d.last_error), 30, 3)?;
    } else if !d.last_text.is_empty() {
        menu.append(&MenuItem::with_id(app, "copy_text", "已复制  —  再复制一次", true, None::<&str>)?)?;
        append_text(app, menu, "t", &d.last_text, 30, 8)?;
    }
    Ok(())
}

// 用代码弹出的菜单开着时，tray-icon 一直借用着托盘对象；这时再 set_menu 会 panic 导致 App 崩溃。
// 所以菜单开着时只记一笔“需要刷新”，等菜单关上再刷新。
static MENU_OPEN: AtomicBool = AtomicBool::new(false);
static MENU_DIRTY: AtomicBool = AtomicBool::new(false);

/// 用缓存的数据重建菜单（主线程调用，不做网络请求，很快）
fn apply_menu(app: &AppHandle) {
    if MENU_OPEN.load(Ordering::SeqCst) {
        MENU_DIRTY.store(true, Ordering::SeqCst);
        return;
    }
    let Some(tray) = app.tray_by_id("main") else { return };
    if let Ok(menu) = build_menu(app) {
        let _ = tray.set_menu(Some(menu));
    }
}

/// 后台取最新数据，再回到主线程重建菜单（任何线程都可以调用）
fn refresh_tray(app: &AppHandle) {
    let h = app.clone();
    thread::spawn(move || {
        fetch_menu_data();
        let h2 = h.clone();
        let _ = h.run_on_main_thread(move || apply_menu(&h2));
    });
}

/// 注册 / 取消全局快捷键
fn set_shortcut(app: &AppHandle, on: bool) {
    let Ok(sc) = quick_shortcut(app).parse::<Shortcut>() else { return };
    let gs = app.global_shortcut();
    let _ = if on { gs.register(sc) } else { gs.unregister(sc) };
}

/// 用代码把菜单栏菜单弹出来（会阻塞到菜单关闭）。
/// 菜单开着时暂时取消全局快捷键，这样 ⌃P 会直接触发菜单里的「结束录音」/「录音」。
fn pop_menu(app: &AppHandle) {
    let Some(tray) = app.tray_by_id("main") else { return };
    if MENU_OPEN.swap(true, Ordering::SeqCst) {
        return; // 已经有一个菜单开着
    }
    set_shortcut(app, false);
    let _ = tray.with_inner_tray_icon(|t| t.show_menu());
    MENU_OPEN.store(false, Ordering::SeqCst);
    set_shortcut(app, true);
    if MENU_DIRTY.swap(false, Ordering::SeqCst) {
        apply_menu(app);
    }
}

static SHORTCUT: OnceLock<String> = OnceLock::new();

/// 快捷键默认 ⌃P，可以在 ~/.research-manager/settings.json 里用 "quick_shortcut" 改，例如 "Control+Shift+P"（重启生效）
fn quick_shortcut(app: &AppHandle) -> &'static str {
    SHORTCUT.get_or_init(|| {
        app.path().home_dir().ok()
            .and_then(|h| fs::read_to_string(h.join(".research-manager/settings.json")).ok())
            .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
            .and_then(|v| v["quick_shortcut"].as_str().map(String::from))
            .unwrap_or_else(|| "Control+P".into())
    })
}

// 菜单栏录音的状态
const IDLE: u8 = 0;
const RECORDING: u8 = 1;
const WORKING: u8 = 2;
static QUICK: AtomicU8 = AtomicU8::new(IDLE);

/// ⌃P / 菜单「录音」：没在录就开始录，菜单立刻弹出并保持打开；在录就结束，
/// 识别后自动复制原话，再弹出菜单显示原话。网络请求都在后台线程里做。
fn quick_toggle(app: &AppHandle) {
    match QUICK.load(Ordering::SeqCst) {
        IDLE => {
            QUICK.store(RECORDING, Ordering::SeqCst);
            apply_menu(app);
            let h = app.clone();
            thread::spawn(move || {
                let ok = post_json("/api/quick_record/start", "{}", 5).is_some_and(|v| v.get("error").is_none());
                if !ok {
                    QUICK.store(IDLE, Ordering::SeqCst);
                    refresh_tray(&h); // 菜单里显示失败原因
                }
            });
            pop_menu(app);
        }
        RECORDING => {
            QUICK.store(WORKING, Ordering::SeqCst);
            apply_menu(app);
            let h = app.clone();
            thread::spawn(move || {
                let _ = post_json("/api/quick_record/stop", "{}", 120);
                QUICK.store(IDLE, Ordering::SeqCst);
                fetch_menu_data();
                let h2 = h.clone();
                let _ = h.run_on_main_thread(move || {
                    apply_menu(&h2);
                    pop_menu(&h2); // 弹出菜单显示原话（已复制）
                });
            });
        }
        _ => {}
    }
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

            // 全局快捷键：任何 App 里按 ⌃P 开始 / 结束录音
            handle.plugin(
                tauri_plugin_global_shortcut::Builder::new()
                    .with_handler(|app, _shortcut, event| {
                        if event.state() == ShortcutState::Pressed {
                            quick_toggle(app);
                        }
                    })
                    .build(),
            )?;
            let combo = quick_shortcut(&handle);
            match combo.parse::<Shortcut>() {
                Ok(sc) => {
                    if let Err(e) = handle.global_shortcut().register(sc) {
                        eprintln!("注册快捷键 {combo} 失败：{e}");
                    }
                }
                Err(e) => eprintln!("快捷键 {combo} 格式不对：{e}"),
            }

            TrayIconBuilder::with_id("main")
                .icon(Image::from_bytes(include_bytes!("../icons/tray.png"))?)
                .icon_as_template(true)
                .tooltip("Research Manager")
                .menu(&build_menu(&handle)?)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "rec" => quick_toggle(app),
                    "copy_text" => {
                        thread::spawn(|| post_json("/api/quick_record/copy", "{}", 5));
                    }
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

            // 先在后台取一次数据，之后定时刷新菜单栏里的会话状态
            refresh_tray(&handle);
            thread::spawn(move || loop {
                thread::sleep(Duration::from_secs(15));
                refresh_tray(&handle);
            });
            Ok(())
        })
        .on_window_event(|window, event| match event {
            // 关窗口 = 隐藏，App 继续待在菜单栏
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("failed to build app");

    app.run(|app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main(app),
        RunEvent::Exit => {
            if let Some(state) = app.try_state::<Server>() {
                if let Some(mut child) = state.0.lock().unwrap().take() {
                    // 先礼貌地让服务退出（它会顺手停掉正在进行的录音），2 秒没退再强制结束
                    let _ = Command::new("kill").arg(child.id().to_string()).status();
                    for _ in 0..20 {
                        if matches!(child.try_wait(), Ok(Some(_))) {
                            return;
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                    let _ = child.kill();
                }
            }
        }
        _ => {}
    });
}
