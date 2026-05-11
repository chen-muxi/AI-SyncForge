"""
AI-SyncForge 模块三集成测试
验证 Ops 应急响应机制：吹哨-接单-救援-唤醒 全链路闭环。
"""

import asyncio
import os
import unittest
from pathlib import Path

TEST_DB_PATH = Path("/tmp/test_ops.db")

for suffix in ("", "-wal", "-shm"):
    p = TEST_DB_PATH.parent / (TEST_DB_PATH.name + suffix)
    if p.exists():
        try:
            os.remove(p)
        except OSError:
            pass

import database
import mcp_tools
import ops_manager

database.DB_PATH = TEST_DB_PATH
database.init_db()


def _clear_tasks():
    conn = database._connect()
    try:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks';")
        conn.commit()
    finally:
        conn.close()


def _force_stale(task_id: int, minutes_ago: int = 11):
    """强制将任务的 updated_at 回拨，模拟超时。需临时移除触发器防止覆盖。"""
    conn = database._connect()
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_tasks_updated_at;")
        conn.execute(
            "UPDATE tasks SET updated_at = datetime('now', ?) WHERE id = ?;",
            (f"-{minutes_ago} minutes", task_id),
        )
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
    finally:
        conn.close()


class TestOpsWatchdog(unittest.TestCase):
    """测试吹哨人逻辑。"""

    def setUp(self):
        _clear_tasks()

    def test_detects_stale_task(self):
        """应检测到超时 10 分钟的 testing 任务。"""
        task_id = database.create_task("proj", "code", "req")
        database.get_pending_task()  # pending -> testing
        _force_stale(task_id, minutes_ago=11)

        stale = ops_manager.get_stale_testing_tasks()
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], task_id)

    def test_ignores_recent_testing_task(self):
        """未超时的 testing 任务不应被检测到。"""
        task_id = database.create_task("proj", "code", "req")
        database.get_pending_task()

        stale = ops_manager.get_stale_testing_tasks()
        self.assertEqual(len(stale), 0)

    def test_ignores_ops_task_type(self):
        """ops_task 类型不参与超时判定。"""
        task_id = database.create_task("ops", "code", "req",
                                       task_type="ops_task")
        # 手动设为 testing
        database.update_task_status(task_id, "testing")
        _force_stale(task_id, minutes_ago=15)

        stale = ops_manager.get_stale_testing_tasks()
        self.assertEqual(len(stale), 0)

    def test_creates_rescue_task(self):
        """应能为超时任务创建急救工单。"""
        task_id = database.create_task("proj", "code", "req")
        database.get_pending_task()
        _force_stale(task_id, minutes_ago=11)

        stale = ops_manager.get_stale_testing_tasks()
        rescue_id = ops_manager.create_ops_rescue_task(stale[0])

        rescue = database.get_task_by_id(rescue_id)
        self.assertEqual(rescue["task_type"], "ops_task")
        self.assertEqual(rescue["priority"], 999)
        self.assertIn(str(task_id), rescue["code_content"])

    def test_no_duplicate_rescue(self):
        """不应为同一任务重复生成急救工单。"""
        task_id = database.create_task("proj", "code", "req")
        database.get_pending_task()
        _force_stale(task_id, minutes_ago=11)

        stale = ops_manager.get_stale_testing_tasks()
        ops_manager.create_ops_rescue_task(stale[0])

        # 第二次应检测到已有 rescue
        has_rescue = ops_manager._has_pending_rescue(task_id)
        self.assertTrue(has_rescue)


class TestFullRescueFlow(unittest.TestCase):
    """全自动自愈测试：吹哨 -> 接单 -> 救援 -> 唤醒。"""

    def setUp(self):
        _clear_tasks()
        mcp_tools._task_events.clear()

    def test_whistle_to_wake_full_cycle(self):
        """完整自愈闭环：Dev 提交 -> 卡死 -> 吹哨 -> Ops 救援 -> Dev 唤醒。"""

        async def scenario():
            # 缩短扫描间隔用于测试
            original_interval = ops_manager.SCAN_INTERVAL
            original_threshold = ops_manager.WHISTLEBLOWER_TIMEOUT_SECONDS
            ops_manager.SCAN_INTERVAL = 1
            ops_manager.WHISTLEBLOWER_TIMEOUT_SECONDS = 0  # 立即判定超时

            try:
                # Dev 提交任务
                dev_task = asyncio.create_task(
                    mcp_tools.submit_and_wait("stuck_proj", "infinite_loop", "会卡死")
                )

                await asyncio.sleep(0.3)

                # QA 获取任务（模拟 QA 拿走后卡死）
                qa_result = await mcp_tools.poll_task(timeout=5)
                self.assertIsNotNone(qa_result["task_id"])
                stuck_task_id = qa_result["task_id"]

                # 模拟卡死：回拨时间使其超时
                _force_stale(stuck_task_id, minutes_ago=11)

                # 启动吹哨人（短暂运行）
                watchdog = asyncio.create_task(ops_manager.ops_watchdog())
                await asyncio.sleep(2)  # 等待一轮扫描

                # Ops-Forge 获取急救任务
                ops_result = await mcp_tools.poll_ops_task(timeout=5)
                self.assertIsNotNone(ops_result["task_id"])
                self.assertIn(str(stuck_task_id), ops_result["code_content"])

                # Ops 执行环境清理并反馈
                manage_result = await mcp_tools.manage_env(
                    "cleanup", "test_container", stuck_task_id
                )
                self.assertTrue(manage_result["success"])

                # Dev 应被唤醒
                result = await asyncio.wait_for(dev_task, timeout=3)
                self.assertEqual(result["status"], "fail_by_ops_intervention")

                # 清理 watchdog
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass

            finally:
                ops_manager.SCAN_INTERVAL = original_interval
                ops_manager.WHISTLEBLOWER_TIMEOUT_SECONDS = original_threshold

        asyncio.run(scenario())


class TestConcurrentIsolation(unittest.TestCase):
    """并发冲突测试：QA 与 Ops 各司其职，互不干扰。"""

    def setUp(self):
        _clear_tasks()
        mcp_tools._task_events.clear()

    def test_qa_and_ops_isolation(self):
        """dev_test 和 ops_task 应被各自的通道独立获取。"""

        async def scenario():
            # 创建混合任务
            dev_id1 = database.create_task("dev1", "code1", "req1",
                                           task_type="dev_test")
            dev_id2 = database.create_task("dev2", "code2", "req2",
                                           task_type="dev_test")
            ops_id1 = database.create_task("ops1", "fix1", "ops_req1",
                                           task_type="ops_task", priority=999)
            ops_id2 = database.create_task("ops2", "fix2", "ops_req2",
                                           task_type="ops_task", priority=500)

            # QA 拉取 - 应只获得 dev_test
            qa1 = await mcp_tools.poll_task(timeout=3)
            qa2 = await mcp_tools.poll_task(timeout=3)
            qa3 = await mcp_tools.poll_task(timeout=3)  # 应超时

            self.assertEqual(qa1["task_id"], dev_id1)
            self.assertEqual(qa2["task_id"], dev_id2)
            self.assertIsNone(qa3["task_id"])

            # Ops 拉取 - 应只获得 ops_task（按优先级）
            ops1 = await mcp_tools.poll_ops_task(timeout=3)
            ops2 = await mcp_tools.poll_ops_task(timeout=3)
            ops3 = await mcp_tools.poll_ops_task(timeout=3)  # 应超时

            self.assertEqual(ops1["task_id"], ops_id1)  # priority 999 先
            self.assertEqual(ops2["task_id"], ops_id2)
            self.assertIsNone(ops3["task_id"])

        asyncio.run(scenario())

    def test_multiple_dev_and_ops_concurrent(self):
        """多个 Dev 和 Ops 任务并发处理不冲突。"""

        async def scenario():
            # 创建 5 个 dev 任务和 3 个 ops 任务
            dev_ids = [
                database.create_task(f"dev_{i}", f"code_{i}", f"req_{i}")
                for i in range(5)
            ]
            ops_ids = [
                database.create_task(f"ops_{i}", f"fix_{i}", f"ops_req_{i}",
                                     task_type="ops_task", priority=100 + i)
                for i in range(3)
            ]

            qa_results = []
            ops_results = []

            async def qa_worker():
                for _ in range(5):
                    r = await mcp_tools.poll_task(timeout=5)
                    if r["task_id"]:
                        qa_results.append(r["task_id"])

            async def ops_worker():
                for _ in range(3):
                    r = await mcp_tools.poll_ops_task(timeout=5)
                    if r["task_id"]:
                        ops_results.append(r["task_id"])

            await asyncio.gather(qa_worker(), ops_worker())

            # QA 获取了所有 dev 任务
            self.assertEqual(sorted(qa_results), sorted(dev_ids))
            # Ops 获取了所有 ops 任务
            self.assertEqual(sorted(ops_results), sorted(ops_ids))

        asyncio.run(scenario())


class TestManageEnv(unittest.TestCase):
    """测试 manage_env 工具。"""

    def setUp(self):
        _clear_tasks()
        mcp_tools._task_events.clear()

    def test_broker_protection(self):
        """严禁对 Broker 容器执行操作。"""

        async def scenario():
            result = await mcp_tools.manage_env("restart", "broker_container")
            self.assertFalse(result["success"])
            self.assertIn("严禁", result["message"])

        asyncio.run(scenario())

    def test_invalid_action_rejected(self):
        """无效操作类型应被拒绝。"""

        async def scenario():
            result = await mcp_tools.manage_env("destroy", "some_container")
            self.assertFalse(result["success"])

        asyncio.run(scenario())

    def test_cleanup_with_task_notification(self):
        """cleanup 操作应更新关联任务并触发事件通知。"""

        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            database.get_pending_task()

            event = asyncio.Event()
            mcp_tools._task_events[task_id] = event

            result = await mcp_tools.manage_env("cleanup", "test_container", task_id)
            self.assertTrue(result["success"])

            # 事件应已被触发
            self.assertTrue(event.is_set())

            # 任务状态应为 ops_intervention
            task = database.get_task_by_id(task_id)
            self.assertEqual(task["status"], "fail_by_ops_intervention")

            mcp_tools._task_events.pop(task_id, None)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
