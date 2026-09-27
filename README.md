# 微信公众号文章采集 Skill

这是一个可分享的独立采集包：skill、微信读书采集服务、SQLite 文章库和导出工具都在包内。它从 GDN 已成熟的微信读书采集实现拆出需要的部分，**不需要安装或连接完整 GDN，也不需要任何 AI 模型 API**。每位使用者用自己的微信读书账号扫码，数据只保存在自己的电脑。

## 安装

需要 Python 3.11+。首次运行需要联网安装 Python 依赖；若电脑没有可用的 Chrome、Edge 或 Chromium，会自动安装 Playwright Chromium。解压 ZIP 后在本目录运行：

```powershell
py -3 install.py --tool codex
```

macOS/Linux 改用 `python3 install.py --tool codex`。Claude Code 用 `--tool claude`；其他支持 `SKILL.md` 的 AI 工具用 `--tool custom --target <skills根目录>`。安装后可在 AI 工具中说：“使用 wechat-article-collector，采集可以叫我才哥最近两天的文章。”若 AI 工具没有立即发现 skill，新开对话或刷新技能列表。无需额外 setup 或填写 Key。

## 使用过程

初次请求会自动启动本地服务。没有有效微信读书登录时，AI 展示二维码；公众号不在书架时，请使用者在微信读书 App 添加；遇到 2041 时，AI 提供微信读书验证链接或书架操作说明。采集结束后默认输出 Markdown，也可要求阅读版 HTML、原样正文 HTML 或多种格式组合（`reading` = Markdown + 阅读版 HTML，不含原样正文，是多数阅读场景的推荐组合）。

对话示例：

- “采集可以叫我才哥最近 2 天的文章，输出 Markdown。”
- “查看采集名单，把某某公众号加进去。”
- “每天北京时间 8 点采集甲、乙公众号，自动保存 HTML。”
- “把上周某某公众号的已采集文章导出原样正文 HTML。”

定时任务由本地服务执行，默认自动保存 Markdown。Windows 创建周期任务时会尝试设置登录自启；若返回 `autostartWarning`，请处理提示，否则电脑重启后要先再次调用 skill 才能启动服务。macOS/Linux 当前不自动设置登录自启，需要自行配置。电脑关机期间不会采集。

## 本地数据

- 运行环境：用户主目录 `.cache/wechat-article-collector/runtime`，可用 `WEREAD_SKILL_RUNTIME_DIR` 覆盖。
- 会话、浏览器状态、SQLite、服务令牌和日志：用户主目录 `.wechat-article-collector`，可用 `WEREAD_SKILL_DATA_DIR` 覆盖。
- 文章归档：上述目录的 `archive`，可用 `WEREAD_SKILL_OUTPUT_DIR` 覆盖。

安装 ZIP 不包含任何人的会话、令牌、二维码或文章。服务只监听本机回环地址，自动生成的服务令牌只用于本机客户端通信；它不是 AI API Key。
