"""Redirect-safe loopback requests to the bundled collector service."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


class CollectorClient:
    def __init__(self):
        self.url = os.getenv("WEREAD_SKILL_SERVICE_URL", "").rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path:
            raise ValueError("本地采集服务地址无效；请通过 run.py 启动")
        self.key = os.getenv("WEREAD_SKILL_SERVICE_TOKEN", "")
        if not self.key:
            raise ValueError("本地采集服务认证信息缺失；请通过 run.py 启动")

    def request(self, path: str, *, method: str = "GET", data: dict | None = None,
                binary: bool = False, authenticated: bool = True, timeout: int = 75):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("采集服务 API 路径无效")
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["X-Collector-Key"] = self.key
        req = Request(self.url + path, data=payload, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as response:
                body = response.read(30_000_001)
        except HTTPError as exc:
            try:
                parsed = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
                detail = str(parsed.get("detail") or parsed.get("message") or exc.code)[:300]
            except Exception:
                detail = str(exc.code)
            if exc.code in {301, 302, 303, 307, 308}:
                detail = "采集服务返回重定向"
            raise ApiError(f"采集服务 HTTP {exc.code}: {detail}", exc.code) from exc
        except URLError as exc:
            raise ApiError(f"本地采集服务不可用：{str(exc.reason)[:200]}") from exc
        if len(body) > 30_000_000:
            raise ApiError("采集服务响应超过 30 MB 限制")
        if binary:
            return body
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("采集服务未返回有效 JSON") from exc
        if not isinstance(result, dict) or result.get("code") != 0:
            raise ApiError(str(result.get("message") or "采集服务返回错误")[:300]
                           if isinstance(result, dict) else "采集服务返回结构无效")
        return result.get("data")

    def query(self, path: str, **values):
        values = {key: value for key, value in values.items() if value not in (None, "")}
        return self.request(path + ("?" + urlencode(values) if values else ""))

    def health(self):
        req = Request(self.url + "/api/health", headers={"Accept": "application/json"})
        try:
            with _OPENER.open(req, timeout=15) as response:
                body = response.read(4097)
        except (HTTPError, URLError) as exc:
            raise ApiError(f"本地采集服务健康检查失败：{str(exc)[:200]}") from exc
        if len(body) > 4096:
            raise ApiError("采集服务健康检查响应过大")
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("采集服务健康检查未返回有效 JSON") from exc
        if not isinstance(result, dict) or result.get("service") != "wechat-article-collector":
            raise ApiError("采集服务健康检查响应格式不正确")
        return result
