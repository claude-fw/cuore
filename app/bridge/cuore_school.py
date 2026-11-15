"""CUORE School: adattatore di SOLA LETTURA fra il motore Cloud School e l'app CUORE (`cuore.school.feed/1`).

Cosa fa: chiama `school_dashboard.build_from_path` (lo stesso punto d'ingresso di `school_dashboard_v1`) per le pagine
che alimentano CUORE School e restituisce i documenti del motore senza modificarli, con provenienza, freschezza ed errori.

Confine di sola lettura (verificato a ogni chiamata, altrimenti nessun dato):
- Registry: il motore lo apre come copia in memoria (`immutable=1`); qui si confronta lo stato del file (e di -wal,
  -shm, -journal) prima e dopo. Se cambia qualcosa il feed viene scartato (`sola_lettura_verificata: false`).
- Rete: durante la lettura ogni connessione in uscita e ogni risoluzione DNS sono bloccate (nessun servizio esterno,
  ClasseViva, Classroom o Drive compresi, e' raggiungibile da questo processo); un tentativo diventa un errore, mai un dato.
- Materie: il motore le scarica da ClasseViva con login; l'adattatore NON lo fa. Usa un'istantanea locale (fuori dal
  repository) nel formato di `cv_subjects`; se manca, le pagine non vengono calcolate.
- Automazione: esito dell'ultimo giro del job scuola (sola lettura, `job/state.json`) e storico dei giri ricostruito da
  log e calendario del LaunchAgent (`cuore_automation.py`: orari eseguiti, parziali, mancati, non verificabili, prossimo
  giro). Con un Registry esplicito (test, collaudi) log e calendario si cercano accanto a quel Registry, mai quelli reali.
  CUORE non dipende da Notion: nessuno stato o dato di Notion entra nel feed.
- Nessun file scritto: nessun bytecode, nessuna cache, nessun log su disco.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import socket
import sys
import threading

sys.dont_write_bytecode = True                     # nemmeno __pycache__ nel motore
HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.normpath(os.path.join(HERE, "..", "..", "scuola", "mcp", "classeviva"))
for p in (ENGINE, os.path.join(ENGINE, "..", "_common")):
    if p not in sys.path:
        sys.path.insert(0, p)

import cuore_automation as AUTO  # noqa: E402
import cuore_school_contract as K  # noqa: E402
import cvv_dryrun as dry  # noqa: E402
import cvv_subject as subj  # noqa: E402
import registry as R  # noqa: E402
import school_context as SC  # noqa: E402
import school_dashboard as SD  # noqa: E402

SCHEMA = "cuore.school.feed/1"
PAGES = ("home", "voti", "assenze", "verifiche", "compiti", "materiali", "studio", "qualita_dati")
SUBJECTS_ENV = "CUORE_SCHOOL_SUBJECTS"
SUBJECTS_DEFAULT = "~/Library/Application Support/Cloud/school/cuore/materie.json"
ZONE = "Europe/Rome"
_LOCK = threading.Lock()                           # il blocco di rete e' di processo: una lettura alla volta


class SubjectsUnavailable(Exception):
    pass


class NetworkBlocked(RuntimeError):
    pass


def subjects_path():
    return os.path.expanduser(os.environ.get(SUBJECTS_ENV, SUBJECTS_DEFAULT))


def load_subjects(path=None):
    """Istantanea delle materie. Formati ammessi: uscita di `cv_subjects` ({subjects: [{id, materia, chiave, docenti}]})
    o elenco già normalizzato del motore ([{id, desc, key, docenti, docenti_nomi}]). Nessuna rete."""
    p = path or subjects_path()
    if not os.path.isfile(p):
        raise SubjectsUnavailable("istantanea materie assente (%s): le pagine richiedono l'elenco materie di ClasseViva" % p)
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("subjects") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise SubjectsUnavailable("istantanea materie vuota o in un formato non riconosciuto")
    out = []
    for s in items:
        if not isinstance(s, dict):
            raise SubjectsUnavailable("istantanea materie: voce non valida")
        if "key" in s and "desc" in s:                                    # formato del motore
            out.append({"id": s.get("id"), "desc": s["desc"], "key": s["key"],
                        "docenti_nomi": list(s.get("docenti_nomi") or []), "docenti": list(s.get("docenti") or [])})
        elif "materia" in s and "chiave" in s:                            # formato di cv_subjects
            names = list(s.get("docenti") or [])
            out.append({"id": s.get("id"), "desc": s["materia"], "key": s["chiave"], "docenti_nomi": names,
                        "docenti": [subj.norm(n) for n in names]})
        else:
            raise SubjectsUnavailable("istantanea materie: campi attesi assenti")
    return out


@contextlib.contextmanager
def no_network():
    """Blocca ogni connessione in uscita e ogni risoluzione di nomi per la durata della lettura."""
    def refuse(*_a, **_k):
        raise NetworkBlocked("rete bloccata: l'adattatore CUORE School legge solo il Registry locale")
    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo)
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo = saved


def _files_state(registry_path, manual_path, subjects_file):
    state = dry._fs_state(registry_path)
    for p in (manual_path, subjects_file):
        if p:
            state.update(dry._fs_state(p))
    return state


def inputs_state(registry_path=None, subjects_file=None, manual_path=None):
    """Stato (dimensione, mtime) dei file letti: se non cambia, il feed calcolato resta valido (cache del ponte)."""
    path = registry_path or R.default_path()
    st = _files_state(path, manual_path or SC.manual_default_path(), subjects_file or subjects_path())
    for name in JOB_FILES:
        st.update(dry._fs_state(os.path.join(job_dir(path), name)))
    for p in automation_paths(registry_path):                      # un nuovo giro del job rinnova il feed
        st.update(dry._fs_state(p))
    return json.dumps(st, sort_keys=True)


def automation_paths(registry_path=None):
    """(log, calendario) del job: quelli reali solo con il Registry reale; con un Registry esplicito, accanto a esso."""
    if registry_path is None:
        return (os.path.expanduser(os.environ.get("CUORE_SCHOOL_JOB_LOG") or AUTO.LOG_DEFAULT),
                os.path.expanduser(os.environ.get("CUORE_SCHOOL_JOB_PLIST") or AUTO.PLIST_DEFAULT))
    d = job_dir(registry_path)
    return os.path.join(d, "school-job.jsonl"), os.path.join(d, "cloud.scuola.job.plist")


JOB_DIR_ENV = "CUORE_SCHOOL_JOB_DIR"
JOB_FILES = ("state.json",)


def job_dir(registry_path=None):
    """Cartella di stato del job scuola (come `school_job.default_state_dir`: accanto al Registry)."""
    return os.path.expanduser(os.environ.get(JOB_DIR_ENV) or os.path.join(os.path.dirname(registry_path or R.default_path()), "job"))


def automation_status(state_dir, log_path=None, plist_path=None, now=None):
    """Ultimo giro del job scuola (solo esito e orari) e, se richiesto, storico dei giri da log e calendario.
    File assente o illeggibile = `disponibile: false` con il motivo: mai un esito inventato. Lo storico e' indipendente
    dallo stato: puo' essere disponibile anche quando `state.json` non lo e', e viceversa."""
    out = {"disponibile": False}
    try:
        with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("formato inatteso")
        ultimo = state.get("ultimo") if isinstance(state.get("ultimo"), dict) else {}
        out = {"disponibile": True,
               "job": {"esito": ultimo.get("esito"), "modo": ultimo.get("modo"), "fine": ultimo.get("fine"),
                       "ultimo_commit_ok": state.get("ultimo_commit_ok")},
               "problemi": job_problems(state.get("problemi")),
               # problemi notificati e scomparsi, la cui chiusura aspetta che la fonte non abbia piu' errori (solo il numero)
               "chiusure_in_attesa": len(state["risolti_in_attesa"]) if isinstance(state.get("risolti_in_attesa"), dict) else 0}
    except (OSError, ValueError) as e:
        out["motivo"] = "stato del job non leggibile (%s)" % type(e).__name__
    if log_path is not None or plist_path is not None:
        out["storico"] = AUTO.history(log_path, plist_path, now)
    return out


# tipi scritti da school_job.derive_problems; `parziale_temporaneo` e `fallito_temporaneo` restano riconosciuti per gli stati
# scritti prima del 25/10 (il job li migra in `temporaneo:<fonte>` al primo giro con errori temporanei)
PROBLEM_KINDS = ("autorizzazione", "keychain", "parziale", "temporaneo", "parziale_temporaneo", "fallito", "fallito_temporaneo",
                 "freschezza", "anomalia")


def _iso(v):
    try:
        return v if isinstance(v, str) and dt.datetime.fromisoformat(v).tzinfo else None
    except ValueError:
        return None


def job_problems(raw):
    """Problemi aperti del job (`state.json["problemi"]`, scritti da school_job.update_notifications): tipo (prima di ':'),
    fonte se e' una delle due, titolo fisso del job, dal, giri, notificato / in osservazione (temporaneo, notificato solo se
    si ripete). Nessuna chiave completa, tipo di dato, uid o testo libero; voci non valide ignorate."""
    out = []
    for key, v in sorted((raw or {}).items()) if isinstance(raw, dict) else []:
        if not isinstance(v, dict):
            continue
        parts = str(key).split(":")
        kind = parts[0] if parts[0] in PROBLEM_KINDS else "altro"
        title = v.get("titolo")
        out.append({"tipo": kind, "fonte": parts[1] if len(parts) > 1 and parts[1] in ("classeviva", "classroom") else None,
                    "titolo": title[:80] if isinstance(title, str) and title.startswith("Cloud Scuola:") else None,
                    "dal": _iso(v.get("dal")), "giri": v.get("giri") if isinstance(v.get("giri"), int) else None,
                    "notificato": bool(_iso(v.get("ultima_notifica"))), "in_osservazione": bool(v.get("attesa_conferma"))})
    return out


def _freshness(doc, now):
    """Dal blocco `freschezza` del motore: età per tipo e, per fonte, la lettura più vecchia (prudente)."""
    blk = next((b for b in (doc or {}).get("blocchi", []) if b.get("id") == "freschezza"), None)
    if not blk or blk.get("stato") != "dati":
        return {"disponibile": False}
    per_kind = blk["dati"].get("per_kind") or {}
    sources, stale = {}, []
    for kind, info in sorted(per_kind.items()):
        src = kind.split("/")[0]
        eta = info.get("eta_ore")
        if info.get("stale"):
            stale.append(kind)
        s = sources.setdefault(src, {"eta_ore_max": None, "non_letti": []})
        if eta is None:
            s["non_letti"].append(kind)
        elif s["eta_ore_max"] is None or eta > s["eta_ore_max"]:
            s["eta_ore_max"] = eta
    for s in sources.values():
        s["aggiornato_al"] = None if s["eta_ore_max"] is None else \
            (now - dt.timedelta(hours=s["eta_ore_max"])).replace(microsecond=0).isoformat()
    return {"disponibile": True, "soglia_ore": blk["dati"].get("soglia_ore"), "per_fonte": sources, "tipi_obsoleti": stale}


def build_feed(registry_path=None, now=None, subjects_file=None, manual_path=None, pages=PAGES):
    """Feed di CUORE School secondo il contratto `cuore.school.feed/1` (cuore_school_contract.py): pagine validate,
    errori con codice e messaggio pubblico, esito e freschezza espliciti. Mai eccezioni."""
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(ZoneInfo(ZONE)).replace(microsecond=0)
    feed = _build_raw(registry_path, now, subjects_file, manual_path, pages)
    feed = K.finalize(feed, freshness=lambda pg: _freshness(pg.get("qualita_dati"), now))
    first = next(iter(feed["pagine"].values()), None)               # solo pagine gia' validate
    if first:
        feed["versione_motore"] = first["provenance"]["dashboard_version"]
        feed["data_riferimento"] = first["riferimento_temporale"]["data_riferimento"]
    return feed


def _build_raw(registry_path, now, subjects_file, manual_path, pages):
    """Lettura del motore in sola lettura; il contratto si applica dopo, in `build_feed`."""
    path = registry_path or R.default_path()
    subjects_file = subjects_file or subjects_path()
    manual_file = manual_path or SC.manual_default_path()
    feed = {"schema": SCHEMA, "fonte": "reale", "motore": "Cloud School · cloud.school.dashboard/1", "generato": now.isoformat(),
            "pagine": {}, "errori": [], "freschezza": {"disponibile": False}, "sola_lettura_verificata": False,
            "scritture": {"registry": False, "classeviva": False, "classroom": False, "drive": False}}
    log_path, plist_path = automation_paths(registry_path)
    feed["automazione"] = automation_status(job_dir(path), log_path, plist_path,
                                            now.astimezone(dt.timezone.utc))   # anche se le pagine falliscono
    if not os.path.isfile(path):
        feed["errori"].append(K.error("registro", "registro_assente"))
        return feed
    try:
        subjects = load_subjects(subjects_file)
    except (SubjectsUnavailable, OSError, ValueError) as e:
        feed["errori"].append(K.error("materie", "materie_non_disponibili", type(e).__name__))
        return feed
    with _LOCK, no_network():
        before = _files_state(path, manual_file, subjects_file)
        try:                                                          # una sola copia del Registry e fonti condivise
            session = SD.PageSession(path, now, manual_path=manual_file, subjects=subjects)
        except Exception as e:  # noqa: BLE001 - nessuna pagina calcolabile: ognuna lo dichiara
            session = None
            for p in pages:
                feed["errori"].append(K.error(p, "rete_bloccata") if isinstance(e, NetworkBlocked)
                                      else K.error(p, "errore_motore", type(e).__name__))
        if session is not None:
            with session:
                for p in pages:
                    try:
                        feed["pagine"][p] = session.build(p)
                    except NetworkBlocked:
                        feed["errori"].append(K.error(p, "rete_bloccata"))
                    except Exception as e:  # noqa: BLE001 - un errore del motore diventa un errore visibile, mai un dato
                        feed["errori"].append(K.error(p, "errore_motore", type(e).__name__))
        changed = before != _files_state(path, manual_file, subjects_file)
    if changed:                                                           # confine violato: nessun dato esce
        feed["pagine"] = {}
        feed["errori"].append(K.error("sola_lettura", "sola_lettura_violata"))
        return feed
    feed["sola_lettura_verificata"] = True
    feed["materie"] = {s["key"]: s["desc"] for s in subjects if s.get("key")}
    return feed


def summary(feed):
    """Solo stati e conteggi (nessun valore scolastico): per controlli da terminale."""
    return {"fonte": feed["fonte"], "sola_lettura_verificata": feed["sola_lettura_verificata"], "errori": feed["errori"],
            "versione_motore": feed.get("versione_motore"), "freschezza": feed["freschezza"],
            "pagine": {p: [(b["id"], b["stato"]) for b in d["blocchi"]] for p, d in feed["pagine"].items()}}


if __name__ == "__main__":
    print(json.dumps(summary(build_feed()), ensure_ascii=False, indent=1))
