#!/bin/bash
# Setup ClasseViva -> Claude (da eseguire UNA volta sul Mac, in Terminale).
# 1) controlla python3  2) salva la password nel Keychain (la digiti tu, non appare mai)
# 3) prova il login reale  4) registra il server MCP in Claude Desktop e Claude Code.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
SVC="cloud.classeviva"

echo "== 1/4 Python"
command -v python3 >/dev/null || { echo "python3 non trovato: installa gli Strumenti da riga di comando (xcode-select --install) e rilancia."; exit 1; }
python3 - <<'PY' || { echo "Serve Python >= 3.9."; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
python3 --version

echo "== 2/4 Credenziali nel Keychain"
if security find-generic-password -s "$SVC" >/dev/null 2>&1; then
  echo "Credenziali gia' presenti nel Keychain (servizio $SVC). Per cambiarle: security delete-generic-password -s $SVC"
else
  read -r -p "Username ClasseViva (es. S1234567X): " CVUSER
  echo "Ora digita la password quando richiesto (non viene mostrata):"
  security add-generic-password -s "$SVC" -a "$CVUSER" -U -w || { echo "Salvataggio annullato."; exit 1; }
fi

echo "== 3/4 Test di login reale"
python3 "$DIR/server.py" --selftest || { echo "Login fallito: vedi messaggio sopra. Registrazione NON eseguita."; exit 1; }

echo "== 4/4 Registrazione in Claude"
python3 "$DIR/register_claude.py"
echo "Fatto. Riavvia Claude Desktop (Cmd+Q e riapri)."
