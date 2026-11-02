import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi, it, expect, beforeEach } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import PaperRating from "../components/PaperRating";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mocked = vi.mocked(invoke);
beforeEach(() => vi.clearAllMocks());

it("保存和清除个人评分，不调用正文保存", async () => {
  mocked
    .mockResolvedValueOnce({ rating: 4 })
    .mockResolvedValueOnce({ rating: null });
  render(<PaperRating arxivId="2610.12345" />);
  fireEvent.click(screen.getByRole("button", { name: "4 分" }));
  await waitFor(() => expect(screen.getByText("4 / 5")).toBeInTheDocument());
  expect(mocked).toHaveBeenCalledWith("rpc", {
    method: "content.rate",
    params: { arxiv_id: "2610.12345", score: 4 },
  });
  fireEvent.click(screen.getByRole("button", { name: "清除" }));
  await waitFor(() => expect(screen.getByText("未评分")).toBeInTheDocument());
});

it("保存失败保留原分数并允许重试", async () => {
  mocked.mockRejectedValueOnce(new Error("后台断开"));
  render(<PaperRating arxivId="2610.12345" rating={3} />);
  fireEvent.click(screen.getByRole("button", { name: "5 分" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("后台断开");
  expect(screen.getByText("3 / 5")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "3 分" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});
