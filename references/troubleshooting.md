# 运维排查手册

记录采集「可以叫我才哥」等公众号时遇到的实际问题与定位方法。先分清症状属于哪一类，再按对应小节处理。

## 症状速查表

| 症状 | 判断要点 | 处理 |
| --- | --- | --- |
| `serviceUrl` 端口每次变化、扫码登录不生效 | 受控环境回收后台子进程 | 见「服务被回收」 |
| `login_status: true` 但读书架报「登录超时」 | 会话 token 已过期（距 `authorized_at` 十几天） | 见「登录会话过期」 |
| 采集报 `-2041` / `captcha_required` / 暂停 1800 秒 | 微信读书安全验证（滑块） | 见「滑块验证反复触发」 |
| 文章入库但 `content` 为空、只生成 `.partial.md` | 正文抓取受频控，未抓到 | 见「正文缺失与逐轮补全」 |

## 服务被回收（受控环境）

**表现**：`doctor` 或连续两次 `run` 的 `serviceUrl` 端口不断变化，`~/.wechat-article-collector/service.log` 里出现多段 `Uvicorn running on ...`，登录轮询线程丢失，扫码后状态不更新。

**原因**：`run.py` 用 `DETACHED_PROCESS` 启动 uvicorn，WorkBuddy 等受控环境会回收这类后台子进程。

**处理**：不要反复调用 `run.py`。用宿主环境的「后台任务/常驻进程」直接托管服务，固定端口：

```bash
export WEREAD_SKILL_DATA_DIR="$HOME/.wechat-article-collector"
export WEREAD_SKILL_BROWSER_ADMIN_URL="http://127.0.0.1:8092/verify"
cd <skill>
python -m uvicorn service.main:app --host 127.0.0.1 --port 8092 --no-access-log
```

服务稳定后，后续命令通过环境变量直连固定端口，绕开 `run.py` 的冷启动：

```bash
export WEREAD_SKILL_SERVICE_URL="http://127.0.0.1:8092"
export WEREAD_SKILL_SERVICE_TOKEN=$(cat "$HOME/.wechat-article-collector/local-token")
export WEREAD_SKILL_DATA_DIR="$HOME/.wechat-article-collector"
python scripts/collector_client.py run --name "公众号名" --days 10 --format reading
```

## 登录会话过期

**表现**：`/api/collector/weread/status` 返回 `login_status: true`、`saved_token: true`，但 `run` 报「读取微信读书书架失败：登录超时」（HTTP 409）。`authorized_at` 是十几天前。

**判断**：`login_status: true` 只表示本地有保存的 token，不代表 token 仍有效。判断会话是否真可用，看读书架是否成功，而不是看 `login_status`。

**处理**：重新扫码登录。

1. `POST /api/collector/weread/qrcode` 生成二维码。
2. `GET /api/collector/weread/qrcode-image` 下载二维码图片（注意是 `/api/collector/weread/qrcode-image`，不是 `/api/admin/...`，后者会 404）。
3. 把图片绝对路径作为 `![微信读书二维码](绝对路径)` 展示给用户扫码。
4. 用户扫码后复查 `status`，`authorized_at` 更新且读书架成功即恢复。

## 滑块验证反复触发

**表现**：`run` 返回 `needs_verification`，`error_code: -2041`、`reason: captcha_required`，提示「文章请求已暂停 1800 秒」。`status` 里 `article_channel_last_check_ok: false`。

**关键认知**：这是**文章通道的频控验证**，与登录无关，登录授权仍然有效，无需重新扫码。连续抓取正文会反复触发。

**处理**：

1. 从 `check-verification` 或 `status` 取 `article_channel_verification_url`，让用户打开完成滑块（或打开微信读书书架中的公众号手动验证）。
2. 用户完成后跑 `check-verification`，确认 `article_channel_last_check_ok: true`。
3. 继续原采集命令。可能再触发 1-2 次，重复即可。

## 正文缺失与逐轮补全

**表现**：`run` 返回 `completed`，但导出结果里大量是 `.partial.md`（仅标题+摘要，正文为空），只有少数 `.md`/`.html` 是完整正文。数据库 `articles.content` 字段长度为 0。

**原因**：正文需按 `reviewId` 单独请求 `/web/mp/content`，受频控影响只成功了一部分；每轮还有 `WEREAD_CONTENT_MAX_PER_RUN`（默认 10）上限与请求间隔限制。

**处理**：**重复运行同一个 `run` 命令即可自动补全**。服务端用 `known_articles()` 找出正文为空的 `source_id`，下次采集自动重新抓取这些文章的正文，无需额外命令或参数。每轮通常补全 2-3 篇，多跑几轮（轮间留几秒间隔）直到 `saved`/`partial` 不再变化。

**判断是否已到底**：

- 短标题版空正文、长标题版已有正文 → 二者是同一篇文章的重复 review 条目（列表项 vs 正文项），内容相同，不算缺失。
- 真正无正文的：连长标题正文项都抓不到正文的文章，通常是微信读书端本身未开放全文或该文章被限制。
- 卡住时查 `status` 的 `article_channel_retry_after_seconds`（>0 说明还在冷却）与 `article_channel_last_check_reason`（`captcha_required` 说明又要滑块）。

## 快速诊断命令

```bash
# 登录与验证状态
curl -s -H "X-Collector-Key: $TOKEN" http://127.0.0.1:8092/api/collector/weread/status

# 书架（判断会话是否真可用）
curl -s -H "X-Collector-Key: $TOKEN" http://127.0.0.1:8092/api/collector/weread/shelf

# 数据库正文为空检查（Windows 路径示例）
python -c "import sqlite3; c=sqlite3.connect(r'C:\Users\<你>\.wechat-article-collector\articles.sqlite3'); \
  [print(r[0], r[1], len(r[2] or '')) for r in c.execute('SELECT id,title,content FROM articles')]"
```

其中 `TOKEN=$(cat ~/.wechat-article-collector/local-token)`。
