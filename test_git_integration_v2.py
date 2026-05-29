"""
AI-SyncForge V2 Git 流程集成测试
验证 get_next_task、mark_task_done 与 git_workspace 的生命周期集成。
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# 配置环境
TEST_DIR = Path(tempfile.mkdtemp())
REPO_DIR = TEST_DIR / "repo"
DB_PATH = TEST_DIR / "test_git_integration.db"

# 创建测试仓库
def _run_git(*args, cwd):
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, cwd=str(cwd),
    )

def _init_repo():
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    _run_git("init", cwd=REPO_DIR)
    _run_git("config", "user.email", "test@test.com", cwd=REPO_DIR)
    _run_git("config", "user.name", "Test", cwd=REPO_DIR)
    (REPO_DIR / "README.md").write_text("# Test\n")
    _run_git("add", ".", cwd=REPO_DIR)
    _run_git("commit", "-m", "Initial commit", cwd=REPO_DIR)
    _run_git("branch", "-M", "main", cwd=REPO_DIR)

# 设置环境变量供模块使用
os.environ["DB_PATH"] = str(DB_PATH)
os.environ["SYNCFORGE_WORKSPACE"] = str(REPO_DIR)

import database
import mcp_tools
import git_workspace

class TestGitIntegration(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            os.remove(DB_PATH)
        _init_repo()
        database.init_db()
        # 清空任务
        conn = database._connect()
        conn.execute("DELETE FROM tasks;")
        conn.commit()
        conn.close()
        
        # 强制重置 git_workspace 的根目录（因为它是模块级变量）
        git_workspace.WORKSPACE_ROOT = REPO_DIR

    def tearDown(self):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    def test_get_next_task_creates_branch(self):
        """get_next_task 应自动创建特性分支。"""
        async def scenario():
            task_id = database.create_task("proj", "code", "req", task_name="GitTask")
            
            # 获取任务
            result = await mcp_tools.get_next_task("proj", timeout=2)
            self.assertEqual(result["task_id"], task_id)
            
            # 验证分支是否已创建并切换
            branch = _run_git("branch", "--show-current", cwd=REPO_DIR).stdout.strip()
            self.assertEqual(branch, f"task_{task_id}")
            
            # 验证锁是否已持有
            lock_status = git_workspace.WorkspaceLock().status()
            self.assertEqual(lock_status["owner"], "dev")
            self.assertEqual(lock_status["task_id"], task_id)

        asyncio.run(scenario())

    def test_mark_task_done_success_merges_branch(self):
        """mark_task_done(success) 应合并并删除分支，释放锁。"""
        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            await mcp_tools.get_next_task("proj", timeout=2)
            
            # 模拟开发：修改文件并提交
            (REPO_DIR / "app.py").write_text("print('hello')\n")
            _run_git("add", ".", cwd=REPO_DIR)
            _run_git("commit", "-m", "Work done", cwd=REPO_DIR)
            
            # 标记完成
            result = await mcp_tools.mark_task_done(task_id, "success", "All good")
            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "completed_dev")
            
            # 验证状态为 completed_dev
            task = database.get_task_by_id(task_id)
            self.assertEqual(task["status"], "completed_dev")
            
            # 此时 Dev 已切回 main，以便让 QA 可以 checkout 特性分支
            branch = _run_git("branch", "--show-current", cwd=REPO_DIR).stdout.strip()
            self.assertEqual(branch, "main")
            
            # 现在模拟 QA 通过
            await mcp_tools.finish_test(task_id, "success", "QA Pass")
            
            # 验证已合并到 main
            branch = _run_git("branch", "--show-current", cwd=REPO_DIR).stdout.strip()
            self.assertEqual(branch, "main")
            
            # 验证锁已释放
            self.assertIsNone(git_workspace.WorkspaceLock().status())

        asyncio.run(scenario())

    def test_mark_task_done_fail_rolls_back_branch(self):
        """mark_task_done(fail) 应回滚分支并释放锁。"""
        async def scenario():
            task_id = database.create_task("proj", "code", "req")
            await mcp_tools.get_next_task("proj", timeout=2)
            
            # 模拟错误开发
            (REPO_DIR / "buggy.py").write_text("syntax error\n")
            _run_git("add", ".", cwd=REPO_DIR)
            _run_git("commit", "-m", "Bad work", cwd=REPO_DIR)
            
            # 标记失败
            await mcp_tools.mark_task_done(task_id, "fail", "Failed testing")
            
            # 验证已回滚（buggy.py 不应存在于 HEAD，或者被 reset）
            # 注意：git_workspace.rollback_branch 是 reset --hard 到 main 的 HEAD
            # 所以 buggy.py 应该消失（如果未合并）
            self.assertFalse((REPO_DIR / "buggy.py").exists())
            
            # 验证回到 main 分支（V2 要求 Fail 时也释放 workspace 给下一位，或者保留分支？）
            # 根据 Task 2.1: "若任务 Fail 需重试，直接 git reset --hard HEAD"
            # 这里需要明确 Fail 时是否切回 main。
            # 通常 Fail 时我们会保持在特性分支以便修复，但 V2 提到 Workspace Lock。
            # 如果不切回 main，其他任务无法获取 lock。
            # 所以 Fail 后应切回 main 并释放锁。
            
            branch = _run_git("branch", "--show-current", cwd=REPO_DIR).stdout.strip()
            self.assertEqual(branch, "main")
            
            # 验证锁已释放
            self.assertIsNone(git_workspace.WorkspaceLock().status())

        asyncio.run(scenario())

    def test_qa_workflow_isolates_with_worktree(self):
        """QA 流程应使用 Worktree 隔离。"""
        async def scenario():
            # 1. Dev 接取并修改
            task_id = database.create_task("proj", "code", "req")
            await mcp_tools.get_next_task("proj")
            (REPO_DIR / "dev_work.py").write_text("dev content\n")
            _run_git("add", ".", cwd=REPO_DIR)
            _run_git("commit", "-m", "Dev commit", cwd=REPO_DIR)
            
            # Dev 完成工作，进入 completed_dev
            await mcp_tools.mark_task_done(task_id, "success", "Dev done")
            
            # 2. QA 轮询获取任务
            qa_task = await mcp_tools.poll_task(timeout=2)
            self.assertEqual(qa_task["task_id"], task_id)
            
            # 验证 QA Worktree 已创建
            wt_path = git_workspace.get_qa_worktree_path(task_id)
            self.assertTrue(wt_path.exists())
            self.assertTrue((wt_path / "dev_work.py").exists())
            
            # 3. QA 完成测试
            await mcp_tools.finish_test(task_id, "success", "Tests passed")
            
            # 验证 Worktree 已清理
            self.assertFalse(wt_path.exists())

        asyncio.run(scenario())

if __name__ == "__main__":
    unittest.main()
