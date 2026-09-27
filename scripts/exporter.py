"""Export article content saved by the bundled collector service."""

from __future__ import annotations

import base64
import html
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .config import archive_dir
except ImportError:
    from config import archive_dir


IMAGE_HOSTS = {"mmbiz.qpic.cn", "wx.qlogo.cn", "mmbiz.qlogo.cn"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_IMAGE_OPENER = build_opener(_NoRedirect)


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:90] or "article"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def normalize_code_blocks(body: str) -> str:
    """把微信读书的代码块结构规整为可正确换行的形式。

    微信读书正文里，一个代码块是 ``<pre>`` 里套多个 ``<code>``，每个
    ``<code>`` 是一行代码，行与行之间没有任何换行符（也没有 ``<br>``）。
    markdownify / 浏览器直接渲染时会把所有行挤成一行。

    这里把每个 ``<pre>`` 内每个 ``<code>`` 的文本提取出来，用 ``\\n``
    连接成纯文本，替换掉 ``<pre>`` 里的高亮 ``<span>`` 结构，从而让
    Markdown 和 HTML 都能正确显示多行代码。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    changed = False
    for pre in soup.find_all("pre"):
        code_tags = pre.find_all("code")
        if not code_tags:
            continue
        # 每个 <code> 取纯文本；若单个 <code> 内已含换行则保留其内部换行
        lines = []
        for code in code_tags:
            text = code.get_text()
            # 去掉单行末尾可能残留的空白，但保留内部换行
            lines.append(text.rstrip())
        # 用换行连接所有行，作为 <pre> 的纯文本
        joined = "\n".join(lines)
        if not joined:
            continue
        # 清空 <pre> 内所有子节点，写入纯文本
        pre.clear()
        pre.append(joined)
        changed = True
    if not changed:
        return body
    return str(soup)


def embed_images(body: str) -> tuple[str, int, int]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(normalize_code_blocks(body), "html.parser")
    embedded = failed = total = 0
    for tag in soup.select("script,iframe,object,embed,form,style,link,meta,base"):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on") or attr.lower() in {"srcdoc", "style"}:
                tag.attrs.pop(attr, None)
        if tag.name == "a":
            href = str(tag.get("href") or "")
            if href and not href.startswith(("https://", "http://", "#")):
                tag.attrs.pop("href", None)
    for image in soup.select("img"):
        src = str(image.get("data-src") or image.get("src") or "")
        if src.startswith("//"):
            src = "https:" + src
        image["src"] = src if src.startswith("data:image/") or urlsplit(src).scheme == "https" else ""
        image.attrs.pop("data-src", None)
        image["loading"] = "lazy"
        image["decoding"] = "async"
        if src.startswith("data:image/"):
            embedded += 1
            continue
        parsed = urlsplit(src)
        if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS or total >= 20_000_000:
            failed += 1
            continue
        try:
            req = Request(src, headers={"Referer": "https://mp.weixin.qq.com/", "User-Agent": "Mozilla/5.0"})
            with _IMAGE_OPENER.open(req, timeout=15) as response:
                mime = response.headers.get_content_type()
                data = response.read(8_000_001)
            if not mime.startswith("image/") or len(data) > 8_000_000 or total + len(data) > 20_000_000:
                failed += 1
                continue
            image["src"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            total += len(data)
            embedded += 1
        except Exception:
            failed += 1
    return str(soup), embedded, failed


def render_html(article: dict) -> tuple[str, dict]:
    body, embedded, failed = embed_images(str(article["content"]))
    title = html.escape(str(article["title"]))
    account = html.escape(str(article["mp_name"]))
    day = html.escape(str(article.get("publish_time_text") or "")[:10])
    url = html.escape(str(article.get("url") or ""), quote=True)
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>{title}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#263445;font:16px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
main{{width:min(100% - 32px,720px);margin:32px auto;padding:36px 42px 54px;background:white;border-radius:14px;box-shadow:0 12px 38px #182a4014}}
h1{{font-size:28px;line-height:1.4;margin:0 0 14px;color:#172334}}.meta{{color:#6b7786;font-size:14px;margin-bottom:26px}}
a{{color:#2463a5;overflow-wrap:anywhere}}article{{overflow-x:hidden}}article *{{max-width:100%!important;box-sizing:border-box}}article img{{display:block;height:auto!important;margin:12px auto}}
article p,article section{{margin-top:0;margin-bottom:12px}}article pre{{background:#152c3d;color:#e6edf3;padding:12px;border-radius:6px;overflow-x:auto;font:12px/1.6 Menlo,Consolas,monospace;white-space:pre}}article pre code{{background:transparent;color:inherit}}article code{{background:#eef1f4;color:#c7254e;padding:1px 4px;border-radius:3px;font:0.88em Menlo,Consolas,monospace}}article table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:15px;display:block;overflow-x:auto}}article th,article td{{border:1px solid #dde3e9;padding:8px 12px;text-align:left;vertical-align:top}}article th{{background:#f0f4f8;font-weight:600;color:#172334;white-space:nowrap}}article td{{background:#fff}}article tr:nth-child(even) td{{background:#fafbfc}}article table p,article table section{{margin:0!important}}article table strong{{color:#172334}}@media(max-width:760px){{body{{background:white}}main{{width:100%;margin:0;padding:24px 18px 36px;border-radius:0;box-shadow:none}}h1{{font-size:24px}}}}
</style></head><body><main><header><h1>{title}</h1><div class="meta">{account} · {day} · <a href="{url}">查看原文</a></div></header>
<article>{body}</article></main></body></html>'''
    return page, {"embeddedImages": embedded, "remoteImages": failed}


def export_articles(client, account: dict, since: str, until: str, fmt: str) -> dict:
    from markdownify import markdownify

    result = {"account": account["name"], "saved": [], "partial": [], "remoteImages": []}
    output_root = archive_dir()
    offset = 0
    while True:
        page = client.query("/api/collector/articles", account_id=account["id"], date_from=since,
                            date_to=until, limit=50, offset=offset)
        rows = page.get("list", [])
        for row in rows:
            article = client.request(f"/api/collector/articles/{row['id']}")
            day = str(article.get("publish_time_text") or "")[:10] or "unknown-date"
            folder = output_root / safe_name(account["name"]) / day
            token = re.sub(r"[^A-Za-z0-9_-]", "", str(article["id"]))
            stem = safe_name(str(article["title"])) + "--article-" + token
            body = str(article.get("content") or "")
            if not body.strip():
                path = folder / (stem + ".partial.md")
                _write(path, f"# {article['title']}\n\n- 公众号：{article['mp_name']}\n- 日期：{day}\n- 原文：{article.get('url', '')}\n- 内容状态：仅摘要\n\n{article.get('digest', '')}\n")
                result["partial"].append(str(path))
                continue
            if fmt in ("md", "both", "reading"):
                path = folder / (stem + ".md")
                normalized = normalize_code_blocks(body)
                md = markdownify(normalized, heading_style="ATX")
                _write(path, f"# {article['title']}\n\n- 公众号：{article['mp_name']}\n- 日期：{day}\n- 原文：{article.get('url', '')}\n- 来源：微信读书采集\n\n{md.strip()}\n")
                result["saved"].append(str(path))
            if fmt in ("html", "both", "reading"):
                path = folder / (stem + ".html")
                rendered, stats = render_html(article)
                _write(path, rendered)
                result["saved"].append(str(path))
                if stats["remoteImages"]:
                    result["remoteImages"].append({"articleId": article["id"], **stats})
            if fmt in ("body-html", "both"):
                path = folder / (stem + ".body.html")
                _write(path, body)
                result["saved"].append(str(path))
        offset += len(rows)
        if not rows or offset >= page.get("total", 0):
            break
    return result
