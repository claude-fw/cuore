"""CUORE School: osservabilita' dell'automazione (job scuola) in SOLA LETTURA, da log JSONL e calendario del LaunchAgent.
Unica definizione, usata dal ponte (`app/bridge/cuore_school.py`, storico in dashboard) e dal job (`school_job.py`,
giri mancati nel riepilogo e nelle notifiche): per questo vive in `scuola/mcp/_common/` (dal 2026-10-25, prima in app/bridge).

Cosa fa: ricostruisce gli ultimi giri del job (`school-job.jsonl` e i file ruotati `.1`..`.5`) e confronta gli orari del
calendario (`StartCalendarInterval` del plist, ora locale) con gli avvii registrati nelle ultime 48 ore.

Regole (mai un esito inventato):
- un giro e' la sequenza `avvio` ... `fine` (o `interrotto`); un `avvio` senza conclusione e' «in corso» se recente,
  altrimenti «incompleto»; un giro OCCUPATO (lock) chiude l'`avvio` piu' recente;
- un orario e' «eseguito» se un avvio cade tra l'orario (-2 min) e l'orario successivo; «in ritardo» se l'avvio arriva
  oltre 15 minuti dopo (es. Mac in stop: launchd esegue al risveglio, accorpando gli orari persi);
- «mancato» SOLO se il log copre tutto l'intervallo senza righe illeggibili; altrimenti «non verificabile» (log assente,
  ruotato oltre l'intervallo, righe corrotte o troncate). Un giro mancato puo' dipendere da Mac spento o in stop:
  questo modulo non lo distingue;
- nessun testo di errore, uid, percorso personale o nome di lettura esce da qui: solo esiti a vocabolario fisso, conteggi,
  orari, durate e la categoria delle anomalie (la parte prima di ':').
Il caricamento effettivo dell'agente in launchd non e' verificabile senza `launchctl`: e' dichiarato come tale.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Rome")
LOG_DEFAULT = "~/Library/Logs/Cloud/school-job.jsonl"
PLIST_DEFAULT = "~/Library/LaunchAgents/cloud.scuola.job.plist"
ROTATED = 5                      # RotatingFileHandler del job: backupCount=5
WINDOW_H = 48
LAST_RUNS = 10
EARLY_MIN = 2                    # avvio fino a 2 minuti prima dell'orario (orologio) ancora attribuito all'orario
LATE_START_MIN = 15              # oltre: avvio in ritardo
LONG_RUN_MIN = 30                # oltre: conclusione molto dopo l'avvio (tempo reale)
GRACE_MIN = 10                   # orario appena passato senza avvio: «in attesa», non ancora mancato
RUNNING_MAX_H = 3                # avvio senza conclusione da piu' di 3 ore: «incompleto»
ESITI = {"OK": "ok", "PARZIALE": "parziale", "FALLITO": "fallito", "AUTORIZZAZIONE": "autorizzazione",
         "OCCUPATO": "occupato"}
ESITI_FONTE = {"ok", "parziale", "fallita", "fallito", "saltata", "autorizzazione", "non_eseguita", "errore"}
FONTI = {"classeviva", "classroom"}


def _ts(value):
    """ISO con fuso -> datetime UTC; None se non interpretabile (mai un orario inventato)."""
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc) if t.tzinfo else None


def _local(t):
    return t.astimezone(ZONE).replace(microsecond=0).isoformat() if t else None


# ------------------------------------------------------------------------------------------------ log
def read_log(path):
    """Righe dei file di log, dal piu' vecchio al piu' recente. Righe illeggibili: contate e collocate fra gli orari
    validi vicini (intervallo in cui potrebbero nascondere un evento)."""
    files = [p for p in ["%s.%d" % (path, i) for i in range(ROTATED, 0, -1)] + [path] if os.path.isfile(p)]
    events, bad, prev = [], [], None
    pending = bad_lines = 0
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            bad.append((prev, None))
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                t = _ts(rec.get("ts")) if isinstance(rec, dict) else None
            except ValueError:
                t = None
            if t is None or not isinstance(rec.get("evento"), str):
                pending += 1
                bad_lines += 1
                continue
            if pending:
                bad.append((prev, t))
                pending = 0
            events.append((t, rec))
            prev = t
    if pending:
        bad.append((prev, None))                                  # righe illeggibili in coda (es. scrittura troncata)
    events.sort(key=lambda e: e[0])
    return {"files": len(files), "events": events, "bad": bad, "bad_lines": bad_lines}


def _source_outcomes(rec):
    out = {}
    f, e = rec.get("fonte"), rec.get("esito")
    if f in FONTI:
        out[f] = e if e in ESITI_FONTE else "sconosciuto"
    return out


def build_runs(events, now):
    """Giri ricostruiti dagli eventi. Ogni giro: avvio, fine, esito (vocabolario fisso), durate, fonti, tentativi,
    categorie di anomalie. Nessun testo libero."""
    runs, open_runs = [], []
    for t, rec in events:
        ev = rec["evento"]
        if ev == "avvio":
            r = {"avvio": t, "fine": None, "esito": None, "exit_code": None, "durata_s": None, "fonti": {},
                 "tentativi": 0, "anomalie": [], "oltre_soglia": None,
                 "modo": rec.get("modo") if rec.get("modo") in ("commit", "dry-run") else None}
            runs.append(r)
            open_runs.append(r)
        elif not open_runs:
            continue                                              # evento di un giro iniziato in un log ruotato via
        elif ev in ("fine", "interrotto"):
            r = open_runs.pop()                                   # LIFO: un giro OCCUPATO chiude il proprio avvio
            r["fine"] = t
            if ev == "interrotto":
                r["esito"] = "interrotto"
            else:
                r["esito"] = ESITI.get(rec.get("esito"), "sconosciuto")
                r["exit_code"] = rec.get("exit_code") if isinstance(rec.get("exit_code"), int) else None
                d = rec.get("durata_s")
                r["durata_s"] = float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) else None
                an = rec.get("anomalie") if isinstance(rec.get("anomalie"), list) else []
                r["anomalie"] = sorted({str(a).split(":")[0][:40] for a in an if isinstance(a, str)})
                ol = rec.get("oltre_soglia")
                r["oltre_soglia"] = len(ol) if isinstance(ol, list) else None
        else:
            r = open_runs[-1]
            if ev == "retry":
                r["tentativi"] += 1
            elif ev == "fonte":
                r["fonti"].update(_source_outcomes(rec))
    for r in runs:
        if r["esito"] is None:
            r["esito"] = "in_corso" if now - r["avvio"] <= dt.timedelta(hours=RUNNING_MAX_H) else "incompleto"
    return runs


# ------------------------------------------------------------------------------------------------ calendario
def read_schedule(path):
    """Orari (ora, minuto) del LaunchAgent. Stato: ok | assente | non_valido | non_supportato | disattivato."""
    if not os.path.isfile(path):
        return {"stato": "assente", "orari": [], "all_avvio": None}
    try:
        with open(path, "rb") as fh:
            pl = plistlib.load(fh)
    except Exception:  # noqa: BLE001 - plist illeggibile: dichiarato, mai interpretato a meta'
        return {"stato": "non_valido", "orari": [], "all_avvio": None}
    if not isinstance(pl, dict):
        return {"stato": "non_valido", "orari": [], "all_avvio": None}
    run_at_load = pl.get("RunAtLoad") is True
    if pl.get("Disabled") is True:
        return {"stato": "disattivato", "orari": [], "all_avvio": run_at_load}
    sci = pl.get("StartInterval")
    if sci is not None and "StartCalendarInterval" not in pl:   # `check` (14/11): nessun orario fisso, giro se serve
        ok = isinstance(sci, int) and not isinstance(sci, bool) and sci > 0
        return ({"stato": "ok", "orari": [], "all_avvio": run_at_load, "ogni_s": sci} if ok
                else {"stato": "non_valido", "orari": [], "all_avvio": run_at_load})
    sci = pl.get("StartCalendarInterval")
    entries = sci if isinstance(sci, list) else [sci] if isinstance(sci, dict) else []
    times = []
    for e in entries:
        ok = (isinstance(e, dict) and set(e) == {"Hour", "Minute"} and isinstance(e.get("Hour"), int)
              and isinstance(e.get("Minute"), int) and 0 <= e["Hour"] <= 23 and 0 <= e["Minute"] <= 59)
        if not ok:                                                # giorni, mesi o campi jolly: non interpretati
            return {"stato": "non_supportato", "orari": [], "all_avvio": run_at_load}
        times.append((e["Hour"], e["Minute"]))
    if not times:
        return {"stato": "non_supportato", "orari": [], "all_avvio": run_at_load}
    return {"stato": "ok", "orari": sorted(set(times)), "all_avvio": run_at_load}


def slots_between(times, start, end):
    """Istanti UTC degli orari locali fra start ed end. Ora legale: l'orario locale e' costruito con il fuso del
    giorno; un orario inesistente (salto in avanti) viene spostato come fa l'orologio."""
    out = []
    day = start.astimezone(ZONE).date() - dt.timedelta(days=1)
    last = end.astimezone(ZONE).date() + dt.timedelta(days=1)
    while day <= last:
        for h, m in times:
            local = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ZONE)
            t = local.astimezone(dt.timezone.utc)
            if start <= t <= end:
                out.append(t)
        day += dt.timedelta(days=1)
    return sorted(set(out))


# ------------------------------------------------------------------------------------------------ classificazione
def assign(runs, schedule, now):
    """Per ogni orario (dal primo avvio registrato a ora + 2 giorni): il PRIMO giro non OCCUPATO avviato tra l'orario
    (-2 min) e l'orario successivo (-2 min). Gli altri giri sono «fuori calendario» (es. all'accesso, a mano)."""
    if schedule["stato"] != "ok":
        return [], {}
    first = min([r["avvio"] for r in runs] + [now]) - dt.timedelta(days=2)
    slots = slots_between(schedule["orari"], first, now + dt.timedelta(days=2))
    primary = {}
    for i, s in enumerate(slots):
        lo = s - dt.timedelta(minutes=EARLY_MIN)
        hi = (slots[i + 1] if i + 1 < len(slots) else s + dt.timedelta(days=1)) - dt.timedelta(minutes=EARLY_MIN)
        cand = [r for r in runs if lo <= r["avvio"] < hi and r["esito"] != "occupato"]
        if cand:
            primary[s] = cand[0]
    return slots, primary


def classify(runs, log, schedule, now, window_h=WINDOW_H):
    """Stato di ogni orario delle ultime `window_h` ore e prossimo giro previsto."""
    slots, primary = assign(runs, schedule, now)
    if not slots:
        return [], None, {}
    start = now - dt.timedelta(hours=window_h)
    nxt = next((s for s in slots if s > now), None)
    covered_from = log["events"][0][0] if log["events"] else None
    out = []
    for i, s in enumerate(slots):
        if not start <= s <= now:
            continue
        lo = s - dt.timedelta(minutes=EARLY_MIN)
        end = (slots[i + 1] if i + 1 < len(slots) else s + dt.timedelta(days=1)) - dt.timedelta(minutes=EARLY_MIN)
        row = {"orario": _local(s)}
        r = primary.get(s)
        if r:
            delay = max(0, round((r["avvio"] - s).total_seconds() / 60))
            row.update(stato="eseguito", esito=r["esito"], avvio=_local(r["avvio"]), ritardo_avvio_min=delay,
                       in_ritardo=delay > LATE_START_MIN)
        elif now - s < dt.timedelta(minutes=GRACE_MIN):
            row.update(stato="in_attesa")
        else:
            horizon = min(end, now)
            hidden = any((a is None or a <= horizon) and (b is None or b >= lo) for a, b in log["bad"])
            if covered_from is None or covered_from > lo or hidden:
                why = ("log assente o vuoto" if covered_from is None else
                       "righe di log illeggibili nell'intervallo" if hidden else "log ruotato: l'intervallo non e' coperto")
                row.update(stato="non_verificabile", motivo=why)
            else:
                busy = next((x for x in runs if x["avvio"] < s and (x["fine"] is None or x["fine"] > s)
                             and x["esito"] != "occupato"), None)
                why = ("giro precedente ancora in corso alle %s (launchd non avvia un secondo giro)" % _local(s)[11:16]
                       if busy else "nessun avvio registrato fino alle %s" % _local(horizon)[11:16])
                row.update(stato="mancato", motivo=why, sovrapposto=bool(busy))
        out.append(row)
    slot_of = {id(r): s for s, r in primary.items()}
    return out, nxt, slot_of


def _public_run(r, slot):
    wall = round((r["fine"] - r["avvio"]).total_seconds() / 60) if r["fine"] else None
    delay = max(0, round((r["avvio"] - slot).total_seconds() / 60)) if slot else None
    return {"avvio": _local(r["avvio"]), "fine": _local(r["fine"]), "esito": r["esito"], "exit_code": r["exit_code"],
            "modo": r["modo"], "durata_lavoro_s": r["durata_s"], "durata_reale_min": wall,
            "conclusione_tardiva": wall is not None and wall > LONG_RUN_MIN,
            "orario_previsto": _local(slot)[11:16] if slot else None, "ritardo_avvio_min": delay,
            "fonti": dict(sorted(r["fonti"].items())), "tentativi": r["tentativi"], "anomalie": r["anomalie"],
            "tipi_oltre_soglia": r["oltre_soglia"]}


def history(log_path=None, plist_path=None, now=None):
    """Storico dell'automazione per il feed `cuore.school.feed/1` (chiave `automazione.storico`). Mai eccezioni."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    log_path = os.path.expanduser(log_path or os.environ.get("CUORE_SCHOOL_JOB_LOG") or LOG_DEFAULT)
    plist_path = os.path.expanduser(plist_path or os.environ.get("CUORE_SCHOOL_JOB_PLIST") or PLIST_DEFAULT)
    try:
        log = read_log(log_path)
        schedule = read_schedule(plist_path)
        runs = build_runs(log["events"], now)
        slots, nxt, slot_of = classify(runs, log, schedule, now)
    except Exception as e:  # noqa: BLE001 - un errore di lettura diventa «non disponibile», mai un esito
        return {"disponibile": False, "motivo": "storico non leggibile (%s)" % type(e).__name__}
    counts = {}
    for row in slots:
        key = row["stato"] if row["stato"] != "eseguito" else row["esito"]
        counts[key] = counts.get(key, 0) + 1
    late = sum(1 for row in slots if row.get("in_ritardo"))
    log_state = ("assente" if not log["files"] else "vuoto" if not log["events"] and not log["bad"]
                 else "con_righe_illeggibili" if log["bad"] else "ok")
    return {
        "disponibile": True,
        "fonte": {"log": "school-job.jsonl" + (" (+%d ruotati)" % (log["files"] - 1) if log["files"] > 1 else ""),
                  "calendario": "LaunchAgent cloud.scuola.job", "caricato_in_launchd": "non verificabile"},
        "calendario": {"stato": schedule["stato"], "orari": ["%02d:%02d" % t for t in schedule["orari"]],
                       "all_avvio": schedule["all_avvio"], "ogni_s": schedule.get("ogni_s")},
        "prossimo_giro": _local(nxt),
        "finestra_ore": WINDOW_H,
        "conteggi": counts,
        "in_ritardo": late,
        "orari": slots,
        "giri": [_public_run(r, slot_of.get(id(r))) for r in runs[-LAST_RUNS:]][::-1],
        "log": {"stato": log_state, "eventi": len(log["events"]), "righe_illeggibili": log["bad_lines"],
                "dal": _local(log["events"][0][0]) if log["events"] else None},
        "letto_il": _local(now),
    }
