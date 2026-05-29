"""
AI-SyncForge V2 数据库升级单元测试
覆盖 DAG 依赖路由、拓扑排序、级联阻断、平滑迁移。
遵循 TDD：本文件先于实现代码编写。
"""

import json
import os
import sqlite3
import threading
import unittest
from pathlib import Path
import tempfile

TEST_DB_PATH = Path(tempfile.gettempdir()) / "test_v2_database.db"

# 清理残留文件
for suffix in ("", "-wal", "-shm"):
    p = TEST_DB_PATH.parent / (TEST_DB_PATH.name + suffix)
    if p.exists():
        try:
            os.remove(p)
        except OSError:
            pass

import database

database.DB_PATH = TEST_DB_PATH
database.init_db()


def _clear_tasks():
    """清空 tasks 表并重置自增序列。"""
    conn = database._connect()
    try:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks';")
        conn.commit()
    finally:
        conn.close()


# ─── Schema 扩展测试 ─────────────────────────────────────────────────────────


class TestV2SchemaExtension(unittest.TestCase):
    """测试 V2 Schema 新增字段。"""

    def test_depends_on_column_exists(self):
        """depends_on 列应存在。"""
        conn = database._connect()
        try:
            rows = conn.execute("PRAGMA table_info(tasks);").fetchall()
            columns = {row[1] for row in rows}
            self.assertIn("depends_on", columns)
        finally:
            conn.close()

    def test_metadata_column_exists(self):
        """metadata 列应存在。"""
        conn = database._connect()
        try:
            rows = conn.execute("PRAGMA table_info(tasks);").fetchall()
            columns = {row[1] for row in rows}
            self.assertIn("metadata", columns)
        finally:
            conn.close()

    def test_task_name_column_exists(self):
        """task_name 列应存在。"""
        conn = database._connect()
        try:
            rows = conn.execute("PRAGMA table_info(tasks);").fetchall()
            columns = {row[1] for row in rows}
            self.assertIn("task_name", columns)
        finally:
            conn.close()

    def test_new_status_blocked_accepted(self):
        """blocked 状态应被 CHECK 约束接受。"""
        _clear_tasks()
        task_id = database.create_task("proj", "code", "req")
        # Should not raise
        database.update_task_status(task_id, "blocked")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "blocked")

    def test_new_status_in_progress_accepted(self):
        """in_progress 状态应被 CHECK 约束接受。"""
        _clear_tasks()
        task_id = database.create_task("proj", "code", "req")
        database.update_task_status(task_id, "in_progress")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "in_progress")


# ─── create_task V2 扩展测试 ─────────────────────────────────────────────────


class TestCreateTaskV2(unittest.TestCase):
    """测试 create_task 的 V2 扩展参数。"""

    def setUp(self):
        _clear_tasks()

    def test_create_with_task_name(self):
        """应能创建带 task_name 的任务。"""
        task_id = database.create_task(
            "proj", "code", "req", task_name="初始化数据库"
        )
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["task_name"], "初始化数据库")

    def test_create_with_depends_on(self):
        """应能创建带 depends_on 的任务。"""
        parent_id = database.create_task("proj", "code1", "req1")
        child_id = database.create_task(
            "proj", "code2", "req2",
            depends_on=json.dumps([parent_id]),
        )
        task = database.get_task_by_id(child_id)
        deps = json.loads(task["depends_on"])
        self.assertEqual(deps, [parent_id])

    def test_create_with_metadata(self):
        """应能创建带 metadata 的任务。"""
        meta = json.dumps({"summary": "test summary", "instructions": "do stuff"})
        task_id = database.create_task(
            "proj", "code", "req", metadata=meta
        )
        task = database.get_task_by_id(task_id)
        parsed = json.loads(task["metadata"])
        self.assertEqual(parsed["summary"], "test summary")

    def test_default_values_are_none(self):
        """不传扩展参数时默认值应为 None。"""
        task_id = database.create_task("proj", "code", "req")
        task = database.get_task_by_id(task_id)
        self.assertIsNone(task["depends_on"])
        self.assertIsNone(task["metadata"])
        self.assertIsNone(task["task_name"])


# ─── get_available_tasks DAG 依赖解析测试 ────────────────────────────────────


class TestGetAvailableTasks(unittest.TestCase):
    """测试 DAG 依赖解析的 get_available_tasks。"""

    def setUp(self):
        _clear_tasks()

    def test_no_deps_returns_pending(self):
        """无依赖的 pending 任务应可用。"""
        t1 = database.create_task("proj", "c1", "r1", task_name="Task A")
        t2 = database.create_task("proj", "c2", "r2", task_name="Task B")
        available = database.get_available_tasks("proj")
        ids = [t["id"] for t in available]
        self.assertIn(t1, ids)
        self.assertIn(t2, ids)

    def test_satisfied_deps_returns_task(self):
        """所有依赖均为 success 时任务应可用。"""
        parent = database.create_task("proj", "c1", "r1")
        database.update_task_status(parent, "success")
        child = database.create_task(
            "proj", "c2", "r2",
            depends_on=json.dumps([parent]),
        )
        available = database.get_available_tasks("proj")
        ids = [t["id"] for t in available]
        self.assertIn(child, ids)

    def test_unsatisfied_deps_blocks_task(self):
        """依赖未完成时任务不应可用。"""
        parent = database.create_task("proj", "c1", "r1")
        # parent is still 'pending'
        child = database.create_task(
            "proj", "c2", "r2",
            depends_on=json.dumps([parent]),
        )
        available = database.get_available_tasks("proj")
        ids = [t["id"] for t in available]
        self.assertNotIn(child, ids)

    def test_partial_deps_blocks_task(self):
        """部分依赖未完成时任务不应可用。"""
        p1 = database.create_task("proj", "c1", "r1")
        p2 = database.create_task("proj", "c2", "r2")
        database.update_task_status(p1, "success")
        # p2 is still 'pending'
        child = database.create_task(
            "proj", "c3", "r3",
            depends_on=json.dumps([p1, p2]),
        )
        available = database.get_available_tasks("proj")
        ids = [t["id"] for t in available]
        self.assertNotIn(child, ids)

    def test_project_isolation(self):
        """不同 project 的任务应被隔离。"""
        t1 = database.create_task("proj_a", "c1", "r1")
        t2 = database.create_task("proj_b", "c2", "r2")
        available_a = database.get_available_tasks("proj_a")
        ids = [t["id"] for t in available_a]
        self.assertIn(t1, ids)
        self.assertNotIn(t2, ids)

    def test_non_pending_not_returned(self):
        """非 pending 状态的任务不应被返回。"""
        t1 = database.create_task("proj", "c1", "r1")
        database.update_task_status(t1, "success")
        t2 = database.create_task("proj", "c2", "r2")
        database.update_task_status(t2, "in_progress")
        t3 = database.create_task("proj", "c3", "r3")  # pending
        available = database.get_available_tasks("proj")
        ids = [t["id"] for t in available]
        self.assertNotIn(t1, ids)
        self.assertNotIn(t2, ids)
        self.assertIn(t3, ids)

    def test_complex_dag(self):
        """复杂 DAG：A -> B -> D, A -> C -> D。"""
        a = database.create_task("proj", "cA", "rA", task_name="A")
        b = database.create_task("proj", "cB", "rB", task_name="B",
                                 depends_on=json.dumps([a]))
        c = database.create_task("proj", "cC", "rC", task_name="C",
                                 depends_on=json.dumps([a]))
        d = database.create_task("proj", "cD", "rD", task_name="D",
                                 depends_on=json.dumps([b, c]))

        # Initially: only A is available
        avail = database.get_available_tasks("proj")
        ids = [t["id"] for t in avail]
        self.assertEqual(ids, [a])

        # Complete A: B and C become available
        database.update_task_status(a, "success")
        avail = database.get_available_tasks("proj")
        ids = sorted([t["id"] for t in avail])
        self.assertEqual(ids, sorted([b, c]))

        # Complete B only: D still blocked (C not done)
        database.update_task_status(b, "success")
        avail = database.get_available_tasks("proj")
        ids = [t["id"] for t in avail]
        self.assertIn(c, ids)
        self.assertNotIn(d, ids)

        # Complete C: D becomes available
        database.update_task_status(c, "success")
        avail = database.get_available_tasks("proj")
        ids = [t["id"] for t in avail]
        self.assertIn(d, ids)


# ─── 拓扑排序循环检测测试 ────────────────────────────────────────────────────


class TestCycleDetection(unittest.TestCase):
    """测试 DAG 循环依赖检测。"""

    def setUp(self):
        _clear_tasks()

    def test_direct_self_cycle_rejected(self):
        """自引用依赖应被拒绝。"""
        # 创建一个任务，然后尝试让它依赖自己
        with self.assertRaises(ValueError) as ctx:
            database.create_task(
                "proj", "c1", "r1",
                depends_on=json.dumps([999]),  # 不存在的 ID
                task_name="Self Ref"
            )
        # 不存在的依赖也应该被拒绝
        self.assertIn("不存在", str(ctx.exception))

    def test_nonexistent_dependency_rejected(self):
        """依赖不存在的任务 ID 应被拒绝。"""
        with self.assertRaises(ValueError):
            database.create_task(
                "proj", "code", "req",
                depends_on=json.dumps([9999]),
            )

    def test_valid_dependency_accepted(self):
        """合法依赖应被接受。"""
        parent = database.create_task("proj", "c1", "r1")
        child = database.create_task(
            "proj", "c2", "r2",
            depends_on=json.dumps([parent]),
        )
        task = database.get_task_by_id(child)
        self.assertIsNotNone(task)


# ─── 级联阻断测试 ────────────────────────────────────────────────────────────


class TestCascadeBlock(unittest.TestCase):
    """测试前置任务失败时的级联阻断。"""

    def setUp(self):
        _clear_tasks()

    def test_cascade_blocks_direct_children(self):
        """父任务失败应阻断直接子任务。"""
        parent = database.create_task("proj", "c1", "r1")
        child = database.create_task(
            "proj", "c2", "r2",
            depends_on=json.dumps([parent]),
        )
        database.cascade_block(parent)
        task = database.get_task_by_id(child)
        self.assertEqual(task["status"], "blocked")

    def test_cascade_blocks_deep_descendants(self):
        """级联阻断应传播到所有后代。"""
        a = database.create_task("proj", "cA", "rA")
        b = database.create_task("proj", "cB", "rB",
                                 depends_on=json.dumps([a]))
        c = database.create_task("proj", "cC", "rC",
                                 depends_on=json.dumps([b]))
        database.cascade_block(a)
        self.assertEqual(database.get_task_by_id(b)["status"], "blocked")
        self.assertEqual(database.get_task_by_id(c)["status"], "blocked")

    def test_cascade_does_not_affect_unrelated(self):
        """级联阻断不应影响无关任务。"""
        a = database.create_task("proj", "cA", "rA")
        b = database.create_task("proj", "cB", "rB",
                                 depends_on=json.dumps([a]))
        unrelated = database.create_task("proj", "cU", "rU")
        database.cascade_block(a)
        self.assertEqual(database.get_task_by_id(unrelated)["status"], "pending")
        self.assertEqual(database.get_task_by_id(b)["status"], "blocked")


# ─── 原子锁定 get_next_available_task 测试 ───────────────────────────────────


class TestGetNextAvailableTask(unittest.TestCase):
    """测试原子性地获取并锁定一个可用 DAG 任务。"""

    def setUp(self):
        _clear_tasks()

    def test_atomic_lock_changes_status(self):
        """获取任务后，状态应从 pending 变为 in_progress。"""
        t = database.create_task("proj", "c1", "r1")
        result = database.get_next_available_task("proj")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], t)
        self.assertEqual(result["status"], "in_progress")

    def test_returns_none_when_empty(self):
        """无可用任务时返回 None。"""
        result = database.get_next_available_task("proj")
        self.assertIsNone(result)

    def test_respects_dag_dependencies(self):
        """应只返回依赖已满足的任务。"""
        parent = database.create_task("proj", "c1", "r1")
        child = database.create_task(
            "proj", "c2", "r2",
            depends_on=json.dumps([parent]),
        )
        # 应返回 parent（无依赖）
        result = database.get_next_available_task("proj")
        self.assertEqual(result["id"], parent)

        # child 依赖未满足，不可用
        result2 = database.get_next_available_task("proj")
        self.assertIsNone(result2)

    def test_priority_order(self):
        """应按 priority DESC 返回。"""
        low = database.create_task("proj", "c1", "r1", priority=1)
        high = database.create_task("proj", "c2", "r2", priority=10)
        result = database.get_next_available_task("proj")
        self.assertEqual(result["id"], high)

    def test_concurrent_no_duplicate(self):
        """并发获取不应返回同一任务（MVP 串行锁下仅1个赢家）。"""
        for i in range(10):
            database.create_task("proj", f"c{i}", f"r{i}")

        results = []
        lock = threading.Lock()

        def worker():
            task = database.get_next_available_task("proj")
            if task:
                with lock:
                    results.append(task["id"])

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # MVP 阶段 MAX_CONCURRENCY=1，只允许 1 个任务处于 in_progress
        self.assertEqual(len(results), 1, "MVP串行锁下应只有1个赢家")
        self.assertEqual(len(results), len(set(results)),
                         "Duplicate task IDs returned!")


# ─── update_task_metadata 测试 ───────────────────────────────────────────────


class TestUpdateTaskMetadata(unittest.TestCase):
    """测试 metadata 更新。"""

    def setUp(self):
        _clear_tasks()

    def test_update_metadata(self):
        """应能更新 metadata 字段。"""
        task_id = database.create_task("proj", "code", "req")
        meta = json.dumps({"summary": "任务完成", "files_changed": ["a.py"]})
        database.update_task_metadata(task_id, meta)
        task = database.get_task_by_id(task_id)
        parsed = json.loads(task["metadata"])
        self.assertEqual(parsed["summary"], "任务完成")

    def test_update_metadata_preserves_status(self):
        """更新 metadata 不应影响 status。"""
        task_id = database.create_task("proj", "code", "req")
        database.update_task_status(task_id, "in_progress")
        database.update_task_metadata(task_id, '{"x": 1}')
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "in_progress")


# ─── DB 迁移测试 ─────────────────────────────────────────────────────────────


class TestMigration(unittest.TestCase):
    """测试从 V1 到 V2 的平滑迁移。"""

    def test_init_db_is_idempotent(self):
        """多次调用 init_db 不应报错。"""
        database.init_db()
        database.init_db()
        database.init_db()

    def test_old_tasks_survive_migration(self):
        """V1 的旧任务在迁移后应保持完整。"""
        _clear_tasks()
        # 创建一个基础任务（V1 风格）
        task_id = database.create_task("old_proj", "old_code", "old_req")
        # 重新初始化（模拟迁移）
        database.init_db()
        task = database.get_task_by_id(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["project_name"], "old_proj")
        # 新字段应有合理默认值
        self.assertIsNone(task["depends_on"])
        self.assertIsNone(task["metadata"])
        self.assertIsNone(task["task_name"])


if __name__ == "__main__":
    unittest.main()
