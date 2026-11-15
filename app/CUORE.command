#!/bin/bash
# CUORE sul Mac: doppio clic nel Finder. Avvia il ponte locale (solo 127.0.0.1) se non e' gia' attivo e apre CUORE nel
# browser predefinito. Dal 06/11 Finance vive nel cloud (M3): il Mac e' in SOLA LETTURA e le modifiche si fanno da
# CUORE online; CUORE_SCRITTURE_MAC=1 riaccende le scritture sul Mac (solo per emergenza). La finestra del Terminale tiene vivo il ponte.
# Nessun servizio installato, nessun LaunchAgent, nessuna porta aperta verso altri dispositivi.
set -u
PORT="${CUORE_PORTA:-8765}"
URL="http://127.0.0.1:${PORT}/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPEN="${CUORE_APRI:-/usr/bin/open}"          # sostituibile solo per le prove (es. /usr/bin/true)

# CUORE risponde con la sua pagina: un altro programma sulla stessa porta non viene scambiato per CUORE
is_cuore() { /usr/bin/curl -s --max-time 2 "$URL" 2>/dev/null | /usr/bin/grep -q "<title>CUORE</title>"; }
port_answers() { /usr/bin/curl -s -o /dev/null --max-time 2 "$URL" 2>/dev/null; }

if is_cuore; then
  echo "CUORE e' gia' attivo su $URL (un'altra finestra del Terminale lo tiene acceso): lo apro."
  "$OPEN" "$URL"
  exit 0
fi
if port_answers; then
  echo "La porta $PORT e' usata da un altro programma: CUORE non parte. Chiudi quel programma o usa CUORE_PORTA=<altra porta>."
  exit 1
fi

echo "Avvio di CUORE su $URL (solo questo Mac). Per fermarlo: Ctrl-C o chiudi questa finestra."
WRITES=()
[ "${CUORE_SCRITTURE_MAC:-}" = "1" ] && WRITES=(--scritture)
/usr/bin/python3 -B "$ROOT/app/bridge/serve.py" --porta "$PORT" ${WRITES[@]+"${WRITES[@]}"} &
PID=$!
stop() { kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; }
trap 'stop; echo; echo "CUORE fermato."' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for _ in $(seq 1 50); do
  if is_cuore; then
    "$OPEN" "$URL"
    wait "$PID"
    exit $?
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "Il ponte non e' partito: vedi i messaggi sopra (porta $PORT gia' occupata?)."
    exit 1
  fi
  sleep 0.2
done
echo "Il ponte non risponde dopo 10 secondi: controlla i messaggi sopra."
exit 1
