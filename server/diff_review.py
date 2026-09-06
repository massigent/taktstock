#!/usr/bin/env python3
"""
Diff Review & Approval Manager for Taktstock Orchestrator
---------------------------------------------------------
Manages Git diff analysis, formatting, and visual approval:
- Generates formatted text summaries for Telegram/n8n (modified files, +lines, -lines)
- Creates standalone, self-contained, responsive dark-themed HTML reports
- Manages the 'WAITING_FOR_APPROVAL' state prior to git push
- Enables approval / rejection / fix requests via webhook or CLI
"""

import os
import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import html

logger = logging.getLogger("TaktstockDiffReview")

HTML_DIFF_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Taktstock Diff Review - {branch_name}</title>
    <style>
        :root {{
            --bg-main: #0d1117;
            --bg-card: #161b22;
            --border: #30363d;
            --text-main: #c9d1d9;
            --text-muted: #8b949e;
            --added-bg: rgba(46, 160, 67, 0.15);
            --added-text: #3fb950;
            --deleted-bg: rgba(248, 81, 73, 0.15);
            --deleted-text: #f85149;
            --accent: #58a6ff;
            --header-bg: #21262d;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-main);
            color: var(--text-main);
            line-height: 1.5;
            padding: 24px 16px;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        header {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 24px;
        }}
        .header-title {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }}
        h1 {{ font-size: 20px; color: #f0f6fc; display: flex; align-items: center; gap: 8px; }}
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            background: rgba(88, 166, 255, 0.15);
            color: var(--accent);
            border: 1px solid rgba(88, 166, 255, 0.3);
        }}
        .stats-bar {{
            display: flex;
            gap: 20px;
            margin-top: 14px;
            padding-top: 14px;
            border-top: 1px solid var(--border);
            font-size: 14px;
        }}
        .stat-added {{ color: var(--added-text); font-weight: 600; }}
        .stat-deleted {{ color: var(--deleted-text); font-weight: 600; }}
        .file-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 20px;
            overflow: hidden;
        }}
        .file-header {{
            background: var(--header-bg);
            padding: 10px 16px;
            font-family: monospace;
            font-size: 13px;
            font-weight: 600;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
        }}
        .diff-content {{
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
            font-size: 12px;
            line-height: 1.6;
            overflow-x: auto;
            white-space: pre;
            padding: 8px 0;
        }}
        .diff-line {{
            display: flex;
            padding: 1px 16px;
        }}
        .diff-line.added {{ background: var(--added-bg); color: var(--added-text); }}
        .diff-line.deleted {{ background: var(--deleted-bg); color: var(--deleted-text); }}
        .diff-line.meta {{ color: var(--accent); background: rgba(88, 166, 255, 0.05); }}
        .diff-line.normal {{ color: var(--text-main); }}
        .actions {{
            display: flex;
            gap: 12px;
            margin-top: 16px;
        }}
        .btn {{
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid transparent;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-approve {{ background: #238636; color: #ffffff; }}
        .btn-approve:hover {{ background: #2ea043; }}
        .btn-reject {{ background: #da3633; color: #ffffff; }}
        .btn-reject:hover {{ background: #f85149; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-title">
                <h1>📋 Visual Diff Review</h1>
                <span class="badge">Branch: {branch_name}</span>
            </div>
            <p style="margin-top: 8px; color: var(--text-muted); font-size: 14px;"><strong>Task:</strong> {task_description}</p>
            <div class="stats-bar">
                <div>📁 <strong>{files_count}</strong> files changed</div>
                <div class="stat-added">+{insertions} lines added</div>
                <div class="stat-deleted">-{deletions} lines removed</div>
                <div style="color: var(--text-muted);">🕒 Generated: {timestamp}</div>
            </div>
        </header>

        <div class="files-container">
            {files_html}
        </div>
    </div>
</body>
</html>
"""

class DiffReviewManager:
    def __init__(self, diffs_dir: Optional[Path] = None):
        default_home = os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME")
        base_dir = Path(default_home) if default_home else (Path.home() / "taktstock")
        self.diffs_dir = Path(diffs_dir) if diffs_dir else (base_dir / "diffs")
        try:
            self.diffs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug(f"Cannot create diffs_dir {self.diffs_dir}: {e}")

    @staticmethod
    def _run_git(cmd: List[str], cwd: Path) -> str:
        if not (cwd / ".git").exists() and not (cwd.parent / ".git").exists():
            return ""
        try:
            res = subprocess.run(
                ["git"] + cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=True
            )
            return res.stdout.strip()
        except (subprocess.CalledProcessError, Exception) as e:
            logger.debug(f"Git diff error ({cmd}): {e}")
            return ""

    def get_status_files(self, workspace_path: Path) -> Dict[str, List[str]]:
        """
        Reliably detects all modified, added, staged, and untracked files
        using 'git status --porcelain -z'.
        For rename and copy ('R' and 'C'), extracts and retains destination path.
        """
        if not (workspace_path / ".git").exists() and not (workspace_path.parent / ".git").exists():
            return {"modified": [], "staged": [], "untracked": [], "all": []}

        try:
            res = subprocess.run(
                ["git", "status", "--porcelain", "-z"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                check=True
            )
            raw = res.stdout
            modified = []
            staged = []
            untracked = []
            all_files = []

            # Splitting by null byte \0 for porcelain -z
            entries = raw.split("\0")
            i = 0
            while i < len(entries):
                entry = entries[i]
                if not entry:
                    i += 1
                    continue
                if len(entry) < 3:
                    i += 1
                    continue
                x = entry[0]
                y = entry[1]
                path_part = entry[3:]

                # In case of rename/copy (R or C), porcelain -z prints original path then null byte then new path
                if x in ("R", "C") or y in ("R", "C"):
                    if i + 1 < len(entries) and entries[i+1]:
                        # The subsequent item is the destination path
                        path_part = entries[i+1]
                        i += 1

                # Staged
                if x in ("M", "A", "R", "D", "C"):
                    if path_part not in staged:
                        staged.append(path_part)
                # Unstaged (modified or deleted)
                if y in ("M", "D"):
                    if path_part not in modified:
                        modified.append(path_part)
                # Untracked
                if x == "?" and y == "?":
                    if path_part not in untracked:
                        untracked.append(path_part)

                if path_part not in all_files:
                    all_files.append(path_part)

                i += 1

            return {
                "modified": modified,
                "staged": staged,
                "untracked": untracked,
                "all": all_files
            }
        except Exception as e:
            logger.debug(f"Error during get_status_files: {e}")
            return {"modified": [], "staged": [], "untracked": [], "all": []}

    def get_diff_data(self, workspace_path: Path, base_ref: Optional[str] = None) -> Dict[str, Any]:
        """Extracts raw diff, stat, and metrics from repository considering tracked, staged, and untracked files."""
        status_info = self.get_status_files(workspace_path)
        status_all_files = status_info.get("all", [])
        untracked_files = status_info.get("untracked", [])

        # 1. Diff uncommitted (unstaged)
        stat_cmd = ["diff", "--stat"]
        diff_cmd = ["diff"]

        if base_ref:
            stat_cmd = ["diff", base_ref, "--stat"]
            diff_cmd = ["diff", base_ref]

        raw_stat = self._run_git(stat_cmd, workspace_path)
        raw_diff = self._run_git(diff_cmd, workspace_path)

        # 2. Includi file staged se presenti (git diff --cached)
        cached_stat = self._run_git(["diff", "--cached", "--stat"], workspace_path)
        cached_diff = self._run_git(["diff", "--cached"], workspace_path)

        combined_stat = raw_stat
        if cached_stat.strip():
            if combined_stat.strip():
                combined_stat = combined_stat + "\n" + cached_stat
            else:
                combined_stat = cached_stat

        combined_diff = raw_diff
        if cached_diff.strip():
            if combined_diff.strip():
                combined_diff = combined_diff + "\n" + cached_diff
            else:
                combined_diff = cached_diff

        # Parsing file e statistiche da combined_stat
        files_changed = []
        insertions = 0
        deletions = 0

        for line in combined_stat.splitlines():
            line = line.strip()
            if "|" in line:
                fname = line.split("|")[0].strip()
                if fname not in files_changed:
                    files_changed.append(fname)
            elif "file changed" in line or "files changed" in line:
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if "insertion" in p:
                        try:
                            insertions += int(p.split()[0])
                        except ValueError:
                            pass
                    elif "deletion" in p:
                        try:
                            deletions += int(p.split()[0])
                        except ValueError:
                            pass

        # 3. Untracked files handling (without fabricating fake code contents in raw diff)
        if untracked_files:
            untracked_stat_lines = []
            for uf in untracked_files:
                uf_path = workspace_path / uf
                line_count = 0
                if uf_path.is_file():
                    try:
                        line_count = len(uf_path.read_text(encoding="utf-8", errors="replace").splitlines())
                    except Exception:
                        line_count = 0
                untracked_stat_lines.append(f" {uf} | {line_count} (untracked, content not included in git diff)")
                insertions += line_count

            if untracked_stat_lines:
                if combined_stat:
                    combined_stat += "\n" + "\n".join(untracked_stat_lines)
                else:
                    combined_stat = "\n".join(untracked_stat_lines)

        # Merge all files detected by status
        for f in status_all_files:
            if f not in files_changed:
                files_changed.append(f)

        has_changes = bool(status_all_files or combined_diff.strip() or files_changed)

        return {
            "stat": combined_stat,
            "raw_diff": combined_diff,
            "files_changed": files_changed,
            "untracked_files": untracked_files,
            "insertions": insertions,
            "deletions": deletions,
            "has_changes": has_changes
        }

    def generate_html_diff(
        self,
        workspace_path: Path,
        task_description: str,
        branch_name: str,
        output_filename: Optional[str] = None
    ) -> Optional[Path]:
        """Generates a standalone HTML file with annotated diff visualization."""
        data = self.get_diff_data(workspace_path)
        if not data["has_changes"]:
            logger.info("No changes detected to generate HTML diff.")
            return None

        raw_diff = data["raw_diff"]
        files_html = []

        # Group diff blocks by file
        file_diffs = raw_diff.split("diff --git ")
        for f_diff in file_diffs:
            if not f_diff.strip():
                continue
            lines = f_diff.splitlines()
            first_line = lines[0]
            # Extract file name (e.g. a/path b/path)
            file_name = first_line.split(" ")[0].replace("a/", "")

            lines_html = []
            for l in lines[1:]:
                escaped = html.escape(l)
                if l.startswith("+") and not l.startswith("+++"):
                    lines_html.append(f'<div class="diff-line added">{escaped}</div>')
                elif l.startswith("-") and not l.startswith("---"):
                    lines_html.append(f'<div class="diff-line deleted">{escaped}</div>')
                elif l.startswith("@@"):
                    lines_html.append(f'<div class="diff-line meta">{escaped}</div>')
                else:
                    lines_html.append(f'<div class="diff-line normal">{escaped}</div>')

            card = f"""
            <div class="file-card">
                <div class="file-header">
                    <span>📄 {html.escape(file_name)}</span>
                </div>
                <div class="diff-content">{''.join(lines_html)}</div>
            </div>
            """
            files_html.append(card)

        rendered_html = HTML_DIFF_TEMPLATE.format(
            branch_name=html.escape(branch_name),
            task_description=html.escape(task_description),
            files_count=len(data["files_changed"]),
            insertions=data["insertions"],
            deletions=data["deletions"],
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            files_html="".join(files_html)
        )

        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = output_filename or f"diff_{ts_str}_{branch_name.replace('/', '_')}.html"
        out_path = self.diffs_dir / out_name
        out_path.write_text(rendered_html, encoding="utf-8")
        logger.info(f"HTML Diff report generated successfully at: {out_path}")
        return out_path

    def format_telegram_summary(self, data: Dict[str, Any], task_description: str, branch_name: str) -> str:
        """Formats an elegant Markdown summary for Telegram notification."""
        if not data.get("has_changes"):
            return "ℹ️ *No changes detected in repository.*"

        files_list = "\n".join([f"  • `{f}`" for f in data["files_changed"][:10]])
        if len(data["files_changed"]) > 10:
            files_list += f"\n  • _...and {len(data['files_changed']) - 10} more files_"

        # Compact snippet of modified lines
        diff_snippet = "\n".join(data["raw_diff"].splitlines()[:15])
        if len(data["raw_diff"].splitlines()) > 15:
            diff_snippet += "\n..."

        msg = (
            f"🔍 *Visual Diff Review Available*\n\n"
            f"📌 *Task*: {task_description}\n"
            f"🌿 *Branch*: `{branch_name}`\n"
            f"📊 *Changes*: +{data['insertions']} / -{data['deletions']} lines across {len(data['files_changed'])} files\n\n"
            f"📁 *Affected files*:\n{files_list}\n\n"
            f"```diff\n{diff_snippet}\n```"
        )
        return msg
