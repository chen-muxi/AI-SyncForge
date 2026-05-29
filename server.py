"""
AI-SyncForge MCP Broker 服务 (V2)
基于 FastMCP 的 SSE 模式服务端，挂载 Dev/QA/Ops 协作工具。
内置 Ops 吹哨人协程实现系统自愈。
V2 新增：DAG 任务路由、意图蒸馏、QA 抗辩机制。
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
import uvicorn

import database
import mcp_tools
import ops_manager

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 初始化数据库
database.init_db()


@asynccontextmanager
async def app_lifespan(app):
    """服务启动时拉起 Ops 吹哨人协程。"""
    asyncio.create_task(ops_manager.ops_watchdog())
    logger.info("Ops watchdog coroutine launched")
    yield


# 创建 MCP Server
mcp = FastMCP(
    "AI-SyncForge Broker",
    instructions="AI-SyncForge 任务调度中心，提供 Dev-QA-Ops 三方异步协作工具。",
    lifespan=app_lifespan,
)


# ─── V1 Dev 工具 ──────────────────────────────────────────────────────────────


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


# ─── V2 Dev 工具 ──────────────────────────────────────────────────────────────


@mcp.tool()
async def get_next_task(project: str, timeout: int = 60) -> dict:
    """
    [Dev] 获取下一个可用的 DAG 任务。
    原子锁定一个依赖已满足的 pending 任务，状态变为 in_progress。
    长轮询挂起等待，无可用任务时不立即返回，节省 Token。
    返回任务详情 + 直接前置节点的摘要（上下文裁剪）。
    Args:
        project: 项目名称
        timeout: 最大等待秒数（默认 60）
    """
    return await mcp_tools.get_next_task(project, timeout)


@mcp.tool()
async def mark_task_done(task_id: int, status: str, summary: str) -> dict:
    """
    [Dev] 标记 DAG 任务完成。
    将 summary 写入 metadata 实现意图蒸馏。
    成功时解锁下游节点（含 I/O 冷却期）。
    失败时触发级联阻断 + 3次自动熔断升级。
    支持 qa_rejection 状态发起 QA 抗辩。
    Args:
        task_id: 任务 ID
        status: 完成状态 ('success' / 'fail' / 'qa_rejection')
        summary: 任务摘要
    """
    return await mcp_tools.mark_task_done(task_id, status, summary)


@mcp.tool()
async def read_task_context(task_id: int) -> dict:
    """
    [Dev] 读取任务的完整上下文。
    通过后端协议直接读取，绕过 IDE 前端字数限制。
    当 metadata 超过 50KB 时自动降级为本地文件路径。
    Args:
        task_id: 任务 ID
    """
    return await mcp_tools.read_task_context(task_id)


# ─── QA 工具 ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def poll_task(timeout: int = 300) -> dict:
    """
    [QA] 长轮询获取待测任务。
    内部循环等待直到有新的 dev_test 任务可用，避免客户端空轮询。
    Args:
        timeout: 最大等待秒数（默认 300）。
    """
    return await mcp_tools.poll_task(timeout)


@mcp.tool()
async def finish_test(task_id: int, status: str, report_meta: str) -> dict:
    """
    [QA] 提交测试结果。
    将测试状态与报告写回任务队列，解除 Dev 端的等待阻塞。
    Args:
        task_id: 任务 ID
        status: 测试结果 ('success' / 'fail')
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
        timeout: 最大等待秒数（默认 300）。
    """
    return await mcp_tools.poll_ops_task(timeout)


@mcp.tool()
async def manage_env(action: str, params: str, related_task_id: int | None = None) -> dict:
    """
    [Ops] 执行环境管理操作。
    支持对故障容器执行 restart/cleanup/logs/inspect 操作。
    严禁重启 Broker 自身容器。操作完成后自动触发事件通知唤醒 Dev。
    Args:
        action: 操作类型 ('restart' / 'cleanup' / 'logs' / 'inspect')
        params: 操作参数（容器名称、路径等）
        related_task_id: 关联的原始故障任务 ID（可选）
    """
    return await mcp_tools.manage_env(action, params, related_task_id)


@mcp.tool()
async def inspect_project_tree(project: str) -> dict:
    """
    [Ops/Dev] 查看项目的 DAG 状态树。
    返回项目中所有任务的状态快照，用于可视化和调试。
    Args:
        project: 项目名称
    """
    return await mcp_tools.inspect_project_tree(project)


@mcp.tool()
async def reset_task_branch(task_id: int) -> dict:
    """
    [Ops] 重置卡死分支。
    将指定任务及其所有下游任务重置为 pending 状态。
    用于死锁解除和灾难恢复。
    Args:
        task_id: 需要重置的任务 ID
    """
    return await mcp_tools.reset_task_branch(task_id)


# ─── 服务启动 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    port = int(os.getenv("SYNCFORGE_PORT", 8000))
    logger.info(f"Starting AI-SyncForge MCP Broker on port {port} (Streamable-HTTP mode)...")
    
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]
    
    # 使用 streamable-http 模式，它对纯 HTTP POST 更加友好，且原生支持根路径交互
    app = mcp.http_app(transport="streamable-http", path="/", middleware=middleware)
    
    # 协议级路由修复：
    # 目的：使用 ASGI 中间件拦截 POST/DELETE / 请求并转发给消息处理器，解决部分客户端预检或清理行为导致的 405
    message_handler_app = None
    target_path = os.getenv("MCP_MESSAGE_PATH", "/messages")
    for route in app.routes:
        if hasattr(route, "path") and route.path.startswith(target_path):
            message_handler_app = route.app
            logger.info(f"Detected message handler at {route.path}")
            break
            
    if message_handler_app:
        class RootBypassMiddleware:
            def __init__(self, app, target_asgi_app):
                self.app = app
                self.target_asgi_app = target_asgi_app

            async def __call__(self, scope, receive, send):
                # 拦截发往根路径的 POST 或 DELETE 请求
                if (
                    scope["type"] == "http" 
                    and scope["path"] == "/" 
                    and scope.get("method") in ("POST", "DELETE")
                ):
                    await self.target_asgi_app(scope, receive, send)
                else:
                    await self.app(scope, receive, send)

        # 包装应用
        app = RootBypassMiddleware(app, message_handler_app)
        logger.info("CRITICAL: Root-Path POST/DELETE bypass middleware active")
    else:
        logger.warning("Could not detect message handler app; Root-Path bypass middleware NOT active")
    
    logger.info("AI-SyncForge Broker is now running in Streamable-HTTP mode")

    # 运行服务
    uvicorn.run(app, host="0.0.0.0", port=port)
