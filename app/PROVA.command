#!/bin/bash
# CUORE · PROVA sul Mac: doppio clic nel Finder. Crea una sessione NUOVA con i dati sintetici di esempio (sotto la
# cartella temporanea), avvia il ponte di prova su 127.0.0.1:8766 e apre il browser. Finance accetta scritture solo su
# quella copia; il ponte reale (8765) e i dati reali non vengono toccati. La finestra del Terminale tiene vivo il ponte:
# chiuderla (o Ctrl-C) lo ferma; la cartella della sessione resta nella cartella temporanea (il sistema la pulisce).
# Nessuna variabile d'ambiente, nessun servizio installato, nessuna porta verso altri dispositivi.
set -u
PORT=8766
URL="http://127.0.0.1:${PORT}/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

is_prova() { /usr/bin/curl -s --max-time 2 "${URL}api/prova" 2>/dev/null | /usr/bin/grep -q '"cuore.prova/1"'; }
port_answers() { /usr/bin/curl -s -o /dev/null --max-time 2 "$URL" 2>/dev/null; }

if is_prova; then
  echo "La prova e' gia' attiva su $URL (un'altra finestra del Terminale la tiene accesa): la apro."
  /usr/bin/open "$URL"
  exit 0
fi
if port_answers; then
  echo "La porta $PORT e' usata da un altro programma: la prova non parte."
  exit 1
fi

echo "Avvio della PROVA su $URL (dati sintetici, solo questo Mac). Per fermarla: Ctrl-C o chiudi questa finestra."
/usr/bin/python3 -B "$ROOT/app/bridge/prova.py" --nuova --porta "$PORT" &
PID=$!
stop() { kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; }
trap 'stop; echo; echo "Prova fermata."' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for _ in $(seq 1 75); do
  if is_prova; then
    /usr/bin/open "$URL"
    wait "$PID"
    exit $?
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "Il ponte di prova non e' partito: vedi i messaggi sopra."
    exit 1
  fi
  sleep 0.2
done
echo "Il ponte di prova non risponde dopo 15 secondi: controlla i messaggi sopra."
exit 1
