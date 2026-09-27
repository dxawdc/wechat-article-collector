from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import random
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
import yaml
from bs4 import BeautifulSoup

from ..config import (
    WEREAD_AUTH_POLL_INTERVAL_SECONDS,
    WEREAD_BASE_URL,
    WEREAD_BROWSER_STATE_FILE,
    WEREAD_CONTENT_MAX_PER_RUN,
    WEREAD_MP_ARTICLES_COUNT,
    WEREAD_MP_ARTICLES_COUNT_PARAM,
    WEREAD_MP_ARTICLES_CURSOR_PARAM,
    WEREAD_MP_ARTICLES_ID_PARAM,
    WEREAD_MP_ARTICLES_PATH,
    WEREAD_QRCODE_FILE,
    WEREAD_RATE_LIMIT_COOLDOWN_SECONDS,
    WEREAD_REQUEST_MIN_INTERVAL_SECONDS,
    WEREAD_SESSION_FILE,
)
from .weread_browser import (
    WeReadBrowserChannel,
    WeReadBrowserChannelError,
    WeReadBrowserChannelUnavailable,
    obfuscate_weread_id,
)
from .weread_captcha import WeReadCaptchaManager


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# The MP article endpoint is an undocumented WeRead web route. It can require
# a short-lived page ticket even while the shelf endpoint accepts the same QR
# login state. A failed refresh must trip the circuit breaker instead of
# repeatedly probing every configured public account.
WEREAD_ARTICLE_CHANNEL_COOLDOWN_SECONDS = 30 * 60
WEREAD_AUTH_CHECK_RETRY_SECONDS = 3.0
WEREAD_AUTH_CHECK_COOLDOWN_SECONDS = 5 * 60
# A completed QR login needs a few seconds before WeRead's web endpoints see
# the new browser session consistently.  Waiting inside the first explicit
# check is much less confusing than returning a transient -2041 to the user.
WEREAD_POST_LOGIN_SETTLE_SECONDS = 5.0

WEREAD_ARTICLE_REASON_CAPTCHA = "captcha_required"
WEREAD_ARTICLE_REASON_BROWSER_UNAVAILABLE = "browser_unavailable"
WEREAD_ARTICLE_REASON_BROWSER_TIMEOUT = "browser_timeout"
WEREAD_ARTICLE_REASON_INVALID_RESPONSE = "invalid_response"
WEREAD_ARTICLE_REASON_SESSION_REFRESH = "session_refresh_failed"
WEREAD_ARTICLE_REASON_UPSTREAM = "upstream_error"

WEREAD_ARTICLE_ACTION_CAPTCHA = "complete_captcha"
WEREAD_ARTICLE_ACTION_REPAIR_BROWSER = "repair_browser"
WEREAD_ARTICLE_ACTION_RETRY = "retry_later"


class WeReadCollectorError(RuntimeError):
    """A recoverable failure reported by the WeRead data source."""

    def __init__(self, message: str, *, code: int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class WeReadRateLimitError(WeReadCollectorError):
    def __init__(self, message: str, retry_after_seconds: float = 0):
        super().__init__(message)
        self.retry_after_seconds = max(0.0, retry_after_seconds)


class WeReadArticleChannelError(WeReadCollectorError):
    """The saved login is present, but WeRead rejected the MP article route."""

    def __init__(
        self,
        message: str,
        retry_after_seconds: float = 0,
        *,
        code: int | None = None,
        reason: str = WEREAD_ARTICLE_REASON_UPSTREAM,
        action_required: str = WEREAD_ARTICLE_ACTION_RETRY,
    ):
        super().__init__(message, code=code)
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        self.reason = reason
        self.action_required = action_required


@dataclass
class WeReadSessionState:
    vid: str = ""
    skey: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    wr_ticket: str = ""
    wr_wrpa: str = ""
    ticket_updated_at: str = ""
    account_name: str = ""
    account_avatar: str = ""
    probe_source_id: str = ""
    authorized_at: str = ""
    updated_at: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.vid and self.skey)


class WeReadCollector:
    """Collect public-account metadata through an authorized WeRead session.

    WeRead's browser client issues a short QR-login bootstrap request, then
    attaches ``x-vid`` and ``x-skey`` to its authenticated requests.  This
    collector intentionally keeps those credentials in a separate local file
    and never sends them to an intermediary service.

    The article-list endpoint is kept configurable because WeRead's web API is
    not a public, versioned integration surface.  The currently observed web
    route is the default; an upstream change only needs an environment update,
    rather than a change to the collection pipeline.
    """

    def __init__(self):
        self.base_url = WEREAD_BASE_URL
        # Keep one browser fingerprint for an authorization session.  Changing
        # it on every request makes an otherwise valid web login look like it
        # is hopping between devices.
        saved_state = self.load_session()
        self._user_agent = saved_state.user_agent or random.choice(USER_AGENTS)
        self.session = self._new_http_session()
        self._restore_session_cookies(saved_state)
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._last_authenticated_request_at = 0.0
        self._rate_limit_until = 0.0
        self._article_channel_unavailable_until = 0.0
        self._article_channel_message = ""
        self._article_channel_reason = ""
        self._article_channel_error_code: int | None = None
        self._article_channel_action_required = ""
        self._auth_check_unavailable_until = 0.0
        self._login_in_progress = False
        self._login_notice = ""
        self._login_started_at = 0.0
        self._pending_login_uid = ""
        self._source_cache: list[dict] = []
        self._source_cache_at = 0.0
        self._browser_channel = WeReadBrowserChannel(
            self.base_url,
            storage_state_path=WEREAD_BROWSER_STATE_FILE,
        )
        self._captcha_manager = WeReadCaptchaManager(
            self.base_url,
            WEREAD_BROWSER_STATE_FILE,
            self._apply_captcha_success,
            browser_channel=self._browser_channel,
        )

    def status(self) -> dict:
        self._clear_stale_login()
        state = self.load_session()
        return {
            "mode": "weread",
            "ready": True,
            "login_status": state.ready,
            "saved_token": state.ready,
            "polling": self._login_in_progress,
            "has_qrcode": WEREAD_QRCODE_FILE.exists(),
            "account_name": state.account_name,
            "authorized_at": state.authorized_at or state.updated_at,
            "expiry_time": "",
            "article_channel_verification_url": self.article_channel_verification_url(
                state.probe_source_id
            ),
            "message": self._status_message(state),
        }

    def check_authorization(self, *, force_article_probe: bool = False) -> dict:
        """Check login and the MP article channel independently.

        Shelf access is necessary but insufficient: the article-list route can
        reject a session independently.  Returning the two states separately
        prevents a transient MP-route failure from being presented as a QR
        login expiration.
        """
        state = self.load_session()
        if not state.ready:
            return {
                "ok": False,
                "login_ok": False,
                "article_channel_ok": None,
                "message": "未找到微信读书授权，请重新扫码登录",
            }
        self._wait_for_recent_login_session(state)
        remaining = self._auth_check_unavailable_until - time.monotonic()
        if remaining > 0:
            return self._transient_auth_check_result(
                "微信读书上游检测冷却中，"
                f"约 {max(1, round(remaining))} 秒后自动重试；授权已保留，无需扫码"
            )

        # Once a shelf source has been discovered, the MP article route is a
        # closer probe for the actual collection path and avoids an extra shelf
        # request.  The source ID is persisted so this also survives restarts.
        if state.probe_source_id:
            result = self._check_persisted_probe(
                state.probe_source_id,
                allow_captcha_retry=force_article_probe,
            )
            if result is not None:
                return result
        try:
            shelf_payload = self._check_shelf_authorization()
        except WeReadCollectorError as exc:
            if self._is_transient_session_error(exc):
                self._auth_check_unavailable_until = time.monotonic() + WEREAD_AUTH_CHECK_COOLDOWN_SECONDS
                return self._transient_auth_check_result(
                    "微信读书上游暂时不可用（登录超时），已保留授权；"
                    "将在 5 分钟后自动重试，无需扫码"
                )
            return {"ok": False, "login_ok": False, "article_channel_ok": None, "message": str(exc)}

        try:
            sources = self._sources_from_shelf_payload(shelf_payload)
            self._source_cache = sources
            self._source_cache_at = time.monotonic()
        except WeReadCollectorError as exc:
            if self._is_transient_session_error(exc):
                return {
                    "ok": True,
                    "login_ok": True,
                    "article_channel_ok": None,
                    "message": f"微信读书登录有效；文章通道暂时无法检测：{exc}",
                }
            return {
                "ok": True,
                "login_ok": True,
                "article_channel_ok": False,
                "message": f"微信读书登录有效；读取书架详情失败：{exc}",
            }

        if not sources:
            return {
                "ok": True,
                "login_ok": True,
                "article_channel_ok": None,
                "message": "微信读书登录有效；书架没有可用于检测的公众号",
            }
        self._remember_probe_source(state, str(sources[0].get("fakeid") or ""))

        try:
            self._request_article_page(
                str(sources[0].get("fakeid") or ""),
                cursor=0,
                count=1,
                allow_captcha_retry=force_article_probe,
            )
        except WeReadArticleChannelError as exc:
            return self._article_channel_check_result(
                exc, str(sources[0].get("fakeid") or "")
            )
        except WeReadCollectorError as exc:
            if self._is_transient_session_error(exc):
                return {
                    "ok": True,
                    "login_ok": True,
                    "article_channel_ok": None,
                    "message": f"微信读书登录有效；文章通道检测暂时不可用：{exc}",
                }
            return {
                "ok": True,
                "login_ok": True,
                "article_channel_ok": False,
                "message": f"微信读书登录有效；文章通道异常：{exc}",
            }
        return {
            "ok": True,
            "login_ok": True,
            "article_channel_ok": True,
            "article_channel_reason": "",
            "article_channel_error_code": None,
            "article_channel_action_required": "",
            "article_channel_retry_after_seconds": 0,
            "message": "微信读书登录和公众号文章通道均可用",
        }

    def _check_shelf_authorization(self):
        """Retry one transient -2041 response before surfacing an unavailable check."""
        for attempt in range(2):
            try:
                return self._authenticated_json("GET", "/web/shelf/sync", "检测微信读书授权有效性")
            except WeReadCollectorError as exc:
                if attempt == 0 and self._is_transient_session_error(exc):
                    time.sleep(WEREAD_AUTH_CHECK_RETRY_SECONDS)
                    continue
                raise

    @staticmethod
    def _wait_for_recent_login_session(state: WeReadSessionState):
        """Let a freshly-issued QR session propagate before its first probe."""
        if not state.authorized_at:
            return
        try:
            authorized_at = datetime.fromisoformat(state.authorized_at)
        except (TypeError, ValueError):
            return
        elapsed = max(0.0, (datetime.utcnow() - authorized_at).total_seconds())
        remaining = WEREAD_POST_LOGIN_SETTLE_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _check_persisted_probe(
        self, source_id: str, *, allow_captcha_retry: bool = False
    ) -> dict | None:
        try:
            self._request_article_page(
                source_id,
                cursor=0,
                count=1,
                allow_captcha_retry=allow_captcha_retry,
            )
        except WeReadArticleChannelError as exc:
            return self._article_channel_check_result(exc, source_id)
        except WeReadCollectorError as exc:
            if self._is_transient_session_error(exc):
                self._auth_check_unavailable_until = time.monotonic() + WEREAD_AUTH_CHECK_COOLDOWN_SECONDS
                return self._transient_auth_check_result(
                    "微信读书上游暂时不可用（登录超时），已保留授权；"
                    "将在 5 分钟后自动重试，无需扫码"
                )
            # A deleted shelf source is not proof of a login problem. Fall back
            # to the shelf probe so it can discover a replacement source.
            return None
        return {
            "ok": True,
            "login_ok": True,
            "article_channel_ok": True,
            "article_channel_reason": "",
            "article_channel_error_code": None,
            "article_channel_action_required": "",
            "article_channel_retry_after_seconds": 0,
            "message": "微信读书登录和公众号文章通道均可用",
        }

    def _article_channel_check_result(
        self, error: WeReadArticleChannelError, source_id: str = ""
    ) -> dict:
        result = {
            "ok": True,
            "login_ok": True,
            "article_channel_ok": False,
            "article_channel_reason": error.reason,
            "article_channel_error_code": error.code,
            "article_channel_action_required": error.action_required,
            "article_channel_retry_after_seconds": max(0, round(error.retry_after_seconds)),
            "message": f"微信读书登录有效；公众号文章通道暂不可用：{error}",
        }
        if error.reason == WEREAD_ARTICLE_REASON_CAPTCHA:
            result["article_channel_verification_url"] = self.article_channel_verification_url(
                source_id
            )
        return result

    @staticmethod
    def _transient_auth_check_result(message: str) -> dict:
        return {
            "ok": None,
            "login_ok": None,
            "article_channel_ok": None,
            "transient": True,
            "message": message,
        }

    def _remember_probe_source(self, state: WeReadSessionState, source_id: str):
        if not source_id or state.probe_source_id == source_id:
            return
        state.probe_source_id = source_id
        state.cookies = self._session_cookie_values()
        self.save_session(state)

    def qrcode_path(self) -> Path:
        return WEREAD_QRCODE_FILE

    def article_channel_verification_url(self, source_id: str = "") -> str:
        """Return the user-openable reader URL that presents WeRead CAPTCHA."""
        value = str(source_id or "").strip()
        if not value:
            return ""
        return f"{self.base_url}/web/mp/reader/{obfuscate_weread_id(value)}"

    def start_article_captcha(self) -> dict:
        state = self._require_session()
        if not state.probe_source_id:
            raise WeReadCollectorError("尚未找到用于验证的微信公众号，请先执行一次授权检测")
        self._set_article_channel_cooldown(
            "管理员正在处理微信读书安全验证",
            reason=WEREAD_ARTICLE_REASON_CAPTCHA,
            error_code=-2041,
            action_required=WEREAD_ARTICLE_ACTION_CAPTCHA,
        )
        # Hand off the page to the administrator-driven interaction while the
        # shared persistent context remains owned by one browser channel.
        self._browser_channel.reset()
        return self._captcha_manager.start(
            source_id=state.probe_source_id,
            cookies=state.cookies,
            user_agent=state.user_agent or self._user_agent,
        )

    def article_captcha_status(self) -> dict:
        return self._captcha_manager.status()

    def article_captcha_screenshot(self) -> bytes:
        return self._captcha_manager.screenshot()

    def replay_article_captcha_drag(self, points: list[dict]) -> dict:
        return self._captcha_manager.replay_drag(points)

    def replay_article_captcha_click(self, point: dict) -> dict:
        return self._captcha_manager.replay_click(point)

    def confirm_article_captcha(self) -> dict:
        return self._captcha_manager.confirm()

    def focus_article_captcha(self) -> dict:
        return self._captcha_manager.focus()

    def cancel_article_captcha(self) -> dict:
        return self._captcha_manager.cancel()

    def _apply_captcha_success(self, cookies: dict[str, str]):
        with self._lock:
            state = self.load_session()
            if not state.ready:
                raise WeReadCollectorError("滑块已通过，但微信读书授权已被移除")
            state.cookies.update(cookies)
            state.vid = state.cookies.get("wr_vid", state.vid)
            state.skey = state.cookies.get("wr_skey", state.skey)
            state.updated_at = datetime.utcnow().isoformat()
            self._replace_session_cookies(state.cookies)
            self.save_session(state)
            self._clear_article_channel_cooldown()

    def close(self):
        """Release the one shared browser owner during application shutdown."""
        self._captcha_manager.close()
        self._browser_channel.close()

    def start_auth(self) -> dict:
        """Create a WeRead QR code and start the user-authorized login poll."""
        self._clear_stale_login(force_if_no_qrcode=True)
        with self._lock:
            if self._login_in_progress:
                return self._qrcode_response(self._login_notice or "微信读书二维码生成中，请稍后刷新")
            self._reset_qrcode()
            # A new QR login starts a new browser session.  Its User-Agent is
            # then persisted with the resulting authorization state.
            self._user_agent = random.choice(USER_AGENTS)
            self.session = self._new_http_session()
            self._login_notice = ""
            self._login_in_progress = True
            self._login_started_at = time.time()

        try:
            # Establish the same browser-session boundary used by WeRead's
            # maintained QR flow before asking for a login UID.  The response
            # can set auxiliary cookies that are not returned by getLoginUid.
            self.session.get(
                f"{self.base_url}/r/weread-skills",
                headers={**self._headers(), "Referer": f"{self.base_url}/"},
                timeout=(8, 20),
            ).raise_for_status()
            response = self.session.get(
                f"{self.base_url}/api/auth/getLoginUid",
                headers={**self._headers(), "Referer": f"{self.base_url}/r/weread-skills"},
                timeout=(8, 20),
            )
            payload = self._json_response(response, "获取微信读书登录二维码失败", invalidate_on_auth=False)
            uid = str(payload.get("uid") or "")
            if not uid:
                raise WeReadCollectorError("获取微信读书登录二维码失败：未返回登录标识")
            self._pending_login_uid = uid
            self._write_qrcode(f"{self.base_url}/web/confirm?uid={quote(uid, safe='')}")
            self._login_notice = "二维码已生成，请使用微信扫码登录微信读书"
            threading.Thread(target=self._poll_login, args=(uid,), daemon=True).start()
        except Exception as exc:
            with self._lock:
                self._login_in_progress = False
                self._login_notice = f"微信读书授权失败：{exc}"
        return self._qrcode_response(self._login_notice or "微信读书二维码生成中，请稍后刷新")

    def search_accounts(self, keyword: str, limit: int = 10, offset: int = 0) -> dict:
        """Return MP sources already present in the reader account's shelf.

        The collector uses ``fakeid`` as the storage field for source IDs;
        under this source it contains WeRead's MP book ID rather than a
        Official Account backend fakeid.
        """
        query = keyword.strip().lower()
        sources = self.list_sources()
        if query:
            sources = [
                source
                for source in sources
                if query in source["name"].lower() or query in source["intro"].lower()
            ]
        limit = max(1, min(int(limit or 10), 100))
        offset = max(0, int(offset or 0))
        return {"list": sources[offset : offset + limit], "total": len(sources), "mode": "weread"}

    def list_sources(self) -> list[dict]:
        payload = self._authenticated_json("GET", "/web/shelf/sync", "读取微信读书书架失败")
        return self._sources_from_shelf_payload(payload)

    def _sources_from_shelf_payload(self, payload: dict) -> list[dict]:
        books = self._extract_shelf_books(payload)
        sources: list[dict] = []
        seen: set[str] = set()
        for item in books:
            if not self._is_mp_book(item):
                continue
            source_id = str(item.get("bookId") or item.get("id") or item.get("mpId") or "")
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            name = str(item.get("name") or item.get("title") or item.get("mpName") or source_id)
            intro = str(item.get("intro") or item.get("description") or item.get("author") or "")
            sources.append(
                {
                    "name": name,
                    "fakeid": source_id,
                    "avatar": str(item.get("cover") or item.get("picUrl") or item.get("coverUrl") or ""),
                    "intro": intro,
                }
            )
        return sources

    def _resolve_source_id(self, account) -> str:
        """Return a WeRead MP book ID, migrating legacy account IDs by name.

        Some earlier implementations saved Official Account backend ``fakeid`` values. They are
        opaque Base64-like strings and cannot be passed to WeRead as ``bookId``.
        When the same account is already on the authorised reader's shelf, its
        ``MP_WXS_...`` book ID is used and persisted by the normal task commit.
        """
        source_id = str(getattr(account, "fakeid", "") or "").strip()
        if source_id.startswith("MP_WXS_"):
            return source_id

        account_name = str(getattr(account, "name", "") or "").strip()
        lookup_name = self._normalise_account_name(account_name)
        candidates = self._cached_sources()
        matched = next(
            (
                item
                for item in candidates
                if self._normalise_account_name(str(item.get("name") or "")) == lookup_name
            ),
            None,
        )
        if matched:
            resolved_id = str(matched.get("fakeid") or "")
            if resolved_id.startswith("MP_WXS_"):
                setattr(account, "fakeid", resolved_id)
                return resolved_id

        display_name = account_name or "this account"
        raise WeReadCollectorError(
            f"{display_name} is not mapped to a WeRead shelf source. "
            "Add the public account to the WeRead shelf, then refresh the collector list. "
            "A legacy WeChat fakeid cannot be used as a WeRead bookId."
        )

    def _cached_sources(self) -> list[dict]:
        # One shelf lookup is enough for a batch of legacy accounts and avoids
        # sending a separate synchronisation request for every account.
        if time.monotonic() - self._source_cache_at >= 600:
            self._source_cache = self.list_sources()
            self._source_cache_at = time.monotonic()
        return self._source_cache

    def _request_article_page(
        self,
        source_id: str,
        *,
        cursor: int,
        count: int,
        allow_captcha_retry: bool = False,
    ) -> dict:
        """Read one MP article page through WeRead's real WRPA browser path.

        ``-2041`` is WeRead's Tencent CAPTCHA branch, not a renewable login
        ticket.  The current web client signs each article-list query through
        ``window.__WRPA__.sr`` before it sends the request.  Running that same
        code in Chromium prevents the old unsigned-request loop that worked
        briefly after login and then remained stuck behind risk control.
        """
        if not source_id:
            raise WeReadCollectorError("微信读书公众号来源 ID 为空")
        self._raise_if_article_channel_unavailable(
            allow_captcha_retry=allow_captcha_retry
        )
        self._raise_if_rate_limited("读取微信读书公众号文章列表失败")
        state = self._require_session()
        params = {
            WEREAD_MP_ARTICLES_ID_PARAM: source_id,
            WEREAD_MP_ARTICLES_CURSOR_PARAM: cursor,
            WEREAD_MP_ARTICLES_COUNT_PARAM: count,
        }
        session_key = f"{state.vid}:{state.authorized_at or state.updated_at}"

        for attempt in range(2):
            try:
                result = self._browser_channel.fetch_articles(
                    source_id=source_id,
                    path=WEREAD_MP_ARTICLES_PATH,
                    params=params,
                    cookies=state.cookies,
                    user_agent=state.user_agent or self._user_agent,
                    session_key=session_key,
                )
            except WeReadBrowserChannelUnavailable as exc:
                self._raise_article_channel_failure(exc)
            except WeReadBrowserChannelError as exc:
                self._raise_article_channel_failure(exc)

            state.cookies = result.cookies or state.cookies
            if result.user_agent:
                state.user_agent = result.user_agent
                self._user_agent = result.user_agent
                self.session.headers["User-Agent"] = result.user_agent
            self._replace_session_cookies(state.cookies)
            state.updated_at = datetime.utcnow().isoformat()
            self.save_session(state)

            try:
                self._assert_weread_ok(result.payload, "读取微信读书公众号文章列表失败")
            except WeReadCollectorError as exc:
                # -2012 is the web-cookie session timeout used by WeRead's own
                # request wrapper. Refresh the cookie jar once, rebuild the
                # browser context, then retry with a new WRPA proof.
                if attempt == 0 and (exc.code == -2012 or "登录超时" in str(exc)):
                    state = self._refresh_web_session_cookies(state)
                    self._browser_channel.reset()
                    session_key = f"{state.vid}:{state.authorized_at or state.updated_at}:refresh"
                    continue
                if self._is_mp_article_channel_error(exc):
                    self._raise_article_channel_failure(exc)
                raise

            self._clear_article_channel_cooldown()
            return result.payload

        raise WeReadCollectorError("读取微信读书公众号文章列表失败：浏览器通道重试未完成")

    def _raise_article_channel_failure(self, error: Exception) -> None:
        reason, action_required = self._classify_article_channel_failure(error)
        error_code = getattr(error, "code", None)
        self._set_article_channel_cooldown(
            str(error),
            reason=reason,
            error_code=error_code,
            action_required=action_required,
        )
        wait_seconds = max(1, round(WEREAD_ARTICLE_CHANNEL_COOLDOWN_SECONDS))
        if reason == WEREAD_ARTICLE_REASON_CAPTCHA:
            message = (
                "微信读书触发安全验证，需要完成滑块；"
                f"文章请求已暂停 {wait_seconds} 秒，登录授权仍有效，无需重新扫码"
            )
        elif reason == WEREAD_ARTICLE_REASON_BROWSER_UNAVAILABLE:
            message = (
                "微信读书文章浏览器组件不可用；"
                f"文章请求已暂停 {wait_seconds} 秒，请检查服务器浏览器组件；"
                "登录授权不受影响，无需重新扫码"
            )
        elif reason == WEREAD_ARTICLE_REASON_BROWSER_TIMEOUT:
            message = (
                "微信读书文章浏览器请求超时；"
                f"文章请求已暂停 {wait_seconds} 秒，稍后自动重试，无需重新扫码"
            )
        elif reason == WEREAD_ARTICLE_REASON_INVALID_RESPONSE:
            message = (
                "微信读书文章接口返回异常；"
                f"文章请求已暂停 {wait_seconds} 秒，稍后自动重试，无需重新扫码"
            )
        elif reason == WEREAD_ARTICLE_REASON_SESSION_REFRESH:
            message = (
                "微信读书文章会话刷新失败；"
                f"文章请求已暂停 {wait_seconds} 秒，稍后自动重试，无需重新扫码"
            )
        else:
            message = (
                "微信读书公众号文章通道暂不可用，"
                f"已暂停文章请求 {wait_seconds} 秒；无需重新扫码"
            )
        raise WeReadArticleChannelError(
            message,
            retry_after_seconds=WEREAD_ARTICLE_CHANNEL_COOLDOWN_SECONDS,
            code=error_code,
            reason=reason,
            action_required=action_required,
        ) from error

    @staticmethod
    def _classify_article_channel_failure(error: Exception) -> tuple[str, str]:
        code = getattr(error, "code", None)
        text = str(error)
        if code == -2041:
            return WEREAD_ARTICLE_REASON_CAPTCHA, WEREAD_ARTICLE_ACTION_CAPTCHA
        if isinstance(error, WeReadBrowserChannelUnavailable) or "浏览器防风控组件不可用" in text:
            return WEREAD_ARTICLE_REASON_BROWSER_UNAVAILABLE, WEREAD_ARTICLE_ACTION_REPAIR_BROWSER
        if "超时" in text:
            if "登录超时" in text or "会话" in text:
                return WEREAD_ARTICLE_REASON_SESSION_REFRESH, WEREAD_ARTICLE_ACTION_RETRY
            return WEREAD_ARTICLE_REASON_BROWSER_TIMEOUT, WEREAD_ARTICLE_ACTION_RETRY
        if "非 JSON" in text or "无效数据" in text or "返回了无效" in text:
            return WEREAD_ARTICLE_REASON_INVALID_RESPONSE, WEREAD_ARTICLE_ACTION_RETRY
        return WEREAD_ARTICLE_REASON_UPSTREAM, WEREAD_ARTICLE_ACTION_RETRY

    def collect_articles(
        self,
        account,
        max_pages: int = 1,
        date_from: str = "",
        date_to: str = "",
        collect_content: bool = True,
        known_source_ids: set[str] | None = None,
        content_pending_source_ids: set[str] | None = None,
    ) -> list[dict]:
        source_id = self._resolve_source_id(account)
        if not source_id:
            raise WeReadCollectorError("该公众号没有微信读书来源 ID；请先从微信读书书架同步并重新添加")

        rows: list[dict] = []
        known_source_ids = known_source_ids or set()
        content_pending_source_ids = content_pending_source_ids or set()
        max_pages = max(1, min(int(max_pages or 1), 20))
        content_fetches = 0
        cursor = 0
        for _page in range(max_pages):
            payload = self._request_article_page(source_id, cursor=cursor, count=WEREAD_MP_ARTICLES_COUNT)
            page_rows = self._parse_article_rows(payload, account, collect_content)
            page_is_already_known = bool(page_rows) and all(
                str(row.get("source_id") or "") in known_source_ids for row in page_rows
            )
            selected_rows = [
                row
                for row in page_rows
                if (
                    not known_source_ids
                    or str(row.get("source_id") or "") not in known_source_ids
                    or str(row.get("source_id") or "") in content_pending_source_ids
                )
            ]
            selected_rows = self._filter_dates(selected_rows, date_from, date_to)
            if collect_content and content_fetches < WEREAD_CONTENT_MAX_PER_RUN:
                content_fetches += self._hydrate_article_content(
                    selected_rows,
                    WEREAD_CONTENT_MAX_PER_RUN - content_fetches,
                )
            rows.extend(selected_rows)
            if page_is_already_known:
                break
            if not page_rows or not self._has_more(payload, page_rows):
                break
            next_cursor = self._next_article_cursor(payload, cursor, page_rows)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return rows

    def load_session(self) -> WeReadSessionState:
        if not WEREAD_SESSION_FILE.exists():
            return WeReadSessionState()
        try:
            data = yaml.safe_load(WEREAD_SESSION_FILE.read_text(encoding="utf-8")) or {}
            return WeReadSessionState(
                vid=str(data.get("vid") or ""),
                skey=str(data.get("skey") or ""),
                cookies={
                    str(name): str(value)
                    for name, value in (data.get("cookies") or {}).items()
                    if name and value is not None
                }
                if isinstance(data.get("cookies"), dict)
                else {},
                user_agent=str(data.get("user_agent") or ""),
                wr_ticket=str(data.get("wr_ticket") or ""),
                wr_wrpa=str(data.get("wr_wrpa") or ""),
                ticket_updated_at=str(data.get("ticket_updated_at") or ""),
                account_name=str(data.get("account_name") or ""),
                account_avatar=str(data.get("account_avatar") or ""),
                probe_source_id=str(data.get("probe_source_id") or ""),
                authorized_at=str(data.get("authorized_at") or ""),
                updated_at=str(data.get("updated_at") or ""),
            )
        except Exception:
            return WeReadSessionState()

    def save_session(self, state: WeReadSessionState):
        WEREAD_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEREAD_SESSION_FILE.write_text(
            yaml.safe_dump(
                {
                    "vid": state.vid,
                    "skey": state.skey,
                    "cookies": state.cookies,
                    "user_agent": state.user_agent,
                    "wr_ticket": state.wr_ticket,
                    "wr_wrpa": state.wr_wrpa,
                    "ticket_updated_at": state.ticket_updated_at,
                    "account_name": state.account_name,
                    "account_avatar": state.account_avatar,
                    "probe_source_id": state.probe_source_id,
                    "authorized_at": state.authorized_at,
                    "updated_at": state.updated_at or datetime.utcnow().isoformat(),
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        try:
            WEREAD_SESSION_FILE.chmod(0o600)
        except OSError:
            # Windows does not support POSIX file modes; the existing local
            # user-level data directory remains the access boundary there.
            pass

    def invalidate_session(self, message: str = "微信读书授权已失效，请重新扫码登录"):
        self._login_notice = message
        self.session.cookies.clear()
        try:
            if WEREAD_SESSION_FILE.exists():
                WEREAD_SESSION_FILE.unlink()
            if WEREAD_BROWSER_STATE_FILE.exists():
                WEREAD_BROWSER_STATE_FILE.unlink()
        except Exception:
            pass

    def _poll_login(self, uid: str):
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                response = self.session.get(
                    f"{self.base_url}/api/auth/getLoginInfo",
                    params={"uid": uid},
                    headers={**self._headers(), "Referer": f"{self.base_url}/r/weread-skills"},
                    timeout=(8, 70),
                )
                payload = self._json_response(response, "轮询微信读书登录状态失败", invalidate_on_auth=False)
                if self._save_login_payload(payload):
                    self._login_notice = "微信读书授权已保存"
                    return
                logic_code = str(payload.get("logicCode") or "")
                if logic_code in {"LOGIN_TIMEOUT", "OTP_EXPIRED"}:
                    self._login_notice = "微信读书二维码已过期，请重新获取"
                    return
                time.sleep(max(1.0, WEREAD_AUTH_POLL_INTERVAL_SECONDS))
            self._login_notice = "微信读书二维码已过期，请重新获取"
        except Exception as exc:
            self._login_notice = f"微信读书授权失败：{exc}"
        finally:
            with self._lock:
                self._login_in_progress = False
                self._pending_login_uid = ""

    def _save_login_payload(self, payload: dict) -> bool:
        if not payload.get("succeed"):
            return False
        vid = str(payload.get("webLoginVid") or payload.get("vid") or "")
        skey = str(payload.get("accessToken") or payload.get("skey") or "")
        refresh_token = str(payload.get("refreshToken") or "")
        if not (vid and skey):
            return False
        # QR credentials are response fields. Do not rely on the endpoint also
        # mirroring all four values into Set-Cookie; that behavior differs by
        # region and has changed before.
        self._set_session_cookie("wr_vid", vid)
        self._set_session_cookie("wr_skey", skey)
        self._set_session_cookie("wr_ql", "0")
        if refresh_token:
            self._set_session_cookie("wr_rt", quote(refresh_token, safe=""))
        authorized_at = datetime.utcnow().isoformat()
        state = WeReadSessionState(
            vid=vid,
            skey=skey,
            cookies=self._session_cookie_values(),
            user_agent=self._user_agent,
            authorized_at=authorized_at,
            updated_at=authorized_at,
        )
        try:
            profile_response = self.session.get(
                f"{self.base_url}/api/userInfo",
                params={"userVid": vid},
                headers=self._headers(state),
                timeout=(8, 20),
            )
            profile = self._json_response(profile_response, "读取微信读书用户信息失败", invalidate_on_auth=False)
            profile = profile.get("data") if isinstance(profile.get("data"), dict) else profile
            if isinstance(profile, dict):
                state.account_name = str(profile.get("name") or profile.get("nick") or "")
                state.account_avatar = str(profile.get("avatar") or "")
        except Exception:
            # The token is usable even if the optional display-name request
            # happens to fail, so do not discard a completed QR login.
            pass
        # Login and the follow-up profile request can both set browser
        # cookies.  Capture the final cookie jar immediately before saving.
        state.cookies = self._session_cookie_values()
        self.save_session(state)
        # A new QR identity must not inherit authentication storage from the
        # previous account.  The browser channel will rebuild and persist a
        # complete state for this session on its first successful request.
        try:
            WEREAD_BROWSER_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        # The dashboard uses the QR image's presence as its display signal.
        # Remove it as soon as the credential is persisted so a completed
        # authorization never leaves a stale QR code on screen.
        self._reset_qrcode()
        self._browser_channel.reset()
        return True

    def _refresh_web_session_cookies(self, state: WeReadSessionState) -> WeReadSessionState:
        """Refresh browser cookies without replacing the valid QR identity."""
        try:
            response = self.session.get(
                f"{self.base_url}/api/userInfo",
                params={"userVid": state.vid},
                headers={**self._headers(state), "Referer": f"{self.base_url}/r/weread-skills"},
                timeout=(8, 20),
            )
            payload = self._json_response(response, "刷新微信读书网页会话失败", invalidate_on_auth=False)
            self._assert_weread_ok(payload, "刷新微信读书网页会话失败", response.status_code)
        except requests.RequestException as exc:
            raise WeReadCollectorError("刷新微信读书网页会话失败：上游请求不可用") from exc

        state.cookies = self._session_cookie_values()
        refreshed_skey = state.cookies.get("wr_skey")
        if refreshed_skey:
            state.skey = refreshed_skey
        state.updated_at = datetime.utcnow().isoformat()
        self.save_session(state)
        return state

    def _authenticated_json(
        self,
        method: str,
        path: str,
        label: str,
        *,
        state: WeReadSessionState | None = None,
        **kwargs,
    ) -> dict:
        state = state or self._require_session()
        self._raise_if_rate_limited(label)
        request_headers = self._headers(state, include_article_ticket=path == WEREAD_MP_ARTICLES_PATH)
        custom_headers = kwargs.pop("headers", None)
        if isinstance(custom_headers, dict):
            request_headers.update(custom_headers)
        with self._request_lock:
            wait_seconds = max(
                self._last_authenticated_request_at + WEREAD_REQUEST_MIN_INTERVAL_SECONDS - time.monotonic(),
                self._rate_limit_until - time.monotonic(),
                0.0,
            )
            if wait_seconds:
                time.sleep(wait_seconds)
            response = self.session.request(
                method,
                self._url(path),
                headers=request_headers,
                timeout=(8, 35),
                **kwargs,
            )
            self._last_authenticated_request_at = time.monotonic()
        payload = self._json_response(response, label)
        self._assert_weread_ok(payload, label, response.status_code)
        return payload

    def _authenticated_text(self, method: str, path: str, label: str, **kwargs) -> str:
        """Issue an authenticated request whose successful payload is HTML.

        The MP content route returns an article page instead of JSON, but error
        responses can still be JSON. Both forms are checked before the HTML is
        handed to the article-body extractor.
        """
        state = self._require_session()
        self._raise_if_rate_limited(label)
        with self._request_lock:
            wait_seconds = max(
                self._last_authenticated_request_at + WEREAD_REQUEST_MIN_INTERVAL_SECONDS - time.monotonic(),
                self._rate_limit_until - time.monotonic(),
                0.0,
            )
            if wait_seconds:
                time.sleep(wait_seconds)
            response = self.session.request(
                method,
                self._url(path),
                headers=self._headers(state),
                timeout=(8, 35),
                **kwargs,
            )
            self._last_authenticated_request_at = time.monotonic()

        if response.status_code in (401, 403):
            self.invalidate_session()
            raise WeReadCollectorError(f"{label}: authorization expired")
        if response.status_code == 429:
            self._set_rate_limit_cooldown(WEREAD_RATE_LIMIT_COOLDOWN_SECONDS)
            raise WeReadRateLimitError(f"{label}: upstream rate limit", WEREAD_RATE_LIMIT_COOLDOWN_SECONDS)
        if response.status_code >= 400:
            raise WeReadCollectorError(f"{label}: HTTP {response.status_code}")

        try:
            payload = response.json()
        except Exception:
            return str(response.text or "")
        if not isinstance(payload, dict):
            return ""
        self._assert_weread_ok(payload, label, response.status_code)
        data = self._unwrap(payload)
        for key in ("content", "html", "articleContent"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return ""

    def _extract_shelf_books(self, payload: dict) -> list[dict]:
        data = self._unwrap(payload)
        books = self._find_list(data, ("books", "bookInfos", "items", "list"))
        if books and any(self._looks_like_book(item) for item in books):
            return books
        book_ids = [
            str(item.get("bookId") or item.get("id") or "")
            for item in books
            if isinstance(item, dict) and (item.get("bookId") or item.get("id"))
        ]
        if not book_ids:
            return []
        resolved: list[dict] = []
        for start in range(0, len(book_ids), 20):
            detail = self._authenticated_json(
                "POST",
                "/web/shelf/syncBook",
                "读取微信读书书架详情失败",
                json={"bookIds": book_ids[start : start + 20]},
            )
            resolved.extend(self._find_list(self._unwrap(detail), ("books", "bookInfos", "items", "list")))
        return resolved

    def _parse_article_rows(self, payload: dict, account, collect_content: bool) -> list[dict]:
        data = self._unwrap(payload)
        items = self._find_list(data, ("articles", "articleList", "list", "items"))
        if not items:
            items = self._mp_review_items(data)
        rows: list[dict] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or item.get("articleId") or item.get("appmsgid") or "")
            url = str(item.get("url") or item.get("link") or item.get("articleUrl") or "")
            if not source_id and url:
                source_id = url
            if not source_id:
                source_id = f"weread-{getattr(account, 'fakeid', '')}-{index}"
            if not url and source_id and not source_id.startswith("http"):
                url = f"{self.base_url}/web/mp/content?reviewId={quote(source_id, safe='')}"
            content = ""
            if collect_content:
                content = str(item.get("content") or item.get("html") or item.get("articleContent") or "")
            rows.append(
                {
                    "source_id": source_id,
                    "review_id": str(item.get("reviewId") or source_id),
                    "title": str(item.get("title") or item.get("name") or ""),
                    "url": url,
                    "cover": str(item.get("picUrl") or item.get("cover") or item.get("coverUrl") or ""),
                    "digest": str(item.get("digest") or item.get("summary") or item.get("intro") or ""),
                    "content": content,
                    "publish_time": self._to_datetime(
                        item.get("publishTime") or item.get("updateTime") or item.get("createTime")
                    ),
                }
            )
        return rows

    def fetch_article_content(self, row: dict) -> str:
        review_id = str(row.get("review_id") or row.get("source_id") or "")
        if not review_id.startswith("MP_WXS_"):
            return ""
        return self._fetch_mp_article_content(review_id)

    def _hydrate_article_content(self, rows: list[dict], capacity: int) -> int:
        """Fetch WeRead article HTML for the first uncached rows in a batch."""
        attempted = 0
        for row in rows:
            if attempted >= capacity or row.get("content"):
                continue
            review_id = str(row.get("review_id") or row.get("source_id") or "")
            if not review_id.startswith("MP_WXS_"):
                continue
            attempted += 1
            try:
                row["content"] = self.fetch_article_content(row)
            except WeReadRateLimitError:
                # The cooldown is recorded centrally. Keep the metadata already
                # fetched in this batch and do not issue another content call.
                break
            except WeReadCollectorError:
                # Individual article access can be unavailable (deleted, paid,
                # or session-restricted). It must not discard the whole list.
                row["content"] = ""
        return attempted

    def _fetch_mp_article_content(self, review_id: str) -> str:
        html = self._authenticated_text(
            "GET",
            "/web/mp/content",
            "Read WeRead article content failed",
            params={"reviewId": review_id},
        )
        return self._extract_article_content(html)

    @staticmethod
    def _extract_article_content(html: str) -> str:
        if not html or len(html) < 20:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
        if not content:
            return ""
        for tag in content.select("script, style, svg, iframe, link, meta"):
            tag.decompose()
        for tag in content.find_all(True):
            style = tag.get("style")
            if style:
                style = re.sub(r"visibility\s*:\s*hidden\s*;?", "visibility: visible;", style, flags=re.I)
                style = re.sub(r"opacity\s*:\s*0\s*;?", "opacity: 1;", style, flags=re.I)
                tag["style"] = style
        for image in content.find_all("img"):
            source = image.get("data-src") or image.get("src")
            if source:
                image["src"] = source
            for attribute in ("data-src", "data-ratio", "data-w", "data-type"):
                image.attrs.pop(attribute, None)
        return str(content)

    @staticmethod
    def _mp_review_items(data: dict) -> list[dict]:
        """Normalise the ``reviews[].subReviews[]`` MP article-list shape."""
        items: list[dict] = []
        reviews = data.get("reviews") if isinstance(data, dict) else []
        if not isinstance(reviews, list):
            return items
        for group in reviews:
            if not isinstance(group, dict):
                continue
            for sub_review in group.get("subReviews") or []:
                if not isinstance(sub_review, dict):
                    continue
                review = sub_review.get("review")
                review = review if isinstance(review, dict) else sub_review
                mp_info = review.get("mpInfo") if isinstance(review.get("mpInfo"), dict) else {}
                review_id = str(review.get("reviewId") or sub_review.get("reviewId") or mp_info.get("originalId") or "")
                if not review_id:
                    continue
                items.append(
                    {
                        "id": review_id,
                        "title": mp_info.get("title") or review.get("title") or "",
                        "url": (
                            mp_info.get("content_url")
                            or mp_info.get("contentUrl")
                            or mp_info.get("source_url")
                            or mp_info.get("sourceUrl")
                            or mp_info.get("url")
                            or review.get("content_url")
                            or review.get("contentUrl")
                            or review.get("url")
                            or ""
                        ),
                        "picUrl": mp_info.get("pic_url") or mp_info.get("picUrl") or review.get("picUrl") or "",
                        "digest": mp_info.get("digest") or review.get("digest") or "",
                        "content": review.get("content") or "",
                        "publishTime": review.get("createTime") or mp_info.get("createTime") or 0,
                        "idx": sub_review.get("idx") or review.get("idx"),
                    }
                )
        return items

    def _has_more(self, payload: dict, rows: list[dict]) -> bool:
        data = self._unwrap(payload)
        for key in ("hasMore", "has_more"):
            if key in data:
                return bool(data.get(key))
        next_page = data.get("nextPage") or data.get("next_page")
        if next_page not in (None, "", 0, "0"):
            return True
        return len(rows) >= 10

    @staticmethod
    def _next_article_cursor(payload: dict, cursor: int, rows: list[dict]) -> int:
        data = WeReadCollector._unwrap(payload)
        for key in ("nextMaxIdx", "nextIdx", "next_index", "nextOffset"):
            value = data.get(key)
            try:
                if value not in (None, ""):
                    return int(value)
            except (TypeError, ValueError):
                continue
        indexes = [item.get("idx") for item in rows if isinstance(item.get("idx"), int)]
        if indexes:
            return max(indexes)
        return cursor + len(rows)

    def _assert_weread_ok(self, payload: dict, label: str, status_code: int = 200):
        message = str(payload.get("errMsg") or payload.get("message") or payload.get("info") or "")
        code = payload.get("errCode", payload.get("code", 0))
        try:
            code = int(code or 0)
        except (TypeError, ValueError):
            code = -1
        if status_code == 429 or self._is_rate_limited(code, message):
            self._set_rate_limit_cooldown(WEREAD_RATE_LIMIT_COOLDOWN_SECONDS)
            wait_seconds = max(1, round(WEREAD_RATE_LIMIT_COOLDOWN_SECONDS))
            raise WeReadRateLimitError(
                f"{label}：微信读书触发频控，已进入冷却期；请约 {wait_seconds} 秒后再试",
                retry_after_seconds=WEREAD_RATE_LIMIT_COOLDOWN_SECONDS,
            )
        if self._is_auth_error(code, message, status_code):
            self.invalidate_session()
            raise WeReadCollectorError(
                f"{label}：微信读书授权已失效，请重新扫码登录",
                code=code,
                status_code=status_code,
            )
        if code not in (0, 200):
            raise WeReadCollectorError(f"{label}：{message or code}", code=code, status_code=status_code)

    def _json_response(self, response: requests.Response, label: str, invalidate_on_auth: bool = True) -> dict:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if response.status_code in (401, 403):
                if invalidate_on_auth:
                    self.invalidate_session()
                raise WeReadCollectorError(
                    f"{label}：微信读书授权已失效，请重新扫码登录",
                    status_code=response.status_code,
                ) from exc
            if response.status_code == 429:
                self._set_rate_limit_cooldown(WEREAD_RATE_LIMIT_COOLDOWN_SECONDS)
                raise WeReadRateLimitError(f"{label}：微信读书触发频控", WEREAD_RATE_LIMIT_COOLDOWN_SECONDS) from exc
            raise WeReadCollectorError(f"{label}：HTTP {response.status_code}", status_code=response.status_code) from exc
        try:
            data = response.json()
        except Exception as exc:
            raise WeReadCollectorError(f"{label}：微信读书返回非 JSON 响应") from exc
        if not isinstance(data, dict):
            raise WeReadCollectorError(f"{label}：微信读书返回无效响应")
        return data

    def _require_session(self) -> WeReadSessionState:
        state = self.load_session()
        if not state.ready:
            raise WeReadCollectorError("请先完成微信读书扫码授权")
        return state

    def _headers(self, state: WeReadSessionState | None = None, *, include_article_ticket: bool = False) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self.base_url}/web/shelf",
            "User-Agent": self._user_agent,
        }
        if state and state.ready:
            headers["x-vid"] = state.vid
            headers["x-skey"] = state.skey
        if include_article_ticket and state:
            if state.wr_ticket:
                headers["x-wr-ticket"] = state.wr_ticket
            if state.wr_wrpa:
                headers["x-wrpa-0"] = state.wr_wrpa
        return headers

    def _renew_article_ticket(self, state: WeReadSessionState | None = None) -> WeReadSessionState:
        """Refresh the ticket required by the MP article-list web route."""
        state = state or self._require_session()
        self._raise_if_rate_limited("刷新微信读书公众号文章票据失败")
        with self._request_lock:
            wait_seconds = max(
                self._last_authenticated_request_at + WEREAD_REQUEST_MIN_INTERVAL_SECONDS - time.monotonic(),
                self._rate_limit_until - time.monotonic(),
                0.0,
            )
            if wait_seconds:
                time.sleep(wait_seconds)
            try:
                response = self.session.post(
                    self._url("/web/login/renewal"),
                    json={"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
                    headers={
                        # This is a Web-reader cookie renewal, not the QR
                        # bootstrap API used for shelf access. Keeping its
                        # header set in the browser-cookie context avoids
                        # mixing the two authentication schemes.
                        **self._headers(),
                        "Content-Type": "application/json;charset=UTF-8",
                        "Origin": self.base_url,
                        "Referer": f"{self.base_url}/web/book/read",
                    },
                    timeout=(8, 15),
                )
            except requests.RequestException as exc:
                raise WeReadCollectorError("刷新微信读书公众号文章票据失败：上游请求超时或网络不可用") from exc
            self._last_authenticated_request_at = time.monotonic()

        payload = self._json_response(response, "刷新微信读书公众号文章票据失败", invalidate_on_auth=False)
        success = payload.get("succ", payload.get("success"))
        if success not in (1, True, "1", "true", "True"):
            raise WeReadCollectorError("刷新微信读书公众号文章票据失败：上游未确认续期")

        response_headers = getattr(response, "headers", {}) or {}
        ticket = str(response_headers.get("x-wr-ticket") or response_headers.get("X-WR-Ticket") or "")
        wrpa = str(response_headers.get("x-wrpa-0") or response_headers.get("X-WRPA-0") or "")
        if not ticket:
            raise WeReadCollectorError("刷新微信读书公众号文章票据失败：上游未返回 x-wr-ticket")

        state.cookies = self._session_cookie_values()
        state.wr_ticket = ticket
        state.wr_wrpa = wrpa
        state.ticket_updated_at = datetime.utcnow().isoformat()
        self.save_session(state)
        return state

    def _new_http_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self._headers())
        return session

    def _restore_session_cookies(self, state: WeReadSessionState):
        if state.cookies:
            self._replace_session_cookies(state.cookies)

    def _replace_session_cookies(self, cookies: dict[str, str]):
        self.session.cookies.clear()
        for name, value in cookies.items():
            if name and value is not None:
                self._set_session_cookie(str(name), str(value))

    def _set_session_cookie(self, name: str, value: str):
        # Keep one canonical cookie per name. Mixing host-only response cookies
        # with manually installed domain cookies can otherwise send duplicate
        # wr_skey/wr_vid values in an undefined order.
        for cookie in list(self.session.cookies):
            if cookie.name != name:
                continue
            try:
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
            except KeyError:
                pass
        self.session.cookies.set(
            name,
            value,
            domain=".weread.qq.com",
            path="/",
            secure=True,
        )

    def _session_cookie_values(self) -> dict[str, str]:
        return {
            str(cookie.name): str(cookie.value)
            for cookie in self.session.cookies
            if cookie.name and cookie.value is not None
        }

    def _write_qrcode(self, url: str):
        try:
            import qrcode
        except ImportError as exc:
            raise WeReadCollectorError("缺少二维码依赖；请重新安装采集服务依赖") from exc
        image = qrcode.make(url)
        WEREAD_QRCODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        image.save(WEREAD_QRCODE_FILE)

    def _qrcode_response(self, message: str) -> dict:
        return {
            "mode": "weread",
            "code": f"/api/admin/wechat/qrcode-image?t={int(time.time())}",
            "is_exists": WEREAD_QRCODE_FILE.exists(),
            "message": message,
        }

    def _reset_qrcode(self):
        try:
            if WEREAD_QRCODE_FILE.exists():
                WEREAD_QRCODE_FILE.unlink()
        except Exception:
            pass

    def _clear_stale_login(self, force_if_no_qrcode: bool = False):
        with self._lock:
            if not self._login_in_progress:
                return
            elapsed = time.time() - (self._login_started_at or time.time())
            if (force_if_no_qrcode and not WEREAD_QRCODE_FILE.exists() and elapsed > 8) or elapsed > 310:
                self._login_in_progress = False
                self._pending_login_uid = ""
                if elapsed > 310:
                    self._login_notice = "微信读书二维码已过期，请重新获取"

    def _status_message(self, state: WeReadSessionState) -> str:
        if self._login_notice:
            return self._login_notice
        if self._login_in_progress:
            return "正在等待微信读书扫码确认"
        if state.ready:
            return "已保存微信读书授权"
        return "未授权，请获取二维码并用微信扫码登录微信读书"

    def _raise_if_rate_limited(self, label: str):
        with self._request_lock:
            remaining = self._rate_limit_until - time.monotonic()
        if remaining > 0:
            raise WeReadRateLimitError(
                f"{label}：微信读书当前处于冷却期，请约 {max(1, round(remaining))} 秒后再试",
                retry_after_seconds=remaining,
            )

    def _set_rate_limit_cooldown(self, seconds: float):
        with self._request_lock:
            self._rate_limit_until = max(self._rate_limit_until, time.monotonic() + max(0.0, seconds))

    def _raise_if_article_channel_unavailable(
        self, *, allow_captcha_retry: bool = False
    ):
        with self._request_lock:
            remaining = self._article_channel_unavailable_until - time.monotonic()
            message = self._article_channel_message
            reason = self._article_channel_reason or WEREAD_ARTICLE_REASON_UPSTREAM
            error_code = self._article_channel_error_code
            action_required = self._article_channel_action_required or WEREAD_ARTICLE_ACTION_RETRY
        if (
            remaining > 0
            and allow_captcha_retry
            and reason == WEREAD_ARTICLE_REASON_CAPTCHA
        ):
            return
        if remaining > 0:
            if reason == WEREAD_ARTICLE_REASON_CAPTCHA:
                cooldown_message = (
                    "微信读书文章通道正在等待安全验证；"
                    f"请完成验证后立即检测，当前冷却剩余约 {max(1, round(remaining))} 秒，"
                    "登录授权仍有效，无需重新扫码"
                )
            else:
                detail = f"（{message}）" if message else ""
                cooldown_message = (
                    "微信读书公众号文章通道仍在冷却中，"
                    f"请约 {max(1, round(remaining))} 秒后重试；无需重新扫码{detail}"
                )
            raise WeReadArticleChannelError(
                cooldown_message,
                retry_after_seconds=remaining,
                code=error_code,
                reason=reason,
                action_required=action_required,
            )

    def _set_article_channel_cooldown(
        self,
        message: str,
        *,
        reason: str,
        error_code: int | None,
        action_required: str,
    ):
        with self._request_lock:
            self._article_channel_unavailable_until = max(
                self._article_channel_unavailable_until,
                time.monotonic() + WEREAD_ARTICLE_CHANNEL_COOLDOWN_SECONDS,
            )
            self._article_channel_message = message
            self._article_channel_reason = reason
            self._article_channel_error_code = error_code
            self._article_channel_action_required = action_required

    def _clear_article_channel_cooldown(self):
        with self._request_lock:
            self._article_channel_unavailable_until = 0.0
            self._article_channel_message = ""
            self._article_channel_reason = ""
            self._article_channel_error_code = None
            self._article_channel_action_required = ""

    def _url(self, path: str) -> str:
        return path if path.startswith("http://") or path.startswith("https://") else f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    @staticmethod
    def _find_list(payload: dict, keys: tuple[str, ...]) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _looks_like_book(item: dict) -> bool:
        return bool(item.get("title") or item.get("name") or item.get("bookName") or item.get("type") is not None)

    @staticmethod
    def _is_mp_book(item: dict) -> bool:
        book_id = str(item.get("bookId") or item.get("id") or item.get("mpId") or "")
        if book_id.startswith("MP_WXS_"):
            return True
        book_type = item.get("type", item.get("bookType"))
        try:
            if int(book_type) == 3:
                return True
        except (TypeError, ValueError):
            pass
        return str(book_type or "").lower() in {"mp", "mparticle", "mp_article"} or bool(item.get("isMp"))

    @staticmethod
    def _normalise_account_name(value: str) -> str:
        return "".join(value.lower().split())

    @staticmethod
    def _is_rate_limited(code: int, message: str) -> bool:
        lowered = message.lower()
        return code in {429, -429} or any(word in lowered for word in ("频繁", "频控", "风控", "小黑屋", "too many", "rate limit"))

    @staticmethod
    def _is_auth_error(code: int, message: str, status_code: int) -> bool:
        lowered = message.lower()
        return status_code in (401, 403) or code in {-2010, 401, 403} or any(
            word in lowered for word in ("用户不存在", "登录失效", "授权失效", "not login", "unauthorized")
        )

    @staticmethod
    def _is_mp_article_channel_error(error: WeReadCollectorError) -> bool:
        return error.code == -2041 or "登录超时" in str(error)

    @staticmethod
    def _is_transient_session_error(error: WeReadCollectorError) -> bool:
        return error.code == -2041 or "登录超时" in str(error)

    @staticmethod
    def _to_datetime(value: Any) -> datetime:
        try:
            number = int(value)
            if number > 10_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number)
        except (TypeError, ValueError, OSError, OverflowError):
            return datetime.utcnow()

    @staticmethod
    def _filter_dates(rows: list[dict], date_from: str, date_to: str) -> list[dict]:
        if not date_from and not date_to:
            return rows
        start = datetime.strptime(date_from, "%Y-%m-%d") if date_from else datetime.min
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1) if date_to else datetime.max
        return [row for row in rows if start <= row["publish_time"] < end]


collector = WeReadCollector()
