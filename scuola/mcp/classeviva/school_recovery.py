#!/usr/bin/env python3
"""RECUPERO ALL'USO DEL JOB SCUOLA (E1): decide SE recuperare un giro, non avvia nulla.

Problema (roadmap §2, 01/11): con il Mac in stop a batteria i giri delle 06:45 e delle 14:30 avanzano a scatti durante
i brevi risvegli in background e finiscono PARZIALE; con il Mac ibernato gli orari si perdono. Quando il Mac torna in
uso, un giro di recupero aggiorna i dati entro pochi minuti invece di aspettare l'orario successivo.

Decisione pura (`decide`) e memoria dei recuperi (`record`). Senza opzioni il comando e' solo diagnostica. Con
`--esegui` (collegamento approvato il 02/11, decisione C-04 S1) lo avvia ogni 10 minuti un LaunchAgent SEPARATO
(`cloud.scuola.recupero`, il job originale e il suo plist non cambiano): se la decisione e' «si'» salva la memoria del
recupero e poi esegue `school_job.py run --commit --notify` al posto suo (il lock del job impedisce due giri insieme).

Regole, nell'ordine (ogni «no» ha un motivo a vocabolario fisso):
  1. un giro e' in corso (avvio senza conclusione recente) -> no: `giro_in_corso`;
  2. si guarda l'ultimo orario passato da almeno `attesa_dopo_orario_min` (lascia a launchd il tempo di eseguirlo);
  3. l'orario e' coperto se dopo di esso (meno 2 minuti) c'e' un giro OK -> no: `ultimo_orario_coperto`;
  4. l'ultimo giro e' fallito per l'autorizzazione -> no: `serve_intervento` (un nuovo giro non la ripara);
  5. l'orario successivo e' vicino -> no: `prossimo_orario_vicino` (ci pensa la pianificazione);
  6. il Mac non e' in uso (inattivita' dell'utente oltre soglia, es. risveglio in background) -> no: `mac_non_in_uso`;
  7. per quell'orario c'e' gia' stato un recupero -> no: `gia_recuperato` (al massimo un recupero per orario);
  8. l'ultimo recupero e' troppo recente -> no: `recupero_recente`;
  altrimenti si': `recupero`, con il numero di orari scoperti nelle ultime 48 ore (accorpati in UN solo giro: dopo un
  lungo periodo di inattivita' non si recuperano gli orari uno per uno).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "_common"))

import cuore_automation as AUTO  # noqa: E402

POLICY = {
    "attesa_dopo_orario_min": 20,     # dopo l'orario: launchd puo' ancora eseguirlo (anche in ritardo)
    "margine_prossimo_orario_min": 30,
    "intervallo_minimo_min": 30,      # fra due recuperi
    "inattivita_max_s": 300,          # oltre: il Mac non e' «in uso» (nessun input dell'utente)
    "finestra_h": 48,
    "memoria_recuperi": 20,
}
EARLY_MIN = AUTO.EARLY_MIN
NEEDS_USER = ("autorizzazione",)


def _iso(t):
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    try:
        t = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo is not None else None


def decide(runs, times, now, idle_s, state=None, policy=None):
    """Decisione pura. `runs` come `cuore_automation.build_runs` (avvio in UTC, esito a vocabolario fisso); `times` =
    orari locali [(ora, minuto)]; `idle_s` = secondi dall'ultimo input dell'utente (None = non noto: non in uso);
    `state` = memoria dei recuperi ({"recuperi": [{"orario", "avviato"}]})."""
    p = dict(POLICY, **(policy or {}))
    state = state or {}
    out = {"recupera": False, "motivo": None, "orario": None, "orari_scoperti": 0}
    if any(r["esito"] == "in_corso" for r in runs):
        out["motivo"] = "giro_in_corso"
        return out
    horizon = now - dt.timedelta(minutes=p["attesa_dopo_orario_min"])
    past = AUTO.slots_between(times, now - dt.timedelta(hours=p["finestra_h"]), horizon)
    if not past:
        out["motivo"] = "nessun_orario"
        return out
    early = dt.timedelta(minutes=EARLY_MIN)
    oks = [r["avvio"] for r in runs if r["esito"] == "ok"]
    uncovered = [s for s in past if not any(a >= s - early for a in oks)]
    last = past[-1]
    out["orario"] = _iso(last)
    out["orari_scoperti"] = len(uncovered)
    if last not in uncovered:
        out["motivo"] = "ultimo_orario_coperto"
        return out
    finished = [r for r in runs if r["esito"] not in ("in_corso",)]
    if finished and max(finished, key=lambda r: r["avvio"])["esito"] in NEEDS_USER:
        out["motivo"] = "serve_intervento"
        return out
    upcoming = AUTO.slots_between(times, now, now + dt.timedelta(minutes=p["margine_prossimo_orario_min"]))
    if upcoming:
        out["motivo"] = "prossimo_orario_vicino"
        return out
    if idle_s is None or idle_s > p["inattivita_max_s"]:
        out["motivo"] = "mac_non_in_uso"
        return out
    done = state.get("recuperi") or []
    if any(d.get("orario") == out["orario"] for d in done):
        out["motivo"] = "gia_recuperato"
        return out
    starts = [t for t in (_parse(d.get("avviato")) for d in done) if t is not None]
    if starts and now - max(starts) < dt.timedelta(minutes=p["intervallo_minimo_min"]):
        out["motivo"] = "recupero_recente"
        return out
    out["recupera"] = True
    out["motivo"] = "recupero"
    return out


def record(state, decision, now, policy=None):
    """Nuova memoria dei recuperi dopo un avvio di recupero (funzione pura; chi la salva decide dove)."""
    if not decision.get("recupera"):
        return dict(state or {})
    p = dict(POLICY, **(policy or {}))
    done = list((state or {}).get("recuperi") or [])
    done.append({"orario": decision["orario"], "avviato": _iso(now)})
    return {"recuperi": done[-p["memoria_recuperi"]:]}


_IDLE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def idle_seconds_from_ioreg(text):
    """Secondi di inattivita' dell'utente dall'uscita di `ioreg -c IOHIDSystem` (valore in nanosecondi); None se assente."""
    m = _IDLE.search(text or "")
    return int(m.group(1)) / 1e9 if m else None


def idle_seconds():
    """Secondi dall'ultimo input dell'utente letti con ioreg; None se non leggibili (= Mac non in uso)."""
    import subprocess
    try:
        return idle_seconds_from_ioreg(subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"],
                                                      capture_output=True, text=True, timeout=10).stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None, execv=None):
    """Solo diagnostica: stampa la decisione per lo stato attuale. Non avvia il job e non scrive nulla."""
    ap = argparse.ArgumentParser(description="Recupero all'uso del job scuola: decisione in sola lettura.")
    ap.add_argument("--log", default=os.path.expanduser(AUTO.LOG_DEFAULT))
    ap.add_argument("--plist", default=os.path.expanduser(AUTO.PLIST_DEFAULT))
    ap.add_argument("--stato", help="memoria dei recuperi (JSON, sola lettura)")
    ap.add_argument("--inattivita-s", type=float, help="secondi di inattivita' (senza: letti con ioreg)")
    ap.add_argument("--esegui", action="store_true", help="se la decisione e' si': salva --stato ed esegue il job")
    a = ap.parse_args(argv)
    if a.esegui and not a.stato:
        ap.error("--esegui richiede --stato (memoria dei recuperi)")
    now = dt.datetime.now(dt.timezone.utc)
    log = AUTO.read_log(a.log)
    schedule = AUTO.read_schedule(a.plist)
    if log["files"] == 0 or schedule["stato"] != "ok":
        print(json.dumps({"recupera": False, "motivo": "log_o_calendario_non_leggibili",
                          "calendario": schedule["stato"]}, ensure_ascii=False))
        return 0
    runs = AUTO.build_runs(log["events"], now)
    times = schedule["orari"]
    idle = a.inattivita_s
    if idle is None:
        idle = idle_seconds()
    state = {}
    if a.stato and os.path.isfile(a.stato):
        with open(a.stato, encoding="utf-8") as f:
            state = json.load(f)
    out = decide(runs, times, now, idle, state)
    out["righe_di_log_illeggibili"] = log["bad_lines"]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.esegui and out["recupera"]:
        tmp = a.stato + ".tmp"                                   # memoria prima dell'avvio: al massimo un recupero per orario
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record(state, out, now), f, ensure_ascii=False)
        os.replace(tmp, a.stato)
        sys.stdout.flush()
        (execv or os.execv)(sys.executable, [sys.executable, os.path.join(HERE, "school_job.py"), "run", "--commit", "--notify"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
