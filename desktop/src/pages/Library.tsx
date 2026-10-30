import { useCallback, useEffect, useState } from "react";
import {
  Search,
  Plus,
  Upload,
  Play,
  PanelRightClose,
  CheckCircle2,
} from "lucide-react";
import { rpc, errorText, chooseImport, statusLabel } from "../api";
import type { ContentItem, Kind } from "../api";
import DocumentView from "../components/DocumentView";
import { Empty, ErrorNotice, Loading, Modal } from "../components/UI";
export default function Library({
  kind,
  notify,
  initialPath,
}: {
  kind: Exclude<Kind, "care">;
  notify: (message: string) => void;
  initialPath?: string;
}) {
  const [items, setItems] = useState<ContentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ContentItem | null>(null);
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("");
  const [state, setState] = useState("");
  const [date, setDate] = useState("");
  const [notes, setNotes] = useState(false);
  const [noteTab, setNoteTab] = useState<"note" | "feedback">("note");
  const [add, setAdd] = useState(false);
  const [evidence, setEvidence] = useState("");
  const [evidenceDate, setEvidenceDate] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const value = await rpc<{ items: ContentItem[] }>("content.list", {
        kind,
      });
      setItems(value.items);
      setError("");
      if (initialPath) {
        const found = value.items.find((item) =>
          [item.path, item.note_path, item.feedback_path].includes(initialPath),
        );
        setSelected((previous) => previous || found || null);
        if (
          found &&
          (found.note_path === initialPath ||
            found.feedback_path === initialPath)
        ) {
          setNotes(true);
          setNoteTab(found.feedback_path === initialPath ? "feedback" : "note");
        }
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, [kind, initialPath]);
  useEffect(() => {
    setSelected(null);
    setNotes(false);
    setQuery("");
    setTopic("");
    setState("");
    setDate("");
    setLoading(true);
    setError("");
    void load();
    const timer = setInterval(() => void load(), 15000);
    return () => clearInterval(timer);
  }, [load]);
  const visible = items.filter(
    (item) =>
      (!query ||
        `${item.title} ${item.arxiv_id || ""}`
          .toLocaleLowerCase()
          .includes(query.toLocaleLowerCase())) &&
      (!topic || item.topic === topic) &&
      (!state || item.status === state) &&
      (!date || item.date?.startsWith(date)),
  );
  async function run(mode?: string) {
    setBusy(true);
    try {
      await rpc("tasks.submit", {
        kind,
        mode,
        ...(mode === "feedback" && selected?.lesson_id
          ? { lesson_id: selected.lesson_id }
          : {}),
      });
      notify(mode === "feedback" ? "反馈已加入任务队列" : "任务已加入队列");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  async function importChat() {
    try {
      const path = await chooseImport();
      if (path) {
        await rpc("review.import", { path });
        notify("记录已导入，相关日期等待复盘");
        void load();
      }
    } catch (err) {
      setError(errorText(err));
    }
  }
  async function addEvidence() {
    setBusy(true);
    try {
      await rpc("review.add", {
        ...(evidenceDate ? { date: evidenceDate } : {}),
        text: evidence,
      });
      setAdd(false);
      setEvidence("");
      notify("当天记录已保存");
      void load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  const currentItem = selected
    ? items.find((item) => item.path === selected.path) || selected
    : null;
  const title = { papers: "论文", course: "课程", review: "复盘" }[kind];
  return (
    <div className="library page-fill">
      <div className="library-list">
        <header className="list-heading">
          <h1>{title}</h1>
          <button
            className="icon-button"
            aria-label={`运行${title}任务`}
            disabled={busy}
            onClick={() => void run()}
          >
            <Play size={15} />
          </button>
        </header>
        <div className="search-box">
          <Search size={14} />
          <input
            aria-label={`搜索${title}`}
            placeholder={`搜索${title}`}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        {kind === "papers" && (
          <div className="list-filters">
            <select
              aria-label="按课题筛选"
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
            >
              <option value="">全部课题</option>
              {[
                ...new Set(items.map((item) => item.topic).filter(Boolean)),
              ].map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
            <select
              aria-label="按状态筛选"
              value={state}
              onChange={(event) => setState(event.target.value)}
            >
              <option value="">全部状态</option>
              {[...new Set(items.map((item) => item.status))].map((value) => (
                <option key={value} value={value}>
                  {statusLabel(value)}
                </option>
              ))}
            </select>
            <input
              aria-label="按日期筛选"
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
            />
          </div>
        )}
        {kind === "review" && (
          <div className="list-actions">
            <button onClick={() => void importChat()}>
              <Upload size={13} />
              导入记录
            </button>
            <button onClick={() => setAdd(true)}>
              <Plus size={13} />
              补充
            </button>
          </div>
        )}
        <div className="list-scroll">
          {loading ? (
            <Loading />
          ) : visible.length ? (
            visible.map((item, index) => (
              <button
                key={item.path}
                className={`library-row ${selected?.path === item.path ? "selected" : ""}`}
                onClick={() => setSelected(item)}
              >
                {kind === "course" && (
                  <span className="lesson-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                )}
                <div>
                  <strong>{item.title}</strong>
                  <div className="row-meta">
                    {kind === "course" ? (
                      <>
                        <span>
                          {item.learned
                            ? "已学完"
                            : item.status === "generated"
                              ? "已生成 · 待学习"
                              : statusLabel(item.status)}
                        </span>
                        {item.feedback_pending && (
                          <span className="accent">待反馈</span>
                        )}
                      </>
                    ) : (
                      <>
                        <span>{item.date?.slice(0, 10) || "未标注日期"}</span>
                        {item.version && <span>v{item.version}</span>}
                        {item.status === "waiting_review" && (
                          <span className="accent">等待采纳</span>
                        )}
                      </>
                    )}
                  </div>
                  {kind === "papers" && (
                    <span className="row-topic">
                      {item.topic || item.arxiv_id}
                    </span>
                  )}
                </div>
                {item.learned && <CheckCircle2 size={14} className="muted" />}
              </button>
            ))
          ) : (
            <p className="list-empty">
              {query ? "没有匹配的内容" : "还没有内容"}
            </p>
          )}
        </div>
        <footer className="list-footer">
          {visible.length} {kind === "course" ? "节" : "篇"} · 保存在本机
        </footer>
      </div>
      <div className="library-main">
        {error && (
          <ErrorNotice message={error} onDismiss={() => setError("")} />
        )}{" "}
        {currentItem ? (
          <DocumentView
            key={currentItem.path}
            path={currentItem.path}
            item={currentItem}
            notify={notify}
            onChanged={() => void load()}
            onNotes={() => setNotes(!notes)}
            onBack={() => setSelected(null)}
          />
        ) : (
          <Empty
            title={kind === "course" ? "沿着课程，一节一节学" : "选择一篇内容"}
            description={
              kind === "papers"
                ? "解读、公式与插图都保存在本机。"
                : kind === "course"
                  ? "阅读正文，记录答案，再获得针对你的反馈。"
                  : "导入当天记录，或补充你希望记下的事情。"
            }
          >
            {kind === "review" && (
              <button onClick={() => void importChat()}>
                <Upload size={14} />
                导入聊天记录
              </button>
            )}
          </Empty>
        )}
      </div>
      {notes && currentItem && (
        <aside className="notes-panel">
          <header>
            <div className="segmented">
              <button
                className={noteTab === "note" ? "selected" : ""}
                onClick={() => setNoteTab("note")}
              >
                我的笔记
              </button>
              {kind === "course" && (
                <button
                  className={noteTab === "feedback" ? "selected" : ""}
                  onClick={() => setNoteTab("feedback")}
                >
                  反馈
                </button>
              )}
            </div>
            <button
              className="icon-button"
              aria-label="收起笔记"
              onClick={() => setNotes(false)}
            >
              <PanelRightClose size={16} />
            </button>
          </header>
          {kind === "course" && (
            <div className="course-actions">
              <label>
                <input
                  type="checkbox"
                  checked={!!currentItem.learned}
                  onChange={(event) =>
                    void rpc("course.learned", {
                      lesson_id: currentItem.lesson_id,
                      learned: event.target.checked,
                    })
                      .then(() => load())
                      .catch((err) => setError(errorText(err)))
                  }
                />
                已学完本节
              </label>
              <button
                className="quiet-button"
                disabled={busy || !currentItem.note_path}
                onClick={() => void run("feedback")}
              >
                获取反馈
              </button>
            </div>
          )}
          {(
            noteTab === "feedback"
              ? currentItem.feedback_path
              : currentItem.note_path
          ) ? (
            <DocumentView
              key={`${currentItem.path}-${noteTab}`}
              compact
              path={
                (noteTab === "feedback"
                  ? currentItem.feedback_path
                  : currentItem.note_path)!
              }
              notify={notify}
              onChanged={() => void load()}
            />
          ) : (
            <Empty
              title={noteTab === "feedback" ? "还没有反馈" : "还没有笔记入口"}
              description={
                noteTab === "feedback"
                  ? "保存答案后，可以请求反馈。"
                  : undefined
              }
            />
          )}
        </aside>
      )}
      <Modal
        open={add}
        onClose={() => setAdd(false)}
        title="补充当天记录"
        description="记录会作为个人提供的复盘来源保存在本机。"
      >
        <label className="stack-field">
          日期
          <input
            type="date"
            value={evidenceDate}
            onChange={(event) => setEvidenceDate(event.target.value)}
          />
          <small>留空使用配置时区的今天。</small>
        </label>
        <label className="stack-field">
          完成了什么、学到了什么
          <textarea
            autoFocus
            rows={8}
            value={evidence}
            onChange={(event) => setEvidence(event.target.value)}
            placeholder="写下真实发生的事情，也可以单独说明讨论和计划…"
          />
        </label>
        <div className="modal-actions">
          <button onClick={() => setAdd(false)}>取消</button>
          <button
            className="primary"
            disabled={!evidence.trim() || busy}
            onClick={() => void addEvidence()}
          >
            保存记录
          </button>
        </div>
      </Modal>
    </div>
  );
}
