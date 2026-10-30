# second proprio

你的个人数字化技能仓库：论文追踪、互动课程、每日复盘与 llq 关心提醒。
内容以本地 Markdown 保存，代码与技能纳入 Git，个人记录默认不进入版本库。

## 从这里开始

- [本机首页](INDEX.md)：最近成果、任务状态和需要你补充的信息。
- [互动课程](courses/README.md)：已有 IL/RL 基础，前三节附 CPU 可运行代码。
- [论文目录](papers/)：按课题、arXiv ID、版本分目录，保留原图。
- [服务器与后台服务](docs/deployment.md)：安装、同步、停用与恢复。
- [Mac App 使用与维护](docs/desktop.md)：配置、任务、编辑与候选版本、打包及回退。
- [后续计划](TODO.md)：手机端及后续能力。

Mac 第一版使用 Tauri 2＋React，通过本机 Unix Socket 连接持久 Python 服务。
安装后的应用使用附带运行时，数据继续留在当前仓库。关闭窗口驻留菜单栏，退出
界面后后台继续工作；暂停 AI、取消任务、停止后台是彼此独立的操作。
日常配置、论文和课程编辑、笔记反馈、复盘来源、心情记录均可在 App 内完成。

在项目根目录使用：

```sh
uv sync --extra learning --group dev
uv run second-proprio validate
uv run second-proprio status
uv run second-proprio run care
uv run second-proprio run papers
uv run second-proprio run course
uv run second-proprio run review
```

桌面服务运行时，`run` 提交任务后立即返回；CLI 与 App 使用同一队列。
旧 `tick` 仅保留兼容，桌面服务持有执行锁时不会启动另一执行器。
失败保留错误与日志，按配置重试；普通重试只记录事件，失败耗尽、登录失效和
缺少来源会提醒。App 的“重试”只针对选中任务，并重置失败预算。
等待来源和主动取消不消耗失败预算，取消任务不会自动重启。
日志、队列与诊断在 `.state`，不进入 Git。

## 时间与课题都在 JSON

首次配置复制 `config/settings.example.json` 为 `config/settings.json`。
修改后先 `validate`；推荐 `configure <候选JSON文件>`，只有候选通过 Schema
校验才替换当前配置，并尝试同步服务器。同步失败会保留旧的服务器版本状态。

| 模块 | Asia/Shanghai 默认时间 |
|---|---|
| 论文 | 周一至周五 12:30 |
| 课程 | 每天 08:00 |
| 复盘 | 每天 22:30 |
| llq | 每天10:30、14:00、17:30、20:00、22:00 |

配置包含分类、关键词、排除词、回溯天数、每日深读上限、课程级别、模型、目录、
服务器、提醒文案和重试设置。模型留空沿用已有 Codex 配置，不引入付费 API。
App 保存配置时使用同一 Schema，并显示本机保存与服务器同步各自的结果。
新值用于后续任务，在途任务继续使用启动时快照；桌面服务不需要重装登录项。

## 课程怎样互动

直接编辑 `private/notes` 中对应小节的 Markdown，在“我的答案／学习笔记／
疑问与卡点”写文字。下次课程任务或手动运行时，回复写入 `private/feedback`。
也可以在 Codex 中调用 `$sp-course`，明确章节后把聊天反馈录入笔记。

笔记正文保留原样；只有你勾选“已学完本节”才计入已学。生成不等于掌握。
每天最多新增一节，离线数日恢复后只接着生成一节；首轮目录全部完成后进入维护。

## 每日复盘的数据来源

后台 CLI 不能仅凭 ChatGPT 登录自动读出聊天历史。导入你提供的
`conversations.json`，或应用内工具保存的规范化快照：

```sh
uv run second-proprio import-chat /绝对路径/conversations.json
uv run second-proprio run review --date 2026-10-29
```

快照格式为 `{"messages":[{"timestamp":"2026-10-29T15:00:00+08:00",
"role":"user","text":"今天完成了……","conversation_id":"标识","title":"标题"}]}`。
只保留用户和助手文本，按北京时间归档，尽量使用当前分支；证据不完整会说明覆盖范围。
无来源时输出“请告诉我今天的记录在哪里。”，不编造总结。应用内可调用
`$sp-daily-review` 读取可访问聊天并导入快照，默认不把 Codex 聊天冒充 ChatGPT 历史。

## llq 的心情由你填写

```sh
uv run second-proprio mood 4 --note '今天状态不错' --action '一起散步'
uv run second-proprio run care
```

分数1–5，仅记录你的判断；当天没填就显示待填写，不沿用昨天。纪念日锚点是
2026-10-29＝第973天。提醒只发本机，不自动联系 llq；离线提醒恢复后合并。

## 后台与服务器边界

Mac 登录后由 launchd 托管持久服务，Codex CLI 串行生成内容，不要求桌面应用
保持打开。状态查询、配置保存与关心提醒不等待 AI 子进程。Mac 必须登录、联网，
Codex 登录与额度可用；睡眠或关机会延迟内容。
通知能否显示还受 macOS 的通知权限和专注模式影响。

服务器只负责排队与公开论文元数据。SSH 未修复前，本机按相同配置补建任务，
保持可用。SSH同步只传白名单配置与任务信封；模型凭据、聊天、笔记、心情和
论文附件不会上传。服务器脚本已准备与部署已成功是两个不同状态。

## 开发与验收

```sh
uv run pytest -q
uv run yapf --style google --diff --recursive src tests courses/examples
uv run pylint src/second_proprio courses/examples
npm --prefix desktop ci
npm --prefix desktop test
npm --prefix desktop run build
cargo test --manifest-path desktop/src-tauri/Cargo.toml
uv run python packaging/build_app.py
```

Google 风格需要代码审查配合，格式化器并不验证注释语言和算法正确性。
技能入口为 `.agents/skills/sp-arxiv`、`sp-course`、`sp-daily-review`、`sp-llq`。
不配置 Git 远端，不自动推送。更改私密目录时必须同时检查 Git 排除规则。
