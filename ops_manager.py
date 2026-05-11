"""
AI-SyncForge 吹哨人模块 (Ops Manager)
持续扫描超时任务，生成运维急救工单，实现系统自愈。
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import database

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 30  # 扫描间隔（秒）
# 从环境变量获取吹哨人判定阈值（秒），默认为 600 秒（10 分钟）
try:
    WHISTLEBLOWER_TIMEOUT_SECONDS = int(os.getenv("WHISTLEBLOWER_TIMEOUT", 600))
except (ValueError, TypeError):
    logger.warning("Invalid WHISTLEBLOWER_TIMEOUT environment variable, using default 600")
    WHISTLEBLOWER_TIMEOUT_SECONDS = 600


def get_stale_testing_tasks(threshold_seconds: int = WHISTLEBLOWER_TIMEOUT_SECONDS) -> list[dict]:
    """
    查询 status='testing' 且 updated_at 超过阈值的任务。
    仅扫描 dev_test 类型（ops_task 不参与超时判定）。
    """
    conn = database._connect()
    try:
        cursor = conn.execute(
            "SELECT * FROM tasks "
            "WHERE status = 'testing' AND task_type = 'dev_test' "
            "AND updated_at <= datetime('now', ?);",
            (f"-{threshold_seconds} seconds",),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def create_ops_rescue_task(stale_task: dict) -> int:
    """
    为超时死锁任务生成一条 ops_task 急救工单。
    code_content 记录被判定为死锁的任务 ID 及元数据。
    """
    rescue_info = (
        f"DEADLOCK_DETECTED: task_id={stale_task['id']}, "
        f"project={stale_task['project_name']}, "
        f"stuck_since={stale_task['updated_at']}"
    )

    task_id = database.create_task(
        project=f"ops_rescue_{stale_task['id']}",
        code=rescue_info,
        req=f"任务 {stale_task['id']} 疑似死锁，请排查并清理故障环境。",
        task_type="ops_task",
        priority=999,
    )
    return task_id


async def ops_watchdog() -> None:
    """
    吹哨人主协程。
    持续扫描超时 testing 任务，发现异常时自动生成 ops_task。
    运行在 Broker 主进程的事件循环中。
    """
    logger.info(
        f"Ops watchdog started: scanning every {SCAN_INTERVAL}s, "
        f"stale threshold = {WHISTLEBLOWER_TIMEOUT_SECONDS}s"
    )

    while True:
        try:
            stale_tasks = await asyncio.to_thread(get_stale_testing_tasks)

            for task in stale_tasks:
                # 检查是否已经为该任务创建过急救工单（避免重复吹哨）
                existing = await asyncio.to_thread(_has_pending_rescue, task["id"])
                if existing:
                    continue

                rescue_id = await asyncio.to_thread(create_ops_rescue_task, task)
                logger.warning(
                    f"WHISTLE BLOWN: Task {task['id']} stuck for >{WHISTLEBLOWER_TIMEOUT_SECONDS}s, "
                    f"ops_task {rescue_id} created"
                )

        except Exception as e:
            logger.error(f"Ops watchdog scan error: {e}", exc_info=True)

        await asyncio.sleep(SCAN_INTERVAL)


def _has_pending_rescue(original_task_id: int) -> bool:
    """检查是否已有针对该任务的待处理急救工单。"""
    conn = database._connect()
    try:
        cursor = conn.execute(
            "SELECT id FROM tasks "
            "WHERE task_type = 'ops_task' AND status IN ('pending', 'testing') "
            "AND project_name = ?;",
            (f"ops_rescue_{original_task_id}",),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()
