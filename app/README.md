# CUORE App — interfaccia e ponte locale

Interfaccia unica di CUORE (Home, School, Finance, Sistema) e il ponte locale che le passa i feed dei moduli in **sola
lettura**, solo su `127.0.0.1`. La stessa pagina gira anche nella vetrina cloud con dati inventati (`../cloud/`).

## Struttura
| Percorso | Ruolo |
|---|---|
| `index.html` | l'app (una pagina, nessuna dipendenza esterna); riletta dal ponte a ogni richiesta |
| `CUORE.command` | avvio sul Mac con doppio clic: ponte su `http://127.0.0.1:8765` e browser; la finestra del Terminale lo tiene acceso |
| `bridge/serve.py` | ponte: solo 127.0.0.1, controlli Host/Origin; `/api/school`, `/api/money`, `/api/events`, `/api/search`, `/api/scritture`; solo GET salvo `--scritture` (sotto) |
| `bridge/scritture.py` | scritture del titolare: parti comuni ai due ponti e scrittore dei dati reali (Finance con `../finance/scripts/scrivi.ts`, School con `school_personale.py`) |
| `bridge/promemoria.py`, `bridge/finance_appunti.py` | promemoria del Mac (spenti senza `~/Library/Application Support/CUORE/promemoria.json`; LaunchAgent di esempio in `launchd/`, 07:15, 15:00, 19:00) e appunti Finance con promemoria facoltativo |
| `bridge/cuore_school.py`, `cuore_school_contract.py` | adattatore School → `cuore.school.feed/1` ([../docs/it/contratto-school.md](../docs/it/contratto-school.md)) |
| `bridge/cuore_money.py`, `cuore_money.ts` | adattatore Finance → `cuore.money.feed/1`, database aperto immutable ([../docs/it/contratto-money.md](../docs/it/contratto-money.md)) |
| `bridge/cuore_events.py` | registro degli eventi `cuore.events/1` ([../docs/it/contratto-eventi.md](../docs/it/contratto-eventi.md)) |
| `bridge/cuore_search.py`, `cuore_classroom.py` | ricerca globale `cuore.search/1` e indice Classroom |
| `bridge/cuore_actions.py` | registro delle azioni proposte, decise ed eseguite (`cuore.actions/1`, catena di hash) |
| `bridge/cuore_mcp.py` | server MCP `cuore` (eventi, ricerca, azioni): **non registrato** in alcun client, serve un'autorizzazione |
| `bridge/export_offline.py`, `offline_template.html` | copia offline School (progettata, livello da decidere) |
| `bridge/verifica_giro.py` | verifica in sola lettura di un giro del job School |
| `PROVA.command`, `bridge/prova.py` | **ponte di prova** (D3b, decisione C-10): processo separato su `127.0.0.1:8766`, Finance su una **copia sintetica** con scritture (`POST /api/prova/esegui`); mai sulla porta 8765, mai sui dati reali |
| `bridge/prova_guard.py`, `prova_money.ts` | controlli fail-closed della copia sintetica lato Python e feed Money della copia ([../finance/docs/ambiente-sintetico.md](../finance/docs/ambiente-sintetico.md)) |
| `tests/` | test per file (sotto) |

## Avvio
Doppio clic su `CUORE.command` (o `/usr/bin/python3 -B app/bridge/serve.py --porta 8765`). Se CUORE è già attivo apre
solo il browser. Nessun LaunchAgent, nessuna porta verso altri dispositivi. Prima di riavviare il ponte controllare che
nessuno lo stia usando: il codice Python si carica all'avvio, `index.html` no.

## Ponte di prova (scritture su dati sintetici)
Doppio clic su `PROVA.command` (o `/usr/bin/python3 -B app/bridge/prova.py --nuova`): crea una sessione nuova sotto la
cartella temporanea con i dati sintetici di esempio, avvia il ponte su `http://127.0.0.1:8766` e apre il browser. La
pagina mostra la fascia «PROVA — dati sintetici» (aggiunta dal ponte di prova: `index.html` su disco non cambia) e il
segnale `<meta name="cuore-prova">` per i moduli di D2. School usa un registro **sintetico** (quello dei test del motore) e i dati personali School (compiti fatti, note, impegni, obiettivi: `bridge/school_personale.py`, file di soli eventi nella sessione); eventi e ricerca non sono collegati (503).
Scritture: `POST /api/prova/esegui` con `{"op": ..., "input": {...}}` (solo JSON, al massimo 64 KiB, una alla volta), eseguite
da `finance/scripts/prova.ts` con i permessi di Node ristretti alla cartella della sessione. Esiti: 200, 400, 403, 404,
413, 415, 422, 500. Il ponte reale non importa niente del ponte di prova e resta in sola lettura.

## Scritture sui dati reali (spente per default)
Si attivano solo avviando il ponte reale con l'opzione esplicita:
`/usr/bin/python3 -B app/bridge/serve.py --porta 8765 --scritture [--school-personale <file .jsonl>]`.
Senza `--scritture` il ponte resta in sola lettura (ogni POST: 405). `CUORE.command` non la passa.
- **Cosa:** le stesse operazioni del ponte di prova (`POST /api/scritture/esegui` con `{"op", "input", "requestId"}`):
  spesa e contanti, correzione e annullamento con motivo e storico, categoria, limiti di spesa, abbinamenti, nuovo conto;
  School: compito fatto, nota, impegno, obiettivo di media, annulla. `GET /api/scritture` dice se sono attive; la pagina
  porta `<meta name="cuore-scritture" content="reali">`, la fascia «DATI REALI · MODIFICHE ATTIVE» e l'etichetta «dati
  reali» nei moduli; prima di un annullamento chiede conferma.
- **Protezioni:** solo `127.0.0.1` e Host del ponte, niente rete privata (intestazione d'identità = 403), `Origin` del
  ponte obbligatorio e `Sec-Fetch-Site` `same-origin`, solo JSON fino a 64 KiB, operazioni dell'elenco, campi validati
  dai servizi; una richiesta alla volta. `requestId` obbligatorio per le scritture: ripetuto con lo stesso contenuto =
  stessa risposta (`ripetuta: true`), con contenuto diverso = 409; dopo un riavvio del ponte lo garantiscono il motore
  (`requestId` dei movimenti manuali) e l'archivio School (`richiesta`). Registro su stderr con sola operazione, esito,
  stato del backup: mai importi, descrizioni o testi.
- **Backup Finance:** prima della prima scrittura di ogni giorno (ora italiana) `scrivi.ts` copia il database con
  `VACUUM INTO` in `<cartella del database>/../backup/<nome>-prima-di-scrittura-<istante>.sqlite3` (nome della rotazione
  C-08), permessi 0600, `integrity_check` e impronta del contenuto uguale all'originale prima e dopo; solo allora il
  file prende il nome definitivo. Backup non verificato o non possibile = scrittura rifiutata (403), database invariato.
  Il database non viene mai migrato da qui (versione diversa = rifiuto); le letture del feed restano immutabili.
- **School:** archivio personale `~/Library/Application Support/CUORE/school/personale.jsonl` (cartella 0700, file 0600,
  soli eventi aggiunti); rifiutato se in iCloud, nel repository o con permessi aperti. Il Registry resta in sola lettura.
- **Promemoria e appunti Finance:** `promemoria_stato` / `promemoria_imposta` (impostazioni in `--promemoria`,
  predefinito `~/Library/Application Support/CUORE/promemoria.json`, scrittura atomica 0600, unica fonte del motore) e
  `appunti_stato` / `appunti_scrivi` (archivio di soli eventi `--finance-appunti`, predefinito
  `~/Library/Application Support/CUORE/finance/appunti.jsonl`, 0700/0600, separato dal database Finance). `GET
  /api/promemoria` mostra lo stato delle impostazioni anche senza `--scritture`. Nel ponte di prova le stesse operazioni
  usano file della sessione.

## Test
Uno per file: con poca memoria la suite intera può essere terminata (codice 137).
```
cd ~/Developer/cuore/app && for t in tests/test_*.py; do /usr/bin/python3 -B -m unittest "tests.$(basename "$t" .py)" || break; done
```
I test usano fixture sintetiche e copie temporanee.
