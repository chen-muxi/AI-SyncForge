"""
AI-SyncForge MCP Broker 服务
基于 FastMCP 的 SSE 模式服务端，挂载 Dev/QA/Ops 协作工具。
内置 Ops 吹哨人协程实现系统自愈。
"""

import asyncio
import logging

from fastmcp import FastMCP

import database
import mcp_tools
import ops_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 初始化数据库
database.init_db()

# 创建 MCP Server
mcp = FastMCP(
    "AI-SyncForge Broker",
    instructions="AI-SyncForge 任务调度中心，提供 Dev-QA-Ops 三方异步协作工具。",
)


# ─── Dev 工具 ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def submit_and_wait(project: str, code: str, req: str) -> dict:
    """
    [Dev] 提交代码并等待测试结果。

    将代码与测试需求写入队列，通过事件通知机制等待 QA 完成测试后毫秒级返回结果。
    超时判定权归属 Ops 监控模块，本工具不主动熔断。

    Args:
        project: 项目名称
        code: 待测代码内容
        req: 测试需求描述
    """
    return await mcp_tools.submit_and_wait(project, code, req)


# ─── QA 工具 ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def poll_task(timeout: int = 300) -> dict:
    """
    [QA] 长轮询获取待测任务。

    内部循环等待直到有新的 dev_test 任务可用，避免客户端空轮询。

    Args:
        timeout: 最大等待秒数（默认 300）
    """
    return await mcp_tools.poll_task(timeout)


@mcp.tool()
async def finish_test(task_id: int, status: str, report_meta: str) -> dict:
    """
    [QA] 提交测试结果。

    将测试状态与报告写回任务队列，解除 Dev 端的等待阻塞。

    Args:
        task_id: 任务 ID
        status: 测试结果（'success' / 'fail'）
        report_meta: 报告路径或描述
    """
    return await mcp_tools.finish_test(task_id, status, report_meta)


# ─── Ops 工具 ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def poll_ops_task(timeout: int = 300) -> dict:
    """
    [Ops] 长轮询获取运维急救任务。

    专供 Ops-Forge 使用，拉取系统自动生成的 ops_task 工单。
    Ops-Forge 启动后进入此长轮询状态，时刻待命。

    Args:
        timeout: 最大等待秒数（默认 300）
    """
    return await mcp_tools.poll_ops_task(timeout)


@mcp.tool()
async def manage_env(action: str, params: str, related_task_id: int | None = None) -> dict:
    """
    [Ops] 执行环境管理操作。

    支持对故障容器执行 restart/cleanup/logs/inspect 操作。
    严禁重启 Broker 自身容器。操作完成后自动触发事件通知唤醒 Dev。

    Args:
        action: 操作类型（'restart' / 'cleanup' / 'logs' / 'inspect'）
        params: 操作参数（容器名称、路径等）
        related_task_id: 关联的原始故障任务 ID（可选）
    """
    return await mcp_tools.manage_env(action, params, related_task_id)


# ─── 服务启动 ─────────────────────────────────────────────────────────────────


@mcp.on_event("startup")
async def on_startup():
    """服务启动时拉起 Ops 吹哨人协程。"""
    asyncio.create_task(ops_manager.ops_watchdog())
    logger.info("Ops watchdog coroutine launched")


if __name__ == "__main__":
    logger.info("Starting AI-SyncForge MCP Broker on port 8000 (SSE mode)...")
    mcp.run(transport="sse", host="0.0.0.0", port=8000)
