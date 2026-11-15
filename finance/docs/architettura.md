# Architettura e decisioni (Fase 2)

## Obiettivo della fase
Un nucleo locale affidabile e testato, con lo stesso codice eseguibile in futuro in un Cloudflare Worker con D1.
Fuori dalla fase: collegamenti bancari, Notion, MCP remoto, notifiche, deploy.

## Livelli
```
cli/main.ts ──► services/* ──► import/importer.ts ──► import/plan.ts (puro)
                    │                 │
                    ▼                 ▼
                domain/* (puro)   import/parsers/* (puri, solo testo)
                    │
                    ▼
                db/types.ts (interfaccia Db) ◄── db/node-sqlite.ts (oggi) / adattatore D1 (Fase 3)
```
- **domain/**: funzioni pure e deterministiche (importi, CSV, regole, riepiloghi, riconciliazione, abbinamenti).
- **import/**: i parser trasformano testo in righe tipizzate; il *piano* decide creare/aggiornare/conflitto senza I/O.
- **services/**: leggono, chiamano le funzioni pure, scrivono in **un solo batch atomico** per operazione.
- **db/**: interfaccia minima `all / first / batch`; solo `node-sqlite.ts` e la CLI usano API di Node
  (verificato da un test di portabilita').

## Decisioni (con motivazione)
| # | Decisione | Perche' |
|---|---|---|
| D1 | TypeScript eseguito direttamente da Node 24 (type stripping), sintassi solo "cancellabile" (`erasableSyntaxOnly`) | nessun bundler o transpiler in sviluppo; lo stesso sorgente e' accettato da wrangler in Fase 3 |
| D2 | SQLite locale con `node:sqlite` (integrato in Node) | nessuna dipendenza nativa da compilare; stesso dialetto di D1 |
| D3 | Interfaccia `Db` asincrona con `batch()` atomico, nessuna transazione interattiva | D1 non offre transazioni interattive: il nucleo usa gia' il modello "leggi -> pianifica -> batch" |
| D4 | Controllo ottimistico di concorrenza (`meta.write_seq` + `write_guard` con CHECK) | un piano calcolato su dati vecchi fa fallire l'intero batch (`ConflictError`) anche in D1, dove tra lettura e scrittura possono inserirsi altre richieste |
| D5 | Importi INTEGER in unita' minime, valute da elenco esplicito ISO 4217 | niente virgola mobile; una valuta sconosciuta e' un errore, non un default |
| D6 | Tabelle `STRICT` e vincoli CHECK | il database rifiuta tipi sbagliati (es. 1.5 in un campo importo) anche se il codice sbagliasse |
| D7 | Storico grezzo, esecuzioni, audit, osservazioni, regole, classificazioni: append-only con trigger | tracciabilita' completa; le correzioni sono nuove righe, mai riscritture |
| D8 | Nessuna cancellazione di transazioni; doppioni e pending sostituite si gestiscono con collegamenti confermati | nessuna perdita di dati per un abbinamento sbagliato; ogni decisione e' reversibile solo aggiungendo informazione |
| D9 | Deduplicazione prudente: id della fonte, altrimenti impronta + occorrenza; tutto il resto e' solo proposta | evitare sia doppioni sia fusioni arbitrarie di operazioni diverse ma simili |
| D10 | File con anche un solo errore: rifiutato per intero | nessuna importazione parziale difficile da spiegare |
| D11 | Formati di importazione neutri e dichiarati non verificati | i formati reali di Revolut e MPS non sono stati verificati: niente compatibilita' inventata |
| D12 | Classificazioni incerte (bassa confidenza, conflitti, nessuna regola, segno incoerente) restano "da classificare" anche nei riepiloghi | i totali non devono contenere supposizioni |
| D13 | Database indicato sempre in modo esplicito; cartelle 0700 e file 0600 dalla CLI | nessun dato reale per sbaglio nel repository o in iCloud |
| D14 | Strumenti minimi: `node --test`, `tsc`, Biome; nessun framework | meno dipendenze da mantenere; tutte bloccate a versioni precise |
| D15 | Formati bancari come **specifiche dichiarative** eseguite da un motore unico (`src/import/tabular/`) | ogni ambiguita' (date, fuso, segni, stati, esclusioni) e' scritta e verificabile; aggiungere una banca non richiede nuovo codice di parsing |
| D16 | Lettore XLSX proprio (ZIP + XML minimi, `DecompressionStream`), nessuna libreria | entrambe le banche esportano "Excel"; nessuna dipendenza, compatibile con i Workers; limiti anti-"bomba", CRC verificato, DTD rifiutate |
| D17 | Contratto dei parser a byte (`parseFile`) invece che a testo | la codifica (UTF-8 rigoroso, windows-1252) e il tipo di file (XLSX, .xls binario, HTML, PDF) si decidono dai byte, con rifiuti espliciti |
| D18 | Righe escluse salvate in `excluded_rows`; righe prima/dopo la tabella solo contate | nessuna riga sparisce in silenzio, ma i dati del documento (spesso personali: intestatario, IBAN) non vengono archiviati |
| D19 | Nessun parser Revolut/MPS senza un'esportazione verificata | le fonti ufficiali indicano il tipo di file ma non le colonne: implementarli significherebbe inventare il formato |
| D20 | Adattatore D1 con tipi strutturali propri, nessun pacchetto di tipi Cloudflare a runtime | funziona con il binding reale e con quello di Miniflare; nessuna dipendenza in produzione |
| D21 | Stessa suite di test su due motori (`CLOUD_FINANCE_TEST_DB=d1`), nessun test "speciale per D1" per la logica | la compatibilita' si dimostra con gli stessi controlli, non con controlli piu' deboli |
| D22 | Wrangler, Miniflare ed esbuild fissati alle versioni esatte della stessa catena (wrangler 4.147.0) | un solo albero di dipendenze, riproducibile, solo per sviluppo |
| D23 | Migrazioni ibride: file SQL per Wrangler GENERATI da `MIGRATIONS`, con guardie di versione/checksum e la stessa riga `schema_migrations` del runner; verifica dello schema con istantanea generata | Wrangler da solo non controlla sequenza, contenuto e checksum (provato); cosi' resta un solo sistema di migrazioni con un solo registro verificabile ([migrazioni-d1.md](migrazioni-d1.md)) |
| D24 | Baseline del registro di Wrangler come batch "tutto o niente" con guardie su storia esatta, registro (prefisso ordinato) e schema con testo SQL esatto; verifica remota possibile solo in sola lettura tramite la CLI di Wrangler | un registro si allinea solo se il database dimostra la propria storia; un dubbio produce un rifiuto, mai una correzione silenziosa ([migrazioni-d1.md](migrazioni-d1.md), [prova-remota-d1.md](prova-remota-d1.md)) |

## Flusso di un'importazione
1. conto e parser; sha256 del file; `write_seq` letto;
2. file gia' importato con successo (stesso conto, formato, sha256) -> esecuzione `duplicate_file`, nessun dato;
3. `parser.parseFile(byte)`: riconoscimento del tipo di file, decodifica (UTF-8 rigoroso o codifica dichiarata, oppure
   XLSX), analisi; un problema qualsiasi -> esecuzione `rejected` con l'elenco dei problemi;
4. impronta e occorrenza per ogni riga; lettura delle transazioni esistenti nella finestra di date (+/-10 giorni) e
   per id della fonte (a blocchi da 90 parametri, limite di D1);
5. piano puro (`planImport`): create / invariate / aggiornate (solo pending) / conflitti, collegamenti proposti,
   elementi da verificare;
6. un batch: guardia di concorrenza, esecuzione, grezzo, transazioni, osservazioni di provenienza, audit, verifiche.
   Errore -> rollback completo ed esecuzione `rejected` con `write_failed`.

## D1/Workers (Fase 3: verificato in locale)
- `src/db/d1.ts` implementa `Db` sul binding D1 (`prepare/bind/all/first/batch`), senza dipendenze; controlla il
  limite di 100 parametri per istruzione prima di inviare la query;
- le migrazioni sono le stesse su Node e su D1; lo schema ottenuto e' identico, oggetto per oggetto, anche passando
  dalla CLI di Wrangler;
- prove, differenze osservate, decisione aperta sulle migrazioni remote e cio' che non e' verificato: [d1.md](d1.md);
- i batch molto grandi (import di migliaia di righe) potrebbero superare i limiti di D1 remoto: andra' misurato e, se
  serve, diviso in piu' import, mai in batch parziali dello stesso import.
