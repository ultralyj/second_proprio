import { useCallback, useEffect, useRef, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import {
  Search,
  Pencil,
  BookOpen,
  Columns2,
  Save,
  History,
  MoreHorizontal,
  NotebookPen,
  Check,
  ArrowLeft,
  GitCompareArrows,
  X,
} from "lucide-react";
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { rpc, RpcError, errorText, formatTime, openPath } from "../api";
import type { Candidate, ContentItem, Document, Revision } from "../api";
import { Markdown } from "./Markdown";
import { Empty, ErrorNotice, Loading, Modal } from "./UI";
interface Props {
  path: string;
  item?: ContentItem;
  onBack?: () => void;
  onChanged?: () => void;
  onNotes?: () => void;
  compact?: boolean;
  notify: (message: string) => void;
}
export default function DocumentView({
  path,
  item,
  onBack,
  onChanged,
  onNotes,
  compact = false,
  notify,
}: Props) {
  const view = useRef<HTMLDivElement>(null);
  const [doc, setDoc] = useState<Document | null>(null);
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"read" | "edit" | "split">("read");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [draftStatus, setDraftStatus] = useState("");
  const [history, setHistory] = useState<Revision[] | null>(null);
  const [revision, setRevision] = useState<Revision | null>(null);
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [candidateText, setCandidateText] = useState("");
  const [conflict, setConflict] = useState<Document | null>(null);
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const loaded = useRef("");
  const current = useRef({ text: "", hash: null as string | null, path: "" });
  const refresh = useCallback(async () => {
    setError("");
    try {
      const value = await rpc<Document>("content.read", { path });
      setDoc(value);
      setText(value.content);
      loaded.current = value.content;
      current.current = { text: value.content, hash: value.hash, path };
      setDraftStatus("");
    } catch (err) {
      setError(errorText(err));
    }
  }, [path]);
  useEffect(() => {
    setDoc(null);
    setMode("read");
    setHistory(null);
    setCandidate(null);
    setSearching(false);
    void refresh();
  }, [refresh]);
  useEffect(() => {
    current.current = { text, hash: doc?.hash ?? null, path };
    if (!doc || mode === "read" || text === loaded.current) return;
    setDraftStatus("正在保存草稿…");
    const timeout = setTimeout(() => {
      rpc("content.draft", { path, content: text, base_hash: doc.hash })
        .then(() => setDraftStatus("草稿已保存"))
        .catch((err) => setDraftStatus(`草稿未保存：${errorText(err)}`));
    }, 700);
    return () => clearTimeout(timeout);
  }, [text, doc, mode, path]);
  // 离开文档时保留最后一次输入，显式保存才修改正式正文。
  useEffect(
    () => () => {
      const value = current.current;
      if (value.path && value.text !== loaded.current)
        void rpc("content.draft", {
          path: value.path,
          content: value.text,
          base_hash: value.hash,
        }).catch(() => {});
    },
    [path],
  );
  const save = useCallback(async () => {
    if (!doc || saving) return;
    setSaving(true);
    setError("");
    try {
      const value = await rpc<Document>("content.save", {
        path,
        content: text,
        base_hash: doc.hash,
      });
      setDoc(value);
      setText(value.content);
      loaded.current = value.content;
      setDraftStatus("正文已保存");
      notify("正文已保存");
      onChanged?.();
    } catch (err) {
      if (err instanceof RpcError && err.code === "conflict") {
        const current = err.details as Document | undefined;
        if (current && typeof current.content === "string")
          setConflict(current);
        else
          void rpc<Document>("content.read", { path })
            .then(setConflict)
            .catch(() => {});
      }
      setError(errorText(err) + "。你的编辑已保留为草稿，请先核对磁盘版本。");
      void rpc("content.draft", {
        path,
        content: text,
        base_hash: doc.hash,
      }).catch(() => {});
    } finally {
      setSaving(false);
    }
  }, [doc, saving, path, text, notify, onChanged]);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const focused = document.activeElement?.closest(".document-view");
      if (focused ? focused !== view.current : compact) return;
      if ((event.metaKey || event.ctrlKey) && event.key === "s") {
        event.preventDefault();
        if (mode !== "read") void save();
      }
      if (
        (event.metaKey || event.ctrlKey) &&
        event.key === "f" &&
        mode === "read"
      ) {
        event.preventDefault();
        setSearching(true);
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [save, mode, compact]);
  async function showHistory() {
    try {
      const result = await rpc<{ items: Revision[] }>("content.history", {
        path,
      });
      setHistory(result.items);
    } catch (err) {
      setError(errorText(err));
    }
  }
  async function restore(value: Revision) {
    if (!doc) return;
    try {
      await rpc("content.restore", {
        path,
        revision: value.id,
        base_hash: doc.hash,
      });
      setHistory(null);
      setRevision(null);
      await refresh();
      notify("已恢复为新的修订");
      onChanged?.();
    } catch (err) {
      setError(errorText(err));
    }
  }
  async function accept() {
    if (!candidate || !doc) return;
    try {
      await rpc("content.accept", {
        candidate_id: candidate.id,
        path,
        base_hash: doc.hash,
        content: candidateText,
      });
      setCandidate(null);
      await refresh();
      onChanged?.();
      notify("候选版本已采纳");
    } catch (err) {
      setError(errorText(err));
    }
  }
  const file =
    candidate?.files.find((value) => value.path === path) ||
    candidate?.files[0];
  const matches = query
    ? text
        .split("\n")
        .map((line, index) => ({ line, index }))
        .filter((value) =>
          value.line.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
        )
        .slice(0, 30)
    : [];
  return (
    <div ref={view} className={`document-view ${compact ? "compact" : ""}`}>
      <div className="document-toolbar">
        <div className="toolbar-group">
          {onBack && (
            <button
              className="icon-button mobile-back"
              onClick={onBack}
              aria-label="返回列表"
            >
              <ArrowLeft size={17} />
            </button>
          )}
          <span className="document-label">
            {compact ? "笔记" : item?.title || path.split("/").pop()}
          </span>
          {doc?.manual_modified && (
            <span className="subtle-tag">已手动修改</span>
          )}
        </div>
        <div className="toolbar-group">
          {mode === "read" ? (
            <button
              className="quiet-button"
              disabled={!doc?.editable}
              onClick={() => setMode("edit")}
            >
              <Pencil size={14} />
              编辑
            </button>
          ) : (
            <>
              <div className="segmented">
                <button
                  aria-label="源码编辑"
                  className={mode === "edit" ? "selected" : ""}
                  onClick={() => setMode("edit")}
                >
                  <Pencil size={14} />
                </button>
                <button
                  aria-label="双栏预览"
                  className={mode === "split" ? "selected" : ""}
                  onClick={() => setMode("split")}
                >
                  <Columns2 size={14} />
                </button>
                <button aria-label="阅读" onClick={() => setMode("read")}>
                  <BookOpen size={14} />
                </button>
              </div>
              <button
                className="primary small"
                disabled={saving || text === doc?.content}
                onClick={() => void save()}
              >
                <Save size={13} />
                {saving ? "保存中" : "保存"}
              </button>
            </>
          )}
          {onNotes && (
            <button className="quiet-button" onClick={onNotes}>
              <NotebookPen size={14} />
              笔记
            </button>
          )}
          {!compact && (
            <>
              <button
                className="icon-button"
                aria-label="修订历史"
                onClick={() => void showHistory()}
              >
                <History size={16} />
              </button>
              <Dropdown.Root>
                <Dropdown.Trigger className="icon-button" aria-label="更多操作">
                  <MoreHorizontal size={18} />
                </Dropdown.Trigger>
                <Dropdown.Portal>
                  <Dropdown.Content className="dropdown" sideOffset={8}>
                    <Dropdown.Item onSelect={() => void openPath(path)}>
                      在外部应用中打开
                    </Dropdown.Item>
                    <Dropdown.Item
                      onSelect={() => {
                        setSearching(true);
                        setMode("read");
                      }}
                    >
                      查找正文
                    </Dropdown.Item>
                    {item?.example_path && (
                      <Dropdown.Item
                        onSelect={() => void openPath(item.example_path!)}
                      >
                        打开示例源码
                      </Dropdown.Item>
                    )}
                    {item?.arxiv_id && (
                      <Dropdown.Item
                        onSelect={() =>
                          void openPath(
                            path.replace(/README\.md$/, "paper.pdf"),
                          )
                        }
                      >
                        打开原始 PDF
                      </Dropdown.Item>
                    )}
                    <Dropdown.Item onSelect={() => void refresh()}>
                      重新读取磁盘版本
                    </Dropdown.Item>
                  </Dropdown.Content>
                </Dropdown.Portal>
              </Dropdown.Root>
            </>
          )}
        </div>
      </div>
      {error && <ErrorNotice message={error} onDismiss={() => setError("")} />}
      {doc?.draft && doc.draft.content !== doc.content && mode === "read" && (
        <div className="inline-notice">
          有一份未提交的编辑草稿。
          <button
            onClick={() => {
              setText(doc.draft!.content);
              setMode("edit");
            }}
          >
            继续编辑
          </button>
        </div>
      )}
      {!!doc?.candidates?.filter(
        (value) =>
          value.status === "pending" || value.status === "waiting_review",
      ).length && (
        <div className="inline-notice">
          <GitCompareArrows size={15} />有 AI 候选版本，当前正文已保留。
          <button
            onClick={() => {
              const value = doc.candidates.find(
                (candidate) =>
                  candidate.status === "pending" ||
                  candidate.status === "waiting_review",
              )!;
              setCandidate(value);
              setCandidateText(
                value.files.find((file) => file.path === path)?.content ||
                  value.files[0]?.content ||
                  "",
              );
            }}
          >
            对比版本
          </button>
        </div>
      )}
      {searching && (
        <div className="find-bar">
          <Search size={14} />
          <input
            autoFocus
            placeholder="在正文中查找"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <span>
            {matches.length}
            {matches.length === 30 ? "+" : ""} 处
          </span>
          <button
            className="icon-button"
            aria-label="结束查找"
            onClick={() => setSearching(false)}
          >
            <X size={14} />
          </button>
        </div>
      )}
      {searching && query && (
        <div className="find-results">
          {matches.length ? (
            matches.map((match) => (
              <div key={match.index}>
                <small>第 {match.index + 1} 行</small>
                {match.line}
              </div>
            ))
          ) : (
            <span>未找到匹配内容</span>
          )}
        </div>
      )}
      {!doc && !error ? (
        <Loading />
      ) : doc ? (
        <>
          <div className={`document-body ${mode}`}>
            {mode !== "read" && (
              <div className="editor">
                <CodeMirror
                  value={text}
                  extensions={[markdown()]}
                  theme={
                    document.documentElement.dataset.theme === "dark"
                      ? "dark"
                      : "light"
                  }
                  onChange={setText}
                  basicSetup={{
                    lineNumbers: !compact,
                    foldGutter: false,
                    highlightActiveLine: false,
                  }}
                  height="100%"
                  aria-label="Markdown 编辑器"
                />
              </div>
            )}
            {mode !== "edit" && (
              <div className="reading-scroll">
                <Markdown
                  content={mode === "read" ? doc.content : text}
                  path={path}
                />
              </div>
            )}
          </div>
          {mode !== "read" && (
            <div className="editor-footer">
              <span>{draftStatus || "编辑模式"}</span>
              <span>{text.length.toLocaleString()} 字符 · ⌘S 保存正文</span>
            </div>
          )}
        </>
      ) : (
        <Empty
          title="无法读取内容"
          description="文件可能已移动，或后台暂时无法连接。"
        />
      )}
      <Modal
        open={!!conflict}
        onClose={() => setConflict(null)}
        title="正文发生了变化"
        description="你的编辑已保留。核对磁盘版本后，选择继续合并或明确保存你的版本。"
        wide
      >
        <div className="compare">
          <section>
            <h3>当前磁盘版本</h3>
            <pre>{conflict?.content}</pre>
          </section>
          <section>
            <h3>我的编辑</h3>
            <pre>{text}</pre>
          </section>
        </div>
        <div className="modal-actions">
          <button onClick={() => setConflict(null)}>保留草稿，稍后处理</button>
          <button
            onClick={() => {
              if (conflict) {
                setDoc(conflict);
                loaded.current = conflict.content;
                setConflict(null);
                setError("");
                setMode("split");
              }
            }}
          >
            以最新版本为基准继续编辑
          </button>
        </div>
      </Modal>
      <Modal
        open={history !== null}
        onClose={() => {
          setHistory(null);
          setRevision(null);
        }}
        title="修订历史"
        description="恢复会创建一个新修订，不删除已有历史。"
        wide
      >
        <div className="history-layout">
          <div className="history-list">
            {history?.length ? (
              history.map((value) => (
                <button
                  key={value.id}
                  className={revision?.id === value.id ? "selected" : ""}
                  onClick={() => setRevision(value)}
                >
                  <strong>{formatTime(value.created_at)}</strong>
                  <small>
                    {value.author === "user" ? "手动编辑" : value.author} ·{" "}
                    {value.hash.slice(0, 8)}
                  </small>
                </button>
              ))
            ) : (
              <p className="muted">还没有保存的修订。</p>
            )}
          </div>
          <div className="history-content">
            {revision ? (
              <>
                <button
                  className="primary small"
                  onClick={() => void restore(revision)}
                >
                  <History size={13} />
                  恢复此版本
                </button>
                <Markdown content={revision.content} path={path} />
              </>
            ) : (
              <Empty title="选择一个修订" />
            )}
          </div>
        </div>
      </Modal>
      <Modal
        open={!!candidate}
        onClose={() => setCandidate(null)}
        title="对比 AI 候选版本"
        description={candidate?.summary || "保留当前正文，核对差异后再采纳。"}
        wide
      >
        <div className="compare">
          <section>
            <h3>当前正文</h3>
            <pre>{doc?.content}</pre>
          </section>
          <section>
            <h3>候选版本 · 可编辑</h3>
            <CodeMirror
              value={candidateText}
              onChange={setCandidateText}
              extensions={[markdown()]}
              height="400px"
            />
          </section>
        </div>
        {candidate && candidate.files.length > 1 && (
          <p className="muted">
            此候选包含 {candidate.files.length} 个关联文件，采纳时一起发布。
            {file?.path}
          </p>
        )}
        {candidate?.files
          .filter((value) => value.path !== path)
          .map((value) => (
            <details key={value.path} className="candidate-file">
              <summary>关联文件：{value.path}</summary>
              <pre>{value.content}</pre>
            </details>
          ))}
        <div className="modal-actions">
          <button
            className="danger-text"
            onClick={() => {
              if (candidate)
                void rpc("content.reject", { candidate_id: candidate.id })
                  .then(() => {
                    setCandidate(null);
                    void refresh();
                  })
                  .catch((err) => setError(errorText(err)));
            }}
          >
            放弃候选
          </button>
          <button onClick={() => setCandidate(null)}>稍后处理</button>
          <button className="primary" onClick={() => void accept()}>
            <Check size={14} />
            采纳此版本
          </button>
        </div>
      </Modal>
    </div>
  );
}
