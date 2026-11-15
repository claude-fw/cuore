# Formati di importazione

## Stato
| Formato | Versione | Verificato su esportazioni reali | Uso |
|---|---|---|---|
| `cloud-finance-csv` | 1 | **no** (formato neutro di questo progetto) | transazioni |
| `cloud-finance-balances-csv` | 1 | **no** (formato neutro di questo progetto) | saldi di fine giornata |
| `revolut-personal-statement` | — | **predisposto**, non importabile (colonne non verificate) | — |
| `mps-digital-banking-movimenti` | — | **predisposto**, non importabile (colonne non verificate) | — |

Nessun formato bancario e' supportato finche' non viene verificato. I formati neutri servono per inserire dati
preparati o convertiti a mano e come riferimento per i parser futuri. I formati bancari si descriveranno con una
**specifica dichiarativa** eseguita dal motore tabellare (CSV o XLSX): fatti verificati, informazioni mancanti e
procedura sono nel codice dei parser.

## `cloud-finance-csv` v1 (transazioni)
CSV RFC 4180, UTF-8 (BOM ammesso), separatore virgola, campi tra doppi apici quando contengono virgole, apici o a capo.
Intestazione **obbligatoria** con esattamente queste colonne (ordine libero; colonne mancanti, ripetute o sconosciute
rendono il file non valido):

| Colonna | Obbligatoria | Regole |
|---|---|---|
| `transaction_id` | no | id stabile della fonte; se presente e' la chiave di deduplicazione; unico nel file |
| `transaction_date` | si' | `YYYY-MM-DD`, data di calendario valida (data dell'operazione) |
| `booking_date` | se `booked` | `YYYY-MM-DD`; **vuota** per le righe `pending` |
| `status` | si' | `pending` o `booked` |
| `amount` | si' | punto decimale, segno `-` per le uscite, nessun separatore delle migliaia, nessun `+`, al massimo i decimali della valuta (EUR: 2) |
| `currency` | si' | deve coincidere con la valuta del conto (un conto per valuta) |
| `description` | si' | max 500 caratteri, conservata esattamente |
| `counterparty` | no | max 500 caratteri |
| `original_amount` | con `original_currency` | importo nella valuta originale (es. pagamento in USD), stesso segno di `amount` |
| `original_currency` | con `original_amount` | valuta supportata, diversa da quella del conto |

Esempio: `examples/sintetici/conto-demo.csv`, `examples/sintetici/carta-demo.csv`.

## `cloud-finance-balances-csv` v1 (saldi)
Colonne: `date` (`YYYY-MM-DD`, una sola riga per data), `balance` (stesse regole di `amount`), `currency` (= valuta
del conto). Significato: **saldo contabile a fine giornata** della data indicata.
Esempio: `examples/sintetici/saldi-conto-demo.csv`.

## Comportamento dell'importazione
- Un solo problema (riga o struttura) -> file rifiutato per intero; l'esecuzione resta registrata con i problemi
  (riga, campo, codice, messaggio). Codici: `csv_malformed`, `empty_file`, `no_rows`, `missing_column`,
  `unknown_column`, `duplicate_column`, `wrong_field_count`, `invalid_date`, `invalid_status`, `missing_booking_date`,
  `unexpected_booking_date`, `currency_mismatch`, `invalid_amount`, `too_many_decimals`, `amount_out_of_range`,
  `unsupported_currency`, `duplicate_id`, `missing_description`, `too_long`, `original_incomplete`,
  `original_same_currency`, `original_sign_mismatch`, `duplicate_date`, `invalid_utf8`, `write_failed`.
  Motore a specifica (formati bancari futuri), in aggiunta: `unsupported_file_type`, `invalid_encoding`,
  `xlsx_malformed`, `header_not_found`, `missing_value`, `unknown_status`, `ambiguous_amount`, `unexpected_sign`,
  `unexpected_cell_type`, `invalid_number_format`, `original_mismatch`; correzioni dichiarate: `correction_invalid`,
  `correction_not_matched`, `correction_ambiguous`, `correction_conflict`, `correction_noop`; con `balanceAfter`: `balance_chain_break`,
  `balance_order`, `balance_with_pending`.
- Righe escluse per regola dichiarata (es. stato "annullata"): salvate in `excluded_rows`, mai importate come
  transazioni; righe prima/dopo la tabella e righe vuote: contate in `notices` del rapporto, non salvate.
- Stesso file gia' importato -> `duplicate_file`, nessuna modifica.
- Deduplicazione, stati e conflitti: vedi [dati-affidabili.md](dati-affidabili.md).

## Dossier derivati (Revolut v3, staging MPS)
Formati `revolut-dossier-v3-*` e `mps-staging-csv`: vedi `scripts/import-plan.ts`.

## Aggiungere un parser per una banca (fase successiva)
1. ottenere un'esportazione reale e creare una **copia anonimizzata** (importi, nomi e id inventati, stessa struttura);
2. documentare qui colonne, formato di date e importi, codifica, separatore, segni, pending/booked, storico disponibile;
3. implementare `StatementParser` in `src/import/parsers/<banca>.ts` (solo testo -> righe tipizzate + problemi),
   con `verifiedAgainstRealExports: true` solo dopo la verifica;
4. registrarlo in `parsers/registry.ts` e aggiungere test con il file anonimizzato (mai il file reale).
