"""
AI-SyncForge V2 Git 工作区管理测试
覆盖特性分支生命周期、QA Worktree 隔离、工作区互斥锁、FATAL_LOCKED 升级。
遵循 TDD：本文件先于实现代码编写。
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path


def _run_git(*args, cwd):
    """辅助函数：执行 git 命令。"""
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, cwd=str(cwd),
    )
    return result


def _create_test_repo(base_dir: Path) -> Path:
    """创建一个带初始提交的临时 git 仓库。"""
    repo = base_dir / "test_repo"
    repo.mkdir(parents=True, exist_ok=True)
    _run_git("init", cwd=repo)
    _run_git("config", "user.email", "test@test.com", cwd=repo)
    _run_git("config", "user.name", "Test", cwd=repo)

    # 创建初始文件和提交
    (repo / "README.md").write_text("# Test Project\n")
    _run_git("add", ".", cwd=repo)
    _run_git("commit", "-m", "Initial commit", cwd=repo)

    # 确保在 main 分支
    result = _run_git("branch", "-M", "main", cwd=repo)
    return repo


# 延迟导入，在设置环境变量后
import git_workspace


# ─── WorkspaceLock 测试 ──────────────────────────────────────────────────────


class TestWorkspaceLock(unittest.TestCase):
    """测试工作区互斥锁。"""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.lock_path = self.tmp_dir / ".syncforge" / "workspace.lock"
        self.lock = git_workspace.WorkspaceLock(
            lock_path=self.lock_path, timeout=10
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_acquire_lock(self):
        """应能成功获取锁。"""
        result = self.lock.acquire("dev", 1)
        self.assertTrue(result)
        self.assertTrue(self.lock_path.exists())

    def test_release_lock(self):
        """获取后应能成功释放锁。"""
        self.lock.acquire("dev", 1)
        result = self.lock.release("dev")
        self.assertTrue(result)
        self.assertFalse(self.lock_path.exists())

    def test_lock_mutual_exclusion(self):
        """已锁时其他 owner 不应能获取。"""
        self.lock.acquire("dev", 1)
        result = self.lock.acquire("qa", 2)
        self.assertFalse(result)

    def test_same_owner_reentrant(self):
        """同一 owner 应能重入获取（更新 task_id）。"""
        self.lock.acquire("dev", 1)
        result = self.lock.acquire("dev", 2)
        self.assertTrue(result)

    def test_release_wrong_owner_fails(self):
        """不同 owner 不应能释放他人的锁。"""
        self.lock.acquire("dev", 1)
        result = self.lock.release("qa")
        self.assertFalse(result)
        # 锁应仍然存在
        self.assertTrue(self.lock_path.exists())

    def test_stale_lock_override(self):
        """超时的锁应被自动覆盖。"""
        stale_lock = git_workspace.WorkspaceLock(
            lock_path=self.lock_path, timeout=0
        )
        stale_lock.acquire("old_dev", 99)
        # 等待超时
        time.sleep(0.1)
        # 新请求应能覆盖
        result = self.lock.acquire("new_dev", 1)
        self.assertTrue(result)

    def test_status_returns_lock_info(self):
        """应能查询当前锁状态。"""
        self.lock.acquire("dev", 42)
        status = self.lock.status()
        self.assertIsNotNone(status)
        self.assertEqual(status["owner"], "dev")
        self.assertEqual(status["task_id"], 42)

    def test_status_returns_none_when_unlocked(self):
        """无锁时应返回 None。"""
        status = self.lock.status()
        self.assertIsNone(status)

    def test_fatal_locked_status(self):
        """FATAL_LOCKED 状态应阻止所有获取。"""
        self.lock.acquire_fatal("dev", 1, "合并冲突")
        # 任何人都不能获取
        result = self.lock.acquire("dev", 1)
        self.assertFalse(result)
        result = self.lock.acquire("ops", 1)
        self.assertFalse(result)
        # 只有 force_release 能释放
        status = self.lock.status()
        self.assertEqual(status["status"], "FATAL_LOCKED")

    def test_force_release_fatal(self):
        """force_release 应能解除 FATAL_LOCKED。"""
        self.lock.acquire_fatal("dev", 1, "冲突")
        result = self.lock.force_release()
        self.assertTrue(result)
        self.assertIsNone(self.lock.status())

    def test_concurrent_lock_safety(self):
        """并发获取锁应保证只有 1 个赢家。"""
        results = []
        lock = threading.Lock()

        def worker(owner):
            acquired = self.lock.acquire(owner, 1)
            if acquired:
                with lock:
                    results.append(owner)

        threads = [
            threading.Thread(target=worker, args=(f"worker_{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, "应只有 1 个线程成功获取锁")


# ─── 特性分支生命周期测试 ────────────────────────────────────────────────────


class TestFeatureBranch(unittest.TestCase):
    """测试特性分支的创建、回滚、合并。"""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.repo = _create_test_repo(self.tmp_dir)
        # 重定向 workspace root 到临时仓库
        self._orig_root = git_workspace.WORKSPACE_ROOT
        git_workspace.WORKSPACE_ROOT = self.repo

    def tearDown(self):
        git_workspace.WORKSPACE_ROOT = self._orig_root
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_feature_branch(self):
        """应能从 main 创建特性分支。"""
        result = git_workspace.create_feature_branch(42)
        self.assertTrue(result["success"])
        self.assertEqual(result["branch"], "task_42")

        # 验证当前分支
        branch = _run_git("branch", "--show-current", cwd=self.repo)
        self.assertEqual(branch.stdout.strip(), "task_42")

    def test_create_branch_from_main(self):
        """特性分支应基于 main 最新提交。"""
        # 在 main 上添加新提交
        (self.repo / "new_file.py").write_text("print('hello')\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Add new_file", cwd=self.repo)
        main_head = _run_git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()

        result = git_workspace.create_feature_branch(1)
        self.assertTrue(result["success"])

        # 分支的 HEAD 应与 main 一致
        branch_head = _run_git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        self.assertEqual(branch_head, main_head)

    def test_rollback_branch(self):
        """回滚应撤销所有未提交更改。"""
        git_workspace.create_feature_branch(1)

        # 修改文件
        (self.repo / "README.md").write_text("Modified content\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Bad change", cwd=self.repo)

        result = git_workspace.rollback_branch(1)
        self.assertTrue(result["success"])

    def test_merge_to_main_success(self):
        """无冲突合并应成功。"""
        git_workspace.create_feature_branch(1)

        # 在分支上修改
        (self.repo / "feature.py").write_text("print('feature')\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Add feature", cwd=self.repo)

        result = git_workspace.merge_to_main(1)
        self.assertTrue(result["success"])

        # 验证回到 main
        branch = _run_git("branch", "--show-current", cwd=self.repo)
        self.assertEqual(branch.stdout.strip(), "main")

        # 验证分支已删除
        branches = _run_git("branch", cwd=self.repo).stdout
        self.assertNotIn("task_1", branches)

        # 验证文件存在
        self.assertTrue((self.repo / "feature.py").exists())

    def test_merge_conflict_fatal_locked(self):
        """合并冲突应触发 FATAL_LOCKED。"""
        # 在 main 上先做修改
        (self.repo / "conflict.txt").write_text("main version\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Main change", cwd=self.repo)

        # 创建分支，回退到冲突前
        git_workspace.create_feature_branch(1)
        # 回退一步让分支历史分叉
        _run_git("reset", "--hard", "HEAD~1", cwd=self.repo)
        # 在分支上做不同的修改
        (self.repo / "conflict.txt").write_text("branch version\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Branch change", cwd=self.repo)

        result = git_workspace.merge_to_main(1)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "FATAL_LOCKED")

    def test_delete_branch(self):
        """应能删除特性分支。"""
        git_workspace.create_feature_branch(1)
        _run_git("checkout", "main", cwd=self.repo)

        result = git_workspace.delete_branch(1)
        self.assertTrue(result["success"])

        branches = _run_git("branch", cwd=self.repo).stdout
        self.assertNotIn("task_1", branches)


# ─── QA Worktree 隔离测试 ────────────────────────────────────────────────────


class TestQAWorktree(unittest.TestCase):
    """测试 QA 独立 Worktree 隔离。"""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.repo = _create_test_repo(self.tmp_dir)
        self._orig_root = git_workspace.WORKSPACE_ROOT
        git_workspace.WORKSPACE_ROOT = self.repo

    def tearDown(self):
        git_workspace.WORKSPACE_ROOT = self._orig_root
        # 先清理 worktrees 再删临时目录
        _run_git("worktree", "prune", cwd=self.repo)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_qa_worktree(self):
        """应能为 QA 创建独立 worktree。"""
        # 先创建分支
        git_workspace.create_feature_branch(1)
        # 切回 main 以便 worktree 使用 task_1
        _run_git("checkout", "main", cwd=self.repo)

        result = git_workspace.create_qa_worktree(1)
        self.assertTrue(result["success"])
        self.assertTrue(Path(result["worktree_path"]).exists())

    def test_worktree_isolates_changes(self):
        """Worktree 中的修改不应影响主工作区。"""
        git_workspace.create_feature_branch(1)

        # 在分支上添加文件
        (self.repo / "feature.py").write_text("v1\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Add feature", cwd=self.repo)

        _run_git("checkout", "main", cwd=self.repo)

        # 创建 worktree
        result = git_workspace.create_qa_worktree(1)
        wt_path = Path(result["worktree_path"])

        # Worktree 应有分支的文件
        self.assertTrue((wt_path / "feature.py").exists())
        # 主工作区不应有该文件（在 main 分支）
        self.assertFalse((self.repo / "feature.py").exists())

    def test_cleanup_qa_worktree(self):
        """应能清理 QA worktree。"""
        git_workspace.create_feature_branch(1)
        _run_git("checkout", "main", cwd=self.repo)

        result = git_workspace.create_qa_worktree(1)
        wt_path = Path(result["worktree_path"])
        self.assertTrue(wt_path.exists())

        cleanup = git_workspace.cleanup_qa_worktree(1)
        self.assertTrue(cleanup["success"])
        self.assertFalse(wt_path.exists())

    def test_qa_worktree_nonexistent_branch_fails(self):
        """不存在的分支应创建 worktree 失败。"""
        result = git_workspace.create_qa_worktree(999)
        self.assertFalse(result["success"])


# ─── 集成流程测试 ────────────────────────────────────────────────────────────


class TestIntegratedWorkflow(unittest.TestCase):
    """测试完整的 Dev-QA Git 隔离工作流。"""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.repo = _create_test_repo(self.tmp_dir)
        self._orig_root = git_workspace.WORKSPACE_ROOT
        git_workspace.WORKSPACE_ROOT = self.repo
        self.lock = git_workspace.WorkspaceLock(
            lock_path=self.repo / ".syncforge" / "workspace.lock",
            timeout=10,
        )

    def tearDown(self):
        git_workspace.WORKSPACE_ROOT = self._orig_root
        _run_git("worktree", "prune", cwd=self.repo)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_full_success_flow(self):
        """完整成功流：创建分支 → 开发 → QA worktree → 合并。"""
        # 1. Dev 获取任务，创建分支
        self.lock.acquire("dev", 1)
        branch_result = git_workspace.create_feature_branch(1)
        self.assertTrue(branch_result["success"])

        # 2. Dev 编写代码
        (self.repo / "app.py").write_text("def hello(): return 'world'\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Implement hello", cwd=self.repo)
        self.lock.release("dev")

        # 3. QA 创建 worktree 测试
        self.lock.acquire("qa", 1)
        _run_git("checkout", "main", cwd=self.repo)
        wt = git_workspace.create_qa_worktree(1)
        self.assertTrue(wt["success"])

        # QA 在独立目录验证
        wt_path = Path(wt["worktree_path"])
        self.assertTrue((wt_path / "app.py").exists())

        # 4. QA 通过，清理 worktree
        git_workspace.cleanup_qa_worktree(1)
        self.lock.release("qa")

        # 5. 合并回主干
        self.lock.acquire("dev", 1)
        merge_result = git_workspace.merge_to_main(1)
        self.assertTrue(merge_result["success"])
        self.lock.release("dev")

        # 验证 main 上有代码
        self.assertTrue((self.repo / "app.py").exists())

    def test_fail_and_retry_flow(self):
        """失败重试流：创建分支 → 失败 → 回滚 → 重试。"""
        # 1. 创建分支
        branch_result = git_workspace.create_feature_branch(1)
        self.assertTrue(branch_result["success"])

        # 2. 写入有 bug 的代码
        (self.repo / "buggy.py").write_text("raise Error\n")
        _run_git("add", ".", cwd=self.repo)
        _run_git("commit", "-m", "Buggy code", cwd=self.repo)

        # 3. QA 报错，回滚
        rollback = git_workspace.rollback_branch(1)
        self.assertTrue(rollback["success"])

        # 4. 验证回到分支创建时的状态
        # (注意：rollback 只回退到上一次 reset 的点，需要特定实现)

    def test_lock_prevents_concurrent_access(self):
        """锁应防止 QA 和 Dev 同时操作。"""
        # Dev 获取锁
        self.lock.acquire("dev", 1)

        # QA 不应能获取
        result = self.lock.acquire("qa", 1)
        self.assertFalse(result)

        # Dev 释放后 QA 应能获取
        self.lock.release("dev")
        result = self.lock.acquire("qa", 1)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
