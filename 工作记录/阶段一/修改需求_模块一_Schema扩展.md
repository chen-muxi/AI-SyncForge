# 修改需求：模块一 - 数据库 Schema 扩展

## 1. 修改背景
为了支持模块三的 Ops 应急响应机制，现有的 `tasks` 表结构无法区分普通开发任务与运维紧急任务，也缺乏优先级调度能力。

## 2. 具体修改项

### 2.1 表结构变更 (SQLite)
在 `tasks` 表中新增以下字段：
- `task_type`: `TEXT` 类型，默认值为 `'dev_test'`。
    - 说明：普通任务为 `'dev_test'`，运维任务为 `'ops_task'`。
- `priority`: `INTEGER` 类型，默认值为 `0`。
    - 说明：数值越高优先级越高，用于后续调度排序。

### 2.2 存储层代码更新 (`storage.py`)
- **`init_db` 函数**：更新建表语句，包含上述新增字段。
- **`get_pending_task` 函数**：
    - **SQL 调整**：增加类型过滤 `WHERE status = 'pending' AND task_type = 'dev_test'`。
    - **排序调整**：增加优先级排序 `ORDER BY priority DESC, created_at ASC`。
- **新增 `poll_ops_task` 函数 (或工具接口)**：
    - **职责**：专供 Ops-Forge 使用，具备原子锁定能力。
    - **SQL 逻辑**：
      ```sql
      UPDATE tasks SET status = 'testing', updated_at = CURRENT_TIMESTAMP
      WHERE id = (
          SELECT id FROM tasks 
          WHERE status = 'pending' AND task_type = 'ops_task' 
          ORDER BY priority DESC, created_at ASC LIMIT 1
      ) RETURNING *;
      ```

## 3. 验收标准
- [ ] 执行数据库初始化后，通过 `PRAGMA table_info(tasks)` 验证字段存在。
- [ ] 模拟插入一条 `ops_task` 和一条 `dev_test` 任务，验证 `get_pending_task` 仅能拉取到 `dev_test` 任务。
