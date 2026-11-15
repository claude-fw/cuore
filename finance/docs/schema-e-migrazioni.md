# Schema dati e migrazioni

Versione attuale dello schema: **6** (`src/db/migrations/`). Tutte le tabelle sono `STRICT`.
Importi: `*_minor` INTEGER in unita' minime della valuta indicata accanto. Date: `YYYY-MM-DD`; istanti: ISO 8601 UTC.

## Tabelle
### v1 — nucleo (`001_core.ts`)
| Tabella | Contenuto | Mutabilita' |
|---|---|---|
| `meta` | `write_seq` per il controllo di concorrenza | aggiornata solo dalla guardia |
| `write_guard` | tabella tecnica della guardia (sempre vuota a fine batch) | — |
| `schema_migrations` | versione, nome, checksum, data | append-only |
| `institutions` | istituti (banche) | inserimento |
| `accounts` | conto: tipo, valuta, **titolarita'** (`owner`, `joint`, `delegate`, `other`) | inserimento |
| `sources` | fonti dei dati (`manual`, `file:<formato>`; in futuro `api:<fornitore>`) | inserimento |
| `import_runs` | ogni tentativo di importazione: file, sha256, formato e versione, esito, conteggi, errori | append-only |
| `raw_records` | ogni riga letta, testo originale in JSON canonico + sha256 | append-only |
| `transactions` | transazione normalizzata: id della fonte o impronta+occorrenza, stato, date, importo, importo originale e valuta, testi originali e normalizzati | identita' immutabile; nessuna cancellazione; mai booked -> pending |
| `transaction_observations` | provenienza: quale riga grezza di quale esecuzione ha creato/confermato/aggiornato/contestato la transazione | append-only |
| `balance_observations` | saldi di fine giornata osservati, con fonte ed eventuale riga grezza | append-only |
| `audit_log` | chi (sistema, import, regola, utente), cosa, prima/dopo | append-only |

### v2 — classificazione e verifica (`002_classification_review.ts`)
| Tabella | Contenuto | Mutabilita' |
|---|---|---|
| `categories` | categorie, gerarchia, gruppo di budget (`needs`/`wants`/`savings`) | append-only |
| `rule_sets` | versioni delle regole con checksum del contenuto | append-only |
| `rules` | regole di una versione (condizioni JSON, tipo, categoria, confidenza) | append-only |
| `classifications` | storico delle classificazioni (`rule` o `user`; `auto`, `confirmed`, `needs_review`) | append-only |
| `current_classifications` (vista) | ultima classificazione per transazione | — |
| `transaction_links` | `transfer`, `refund`, `pending_booked`, `possible_duplicate`: proposto -> confermato/rifiutato | si decide una sola volta |
| `review_items` | coda da verificare (un solo elemento aperto per tipo ed entita') | si chiude una sola volta |
| `reconciliations` | esiti delle riconciliazioni | append-only |

### v3 — pianificazione (`003_planning.ts`)
| Tabella | Contenuto | Mutabilita' |
|---|---|---|
| `savings_goals` | obiettivi di risparmio | stato modificabile, nessuna cancellazione |
| `goal_allocations` | accantonamenti virtuali (correzioni = importi negativi) | append-only |
| `planned_purchases` | acquisti pianificati; `notion_block_id` (colonna storica, mai più scritta: Notion escluso da CUORE); `transaction_id` solo se acquistato | nessuna cancellazione |
| `fx_rates` | cambi osservati con fonte (tasso come testo decimale) | append-only |

### v4 — formati bancari (`004_bank_formats.ts`)
| Modifica | Contenuto | Mutabilita' |
|---|---|---|
| `transactions.source_type` (colonna aggiunta) | tipo di operazione come scritto dalla fonte (dato osservato, non classificazione); `derived:fee` per le commissioni separate | aggiornabile solo come gli altri campi delle pending |
| `excluded_rows` | righe lette ma escluse per regola dichiarata dalla specifica (motivo, id della fonte, grezzo, sha256) | append-only |

### v6 — profilo dei conti (`006_account_profiles.ts`)
| Tabella | Contenuto | Mutabilita' |
|---|---|---|
| `account_profiles` | intestatario formale, utilizzatore principale, supervisione familiare (`unknown` finche' l'utente non dichiara; mai dedotti da un estratto) | append-only, vale l'ultima dichiarazione |

| `account_status_events` | stato operativo (`closed`/`reopened`), data effettiva solo se nota con il suo stato (`verified`, `declared`, `unknown`), ultimo movimento osservato al momento della dichiarazione, motivo | append-only, vale l'ultimo evento |

`closeAccount` registra un evento solo se l'ultimo saldo osservato e' zero e nessun movimento e' successivo alla data
(se nota). `accounts.closed_at` non viene usato.
`accounts.holder_role` resta per compatibilita' e non e' una dichiarazione di titolarita'.

### v5 — crediti e debiti (`005_obligations.ts`)
| Tabella | Contenuto | Mutabilita' |
|---|---|---|
| `counterparties` | persone o enti (nessun dato bancario) | append-only |
| `obligations` | credito (`receivable`) o debito (`payable`): controparte, valuta, descrizione, apertura, scadenza facoltativa | append-only |
| `obligation_events` | `open` (uno solo), `increase`, `payment`, `forgive`, `adjust`; movimento bancario facoltativo e verificato | append-only |

Residuo e stato si calcolano dagli eventi. Regole ed effetto sui riepiloghi: [crediti-e-debiti.md](crediti-e-debiti.md).

## Regole delle migrazioni
- Ogni migrazione e' un elenco di istruzioni **singole**, applicate in un batch atomico insieme alla sua riga in
  `schema_migrations`. Un errore annulla tutta la migrazione (testato).
- Solo in avanti e solo additive: il validatore rifiuta `DROP`, `DELETE FROM`, `RENAME`, `REPLACE` (testato).
- Il checksum di ogni migrazione applicata deve coincidere con il codice: modificare una migrazione gia' applicata
  blocca tutto (testato). Per cambiare lo schema si aggiunge una migrazione nuova in coda a `MIGRATIONS`.
- Un database a una versione piu' alta del codice non viene toccato (testato).
- Le migrazioni si eseguono solo su richiesta (`cloud-finance migrate`); gli altri comandi rifiutano un database
  non aggiornato.
- Prima di migrare un database con dati reali (fasi successive): copia di sicurezza del file (con il database chiuso)
  e `PRAGMA integrity_check`. In questa fase non esistono database reali.

## Come aggiungere una migrazione
1. creare `src/db/migrations/00N_nome.ts` che esporta `{ version: N, name, statements: [...] }`;
2. aggiungerla in coda a `MIGRATIONS` in `src/db/migrations/index.ts`;
3. `npm run migrations:generate` (file SQL per Wrangler in `migrations/` e istantanea dello schema);
4. aggiungere un test di aggiornamento da N-1 a N con dati presenti;
5. aggiornare questo documento e verificare con `npm run check`.

Percorsi di applicazione (runner e Wrangler), garanzie e recupero: [migrazioni-d1.md](migrazioni-d1.md).
