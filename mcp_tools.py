"""
AI-SyncForge MCP 工具层
将核心状态机封装为 MCP 标准工具，实现 Dev-QA 异步协作流。
基于 asyncio.Event 内存通知机制实现零延迟状态回调。
"""

import asyncio
import logging
import os

import database

logger = logging.getLogger(__name__)

POLL_TASK_INTERVAL = 3  # poll_task 内部轮询间隔
# 从环境变量获取物理死守时间，默认为 1200 秒（20 分钟）
try:
    PHYSICAL_DEADLINE = int(os.getenv("PHYSICAL_TIMEOUT", 1200))
except (ValueError, TypeError):
    logger.warning("Invalid PHYSICAL_TIMEOUT environment variable, using default 1200")
    PHYSICAL_DEADLINE = 1200

# 内存事件注册表：task_id -> asyncio.Event
_task_events: dict[int, asyncio.Event] = {}


def _notify_task(task_id: int) -> None:
    """若存在对应 task_id 的等待事件，触发唤醒。"""
    event = _task_events.get(task_id)
    if event is not None:
        event.set()


async def submit_and_wait(
    project: str,
    code: str,
    req: str,
) -> dict:
    """
    [Dev 工具] 提交代码并等待测试结果。

    使用 asyncio.Event 实现零延迟异步等待，状态变更后毫秒级响应。
    保留 20 分钟物理死守防止极端情况下的协程泄露，
    但不主动熔断——超时判定权归属模块三。

    Args:
        project: 项目名称
        code: 待测代码内容
        req: 测试需求描述

    Returns:
        包含任务最终状态与报告信息的字典
    """
    task_id = await asyncio.to_thread(database.create_task, project, code, req)
    logger.info(f"Task {task_id} created, awaiting event notification...")

    event = asyncio.Event()
    _task_events[task_id] = event

    try:
        # 竞态防护：在注册事件后立刻检查数据库，防止 QA 抢跑导致错过通知
        task = await asyncio.to_thread(database.get_task_by_id, task_id)
        if task and task["status"] in ("success", "fail", "fail_by_ops_intervention"):
            logger.info(f"Task {task_id} already completed before wait: {task['status']}")
            # 已完成，无需等待
        else:
            try:
                await asyncio.wait_for(event.wait(), timeout=PHYSICAL_DEADLINE)
            except asyncio.TimeoutError:
                logger.error(
                    f"Task {task_id} hit physical deadline ({PHYSICAL_DEADLINE}s), "
                    "releasing coroutine without modifying status"
                )
                task = await asyncio.to_thread(database.get_task_by_id, task_id)
                return {
                    "task_id": task_id,
                    "status": task["status"] if task else "unknown",
                    "message": f"物理死守超时（{PHYSICAL_DEADLINE}s），协程释放。任务状态未被修改，等待模块三处理。",
                }

        task = await asyncio.to_thread(database.get_task_by_id, task_id)
        if task is None:
            return {"task_id": task_id, "status": "error", "message": "Task not found"}

        logger.info(f"Task {task_id} completed with status: {task['status']}")
        return {
            "task_id": task_id,
            "status": task["status"],
            "report_path": task["report_path"],
            "retry_count": task["retry_count"],
        }
    finally:
        _task_events.pop(task_id, None)


async def poll_task(timeout: int = 300) -> dict:
    """
    [QA 工具] 长轮询获取待测任务。

    在服务端内部循环等待，直到有可用任务或超时。
    避免客户端频繁发起空请求。

    Args:
        timeout: 最大等待秒数，默认 300 秒

    Returns:
        任务详情字典，或超时无任务的提示
    """
    elapsed = 0
    while elapsed < timeout:
        task = await asyncio.to_thread(database.get_pending_task)
        if task is not None:
            logger.info(f"Task {task['id']} dispatched to QA")
            return {
                "task_id": task["id"],
                "project_name": task["project_name"],
                "code_content": task["code_content"],
                "test_requirement": task["test_requirement"],
            }

        await asyncio.sleep(POLL_TASK_INTERVAL)
        elapsed += POLL_TASK_INTERVAL

    return {"task_id": None, "message": "轮询超时，当前无待测任务。"}


async def poll_ops_task(timeout: int = 300) -> dict:
    """
    [Ops 工具] 长轮询获取运维急救任务。

    专供 Ops-Forge 使用，仅拉取 task_type='ops_task' 的任务。
    Ops-Forge 启动后进入此长轮询状态，时刻待命。

    Args:
        timeout: 最大等待秒数，默认 300 秒

    Returns:
        运维任务详情字典，或超时无任务的提示
    """
    elapsed = 0
    while elapsed < timeout:
        task = await asyncio.to_thread(database.poll_ops_task)
        if task is not None:
            logger.info(f"Ops task {task['id']} dispatched to Ops-Forge")
            return {
                "task_id": task["id"],
                "project_name": task["project_name"],
                "code_content": task["code_content"],
                "test_requirement": task["test_requirement"],
                "priority": task["priority"],
            }

        await asyncio.sleep(POLL_TASK_INTERVAL)
        elapsed += POLL_TASK_INTERVAL

    return {"task_id": None, "message": "轮询超时，当前无运维任务。"}


async def manage_env(action: str, params: str, related_task_id: int | None = None) -> dict:
    """
    [Ops 工具] 执行环境管理操作。

    支持对故障容器的 restart/cleanup/logs 等操作。
    严禁重启 Broker 自身容器。

    Args:
        action: 操作类型 ('restart' / 'cleanup' / 'logs')
        params: 操作参数（如容器名称、路径等）
        related_task_id: 关联的原始任务 ID（可选）

    Returns:
        操作结果
    """
    ALLOWED_ACTIONS = ("restart", "cleanup", "logs", "inspect")

    if action not in ALLOWED_ACTIONS:
        return {"success": False, "message": f"Unsupported action: {action}"}

    # 安全防护：防止通过名称或 ID 误伤 Broker 自身容器
    FORBIDDEN_KEYWORDS = ("broker", "syncforge", "ai-syncforge")
    if any(kw in params.lower() for kw in FORBIDDEN_KEYWORDS):
        return {
            "success": False,
            "message": "FORBIDDEN: 严禁对 Broker 自身容器（AI-SyncForge）执行破坏性操作。",
        }

    logger.info(f"Ops manage_env: action={action}, params={params}")

    # 模拟环境操作（实际部署时接入 Docker SDK）
    result_message = f"Action '{action}' executed with params: {params}"

    if related_task_id is not None:
        await asyncio.to_thread(
            database.update_task_status,
            related_task_id,
            "fail_by_ops_intervention",
            f"OPS: {action} - {params}",
        )
        _notify_task(related_task_id)
        result_message += f" | Task {related_task_id} marked as ops_intervention"

    return {"success": True, "action": action, "message": result_message}


async def finish_test(task_id: int, status: str, report_meta: str) -> dict:
    """
    [QA/Ops 工具] 提交测试或修复结果。

    更新任务状态后立即触发内存事件通知，唤醒等待中的 submit_and_wait 协程。
    所有外部状态变更必须通过此接口，确保事件信号不被绕过。

    Args:
        task_id: 任务 ID
        status: 结果状态 ('success' / 'fail' / 'fail_by_ops_intervention')
        report_meta: 报告路径或描述

    Returns:
        操作确认信息
    """
    if status not in ("success", "fail", "fail_by_ops_intervention"):
        return {"success": False, "message": f"Invalid status: {status}"}

    await asyncio.to_thread(database.update_task_status, task_id, status, report_meta)
    _notify_task(task_id)
    logger.info(f"Task {task_id} finished: {status}, event notified")
    return {
        "success": True,
        "task_id": task_id,
        "status": status,
        "report_meta": report_meta,
    }
