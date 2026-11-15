# Migrazioni: strategia ibrida (runner + Wrangler)

Decisione del 2026-10-21. Verificata **solo in locale** (Node SQLite, D1 locale via Miniflare/workerd, Wrangler
4.147.0 `--local`). Nessun D1 remoto, nessun deploy.

## Principio: una sola fonte, due esecutori, un solo registro verificabile
- **Fonte unica**: `MIGRATIONS` in `src/db/migrations/*.ts`. Non esistono migrazioni scritte a mano in SQL.
- **File per Wrangler** (`migrations/000N_<nome>.sql`) **generati** da `MIGRATIONS` con `npm run migrations:generate`;
  `npm run migrations:check` (dentro `npm run check`) fallisce se un file differisce anche di un byte, manca o e' in piu'.
- **Registro del progetto**: `schema_migrations` (versione, nome, checksum) viene scritto IDENTICO dal runner e dai
  file SQL. `d1_migrations` e' solo il registro interno di Wrangler (nomi dei file), verificato per coerenza.

## Cosa garantisce realmente ciascun percorso (verificato dai test)
| Controllo | Runner (`migrate()`) | Wrangler standard | Ibrido (file generati) |
|---|---|---|---|
| Sequenza delle versioni | si' (codice) | **no**: applica qualunque file mancante per nome | si': guardia SQL nel file (`MAX(version) = N-1`) |
| Migrazione gia' registrata e poi modificata nel codice | si' (checksum) | **no** | si': guardia SQL verifica i checksum precedenti; `migrate:verify` e `migrationStatus` lo segnalano |
| File SQL modificato a mano dopo l'applicazione | n/a | **non rilevato** (provato) | rilevato da `migrations:check` (confronto con il generato) |
| Doppia applicazione | si' | solo per nome del file | per nome (Wrangler) + guardia di versione; file 1: le tabelle esistono gia' |
| Atomicita' di una migrazione | si' (batch) | si' **in locale** (batch: provato); remoto non verificato | come Wrangler |
| Database piu' recente del codice | si' | no | `migrate:verify` / `migrationStatus` |
| Istruzioni distruttive vietate | si' (validatore) | no | si': i file nascono solo da `MIGRATIONS` validate |
| Coerenza dello schema reale | no | no | si': `verifyDatabase` confronta `sqlite_master` con l'istantanea generata |

Equivalenza **dimostrata dai test** (non presunta): stesso schema (testo SQL esatto) e stessa storia
`schema_migrations` tra runner, file SQL eseguiti a batch e `wrangler d1 migrations apply --local`
(`test/migration-sql.test.ts`, `test/d1-migrations.test.ts`).

## Contenuto di un file generato
1. intestazione: versione, nome, checksum (lo stesso del runner);
2. guardia (da N = 2): righe in `write_guard` il cui CHECK fallisce se la versione registrata non e' N-1 o se un
   checksum precedente non coincide. Un fallimento mostra `write_guard_seq`: significa "database non alla versione
   attesa o storia diversa dal codice";
3. le istruzioni della migrazione;
4. la riga in `schema_migrations`. Wrangler aggiunge la propria riga in `d1_migrations`; tutto in un solo batch.

## Procedure
**Nuova migrazione**
1. aggiungere `src/db/migrations/00N_nome.ts` in coda a `MIGRATIONS` (solo istruzioni additive);
2. `npm run migrations:generate` (scrive `migrations/00N_nome.sql` e l'istantanea dello schema);
3. `npm run check` (deve passare: include confronto dei file, test su Node, su D1 locale e con Wrangler).
Mai modificare una migrazione gia' applicata: se ne aggiunge una nuova.

**Inizializzare / applicare (locale)**
```bash
npx wrangler d1 migrations apply <database> --local     # con migrations_dir = "migrations" nel config
```
In alternativa, per SQLite locale: `npm run cli -- migrate`. Entrambi lasciano lo stesso `schema_migrations`.

**Verificare (sola lettura)**
```bash
npm run cli -- migrate:verify                              # database SQLite della CLI
node scripts/migrations.ts verify --db <file.sqlite>        # qualsiasi file SQLite, anche lo stato locale di D1
node scripts/migrations.ts verify-wrangler --database <nome> --config <file> --local --persist-to <dir>
```
`verify-wrangler` esegue la STESSA verifica passando dalla CLI di Wrangler (una sola `SELECT` per query, senza `;`
ne' parametri, `batch` vietato): e' il modo previsto per verificare in futuro un D1 remoto (`--remote --account-id
<id>` al posto di `--local`; il bersaglio va sempre indicato, non c'e' un valore predefinito). Con `--remote` l'ID
deve coincidere con l'unico `account_id` della configurazione e con `CLOUDFLARE_ACCOUNT_ID` se impostata; solo
allora viene passato a Wrangler, e mai insieme a variabili di credenziali (regola e motivi in
`scripts/wrangler-db.ts`, procedura in [prova-remota-d1.md](prova-remota-d1.md)). Provato SOLO con `--local`
(stesso risultato della lettura diretta del file); per `--remote` sono provati solo i rifiuti, che avvengono prima
di avviare Wrangler.
`ok` = nessun checksum diverso, schema identico all'istantanea (se alla versione del codice), registro Wrangler
coerente; `upToDate` = nessuna migrazione in attesa. Su un database a una versione precedente lo schema NON viene
confrontato (l'istantanea e' solo dell'ultima versione): `ok` vale solo per storia e registro.

**Database migrato dal runner che passa a Wrangler** (allineamento del registro)
```bash
node scripts/migrations.ts baseline > baseline.sql
npx wrangler d1 execute <database> --local --file baseline.sql
```
Il baseline e' un unico batch "tutto o niente" con guardie (`src/db/baseline.ts`). Registra i file SOLO se il
database dimostra di essere esattamente alla versione del codice:
| Condizione | Esito |
|---|---|
| storia `schema_migrations` = 1..N con nomi e checksum del codice | richiesta (riga in piu', in meno, versione 0, checksum diverso -> rifiuto) |
| registro `d1_migrations` vuoto o prefisso ORDINATO dei file attesi | richiesta (nome estraneo, buco, ordine invertito -> rifiuto); completa solo i file mancanti in coda |
| schema del progetto identico all'istantanea, testo SQL ESATTO | richiesta (oggetto in piu', mancante o diverso, anche solo negli spazi -> rifiuto) |
| database vuoto o a una versione precedente | rifiuto: prima si porta il database alla versione del codice |
| rieseguito su un database gia' allineato | nessuna modifica (idempotente) |
Un rifiuto non scrive nulla, nemmeno la tabella `d1_migrations`; il messaggio contiene `write_guard_seq`
(o `no such table` su un database non inizializzato). Il baseline non corregge mai dati incoerenti.
Senza allineamento `migrations apply` fallisce subito sul file 1 senza modifiche (provato).

**Interruzioni e divergenze (provato in locale)**
| Situazione | Comportamento |
|---|---|
| errore a meta' di una migrazione o del baseline (stesso file) | batch annullato per intero: nessuna tabella, nessuna riga di registro |
| file mancante nella sequenza | Wrangler applica il successivo per nome; la guardia lo rifiuta senza modifiche (schema identico alla versione precedente); aggiunti i file mancanti, lo stesso database si completa normalmente |
| manca il primo file | rifiuto (`no such table`); unico residuo la tabella `d1_migrations` VUOTA che Wrangler crea prima di applicare; `verify`: versione 0, coerente |
| `d1_migrations` avanti rispetto a `schema_migrations` (file registrato ma non applicato) | Wrangler salta il file registrato; il successivo e' rifiutato dalla guardia; `verify` incoerente; baseline rifiutato |
| `schema_migrations` avanti rispetto a `d1_migrations` | `migrations apply` rifiutato (tabelle gia' esistenti o guardia); baseline completa il registro se storia e schema sono dimostrati |
| oggetti inattesi nello schema | `verify` li elenca; il baseline rifiuta; NOTA: `migrations apply` NON controlla lo schema (solo la storia): per questo la procedura richiede `verify` prima e dopo ogni applicazione |
| processo terminato bruscamente (es. interruzione di rete) | NON provato: in locale il batch e' atomico; da remoto vedi i limiti sotto |

**Recupero dopo un errore**
- una migrazione fallita non lascia nulla (batch atomico, provato in locale): correggere la causa e rieseguire;
- guardia fallita (`write_guard_seq`): eseguire `verify` per vedere versione, checksum, schema e registro; NON
  modificare a mano `schema_migrations` (immutabile per trigger), `d1_migrations` ne' i file generati;
- schema diverso dall'istantanea (`schema.diff`): oggetti aggiunti o modificati fuori dalle migrazioni; da indagare
  prima di qualsiasi nuova migrazione;
- non esiste un "annulla" automatico di una migrazione riuscita: le migrazioni sono solo additive; si corregge con
  una nuova migrazione. Eliminare il database non e' un recupero per un database con dati reali.

## Non verificato
- `wrangler d1 migrations apply --remote` e `d1 execute --remote --file`: Wrangler usa canali diversi dal batch
  locale; l'atomicita' remota di un file non e' provata.
- Testo SQL in `sqlite_master` e oggetti interni su D1 remoto: se differiscono, baseline e verifica RIFIUTANO
  (direzione sicura), ma la procedura andrebbe rivista.
- Concorrenza tra due applicazioni simultanee della stessa migrazione su D1 remoto.
- Time Travel / ripristino di D1 remoto dopo una migrazione.

Procedura per la futura prova remota (non eseguita): [prova-remota-d1.md](prova-remota-d1.md).
