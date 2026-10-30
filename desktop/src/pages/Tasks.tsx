import { useCallback, useEffect, useState } from "react";
import { Pause, Play, RotateCcw, Square, ChevronRight } from "lucide-react";
import { rpc, errorText, kindNames, formatTime } from "../api";
import type { Task, Candidate, Page } from "../api";
import { Empty, ErrorNotice, Loading, Modal, Status } from "../components/UI";
export default function Tasks({
  notify,
  navigate,
}: {
  notify: (message: string) => void;
  navigate?: (page: Page, path?: string) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<Task | null>(null);
  const [events, setEvents] = useState<Record<string, unknown>[]>([]);
  const [busy, setBusy] = useState(false);
  const [cancel, setCancel] = useState<Task | null>(null);
  const load = useCallback(async () => {
    try {
      const value = await rpc<{ tasks: Task[]; paused: boolean }>("tasks.list");
      setTasks(value.tasks);
      setPaused(value.paused);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [load]);
  async function action(
    method: string,
    params: Record<string, unknown>,
    message: string,
  ) {
    setBusy(true);
    try {
      await rpc(method, params);
      notify(message);
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  function detail(task: Task) {
    setEvents([]);
    setCandidates([]);
    setSelected(task);
  }
  const selectedId = selected?.id;
  useEffect(() => {
    if (!selectedId) return;
    let mounted = true;
    async function refreshDetail() {
      try {
        const [detailResult, candidateResult] = await Promise.all([
          rpc<{ task: Task; events: Record<string, unknown>[] }>(
            "tasks.detail",
            { id: selectedId },
          ),
          rpc<{ items: Candidate[] }>("content.candidates"),
        ]);
        if (!mounted) return;
        setSelected(detailResult.task);
        setEvents(detailResult.events || []);
        setCandidates(
          candidateResult.items.filter(
            (candidate) => candidate.task_id === selectedId,
          ),
        );
      } catch (err) {
        if (mounted) setError(errorText(err));
      }
    }
    void refreshDetail();
    const timer = setInterval(() => void refreshDetail(), 2000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [selectedId]);
  const elapsed = selected?.started_at
    ? Math.max(
        0,
        Math.floor(
          ((selected.finished_at
            ? Date.parse(selected.finished_at)
            : Date.now()) -
            Date.parse(selected.started_at)) /
            1000,
        ),
      )
    : null;
  const active = new Set(["pending", "running", "cancel_requested"]);
  const attention = new Set(["failed", "waiting_source", "waiting_review"]);
  const visible = tasks
    .filter(
      (task) =>
        filter === "all" ||
        (filter === "active" && active.has(task.status)) ||
        (filter === "attention" && attention.has(task.status)) ||
        (filter === "completed" &&
          ["completed", "skipped", "cancelled"].includes(task.status)),
    )
    .sort(
      (a, b) =>
        Number(active.has(b.status)) - Number(active.has(a.status)) ||
        b.scheduled_at.localeCompare(a.scheduled_at),
    );
  return (
    <div className="page-scroll">
      <header className="page-heading">
        <div>
          <h1>任务</h1>
          <p>查看执行过程，决定下一步。</p>
        </div>
        <button
          disabled={busy}
          onClick={() =>
            void action(
              "tasks.pause",
              { paused: !paused },
              paused ? "AI 任务已恢复" : "已暂停后续 AI 任务；当前任务继续",
            )
          }
        >
          {paused ? <Play size={14} /> : <Pause size={14} />}{" "}
          {paused ? "恢复 AI 任务" : "暂停 AI 任务"}
        </button>
      </header>
      {error && <ErrorNotice message={error} onDismiss={() => setError("")} />}
      {paused && (
        <div className="inline-notice">
          后续 AI 任务已暂停。当前任务继续，关心提醒独立运行。
        </div>
      )}
      <div className="tabs">
        {[
          ["all", "全部"],
          ["active", "进行中"],
          ["attention", "需要处理"],
          ["completed", "历史"],
        ].map(([key, label]) => (
          <button
            key={key}
            className={filter === key ? "selected" : ""}
            onClick={() => setFilter(key)}
          >
            {label}
            <span>
              {
                tasks.filter(
                  (task) =>
                    key === "all" ||
                    (key === "active" && active.has(task.status)) ||
                    (key === "attention" && attention.has(task.status)) ||
                    (key === "completed" &&
                      ["completed", "skipped", "cancelled"].includes(
                        task.status,
                      )),
                ).length
              }
            </span>
          </button>
        ))}
      </div>
      {loading ? (
        <Loading />
      ) : visible.length ? (
        <div className="task-list">
          {visible.map((task) => (
            <div className="task-row" key={task.id}>
              <button className="task-main" onClick={() => void detail(task)}>
                <div className="task-title">
                  <strong>{kindNames[task.kind] || task.kind}</strong>
                  <Status value={task.status} />
                </div>
                <p>
                  {task.stage ||
                    task.result?.summary ||
                    task.last_error ||
                    "等待后台执行"}
                </p>
                <div className="row-meta">
                  <span>计划 {formatTime(task.scheduled_at)}</span>
                  {task.retry_at && (
                    <span>重试 {formatTime(task.retry_at)}</span>
                  )}
                  <span>执行 {task.attempts} 次</span>
                </div>
              </button>
              <div className="task-actions">
                {["failed", "cancelled", "waiting_source"].includes(
                  task.status,
                ) && (
                  <button
                    className="quiet-button"
                    disabled={busy}
                    onClick={() =>
                      void action(
                        "tasks.retry",
                        { id: task.id },
                        "选中任务已重新入队",
                      )
                    }
                  >
                    <RotateCcw size={13} />
                    重试
                  </button>
                )}
                {["pending", "running"].includes(task.status) && (
                  <button
                    className="quiet-button"
                    disabled={busy}
                    onClick={() => setCancel(task)}
                  >
                    <Square size={12} />
                    取消
                  </button>
                )}
                <button
                  className="icon-button"
                  aria-label="任务详情"
                  onClick={() => void detail(task)}
                >
                  <ChevronRight size={17} />
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <Empty
          title="这里暂时没有任务"
          description="运行任务后，执行过程会显示在这里。"
        />
      )}
      <Modal
        open={!!selected}
        onClose={() => setSelected(null)}
        title={selected ? `${kindNames[selected.kind]} · 任务详情` : "任务详情"}
        description={selected?.id}
        wide
      >
        {selected && (
          <>
            <div className="detail-grid">
              <span>状态</span>
              <Status value={selected.status} />
              <span>计划时间</span>
              <strong>{formatTime(selected.scheduled_at)}</strong>
              <span>开始时间</span>
              <strong>{formatTime(selected.started_at)}</strong>
              <span>当前阶段</span>
              <strong>{selected.stage || "—"}</strong>
              <span>耗时</span>
              <strong>
                {elapsed === null || !Number.isFinite(elapsed)
                  ? "—"
                  : `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`}
              </strong>
              <span>执行次数 / 失败次数</span>
              <strong>
                {selected.attempts} /{" "}
                {selected.failures ?? selected.failure_attempts ?? 0}
              </strong>
              <span>下次重试</span>
              <strong>{formatTime(selected.retry_at)}</strong>
            </div>
            {selected.last_error && (
              <ErrorNotice message={selected.last_error} />
            )}
            {!!selected.result?.artifacts?.length && (
              <div className="task-artifacts">
                <h3>任务成果</h3>
                {selected.result.artifacts.map((path) => (
                  <button
                    key={path}
                    onClick={() => {
                      setSelected(null);
                      navigate?.(
                        selected.kind === "care" ? "care" : selected.kind,
                        path,
                      );
                    }}
                  >
                    {path.split("/").pop()}
                  </button>
                ))}
              </div>
            )}
            {candidates.map((candidate) => (
              <div key={candidate.id} className="inline-notice">
                {candidate.summary || "有候选版本待采纳"}
                <button
                  onClick={() => {
                    const path = candidate.files.find((file) =>
                      file.path.endsWith(".md"),
                    )?.path;
                    setSelected(null);
                    navigate?.(
                      selected.kind === "care" ? "care" : selected.kind,
                      path,
                    );
                  }}
                >
                  查看候选
                </button>
              </div>
            ))}
            <h3>执行记录</h3>
            <div className="event-log">
              {events.length ? (
                events.map((event, index) => (
                  <div key={index}>
                    <time>
                      {formatTime(String(event.at || event.timestamp || ""))}
                    </time>
                    <strong>{String(event.stage || "")}</strong>
                    <span>{String(event.message || "")}</span>
                  </div>
                ))
              ) : (
                <p className="muted">暂无结构化事件记录。</p>
              )}
            </div>
          </>
        )}
      </Modal>
      <Modal
        open={!!cancel}
        onClose={() => setCancel(null)}
        title="取消这项任务？"
        description="已验收成果保留。当前未完成内容保留为草稿，取消后不会自动重试。"
      >
        <div className="modal-actions">
          <button onClick={() => setCancel(null)}>继续运行</button>
          <button
            className="danger"
            onClick={() => {
              if (cancel)
                void action(
                  "tasks.cancel",
                  { id: cancel.id },
                  "已请求取消，等待进程退出",
                );
              setCancel(null);
            }}
          >
            取消任务
          </button>
        </div>
      </Modal>
    </div>
  );
}
