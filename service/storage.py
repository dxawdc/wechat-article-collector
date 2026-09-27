"""Small SQLite store for shelf selections, collection jobs and articles."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(row) -> dict | None:
    return dict(row) if row else None


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, fakeid TEXT NOT NULL UNIQUE,
                    avatar TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                    account_ids TEXT NOT NULL, date_from TEXT NOT NULL DEFAULT '',
                    date_to TEXT NOT NULL DEFAULT '', max_pages INTEGER NOT NULL DEFAULT 5,
                    cron TEXT NOT NULL DEFAULT '', export_format TEXT NOT NULL DEFAULT 'md',
                    status TEXT NOT NULL DEFAULT 'active',
                    last_run_at TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
                    mp_account_id INTEGER NOT NULL, mp_name TEXT NOT NULL, title TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '',
                    digest TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
                    publish_time TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS articles_account_date ON articles(mp_account_id,publish_time);
                CREATE TABLE IF NOT EXISTS collect_logs (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, account_id INTEGER,
                    level TEXT NOT NULL, message TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "export_format" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN export_format TEXT NOT NULL DEFAULT 'md'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def accounts(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM accounts ORDER BY name")]

    def add_account(self, source: dict) -> dict:
        with self.connect() as db:
            db.execute("""INSERT INTO accounts(name,fakeid,avatar,intro,enabled) VALUES(?,?,?,?,1)
                ON CONFLICT(fakeid) DO UPDATE SET name=excluded.name,avatar=excluded.avatar,intro=excluded.intro""",
                (source["name"], source["fakeid"], source.get("avatar") or "", source.get("intro") or ""))
            return _row(db.execute("SELECT * FROM accounts WHERE fakeid=?", (source["fakeid"],)).fetchone())

    def remove_account(self, account_id: int) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    def account(self, account_id: int) -> dict | None:
        with self.connect() as db:
            return _row(db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())

    def create_task(self, data: dict, task_type: str) -> dict:
        with self.connect() as db:
            cur = db.execute("""INSERT INTO tasks(name,type,account_ids,date_from,date_to,max_pages,cron,export_format,status)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (data.get("name") or "单次采集", task_type, json.dumps(data["account_ids"]),
                 data.get("date_from") or "", data.get("date_to") or "",
                 data.get("max_pages") or 5, data.get("cron") or "", data.get("export_format") or "md",
                 "active" if task_type == "scheduled" else "paused"))
            return self._task(db, cur.lastrowid)

    def _task(self, db, task_id: int) -> dict | None:
        row = _row(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if row:
            row["account_ids"] = json.loads(row["account_ids"])
        return row

    def task(self, task_id: int) -> dict | None:
        with self.connect() as db:
            return self._task(db, task_id)

    def tasks(self, task_type: str = "scheduled") -> list[dict]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM tasks WHERE type=? ORDER BY id DESC", (task_type,))]
            return [self._task(db, task_id) for task_id in ids]

    def update_task(self, task_id: int, data: dict) -> dict | None:
        with self.connect() as db:
            if not self._task(db, task_id):
                return None
            db.execute("""UPDATE tasks SET name=?,account_ids=?,cron=?,max_pages=?,export_format=?,status='active' WHERE id=?""",
                       (data["name"], json.dumps(data["account_ids"]), data["cron"],
                        data["max_pages"], data["export_format"], task_id))
            return self._task(db, task_id)

    def delete_task(self, task_id: int) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM tasks WHERE id=? AND type='scheduled'", (task_id,)).rowcount > 0

    def mark_task_run(self, task_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET last_run_at=? WHERE id=?", (_now(), task_id))

    def log(self, task_id: int, account_id: int | None, level: str, message: str,
            new_count: int = 0, error: str = "") -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO collect_logs(task_id,account_id,level,message,new_count,error,created_at)
                VALUES(?,?,?,?,?,?,?)""", (task_id, account_id, level, message, new_count, error, _now()))

    def logs(self, task_id: int) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT level,message,new_count,error FROM collect_logs WHERE task_id=? ORDER BY id", (task_id,))]

    def known_articles(self, account_id: int) -> tuple[set[str], set[str]]:
        with self.connect() as db:
            rows = db.execute("SELECT source_id,content FROM articles WHERE mp_account_id=?", (account_id,)).fetchall()
        return ({row["source_id"] for row in rows},
                {row["source_id"] for row in rows if not row["content"]})

    def upsert_article(self, account: dict, article: dict) -> int:
        publish = article.get("publish_time")
        if isinstance(publish, datetime):
            publish = publish.isoformat(timespec="seconds")
        with self.connect() as db:
            existing = db.execute("SELECT id,content FROM articles WHERE source_id=?",
                                  (article["source_id"],)).fetchone()
            if existing:
                if article.get("content") and not existing["content"]:
                    db.execute("UPDATE articles SET content=?,digest=?,cover=? WHERE id=?",
                               (article["content"], article.get("digest") or "",
                                article.get("cover") or "", existing["id"]))
                return 0
            db.execute("""INSERT INTO articles(source_id,mp_account_id,mp_name,title,url,cover,digest,content,publish_time)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (article["source_id"], account["id"], account["name"], article["title"],
                 article.get("url") or "", article.get("cover") or "", article.get("digest") or "",
                 article.get("content") or "", str(publish or "")))
            return 1

    def articles(self, account_id: int, date_from: str = "", date_to: str = "",
                 limit: int = 50, offset: int = 0) -> dict:
        where = " WHERE mp_account_id=?"
        params: list = [account_id]
        if date_from:
            where += " AND substr(publish_time,1,10)>=?"
            params.append(date_from)
        if date_to:
            where += " AND substr(publish_time,1,10)<=?"
            params.append(date_to)
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM articles" + where, params).fetchone()[0]
            rows = [self._article(dict(row)) for row in db.execute(
                "SELECT * FROM articles" + where + " ORDER BY publish_time DESC LIMIT ? OFFSET ?",
                (*params, limit, offset))]
        return {"list": rows, "total": total}

    def article(self, article_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return self._article(dict(row)) if row else None

    def article_by_source(self, source_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM articles WHERE source_id=?", (source_id,)).fetchone()
        return self._article(dict(row)) if row else None

    @staticmethod
    def _article(row: dict) -> dict:
        row["publish_time_text"] = row.get("publish_time", "").replace("T", " ")
        return row
