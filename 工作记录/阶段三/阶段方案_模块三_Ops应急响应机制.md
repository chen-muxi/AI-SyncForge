# AI-SyncForge 阶段方案：模块三 - 核心应急响应与全链路打通 (最终版)

## 1. 模块定位
本模块是系统的“协同中枢”，负责在极端死锁情况下，通过“吹哨-接单-救援”机制实现系统自愈。

## 2. 核心架构设计 (职责解耦版)

### 2.1 吹哨人逻辑 (`ops_manager.py`)
- **职责**：仅负责异常判定与任务生成。
- **监控逻辑**：以 `asyncio` 协程运行在 Broker 主进程内，持续扫描 `status='testing'` 且 `updated_at` 超时 10 分钟的任务。
- **唯一动作**：在数据库中生成一条急救记录：
    - `task_type`: `'ops_task'`
    - `priority`: `999`
    - `code_content`: 记录被判定为死锁的任务 ID 及其容器元数据。

### 2.2 Ops 专属通道 (`poll_ops_task`)
- **职责**：为 Ops-Forge 智能体提供拉取入口。
- **工具实现**：新增 `poll_ops_task()` 工具。
- **查询逻辑**：采用原子锁语法，拉取瞬间即锁定状态：
  ```sql
  UPDATE tasks 
  SET status = 'testing', updated_at = CURRENT_TIMESTAMP
  WHERE id = (
      SELECT id FROM tasks 
      WHERE status = 'pending' AND task_type = 'ops_task' 
      ORDER BY priority DESC, created_at ASC 
      LIMIT 1
  ) 
  RETURNING *;
  ```
- **运行模式**：Ops-Forge 启动后进入长轮询状态，时刻待命。

### 2.3 运维执行者 (`manage_env`)
- **执行归属**：**完全由 Ops-Forge 调用**。
- **能力**：支持对故障容器的 `restart`, `cleanup`, `logs` 等操作。
- **反馈闭环**：Ops-Forge 处理完故障后，必须调用 `update_task_status` 传入 `fail_by_ops_intervention` 状态，触发内存 `asyncio.Event` 唤醒 Cursor。

## 3. 全链路闭环 Workflow (救援执行流)

1.  **死锁发生**：QA 任务卡死超时（10 分钟）。
2.  **吹哨**：`ops_manager.py` 扫描到异常，在数据库插入一条 `ops_task`。
3.  **接单**：一直处于 `poll_ops_task` 长轮询状态的 Ops-Forge 立即获取到该急救任务。
4.  **急救**：Ops-Forge 分析任务后，调用 `manage_env` 工具清理故障容器环境，并收集故障现场日志。
5.  **反馈唤醒**：Ops-Forge 清理完毕后，调用 `update_task_status`（传入 `fail_by_ops_intervention` 和故障日志路径）。此操作将触发 FastMCP 内存中的 `asyncio.Event`。
6.  **闭环**：阻塞中的 `submit_and_wait` 瞬间被唤醒，将故障反馈传递给 Cursor。

## 4. 验收标准
- [ ] **全自动自愈测试**：人为制造容器死锁，观察系统是否能在 11 分钟左右自动完成“吹哨-重置-唤醒-重试”全过程。
- [ ] **并发冲突测试**：验证在有多条 `dev_test` 和 `ops_task` 时，QA 与 Ops 智能体是否能精准各司其职，互不干扰。
