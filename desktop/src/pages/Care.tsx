/** 展示纪念天数并编辑指定日期的私人记录，爱心动画不写入记录。 */
import { useEffect, useRef, useState } from "react";
import { Plus, Trash2, Heart, ChevronLeft, ChevronRight } from "lucide-react";
import { rpc, errorText } from "../api";
import type { Relationship } from "../api";
import type { CSSProperties } from "react";
import "./personal.css";
import { ErrorNotice, Field, Loading, Section } from "../components/UI";

type HeartParticle = {
  x: number;
  y: number;
  size: number;
  turn: number;
  delay: number;
};

export default function Care({
  notify,
}: {
  notify: (message: string) => void;
}) {
  const [date, setDate] = useState("");
  const [record, setRecord] = useState<Relationship | null>(null);
  const visibleDate = useRef<string | null>(null);
  visibleDate.current = record?.date ?? null;
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [action, setAction] = useState("");
  const [busy, setBusy] = useState(false);
  const [heartClicks, setHeartClicks] = useState(0);
  const [heartBurst, setHeartBurst] = useState(false);
  const [heartParticles, setHeartParticles] = useState<HeartParticle[]>([]);
  useEffect(() => {
    if (!heartClicks) return;
    setHeartBurst(true);
    const timer = setTimeout(() => setHeartBurst(false), 900);
    return () => clearTimeout(timer);
  }, [heartClicks]);
  useEffect(() => {
    let current = true;
    setRecord(null);
    setAction("");
    // 快速切换日期时，较晚返回的旧请求不得替换当前日期。
    void rpc<Relationship>("relationship.get", date ? { date } : {})
      .then((value) => {
        if (!current) return;
        setRecord(value);
        setSaved(JSON.stringify(value));
        setError("");
      })
      .catch((err) => {
        if (current) setError(errorText(err));
      });
    return () => {
      current = false;
    };
  }, [date]);
  async function save() {
    if (!record) return;
    const submitted = JSON.stringify(record);
    setBusy(true);
    try {
      const value = await rpc<Relationship>("relationship.save", {
        date: record.date,
        mood: record.mood,
        note: record.note,
        actions: record.actions,
      });
      if (visibleDate.current !== record.date) return;
      setRecord((latest) => {
        if (!latest || latest.date !== record.date) return latest;
        return JSON.stringify(latest) === submitted
          ? value
          : { ...latest, history_dates: value.history_dates };
      });
      setSaved(JSON.stringify(value));
      notify("关心记录已保存");
    } catch (err) {
      if (visibleDate.current === record.date) setError(errorText(err));
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
  function lightHeart() {
    const count = 4 + Math.floor(Math.random() * 7);
    setHeartParticles(
      Array.from({ length: count }, () => {
        const angle = Math.random() * Math.PI * 2;
        const distance = 36 + Math.random() * 46;
        return {
          x: Math.cos(angle) * distance,
          y: Math.sin(angle) * distance,
          size: 8 + Math.random() * 6,
          turn: Math.random() * 110 - 55,
          delay: Math.random() * 100,
        };
      }),
    );
    setHeartClicks((value) => value + 1);
  }
  return (
    <div className="page-scroll narrow care-page">
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
            <button
              className={`care-heart-button ${heartClicks ? "is-loved" : ""}`}
              aria-label="点亮爱心"
              aria-pressed={heartClicks > 0}
              onClick={lightHeart}
            >
              <Heart
                key={`heart-${heartClicks}`}
                className={heartBurst ? "care-heart-bounce" : ""}
                size={30}
                strokeWidth={1.4}
              />
              {heartBurst && (
                <span
                  key={heartClicks}
                  className="care-heart-burst"
                  aria-hidden="true"
                >
                  {heartParticles.map((particle, index) => (
                    <Heart
                      key={index}
                      className="care-heart-particle"
                      size={particle.size}
                      fill="currentColor"
                      style={
                        {
                          "--heart-x": `${particle.x}px`,
                          "--heart-y": `${particle.y}px`,
                          "--heart-turn": `${particle.turn}deg`,
                          "--heart-delay": `${particle.delay}ms`,
                          left: `calc(50% - ${particle.size / 2}px)`,
                          top: `calc(50% - ${particle.size / 2}px)`,
                        } as CSSProperties
                      }
                    />
                  ))}
                </span>
              )}
            </button>
            <span>与 {record.name} 在一起</span>
            <strong>
              {record.days.toLocaleString()}
              <small>天</small>
            </strong>
            <p>{record.date}</p>
          </div>
          <Section title="今天的状态">
            <Field label="心情指数">
              <div className="mood-selector">
                {[1, 2, 3, 4, 5].map((value) => (
                  <button
                    key={value}
                    data-mood={value}
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
