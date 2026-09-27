from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
from pathlib import Path
import threading
import time
from typing import Callable

from ..config import WEREAD_BROWSER_HEADLESS
from .weread_browser import WeReadBrowserChannel, find_chromium_executable, obfuscate_weread_id


class WeReadCaptchaError(RuntimeError):
    """The administrator-operated WeRead CAPTCHA browser is unavailable."""


class WeReadCaptchaManager:
    """Expose one administrator-operated CAPTCHA browser through safe primitives.

    Playwright remains on one worker thread.  The admin UI only receives PNG
    screenshots and submits the user's pointer trajectory; it never receives
    session cookies and no VNC or browser-debugging port is exposed.
    """

    VIEWPORT_WIDTH = 1280
    VIEWPORT_HEIGHT = 820
    MAX_SESSION_SECONDS = 15 * 60
    MAX_DRAG_POINTS = 160

    def __init__(
        self,
        base_url: str,
        storage_state_path: str | os.PathLike[str] | None,
        on_success: Callable[[dict[str, str]], None],
        browser_channel: WeReadBrowserChannel | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.storage_state_path = Path(storage_state_path) if storage_state_path else None
        self.on_success = on_success
        self.browser_channel = browser_channel
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weread-captcha")
        self._submit_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._closed = False
        self._phase = "idle"
        self._message = "未启动安全验证"
        self._started_at = 0.0
        self._last_code: int | None = None
        self._article_ok = False
        self._success_reported = False
        self._last_screenshot = b""
        atexit.register(self.close)

    def start(
        self,
        *,
        source_id: str,
        cookies: dict[str, str],
        user_agent: str,
    ) -> dict:
        if self.browser_channel is not None:
            return self.browser_channel.start_captcha(
                source_id=source_id,
                cookies=cookies,
                user_agent=user_agent,
                on_success=self.on_success,
            )
        return self._submit(self._start, source_id, dict(cookies), user_agent, timeout=45)

    def status(self) -> dict:
        if self.browser_channel is not None:
            return self.browser_channel.captcha_status()
        return self._submit(self._status, timeout=10)

    def screenshot(self) -> bytes:
        if self.browser_channel is not None:
            return self.browser_channel.captcha_screenshot()
        return self._submit(self._screenshot, timeout=15)

    def replay_drag(self, points: list[dict]) -> dict:
        if self.browser_channel is not None:
            return self.browser_channel.replay_captcha_drag(points)
        return self._submit(self._replay_drag, list(points), timeout=30)

    def replay_click(self, point: dict) -> dict:
        if self.browser_channel is not None:
            return self.browser_channel.replay_captcha_click(point)
        return self._submit(self._replay_click, dict(point), timeout=15)

    def confirm(self) -> dict:
        if self.browser_channel is not None:
            return self.browser_channel.confirm_captcha()
        return self._submit(self._confirm, timeout=20)

    def focus(self) -> dict:
        if self.browser_channel is None:
            raise WeReadCaptchaError("当前验证浏览器不支持切换窗口")
        return self.browser_channel.focus_captcha()

    def cancel(self) -> dict:
        if self.browser_channel is not None:
            return self.browser_channel.cancel_captcha()
        return self._submit(self._cancel, timeout=15)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.browser_channel is not None:
            return
        try:
            self._submit(self._close_runtime, timeout=15, allow_closed=True)
        except Exception:
            pass
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(self, function, *args, timeout: float, allow_closed: bool = False):
        if self._closed and not allow_closed:
            raise WeReadCaptchaError("微信读书安全验证会话已经关闭")
        with self._submit_lock:
            future = self._executor.submit(function, *args)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise WeReadCaptchaError("微信读书安全验证操作超时") from exc

    def _start(self, source_id: str, cookies: dict[str, str], user_agent: str) -> dict:
        self._pump()
        if self._phase in {"loading", "waiting"} and self._page is not None:
            return self._status_payload()
        if not source_id:
            raise WeReadCaptchaError("缺少用于安全验证的微信公众号来源")

        self._close_runtime()
        self._phase = "loading"
        self._message = "正在打开微信读书安全验证页面"
        self._started_at = time.monotonic()
        self._last_code = None
        self._article_ok = False
        self._success_reported = False
        self._last_screenshot = b""
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            executable_path = find_chromium_executable()
            launch_options = {
                "headless": WEREAD_BROWSER_HEADLESS,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            }
            if executable_path:
                launch_options["executable_path"] = executable_path
            # Compatibility-only fallback.  The production collector injects a
            # shared WeReadBrowserChannel above; it alone may own the persistent
            # profile.  Keep this direct manager ephemeral so it cannot contend
            # for the same Chrome user-data directory.
            self._browser = self._playwright.chromium.launch(**launch_options)
            context_options = {
                "user_agent": user_agent or None,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "viewport": {"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
            }
            if self.storage_state_path and self.storage_state_path.is_file():
                context_options["storage_state"] = str(self.storage_state_path)
            self._context = self._browser.new_context(**context_options)
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
            self._page.on("response", self._observe_response)
            reader_id = obfuscate_weread_id(source_id)
            self._page.goto(
                f"{self.base_url}/web/mp/reader/{reader_id}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            self._page.wait_for_timeout(1_200)
            if self._phase == "loading":
                self._phase = "waiting"
                self._message = "验证页面已打开：滑块题请拖动，图片题请点选后确认"
            self._pump()
            return self._status_payload()
        except Exception as exc:
            self._phase = "failed"
            self._message = f"服务器浏览器无法启动或打开验证页面：{exc}"
            self._close_runtime(keep_status=True)
            raise WeReadCaptchaError(self._message) from exc

    def _observe_response(self, response):
        if "/web/mp/articles" not in response.url:
            return
        try:
            payload = response.json()
            self._last_code = int(payload.get("errCode", payload.get("code", 0)) or 0)
        except Exception:
            return
        if response.ok and self._last_code in (0, 200):
            self._article_ok = True
            self._phase = "success"
            self._message = "安全验证已通过，文章通道正在恢复"
        elif self._last_code == -2041:
            self._phase = "waiting"
            self._message = "需要完成安全验证（可能为滑块或图片点选）"
        elif self._last_code in (-2010, -2012):
            self._phase = "failed"
            self._message = "微信读书网页会话已失效，请先重新检测授权状态"
        elif self._last_code not in (None, 0, 200):
            self._phase = "failed"
            self._message = f"微信读书验证页面返回异常（{self._last_code}）"

    def _pump(self):
        if self._page is not None and self._phase in {"loading", "waiting", "success"}:
            try:
                self._page.wait_for_timeout(50)
            except Exception:
                if self._phase != "success":
                    self._phase = "failed"
                    self._message = "安全验证页面连接已断开"
        if self._article_ok and self._context is not None:
            self._finish_success()
        elif (
            self._phase in {"loading", "waiting"}
            and self._started_at
            and time.monotonic() - self._started_at >= self.MAX_SESSION_SECONDS
        ):
            self._phase = "expired"
            self._message = "安全验证页面已超时，请重新打开"
            self._close_runtime(keep_status=True)

    def _finish_success(self):
        try:
            if self._page is not None:
                self._last_screenshot = self._page.screenshot(type="png")
            self._persist_storage_state()
            cookies = {
                str(item.get("name")): str(item.get("value"))
                for item in self._context.cookies(self.base_url)
                if item.get("name") and item.get("value") is not None
            }
            self.on_success(cookies)
            self._phase = "success"
            self._message = "安全验证已通过，文章通道已恢复"
        except Exception as exc:
            self._phase = "failed"
            self._message = f"滑块已通过，但保存浏览器状态失败：{exc}"
        finally:
            self._article_ok = False
            self._close_runtime(keep_status=True)

    def _status(self) -> dict:
        self._pump()
        return self._status_payload()

    def _status_payload(self) -> dict:
        active = self._phase in {"loading", "waiting"}
        just_completed = self._phase == "success" and not self._success_reported
        if just_completed:
            self._success_reported = True
        remaining = 0
        if active and self._started_at:
            remaining = max(0, round(self.MAX_SESSION_SECONDS - (time.monotonic() - self._started_at)))
        return {
            "phase": self._phase,
            "active": active,
            "can_drag": self._phase == "waiting" and self._page is not None,
            "can_click": self._phase == "waiting" and self._page is not None,
            "message": self._message,
            "article_code": self._last_code,
            "remaining_seconds": remaining,
            "viewport_width": self.VIEWPORT_WIDTH,
            "viewport_height": self.VIEWPORT_HEIGHT,
            "just_completed": just_completed,
        }

    def _screenshot(self) -> bytes:
        self._pump()
        if self._page is not None:
            self._last_screenshot = self._page.screenshot(type="png")
        if not self._last_screenshot:
            raise WeReadCaptchaError("安全验证页面尚未准备好")
        return self._last_screenshot

    def _replay_drag(self, points: list[dict]) -> dict:
        self._pump()
        if self._phase != "waiting" or self._page is None:
            raise WeReadCaptchaError("当前没有可操作的安全验证页面")
        if len(points) < 2:
            raise WeReadCaptchaError("拖动轨迹过短，请按住滑块后拖到缺口")
        normalized = []
        for item in points[: self.MAX_DRAG_POINTS]:
            try:
                x = min(self.VIEWPORT_WIDTH - 1, max(0.0, float(item.get("x", 0))))
                y = min(self.VIEWPORT_HEIGHT - 1, max(0.0, float(item.get("y", 0))))
                delay_ms = min(80, max(0, int(item.get("delay_ms", 16) or 0)))
            except (TypeError, ValueError):
                continue
            normalized.append((x, y, delay_ms))
        if len(normalized) < 2:
            raise WeReadCaptchaError("拖动轨迹无效")

        mouse = self._page.mouse
        mouse.move(normalized[0][0], normalized[0][1])
        mouse.down()
        try:
            for x, y, delay_ms in normalized[1:]:
                mouse.move(x, y)
                if delay_ms:
                    self._page.wait_for_timeout(delay_ms)
        finally:
            mouse.up()
        self._page.wait_for_timeout(900)
        self._pump()
        return self._status_payload()

    def _replay_click(self, point: dict) -> dict:
        self._pump()
        if self._phase != "waiting" or self._page is None:
            raise WeReadCaptchaError("当前没有可操作的安全验证页面")
        try:
            x = min(self.VIEWPORT_WIDTH - 1, max(0.0, float(point.get("x", 0))))
            y = min(self.VIEWPORT_HEIGHT - 1, max(0.0, float(point.get("y", 0))))
        except (TypeError, ValueError) as exc:
            raise WeReadCaptchaError("点选坐标无效") from exc
        self._page.mouse.click(x, y)
        self._page.wait_for_timeout(350)
        self._pump()
        return self._status_payload()

    def _confirm(self) -> dict:
        return self._replay_click(
            {"x": self.VIEWPORT_WIDTH * 0.84, "y": self.VIEWPORT_HEIGHT * 0.88}
        )

    def _persist_storage_state(self):
        if self._context is None or self.storage_state_path is None:
            return
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.storage_state_path.with_name(self.storage_state_path.name + ".captcha.tmp")
        try:
            self._context.storage_state(path=str(temporary_path))
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

    def _cancel(self) -> dict:
        self._phase = "cancelled"
        self._message = "已关闭安全验证页面"
        self._close_runtime(keep_status=True)
        return self._status_payload()

    def _close_runtime(self, keep_status: bool = False):
        page, context, browser, playwright = (
            self._page,
            self._context,
            self._browser,
            self._playwright,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        if not keep_status:
            self._phase = "idle"
            self._message = "未启动安全验证"
            self._started_at = 0.0
            self._last_code = None
            self._article_ok = False
            self._success_reported = False
