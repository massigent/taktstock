#!/usr/bin/env python3
"""
Taktstock Projects Manager
--------------------------
Manages discovery, scanning, analysis, and safe inspection of projects
present in the central projects directory (default: ~/progetti or TAKTSTOCK_PROJECTS_DIR).

Features:
- Automatic detection of Git repositories, branch, technology stack, and descriptions
- Transparent alias and symlink handling (e.g. assistente -> Assistente)
- Compact directory tree generation for LLM agent context
- Read-only file inspection with strict sandboxing (no access outside project folder)
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("TaktstockProjectsManager")

# Base projects path
def get_base_progetti_dir() -> Path:
    env_dir = os.environ.get("TAKTSTOCK_PROJECTS_DIR") or os.environ.get("UFFICIO_PROJECTS_DIR") or os.environ.get("PROGETTI_DIR")
    if env_dir and Path(env_dir).exists():
        return Path(env_dir).resolve()
    
    home = Path.home()
    real_home = home.parent if home.name.startswith(".") else home

    for candidate in [
        home / "progetti",
        real_home / "progetti",
        real_home / "Documents" / "GitHub",
        home / "Documents" / "GitHub",
        Path("/app/repos"),
        Path("/workspace/repos"),
    ]:
        if candidate.exists():
            return candidate.resolve()
        
    return Path.cwd().resolve()

IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", "__pycache__", ".DS_Store",
    ".wrangler", "backups", ".claude", ".gemini", ".next", ".astro"
}

IGNORED_FILES = {
    ".DS_Store", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"
}

def detect_tech_stack(project_dir: Path, files: List[str]) -> str:
    """Automatically detects project technology stack by analyzing files."""
    tech = []
    
    # 1. Node / JavaScript / TypeScript / Frontend
    pkg_file = project_dir / "package.json"
    if pkg_file.exists():
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8", errors="ignore"))
            deps = dict(pkg.get("dependencies", {}))
            deps.update(pkg.get("devDependencies", {}))
            if "astro" in deps:
                tech.append("Astro")
            elif "react" in deps:
                tech.append("React")
            elif "vue" in deps:
                tech.append("Vue")
            elif "next" in deps:
                tech.append("Next.js")
                
            if "vite" in deps:
                tech.append("Vite")
            if "typescript" in deps:
                tech.append("TypeScript")
            elif "javascript" not in [t.lower() for t in tech] and not tech:
                tech.append("Node.js")
        except Exception:
            tech.append("Node.js")
            
    # 2. PHP / WordPress
    if (project_dir / "functions.php").exists() or (project_dir / "style.css").exists() or any(f.endswith(".php") for f in files):
        tech.append("WordPress / PHP")
        
    # 3. Python
    if any(f.endswith(".py") for f in files) or (project_dir / "requirements.txt").exists() or (project_dir / "pyproject.toml").exists():
        tech.append("Python")

    # 4. Wiki / Knowledge Base
    if project_dir.name.lower() in ["wikillm", "wiki", "metodogentili"] or (project_dir / "Obsidian Vault").is_dir():
        return "Knowledge Base / Universal Skills"

    # 5. n8n / Database / SQL / MiniApp
    if any(f.endswith(".sql") for f in files) or any("MiniApp" in f or "Cron" in f or "Diario" in f for f in files):
        tech.append("n8n Workflows / PostgreSQL / Cloudflare")
        
    # 6. Docker / DevOps
    if (project_dir / "docker-compose.yml").exists() or (project_dir / "Dockerfile").exists():
        tech.append("Docker")
        
    # 7. Documentation / Config
    if not tech or all(f.endswith(".md") or f.endswith(".txt") for f in files if not (project_dir / f).is_dir()):
        tech.append("Documentation / Config")
        
    return ", ".join(tech) if tech else "Generic Project"

def extract_project_description(project_dir: Path) -> str:
    """Extracts short readable project description from docs or manifest."""
    # Try package.json
    pkg_file = project_dir / "package.json"
    if pkg_file.exists():
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8", errors="ignore"))
            desc = pkg.get("description", "").strip()
            if desc:
                return desc
        except Exception:
            pass
            
    # Try prioritized documentation files
    doc_candidates = [
        "README.md", "AGENTS.md", "CLAUDE.md", "PROJECT_GUIDE.md",
        "SYSTEM_ARCHITECTURE.md", "HANDOFF.md"
    ]
    for doc_name in doc_candidates:
        doc_path = project_dir / doc_name
        if doc_path.exists():
            try:
                lines = doc_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line in lines:
                    clean = line.strip().lstrip("#").strip()
                    if clean and not clean.startswith("!") and not clean.startswith(">") and len(clean) > 5:
                        return clean[:120]
            except Exception:
                pass
                
    return "Working repository"

def get_git_info(project_dir: Path) -> Dict[str, Any]:
    """Retrieves basic Git repository info if present."""
    git_dir = project_dir / ".git"
    if not git_dir.exists():
        return {"is_git": False, "branch": "", "remote": ""}
        
    branch = "main"
    remote = ""
    try:
        # Leggi branch corrente da .git/HEAD
        head_file = git_dir / "HEAD"
        if head_file.exists():
            head_content = head_file.read_text(encoding="utf-8", errors="ignore").strip()
            if head_content.startswith("ref: refs/heads/"):
                branch = head_content.replace("ref: refs/heads/", "")
                
        # Leggi remote url da .git/config
        config_file = git_dir / "config"
        if config_file.exists():
            conf_lines = config_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            for i, line in enumerate(conf_lines):
                if '[remote "origin"]' in line:
                    for sub in conf_lines[i+1:i+6]:
                        if "url =" in sub:
                            remote = sub.split("=", 1)[1].strip()
                            break
    except Exception as e:
        logger.debug(f"Errore lettura git info per {project_dir}: {e}")
        
    return {"is_git": True, "branch": branch, "remote": remote}

class ProjectsManager:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or get_base_progetti_dir()

    def list_projects(self) -> List[Dict[str, Any]]:
        """Scansiona e restituisce tutti i progetti disponibili raggruppando symlink e alias."""
        if not self.base_dir.exists():
            return []
            
        projects_by_real_path: Dict[str, Dict[str, Any]] = {}
        aliases_map: Dict[str, List[str]] = {}
        
        for item in sorted(self.base_dir.iterdir()):
            if item.name.startswith(".") or item.name in IGNORED_DIRS:
                continue
                
            real_path = item.resolve()
            if not real_path.is_dir():
                continue
                
            real_str = str(real_path)
            if item.is_symlink():
                aliases_map.setdefault(real_str, []).append(item.name)
            else:
                if real_str not in projects_by_real_path:
                    files = [f.name for f in item.iterdir() if not f.name.startswith(".")]
                    tech = detect_tech_stack(item, files)
                    desc = extract_project_description(item)
                    git_info = get_git_info(item)
                    
                    # File principali top-level
                    key_files = sorted([f for f in files if f not in IGNORED_FILES])[:15]
                    
                    projects_by_real_path[real_str] = {
                        "name": item.name,
                        "path": real_str,
                        "aliases": [item.name.lower()],
                        "tech_stack": tech,
                        "description": desc,
                        "is_git": git_info["is_git"],
                        "git_branch": git_info["branch"],
                        "git_remote": git_info["remote"],
                        "key_files": key_files,
                    }
                    
        # Collega symlink come alias
        for real_str, sym_aliases in aliases_map.items():
            if real_str in projects_by_real_path:
                for a in sym_aliases:
                    if a.lower() not in projects_by_real_path[real_str]["aliases"]:
                        projects_by_real_path[real_str]["aliases"].append(a.lower())
                        
        result = list(projects_by_real_path.values())
        result.sort(key=lambda x: x["name"].lower())
        return result

    def find_project(self, query: str) -> Optional[Dict[str, Any]]:
        """Trova un progetto per nome esatto, alias, o corrispondenza parziale."""
        if not query:
            return None
            
        clean_q = query.strip().lower()
        # Rimuovi prefissi comuni come 'repo:', 'progetto:', ecc.
        clean_q = clean_q.replace("repo:", "").replace("progetto:", "").strip()
        if clean_q.startswith("/"):
            clean_q = Path(clean_q).name.lower()
            
        projects = self.list_projects()
        
        # 1. Corrispondenza esatta su nome o alias
        for p in projects:
            if p["name"].lower() == clean_q or clean_q in [a.lower() for a in p.get("aliases", [])]:
                return p
                
        # 2. Corrispondenza su prefisso
        for p in projects:
            if p["name"].lower().startswith(clean_q):
                return p
                
        # 3. Corrispondenza come sottostringa
        for p in projects:
            if clean_q in p["name"].lower() or any(clean_q in a.lower() for a in p.get("aliases", [])):
                return p
                
        return None

    def get_project_tree(self, project_path: str, max_depth: int = 2, max_entries: int = 35) -> str:
        """Constructs a compact tree view of project directories and files."""
        p = Path(project_path).resolve()
        if not p.exists() or not p.is_dir():
            return "Folder not accessible or non-existent."
            
        lines = [f"📂 {p.name}/"]
        count = 0
        
        def _walk(current: Path, prefix: str, depth: int):
            nonlocal count
            if depth > max_depth or count >= max_entries:
                return
                
            try:
                entries = sorted(list(current.iterdir()), key=lambda x: (not x.is_dir(), x.name.lower()))
            except Exception:
                return
                
            visible_entries = [e for e in entries if not e.name.startswith(".") and e.name not in IGNORED_DIRS]
            
            for i, entry in enumerate(visible_entries):
                if count >= max_entries:
                    lines.append(f"{prefix}└── ... (additional files omitted for brevity)")
                    break
                    
                is_last = (i == len(visible_entries) - 1)
                connector = "└── " if is_last else "├── "
                sub_prefix = "    " if is_last else "│   "
                
                if entry.is_dir():
                    lines.append(f"{prefix}{connector}📁 {entry.name}/")
                    count += 1
                    _walk(entry, prefix + sub_prefix, depth + 1)
                else:
                    lines.append(f"{prefix}{connector}📄 {entry.name}")
                    count += 1
                    
        _walk(p, "", 1)
        return "\n".join(lines)

    def get_project_doc_snippet(self, project_path: str, max_chars: int = 1500) -> str:
        """Extracts a significant excerpt from the project's main documentation."""
        p = Path(project_path).resolve()
        doc_candidates = [
            "AGENTS.md", "README.md", "PROJECT_GUIDE.md", "CLAUDE.md",
            "SYSTEM_ARCHITECTURE.md", "MEMORY.md"
        ]
        for name in doc_candidates:
            doc_f = p / name
            if doc_f.exists() and doc_f.is_file():
                try:
                    content = doc_f.read_text(encoding="utf-8", errors="ignore").strip()
                    if content:
                        header = f"--- Excerpt from {name} ---"
                        snippet = content[:max_chars]
                        if len(content) > max_chars:
                            snippet += "\n... [continued in original file]"
                        return f"{header}\n{snippet}"
                except Exception:
                    pass
        return "No Markdown documentation found in root folder."

    def safe_read_file(self, project_path: str, rel_file_path: str, max_chars: int = 4000) -> Optional[str]:
        """
        Safely reads a file within the project, strictly verifying that
        the target path does not escape the project directory (Path Traversal Protection).
        """
        proj_root = Path(project_path).resolve()
        if not proj_root.exists() or not proj_root.is_dir():
            return None
            
        target = (proj_root / rel_file_path.lstrip("/")).resolve()
        
        # Strict sandbox check: the target file MUST be a descendant of proj_root
        try:
            target.relative_to(proj_root)
        except ValueError:
            logger.warning(f"Path Traversal attempt blocked: {rel_file_path} outside {proj_root}")
            return "⛔ Access denied: the requested file is located outside the authorized project directory."
            
        if not target.exists() or not target.is_file():
            return f"⚠️ File '{rel_file_path}' not found in project."
            
        try:
            content = target.read_text(encoding="utf-8", errors="ignore")
            if len(content) > max_chars:
                return content[:max_chars] + f"\n\n... [File truncated at {max_chars} characters. Use a subtask if full file is needed]"
            return content
        except Exception as e:
            return f"Error reading file: {e}"

    def format_projects_telegram_menu(self) -> str:
        """Formats the list of available projects for immediate dispatch to Telegram."""
        projects = self.list_projects()
        if not projects:
            return f"⚠️ No projects found in directory `{self.base_dir}`."
            
        out = "🎯 *AVAILABLE PROJECTS IN TAKTSTOCK*\n"
        out += "Here is the list of projects configured on the server:\n\n"
        
        for p in projects:
            name = p["name"]
            tech = p["tech_stack"]
            desc = p["description"]
            aliases = ", ".join(p.get("aliases", []))
            
            icon = "📁"
            if "astro" in tech.lower(): icon = "🚀"
            elif "react" in tech.lower() or "vite" in tech.lower(): icon = "📊"
            elif "wordpress" in tech.lower() or "php" in tech.lower(): icon = "👤"
            elif "n8n" in tech.lower() or "sql" in tech.lower(): icon = "🤖"
            elif "docs" in tech.lower() or "config" in tech.lower(): icon = "🏠"
            
            out += f"{icon} *{name}*\n"
            out += f"  • *Stack:* `{tech}`\n"
            out += f"  • *Description:* _{desc}_\n"
            if len(p.get("aliases", [])) > 1:
                out += f"  • *Aliases:* `{aliases}`\n"
            out += "\n"
            
        out += "💬 *How to select a project:*\n"
        out += "Simply type in chat:\n"
        out += "👉 `work on <project_name>`\n"
        out += "or use the command: `/project <project_name>`"
        return out
