"""Verifica in SOLA LETTURA di un giro reale del job School, dopo che e' avvenuto (mai un giro avviato o simulato da qui).

    python3 -B app/bridge/verifica_giro.py prima [--riferimento FILE]        # impronte e stato PRIMA del giro
    python3 -B app/bridge/verifica_giro.py dopo 20:00 [--riferimento FILE]   # il primo giro concluso dopo il riferimento

Legge: log JSONL del job, `state.json` e `last_summary.json`, calendario del LaunchAgent, feed School (copia in memoria del
Registry, file confrontati prima/dopo) e solo le IMPRONTE di Registry e database Finance. Scrive solo il file di
riferimento (predefinito nella cartella temporanea di sistema, mai accanto ai dati reali). Stampa esiti, conteggi,
categorie, orari e impronte: nessun dato scolastico, uid, chiave completa di problema o testo d'errore.
Il riepilogo e lo stato del job vengono riscritti a ogni giro: se appartengono a un giro successivo a quello esaminato,
il rapporto lo dichiara (`riepilogo_del_giro_esaminato: false`) invece di attribuirli al giro sbagliato.
Sostituisce lo script temporaneo `check_giro.py` della sessione del 24-25/10.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cuore_school as CS  # noqa: E402   (sys.path del motore e di _common)
import cuore_automation as AUTO  # noqa: E402
import registry as R  # noqa: E402
import school_job as J  # noqa: E402

DEFAULT_REF = os.path.join(tempfile.gettempdir(), "cuore-verifica-giro.json")
SAME_RUN_S = 120              # riepilogo e avvio del log distanti al massimo 2 minuti = stesso giro


def real_paths():
    import cuore_money
    state_dir = J.default_state_dir()
    return {"log": J.default_log_file(), "plist": os.path.expanduser(AUTO.PLIST_DEFAULT), "registry": R.default_path(),
            "state": os.path.join(state_dir, "state.json"), "summary": os.path.join(state_dir, "last_summary.json"),
            "finance_db": cuore_money.db_path()}


def _sha(p):
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return "assente"


def _json(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _utc(v):
    try:
        t = dt.datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc) if t is not None and t.tzinfo else None


def _local(t):
    return t.astimezone(AUTO.ZONE).strftime("%Y-%m-%d %H:%M:%S") if t else None


def fingerprints(paths):
    reg_dir = os.path.dirname(paths["registry"])
    out = {k: _sha(paths[k]) for k in ("registry", "state", "summary", "finance_db", "plist")}
    out["registry_accessori"] = sorted(f for f in (os.listdir(reg_dir) if os.path.isdir(reg_dir) else [])
                                       if f.startswith(os.path.basename(paths["registry"]) + "-"))
    return out


def problems_view(state):
    """Problemi aperti ridotti a tipo e fonte (le chiavi complete possono contenere uid)."""
    return [{"tipo": p["tipo"], "fonte": p["fonte"], "giri": p["giri"], "notificato": p["notificato"],
             "in_osservazione": p["in_osservazione"]} for p in CS.job_problems((state or {}).get("problemi"))]


def state_view(state):
    if state is None:
        return {"leggibile": False}
    u = state.get("ultimo") if isinstance(state.get("ultimo"), dict) else {}
    return {"leggibile": True, "chiavi": sorted(state), "notion_presente": "notion" in state,
            "ultimo": {k: u.get(k) for k in ("inizio", "fine", "modo", "esito", "exit_code")},
            "problemi": problems_view(state),
            "chiusure_in_attesa": len(state["risolti_in_attesa"]) if isinstance(state.get("risolti_in_attesa"), dict) else 0}


def _events(log_path):
    """Eventi del log nell'ordine dei file (come cuore_automation: anche i ruotati), righe illeggibili saltate."""
    log = AUTO.read_log(log_path)
    return [rec for _, rec in log["events"]]


def before(paths, now=None):
    ends = [e["ts"] for e in _events(paths["log"]) if e.get("evento") == "fine"]
    return {"registrato": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
            "ultimo_fine": ends[-1] if ends else None, "impronte": fingerprints(paths), "stato": state_view(_json(paths["state"]))}


def after(ref, paths, expected, now=None, feed_fn=None):
    """Rapporto sul PRIMO giro concluso dopo il riferimento. `feed_fn`: costruzione del feed School (predefinita:
    cuore_school.build_feed in sola lettura). Ritorna (codice, rapporto): 2 = nessun giro nuovo concluso."""
    now = now or dt.datetime.now(dt.timezone.utc)
    ev = _events(paths["log"])
    ref_end = _utc(ref.get("ultimo_fine"))
    ends = [i for i, e in enumerate(ev) if e.get("evento") == "fine" and (ref_end is None or (_utc(e.get("ts")) or now) > ref_end)]
    if not ends:
        return 2, {"giro": "nessun nuovo `fine` nel log dopo il riferimento", "riferimento": ref.get("ultimo_fine"),
                   "nota": "giro non ancora avvenuto, in corso, o non registrato: nessuna conclusione"}
    k = ends[0]
    start = next((i for i in range(k, -1, -1) if ev[i].get("evento") == "avvio"), None)
    fine, t0 = ev[k], _utc(ev[start]["ts"]) if start is not None else None
    t1 = _utc(fine.get("ts"))
    fonti = {}
    for e in ev[(start if start is not None else k):k]:
        if e.get("evento") == "fonte" and isinstance(e.get("kinds"), dict):
            cats = {}
            for v in e["kinds"].values():
                if isinstance(v, dict) and v.get("stato") not in J.OK_STATES:
                    c = "%s/%s" % (v.get("categoria"), v.get("motivo") or "-")
                    cats[c] = cats.get(c, 0) + 1
            fonti[e.get("fonte")] = {"esito": e.get("esito"), "tipi": len(e["kinds"]), "tipi_in_errore": cats}
    h = AUTO.history(paths["log"], paths["plist"], now)
    slot = next((o for o in h.get("orari") or [] if o["orario"][11:16] == expected and t0 is not None and o.get("avvio")
                 and _utc(o["avvio"]) is not None and abs((_utc(o["avvio"]) - t0).total_seconds()) < 60), None)
    report = {
        "1_orario": {"avvio": _local(t0), "fine": _local(t1), "orario_atteso": expected,
                     "stato_orario": slot and {x: slot.get(x) for x in ("stato", "esito", "in_ritardo", "ritardo_avvio_min")}},
        "2_esito": {"esito": fine.get("esito"), "exit_code": fine.get("exit_code"), "durata_lavoro_s": fine.get("durata_s"),
                    "durata_reale_s": fine.get("durata_reale_s"), "stop_durante_il_giro_s": fine.get("stop_durante_il_giro_s"),
                    "anomalie_categorie": sorted({str(a).split(":")[0] for a in fine.get("anomalie") or []}),
                    "codice_con_diagnostica_dell_automazione": all(x in fine for x in ("durata_reale_s", "stop_durante_il_giro_s"))},
        "3_fonti": fonti,
    }
    try:
        feed = (feed_fn or CS.build_feed)()
        import cuore_school_contract as K
        fr = feed.get("freschezza") or {}
        report["5_feed"] = {"esito": feed.get("esito"), "problemi_contratto": len(K.validate_feed(feed)),
                            "pagine": len(feed.get("pagine") or {}), "errori": [e.get("codice") for e in feed.get("errori") or []]}
        report["6_freschezza"] = {"stato": fr.get("stato"), "tipi_obsoleti": len(fr.get("tipi_obsoleti") or []),
                                  "per_fonte": {f: {"eta_ore_max": s.get("eta_ore_max"), "non_letti": len(s.get("non_letti") or [])}
                                                for f, s in (fr.get("per_fonte") or {}).items()}}
    except Exception as e:      # noqa: BLE001 - il feed non leggibile e' un risultato, non un crash della verifica
        report["5_feed"] = {"non_disponibile": type(e).__name__}
    summ, state = _json(paths["summary"]), _json(paths["state"])
    same = bool(summ and t0 and _utc(summ.get("inizio")) and abs((_utc(summ["inizio"]) - t0).total_seconds()) <= SAME_RUN_S)
    job = {"riepilogo_del_giro_esaminato": same,
           "nota": None if same else "riepilogo e stato appartengono a un giro successivo: le notifiche di questo giro non sono "
                                     "piu' osservabili (il job non le registra altrove)",
           "stato": state_view(state)}
    if summ:
        job["notifiche_del_riepilogo"] = [(n.get("titolo"), n.get("inviata")) for n in summ.get("notifiche") or []]
        job["problemi_in_osservazione"] = len(summ.get("problemi_in_osservazione") or [])
        job["automazione"] = {x: (summ.get("automazione") or {}).get(x) for x in
                              ("questo_giro", "stop_durante_il_giro_s", "giri_mancati_consecutivi")}
    report["8_job" if same else "8_job_di_un_giro_successivo"] = job
    fp = fingerprints(paths)
    report["7_impronte"] = {x: {"prima": ref["impronte"].get(x), "dopo": fp[x], "uguale": ref["impronte"].get(x) == fp[x]}
                            for x in fp}
    report["7_impronte"]["nota"] = ("registry, state e summary cambiano per le scritture del job; finance_db e plist devono "
                                    "restare uguali")
    return 0, report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verifica in sola lettura di un giro reale del job School.")
    ap.add_argument("modo", choices=("prima", "dopo"))
    ap.add_argument("orario", nargs="?", default="14:30", help="orario del calendario atteso (solo con 'dopo')")
    ap.add_argument("--riferimento", default=DEFAULT_REF)
    a = ap.parse_args(argv)
    paths = real_paths()
    if a.modo == "prima":
        ref = before(paths)
        fd = os.open(a.riferimento, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(ref, f, indent=1)
        print(json.dumps(ref, indent=1, ensure_ascii=False))
        return 0
    try:
        with open(a.riferimento, encoding="utf-8") as f:
            ref = json.load(f)
    except (OSError, ValueError):
        print("riferimento assente o illeggibile: eseguire prima `verifica_giro.py prima`", file=sys.stderr)
        return 64
    code, rep = after(ref, paths, a.orario)
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
