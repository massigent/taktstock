#!/usr/bin/env python3
"""
Git Worktree Manager for Taktstock Orchestrator
-----------------------------------------------
Manages complete task isolation via git worktrees:
- Maintains a central copy of the repository (bare clone or working clone)
- Creates isolated temporary working folders on dedicated branches for each agent/task
- Supports parallel execution without file conflicts
- Manages automatic cleanup (remove/prune)
"""

import os
import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("TaktstockWorktrees")

class GitWorktreeManager:
    def __init__(self, base_repos_dir: Path, base_worktrees_dir: Path):
        self.repos_dir = Path(base_repos_dir)
        self.worktrees_dir = Path(base_worktrees_dir)
        try:
            self.repos_dir.mkdir(parents=True, exist_ok=True)
            self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug(f"Cannot create worktrees/repos dir: {e}")

    @staticmethod
    def is_worktree_dirty(worktree_path: Optional[Path]) -> bool:
        """
        Checks if the worktree contains uncommitted changes or untracked files
        using 'git status --porcelain -z'.
        In case of Git command error or indeterminable status, adopts fail-safe
        behavior and returns True to preserve files and prevent data loss.
        """
        if not worktree_path:
            return False
        p = Path(worktree_path)
        if not p.exists():
            return False
        # If not a Git directory or worktree, it is not a dirty Git worktree
        if not (p / ".git").exists() and not (p.parent / ".git").exists():
            return False
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain", "-z"],
                cwd=str(p),
                capture_output=True,
                check=False
            )
            if res.returncode == 0:
                raw = res.stdout.strip(b"\x00")
                return len(raw) > 0
            # Se git status fallisce (codice != 0), lo stato non è determinabile -> fail-safe: True
            logger.warning(f"git status non riuscito (codice {res.returncode}) su {p}. Preservazione prudenziale.")
            return True
        except Exception as e:
            logger.warning(f"Eccezione durante verifica dirty di {p}: {e}. Preservazione prudenziale.")
            return True

    @staticmethod
    def _run_git(cmd: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git"] + cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Git command failed ({' '.join(cmd)}): {e.stderr.strip()}")
            raise e

    def get_repo_name(self, repo_url: str) -> str:
        clean = repo_url.rstrip("/")
        if clean.endswith(".git"):
            clean = clean[:-4]
        return clean.split("/")[-1]

    def setup_main_repo(self, repo_url: str) -> Path:
        """Clona o aggiorna il repository principale nella cartella repos centralizzata, oppure usa il repo locale."""
        local_p = Path(repo_url).resolve()
        if local_p.exists() and local_p.is_dir() and (local_p / ".git").exists():
            logger.info(f"Uso del repository locale esistente: {local_p}")
            return local_p

        repo_name = self.get_repo_name(repo_url)
        repo_path = self.repos_dir / repo_name

        if not repo_path.exists():
            logger.info(f"Cloning master repository {repo_url} into {repo_path}...")
            self._run_git(["clone", repo_url, str(repo_path)])
        else:
            logger.info(f"Updating master repository in {repo_path}...")
            try:
                self._run_git(["fetch", "--prune", "origin"], cwd=repo_path)
            except Exception as e:
                logger.warning(f"Error during git fetch in {repo_path}: {e}")

        return repo_path

    def create_worktree(
        self,
        repo_path: Path,
        branch_name: str,
        base_branch: str = "main",
        worktree_id: Optional[str] = None
    ) -> Path:
        """
        Creates a new isolated git worktree on a dedicated branch.
        Returns the path of the isolated working directory.
        """
        w_id = worktree_id or f"wt_{int(datetime.now().timestamp())}_{branch_name.replace('/', '_')}"
        worktree_path = self.worktrees_dir / w_id

        # Ensure directory does not already exist
        if worktree_path.exists():
            logger.warning(f"Worktree path {worktree_path} already exists, preemptive removal...")
            self.remove_worktree(repo_path, worktree_path, force=True)

        logger.info(f"Creating Git Worktree in {worktree_path} on branch '{branch_name}' (from '{base_branch}')...")

        # Check if branch exists or create fresh with -B / -b
        try:
            # Try first with -B to create or reset branch from base_branch
            self._run_git(
                ["worktree", "add", "-B", branch_name, str(worktree_path), base_branch],
                cwd=repo_path
            )
        except Exception:
            # Fallback if base_branch is origin/<base_branch> or HEAD
            try:
                self._run_git(
                    ["worktree", "add", "-B", branch_name, str(worktree_path), f"origin/{base_branch}"],
                    cwd=repo_path
                )
            except Exception:
                # Fallback direct creation on current HEAD
                self._run_git(
                    ["worktree", "add", "-B", branch_name, str(worktree_path)],
                    cwd=repo_path
                )

        logger.info(f"Isolated worktree created successfully: {worktree_path}")
        return worktree_path

    def remove_worktree(self, repo_path: Path, worktree_path: Path, force: bool = True):
        """Removes a worktree and cleans up git metadata state."""
        logger.info(f"Removing worktree: {worktree_path}")
        try:
            cmd = ["worktree", "remove"]
            if force:
                cmd.append("--force")
            cmd.append(str(worktree_path))
            self._run_git(cmd, cwd=repo_path)
        except Exception as e:
            logger.warning(f"Git worktree removal failed: {e}. Manual directory removal...")
            if worktree_path.exists():
                import shutil
                shutil.rmtree(worktree_path, ignore_errors=True)

        try:
            self._run_git(["worktree", "prune"], cwd=repo_path)
        except Exception:
            pass

    def list_worktrees(self, repo_path: Path) -> List[Dict[str, str]]:
        """Lists all active worktrees for the specified repository."""
        if not repo_path.exists():
            return []
        try:
            proc = self._run_git(["worktree", "list", "--porcelain"], cwd=repo_path)
            lines = proc.stdout.strip().split("\n")
            worktrees = []
            curr: Dict[str, str] = {}
            for line in lines:
                if not line.strip():
                    if curr:
                        worktrees.append(curr)
                        curr = {}
                    continue
                if line.startswith("worktree "):
                    curr["worktree"] = line.split(" ", 1)[1]
                elif line.startswith("branch "):
                    curr["branch"] = line.split(" ", 1)[1]
                elif line.startswith("HEAD "):
                    curr["head"] = line.split(" ", 1)[1]
            if curr:
                worktrees.append(curr)
            return worktrees
        except Exception as e:
            logger.error(f"Error listing worktrees: {e}")
            return []
