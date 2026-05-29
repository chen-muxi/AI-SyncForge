"""
AI-SyncForge MCP 工具实现 (V2)
提供 Dev/QA/Ops 三方协作工具的具体逻辑实现。
V2 新增：DAG 路由调度、意图蒸馏、I/O 冷却锁、QA 抗辩机制。
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import database
import git_workspace

logger = logging.getLogger(__name__)

POLL_TASK_INTERVAL = 3  # poll_task 内部轮询间隔
GET_NEXT_TASK_INTERVAL = 3  # get_next_task 内部轮询间隔

# 从环境变量获取物理死守时间，默认为 1200 秒（20 分钟）
try:
    PHYSICAL_DEADLINE = int(os.getenv("PHYSICAL_TIMEOUT", 1200))
except (ValueError, TypeError):
    logger.warning("Invalid PHYSICAL_TIMEOUT environment variable, using default 1200")
    PHYSICAL_DEADLINE = 1200

# I/O 落盘冷却期（毫秒）：防止 OS 层文件系统竞态
try:
    IO_SETTLE_DELAY_MS = int(os.getenv("IO_SETTLE_DELAY_MS", 1000))
except (ValueError, TypeError):
    IO_SETTLE_DELAY_MS = 1000

# metadata 安全阀阈值（字节），超过此大小降级为文件
METADATA_SIZE_LIMIT = 50 * 1024  # 50KB

# 自动熔断阈值
MAX_RETRY_BEFORE_ESCALATION = 3

# QA 抗辩需要的最小重试次数
MIN_RETRY_FOR_QA_REJECTION = 2

# 内存事件注册表：task_id -> asyncio.Event
_task_events: dict[int, asyncio.Event] = {}


def _notify_task(task_id: int) -> None:
    """若存在对应 task_id 的等待事件，触发唤醒。"""
    event = _task_events.get(task_id)
    if event is not None:
        event.set()


# ─── V1 兼容工具 ─────────────────────────────────────────────────────────────


async def submit_and_wait(
    project: str,
    code: str,
    req: str,
) -> dict:
    """
    [Dev 工具] 提交代码并等待测试结果。
    使用 asyncio.Event 实现零延迟异步等待，状态变更后毫秒级响应。
    保留 20 分钟物理死守防止极端情况下的协程泄露。
    但不主动熔断——超时判定权归属 Ops 模块。
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
                    "message": f"物理死守超时（{PHYSICAL_DEADLINE}s），协程释放。任务状态未被修改，等待运维处理。",
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
        timeout: 最大等待秒数，默认 300 秒。
    Returns:
        任务详情字典，或超时无任务的提示
    """
    start_time = asyncio.get_event_loop().time()
    end_time = start_time + timeout
    
    while asyncio.get_event_loop().time() < end_time:
        task = await asyncio.to_thread(database.get_pending_task)
        if task is not None:
            task_id = task["id"]
            logger.info(f"Task {task_id} dispatched to QA")
            
            # [Task 2.1] QA Worktree 隔离
            await asyncio.to_thread(git_workspace.create_qa_worktree, task_id)
            wt_path = git_workspace.get_qa_worktree_path(task_id)

            return {
                "task_id": task_id,
                "project_name": task["project_name"],
                "code_content": task["code_content"],
                "test_requirement": task["test_requirement"],
                "qa_worktree": str(wt_path),
            }

        remaining = end_time - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(POLL_TASK_INTERVAL, remaining))

    return {
        "task_id": None,
        "project_name": None,
        "code_content": None,
        "test_requirement": None,
        "message": "轮询超时，当前无待测任务。",
    }


async def poll_ops_task(timeout: int = 300) -> dict:
    """
    [Ops 工具] 长轮询获取运维急救任务。
    专供 Ops-Forge 使用，仅拉取 task_type='ops_task' 的任务。
    Ops-Forge 启动后进入此长轮询状态，时刻待命。
    Args:
        timeout: 最大等待秒数，默认 300 秒。
    Returns:
        运维任务详情字典，或超时无任务的提示
    """
    start_time = asyncio.get_event_loop().time()
    end_time = start_time + timeout

    while asyncio.get_event_loop().time() < end_time:
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

        remaining = end_time - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(POLL_TASK_INTERVAL, remaining))

    return {
        "task_id": None,
        "project_name": None,
        "code_content": None,
        "test_requirement": None,
        "priority": None,
        "message": "轮询超时，当前无运维任务。",
    }


async def manage_env(action: str, params: str, related_task_id: int | None = None) -> dict:
    """
    [Ops 工具] 执行环境管理操作。
    支持对故障容器的 restart/cleanup/logs 等操作。
    严禁重启 Broker 自身容器。
    Args:
        action: 操作类型 ('restart' / 'cleanup' / 'logs' / 'inspect')
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
    
    # [Task 2.1] 成功后合并到主干
    if status == "success":
        lock = git_workspace.WorkspaceLock()
        if not await asyncio.to_thread(lock.acquire, "qa", task_id):
            # 锁被占用，QA 无法合并
            logger.warning(f"Task {task_id} QA passed but workspace is locked, merge deferred")
            return {
                "success": False,
                "task_id": task_id,
                "status": "completed_dev", # 保持状态，提示重试
                "message": "工作区已被 Dev 锁定，QA 合并操作已推迟。请等待 Dev 释放锁后重试 finish_test。",
            }

        try:
            merge_res = await asyncio.to_thread(git_workspace.merge_to_main, task_id)
            if not merge_res["success"]:
                # 合并冲突，升级为 FATAL_LOCKED
                lock.acquire_fatal("qa", task_id, f"Final Merge Conflict: {merge_res['message']}")
                logger.critical(f"Task {task_id} QA passed but merge failed!")
            else:
                logger.info(f"Task {task_id} successfully merged to main after QA pass")
        finally:
            lock.release("qa")

    # [Task 2.1] 清理 QA Worktree
    await asyncio.to_thread(git_workspace.cleanup_qa_worktree, task_id)

    _notify_task(task_id)
    logger.info(f"Task {task_id} finished: {status}, event notified")
    return {
        "success": True,
        "task_id": task_id,
        "status": status,
        "report_meta": report_meta,
    }


# ─── V2 DAG 路由工具 ─────────────────────────────────────────────────────────


async def get_next_task(project: str, timeout: int = 60) -> dict:
    """
    [Dev 工具] 获取下一个可用的 DAG 任务。
    
    原子性地获取并锁定一个可用任务（依赖已满足、遵循 MAX_CONCURRENCY 串行锁）。
    包含直接前置节点的摘要，实现上下文窗口滑动管理。
    
    长轮询：无可用任务时挂起等待，直到有任务可用或超时。
    
    Args:
        project: 项目名称
        timeout: 最大等待秒数（默认 60）
    Returns:
        任务详情 + 前置摘要，或超时提示
    """
    start_time = asyncio.get_event_loop().time()
    end_time = start_time + timeout

    while asyncio.get_event_loop().time() < end_time:
        task = await asyncio.to_thread(database.get_next_available_task, project)
        if task is not None:
            logger.info(f"DAG task {task['id']} dispatched to Dev (project={project})")

            # [Task 2.1] Workspace Lock & Feature Branch
            lock = git_workspace.WorkspaceLock()
            if not await asyncio.to_thread(lock.acquire, "dev", task["id"]):
                # 锁被占用，回退状态为 pending，继续等待
                await asyncio.to_thread(database.update_task_status, task["id"], "pending")
                logger.warning(f"Workspace locked, task {task['id']} reverted to pending")
                await asyncio.sleep(GET_NEXT_TASK_INTERVAL)
                continue

            # 创建并切换分支
            git_res = await asyncio.to_thread(git_workspace.create_feature_branch, task["id"])
            if not git_res["success"]:
                # 分支操作失败，释放锁并回退
                lock.release("dev")
                await asyncio.to_thread(database.update_task_status, task["id"], "pending")
                logger.error(f"Failed to create branch for task {task['id']}: {git_res['message']}")
                await asyncio.sleep(GET_NEXT_TASK_INTERVAL)
                continue

            # 获取前置节点摘要（上下文裁剪）
            predecessors = await asyncio.to_thread(
                database.get_predecessor_summaries, task["id"]
            )

            return {
                "task_id": task["id"],
                "task_name": task.get("task_name"),
                "project_name": task["project_name"],
                "code_content": task["code_content"],
                "test_requirement": task["test_requirement"],
                "status": task["status"],
                "depends_on": task.get("depends_on"),
                "metadata": task.get("metadata"),
                "predecessor_summaries": predecessors,
                "branch": git_res["branch"],
            }

        remaining = end_time - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(GET_NEXT_TASK_INTERVAL, remaining))

    return {
        "task_id": None,
        "task_name": None,
        "project_name": project,
        "status": None,
        "message": f"长轮询超时（{timeout}s），当前无可用任务。",
    }


async def mark_task_done(
    task_id: int,
    status: str,
    summary: str,
) -> dict:
    """
    [Dev 工具] 标记 DAG 任务完成。
    
    1. 将 summary 写入 metadata（意图蒸馏）
    2. 更新任务状态
    3. 若成功：I/O 冷却后解锁下游节点
    4. 若失败：触发级联阻断 + 检查自动熔断
    5. 若 qa_rejection：生成 Ops 仲裁工单
    
    Args:
        task_id: 任务 ID
        status: 完成状态 ('success' / 'fail' / 'qa_rejection')
        summary: 任务摘要（1KB 以内）
    Returns:
        操作结果
    """
    VALID_STATUSES = ("success", "fail", "qa_rejection")
    if status not in VALID_STATUSES:
        return {
            "success": False,
            "message": f"Invalid status: {status}. Valid: {VALID_STATUSES}",
        }

    # 获取当前任务信息
    task = await asyncio.to_thread(database.get_task_by_id, task_id)
    if task is None:
        return {"success": False, "message": f"Task {task_id} not found"}

    # QA 抗辩：需要 retry_count >= MIN_RETRY_FOR_QA_REJECTION
    if status == "qa_rejection":
        if task["retry_count"] < MIN_RETRY_FOR_QA_REJECTION:
            return {
                "success": False,
                "message": (
                    f"qa_rejection 需要 retry_count >= {MIN_RETRY_FOR_QA_REJECTION}，"
                    f"当前 retry_count = {task['retry_count']}"
                ),
            }
        # 生成 Ops 仲裁工单
        ops_task_id = await asyncio.to_thread(
            database.create_task,
            f"qa_audit_{task_id}",
            json.dumps({
                "original_task_id": task_id,
                "project": task["project_name"],
                "dev_summary": summary,
                "retry_count": task["retry_count"],
            }),
            f"QA 抗辩：任务 {task_id} 经过 {task['retry_count']} 次失败后 Dev 提出质疑，"
            "请审计 QA 测试脚本是否正确。",
            task_type="ops_task",
            priority=99,
        )
        logger.warning(
            f"QA REJECTION: Task {task_id} challenged by Dev, "
            f"ops audit task {ops_task_id} created"
        )
        return {
            "success": True,
            "task_id": task_id,
            "status": "qa_rejection",
            "ops_task_created": ops_task_id,
            "message": "QA 抗辩已受理，Ops 仲裁工单已生成。",
        }

    # 1. 写入 summary 到 metadata（意图蒸馏）
    existing_meta = {}
    if task["metadata"]:
        try:
            existing_meta = json.loads(task["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass

    existing_meta["summary"] = summary[:1024]  # 截断至 1KB
    existing_meta["completed_at"] = str(asyncio.get_event_loop().time())
    meta_json = json.dumps(existing_meta, ensure_ascii=False)
    await asyncio.to_thread(database.update_task_metadata, task_id, meta_json)

    # 2. 更新状态
    if status == "fail":
        await asyncio.to_thread(
            database.update_task_status, task_id, "fail", summary
        )

        # [Task 2.1] 失败回滚并释放锁
        await asyncio.to_thread(git_workspace.rollback_branch, task_id)
        # 强制切回 main 以便后续任务
        await asyncio.to_thread(git_workspace.run_git, "checkout", git_workspace.get_main_branch())
        git_workspace.WorkspaceLock().release("dev")

        # 检查自动熔断
        updated_task = await asyncio.to_thread(database.get_task_by_id, task_id)
        if updated_task and updated_task["retry_count"] >= MAX_RETRY_BEFORE_ESCALATION:
            # 自动升级为 ops_task
            escalation_id = await asyncio.to_thread(
                database.create_task,
                f"escalation_{task_id}",
                json.dumps({
                    "original_task_id": task_id,
                    "project": task["project_name"],
                    "retry_count": updated_task["retry_count"],
                    "last_error": summary,
                }),
                f"任务 {task_id} 重试达 {updated_task['retry_count']} 次，自动升级为运维工单。",
                task_type="ops_task",
                priority=99,
            )
            logger.warning(
                f"AUTO ESCALATION: Task {task_id} retried "
                f"{updated_task['retry_count']} times, ops_task {escalation_id} created"
            )

        # 3. 级联阻断下游
        blocked = await asyncio.to_thread(database.cascade_block, task_id)
        logger.info(f"Task {task_id} failed, cascade blocked: {blocked}")

        return {
            "success": True,
            "task_id": task_id,
            "status": "fail",
            "cascade_blocked": blocked,
            "message": f"任务失败，分支已回滚并释放锁。{len(blocked)} 个下游任务被级联阻断。",
        }

    elif status == "success":
        # [Task 2.1] Dev 成功，进入完成状态，等待 QA
        # 切换回 main，否则 QA 无法在 Worktree 中 checkout 此分支
        await asyncio.to_thread(git_workspace.run_git, "checkout", git_workspace.get_main_branch())

        await asyncio.to_thread(
            database.update_task_status, task_id, "completed_dev"
        )

        # 释放 Workspace Lock 给 QA 或其他 Dev
        git_workspace.WorkspaceLock().release("dev")

        # I/O 落盘冷却期
        delay_seconds = IO_SETTLE_DELAY_MS / 1000.0
        await asyncio.sleep(delay_seconds)
        logger.info(
            f"Task {task_id} marked as completed_dev, I/O settle delay {IO_SETTLE_DELAY_MS}ms applied"
        )

        return {
            "success": True,
            "task_id": task_id,
            "status": "completed_dev",
            "message": "Dev 开发完成，任务已提交至 QA 队列，Workspace 已释放。",
        }

    return {"success": False, "message": f"Unexpected status: {status}"}


async def read_task_context(task_id: int) -> dict:
    """
    [Dev 工具] 读取任务的完整上下文。
    
    通过后端协议直接读取，绕过 IDE 前端的物理字数限制。
    当 metadata 超过 50KB 时，自动降级为本地文件路径。
    
    Args:
        task_id: 任务 ID
    Returns:
        任务上下文详情
    """
    task = await asyncio.to_thread(database.get_task_by_id, task_id)
    if task is None:
        return {"success": False, "message": f"Task {task_id} not found"}

    # 获取前置节点摘要
    predecessors = await asyncio.to_thread(
        database.get_predecessor_summaries, task_id
    )

    result = {
        "success": True,
        "task_id": task_id,
        "task_name": task.get("task_name"),
        "project_name": task["project_name"],
        "code_content": task["code_content"],
        "test_requirement": task["test_requirement"],
        "status": task["status"],
        "depends_on": task.get("depends_on"),
        "predecessor_summaries": predecessors,
        "retry_count": task["retry_count"],
    }

    # 50KB 安全阀
    metadata_raw = task.get("metadata") or ""
    if len(metadata_raw.encode("utf-8")) > METADATA_SIZE_LIMIT:
        # 降级：写入本地文件
        scratch_dir = Path(os.getenv(
            "SYNCFORGE_SCRATCH_DIR",
            Path(__file__).parent / "scratch",
        ))
        scratch_dir.mkdir(parents=True, exist_ok=True)
        ctx_file = scratch_dir / f"ctx_{task_id}.md"
        ctx_file.write_text(metadata_raw, encoding="utf-8")
        result["metadata"] = f"[OVERSIZED] 写入本地文件: {ctx_file}"
        result["ctx_file"] = str(ctx_file)
        logger.warning(
            f"Task {task_id} metadata oversized ({len(metadata_raw)} bytes), "
            f"written to {ctx_file}"
        )
    else:
        result["metadata"] = metadata_raw if metadata_raw else None

    return result


async def inspect_project_tree(project: str) -> dict:
    """
    [Ops/Dev 工具] 查看项目的 DAG 状态树。
    
    返回项目中所有任务的状态快照，用于可视化和调试。
    
    Args:
        project: 项目名称
    Returns:
        任务列表及其依赖关系
    """
    conn = database._connect()
    try:
        cursor = conn.execute(
            "SELECT id, task_name, status, depends_on, priority, retry_count, "
            "task_type, created_at, updated_at "
            "FROM tasks WHERE project_name = ? "
            "ORDER BY id ASC;",
            (project,),
        )
        tasks = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    return {
        "success": True,
        "project": project,
        "task_count": len(tasks),
        "tasks": tasks,
    }


async def reset_task_branch(task_id: int) -> dict:
    """
    [Ops 工具] 重置卡死分支。
    
    将指定任务及其所有下游任务重置为 pending 状态。
    用于死锁解除。
    
    Args:
        task_id: 需要重置的任务 ID
    Returns:
        重置结果
    """
    task = await asyncio.to_thread(database.get_task_by_id, task_id)
    if task is None:
        return {"success": False, "message": f"Task {task_id} not found"}

    reset_ids = [task_id]

    # 收集所有下游任务
    conn = database._connect()
    try:
        queue = [task_id]
        visited = {task_id}

        while queue:
            current_id = queue.pop(0)
            cursor = conn.execute(
                "SELECT id, depends_on FROM tasks WHERE depends_on IS NOT NULL;",
            )
            for row in cursor.fetchall():
                try:
                    dep_ids = json.loads(row[0 + 1])  # depends_on
                except (json.JSONDecodeError, TypeError):
                    continue
                if current_id in dep_ids and row[0] not in visited:
                    visited.add(row[0])
                    reset_ids.append(row[0])
                    queue.append(row[0])

        # 重置所有收集到的任务
        for rid in reset_ids:
            conn.execute(
                "UPDATE tasks SET status = 'pending', retry_count = 0 WHERE id = ?;",
                (rid,),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Branch reset from task {task_id}: {reset_ids}")
    return {
        "success": True,
        "reset_task_ids": reset_ids,
        "message": f"已重置 {len(reset_ids)} 个任务。",
    }
