"""Bundled, local-only WeRead collection API. No AI providers or model calls."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from itertools import count
import json
import os
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .config import DATA_DIR, WEREAD_QRCODE_FILE
from .integrations.weread import (WeReadArticleChannelError, WeReadCollectorError,
                                  WeReadRateLimitError, collector)
from .storage import Store
from .output import write_article


DATA_DIR.mkdir(parents=True, exist_ok=True)
if os.name != "nt":
    DATA_DIR.chmod(0o700)
store = Store(DATA_DIR / "articles.sqlite3")
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weread-collect")
scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Shanghai"))
state_lock = threading.Lock()
job_ids = count(1)
jobs: dict[int, dict] = {}
last_check: dict = {}


def reload_schedules() -> None:
    if not scheduler.running:
        return
    for job in scheduler.get_jobs():
        if job.id.startswith("collect_"):
            scheduler.remove_job(job.id)
    for task in store.tasks():
        if task["status"] == "active" and task["cron"]:
            scheduler.add_job(enqueue_collect,
                              CronTrigger.from_crontab(task["cron"], timezone=scheduler.timezone),
                              id=f"collect_{task['id']}", args=[task["id"]], replace_existing=True)


@asynccontextmanager
async def lifespan(_app):
    scheduler.start()
    reload_schedules()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        executor.shutdown(wait=False, cancel_futures=True)
        collector.close()


app = FastAPI(title="WeRead article collector", lifespan=lifespan)


def ok(data):
    return {"code": 0, "data": data}


def require_local_key(x_collector_key: str = Header(default="", alias="X-Collector-Key")):
    import secrets

    path = DATA_DIR / "local-token"
    if not path.is_file() or not secrets.compare_digest(x_collector_key, path.read_text(encoding="ascii").strip()):
        raise HTTPException(status_code=401, detail="本地采集服务认证失败")


def current_status() -> dict:
    result = collector.status()
    result.update({
        "article_channel_last_check_ok": last_check.get("article_channel_ok"),
        "article_channel_last_check_message": last_check.get("message", ""),
        "article_channel_last_check_reason": last_check.get("article_channel_reason", ""),
        "article_channel_error_code": last_check.get("article_channel_error_code"),
        "article_channel_retry_after_seconds": last_check.get("article_channel_retry_after_seconds", 0),
    })
    return result


def _run_collect(task_id: int, job_id: int) -> None:
    with state_lock:
        jobs[job_id]["status"] = "running"
    task = store.task(task_id)
    if not task:
        with state_lock:
            jobs[job_id]["status"] = "failed"
        return
    store.mark_task_run(task_id)
    try:
        for account_id in task["account_ids"]:
            account = store.account(account_id)
            if not account or not account["enabled"]:
                continue
            try:
                known, missing = (set(), set()) if (task["date_from"] or task["date_to"]) \
                    else store.known_articles(account_id)
                rows = collector.collect_articles(
                    SimpleNamespace(**account), max_pages=task["max_pages"], date_from=task["date_from"],
                    date_to=task["date_to"], collect_content=True,
                    known_source_ids=known, content_pending_source_ids=missing)
                new_count = 0
                for article in rows:
                    new_count += store.upsert_article(account, article)
                    if task["type"] == "scheduled":
                        saved = store.article_by_source(article["source_id"])
                        if saved:
                            write_article(saved, task["export_format"])
                store.log(task_id, account_id, "info", f"{account['name']} 采集完成", new_count)
            except (WeReadArticleChannelError, WeReadCollectorError, WeReadRateLimitError) as exc:
                if isinstance(exc, WeReadArticleChannelError):
                    last_check.update({"article_channel_ok": False,
                                       "article_channel_reason": exc.reason,
                                       "article_channel_error_code": exc.code,
                                       "message": str(exc)})
                store.log(task_id, account_id, "error", f"{account['name']} 采集失败", error=str(exc))
                break
            except Exception as exc:
                store.log(task_id, account_id, "error", f"{account['name']} 采集失败", error=str(exc))
                break
    finally:
        with state_lock:
            jobs[job_id]["status"] = "done"


def enqueue_collect(task_id: int) -> dict:
    job_id = next(job_ids)
    with state_lock:
        jobs[job_id] = {"id": job_id, "task_id": task_id, "status": "queued"}
        value = dict(jobs[job_id])
    executor.submit(_run_collect, task_id, job_id)
    return value


def _account_ids(ids: list[int]) -> list[int]:
    selected = list(dict.fromkeys(ids))
    if not selected or len(selected) > 100 or any(not store.account(value) for value in selected):
        raise HTTPException(status_code=400, detail="请指定已有的公众号")
    return selected


class AccountBody(BaseModel):
    name: str


class CollectBody(BaseModel):
    account_ids: list[int]
    date_from: str = ""
    date_to: str = ""
    max_pages: int = 5


class ScheduleBody(BaseModel):
    name: str
    account_ids: list[int]
    cron: str
    max_pages: int = 5
    export_format: Literal["md", "html", "body-html", "both"] = "md"


def _dates(start: str, end: str) -> None:
    try:
        left = date.fromisoformat(start) if start else None
        right = date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD") from exc
    if left and right and left > right:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "wechat-article-collector"}


@app.get("/verify", response_class=HTMLResponse)
def verification_help():
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>微信读书验证</title>' \
           '<body style="font:16px/1.8 sans-serif;max-width:640px;margin:8vh auto;padding:24px">' \
           '<h1>请在微信读书中完成人工验证</h1><p>打开书架中的目标公众号，按微信读书提示操作。</p>' \
           '<p><a href="https://weread.qq.com/web/shelf">打开微信读书书架</a></p></body></html>'


@app.get("/api/collector/weread/status", dependencies=[Depends(require_local_key)])
def weread_status():
    return ok(current_status())


@app.post("/api/collector/weread/qrcode", dependencies=[Depends(require_local_key)])
def weread_qrcode():
    return ok(collector.start_auth())


@app.get("/api/collector/weread/qrcode-image", dependencies=[Depends(require_local_key)])
def weread_qrcode_image():
    if not WEREAD_QRCODE_FILE.is_file():
        raise HTTPException(status_code=404, detail="二维码尚未生成")
    return FileResponse(WEREAD_QRCODE_FILE, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/collector/weread/check", dependencies=[Depends(require_local_key)])
def weread_check():
    last_check.clear()
    last_check.update(collector.check_authorization(force_article_probe=True))
    return ok(current_status())


@app.get("/api/collector/weread/shelf", dependencies=[Depends(require_local_key)])
def weread_shelf(q: str = "", limit: int = 100):
    try:
        return ok(collector.search_accounts(q, limit=min(max(limit, 1), 100)))
    except WeReadCollectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/collector/accounts", dependencies=[Depends(require_local_key)])
def accounts():
    rows = store.accounts()
    return ok({"list": rows, "total": len(rows)})


@app.post("/api/collector/accounts", dependencies=[Depends(require_local_key)])
def add_account(body: AccountBody):
    try:
        rows = collector.search_accounts(body.name.strip(), limit=100)["list"]
    except WeReadCollectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    matches = [row for row in rows if row.get("name") == body.name.strip()
               and str(row.get("fakeid") or "").startswith("MP_WXS_")]
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="请先在微信读书书架添加该公众号")
    return ok(store.add_account(matches[0]))


@app.delete("/api/collector/accounts/{account_id}", dependencies=[Depends(require_local_key)])
def remove_account(account_id: int):
    if not store.remove_account(account_id):
        raise HTTPException(status_code=404, detail="公众号不存在")
    return ok({"id": account_id})


@app.post("/api/collector/collect", dependencies=[Depends(require_local_key)])
def collect(body: CollectBody):
    ids = _account_ids(body.account_ids)
    _dates(body.date_from, body.date_to)
    if not 1 <= body.max_pages <= 20:
        raise HTTPException(status_code=400, detail="max_pages 须在 1 至 20 之间")
    task = store.create_task({**body.model_dump(), "account_ids": ids}, "manual")
    return ok({"task": task, "job": enqueue_collect(task["id"])})


@app.get("/api/collector/collect-tasks", dependencies=[Depends(require_local_key)])
def schedules():
    rows = store.tasks()
    return ok({"list": rows, "total": len(rows)})


def _schedule_data(body: ScheduleBody) -> dict:
    ids = _account_ids(body.account_ids)
    if not 1 <= body.max_pages <= 20:
        raise HTTPException(status_code=400, detail="max_pages 须在 1 至 20 之间")
    try:
        CronTrigger.from_crontab(body.cron)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的 cron 表达式") from exc
    return {**body.model_dump(), "account_ids": ids, "name": body.name.strip() or "定时采集"}


@app.post("/api/collector/collect-tasks", dependencies=[Depends(require_local_key)])
def add_schedule(body: ScheduleBody):
    task = store.create_task(_schedule_data(body), "scheduled")
    reload_schedules()
    return ok(task)


@app.put("/api/collector/collect-tasks/{task_id}", dependencies=[Depends(require_local_key)])
def update_schedule(task_id: int, body: ScheduleBody):
    if not store.task(task_id) or store.task(task_id)["type"] != "scheduled":
        raise HTTPException(status_code=404, detail="定时任务不存在")
    task = store.update_task(task_id, _schedule_data(body))
    reload_schedules()
    return ok(task)


@app.delete("/api/collector/collect-tasks/{task_id}", dependencies=[Depends(require_local_key)])
def delete_schedule(task_id: int):
    if not store.delete_task(task_id):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    reload_schedules()
    return ok({"id": task_id})


@app.get("/api/collector/jobs", dependencies=[Depends(require_local_key)])
def job_status():
    with state_lock:
        current = next((dict(row) for row in jobs.values() if row["status"] == "running"), None)
        queued = [dict(row) for row in jobs.values() if row["status"] == "queued"]
    return ok({"current": current, "queued": queued, "running": current is not None,
               "queue_size": len(queued)})


@app.get("/api/collector/collect-logs", dependencies=[Depends(require_local_key)])
def collect_logs(task_id: int):
    return ok({"list": store.logs(task_id)})


@app.get("/api/collector/articles", dependencies=[Depends(require_local_key)])
def articles(account_id: int, date_from: str = "", date_to: str = "", limit: int = 50, offset: int = 0):
    _dates(date_from, date_to)
    return ok(store.articles(account_id, date_from, date_to,
                             limit=min(max(limit, 1), 100), offset=max(offset, 0)))


@app.get("/api/collector/articles/{article_id}", dependencies=[Depends(require_local_key)])
def article(article_id: int):
    value = store.article(article_id)
    if not value:
        raise HTTPException(status_code=404, detail="文章不存在")
    return ok(value)
