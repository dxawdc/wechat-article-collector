from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable
from urllib.parse import urlencode

from ..config import (
    WEREAD_BROWSER_ADMIN_URL,
    WEREAD_BROWSER_CHANNEL,
    WEREAD_BROWSER_COMMAND_TIMEOUT_SECONDS,
    WEREAD_BROWSER_HEADLESS,
    WEREAD_BROWSER_MODE,
    WEREAD_BROWSER_USER_DATA_DIR,
)


class WeReadBrowserChannelError(RuntimeError):
    """The real-browser article channel could not complete a request."""


class WeReadBrowserChannelUnavailable(WeReadBrowserChannelError):
    """Playwright or its Chromium runtime is not installed."""


@dataclass
class WeReadBrowserArticleResult:
    payload: dict[str, Any]
    cookies: dict[str, str]
    user_agent: str = ""


def find_chromium_executable() -> str:
    configured = os.getenv("WEREAD_SKILL_CHROMIUM_EXECUTABLE", "").strip()
    if configured:
        return configured
    executable = next(
        (
            path
            for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")
            if (path := shutil.which(name))
        ),
        "",
    )
    if executable:
        return executable
    candidates = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.getenv(variable, "").strip()
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    candidates.extend((
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ))
    return next((str(path) for path in candidates if path.is_file()), "")


def obfuscate_weread_id(value: object) -> str:
    """Return the reader-page ID format used by WeRead's current web client."""
    text = str(value)
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    if text.isdigit():
        kind = "3"
        chunks = [format(int(text[index : index + 9]), "x") for index in range(0, len(text), 9)]
    else:
        kind = "4"
        chunks = ["".join(format(ord(char), "x") for char in text)]
    output = digest[:3] + kind + "2" + digest[-2:]
    output += "g".join(f"{len(chunk):02x}{chunk}" for chunk in chunks)
    if len(output) < 20:
        output += digest[: 20 - len(output)]
    return output + hashlib.md5(output.encode("utf-8")).hexdigest()[:3]


class WeReadBrowserChannel:
    """Execute WeRead's WRPA proof inside one persistent browser context.

    The public-account list route is protected by a browser-generated
    ``x-wrpa-0`` value.  Reimplementing that private algorithm would be both
    brittle and likely to drift again, so the service lets WeRead's own current
    JavaScript generate it and performs the request in the same browser
    context.  A single worker thread owns Playwright because its sync API is
    thread-affine.
    """

    VIEWPORT_WIDTH = 1280
    VIEWPORT_HEIGHT = 900
    CAPTCHA_CLICK_COOLDOWN_SECONDS = 1.2
    CAPTCHA_SUBMIT_SETTLE_SECONDS = 5.0
    CAPTCHA_FAST_COOLDOWN_SECONDS = 5 * 60.0

    def __init__(self, base_url: str, storage_state_path: str | os.PathLike[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.storage_state_path = Path(storage_state_path) if storage_state_path else None
        self.user_data_dir = (
            Path(WEREAD_BROWSER_USER_DATA_DIR)
            if WEREAD_BROWSER_MODE == "persistent" and WEREAD_BROWSER_USER_DATA_DIR
            else None
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weread-browser")
        self._submit_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._session_key = ""
        self._native_user_agent = ""
        self._profile_lock_handle = None
        self._captcha_page = None
        self._admin_page = None
        self._captcha_callback = None
        self._captcha_phase = "idle"
        self._captcha_message = "未启动安全验证"
        self._captcha_started_at = 0.0
        self._captcha_last_code: int | None = None
        self._captcha_article_ok = False
        self._captcha_success_reported = False
        self._captcha_last_screenshot = b""
        self._captcha_next_action_at = 0.0
        self._captcha_submit_until = 0.0
        self._captcha_cooldown_until = 0.0
        self._captcha_round = 0
        self._closed = False
        atexit.register(self.close)

    def fetch_articles(
        self,
        *,
        source_id: str,
        path: str,
        params: dict[str, Any],
        cookies: dict[str, str],
        user_agent: str,
        session_key: str,
        timeout_seconds: float = WEREAD_BROWSER_COMMAND_TIMEOUT_SECONDS,
    ) -> WeReadBrowserArticleResult:
        if self._closed:
            raise WeReadBrowserChannelError("微信读书浏览器通道已经关闭")
        with self._submit_lock:
            future = self._executor.submit(
                self._fetch_articles,
                source_id,
                path,
                params,
                cookies,
                user_agent,
                session_key,
            )
        try:
            return future.result(timeout=max(10.0, timeout_seconds))
        except FutureTimeoutError as exc:
            future.cancel()
            raise WeReadBrowserChannelError("微信读书浏览器通道请求超时") from exc

    def reset(self):
        if self._closed:
            return
        with self._submit_lock:
            future = self._executor.submit(self._reset_context)
        try:
            future.result(timeout=15)
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            with self._submit_lock:
                future = self._executor.submit(self._close_runtime)
            future.result(timeout=15)
        except Exception:
            pass
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _ensure_runtime(self, user_agent: str = ""):
        if self._browser is not None or self._context is not None:
            return
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise WeReadBrowserChannelUnavailable("缺少 Playwright 依赖") from exc
        try:
            if self._playwright is None:
                self._playwright = sync_playwright().start()
            executable_path = find_chromium_executable()
            launch_options = {
                "headless": WEREAD_BROWSER_HEADLESS,
                "args": [
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            }
            if WEREAD_BROWSER_CHANNEL:
                launch_options["channel"] = WEREAD_BROWSER_CHANNEL
            elif executable_path:
                launch_options["executable_path"] = executable_path
            if self.user_data_dir:
                self.user_data_dir.mkdir(parents=True, exist_ok=True)
                self._acquire_profile_lock()
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(self.user_data_dir),
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
                    **launch_options,
                )
            else:
                self._browser = self._playwright.chromium.launch(**launch_options)
        except WeReadBrowserChannelUnavailable:
            self._close_runtime()
            raise
        except PlaywrightError as exc:
            self._close_runtime()
            raise WeReadBrowserChannelUnavailable(
                "Playwright Chromium 未安装或无法启动"
            ) from exc

    def _acquire_profile_lock(self):
        if self.user_data_dir is None or self._profile_lock_handle is not None:
            return
        lock_path = self.user_data_dir.parent / ".weread-skill-profile.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WeReadBrowserChannelUnavailable(
                "微信读书浏览器 Profile 正被另一实例使用，请保持采集服务单实例运行"
            ) from exc
        self._profile_lock_handle = handle

    def _release_profile_lock(self):
        handle, self._profile_lock_handle = self._profile_lock_handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def _ensure_context(
        self,
        source_id: str,
        cookies: dict[str, str],
        user_agent: str,
        session_key: str,
    ):
        if not self.user_data_dir and self._context is not None and self._session_key != session_key:
            self._reset_context()
        self._ensure_runtime(user_agent)
        if self._context is None:
            if not self.user_data_dir:
                context_options = dict(
                    user_agent=user_agent,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
                )
                if self.storage_state_path and self.storage_state_path.is_file():
                    context_options["storage_state"] = str(self.storage_state_path)
                self._context = self._browser.new_context(**context_options)
        if self._page is None:
            browser_cookies = [
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": ".weread.qq.com",
                    "path": "/",
                    "secure": True,
                }
                for name, value in cookies.items()
                if name and value is not None
            ]
            if browser_cookies:
                self._context.add_cookies(browser_cookies)
            self._page = self._context.new_page()
            self._session_key = session_key
            try:
                self._native_user_agent = str(self._page.evaluate("() => navigator.userAgent") or "")
            except Exception:
                self._native_user_agent = ""
        elif self.user_data_dir and self._session_key != session_key:
            # A persistent Profile must survive a refreshed QR credential.  Only
            # merge the new cookies; closing the context would also close Chrome.
            browser_cookies = [
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": ".weread.qq.com",
                    "path": "/",
                    "secure": True,
                }
                for name, value in cookies.items()
                if name and value is not None
            ]
            if browser_cookies:
                self._context.add_cookies(browser_cookies)
            self._session_key = session_key

        reader_id = obfuscate_weread_id(source_id)
        reader_prefix = f"{self.base_url}/web/mp/reader/"
        if not self._page.url.startswith(reader_prefix):
            self._page.goto(
                reader_prefix + reader_id,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        self._page.wait_for_function(
            "() => Boolean(window.__WRPA__ && typeof window.__WRPA__.sr === 'function')",
            timeout=25_000,
        )

    def _fetch_articles(
        self,
        source_id: str,
        path: str,
        params: dict[str, Any],
        cookies: dict[str, str],
        user_agent: str,
        session_key: str,
    ) -> WeReadBrowserArticleResult:
        try:
            self._ensure_context(source_id, cookies, user_agent, session_key)
            query = urlencode(params)
            request_path = path if path.startswith("/") else "/" + path
            result = self._page.evaluate(
                """
                async ({ path, query }) => {
                  const responseData = async response => {
                    const text = await response.text()
                    let data = null
                    try { data = JSON.parse(text) } catch (_) {}
                    return { status: response.status, data, json: Boolean(data) }
                  }
                  const request = async () => {
                    const proof = await window.__WRPA__.sr({ query })
                    if (!proof) throw new Error('WRPA proof is empty')
                    return responseData(await fetch(`${path}?${query}`, {
                      method: 'GET',
                      credentials: 'include',
                      headers: { 'x-wrpa-0': String(proof) }
                    }))
                  }
                  let result = await request()
                  const firstCode = Number(result.data?.errCode ?? result.data?.code ?? 0)
                  if (firstCode === -2012 || firstCode === -2010) {
                    const ql = document.cookie.split(';').some(item => item.trim() === 'wr_ql=1')
                    await fetch('/web/login/renewal', {
                      method: 'POST',
                      credentials: 'include',
                      headers: { 'Content-Type': 'application/json;charset=UTF-8' },
                      body: JSON.stringify({ rq: encodeURIComponent(path), ql })
                    })
                    result = await request()
                  }
                  return result
                }
                """,
                {"path": request_path, "query": query},
            )
            if not isinstance(result, dict) or not result.get("json"):
                raise WeReadBrowserChannelError("微信读书浏览器通道返回了非 JSON 响应")
            payload = result.get("data")
            if not isinstance(payload, dict):
                raise WeReadBrowserChannelError("微信读书浏览器通道返回了无效数据")
            browser_cookies = self._context.cookies(self.base_url)
            self._persist_storage_state()
            cookie_values = {
                str(item.get("name")): str(item.get("value"))
                for item in browser_cookies
                if item.get("name") and item.get("value") is not None
            }
            return WeReadBrowserArticleResult(
                payload=payload,
                cookies=cookie_values,
                user_agent=self._native_user_agent,
            )
        except WeReadBrowserChannelError:
            raise
        except Exception as exc:
            raise WeReadBrowserChannelError(f"微信读书浏览器通道失败：{exc}") from exc

    # CAPTCHA uses the same persistent context and executor as article reads.
    # Keeping this ownership here prevents two Chromium instances from trying to
    # lock or overwrite one User Data Directory.
    def start_captcha(
        self,
        *,
        source_id: str,
        cookies: dict[str, str],
        user_agent: str,
        on_success: Callable[[dict[str, str]], None],
    ) -> dict:
        with self._submit_lock:
            future = self._executor.submit(
                self._start_captcha, source_id, dict(cookies), user_agent, on_success
            )
        try:
            return future.result(timeout=45)
        except FutureTimeoutError as exc:
            future.cancel()
            raise WeReadBrowserChannelError("微信读书安全验证页面打开超时") from exc

    def captcha_status(self) -> dict:
        return self._captcha_submit(self._captcha_status, timeout=10)

    def captcha_screenshot(self) -> bytes:
        return self._captcha_submit(self._captcha_screenshot, timeout=15)

    def replay_captcha_drag(self, points: list[dict]) -> dict:
        return self._captcha_submit(self._replay_captcha_drag, list(points), timeout=30)

    def replay_captcha_click(self, point: dict) -> dict:
        return self._captcha_submit(self._replay_captcha_click, dict(point), timeout=15)

    def confirm_captcha(self) -> dict:
        return self._captcha_submit(self._confirm_captcha, timeout=20)

    def focus_captcha(self) -> dict:
        return self._captcha_submit(self._focus_captcha, timeout=10)

    def cancel_captcha(self) -> dict:
        return self._captcha_submit(self._cancel_captcha, timeout=15)

    def _captcha_submit(self, function, *args, timeout: float):
        if self._closed:
            raise WeReadBrowserChannelError("微信读书浏览器通道已经关闭")
        with self._submit_lock:
            future = self._executor.submit(function, *args)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise WeReadBrowserChannelError("微信读书安全验证操作超时") from exc

    def _start_captcha(
        self,
        source_id: str,
        cookies: dict[str, str],
        user_agent: str,
        on_success: Callable[[dict[str, str]], None],
    ) -> dict:
        self._captcha_pump()
        if (
            self._captcha_phase == "cooldown"
            and time.monotonic() < self._captcha_cooldown_until
        ):
            return self._captcha_status_payload()
        if self._captcha_phase in {"loading", "waiting"} and self._captcha_page is not None:
            return self._captcha_status_payload()
        if not source_id:
            raise WeReadBrowserChannelError("缺少用于安全验证的微信公众号来源")
        self._captcha_phase = "loading"
        self._captcha_message = "正在打开微信读书安全验证页面"
        self._captcha_started_at = time.monotonic()
        self._captcha_last_code = None
        self._captcha_article_ok = False
        self._captcha_success_reported = False
        self._captcha_last_screenshot = b""
        self._captcha_next_action_at = 0.0
        self._captcha_submit_until = 0.0
        self._captcha_cooldown_until = 0.0
        self._captcha_round = 0
        self._captcha_callback = on_success
        try:
            self._ensure_context(source_id, cookies, user_agent, "captcha")
            self._captcha_page = self._page
            self._captcha_page.on("response", self._observe_captcha_response)
            self._captcha_page.wait_for_timeout(1_200)
            if not WEREAD_BROWSER_HEADLESS:
                self._ensure_admin_page()
                self._captcha_page.bring_to_front()
            if self._captcha_phase == "loading":
                self._captcha_phase = "waiting"
                self._captcha_message = (
                    "持久 Chrome 已打开管理后台和验证页，请直接切换标签完成验证"
                    if not WEREAD_BROWSER_HEADLESS
                    else "验证页面已打开：滑块题请拖动，图片题请点选后确认"
                )
            self._captcha_pump()
            return self._captcha_status_payload()
        except Exception as exc:
            self._captcha_phase = "failed"
            self._captcha_message = f"服务器浏览器无法打开验证页面：{exc}"
            raise WeReadBrowserChannelError(self._captcha_message) from exc

    def _observe_captcha_response(self, response):
        response_url = str(response.url or "")
        if "/web/mp/articles" not in response_url:
            if not any(marker in response_url.lower() for marker in ("captcha", "turing", "verify")):
                return
            try:
                response_text = str(response.json())
            except Exception:
                return
            if any(
                marker in response_text
                for marker in ("操作过快", "操作过于频繁", "稍等一会", "稍后再试", "请稍后")
            ):
                self._enter_captcha_rate_limit()
            return
        try:
            payload = response.json()
            self._captcha_last_code = int(payload.get("errCode", payload.get("code", 0)) or 0)
        except Exception:
            return
        if response.ok and self._captcha_last_code in (0, 200):
            self._captcha_article_ok = True
            self._captcha_phase = "success"
            self._captcha_message = "安全验证已通过，文章通道正在恢复"
        elif self._captcha_last_code == -2041:
            if self._captcha_phase == "submitting":
                self._captcha_submit_until = max(
                    self._captcha_submit_until,
                    time.monotonic() + self.CAPTCHA_SUBMIT_SETTLE_SECONDS,
                )
                self._captcha_next_action_at = self._captcha_submit_until
                self._captcha_message = "本轮已提交，验证服务仍要求确认；请等待页面稳定"
            else:
                self._captcha_phase = "waiting"
                self._captcha_message = "需要完成安全验证（可能为滑块或图片点选）"
        elif self._captcha_last_code in (-2010, -2012):
            self._captcha_phase = "failed"
            self._captcha_message = "微信读书网页会话已失效，请先重新扫码"

    def _captcha_pump(self):
        active_phases = {"loading", "waiting", "submitting", "cooldown", "success"}
        if self._captcha_page is not None and self._captcha_phase in active_phases:
            try:
                self._captcha_page.wait_for_timeout(25)
            except Exception:
                if self._captcha_phase != "success":
                    self._captcha_phase = "failed"
                    self._captcha_message = "安全验证页面连接已断开"
        if self._captcha_phase in {"loading", "waiting", "submitting"}:
            if self._captcha_page_shows_rate_limit():
                self._enter_captcha_rate_limit()
        if self._captcha_phase == "cooldown" and self._captcha_page is not None:
            self._suspend_captcha_page_for_cooldown()
        if self._captcha_article_ok and self._context is not None:
            self._finish_captcha_success()
            return
        now = time.monotonic()
        if self._captcha_phase == "cooldown" and now >= self._captcha_cooldown_until:
            if self._captcha_page is None:
                self._captcha_phase = "expired"
                self._captcha_message = "操作频繁冷却已结束，请重新打开安全验证"
            else:
                self._captcha_phase = "waiting"
                self._captcha_message = "等待已结束；如果页面出现新题，请完成新题后再提交"
        elif self._captcha_phase == "submitting" and now >= self._captcha_submit_until:
            self._captcha_phase = "waiting"
            self._captcha_message = (
                f"第 {self._captcha_round} 轮尚未恢复文章通道；"
                "如果页面已刷新为下一题，说明验证服务要求继续下一轮"
            )
        if (
            self._captcha_phase in {"loading", "waiting", "submitting", "cooldown"}
            and self._captcha_started_at
            and now - self._captcha_started_at >= 15 * 60
        ):
            self._captcha_phase = "expired"
            self._captcha_message = "安全验证页面已超时，请重新打开"

    def _captcha_page_shows_rate_limit(self) -> bool:
        if self._captcha_page is None:
            return False
        pattern = re.compile(r"操作(?:过快|过于频繁)|稍后再试|稍等一会")
        frames = getattr(self._captcha_page, "frames", [])
        if not isinstance(frames, (list, tuple)):
            return False
        for frame in frames:
            try:
                matches = frame.get_by_text(pattern)
                for index in range(min(matches.count(), 6)):
                    if matches.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _enter_captcha_rate_limit(self):
        self._captcha_phase = "cooldown"
        self._captcha_cooldown_until = max(
            self._captcha_cooldown_until,
            time.monotonic() + self.CAPTCHA_FAST_COOLDOWN_SECONDS,
        )
        self._captcha_next_action_at = self._captcha_cooldown_until
        self._captcha_message = "验证服务提示操作过于频繁，本轮已停止；请等待 5 分钟后重试"

    def _suspend_captcha_page_for_cooldown(self):
        page, self._captcha_page = self._captcha_page, None
        if page is None:
            return
        try:
            self._captcha_last_screenshot = page.screenshot(type="png")
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass
        if self._page is page:
            self._page = None
            self._session_key = ""

    def _finish_captcha_success(self):
        try:
            if self._captcha_page is not None:
                self._captcha_last_screenshot = self._captcha_page.screenshot(type="png")
            self._persist_storage_state()
            cookies = {
                str(item.get("name")): str(item.get("value"))
                for item in self._context.cookies(self.base_url)
                if item.get("name") and item.get("value") is not None
            }
            if self._captcha_callback:
                self._captcha_callback(cookies)
            self._captcha_phase = "success"
            self._captcha_message = "安全验证已通过，文章通道已恢复"
        except Exception as exc:
            self._captcha_phase = "failed"
            self._captcha_message = f"滑块已通过，但保存浏览器状态失败：{exc}"
        finally:
            self._captcha_article_ok = False

    def _captcha_status(self) -> dict:
        self._captcha_pump()
        return self._captcha_status_payload()

    def _captcha_status_payload(self) -> dict:
        active = self._captcha_phase in {"loading", "waiting", "submitting", "cooldown"}
        just_completed = self._captcha_phase == "success" and not self._captcha_success_reported
        if just_completed:
            self._captcha_success_reported = True
        remaining = 0
        if active and self._captcha_started_at:
            remaining = max(0, round(15 * 60 - (time.monotonic() - self._captcha_started_at)))
        now = time.monotonic()
        action_ready_at = self._captcha_next_action_at
        if self._captcha_phase == "submitting":
            action_ready_at = max(action_ready_at, self._captcha_submit_until)
        elif self._captcha_phase == "cooldown":
            action_ready_at = max(action_ready_at, self._captcha_cooldown_until)
        next_action_seconds = max(0, math.ceil(action_ready_at - now))
        can_interact = (
            self._captcha_phase == "waiting"
            and self._captcha_page is not None
            and next_action_seconds == 0
        )
        return {
            "phase": self._captcha_phase,
            "active": active,
            "can_drag": can_interact,
            "can_click": can_interact,
            "message": self._captcha_message,
            "article_code": self._captcha_last_code,
            "remaining_seconds": remaining,
            "viewport_width": self.VIEWPORT_WIDTH,
            "viewport_height": self.VIEWPORT_HEIGHT,
            "browser_visible": not WEREAD_BROWSER_HEADLESS,
            "has_browser_page": self._captcha_page is not None,
            "next_action_seconds": next_action_seconds,
            "round": self._captcha_round,
            "just_completed": just_completed,
        }

    def _captcha_screenshot(self) -> bytes:
        self._captcha_pump()
        if self._captcha_page is not None:
            self._captcha_last_screenshot = self._captcha_page.screenshot(type="png")
        if not self._captcha_last_screenshot:
            raise WeReadBrowserChannelError("安全验证页面尚未准备好")
        return self._captcha_last_screenshot

    def _replay_captcha_drag(self, points: list[dict]) -> dict:
        self._captcha_pump()
        if self._captcha_phase in {"submitting", "cooldown"}:
            return self._captcha_status_payload()
        if self._captcha_phase != "waiting" or self._captcha_page is None:
            raise WeReadBrowserChannelError("当前没有可操作的安全验证页面")
        if not self._captcha_action_ready():
            return self._captcha_status_payload()
        if len(points) < 2:
            raise WeReadBrowserChannelError("拖动轨迹过短，请按住滑块后拖到缺口")
        normalized = []
        for item in points[:160]:
            try:
                x = min(self.VIEWPORT_WIDTH - 1, max(0.0, float(item.get("x", 0))))
                y = min(self.VIEWPORT_HEIGHT - 1, max(0.0, float(item.get("y", 0))))
                delay_ms = min(80, max(0, int(item.get("delay_ms", 16) or 0)))
            except (TypeError, ValueError):
                continue
            normalized.append((x, y, delay_ms))
        if len(normalized) < 2:
            raise WeReadBrowserChannelError("拖动轨迹无效")
        mouse = self._captcha_page.mouse
        mouse.move(normalized[0][0], normalized[0][1])
        mouse.down()
        try:
            for x, y, delay_ms in normalized[1:]:
                mouse.move(x, y)
                if delay_ms:
                    self._captcha_page.wait_for_timeout(delay_ms)
        finally:
            mouse.up()
        self._captcha_page.wait_for_timeout(900)
        self._captcha_next_action_at = time.monotonic() + self.CAPTCHA_CLICK_COOLDOWN_SECONDS
        self._captcha_message = "已回放滑块操作，请等待验证页面响应"
        self._captcha_pump()
        return self._captcha_status_payload()

    def _replay_captcha_click(self, point: dict) -> dict:
        self._captcha_pump()
        if self._captcha_phase in {"submitting", "cooldown"}:
            return self._captcha_status_payload()
        if self._captcha_phase != "waiting" or self._captcha_page is None:
            raise WeReadBrowserChannelError("当前没有可操作的安全验证页面")
        if not self._captcha_action_ready():
            return self._captcha_status_payload()
        try:
            x = min(self.VIEWPORT_WIDTH - 1, max(0.0, float(point.get("x", 0))))
            y = min(self.VIEWPORT_HEIGHT - 1, max(0.0, float(point.get("y", 0))))
            press_ms = min(250, max(40, int(point.get("delay_ms", 85) or 85)))
        except (TypeError, ValueError) as exc:
            raise WeReadBrowserChannelError("点选坐标无效") from exc
        # The confirmation control is normally at the lower right. It may be
        # an iframe button whose hit target is less forgiving than a screenshot
        # coordinate, so use the explicit-confirm path for this area.
        if x >= self.VIEWPORT_WIDTH * 0.7 and y >= self.VIEWPORT_HEIGHT * 0.8:
            return self._confirm_captcha()
        self._human_click(x, y, press_ms=press_ms)
        # Image-choice captchas often update a selected marker immediately;
        # allow that DOM change to render before the next screenshot is fetched.
        self._captcha_page.wait_for_timeout(500)
        self._captcha_next_action_at = time.monotonic() + self.CAPTCHA_CLICK_COOLDOWN_SECONDS
        self._captcha_message = "已记录本次点选，请稍等画面更新后继续"
        self._captcha_pump()
        return self._captcha_status_payload()

    def _confirm_captcha(self) -> dict:
        self._captcha_pump()
        if self._captcha_phase in {"submitting", "cooldown"}:
            return self._captcha_status_payload()
        if self._captcha_phase != "waiting" or self._captcha_page is None:
            raise WeReadBrowserChannelError("当前没有可提交的安全验证页面")
        if not self._captcha_action_ready():
            return self._captcha_status_payload()
        clicked = self._click_visible_confirm_control()
        if not clicked:
            # Canvas widgets do not expose their confirm button in the DOM.
            # This is the stable lower-right location in the fixed viewport.
            self._human_click(self.VIEWPORT_WIDTH * 0.84, self.VIEWPORT_HEIGHT * 0.88)
        if self._captcha_phase not in {"success", "cooldown"}:
            self._captcha_round += 1
            self._captcha_phase = "submitting"
            self._captcha_submit_until = time.monotonic() + self.CAPTCHA_SUBMIT_SETTLE_SECONDS
            self._captcha_next_action_at = self._captcha_submit_until
            self._captcha_message = (
                f"已提交第 {self._captcha_round} 轮选择，正在等待验证服务确认；请勿重复提交"
            )
        self._captcha_page.wait_for_timeout(1_500)
        self._captcha_pump()
        return self._captcha_status_payload()

    def _focus_captcha(self) -> dict:
        self._captcha_pump()
        if WEREAD_BROWSER_HEADLESS:
            raise WeReadBrowserChannelError("当前服务器使用无界面浏览器，无法切换到可见窗口")
        if self._captcha_page is None:
            raise WeReadBrowserChannelError("当前没有已打开的安全验证页面")
        self._captcha_page.bring_to_front()
        self._captcha_message = "已切换到真实 Chrome；请直接在该窗口完成验证"
        return self._captcha_status_payload()

    def _ensure_admin_page(self):
        if self._context is None or not WEREAD_BROWSER_ADMIN_URL:
            return
        if self._admin_page is not None:
            try:
                if not self._admin_page.is_closed():
                    return
            except Exception:
                pass
            self._admin_page = None
        blank_page = None
        for page in self._context.pages:
            try:
                if str(page.url).startswith(WEREAD_BROWSER_ADMIN_URL):
                    self._admin_page = page
                    return
                if page is not self._captcha_page and str(page.url) in {"", "about:blank"}:
                    blank_page = page
            except Exception:
                continue
        page = blank_page or self._context.new_page()
        try:
            page.goto(WEREAD_BROWSER_ADMIN_URL, wait_until="domcontentloaded", timeout=15_000)
            self._admin_page = page
        except Exception:
            try:
                page.close()
            except Exception:
                pass

    def _captcha_action_ready(self) -> bool:
        remaining = self._captcha_next_action_at - time.monotonic()
        if remaining <= 0:
            return True
        self._captcha_message = f"操作过快，请等待约 {max(1, math.ceil(remaining))} 秒后继续"
        return False

    def _human_click(self, x: float, y: float, *, press_ms: int = 85):
        mouse = self._captcha_page.mouse
        mouse.move(x, y)
        self._captcha_page.wait_for_timeout(55)
        mouse.down()
        self._captcha_page.wait_for_timeout(min(250, max(40, int(press_ms))))
        mouse.up()

    def _click_visible_confirm_control(self) -> bool:
        """Click a user-requested confirm control if a widget exposes one."""
        frames = getattr(self._captcha_page, "frames", [])
        if not isinstance(frames, (list, tuple)):
            return False
        for frame in frames:
            try:
                controls = frame.get_by_text(re.compile(r"^(确定|提交|完成)$"))
                for index in range(min(controls.count(), 8)):
                    control = controls.nth(index)
                    if not control.is_visible():
                        continue
                    box = control.bounding_box()
                    if not box or box.get("y", 0) < self.VIEWPORT_HEIGHT * 0.45:
                        continue
                    self._human_click(
                        float(box["x"]) + float(box["width"]) / 2,
                        float(box["y"]) + float(box["height"]) / 2,
                    )
                    return True
            except Exception:
                continue
        return False

    def _cancel_captcha(self) -> dict:
        self._captcha_phase = "cancelled"
        self._captcha_message = "已关闭安全验证页面"
        self._captcha_page = None
        return self._captcha_status_payload()

    def _reset_context(self):
        page, context = self._page, self._context
        self._page = None
        self._session_key = ""
        try:
            if page is not None:
                page.close()
        finally:
            if context is not None and not self.user_data_dir:
                self._context = None
                self._persist_storage_state(context)
                context.close()

    def _persist_storage_state(self, context=None):
        context = context or self._context
        if context is None or self.storage_state_path is None:
            return
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.storage_state_path.with_name(self.storage_state_path.name + ".tmp")
        try:
            context.storage_state(path=str(temporary_path))
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary_path, self.storage_state_path)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _close_runtime(self):
        captcha_page = self._captcha_page
        self._captcha_page = None
        self._admin_page = None
        try:
            if captcha_page is not None:
                captcha_page.close()
        except Exception:
            pass
        try:
            self._reset_context()
        except Exception:
            pass
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = None
        self._browser = None
        self._playwright = None
        try:
            if context is not None:
                self._persist_storage_state(context)
                context.close()
            elif browser is not None:
                browser.close()
        finally:
            if playwright is not None:
                playwright.stop()
            self._release_profile_lock()
