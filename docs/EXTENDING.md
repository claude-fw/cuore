# Extending CUORE

CUORE is meant to be a base you grow, not a finished product. This page lists the extension points, in the order you are
most likely to need them. The ground rule for all of them: **write the test first with invented data, then run
`npm run verifica` from the repository root** (about 5 minutes). If it says `TUTTO OK`, you did not break the engines.

> The code, comments and test names are in Italian (the author's language); the contracts in `docs/it/` describe the data
> that flows between modules. Read the relevant contract before changing a feed.

## 1. Add a bank

*Easiest path: no code.* Follow *Connect your own bank* in the main README (Enable Banking, `finance/scripts/bank-sync.ts`).
To show a friendly name in the UI, add your bank to the `BANCHE` list in `app/index.html`
(`[accountId, "Bank name", "optional note"]`).

*Files instead of the API:* describe the bank's CSV/XLSX as a **declarative spec**, not code
(`finance/src/import/tabular/spec.ts`: header, date formats, decimal separator, sign of outflows, statuses to skip), register
it next to the existing ones in `finance/src/import/parsers/`, and add a test with an **anonymised copy** of a real export.
A format counts as supported only after it was verified against a real export (`finance/docs/formati-import.md`).

## 2. Change or add money rules, budgets, categories

Classification rules, budgets and goals are data, not code: see `finance/docs/` (`movimenti-manuali-e-budget.md`,
`dati-affidabili.md`, `crediti-e-debiti.md`). Every derived value must say where it comes from, and unknown stays `null`
(never a silent 0): keep that rule in new features.

## 3. Change the database (a new table or column)

1. Add `finance/src/db/migrations/009_<name>.ts` (the existing 001–008 are the template) and register it in
   `migrations/index.ts`. Tables are `STRICT`; history tables are append-only.
2. `cd finance && npm run migrations:generate` refreshes the schema snapshot; `npm run migrations:check` (part of
   `verifica`) fails if the snapshot or a checksum of an applied migration changed. **Never edit an applied migration:** add a
   new one.
3. The same migrations run on SQLite, Cloudflare D1 and the Durable Object SQL (`finance/docs/d1.md`).

## 4. Add a school source (another register, another LMS)

The school side is a pipeline: **client → Registry (append-only SQLite) → derived layers → feed → UI/MCP**.
Write a new client next to `scuola/mcp/classeviva/cvv_*` / `cls_*` that returns raw records, make it write into the
Registry the same way, and keep it **read-only and separate** (records from different sources are never silently merged).
The feed the UI reads is `cuore.school.feed/1` (`docs/it/contratto-school.md`): if you stay inside it, the UI works unchanged.
Credentials go in the macOS Keychain, never in files.

## 5. Add a card or a page to the dashboard

The whole UI is `app/index.html` (design tokens in the `:root` block, explained in `docs/it/design.md`). Rules that keep it
coherent: only `--*` variables for colours (define each new one for light **and** dark), one colour per area
(School / Finance / System), tabular numbers, every card opens a detail view. Put demo data for the new card in the demo block of that file so
the page works with no server. Screens have stable codes (see the comment above the screen definitions): keep new ones unique.

## 6. Add an automation (a scheduled job)

- *In the cloud:* add a job type in `cloud/src/automazioni.ts` (the queue guarantees one run per `type:date-hour` key,
  retries with backoff, and records the result), and a cron line in `cloud/wrangler.jsonc` if you need a new time (the
  Free plan allows a limited number of crons per account).
- *On the Mac:* a `launchd` agent like the school job (`scuola/mcp/classeviva/launchd/`), always installed by an explicit
  command, never silently.
- Make every job visible in *System → Automations* with its last result: a job nobody can see is a job nobody trusts.

## 7. Add a tool for Claude (MCP)

Servers live in `app/bridge/cuore_mcp.py` (`cuore`: search, events, proposed actions) and
`scuola/mcp/classeviva/server.py` (`classeviva`: read-only school tools). Add a tool function, describe it precisely, and
keep the pattern: **read tools return facts with their source; anything that changes data is a *proposed action***
(`app/bridge/cuore_actions.py`) that the owner approves in an interactive terminal before it runs.

## 8. Add a notification or a reminder

Push notifications and reminders are in `cloud/src/notifiche.ts`, `promemoria.ts` and `push.ts`; notifications are recorded
once per key and only sent when switched on. Reminders on the Mac: `app/bridge/promemoria.py`.

## Habits that keep it healthy

- **Tests with invented data only.** Never paste a real export, grade, circular or transaction into a test or a fixture: shift
  dates, change amounts and names. A copy with a letter changed is still recognisable.
- **Before publishing a fork**, compare every phrase and number of your fixtures with your own real data, not only with
  keyword searches, and recreate the git history from a single clean commit.
- **Update the docs with the code:** the matching contract in `docs/it/`, the module `README.md`, and
  [docs/SETUP.md](SETUP.md) if setup steps change.
- **Small steps:** one feature, one test, `npm run verifica` green, one commit.
