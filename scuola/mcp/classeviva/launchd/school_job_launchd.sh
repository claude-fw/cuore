#!/bin/bash
# Gestione del job Scuola con launchd (agente utente). NIENTE viene attivato senza `install --conferma`.
#   render [file]        scrive il plist con i percorsi reali (default: stdout). Nessun effetto su launchd.
#   install --conferma   copia il plist in ~/Library/LaunchAgents e lo carica (avvia subito un primo giro: RunAtLoad).
#   uninstall            scarica l'agente e sposta il plist nel Cestino (`trash`); job, log e stato restano.
#   status               mostra se l'agente e' caricato e l'ultimo esito del job.
# Disattivazione temporanea senza disinstallare: `launchctl bootout gui/$(id -u)/cloud.scuola.job`.
set -euo pipefail
LABEL="cloud.scuola.job"
HERE="$(cd "$(dirname "$0")" && pwd)"
JOB="$(cd "$HERE/.." && pwd)/school_job.py"
PYTHON="${PYTHON:-/usr/bin/python3}"
TEMPLATE="$HERE/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

render() {
  sed -e "s|@PYTHON@|$PYTHON|g" -e "s|@JOB@|$JOB|g" -e "s|@HOME@|$HOME|g" "$TEMPLATE"
}

case "${1:-}" in
  render)
    if [ -n "${2:-}" ]; then render > "$2"; else render; fi ;;
  install)
    if [ "${2:-}" != "--conferma" ]; then
      echo "Attiva l'esecuzione automatica (controllo ogni 5 minuti e all'accesso: giro solo se serve). Ripeti con: $0 install --conferma"; exit 64
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/Cloud"
    render > "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootstrap "gui/$(id -u)" "$TARGET"
    echo "Caricato $LABEL. Verifica: $0 status" ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    if [ -f "$TARGET" ]; then trash "$TARGET"; fi
    echo "Scaricato $LABEL (plist nel Cestino). Job, log e stato non sono stati toccati." ;;
  status)
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then echo "agente: caricato"; else echo "agente: NON caricato"; fi
    "$PYTHON" "$JOB" status ;;
  *)
    sed -n '2,8p' "$0"; exit 64 ;;
esac
