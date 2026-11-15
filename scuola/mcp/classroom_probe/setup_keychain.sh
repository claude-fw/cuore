#!/bin/bash
# Salva client id e client secret OAuth nel Keychain di macOS (servizio cloud.classroom-probe).
# Non scrive nessun file. Il secret viene chiesto dal comando `security` stesso
# (input nascosto), quindi non compare ne' nella cronologia ne' nella lista processi.
set -euo pipefail
SERVICE="cloud.classroom-probe"

read -r -p "Client ID (finisce con .apps.googleusercontent.com): " CLIENT_ID
case "$CLIENT_ID" in
  *.apps.googleusercontent.com) ;;
  *) echo "Il Client ID non ha il formato atteso. Interrompo."; exit 1 ;;
esac

security add-generic-password -U -s "$SERVICE" -a client_id -l "Cloud Classroom probe: client id" -w "$CLIENT_ID"
echo "Ora incolla il Client Secret (non verra' mostrato) e confermalo quando richiesto:"
security add-generic-password -U -s "$SERVICE" -a client_secret -l "Cloud Classroom probe: client secret" -w
echo "Fatto. Verifica con: python3 classroom_probe.py --check"
