# 本机后台运行与服务器部署

## 运行边界

Mac App 安装后，launchd 托管 Application Support 中的版本化 Python 后台服务，
负责同步、调度和串行生成；界面退出不停止后台。Codex CLI 在 Mac 上使用已有登录。
当前 App 的启动、停止和回退操作见 [Mac App 使用说明](desktop.md)。
服务器只维护调度、任务信封与公共论文元数据，不持有 Codex 登录、原始聊天、
个人笔记、心情数据或论文 PDF／图片。

服务器 SSH 已修复，并于 **2026-10-30** 完成轻量部署和真实同步验收。
服务位于 `/home/ubuntu/second-proprio`，使用用户级 systemd 计时器；没有部署模型。
完整的验收范围与尚未做的整机重启验证见下文“实际部署记录”。

## 本机旧脚本安装（仅未启用 Mac App 时）

已迁移到 Mac App 的工作区不要重复安装旧 `tick` 登录项；通过 App 管理后台。
以下命令保留给未迁移的命令行工作区。

在真实项目根目录建立运行环境后执行：

```sh
uv sync
uv run second-proprio --root "$PWD" validate
uv run second-proprio --root "$PWD" install-local
```

安装器生成 `.state/deploy/com.second-proprio.worker.plist`，随后注册到当前用户的
`~/Library/LaunchAgents`。运行日志位于 `.state/logs`。轮询间隔读取 JSON 中的
`scheduler.sync_interval_seconds`，默认 300 秒。更改轮询间隔后重新执行安装命令；
更改业务时间和课题无需重装服务，下一次 `tick` 会读取最新有效配置。

首次验收先手动运行 `tick`；随后通过 `launchctl print` 检查服务，并验证退出应用
仍可执行。Mac 尚未登录、关机或睡眠时不生成内容。模型登录失效时保留任务并提示。
后台进程不读取交互式 shell 配置，也不会自动复制 shell 中的代理或密钥到服务器。
如果网络需要代理，应使用项目明确支持的本机运行环境配置并实际验证后台联网。

停止后台运行可使用：

```sh
launchctl bootout "gui/$(id -u)/com.second-proprio.worker"
```

## 服务器准备与上传

1. 使用已有 `tencent-vps` SSH 别名并严格校验主机密钥；当前登录已验证可用。
2. 服务器安装发行版提供的 Python 3、venv、pip、systemd；不安装 GPU 环境、PyTorch
   或 Codex CLI。具体 Python 最低版本以项目 `pyproject.toml` 为准。
3. 在本机调用 `render-systemd` 生成部署文件，并生成仅含调度与公共论文检索参数的
   `config/server.json` 服务器配置。不要上传本机完整 `config/settings.json`。
4. 将应用源码、`pyproject.toml`、README、必要配置 schema 与服务器配置上传到
   `/home/ubuntu/second-proprio`，并将生成的部署文件放入其 `.state/deploy`。
   首次上传使用明确白名单；不要递归复制整个仓库，不上传 `.venv`、`private`、
   `papers`、个人配置、登录信息或本机队列数据库。
5. 在远端项目目录运行 `.state/deploy/install-server.sh`，使用 `ubuntu` 用户安装
   独立虚拟环境及 user systemd 服务。脚本不会执行 `sudo`。
6. 管理员执行 `sudo loginctl enable-linger ubuntu`，使用户服务不依赖 SSH 登录。
   这一步需服务器管理员权限，未完成时不要宣称断开 SSH 后仍保证运行。

服务器计时器每分钟检查一次，实际业务时间来自同步的 JSON。每天论文任务只采集
公共元数据，模型解读和全文下载由 Mac 处理。所有时间按配置时区解释，不依赖系统时区。

## 同步协议与故障恢复

Mac 通过 SSH 调用远端虚拟环境 Python 的 `server-exchange`，向标准输入写入一个
JSON 对象，远端标准输出只能返回协议 JSON；运行日志应写入标准错误。

- 请求：`config` 是脱敏配置，`tasks` 是任务信封。
- 响应：`config_revision` 是已应用配置的 canonical JSON SHA256，`tasks` 是服务器队列。
- 信封仅包含任务标识、类型、计划时间、配置版本、状态、尝试次数与公共论文候选。
  不传本机结果、错误堆栈、私人正文、输出路径或执行命令。
- SSH 使用已有配置和严格主机密钥校验，不接受服务器提供的命令。
- 新远端任务进入本机时由本机重新认领；服务器状态不能替本机确认内容已生成。
- `.state/sync-status.json` 分别记录期望配置版本与上次确认的服务器版本。
  SSH 失败、协议无效或配置版本不匹配时不推进同步成功状态，本机继续按有效配置运行。
- 服务器重新连通后，统一任务 ID 去重；本机已完成状态作为确认回传。

## 部署验收

检查以下行为后再把服务器标记为已部署：

1. `systemctl --user status second-proprio.timer` 显示已启用，日志中的 `server-tick`
   成功运行，确认没有调用模型或下载全文。
2. Mac `sync` 返回成功，并显示服务器配置版本与本机期望版本相同。
3. 调整一项测试时间，同步后服务器按新配置生成工作项；非法配置不会替换有效配置。
4. 断网期间 Mac 仍可登记本地任务；恢复后不产生重复工作项，不覆盖本机结果。
5. 退出 SSH 并重启服务器后，用户计时器继续工作；Mac 登录后能自动同步待处理任务。
6. 随机检查传输 JSON 与远端目录，确认不存在个人聊天、笔记、心情或登录凭据。

## 实际部署记录（2026-10-30）

已完成：

- 使用严格主机密钥检查登录 `tencent-vps`；部署前目标项目目录和同名服务均不存在，未覆盖其他项目。
- 白名单上传约 66 KB 源码、脱敏服务器配置、Schema 和部署文件；校验模板中的关系信息已改为通用占位值。
- 安装 Ubuntu 的 Python 3.12 venv 支持和项目标准依赖；远端项目含虚拟环境约 27 MB，未安装 PyTorch 或 Codex。
- `systemd-analyze --user verify` 通过；`second-proprio.timer` 为 enabled/active，`second-proprio.service` 最近运行退出码 0。
- 为 `ubuntu` 启用 linger，断开 SSH 后重新连接仍可确认计时器工作；重启用户计时器并再次运行服务成功。
- 实际 arXiv 元数据采集取得 **26 条关键词初筛候选**，报告 `complete=true`、`errors=[]`；候选还需由 Mac 判断是否深读。
- 正式 Mac 后台于 15:08（Asia/Shanghai）完成 `config.sync`：`status=ok`，`current_revision`、`desired_revision`、`remote_revision` 相同；收发均为 13 个任务。原有 13 个任务 ID 与已完成成果完整保留。
- 独立临时目录中验证新日程登记、进程重开后的队列去重、完成确认、非法配置不替换有效文件；测试未改动正式日程。
- 远端不存在 `private`、`papers`、`courses` 数据目录；仅保存队列和公开论文元数据／请求缓存，无聊天、心情、登录凭据、PDF 或插图。

首次元数据采集约 2 分 40 秒，期间服务器持有执行锁，同步返回可观测的“已有执行器正在工作”。
Mac 本机调度和界面仍可用；采集完成后重试同步成功。这是当前服务器实现的已知行为。

**尚未执行整台 Ubuntu 主机重启，也未人为中断正式网络连接**，避免影响服务器其他业务。
目前验证的是用户服务重启、SSH 会话断开、协议重试及已有的模拟网络故障测试；整机重启和真实网络中断后的恢复仍列入 TODO，不能视作已验收。
