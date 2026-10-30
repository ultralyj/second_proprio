//! second proprio 的桌面边界：仅允许受控本机操作。
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use tauri::{Manager, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;

#[derive(Clone)]
struct Workspace {
    root: PathBuf,
}

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}
fn support() -> PathBuf {
    home().join("Library/Application Support/second-proprio")
}
fn workspace_root() -> PathBuf {
    if let Some(value) = std::env::var_os("SECOND_PROPRIO_WORKSPACE") {
        return PathBuf::from(value);
    }
    if let Ok(text) = fs::read_to_string(support().join("desktop.json")) {
        if let Ok(value) = serde_json::from_str::<Value>(&text) {
            if let Some(path) = value["workspace"].as_str() {
                return PathBuf::from(path);
            }
        }
    }
    home().join("repo/second-proprio")
}
fn config(root: &Path) -> Value {
    fs::read_to_string(root.join("config/settings.json"))
        .or_else(|_| fs::read_to_string(root.join("config/settings.example.json")))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(json!({}))
}
fn state_directory(root: &Path) -> PathBuf {
    root.join(config(root)["paths"]["state"].as_str().unwrap_or(".state"))
}
fn allowed_method(method: &str) -> bool {
    matches!(
        method,
        "system.status"
            | "system.events"
            | "system.stop"
            | "config.get"
            | "config.save"
            | "config.validate"
            | "config.sync"
            | "tasks.list"
            | "tasks.detail"
            | "tasks.submit"
            | "tasks.pause"
            | "tasks.cancel"
            | "tasks.retry"
            | "tasks.events"
            | "content.list"
            | "content.read"
            | "content.save"
            | "content.draft"
            | "content.history"
            | "content.restore"
            | "content.candidates"
            | "content.accept"
            | "content.reject"
            | "course.learned"
            | "review.import"
            | "review.add"
            | "relationship.get"
            | "relationship.save"
    )
}
fn socket_request(root: &Path, method: &str, params: Value) -> Result<Value, String> {
    if !allowed_method(method) {
        return Err("不支持此操作".into());
    }
    let mut socket = UnixStream::connect(state_directory(root).join("desktop.sock"))
        .map_err(|_| "后台尚未连接，请启动后台服务。".to_string())?;
    socket
        .set_read_timeout(Some(Duration::from_secs(30)))
        .map_err(|e| e.to_string())?;
    socket
        .set_write_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| e.to_string())?;
    let request = json!({"id": "desktop", "method": method, "params": params});
    let mut bytes = serde_json::to_vec(&request).map_err(|e| e.to_string())?;
    if bytes.len() > 8 * 1024 * 1024 {
        return Err("请求过大".into());
    }
    bytes.push(b'\n');
    socket.write_all(&bytes).map_err(|e| e.to_string())?;
    let mut response = String::new();
    BufReader::new(socket)
        .take(16 * 1024 * 1024)
        .read_line(&mut response)
        .map_err(|e| e.to_string())?;
    let value: Value =
        serde_json::from_str(&response).map_err(|_| "后台返回了无效响应".to_string())?;
    if let Some(error) = value.get("error") {
        return Err(error.to_string());
    }
    Ok(value.get("result").cloned().unwrap_or(value))
}
#[tauri::command]
async fn rpc(
    workspace: State<'_, Workspace>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let root = workspace.root.clone();
    tauri::async_runtime::spawn_blocking(move || {
        socket_request(&root, &method, params.unwrap_or(json!({})))
    })
    .await
    .map_err(|e| e.to_string())?
}
fn scoped_path(root: &Path, path: &str) -> Result<PathBuf, String> {
    let root = root
        .canonicalize()
        .map_err(|_| "工作区不存在".to_string())?;
    let path = PathBuf::from(path);
    let target = if path.is_absolute() {
        path
    } else {
        root.join(path)
    }
    .canonicalize()
    .map_err(|_| "文件不存在".to_string())?;
    if !target.starts_with(&root) {
        return Err("文件不在当前工作区内".into());
    }
    Ok(target)
}
#[tauri::command]
async fn open_path(workspace: State<'_, Workspace>, path: String) -> Result<(), String> {
    let target = scoped_path(&workspace.root, &path)?;
    if target.is_file()
        && !matches!(
            target.extension().and_then(|v| v.to_str()),
            Some("md" | "pdf" | "py" | "json" | "png" | "jpg" | "jpeg" | "svg" | "webp")
        )
    {
        return Err("不支持打开此文件类型".into());
    }
    let mut command = Command::new("/usr/bin/open");
    if target.extension().and_then(|value| value.to_str()) == Some("py") {
        // 示例入口用于编辑源码，避免系统的 Python Launcher 直接执行文件。
        command.arg("-t");
    }
    command.arg(target).spawn().map_err(|e| e.to_string())?;
    Ok(())
}
#[tauri::command]
async fn open_url(url: String) -> Result<(), String> {
    let parsed = url::Url::parse(&url).map_err(|_| "链接无效".to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("只支持网页链接".into());
    }
    Command::new("/usr/bin/open")
        .arg(parsed.as_str())
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}
#[tauri::command]
async fn read_asset(workspace: State<'_, Workspace>, path: String) -> Result<String, String> {
    let target = scoped_path(&workspace.root, &path)?;
    let mime = match target
        .extension()
        .and_then(|v| v.to_str())
        .unwrap_or("")
        .to_lowercase()
        .as_str()
    {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "svg" => "image/svg+xml",
        "webp" => "image/webp",
        "gif" => "image/gif",
        _ => return Err("只支持图片资源".into()),
    };
    if fs::metadata(&target).map_err(|e| e.to_string())?.len() > 16 * 1024 * 1024 {
        return Err("图片超过16 MiB".into());
    }
    let bytes = fs::read(target).map_err(|e| e.to_string())?;
    Ok(format!("data:{};base64,{}", mime, STANDARD.encode(bytes)))
}
#[tauri::command]
async fn choose_import(app: tauri::AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .add_filter("聊天记录 JSON", &["json"])
            .blocking_pick_file()
            .map(|file| {
                file.into_path()
                    .map(|p| p.to_string_lossy().into_owned())
                    .map_err(|e| e.to_string())
            })
            .transpose()
    })
    .await
    .map_err(|e| e.to_string())?
}
fn run_bounded(mut command: Command, timeout: Duration) -> Result<std::process::Output, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    let start = Instant::now();
    loop {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return child.wait_with_output().map_err(|e| e.to_string());
        }
        if start.elapsed() > timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Err("操作超时，请查看后台状态".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
fn engine_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("runtime/second-proprio-engine/second-proprio-engine");
    if bundled.is_file() {
        return Ok(bundled);
    }
    if let Ok(text) = fs::read_to_string(support().join("runtime.json")) {
        if let Ok(value) = serde_json::from_str::<Value>(&text) {
            if let Some(path) = value["engine"].as_str() {
                let candidate = PathBuf::from(path);
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
        }
    }
    Err("后端运行时尚未打包，请从安装后的 App 启动。".into())
}
#[tauri::command]
async fn service_control(
    app: tauri::AppHandle,
    workspace: State<'_, Workspace>,
    action: String,
) -> Result<Value, String> {
    if !matches!(action.as_str(), "start" | "stop") {
        return Err("不支持此服务操作".into());
    }
    let engine = engine_path(&app)?;
    let root = workspace.root.clone();
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut cmd = Command::new(engine);
        cmd.arg("desktop-control")
            .arg(action)
            .arg("--root")
            .arg(root)
            .arg("--app")
            .arg(executable);
        let output = run_bounded(cmd, Duration::from_secs(45))?;
        let result = serde_json::from_slice::<Value>(&output.stdout)
            .map_err(|_| format!("服务管理失败：{}", String::from_utf8_lossy(&output.stderr)))?;
        Ok(result)
    })
    .await
    .map_err(|e| e.to_string())?
}
#[tauri::command]
async fn check_login(workspace: State<'_, Workspace>) -> Result<Value, String> {
    let value = config(&workspace.root);
    let configured = value["runtime"]["codex_command"]
        .as_str()
        .unwrap_or("codex")
        .to_string();
    tauri::async_runtime::spawn_blocking(move || {
        let bundled = Path::new("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex");
        let executable = if configured == "codex" && bundled.exists() { bundled.to_path_buf() } else { PathBuf::from(configured) };
        let mut cmd = Command::new(executable); cmd.args(["login", "status"]);
        match run_bounded(cmd, Duration::from_secs(15)) {
            Ok(output) => {
                let message = format!("{}{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
                Ok(json!({"status": if output.status.success() {"logged_in"} else {"not_logged_in"}, "message": message.trim()}))
            },
            Err(error) => Ok(json!({"status":"unavailable", "message":error}))
        }
    }).await.map_err(|e| e.to_string())?
}
fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}
pub fn run() {
    let args: Vec<String> = std::env::args().collect();
    let notification = args.iter().any(|arg| arg == "--notify-json");
    let mut context = tauri::generate_context!();
    if notification {
        context.config_mut().app.windows.clear();
    }
    let builder = tauri::Builder::default().plugin(tauri_plugin_notification::init());
    if notification {
        let mut input = String::new();
        if std::io::stdin()
            .take(16384)
            .read_to_string(&mut input)
            .is_err()
        {
            return;
        }
        let value: Value = serde_json::from_str(&input).unwrap_or(json!({}));
        builder
            .setup(move |app| {
                let title = value["title"].as_str().unwrap_or("second proprio");
                let body = value["message"]
                    .as_str()
                    .or_else(|| value["body"].as_str())
                    .unwrap_or("");
                match app.notification().builder().title(title).body(body).show() {
                    Ok(_) => println!("{}", json!({"status":"accepted"})),
                    Err(error) => {
                        println!("{}", json!({"status":"error","message":error.to_string()}))
                    }
                }
                app.handle().exit(0);
                Ok(())
            })
            .run(context)
            .expect("通知入口初始化失败");
        return;
    }
    let root = workspace_root();
    builder
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_main(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(Workspace { root })
        .invoke_handler(tauri::generate_handler![
            rpc,
            open_path,
            open_url,
            read_asset,
            choose_import,
            service_control,
            check_login
        ])
        .setup(|app| {
            use tauri::menu::{Menu, MenuItem};
            let open = MenuItem::with_id(app, "open", "打开 second proprio", true, None::<&str>)?;
            let pause = MenuItem::with_id(app, "pause", "暂停 AI 任务", true, None::<&str>)?;
            let resume = MenuItem::with_id(app, "resume", "恢复 AI 任务", true, None::<&str>)?;
            let quit =
                MenuItem::with_id(app, "quit-ui", "退出界面（后台继续）", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &pause, &resume, &quit])?;
            tauri::tray::TrayIconBuilder::with_id("main-tray")
                .menu(&menu)
                .tooltip("second proprio")
                .title("sp")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "quit-ui" => app.exit(0),
                    "pause" | "resume" => {
                        let root = app.state::<Workspace>().root.clone();
                        let paused = event.id.as_ref() == "pause";
                        std::thread::spawn(move || {
                            let _ = socket_request(&root, "tasks.pause", json!({"paused":paused}));
                        });
                    }
                    _ => (),
                })
                .build(app)?;
            show_main(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(context)
        .expect("second proprio 启动失败");
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_unknown_rpc_commands() {
        assert!(!allowed_method("shell.exec"));
        assert!(allowed_method("content.save"));
    }
    #[test]
    fn rejects_paths_outside_workspace() {
        assert!(scoped_path(Path::new("/tmp"), "/etc/passwd").is_err());
    }
}
