import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  BookOpen,
  FileText,
  Heart,
  Plus,
  Play,
} from "lucide-react";
import { rpc, formatTime, kindNames, errorText } from "../api";
import type { ContentItem, Kind, Page, Relationship, Task } from "../api";
import { ErrorNotice, Loading, Status } from "../components/UI";
export default function Today({
  navigate,
  notify,
}: {
  navigate: (page: Page, path?: string) => void;
  notify: (message: string) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [content, setContent] = useState<ContentItem[]>([]);
  const [care, setCare] = useState<Relationship | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let mounted = true;
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
        setTasks(tasks.tasks);
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
  const active = tasks.filter((task) =>
    ["pending", "running", "cancel_requested"].includes(task.status),
  );
  const attention = tasks.filter((task) =>
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
          <p>读一点，写一点，记得关心。</p>
        </div>
        <span className="local-label">本机工作空间</span>
      </header>
      {error && <ErrorNotice message={error} onDismiss={() => setError("")} />}
      {loading ? (
        <Loading />
      ) : (
        <>
          <section className="today-section">
            <div className="section-heading">
              <h2>需要处理</h2>
              <button
                className="quiet-button"
                onClick={() => navigate("tasks")}
              >
                所有任务
                <ArrowUpRight size={13} />
              </button>
            </div>
            {attention.length ? (
              attention.slice(0, 4).map((task) => (
                <button
                  className="attention-row"
                  key={task.id}
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
              ))
            ) : (
              <p className="quiet-empty">没有需要你处理的问题。</p>
            )}
          </section>
          <section className="today-section">
            <div className="section-heading">
              <h2>新增与最近内容</h2>
            </div>
            {content.length ? (
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
          </section>
          {care && (
            <section className="today-section care-summary">
              <Heart size={21} strokeWidth={1.3} />
              <div>
                <h2>关心 {care.name}</h2>
                <p>
                  在一起第 <strong>{care.days}</strong> 天 · 心情
                  {care.mood === null ? "待填写" : ` ${care.mood}/5`}
                </p>
              </div>
              <button onClick={() => navigate("care")}>
                <Plus size={13} />
                记一笔
              </button>
            </section>
          )}
          <section className="today-section">
            <div className="section-heading">
              <h2>后台正在做什么</h2>
              {active.length > 0 && (
                <span className="muted">{active.length} 项任务</span>
              )}
            </div>
            {active.length ? (
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
          </section>
        </>
      )}
    </div>
  );
}
