/** 汇总近期成果、待办和关心记录，折叠状态仅属于当前界面。 */
import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronRight,
  FileText,
  Heart,
  Plus,
  Play,
} from "lucide-react";
import { rpc, formatTime, kindNames, errorText } from "../api";
import type {
  ConfigResponse,
  ContentItem,
  Kind,
  Page,
  Relationship,
  Task,
} from "../api";
import { ErrorNotice, Loading, Status } from "../components/UI";
import "./personal.css";
const DEFAULT_QUESTION = "此刻，我最值得投入的一件事是什么？";
function TodayGroup({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <section className={`today-group ${open ? "is-open" : ""}`}>
      <h2>
        <button
          className="today-group-toggle"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen(!open)}
        >
          <ChevronRight size={14} aria-hidden="true" />
          <span>{title}</span>
          {typeof count === "number" && count > 0 && <small>{count}</small>}
        </button>
      </h2>
      <div
        className="today-group-collapse"
        id={id}
        aria-hidden={!open}
        inert={!open}
      >
        <div>
          <div className="today-group-body">{children}</div>
        </div>
      </div>
    </section>
  );
}
export default function Today({
  navigate,
  notify,
}: {
  navigate: (page: Page, path?: string) => void;
  notify: (message: string) => void;
}) {
  const [question, setQuestion] = useState(DEFAULT_QUESTION);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [content, setContent] = useState<ContentItem[]>([]);
  const [care, setCare] = useState<Relationship | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [acknowledging, setAcknowledging] = useState<string[]>([]);
  const acknowledged = useRef(new Map<string, string>());
  useEffect(() => {
    let mounted = true;
    void rpc<ConfigResponse>("config.get")
      .then(({ config }) => {
        if (!mounted) return;
        const value = config.desktop?.home_question;
        setQuestion(
          typeof value === "string" && value.trim() ? value : DEFAULT_QUESTION,
        );
      })
      .catch((err) => {
        if (mounted) setError(errorText(err));
      });
    async function load() {
      try {
        const [tasks, papers, courses, reviews, care] = await Promise.all([
          rpc<{ tasks: Task[] }>("tasks.list"),
          rpc<{ items: ContentItem[] }>("content.list", { kind: "papers" }),
          rpc<{ items: ContentItem[] }>("content.list", { kind: "course" }),
          rpc<{ items: ContentItem[] }>("content.list", { kind: "review" }),
          rpc<Relationship>("relationship.get"),
        ]);
        if (!mounted) return;
        setTasks(
          tasks.tasks.map((task) =>
            task.attention_version &&
            acknowledged.current.get(task.id) === task.attention_version
              ? { ...task, attention_dismissed: true }
              : task,
          ),
        );
        setContent(
          [...papers.items, ...courses.items, ...reviews.items]
            .filter((item) => !["planned", "pending"].includes(item.status))
            .sort((a, b) => (b.date || "").localeCompare(a.date || ""))
            .slice(0, 6),
        );
        setCare(care);
      } catch (err) {
        if (mounted) setError(errorText(err));
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    const timer = setInterval(() => void load(), 10000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, []);
  async function submit(kind: Kind) {
    try {
      await rpc("tasks.submit", { kind });
      notify("任务已加入队列");
    } catch (err) {
      setError(errorText(err));
    }
  }
  async function acknowledge(task: Task) {
    setAcknowledging((current) => [...current, task.id]);
    try {
      const result = await rpc<{ task: Task }>("tasks.acknowledge", {
        id: task.id,
        attention_version: task.attention_version,
      });
      if (result.task.attention_version) {
        acknowledged.current.set(task.id, result.task.attention_version);
      }
      setTasks((current) =>
        current.map((item) =>
          item.id === task.id &&
          item.attention_version === result.task.attention_version
            ? result.task
            : item,
        ),
      );
      notify("已处理");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setAcknowledging((current) => current.filter((id) => id !== task.id));
    }
  }
  const active = tasks.filter((task) =>
    ["pending", "running", "cancel_requested"].includes(task.status),
  );
  const attention = tasks.filter(
    (task) =>
      !task.attention_dismissed &&
      ["failed", "waiting_source", "waiting_review"].includes(task.status),
  );
  const day = care?.date
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "long",
        day: "numeric",
        weekday: "long",
        timeZone: "UTC",
      }).format(new Date(care.date + "T12:00:00Z"))
    : "本机个人工作台";
  return (
    <div className="page-scroll today">
      <header className="page-heading">
        <div>
          <p className="eyebrow">{day}</p>
          <h1>今天</h1>
          <p className="today-question">{question}</p>
        </div>
        <span className="local-label">本机工作空间</span>
      </header>
      {error && <ErrorNotice message={error} onDismiss={() => setError("")} />}
      <div className="today-groups">
        <TodayGroup title="需要处理" count={attention.length}>
          {loading ? (
            <Loading />
          ) : attention.length ? (
            attention.slice(0, 4).map((task) => (
              <div className="attention-item" key={task.id}>
                <button
                  className="attention-row"
                  onClick={() =>
                    navigate(
                      task.status === "waiting_source" && task.kind === "review"
                        ? "review"
                        : "tasks",
                    )
                  }
                >
                  <span className="attention-dot" />
                  <div>
                    <strong>{kindNames[task.kind]}</strong>
                    <p>
                      {task.last_error ||
                        task.result?.summary ||
                        "需要你查看后继续"}
                    </p>
                  </div>
                  <Status value={task.status} />
                </button>
                <button
                  className="quiet-button attention-complete"
                  disabled={acknowledging.includes(task.id)}
                  onClick={() => void acknowledge(task)}
                >
                  <Check size={13} aria-hidden="true" />
                  {acknowledging.includes(task.id) ? "处理中…" : "处理完成"}
                </button>
              </div>
            ))
          ) : (
            <p className="quiet-empty">没有需要你处理的问题。</p>
          )}
          <button
            className="quiet-button today-more"
            onClick={() => navigate("tasks")}
          >
            所有任务
            <ArrowUpRight size={13} />
          </button>
        </TodayGroup>
        <TodayGroup title="新增与最近内容" count={content.length}>
          {loading ? (
            <Loading />
          ) : content.length ? (
            content.map((item) => (
              <button
                className="recent-row"
                key={item.path}
                onClick={() =>
                  navigate(
                    item.kind === "papers"
                      ? "papers"
                      : item.kind === "course"
                        ? "course"
                        : "review",
                    item.path,
                  )
                }
              >
                {item.kind === "course" ? (
                  <BookOpen size={18} />
                ) : (
                  <FileText size={18} />
                )}
                <div>
                  <strong>{item.title}</strong>
                  <span>
                    {item.kind === "course"
                      ? "互动课程"
                      : item.kind === "papers"
                        ? "论文解读"
                        : "每日复盘"}{" "}
                    · {item.date?.slice(0, 10) || "本机内容"}
                  </span>
                </div>
                <ArrowUpRight size={14} />
              </button>
            ))
          ) : (
            <p className="quiet-empty">成果完成后，会出现在这里。</p>
          )}
        </TodayGroup>
        <TodayGroup title={care ? `关心 ${care.name}` : "关心"}>
          {loading ? (
            <Loading />
          ) : care ? (
            <div className="care-summary">
              <Heart size={21} strokeWidth={1.3} />
              <div>
                <p>
                  在一起第 <strong>{care.days}</strong> 天 · 心情
                  {care.mood === null ? "待填写" : ` ${care.mood}/5`}
                </p>
              </div>
              <button onClick={() => navigate("care")}>
                <Plus size={13} />
                记一笔
              </button>
            </div>
          ) : (
            <p className="quiet-empty">暂时无法读取关心记录。</p>
          )}
        </TodayGroup>
        <TodayGroup title="后台任务" count={active.length}>
          {loading ? (
            <Loading />
          ) : active.length ? (
            active.slice(0, 3).map((task) => (
              <button
                className="activity-row"
                key={task.id}
                onClick={() => navigate("tasks")}
              >
                <Status value={task.status} />
                <span>
                  {kindNames[task.kind]}
                  {task.stage ? ` · ${task.stage}` : ""}
                </span>
                <time>{formatTime(task.scheduled_at)}</time>
              </button>
            ))
          ) : (
            <p className="quiet-empty">后台空闲，下一项任务会按日程执行。</p>
          )}
          <div className="quick-actions">
            <button onClick={() => void submit("papers")}>
              <Play size={12} />
              检索论文
            </button>
            <button onClick={() => void submit("course")}>
              <Play size={12} />
              更新课程
            </button>
            <button onClick={() => void submit("review")}>
              <Play size={12} />
              生成复盘
            </button>
          </div>
        </TodayGroup>
      </div>
    </div>
  );
}
