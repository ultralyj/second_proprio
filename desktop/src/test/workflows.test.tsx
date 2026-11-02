import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import type { Config, Document } from "../api";
import DocumentView from "../components/DocumentView";
import Settings from "../pages/Settings";
import Care from "../pages/Care";
import Tasks from "../pages/Tasks";
import App from "../App";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@uiw/react-codemirror", () => ({
  default: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      aria-label="源码"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));
vi.mock("../components/Markdown", () => ({
  Markdown: ({ content }: { content: string }) => <article>{content}</article>,
}));
const mocked = vi.mocked(invoke);
const config: Config = {
  version: 1,
  timezone: "Asia/Shanghai",
  paths: {
    papers: "papers",
    courses: "courses",
    private: "private",
    state: ".state",
  },
  scheduler: {
    enabled: true,
    start_date: "2026-10-29",
    sync_interval_seconds: 300,
    retry_seconds: 1800,
    max_attempts: 3,
  },
  schedules: Object.fromEntries(
    ["papers", "course", "review", "care"].map((kind) => [
      kind,
      { weekdays: [0, 1, 2, 3, 4], times: ["08:00"] },
    ]),
  ) as Config["schedules"],
  server: {
    enabled: false,
    ssh_host: "fixture",
    root: "/fixture",
    connect_timeout_seconds: 8,
  },
  arxiv: {
    lookback_days: 7,
    max_papers_per_day: 3,
    request_interval_seconds: 3.1,
    topics: [
      {
        id: "robot",
        name: "机器人",
        categories: ["cs.RO"],
        keywords: ["robot"],
        exclude_keywords: [],
      },
    ],
  },
  course: { daily_new_lessons: 1, level: "已有基础" },
  review: { source_directory: "private/chat-imports" },
  relationship: {
    name: "llq",
    anchor_date: "2026-10-29",
    anchor_days: 973,
    mood_min: 1,
    mood_max: 5,
    prompts: ["问问今天过得怎么样。"],
  },
  runtime: {
    codex_command: "codex",
    model: null,
    timeout_seconds: 1800,
    notify: true,
    auto_run: true,
  },
};
const document: Document = {
  path: "courses/lesson.md",
  content: "# Original",
  hash: "base-a",
  manual_modified: false,
  editable: true,
  draft: null,
  candidates: [],
};
function mockRpc(
  handler: (method: string, params: Record<string, unknown>) => unknown,
) {
  mocked.mockImplementation(async (command, args) => {
    if (command === "rpc") {
      const payload = args as {
        method: string;
        params: Record<string, unknown>;
      };
      return handler(payload.method, payload.params);
    }
    throw new Error(`意外的命令 ${command}`);
  });
}
beforeEach(() => vi.clearAllMocks());
describe("正文版本保护", () => {
  it("旧保存请求失败时仍将已自动保存的新输入作为最后草稿", async () => {
    let failSave!: (reason: Error) => void;
    const pending = new Promise<Document>((_, reject) => {
      failSave = reject;
    });
    mockRpc((method) => {
      if (method === "content.read" || method === "content.draft")
        return document;
      if (method === "content.save") return pending;
      throw new Error(method);
    });
    render(<DocumentView path={document.path} notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("编辑"));
    fireEvent.change(screen.getByLabelText("源码"), {
      target: { value: "提交内容 A" },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent.change(screen.getByLabelText("源码"), {
      target: { value: "后续草稿 B" },
    });
    const latestDraft = {
      method: "content.draft",
      params: {
        path: document.path,
        content: "后续草稿 B",
        base_hash: "base-a",
      },
    };
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("rpc", latestDraft),
    );
    await act(async () => failSave(new Error("保存请求失败")));
    expect(screen.getByRole("alert")).toHaveTextContent("保存请求失败");
    expect(screen.getByLabelText("源码")).toHaveValue("后续草稿 B");
    expect(mocked).toHaveBeenLastCalledWith("rpc", latestDraft);
  });
  it("保存回包保留等待期间的新输入，并使用已保存版本作为下次基准", async () => {
    let finishSave!: (value: Document) => void;
    const pending = new Promise<Document>((resolve) => {
      finishSave = resolve;
    });
    let saves = 0;
    mockRpc((method, params) => {
      if (method === "content.read" || method === "content.draft")
        return document;
      if (method === "content.save") {
        saves += 1;
        return saves === 1
          ? pending
          : { ...document, content: params.content, hash: "base-c" };
      }
      throw new Error(method);
    });
    render(<DocumentView path={document.path} notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("编辑"));
    fireEvent.change(screen.getByLabelText("源码"), {
      target: { value: "首次提交" },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent.change(screen.getByLabelText("源码"), {
      target: { value: "继续输入的草稿" },
    });
    await act(async () =>
      finishSave({ ...document, content: "首次提交", hash: "base-b" }),
    );
    expect(screen.getByLabelText("源码")).toHaveValue("继续输入的草稿");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(mocked).toHaveBeenLastCalledWith("rpc", {
      method: "content.save",
      params: {
        path: document.path,
        content: "继续输入的草稿",
        base_hash: "base-b",
      },
    });
  });
  it("按原始基准保存，冲突时保留编辑内容", async () => {
    mockRpc((method) => {
      if (method === "content.read") return document;
      if (method === "content.save")
        throw { message: "正文已被外部修改", code: "conflict" };
      if (method === "content.draft") return document;
      throw new Error(method);
    });
    render(<DocumentView path={document.path} notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("编辑"));
    fireEvent.change(screen.getByLabelText("源码"), {
      target: { value: "# My edit" },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "正文发生了变化",
    );
    await userEvent.click(screen.getByText("保留草稿，稍后处理"));
    expect(screen.getByRole("alert")).toHaveTextContent("正文已被外部修改");
    expect(screen.getByLabelText("源码")).toHaveValue("# My edit");
    expect(mocked).toHaveBeenCalledWith("rpc", {
      method: "content.save",
      params: {
        path: document.path,
        content: "# My edit",
        base_hash: "base-a",
      },
    });
    expect(mocked).toHaveBeenCalledWith("rpc", {
      method: "content.draft",
      params: {
        path: document.path,
        content: "# My edit",
        base_hash: "base-a",
      },
    });
  });
  it("Command-S 仅保存获得焦点的笔记", async () => {
    mockRpc((method, params) => {
      if (method === "content.read") return { ...document, path: params.path };
      if (method === "content.save")
        return {
          ...document,
          path: params.path,
          content: params.content,
          hash: "saved",
        };
      if (method === "content.draft") return document;
      throw new Error(method);
    });
    render(
      <>
        <DocumentView path="courses/lesson.md" notify={vi.fn()} />
        <DocumentView path="private/notes/lesson.md" compact notify={vi.fn()} />
      </>,
    );
    const buttons = await screen.findAllByText("编辑");
    await userEvent.click(buttons[0]);
    await userEvent.click(buttons[1]);
    const editors = screen.getAllByLabelText("源码");
    fireEvent.change(editors[0], { target: { value: "main draft" } });
    fireEvent.change(editors[1], { target: { value: "note draft" } });
    editors[1].focus();
    fireEvent.keyDown(window, { key: "s", metaKey: true });
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("rpc", {
        method: "content.save",
        params: {
          path: "private/notes/lesson.md",
          content: "note draft",
          base_hash: "base-a",
        },
      }),
    );
    const saves = mocked.mock.calls.filter(
      ([, args]) => (args as { method: string }).method === "content.save",
    );
    expect(saves).toHaveLength(1);
  });
  it("恢复编辑草稿但不自动提交正文", async () => {
    mockRpc((method) => {
      if (method === "content.read")
        return {
          ...document,
          draft: {
            content: "# Draft",
            base_hash: "base-a",
            updated_at: "2026-10-30",
          },
        };
      if (method === "content.draft") return document;
      throw new Error(method);
    });
    render(<DocumentView path={document.path} notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("继续编辑"));
    expect(screen.getByLabelText("源码")).toHaveValue("# Draft");
    expect(
      mocked.mock.calls.some(
        ([, args]) => (args as { method: string }).method === "content.save",
      ),
    ).toBe(false);
  });
  it("比较候选并在采纳时提交正确的文档路径", async () => {
    const candidate = {
      id: "candidate-1",
      status: "waiting_review",
      created_at: "2026-10-30",
      summary: "更新公式",
      files: [
        { path: document.path, content: "# AI candidate", base_hash: "base-a" },
      ],
    };
    mockRpc((method) => {
      if (method === "content.read")
        return { ...document, candidates: [candidate] };
      if (method === "content.accept") return { status: "completed" };
      throw new Error(method);
    });
    render(<DocumentView path={document.path} notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("对比版本"));
    expect(screen.getByLabelText("源码")).toHaveValue("# AI candidate");
    await userEvent.click(screen.getByText("采纳此版本"));
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("rpc", {
        method: "content.accept",
        params: {
          candidate_id: "candidate-1",
          path: document.path,
          base_hash: "base-a",
          content: "# AI candidate",
        },
      }),
    );
  });
});
describe("私人记录与设置", () => {
  it("快速切换关心日期时忽略旧日期的迟到响应", async () => {
    const record = {
      date: "2026-10-30",
      name: "llq",
      days: 974,
      mood: null,
      note: "今天",
      actions: [],
      history_dates: [],
    };
    let finishOld!: (value: typeof record) => void;
    const pending = new Promise<typeof record>((resolve) => {
      finishOld = resolve;
    });
    mockRpc((method, params) => {
      if (method !== "relationship.get") throw new Error(method);
      if (params.date === "2026-10-29") return pending;
      return params.date
        ? { ...record, date: params.date, note: "当前日期" }
        : record;
    });
    render(<Care notify={vi.fn()} />);
    await screen.findByDisplayValue("今天");
    fireEvent.change(screen.getByLabelText("查看日期"), {
      target: { value: "2026-10-29" },
    });
    fireEvent.change(screen.getByLabelText("查看日期"), {
      target: { value: "2026-10-28" },
    });
    await screen.findByDisplayValue("当前日期");
    await act(async () =>
      finishOld({ ...record, date: "2026-10-29", note: "迟到记录" }),
    );
    expect(screen.getByLabelText("查看日期")).toHaveValue("2026-10-28");
    expect(screen.getByLabelText("备注")).toHaveValue("当前日期");
  });
  it("关心记录保存期间继续输入的备注不会被回包覆盖", async () => {
    const record = {
      date: "2026-10-30",
      name: "llq",
      days: 974,
      mood: null,
      note: "原记录",
      actions: [],
      history_dates: [],
    };
    let finishSave!: (value: typeof record) => void;
    const pending = new Promise<typeof record>((resolve) => {
      finishSave = resolve;
    });
    mockRpc((method) => (method === "relationship.get" ? record : pending));
    render(<Care notify={vi.fn()} />);
    await screen.findByDisplayValue("原记录");
    fireEvent.change(screen.getByLabelText("备注"), {
      target: { value: "首次修改" },
    });
    await userEvent.click(screen.getByRole("button", { name: "保存记录" }));
    fireEvent.change(screen.getByLabelText("备注"), {
      target: { value: "继续写下的小事" },
    });
    await act(async () => finishSave({ ...record, note: "首次修改" }));
    expect(screen.getByLabelText("备注")).toHaveValue("继续写下的小事");
    expect(screen.getByRole("button", { name: "保存记录" })).toBeEnabled();
  });
  it("使用明确空值清空心情、备注与行动", async () => {
    const record = {
      date: "2026-10-30",
      name: "llq",
      days: 974,
      mood: 4,
      note: "原备注",
      actions: ["原行动"],
      history_dates: [],
    };
    mockRpc((method, params) =>
      method === "relationship.get"
        ? record
        : method === "relationship.save"
          ? { ...record, ...params }
          : null,
    );
    render(<Care notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("清空"));
    fireEvent.change(screen.getByRole("textbox", { name: "备注" }), {
      target: { value: "" },
    });
    await userEvent.click(screen.getByRole("button", { name: "删除行动 1" }));
    await userEvent.click(screen.getByText("保存记录"));
    expect(mocked).toHaveBeenCalledWith("rpc", {
      method: "relationship.save",
      params: { date: "2026-10-30", mood: null, note: "", actions: [] },
    });
  });
  it("按配置基准版本提交业务修改", async () => {
    mockRpc((method, params) =>
      method === "config.get"
        ? { config, revision: "r1", sync: { status: "disabled" } }
        : method === "config.save"
          ? { config: params.config, revision: "r2", saved: true }
          : null,
    );
    render(<Settings notify={vi.fn()} onSaved={vi.fn()} />);
    const timezone = await screen.findByRole("textbox", { name: "时区" });
    fireEvent.change(timezone, { target: { value: "Europe/London" } });
    await userEvent.click(screen.getByText("保存更改"));
    expect(mocked).toHaveBeenCalledWith("rpc", {
      method: "config.save",
      params: {
        config: { ...config, timezone: "Europe/London" },
        expected_revision: "r1",
      },
    });
    expect(
      await screen.findByText("本机已保存 · 后续任务使用新配置"),
    ).toBeInTheDocument();
    expect(screen.getByText("服务器未启用")).toBeInTheDocument();
  });
});
describe("配置文本列表", () => {
  it("关键词仍获得焦点时可保存，且保留换行输入", async () => {
    mockRpc((method, params) => {
      if (method === "config.get") return { config, revision: "v1", sync: {} };
      if (method === "config.save")
        return { config: params.config, revision: "v2", sync: {} };
      throw new Error(method);
    });
    render(<Settings notify={vi.fn()} onSaved={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "论文" }));
    const input = screen.getByLabelText("关键词");
    input.focus();
    fireEvent.change(input, { target: { value: "robot\ndexterous\n" } });
    expect(input).toHaveValue("robot\ndexterous\n");
    expect(screen.getByRole("button", { name: "保存更改" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "保存更改" }));
    const call = mocked.mock.calls.find(
      ([, args]) => (args as { method: string }).method === "config.save",
    );
    expect(
      (call?.[1] as { params: { config: Config } }).params.config.arxiv
        .topics[0].keywords,
    ).toEqual(["robot", "dexterous"]);
  });
});
describe("服务器配置同步确认", () => {
  it("等待当前服务器版本确认，刷新不覆盖表单草稿", async () => {
    vi.useFakeTimers();
    try {
      let effective = {
        ...config,
        server: { ...config.server, enabled: true },
      };
      let revision = "local-old";
      let sync = {
        status: "ok",
        current_revision: "public-old",
        desired_revision: "public-old",
        remote_revision: "public-old",
        in_progress: false,
      };
      mockRpc((method, params) => {
        if (method === "config.get")
          return { config: effective, revision, sync };
        if (method === "config.save") {
          effective = params.config as Config;
          revision = "local-new";
          sync = { ...sync, current_revision: "public-new" };
          return { config: effective, revision, sync };
        }
        throw new Error(method);
      });
      await act(async () => {
        render(<Settings notify={vi.fn()} onSaved={vi.fn()} />);
      });
      expect(screen.getByText("服务器已同步")).toBeInTheDocument();
      fireEvent.change(screen.getByRole("textbox", { name: "时区" }), {
        target: { value: "Europe/London" },
      });
      await act(async () => {
        fireEvent.click(screen.getByText("保存更改"));
      });
      expect(
        screen.getByText("服务器等待同步 · 当前配置尚未确认"),
      ).toBeInTheDocument();
      expect(screen.queryByText("服务器已同步")).not.toBeInTheDocument();
      fireEvent.change(screen.getByRole("textbox", { name: "时区" }), {
        target: { value: "Europe/Berlin" },
      });
      sync = { ...sync, desired_revision: "public-new", in_progress: true };
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(
        screen.getByText("服务器同步中 · 等待后台确认"),
      ).toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "时区" })).toHaveValue(
        "Europe/Berlin",
      );
      sync = { ...sync, remote_revision: "public-new", in_progress: false };
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(screen.getByText("服务器已同步")).toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "时区" })).toHaveValue(
        "Europe/Berlin",
      );
    } finally {
      vi.useRealTimers();
    }
  });
  it("同步失败时显示服务器保留配置，不误报本机保存失败", async () => {
    const enabled = { ...config, server: { ...config.server, enabled: true } };
    mockRpc((method, params) => {
      if (method === "config.get")
        return {
          config: enabled,
          revision: "local-old",
          sync: {
            status: "error",
            current_revision: "public-new",
            desired_revision: "public-new",
            remote_revision: "public-old",
            in_progress: false,
            error: "SSH 无法连接",
          },
        };
      if (method === "config.save")
        return {
          config: params.config,
          revision: "local-new",
          sync: {
            status: "error",
            current_revision: "public-new",
            desired_revision: "public-new",
            remote_revision: "public-old",
            in_progress: false,
            error: "SSH 无法连接",
          },
        };
      throw new Error(method);
    });
    render(<Settings notify={vi.fn()} onSaved={vi.fn()} />);
    const timezone = await screen.findByRole("textbox", { name: "时区" });
    fireEvent.change(timezone, { target: { value: "Europe/London" } });
    await userEvent.click(screen.getByText("保存更改"));
    expect(
      await screen.findByText("本机已保存 · 后续任务使用新配置"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("服务器同步失败 · 仍使用旧配置"),
    ).toBeInTheDocument();
    expect(screen.getByText("查看同步详情")).toBeInTheDocument();
    expect(screen.queryByText("服务器已同步")).not.toBeInTheDocument();
  });
});
describe("任务控制与连接", () => {
  it("只重试选中的单个任务", async () => {
    const tasks = [
      {
        id: "first",
        kind: "papers",
        status: "failed",
        scheduled_at: "2026-10-30T12:30:00+08:00",
        attempts: 3,
      },
      {
        id: "second",
        kind: "course",
        status: "completed",
        scheduled_at: "2026-10-30T08:00:00+08:00",
        attempts: 1,
      },
    ];
    mockRpc((method) =>
      method === "tasks.list" ? { tasks, paused: false } : {},
    );
    render(<Tasks notify={vi.fn()} />);
    await userEvent.click(await screen.findByText("重试"));
    expect(mocked).toHaveBeenCalledWith("rpc", {
      method: "tasks.retry",
      params: { id: "first" },
    });
  });
  it("后台未运行时如实显示未连接界面", async () => {
    mocked.mockRejectedValue("本机后台尚未启动");
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "本机后台尚未启动",
    );
    expect(screen.getByText("启动并连接后台")).toBeInTheDocument();
    expect(screen.queryByText("后台已就绪")).not.toBeInTheDocument();
  });
});
