"""
AI-SyncForge 核心状态机与存储引擎 (V2)
基于 SQLite 的任务队列持久化层，提供原子性 CRUD 操作。
V2 新增：DAG 依赖路由、拓扑排序、级联阻断、平滑迁移。
"""

import json
import os
import sqlite3
import threading
from pathlib import Path

# 从环境变量获取数据库路径，默认为当前目录下的 task_queue.db
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "task_queue.db"))

# 确保数据库所在的目录存在，否则 SQLite 会报 "unable to open database file" 错误
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_write_lock = threading.Lock()

FAIL_STATUSES = ("fail", "fail_by_ops_intervention")

# MVP 阶段硬编码串行锁，彻底杜绝契约冲突
MAX_CONCURRENCY = 1


def _connect() -> sqlite3.Connection:
    """创建数据库连接，启用 Row 模式。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    初始化数据库：开启 WAL 模式、创建表、索引与触发器（幂等）。
    V2 新增字段：depends_on, metadata, task_name。
    V2 新增状态：blocked, in_progress。
    自动执行平滑迁移。
    """
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
                                    'fail', 'fail_by_ops_intervention',
                                    'blocked', 'in_progress', 'completed_dev'
                                )),
                report_path     TEXT,
                retry_count     INTEGER DEFAULT 0,
                depends_on      TEXT,
                metadata        TEXT,
                task_name       TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_project_status
            ON tasks(project_name, status);
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
            AFTER UPDATE ON tasks
            FOR EACH ROW
            WHEN (NEW.updated_at IS OLD.updated_at)
            BEGIN
                UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END;
        """)

        conn.commit()

        # 平滑迁移：为已有表补全 V2 新字段
        _migrate_v1_to_v2(conn)

    finally:
        conn.close()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    平滑迁移：检测并补全 V2 新增字段。
    若字段已存在则静默跳过（幂等）。
    同时处理 CHECK 约束的升级（SQLite 不支持 ALTER CHECK，通过重建表实现）。
    """
    cursor = conn.execute("PRAGMA table_info(tasks);")
    existing_columns = {row[1] for row in cursor.fetchall()}

    v2_columns = {
        "depends_on": "TEXT",
        "metadata": "TEXT",
        "task_name": "TEXT",
    }

    for col_name, col_type in v2_columns.items():
        if col_name not in existing_columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type};")
            conn.commit()

    # 检查是否需要升级 CHECK 约束（V1 -> V2 新增 blocked, in_progress）
    # SQLite 不支持 ALTER CHECK，需要通过表重建
    # 这里使用一个安全策略：尝试插入新状态值，如果被拒绝则重建表
    _ensure_v2_check_constraint(conn)


def _ensure_v2_check_constraint(conn: sqlite3.Connection) -> None:
    """
    确保 CHECK 约束支持 V2 新增状态值。
    SQLite 不支持修改 CHECK 约束，需要通过表重建。
    """
    try:
        # 尝试插入 completed_dev 状态的行来测试约束
        conn.execute(
            "INSERT INTO tasks (project_name, status) VALUES ('__constraint_test__', 'completed_dev');"
        )
        # 如果成功说明约束已经是 V2，删除测试行
        conn.execute("DELETE FROM tasks WHERE project_name = '__constraint_test__';")
        conn.commit()
    except sqlite3.IntegrityError:
        # 约束拒绝了 blocked 状态，需要重建表
        _rebuild_table_with_v2_constraint(conn)


def _rebuild_table_with_v2_constraint(conn: sqlite3.Connection) -> None:
    """
    通过 SQLite 推荐的表重建方式升级 CHECK 约束。
    12 步安全重建流程。
    """
    conn.execute("PRAGMA foreign_keys=OFF;")

    conn.execute("""
        CREATE TABLE tasks_v2 (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name    TEXT,
            code_content    TEXT,
            test_requirement TEXT,
            task_type       TEXT DEFAULT 'dev_test',
            priority        INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'testing', 'success',
                                'fail', 'fail_by_ops_intervention',
                                'blocked', 'in_progress', 'completed_dev'
                            )),
            report_path     TEXT,
            retry_count     INTEGER DEFAULT 0,
            depends_on      TEXT,
            metadata        TEXT,
            task_name       TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 迁移数据（只复制存在的列）
    cursor = conn.execute("PRAGMA table_info(tasks);")
    old_columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        "id", "project_name", "code_content", "test_requirement",
        "task_type", "priority", "status", "report_path", "retry_count",
        "depends_on", "metadata", "task_name", "created_at", "updated_at",
    ]

    shared_columns = [c for c in new_columns if c in old_columns]
    cols_str = ", ".join(shared_columns)

    conn.execute(f"INSERT INTO tasks_v2 ({cols_str}) SELECT {cols_str} FROM tasks;")
    conn.execute("DROP TABLE tasks;")
    conn.execute("ALTER TABLE tasks_v2 RENAME TO tasks;")

    # 重建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tasks_project_status
        ON tasks(project_name, status);
    """)

    # 重建触发器
    conn.execute("DROP TRIGGER IF EXISTS trg_tasks_updated_at;")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
        AFTER UPDATE ON tasks
        FOR EACH ROW
        WHEN (NEW.updated_at IS OLD.updated_at)
        BEGIN
            UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
        END;
    """)

    conn.execute("PRAGMA foreign_keys=ON;")
    conn.commit()


# ─── CRUD 接口 ───────────────────────────────────────────────────────────────


def create_task(
    project: str,
    code: str,
    req: str,
    task_type: str = "dev_test",
    priority: int = 0,
    depends_on: str | None = None,
    metadata: str | None = None,
    task_name: str | None = None,
) -> int:
    """
    插入新任务，返回 task_id。
    V2 新增参数：depends_on, metadata, task_name。
    若 depends_on 非空，执行依赖验证（存在性检查）。
    """
    # 依赖验证
    if depends_on is not None:
        _validate_dependencies(depends_on)

    with _write_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "INSERT INTO tasks "
                "(project_name, code_content, test_requirement, task_type, priority, "
                "depends_on, metadata, task_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (project, code, req, task_type, priority, depends_on, metadata, task_name),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def _validate_dependencies(depends_on_json: str) -> None:
    """
    验证 depends_on 中的所有 task ID 是否存在。
    若有不存在的 ID，抛出 ValueError。
    """
    try:
        dep_ids = json.loads(depends_on_json)
    except (json.JSONDecodeError, TypeError):
        raise ValueError(f"depends_on 格式无效，应为 JSON 数组: {depends_on_json}")

    if not isinstance(dep_ids, list):
        raise ValueError(f"depends_on 应为数组: {depends_on_json}")

    if not dep_ids:
        return

    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in dep_ids)
        cursor = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders});",
            dep_ids,
        )
        found_ids = {row[0] for row in cursor.fetchall()}
        missing = set(dep_ids) - found_ids
        if missing:
            raise ValueError(f"依赖的任务 ID 不存在: {missing}")
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


def update_task_metadata(task_id: int, metadata: str) -> None:
    """更新任务的 metadata 字段。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE tasks SET metadata = ? WHERE id = ?;",
                (metadata, task_id),
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
                "    WHERE status = 'completed_dev' AND task_type = 'dev_test' "
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


# ─── V2 DAG 路由接口 ─────────────────────────────────────────────────────────


def get_available_tasks(project: str) -> list[dict]:
    """
    获取指定项目中所有可用任务（依赖已满足的 pending 任务）。
    
    可用条件：
    1. status = 'pending'
    2. depends_on IS NULL 或 depends_on 中所有 ID 的状态均为 'success'
    
    按 priority DESC, created_at ASC 排序。
    """
    conn = _connect()
    try:
        # 获取所有 pending 任务
        cursor = conn.execute(
            "SELECT * FROM tasks "
            "WHERE project_name = ? AND status = 'pending' "
            "ORDER BY priority DESC, created_at ASC;",
            (project,),
        )
        pending_tasks = [dict(row) for row in cursor.fetchall()]

        available = []
        for task in pending_tasks:
            if task["depends_on"] is None:
                available.append(task)
                continue

            try:
                dep_ids = json.loads(task["depends_on"])
            except (json.JSONDecodeError, TypeError):
                # 无法解析依赖，跳过
                continue

            if not dep_ids:
                available.append(task)
                continue

            # 检查所有依赖是否已 success
            placeholders = ",".join("?" for _ in dep_ids)
            dep_cursor = conn.execute(
                f"SELECT COUNT(*) FROM tasks "
                f"WHERE id IN ({placeholders}) AND status = 'success';",
                dep_ids,
            )
            success_count = dep_cursor.fetchone()[0]
            if success_count == len(dep_ids):
                available.append(task)

        return available
    finally:
        conn.close()


def get_next_available_task(project: str) -> dict | None:
    """
    原子性地获取并锁定一个可用的 DAG 任务。
    状态从 pending 变为 in_progress。
    遵循 MAX_CONCURRENCY 串行锁。
    
    按 priority DESC, created_at ASC 排序。
    """
    with _write_lock:
        conn = _connect()
        try:
            # 检查并发限制：当前 in_progress 的任务数量
            cursor = conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE project_name = ? AND status = 'in_progress';",
                (project,),
            )
            in_progress_count = cursor.fetchone()[0]
            if in_progress_count >= MAX_CONCURRENCY:
                return None

            # 获取所有 pending 任务
            cursor = conn.execute(
                "SELECT * FROM tasks "
                "WHERE project_name = ? AND status = 'pending' "
                "ORDER BY priority DESC, created_at ASC;",
                (project,),
            )
            pending_tasks = [dict(row) for row in cursor.fetchall()]

            for task in pending_tasks:
                if task["depends_on"] is None:
                    # 无依赖，直接可用
                    pass
                else:
                    try:
                        dep_ids = json.loads(task["depends_on"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    if dep_ids:
                        placeholders = ",".join("?" for _ in dep_ids)
                        dep_cursor = conn.execute(
                            f"SELECT COUNT(*) FROM tasks "
                            f"WHERE id IN ({placeholders}) AND status = 'success';",
                            dep_ids,
                        )
                        success_count = dep_cursor.fetchone()[0]
                        if success_count != len(dep_ids):
                            continue

                # 原子锁定
                lock_cursor = conn.execute(
                    "UPDATE tasks SET status = 'in_progress' "
                    "WHERE id = ? AND status = 'pending' "
                    "RETURNING *;",
                    (task["id"],),
                )
                row = lock_cursor.fetchone()
                conn.commit()
                if row is not None:
                    return dict(row)

            return None
        finally:
            conn.close()


def cascade_block(failed_task_id: int) -> list[int]:
    """
    级联阻断：将所有依赖 failed_task_id 的下游任务状态设为 blocked。
    采用 BFS 方式遍历整个依赖树。
    
    Returns:
        被阻断的任务 ID 列表。
    """
    blocked_ids = []

    with _write_lock:
        conn = _connect()
        try:
            # BFS 遍历下游依赖
            queue = [failed_task_id]
            visited = {failed_task_id}

            while queue:
                current_id = queue.pop(0)

                # 查找所有 depends_on 包含 current_id 的任务
                cursor = conn.execute(
                    "SELECT id, depends_on, status FROM tasks "
                    "WHERE depends_on IS NOT NULL AND status IN ('pending', 'blocked');",
                )
                for row in cursor.fetchall():
                    try:
                        dep_ids = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    if current_id in dep_ids and row[0] not in visited:
                        visited.add(row[0])
                        # 阻断该任务
                        conn.execute(
                            "UPDATE tasks SET status = 'blocked' WHERE id = ?;",
                            (row[0],),
                        )
                        blocked_ids.append(row[0])
                        queue.append(row[0])

            conn.commit()
        finally:
            conn.close()

    return blocked_ids


def get_predecessor_summaries(task_id: int) -> list[dict]:
    """
    获取指定任务的直接前置节点的摘要。
    用于上下文窗口滑动管理（Context Pruning）。
    
    Returns:
        前置任务的 [{id, task_name, summary}] 列表。
    """
    conn = _connect()
    try:
        task = get_task_by_id(task_id)
        if task is None or task["depends_on"] is None:
            return []

        try:
            dep_ids = json.loads(task["depends_on"])
        except (json.JSONDecodeError, TypeError):
            return []

        if not dep_ids:
            return []

        summaries = []
        for dep_id in dep_ids:
            dep_task = get_task_by_id(dep_id)
            if dep_task is None:
                continue

            summary = None
            if dep_task["metadata"]:
                try:
                    meta = json.loads(dep_task["metadata"])
                    summary = meta.get("summary")
                except (json.JSONDecodeError, TypeError):
                    pass

            summaries.append({
                "id": dep_task["id"],
                "task_name": dep_task["task_name"],
                "summary": summary,
                "status": dep_task["status"],
            })

        return summaries
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
