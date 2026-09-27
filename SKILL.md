---
name: wechat-article-collector
description: 通过随 skill 安装的独立微信读书采集服务，在 AI 对话中采集微信公众号文章、维护公众号列表、设置周期任务，并导出 Markdown 或 HTML；扫码、添加书架及 2041 人工验证由用户完成。
agent_created: true
---

# 微信公众号文章采集

此 skill 随包提供独立的**本地采集服务**，基于已成熟的微信读书采集实现，处理微信读书登录、书架、公众号名单、文章采集、定时和文件导出。本地服务仅监听 `127.0.0.1`，首次调用自动启动，并把会话、文章和任务保存在使用者本机。

## 调用入口

确定此 `SKILL.md` 所在目录，以下称 `<skill>`。Windows 运行 `py -3 <skill>\scripts\run.py ...`；macOS/Linux 用 `python3 <skill>/scripts/run.py ...`。需要 Python 3.11+。首次运行会为服务创建独立 Python 环境并安装依赖；没有 Chrome、Edge 或 Chromium 时会尝试安装 Playwright Chromium。之后用户直接在 AI 工具里用自然语言说要采集哪个公众号即可。`doctor` 可检查本地服务；所有 CLI 命令返回 JSON。

## 采集流程

“采集某公众号最近两天文章”运行 `run --name "公众号名" --days 2`，按北京时间包含今天和昨天。指定日期用 `--since YYYY-MM-DD --until YYYY-MM-DD`，两端均包含。导出格式：`md`（仅 Markdown）、`html`（仅阅读版 HTML）、`body-html`（仅原样正文片段）、`both`（三种都出）、`reading`（Markdown + 阅读版 HTML，不含原样正文片段，推荐）。

按 JSON `status` 继续：

- `needs_login`：将 `qrImage` 绝对路径作为 `![微信读书二维码](绝对路径)` 展示；用户扫码后重试原命令。
- `needs_shelf_add`：请用户在微信读书 App 将目标公众号加入书架，可附上返回的 `shelfUrl`；用户完成后重试。
- `needs_verification`：优先提供 `verificationUrl`。若为空，引导用户打开微信读书书架中的公众号手动完成验证；`adminUrl` 仅提供本地操作说明。用户完成后运行 `check-verification`，再执行原命令。2041 属于文章通道验证，不代表登录失效。
- `cooldown`：按采集服务提示等待，不密集重试。
- `running` / `unconfirmed`：记录 `taskId`、`jobId`、公众号、日期与格式；执行 `resume --task-id <ID> --job-id <ID> --name "公众号名" --since ... --until ... --format ...` 查询同一任务，避免重复入队。
- `completed`：报告新增文章数、完整文件数、部分内容数和保存路径。`queued` 仅表示入队，不能称为完成。

`run` 会核查登录、书架和验证状态，自动将书架中目标公众号加入本地名单，再入队采集、核对日志并导出。`status`、`shelf --name`、`jobs`、`logs --task-id` 用于诊断；`articles --name ... --days N` 查看已入库文章；`export --name ... --days N --format md|html|body-html|both|reading` 只导出，不重新采集。正文缺失时仅生成 `.partial.md` 摘要，不称为全文。

## 名单与周期

`accounts` 查看名单，`add --name "公众号名"` 从书架添加，`remove --name "公众号名"` 从名单删除。`schedule --names "甲" "乙" --daily 08:00 --title "每日采集" --format md` 创建或更新定时任务；也可用 `--cron "0 8 * * *"` 和 `--format html|body-html|both`。时区为亚洲/上海。`schedules` 查看，`unschedule --title` 删除。定时采集由本地服务执行并自动写文件。Windows 创建周期任务时会尝试设置登录自启；若 JSON 返回 `autostartWarning`，明确告知用户该任务目前只会在本地服务运行期间执行。其他系统需要自行设置登录自启。若用户只说“定期”而没有时间，先维护名单，再询问具体周期。

## 文件和权限

会话、数据库、服务令牌和归档文件默认在使用者主目录 `.wechat-article-collector`；可用 `WEREAD_SKILL_DATA_DIR`、`WEREAD_SKILL_OUTPUT_DIR` 配置。`html` 是适合本地阅读的窄栏页，尽量内嵌公众号图片；若返回 `remoteImages`，说明有图片未能离线保存。`body-html` 是采集到的正文 HTML 原样片段，不代表微信读书整页响应。

不要在对话、仓库或分享 ZIP 中包含微信读书会话、本地服务令牌、二维码和文章正文。本地服务令牌只用于本机 CLI 与服务通信。故障或接口细节见 [本地服务参考](references/service.md)，安装方式见 [README](README.md)。

### 沙箱/受控环境下的已知问题

在 WorkBuddy 等会对后台子进程做回收的受控环境里，`run.py` 用 `DETACHED_PROCESS` 启动的 uvicorn 服务可能在父进程结束后被回收，表现为每次 `run.py` 调用都冷启动新服务（`serviceUrl` 端口每次变化），登录轮询线程随进程丢失，扫码登录永不生效。应对：用宿主环境的「后台任务/常驻进程」机制直接托管 `python -m uvicorn service.main:app --host 127.0.0.1 --port <固定端口>`，再通过 `X-Collector-Key`（读数据目录 `local-token`）调 API 完成登录→采集→导出闭环，不要反复调用 `run.py`。首次装依赖若因超时失败，见 `ensure_runtime` 的补救提示。
