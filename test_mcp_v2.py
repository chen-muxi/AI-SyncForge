"""
AI-SyncForge V2 MCP 工具集成测试
覆盖 get_next_task、mark_task_done、read_task_context、
I/O 冷却锁、QA 抗辩机制等 V2 新增工具。
遵循 TDD：本文件先于实现代码编写。
"""

import asyncio
import json
import os
import time
import unittest
from pathlib import Path
import tempfile

TEST_DB_PATH = Path(tempfile.gettempdir()) / "test_mcp_v2.db"

for suffix in ("", "-wal", "-shm"):
    p = TEST_DB_PATH.parent / (TEST_DB_PATH.name + suffix)
    if p.exists():
        try:
            os.remove(p)
        except OSError:
            pass

import database
import mcp_tools

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
    mcp_tools._task_events.clear()


# ─── get_next_task 测试 ──────────────────────────────────────────────────────


class TestGetNextTask(unittest.TestCase):
    """测试 get_next_task DAG 调度工具。"""

    def setUp(self):
        _clear_tasks()

    def test_returns_available_task(self):
        """有可用任务时应立即返回。"""

        async def scenario():
            database.create_task("proj", "c1", "r1", task_name="任务一")
            result = await mcp_tools.get_next_task("proj", timeout=5)
            self.assertIsNotNone(result["task_id"])
            self.assertEqual(result["task_name"], "任务一")
            self.assertEqual(result["status"], "in_progress")

        asyncio.run(scenario())

    def test_returns_none_when_no_task(self):
        """无可用任务时应在超时后返回。"""

        async def scenario():
            result = await mcp_tools.get_next_task("proj", timeout=2)
            self.assertIsNone(result["task_id"])
            self.assertIn("无可用", result["message"])

        asyncio.run(scenario())

    def test_dag_dependency_respected(self):
        """应遵循 DAG 依赖。"""

        async def scenario():
            parent = database.create_task("proj", "c1", "r1", task_name="Parent")
            child = database.create_task(
                "proj", "c2", "r2", task_name="Child",
                depends_on=json.dumps([parent]),
            )
            # 应返回 parent
            result = await mcp_tools.get_next_task("proj", timeout=3)
            self.assertEqual(result["task_id"], parent)
            self.assertEqual(result["task_name"], "Parent")

        asyncio.run(scenario())

    def test_includes_predecessor_summaries(self):
        """应包含直接前置节点的摘要。"""

        async def scenario():
            parent = database.create_task("proj", "c1", "r1", task_name="Parent")
            meta = json.dumps({"summary": "完成了数据库初始化"})
            database.update_task_metadata(parent, meta)
            database.update_task_status(parent, "success")

            child = database.create_task(
                "proj", "c2", "r2", task_name="Child",
                depends_on=json.dumps([parent]),
            )

            result = await mcp_tools.get_next_task("proj", timeout=3)
            self.assertEqual(result["task_id"], child)
            self.assertIsNotNone(result.get("predecessor_summaries"))
            self.assertEqual(len(result["predecessor_summaries"]), 1)
            self.assertEqual(
                result["predecessor_summaries"][0]["summary"],
                "完成了数据库初始化",
            )

        asyncio.run(scenario())

    def test_project_isolation(self):
        """不同项目的任务应隔离。"""

        async def scenario():
            database.create_task("proj_a", "c1", "r1")
            database.create_task("proj_b", "c2", "r2")
            result = await mcp_tools.get_next_task("proj_a", timeout=3)
            self.assertIsNotNone(result["task_id"])

            # proj_a 的任务已被锁定，再取应无任务（MAX_CONCURRENCY=1）
            result2 = await mcp_tools.get_next_task("proj_a", timeout=2)
            self.assertIsNone(result2["task_id"])

        asyncio.run(scenario())

    def test_long_poll_waits_for_task(self):
        """长轮询应在任务到来时立即返回。"""

        async def scenario():
            async def delayed_create():
                await asyncio.sleep(1.5)
                database.create_task("proj", "c1", "r1", task_name="Delayed")

            asyncio.create_task(delayed_create())
            t0 = time.perf_counter()
            result = await mcp_tools.get_next_task("proj", timeout=10)
            elapsed = time.perf_counter() - t0

            self.assertIsNotNone(result["task_id"])
            self.assertLess(elapsed, 5, "Should not wait full timeout")

        asyncio.run(scenario())


# ─── mark_task_done 测试 ─────────────────────────────────────────────────────


class TestMarkTaskDone(unittest.TestCase):
    """测试 mark_task_done DAG 任务完成工具。"""

    def setUp(self):
        _clear_tasks()

    def test_marks_success(self):
        """应能标记任务为 success。"""

        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            database.update_task_status(task_id, "in_progress")
            result = await mcp_tools.mark_task_done(
                task_id, "success", "完成了核心功能"
            )
            self.assertTrue(result["success"])
            task = database.get_task_by_id(task_id)
            self.assertEqual(task["status"], "success")

        asyncio.run(scenario())

    def test_writes_summary_to_metadata(self):
        """应将 summary 写入 metadata。"""

        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            database.update_task_status(task_id, "in_progress")
            await mcp_tools.mark_task_done(task_id, "success", "实现了登录模块")

            task = database.get_task_by_id(task_id)
            meta = json.loads(task["metadata"])
            self.assertEqual(meta["summary"], "实现了登录模块")

        asyncio.run(scenario())

    def test_unlocks_downstream_on_success(self):
        """成功完成后应使下游任务变为可用。"""

        async def scenario():
            parent = database.create_task("proj", "c1", "r1")
            child = database.create_task(
                "proj", "c2", "r2",
                depends_on=json.dumps([parent]),
            )
            database.update_task_status(parent, "in_progress")

            # 完成 parent
            await mcp_tools.mark_task_done(parent, "success", "Parent done")

            # child 应变为可用
            available = database.get_available_tasks("proj")
            ids = [t["id"] for t in available]
            self.assertIn(child, ids)

        asyncio.run(scenario())

    def test_cascade_blocks_on_fail(self):
        """失败后应触发级联阻断。"""

        async def scenario():
            parent = database.create_task("proj", "c1", "r1")
            child = database.create_task(
                "proj", "c2", "r2",
                depends_on=json.dumps([parent]),
            )
            database.update_task_status(parent, "in_progress")

            # 失败
            result = await mcp_tools.mark_task_done(parent, "fail", "编译错误")

            self.assertTrue(result["success"])
            self.assertIn("cascade_blocked", result)
            task = database.get_task_by_id(child)
            self.assertEqual(task["status"], "blocked")

        asyncio.run(scenario())

    def test_io_settle_delay(self):
        """I/O 冷却期：mark_task_done 后应有延迟再解锁下游。"""

        async def scenario():
            parent = database.create_task("proj", "c1", "r1")
            database.update_task_status(parent, "in_progress")

            t0 = time.perf_counter()
            await mcp_tools.mark_task_done(parent, "success", "Done")
            elapsed = time.perf_counter() - t0

            # 应至少等待 IO_SETTLE_DELAY_MS（默认 1000ms）
            self.assertGreaterEqual(elapsed, 0.5, "I/O 冷却期应生效")

        asyncio.run(scenario())

    def test_qa_rejection_status(self):
        """QA 抗辩：fail 2 次后 Dev 可提交 qa_rejection。"""

        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            database.update_task_status(task_id, "in_progress")

            # 模拟 2 次失败
            database.update_task_status(task_id, "fail")
            database.update_task_status(task_id, "fail")

            # 第 3 次 Dev 提交 qa_rejection
            result = await mcp_tools.mark_task_done(
                task_id, "qa_rejection", "QA 测试脚本有误"
            )
            self.assertTrue(result["success"])
            self.assertIn("ops_task_created", result)

        asyncio.run(scenario())

    def test_qa_rejection_requires_retry_count(self):
        """qa_rejection 应在 retry_count >= 2 时才允许。"""

        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            database.update_task_status(task_id, "in_progress")

            # retry_count=0，直接 qa_rejection 应被拒绝
            result = await mcp_tools.mark_task_done(
                task_id, "qa_rejection", "想直接跳过"
            )
            self.assertFalse(result["success"])
            self.assertIn("retry_count", result["message"])

        asyncio.run(scenario())

    def test_invalid_status_rejected(self):
        """无效状态应被拒绝。"""

        async def scenario():
            result = await mcp_tools.mark_task_done(1, "magic", "what")
            self.assertFalse(result["success"])

        asyncio.run(scenario())


# ─── read_task_context 测试 ──────────────────────────────────────────────────


class TestReadTaskContext(unittest.TestCase):
    """测试 read_task_context 上下文读取工具。"""

    def setUp(self):
        _clear_tasks()

    def test_returns_task_details(self):
        """应返回任务的完整上下文。"""

        async def scenario():
            task_id = database.create_task(
                "proj", "print(1)", "测试输出",
                task_name="测试任务",
                metadata=json.dumps({"summary": "init"}),
            )
            result = await mcp_tools.read_task_context(task_id)
            self.assertTrue(result["success"])
            self.assertEqual(result["task_id"], task_id)
            self.assertEqual(result["task_name"], "测试任务")
            self.assertIn("predecessor_summaries", result)

        asyncio.run(scenario())

    def test_nonexistent_task_returns_error(self):
        """查询不存在的任务应返回错误。"""

        async def scenario():
            result = await mcp_tools.read_task_context(99999)
            self.assertFalse(result["success"])

        asyncio.run(scenario())

    def test_large_metadata_safety_valve(self):
        """超过 50KB 的 metadata 应触发文件降级。"""

        async def scenario():
            large_meta = json.dumps({"log": "x" * 60000})  # >50KB
            task_id = database.create_task(
                "proj", "code", "req",
                metadata=large_meta,
            )
            result = await mcp_tools.read_task_context(task_id)
            self.assertTrue(result["success"])
            # 大 metadata 应被替换为文件路径
            if len(large_meta) > 50 * 1024:
                self.assertIn("ctx_file", result)

        asyncio.run(scenario())

    def test_includes_predecessor_summaries(self):
        """应包含前置节点的摘要。"""

        async def scenario():
            parent = database.create_task(
                "proj", "c1", "r1", task_name="Parent",
                metadata=json.dumps({"summary": "做完了A"}),
            )
            database.update_task_status(parent, "success")
            child = database.create_task(
                "proj", "c2", "r2", task_name="Child",
                depends_on=json.dumps([parent]),
            )
            result = await mcp_tools.read_task_context(child)
            self.assertTrue(result["success"])
            self.assertEqual(len(result["predecessor_summaries"]), 1)

        asyncio.run(scenario())


# ─── inspect_project_tree 测试 ───────────────────────────────────────────────


class TestInspectProjectTree(unittest.TestCase):
    """测试 DAG 状态树可视化。"""

    def setUp(self):
        _clear_tasks()

    def test_returns_project_dag(self):
        """应返回项目的 DAG 状态树。"""

        async def scenario():
            a = database.create_task("proj", "cA", "rA", task_name="A")
            b = database.create_task("proj", "cB", "rB", task_name="B",
                                     depends_on=json.dumps([a]))
            result = await mcp_tools.inspect_project_tree("proj")
            self.assertTrue(result["success"])
            self.assertGreaterEqual(len(result["tasks"]), 2)

        asyncio.run(scenario())

    def test_empty_project(self):
        """空项目应返回空列表。"""

        async def scenario():
            result = await mcp_tools.inspect_project_tree("empty")
            self.assertTrue(result["success"])
            self.assertEqual(len(result["tasks"]), 0)

        asyncio.run(scenario())


# ─── 自动熔断测试 ────────────────────────────────────────────────────────────


class TestAutoEscalation(unittest.TestCase):
    """测试 3 次重试自动熔断机制。"""

    def setUp(self):
        _clear_tasks()

    def test_escalation_after_three_retries(self):
        """任务重试达 3 次后应自动升级为 ops_task。"""

        async def scenario():
            task_id = database.create_task("proj", "buggy", "req")
            database.update_task_status(task_id, "in_progress")

            # 3 次失败
            for i in range(3):
                await mcp_tools.mark_task_done(task_id, "fail", f"错误{i+1}")
                # 重置为 in_progress（模拟 Dev 重新接取）
                if i < 2:
                    database.update_task_status(task_id, "in_progress")

            # 第 3 次失败后应生成 ops_task
            task = database.get_task_by_id(task_id)
            self.assertEqual(task["retry_count"], 3)

            # 检查是否有 ops_task 生成
            conn = database._connect()
            try:
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE task_type = 'ops_task' "
                    "AND project_name LIKE ?;",
                    (f"escalation_{task_id}%",),
                )
                ops_tasks = cursor.fetchall()
                self.assertGreaterEqual(len(ops_tasks), 1)
            finally:
                conn.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
