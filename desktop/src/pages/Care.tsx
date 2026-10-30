import { useCallback, useEffect, useState } from "react";
import { Plus, Trash2, Heart, ChevronLeft, ChevronRight } from "lucide-react";
import { rpc, errorText } from "../api";
import type { Relationship } from "../api";
import { ErrorNotice, Field, Loading, Section } from "../components/UI";
export default function Care({
  notify,
}: {
  notify: (message: string) => void;
}) {
  const [date, setDate] = useState("");
  const [record, setRecord] = useState<Relationship | null>(null);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [action, setAction] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const value = await rpc<Relationship>(
        "relationship.get",
        date ? { date } : {},
      );
      setRecord(value);
      setSaved(JSON.stringify(value));
      setError("");
    } catch (err) {
      setError(errorText(err));
    }
  }, [date]);
  useEffect(() => {
    void load();
  }, [load]);
  async function save() {
    if (!record) return;
    setBusy(true);
    try {
      const value = await rpc<Relationship>("relationship.save", {
        date: record.date,
        mood: record.mood,
        note: record.note,
        actions: record.actions,
      });
      setRecord(value);
      setSaved(JSON.stringify(value));
      notify("关心记录已保存");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  function step(days: number) {
    if (!record) return;
    const value = new Date(record.date + "T12:00:00Z");
    value.setUTCDate(value.getUTCDate() + days);
    setDate(value.toISOString().slice(0, 10));
  }
  return (
    <div className="page-scroll narrow">
      <header className="page-heading">
        <div>
          <h1>关心</h1>
          <p>记住小事，也留一点时间认真听。</p>
        </div>
        <div className="toolbar-group">
          <button
            className="icon-button"
            aria-label="前一天"
            onClick={() => step(-1)}
          >
            <ChevronLeft size={16} />
          </button>
          <input
            className="date-control"
            aria-label="查看日期"
            type="date"
            value={record?.date || date}
            onChange={(event) => setDate(event.target.value)}
          />
          <button
            className="icon-button"
            aria-label="后一天"
            onClick={() => step(1)}
          >
            <ChevronRight size={16} />
          </button>
          <button className="quiet-button" onClick={() => setDate("")}>
            今天
          </button>
        </div>
      </header>
      {error && <ErrorNotice message={error} onDismiss={() => setError("")} />}
      {!record ? (
        <Loading />
      ) : (
        <>
          <div className="relationship-hero">
            <Heart size={24} strokeWidth={1.2} />
            <span>与 {record.name} 在一起</span>
            <strong>
              {record.days.toLocaleString()}
              <small>天</small>
            </strong>
            <p>{record.date}</p>
          </div>
          <Section
            title="今天的状态"
            description="由你手动填写；未填写时不会推测，也不会沿用昨天的分数。"
          >
            <Field label="心情指数">
              <div className="mood-selector">
                {[1, 2, 3, 4, 5].map((value) => (
                  <button
                    key={value}
                    aria-label={`心情 ${value} 分`}
                    aria-pressed={record.mood === value}
                    className={record.mood === value ? "selected" : ""}
                    onClick={() => setRecord({ ...record, mood: value })}
                  >
                    {value}
                  </button>
                ))}
                <button
                  className="quiet-button"
                  onClick={() => setRecord({ ...record, mood: null })}
                >
                  {record.mood === null ? "待填写" : "清空"}
                </button>
              </div>
            </Field>
            <label className="stack-field">
              备注
              <textarea
                rows={3}
                value={record.note}
                onChange={(event) =>
                  setRecord({ ...record, note: event.target.value })
                }
                placeholder="她今天分享了什么？有什么值得记住？"
              />
            </label>
          </Section>
          <Section title="今天已经关心过什么">
            <div className="action-list">
              {record.actions.length ? (
                record.actions.map((value, index) => (
                  <div key={index}>
                    <input
                      aria-label={`关心行动 ${index + 1}`}
                      value={value}
                      onChange={(event) =>
                        setRecord({
                          ...record,
                          actions: record.actions.map((action, i) =>
                            i === index ? event.target.value : action,
                          ),
                        })
                      }
                    />
                    <button
                      className="icon-button"
                      aria-label={`删除行动 ${index + 1}`}
                      onClick={() =>
                        setRecord({
                          ...record,
                          actions: record.actions.filter((_, i) => i !== index),
                        })
                      }
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))
              ) : (
                <p className="muted">还没有记录。</p>
              )}
            </div>
            <form
              className="add-action"
              onSubmit={(event) => {
                event.preventDefault();
                if (action.trim()) {
                  setRecord({
                    ...record,
                    actions: [...record.actions, action.trim()],
                  });
                  setAction("");
                }
              }}
            >
              <input
                value={action}
                onChange={(event) => setAction(event.target.value)}
                placeholder="记下一件小事"
                aria-label="新增关心行动"
              />
              <button disabled={!action.trim()}>
                <Plus size={14} />
                添加
              </button>
            </form>
          </Section>
          <div className="form-footer">
            <span className="muted">这些记录只保存在本机。</span>
            <button
              className="primary"
              disabled={busy || saved === JSON.stringify(record)}
              onClick={() => void save()}
            >
              {busy ? "保存中…" : "保存记录"}
            </button>
          </div>
          {record.history_dates.length > 0 && (
            <div className="history-dates">
              <span>最近记录</span>
              {record.history_dates
                .slice(-10)
                .reverse()
                .map((value) => (
                  <button
                    key={value}
                    className="quiet-button"
                    onClick={() => setDate(value)}
                  >
                    {value.slice(5)}
                  </button>
                ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
