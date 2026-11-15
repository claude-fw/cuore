# Cloud Finance — nucleo locale (Fasi 2-3)

Nucleo TypeScript per conti, transazioni, saldi, riconciliazione, classificazione, obiettivi, acquisti pianificati,
crediti e debiti.
Gira in locale su SQLite (`node:sqlite`) e, con lo stesso codice, su **Cloudflare D1 locale** (Miniflare/workerd e
Wrangler `--local`): verifica in [docs/d1.md](docs/d1.md). Nessun deploy e nessuna risorsa remota.

> Solo dati **sintetici** nel repository. Nessun collegamento bancario, nessun accesso a Notion, nessun servizio remoto.
> I formati di importazione disponibili sono formati **neutri di questo progetto**: non sono i formati di Revolut o
> MPS, che non sono ancora stati verificati (`verifiedAgainstRealExports: false`).

## Requisiti
- Node.js **>= 24** (usa `node:sqlite` e l'esecuzione diretta di TypeScript, senza compilazione).
- Dipendenze di sviluppo (versioni bloccate): `typescript` 7.0.2, `@biomejs/biome` 2.5.15, `@types/node` 24.19.1,
  `wrangler` 4.147.0, `miniflare` 5.20261001.0-alpha, `esbuild` 0.28.1 (le ultime tre solo per i test D1 locali).
  Nessuna dipendenza di esecuzione.

## Installazione
```bash
npm install --ignore-scripts
npm run check        # type-check + lint + migrazioni allineate + test (Node e D1 locale) + build
```

## Uso rapido (dati sintetici)
Il database va sempre indicato in modo esplicito (`--db` o `CLOUD_FINANCE_DB`), **fuori** dal repository e fuori da
iCloud (un database SQLite attivo in una cartella sincronizzata rischia di corrompersi).

```bash
export CLOUD_FINANCE_DB="$HOME/Library/Application Support/Cloud/finance/demo.sqlite3"
npm run cli -- migrate
npm run cli -- account:add --id conto --name "Conto DEMO" --type current --currency EUR
npm run cli -- account:add --id carta --name "Carta DEMO" --type card --currency EUR
npm run cli -- import --account conto --format cloud-finance-csv --file examples/sintetici/conto-demo.csv
npm run cli -- import --account carta --format cloud-finance-csv --file examples/sintetici/carta-demo.csv
npm run cli -- import --account conto --format cloud-finance-balances-csv --file examples/sintetici/saldi-conto-demo.csv
npm run cli -- rules:publish --file examples/sintetici/regole-demo.json
npm run cli -- classify
npm run cli -- detect
npm run cli -- review
npm run cli -- reconcile --account conto --from 2026-01-01 --to 2026-01-31
npm run cli -- report --from 2026-01-01 --to 2026-01-31
npm run cli -- query summary --input '{"from":"2026-01-01","to":"2026-01-31"}'   # sola lettura
```
`npm run cli -- --help` elenca tutti i comandi (conti, import, saldi, regole, classificazione manuale, collegamenti,
coda di verifica, riconciliazione, riepiloghi, liquidita', obiettivi, acquisti pianificati, crediti e debiti, cambi).

Uscite della CLI: JSON su stdout; codice 0 = ok, 2 = input non valido o conflitto, 3 = file rifiutato, 1 = errore interno.

## Comandi di sviluppo
| Comando | Effetto |
|---|---|
| `npm test` | test (`node --test`, database in memoria e file temporanei, dati sintetici) |
| `npm run migrations:generate` | rigenera `migrations/*.sql` (per Wrangler) e l'istantanea dello schema da `MIGRATIONS` |
| `npm run migrations:check` | verifica che file SQL e istantanea coincidano con il codice |
| `node scripts/probe-format.ts --file <export>` | analisi LOCALE della struttura di un export bancario (solo forme, nessun dato in chiaro): vedi la checklist |
| `node scripts/import-plan.ts --plan <piano.json>` | prova di un piano di importazione su database temporaneo (dry-run, solo aggregati) |
| `node scripts/import-plan.ts --plan <piano.json> --apply <db.sqlite3>` | aggiornamento incrementale: prova su copia, backup verificato, append-only, sostituzione atomica |
| `node scripts/review.ts export\|check ...` | revisione assistita: file di decisioni e scheda (sola lettura), validazione e simulazione su copia |
| `node scripts/mcp-server.ts --db <db.sqlite3>` | prototipo MCP locale in sola lettura (stdio), non registrato in alcun client |
| `npm run test:d1` | stessa suite su D1 locale + compatibilita' D1 + nucleo dentro workerd + Wrangler CLI `--local` |
| `npm run typecheck` | `tsc` in modalita' rigorosa (nessuna emissione) |
| `npm run lint` | Biome: lint + formattazione (solo controllo) |
| `npm run format` | Biome: formatta i file |
| `npm run build` | compila in `dist/` (JavaScript + dichiarazioni) |
| `npm run check` | tutto quanto sopra, in ordine |

## Documentazione
- [Architettura e decisioni](docs/architettura.md)
- [Schema dati e migrazioni](docs/schema-e-migrazioni.md)
- [Formati di importazione](docs/formati-import.md)
- [Crediti e debiti (schema v5)](docs/crediti-e-debiti.md)
- [Consultazione in sola lettura (contratto v1, base per MCP e dashboard)](docs/consultazione.md)
- [Ricerca FTS5 sui movimenti (v8, pronta, in attesa di attivazione)](docs/ricerca.md)
- [Classificazione, collegamenti, verifica e riconciliazione](docs/dati-affidabili.md)
- [Cloudflare D1: verifica locale](docs/d1.md)
- [Migrazioni: strategia ibrida runner + Wrangler](docs/migrazioni-d1.md)
- [Procedura per una futura prova su D1 remoto (non eseguita)](docs/prova-remota-d1.md)
- [Ambiente sintetico: copia di prova per le scritture (D3a)](docs/ambiente-sintetico.md)

## Struttura
```
src/
  db/          interfaccia Db, adattatori node:sqlite e D1, migrazioni, scrittura con controllo di concorrenza
  domain/      funzioni pure: importi, CSV, testo/date, hash, regole, riepiloghi, riconciliazione, abbinamenti, obiettivi, crediti/debiti
  import/      contratto dei parser, parser dei formati neutri, piano di importazione (puro), importatore,
               tabular/ (motore a specifica per formati bancari), xlsx/ (lettore ZIP/XML/foglio senza dipendenze),
               parsers/bank-formats.ts (formati bancari solo predisposti)
  services/    operazioni sul database: conti, saldi, classificazione, collegamenti, verifica, riconciliazione,
               pianificazione, crediti e debiti, riepiloghi
  query/       consultazione in sola lettura (contratto v1): operazioni, validazione, busta di risposta
  cli/         interfaccia a riga di comando (unico punto con I/O di Node oltre all'adattatore SQLite)
test/          test automatici
examples/sintetici/  file di esempio con dati inventati
```
