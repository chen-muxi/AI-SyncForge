"""
AI-SyncForge Git 工作区管理器
提供特性分支生命周期管理、QA Worktree 隔离、工作区互斥锁。

核心能力：
- WorkspaceLock: 文件级跨进程互斥锁，支持超时回收和 FATAL_LOCKED
- 特性分支: create / rollback / merge / delete
- QA Worktree: 物理隔离的测试目录
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 工作区根目录（可通过环境变量覆盖，测试时动态替换）
WORKSPACE_ROOT = Path(os.getenv("SYNCFORGE_WORKSPACE", Path.cwd()))


def _get_lock_path() -> Path:
    return WORKSPACE_ROOT / ".syncforge" / "workspace.lock"


def _get_qa_worktree_dir() -> Path:
    return WORKSPACE_ROOT / ".syncforge" / "qa_worktrees"


# ─── Git 命令执行器 ──────────────────────────────────────────────────────────


def run_git(*args, cwd=None) -> subprocess.CompletedProcess:
    """
    执行 git 命令。
    所有 git 操作统一通过此函数，便于日志追踪。
    """
    cmd = ["git"] + list(args)
    work_dir = str(cwd or WORKSPACE_ROOT)
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=work_dir,
        timeout=30,
    )
    if result.returncode != 0:
        logger.debug(
            f"Git command failed: {' '.join(cmd)}\n"
            f"  cwd={work_dir}\n"
            f"  stderr={result.stderr.strip()}"
        )
    return result


def get_main_branch() -> str:
    """检测主分支名称（main 或 master）。"""
    result = run_git("branch", "--list", "main")
    if "main" in result.stdout:
        return "main"
    result = run_git("branch", "--list", "master")
    if "master" in result.stdout:
        return "master"
    # 默认 main
    return "main"


# ─── WorkspaceLock 工作区互斥锁 ──────────────────────────────────────────────


class WorkspaceLock:
    """
    文件级工作区互斥锁。
    
    支持：
    - 跨进程互斥（Dev / QA 各自持有不同锁）
    - 同 owner 重入（更新 task_id）
    - 超时自动回收（stale lock detection）
    - FATAL_LOCKED 状态（合并冲突时锁死，只能 force_release）
    - 并发安全（内部 threading.Lock）
    """

    def __init__(
        self,
        lock_path: Path | None = None,
        timeout: int = 3600,
    ):
        self.lock_path = Path(lock_path) if lock_path else _get_lock_path()
        self.timeout = timeout
        self._lock = threading.Lock()

    def acquire(self, owner: str, task_id: int) -> bool:
        """
        尝试获取锁。
        
        - 无锁：直接获取
        - 同 owner：重入（更新 task_id）
        - 不同 owner 且未超时且进程存活：拒绝
        - 超时或进程死亡：覆盖
        - FATAL_LOCKED：拒绝（需 force_release）
        
        Returns:
            True 表示成功获取锁
        """
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)

            if self.lock_path.exists():
                try:
                    lock_data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    # 损坏的锁文件，覆盖
                    pass
                else:
                    # FATAL_LOCKED 不允许任何获取
                    if lock_data.get("status") == "FATAL_LOCKED":
                        logger.warning(
                            f"Workspace FATAL_LOCKED, cannot acquire. "
                            f"Reason: {lock_data.get('reason')}"
                        )
                        return False

                    # 同 owner 可重入
                    if lock_data.get("owner") == owner:
                        pass  # 允许覆盖
                    else:
                        # 检查是否超时
                        elapsed = time.time() - lock_data.get("timestamp", 0)
                        if elapsed <= self.timeout:
                            # 检查 PID 是否存活
                            pid = lock_data.get("pid")
                            if pid and _is_process_alive(pid):
                                return False
                            logger.warning(
                                f"Dead process lock detected (pid={pid}), overriding"
                            )
                        else:
                            logger.warning(
                                f"Stale lock detected "
                                f"(owner={lock_data.get('owner')}, "
                                f"elapsed={elapsed:.0f}s), overriding"
                            )

            # 写入锁
            lock_data = {
                "owner": owner,
                "task_id": task_id,
                "pid": os.getpid(),
                "timestamp": time.time(),
                "status": "LOCKED",
            }
            self.lock_path.write_text(
                json.dumps(lock_data, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info(f"Workspace lock acquired: owner={owner}, task_id={task_id}")
            return True

    def acquire_fatal(self, owner: str, task_id: int, reason: str) -> None:
        """
        设置 FATAL_LOCKED 状态。
        
        合并冲突等致命错误时调用。
        此状态下所有 acquire 请求均被拒绝，只能通过 force_release 解除。
        """
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_data = {
                "owner": owner,
                "task_id": task_id,
                "pid": os.getpid(),
                "timestamp": time.time(),
                "status": "FATAL_LOCKED",
                "reason": reason,
            }
            self.lock_path.write_text(
                json.dumps(lock_data, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.critical(
                f"FATAL_LOCKED: owner={owner}, task_id={task_id}, reason={reason}"
            )

    def release(self, owner: str) -> bool:
        """
        释放锁。
        
        只有锁的 owner 才能释放。
        FATAL_LOCKED 状态不允许普通释放。
        
        Returns:
            True 表示成功释放
        """
        with self._lock:
            if not self.lock_path.exists():
                return True
            try:
                lock_data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if lock_data.get("status") == "FATAL_LOCKED":
                    logger.warning("Cannot release FATAL_LOCKED lock, use force_release")
                    return False
                if lock_data.get("owner") == owner:
                    self.lock_path.unlink()
                    logger.info(f"Workspace lock released: owner={owner}")
                    return True
                return False
            except (json.JSONDecodeError, OSError):
                self.lock_path.unlink(missing_ok=True)
                return True

    def force_release(self) -> bool:
        """
        强制释放锁（含 FATAL_LOCKED）。
        
        仅供人类或 Ops 使用。
        
        Returns:
            True 表示成功释放
        """
        with self._lock:
            self.lock_path.unlink(missing_ok=True)
            logger.warning("Workspace lock FORCE RELEASED")
            return True

    def status(self) -> dict | None:
        """
        查询当前锁状态。
        
        Returns:
            锁信息字典，或 None（无锁）
        """
        if not self.lock_path.exists():
            return None
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


def _is_process_alive(pid: int) -> bool:
    """检查进程是否存活（跨平台）。"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ─── 特性分支生命周期 ────────────────────────────────────────────────────────


def create_feature_branch(task_id: int) -> dict:
    """
    从 main 创建并切换到特性分支 task_{id}。
    
    流程：
    1. 切换到 main 分支
    2. 拉取最新代码（若有远程）
    3. 创建 task_{id} 分支并切换
    
    Returns:
        {"success": bool, "branch": str, "message": str}
    """
    branch_name = f"task_{task_id}"
    main_branch = get_main_branch()

    # 切换到 main
    result = run_git("checkout", main_branch)
    if result.returncode != 0:
        return {
            "success": False,
            "branch": branch_name,
            "message": f"无法切换到 {main_branch}: {result.stderr.strip()}",
        }

    # 尝试拉取最新（静默失败，本地可能无远程）
    run_git("pull", "--rebase")

    # 创建并切换到特性分支
    result = run_git("checkout", "-b", branch_name)
    if result.returncode != 0:
        # 分支可能已存在，尝试直接切换
        result = run_git("checkout", branch_name)
        if result.returncode != 0:
            return {
                "success": False,
                "branch": branch_name,
                "message": f"无法创建/切换分支 {branch_name}: {result.stderr.strip()}",
            }
        logger.info(f"Switched to existing branch {branch_name}")
    else:
        logger.info(f"Created and switched to branch {branch_name}")

    return {
        "success": True,
        "branch": branch_name,
        "message": f"特性分支 {branch_name} 已创建。",
    }


def rollback_branch(task_id: int) -> dict:
    """
    回滚特性分支上的所有更改。
    
    使用 `git reset --hard` 回退到 main 基线。
    
    Returns:
        {"success": bool, "branch": str, "message": str}
    """
    branch_name = f"task_{task_id}"
    main_branch = get_main_branch()

    # 获取 main 分支的 HEAD
    main_head = run_git("rev-parse", main_branch)
    if main_head.returncode != 0:
        return {
            "success": False,
            "branch": branch_name,
            "message": f"无法获取 {main_branch} HEAD: {main_head.stderr.strip()}",
        }

    # 硬回退到 main 基线
    result = run_git("reset", "--hard", main_head.stdout.strip())
    if result.returncode != 0:
        return {
            "success": False,
            "branch": branch_name,
            "message": f"回滚失败: {result.stderr.strip()}",
        }

    # 清理未跟踪文件
    run_git("clean", "-fd")

    logger.info(f"Branch {branch_name} rolled back to {main_branch} baseline")
    return {
        "success": True,
        "branch": branch_name,
        "message": f"分支 {branch_name} 已回滚至 {main_branch} 基线。",
    }


def merge_to_main(task_id: int) -> dict:
    """
    将特性分支合并回主干。
    
    流程：
    1. 切换到 main
    2. 执行 --no-ff 合并
    3. 若冲突：abort + 设置 FATAL_LOCKED
    4. 成功后删除特性分支
    
    Returns:
        {"success": bool, "branch": str, "status": str, "message": str}
    """
    branch_name = f"task_{task_id}"
    main_branch = get_main_branch()

    # 切换到 main
    result = run_git("checkout", main_branch)
    if result.returncode != 0:
        return {
            "success": False,
            "branch": branch_name,
            "status": "FATAL_LOCKED",
            "message": f"无法切换到 {main_branch}: {result.stderr.strip()}",
        }

    # 执行合并
    result = run_git(
        "merge", branch_name, "--no-ff",
        "-m", f"Merge {branch_name}: task {task_id} completed",
    )
    if result.returncode != 0:
        # 合并冲突！
        run_git("merge", "--abort")
        logger.critical(
            f"MERGE CONFLICT: {branch_name} → {main_branch}\n"
            f"  stderr: {result.stderr.strip()}"
        )
        return {
            "success": False,
            "branch": branch_name,
            "status": "FATAL_LOCKED",
            "message": (
                f"合并冲突！分支 {branch_name} 无法自动合并到 {main_branch}。"
                f"需要人工介入。\n{result.stderr.strip()}"
            ),
        }

    # 删除已合并的特性分支
    run_git("branch", "-d", branch_name)
    logger.info(f"Branch {branch_name} merged to {main_branch} and deleted")

    return {
        "success": True,
        "branch": branch_name,
        "status": "merged",
        "message": f"分支 {branch_name} 已合并到 {main_branch}，特性分支已删除。",
    }


def delete_branch(task_id: int) -> dict:
    """
    删除特性分支（不合并）。
    
    Returns:
        {"success": bool, "branch": str, "message": str}
    """
    branch_name = f"task_{task_id}"

    result = run_git("branch", "-D", branch_name)
    if result.returncode != 0:
        return {
            "success": False,
            "branch": branch_name,
            "message": f"删除失败: {result.stderr.strip()}",
        }

    logger.info(f"Branch {branch_name} deleted")
    return {
        "success": True,
        "branch": branch_name,
        "message": f"分支 {branch_name} 已删除。",
    }


# ─── QA Worktree 隔离 ────────────────────────────────────────────────────────


def create_qa_worktree(task_id: int, branch_name: str | None = None) -> dict:
    """
    为 QA 创建独立的 git worktree。
    
    Worktree 位于 .syncforge/qa_worktrees/qa_{task_id}。
    QA 在此目录运行测试，完全不影响主工作区。
    
    Args:
        task_id: 任务 ID
        branch_name: 分支名（默认 task_{id}）
    
    Returns:
        {"success": bool, "worktree_path": str, "branch": str, "message": str}
    """
    if branch_name is None:
        branch_name = f"task_{task_id}"

    qa_dir = _get_qa_worktree_dir()
    worktree_path = qa_dir / f"qa_{task_id}"
    qa_dir.mkdir(parents=True, exist_ok=True)

    # 检查分支是否存在
    check = run_git("branch", "--list", branch_name)
    if branch_name not in check.stdout:
        return {
            "success": False,
            "worktree_path": str(worktree_path),
            "branch": branch_name,
            "message": f"分支 {branch_name} 不存在。",
        }

    # 创建 worktree
    result = run_git("worktree", "add", str(worktree_path), branch_name)
    if result.returncode != 0:
        return {
            "success": False,
            "worktree_path": str(worktree_path),
            "branch": branch_name,
            "message": f"Worktree 创建失败: {result.stderr.strip()}",
        }

    logger.info(f"QA worktree created at {worktree_path} for branch {branch_name}")
    return {
        "success": True,
        "worktree_path": str(worktree_path),
        "branch": branch_name,
        "message": f"QA worktree 已创建于 {worktree_path}。",
    }


def cleanup_qa_worktree(task_id: int) -> dict:
    """
    清理 QA worktree。
    
    Args:
        task_id: 任务 ID
    
    Returns:
        {"success": bool, "message": str}
    """
    worktree_path = _get_qa_worktree_dir() / f"qa_{task_id}"

    result = run_git("worktree", "remove", str(worktree_path), "--force")
    if result.returncode != 0:
        # 手动清理
        import shutil
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        run_git("worktree", "prune")
        logger.warning(f"Worktree force-cleaned: {worktree_path}")

    logger.info(f"QA worktree qa_{task_id} cleaned up")
    return {
        "success": True,
        "message": f"Worktree qa_{task_id} 已清理。",
    }


def get_qa_worktree_path(task_id: int) -> Path:
    """获取 QA worktree 的物理路径。"""
    return _get_qa_worktree_dir() / f"qa_{task_id}"
