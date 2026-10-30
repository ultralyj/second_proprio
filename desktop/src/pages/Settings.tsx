import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Plus,
  Trash2,
  FolderOpen,
  ShieldCheck,
  Power,
} from "lucide-react";
import { rpc, errorText, checkLogin, openPath, serviceControl } from "../api";
import type { Config, ConfigResponse, Kind, Schedule, Task } from "../api";
import {
  ErrorNotice,
  Field,
  Loading,
  Modal,
  Section,
  Toggle,
} from "../components/UI";
function ListInput({
  value,
  onChange,
  label,
  placeholder,
}: {
  value: string[];
  onChange: (value: string[]) => void;
  label: string;
  placeholder?: string;
}) {
  const [text, setText] = useState(value.join("\n"));
  const focused = useRef(false);
  const encoded = value.join("\n");
  useEffect(() => {
    if (!focused.current) setText(encoded);
  }, [encoded]);
  const parse = (raw: string) =>
    raw
      .split(/\n|[,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
  return (
    <textarea
      aria-label={label}
      rows={Math.min(5, Math.max(2, value.length))}
      value={text}
      placeholder={placeholder}
      onFocus={() => {
        focused.current = true;
      }}
      onChange={(event) => {
        setText(event.target.value);
        onChange(parse(event.target.value));
      }}
      onBlur={() => {
        focused.current = false;
        onChange(parse(text));
      }}
    />
  );
}
function ScheduleForm({
  value,
  onChange,
  label,
}: {
  value: Schedule;
  onChange: (value: Schedule) => void;
  label: string;
}) {
  return (
    <>
      <Field label={`自动${label}`}>
        <Toggle
          value={value.enabled !== false}
          onChange={(enabled) => onChange({ ...value, enabled })}
          label={`自动${label}`}
        />
      </Field>
      <Field label="运行日期">
        <div className="weekday-selector">
          {["一", "二", "三", "四", "五", "六", "日"].map((day, index) => (
            <button
              key={day}
              aria-label={`星期${day}`}
              aria-pressed={value.weekdays.includes(index)}
              className={value.weekdays.includes(index) ? "selected" : ""}
              onClick={() =>
                onChange({
                  ...value,
                  weekdays: value.weekdays.includes(index)
                    ? value.weekdays.filter((value) => value !== index)
                    : [...value.weekdays, index].sort(),
                })
              }
            >
              {day}
            </button>
          ))}
        </div>
      </Field>
      <Field label="运行时间" hint="每行一个，使用 HH:MM 格式">
        <ListInput
          label="运行时间"
          value={value.times}
          onChange={(times) => onChange({ ...value, times })}
          placeholder="08:00"
        />
      </Field>
    </>
  );
}
function describeServerSync(
  enabled: boolean,
  sync: Record<string, unknown> | undefined,
  readError: string,
): { text: string; detail?: string } {
  if (!enabled) return { text: "服务器未启用" };
  if (readError)
    return { text: "服务器状态暂不可用 · 本机继续运行", detail: readError };
  const current =
    typeof sync?.current_revision === "string" ? sync.current_revision : null;
  const remote =
    typeof sync?.remote_revision === "string" ? sync.remote_revision : null;
  const confirmed = Boolean(current && remote && current === remote);
  if (sync?.in_progress === true)
    return { text: "服务器同步中 · 等待后台确认" };
  if (sync?.status === "error") {
    const isEarlierAttempt =
      current && sync.desired_revision && sync.desired_revision !== current;
    const detail =
      typeof sync.error === "string"
        ? sync.error
        : "同步未完成，后台会继续尝试。";
    if (isEarlierAttempt)
      return {
        text: "服务器等待同步 · 当前配置尚未确认",
        detail: `上次同步失败：${detail}`,
      };
    return {
      text: confirmed
        ? "服务器同步失败 · 当前配置此前已确认"
        : remote
          ? "服务器同步失败 · 仍使用旧配置"
          : "服务器同步失败 · 尚未确认配置",
      detail,
    };
  }
  if (sync?.status === "ok" && confirmed) return { text: "服务器已同步" };
  return { text: "服务器等待同步 · 当前配置尚未确认" };
}
export default function Settings({
  notify,
  onSaved,
}: {
  notify: (message: string) => void;
  onSaved: () => void;
}) {
  const [response, setResponse] = useState<ConfigResponse | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [tab, setTab] = useState("general");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [stop, setStop] = useState(false);
  const [login, setLogin] = useState("");
  const [serverState, setServerState] = useState<{
    enabled: boolean;
    sync?: Record<string, unknown>;
  } | null>(null);
  const [syncReadError, setSyncReadError] = useState("");
  const syncEpoch = useRef(0);
  useEffect(() => {
    rpc<ConfigResponse>("config.get")
      .then((value) => {
        setResponse(value);
        setConfig(value.config);
        setServerState({
          enabled: value.config.server.enabled,
          sync: value.sync,
        });
      })
      .catch((err) => setError(errorText(err)));
  }, []);
  useEffect(() => {
    if (!response) return;
    let mounted = true;
    async function refreshSync() {
      const epoch = syncEpoch.current;
      try {
        const value = await rpc<ConfigResponse>("config.get");
        if (!mounted || epoch !== syncEpoch.current) return;
        setServerState({
          enabled: value.config.server.enabled,
          sync: value.sync,
        });
        setSyncReadError("");
      } catch (err) {
        if (mounted && epoch === syncEpoch.current)
          setSyncReadError(errorText(err));
      }
    }
    void refreshSync();
    const timer = setInterval(() => void refreshSync(), 2000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [response?.revision]);
  function update(edit: (value: Config) => void) {
    setConfig((previous) => {
      if (!previous) return previous;
      const value = structuredClone(previous);
      edit(value);
      return value;
    });
    setMessage("");
  }
  async function save() {
    if (!config || !response) return;
    setBusy(true);
    setError("");
    try {
      const value = await rpc<ConfigResponse>("config.save", {
        config,
        expected_revision: response.revision,
      });
      syncEpoch.current += 1;
      setResponse(value);
      setConfig(value.config);
      setServerState({
        enabled: value.config.server.enabled,
        sync: value.sync,
      });
      setSyncReadError("");
      setMessage("本机已保存 · 后续任务使用新配置");
      notify("配置已保存");
      onSaved();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  async function stopService() {
    setBusy(true);
    try {
      await rpc("tasks.pause", { paused: true });
      const value = await rpc<{ tasks: Task[] }>("tasks.list");
      for (const task of value.tasks.filter(
        (task) => task.status === "running",
      ))
        await rpc("tasks.cancel", { id: task.id });
      for (let index = 0; index < 60; index++) {
        const value = await rpc<{ tasks: Task[] }>("tasks.list");
        if (
          !value.tasks.some((task) =>
            ["running", "cancel_requested"].includes(task.status),
          )
        ) {
          await serviceControl("stop");
          setStop(false);
          notify("后台已停止");
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      throw new Error("取消仍在处理中，后台尚未停止。稍后再试。");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  const schedule = (kind: Kind, label: string) =>
    config && (
      <ScheduleForm
        value={config.schedules[kind]}
        label={label}
        onChange={(value) =>
          update((config) => {
            config.schedules[kind] = value;
          })
        }
      />
    );
  const dirty =
    config &&
    response &&
    JSON.stringify(config) !== JSON.stringify(response.config);
  const serverStatus = serverState
    ? describeServerSync(serverState.enabled, serverState.sync, syncReadError)
    : { text: "正在读取服务器状态" };
  return (
    <div className="settings-layout">
      <aside className="settings-nav">
        <h1>设置</h1>
        {[
          ["general", "通用"],
          ["papers", "论文"],
          ["course", "课程"],
          ["review", "复盘"],
          ["care", "关心"],
          ["advanced", "高级"],
        ].map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "selected" : ""}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </aside>
      <div className="settings-main">
        <div className="settings-scroll">
          {error && (
            <ErrorNotice message={error} onDismiss={() => setError("")} />
          )}
          {!config ? (
            <Loading />
          ) : (
            <>
              {tab === "general" && (
                <>
                  <Section title="通用">
                    <Field label="外观">
                      <select
                        value={config.desktop?.appearance || "system"}
                        onChange={(event) =>
                          update((config) => {
                            config.desktop = {
                              ...config.desktop,
                              appearance: event.target.value as
                                "system" | "light" | "dark",
                            };
                          })
                        }
                      >
                        <option value="system">跟随系统</option>
                        <option value="light">浅色</option>
                        <option value="dark">深色</option>
                      </select>
                    </Field>
                    <Field label="时区">
                      <input
                        value={config.timezone}
                        onChange={(event) =>
                          update((config) => {
                            config.timezone = event.target.value;
                          })
                        }
                      />
                    </Field>
                    <Field
                      label="登录后启动后台"
                      hint="关闭窗口或退出界面不会停止后台"
                    >
                      <Toggle
                        label="登录后启动后台"
                        value={config.desktop?.launch_at_login !== false}
                        onChange={(value) =>
                          update((config) => {
                            config.desktop = {
                              ...config.desktop,
                              launch_at_login: value,
                            };
                          })
                        }
                      />
                    </Field>
                    <Field label="系统通知">
                      <Toggle
                        label="系统通知"
                        value={config.runtime.notify}
                        onChange={(value) =>
                          update((config) => {
                            config.runtime.notify = value;
                          })
                        }
                      />
                    </Field>
                    <Field label="任务通知">
                      <select
                        value={
                          config.desktop?.notifications || "content_and_action"
                        }
                        onChange={(event) =>
                          update((config) => {
                            config.desktop = {
                              ...config.desktop,
                              notifications: event.target.value as
                                "content_and_action" | "action_only" | "all",
                            };
                          })
                        }
                      >
                        <option value="content_and_action">
                          新内容与需要处理时
                        </option>
                        <option value="action_only">只在需要处理时</option>
                        <option value="all">每次任务结果</option>
                      </select>
                    </Field>
                  </Section>
                  <Section
                    title="本机后台"
                    description="后台独立运行。暂停 AI 任务请前往任务页面，关心提醒可单独关闭。"
                  >
                    <Field
                      label="停止后台"
                      hint="取消当前任务并保留草稿，然后停止服务"
                    >
                      <button
                        className="danger-text"
                        onClick={() => setStop(true)}
                      >
                        <Power size={14} />
                        停止后台…
                      </button>
                    </Field>
                  </Section>
                </>
              )}
              {tab === "papers" && (
                <>
                  <Section title="论文追踪">
                    {schedule("papers", "检索")}
                    <Field label="首次回溯天数">
                      <input
                        type="number"
                        min={1}
                        value={config.arxiv.lookback_days}
                        onChange={(event) =>
                          update((config) => {
                            config.arxiv.lookback_days = Number(
                              event.target.value,
                            );
                          })
                        }
                      />
                    </Field>
                    <Field label="每日深读上限">
                      <input
                        type="number"
                        min={1}
                        value={config.arxiv.max_papers_per_day}
                        onChange={(event) =>
                          update((config) => {
                            config.arxiv.max_papers_per_day = Number(
                              event.target.value,
                            );
                          })
                        }
                      />
                    </Field>
                  </Section>
                  <Section
                    title="研究课题"
                    description="分类、关键词和排除词每行一个，也可用逗号分隔。"
                  >
                    {config.arxiv.topics.map((topic, index) => (
                      <div className="topic-form" key={topic.id}>
                        <header>
                          <strong>{topic.name || "新课题"}</strong>
                          <button
                            className="icon-button"
                            aria-label={`删除课题 ${topic.name}`}
                            disabled={config.arxiv.topics.length === 1}
                            onClick={() =>
                              update((config) => {
                                config.arxiv.topics.splice(index, 1);
                              })
                            }
                          >
                            <Trash2 size={14} />
                          </button>
                        </header>
                        <Field label="名称">
                          <input
                            value={topic.name}
                            onChange={(event) =>
                              update((config) => {
                                config.arxiv.topics[index].name =
                                  event.target.value;
                              })
                            }
                          />
                        </Field>
                        <Field label="arXiv 分类">
                          <ListInput
                            label="arXiv 分类"
                            value={topic.categories}
                            onChange={(value) =>
                              update((config) => {
                                config.arxiv.topics[index].categories = value;
                              })
                            }
                          />
                        </Field>
                        <Field label="关键词">
                          <ListInput
                            label="关键词"
                            value={topic.keywords}
                            onChange={(value) =>
                              update((config) => {
                                config.arxiv.topics[index].keywords = value;
                              })
                            }
                          />
                        </Field>
                        <Field label="排除词">
                          <ListInput
                            label="排除词"
                            value={topic.exclude_keywords}
                            onChange={(value) =>
                              update((config) => {
                                config.arxiv.topics[index].exclude_keywords =
                                  value;
                              })
                            }
                          />
                        </Field>
                        <small className="muted">课题标识：{topic.id}</small>
                      </div>
                    ))}
                    <button
                      onClick={() =>
                        update((config) => {
                          config.arxiv.topics.push({
                            id: `topic-${Date.now()}`,
                            name: "新课题",
                            categories: ["cs.RO"],
                            keywords: [],
                            exclude_keywords: [],
                          });
                        })
                      }
                    >
                      <Plus size={14} />
                      添加课题
                    </button>
                  </Section>
                </>
              )}
              {tab === "course" && (
                <>
                  <Section
                    title="课程节奏"
                    description="每天最多新增一节。手动获取反馈不推进课程，也不消耗新增课程额度。"
                  >
                    {schedule("course", "推进课程")}
                    <label className="stack-field">
                      学习基础与偏好
                      <textarea
                        value={config.course.level}
                        rows={5}
                        onChange={(event) =>
                          update((config) => {
                            config.course.level = event.target.value;
                          })
                        }
                      />
                    </label>
                  </Section>
                  <p className="muted">
                    笔记默认随每日课程处理。你也可以在章节内点击“获取反馈”。
                  </p>
                </>
              )}
              {tab === "review" && (
                <>
                  <Section title="每日复盘">
                    {schedule("review", "生成复盘")}
                  </Section>
                  <Section
                    title="记录来源"
                    description="导入 ChatGPT 导出 JSON 或记录快照，也可以在复盘页面手工补充当天记录。"
                  >
                    <Field label="本机来源目录">
                      <code>{config.review.source_directory}</code>
                    </Field>
                  </Section>
                  <p className="muted">
                    CLI 登录不等于可读取 ChatGPT
                    聊天历史。没有来源时会提醒你补充。
                  </p>
                </>
              )}
              {tab === "care" && (
                <>
                  <Section title="纪念日">
                    <Field label="名称">
                      <input
                        value={config.relationship.name}
                        onChange={(event) =>
                          update((config) => {
                            config.relationship.name = event.target.value;
                          })
                        }
                      />
                    </Field>
                    <Field label="基准日期">
                      <input
                        type="date"
                        value={config.relationship.anchor_date}
                        onChange={(event) =>
                          update((config) => {
                            config.relationship.anchor_date =
                              event.target.value;
                          })
                        }
                      />
                    </Field>
                    <Field label="基准天数">
                      <input
                        type="number"
                        min={1}
                        value={config.relationship.anchor_days}
                        onChange={(event) =>
                          update((config) => {
                            config.relationship.anchor_days = Number(
                              event.target.value,
                            );
                          })
                        }
                      />
                    </Field>
                  </Section>
                  <Section title="关心提醒">
                    {schedule("care", "提醒")}
                    <label className="stack-field">
                      提醒文案
                      <ListInput
                        label="提醒文案"
                        value={config.relationship.prompts}
                        onChange={(value) =>
                          update((config) => {
                            config.relationship.prompts = value;
                          })
                        }
                      />
                    </label>
                  </Section>
                </>
              )}
              {tab === "advanced" && (
                <>
                  <Section title="Codex">
                    <Field label="CLI 路径">
                      <input
                        value={config.runtime.codex_command}
                        onChange={(event) =>
                          update((config) => {
                            config.runtime.codex_command = event.target.value;
                          })
                        }
                      />
                    </Field>
                    <Field label="模型" hint="留空继承已有 Codex 模型配置">
                      <input
                        value={config.runtime.model || ""}
                        placeholder="继承 Codex 配置"
                        onChange={(event) =>
                          update((config) => {
                            config.runtime.model = event.target.value || null;
                          })
                        }
                      />
                    </Field>
                    <Field label="登录状态">
                      <button
                        onClick={() =>
                          void checkLogin()
                            .then((value) =>
                              setLogin(value.message || value.status),
                            )
                            .catch((err) => setLogin(errorText(err)))
                        }
                      >
                        <ShieldCheck size={14} />
                        检查登录
                      </button>
                    </Field>
                    {login && <p className="inline-status">{login}</p>}
                  </Section>
                  <button
                    className="advanced-toggle"
                    onClick={() => setAdvanced(!advanced)}
                  >
                    <ChevronDown
                      size={15}
                      style={{
                        transform: advanced ? "rotate(180deg)" : "none",
                      }}
                    />
                    执行与服务器参数
                  </button>
                  {advanced && (
                    <>
                      <Section title="任务执行">
                        <Field label="单次超时（秒）">
                          <input
                            type="number"
                            min={30}
                            max={7200}
                            value={config.runtime.timeout_seconds}
                            onChange={(event) =>
                              update((config) => {
                                config.runtime.timeout_seconds = Number(
                                  event.target.value,
                                );
                              })
                            }
                          />
                        </Field>
                        <Field label="失败重试间隔（秒）">
                          <input
                            type="number"
                            min={1}
                            value={config.scheduler.retry_seconds}
                            onChange={(event) =>
                              update((config) => {
                                config.scheduler.retry_seconds = Number(
                                  event.target.value,
                                );
                              })
                            }
                          />
                        </Field>
                        <Field
                          label="最多尝试次数"
                          hint="含首次执行；等待来源与主动取消不计失败"
                        >
                          <input
                            type="number"
                            min={1}
                            max={10}
                            value={config.scheduler.max_attempts}
                            onChange={(event) =>
                              update((config) => {
                                config.scheduler.max_attempts = Number(
                                  event.target.value,
                                );
                              })
                            }
                          />
                        </Field>
                        <Field label="服务器同步间隔（秒）">
                          <input
                            type="number"
                            min={60}
                            value={config.scheduler.sync_interval_seconds}
                            onChange={(event) =>
                              update((config) => {
                                config.scheduler.sync_interval_seconds = Number(
                                  event.target.value,
                                );
                              })
                            }
                          />
                        </Field>
                      </Section>
                      <Section title="服务器同步">
                        <Field label="启用服务器同步">
                          <Toggle
                            label="启用服务器同步"
                            value={config.server.enabled}
                            onChange={(value) =>
                              update((config) => {
                                config.server.enabled = value;
                              })
                            }
                          />
                        </Field>
                        <Field label="SSH 主机">
                          <input
                            value={config.server.ssh_host}
                            onChange={(event) =>
                              update((config) => {
                                config.server.ssh_host = event.target.value;
                              })
                            }
                          />
                        </Field>
                        <Field label="服务器目录">
                          <input
                            value={config.server.root}
                            onChange={(event) =>
                              update((config) => {
                                config.server.root = event.target.value;
                              })
                            }
                          />
                        </Field>
                        <Field label="连接超时（秒）">
                          <input
                            type="number"
                            min={1}
                            value={config.server.connect_timeout_seconds}
                            onChange={(event) =>
                              update((config) => {
                                config.server.connect_timeout_seconds = Number(
                                  event.target.value,
                                );
                              })
                            }
                          />
                        </Field>
                        <p className="inline-status">{serverStatus.text}</p>
                        {serverStatus.detail && (
                          <p className="inline-status">{serverStatus.detail}</p>
                        )}
                      </Section>
                    </>
                  )}
                  <Section
                    title="本机数据"
                    description="第一版保留当前目录，不在运行期间搬迁资料。"
                  >
                    {Object.entries(config.paths).map(([key, path]) => (
                      <Field
                        key={key}
                        label={
                          {
                            papers: "论文",
                            courses: "课程",
                            private: "私密记录",
                            state: "运行状态",
                          }[key] || key
                        }
                      >
                        <code>{path}</code>
                        <button
                          className="icon-button"
                          aria-label={`打开 ${path}`}
                          onClick={() => void openPath(path)}
                        >
                          <FolderOpen size={14} />
                        </button>
                      </Field>
                    ))}
                  </Section>
                </>
              )}
            </>
          )}
        </div>
        <footer className="settings-footer">
          <div role="status" aria-live="polite">
            <div className={message ? "saved-label" : "muted"}>
              {message ||
                (dirty ? "有未保存的更改" : "配置与本机 JSON 保持一致")}
            </div>
            <div className="muted" title={serverStatus.detail}>
              {serverStatus.text}
              {serverStatus.detail && (
                <button
                  className="quiet-button"
                  onClick={() => {
                    setTab("advanced");
                    setAdvanced(true);
                  }}
                >
                  查看同步详情
                </button>
              )}
            </div>
          </div>
          <button
            className="primary"
            disabled={!dirty || busy}
            onClick={() => void save()}
          >
            {busy ? "保存中…" : "保存更改"}
          </button>
        </footer>
      </div>
      <Modal
        open={stop}
        onClose={() => setStop(false)}
        title="停止后台服务？"
        description="当前任务会被取消并保留草稿，后续任务和关心提醒停止。重新打开 App 后可手动启动。"
      >
        <div className="modal-actions">
          <button disabled={busy} onClick={() => setStop(false)}>
            保持运行
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() => void stopService()}
          >
            {busy ? "正在安全停止…" : "取消当前任务并停止后台"}
          </button>
        </div>
      </Modal>
    </div>
  );
}
