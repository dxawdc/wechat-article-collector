"""通过 GitHub Contents API 批量上传 skill 文件（绕过 github.com git 协议被墙）。"""
import base64
import json
import os
import sys
import urllib.request

TOKEN = sys.argv[1]
OWNER = "dxawdc"
REPO = "wechat-article-collector"
ROOT = r"C:/Users/10210/.workbuddy/skills/wechat-article-collector"
BRANCH = "main"

# 只上传真实源码/文档文件，排除 .git 与运行时产物
SKIP_DIRS = {".git", "__pycache__", ".cache", ".wechat-article-collector"}
SKIP_EXT = {".pyc", ".pyo"}

def collect_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(tuple(SKIP_EXT)):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            out.append((rel, full))
    return sorted(out)

def api(path, method="GET", data=None):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, body, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def main():
    files = collect_files()
    print(f"待上传 {len(files)} 个文件")
    ok, fail = 0, 0
    for rel, full in files:
        # 跳过 .gitignore 里的空文件特例无关，全部上传
        with open(full, "rb") as f:
            raw = f.read()
        content = base64.b64encode(raw).decode("ascii")
        payload = {"message": f"add {rel}", "content": content, "branch": BRANCH}
        # 文件路径中的中文需 URL 编码
        from urllib.parse import quote
        code, resp = api(quote(rel), method="PUT", data=payload)
        if code in (200, 201):
            ok += 1
            print(f"  [OK] {rel}")
        else:
            fail += 1
            print(f"  [FAIL {code}] {rel}: {resp[:200]}")
    print(f"\n完成：成功 {ok}，失败 {fail}")

if __name__ == "__main__":
    main()
