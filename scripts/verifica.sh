#!/bin/sh
# Verifica rapida di CUORE: una riga per controllo, dettaglio solo se qualcosa fallisce.
# Uso: npm run verifica   (dalla radice del repository). Nessun dato reale: i test usano valori fittizi.
cd "$(dirname "$0")/.." || exit 1
fail=0
run() { # nome cartella comando...
  n=$1; d=$2; shift 2
  out=$(cd "$d" && "$@" 2>&1); s=$?
  if [ $s -eq 0 ]; then echo "OK    $n"; else echo "FAIL  $n"; echo "$out" | tail -25; fail=1; fi
}
run "finance typecheck"  finance npm run -s typecheck
run "finance lint"       finance npm run -s lint
run "finance migrazioni" finance npm run -s migrations:check
run "finance test"       finance npm run -s test
run "cloud typecheck"    cloud npm run -s typecheck
run "cloud lint"         cloud npm run -s lint
run "cloud test"         cloud npm run -s test
run "app test"           app sh -c 'for t in tests/test_*.py; do /usr/bin/python3 -B -m unittest "tests.$(basename "$t" .py)" >/dev/null 2>&1 || { echo "fallito: $t"; exit 1; }; done'
run "school test"        scuola/mcp/classeviva /usr/bin/python3 -B -m unittest discover -s tests
[ $fail -eq 0 ] && echo "TUTTO OK" || echo "ALCUNI CONTROLLI FALLITI"
exit $fail
