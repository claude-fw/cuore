# Setup guide: from a clone to your own CUORE

This is the step-by-step path. Every stage works on its own, so you can stop wherever you have what you need.

> **What has been tested and what has not.** Stages 1–2 (demo, local Worker, the full check suite) were run from a fresh
> clone. Stages 3–7 describe how the author set up the deployment they run; the commands
> exist in this repository, but the guide itself has not been replayed on a brand-new Cloudflare, Google or bank account. Expect small gaps and
> read the linked scripts, whose header comments are detailed (they are in Italian).

## What you need

| For | You need |
|---|---|
| Everything | Node.js ≥ 24, Python ≥ 3.9 (the macOS system Python is fine), `git` |
| School job and local bridge | macOS (Keychain, `launchd`) |
| Online version | a Cloudflare account (the Free plan is enough) and a GitHub account (used as the login provider) |
| Bank sync | an [Enable Banking](https://enablebanking.com) application, and an account at a bank they support |
| School (Italy) | ClasseViva credentials; for Classroom, a Google Cloud OAuth client |

## 1. Look at it, with invented data (2 minutes)

```bash
git clone <this repository> cuore && cd cuore
open app/index.html            # the whole UI, demo data, no server, no network
```

For the real server code, still with invented data:

```bash
cd finance && npm ci && cd ../cloud
npm run dev                    # Worker + Durable Objects on http://127.0.0.1:8787
```
Open `http://127.0.0.1:8787/__locale/accedi` and choose *«Entra come titolare simulato»*: this local page replaces
Cloudflare Access, exists only in local mode, and lands on the app with a banner saying the data is invented (**«VETRINA · DATI INVENTATI»**; the local login page itself
is marked «ACCESSO SIMULATO · SOLO LOCALE»). Opening `app/index.html` directly shows «DATI DIMOSTRATIVI» instead.

## 2. Check that your clone works

```bash
npm run verifica               # from the repository root, ~5 minutes
```
It runs type checks, lint, migrations and every test suite (finance, cloud, app, school). `TUTTO OK` at the end means
the engines work on your machine. If it fails on a fresh clone, that is a bug worth reporting.

## 3. School data on your Mac (ClasseViva and Classroom)

Everything here stays on the Mac; credentials live in the macOS Keychain, never in files.

1. **ClasseViva:** `sh scuola/mcp/classeviva/setup.sh`. It stores your username and password in the Keychain
   (service `cloud.classeviva`), tests a real login, and registers the `classeviva` MCP server in Claude Desktop and
   Claude Code. Your password is typed into the macOS prompt and never shown.
2. **Google Classroom (optional):** create an OAuth client in Google Cloud (Desktop app, scope
   `classroom.courses.readonly` plus the read-only Classroom scopes listed in `scuola/docs/classroom-probe.md`), then
   `sh scuola/mcp/classroom_probe/setup_keychain.sh` and `python3 scuola/mcp/classeviva/cls_auth.py authorize
   --account <your school Google account>`. Some school domains restrict OAuth apps: the probe tells you.
3. **First run, without writing anything:** `/usr/bin/python3 scuola/mcp/classeviva/school_job.py run` (dry run).
   When it looks right: `... run --commit` writes the local Registry (`~/Library/Application Support/Cloud/school/`).
4. **Automatic runs:** `sh scuola/mcp/classeviva/launchd/school_job_launchd.sh install --conferma`. The agent points to
   the absolute path of this repository: do not move it afterwards. Details: `scuola/docs/automazione-scuola.md`.
5. **Local dashboard on your data:** `python3 app/bridge/serve.py` serves the app and the read-only feeds on
   `127.0.0.1:8765` only. It reads the Registry from stage 3 and the Finance database from
   `~/Library/Application Support/Cloud/finance/db/cloud-finance.sqlite3` (move or point your database there).

Using another country's school register? The Registry, feed contract (`docs/it/contratto-school.md`) and UI are generic;
the two clients (`cvv_*`, `cls_*`) are what you would replace.

## 4. Money

The Finance engine is a SQLite database plus a command-line tool. From `finance/`:

```bash
npm run cli -- --db ~/finance.sqlite3 migrate                      # create the database (any path outside iCloud and this repository)
npm run cli -- --db ~/finance.sqlite3 institution:add --id mybank --name "My bank"
npm run cli -- --db ~/finance.sqlite3 account:add --id main --name "Main account" --type current --currency EUR --institution mybank
npm run cli -- --db ~/finance.sqlite3 formats                      # which import formats exist
npm run cli -- --db ~/finance.sqlite3 import --account main --format cloud-finance-csv --file examples/sintetici/conto-demo.csv
npm run cli -- --db ~/finance.sqlite3 rules:publish --file examples/sintetici/regole-demo.json   # classification rules (categories)
npm run cli -- --db ~/finance.sqlite3 classify
npm run cli -- --db ~/finance.sqlite3 report --from 2026-01-01 --to 2026-12-31
```
(The import, rules and report lines use the invented example files; use your own export, converted to a supported format, or the bank sync of
stage 6.) `npm run cli -- --help` lists everything else: rules, budgets, goals, credits and debts, reconciliation.
Everything is append-only with a reason; corrections never delete history (`finance/docs/dati-affidabili.md`).
Do your first imports on a *copy* of the database, and keep it outside iCloud (iCloud corrupts live databases).

## 5. Put it online on your own Cloudflare account

Order matters: the Worker refuses every request (503) until Access is configured, by design, so an unfinished setup is never open.
The Access application needs the Worker's address, `https://cuore.<your-subdomain>.workers.dev` (your subdomain is in the
Cloudflare dashboard under Workers & Pages). If you do not have one yet, run a first `wrangler deploy` (step 5) with the empty
values: it deploys and answers 503, which is fine. Then come back to step 1. (This order has not been replayed on a new account.)

1. **Cloudflare Access:** in Zero Trust, create a *self-hosted application* in front of your Worker with **GitHub** as
   the only login method and a policy allowing only your e-mail. Note the team domain and the application **AUD**.
2. **Configuration:** in `cloud/wrangler.jsonc` fill `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, `VAPID_SUBJECT`.
   Generate push keys with `npm run vapid` (public key → `VAPID_PUBLIC_KEY`; the private one is a secret).
3. **Secrets** (`../finance/node_modules/.bin/wrangler secret put <NAME>` from `cloud/`):
   `ACCESS_ALLOWED_EMAILS` (your e-mail, never in a file), `VAPID_PRIVATE_JWK`, and later the bank and backup secrets.
4. **Check before deploying:** `npm run configurazione` lists, in plain words, what is still missing; `npm run pacchetto`
   builds the bundle offline and checks size limits. **Backup bucket:** `wrangler.jsonc` already declares an R2 bucket
   `cuore-backup`, so create it first (`../finance/node_modules/.bin/wrangler r2 bucket create cuore-backup`; R2 may ask for a
   payment method), or remove the `r2_buckets` block if you want to skip backups for now. A deploy that references a
   missing bucket will most likely fail (not tested).
5. **Deploy:** `../finance/node_modules/.bin/wrangler deploy` from `cloud/`. Open the Worker URL: Cloudflare Access asks
   you to log in with GitHub, then you see the app.
6. **Send school data from the Mac:** in Access create a *service token* `mac-school` allowed only on
   `POST /api/school`; put its Client ID in `ACCESS_SERVIZIO_SCHOOL`; on the Mac create
   `~/Library/Application Support/Cloud/school/job/cuore-cloud.json` with `{"url": "https://<your-worker>/api/school",
   "token_file": "<absolute path>"}` (permissions 600; the token file holds the service token's `client_id` and
   `client_secret`). Test without sending: `python3 scuola/mcp/classeviva/school_cloud_push.py prova`.
7. **Encrypted backups (recommended):** with the R2 bucket from step 4 in place,
   `npm run chiavi-mac -- --recupero-su <file on a USB stick>` generates the keys (the offline recovery key must live
   **outside** the Mac), put the two printed public keys in `wrangler.jsonc`, and add the signing key as a secret as
   described at the top of `cloud/scripts/chiavi-mac.ts`. To pull a backup back to the Mac (`npm run ritira-backup -- --url
   https://<your-worker> --cartella <folder outside iCloud and the repository>`), create one more Access service token,
   `mac-backup`, put its Client ID in `ACCESS_SERVIZIO_BACKUP`, and export its `CF_ACCESS_CLIENT_ID` and
   `CF_ACCESS_CLIENT_SECRET` in the terminal that runs the command (never on the command line).
8. **Move your Finance data online (one time):** the shipped `wrangler.jsonc` has `CUORE_FINANCE_ORIGINE: "mac"`, which
   means the cloud waits for your data from the Mac, so Finance stays empty until you send it. First a dry run, no network:
   `node cloud/scripts/migra-finance.ts --database <your finance.sqlite3>` prints table counts and a fingerprint. Then
   create an Access service token `mac-migrazione` (put its Client ID in `ACCESS_SERVIZIO_MIGRAZIONE`, redeploy), export
   `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET`, and send with `--invia --url https://<your-worker>
   --conferma-impronta <first 12 digits of the fingerprint>`. The cloud refuses if it already has data or if schema or
   fingerprint differ: nothing partial. When it succeeds, delete the `mac-migrazione` token and remove its variable
   (`npm run configurazione` reminds you).

Free plan note: the app is designed around the daily row-read quota of Durable Objects; see "Cheap by construction" in
the main README before adding heavy queries.

## 6. Connect your bank

Full explanation in the main README (*Connect your own bank*). In short:

```bash
cd finance
node scripts/bank-sync.ts banche   --collegamenti <file.json> --paese <XX> --conferma-rete   # list supported banks
node scripts/bank-sync.ts consenso --collegamenti <file.json> --banca "<name>" --paese <XX> --conferma-rete
node scripts/bank-sync.ts sessione --collegamenti <file.json> --codice <code> --conferma-rete
node scripts/bank-sync.ts prova-su-copia --db <copy of your db> --collegamenti <file.json> --stato <state.json> --primo-avvio --conferma-rete
```
Then store the key and accounts as the Worker secrets `BANCA_CHIAVE` and `BANCA_COLLEGAMENTI` (format in
`cloud/src/banca.ts`). `--stato` is a small file where the script records its attempts (the first time it must not exist yet, hence
`--primo-avvio`; later runs omit that flag). Always run `prova-su-copia` first: it syncs a *copy* of your database and checks that nothing
else changed. To show the bank's name in the UI, add it to the `BANCHE` list in `app/index.html`.

## 7. Claude integration (optional)

Two MCP servers: `classeviva` (read-only school tools; registered by `setup.sh` in stage 3) and `cuore`
(`app/bridge/cuore_mcp.py`: search, events, and *proposed actions* that change nothing until you approve them).
Register them with your absolute paths, as `setup.sh` does for `classeviva`.

## If something goes wrong

- `npm run verifica` fails on a fresh clone → open an issue with the failing suite name.
- The Worker answers 503 to everything → Access values are still empty or invalid (`npm run configurazione` says which).
- The school job ends `AUTORIZZAZIONE` (exit code 3) → re-run the Classroom authorisation (stage 3, step 2).
- A bank consent expired → the app shows the days left; repeat `consenso` and `sessione`.

## Next: make it yours

See [EXTENDING.md](EXTENDING.md) for how to add a bank, a school source, a dashboard card, an automation or a Claude tool.
