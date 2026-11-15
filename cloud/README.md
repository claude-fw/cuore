# CUORE Cloud — Cloudflare Worker

Worker Cloudflare che serve l'interfaccia di `app/index.html` in **modalità vetrina** (solo dati inventati) dietro la
verifica del token di Cloudflare Access, con un Durable Object SQLite che esegue il motore Finance sugli **esempi
sintetici** di `finance/examples/sintetici`.
Il deploy sul tuo account Cloudflare è descritto in «Produzione» più sotto: niente parte da solo, ogni passo remoto è esplicito.

## Struttura
| File | Ruolo |
|---|---|
| `src/access.ts` | verifica del token di Access (`Cf-Access-Jwt-Assertion`): RS256 per `kid`, `iss`, `aud`, `exp`/`nbf`/`iat`, `type` = `app`, email in elenco; configurazione chiusa per difetto |
| `src/app.ts` | porta d'ingresso comune: interruttore `CUORE_STATO`, solo GET/HEAD, configurazione, richieste da altri siti, identità, rotte `/` e `/api/vetrina/stato`, intestazioni di sicurezza |
| `src/index.ts` | ingresso di **produzione** (`wrangler.jsonc`): chiavi pubbliche del team di Access; nessun accesso simulato |
| `src/locale.ts` | ingresso **solo locale** (`wrangler.locale.jsonc`): accesso SIMULATO con chiave in memoria, risponde solo a localhost |
| `src/finance-do.ts`, `src/durable-db.ts` | Durable Object «Finance» (schema v7 sintetico, marcato `sintetico`; conti conto/carta/contanti DEMO) e adattatore `Db` del motore |
| `src/finance-api.ts` | API di scrittura C2 `POST /api/finance/esegui`: stesse operazioni del ponte di prova, `requestId` idempotente, `versione` per i conflitti; aperta solo con `CUORE_SCRITTURE=attive` (solo in `wrangler.locale.jsonc`) |
| `src/automazioni.ts` | C3: Cron (`scheduled`, 4 turni al giorno solo in `wrangler.locale.jsonc`), coda `cloud_coda` + esecutore B3 persistente (`SqlJobStore`), riprove con l'allarme del DO, lavori `abbinamenti`/`budget`/`push`, notifiche (spente per difetto) |
| `src/push.ts` | push Web senza contenuto firmato VAPID (ES256); solo servizi push noti in https; chiave `VAPID_PRIVATE_JWK` + `VAPID_SUBJECT`, senza chiave nessun invio |
| `src/school.ts`, `src/school-do.ts`, `src/school-feed-campi.json` | E3: Durable Object «School» (SQLite proprio, separato da Finance) con SOLO l'ultimo feed `cuore.school.feed/1` inviato dal Mac; schema chiuso con i campi ammessi in `school-feed-campi.json` (stesso file usato dal mittente sul Mac); vedi sotto |
| `src/migrazione.ts`, `scripts/migra-finance.ts` | M3 (solo prova, database sintetici): dati Finance dal Mac a un DO **separato** («finance», nasce vuoto) con l'esportazione logica di L1, a blocchi, in una sola transazione verificata; spenta per difetto (vedi sotto) |
| `src/backup.ts`, `src/backup-cloud.ts` | L1: formato `cuore.finance.backup/1` (cifrato, firmato) e backup **conservati** nel bucket R2 `BACKUP` (una copia al giorno dal Cron, ultime 7) con il ritiro del Mac; vedi sotto |
| `scripts/ripristina-backup.ts`, `scripts/ritira-backup.ts` | sul Mac: ripristino di un backup in un database **nuovo** (firma, integrità, schema, impronta) e ritiro dal cloud con il token `mac-backup` (chiavi dal Portachiavi per difetto) |
| `scripts/chiavi-mac.ts` | L1: genera le chiavi vere dei backup (lo lancia SOLO il titolare): privata del Mac nel Portachiavi, stampa solo `kid` e pubbliche; recupero su chiavetta o carta |
| `src/online.ts`, `src/school-personale.ts` | **dati veri online** (`CUORE_FINANCE_ORIGINE` = "mac"): pagina con «online · dati reali» e le stesse API del ponte (`/api/money` dal DO con `app/bridge/cuore_money_query.mts`, `/api/search` come `cuore_search.py`, `/api/scritture`, `POST /api/scritture/esegui` → C2 o note School nel DO School); promemoria e appunti restano sul Mac. |
| `src/misure.ts`, `wrangler.misure.jsonc`, `scripts/misura-banco.ts` | **banco di misura** (Worker separato, solo dati sintetici): CPU del motore Finance con 5000 movimenti e prova del PITR; vedi sotto |
| `scripts/pacchetto.ts`, `scripts/controlla-configurazione.ts` | prove di pubblicazione senza rete: pacchetto di produzione e configurazione mancante (chi fa cosa) |

Nessuna dipendenza propria: TypeScript, Biome, esbuild, Miniflare e Wrangler sono quelli già installati in `../finance`.

## Feed School dal Mac (E3) — pronto in locale, SPENTO
- `POST /api/school`: solo il token di servizio di Access del Mac (`mac-school`). Il Worker verifica il token firmato da
  Access (`Cf-Access-Jwt-Assertion`: firma, emittente, audience, orari, nessuna email, `common_name` = Client ID fra
  quelli in `ACCESS_SERVIZIO_SCHOOL`). Senza quella variabile la rotta risponde 503 (`invio_school_spento`). JSON, al
  massimo 1 MiB; schema chiuso (campo in più, versione diversa, stato o messaggio fuori vocabolario, chiave che sembra
  una credenziale = 422); un feed più vecchio dell'ultimo = 409; lo stesso feed ripetuto = 200 `ripetuto`.
- `GET /api/school` (titolare, Access): l'ultimo feed più `consegna` (`ricevuto`, `generato`, `eta_minuti`, `stato`
  recente/vecchio, soglia 13 ore); oltre la soglia `freschezza.stato` diventa `dati_vecchi`. 404 se non è mai arrivato
  nulla. Il token del Mac non legge; il token del titolare non scrive.
- Nessuno storico, nessuna credenziale ClasseViva/Classroom, nessuna chiamata alle fonti. L'interfaccia in modalità
  vetrina oggi non chiama ancora `/api/school` (`app/` non modificata).
- Locale: `src/locale.ts` simula anche il token del Mac (valori fittizi `SIMULATED_SERVICE`, Client ID in
  `wrangler.locale.jsonc`). Mittente sul Mac: `scuola/mcp/classeviva/school_cloud_push.py`.

## Migrazione dei dati veri (M3) — pronta come prova, SPENTA
- `POST /api/finance/migrazione`: solo un token di servizio fra `ACCESS_SERVIZIO_MIGRAZIONE` **e** con
  `CUORE_FINANCE_ORIGINE` = `mac`; altrimenti 503. Con `mac` il Worker usa un DO Finance separato («finance») che non
  carica esempi e, finché la migrazione non è completata, rifiuta scritture, catture e Cron (409).
- Protocollo `cuore.finance.migrazione/1`: inizio (manifesto con impronta logica), blocchi, fine. Alla fine una sola
  transazione: chiavi esterne differite, riletture riga per riga, `foreign_key_check`; qualunque differenza = nulla
  scritto. Un secondo invio dopo il successo = 409.
- Sul Mac: `npm run migrazione -- --database <file>` = prova **a vuoto** (istantanea in memoria, solo conteggi e
  impronta). L'invio vuole `--invia`, `--url https://…`, `--conferma-impronta <12 cifre>` e il token nell'ambiente.
- Provata solo su database sintetici (v7 «come la reale» e v8): Mac → DO (Miniflare) → backup L1 → ripristino,
  impronte uguali (`test/migrazione.test.ts`).

## Backup conservati nel cloud (L1) — pronti in locale, SPENTI
- Al turno del Cron il DO Finance scrive in R2 una copia `cuore.finance.backup/1` del giorno
  (`finance/AAAA-MM-GG.cuorebak.json`, cifrata per i destinatari configurati e firmata) e tiene le **ultime 7**.
  Senza binding `BACKUP` nessun lavoro; senza chiavi (`BACKUP_SIGNING_PRIVATE_JWK`, `BACKUP_MAC_PUBLIC_JWK`,
  `BACKUP_RECOVERY_PUBLIC_JWK`) il lavoro si salta.
- `GET /api/finance/backup` (la più recente, oppure `?giorno=AAAA-MM-GG`) e `GET /api/finance/backup/elenco`: solo il
  token di servizio `mac-backup` (Client ID in `ACCESS_SERVIZIO_BACKUP`, stesso schema di `mac-school`); senza Client ID
  o senza bucket 503; titolare o altri token 403.
- Sul Mac: `npm run ritira-backup -- --url … --cartella <fuori da iCloud> --chiave-privata … --chiave-firma …`
  (token nell'ambiente: `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`): scarica, verifica la firma e ripristina in un
  database nuovo con `ripristina-backup.ts`; `--elenco` mostra i giorni. Non sovrascrive nulla.
- In `wrangler.jsonc` il binding R2 è **commentato** finché non si attiva R2 (può chiedere un metodo di
  pagamento [da verificare]); in `wrangler.locale.jsonc` il bucket è simulato. Provato in `test/backup-cloud.test.ts`
  (R2 di Miniflare in memoria): 7 copie e rotazione, rotte chiuse, ritiro solo col token giusto, impronta uguale.

## Banco di misura (CPU dei Durable Object sul Free e PITR) — Worker separato, SPENTO
- `src/misure.ts` è un ingresso **diverso** da quello di produzione (`wrangler.misure.jsonc`, nome
  `cuore-misure-sintetiche`, DO `BancoMisure`, niente binding della vetrina, niente Cron). `index.ts` non lo importa e
  `npm run pacchetto` controlla che non entri nella vetrina. Scelto così (e non con una rotta spenta nella vetrina)
  per non toccare l'ingresso di produzione e poterlo cancellare intero dopo la misura.
- Chiuso per difetto: senza `MISURE_TOKEN_SHA256` (si passa solo al deploy con `--var`) 503; token Bearer sbagliato 401.
- `POST /misure/azzera` · `/misure/carica` (a blocchi) · `/misure/esegui` (`classifica`, `trasferimenti`, `feed`,
  `consulta` + `nome`, `backup`, `stato`) · `/misure/pitr/…` (`segnalibro`, `scrivi`, `prepara`, `riavvia`, `leggi`).
- `npm run misure` lo fa girare in locale (Miniflare, token in memoria) e stampa i tempi; con `--url https://…` e
  `MISURE_TOKEN` nell'ambiente misura il banco pubblicato; `--pitr` prova il ripristino a un istante (in Miniflare non
  esiste: 501).

## Comandi (dalla cartella `cloud/`)
- `npm test` — verifica del token (Node) e i due ingressi in workerd (Miniflare), chiavi di prova generate a ogni
  esecuzione, chiavi del team servite da un finto `outboundService`, oggetto `cf` fittizio (`cf: false`): nessuna rete.
- `npm run typecheck`, `npm run lint` (solo `src/`, `test/`, `scripts/` e le configurazioni: `.wrangler/` è cache).
- `npm run pacchetto` — `wrangler deploy --dry-run` con `wrangler.jsonc` in una cartella temporanea fuori dal
  repository (metriche spente, credenziali tolte, proxy verso una porta chiusa): dimensione compressa contro i limiti
  Free/Paid, nessun modulo `node:`, nessun accesso simulato, nessun segreto. Il 04/11: ~148 KiB compressi.
- `npm run configurazione` — cosa manca in `wrangler.jsonc` per pubblicare, con «serve l'account» / «basta
  il titolare»; dei segreti solo i nomi; controlla anche `wrangler.misure.jsonc` (Worker separato, spento).
- `npm run misure` — banco di misura in locale (5000 movimenti sintetici; `--movimenti`, `--blocco`, `--ripetizioni`,
  `--ricarica-ogni`, `--pitr`, `--url`).
- `npm run ritira-backup` — ritiro e ripristino di un backup conservato (sopra).
- `npm run dev` — vetrina locale su `http://127.0.0.1:8787` con `WRANGLER_SEND_METRICS=false` e
  `CLOUDFLARE_CF_FETCH_ENABLED=false` (senza, Miniflare scarica `workers.cloudflare.com/cf.json` e lo salva in
  `.wrangler/cache/`: è successo il 27/10), senza `--remote`; aprire `/__locale/accedi`. Con
  `--persist-to <cartella fuori dal repository>` lo stato del Durable Object resta fuori da qui.

## Produzione (sul tuo account Cloudflare)
Servono: Worker `cuore` sull'account Cloudflare, applicazione Access a livello di Worker con il
solo GitHub come provider, `ACCESS_TEAM_DOMAIN` e `ACCESS_AUD` come variabili, `ACCESS_ALLOWED_EMAILS` come segreto.
Senza questi valori il Worker risponde 503 a tutto. Per E3 in più: migrazione `v2` (classe `SchoolFeed`, già in
`wrangler.jsonc`), token di servizio `mac-school` in Access ammesso solo su `POST /api/school`, il suo Client ID come
variabile `ACCESS_SERVIZIO_SCHOOL`. Per i backup L1: R2 attivato, bucket `cuore-backup`, binding `BACKUP`, Cron, token
`mac-backup` (`ACCESS_SERVIZIO_BACKUP`) e chiavi. `wrangler deploy`, `secret put` e `--remote` sono passi tuoi, da lanciare solo quando i valori sopra sono pronti.

## Notifiche push e promemoria online
Rotte (dopo Access; le POST come le scritture: `CUORE_SCRITTURE` attive, Origin del sito, JSON):
`GET /api/notifiche`, `GET /api/notifiche/recenti`, `POST /api/notifiche/{iscrizione,annulla,imposta,prova}`,
`GET /api/promemoria`; operazioni del ponte `promemoria_*`/`appunti_*` su `POST /api/scritture/esegui`.
File statici serviti: `/sw.js`, `/push.js`, `/manifest.webmanifest`, icone. Chiavi: `npm run vapid`
Test: `test/notifiche.test.ts`.
