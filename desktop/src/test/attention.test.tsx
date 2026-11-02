import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import type { Task } from "../api";
import Today from "../pages/Today";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mocked = vi.mocked(invoke);

function useAttentionFixture(error?: Error) {
  let task: Task = {
    id: "paper-failure",
    kind: "papers",
    status: "failed",
    scheduled_at: "2026-10-30T12:30:00+08:00",
    attempts: 3,
    last_error: "论文需要查看",
    attention_version: "failure-v1",
    attention_dismissed: false,
  };
  mocked.mockImplementation(async (command, args) => {
    if (command !== "rpc") throw new Error(String(command));
    const { method } = args as { method: string };
    if (method === "config.get") return { config: {} };
    if (method === "tasks.list") return { tasks: [{ ...task }] };
    if (method === "content.list") return { items: [] };
    if (method === "relationship.get")
      return { date: "2026-10-30", name: "llq", days: 974, mood: null };
    if (method === "tasks.acknowledge") {
      if (error) throw error;
      task = { ...task, attention_dismissed: true };
      return { task };
    }
    throw new Error(`Unexpected method ${method}`);
  });
}

beforeEach(() => vi.clearAllMocks());

describe("首页待办的处理确认", () => {
  it("再次进入时隐藏已确认事项而不修改任务完成状态", async () => {
    useAttentionFixture();
    const navigate = vi.fn();
    const notify = vi.fn();
    const view = render(<Today navigate={navigate} notify={notify} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "需要处理 1" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "处理完成" }));
    expect(await screen.findByText("没有需要你处理的问题。")).toBeVisible();
    expect(screen.queryByText("论文需要查看")).not.toBeInTheDocument();
    expect(mocked).toHaveBeenCalledWith("rpc", {
      method: "tasks.acknowledge",
      params: { id: "paper-failure", attention_version: "failure-v1" },
    });
    expect(navigate).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledWith("已处理");
    view.unmount();
    render(<Today navigate={navigate} notify={notify} />);
    await screen.findByRole("button", { name: "关心 llq" });
    await userEvent.click(screen.getByRole("button", { name: "需要处理" }));
    expect(screen.getByText("没有需要你处理的问题。")).toBeVisible();
    const listed = await invoke<{ tasks: Task[] }>("rpc", {
      method: "tasks.list",
    });
    expect(listed.tasks[0].status).toBe("failed");
  });

  it("确认保存失败时仍显示原事项", async () => {
    useAttentionFixture(new Error("后台连接不可用"));
    const notify = vi.fn();
    render(<Today navigate={vi.fn()} notify={notify} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "需要处理 1" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "处理完成" }));
    expect(await screen.findByText("后台连接不可用")).toBeVisible();
    expect(screen.getByText("论文需要查看")).toBeVisible();
    expect(screen.getByRole("button", { name: "处理完成" })).toBeEnabled();
    expect(notify).not.toHaveBeenCalled();
  });
});
