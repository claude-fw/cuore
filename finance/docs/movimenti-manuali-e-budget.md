# Movimenti manuali, riconciliazione con la banca, budget e rotazione dei backup (01/11/2026)

Stato: **attivo dal 03/11/2026** (C-07): v7 in `MIGRATIONS`, `EXCLUDED_IDS_SQL` aggiornata, database reale migrato
da 6 a 7 dopo un backup verificato (`backup/cloud-finance-prima-di-v7-2026-11-03T22-25-45.sqlite3`, contenuto identico
all'originale). La sezione sotto resta come storia della procedura.

## Perché non era attivo (fino al 03/11)
La migrazione v7 (`src/db/migrations/007_manual_and_budgets.ts`) è in `PENDING_MIGRATIONS`, non in `MIGRATIONS`:
`runQuery` (ponte, CLI) rifiuta un database con versione diversa da `SCHEMA_VERSION`, quindi registrarla senza migrare
il database reale (v6) romperebbe la dashboard.

**Attivazione** (autorizzazione del titolare):
1. aggiungere `migration007` in coda a `MIGRATIONS` e lanciare `npm run migrations:generate`;
2. in `EXCLUDED_IDS_SQL` (`services/effective.ts`): aggiungere `UNION SELECT transaction_id AS id FROM manual_voids`
   (annullati fuori da riepiloghi e consultazione) e non escludere i `possible_duplicate` il cui collegamento è in
   `link_reversals` (abbinamento revocato: il manuale torna a contare);
3. migrare il database reale con backup verificato (stessa procedura di `import-plan --apply`);
4. `npm run check` e test del ponte (`app/tests/test_money_bridge.py`).

## Movimenti manuali (`src/services/manual.ts`)
- `addManualTransaction`: conto esistente e aperto alla data, valuta del conto, importo diverso da zero, descrizione;
  `requestId` del client = idempotenza (`source_txn_id = manual:<requestId>`); possibili doppioni (stesso conto e importo
  a ±3 giorni) restituiti senza scrivere finché non arriva `confirmDuplicate: true`; impronta uguale a quella
  dell'importazione; `booked` (predefinito) o `pending`.
- `correctManualTransaction`: solo movimenti manuali non annullati, con motivo; data, importo, descrizione, controparte,
  passaggio da `pending` a `booked`; mai il contrario.
- `voidManualTransaction`: riga append-only in `manual_voids` con motivo; nessuna cancellazione.
- `manualHistory`: storico da `audit_log` (prima/dopo, motivo).
- I movimenti importati non si correggono: restano come la fonte (si interviene con classificazione e collegamenti).

## Riconciliazione manuale ↔ banca (`src/services/manual-matching.ts`, A4)
- Contratto esistente: collegamento `possible_duplicate` da **banca** (`from`, fonte originale, resta) a **manuale**
  (`to`); confermato, il manuale esce da riepiloghi e riconciliazioni (`EXCLUDED_IDS_SQL`, già attivo). Nessuna fusione
  né cancellazione.
- `detectManualMatches`: candidati = stesso conto, valuta e importo, movimento importato, data a ±3 giorni; esclusi
  manuali annullati o già riconciliati, movimenti bancari esclusi o già usati, coppie già collegate in qualsiasi stato
  (un rifiuto non torna). Esito per manuale: «univoco» o «ambiguo» (più candidati da una parte o dall'altra): tutte le
  coppie proposte e un elemento di revisione `possible_duplicate` per il manuale. **Mai una conferma automatica.**
  Ripetibile: una seconda esecuzione non propone nulla di nuovo.
- `confirmManualMatch`: controlla che manuale e movimento siano ancora liberi; nello stesso batch rifiuta le altre
  proposte che li coinvolgono e chiude le revisioni rimaste senza proposte.
- `rejectManualMatch` (con motivo), `reverseManualMatch` (revoca con motivo in `link_reversals`, il collegamento resta
  nello storico), `manualMatchState` (`aperto` · `proposto` · `ambiguo` · `riconciliato` · `annullato`).
- Fino all'attivazione (passo 2 sopra) una revoca riapre il manuale per il rilevatore, ma `EXCLUDED_IDS_SQL` lo esclude
  ancora dai riepiloghi.

## Budget (`src/services/budgets.ts`)
- `setBudget`: ambito `category` (con sottocategorie), `budget_group` (`needs`/`wants`/`savings`) o `total`, valido da
  un mese; versioni append-only, vale l'ultima con `valid_from` ≤ mese; importo `null` = budget tolto.
- `budgetStatus(mese, valuta)`: speso = uscite e commissioni contabilizzate con classificazione decisa (come
  `domain/summary.ts`); a parte, senza sommarli: uscite non classificate, in attesa, rimborsi. Nessuna categoria
  viene assegnata dal budget.

## Rotazione dei backup (`src/domain/backup-retention.ts`)
Pianificatore puro dei backup `<db>-prima-di-<sync|apply>-<istante>.sqlite3`: tiene gli ultimi N, l'ultimo di ogni
giorno e di ogni mese entro i limiti dati, sempre il più recente; ignora i nomi estranei; **non cancella**. Politica
(proposta: 7 ultimi, 30 giornalieri, 12 mensili) e applicazione (spostamento nel Cestino) da decidere.

## Test
`test/manual-budgets.test.ts` (10), `test/manual-matching.test.ts` (7), `test/backup-retention.test.ts` (4), anche con
il backend D1 (`npm run test:d1`); prove di mutazione rilevate il 01/11: esclusione degli annullati, idempotenza, coppie
già collegate, movimento bancario già usato.
