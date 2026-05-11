"""
AI-SyncForge 核心状态机与存储引擎
基于 SQLite 的任务队列持久化层，提供原子性 CRUD 操作。
"""

import os
import sqlite3
import threading
from pathlib import Path

# 从环境变量获取数据库路径，默认为当前目录下的 task_queue.db
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "task_queue.db"))

_write_lock = threading.Lock()

FAIL_STATUSES = ("fail", "fail_by_ops_intervention")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库：开启 WAL 模式、创建表、索引与触发器（幂等）。"""
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name    TEXT,
                code_content    TEXT,
                test_requirement TEXT,
                task_type       TEXT DEFAULT 'dev_test',
                priority        INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'pending'
                                CHECK(status IN (
                                    'pending', 'testing', 'success',
                                    'fail', 'fail_by_ops_intervention'
                                )),
                report_path     TEXT,
                retry_count     INTEGER DEFAULT 0,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
            AFTER UPDATE ON tasks
            FOR EACH ROW
            BEGIN
                UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END;
        """)

        conn.commit()
    finally:
        conn.close()


# ─── CRUD 接口 ───────────────────────────────────────────────────────────────


def create_task(
    project: str,
    code: str,
    req: str,
    task_type: str = "dev_test",
    priority: int = 0,
) -> int:
    """插入新任务，返回 task_id。"""
    with _write_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "INSERT INTO tasks (project_name, code_content, test_requirement, task_type, priority) "
                "VALUES (?, ?, ?, ?, ?);",
                (project, code, req, task_type, priority),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def update_task_status(task_id: int, status: str, report_path: str | None = None) -> None:
    """
    更新任务状态及报告路径。
    若状态为失败类型，同时将 retry_count 加 1。
    """
    with _write_lock:
        conn = _connect()
        try:
            if status in FAIL_STATUSES:
                conn.execute(
                    "UPDATE tasks SET status = ?, report_path = ?, retry_count = retry_count + 1 "
                    "WHERE id = ?;",
                    (status, report_path, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = ?, report_path = ? WHERE id = ?;",
                    (status, report_path, task_id),
                )
            conn.commit()
        finally:
            conn.close()


def get_pending_task() -> dict | None:
    """
    原子性地获取并锁定一个 dev_test 类型的待处理任务。
    按优先级降序、创建时间升序排列。
    """
    with _write_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'testing' "
                "WHERE id = ("
                "    SELECT id FROM tasks "
                "    WHERE status = 'pending' AND task_type = 'dev_test' "
                "    ORDER BY priority DESC, created_at ASC LIMIT 1"
                ") RETURNING *;"
            )
            row = cursor.fetchone()
            conn.commit()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()


def poll_ops_task() -> dict | None:
    """
    原子性地获取并锁定一个 ops_task 类型的待处理任务。
    专供 Ops-Forge 使用，按优先级降序、创建时间升序排列。
    """
    with _write_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'testing' "
                "WHERE id = ("
                "    SELECT id FROM tasks "
                "    WHERE status = 'pending' AND task_type = 'ops_task' "
                "    ORDER BY priority DESC, created_at ASC LIMIT 1"
                ") RETURNING *;"
            )
            row = cursor.fetchone()
            conn.commit()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()


def get_task_by_id(task_id: int) -> dict | None:
    """查询特定任务的当前详情。"""
    conn = _connect()
    try:
        cursor = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
