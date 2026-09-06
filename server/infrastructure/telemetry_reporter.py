#!/usr/bin/env python3
"""
CLI Telemetry Reporter for Taktstock
------------------------------------
Generates a tabular summary of agent usage (last N days):
- Total calls, successes, failures
- Average duration (ms)
- Total reported tokens
- Escalation count by reason
"""

import sys
import argparse
from pathlib import Path

# Setup import path
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

try:
    from infrastructure.telemetry_repository import TelemetryRepository
except ImportError:
    from server.infrastructure.telemetry_repository import TelemetryRepository


def main():
    parser = argparse.ArgumentParser(description="Taktstock Agent Usage Telemetry Report (Phase 1).")
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=7,
        help="Number of historical days to analyze (default: 7)"
    )
    args = parser.parse_args()

    repo = TelemetryRepository()
    report_text = repo.format_cli_report(days=args.days)
    print(report_text)


if __name__ == "__main__":
    main()
