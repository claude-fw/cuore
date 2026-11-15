# Integrazione ClasseViva → Registry locale → Claude e dashboard CUORE

Stato: **bridge attivo in Claude Desktop, lettura reale verificata; fase di hardening (v0.2.0) completata sul codice, da ricaricare con riavvio di Claude Desktop e verificare dal vivo. Nessuna integrazione con Notion (rimossa il 2026-10-24).**
Aggiornato: 2026-10-24 (architettura); il resto del documento descrive lo stato al 2026-10-03.

## Architettura
```
ClasseViva (web.spaggiari.eu/rest/v1, API NON ufficiale)
   │  HTTPS, sola lettura, login con credenziali dal Keychain
   ▼
mcp/classeviva  (server MCP locale, stdio, Python stdlib)   ← Claude Desktop / Claude Code
   │  strumenti cv_* e school_* (dati normalizzati, Registry locale, sola lettura)
   ▼
Claude  (ragiona, risponde, decide)  ·  dashboard CUORE (`app/`, sola lettura)
```
Regola: ClasseViva = fonte primaria; Registry locale = dati di CUORE; Claude = orchestrazione. Notion non fa parte del
sistema (integrazione rimossa il 2026-10-24). In caso di differenza vince ClasseViva e si segnalano i conflitti rilevanti.

## Componenti e percorsi
| Cosa | Dove |
|---|---|
| Mini framework MCP riusabile (stdlib) | `~/Developer/cuore/scuola/mcp/_common/cloud_mcp.py` |
| Client ClasseViva (sola lettura) | `~/Developer/cuore/scuola/mcp/classeviva/cvv_client.py` |
| Server MCP | `~/Developer/cuore/scuola/mcp/classeviva/server.py` |
| Setup una tantum (Keychain + test + registrazione) | `~/Developer/cuore/scuola/mcp/classeviva/setup.sh` |
| Registrazione Claude Desktop/Code (con backup) | `~/Developer/cuore/scuola/mcp/classeviva/register_claude.py` |
| Risoluzione materia (pura) | `~/Developer/cuore/scuola/mcp/classeviva/cvv_subject.py` |
| Test (registro finto) | `~/Developer/cuore/scuola/mcp/classeviva/tests/` (`test_bridge.py`, `test_subject.py`, `test_absences.py`, `test_records.py`, `test_registry.py`, `test_cvv_registry.py`: 128 test) |
| Notion Verifiche (data source) | `collection://<id-del-tuo-database>` |
| Notion Compiti (data source) | `collection://<id-del-tuo-database>` |

Dipendenze: **nessuna** oltre a `python3` ≥ 3.9 (e `security` di macOS). Nessun pip/npm/Homebrew installato.

## Attivazione (una volta, sul Mac)
In Terminale: `bash ~/Developer/cuore/scuola/mcp/classeviva/setup.sh`
1. controlla python3; 2. chiede utente e password (la password la digiti nel prompt del Keychain: non appare né finisce nella cronologia); 3. prova il login reale; 4. registra il server `classeviva` in Claude Desktop (JSON, backup datato, non tocca altri server) e in Claude Code (`claude mcp add --scope user`). Poi riavvia Claude Desktop (Cmd+Q).
Al primo uso macOS può chiedere di consentire l'accesso al Keychain: scegli "Consenti sempre" solo se riconosci `security`/Python.

## Autenticazione e segreti
- Password solo nel Keychain (servizio `cloud.classeviva`, account = username). Mai in file, repo, log o config MCP.
- Il token di sessione resta in memoria del processo; non viene scritto.
- `claude_desktop_config.json` contiene solo il percorso di python e del server.
- Rimozione: `security delete-generic-password -s cloud.classeviva`.

## Strumenti MCP (tutti in sola lettura)
`cv_status`, `cv_agenda`, `cv_tests` (restituisce anche `da_verificare`), `cv_homework`, `cv_next_test(subject)`, `cv_subjects`, `cv_lessons`, `cv_grades`, `cv_noticeboard`, `cv_absences`, `cv_diagnostics`, `cv_sync_plan(kind, notion_rows, from/to)`, `cv_registry_dryrun(kinds, simulate, second_read)` (dry-run live del registro locale: non scrive nulla), `cv_registry_sync(kinds, commit, expect_create)` (sync manuale nel registro locale: default simulazione, scrive solo con `commit=true`).

## Hardening v0.2.0 (2026-10-03)
### Materia degli eventi (`cvv_subject.py`)
Mai assegnata per supposizione. Criteri in ordine; ogni evento riporta `subject`, `subject_status` (determinata / da_verificare), `subject_label`, `subject_criterion`, `subject_confidence`, `subject_candidates`.
| Criterio | Confidenza | Quando |
|---|---|---|
| `api:subjectDesc` | alta | l'API indica la materia |
| `testo:nome-materia` | alta | il testo cita una materia fra i candidati del docente |
| `docente-unico` | media | il docente insegna una sola materia (flag `ALLOW_UNIQUE_TEACHER`) |
| `testo:argomento` | media | docente multi-materia (es. storia/filosofia, matematica/fisica, italiano/latino) e lessico di un solo candidato |
| **Materia da verificare** | - | ambiguità, segnali contrastanti, conflitto testo/docente, docente sconosciuto |
Educazione civica / studio assistito solo se citate nel testo. `cv_tests(subject=...)` filtra per chiave materia (latino↔latina, ecc.) e separa le voci sicure da quelle `da_verificare` che hanno quel candidato.
### Endpoint e zero
- Le liste sono lette in modo stretto: se manca la chiave attesa → errore esplicito, mai "0" silenzioso.
- Compiti: v1 non ha endpoint dedicato; sono eventi agenda `AGHW`. `cv_homework` usa due fonti (agenda/all filtrata + agenda/AGHW), unisce per evtId e riporta le `fonti`. L'API web "w1" (`homeworks/index`) usa cookie e non è implementata.
- Voti: `/students/{id}/grades`; 0 voti = chiave presente e lista vuota. `cv_diagnostics` mostra per endpoint ok/elementi e distingue "vuoto" da "non disponibile".
### Intervallo date valido (v0.2.1)
ClasseViva accetta solo date nell'anno scolastico (1 settembre - 31 agosto; verificato sul registro reale: "dates must be beween 20260901 and 20270831", il refuso è dell'API). `cv_diagnostics` limita le finestre all'anno scolastico corrente (`school_year_range`) e, se l'API rifiuta ancora l'intervallo, usa quello indicato nel messaggio d'errore (`parse_valid_range`) e riprova una volta. Gli altri strumenti non applicano il limite: date fuori anno danno errore esplicito.
### Scelte di classificazione
- **"Tema in classe"** resta un **evento** (non una verifica): non entra nel database Verifiche e non viene sincronizzato come verifica. Scelta dell'utente, 2026-10-03; ridiscutibile in futuro.
- Le verifiche sono dedotte da parole chiave nel testo (verifica, interrogazione, test...).
### Assenze (v0.3.0)
`cv_absences` separa quattro livelli: **raw** (10 campi ClasseViva esatti, `evtValue` resta dato grezzo), **interpreted** (solo tabelle verificate), **derived** (nome), **manuali** (Note, solo in Notion). Hash solo sul raw; chiave `cvv:<evtId>`; `cv_sync_plan(kind="assenza")` finestra = anno scolastico, nessuna cancellazione.
Interpretazioni **verificate sulla UI web ufficiale** (UI web): `ABR0` = Entrata in ritardo; `ABA0` = Assenza; `webJustifStatus` 1 = In approvazione, 0 = Da giustificare; `evtValue` = ore perse **solo per ABR0** (un solo caso: 30/09, "1 ora persa"). Ogni altro codice/valore resta **"Da verificare"** (grezzo conservato, segnalato in `da_verificare`). Scadenza giustificazione: non esposta dall'API né visibile nella UI, nessun campo.
**Versione della logica interpretativa** (separata dall'hash): `interp_version` = `r<revisione>-<impronta delle tabelle>`. Le tabelle sono incluse automaticamente (se ne modifichi una la versione cambia da sola); la revisione `ABSENCE_RULES_REV` si incrementa a mano solo se cambia la *logica* di `interpret_absence`. In Notion la versione vive nella proprietà "Versione interpretazione". `plan_sync` ha quattro esiti: `create`; `update` (cambia il dato ClasseViva: riscrivi grezzo + interpretazioni + versione); `reinterpret` (dato invariato, versione diversa: riscrivi SOLO interpretazioni + versione); `unchanged`. Mai toccati: Note e campi manuali; il grezzo cambia solo con `update`. Per nuove interpretazioni basta `interp_version(rev, *tabelle)` e un campo `interp_version` negli item.
Non verificati: altri codici (ritardo breve, uscita), altri valori di webJustifStatus, significato di `hoursAbsence` (sempre `[]`), cambio di `isJustified` dopo l'approvazione.
### Limiti residui
- Voti e compiti reali: 0 osservati prima dell'hardening, da rivalutare con `cv_diagnostics` dopo il riavvio.
- Mappatura codici assenza (ABR0/ABA0) probabile, non verificata.

## Come si identifica un evento / niente duplicati
- Chiave: `uid = cvv:<evtId>` (ID evento ClasseViva), salvata in Notion nel campo **ID ClasseViva**.
- Se manca l'ID: `sec:<sha1(materia|data|testo)>` (identificatore secondario).
- **Hash** = impronta di tipo, materia, data, orari, testo: se cambia, la voce è stata modificata → aggiorna.
- `cv_sync_plan` confronta registro e Notion e restituisce `create / update / unchanged / missing_in_registry`.
- **Nessuna cancellazione automatica**: le voci non più nel registro (dentro la finestra) vengono solo marcate Stato = "Non più nel registro".

## Protocollo di sync (per Claude)
1. `notion-query-data-sources` sul database giusto: `SELECT url, "ID ClasseViva", "Hash", "date:Data:start" ...` (per Compiti: `"date:Scadenza:start"`).
2. `cv_sync_plan(kind="verifica"|"compito", notion_rows=[{uid, hash, page_id, date}], from_date, to_date)`.
3. `create` → `notion-create-pages` (Fonte = ClasseViva, Ultimo aggiornamento = oggi); `update` → `notion-update-page` sulla `page_id` indicata (aggiorna anche Hash); `missing_in_registry` → solo Stato "Non più nel registro" e segnalazione all'utente.
4. Riepilogo: quante create/aggiornate/invariate/mancanti e conflitti significativi.
Campi: Verifiche = Nome, Materia, Tipo, Data, Descrizione, Stato, ID ClasseViva, Hash, Ultimo aggiornamento, Fonte. Compiti = Nome, Materia, Scadenza, Descrizione, Stato, ID ClasseViva, Hash, Ultimo aggiornamento, Fonte.
Lo Stato scelto dall'utente (es. "Fatto") non va sovrascritto da un update.

## Limiti noti
- **API non ufficiale** (ricostruita dalla community): può cambiare senza preavviso; Spaggiari non la supporta. Le intestazioni richieste (User-Agent/chiave) sono quelle dell'app Android documentate pubblicamente.
- **Account studente**: la community segnala che gli account tipo S potrebbero non avere accesso all'API. Da accertare con il login reale (Test A). Se fallisce → fallback browser (non implementato).
- **"Verifica" non è un campo**: ClasseViva ha solo agenda/annotazioni/compiti. `kind` è DEDOTTO da codice evento (AGHW=compito) e parole chiave nel testo (verifica, interrogazione, test…). Può sbagliare: Claude deve segnalare l'incertezza.
- La materia manca spesso negli eventi di agenda: vedi criteri sopra; altrimenti "Materia da verificare".

## Test
Eseguiti su registro FINTO (non contano come test sul registro reale): login, id studente, password mai negli errori, classificazione, paginazione finestre da 31 giorni senza duplicati, ricerca materia, piano di sync (creazione / invariato / aggiornamento / mancante senza cancellazione), protocollo MCP via stdio, registrazione su config Desktop finta (idempotente, rifiuta JSON invalido). Notion: creata, riletta, aggiornata una voce `[TEST]` con ID `test:0001`: 1 sola riga dopo la seconda sync, aggiornata correttamente.
Lancio: `cd ~/Developer/cuore/scuola/mcp/classeviva && python3 -m unittest discover -s tests -v`

## Manutenzione e ripristino
- Se Claude non vede gli strumenti: riavvia Claude Desktop; `claude mcp list` (Code); `python3 server.py --selftest` verifica login.
- Se ClasseViva cambia API: modifica solo `cvv_client.py` (profili intestazioni, percorsi); i test sul mock vanno aggiornati di conseguenza.
- Ripristino config Desktop: copia di backup `claude_desktop_config.json.bak-<data>` nella stessa cartella.
- Nuovi MCP: aggiungere `mcp/<nome>/server.py` usando `_common/cloud_mcp.py`.

## Registro locale (Fase 1 del sistema Scuola)
Vedi `docs/registro-scuola.md`: registro SQLite append-only per RAW, versioni, hash e stato dei record; Notion come proiezione. Non ancora collegato a `server.py`.

## Google Classroom (fonte separata)
Test OAuth minimo (solo `courses.readonly`): vedi `docs/classroom-probe.md`. Nessun adapter, nessuna sync.
