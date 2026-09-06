#!/usr/bin/env python3
"""
Taktstock Read-Only n8n MCP Server
----------------------------------
Server MCP conforme allo standard Model Context Protocol (JSON-RPC 2.0 su stdio).
Fornisce a Luna e agli agenti Taktstock accesso ESCLUSIVAMENTE IN SOLA LETTURA
ai workflow live n8n per preflight, ispezione e sincronizzazione mirror.

Politica di sicurezza:
- Supporta SOLO richieste HTTP GET verso le API n8n.
- Nessun tool di scrittura, aggiornamento, esecuzione o cancellazione.
- Rifiuto esplicito fail-closed per qualsiasi operazione mutante.
- Nessuna propagazione o stampa di segreti/chiavi API.
"""

import os
import sys
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, Any, List, Optional

# Configurazione logging su stderr per non interferire con stdout JSON-RPC
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][n8n-ro-mcp] %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("n8n-ro-mcp")

N8N_API_URL = os.environ.get("N8N_API_URL", "https://n8n-netcup.salus.academy/").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "").strip()

TOOLS_DEFINITIONS = [
    {
        "name": "search_workflows",
        "description": "Cerca workflow n8n live per nome o tag (read-only). Restituisce catalogo e metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Filtro per nome o descrizione del workflow"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filtro per tag del workflow"
                },
                "limit": {
                    "type": "integer",
                    "description": "Numero massimo di risultati (default: 50, max: 100)",
                    "default": 50
                }
            }
        }
    },
    {
        "name": "get_workflow_details",
        "description": "Recupera la definizione completa in sola lettura di un workflow (nodi, connessioni, settings, trigger).",
        "inputSchema": {
            "type": "object",
            "required": ["workflowId"],
            "properties": {
                "workflowId": {
                    "type": "string",
                    "description": "ID univoco del workflow n8n da esportare/leggere"
                }
            }
        }
    },
    {
        "name": "list_workflows",
        "description": "Elenca tutti i workflow presenti nell'istanza n8n live con metadata essenziali.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Numero massimo di workflow da elencare (default: 100)",
                    "default": 100
                }
            }
        }
    }
]


def _get_api_key() -> str:
    """Recupera la chiave API da env o file di configurazione con fallback sicuro."""
    if N8N_API_KEY:
        return N8N_API_KEY
    
    # Prova a leggere da config Luna
    luna_cfg = os.path.expanduser("~/.codex/accounts/luna/config.toml")
    if os.path.exists(luna_cfg):
        try:
            with open(luna_cfg, "r", encoding="utf-8") as f:
                for line in f:
                    if "N8N_API_KEY" in line and "=" in line:
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def n8n_get_request(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Esegue una chiamata HTTP GET in sola lettura all'API n8n."""
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("N8N_API_KEY non configurata. Impossibile accedere all'API n8n.")

    url = f"{N8N_API_URL}/{endpoint.lstrip('/')}"
    if params:
        query_str = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query_str:
            url += f"?{query_str}"

    req = urllib.request.Request(
        url,
        headers={
            "X-N8N-API-KEY": api_key,
            "Accept": "application/json",
            "User-Agent": "Taktstock-ReadOnly-MCP/1.0"
        },
        method="GET"
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"API n8n ha risposto con codice HTTP {resp.status}")
        raw_body = resp.read().decode("utf-8")
        return json.loads(raw_body)


def handle_search_workflows(args: Dict[str, Any]) -> Dict[str, Any]:
    """Gestisce la ricerca read-only dei workflow."""
    limit = min(int(args.get("limit", 50)), 100)
    data = n8n_get_request("api/v1/workflows", {"limit": limit})
    workflows = data.get("data", [])

    query = str(args.get("query", "")).strip().lower()
    tag_filter = set(args.get("tags") or [])

    results = []
    for wf in workflows:
        wf_name = str(wf.get("name", "")).lower()
        wf_desc = str(wf.get("description", "") or "").lower()
        
        # Filtro query
        if query and (query not in wf_name and query not in wf_desc and query != wf.get("id")):
            continue

        # Filtro tag
        wf_tags = {t.get("name") for t in wf.get("tags", []) if isinstance(t, dict)}
        if tag_filter and not tag_filter.issubset(wf_tags):
            continue

        results.append({
            "id": wf.get("id"),
            "name": wf.get("name"),
            "active": wf.get("active"),
            "updatedAt": wf.get("updatedAt"),
            "createdAt": wf.get("createdAt"),
            "isArchived": wf.get("isArchived", False),
            "nodesCount": len(wf.get("nodes", [])),
            "tags": wf.get("tags", [])
        })

    return {
        "count": len(results),
        "data": results
    }


def handle_get_workflow_details(args: Dict[str, Any]) -> Dict[str, Any]:
    """Gestisce l'esportazione / lettura della struttura di un singolo workflow."""
    wf_id = str(args.get("workflowId", "")).strip()
    if not wf_id:
        raise ValueError("Parametro obbligatorio 'workflowId' mancante.")

    wf_data = n8n_get_request(f"api/v1/workflows/{wf_id}")
    return {
        "workflow": wf_data
    }


def handle_list_workflows(args: Dict[str, Any]) -> Dict[str, Any]:
    """Elenca tutti i workflow con metadata."""
    limit = min(int(args.get("limit", 100)), 100)
    data = n8n_get_request("api/v1/workflows", {"limit": limit})
    workflows = data.get("data", [])
    
    catalog = [{
        "id": w.get("id"),
        "name": w.get("name"),
        "active": w.get("active"),
        "updatedAt": w.get("updatedAt"),
        "createdAt": w.get("createdAt"),
        "nodesCount": len(w.get("nodes", []))
    } for w in workflows]

    return {
        "total": len(catalog),
        "workflows": catalog
    }


def process_jsonrpc_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Elabora un messaggio JSON-RPC in entrata conforme al protocollo MCP."""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "n8n-readonly-mcp",
                    "version": "1.0.0"
                }
            }
        }

    elif method == "notifications/initialized":
        return None

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": TOOLS_DEFINITIONS
            }
        }

    elif method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        try:
            if tool_name == "search_workflows":
                content_res = handle_search_workflows(args)
            elif tool_name == "get_workflow_details":
                content_res = handle_get_workflow_details(args)
            elif tool_name == "list_workflows":
                content_res = handle_list_workflows(args)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "isError": True,
                        "content": [{
                            "type": "text",
                            "text": f"Tool '{tool_name}' non consentito. Questo server MCP è rigorosamente in sola lettura (supporta solo search_workflows, get_workflow_details e list_workflows)."
                        }]
                    }
                }

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(content_res, ensure_ascii=False, indent=2)
                    }]
                }
            }
        except Exception as e:
            logger.error(f"Errore esecuzione tool {tool_name}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "isError": True,
                    "content": [{
                        "type": "text",
                        "text": f"Errore read-only MCP: {str(e)}"
                    }]
                }
            }

    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {}
        }

    else:
        if msg_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Metodo non supportato: {method}"
                }
            }
        return None


def main():
    """Loop principale stdio per il server MCP."""
    logger.info("Avvio server n8n-readonly-mcp...")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = process_jsonrpc_message(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            logger.warning(f"Messaggio non valido: {line[:100]}")
        except Exception as e:
            logger.error(f"Eccezione nel loop MCP: {e}")


if __name__ == "__main__":
    main()
