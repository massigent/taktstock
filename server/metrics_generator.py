#!/usr/bin/env python3
"""
Taktstock Markdown Metrics & Cost Report Generator
--------------------------------------------------
Reads execution runs history and automatically generates an
analytical report formatted in Markdown (`METRICS.md`) with:
- Overall summary (Runs, Total Tokens, Estimated Cost, Success Rate)
- Breakdown table by Preset (Light, Standard, Critical)
- Daily breakdown table with activity details
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

BASE_DIR = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or os.environ.get("ORCH_HOME") or (Path.home() / "taktstock"))
STATE_DIR = BASE_DIR / "state"
DEFAULT_OUTPUT_MD = Path(__file__).resolve().parent.parent / "METRICS.md"

def generate_metrics_markdown(history_path: Path, output_md_path: Path):
    if not history_path.exists():
        print(f"No history file found at {history_path}")
        return

    runs = []
    with open(history_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    runs.append(json.loads(line))
                except Exception:
                    pass

    total_runs = len(runs)
    if total_runs == 0:
        print("Empty history, no data to aggregate.")
        return

    completed_runs = sum(1 for r in runs if r.get("status") == "COMPLETED")
    success_rate = (completed_runs / total_runs) * 100

    # Aggregations by preset
    preset_stats = defaultdict(lambda: {"runs": 0, "tokens": 0, "completed": 0})
    # Aggregations by date (YYYY-MM-DD)
    daily_stats = defaultdict(lambda: {"runs": 0, "tokens": 0, "tasks": []})

    total_tokens = 0
    for r in runs:
        preset = r.get("preset", "standard").lower()
        tokens = r.get("tokens_used", {}).get("total", 0)
        num_tokens = tokens if isinstance(tokens, (int, float)) else 0
        total_tokens += num_tokens
        
        preset_stats[preset]["runs"] += 1
        preset_stats[preset]["tokens"] += num_tokens
        if r.get("status") == "COMPLETED":
            preset_stats[preset]["completed"] += 1

        ts = r.get("timestamp", datetime.now().isoformat())
        day = ts[:10]
        daily_stats[day]["runs"] += 1
        daily_stats[day]["tokens"] += tokens
        daily_stats[day]["tasks"].append(r.get("task", "Task")[:50])

    # Estimated average cost ($2.5 per 1M tokens across model mix)
    total_cost = (total_tokens / 1_000_000) * 2.50

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md_content = f"""# 📈 Taktstock Multi-Agent — Metrics & Cost Report

*Automatically generated on: `{now_str}`*

---

## 📊 General Summary

| Metric | Value |
|---|---|
| **Total Runs** | **{total_runs}** |
| **Completed Successfully** | **{completed_runs}** |
| **Success Rate** | **{success_rate:.1f}%** |
| **Total Tokens Consumed** | **{total_tokens:,}** |
| **Total Estimated Cost** | **~${total_cost:.3f}** |

---

## ⚡ Breakdown by Preset

| Preset | Runs | Tokens Consumed | Estimated Cost | Success Rate |
|---|---|---|---|---|
"""
    for preset, st in sorted(preset_stats.items()):
        p_tokens = st["tokens"]
        p_cost = (p_tokens / 1_000_000) * 2.50
        p_rate = (st["completed"] / max(1, st["runs"])) * 100
        md_content += f"| `{preset.upper()}` | {st['runs']} | {p_tokens:,} | ~${p_cost:.3f} | {p_rate:.1f}% |\n"

    md_content += """
---

## 📅 Activity by Day

| Date | Runs | Tokens | Main Activities |
|---|---|---|---|
"""
    for day, dst in sorted(daily_stats.items(), reverse=True):
        tasks_summary = ", ".join(dict.fromkeys(dst["tasks"]))[:80]
        md_content += f"| **{day}** | {dst['runs']} | {dst['tokens']:,} | {tasks_summary} |\n"

    md_content += """
---

> [!TIP]
> To view the real-time web dashboard with graphical interface and HTML Diff links:
> Open `http://<server-ip>:8765/` in your browser.
"""

    output_md_path.write_text(md_content, encoding="utf-8")
    print(f"✅ Metrics report generated successfully at: {output_md_path}")

def main():
    parser = argparse.ArgumentParser(description="Markdown metrics report generator for Taktstock")
    parser.add_argument("--history", default=str(STATE_DIR / "runs_history.jsonl"), help="Path to runs_history.jsonl file")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_MD), help="Path to output Markdown file (default: METRICS.md)")
    args = parser.parse_args()

    generate_metrics_markdown(Path(args.history), Path(args.output))

if __name__ == "__main__":
    main()
