# Ambiente sintetico (copia di prova) — D3a

Creato il 2026-11-01 (blocco D3, decisione C-10). Serve a provare le scritture di Finance (movimenti manuali, budget,
abbinamenti) senza mai avvicinarsi ai dati reali. Lo usa il ponte di prova dell'app (`app/bridge/prova.py`, D3b).

## Sessione di prova
Una cartella **nuova** sotto la cartella temporanea dell'utente con `finance-sintetico.sqlite3` (marcato
`meta.dataset = 'sintetico'` e `meta.sessione = <id>`) e `sessione.json` (`cuore.prova.sessione/1`, stato `aperta` |
`chiusa`). Il generatore (`src/synthetic/generate.ts`) importa solo `examples/sintetici` con orologio e id fissi: due
sessioni hanno gli stessi dati (12 movimenti, 2 saldi, 12 classificati); lo schema è quello attivo più le migrazioni in
attesa (v7: movimenti manuali e budget), applicate **solo** qui.

## Protezioni (fail-closed, `src/synthetic/guard.ts` e `app/bridge/prova_guard.py`)
1. Percorso esplicito e assoluto; nessun percorso predefinito; nessuna variabile d'ambiente letta.
2. `realpath` sotto la cartella temporanea e fuori da dati reali (`~/Library/Application Support/Cloud`, `/CUORE`),
   iCloud (`~/Library/Mobile Documents`, `~/Library/CloudStorage`) e repository. `realpathSync.native`: sotto il modello
   dei permessi di Node chiede la lettura del solo percorso dato.
3. Nessun collegamento simbolico; database con un solo collegamento fisico; proprietario corrente, cartella 0700, file 0600.
4. Marcatore e id della sessione verificati in sola lettura prima di aprire in scrittura e di nuovo sulla connessione
   in scrittura, prima di ogni istruzione (anche prima delle migrazioni). Il database reale non ha il marcatore.
5. `runQuery` ammette lo schema sintetico solo su un database marcato; senza opzioni il percorso reale è invariato (v6).
6. Lato Python il marcatore si legge con `immutable=1` (database in WAL), ammesso solo con `-wal` assente o vuoto.

## Uso da terminale (`scripts/prova.ts`)
```
node scripts/prova.ts crea     --dati <cartella nuova sotto $TMPDIR>
node scripts/prova.ts verifica --dati <cartella>
node scripts/prova.ts esegui   --dati <cartella>   < {"op": "aggiungi_manuale", "input": {...}}
node scripts/prova.ts chiudi   --dati <cartella>   # scritture rifiutate, nessun file cancellato
```
Uscite: 0 riuscita, 64 uso, 2 rifiuto di sicurezza, 1 errore del servizio con `codice` `non_valido` | `non_trovato` |
`errore`. Operazioni di `esegui`: `consulta`, `aggiungi_manuale`, `correggi_manuale`, `annulla_manuale`,
`storico_manuale`, `imposta_budget`, `stato_budget`, `rileva_abbinamenti`, `conferma_abbinamento`, `rifiuta_abbinamento`,
`revoca_abbinamento`. Dall'app: [../../app/README.md](../../app/README.md) (ponte di prova).

## Test
`test/synthetic.test.ts` (generazione, percorsi, collegamenti, marcatore, migrazioni, schema per percorso, processo
separato) e `app/tests/test_prova.py` (controlli Python, ponte di prova).
