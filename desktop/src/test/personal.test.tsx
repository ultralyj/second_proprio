import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import type { Config } from "../api";
import Today from "../pages/Today";
import Care from "../pages/Care";
import Settings from "../pages/Settings";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
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

const sources = {
  directory: "/Users/example/未配置目录",
  exists: false,
  file_count: 2,
  dates: { "2026-10-30": { messages: 3 } },
  coverage_start: "2026-10-30T01:00:00Z",
  coverage_end: "2026-10-30T03:00:00Z",
  errors: [],
  warnings: [],
};
const record = {
  date: "2026-10-30",
  name: "llq",
  days: 974,
  mood: null,
  note: "",
  actions: [],
  history_dates: [],
};
function useReadOnlyFixture(value = config) {
  mocked.mockImplementation(async (command, args) => {
    if (command !== "rpc") throw new Error(String(command));
    const { method } = args as { method: string };
    if (method === "config.get")
      return { config: value, revision: "r1", sync: { status: "disabled" } };
    if (method === "tasks.list") return { tasks: [], paused: false };
    if (method === "content.list") return { items: [] };
    if (method === "relationship.get") return record;
    throw new Error(`Unexpected mutation ${method}`);
  });
}
beforeEach(() => vi.clearAllMocks());
describe("首页默认折叠", () => {
  it("保留首页标题与叩问，所有内容分组初始折叠", async () => {
    useReadOnlyFixture();
    render(<Today navigate={vi.fn()} notify={vi.fn()} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("今天");
    expect(
      screen.getByText("此刻，我最值得投入的一件事是什么？"),
    ).toBeInTheDocument();
    await screen.findByRole("button", { name: "关心 llq" });
    const toggles = screen
      .getAllByRole("button")
      .filter((button) => button.hasAttribute("aria-expanded"));
    expect(toggles).toHaveLength(4);
    toggles.forEach((button) =>
      expect(button).toHaveAttribute("aria-expanded", "false"),
    );
    expect(
      screen.queryByRole("button", { name: "检索论文" }),
    ).not.toBeInTheDocument();
    const background = screen.getByRole("button", { name: "后台任务" });
    await userEvent.click(background);
    expect(background).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("button", { name: "检索论文" }),
    ).toBeInTheDocument();
    await userEvent.click(background);
    expect(
      screen.queryByRole("button", { name: "检索论文" }),
    ).not.toBeInTheDocument();
    expect(
      mocked.mock.calls.some(
        ([, args]) => (args as { method: string }).method === "tasks.submit",
      ),
    ).toBe(false);
  });
  it("从已保存配置读取个人叩问", async () => {
    useReadOnlyFixture({
      ...config,
      desktop: { home_question: "今天，我想认真完成什么？" },
    });
    render(<Today navigate={vi.fn()} notify={vi.fn()} />);
    expect(
      await screen.findByText("今天，我想认真完成什么？"),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});
describe("关心页面交互", () => {
  it("点亮爱心和播放动画不写入记录或发送消息", async () => {
    useReadOnlyFixture();
    const notify = vi.fn();
    const { container } = render(<Care notify={notify} />);
    const heart = await screen.findByRole("button", { name: "点亮爱心" });
    await userEvent.click(heart);
    expect(heart).toHaveAttribute("aria-pressed", "true");
    expect(heart).toHaveClass("is-loved");
    const particles = container.querySelectorAll(".care-heart-particle");
    expect(particles.length).toBeGreaterThanOrEqual(4);
    expect(particles.length).toBeLessThanOrEqual(10);
    expect(screen.getByRole("button", { name: "保存记录" })).toBeDisabled();
    expect(
      mocked.mock.calls.every(
        ([command, args]) =>
          command === "rpc" &&
          (args as { method: string }).method === "relationship.get",
      ),
    ).toBe(true);
    expect(notify).not.toHaveBeenCalled();
  });
});
describe("个人配置", () => {
  it("区分目录不存在与已导入记录，不扫描未保存的路径草稿", async () => {
    mocked.mockImplementation(async (command, args) => {
      if (command !== "rpc") throw new Error(String(command));
      const { method } = args as { method: string };
      if (method === "config.get")
        return { config, revision: "r1", sync: { status: "disabled" } };
      if (method === "review.sources") return sources;
      throw new Error(method);
    });
    render(<Settings notify={vi.fn()} onSaved={vi.fn()} />);
    await screen.findByRole("textbox", { name: /首页自我叩问/ });
    await userEvent.click(screen.getByRole("button", { name: "复盘" }));
    expect(await screen.findByText("配置目录尚不存在")).toBeInTheDocument();
    expect(
      screen.getByText("共发现 2 个来源文件（含 App 内导入记录）"),
    ).toBeInTheDocument();
    expect(screen.getByText("可复盘 1 天 · 3 条消息")).toBeInTheDocument();
    expect(
      screen.getByText(/覆盖 2026\/10\/30 09:00 — 2026\/10\/30 11:00/),
    ).toBeInTheDocument();
    const before = mocked.mock.calls.filter(
      ([, args]) => (args as { method: string }).method === "review.sources",
    ).length;
    fireEvent.change(screen.getByRole("textbox", { name: /聊天记录目录/ }), {
      target: { value: "/Users/example/尚未保存" },
    });
    expect(
      mocked.mock.calls.filter(
        ([, args]) => (args as { method: string }).method === "review.sources",
      ),
    ).toHaveLength(before);
  });
  it("仅显式保存时提交自定义叩问与外部复盘目录", async () => {
    mocked.mockImplementation(async (command, args) => {
      if (command === "choose_directory") return "/Users/example/聊天记录";
      const { method, params } = args as {
        method: string;
        params: { config: Config };
      };
      if (method === "config.get")
        return { config, revision: "r1", sync: { status: "disabled" } };
      if (method === "review.sources") return sources;
      if (method === "config.save")
        return {
          config: params.config,
          revision: "r2",
          sync: { status: "disabled" },
        };
      throw new Error(method);
    });
    render(<Settings notify={vi.fn()} onSaved={vi.fn()} />);
    const question = await screen.findByRole("textbox", {
      name: /首页自我叩问/,
    });
    fireEvent.change(question, {
      target: { value: "今天，我想认真完成什么？" },
    });
    await userEvent.click(screen.getByRole("button", { name: "复盘" }));
    await userEvent.click(
      screen.getByRole("button", { name: "选择复盘来源目录" }),
    );
    const directory = screen.getByRole("textbox", { name: /聊天记录目录/ });
    expect(directory).toHaveValue("/Users/example/聊天记录");
    fireEvent.change(directory, {
      target: { value: "/Users/example/另一份记录" },
    });
    expect(
      mocked.mock.calls.some(
        ([, args]) =>
          (args as { method?: string } | undefined)?.method === "config.save",
      ),
    ).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "保存更改" }));
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("rpc", {
        method: "config.save",
        params: {
          config: {
            ...config,
            desktop: { home_question: "今天，我想认真完成什么？" },
            review: { source_directory: "/Users/example/另一份记录" },
          },
          expected_revision: "r1",
        },
      }),
    );
    expect(mocked).toHaveBeenCalledWith("choose_directory");
  });
});
