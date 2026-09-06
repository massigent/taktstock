#!/usr/bin/env python3
"""
CLI Telemetry Reporter for Taktstock
------------------------------------
Genera un riepilogo tabellare dell'utilizzo agenti (ultimi N giorni):
- Chiamate totali, successi, fallimenti
- Durata media (ms)
- Token riportati totali
- Conteggio escalation per motivo
"""

import sys
import argparse
from pathlib import Path

# Setup path di importazione
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

try:
    from infrastructure.telemetry_repository import TelemetryRepository
except ImportError:
    from server.infrastructure.telemetry_repository import TelemetryRepository


def main():
    parser = argparse.ArgumentParser(description="Report telemetria utilizzo agenti Taktstock (Fase 1).")
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=7,
        help="Numero di giorni di storico da analizzare (default: 7)"
    )
    args = parser.parse_args()

    repo = TelemetryRepository()
    report_text = repo.format_cli_report(days=args.days)
    print(report_text)


if __name__ == "__main__":
    main()
