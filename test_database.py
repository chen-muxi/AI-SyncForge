"""
AI-SyncForge 模块一单元测试
覆盖任务 CRUD 操作与状态机校验。
"""

import os
import sqlite3
import threading
import unittest
from pathlib import Path

TEST_DB_PATH = Path("/tmp/test_task_queue.db")

# 在导入 database 模块之前清理残留文件
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


class TestDatabaseInit(unittest.TestCase):
    """测试数据库初始化。"""

    def test_db_file_created(self):
        """数据库文件应被成功创建。"""
        self.assertTrue(TEST_DB_PATH.exists())

    def test_wal_mode_enabled(self):
        """应启用 WAL 日志模式。"""
        conn = database._connect()
        try:
            result = conn.execute("PRAGMA journal_mode;").fetchone()
            self.assertEqual(result[0], "wal")
        finally:
            conn.close()

    def test_table_exists(self):
        """tasks 表应存在。"""
        conn = database._connect()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks';"
            )
            self.assertIsNotNone(cursor.fetchone())
        finally:
            conn.close()

    def test_trigger_exists(self):
        """updated_at 触发器应存在。"""
        conn = database._connect()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_tasks_updated_at';"
            )
            self.assertIsNotNone(cursor.fetchone())
        finally:
            conn.close()

    def test_index_exists(self):
        """status 索引应存在。"""
        conn = database._connect()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_tasks_status';"
            )
            self.assertIsNotNone(cursor.fetchone())
        finally:
            conn.close()

    def test_idempotent_init(self):
        """重复调用 init_db 不应报错。"""
        database.init_db()
        database.init_db()


class TestCreateTask(unittest.TestCase):
    """测试任务创建。"""

    def setUp(self):
        _clear_tasks()

    def test_create_returns_id(self):
        """创建任务应返回有效的 task_id。"""
        task_id = database.create_task("proj_a", "print('hello')", "测试输出")
        self.assertIsInstance(task_id, int)
        self.assertGreater(task_id, 0)

    def test_default_status_is_pending(self):
        """新任务默认状态应为 pending。"""
        task_id = database.create_task("proj_b", "x = 1", "验证赋值")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "pending")

    def test_default_retry_count_is_zero(self):
        """新任务默认 retry_count 为 0。"""
        task_id = database.create_task("proj_c", "y = 2", "验证计数")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["retry_count"], 0)

    def test_fields_stored_correctly(self):
        """所有字段应正确存储。"""
        task_id = database.create_task("my_proj", "code_here", "test_req")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["project_name"], "my_proj")
        self.assertEqual(task["code_content"], "code_here")
        self.assertEqual(task["test_requirement"], "test_req")


class TestGetPendingTask(unittest.TestCase):
    """测试原子性任务获取与锁定。"""

    def setUp(self):
        _clear_tasks()

    def test_get_pending_locks_status(self):
        """获取任务后，状态应自动变为 testing。"""
        task_id = database.create_task("proj_d", "code_d", "req_d")
        fetched = database.get_pending_task()
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], task_id)
        self.assertEqual(fetched["status"], "testing")

        refreshed = database.get_task_by_id(task_id)
        self.assertEqual(refreshed["status"], "testing")

    def test_no_pending_returns_none(self):
        """无待处理任务时应返回 None。"""
        result = database.get_pending_task()
        self.assertIsNone(result)

    def test_fifo_order(self):
        """应按 FIFO 顺序提取任务。"""
        id1 = database.create_task("proj_e", "code_e1", "req_e1")
        id2 = database.create_task("proj_f", "code_e2", "req_e2")

        first = database.get_pending_task()
        self.assertEqual(first["id"], id1)

        second = database.get_pending_task()
        self.assertEqual(second["id"], id2)

    def test_already_testing_not_returned(self):
        """已在 testing 状态的任务不应被重复提取。"""
        task_id = database.create_task("proj_x", "code_x", "req_x")
        database.get_pending_task()  # 变为 testing
        result = database.get_pending_task()
        self.assertIsNone(result)


class TestUpdateTaskStatus(unittest.TestCase):
    """测试状态更新逻辑。"""

    def setUp(self):
        _clear_tasks()

    def test_transition_testing_to_success(self):
        """testing -> success 转换应正常工作。"""
        task_id = database.create_task("proj_g", "code_g", "req_g")
        database.get_pending_task()
        database.update_task_status(task_id, "success", "/reports/g.json")

        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "success")
        self.assertEqual(task["report_path"], "/reports/g.json")
        self.assertEqual(task["retry_count"], 0)

    def test_transition_testing_to_fail_increments_retry(self):
        """testing -> fail 应将 retry_count 加 1。"""
        task_id = database.create_task("proj_h", "code_h", "req_h")
        database.get_pending_task()
        database.update_task_status(task_id, "fail", "/reports/h_err.log")

        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "fail")
        self.assertEqual(task["retry_count"], 1)

    def test_transition_to_ops_intervention_increments_retry(self):
        """转为 fail_by_ops_intervention 应将 retry_count 加 1。"""
        task_id = database.create_task("proj_i", "code_i", "req_i")
        database.get_pending_task()
        database.update_task_status(task_id, "fail_by_ops_intervention", "/logs/ops.log")

        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "fail_by_ops_intervention")
        self.assertEqual(task["retry_count"], 1)

    def test_multiple_failures_accumulate_retry(self):
        """多次失败应累加 retry_count。"""
        task_id = database.create_task("proj_retry", "code", "req")
        database.get_pending_task()
        database.update_task_status(task_id, "fail", "/log1")
        database.update_task_status(task_id, "fail", "/log2")

        task = database.get_task_by_id(task_id)
        self.assertEqual(task["retry_count"], 2)

    def test_updated_at_changes_on_update(self):
        """更新状态后 updated_at 应被触发器刷新。"""
        task_id = database.create_task("proj_j", "code_j", "req_j")
        task_before = database.get_task_by_id(task_id)

        import time
        time.sleep(1.1)

        database.get_pending_task()
        task_after = database.get_task_by_id(task_id)

        self.assertNotEqual(task_before["updated_at"], task_after["updated_at"])


class TestConcurrency(unittest.TestCase):
    """测试并发场景下的线程安全。"""

    def setUp(self):
        _clear_tasks()

    def test_concurrent_get_pending_no_duplicate(self):
        """并发调用 get_pending_task 不应返回同一任务。"""
        num_tasks = 20
        for i in range(num_tasks):
            database.create_task(f"conc_{i}", f"code_{i}", f"req_{i}")

        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                task = database.get_pending_task()
                if task:
                    with lock:
                        results.append(task["id"])
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(num_tasks)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), len(set(results)),
                         "Duplicate task IDs were returned!")
        self.assertEqual(len(results), num_tasks)

    def test_concurrent_get_pending_single_task(self):
        """20 个线程抢夺同一个任务，必须且只能有 1 个成功。"""
        database.create_task("single_proj", "code", "req")

        results = []
        lock = threading.Lock()

        def worker():
            task = database.get_pending_task()
            if task:
                with lock:
                    results.append(task["id"])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, "Should only have ONE winner!")


class TestSchemaExtension(unittest.TestCase):
    """测试 Schema 扩展：task_type 与 priority 字段。"""

    def setUp(self):
        _clear_tasks()

    def test_task_type_and_priority_columns_exist(self):
        """通过 PRAGMA table_info 验证新字段存在。"""
        conn = database._connect()
        try:
            rows = conn.execute("PRAGMA table_info(tasks);").fetchall()
            columns = {row[1] for row in rows}
            self.assertIn("task_type", columns)
            self.assertIn("priority", columns)
        finally:
            conn.close()

    def test_default_task_type_is_dev_test(self):
        """默认 task_type 应为 dev_test。"""
        task_id = database.create_task("proj", "code", "req")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["task_type"], "dev_test")

    def test_default_priority_is_zero(self):
        """默认 priority 应为 0。"""
        task_id = database.create_task("proj", "code", "req")
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["priority"], 0)

    def test_create_ops_task(self):
        """应能创建 ops_task 类型的任务。"""
        task_id = database.create_task("ops_proj", "fix", "紧急修复",
                                       task_type="ops_task", priority=10)
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["task_type"], "ops_task")
        self.assertEqual(task["priority"], 10)

    def test_get_pending_task_only_returns_dev_test(self):
        """get_pending_task 仅返回 dev_test 类型任务。"""
        ops_id = database.create_task("ops", "ops_code", "ops_req",
                                      task_type="ops_task", priority=99)
        dev_id = database.create_task("dev", "dev_code", "dev_req",
                                      task_type="dev_test")

        result = database.get_pending_task()
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], dev_id)
        self.assertEqual(result["task_type"], "dev_test")

        # ops_task 应仍处于 pending
        ops_task = database.get_task_by_id(ops_id)
        self.assertEqual(ops_task["status"], "pending")

    def test_get_pending_task_priority_order(self):
        """get_pending_task 应按 priority DESC 排序。"""
        low_id = database.create_task("low", "c1", "r1", priority=1)
        high_id = database.create_task("high", "c2", "r2", priority=5)

        first = database.get_pending_task()
        self.assertEqual(first["id"], high_id)

        second = database.get_pending_task()
        self.assertEqual(second["id"], low_id)


class TestPollOpsTask(unittest.TestCase):
    """测试 poll_ops_task 原子锁定。"""

    def setUp(self):
        _clear_tasks()

    def test_poll_ops_task_only_returns_ops_task(self):
        """poll_ops_task 仅返回 ops_task 类型任务。"""
        database.create_task("dev_proj", "dev_code", "dev_req",
                            task_type="dev_test")
        ops_id = database.create_task("ops_proj", "fix_code", "fix_req",
                                      task_type="ops_task", priority=5)

        result = database.poll_ops_task()
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], ops_id)
        self.assertEqual(result["task_type"], "ops_task")
        self.assertEqual(result["status"], "testing")

    def test_poll_ops_task_returns_none_when_empty(self):
        """无 ops_task 时应返回 None。"""
        database.create_task("dev_proj", "code", "req", task_type="dev_test")
        result = database.poll_ops_task()
        self.assertIsNone(result)

    def test_poll_ops_task_priority_order(self):
        """poll_ops_task 应按 priority DESC 排序。"""
        low_id = database.create_task("ops_low", "c1", "r1",
                                      task_type="ops_task", priority=1)
        high_id = database.create_task("ops_high", "c2", "r2",
                                       task_type="ops_task", priority=10)

        first = database.poll_ops_task()
        self.assertEqual(first["id"], high_id)

        second = database.poll_ops_task()
        self.assertEqual(second["id"], low_id)

    def test_poll_ops_task_locks_status(self):
        """获取后状态应变为 testing。"""
        task_id = database.create_task("ops", "code", "req",
                                       task_type="ops_task")
        database.poll_ops_task()
        task = database.get_task_by_id(task_id)
        self.assertEqual(task["status"], "testing")

    def test_concurrent_poll_ops_single_task(self):
        """20 个线程抢夺同一个 Ops 任务，必须且只能有 1 个成功。"""
        database.create_task("ops_single", "fix", "req", task_type="ops_task")

        results = []
        lock = threading.Lock()

        def worker():
            task = database.poll_ops_task()
            if task:
                with lock:
                    results.append(task["id"])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, "Should only have ONE Ops winner!")


class TestStatusConstraint(unittest.TestCase):
    """测试状态字段的 CHECK 约束。"""

    def setUp(self):
        _clear_tasks()

    def test_invalid_status_rejected(self):
        """无效状态值应被数据库拒绝。"""
        task_id = database.create_task("proj_z", "code_z", "req_z")
        with self.assertRaises(sqlite3.IntegrityError):
            database.update_task_status(task_id, "invalid_status")


if __name__ == "__main__":
    unittest.main()
