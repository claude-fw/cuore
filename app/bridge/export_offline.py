"""CUORE: copia offline ESPLICITA e ridotta di CUORE Money (`cuore.offline/1`), da aprire senza il ponte.

    python3 app/bridge/export_offline.py --db <database Finance> --out <file.html>

- Solo su comando: nessuna automazione, nessuna sincronizzazione. Il database SQLite NON viene copiato: la lettura passa
  da `cuore_money.build_feed` (immutable + readOnly, -wal non vuoto rifiutato, stato dei file confrontato prima/dopo) e
  nel file finisce solo un riassunto aggregato.
- Il file di uscita deve essere nuovo (mai sovrascritto), con permessi 0600, FUORI dal repository e fuori dalle cartelle
  sincronizzate automaticamente (iCloud Drive, CloudStorage, Dropbox): spostarlo su un altro dispositivo e' una scelta
  manuale del titolare.
- Contenuto: totali dei saldi osservati, entrate e spesa del mese, spesa per gruppo di budget e per categoria,
  obiettivi, conteggi degli acquisti pianificati, avvisi sui dati. Omessi (elencati nel file): movimenti, descrizioni,
  nomi e identificativi dei conti, crediti/debiti singoli, titoli degli acquisti, percorso del database, CUORE School.
- Ogni copia porta ora di creazione, ora della lettura, origine e stato; la pagina la mostra come COPIA OFFLINE, mai
  come dati aggiornati.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cuore_money  # noqa: E402

SCHEMA = "cuore.offline/1"
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
SYNCED = ("~/Library/Mobile Documents", "~/Library/CloudStorage", "~/Dropbox", "~/Google Drive")
OMESSI = [
    "movimenti singoli e loro descrizioni",
    "nomi e identificativi dei conti, saldi per conto",
    "crediti e debiti singoli (resta solo la posizione per valuta)",
    "titoli degli acquisti pianificati (restano i conteggi)",
    "percorso e contenuto del database Finance",
    "CUORE School (le pagine contengono dati scolastici personali: minimizzazione non ancora progettata)",
]


class ExportError(Exception):
    pass


def _inside(path, root):
    root = os.path.realpath(os.path.expanduser(root))
    return path == root or path.startswith(root + os.sep)


def check_out(out, repo=REPO, synced=SYNCED):
    """Percorso di uscita ammesso, altrimenti ExportError. Restituisce il percorso assoluto."""
    if not out.endswith(".html"):
        raise ExportError("il file di uscita deve terminare con .html")
    path = os.path.realpath(os.path.abspath(os.path.expanduser(out)))
    if _inside(path, repo):
        raise ExportError("uscita dentro il repository: non ammessa")
    for s in synced:
        if _inside(path, s):
            raise ExportError("uscita in una cartella sincronizzata automaticamente: non ammessa")
    if os.path.lexists(path):
        raise ExportError("il file di uscita esiste gia': nessuna sovrascrittura")
    if not os.path.isdir(os.path.dirname(path)):
        raise ExportError("la cartella di uscita non esiste")
    return path


def _ok(feed, op):
    e = (feed.get("operazioni") or {}).get(op) or {}
    return e.get("data") if e.get("ok") else None


def _plan(feed, kind):
    d = _ok(feed, "planning")
    return None if d is None else d.get(kind)


def reduce_feed(feed, now):
    """Payload offline: solo aggregati. `None` = non disponibile (errore della fonte), mai sostituito da zero."""
    bal, ov, sm, ds, ob = (_ok(feed, k) for k in ("balances", "overview", "summary", "data_status", "obligations"))
    goals, purchases, cats = _plan(feed, "goals"), _plan(feed, "purchases"), _ok(feed, "categories")
    money = {
        "oggi": feed.get("oggi"),
        "mese": (feed.get("periodo") or {}).get("mese"),
        "aggiornamento": feed.get("aggiornamento"),
        "saldi_totali": None if bal is None else [
            {k: t.get(k) for k in ("currency", "observedSumMinor", "complete", "allVerifiedAsOf")} for t in bal.get("totals", [])],
        "flussi_del_mese": None if ov is None else [
            {"currency": c["currency"], "entrateMinor": c["income"]["total"], "spesaRealeMinor": c["spending"]["net"],
             "daClassificare": c["unclassified"]["count"], "inAttesa": (c.get("pending") or {}).get("count")}
            for c in ov.get("currencies", [])],
        "budget": None if sm is None else [
            {k: c.get(k) for k in ("currency", "netSpending", "unclassifiedOut", "unclassifiedCount", "spendingByBudgetGroup",
                                   "budgetSharesBp", "spendingByCategory")} for c in sm.get("currencies", [])],
        "nomi_categorie": None if cats is None else {c["id"]: c["name"] for c in cats.get("items", [])},
        "obiettivi": None if goals is None else [
            {k: g.get(k) for k in ("name", "currency", "targetMinor", "allocatedMinor", "progressBp", "deadline", "status")}
            for g in goals],
        "acquisti": None if purchases is None else {s: sum(1 for p in purchases if p.get("status") == s)
                                                    for s in ("idea", "planned", "purchased", "cancelled")},
        "crediti_debiti": None if ob is None else ob.get("position"),
        "avvisi": None if ds is None else sorted({a.get("code") for a in ds.get("anomalies", []) if a.get("code")}),
    }
    stato = cuore_money.outcome(feed)                                   # completo | parziale | non_disponibile
    return {
        "schema": SCHEMA,
        "creato_il": now.isoformat(),
        "origine": {"dispositivo": "Mac del titolare (ponte CUORE locale)", "motore": feed.get("motore"),
                    "feed": feed.get("schema"), "letto_il": feed.get("generato")},
        "stato": {"money": stato, "school": "omesso"},
        "errori": [e.get("ambito") for e in feed.get("errori", [])],
        "omessi": OMESSI,
        "money": money,
    }


def render(payload):
    """Pagina unica: dati in un blocco JSON inerte (mai interpretato come HTML), testo inserito solo con textContent."""
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    with open(os.path.join(HERE, "offline_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    assert tpl.count("__CUORE_OFFLINE_JSON__") == 1
    return tpl.replace("__CUORE_OFFLINE_JSON__", data)


def export(db, out, now=None, build=cuore_money.build_feed):
    path = check_out(out)
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(ZoneInfo(cuore_money.ZONE)).replace(microsecond=0)
    feed = build(db)
    if not feed.get("sola_lettura_verificata"):
        raise ExportError("lettura non riuscita, nessun file scritto: " + "; ".join(e.get("ambito", "?") for e in feed.get("errori", [])))
    html = render(reduce_feed(feed, now)).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(html)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="database Finance (letto in sola lettura, mai copiato)")
    ap.add_argument("--out", required=True, help="nuovo file .html fuori dal repository e dalle cartelle sincronizzate")
    a = ap.parse_args(argv)
    try:
        path = export(a.db, a.out)
    except ExportError as e:
        print("copia offline non creata: %s" % e, file=sys.stderr)
        return 2
    print("copia offline creata (0600): %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
