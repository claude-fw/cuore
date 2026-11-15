# CUORE Money — contratto dei dati (motore → ponte → dashboard → assistente)

Stato: **in vigore dal 2026-10-24**, verificato con test su dati sintetici (`finance/test/consultation-planning.test.ts`,
`finance/test/readonly-sqlite.test.ts`, `app/tests/test_money_bridge.py`, `app/tests/test_offline_export.py`).

## Una sola fonte
Tutti i dati di CUORE Money vengono dalla **consultazione v1** del motore Finance (`runQuery`, `finance/docs/consultazione.md`).
Le stesse operazioni sono gli strumenti del server MCP (assistente) e la base di `/api/money` (dashboard): nessuna
logica contabile nel ponte o nella dashboard, nessuna seconda copia dei dati.

| Livello | Cosa fa | Cosa NON fa |
|---|---|---|
| Motore (`finance/src/query`) | calcola e restituisce le buste `{ ok, contractVersion, operation, data \| error }` | non legge l'orologio, non scrive |
| Lettura (`readImmutable`) | istantanea coerente in memoria; rifiuta scritture in corso | non prende lock, non crea file |
| Adattatore (`app/bridge/cuore_money.ts`) | sceglie periodo e operazioni, riduce i campi al minimo | non calcola importi |
| Ponte (`app/bridge/cuore_money.py`) | valida ogni busta campo per campo, aggiunge provenienza, aggiornamento ed esito | non corregge dati: una busta non conforme diventa `contratto_non_valido` |
| Dashboard (`app/index.html`) | mostra, distingue gli stati | non somma valute, non stima |

Operazioni lette da CUORE: `accounts`, `overview`, `summary`, `categories`, `planning`, `balances`, `data_status`,
`obligations`, `transactions` (ultimi 30 giorni, al massimo 50). Dal 04/11 (A5) anche `manual` (movimenti inseriti a mano
degli ultimi 30 giorni, annullati compresi con `voided`) e `budgets` (limiti di spesa del mese), aggiunte additive; ogni
movimento di `transactions` porta il suo `id` interno (casuale, mai quello della banca), che serve alle modifiche del
titolare quando il ponte reale è avviato con `--scritture` (app/README.md). `categories` e `planning` sono **aggiunte additive**
alla v1 (2026-10-24): i consumatori esistenti non cambiano, `contractVersion` resta 1.
Dal 04/11 anche `daily` (serie giornaliera, operazione additiva del motore): dal primo giorno del mese precedente a oggi,
per giorno e valuta `income`, `netSpending`, `unclassifiedOut`, `pendingCount`, solo i giorni con movimenti; serve al
grafico «Come sta andando il mese» del Riepilogo (spesa accumulata contro il mese prima). Validato dal ponte
(`validate("daily")`, test `test_serie_giornaliera`); periodo nel feed in `periodo.giorni`.
`trend` (2026-10-26) non è un'operazione nuova del motore: l'adattatore ripete `summary` per gli ultimi 12 mesi di
calendario (l'ultimo fino a oggi) e tiene solo `currency`, `income`, `netSpending`, `unclassifiedOut`,
`unclassifiedCount`, `pendingCount`, `spendingByCategory`; mesi in ordine e senza ripetizioni, al primo errore la busta
di errore (mai una serie con mesi mancanti). Validato dal ponte (`validate("trend")`, test `test_serie_mensile`).

## Categorie e gruppi di budget
- `summary` (mese corrente, per valuta): `spendingByCategory` (per id di categoria), `spendingByBudgetGroup` e
  `budgetSharesBp` con le chiavi fisse `needs`, `wants`, `savings`, `unassigned` (= categoria senza gruppo).
- `categories`: nome e gruppo di ogni id. La dashboard mostra il nome; se manca, l'id reso leggibile.
- La spesa **senza categoria** non entra in nessun gruppo: è `unclassifiedOut` con `unclassifiedCount` e la dashboard
  la mostra come barra «Da classificare» e nota «incompleto». Nessuna classificazione automatica nel ponte.

## Obiettivi e acquisti
- `planning.goals[].status` = stato **dichiarato**; `reached`/`progressBp` = avanzamento **calcolato**. Possono
  divergere (es. obiettivo «attivo» già raggiunto): si mostrano entrambi, nessuno viene corretto.
- `planning.purchases[].estimatedMinor: null` = «stima mancante» (il motore non ammette stime pari a zero).
- Nel ponte restano solo i campi necessari (niente legami con movimenti o blocchi Notion).

## Assente, non disponibile, zero
| Stato | Dove si vede nei dati | Come appare |
|---|---|---|
| **Non disponibile** | busta `ok: false` (errore del motore, `schema_mismatch`, `contratto_non_valido`) o operazione assente | «Non disponibile» con il motivo; il resto della pagina resta visibile |
| **Mancante** | campo `null` dentro una busta valida (saldo non osservato, stima, scadenza, ultimo movimento) | «mancante», «stima mancante», «scadenza mancante», «nessuno» |
| **Zero** | intero `0` o lista vuota in una busta valida | «0,00 €», «Nessun obiettivo registrato», «Zero spese nel periodo» |
| **Non implementato** | sezione senza fonte nel motore (investimenti, serie mensili) | «Non disponibile: non implementato nel motore» |

## Provenienza, aggiornamento, esito
- Ogni pagina Money mostra le operazioni usate (nome leggibile e nome tecnico) con «letto» o «non disponibile».
- `aggiornamento` (calcolato dal ponte da `data_status`): `letto_il` (ora della lettura), `ultimo_movimento` (ultimo
  movimento **contabilizzato**; il banner riporta invece l'ultimo movimento **anche in attesa**, da `overview`),
  `ultima_importazione`, `avvisi_dati_vecchi`, `stato` = `ok` | `dati_vecchi` | `nessun_dato` (nessun movimento
  registrato) | `non_disponibile`.
- `esito` del feed (`cuore_money.outcome`, unica definizione usata anche dalla copia offline): `completo` |
  `parziale` (lettura verificata, alcune operazioni non disponibili, elencate in `errori`) | `non_disponibile`.

## Assistente (MCP)
Stesse operazioni (`cloud_finance_<operazione>`), stessa lettura `readImmutable`, stesse buste. Il server non è
registrato in alcun client: la registrazione richiede un'autorizzazione separata.

## Registro degli eventi CUORE (`cuore.events/1`) — adattatore Finance
Stato (verificato nel codice il 2026-10-26): **collegato nel ponte**: `serve.events_build(money.get)` passa al registro
il feed Money già in cache e l'adattatore ne usa solo i metadati di `data_status` (fonte `finance.data_status`); testato
su dati sintetici. Codice `app/bridge/cuore_events.py` (`finance_events`), test `app/tests/test_events.py`.
- Legge solo il risultato di `data_status` già presente nel feed Money: nessuna nuova lettura del database, nessun
  movimento, importo, descrizione, saldo o estratto.
- `importazione_riuscita` (fatto osservato): da `lastImportAt` per conto, esito sempre `committed` (`lastImportStatus`
  descrive l'ultima esecuzione, che può essere un'altra).
- `anomalia_dati` (anomalia): un evento per anomalia del motore (`stale_data`, `stale_import`, `no_imports`,
  `last_import_rejected`, `balance_missing`, …) con gravità dal motore; istante solo se il motore lo dichiara
  (`detail.at`, es. importazione rifiutata), altrimenti `quando: null` e qualità «incompleto». Periodo
  (`reconciliation_difference`) e data (`balance_conflict`) entrano nel dettaglio e nella chiave, solo se nella forma
  attesa: due periodi non quadrati dello stesso conto sono due anomalie (prima del 25/10 sera il secondo veniva scartato
  come duplicato). `message`, importi (`differenceMinor`) e gli altri campi di `detail` non entrano mai nel registro.
- Catena verificata su un database **sintetico**: motore Finance → `cuore_money.build_feed` → adattatore → validatore
  (`test_dal_motore_finance_reale_su_database_sintetico`). Contratto completo degli eventi:
  [contratto-eventi.md](contratto-eventi.md).
- Collegamento: `serve.events_build` (una riga nel ponte). La scheda «Eventi recenti» (Sistema → Stato) mostra per
  Finance solo conto, codice, periodo o data dell'anomalia e istante; un feed Money non calcolabile rende la fonte `illeggibile`, mai «nessun evento».

## Checklist di accettazione per la prossima importazione reale
Procedura già esistente (`finance/scripts/import-plan.ts`), verificata il 24/10 su
copie isolate (prova da zero identica al rapporto del 22/10; `--apply` su copia: nessun dato nuovo). Nessuna voce è
eseguita da CUORE: ogni passo con scrittura richiede l'autorizzazione del titolare.
1. **File**: estratto fornito dal titolare, in una cartella privata fuori dal repository; impronta annotata nel
   manifesto (`MANIFEST.sha256`). Mai nel repository, mai in iCloud/Drive sincronizzati.
2. **Formato**: `scripts/probe-format.ts` sul file (sola lettura); il parser deve riconoscerlo senza `xlsx_malformed`
   né colonne inattese. Un formato nuovo = specifica e test sintetici prima di tutto.
3. **Piano**: piano incrementale (`accounts: []`, nessun istituto o conto creato), politica dei trasferimenti
   `same_source_id` (variante A), saldi osservati e valori attesi dagli estratti.
4. **Prova a vuoto**: `node scripts/import-plan.ts --plan <piano>` → esito `COERENTE`; controllare `antiDuplicate`,
   `doubleCounting`, `transferPolicy.notConfirmedByInstitution`, `links.detect.ambiguous`, anomalie.
5. **Prova su copia**: `--apply` su una copia del database in una cartella temporanea privata → `AGGIORNATO`, delta
   atteso (solo righe nuove), nessun movimento contabilizzato modificato (controllo append-only dello script).
6. **Applicazione reale** (solo con autorizzazione): `--apply <database>` → backup verificato fuori da `db/`, rapporto in
   `report/`, sostituzione atomica.
7. **Dopo**: `data_status` senza `stale_*` per il conto aggiornato; nessuna nuova voce in coda di verifica non spiegata;
   ripetendo il passo 6 con lo stesso file → «nessun dato nuovo» (idempotenza).
Criterio di rifiuto: qualunque passo diverso dall'atteso ferma la procedura; il database resta quello precedente.
