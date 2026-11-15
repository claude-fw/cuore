"""CUORE Money: feed di SOLA LETTURA del motore Cloud Finance per l'app CUORE (`cuore.money.feed/1`).

Esegue `cuore_money.ts` (consultazione v1 di Finance, invariata) in un processo Node e aggiunge provenienza ed errori.
Usato dal ponte (`serve.py`, `/api/money`). Tutte le sezioni vengono dalla consultazione v1, comprese `categories`
(nomi e gruppi di budget) e `planning` (obiettivi e acquisti): ogni operazione ha la sua busta e il suo esito.
Contratto dei dati per la dashboard: `docs/it/contratto-money.md`.

Confine di sola lettura (verificato a ogni lettura, altrimenti nessun dato):
- SQLite: `readImmutable` del motore Finance copia un'istantanea coerente in memoria (scritture rifiutate, nessun
  lock, nessun -wal/-shm creato; copia scartata se il file cambia mentre viene letta).
  Il modello dei permessi di Node 24 NON vincola `node:sqlite` (verificato): resta solo come difesa aggiuntiva
  (niente scritture via fs, niente processi figli, worker o addon).
- `immutable=1` ignora un -wal: se esiste un -wal NON vuoto (dati non ancora riportati nel file, database forse in uso)
  la lettura viene rifiutata.
- Stato dei file (database, -wal, -shm, -journal) confrontato prima/dopo: se cambia, il feed e' scartato.
- Nessun file scritto da questo modulo, nessun log; ambiente del processo Node ridotto (nessuna variabile ereditata).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
FINANCE = os.path.normpath(os.path.join(HERE, "..", "..", "finance"))
SCRIPT = os.path.join(HERE, "cuore_money.ts")
SCHEMA = "cuore.money.feed/1"
DB_ENV = "CUORE_MONEY_DB"
DB_DEFAULT = "~/Library/Application Support/Cloud/finance/db/cloud-finance.sqlite3"
NODE_MIN_MAJOR = 24                      # node:sqlite e TypeScript eseguito direttamente, come il motore Finance
TIMEOUT_S = 60
ZONE = "Europe/Rome"


def db_path():
    return os.path.expanduser(os.environ.get(DB_ENV, DB_DEFAULT))


def files_state(path):
    out = {}
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            st = os.stat(p)
            out[p] = {"esiste": True, "byte": st.st_size, "mtime_ns": st.st_mtime_ns}
        except OSError:
            out[p] = {"esiste": False}
    return out


def node_command(node, path, today):
    """Comando Node con il modello dei permessi: lettura solo del codice e del database; niente scritture via fs,
    processi figli, worker o addon. (Non vincola node:sqlite: la sola lettura la garantisce l'apertura immutable.)"""
    side = ["--allow-fs-read=%s%s" % (path, x) for x in ("-wal", "-shm", "-journal")]     # solo per controllarne lo stato
    return [node, "--permission", "--allow-fs-read=%s" % FINANCE, "--allow-fs-read=%s" % HERE,
            "--allow-fs-read=%s" % path] + side + [SCRIPT, "--db", path, "--today", today]


def _node():
    node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
    if not node:
        return None, "Node non trovato"
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, env={}).stdout.strip()
        major = int(v.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, "versione di Node non leggibile: %s" % type(e).__name__
    if major < NODE_MIN_MAJOR:
        return None, "Node %s: serve almeno la %d" % (v, NODE_MIN_MAJOR)
    return node, None


# ------------------------------------------------------------------------------------------------ contratto
def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_date(v):
    return isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-"


def _need(problems, cond, where):
    if not cond:
        problems.append(where)


def _list_of(problems, v, where, check):
    if not isinstance(v, list):
        problems.append(where + ": lista attesa")
        return
    for i, x in enumerate(v):
        if not isinstance(x, dict):
            problems.append("%s[%d]: oggetto atteso" % (where, i))
        else:
            check(x, "%s[%d]" % (where, i))


def validate(op, data):
    """Problemi strutturali dei dati di UN'operazione riuscita (vuoto = conforme). Importi: interi in unita' minime;
    null ammesso dove il contratto v1 lo prevede (saldo non osservato). Campi oltre il contratto minimo = problema."""
    p = []
    if op == "accounts":
        def acc(a, w):
            _need(p, isinstance(a.get("id"), str) and isinstance(a.get("name"), str), w + ".id/name")
            _need(p, isinstance(a.get("currency"), str) and isinstance(a.get("closed"), bool), w + ".currency/closed")
            _need(p, "profile" not in a, w + ": profilo di titolarita' non ammesso")
        _list_of(p, data, "accounts", acc)
    elif op == "overview":
        if not isinstance(data, dict):
            return ["overview: oggetto atteso"]
        _need(p, _is_date(data.get("from")) and _is_date(data.get("to")), "overview.from/to")

        def cur(c, w):
            _need(p, isinstance(c.get("currency"), str), w + ".currency")
            for grp, key in (("income", "total"), ("spending", "net"), ("spending", "purchases"), ("unclassified", "count")):
                _need(p, _is_int((c.get(grp) or {}).get(key)), "%s.%s.%s" % (w, grp, key))
        _list_of(p, data.get("currencies"), "overview.currencies", cur)

        def tot(t, w):
            _need(p, isinstance(t.get("currency"), str) and _is_int(t.get("observedSumMinor")), w)
            _need(p, isinstance(t.get("complete"), bool), w + ".complete")
        _list_of(p, (data.get("money") or {}).get("totals"), "overview.money.totals", tot)
    elif op == "balances":
        if not isinstance(data, dict):
            return ["balances: oggetto atteso"]

        def bal(b, w):
            _need(p, isinstance(b.get("accountId"), str) and isinstance(b.get("currency"), str), w + ".accountId/currency")
            _need(p, b.get("status") in ("observed", "missing", "conflict"), w + ".status")
            _need(p, b.get("balanceMinor") is None or _is_int(b.get("balanceMinor")), w + ".balanceMinor")
            _need(p, b.get("observedOn") is None or _is_date(b.get("observedOn")), w + ".observedOn")
            _need(p, isinstance(b.get("verifiedAsOf"), bool), w + ".verifiedAsOf")
        _list_of(p, data.get("accounts"), "balances.accounts", bal)
        _list_of(p, data.get("totals"), "balances.totals",
                 lambda t, w: _need(p, isinstance(t.get("complete"), bool) and _is_int(t.get("observedSumMinor")), w))
    elif op == "data_status":
        if not isinstance(data, dict):
            return ["data_status: oggetto atteso"]
        _list_of(p, data.get("accounts"), "data_status.accounts",
                 lambda a, w: _need(p, isinstance(a.get("accountId"), str), w + ".accountId"))
        _list_of(p, data.get("anomalies"), "data_status.anomalies",
                 lambda a, w: _need(p, isinstance(a.get("code"), str) and a.get("severity") in ("info", "warning", "error"),
                                    w + ".code/severity"))
    elif op == "obligations":
        if not isinstance(data, dict):
            return ["obligations: oggetto atteso"]

        def obl(o, w):
            _need(p, o.get("direction") in ("receivable", "payable"), w + ".direction")
            _need(p, _is_int(o.get("residualMinor")) and isinstance(o.get("currency"), str), w + ".residualMinor/currency")
            _need(p, set(o) <= {"direction", "currency", "dueOn", "daysToDue", "status", "overdue", "residualMinor"},
                  w + ": campi oltre il contratto minimo")
        _list_of(p, data.get("items"), "obligations.items", obl)
        _need(p, isinstance(data.get("truncated"), bool), "obligations.truncated")
    elif op == "summary":
        if not isinstance(data, dict):
            return ["summary: oggetto atteso"]
        _need(p, _is_date(data.get("from")) and _is_date(data.get("to")), "summary.from/to")
        _need(p, _is_int(data.get("openReviewItems")), "summary.openReviewItems")

        def amounts(v, w, keys=None):
            ok = isinstance(v, dict) and all(isinstance(k, str) and _is_int(x) for k, x in v.items())
            _need(p, ok and (keys is None or set(v) == keys), w)

        def sc(c, w):
            _need(p, isinstance(c.get("currency"), str), w + ".currency")
            for k in ("netSpending", "unclassifiedOut", "unclassifiedCount", "pendingCount"):
                _need(p, _is_int(c.get(k)), "%s.%s" % (w, k))
            amounts(c.get("spendingByCategory"), w + ".spendingByCategory")
            amounts(c.get("spendingByBudgetGroup"), w + ".spendingByBudgetGroup", BUDGET_GROUPS)
            amounts(c.get("budgetSharesBp"), w + ".budgetSharesBp", BUDGET_GROUPS)
            _need(p, set(c) <= SUMMARY_FIELDS, w + ": campi oltre il contratto minimo")
        _list_of(p, data.get("currencies"), "summary.currencies", sc)
    elif op == "categories":
        if not isinstance(data, dict):
            return ["categories: oggetto atteso"]

        def cat(c, w):
            _need(p, set(c) == {"id", "name", "parentId", "budgetGroup"}, w + ": campi diversi da quelli attesi")
            _need(p, isinstance(c.get("id"), str) and isinstance(c.get("name"), str), w + ".id/name")
            _need(p, c.get("budgetGroup") in ("needs", "wants", "savings", None), w + ".budgetGroup")
        _list_of(p, data.get("items"), "categories.items", cat)
    elif op == "planning":
        if not isinstance(data, dict):
            return ["planning: oggetto atteso"]

        def goal(g, w):
            _need(p, set(g) == GOAL_FIELDS, w + ": campi diversi da quelli attesi")
            _need(p, isinstance(g.get("name"), str) and isinstance(g.get("currency"), str), w + ".name/currency")
            _need(p, all(_is_int(g.get(k)) for k in ("targetMinor", "allocatedMinor", "remainingMinor", "progressBp")), w + ": importi")
            _need(p, g.get("deadline") is None or _is_date(g.get("deadline")), w + ".deadline")
            _need(p, g.get("status") in ("active", "paused", "reached", "cancelled") and isinstance(g.get("reached"), bool),
                  w + ".status/reached")

        def purchase(x, w):
            _need(p, set(x) == PURCHASE_FIELDS, w + ": campi diversi da quelli attesi")
            _need(p, isinstance(x.get("title"), str) and isinstance(x.get("currency"), str), w + ".title/currency")
            _need(p, x.get("estimatedMinor") is None or _is_int(x.get("estimatedMinor")), w + ".estimatedMinor")
            _need(p, x.get("priority") is None or _is_int(x.get("priority")), w + ".priority")
            _need(p, x.get("status") in ("idea", "planned", "purchased", "cancelled"), w + ".status")
        _list_of(p, data.get("goals"), "planning.goals", goal)
        _list_of(p, data.get("purchases"), "planning.purchases", purchase)
    elif op == "transactions":
        if not isinstance(data, dict):
            return ["transactions: oggetto atteso"]

        def tx(t, w):
            _need(p, _is_date(t.get("date")) and _is_int(t.get("amountMinor")) and isinstance(t.get("currency"), str), w)
            _need(p, set(t) <= {"id", "date", "status", "accountId", "amountMinor", "currency", "description", "kind",
                                "categoryId", "excluded"}, w + ": campi oltre il contratto minimo")
        _list_of(p, data.get("items"), "transactions.items", tx)
        _need(p, isinstance(data.get("truncated"), bool), "transactions.truncated")
    elif op == "manual":
        if not isinstance(data, dict):
            return ["manual: oggetto atteso"]

        def man(m, w):
            _need(p, set(m) == MANUAL_FIELDS, w + ": campi diversi da quelli attesi")
            _need(p, isinstance(m.get("id"), str) and _is_date(m.get("transactionDate")) and _is_int(m.get("amountMinor")), w)
            _need(p, isinstance(m.get("voided"), bool) and (m.get("voidedAt") is None) == (not m.get("voided")), w + ".voided")
        _list_of(p, data.get("items"), "manual.items", man)
        _need(p, isinstance(data.get("truncated"), bool), "manual.truncated")
    elif op == "budgets":
        if not isinstance(data, dict):
            return ["budgets: oggetto atteso"]

        def line(x, w):
            _need(p, set(x) == BUDGET_LINE_FIELDS, w + ": campi diversi da quelli attesi")
            _need(p, x.get("scope") in ("category", "budget_group", "total"), w + ".scope")
            _need(p, all(_is_int(x.get(k)) for k in ("budgetMinor", "spentMinor", "remainingMinor")), w + ": importi")
            _need(p, isinstance(x.get("overBudget"), bool), w + ".overBudget")

        def bcur(c, w):
            _need(p, isinstance(c.get("currency"), str) and c.get("month") == data.get("month"), w + ".currency/month")
            _need(p, all(_is_int(c.get(k)) for k in ("unclassifiedOutflowMinor", "pendingOutflowMinor", "refundsMinor")),
                  w + ": importi a parte")
            _list_of(p, c.get("lines"), w + ".lines", line)
        _need(p, isinstance(data.get("month"), str) and len(data["month"]) == 7, "budgets.month")
        _list_of(p, data.get("currencies"), "budgets.currencies", bcur)
    elif op == "trend":
        if not isinstance(data, dict):
            return ["trend: oggetto atteso"]

        def month(m, w):
            ok = isinstance(m.get("month"), str) and len(m["month"]) == 7 and _is_date(m.get("from")) and _is_date(m.get("to"))
            _need(p, ok and m["from"].startswith(m["month"]) and m["from"] <= m["to"], w + ".month/from/to")

            def tc(c, w2):
                _need(p, isinstance(c.get("currency"), str), w2 + ".currency")
                for k in ("income", "netSpending", "unclassifiedOut", "unclassifiedCount", "pendingCount"):
                    _need(p, _is_int(c.get(k)), "%s.%s" % (w2, k))
                v = c.get("spendingByCategory")
                _need(p, isinstance(v, dict) and all(isinstance(k, str) and _is_int(x) for k, x in v.items()),
                      w2 + ".spendingByCategory")
                _need(p, set(c) <= TREND_FIELDS, w2 + ": campi oltre il contratto minimo")
            _list_of(p, m.get("currencies"), w + ".currencies", tc)
        _list_of(p, data.get("months"), "trend.months", month)
        months = [m.get("month") for m in data.get("months") or [] if isinstance(m, dict)]
        _need(p, months == sorted(set(months)), "trend.months: mesi ripetuti o non in ordine")
    elif op == "daily":
        if not isinstance(data, dict):
            return ["daily: oggetto atteso"]
        _need(p, _is_date(data.get("from")) and _is_date(data.get("to")), "daily.from/to")

        def day(x, w):
            _need(p, _is_date(x.get("date")) and set(x) == {"date", "currencies"}, w + ".date")

            def dc(c, w2):
                _need(p, set(c) == DAILY_FIELDS and isinstance(c.get("currency"), str), w2 + ": campi diversi da quelli attesi")
                _need(p, all(_is_int(c.get(k)) for k in DAILY_FIELDS - {"currency"}), w2 + ": importi")
            _list_of(p, x.get("currencies"), w + ".currencies", dc)
        _list_of(p, data.get("days"), "daily.days", day)
        dates = [x.get("date") for x in data.get("days") or [] if isinstance(x, dict)]
        _need(p, dates == sorted(set(dates)), "daily.days: giorni ripetuti o non in ordine")
    else:
        p.append("operazione non prevista dal contratto: %s" % op)
    return p


BUDGET_GROUPS = {"needs", "wants", "savings", "unassigned"}
SUMMARY_FIELDS = {"currency", "netSpending", "unclassifiedOut", "unclassifiedCount", "pendingCount", "spendingByCategory",
                  "spendingByBudgetGroup", "budgetSharesBp"}
EXPECTED_OPS = ("accounts", "overview", "summary", "categories", "planning", "balances", "data_status", "obligations",
                "transactions", "trend", "manual", "budgets", "daily")
DAILY_FIELDS = {"currency", "income", "netSpending", "unclassifiedOut", "pendingCount"}
MANUAL_FIELDS = {"id", "accountId", "transactionDate", "bookingDate", "status", "amountMinor", "currency", "description",
                 "voided", "voidedAt"}
BUDGET_LINE_FIELDS = {"scope", "target", "budgetMinor", "spentMinor", "remainingMinor", "overBudget", "budgetId"}
TREND_FIELDS = {"currency", "income", "netSpending", "unclassifiedOut", "unclassifiedCount", "pendingCount",
                "spendingByCategory"}
GOAL_FIELDS = {"id", "name", "currency", "targetMinor", "deadline", "status", "allocatedMinor", "remainingMinor",
               "progressBp", "reached"}
PURCHASE_FIELDS = {"id", "title", "currency", "estimatedMinor", "priority", "status", "goalId"}

def _freshness(feed):
    """Aggiornamento dei dati (non della lettura): ultimo movimento e ultima importazione riuscita, avvisi di dati vecchi."""
    ds = (feed["operazioni"].get("data_status") or {})
    accts = ds.get("data", {}).get("accounts", []) if ds.get("ok") else []
    dates = [a.get("lastBookedOn") for a in accts if _is_date(a.get("lastBookedOn"))]
    imports = [a.get("lastImportAt") for a in accts if isinstance(a.get("lastImportAt"), str)]
    stale = [a.get("code") for a in (ds.get("data", {}).get("anomalies", []) if ds.get("ok") else [])
             if str(a.get("code", "")).startswith("stale")]
    return {"letto_il": feed["generato"], "ultimo_movimento": max(dates) if dates else None,
            "ultima_importazione": max(imports) if imports else None, "avvisi_dati_vecchi": len(stale),
            "stato": "non_disponibile" if not ds.get("ok") else "dati_vecchi" if stale else "ok" if dates else "nessun_dato"}


def outcome(feed):
    """Esito complessivo: `completo` (tutto letto), `parziale` (lettura verificata, alcune sezioni non disponibili),
    `non_disponibile` (nessun dato utilizzabile). Unica definizione, usata anche dalla copia offline."""
    if not feed.get("sola_lettura_verificata") or not any(e.get("ok") for e in feed.get("operazioni", {}).values()):
        return "non_disponibile"
    return "parziale" if feed.get("errori") else "completo"


def build_feed(path=None, today=None, run=subprocess.run):
    """Feed di CUORE Money. Mai eccezioni: gli errori finiscono in `errori` e i dati restano assenti."""
    from zoneinfo import ZoneInfo
    path = os.path.abspath(path or db_path())
    now = dt.datetime.now(ZoneInfo(ZONE)).replace(microsecond=0)
    today = today or now.date().isoformat()
    feed = {"schema": SCHEMA, "fonte": "reale", "motore": "Cloud Finance · consultazione v1", "generato": now.isoformat(),
            "oggi": today, "operazioni": {}, "errori": [], "sola_lettura_verificata": False,
            "scritture": {"database": False, "banche": False}, "esito": "non_disponibile"}
    if not os.path.isfile(path):
        feed["errori"].append({"ambito": "database", "messaggio": "database Finance assente"})
        return feed
    wal = path + "-wal"
    if os.path.isfile(wal) and os.path.getsize(wal) > 0:
        feed["errori"].append({"ambito": "database", "messaggio": "-wal non vuoto: dati non ancora consolidati o database "
                                                                 "in uso (es. importazione in corso); nessuna lettura"})
        return feed
    node, err = _node()
    if err:
        feed["errori"].append({"ambito": "node", "messaggio": err})
        return feed
    before = files_state(path)
    try:
        r = run(node_command(node, path, today), capture_output=True, text=True, timeout=TIMEOUT_S, cwd=HERE,
                env={"LANG": "C.UTF-8", "TZ": ZONE})
    except subprocess.TimeoutExpired:
        r = None
        feed["errori"].append({"ambito": "lettura", "messaggio": "tempo massimo superato (%d s)" % TIMEOUT_S})
    except OSError as e:
        r = None
        feed["errori"].append({"ambito": "lettura", "messaggio": "Node non avviabile: %s" % type(e).__name__})
    changed = before != files_state(path)
    if changed:                                                    # confine violato: nessun dato esce
        feed["errori"].append({"ambito": "sola_lettura", "messaggio": "un file del database e' cambiato durante la lettura"})
        return feed
    if r is None:
        return feed
    if r.returncode != 0:
        feed["errori"].append({"ambito": "lettura", "messaggio": "uscita %d: %s" % (r.returncode, (r.stderr or "").strip()[:200])})
        return feed
    try:
        out = json.loads(r.stdout)
    except ValueError:
        feed["errori"].append({"ambito": "lettura", "messaggio": "risposta non JSON"})
        return feed
    if out.get("schema") != "cuore.money.query/1":
        feed["errori"].append({"ambito": "lettura", "messaggio": "risposta di schema inatteso"})
        return feed
    feed["sola_lettura_verificata"] = True
    feed["periodo"] = out.get("periodo")
    ops = out.get("operazioni") or {}
    if set(ops) != set(EXPECTED_OPS):
        feed["errori"].append({"ambito": "contratto", "messaggio": "operazioni diverse da quelle previste"})
        return feed
    for op in EXPECTED_OPS:
        env = ops[op]
        if not isinstance(env, dict) or env.get("contractVersion") != 1:
            env = {"ok": False, "error": {"code": "contratto_non_valido", "message": "busta della consultazione non riconosciuta"}}
        elif env.get("ok"):
            problems = validate(op, env.get("data"))
            if problems:                                            # dati non conformi: mai mostrati come reali
                env = {"ok": False, "contractVersion": 1, "operation": op,
                       "error": {"code": "contratto_non_valido", "message": "; ".join(problems[:5])}}
        feed["operazioni"][op] = env
        if not env.get("ok"):
            e = env.get("error") or {}
            feed["errori"].append({"ambito": op, "codice": e.get("code"), "messaggio": e.get("message")})
    feed["aggiornamento"] = _freshness(feed)
    feed["esito"] = outcome(feed)
    return feed


def summary(feed):
    """Solo esiti e conteggi (nessun importo, descrizione o nome): per controlli da terminale."""
    ops = {}
    for op, env in feed["operazioni"].items():
        d = env.get("data")
        n = len(d["items"]) if op == "transactions" and env.get("ok") else (len(d) if isinstance(d, list) else None)
        ops[op] = {"ok": env.get("ok"), "elementi": n}
    plan = (feed["operazioni"].get("planning") or {})
    if plan.get("ok"):
        ops["planning"]["elementi"] = {k: len(plan["data"][k]) for k in ("goals", "purchases")}
    return {"fonte": feed["fonte"], "sola_lettura_verificata": feed["sola_lettura_verificata"], "errori": feed["errori"],
            "oggi": feed["oggi"], "operazioni": ops}


if __name__ == "__main__":
    # Percorso SEMPRE esplicito da terminale: nessuna lettura del database reale per default.
    if len(sys.argv) != 3 or sys.argv[1] != "--db":
        print("uso: python3 cuore_money.py --db <database.sqlite3>   (stampa solo esiti e conteggi)", file=sys.stderr)
        sys.exit(64)
    print(json.dumps(summary(build_feed(sys.argv[2])), ensure_ascii=False, indent=1))
