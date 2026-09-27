# 独立采集服务参考

服务基于已成熟的微信读书采集模块 `service/integrations/weread.py`、`weread_browser.py` 和 `weread_captcha.py` 实现。`scripts/run.py` 首次调用自动启动服务；服务只监听 `127.0.0.1`，通过本机自动生成的令牌认证，令牌不需由用户复制到 AI 对话。

| 能力 | 本地接口 |
| --- | --- |
| 授权状态、二维码、人工验证复查 | `/api/collector/weread/status`、`qrcode`、`qrcode-image`、`check` |
| 微信读书书架 | `/api/collector/weread/shelf` |
| 公众号名单 | `/api/collector/accounts` |
| 单次采集、周期任务 | `/api/collector/collect`、`/api/collector/collect-tasks` |
| 队列和日志 | `/api/collector/jobs`、`/api/collector/collect-logs` |
| 文章列表与正文 | `/api/collector/articles` |

`-2041` 是微信读书文章通道验证，不应清除登录态。优先提供服务状态中的 `article_channel_verification_url`；若为空，让用户打开微信读书书架中的公众号手动验证。完成后执行一次 `check-verification`。采集过程的频控与冷却由移植的微信读书模块负责。

会话、浏览器状态、SQLite、服务日志和导出文件都在使用者自己的数据目录，安装 ZIP 不包含这些文件。`body-html` 是保存的正文片段；`html` 增加本地阅读容器；Markdown 从正文转换。定时任务按亚洲/上海时区由服务执行，并按配置格式自动写归档文件。macOS/Linux 上如需重启电脑后仍自动执行，应为本地服务配置用户级登录自启。

## 正文转换的两个已知坑（已修复）

微信读书正文 HTML 的结构和常见网页不同，`scripts/exporter.py` 里做了专门适配，改动均在 `render_html`（HTML 阅读版）与 `normalize_code_blocks`（Markdown 转换）两处，`service/output.py` 复用同一套函数，无需单独维护。

1. **代码块换行丢失**：微信读书的代码块是 `<pre>` 里套**多个 `<code>`**，每个 `<code>` 是一行，行与行之间既无 `\n` 也无 `<br>`。若直接交给 markdownify，多行代码会被拼成一行（空格连接）。修复方法：`normalize_code_blocks()` 在转换前把 `<pre>` 内每个 `<code>` 的纯文本提取出来、用 `\n` 连接，并去掉高亮 `<span>` 嵌套，再让 `<pre>` 承载纯文本。
2. **表格无样式**：正文里的表格是标准 `<table><thead><th>…<tbody><td>…` 结构，但单元格里套了 `<section>`（自带 margin）。`render_html` 的 CSS 原本没有 `table` 相关规则，浏览器会把表格渲染成无边框、无内边距、内容挤成一排。修复方法：CSS 里补 `article table`（边框折叠、100% 宽、超宽横向滚动）、`article th,td`（边框+内边距）、表头浅灰底加粗、偶数行斑马纹，并清掉 `article table p/section` 的 margin。

这两处修复都落在 `scripts/exporter.py`，后续采集任何公众号都会自动生效，无需每次手动处理。
