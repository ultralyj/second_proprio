import { useEffect, useState } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readAsset } from "../api";
import mermaid from "mermaid";
import { Markdown } from "../components/Markdown";

vi.mock("../api", () => ({ readAsset: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(),
  },
}));

const path = "papers/first/README.md";
const content = [
  "# 论文解读",
  "",
  "原有解释。",
  "",
  "![原文图](figures/pipeline.png)",
  "",
  "```mermaid",
  "graph LR",
  "  A --> B",
  "```",
].join("\n");

beforeEach(() => {
  vi.clearAllMocks();
  document.documentElement.dataset.theme = "light";
  vi.mocked(readAsset).mockImplementation(async (asset) => `asset://${asset}`);
  vi.mocked(mermaid.render).mockImplementation(async (id, source) => ({
    svg: `<svg data-diagram="${id}"><text>${source}</text></svg>`,
    diagramType: "flowchart",
  }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  delete document.documentElement.dataset.theme;
});

async function loaded(container: HTMLElement) {
  const image = await screen.findByRole("img", { name: "原文图" });
  await waitFor(() =>
    expect(container.querySelector(".mermaid svg")).not.toBeNull(),
  );
  return { image, diagram: container.querySelector(".mermaid svg") };
}

function PollingReader() {
  const [polls, setPolls] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setPolls((count) => count + 1), 5000);
    return () => clearInterval(timer);
  }, []);
  return (
    <>
      <output>轮询 {polls}</output>
      <Markdown content={content} path={path} />
    </>
  );
}

describe("论文阅读渲染稳定性", () => {
  it("每五秒状态轮询时保留图片和管线节点，不重新请求或生成", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const { container } = render(<PollingReader />);
    const initial = await loaded(container);
    for (let count = 1; count <= 3; count++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(screen.getByText(`轮询 ${count}`)).toBeInTheDocument();
      expect(screen.queryByText("正在读取图片…")).not.toBeInTheDocument();
      expect(screen.getByRole("img", { name: "原文图" })).toBe(initial.image);
      expect(container.querySelector(".mermaid svg")).toBe(initial.diagram);
    }
    expect(readAsset).toHaveBeenCalledTimes(1);
    expect(mermaid.render).toHaveBeenCalledTimes(1);
  });

  it("更新解读文字时保留未变化的插图和管线", async () => {
    const { container, rerender } = render(
      <Markdown content={content} path={path} />,
    );
    const initial = await loaded(container);
    rerender(
      <Markdown
        content={content.replace("原有解释。", "补充了方法细节。")}
        path={path}
      />,
    );
    expect(screen.getByText("补充了方法细节。")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "原文图" })).toBe(initial.image);
    expect(container.querySelector(".mermaid svg")).toBe(initial.diagram);
    expect(readAsset).toHaveBeenCalledTimes(1);
    expect(mermaid.render).toHaveBeenCalledTimes(1);
  });

  it("切换论文后按新目录读取相对图片", async () => {
    const { container, rerender } = render(
      <Markdown content={content} path={path} />,
    );
    await loaded(container);
    rerender(<Markdown content={content} path="papers/second/README.md" />);
    await waitFor(() =>
      expect(screen.getByRole("img", { name: "原文图" })).toHaveAttribute(
        "src",
        "asset://papers/second/figures/pipeline.png",
      ),
    );
    expect(readAsset).toHaveBeenNthCalledWith(
      1,
      "papers/first/figures/pipeline.png",
    );
    expect(readAsset).toHaveBeenNthCalledWith(
      2,
      "papers/second/figures/pipeline.png",
    );
  });

  it("修改管线源码时更新图示且不重载相同图片", async () => {
    const { container, rerender } = render(
      <Markdown content={content} path={path} />,
    );
    const initial = await loaded(container);
    rerender(
      <Markdown content={content.replace("A --> B", "A --> C")} path={path} />,
    );
    await waitFor(() =>
      expect(container.querySelector(".mermaid svg")).toHaveTextContent(
        "A --> C",
      ),
    );
    expect(mermaid.render).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("img", { name: "原文图" })).toBe(initial.image);
    expect(readAsset).toHaveBeenCalledTimes(1);
  });

  it("切换主题仍更新管线配色，并保留图片", async () => {
    const { container } = render(<Markdown content={content} path={path} />);
    const initial = await loaded(container);
    await act(async () => {
      document.documentElement.dataset.theme = "dark";
    });
    await waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(2));
    expect(mermaid.initialize).toHaveBeenLastCalledWith(
      expect.objectContaining({ theme: "dark" }),
    );
    expect(screen.getByRole("img", { name: "原文图" })).toBe(initial.image);
    expect(readAsset).toHaveBeenCalledTimes(1);
  });
});
