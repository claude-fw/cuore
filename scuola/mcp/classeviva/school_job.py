#!/usr/bin/env python3
"""JOB DI SINCRONIZZAZIONE SCUOLA: coordina i componenti esistenti, non li sostituisce.

    fonti (ClasseViva, Classroom) -> Registry (cvv_sync.run, con tutte le sue protezioni) -> freschezza
    (Registry.freshness) -> riepilogo, log, notifiche locali.

Garanzie
  * DEFAULT = dry-run: il Registry si scrive solo con --commit. Mai scritture su ClasseViva, Classroom o altri servizi.
  * Ogni tipo e' letto UNA volta (Reader memorizzato): piano, controlli del job e scrittura usano gli STESSI record,
    quindi expect_create coincide per costruzione e i controlli valgono proprio sui dati scritti.
  * Retry limitato e solo per errori TEMPORANEI (rete, 5xx, 429); autorizzazione e dati anomali non si ritentano.
  * Doppia scadenza: `deadline_s` in time.monotonic (tempo di esecuzione) e `real_deadline_s` su CLOCK_MONOTONIC (conta
    anche lo stop del Mac e non segue le correzioni dell'orologio civile), con budget PER FONTE: una fonte lenta non
    consuma il tempo dell'altra. Oltre il tempo reale: per quella fonte nessuna lettura nuova e nessun retry (controllo a
    ogni tentativo); i tipi non letti restano in errore temporaneo con motivo 'scadenza_tempo_reale', il Registry
    registra un run 'error' (freschezza: 'ultimo_run_in_errore'), esito PARZIALE o FALLITO.
  * Ordine delle fonti: prima quella con la lettura riuscita piu' vecchia (source_order), quindi una fonte rimasta
    indietro recupera al giro successivo invece di restare sempre ultima.
  * Una fonte che fallisce non marca nulla come scomparso (lo garantisce gia' il Registry); i tipi falliti vengono
    registrati come run 'error', cosi' la freschezza li mostra.
  * Scomparse sopra soglia: il tipo viene TRATTENUTO (non scritto) e segnalato per un controllo umano.
  * Registro assente o schema da migrare: nessuna scrittura (non si crea ne' migra mai un registro da solo).
  * Lock non bloccante (school-job.lock): due job non girano insieme; il lock del Registry resta quello di cvv_sync.
  * Nessun servizio esterno oltre alle fonti in sola lettura (ClasseViva, Classroom): CUORE non dipende da Notion.
    Unica eccezione, SPENTA per difetto: dopo un giro --commit, solo con la configurazione `cuore-cloud.json` nella
    cartella di stato, il feed School minimizzato va al cloud CUORE (school_cloud_push.py, blocco E3); senza
    configurazione nulla cambia, e l'invio non cambia mai esito o codice d'uscita.

Codici di uscita: 0 OK, 1 PARZIALE, 2 FALLITO, 3 AUTORIZZAZIONE (autorizzazione scaduta o credenziali non leggibili:
serve un intervento), 4 OCCUPATO, 64 uso errato.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "_common"))

import cls_registry as CL   # noqa: E402
import cuore_automation as AUTO   # noqa: E402   (_common: orari del calendario e giri dal log, sola lettura)
import cvv_registry as CR   # noqa: E402
import cvv_sync as S        # noqa: E402
import registry as R        # noqa: E402

SCHEMA = "cloud.school.job/1"
EXIT = {"OK": 0, "PARZIALE": 1, "FALLITO": 2, "AUTORIZZAZIONE": 3, "OCCUPATO": 4}
EXIT_USAGE = 64
SOURCES = {"classeviva": (CR, ("agenda_event", "grade", "absence", "notice", "lesson")),
           "classroom": (CL, tuple(CL.ALL_KINDS))}
ORDER = ("classeviva", "classroom")
POLICY = {"max_missing": 5,         # scomparse per tipo oltre le quali il tipo e' trattenuto (controllo umano)
          "attempts": 3,            # tentativi per lettura (1 + 2 retry), solo per errori temporanei
          "delays": (30, 120),      # attese prima del 2o e del 3o tentativo (secondi)
          "deadline_s": 1800,       # oltre questo tempo dall'avvio non si avviano altri retry
          "real_deadline_s": 1800,  # tempo REALE per FONTE dall'avvio della fonte (CLOCK_MONOTONIC: conta anche lo stop
                                    # del Mac, che time.monotonic su macOS non conta): oltre, nessuna nuova lettura ne'
                                    # retry per quella fonte; la fonte successiva ha il suo budget (giro <= 2 x 30 min)
          "soglia_ore": 48,         # freschezza (stessa soglia provvisoria di school_freshness)
          "renotify_hours": 24,     # un problema ancora aperto viene ri-notificato al massimo una volta al giorno
          "conferma_giri": 2,       # problemi dovuti SOLO a errori temporanei (rete, Mac in stop): notificati dal 2o giro
          "giri_mancati_notifica": 3}   # orari consecutivi senza alcun avvio (automazione ferma): notificati da 3
STOP_MIN_S = 300                    # tempo reale oltre il tempo di esecuzione: oltre 5 minuti = Mac in stop durante il giro
EVENTS_KEEP_DAYS = 14               # eventi gia' notificati conservati in state.json (deduplica), poi dimenticati
OK_STATES = ("scritto", "simulato")
DEADLINE_REASON = "scadenza_tempo_reale"        # campo 'motivo' dei tipi non letti (o senza retry) per il tempo reale
NEEDS_USER = ("autorizzazione", "keychain")     # categorie che non si risolvono ritentando
CHANGING = ("update", "missing", "state_change", "reappeared")   # categorie del piano con uid riportati nel riepilogo


def default_state_dir():
    return os.path.join(os.path.dirname(R.default_path()), "job")


def default_log_file():
    return os.path.expanduser("~/Library/Logs/Cloud/school-job.jsonl")


# ------------------------------------------------------------------------------------------------ errori
_TEMP = re.compile(r"rete non raggiungibile|URLError|timed? ?out|TimeoutError|Connection(Reset|Refused|Aborted)Error|"
                   r"RemoteDisconnected|IncompleteRead|HTTP 5\d\d|HTTP 429|\[server\]|\[rate_limit\]|tentativi esauriti", re.I)
# credenziali NON LEGGIBILI (Portachiavi bloccato, voce mancante, `security` assente): non e' detto che l'autorizzazione
# sia scaduta, quindi il messaggio non deve spingere a riautorizzare. Va controllato PRIMA di _AUTH.
_KEYCHAIN = re.compile(r"Credenziali non trovate|Accesso al Keychain|Comando 'security'|Classroom non autorizzato|"
                       r"client id/secret mancanti|Keychain non disponibile", re.I)
_AUTH = re.compile(r"AuthError|Login ClasseViva fallito|\[(auth|scope_insufficient|permission|api_disabled)\]", re.I)
_DATA = re.compile(r"Risposta inattesa|troppe pagine", re.I)
_SECRETISH = re.compile(r"[A-Za-z0-9._~+/=-]{40,}")


def classify_error(message):
    """'temporaneo' | 'keychain' | 'autorizzazione' | 'dati' | 'errore'. L'ordine conta: un 5xx o un errore di rete durante
    il rinnovo del token e' temporaneo; credenziali illeggibili non sono un'autorizzazione scaduta."""
    m = str(message or "")
    if _TEMP.search(m):
        return "temporaneo"
    if _KEYCHAIN.search(m):
        return "keychain"
    if _AUTH.search(m):
        return "autorizzazione"
    if _DATA.search(m):
        return "dati"
    return "errore"


def mask(text, limit=300):
    """Nessun token o segreto nei log/riepiloghi: maschera le sequenze lunghe tipo token e tronca."""
    return _SECRETISH.sub("<MASCHERATO>", str(text))[:limit]


def _err(e):
    return "%s: %s" % (type(e).__name__, e)


# ------------------------------------------------------------------------------------------------ letture
class RealTimeExceeded(Exception):
    """Tempo REALE massimo del giro superato (es. Mac in stop): la lettura non e' iniziata o non viene ritentata.
    Il messaggio contiene 'tentativi esauriti', quindi classify_error lo tratta come errore temporaneo: il tipo resta
    in errore (mai aggiornato) e il giro successivo lo rilegge normalmente."""

    def __init__(self, elapsed_s, limit_s):
        super().__init__("tempo reale massimo superato (%d s dall'avvio, limite %d s; es. Mac in stop): tentativi esauriti, "
                         "si riprova al giro successivo" % (elapsed_s, limit_s))


def sleep_inclusive_clock():
    """Secondi da CLOCK_MONOTONIC. Su macOS (man clock_gettime) avanza anche con il sistema in stop e non si puo'
    impostare (solo CLOCK_REALTIME si imposta), quindi non salta con le correzioni dell'orologio civile.
    time.monotonic su macOS usa mach_absolute_time (= CLOCK_UPTIME_RAW), che durante lo stop si ferma: il 2026-10-22
    un giro ha misurato 371 s su 8 h 23 min reali. Nessun ripiego: se CLOCK_MONOTONIC manca, errore esplicito."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class RealDeadline:
    """Scadenza del giro a tempo REALE: `clock` restituisce secondi (Deps.real_clock = sleep_inclusive_clock)."""

    def __init__(self, clock, limit_s):
        self.clock, self.limit_s = clock, limit_s
        self.start = clock()

    def elapsed(self):
        return self.clock() - self.start

    def __call__(self, extra_s=0):
        return self.elapsed() + extra_s > self.limit_s

    def error(self):
        return RealTimeExceeded(self.elapsed(), self.limit_s)


class Reader:
    """Lettura con retry limitato (solo errori temporanei) e MEMORIZZATA: le chiamate successive restituiscono una copia
    dello stesso risultato o rilanciano lo stesso errore, senza rileggere la fonte.
    `expired` (RealDeadline, facoltativo): a tempo reale superato nessun primo tentativo, nessuna attesa, nessun retry."""

    def __init__(self, fn, label, policy, deadline, sleep, monotonic, log, expired=None):
        self.fn, self.label, self.policy, self.deadline = fn, label, policy, deadline
        self.sleep, self.monotonic, self.log, self.expired = sleep, monotonic, log, expired
        self.done, self.value, self.exc, self.attempts, self.errors = False, None, None, 0, []
        self.deadline_hit = False                           # True se il tempo reale ha impedito una lettura o un retry

    def __call__(self):
        if not self.done:
            self._read()
        if self.exc is not None:
            raise self.exc
        return copy.deepcopy(self.value)

    def _read(self):
        delays = list(self.policy["delays"])
        for attempt in range(1, self.policy["attempts"] + 1):
            if self.expired is not None and self.expired():     # controllato a OGNI tentativo: anche dopo un'attesa
                self.deadline_hit = True
                if attempt == 1:                                # passata in stop, il retry non parte
                    self.exc = self.expired.error()             # lettura mai iniziata: tentativi = 0
                    self.errors.append({"tentativo": 0, "categoria": "temporaneo", "errore": mask(_err(self.exc))})
                break                                           # altrimenti resta l'ultimo errore reale
            self.attempts = attempt
            try:
                self.value = list(self.fn())
                self.exc = None
                break
            except Exception as e:      # noqa: BLE001 - classificato e riportato, mai nascosto
                self.exc = e
                cat = classify_error(_err(e))
                self.errors.append({"tentativo": attempt, "categoria": cat, "errore": mask(_err(e))})
                delay = delays[min(attempt - 1, len(delays) - 1)] if delays else 0
                if cat != "temporaneo" or attempt == self.policy["attempts"] or self.monotonic() + delay > self.deadline:
                    break
                if self.expired is not None and self.expired(delay):
                    self.deadline_hit = True
                    break                                       # tempo reale: niente attesa ne' nuovo tentativo
                self.log("retry", lettura=self.label, tentativo=attempt, attesa_s=delay, errore=mask(_err(e)))
                self.sleep(delay)
        self.done = True


class _Login:
    """Client ClasseViva creato al primo uso. Un errore di AUTORIZZAZIONE o di Portachiavi viene memorizzato: nessun nuovo
    tentativo di login con credenziali rifiutate (evita blocchi dell'account); un errore temporaneo si puo' ritentare."""

    def __init__(self, factory):
        self.factory, self.client, self.auth_exc = factory, None, None

    def __call__(self):
        if self.auth_exc is not None:
            raise self.auth_exc
        if self.client is None:
            try:
                self.client = self.factory()
            except Exception as e:      # noqa: BLE001
                if classify_error(_err(e)) in NEEDS_USER:
                    self.auth_exc = e
                raise
        return self.client


def classeviva_fetchers(today=None, factory=None):
    """Stesse letture di server._registry_fetchers (finestre comprese; verificato da test), ma con login pigro."""
    import cvv_client as cv
    import server as SV

    def real_factory():
        user, pw = cv.load_credentials()
        c = cv.Client(user, pw)
        c.login()
        return c
    client = _Login(factory or real_factory)
    today = today or dt.date.today()
    ys, ye = cv.school_year_range(today)
    lesson_end = min(ye, today)
    return {"agenda_event": (lambda: SV._agenda_in_range(client(), ys, ye)[2], (str(ys), str(ye))),
            "grade": (lambda: client().grades(), None), "absence": (lambda: client().absences(), None),
            "notice": (lambda: client().noticeboard(), None),
            "lesson": (lambda: client().lessons(ys, lesson_end), (str(ys), str(lesson_end)))}


def classroom_fetchers():
    """Come server._cls_fetchers; in piu' una verifica preliminare del rinnovo del token (nessun dato letto)."""
    import cls_auth as A
    import cls_client as C
    provider = A.token_provider()
    return C.make_fetchers(C.ClassroomClient(provider)), lambda: provider(True)


class Deps:
    """Dipendenze iniettabili (i test usano fonti sintetiche, nessuna rete)."""

    def __init__(self, sources=None, now=None, sleep=time.sleep, monotonic=time.monotonic, notify_send=None,
                 real_clock=sleep_inclusive_clock, schedule=None):
        self.sources = sources or {"classeviva": lambda: (classeviva_fetchers(), None), "classroom": classroom_fetchers}
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc).astimezone().replace(microsecond=0))
        self.sleep, self.monotonic, self.notify_send = sleep, monotonic, notify_send
        self.real_clock = real_clock                        # real_deadline_s e stop del Mac durante il giro
        # (log JSONL, plist del LaunchAgent) per gli orari mancati; None = non controllati (test, uso come libreria).
        # Solo `main` lo imposta: una libreria non legge mai il log o il calendario reali da sola.
        self.schedule = schedule


# ------------------------------------------------------------------------------------------------ una fonte
def _anomaly(kind_key, gravita, fonte, kind, testo):
    return {"chiave": kind_key, "gravita": gravita, "fonte": fonte, "kind": kind, "testo": mask(testo, 500)}


def _dedupe(anomalies):
    """Una voce per chiave (piu' avvisi dello stesso tipo nello stesso kind diventano un'unica anomalia)."""
    out = {}
    for a in anomalies:
        if a["chiave"] in out:
            prev = out[a["chiave"]]
            if a["testo"] not in prev["testo"]:
                prev["testo"] = mask(prev["testo"] + " | " + a["testo"], 500)
        else:
            out[a["chiave"]] = dict(a)
    return list(out.values())


def _kind_anomalies(fonte, kind, entry, policy):
    out = []
    c = entry.get("conteggi") or {}
    amb = (entry.get("riconciliazione") or {}).get("ambigue") or []
    if amb:
        ids = ",".join(sorted(str(a.get("evtId")) for a in amb))
        out.append(_anomaly("assenze_ambigue:%s" % ids, "controllo", fonte, kind,
                            "%d assenze con identita' ambigua: non registrate, candidati non marcati come scomparsi" % len(amb)))
    if c.get("missing_refused"):
        out.append(_anomaly("scomparse_rifiutate:%s/%s" % (fonte, kind), "controllo", fonte, kind,
                            "il Registry ha rifiutato di marcare %d record come scomparsi (calo anomalo della lettura)"
                            % c["missing_refused"]))
    if c.get("missing", 0) > policy["max_missing"]:
        out.append(_anomaly("scomparse_sopra_soglia:%s/%s" % (fonte, kind), "controllo", fonte, kind,
                            "%d record risultano scomparsi (soglia %d): tipo trattenuto, nulla scritto"
                            % (c["missing"], policy["max_missing"])))
    for w in entry.get("avvisi") or []:
        if "NON DETERMINABILE" in w or "senza versione registrata con titolo" in w:
            out.append(_anomaly("titoli_non_determinabili:%s/%s" % (fonte, kind), "info", fonte, kind, w))
    return out


def run_source(fonte, deps, kinds, commit, path, policy, deadline, log, expired=None):
    A = SOURCES[fonte][0]
    res = {"esito": None, "kinds": {}, "autorizzazione": None, "verifica_post_scrittura": None, "backup": None}
    anomalies = []

    def fail_all(categoria, errore, stato="errore"):
        for k in kinds:
            res["kinds"][k] = {"stato": stato, "categoria": categoria, "errore": mask(errore)}

    # Scadenza a tempo reale: la applicano i Reader (autorizzazione e ogni tipo). Un tipo non letto passa comunque dal
    # commit sotto come lettura fallita, quindi il Registry registra un run 'error' e la freschezza lo mostra.
    try:
        fetchers, preflight = deps.sources[fonte]()
    except Exception as e:      # noqa: BLE001 (import, Keychain...)
        fail_all(classify_error(_err(e)), _err(e))
        fetchers, preflight = None, None
    if fetchers is not None and preflight is not None:
        pre = Reader(lambda: (preflight(), [])[1], "%s:autorizzazione" % fonte, policy, deadline, deps.sleep, deps.monotonic, log,
                     expired)
        try:
            pre()
            res["autorizzazione"] = {"esito": "ok", "tentativi": pre.attempts}
        except Exception as e:      # noqa: BLE001
            cat = classify_error(_err(e))
            res["autorizzazione"] = {"esito": "errore", "categoria": cat, "errore": mask(_err(e)), "tentativi": pre.attempts}
            err = e

            def boom(err=err):
                raise err
            fetchers = {k: (boom, fetchers[k][1]) for k in fetchers}       # nessuna lettura; run 'error' registrati
    if fetchers is None:
        res["esito"] = "fallita"
        return res, anomalies

    readers = {k: Reader(fetchers[k][0], "%s:%s" % (fonte, k), policy, deadline, deps.sleep, deps.monotonic, log, expired)
               for k in kinds}
    memo = {k: (readers[k], fetchers[k][1]) for k in kinds}
    plan = S.run(memo, kinds=kinds, commit=False, db_path=path, adapter=A)
    if "errore_baseline" in plan:
        fail_all("registro", plan["errore_baseline"])
        res["esito"] = "fallita"
        return res, anomalies
    migr = (plan.get("schema") or {}).get("migrazione_necessaria")
    for k in kinds:
        e = plan["piano"][k]
        if e.get("stato") != "ok":
            res["kinds"][k] = {"stato": "errore", "categoria": classify_error(e.get("errore")), "errore": mask(e.get("errore")),
                               "tentativi": readers[k].attempts}
            if readers[k].deadline_hit:                      # lettura non iniziata o retry annullato dal tempo reale
                res["kinds"][k]["motivo"] = DEADLINE_REASON
            continue
        c = {x: v for x, v in (e.get("conteggi") or {}).items() if v}
        anomalies.extend(_kind_anomalies(fonte, k, e, policy))
        held = None
        if migr:
            held = "schema del Registry da migrare: eseguire la migrazione a mano (school_registry_migrate)"
        elif c.get("missing", 0) > policy["max_missing"]:
            held = "scomparse sopra soglia (%d > %d): controllo umano" % (c["missing"], policy["max_missing"])
        res["kinds"][k] = {"stato": "trattenuto" if held else "simulato", "conteggi": c, "tentativi": readers[k].attempts}
        # identita' dei record che cambiano (non le sole creazioni): servono a chi approva una scrittura
        uids = {cat: list(v) for cat, v in (e.get("uid") or {}).items() if cat in CHANGING and v}
        if uids:
            res["kinds"][k]["uid"] = uids
        if held:
            res["kinds"][k]["motivo"] = held
    if migr:
        anomalies.append(_anomaly("migrazione_schema", "controllo", fonte, None, "schema del Registry da migrare: nulla scritto"))

    to_write = [k for k in kinds if res["kinds"][k]["stato"] != "trattenuto"]
    if commit and to_write:
        expect = sum(res["kinds"][k].get("conteggi", {}).get("create", 0) for k in to_write if res["kinds"][k]["stato"] == "simulato")
        out = S.run({k: memo[k] for k in to_write}, kinds=to_write, commit=True, expect_create=expect, db_path=path, adapter=A)
        log("commit", fonte=fonte, kinds=to_write, esito=mask(out.get("esito")))
        if out.get("mode") == "COMMIT":
            ok_verify = bool((out.get("verifica_post_scrittura") or {}).get("tutto_ok")) and out.get("coerenza_piano_scrittura")
            res["verifica_post_scrittura"] = ok_verify
            res["backup"] = out.get("backup_prima_della_scrittura")
            for k in to_write:
                w = out["esito_scrittura"][k]
                if res["kinds"][k]["stato"] != "simulato":
                    continue                                   # gia' in errore: il run 'error' e' stato registrato
                if w.get("stato") != "ok":
                    res["kinds"][k] = {"stato": "errore", "categoria": classify_error(w.get("errore")), "errore": mask(w.get("errore"))}
                else:
                    res["kinds"][k]["stato"] = "scritto" if ok_verify else "scritto_non_verificato"
                    res["kinds"][k]["conteggi"] = w.get("conteggi") or {}
            if not ok_verify:
                anomalies.append(_anomaly("verifica_post_scrittura:%s" % fonte, "controllo", fonte, None,
                                          "scritto ma la verifica post-scrittura non e' superata: %s" % out.get("esito")))
        else:
            busy = "un'altra sincronizzazione" in str(out.get("esito"))
            for k in to_write:
                if res["kinds"][k]["stato"] == "simulato":
                    res["kinds"][k] = {"stato": "errore", "categoria": "occupato" if busy else "registro",
                                       "errore": mask(out.get("esito"))}
    states = [v["stato"] for v in res["kinds"].values()]
    ok = [s for s in states if s in OK_STATES]
    res["esito"] = "ok" if len(ok) == len(states) else ("fallita" if not ok else "parziale")
    return res, _dedupe(anomalies)


# ------------------------------------------------------------------------------------------------ registro: freschezza
def freshness_summary(path, now, soglia):
    if not os.path.isfile(path):
        return {"errore": "registro assente"}
    try:
        reg = R.Registry.from_snapshot(path)
    except Exception as e:      # noqa: BLE001
        return {"errore": mask(_err(e))}
    try:
        fr = reg.freshness(now.isoformat(), stale_after_hours=soglia)
    finally:
        reg.close()
    stale, fresh = [], []
    for key, v in sorted(fr["per_kind"].items()):
        (stale if v.get("stale") else fresh).append(
            {"tipo": key, "motivo": v.get("motivo"), "eta_ore": v.get("eta_ore"),
             "ultima_lettura_riuscita": v.get("ultima_lettura_riuscita"), "errori_consecutivi": v.get("errori_consecutivi")})
    return {"soglia_ore": soglia, "oltre_soglia": stale, "aggiornati": [f["tipo"] for f in fresh],
            "nota": "conta solo le scritture reali (commit); un dry-run non aggiorna la freschezza"}


# ------------------------------------------------------------------------------------------------ stato persistente
def _write_private(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_state(state_dir):
    try:
        with open(os.path.join(state_dir, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_state_for_update(state_dir, now):
    """Stato da aggiornare in `_finish`: (stato, nota). Assente -> ({}, None). Illeggibile o non oggetto JSON -> il file
    viene CONSERVATO accanto (state.json.illeggibile-<istante>, mai cancellato) e lo stato riparte da zero, dichiarandolo:
    al peggio un problema gia' notificato viene notificato una volta in piu'. Voci di versioni precedenti (es. problemi
    senza 'giri') restano valide; `problemi` non oggetto -> azzerato e dichiarato."""
    p = os.path.join(state_dir, "state.json")
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = None
    if isinstance(st, dict):
        if not isinstance(st.get("problemi", {}), dict):
            st["problemi"] = {}
            return st, "state.json: 'problemi' non valido, azzerato"
        return st, None
    keep = "%s.illeggibile-%s" % (p, now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    try:
        os.replace(p, keep)
        return {}, "state.json illeggibile: conservato come %s, stato ripartito da zero" % os.path.basename(keep)
    except OSError:
        return {}, "state.json illeggibile e non spostabile: stato ripartito da zero"


# ------------------------------------------------------------------------------------------------ notifiche
def osascript_send(title, message):
    """Notifica locale macOS. Testi passati come argomenti (mai interpolati nello script): nessuna iniezione."""
    subprocess.run(["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run", title, message], capture_output=True, timeout=15)


def _failed_kinds(r):
    return sorted(k for k, v in r["kinds"].items() if v["stato"] not in OK_STATES and v.get("categoria") not in NEEDS_USER)


SOURCE_LABEL = {"classeviva": "ClasseViva", "classroom": "Classroom"}
SOURCE_KEYS = ("autorizzazione", "keychain", "parziale", "parziale_temporaneo", "temporaneo")   # chiavi `<tipo>:<fonte>...`


def _key_source(key):
    """Fonte di una chiave di problema (`autorizzazione:classroom`, `temporaneo:classeviva`, `parziale:classroom:a,b`...);
    '*' per i problemi che riguardano tutto il giro (fallito, fallito_temporaneo, freschezza, anomalie)."""
    parts = str(key).split(":")
    return parts[1] if parts[0] in SOURCE_KEYS and len(parts) > 1 and parts[1] else "*"


def failing_sources(summary):
    """Fonti con almeno un tipo non scritto (qualsiasi categoria) o non eseguite: per loro nessuna «risoluzione»."""
    return {f for f, r in (summary.get("fonti") or {}).items()
            if r.get("esito") in ("fallita", "non_eseguita") or any(v["stato"] not in OK_STATES for v in r["kinds"].values())}


def derive_problems(summary, policy=None):
    """{chiave: (titolo, messaggio, giri_minimi)}: solo cio' che richiede attenzione. Chiavi stabili -> nessuna notifica
    ripetuta. Gravita' e persistenza:
      * subito (1 giro): credenziali illeggibili, autorizzazione, errori NON temporanei, controlli richiesti, dati davvero
        vecchi (nessuna lettura riuscita entro la soglia);
      * solo se si ripete (`conferma_giri` giri consecutivi): tipi di una fonte in errore SOLO temporaneo (rete, timeout,
        tempo reale superato con il Mac in stop) - il giro successivo di norma recupera da solo. Una sola chiave per fonte,
        `temporaneo:<fonte>`, che il giro sia PARZIALE o FALLITO: il conteggio dei giri non riparte quando cambia l'esito
        (difetto osservato il 25/10: parziale -> fallito -> parziale di Classroom senza notifica).
    Il riepilogo, il log, l'esito (PARZIALE/FALLITO) e la dashboard restano invariati: si regola solo la notifica.
    «Ultimo run in errore» con dati ancora entro la soglia non e' «dati non aggiornati»: lo copre il problema del giro."""
    confirm = (policy or POLICY).get("conferma_giri", 2)
    p = {}
    temp_only = summary["esito"] == "FALLITO" and not summary.get("motivo")
    any_failed = False
    for fonte, r in summary["fonti"].items():
        for k in _failed_kinds(r):
            any_failed = True
            temp_only = temp_only and r["kinds"][k].get("categoria") == "temporaneo"
    for fonte, r in summary["fonti"].items():
        cats = {v.get("categoria") for v in r["kinds"].values() if v["stato"] == "errore"}
        cats.add((r.get("autorizzazione") or {}).get("categoria"))
        if "keychain" in cats:
            p["keychain:%s" % fonte] = ("Cloud Scuola: credenziali %s non leggibili" % fonte,
                                        "Portachiavi bloccato o voce mancante. Non riautorizzare: verifica prima con "
                                        "%s (docs/automazione-scuola.md)." % ("cls_status" if fonte == "classroom" else "cv_status"))
        elif "autorizzazione" in cats:
            hint = ("riautorizza con `python3 cls_auth.py authorize` (docs/automazione-scuola.md)" if fonte == "classroom"
                    else "credenziali ClasseViva rifiutate: aggiornale con setup.sh")
            p["autorizzazione:%s" % fonte] = ("Cloud Scuola: autorizzazione %s" % fonte, "Accesso non valido: %s." % hint, 1)
        failed = _failed_kinds(r)
        temp = [k for k in failed if r["kinds"][k].get("categoria") == "temporaneo"]
        other = [k for k in failed if k not in temp]
        if temp:
            p["temporaneo:%s" % fonte] = (
                "Cloud Scuola: %s non aggiornato da piu' giri" % SOURCE_LABEL.get(fonte, fonte),
                "%s: non aggiornati %s anche al giro precedente (errori temporanei: rete, tempo scaduto o Mac in stop). "
                "Dettagli: school_job.py status." % (fonte, ", ".join(temp)), confirm)
        if other and summary["esito"] != "FALLITO":
            p["parziale:%s:%s" % (fonte, ",".join(other))] = (
                "Cloud Scuola: sincronizzazione parziale", "%s: non aggiornati %s." % (fonte, ", ".join(other)), 1)
    if summary["esito"] == "FALLITO" and not (temp_only and any_failed):          # soli temporanei: chiavi per fonte
        p["fallito"] = ("Cloud Scuola: sincronizzazione fallita", "Nessun dato aggiornato. Dettagli: school_job.py status.", 1)
    fr = summary.get("freschezza") or {}
    soglia = fr.get("soglia_ore")
    stale = [s for s in fr.get("oltre_soglia") or []
             if not (s.get("motivo") == "ultimo_run_in_errore" and s.get("eta_ore") is not None and soglia is not None
                     and s["eta_ore"] <= soglia)]
    if stale:
        p["freschezza"] = ("Cloud Scuola: dati non aggiornati", "%d tipi senza lettura riuscita entro %s h (o mai letti): %s." % (
            len(stale), soglia, ", ".join(s["tipo"] for s in stale[:4]) + (" ..." if len(stale) > 4 else "")), 1)
    for a in summary.get("anomalie") or []:
        if a["gravita"] == "controllo":
            p["anomalia:%s" % a["chiave"]] = ("Cloud Scuola: controllo richiesto", a["testo"][:180], 1)
    return p


def derive_events(summary, policy=None):
    """Eventi da notificare UNA volta (non problemi aperti: non hanno una «risoluzione»). Oggi: automazione ferma, cioe'
    almeno `giri_mancati_notifica` orari consecutivi senza alcun avvio prima di questo giro (Mac spento o in stop, o
    agente non attivo: il job non lo distingue). Un orario saltato perche' il giro precedente era ancora in corso, o non
    verificabile dal log, non conta."""
    need = (policy or POLICY).get("giri_mancati_notifica", 3)
    seq = (summary.get("automazione") or {}).get("giri_mancati_consecutivi") or []
    if len(seq) < need:
        return {}
    return {"giri_mancati:%s" % seq[0]: (
        "Cloud Scuola: automazione ferma",
        "%d giri programmati senza avvio, dal %s al %s (Mac spento o in stop, o automazione non attiva). Il giro di adesso e' "
        "partito." % (len(seq), seq[0][:16].replace("T", " "), seq[-1][:16].replace("T", " ")))}


def _parse_ts(v):
    try:
        t = dt.datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    return t if t is not None and t.tzinfo else None


def _merge_history(entries):
    """Storia di piu' voci di problema fuse in una (migrazione): inizio piu' vecchio, giri massimi, ultima notifica piu'
    recente. Voci o date non valide ignorate, mai inventate."""
    dals = [(t, e["dal"]) for e in entries if (t := _parse_ts(e.get("dal")))]
    lasts = [(t, e["ultima_notifica"]) for e in entries if (t := _parse_ts(e.get("ultima_notifica")))]
    giri = [e["giri"] for e in entries if isinstance(e.get("giri"), int)] or [1]
    return {"dal": min(dals)[1] if dals else None, "ultima_notifica": max(lasts)[1] if lasts else None, "giri": max(giri)}


def migrate_problem_keys(prev, problems):
    """Compatibilita' (25/10): `parziale_temporaneo:<fonte>`, `fallito_temporaneo` (senza fonte: vale per ogni fonte) e
    `parziale:<fonte>:<tipi>` non piu' prodotti confluiscono in `temporaneo:<fonte>` se quella fonte e' ancora in errore
    temporaneo: la storia (giri, inizio, ultima notifica) passa alla chiave nuova e le vecchie non contano come «risolte».
    Le vecchie voci di una fonte ora senza errori temporanei restano e si chiudono con le regole normali."""
    prev = dict(prev)
    shared = prev.get("fallito_temporaneo") if isinstance(prev.get("fallito_temporaneo"), dict) else None
    adopted_shared = False
    for key in sorted(problems):
        if not key.startswith("temporaneo:") or key in prev:
            continue
        fonte = key.split(":", 1)[1]
        olds = sorted(k for k in prev if isinstance(prev[k], dict) and k not in problems and
                      (k == "parziale_temporaneo:%s" % fonte or k.startswith("parziale:%s:" % fonte)))
        entries = [prev[k] for k in olds] + ([shared] if shared else [])
        if entries:
            prev[key] = _merge_history(entries)
            for k in olds:
                del prev[k]
            adopted_shared = adopted_shared or bool(shared)
    if adopted_shared:
        del prev["fallito_temporaneo"]
    return prev


def update_notifications(state, problems, now, send, renotify_hours, failing=None):
    """Aggiorna lo stato dei problemi e invia solo: problemi nuovi (o confermati dai giri richiesti), problemi aperti da
    oltre renotify_hours, e la chiusura dei problemi notificati. `failing`: fonti con tipi ancora in errore in questo giro.
    Chiusura mai falsa: un problema notificato che scompare viene annunciato solo quando la sua fonte non ha piu' errori
    (problemi di tutto il giro: nessuna fonte in errore) e nessun problema notificato resta aperto; fino ad allora resta in
    `state["risolti_in_attesa"]`, cancellato solo dopo un invio riuscito. Piu' fonti `temporaneo:` in scadenza nello
    stesso giro (stessa causa) = una sola notifica. Ritorna l'elenco delle notifiche (inviate o, senza send, solo calcolate)."""
    failing = set(failing or ())
    prev = migrate_problem_keys(state.get("problemi") or {}, problems)
    cur, sent = {}, []
    for key, spec in sorted(problems.items()):
        title, msg = spec[0], spec[1]
        need = spec[2] if len(spec) > 2 else 1               # giri consecutivi prima della prima notifica
        old = prev.get(key) if isinstance(prev.get(key), dict) else None
        giri = (old.get("giri", 1) if old else 0) + 1        # voci di versioni precedenti: almeno un giro gia' visto
        last = old.get("ultima_notifica") if old else None
        try:
            elapsed = (now - dt.datetime.fromisoformat(last)).total_seconds() if last else None
        except (TypeError, ValueError):
            elapsed = None                                    # data illeggibile: si notifica di nuovo
        due = giri >= need and (elapsed is None or elapsed >= renotify_hours * 3600)
        # 'ultima_notifica' cambia SOLO dopo un invio riuscito (sotto): un giro senza --notify o un invio fallito non
        # deve impedire la notifica al giro successivo
        cur[key] = {"dal": (old or {}).get("dal") or now.isoformat(), "ultima_notifica": last, "titolo": title, "giri": giri}
        if giri < need:
            cur[key]["attesa_conferma"] = need                # tracciato ma non ancora notificato
        if due:
            sent.append(((key,), title, msg))
    temp = [s for s in sent if s[0][0].startswith("temporaneo:")]
    if len(temp) > 1:                                         # stessa causa su piu' fonti: una notifica sola
        names = [SOURCE_LABEL.get(s[0][0].split(":", 1)[1], s[0][0].split(":", 1)[1]) for s in temp]
        sent = [s for s in sent if s not in temp] + [(tuple(k for s in temp for k in s[0]),
                                                      "Cloud Scuola: %s non aggiornati da piu' giri" % " e ".join(names),
                                                      " ".join(s[2] for s in temp))]
    closable = state.get("risolti_in_attesa") if isinstance(state.get("risolti_in_attesa"), dict) else {}
    closable = {k: v for k, v in closable.items() if isinstance(v, str) and k not in cur}
    for k in sorted(set(prev) - set(cur)):                                                  # solo problemi notificati davvero
        if isinstance(prev[k], dict) and prev[k].get("ultima_notifica"):
            closable[k] = prev[k].get("titolo") or k
    ready = sorted(k for k in closable if (_key_source(k) == "*" and not failing) or
                   (_key_source(k) != "*" and _key_source(k) not in failing))
    due_now = {k for s in sent for k in s[0]}
    still_open = [k for k in cur if cur[k].get("ultima_notifica") or k in due_now]            # notificati e ancora aperti
    if ready and not still_open:
        fixed = "; ".join(closable[k] for k in ready[:3])
        if not cur and len(ready) == len(closable):
            sent.append((None, "Cloud Scuola: tutto regolare", "Risolto: %s." % fixed))
        else:                                       # restano solo problemi in osservazione, mai notificati, o in attesa
            watch = "; ".join(cur[k]["titolo"] for k in sorted(cur)[:3])
            sent.append((None, "Cloud Scuola: problema risolto", "Risolto: %s.%s" % (fixed, " In osservazione: %s." % watch
                                                                                      if watch else "")))
    state["problemi"] = cur
    out = []
    for keys, title, msg in sent:
        ok = None
        if send is not None:
            try:
                send(title, msg)
                ok = True
            except Exception:      # noqa: BLE001 - una notifica fallita non fa fallire il job
                ok = False
        if ok and keys is not None:
            for key in keys:
                cur[key]["ultima_notifica"] = now.isoformat()
        if ok and keys is None:
            for k in ready:
                closable.pop(k, None)                         # chiusura annunciata davvero
        out.append({"titolo": title, "messaggio": msg, "inviata": ok})
    if closable:
        state["risolti_in_attesa"] = closable
    else:
        state.pop("risolti_in_attesa", None)
    return out


def update_events(state, events, now, send):
    """Eventi notificati UNA volta per chiave (idempotente): registrati in state['eventi_notificati'] solo dopo un invio
    riuscito, come i problemi; dimenticati dopo EVENTS_KEEP_DAYS. Nessuna «risoluzione»: un evento e' gia' concluso."""
    done = state.get("eventi_notificati")
    done = {k: v for k, v in done.items() if isinstance(v, str)} if isinstance(done, dict) else {}
    for k in list(done):
        try:
            if (now - dt.datetime.fromisoformat(done[k])).days > EVENTS_KEEP_DAYS:
                del done[k]
        except (TypeError, ValueError):
            del done[k]
    out = []
    for key, (title, msg) in sorted(events.items()):
        if key in done:
            continue
        ok = None
        if send is not None:
            try:
                send(title, msg)
                ok = True
            except Exception:      # noqa: BLE001 - una notifica fallita non fa fallire il job
                ok = False
        if ok:
            done[key] = now.isoformat()
        out.append({"titolo": title, "messaggio": msg, "inviata": ok})
    state["eventi_notificati"] = done
    return out


def automation_check(deps, now, mono_s, real_s):
    """Diagnostica dell'automazione per il riepilogo (sola lettura, mai eccezioni):
      * `stop_durante_il_giro_s`: tempo reale oltre il tempo di esecuzione (> STOP_MIN_S = Mac in stop durante il giro);
      * con `deps.schedule` (log, plist): orari delle ultime 48 h secondo cuore_automation - questo giro (orario previsto,
        ritardo: un avvio in ritardo e' tipico del risveglio dallo stop, launchd accorpa gli orari persi), orari mancati
        con la causa distinguibile (giro precedente ancora in corso, oppure nessun avvio: Mac spento o in stop o agente
        non attivo, che il job non distingue), orari non verificabili, e gli orari mancati CONSECUTIVI subito prima di
        questo giro (base dell'evento «automazione ferma»)."""
    out = {"durata_reale_s": round(real_s, 1) if real_s is not None else None,
           "stop_durante_il_giro_s": round(real_s - mono_s) if real_s is not None and real_s - mono_s > STOP_MIN_S else 0}
    if deps.schedule is None:
        out["orari"] = "non controllati"
        return out
    try:
        h = AUTO.history(deps.schedule[0], deps.schedule[1], now)
    except Exception as e:      # noqa: BLE001
        h = {"disponibile": False, "motivo": "storico non leggibile (%s)" % type(e).__name__}
    if not h.get("disponibile"):
        out["orari"] = h.get("motivo") or "non disponibili"
        return out
    rows = h.get("orari") or []
    here = next((i for i in range(len(rows) - 1, -1, -1)
                 if rows[i]["stato"] == "eseguito" and rows[i].get("esito") == "in_corso"), None)
    if here is not None:
        r = rows[here]
        out["questo_giro"] = {"orario_previsto": r["orario"], "ritardo_avvio_min": r.get("ritardo_avvio_min"),
                              "in_ritardo": bool(r.get("in_ritardo"))}
    else:
        out["questo_giro"] = {"orario_previsto": None, "nota": "fuori calendario o gia' attribuito a un altro avvio"}
    out["orari_mancati_48h"] = [{"orario": r["orario"], "causa": "giro precedente ancora in corso" if r.get("sovrapposto")
                                 else "nessun avvio (Mac spento o in stop, o automazione non attiva)"}
                                for r in rows if r["stato"] == "mancato"]
    out["orari_non_verificabili_48h"] = sum(1 for r in rows if r["stato"] == "non_verificabile")
    # sequenza prima di QUESTO giro; se non e' attribuito a un orario, prima dell'ultimo orario eseguito: cosi' un avviso
    # non inviato (senza --notify o invio fallito) si ritrova al giro successivo, e la deduplica evita le ripetizioni
    ref = here if here is not None else next((i for i in range(len(rows) - 1, -1, -1) if rows[i]["stato"] == "eseguito"), None)
    seq = []
    for r in reversed(rows[:ref] if ref is not None else rows):
        if r["stato"] == "mancato" and not r.get("sovrapposto"):
            seq.append(r["orario"])
        else:
            break
    out["giri_mancati_consecutivi"] = seq[::-1]
    return out


# ------------------------------------------------------------------------------------------------ log
class JsonLog:
    """Righe JSON con rotazione (1 MB x 5). Nessun segreto: i testi passano da mask()."""

    def __init__(self, path):
        self.logger = logging.getLogger("school_job.%s" % id(self))
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        if path:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(h)
            self.handler = h
        else:
            self.handler = None

    def __call__(self, evento, **kw):
        rec = {"ts": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "evento": evento}
        rec.update(kw)
        self.logger.info(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str))

    def close(self):
        if self.handler:
            self.handler.close()
            self.logger.removeHandler(self.handler)


# ------------------------------------------------------------------------------------------------ job
def overall(fonti, busy=False):
    if busy:
        return "OCCUPATO"
    states, auth = [], False
    for r in fonti.values():
        states.extend(v["stato"] for v in r["kinds"].values())
        if (r.get("autorizzazione") or {}).get("categoria") in NEEDS_USER or any(
                v.get("categoria") in NEEDS_USER for v in r["kinds"].values()):
            auth = True
    ok = [s for s in states if s in OK_STATES]
    if auth:
        return "AUTORIZZAZIONE"
    if not ok:
        return "FALLITO"
    return "OK" if len(ok) == len(states) else "PARZIALE"


def source_order(path, sources, kinds_of, now):
    """Prima la fonte con la lettura riuscita (commit) PIU' VECCHIA tra i suoi tipi, a parita' l'ordine ORDER.
    In un giro normale ClasseViva e' letta prima di Classroom, quindi resta prima; se un giro ha lasciato indietro una
    fonte (es. tipi saltati per il tempo reale), al giro successivo quella fonte parte per prima. Sola lettura (copia in
    memoria del Registry); se la freschezza non si legge si usa ORDER e lo si dichiara."""
    base = {f: (ORDER.index(f) if f in ORDER else len(ORDER), i) for i, f in enumerate(sources)}
    try:
        reg = R.Registry.from_snapshot(path)
        try:
            per = reg.freshness(now.isoformat())["per_kind"]
        finally:
            reg.close()
    except Exception as e:      # noqa: BLE001
        return {"ordine": sorted(sources, key=lambda f: base[f]), "criterio": "predefinito",
                "nota": "freschezza non leggibile: %s" % mask(_err(e), 120)}

    def oldest(f):
        ts = []
        for k in kinds_of[f]:
            v = (per.get("%s/%s" % (f, k)) or {}).get("ultima_lettura_riuscita")
            try:
                t = dt.datetime.fromisoformat(v) if v else None
            except ValueError:
                t = None
            ts.append(t.timestamp() if t and t.tzinfo else float("-inf"))     # mai letto o illeggibile = piu' vecchio
        return min(ts) if ts else float("-inf")
    return {"ordine": sorted(sources, key=lambda f: (oldest(f), base[f])), "criterio": "lettura_riuscita_piu_vecchia"}


def run_job(sources=ORDER, commit=False, db_path=None, state_dir=None, policy=None, deps=None, log=None, kinds=None):
    pol = dict(POLICY, **(policy or {}))
    deps = deps or Deps()
    log = log or (lambda *a, **k: None)
    path = db_path or R.default_path()
    state_dir = state_dir or default_state_dir()
    t0, start = deps.monotonic(), deps.now()
    try:
        r0 = deps.real_clock()                       # tempo reale (stop compreso): stop del Mac durante il giro
    except Exception:      # noqa: BLE001
        r0 = None
    summary = {"schema": SCHEMA, "modo": "commit" if commit else "dry-run", "inizio": start.isoformat(), "fonti": {},
               "anomalie": [], "registro": {"percorso": path}, "scritture": {"registro": False, "classeviva": False,
                                                                                "classroom": False}}
    log("avvio", modo=summary["modo"], fonti=list(sources))
    lock_dir = os.path.dirname(path)
    if not os.path.isfile(path):
        summary.update(esito="FALLITO", motivo="registro assente: il job non crea mai un registro (prima sync a mano)")
        for f in sources:
            summary["fonti"][f] = {"esito": "non_eseguita", "kinds": {}, "autorizzazione": None}
        return _finish(summary, state_dir, deps, pol, t0, log, path, r0)
    try:
        with S._Lock(os.path.join(lock_dir, "school-job.lock")):
            deadline = t0 + pol["deadline_s"]
            ks_of = {f: list((kinds or {}).get(f) or SOURCES[f][1]) for f in sources}
            summary["ordine_fonti"] = source_order(path, sources, ks_of, deps.now())
            for f in summary["ordine_fonti"]["ordine"]:
                expired = RealDeadline(deps.real_clock, pol["real_deadline_s"])     # budget PER FONTE: nessuna
                res, anom = run_source(f, deps, ks_of[f], commit, path, pol, deadline, log, expired)   # fonte affamata
                summary["fonti"][f] = res
                summary["anomalie"].extend(anom)
                if any(v["stato"] in ("scritto", "scritto_non_verificato") for v in res["kinds"].values()):
                    summary["scritture"]["registro"] = True
                log("fonte", fonte=f, esito=res["esito"],
                    kinds={k: {x: v.get(x) for x in ("stato", "categoria", "conteggi", "motivo")} for k, v in res["kinds"].items()})
            summary["esito"] = overall(summary["fonti"])
    except S.SyncBusy:
        summary.update(esito="OCCUPATO", motivo="un altro job e' in corso (school-job.lock): nulla eseguito")
    return _finish(summary, state_dir, deps, pol, t0, log, path, r0)


def _finish(summary, state_dir, deps, pol, t0, log, path, r0=None):
    now = deps.now()
    summary["freschezza"] = freshness_summary(path, now, pol["soglia_ore"])
    summary["fine"] = now.isoformat()
    summary["durata_s"] = round(deps.monotonic() - t0, 1)
    summary["exit_code"] = EXIT[summary["esito"]]
    try:
        real_s = deps.real_clock() - r0 if r0 is not None else None
    except Exception:      # noqa: BLE001
        real_s = None
    summary["automazione"] = automation_check(deps, now, deps.monotonic() - t0, real_s)
    if summary["esito"] != "OCCUPATO":          # un job concorrente non deve alterare (ne' spostare) lo stato dei problemi
        state, note = load_state_for_update(state_dir, now)
        if note:
            summary["anomalie"].append(_anomaly("stato_job_illeggibile", "info", None, None, note))
        state.pop("notion", None)                # stato storico dell'integrazione Notion, rimossa: non piu' conservato
        summary["notifiche"] = update_notifications(state, derive_problems(summary, pol), now, deps.notify_send,
                                                    pol["renotify_hours"], failing=failing_sources(summary))
        summary["notifiche"] += update_events(state, derive_events(summary, pol), now, deps.notify_send)
        summary["problemi_in_osservazione"] = sorted(k for k, v in state["problemi"].items() if v.get("attesa_conferma"))
        state["ultimo"] = {k: summary[k] for k in ("inizio", "fine", "modo", "esito", "exit_code")}
        if summary["modo"] == "commit" and summary["esito"] == "OK":
            state["ultimo_commit_ok"] = summary["fine"]
        _write_private(os.path.join(state_dir, "state.json"), state)
        _write_private(os.path.join(state_dir, "last_summary.json"), summary)
    au = summary["automazione"]
    log("fine", esito=summary["esito"], exit_code=summary["exit_code"], durata_s=summary["durata_s"],
        anomalie=[a["chiave"] for a in summary["anomalie"]],
        oltre_soglia=[s["tipo"] for s in (summary["freschezza"].get("oltre_soglia") or [])],
        durata_reale_s=au.get("durata_reale_s"), stop_durante_il_giro_s=au.get("stop_durante_il_giro_s"),
        orari_mancati=len(au.get("orari_mancati_48h") or []))
    return summary


def render(summary):
    lines = ["Cloud Scuola job: %s (%s, uscita %d, %.0f s)" % (summary["esito"], summary["modo"], summary["exit_code"],
                                                                summary.get("durata_s") or 0)]
    if summary.get("motivo"):
        lines.append("  " + summary["motivo"])
    for f, r in summary["fonti"].items():
        a = r.get("autorizzazione") or {}
        lines.append("  %s: %s%s" % (f, r["esito"], " (autorizzazione: %s)" % a.get("categoria", a.get("esito")) if a else ""))
        for k, v in r["kinds"].items():
            det = v.get("conteggi") or v.get("motivo") or "%s: %s" % (v.get("categoria"), v.get("errore"))
            lines.append("    %-13s %-22s %s" % (k, v["stato"], det))
            for cat, ids in sorted((v.get("uid") or {}).items()):
                lines.append("      %s: %s" % (cat, ", ".join(ids[:10]) + (" ..." if len(ids) > 10 else "")))
    for a in summary["anomalie"]:
        lines.append("  [%s] %s" % (a["gravita"], a["testo"]))
    fr = summary.get("freschezza") or {}
    if fr.get("oltre_soglia"):
        lines.append("  oltre %s h: %s" % (fr["soglia_ore"], ", ".join(s["tipo"] for s in fr["oltre_soglia"])))
    au = summary.get("automazione") or {}
    if au:
        q = au.get("questo_giro") or {}
        parts = ["orario %s" % q["orario_previsto"][11:16] if q.get("orario_previsto") else "fuori calendario",
                 "avvio +%s min" % q["ritardo_avvio_min"] if q.get("ritardo_avvio_min") else "",
                 "stop del Mac durante il giro: %d min" % (au["stop_durante_il_giro_s"] // 60) if au.get("stop_durante_il_giro_s") else "",
                 "orari mancati (48 h): %d" % len(au["orari_mancati_48h"]) if au.get("orari_mancati_48h") else "",
                 au["orari"] if isinstance(au.get("orari"), str) else ""]
        lines.append("  automazione: " + ", ".join(x for x in parts if x))
    if summary.get("problemi_in_osservazione"):
        lines.append("  in osservazione (notifica se si ripete): " + ", ".join(summary["problemi_in_osservazione"]))
    if summary.get("invio_cloud"):
        c = summary["invio_cloud"]
        lines.append("  invio al cloud: %s%s" % (c.get("esito"), " (%s)" % c["motivo"] if c.get("motivo") else ""))
    return "\n".join(lines)


def cloud_push_step(state_dir, db_path, log):
    """Passo FACOLTATIVO (blocco E3): invio del feed School al cloud, SPENTO per difetto (school_cloud_push.py). Senza
    `cuore-cloud.json` nella cartella di stato non fa nulla (nessun log, nessuna rete). Non cambia mai esito, codice
    d'uscita o stato del job: un errore o un'interruzione diventano solo l'esito dell'invio."""
    try:
        import school_cloud_push as CP
        return CP.job_step(state_dir, registry_path=db_path, log=log)
    except (Exception, KeyboardInterrupt) as e:      # noqa: BLE001
        return {"esito": "errore", "motivo": "interrotto" if isinstance(e, KeyboardInterrupt) else "imprevisto"}


def check_due(state, now, ore=3.0, richiesta=None, pausa_h=1.0, idle_s=0.0):
    """`check` (ogni 5 minuti dal LaunchAgent, anche al risveglio del Mac): perche' fare un giro adesso, o None.
    - una richiesta dal telefono piu' recente dell'inizio dell'ultimo giro -> subito;
    - ultimo giro riuscito piu' vecchio di `ore` -> si', solo con il Mac in uso (`idle_s`: secondi senza tastiera o
      mouse, al massimo 300; None = non noto) e non prima di `pausa_h` dall'ultimo tentativo (anche fallito): nei brevi
      risvegli a coperchio chiuso un giro avanzerebbe a scatti per ore, e una fonte che non risponde non lo fa
      ripartire ogni 5 minuti."""
    def ts(v):
        try:
            return dt.datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v).timestamp() if isinstance(v, str) else None
        except ValueError:
            return None
    t = now.timestamp()
    tentativo = ts((state.get("ultimo") or {}).get("inizio"))
    ok = ts(state.get("ultimo_commit_ok"))
    r = ts(richiesta)
    if r is not None and (tentativo is None or r > tentativo):
        return "richiesta_dal_telefono"
    if idle_s is None or idle_s > 300:
        return None
    if ok is not None and t - ok < ore * 3600:
        return None
    if tentativo is not None and t - tentativo < pausa_h * 3600:
        return None
    return "dati_vecchi" if ok is not None else "mai_riuscito"


def main(argv=None, deps=None):
    ap = argparse.ArgumentParser(prog="school_job.py", description="Job di sincronizzazione Scuola (default: dry-run)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--commit", action="store_true", help="scrive nel Registry (senza: dry-run, nulla scritto)")
    r.add_argument("--sources", default=",".join(ORDER))
    r.add_argument("--notify", action="store_true", help="notifiche locali macOS (deduplicate)")
    r.add_argument("--max-missing", type=int, default=POLICY["max_missing"])
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("check", help="giro (commit, notifiche) solo se serve: richiesta dal telefono o dati vecchi")
    c.add_argument("--ore", type=float, default=3.0)
    for p in (r, c, sub.add_parser("status")):
        p.add_argument("--db", default=None)
        p.add_argument("--state-dir", default=None)
    for p in (r, c):
        p.add_argument("--log-file", default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    os.umask(0o077)
    if args.cmd == "status":
        st = load_state(args.state_dir or default_state_dir())
        try:
            with open(os.path.join(args.state_dir or default_state_dir(), "last_summary.json")) as f:
                print(render(json.load(f)))
        except (OSError, ValueError):
            print("nessuna esecuzione registrata")
        print(json.dumps({k: st.get(k) for k in ("ultimo", "ultimo_commit_ok", "problemi")}, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "check":
        sd = args.state_dir or default_state_dir()
        import school_cloud_push as CP
        import school_recovery as SR
        perche = check_due(load_state(sd), dt.datetime.now(dt.timezone.utc), args.ore, CP.pending_request(sd),
                           idle_s=SR.idle_seconds())
        if perche is None:
            return 0                                     # niente da fare: nessuna rete, nessun log
        args.commit, args.notify, args.sources, args.max_missing, args.json = True, True, ",".join(ORDER), POLICY["max_missing"], False
    sources = [s for s in args.sources.split(",") if s]
    if not sources or any(s not in SOURCES for s in sources):
        print("fonti ammesse: %s" % ", ".join(ORDER), file=sys.stderr)
        return EXIT_USAGE
    deps = deps or Deps()
    if args.notify and deps.notify_send is None:
        deps.notify_send = osascript_send
    log = JsonLog(args.log_file or default_log_file())
    if deps.schedule is None:       # orari mancati: log e calendario reali SOLO da qui (il LaunchAgent esegue `run`)
        deps.schedule = (args.log_file or default_log_file(),
                         os.path.expanduser(os.environ.get("CUORE_SCHOOL_JOB_PLIST") or AUTO.PLIST_DEFAULT))

    def on_term(signum, frame):     # launchd: SIGTERM -> uscita ordinata (ogni tipo e' una transazione: nulla di parziale)
        raise KeyboardInterrupt()
    old = signal.signal(signal.SIGTERM, on_term)
    try:
        summary = run_job([s for s in ORDER if s in sources], commit=args.commit, db_path=args.db, state_dir=args.state_dir,
                          policy={"max_missing": args.max_missing}, deps=deps, log=log)
        if args.commit and summary["esito"] != "OCCUPATO":         # E3, facoltativo: senza configurazione non fa nulla
            cloud = cloud_push_step(args.state_dir or default_state_dir(), args.db, log)
            if cloud is not None:
                summary["invio_cloud"] = cloud
    except KeyboardInterrupt:
        log("interrotto")
        print("interrotto: ogni tipo gia' scritto e' una transazione completa; rilancia il job", file=sys.stderr)
        return EXIT["FALLITO"]
    finally:
        signal.signal(signal.SIGTERM, old)
        log.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1) if args.json else render(summary))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
