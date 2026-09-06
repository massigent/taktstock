"""
skills_manager.py — Universal Skills Manager for Taktstock Multi-Agent
Manages the catalog, automatic detection, and injection of Universal Skills
(Training, Nutrition, Marketing, Web Design, n8n Workflows, OWASP Security, Antigravity CLI)
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("SkillsManager")

BASE_DIR = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or os.environ.get("ORCH_HOME") or (Path.home() / "taktstock"))
SERVER_DIR = Path(__file__).resolve().parent
PROGETTI_DIR = Path(os.environ.get("PROGETTI_DIR") or (Path.home() / "progetti"))
if not PROGETTI_DIR.exists():
    for candidate in [Path.home() / "Documents" / "GitHub", Path.home() / "projects"]:
        if candidate.exists():
            PROGETTI_DIR = candidate
            break


def parse_frontmatter(content: str) -> Dict[str, str]:
    """Extracts simple YAML frontmatter metadata from a SKILL.md file."""
    meta = {}
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta


class SkillsManager:
    def __init__(self, skills_dir: Optional[Path] = None):
        self.skills_dir = skills_dir or (BASE_DIR / "skills")
        if not self.skills_dir.exists():
            local_s = SERVER_DIR.parent / "skills"
            if local_s.exists():
                self.skills_dir = local_s

        self.wiki_skills_dir = PROGETTI_DIR / "WikiLLM" / ".agents" / "skills"
        if not self.wiki_skills_dir.exists():
            local_wiki = Path(os.environ.get("WIKI_SKILLS_DIR") or (Path.home() / "Documents" / "WikiLLM" / ".agents" / "skills"))
            if local_wiki.exists():
                self.wiki_skills_dir = local_wiki

    def list_skills(self) -> List[Dict[str, Any]]:
        """Returns a list of all available Universal Skills with metadata and paths."""
        skills = {}

        # Search in central skills directory and WikiLLM
        search_dirs = [self.skills_dir, self.wiki_skills_dir]
        for s_dir in search_dirs:
            if not s_dir or not s_dir.exists():
                continue
            for item in s_dir.iterdir():
                if item.is_dir():
                    skill_file = item / "SKILL.md"
                    if not skill_file.exists():
                        skill_file = item / "skill.md"
                    if skill_file.exists():
                        s_id = item.name.lower()
                        if s_id not in skills:
                            skills[s_id] = self._load_skill_file(skill_file, s_id)

        icon_map = {
            "esperto-allenamento": "🏋️‍♂️",
            "esperto-nutrizione": "🥗",
            "esperto-marketing": "📈",
            "esperto-web-design": "🎨",
            "n8n-workflows": "⚙️",
            "n8n-workflow-patterns": "⚙️",
            "security-owasp": "🛡️",
            "esperto-security-owasp": "🛡️",
            "esperto-antigravity-cli": "🚀",
            "tavily-search": "🔍",
            "tavily-dynamic-search": "⚡",
            "tavily-extract": "📄",
            "tavily-map": "🗺️",
            "tavily-crawl": "🕸️",
            "tavily-research": "🔬",
            "tavily-cli": "🛠️",
            "tavily-best-practices": "🏗️"
        }

        triggers_map = {
            "esperto-allenamento": [
                "training", "calisthenics", "strength", "tendon", "mobility", "workout", "hypertrophy",
                "shoulder", "knee", "elbow", "allenamento", "forza", "tendin", "tnt", "mobilit", "scheda", "ipertrofia"
            ],
            "esperto-nutrizione": [
                "nutrition", "metabolism", "diet", "meal plan", "bioenergetics", "thyroid", "pufa", "sugar",
                "insulin", "supplement", "cholesterol", "nutrizion", "metabolism", "dieta", "piani alimentar",
                "bioenergetic", "tiroide", "zuccher", "insulino", "integrator", "colesterolo"
            ],
            "esperto-marketing": [
                "marketing", "copywriting", "kennedy", "cialdini", "offer", "positioning", "landing page",
                "conversion", "funnel", "sales", "avatar", "offerta", "posizionament", "vendita"
            ],
            "esperto-web-design": [
                "web design", "frontend", "astro", "tailwind", "ux", "ui", "lcp", "cls", "accessibility",
                "motion", "design", "accessibilit"
            ],
            "n8n-workflows": [
                "n8n", "workflow", "webhook", "node", "automation", "postgresql", "cron", "trigger", "automazion"
            ],
            "security-owasp": [
                "security", "owasp", "injection", "traversal", "sanitize", "sanitization", "gdpr", "privacy",
                "vulnerability", "sicurezza", "sanitizz", "vulnerabilit"
            ],
            "esperto-antigravity-cli": [
                "code", "worktree", "refactor", "build", "linter", "test", "git branch", "debug", "codice"
            ],
            "tavily-search": [
                "tavily", "web search", "online search", "search internet", "search engine", "news",
                "ricerca web", "cerca online", "cerca su internet", "motore di ricerca", "notizie"
            ],
            "tavily-dynamic-search": [
                "dynamic search", "filter web", "curated search", "ricerca dinamica", "filtra web"
            ],
            "tavily-extract": [
                "extract", "read page", "read url", "download page", "scraping", "estrai", "leggi pagina", "leggi url", "scarica pagina"
            ],
            "tavily-map": [
                "site map", "map", "site structure", "sitemap", "url list", "mappa sito", "struttura sito", "lista url"
            ],
            "tavily-crawl": [
                "crawl", "download docs", "download documentation", "bulk extract", "crawling", "scarica docs", "scarica documentazione"
            ],
            "tavily-research": [
                "deep research", "research report", "market study", "competitive analysis",
                "ricerca approfondita", "report di ricerca", "indagine di mercato", "analisi competitiva"
            ],
            "tavily-cli": ["tvly", "tavily cli", "tavily command", "comando tavily"],
            "tavily-best-practices": ["tavily python", "tavily sdk", "integrate tavily", "tavily api", "integra tavily"]
        }

        result = []
        for s_id, s_data in skills.items():
            s_data["icon"] = icon_map.get(s_id, "🧰")
            s_data["triggers"] = triggers_map.get(s_id, [s_id])
            result.append(s_data)

        return sorted(result, key=lambda x: x["id"])

    def _load_skill_file(self, skill_path: Path, skill_id: str) -> Dict[str, Any]:
        try:
            content = skill_path.read_text(encoding="utf-8", errors="ignore")
            fm = parse_frontmatter(content)
            
            body = content
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2].strip()

            name = fm.get("name", skill_id)
            desc = fm.get("description", "")
            if not desc:
                lines = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
                desc = lines[0] if lines else "Taktstock Universal Skill"

            return {
                "id": skill_id,
                "name": name,
                "description": desc,
                "path": str(skill_path.resolve()),
                "content": body,
                "raw_content": content
            }
        except Exception as e:
            logger.error(f"Error loading skill {skill_path}: {e}")
            return {
                "id": skill_id,
                "name": skill_id,
                "description": "Universal Skill",
                "path": str(skill_path.resolve()),
                "content": "",
                "raw_content": ""
            }

    def get_skill(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """Finds a skill by ID or fuzzy name matching."""
        all_s = self.list_skills()
        target = skill_id.strip().lower().replace("_", "-")
        for s in all_s:
            if s["id"] == target or s["name"].lower() == target or target in s["id"]:
                return s
        return None

    def detect_relevant_skills(self, query_text: str) -> List[Dict[str, Any]]:
        """Identifies relevant skills based on user input or task description."""
        query_low = query_text.lower()
        all_s = self.list_skills()
        matched = []

        for s in all_s:
            triggers = s.get("triggers", [])
            if any(t in query_low for t in triggers):
                matched.append(s)
            elif s["id"] in query_low or s["name"].lower() in query_low:
                matched.append(s)

        return matched

    def format_skills_telegram_menu(self) -> str:
        """Generates the Skills catalog formatted for Telegram."""
        skills = self.list_skills()
        if not skills:
            return "🧰 *No Universal Skills configured.*"

        lines = ["🧰 *TAKTSTOCK MULTI-AGENT UNIVERSAL SKILLS*"]
        lines.append("All 7 agents automatically inherit and apply these capabilities:\n")

        for s in skills:
            trig_sample = ", ".join(s.get("triggers", [])[:4])
            lines.append(f"{s['icon']} *{s['id']}*")
            lines.append(f"  • *Focus:* _{s['description'][:130]}..._")
            lines.append(f"  • *Trigger:* `{trig_sample}`\n")

        lines.append("💡 *How to use skills:* Agents apply them automatically when they detect relevant topics. You can also explicitly request: `apply skill <name>` or `/skill <name>`.")
        return "\n".join(lines)

    def format_skills_for_prompt(self, user_context: str = "") -> str:
        """Generates prompt section with skills overview and deep injection of activated skills."""
        skills = self.list_skills()
        if not skills:
            return ""

        prompt_lines = [
            "### 🧰 TAKTSTOCK UNIVERSAL SKILLS (Shared Knowledge Across 7 Agents):",
            "The agents have access to the following domain skills. When the task touches one of these areas, the team strictly applies the methodology and frameworks of the corresponding skill:"
        ]

        for s in skills:
            prompt_lines.append(f"- {s['icon']} **{s['id']}**: {s['description']}")

        matched = self.detect_relevant_skills(user_context) if user_context else []
        if matched:
            prompt_lines.append("\n🎯 **SPECIALIZED DIRECTIVES ACTIVATED FOR THIS TASK:**")
            for m in matched[:2]:
                snippet = m['content'][:1800]
                prompt_lines.append(f"\n#### {m['icon']} ACTIVE SKILL: {m['name']}\n{snippet}\n...")

        return "\n".join(prompt_lines)


