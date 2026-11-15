# Consultazione in sola lettura (contratto v1)

Stato: **implementata e verificata solo in locale** (SQLite via Node, D1 locale, nucleo dentro workerd) con dati
sintetici. **Server MCP: solo il prototipo stdio locale** (`scripts/mcp-server.ts`, sezione «MCP locale in sola
lettura» sotto), **non registrato** in alcun client: questo e' il livello che il server MCP e la dashboard
chiameranno. Codice: `src/query/consultation.ts` (operazioni) e `src/query/operations.ts` (contratto, validazione,
busta di risposta).

## Principi
- **Sola lettura garantita**: ogni operazione passa da `readOnlyDb`, che rifiuta qualsiasi scrittura; i servizi
  riusati ricevono un contesto senza orologio ne' generatore di id. CLI `query`, server MCP e CUORE Money leggono con
  `readImmutable` (`src/db/node-sqlite.ts`): un'istantanea coerente dei byte del file viene copiata in memoria e solo
  quella viene interrogata (nessun lock, nessun `-wal`/`-shm`, scrittore mai bloccato). Copia rifiutata con `-wal`
  non vuoto o `-journal` presente (scrittura in corso) e scartata, con nuovi tentativi, se il file cambia mentre viene
  letto; `quick_check` sulla copia. Provato con uno scrittore concorrente reale (`test/readonly-sqlite.test.ts`):
  nessuna lettura incoerente, mentre un'apertura `immutable=1` sul file vivo senza controlli ne produce.
- **Nessuna logica contabile duplicata**: riepilogo = `periodReport`; saldi = `liquidity`; crediti/debiti =
  `listObligations` + `obligationsPosition`. Tipo, categoria ed esclusioni dei movimenti vengono da un'unica
  definizione SQL (`src/services/effective.ts`) usata anche dal riepilogo: la somma dei movimenti elencati coincide
  con il riepilogo per valuta e tipo (provato su tutte le pagine).
- **Nessun dato inventato**: un saldo e' sempre un saldo **osservato** di fine giornata, mai ricostruito dai movimenti.
- **Dati personali ridotti**: nessuna riga grezza, nome di file, impronta, id della fonte o esecuzione d'importazione;
  IBAN e sequenze di 8+ cifre nelle descrizioni mascherati (`[IBAN]`, `[…1234]`). Il database resta intatto.
- Importi interi in **unita' minime** della valuta indicata accanto; valute mai sommate tra loro.

## Busta di risposta
```json
{ "ok": true,  "contractVersion": 1, "operation": "summary", "data": { } }
{ "ok": false, "contractVersion": 1, "operation": "summary", "error": { "code": "invalid_input", "message": "..." } }
```
`runQuery(db, operazione, input, { today })` non lancia mai eccezioni. `today` (YYYY-MM-DD) e' deciso dal chiamante
(la CLI usa Europe/Rome) e vale solo quando `asOf` manca: il nucleo non legge mai l'orologio di sistema.

| Codice | Quando |
|---|---|
| `invalid_input` | input non oggetto, **campi non previsti**, date non valide, periodo invertito, valori fuori elenco, limiti superati, cursore non valido o usato con filtri diversi |
| `unknown_operation` | operazione inesistente (il messaggio elenca quelle disponibili) |
| `not_found` | conto indicato inesistente |
| `schema_mismatch` | database vuoto o a una versione diversa da quella del codice: nessuna lettura dei dati |
| `not_available` | operazione che richiede una migrazione non applicata (oggi solo `search`, v8 in attesa: [ricerca.md](ricerca.md)) |
| `internal` | qualsiasi altro errore; messaggio generico, nessun dettaglio SQL esposto |

## Operazioni
| Operazione | Input | Output principale |
|---|---|---|
| `categories` | — | `items`: id, nome, `parentId`, `budgetGroup` (`needs`/`wants`/`savings`; `null` = senza gruppo, contato come `unassigned` in `summary`) |
| `planning` | — | `goals`: nome, valuta, `targetMinor`, `deadline` (null = senza scadenza), `status` **dichiarato** (`active`/`paused`/`reached`/`cancelled`), avanzamento **calcolato** dagli accantonamenti (`allocatedMinor`, `remainingMinor`, `progressBp`, `reached`): i due possono divergere e nessuno viene corretto d'ufficio; `purchases`: titolo, valuta, `estimatedMinor` (null = stima non registrata; il motore rifiuta stime <= 0), `priority`, `status`, `goalId`, `linkedToTransaction`. Nessun id di blocchi Notion o di movimenti |
| `accounts` | — | id, nome, tipo, valuta, istituto, profilo dichiarato (intestatario formale, utilizzatore, supervisione familiare: `unknown` se non dichiarato), stato operativo (chiuso, data di chiusura solo se nota con il suo stato, ultimo movimento osservato separato) |
| `summary` | `from`, `to` (obbligatori), `accountIds?`, `currency?` | per valuta: entrate, spese, commissioni, rimborsi, netto, trasferimenti, crediti/debiti (`obligationsIn/Out`), da classificare, pending, spesa per categoria e gruppo; `excludedDuplicates`, `openReviewItems`, `notes` (avvertenze di completezza) |
| `daily` | `from`, `to` (obbligatori, al massimo 93 giorni) | per giorno (solo quelli con movimenti, in ordine) e valuta: `income`, `netSpending` (spese + commissioni - rimborsi), `unclassifiedOut`, `pendingCount`, con le stesse regole di `summary` (stesso servizio, `dailyReport`): la somma dei giorni coincide con il riepilogo del periodo. Aggiunta additiva del 04/11 |
| `balances` | `asOf?`, `accountIds?` | per conto: `status` (`observed`/`missing`/`conflict`), `balanceMinor` (null se non osservato o in conflitto), `observedOn`, `ageDays`, ultima `reconciliation` che termina in `observedOn`, `bookedMovementsAfter`, `pendingAfter`, `verifiedAsOf`, `notes`; `totals` per valuta con `complete` e `allVerifiedAsOf` |
| `transactions` | `from`, `to` (obbligatori, sulla data effettiva), `accountIds?`, `statuses?`, `kinds?`, `categoryId?` (`null` = senza categoria), `includeExcluded?`, `limit?`, `cursor?` | `items` (data effettiva, date, stato, importo, valuta, descrizione e controparte mascherate, tipo effettivo, categoria, stato della classificazione, `excluded`), `nextCursor` |
| `obligations` | `asOf?`, `status?` (`open` predefinito, `closed`, `all`), `direction?`, `currency?`, `counterpartyId?` | per credito/debito: controparte, residuo e componenti, scadenza, `daysToDue`, `overdue`, movimenti collegati; `position` per valuta; `truncated` |
| `data_status` | `asOf?`, `staleAfterDays?` | per conto: ultima importazione riuscita e suo esito, ultimo movimento, ultimo saldo, ultima riconciliazione, pending; elementi da verificare aperti per tipo; `anomalies` (nessun avviso di freschezza o saldo mancante per i conti chiusi) |
| `overview` | `from`, `to` (obbligatori), `asOf?` (predefinito `to`) | per valuta: entrate divise in stipendio (`stipendio`), sostegno familiare (`sostegno-familiare`), interessi (`interessi`) e altre; spesa reale (acquisti + commissioni - rimborsi); trasferimenti tra conti propri contati una volta e netto verso i conti di risparmio; da classificare; denaro rimasto per conto, per istituto e totale dai SOLI saldi osservati dei conti attivi; accantonato (conti `savings` attivi); data dell'ultimo movimento per conto e note sui dati mancanti |
| `manual` | `from`, `to` (obbligatori, data del movimento) | movimenti inseriti a mano (al massimo 200, `truncated`): id, conto, date, stato, importo, valuta, descrizione mascherata, `voided` e `voidedAt` (annullati compresi: restano nello storico, fuori da elenchi e riepiloghi). Aggiunta additiva del 04/11 (A5) |
| `budgets` | `month?` (YYYY-MM, predefinito il mese di oggi), `currency?` | per valuta con un budget: righe in vigore nel mese (ambito, budget, speso, residuo, superato) e a parte uscite da classificare, in attesa e rimborsi (servizio `budgetStatus`). Aggiunta additiva del 04/11 (A5) |

`verifiedAsOf` e' vero **solo** se il saldo e' stato osservato proprio in `asOf` e l'ultima riconciliazione che
termina in quella data ha esito `ok`. Un saldo di giorni precedenti, con movimenti successivi, non riconciliato, in
conflitto o assente non e' mai presentato come saldo corrente verificato: `notes` spiega il perche'. I `totals`
sommano solo saldi osservati e dichiarano `complete: false` se un conto della valuta non ha un saldo utilizzabile.

Anomalie di `data_status`: `no_imports`, `stale_import` (ultima importazione riuscita piu' vecchia della soglia),
`stale_data` (ULTIMO MOVIMENTO del conto piu' vecchio della soglia rispetto alla data richiesta: un estratto vecchio
importato oggi resta vecchio; i conti chiusi sono esclusi),
`last_import_rejected`, `balance_missing`, `balance_conflict` (errore), `reconciliation_difference` (errore, dagli
elementi da verificare aperti: periodo, esito, differenza).

Operazione **in attesa** (`PENDING_OPERATIONS`, non negli strumenti MCP): `search`, ricerca a testo libero sui
movimenti, solo su un database con la v8; vedi [ricerca.md](ricerca.md).

## Limiti e paginazione
| Limite | Valore |
|---|---|
| movimenti per pagina | predefinito 50, massimo 200 |
| conti per richiesta | 20 (D1 accetta al massimo 100 parametri per istruzione) |
| crediti/debiti restituiti | 200 (`truncated: true` oltre; la posizione li include comunque) |
| soglia dati vecchi | 7 giorni predefiniti, 1-366 |

Paginazione a chiave (data effettiva, id): ordine stabile, nessun movimento ripetuto o saltato tra le pagine. Il
cursore e' opaco e contiene i filtri: riusarlo con filtri diversi e' rifiutato. I dati importati tra una pagina e
l'altra compaiono solo se successivi al cursore (nessuna istantanea isolata).

## CLI
```bash
npm run cli -- query summary --input '{"from":"2026-01-01","to":"2026-01-31"}'
npm run cli -- query transactions --input '{"from":"2026-01-01","to":"2026-01-31","kinds":["expense"],"limit":20}'
npm run cli -- query balances
npm run cli -- query data_status --input '{"staleAfterDays":14}'
```
Il database deve esistere (mai creato o migrato da `query`); codice di uscita 0 se `ok`, 2 altrimenti.

## MCP locale in sola lettura (prototipo, non registrato)
```bash
node scripts/mcp-server.ts --db <database.sqlite3>
```
- stdio, JSON-RPC 2.0 una riga per messaggio, nessuna dipendenza; metodi `initialize`, `tools/list`, `tools/call`,
  `ping` (altri: errore -32601; JSON non valido: -32700);
- uno strumento per operazione del contratto (`cloud_finance_<operazione>`), schema d'ingresso derivato da
  `OPERATIONS`, annotazioni `readOnlyHint: true`, `destructiveHint: false`; risposta = busta di `runQuery` (testo +
  `structuredContent`), `isError` per gli errori del contratto;
- database letto con `readImmutable` a ogni chiamata (istantanea in memoria; con una scrittura in corso lo strumento
  risponde con `isError` e `database_unavailable`, mai con dati parziali); `asOf` assente = data odierna Europe/Rome (data di richiesta, distinta dalla data
  dell'ultimo movimento riportata da `overview`/`data_status`); solo protocollo su stdout, log senza dati su stderr;
- provato con test automatici su database sintetico e con una sessione sul database locale (file invariato).
Registrazione in un client (es. configurazione MCP dell'app) NON fatta: richiede autorizzazione, nessun segreto
necessario (solo il percorso del database).

Configurazione **remota** (solo dopo autorizzazione esplicita, non fatta): Worker con binding D1 in sola lettura sul
modello di Cloud Gmail (`private-google-mcp`), autenticazione, prova su D1 di prova secondo [prova-remota-d1.md](prova-remota-d1.md).
