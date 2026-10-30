import { useCallback, useEffect, useState } from "react";
import {
  Sun,
  LibraryBig,
  BookOpen,
  NotebookPen,
  Heart,
  ListTodo,
  Settings as SettingsIcon,
  RefreshCw,
  Check,
  LoaderCircle,
  Minus,
  WifiOff,
} from "lucide-react";
import { rpc, errorText, serviceControl, setDisplayTimezone } from "./api";
import type { ConfigResponse, Page, SystemStatus } from "./api";
import Today from "./pages/Today";
import Library from "./pages/Library";
import Care from "./pages/Care";
import Tasks from "./pages/Tasks";
import Settings from "./pages/Settings";
import { ErrorNotice } from "./components/UI";
const navigation = [
  { id: "today" as const, label: "今日", icon: Sun },
  { id: "papers" as const, label: "论文", icon: LibraryBig },
  { id: "course" as const, label: "课程", icon: BookOpen },
  { id: "review" as const, label: "复盘", icon: NotebookPen },
  { id: "care" as const, label: "关心", icon: Heart },
];
export default function App() {
  const [page, setPage] = useState<Page>("today");
  const [path, setPath] = useState<string>();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [toast, setToast] = useState("");
  const [theme, setTheme] = useState("system");
  const notify = useCallback((message: string) => setToast(message), []);
  const refresh = useCallback(async () => {
    try {
      const value = await rpc<SystemStatus>("system.status");
      setStatus(value);
      setConnectionError("");
    } catch (error) {
      setConnectionError(errorText(error));
    }
  }, []);
  const loadConfig = useCallback(async () => {
    try {
      const value = await rpc<ConfigResponse>("config.get");
      setDisplayTimezone(value.config.timezone);
      setTheme(value.config.desktop?.appearance || "system");
    } catch {
      /* 连接状态由独立健康检查显示。 */
    }
  }, []);
  useEffect(() => {
    void refresh();
    void loadConfig();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh, loadConfig]);
  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme =
        theme === "system" ? (query.matches ? "dark" : "light") : theme;
    };
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 4000);
    return () => clearTimeout(timer);
  }, [toast]);
  const navigate = useCallback((next: Page, nextPath?: string) => {
    setPath(nextPath);
    setPage(next);
  }, []);
  useEffect(() => {
    function key(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key === ",") {
        event.preventDefault();
        navigate("settings");
      }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [navigate]);
  async function start() {
    setConnecting(true);
    try {
      const value = (await serviceControl("start")) as {
        status: string;
        message?: string;
      };
      if (value.status === "waiting_old_worker" || value.status === "error") {
        setConnectionError(value.message || "旧任务仍在运行，后台尚未切换。");
        return;
      }
      for (let index = 0; index < 20; index++) {
        try {
          const value = await rpc<SystemStatus>("system.status");
          setStatus(value);
          setConnectionError("");
          void loadConfig();
          return;
        } catch {
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      }
      throw new Error("后台启动后暂未响应，请查看启动诊断。");
    } catch (err) {
      setConnectionError(errorText(err));
    } finally {
      setConnecting(false);
    }
  }
  const active = !!status?.active_task;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="titlebar" data-tauri-drag-region="true" />
        <div className="brand">
          <span className="brand-mark">
            <span />
            <span />
          </span>
          <div>
            <strong>second proprio</strong>
            <small>个人工作台</small>
          </div>
        </div>
        <nav aria-label="主导航">
          {navigation.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={page === id ? "selected" : ""}
              onClick={() => navigate(id)}
              aria-current={page === id ? "page" : undefined}
            >
              <Icon size={17} strokeWidth={1.6} />
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <nav aria-label="管理">
            <button
              className={page === "tasks" ? "selected" : ""}
              onClick={() => navigate("tasks")}
            >
              <ListTodo size={17} strokeWidth={1.6} />
              任务{active && <span className="nav-dot" />}
            </button>
            <button
              className={page === "settings" ? "selected" : ""}
              onClick={() => navigate("settings")}
            >
              <SettingsIcon size={17} strokeWidth={1.6} />
              设置
            </button>
          </nav>
          <div className="service-state">
            {connectionError ? (
              <WifiOff size={12} />
            ) : active ? (
              <LoaderCircle size={12} className="spin" />
            ) : status?.paused ? (
              <Minus size={12} />
            ) : (
              <span className="service-dot" />
            )}
            <span>
              {connectionError
                ? "后台未连接"
                : active
                  ? "后台正在工作"
                  : status?.paused
                    ? "AI 任务已暂停"
                    : "后台已就绪"}
            </span>
            <button
              className="icon-button"
              aria-label="刷新连接"
              onClick={() => void refresh()}
            >
              <RefreshCw size={12} />
            </button>
          </div>
        </div>
      </aside>
      <main>
        <div className="main-titlebar" data-tauri-drag-region="true" />
        {!status ? (
          <div className="connection-screen">
            <span className="connection-symbol">
              <BookOpen size={30} strokeWidth={1.2} />
            </span>
            <h1>欢迎回到 second proprio</h1>
            <p>连接本机后台，继续你的阅读与记录。</p>
            {connectionError && <ErrorNotice message={connectionError} />}
            <button
              className="primary"
              disabled={connecting}
              onClick={() => void start()}
            >
              {connecting && <LoaderCircle size={14} className="spin" />}
              {connecting ? "正在连接…" : "启动并连接后台"}
            </button>
            <small>资料保存在你的工作目录，界面退出后后台仍可继续。</small>
          </div>
        ) : (
          <>
            {connectionError && (
              <div className="connection-banner">
                连接暂时中断，显示上次读取的内容。
                <button disabled={connecting} onClick={() => void start()}>
                  重新连接
                </button>
              </div>
            )}
            {page === "today" && <Today navigate={navigate} notify={notify} />}{" "}
            {["papers", "course", "review"].includes(page) && (
              <Library
                key={page}
                kind={page as "papers" | "course" | "review"}
                notify={notify}
                initialPath={path}
              />
            )}{" "}
            {page === "care" && <Care notify={notify} />}{" "}
            {page === "tasks" && <Tasks notify={notify} navigate={navigate} />}{" "}
            {page === "settings" && (
              <Settings notify={notify} onSaved={() => void loadConfig()} />
            )}
          </>
        )}
      </main>
      {toast && (
        <div className="toast" role="status">
          <Check size={15} />
          {toast}
        </div>
      )}
    </div>
  );
}
