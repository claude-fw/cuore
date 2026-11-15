# Ricerca a testo libero sui movimenti (blocco A6, 03/11/2026)

Stato (06/11/2026, branch `dati-veri-online`): **attiva nel codice, database reale ancora v7**. `migration008` è in
`MIGRATIONS` (schema del codice = 8, `PENDING_MIGRATIONS` vuoto), `search` è in `OPERATIONS` (anche strumento MCP),
`migrations/0008_search.sql` generato. Mac e cloud hanno così la stessa versione prima della migrazione M3.
**Attenzione:** con questo codice un database v7 risponde `schema_mismatch` a ogni consultazione e `scrivi.ts` rifiuta
le scritture finché il database reale non passa a v8 (passo 4 di «Attivazione»): unire il branch e migrare il database
reale vanno fatti insieme, poi si riavvia il ponte.

## Cosa fa
- Cerca in **descrizione, controparte e nota dei movimenti manuali** (motivo della prima riga di audit
  `add_manual_transaction`). Ogni parola vale come prefisso, servono tutte (AND); maiuscole e accenti ignorati.
- Filtri facoltativi: `from`/`to` (data effettiva), `accountIds` (max 20), `categoryId` (`null` = senza categoria);
  `limit` 1–50 (predefinito 20); risultati dal più recente. `truncated: true` = ci sono altri risultati.
- Esclusi doppioni confermati, pending sostituite e manuali annullati (`EXCLUDED_IDS_SQL`).
- Regole della consultazione v1: sola lettura, importi in unità minime con la loro valuta (mai sommati), IBAN e numeri
  lunghi mascherati in descrizione, controparte e nota.
- **Nessun oracolo sui dati mascherati**: un movimento è restituito solo se ogni parola compare anche nel testo
  mostrato (mascherato). Cercare cifre di un IBAN o di una carta non rivela quali movimenti le contengono; restano
  cercabili le 4 cifre finali che la maschera mostra.

```bash
npm run cli -- query search --input '{"q":"caffè centrale","from":"2026-01-01","accountIds":["conto"]}'
```

| Campo d'ingresso | Note |
|---|---|
| `q` (obbligatorio) | max 100 caratteri; parole = sequenze di lettere e cifre; quelle di 1 carattere ignorate; max 8 parole |
| `from`, `to`, `accountIds`, `categoryId`, `limit` | come sopra; campi non previsti rifiutati (`invalid_input`) |

Uscita: `terms` (parole normalizzate), `items` (come `transactions` più `note`, senza `excluded`), `limit`,
`truncated`. Errori: `invalid_input`, `not_found` (conto), **`not_available`** (database senza v8: le altre
operazioni restano invariate), `schema_mismatch`, `internal`.

## Disegno
- `transactions_search_source` (vista): `rowid` del movimento, descrizione, controparte, nota.
- `transactions_search`: tabella virtuale **FTS5 a contenuto esterno** sulla vista (nessuna copia del testo),
  tokenizer `unicode61 remove_diacritics 2`.
- Trigger (nessuno di cancellazione: `transactions` e `audit_log` restano append-only):
  nuovo movimento → indicizzato; descrizione o controparte cambiate → vecchi valori tolti (`'delete'`) e nuovi
  inseriti (i cambi di sola data, stato o importo non toccano l'indice); prima audit `add_manual_transaction` con
  motivo → movimento reindicizzato con la nota.
- Riempimento iniziale: `INSERT INTO transactions_search(transactions_search) VALUES('rebuild')` nella migrazione.
- **Nessun SQL dall'input**: la domanda diventa parole di sole lettere e cifre, ognuna tra virgolette (`"parola"*`),
  passate come parametro di `MATCH`. `"`, `NEAR(`, `OR`, `AND`, `col:`, `*`, `^`, `'); DROP …` sono solo testo
  (o `invalid_input` se non resta nessuna parola).
- **Indice derivato**: `NOT_DERIVED` (`src/db/schema-check.ts`) esclude la tabella virtuale e le tabelle ombra
  (`_data`, `_idx`, `_config`, `_docsize`; `_idx` e `_config` sono WITHOUT ROWID) dalle impronte logiche
  (`logicalChecksum` di `scripts/import-plan.ts`); un ripristino che reinserisce i movimenti lo rigenera dai trigger
  (provato: `test/search.test.ts`).

## Disponibilità di FTS5 (verificata il 03/11/2026)
| Motore | Esito |
|---|---|
| `node:sqlite` (Node 24.21, SQLite 3.53.4) | FTS5 presente: tabella virtuale, vista come contenuto esterno, `rebuild`, `integrity-check`, trigger |
| D1 locale (Miniflare/workerd, `npm run test:d1`) | FTS5 presente: stessi test verdi |
| Durable Object SQLite (workerd, test di `cloud/`) | migrazione v8 applicata dalla vetrina sintetica, consultazione e backup verdi **con la modifica a `cloud/src/backup.ts` sotto** |

Nessun ripiego `LIKE` necessario: FTS5 c'è in tutti i motori provati. Se un motore futuro non lo avesse, la v8
fallirebbe alla migrazione (nessuno stato parziale: batch atomico) e `search` resterebbe `not_available`.

## Impatto su `cloud/` (da fare in un blocco che può toccare `cloud/`)
La vetrina e i test di `cloud/` migrano con `[...MIGRATIONS, ...PENDING_MIGRATIONS]`, quindi ricevono già la v8:
1. `cloud/src/backup.ts`, `PROJECT_TABLES_SQL`: aggiungere `AND ${NOT_DERIVED}` (import da
   `finance/src/db/schema-check.ts`). Senza, `SELECT * … ORDER BY rowid` fallisce su `transactions_search_config`
   (WITHOUT ROWID) e 14 test di backup/ripristino falliscono; con la modifica passano. Stessa regola per
   `logicalChecksumPortable` (usa la stessa query) e coerente con `logicalChecksum` di Node.
2. `cloud/test/worker.test.ts`: `versione_schema` atteso 7 → `SYNTHETIC_QUERY_SCHEMA_VERSION` (oggi 8).
3. `cloud/scripts/ripristina-backup.ts`: nessuna modifica necessaria (l'indice non è nel backup; i trigger lo
   ricostruiscono all'inserimento dei movimenti; `compareSchema` confronta anche le tabelle FTS, uguali).
Verificato su una copia di `cloud/` nella cartella temporanea: 62/62 test verdi con 1 e 2.

## Attivazione (con l'autorizzazione del titolare) — passi 1–3 FATTI il 06/11 nel codice
1. Fatte le modifiche a `cloud/` sopra.
2. `migration008` in coda a `MIGRATIONS`, `PENDING_MIGRATIONS = []`; `search` da `PENDING_OPERATIONS` a
   `OPERATIONS`; aggiornare i test che contano le operazioni (`consultation.test.ts`, `readonly-sqlite.test.ts`,
   `mcp-server.test.ts`: input valido per `search`) e `synthetic.test.ts`/`manual-budgets.test.ts`/`search.test.ts`.
3. `npm run migrations:generate` (crea `migrations/0008_search.sql` e aggiorna l'istantanea), `npm run check`.
4. Database reale: backup verificato, poi migrazione 7 → 8 (solo aggiunte: vista, tabella virtuale, trigger,
   `rebuild`), `integrity-check` FTS (`INSERT INTO transactions_search(transactions_search, rank)
   VALUES('integrity-check', 1)`), test del ponte (`app/tests/test_money_bridge.py`).
5. Da sapere: `wrangler d1 export` non esporta i database con tabelle virtuali (limite di D1 riportato dalla
   documentazione Cloudflare, non verificato qui): il backup L1 è un'esportazione logica e non ne dipende.
