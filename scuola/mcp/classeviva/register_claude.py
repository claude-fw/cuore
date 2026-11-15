#!/usr/bin/env python3
"""Registra il server MCP "classeviva" in Claude Desktop (JSON) e Claude Code (CLI).

Sicuro: fa un backup datato, AGGIUNGE soltanto la chiave "classeviva", non tocca altri server,
non sovrascrive se esiste gia', si ferma se il JSON non e' valido. Nessun segreto in config.
"""
import json, os, shutil, subprocess, sys, time

HOME = os.path.expanduser("~")
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")  # accanto a questo file
PY = shutil.which("python3") or "/usr/bin/python3"
DESKTOP = os.path.join(HOME, "Library", "Application Support", "Claude", "claude_desktop_config.json")


def desktop():
    if not os.path.isdir(os.path.dirname(DESKTOP)):
        print("Claude Desktop: cartella config non trovata, salto."); return
    cfg = {}
    if os.path.exists(DESKTOP):
        try:
            cfg = json.load(open(DESKTOP))
        except ValueError:
            print("Claude Desktop: JSON non valido, NON modifico."); return
        bak = DESKTOP + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DESKTOP, bak); print("Backup:", bak)
    servers = cfg.setdefault("mcpServers", {})
    if "classeviva" in servers:
        print("Claude Desktop: 'classeviva' gia' presente, non tocco."); return
    servers["classeviva"] = {"command": PY, "args": [SERVER]}
    json.dump(cfg, open(DESKTOP, "w"), indent=2, ensure_ascii=False)
    print("Claude Desktop: aggiunto 'classeviva'. Riavvia Claude (Cmd+Q e riapri).")


def code():
    claude = shutil.which("claude")
    if not claude:
        print("Claude Code: comando 'claude' non trovato nel PATH, salto."); return
    got = subprocess.run([claude, "mcp", "get", "classeviva"], capture_output=True, text=True)
    if got.returncode == 0:
        print("Claude Code: 'classeviva' gia' registrato, non tocco."); return
    r = subprocess.run([claude, "mcp", "add", "--scope", "user", "classeviva", "--", PY, SERVER],
                       capture_output=True, text=True)
    print("Claude Code:", (r.stdout + r.stderr).strip() or "ok")


if __name__ == "__main__":
    desktop(); code()
