# Registro locale SCUOLA (Fase 1)

Livello **L1 RAW + versioni + stato** del sistema Scuola di Cloud. Notion è una *proiezione*, non la fonte primaria.

```
FONTI → RAW (registro) → NORMALIZZATO → ANALYTICS → CONTESTO PER MATERIA → AGENTI
                 └────────→ PROIEZIONE (Notion)
```

## File
| File | Ruolo |
|---|---|
| `mcp/classeviva/registry.py` | registro generico (SQLite, solo libreria standard). Non conosce ClasseViva né Notion |
| `mcp/classeviva/cvv_registry.py` | adattatore ClasseViva: riusa i `normalize_*` di `cvv_client.py`, ID `cvv:<id>`, campi volatili, proiezione |
| `mcp/classeviva/dryrun_registry.py` | dimostrazione offline (dati sintetici o file) RAW → registro → normalizzato → proiezione |
| `mcp/classeviva/cvv_dryrun.py` | **dry-run live** (Fase 2A): logica del tool `cv_registry_dryrun` |
| `mcp/classeviva/cvv_sync.py` | **sync manuale** (Fase 2B): logica del tool `cv_registry_sync`; unico punto che scrive il registro reale |
| `mcp/classeviva/tests/test_registry.py`, `test_cvv_registry.py` | 38 test |

**Non modificati**: `cvv_client.py`, `cvv_subject.py`, `server.py`, i test esistenti, Notion, scheduler, Classroom.
Fase 1 non crea nessun database: il percorso predefinito è solo calcolato da `registry.default_path()`.

## Dove vive
`~/Library/Application Support/Cloud/school/registry.sqlite3` (override: variabile `CLOUD_SCHOOL_DB`). **Fuori da iCloud**: un database SQLite vivo in iCloud rischia di corrompersi. File con permessi 0600. Backup = copia del file (da definire nella fase scheduler).

## Modello
Identità di un record: `(source, kind, source_uid)`. `kind` = tipo di record dell'API (`agenda_event`, `grade`, `absence`, `notice`), non la lettura interpretata.
L'ID storico `cvv:<id>` è in `legacy_uid`; senza ID di fonte vale il fallback `sec:<sha1>` dei normalizzatori. Il kind evita collisioni fra tipi con lo stesso numero.

| Tabella | Contenuto | Mutabile |
|---|---|---|
| `versions` | RAW esatto (testo JSON, ordine dei campi originale), `content_hash`, esclusioni dall'hash, data, run | **no** (trigger) |
| `events` | create / update / state_change / missing / reappeared | **no** (trigger) |
| `records` | stato corrente: versione, hash, `present` / `missing_in_source`, `first_seen` / `last_seen`, stato volatile | sì, ma mai cancellabile (trigger) |
| `runs` | un record per lettura: ok / error, conteggi, avvisi | append |
| `projections` | cosa è stato scritto su un target (es. Notion): hash e versione di logica proiettati | sì |

I campi manuali dell'utente (es. Note) **non** vivono nel registro.

## Hash e versioni
- `content_hash` = sha256 del RAW in JSON canonico, meno i **campi volatili** dichiarati. Nuova versione solo se cambia.
- Volatili (oggi solo `notice.readStatus`): registrati come *stato osservato* in `records.volatile_json`, non come RAW; la versione 1 del RAW resta com'è stata catturata.
- Versione della logica interpretativa (`interp_version`): resta nei normalizzatori, separata dall'hash. Il registro non memorizza l'interpretazione (si rigenera); la confronta nelle `projections`.
- Cambiare la lista dei volatili è una modifica di configurazione: può generare al massimo una nuova versione per record.
- Hash dei normalizzatori (`legacy_hash`, sha1 12 caratteri) ≠ hash del registro (sha256). Il primo serve solo ai record già in Notion (Verifiche, Assenze).

## Scomparsa e anomalie
Mai cancellazioni. Un record assente da una lettura *completa* → `missing_in_source`; se riappare → `reappeared`.
Non viene marcato nulla se: la lista è vuota; calo sotto `min_ratio` (0.5) con ≥2 record attesi; lettura parziale (`complete=False`); fonte in errore. Viene registrato un avviso. `allow_empty=True` salta i controlli (solo per chi si assume la responsabilità). Con `window=(da, a)` si possono marcare solo record con `item_date` nella finestra.

## Errori e isolamento
`run_source()` cattura qualsiasi errore, registra un run `error` e non tocca alcun record. Ogni `ingest` è una transazione: o tutto o niente. Un tipo/fonte che fallisce non blocca gli altri.

## Verifica
`Registry.verify_integrity()` ricalcola gli hash dai RAW salvati e confronta la versione corrente.
Dry-run: `cd mcp/classeviva && python3 dryrun_registry.py [--kind notice|absence|agenda_event] [--input raw.json]` (non scrive nulla).
Test: `python3 -W ignore -m unittest discover -s tests` → 128 test (54 precedenti + 38 del registro + 15 del dry-run live + 21 del sync).

## Fase 2A: dry-run live (`cv_registry_dryrun`)
Tool MCP in sola lettura (server `classeviva` v0.3.0). Parametri: `kinds` (default tutti), `simulate` (default `true`), `second_read` (default `false`). Non c'è alcun parametro per scegliere un percorso di scrittura.
- Legge ClasseViva con il bridge esistente e passa i record RAW a un registro **solo in memoria**.
- Baseline: se esiste un registro reale ne carica una **copia in sola lettura** (`Registry.from_snapshot`: SQLite `immutable=1`, nessun file creato, rifiuta un `-wal` non vuoto); se non esiste, baseline vuota e dichiarato. Una baseline illeggibile **non** viene sostituita da una vuota: il dry-run si ferma.
- Risposta in due sezioni inequivocabili: `RISULTATO_REALE` (cosa farebbe il registro con i dati letti ora) e `SIMULAZIONE_NON_ACCADUTA` (scenari finti su una copia dei record appena letti, ogni passo etichettato `[SIMULATO]`). La sezione reale non contiene mai dati simulati. La simulazione non viene eseguita con meno di 2 record reali: nessun dato inventato.
- `verifica_nessuna_scrittura` confronta, prima e dopo, esistenza/dimensione/mtime del file reale e di `-wal`, `-shm`, `-journal`.
- Agenda: finestra = anno scolastico; la classificazione verifica/compito/evento è **dedotta** dai normalizzatori e la materia non è risolta in questo dry-run.
- Il test sulla mutazione (sostituendo la copia in sola lettura con una scrittura sul file reale) fa fallire 14 test su 15.

## Fase 2B: sync manuale (`cv_registry_sync`)
Server `classeviva` v0.4.0. Parametri: `kinds` (default tutti), `commit` (booleano, **default `false`**), `expect_create` (intero, opzionale). Nessun parametro per il percorso: il file e' sempre `~/Library/Application Support/Cloud/school/registry.sqlite3` (override solo via variabile d'ambiente `CLOUD_SCHOOL_DB`, usata dai test).
- **`commit=false`** (default): una lettura live + piano contro la baseline, identico al dry-run verificato. Il database reale non viene aperto in scrittura, ne' la cartella creata. Il riepilogo indica cosa verrebbe scritto e il valore di `expect_create` da usare.
- **`commit=true`** (solo il booleano vero; `"true"`, `1`, ecc. sono rifiutati con errore): 1) una sola lettura per tipo, piano e scrittura usano gli stessi record; 2) piano in memoria; 3) se `expect_create` e' diverso dai record da creare: rifiuta, nulla scritto; 4) lock non bloccante (`registry.lock`); 5) cartelle mancanti create con 0700 (quelle esistenti non vengono toccate), file 0600; 6) backup in `backups/` (0700, file 0600) **solo** se il piano modifica righe esistenti (update/missing/state_change/reappeared), mai cancellato; 7) scrittura per tipo, una transazione ciascuno; un tipo in errore non blocca gli altri e non marca nulla come scomparso; 8) verifica post-scrittura rileggendo il file da zero.
- **Verifica post-scrittura:** permessi file/cartella, nessun `-wal` residuo, `PRAGMA integrity_check`, hash ricalcolati dai RAW, ogni record letto da ClasseViva presente con RAW identico (e stato volatile coerente), `readStatus` escluso dall'hash delle comunicazioni, conteggi, eventi, run.
- Idempotenza: una seconda sync con gli stessi dati non crea versioni ne' eventi (cambia solo `last_seen` e viene aggiunto un run).
- Mai: scritture su ClasseViva, Notion, Classroom; cancellazioni; scheduler.
- Prima scrittura reale: richiede autorizzazione esplicita (`commit=true, expect_create=<numero visto nella simulazione>`).

## Identità e risposte degradate (aggiornato 2026-10-19)
Correzioni a valle di fenomeni osservati sui dati reali. Regola comune: nessun valore inventato, nessuna versione "fantasma", le modifiche vere restano modifiche, la protezione vale nel dry-run, nella sync e nella verifica post-scrittura (`verify_items` dell'adattatore, usato da `cvv_sync._verify`).

**Implementato**
- **Classroom, file Drive senza titolo** (commit `55b53e2`, test `tests/test_cls_degraded.py`). Classroom a volte restituisce un `driveFile` senza `title`. Se esiste una versione registrata con titolo valido dello stesso file, i campi vengono ricostruiti da lì; altrimenti il file resta come letto con l'avviso "senza versione registrata con titolo".
- **Classroom, link con titolo assente o anti-bot** (Blocco 1, 2026-10-19, test `tests/test_cls_links.py`). Osservato il 2026-10-16: un link con titolo valido è tornato prima senza titolo, poi con il titolo della pagina di controllo del sito di destinazione ("Just a moment..."). Causa probabile, non verificabile da qui: il titolo è quello che Google ottiene dal sito di destinazione quando il sito mostra una pagina di controllo anti-bot. Il sistema non tenta di aggirare la protezione e non visita i link.
  - Link coperti: `materials[].link` (compiti, materiali, annunci) e `assignmentSubmission.attachments[].link` (consegne).
  - Titoli anti-bot riconosciuti da una lista esatta (`cls_registry._ANTIBOT_TITLES`, dopo normalizzazione di spazi, maiuscole e puntini) più il prefisso "checking your browser before accessing ". Titoli simili ma legittimi ("Just a moment in history") restano validi.
  - Link degradato con **stesso url unico** in una versione registrata: `title` e `thumbnailUrl` ripresi da quella versione, nessuna nuova versione. Url duplicati nello stesso elemento: nessuna scelta, resta come letto.
  - Prima lettura già degradata: il RAW resta com'è; il normalizzato riporta `title: null` e `titolo_non_determinabile: "titolo_antibot_della_fonte"`, con avviso "NON DETERMINABILE".
  - Url cambiato, titolo cambiato in un altro titolo valido, altri campi cambiati: restano aggiornamenti reali.
- **ClasseViva, assenze ri-emesse dopo la giustificazione** (Blocco 1, 2026-10-19, test `tests/test_absence_identity.py`). ClasseViva può assegnare un `evtId` nuovo all'assenza giustificata: senza correzione la vecchia risultava scomparsa e ne nasceva una seconda.
  - Chiave dell'assenza: `(evtDate[:10], evtCode, evtHPos)` (`cvv_registry.absence_key`).
  - `resolve_absences`: un `evtId` già noto aggiorna il suo record. Un `evtId` nuovo viene ricollegato a un record non più letto **solo se** la chiave coincide, c'è un solo candidato, una sola lettura con quella chiave e il nuovo `evtId` numerico è maggiore del vecchio.
  - Il record mantiene `source_uid` e `legacy_uid` (`cvv:<evtId originario>`), il RAW nuovo diventa una nuova versione e il registro scrive l'evento `reidentified` con provenienza (`regola: assenza_rinumerata`, chiave, `evtId` precedente e nuovo, versione precedente, campi cambiati).
  - Casi ambigui: **trattenuti** (non registrati e candidati non marcati come scomparsi, `registry.ingest(hold=...)`), con avviso e conteggio in `riconciliazione` del dry-run. Assenze senza data o codice: non registrate.
  - Consumatori: la riga Notion "🕘 Assenze" e gli eventi di `school_records` usano `legacy_uid`, quindi l'assenza giustificata aggiorna la riga esistente.
- **Stato `missing_in_source` verso Notion** (Blocco 1 per le assenze; 2026-10-19 per l'agenda, test `tests/test_agenda_missing_status.py`). `notion_dashboard` confrontava `source_status` con `"missing"`, valore che il registro non usa mai (`registry.MISSING = "missing_in_source"`): un'assenza o un evento di agenda sparito dalla fonte restava "presente". Ora il piano produce `flag_missing` (solo `update_properties` dello Stato: "Non più nel registro"), mai cancellazioni; le righe "Fatto" o già segnate non vengono toccate.

- **Elemento ricomparso nella fonte** (2026-10-19, test `tests/test_agenda_missing_status.py`, percorso completo Registry → piano → esecuzione simulata → rilettura → piano vuoto). Prima la riga Notion di una verifica/compito ricomparso restava "Non più nel registro" per sempre (nessuna operazione proposta). Ora `notion_projection.plan_ops` produce `restore` e la dashboard `restore_row`: solo `Stato` → Stato iniziale del database ("Da preparare"/"Da fare"), solo se la riga era marcata "Non più nel registro"; "Fatto" e gli altri Stati dell'utente mai toccati; controllo di sicurezza dedicato in `check_ops_safe`/`check_plan_safe`. Le righe generiche (assenze, voti...) già si ripristinavano.
- **Fonti alternative per i titoli non determinabili** (verifica del 2026-10-19, sola lettura): per i 2 link reali senza titolo lo stesso url con un titolo valido non compare in nessun altro record o versione del Registry. Nessuna euristica aggiunta: restano "non determinabili" e il job li riporta come anomalia `info`.

**Ancora aperto**
- Le assenze trattenute come ambigue compaiono negli avvisi, nei run e nel riepilogo/notifiche del job (`docs/automazione-scuola.md`), non in Notion (proposta separata, § 10 di quel documento).
- Per i link con titolo non determinabile non esiste un recupero successivo: si aggiornano solo se Classroom restituisce di nuovo il titolo.

## Rollback
Fase 1 aggiunge solo file nuovi: per tornare indietro basta il revert del commit. Nessun dato esterno modificato.

## Da decidere / rinviato
- `readStatus` fuori dall'hash: proposta, in attesa di conferma.
- Collegamento del registro a `server.py` (tool di dry-run live e di sync): fase successiva, richiede riavvio di Claude Desktop.
- Migrazione/seed delle `projections` per i record già in Notion (Verifiche, Assenze): non fatto.
- Lock file per esecuzioni concorrenti: fase motore di sync. (Oggi esiste il lock non bloccante `registry.lock` della sync manuale; manca uno scheduler che lo usi.)

## Documentazione collegata
- Architettura e decisioni: `docs/architettura-scuola.md`
- Agenti per materia: `docs/agente-materia-template.md`
- Job, scheduler, notifiche, autorizzazioni: `docs/automazione-scuola.md`
- Ricerca consumo token Claude Code: `docs/ricerca-token-claude-code.md`
