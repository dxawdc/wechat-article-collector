"""Control the bundled local WeRead article collector."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from api import ApiError, CollectorClient
from exporter import export_articles


BEIJING = timezone(timedelta(hours=8))
FORMATS = ("md", "html", "body-html", "both", "reading")


def emit(data: dict) -> int:
    print(json.dumps(data, ensure_ascii=True, indent=2))
    return 1 if data.get("status") == "error" else 0


def connection_check(client: CollectorClient) -> dict:
    health = client.health()
    status = client.request("/api/collector/weread/status")
    return {
        "status": "ready",
        "serviceUrl": client.url,
        "health": health,
        "loginStatus": bool(status.get("login_status")),
        "articleChannelStatus": status.get("article_channel_last_check_ok"),
        "verificationUrl": status.get("article_channel_verification_url") or "",
    }


def qr_path(client: CollectorClient) -> Path:
    suffix = hashlib.sha256(client.url.encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"weread-skill-login-{suffix}.png"


def login_qr(client: CollectorClient) -> dict:
    info = client.request("/api/collector/weread/qrcode", method="POST")
    result = {"status": "needs_login", "message": info.get("message") or "请扫描微信读书登录二维码"}
    if info.get("is_exists"):
        image = client.request("/api/collector/weread/qrcode-image", binary=True)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("本地采集服务返回的登录二维码不是 PNG 图片")
        path = qr_path(client)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(image)
        result["qrImage"] = str(path)
    return result


def status(client: CollectorClient) -> dict:
    value = client.request("/api/collector/weread/status")
    if value.get("login_status"):
        qr_path(client).unlink(missing_ok=True)
    return {"status": "ok", "serviceUrl": client.url, **value}


def ensure_account(client: CollectorClient, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("公众号名称不能为空")
    shelf = client.query("/api/collector/weread/shelf", q=name, limit=100)
    if not any(item.get("name") == name for item in shelf.get("list", [])):
        return {"status": "needs_shelf_add", "name": name,
                "shelfUrl": "https://weread.qq.com/web/shelf",
                "message": "请先在微信读书 App 将该公众号加入书架"}
    accounts = client.request("/api/collector/accounts")["list"]
    existing = next((row for row in accounts if row.get("name") == name
                     and str(row.get("fakeid") or "").startswith("MP_WXS_")), None)
    if existing:
        return existing
    return client.request("/api/collector/accounts", method="POST", data={"name": name})


def resolve_account(client: CollectorClient, name: str) -> dict:
    accounts = client.request("/api/collector/accounts")["list"]
    matches = [row for row in accounts if row.get("name") == name]
    if len(matches) != 1:
        raise ValueError("本地采集名单中没有唯一匹配的公众号；请先查看 accounts")
    return matches[0]


def date_window(args) -> tuple[str, str]:
    today = datetime.now(BEIJING).date()
    if args.since or args.until:
        until = datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else today
        since = datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else until - timedelta(days=args.days - 1)
    else:
        if not 1 <= args.days <= 365:
            raise ValueError("--days 须在 1 至 365 之间")
        since, until = today - timedelta(days=args.days - 1), today
    if since > until:
        raise ValueError("开始日期不能晚于结束日期")
    return since.isoformat(), until.isoformat()


def verification_result(client: CollectorClient, message: str = "") -> dict:
    current = client.request("/api/collector/weread/status")
    return {"status": "needs_verification", "message": message or current.get("article_channel_last_check_message") or "请完成人工验证",
            "verificationUrl": current.get("article_channel_verification_url") or "",
            "adminUrl": client.url + "/verify"}


def _classify_failure(client: CollectorClient, message: str) -> dict:
    if any(marker in message for marker in ("-2041", "滑块", "安全验证", "captcha_required")):
        return verification_result(client, message)
    if any(marker in message for marker in ("-2010", "重新扫码", "登录失效", "授权已失效")):
        return {"status": "needs_login", "message": message}
    if any(marker in message for marker in ("频控", "冷却", "429")):
        return {"status": "cooldown", "message": message}
    return {"status": "failed", "message": message}


def finish_job(client: CollectorClient, task_id: int, job_id: int | None, account: dict,
               since: str, until: str, fmt: str, wait_seconds: int) -> dict:
    deadline = time.monotonic() + max(0, min(wait_seconds, 900))
    first_seen = time.monotonic()
    while True:
        logs = client.query("/api/collector/collect-logs", task_id=task_id).get("list", [])
        failures = [row for row in logs if row.get("level") == "error"]
        if failures:
            failure = _classify_failure(client, str(failures[-1].get("error") or failures[-1].get("message") or "采集失败"))
            return {**failure, "taskId": task_id, "logs": logs}
        jobs = client.request("/api/collector/jobs")
        active = [jobs.get("current"), *(jobs.get("queued") or [])]
        pending = any(row and (int(row.get("id") or 0) == job_id if job_id else int(row.get("task_id") or 0) == task_id)
                      for row in active)
        completed = any(row.get("level") == "info" and "采集完成" in str(row.get("message") or "") for row in logs)
        if completed and not pending:
            exported = export_articles(client, account, since, until, fmt)
            return {"status": "completed", "taskId": task_id, "newCount": sum(int(row.get("new_count") or 0) for row in logs),
                    "window": {"since": since, "until": until}, "export": exported, "logs": logs}
        if time.monotonic() >= deadline:
            status_name = "running" if pending else "unconfirmed"
            return {"status": status_name, "taskId": task_id, "jobId": job_id,
                    "window": {"since": since, "until": until}, "message": "采集任务尚未确认完成；请使用 resume 查询，不要重新入队"}
        if not pending and not completed and time.monotonic() - first_seen > 20:
            return {"status": "unconfirmed", "taskId": task_id, "jobId": job_id,
                    "message": "本地采集队列中已无此任务且没有完成日志；请检查 GDN 服务状态"}
        time.sleep(3)


def daily_cron(value: str) -> str:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise ValueError("每日时间须为 HH:MM，例如 08:00") from exc
    return f"{minute} {hour} * * *"


def schedule_cron(args) -> str:
    if args.cron:
        return args.cron
    if args.daily:
        return daily_cron(args.daily)
    raise ValueError("请指定 --daily HH:MM 或 --cron 表达式")


def add_date_args(command, *, default_days: int = 7):
    command.add_argument("--days", type=int, default=default_days, help="含今天的最近自然日数量，默认 7")
    command.add_argument("--since", default="")
    command.add_argument("--until", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "jobs", "accounts", "schedules", "login-qr", "check-verification"):
        sub.add_parser(name)
    shelf = sub.add_parser("shelf"); shelf.add_argument("--name", default="")
    add = sub.add_parser("add"); add.add_argument("--name", required=True)
    remove = sub.add_parser("remove"); remove.add_argument("--name", required=True)
    logs = sub.add_parser("logs"); logs.add_argument("--task-id", type=int, required=True)
    unschedule = sub.add_parser("unschedule"); unschedule.add_argument("--title", required=True)
    for name in ("run", "collect", "export", "articles", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--name", required=True)
        add_date_args(command)
        if name in ("run", "collect"):
            command.add_argument("--max-pages", type=int, default=5)
        if name in ("run", "export", "resume"):
            command.add_argument("--format", choices=FORMATS, default="md")
        if name in ("run", "resume"):
            command.add_argument("--wait-seconds", type=int, default=180)
        if name == "resume":
            command.add_argument("--task-id", type=int, required=True)
            command.add_argument("--job-id", type=int)
        if name == "articles":
            command.add_argument("--limit", type=int, default=50)
            command.add_argument("--offset", type=int, default=0)
    schedule = sub.add_parser("schedule")
    schedule.add_argument("--names", nargs="+", required=True)
    schedule.add_argument("--daily", default="")
    schedule.add_argument("--cron", default="")
    schedule.add_argument("--title", default="定时采集")
    schedule.add_argument("--max-pages", type=int, default=5)
    schedule.add_argument("--format", choices=FORMATS, default="md")
    return parser


def execute(args) -> dict:
    client = CollectorClient()
    if not client.key:
        return {"status": "error", "message": "本地采集服务认证信息缺失；请通过 run.py 启动"}
    if args.command == "doctor":
        return connection_check(client)
    if args.command == "status":
        return status(client)
    if args.command == "login-qr":
        return login_qr(client)
    if args.command == "check-verification":
        client.request("/api/collector/weread/check", method="POST")
        return status(client)
    if args.command == "shelf":
        return {"status": "ok", **client.query("/api/collector/weread/shelf", q=args.name)}
    if args.command == "accounts":
        return {"status": "ok", **client.request("/api/collector/accounts")}
    if args.command == "add":
        return ensure_account(client, args.name)
    if args.command == "remove":
        account = resolve_account(client, args.name)
        return {"status": "removed", **client.request(f"/api/collector/accounts/{account['id']}", method="DELETE")}
    if args.command == "jobs":
        return {"status": "ok", **client.request("/api/collector/jobs")}
    if args.command == "logs":
        return {"status": "ok", **client.query("/api/collector/collect-logs", task_id=args.task_id)}
    if args.command == "schedules":
        return {"status": "ok", **client.request("/api/collector/collect-tasks")}
    if args.command == "unschedule":
        rows = client.request("/api/collector/collect-tasks")["list"]
        task = next((row for row in rows if row.get("name") == args.title), None)
        if not task:
            return {"status": "not_found", "title": args.title}
        return {"status": "removed", **client.request(f"/api/collector/collect-tasks/{task['id']}", method="DELETE")}
    if args.command == "schedule":
        cron = schedule_cron(args)
        ids = []
        for name in args.names:
            account = ensure_account(client, name)
            if account.get("status", "").startswith("needs_"):
                return account
            ids.append(account["id"])
        rows = client.request("/api/collector/collect-tasks")["list"]
        existing = next((row for row in rows if row.get("name") == args.title), None)
        path = f"/api/collector/collect-tasks/{existing['id']}" if existing else "/api/collector/collect-tasks"
        task = client.request(path, method="PUT" if existing else "POST",
                              data={"name": args.title, "account_ids": ids, "cron": cron,
                                    "max_pages": args.max_pages, "export_format": args.format})
        return {"status": "scheduled", "task": task, "timezone": "Asia/Shanghai"}
    since, until = date_window(args)
    if args.command == "articles":
        account = resolve_account(client, args.name)
        result = client.query("/api/collector/articles", account_id=account["id"], date_from=since,
                              date_to=until, limit=max(1, min(args.limit, 100)), offset=max(0, args.offset))
        return {"status": "ok", "window": {"since": since, "until": until}, **result}
    if args.command == "export":
        account = resolve_account(client, args.name)
        return {"status": "completed", "window": {"since": since, "until": until},
                "export": export_articles(client, account, since, until, args.format)}
    if args.command == "resume":
        account = resolve_account(client, args.name)
        return finish_job(client, args.task_id, args.job_id, account, since, until, args.format, args.wait_seconds)
    if args.command == "run":
        current = status(client)
        if not current.get("login_status"):
            return login_qr(client)
        if current.get("article_channel_last_check_reason") == "captcha_required":
            return verification_result(client)
    try:
        account = ensure_account(client, args.name)
    except ApiError as exc:
        if args.command == "run" and any(marker in str(exc) for marker in ("-2010", "重新扫码", "登录失效", "授权已失效")):
            return login_qr(client)
        raise
    if account.get("status", "").startswith("needs_"):
        return account
    queued = client.request("/api/collector/collect", method="POST",
                            data={"account_ids": [account["id"]], "date_from": since,
                                  "date_to": until, "max_pages": args.max_pages})
    task_id = int(queued["task"]["id"])
    job_id = int(queued["job"]["id"])
    if args.command == "collect":
        return {"status": "queued", "taskId": task_id, "jobId": job_id,
                "window": {"since": since, "until": until}}
    return finish_job(client, task_id, job_id, account, since, until, args.format, args.wait_seconds)


def main() -> int:
    args = build_parser().parse_args()
    try:
        return emit(execute(args))
    except (ApiError, ValueError, OSError) as exc:
        return emit({"status": "error", "message": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
