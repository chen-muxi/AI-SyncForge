"""
AI-SyncForge MCP Broker 服务
基于 FastMCP 的 SSE 模式服务端，挂载 Dev/QA/Ops 协作工具。
内置 Ops 吹哨人协程实现系统自愈。
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
