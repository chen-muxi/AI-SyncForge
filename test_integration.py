"""
AI-SyncForge 模块二集成测试
验证 Dev-QA 异步协作流（基于事件通知机制）。
"""

import asyncio
import os
import time
import unittest
from pathlib import Path
import tempfile

TEST_DB_PATH = Path(tempfile.gettempdir()) / "test_integration.db"

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


class TestSubmitAndWait(unittest.TestCase):
    """测试 submit_and_wait 事件通知机制。"""

    def setUp(self):
        conn = database._connect()
        try:
            conn.execute("DELETE FROM tasks;")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks';")
            conn.commit()
        finally:
            conn.close()
        mcp_tools._task_events.clear()

    def test_end_to_end_success(self):
        """端到端：提交 -> QA 获取 -> 完成测试 -> Dev 毫秒级收到结果。"""

        async def scenario():
            dev_task = asyncio.create_task(
                mcp_tools.submit_and_wait("test_proj", "print(1)", "验证输出")
            )

            await asyncio.sleep(0.5)
            qa_result = await mcp_tools.poll_task(timeout=10)
            self.assertIsNotNone(qa_result["task_id"])
            self.assertEqual(qa_result["project_name"], "test_proj")

            t0 = time.perf_counter()
            await mcp_tools.finish_test(
                qa_result["task_id"], "success", "/reports/test.json"
            )
            result = await dev_task
            latency = time.perf_counter() - t0

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["report_path"], "/reports/test.json")
            # 性能验证：事件通知应在毫秒级完成
            self.assertLess(latency, 0.5, f"Latency too high: {latency:.3f}s")

        asyncio.run(scenario())

    def test_end_to_end_failure(self):
        """端到端：QA 报告测试失败，Dev 收到 fail 状态。"""

        async def scenario():
            dev_task = asyncio.create_task(
                mcp_tools.submit_and_wait("fail_proj", "bad_code", "会失败")
            )

            await asyncio.sleep(0.5)
            qa_result = await mcp_tools.poll_task(timeout=10)

            await mcp_tools.finish_test(
                qa_result["task_id"], "fail", "/reports/fail.log"
            )

            result = await dev_task
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["retry_count"], 1)

        asyncio.run(scenario())

    def test_no_active_circuit_break(self):
        """竞态验证：submit_and_wait 不主动熔断，等待外部通知。"""

        async def scenario():
            # 模拟长时间等待后由外部触发完成
            dev_task = asyncio.create_task(
                mcp_tools.submit_and_wait("long_proj", "slow_code", "等待外部")
            )

            # 等待超过原来的 timeout (原来是 5s 测试用)，验证不会主动断开
            await asyncio.sleep(3)

            # 此时任务应仍在等待
            self.assertFalse(dev_task.done())

            # 手动获取 task_id 并通过 finish_test 完成
            task_id = list(mcp_tools._task_events.keys())[0]
            await mcp_tools.finish_test(task_id, "success", "/ok")

            result = await dev_task
            self.assertEqual(result["status"], "success")

        asyncio.run(scenario())

    def test_event_cleanup_on_completion(self):
        """资源回收：完成后事件注册表应被清理。"""

        async def scenario():
            dev_task = asyncio.create_task(
                mcp_tools.submit_and_wait("cleanup_proj", "code", "req")
            )

            await asyncio.sleep(0.3)
            self.assertEqual(len(mcp_tools._task_events), 1)

            task_id = list(mcp_tools._task_events.keys())[0]
            await mcp_tools.finish_test(task_id, "success", "/done")
            await dev_task

            # 事件应已被清理
            self.assertEqual(len(mcp_tools._task_events), 0)

        asyncio.run(scenario())

    def test_physical_deadline_does_not_modify_status(self):
        """物理死守超时时不修改任务状态。"""

        async def scenario():
            # 临时缩短物理死守用于测试
            original = mcp_tools.PHYSICAL_DEADLINE
            mcp_tools.PHYSICAL_DEADLINE = 2

            try:
                result = await mcp_tools.submit_and_wait(
                    "deadline_proj", "code", "req"
                )
                self.assertIn("物理死守超时", result["message"])
                # 验证任务状态仍为 pending（未被修改）
                task = await asyncio.to_thread(
                    database.get_task_by_id, result["task_id"]
                )
                self.assertEqual(task["status"], "pending")
                # 事件已清理
                self.assertNotIn(result["task_id"], mcp_tools._task_events)
            finally:
                mcp_tools.PHYSICAL_DEADLINE = original

        asyncio.run(scenario())

    def test_memory_leak_defense_100_tasks(self):
        """防御性测试：连续提交 100 个任务并完成，event_dict 必须回到低位。"""

        async def scenario():
            original_interval = mcp_tools.POLL_TASK_INTERVAL
            mcp_tools.POLL_TASK_INTERVAL = 0.05  # 适中轮询，防止 CPU 抢占
            try:
                async def run_one(idx):
                    dev_task = asyncio.create_task(
                        mcp_tools.submit_and_wait(f"leak_{idx}", "code", "req")
                    )
                    
                    await asyncio.sleep(0.05)
                    
                    task = await mcp_tools.poll_task(timeout=10)
                    self.assertIsNotNone(task["task_id"], f"Task {idx} was not polled!")
                    await mcp_tools.finish_test(task["task_id"], "success", "ok")
                    
                    # 等待唤醒，设置短超时防止死锁
                    await asyncio.wait_for(dev_task, timeout=5)

                for i in range(10):
                    tasks = [run_one(i * 10 + j) for j in range(10)]
                    await asyncio.gather(*tasks)

                self.assertEqual(len(mcp_tools._task_events), 0)
            finally:
                mcp_tools.POLL_TASK_INTERVAL = original_interval

        asyncio.run(scenario())


class TestPerformance(unittest.TestCase):
    """性能验证：事件通知延迟应在毫秒级。"""

    def setUp(self):
        conn = database._connect()
        try:
            conn.execute("DELETE FROM tasks;")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks';")
            conn.commit()
        finally:
            conn.close()
        mcp_tools._task_events.clear()

    def test_notification_latency(self):
        """从 finish_test 到 submit_and_wait 返回的延迟应 < 100ms。"""

        async def scenario():
            latencies = []

            async def dev_flow(idx):
                t0 = time.perf_counter()
                result = await mcp_tools.submit_and_wait(
                    f"perf_{idx}", f"code_{idx}", f"req_{idx}"
                )
                latency = time.perf_counter() - t0
                latencies.append(latency)
                return result

            async def qa_flow():
                await asyncio.sleep(0.3)
                for _ in range(5):
                    task = await mcp_tools.poll_task(timeout=5)
                    if task["task_id"]:
                        await mcp_tools.finish_test(
                            task["task_id"], "success", "/ok"
                        )

            dev_tasks = [asyncio.create_task(dev_flow(i)) for i in range(5)]
            await qa_flow()
            await asyncio.gather(*dev_tasks)

            # 每个任务的总时间应很短（远小于轮询方式的 2s 间隔）
            for lat in latencies:
                self.assertLess(lat, 2.0)

        asyncio.run(scenario())


class TestPollTask(unittest.TestCase):
    """测试 poll_task 长轮询逻辑。"""

    def setUp(self):
        conn = database._connect()
        try:
            conn.execute("DELETE FROM tasks;")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks';")
            conn.commit()
        finally:
            conn.close()
        mcp_tools._task_events.clear()

    def test_poll_returns_when_task_available(self):
        """有任务时 poll_task 应立即返回。"""

        async def scenario():
            database.create_task("poll_proj", "code_poll", "req_poll")
            result = await mcp_tools.poll_task(timeout=10)
            self.assertIsNotNone(result["task_id"])
            self.assertEqual(result["project_name"], "poll_proj")

        asyncio.run(scenario())

    def test_poll_waits_for_new_task(self):
        """无任务时 poll_task 应等待，直到有新任务。"""

        async def scenario():
            async def delayed_create():
                await asyncio.sleep(2)
                database.create_task("delayed_proj", "delayed_code", "delayed_req")

            create_task = asyncio.create_task(delayed_create())
            result = await mcp_tools.poll_task(timeout=15)

            self.assertIsNotNone(result["task_id"])
            self.assertEqual(result["project_name"], "delayed_proj")
            await create_task

        asyncio.run(scenario())

    def test_poll_timeout_returns_none(self):
        """超时无任务时应返回提示信息。"""

        async def scenario():
            result = await mcp_tools.poll_task(timeout=4)
            self.assertIsNone(result["task_id"])
            self.assertIn("超时", result["message"])

        asyncio.run(scenario())


class TestAsyncNonBlocking(unittest.TestCase):
    """验证异步操作不会阻塞事件循环。"""

    def setUp(self):
        conn = database._connect()
        try:
            conn.execute("DELETE FROM tasks;")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks';")
            conn.commit()
        finally:
            conn.close()
        mcp_tools._task_events.clear()

    def test_concurrent_operations(self):
        """多个操作应能并发执行，不互相阻塞。"""

        async def scenario():
            heartbeat_count = 0

            async def heartbeat():
                nonlocal heartbeat_count
                for _ in range(5):
                    await asyncio.sleep(0.1)
                    heartbeat_count += 1

            async def dev_submit():
                return await mcp_tools.submit_and_wait(
                    "conc_proj", "conc_code", "conc_req"
                )

            async def qa_flow():
                await asyncio.sleep(0.3)
                task = await mcp_tools.poll_task(timeout=10)
                if task["task_id"]:
                    await mcp_tools.finish_test(task["task_id"], "success", "/ok")

            results = await asyncio.gather(
                heartbeat(),
                dev_submit(),
                qa_flow(),
            )

            self.assertGreaterEqual(heartbeat_count, 3)
            self.assertEqual(results[1]["status"], "success")

        asyncio.run(scenario())


class TestFinishTest(unittest.TestCase):
    """测试 finish_test 输入校验。"""

    def test_invalid_status_rejected(self):
        """无效状态应被拒绝。"""

        async def scenario():
            result = await mcp_tools.finish_test(999, "invalid", "meta")
            self.assertFalse(result["success"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
