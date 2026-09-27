# 微信公众号文章采集 Skill

一个可分享的独立采集包：skill 本体、微信读书采集服务、SQLite 文章库和导出工具都在包内。每位使用者用自己的微信读书账号扫码登录，文章数据只保存在自己的电脑上，采集、存储、导出全程本地完成。

## 功能概览

- 在 AI 对话中用自然语言采集指定公众号的文章，无需手工抓取
- 维护本地公众号名单，支持单次采集与定时周期采集
- 导出为 Markdown、阅读版 HTML（内嵌图片）或原样正文 HTML
- 自动处理微信读书登录、书架添加、2041 人工验证等交互环节

## 安装

需要 Python 3.11+。首次运行需要联网安装 Python 依赖；若电脑没有可用的 Chrome、Edge 或 Chromium，会自动安装 Playwright Chromium。解压 ZIP 后在本目录运行：

```powershell
py -3 install.py --tool codex
```

macOS/Linux 改用 `python3 install.py --tool codex`。Claude Code 用 `--tool claude`；其他支持 `SKILL.md` 的 AI 工具用 `--tool custom --target <skills根目录>`。

安装后可在 AI 工具中说：“使用 wechat-article-collector，采集可以叫我才哥最近两天的文章。”若 AI 工具没有立即发现 skill，新开对话或刷新技能列表即可。

## 使用流程

### 1. 采集文章

初次请求会自动启动本地服务。之后每一步都可能需要你的配合，AI 会给出对应提示：

| 环节 | 说明 |
| --- | --- |
| 登录 | 没有有效微信读书登录时，AI 展示二维码，用微信「扫一扫」扫码并确认登录 |
| 书架 | 目标公众号不在书架时，在微信读书 App 里搜索并关注该公众号 |
| 2041 验证 | 遇到人机验证时，AI 提供验证链接或书架操作说明，完成后继续 |

采集结束后默认输出 Markdown，也可指定其他格式。

对话示例：

- “采集可以叫我才哥最近 2 天的文章，输出 Markdown。”
- “查看采集名单，把某某公众号加进去。”
- “每天北京时间 8 点采集甲、乙公众号，自动保存 HTML。”
- “把上周某某公众号的已采集文章导出原样正文 HTML。”

### 2. 导出格式

| 格式 | 输出内容 |
| --- | --- |
| `md` | 仅 Markdown 正文 |
| `html` | 仅阅读版窄栏页（内嵌图片） |
| `body-html` | 仅采集到的正文 HTML 原样片段 |
| `both` | Markdown + 阅读版 HTML + 原样正文，三种都出 |
| `reading` | Markdown + 阅读版 HTML，不含原样正文片段（多数阅读场景的推荐组合） |

### 3. 名单与定时采集

- 维护名单：`accounts` 查看，`add` 添加，`remove` 删除
- 定时任务：`schedule --names "甲" "乙" --daily 08:00 --title "每日采集" --format md`，或用 `--cron "0 8 * * *"` 指定 cron 表达式，时区为亚洲/上海
- 查看与删除：`schedules` 查看，`unschedule --title` 删除

定时任务由本地服务执行并自动写文件。Windows 创建周期任务时会尝试设置登录自启；若返回 `autostartWarning`，说明该任务目前只会在本地服务运行期间执行，需要处理提示，否则电脑重启后要先再次调用 skill 才能启动服务。macOS/Linux 当前不自动设置登录自启，需要自行配置。电脑关机期间不会采集。

## 常见问题处理

### 登录相关

- **扫码后仍提示未登录**：确认扫码后已在微信里点了「确认登录」。若在 WorkBuddy 等会回收后台进程的受控环境里使用，本地服务进程可能被回收，导致登录轮询线程丢失；此时应由宿主环境的「后台任务/常驻进程」机制托管服务进程（固定端口），而非反复调用 `run.py`。详见 `SKILL.md` 的「沙箱/受控环境下的已知问题」一节。
- **登录态失效（-2010）**：按提示重新扫码登录即可，不影响已入库的文章。

### 采集相关

- **2041 人机验证**：这是微信读书对文章通道的验证，不代表登录失效。优先使用 AI 提供的验证链接；若为空，打开微信读书书架中的目标公众号手动完成验证，然后运行 `check-verification` 再重试。不要密集重试，以免触发更严格的频控。
- **频控/冷却**：按服务提示等待，不要反复重试。
- **正文缺失**：部分文章只有摘要、无完整正文时，会生成 `.partial.md` 摘要文件，这属于正常情况，不视为全文采集成功。

### 导出显示

- **代码块挤成一行**：已内置处理——微信读书的代码块是 `<pre>` 内多个 `<code>`（每行一个），导出时会自动还原换行。
- **表格无边框**：阅读版 HTML 已内置表格样式（边框、斑马纹、表头底色），无需额外处理。

## 本地数据

- 运行环境：用户主目录 `.cache/wechat-article-collector/runtime`，可用 `WEREAD_SKILL_RUNTIME_DIR` 覆盖
- 会话、浏览器状态、SQLite、服务令牌和日志：用户主目录 `.wechat-article-collector`，可用 `WEREAD_SKILL_DATA_DIR` 覆盖
- 文章归档：上述目录的 `archive`，可用 `WEREAD_SKILL_OUTPUT_DIR` 覆盖

安装 ZIP 不包含任何人的会话、令牌、二维码或文章。服务只监听本机回环地址（`127.0.0.1`），自动生成的服务令牌只用于本机客户端与服务之间的通信。

## 目录结构

```
wechat-article-collector/
├── SKILL.md                 # skill 入口与调用说明
├── README.md                # 本文件
├── install.py               # 安装脚本
├── requirements.txt         # 依赖清单（版本锁定）
├── agents/openai.yaml       # Codex 侧接口描述
├── references/service.md    # 本地服务参考与已知坑
├── scripts/                 # CLI 客户端与导出器
│   ├── run.py               # 启动服务并运行 CLI（自动建运行环境）
│   ├── collector_client.py  # 对话 CLI 命令实现
│   ├── exporter.py          # Markdown / 阅读版 HTML / 原样正文导出
│   └── ...
└── service/                 # 本地采集服务
    ├── main.py              # FastAPI 入口（仅监听 127.0.0.1）
    ├── storage.py           # SQLite 文章库
    ├── output.py            # 定时任务导出
    └── integrations/        # 微信读书采集模块
        ├── weread.py        # 核心采集逻辑
        ├── weread_browser.py# 浏览器通道
        └── weread_captcha.py# 滑块/点选验证处理
```
