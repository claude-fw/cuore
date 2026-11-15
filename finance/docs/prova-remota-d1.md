# Procedura per la futura prova su D1 remoto (NON eseguita)

Stato: **solo documentazione**. Nessuna risorsa Cloudflare e' stata creata, nessuna autenticazione e' stata fatta.
Scopo della prova: un database D1 **isolato e usa e getta**, con soli dati sintetici, per verificare da remoto cio' che
in locale non si puo' provare (atomicita' di `migrations apply --remote` e `execute --remote --file`, testo SQL in
`sqlite_master`, oggetti interni di D1). Non e' un passo verso la produzione.
I comandi esistono in Wrangler 4.147.0 (controllati con `--help` in locale); il loro comportamento remoto no.

## 0. Condizioni per iniziare (tutte)
- autorizzazione esplicita del titolare per QUESTA prova (creazione ed eliminazione di un database di prova);
- `npm run check` verde e `git status` annotato prima di iniziare;
- nessun dato reale: solo migrazioni e, al massimo, i file di `examples/sintetici`;
- Wrangler del repository (`npx wrangler`, versione 4.147.0), telemetria disattivata:
  `export WRANGLER_SEND_METRICS=false`.
- **comandi remoti eseguiti dal titolare in un Terminale interattivo, con `CI` NON impostato** (`echo "$CI"` vuoto).
  Verificato nel sorgente di Wrangler 4.147.0 (`confirm2`, `isNonInteractiveOrCI`): in un contesto non interattivo
  (variabile `CI` impostata o processo senza terminale, come un comando lanciato da un assistente) le conferme di
  `migrations apply` e `d1 delete` rispondono da sole "si'" e il comando procede senza chiedere.

## 1. Identita' e account (li esegue il titolare, solo dopo l'autorizzazione della prova)
1. Login con privilegi minimi, fatto a mano dal titolare nel proprio Terminale:
   ```bash
   npx wrangler login --scopes account:read user:read d1:write --use-keyring
   ```
   - verificato **solo staticamente** (aiuto e sorgente di Wrangler 4.147.0, nessun login eseguito): i tre ambiti
     esistono, `offline_access` viene aggiunto da Wrangler; senza `--scopes` Wrangler chiederebbe TUTTI gli ambiti
     predefiniti (Workers, Pages, Secrets Store, ...), compreso l'accesso ad altri progetti dell'account;
   - `--use-keyring` su macOS: credenziali in un file cifrato nella configurazione di Wrangler, chiave nel Portachiavi
     (comando di sistema `security`), nessuna installazione di pacchetti (quella avviene solo su Windows);
   - NON verificato: che questi ambiti bastino per `d1 create`, `d1 list`, `migrations apply`, `d1 delete`. Se un
     comando fallisce per permessi: FERMARSI; non rifare il login con ambiti piu' ampi senza una nuova decisione.
2. `npx wrangler whoami` — il titolare conferma email e **ID account** attesi; l'ID si copia da qui, mai indovinato.
3. `unset CLOUDFLARE_ACCOUNT_ID` (l'account si fissa nella configurazione dedicata, passo 2) e
   `npx wrangler d1 list` — annotare i database esistenti: la prova non deve toccarli.

## 2. Nome univoco e configurazione dedicata
- Nome: `cloud-finance-prova-<AAAAMMGG>-<4 caratteri casuali>` (es. `cloud-finance-prova-20261022-7f3a`); verificare
  che non compaia in `d1 list`. Mai riusare il nome di un database esistente.
- Cartella **fuori dal repository e fuori da iCloud**, es. `~/Library/Application Support/Cloud/finance/prova-remota/`,
  con due configurazioni che differiscono SOLO per `migrations_dir`:
  - `cfg.jsonc`: `migrations_dir` -> cartella `migrations/` del repository (tutte le migrazioni);
  - `cfg-buco.jsonc`: `migrations_dir` -> `migrations-buco/` nella cartella dedicata, con le copie di
    `0001_core.sql` e `0003_planning.sql` soltanto (sequenza volutamente incompleta, passo 4).
- Entrambe contengono `account_id` (l'ID confermato con `whoami`) **una sola volta** e il `database_id` restituito
  al passo 3. In Wrangler 4.147.0 `account_id` della configurazione ha la precedenza su `CLOUDFLARE_ACCOUNT_ID` e
  sull'account in cache: fissarlo qui rende esplicito il bersaglio di TUTTI i comandi della prova.
- Questi file contengono gli ID del database e dell'account: non vanno nel repository.
- `verify-wrangler --remote` richiede `--account-id <id>` IDENTICO all'`account_id` della configurazione, e rifiuta
  (prima di avviare Wrangler) configurazioni senza `account_id`, con piu' `account_id`, con un valore diverso, o un
  `CLOUDFLARE_ACCOUNT_ID` d'ambiente diverso. Solo allora passa `CLOUDFLARE_ACCOUNT_ID` al processo di Wrangler;
  non inoltra mai variabili di credenziali (codice: `scripts/wrangler-db.ts`; test: `test/wrangler-db.test.ts`).

## 3. Creazione
`npx wrangler d1 create <nome>` — crea UNA risorsa remota (conta nel limite di database del piano; costi e limiti
del piano attuale da controllare nella dashboard prima di procedere: non verificati qui).

## 4. Applicazione, rifiuto di una sequenza incompleta, verifica
`V` = `node scripts/migrations.ts verify-wrangler --database <nome> --remote --account-id <id>`
```bash
$V --config <cfg.jsonc>                                                    # atteso: version 0, ok=true, upToDate=false
npx wrangler d1 migrations apply <nome> --remote --config <cfg-buco.jsonc> # atteso: 0001 applicata, 0003 RIFIUTATA (write_guard_seq), uscita != 0
$V --config <cfg.jsonc>                                                    # atteso: version 1, ok=true
npx wrangler d1 execute <nome> --remote --config <cfg.jsonc> --command "SELECT name FROM sqlite_master WHERE name IN ('savings_goals', 'goal_allocations', 'planned_purchases', 'fx_rates')"
                                                                           # atteso: nessuna riga (nessun residuo di 0003)
npx wrangler d1 migrations apply <nome> --remote --config <cfg.jsonc>      # atteso: 0002, 0003, 0004 applicate
npx wrangler d1 migrations list <nome> --remote --config <cfg.jsonc>       # atteso: nessuna in attesa
$V --config <cfg.jsonc>                                                    # atteso: ok=true, upToDate=true, schema confrontato, registro coerente
```
I due `migrations apply` passano dall'endpoint `/query`: il rifiuto di 0003 e' la prova remota del canale usato
dalle migrazioni. Lo stesso percorso (rifiuto senza residui, poi completamento sullo stesso database) e' provato in
LOCALE da `test/d1-migrations.test.ts` ("L3 — sequenza incompleta"). In locale, se manca anche `0001`, l'unico
residuo e' la tabella `d1_migrations` VUOTA creata da Wrangler prima di applicare (provato).
Se `verify` riporta `schema.diff` con soli oggetti interni di D1 remoto (nomi non noti in locale) o differenze di
solo testo: FERMARSI e documentare; non modificare il codice durante la prova.

## 5. Prove di errore (solo sul database di prova)
1. Doppia applicazione: `npx wrangler d1 execute <nome> --remote --config <cfg.jsonc> --file migrations/0003_planning.sql`
   -> atteso rifiuto `write_guard_seq`; poi `verify` ancora `ok`. Questo prova l'atomicita' remota di
   `execute --file` per un errore a meta' file. Attenzione: da remoto `execute --file` usa l'API di **import**
   (caricamento su R2 ed elaborazione lato D1, database non disponibile nel frattempo), mentre
   `migrations apply` usa l'endpoint `/query` (sorgente di Wrangler 4.147.0): questa prova NON copre il canale
   di `migrations apply` (coperto dal passo 4).
2. Baseline su database gia' allineato: `node scripts/migrations.ts baseline > <file fuori dal repository>` e
   `d1 execute --remote --file <file>` -> atteso successo senza modifiche (idempotente, come in locale); poi
   `verify` ancora `ok` e `d1_migrations` invariato.
3. Annotare per ogni passo: comando, codice di uscita, output di `verify`.

## 6. Gestione degli errori durante la prova
- fermarsi al primo esito inatteso; NON modificare a mano `schema_migrations`, `d1_migrations` o i file generati;
- eseguire `verify-wrangler` (con `--account-id` e la configurazione dedicata) e salvare l'output;
- se serve analizzare: `npx wrangler d1 export <nome> --remote --output <file fuori repository>` (non modifica il
  database remoto, ma secondo la documentazione Cloudflare un export in corso blocca le altre richieste; il file
  locale contiene i dati: solo sintetici in questa prova);
- `time-travel restore` NON e' un "annulla" innocuo: secondo la documentazione Cloudflare e' un'operazione
  distruttiva che sovrascrive il database; restituisce un bookmark dello stato precedente con cui si puo' tornare
  indietro (entro la finestra di Time Travel: 7 giorni su Workers Free, 30 su Paid). Annotare comunque il bookmark
  attuale prima (`npx wrangler d1 time-travel info <nome>`); non necessario per un database di prova.

## 7. Pulizia
- solo dopo aver salvato gli esiti: `npx wrangler d1 delete <nome>` (conferma SOLO in un Terminale interattivo con
  `CI` non impostato, vedi 0.; non usare `-y`); ricontrollare il nome prima di confermare;
- eliminare la cartella dedicata del passo 2 (Cestino);
- `npx wrangler logout` (consigliato: rimuove le credenziali salvate con `--use-keyring`);
- verificare con `npx wrangler d1 list` che restino solo i database annotati al passo 1.3.

## Reversibilita' delle operazioni
| Operazione | Reversibile? | Note |
|---|---|---|
| `wrangler login` | si' | `wrangler logout`; revoca anche dalla dashboard |
| `whoami`, `d1 list`, `d1 info`, `migrations list`, `verify-wrangler` | nessun effetto | sola lettura |
| `d1 export` | nessun effetto remoto | crea un file locale con i dati: va trattato come dato personale se il database li contiene |
| `d1 create` | si', con `d1 delete` | consuma una risorsa del piano finche' esiste |
| `migrations apply` riuscita | **no** (nessun rollback automatico) | si corregge solo in avanti con una nuova migrazione; per un database di prova si elimina |
| migrazione o baseline rifiutati | nulla da annullare | atomicita' provata in locale; da remoto e' proprio cio' che la prova deve verificare |
| `baseline` riuscito | tecnicamente si' (righe in `d1_migrations`) | non annullarlo a mano: un registro modificato a mano e' cio' che le guardie rifiutano |
| `time-travel restore` | si', entro la finestra di Time Travel (bookmark restituito dal restore) | distruttivo: sovrascrive lo stato corrente |
| `d1 delete` | **da considerare no** | elimina database e dati; la documentazione di Time Travel non prevede il recupero di un database eliminato; per un database con dati reali NON e' un recupero |

Eliminare il database e' la pulizia corretta SOLO per questa prova (database usa e getta, dati sintetici). Per un
database che contiene dati reali il recupero e': fermarsi, `verify`, `export`, analisi, correzione in avanti.

## Limiti NON verificati (richiedono il remoto)
- atomicita' dell'endpoint `/query` con piu' istruzioni (usato da `migrations apply` e `execute --command`): la
  documentazione Cloudflare dice che le istruzioni sono "eseguite come batch", non garantisce esplicitamente il
  rollback; in locale il batch e' atomico (provato);
- atomicita' dell'API di import (`execute --file`): non documentata; in locale e' atomica (provato);
- sufficienza degli ambiti `account:read user:read d1:write` per tutti i comandi della prova;
- testo SQL in `sqlite_master` e oggetti interni del D1 remoto (se diversi, verifica e baseline RIFIUTANO);
- formato JSON di `d1 execute --remote --json` letto da `verify-wrangler` (provato solo con `--local`);
- piano e fatturazione dell'account; recupero di un database eliminato.
