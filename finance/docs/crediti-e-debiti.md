# Crediti e debiti (schema v5)

Stato: **implementato e verificato solo in locale** (SQLite via Node e D1 locale), con dati sintetici. Non pronto
per la produzione: nessun dato reale, nessun D1 remoto, nessuna proiezione Notion.

Origine: nel To-do Notion esistono voci di somme dovute a familiari e amici (Fase 0). Sostituisce la proposta P4.

## Modello (migrazione `005_obligations.ts`, solo additiva)
| Tabella | Contenuto | Mutabilita' |
|---|---|---|
| `counterparties` | persona o ente: id scelto dall'utente, nome, nota. Nessun dato bancario | append-only (per cambiare: nuova controparte) |
| `obligations` | credito (`receivable`, mi devono) o debito (`payable`, devo io): controparte, valuta, descrizione, data di apertura, scadenza facoltativa (non precedente all'apertura) | append-only: si cambia solo con eventi |
| `obligation_events` | `open` (uno e uno solo per obbligazione, indice univoco parziale), `increase`, `payment`, `forgive` (condono), `adjust` (rettifica, con segno e motivazione obbligatoria); importo intero in unita' minime; `transaction_id` facoltativo solo per `open`, `increase`, `payment` | append-only |

Rispetto alla proposta: nessuna tabella `obligation_settlements` (un movimento che ripaga piu' obbligazioni produce
piu' eventi con lo stesso `transaction_id`) e nessuno stato memorizzato: **residuo, stato e scadenza superata si
calcolano sempre dagli eventi** (`src/domain/obligations.ts`).

`residuo = apertura + aumenti - pagamenti - condoni +/- rettifiche`; residuo 0 = `closed`; aperto con scadenza
passata = `overdue`. `obligations --date` usa solo gli eventi fino a quella data (posizione storica).

## Regole (tutte provate in `test/obligations.test.ts`, su Node e D1 locale)
- **Un credito o debito non e' cassa**: crearlo o registrare un evento manuale non crea transazioni, entrate o spese.
- **Nessun pagamento in eccesso**: un evento che renderebbe il residuo negativo e' rifiutato.
- **Ordine cronologico**: un evento non puo' precedere l'apertura ne' l'ultimo evento registrato; per correggere il
  passato si usa una rettifica datata oggi.
- **Doppioni**: apertura identica (controparte, direzione, valuta, data, descrizione, importo) o evento identico
  rifiutati salvo conferma esplicita (`--confirm-duplicate`).
- **Collegamento a un movimento bancario: solo manuale e verificato**, mai automatico. Il movimento deve essere
  contabilizzato, non zero, non escluso (doppione confermato, pending sostituita), non gia' collegato a crediti/debiti,
  non coinvolto in un trasferimento o rimborso proposto o confermato. Le quote devono sommare **esattamente**
  l'importo del movimento (nessun collegamento parziale), stessa valuta, segno coerente:

  | | apertura / aumento | pagamento |
  |---|---|---|
  | credito (`receivable`) | uscita (ho dato denaro) | entrata (mi restituiscono) |
  | debito (`payable`) | entrata (ho ricevuto denaro) | uscita (restituisco) |
- Tutte le scritture sono in un batch atomico con audit e controllo di concorrenza: un errore non lascia eventi a meta'.

## Effetto sui riepiloghi (nessun doppio conteggio)
- Un movimento collegato diventa di tipo `obligation`: esce da entrate, spese e "da classificare" e compare in
  `obligationsIn` / `obligationsOut` del riepilogo per valuta (come i trasferimenti in `transfersIn/Out`).
- Il collegamento prevale sulla classificazione: un movimento gia' classificato come spesa e poi collegato non e'
  contato due volte.
- `detect` non propone mai come trasferimento o rimborso un movimento collegato; `link:decide` rifiuta di confermare
  un trasferimento o rimborso che coinvolge un movimento collegato.
- Trasferimenti tra conti propri restano distinti: un movimento e' o trasferimento o credito/debito, mai entrambi.
- La riconciliazione bancaria non cambia: conta i movimenti reali, collegati o no.
- Valute mai sommate: la posizione (`receivable`, `payable`, `net`, aperti, scaduti) e' per valuta.

## CLI
```bash
npm run cli -- counterparty:add --id amico-demo --name "Amico DEMO"
npm run cli -- obligation:add --counterparty amico-demo --direction receivable --amount 10.00 --currency EUR \
  --date 2026-01-20 --due 2026-02-28 --description "Cena DEMO" [--txn <id movimento>] [--confirm-duplicate]
npm run cli -- obligation:event --id <id> --kind payment --amount 4.00 --date 2026-01-25 [--note ...]
npm run cli -- obligation:link --txn <id movimento> --allocation <id>:payment:4.00 [--allocation ...]
npm run cli -- obligations --date 2026-03-01
```

## Scelte prudenti adottate (dedotte, modificabili con una migrazione successiva)
1. Debiti storici in contanti: si registrano con `obligation:add` senza movimento (nessun effetto sulla cassa).
2. Movimento in parte debito e in parte spesa: **rifiutato** (servirebbe la suddivisione delle transazioni).
3. Pagamento oltre il residuo: rifiutato (non diventa automaticamente un'obbligazione inversa).

## Decisioni aperte (non bloccanti)
1. Rappresentare anche crediti/debiti con familiari (ricariche come prestiti o no).
2. Suddivisione di un movimento tra debito e spesa (nuova tabella di quote, migrazione futura).
3. Pagamento in eccesso: rifiuto (oggi) o apertura automatica di un'obbligazione inversa.
4. Visualizzazione nella dashboard CUORE: riepilogo per persona o elenco (Notion escluso dal 2026-10-24).
5. Proposte automatiche di collegamento (stessa controparte, importo compatibile): oggi assenti, solo manuale.
