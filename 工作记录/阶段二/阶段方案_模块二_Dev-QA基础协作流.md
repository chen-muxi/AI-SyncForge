# AI-SyncForge 阶段方案：模块二 - Dev-QA 基础协作流

## 1. 模块定位
本模块是系统的骨架，负责引入 FastMCP 框架并打通 Cursor (开发) 与 Antigravity (质检) 之间的通信链路。实现基于 SSE 的异步阻塞与回调机制。

## 2. 核心开发任务

### 2.1 引入 FastMCP 服务 (`server.py`)
- 使用 `fastmcp` 库创建 MCP Server。
- 配置服务以 SSE 模式运行，确保支持跨设备/局域网连接。
- 导入 `database.py` 中的 CRUD 接口。
- **异步安全原则（重点）**：由于 FastMCP 运行在 `asyncio` 事件循环中，所有对 `database.py` 同步接口（如 `create_task`, `get_pending_task`, `update_task_status`）的调用必须通过 `asyncio.to_thread()` 进行包裹，严禁直接执行同步 I/O 以免卡死服务。

### 2.2 包装 MCP Tools (`mcp_tools.py`)
将模块一的功能包装为标准 MCP 工具：

1.  **`submit_and_wait(project, code, req)`**:
    *   使用 `asyncio.to_thread(create_task, ...)` 写入数据库。
    *   **核心逻辑**：基于 `asyncio.Event` 内存信号机制实现。注册 Event 后直接 `await`，等待 `finish_test` 或 `manage_env` 触发信号。
    *   **物理死守**：设置 1200 秒（20 分钟）的物理死守，仅用于防止极端情况下的协程泄露，不执行主动业务熔断。
    *   **退出条件**：信号触发后，从数据库读取最新状态并返回。

2.  **`poll_task()`**:
    *   **长轮询模式**：利用 `asyncio.sleep()` 在工具内部实现非阻塞的短时挂起与循环检测。
    *   持续调用 `asyncio.to_thread(get_pending_task)` 直到获取到任务才返回，避免客户端频繁发起空请求。

3.  **`finish_test(task_id, status, report_meta)`**:
    *   使用 `asyncio.to_thread(update_task_status, ...)` 更新结果。
    *   **信号同步**：更新数据库后立即调用 `_notify_task(task_id)`，确保 `submit_and_wait` 毫秒级唤醒。

### 2.3 集成与连接逻辑
- 确保 `server.py` 能够正确挂载上述工具。
- 处理基本的异常捕获，防止因单次数据库读取错误导致 SSE 服务崩溃。

## 3. 验收标准
- [ ] 启动 `server.py`，SSE 服务端口（默认 8000）正常响应。
- [ ] **异步阻塞验证**：确认数据库操作期间 SSE 服务仍能响应心跳或其他并发请求。
- [ ] **端到端模拟测试**：
    1.  通过模拟工具（或 Cursor）调用 `submit_and_wait`，观测到进程进入等待状态。
    2.  另开一个脚本模拟质检员调用 `poll_task` 获取该任务。
    3.  质检员调用 `finish_test` 提交模拟结果。
    4.  观测到最初的 `submit_and_wait` 成功返回结果并解除阻塞。
- [ ] **超时熔断测试**：模拟质检员不响应的情况，确认 `submit_and_wait` 在超时后能自动熔断并更新数据库。

> **注意**：本阶段专注于“阻塞与唤醒”逻辑，暂不涉及真正的 Docker 运行环境或运维监控。

