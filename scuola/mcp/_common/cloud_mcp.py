"""Mini server MCP (stdio, JSON-RPC a righe) - solo libreria standard, Python >= 3.9.

Base riutilizzabile per i server MCP locali di Cloud: nessuna dipendenza da installare.
Il canale stdout e' riservato al protocollo; i log vanno su stderr.
"""
from __future__ import annotations

import json
import sys
import traceback

DEFAULT_PROTOCOL = "2025-06-18"


class Server:
    def __init__(self, name, version="0.1.0", instructions=None):
        self.name, self.version, self.instructions = name, version, instructions
        self._tools = {}

    def tool(self, name, description, properties=None, required=None):
        """Decoratore: registra una funzione come strumento MCP."""
        schema = {"type": "object", "properties": properties or {}}
        if required:
            schema["required"] = required

        def deco(fn):
            self._tools[name] = {"fn": fn, "def": {
                "name": name, "description": description, "inputSchema": schema}}
            return fn
        return deco

    def call(self, name, arguments):
        if name not in self._tools:
            raise KeyError("strumento sconosciuto: %s" % name)
        return self._tools[name]["fn"](**(arguments or {}))

    def _handle(self, msg):
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": self.name, "version": self.version}}
            if self.instructions:
                res["instructions"] = self.instructions
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"tools": [t["def"] for t in self._tools.values()]}}
        if method == "tools/call":
            try:
                out = self.call(params.get("name"), params.get("arguments"))
                text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:  # errore dello strumento, non del protocollo
                sys.stderr.write(traceback.format_exc())
                result = {"content": [{"type": "text", "text": "Errore: %s" % exc}],
                          "isError": True}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        if mid is None:  # notifica (es. notifications/initialized): nessuna risposta
            return None
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "metodo non supportato: %s" % method}}

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                resp = self._handle(json.loads(line))
            except Exception:
                sys.stderr.write(traceback.format_exc())
                continue
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
