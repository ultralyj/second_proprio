import { invoke } from "@tauri-apps/api/core";
export type Page =
  "today" | "papers" | "course" | "review" | "care" | "tasks" | "settings";
export type Kind = "papers" | "course" | "review" | "care";
export interface Topic {
  id: string;
  name: string;
  categories: string[];
  keywords: string[];
  exclude_keywords: string[];
}
export interface Schedule {
  weekdays: number[];
  times: string[];
  enabled?: boolean;
}
export interface Config {
  version: number;
  timezone: string;
  paths: Record<string, string>;
  scheduler: {
    enabled: boolean;
    start_date: string;
    sync_interval_seconds: number;
    retry_seconds: number;
    max_attempts: number;
  };
  schedules: Record<Kind, Schedule>;
  server: {
    enabled: boolean;
    ssh_host: string;
    root: string;
    connect_timeout_seconds: number;
  };
  arxiv: {
    lookback_days: number;
    max_papers_per_day: number;
    request_interval_seconds: number;
    topics: Topic[];
  };
  course: { daily_new_lessons: number; level: string };
  review: { source_directory: string };
  relationship: {
    name: string;
    anchor_date: string;
    anchor_days: number;
    mood_min: number;
    mood_max: number;
    prompts: string[];
  };
  runtime: {
    codex_command: string;
    model: string | null;
    timeout_seconds: number;
    notify: boolean;
    auto_run: boolean;
  };
  desktop?: {
    appearance?: "system" | "light" | "dark";
    launch_at_login?: boolean;
    notifications?: "content_and_action" | "action_only" | "all";
    [key: string]: unknown;
  };
}
export interface ConfigResponse {
  config: Config;
  revision: string;
  sync?: Record<string, unknown>;
}
export interface ContentItem {
  path: string;
  title: string;
  kind: string;
  status: string;
  date: string;
  topic?: string;
  arxiv_id?: string;
  version?: number;
  lesson_id?: string;
  learned?: boolean;
  feedback_pending?: boolean;
  note_path?: string;
  feedback_path?: string;
  example_path?: string;
  pdf_path?: string;
}
export interface Candidate {
  id: string;
  task_id?: string;
  status: string;
  created_at: string;
  files: { path: string; content: string; base_hash: string | null }[];
  summary: string;
}
export interface Document {
  path: string;
  content: string;
  hash: string | null;
  manual_modified: boolean;
  editable: boolean;
  draft: {
    content: string;
    base_hash: string | null;
    updated_at: string;
  } | null;
  candidates: Candidate[];
}
export interface Revision {
  id: string;
  created_at: string;
  author: string;
  hash: string;
  content: string;
}
export interface Task {
  id: string;
  kind: Kind;
  status: string;
  scheduled_at: string;
  attempts: number;
  failure_attempts?: number;
  failures?: number;
  finished_at?: string;
  retry_at?: string;
  last_error?: string;
  stage?: string;
  started_at?: string;
  updated_at?: string;
  progress?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  result?: { summary?: string; artifacts?: string[] };
  events?: Record<string, unknown>[];
}
export interface Relationship {
  date: string;
  name: string;
  days: number;
  mood: number | null;
  note: string;
  actions: string[];
  history_dates: string[];
}
export interface SystemStatus {
  status?: string;
  paused?: boolean;
  root?: string;
  version?: string;
  running?: boolean;
  active_task?: Task | null;
  counts?: Record<string, number>;
  sync?: Record<string, unknown>;
  [key: string]: unknown;
}
export class RpcError extends Error {
  code: string;
  details: unknown;
  constructor(message: string, code = "error", details?: unknown) {
    super(message);
    this.code = code;
    this.details = details;
  }
}
export function errorText(error: unknown): string {
  return error instanceof Error
    ? error.message
    : typeof error === "string"
      ? error
      : JSON.stringify(error);
}
export async function rpc<T>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  try {
    return await invoke<T>("rpc", { method, params });
  } catch (error) {
    if (error instanceof RpcError) throw error;
    if (typeof error === "object" && error !== null && "message" in error) {
      const value = error as {
        message: string;
        code?: string;
        details?: unknown;
      };
      throw new RpcError(value.message, value.code, value.details);
    }
    const message = errorText(error);
    try {
      const value = JSON.parse(message);
      if (value.message)
        throw new RpcError(value.message, value.code, value.details);
    } catch (parsed) {
      if (parsed instanceof RpcError) throw parsed;
    }
    throw new RpcError(
      message.includes("__TAURI")
        ? "请从已安装的 second proprio App 打开；浏览器预览无法连接本机后台。"
        : message,
    );
  }
}
export const openPath = (path: string) => invoke("open_path", { path });
export const chooseImport = () => invoke<string | null>("choose_import");
export const serviceControl = (action: "start" | "stop") =>
  invoke("service_control", { action });
export const checkLogin = () =>
  invoke<{ status: string; message: string }>("check_login");
export const readAsset = (path: string) =>
  invoke<string>("read_asset", { path });
export const kindNames: Record<string, string> = {
  papers: "论文解读",
  course: "互动课程",
  review: "每日复盘",
  care: "关心提醒",
};
export const statusNames: Record<string, string> = {
  pending: "等待执行",
  running: "正在运行",
  completed: "已完成",
  failed: "失败",
  waiting_source: "等待来源",
  waiting_review: "等待采纳",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  skipped: "已跳过",
  done: "已完成",
  generated: "已生成",
  needs_review: "待核对",
  draft: "草稿",
  planned: "待生成",
  rejected: "已放弃",
};
export const statusLabel = (status: string) => statusNames[status] || status;
let displayTimezone = "Asia/Shanghai";
export function setDisplayTimezone(timezone: string): void {
  try {
    new Intl.DateTimeFormat("zh-CN", { timeZone: timezone }).format();
    displayTimezone = timezone;
  } catch {
    /* 非法时区不会改变已生效的显示设置。 */
  }
}
export function formatTime(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", {
        timeZone: displayTimezone,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}
