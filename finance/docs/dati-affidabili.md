# Dati affidabili: deduplicazione, stati, classificazione, collegamenti, verifica, riconciliazione

## Identita' e deduplicazione
| Caso | Comportamento |
|---|---|
| riga con `transaction_id` | stessa transazione solo se (conto, id) coincide |
| riga senza id | stessa transazione solo se coincidono **impronta** e **occorrenza** |
| impronta | sha256 di conto, data operazione, importo, valuta, descrizione e controparte normalizzate (minuscole, spazi compressi, NFKC), importo e valuta originali |
| occorrenza | posizione tra le righe identiche dello stesso file: due operazioni identiche restano due, e reimportando lo stesso periodo restano due |
| stessa operazione con descrizione diversa in un export successivo | nuova transazione + collegamento `possible_duplicate` **proposto** + verifica |
| differenze solo di maiuscole o spazi | stessa transazione; resta il testo della prima importazione, il nuovo testo e' nel grezzo |

Nessuna regola unisce due transazioni da sola. Un doppione confermato non viene cancellato: viene escluso da
riepiloghi e riconciliazioni.

## Stati pending / booked
| Esistente | In arrivo | Esito |
|---|---|---|
| pending | pending diversa o booked | aggiornata (audit con prima/dopo); importo cambiato alla contabilizzazione -> verifica `amount_changed_on_booking` |
| booked | booked diversa | **nessuna modifica**, verifica `source_changed` con le differenze |
| booked | pending | **nessuna modifica**, verifica `status_regression` (il database lo impedisce comunque) |
| pending senza id | booked con descrizione diversa, stesso importo, entro 5 giorni | nuova transazione + collegamento `pending_booked` proposto |
| pending nel periodo del file ma assente dal file | — | verifica `pending_not_seen`, nessuna modifica |

I riepiloghi contano solo le contabilizzate; le pending sono riportate a parte. Una pending sostituita da una
contabilizzata (collegamento confermato) esce dalle pending.

## Classificazione
- Regole in JSON (`cloud-finance-rules/1`, esempio `examples/sintetici/regole-demo.json`): condizioni su
  `date` (data effettiva YYYY-MM-DD: `equals`, `gte`, `lte`; per regole valide solo in un periodo, es. quando un
  estratto di un altro conto copre o non copre quelle date), `description`, `counterparty` (testo normalizzato:
  `equals`, `contains`, `starts_with`, `one_of`), `amount_minor`
  (`equals`, `gte`, `lte`), `currency`, `account_id`, `direction` (`in`/`out`), `source_type` (tipo scritto dalla
  fonte, confronto esatto: e' l'unico modo in cui un'etichetta della banca come "rimborso" influisce sulla
  classificazione, e solo tramite una regola scritta dall'utente); esito: tipo (`income`, `expense`,
  `transfer`, `refund`, `fee`), categoria facoltativa, confidenza `high`/`low`.
- Ogni pubblicazione con contenuto nuovo crea una **versione**; ripubblicare lo stesso contenuto non crea nulla; le
  regole pubblicate non si modificano.
- Esiti: nessuna regola, regole in conflitto a pari priorita', confidenza bassa, segno incoerente con il tipo ->
  `needs_review` (+ elemento da verificare). Solo `high` senza conflitti -> `auto`.
- La classificazione dell'utente (`classify:set` o conferma di un collegamento) e' `confirmed` e le regole non la
  toccano piu'. Lo storico e' append-only (versione e regola per ogni riga).
- Le regole non deducono mai **che cosa** e' stato acquistato: usano solo i testi gia' presenti.
- Nei riepiloghi, `needs_review` vale come "da classificare".

## Collegamenti (`detect`)
- **Trasferimento tra conti propri**: uscita e entrata in conti diversi, stessa valuta, stesso importo assoluto, entro
  3 giorni, abbinamento univoco da entrambi i lati; altrimenti verifica `transfer_ambiguous`. Valute diverse non si
  abbinano (cambio non noto con certezza).
- **Rimborso**: entrata con parola di rimborso (`rimborso`, `refund`, `storno`, `reversal`, `chargeback`, `reso`) e
  stessa controparte di una spesa nello stesso conto, entro 90 giorni, importo non superiore.
- Tutto e' **proposto**; la conferma (`link:decide`) classifica le transazioni coinvolte; un rifiuto non viene
  riproposto. Un collegamento si decide una sola volta.

## Crediti e debiti
Un credito o debito non e' entrata ne' spesa. Un movimento collegato a mano (`obligation:add --txn`,
`obligation:link`) esce da entrate, spese e "da classificare" e compare in `obligationsIn`/`obligationsOut`;
non puo' essere anche trasferimento o rimborso. Dettagli: [crediti-e-debiti.md](crediti-e-debiti.md).

## Riconciliazione
`saldo apertura (fine giornata from) + movimenti contabilizzati con booking_date in (from, to] = saldo chiusura`.
Esiti `ok`, `difference` (verifica `reconciliation_difference`), `insufficient_data`, `conflict` (saldi diversi per
la stessa data). Lo storico delle riconciliazioni e' conservato; un esito `ok` successivo chiude la verifica aperta.
`reconcile --observed` controlla tutte le coppie di date consecutive con saldo osservato.

## Valute
- Un conto = una valuta; importi sempre nella valuta del conto; importo originale facoltativo.
- I riepiloghi non sommano mai valute diverse.
- Conversioni solo con cambi registrati (`fx:add`, con fonte), aritmetica intera, arrotondamento meta' lontano da
  zero; il risultato e' sempre marcato `estimated: true`.

## Confermato, stimato, da verificare
| Dato | Stato |
|---|---|
| transazione contabilizzata importata | osservato dalla fonte |
| pending | provvisorio, fuori dai totali |
| classificazione `auto` | regola certa, modificabile dall'utente |
| classificazione `needs_review` / nessuna | da classificare |
| `confirmed` | decisione dell'utente |
| saldo osservato unico | osservato; piu' valori per la stessa data = conflitto |
| importo convertito | stima |
