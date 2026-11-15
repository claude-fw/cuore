"""Scritture del titolare dalla dashboard: parti comuni ai due ponti e scrittore dei dati reali (D2-reale, E4-reale).

Comuni (usate anche dal ponte di prova): elenco delle operazioni, lettura della richiesta POST (solo JSON, al massimo
64 KiB), processo Node con ambiente ridotto, traduzione degli esiti, operazioni School sull'archivio personale.

`Scrittore` (solo ponte reale, `serve.py --scritture`): Finance con `finance/scripts/scrivi.ts` sul database indicato
(backup verificato prima della prima scrittura del giorno, lo fa lo script), School nell'archivio personale fuori da
iCloud e dal repository (cartella 0700, file 0600); impostazioni dei promemoria (`promemoria.json`, scrittura atomica
0600, unica fonte del motore `promemoria.py`) e appunti Finance (`finance_appunti.py`, archivio 0700/0600 separato dal
database Finance). Una richiesta alla volta; ogni scrittura porta un `requestId`:
la stessa richiesta ripetuta riceve la stessa risposta (`ripetuta: true`) senza rieseguire nulla, un `requestId` gia'
usato con un contenuto diverso e' rifiutato (409). Oltre la vita del processo valgono le difese del motore:
`requestId` nei movimenti manuali di Finance, `richiesta` negli eventi School. Nel registro solo operazione, esito e
conteggi: mai importi, descrizioni o testi.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import finance_appunti  # noqa: E402
import promemoria  # noqa: E402
import school_personale  # noqa: E402

REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
FINANCE = os.path.join(REPO_ROOT, "finance")
SCRIVI = os.path.join(FINANCE, "scripts", "scrivi.ts")
MAX_BODY = 64 * 1024
TIMEOUT_S = 120
FINANCE_OPS = ("consulta", "aggiungi_manuale", "correggi_manuale", "annulla_manuale", "storico_manuale", "imposta_budget",
               "stato_budget", "rileva_abbinamenti", "conferma_abbinamento", "rifiuta_abbinamento", "revoca_abbinamento",
               "crea_conto", "classifica", "aggiungi_acquisto", "segna_acquisto")
SCHOOL_OPS = ("school_stato", "school_scrivi")
PROMEMORIA_OPS = ("promemoria_stato", "promemoria_imposta", "appunti_stato", "appunti_scrivi")
LOCAL_OPS = SCHOOL_OPS + PROMEMORIA_OPS          # eseguite da Python, non da Node
OPS = FINANCE_OPS + LOCAL_OPS
READ_OPS = frozenset({"consulta", "storico_manuale", "stato_budget", "school_stato", "promemoria_stato", "appunti_stato"})
EXIT_STATUS = {0: 200, 2: 403, 64: 400}
CODE_STATUS = {"non_valido": 422, "non_trovato": 404}
INPUT_ERRORS = (school_personale.NonValido, finance_appunti.NonValido, promemoria.ConfigNonValida)
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$")


def read_request(headers, rfile):
    """(richiesta, None) oppure (None, (stato HTTP, documento)): solo JSON, lunghezza dichiarata, operazione nota."""
    if (headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
        return None, (415, {"ok": False, "errore": "tipo_non_ammesso"})
    try:
        size = int(headers.get("Content-Length") or "-1")
    except ValueError:
        size = -1
    if size < 0:
        return None, (400, {"ok": False, "errore": "lunghezza_mancante"})
    if size > MAX_BODY:
        return None, (413, {"ok": False, "errore": "richiesta_troppo_grande"})
    try:
        req = json.loads(rfile.read(size).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, (400, {"ok": False, "errore": "richiesta_non_valida"})
    if not isinstance(req, dict) or req.get("op") not in OPS or not isinstance(req.get("input", {}), dict):
        return None, (400, {"ok": False, "errore": "operazione_sconosciuta"})
    return req, None


def run_node(cmd, stdin, env, run=subprocess.run, timeout=TIMEOUT_S):
    """Processo Node con ambiente ridotto: (codice di uscita, documento JSON | None)."""
    try:
        r = run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env, cwd=HERE)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, {"ok": False, "errore": "errore", "motivo": "processo non completato (%s)" % type(e).__name__}
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        return r.returncode, None


def node_status(code, doc):
    if doc is None:
        return 500, {"ok": False, "errore": "risposta_non_valida"}
    return EXIT_STATUS.get(code) or CODE_STATUS.get(doc.get("codice"), 500), doc


def school_op(path, op, i, richiesta=None):
    if op == "school_stato":
        return school_personale.stato(path)
    return school_personale.aggiungi(path, i, richiesta=richiesta)


def promemoria_op(settings_path, notes_path, op, i, richiesta=None, now=None):
    """Impostazioni dei promemoria e appunti Finance (stesse regole nei due ponti). Errori d'ingresso: ValueError."""
    if op == "promemoria_stato":
        return promemoria.settings_status(settings_path, now)
    if op == "promemoria_imposta":
        return promemoria.update_settings(settings_path, i, now)
    if op == "appunti_scrivi":
        return finance_appunti.aggiungi(notes_path, i, richiesta=richiesta)
    st, now = finance_appunti.stato(notes_path), now or promemoria._now()
    settings = promemoria.read_settings(settings_path)[0] if settings_path else None
    for a in st["appunti"]:
        t = promemoria.appunto_send(settings, a["quando"], now) if settings and a["stato"] == "aperto" and a["promemoria"] else None
        a["invio_previsto"] = t.isoformat() if t else None
    return st


# ------------------------------------------------------------------------------------------------ ponte reale
class Rifiuto(Exception):
    """Rifiuto di sicurezza (403)."""


_inside = promemoria._inside                      # usato anche da backup_settimanale.py


def _private(path, kind, label="archivio School", strict=True):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR if kind == "dir" else stat.S_ISREG)(st.st_mode):
        raise Rifiuto("%s: %s non valido" % (label, "cartella" if kind == "dir" else "file"))
    if st.st_uid != os.getuid() or (strict and st.st_mode & 0o077) or (kind == "file" and st.st_nlink != 1):
        raise Rifiuto("%s: proprietario, permessi o collegamenti non ammessi" % label)


def archive(path, create=False, label="archivio School", ext=".jsonl", strict=True):
    """Percorso verificato di un archivio personale (School, appunti Finance) o delle impostazioni dei promemoria.
    Fuori da iCloud e dal repository; cartella 0700 creata alla prima scrittura; `strict` = cartella e file senza
    permessi per altri (le impostazioni, copiabili a mano, si controllano solo per posizione, tipo e proprietario)."""
    try:
        promemoria.outside_cloud_and_repo(path, ext, label)
    except promemoria.ConfigNonValida as e:
        raise Rifiuto(str(e)) from None
    d = os.path.dirname(path)
    if not os.path.lexists(d):
        if not create:
            return path
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    _private(d, "dir", label, strict)
    if os.path.lexists(path):
        _private(path, "file", label, strict)
    return path


def school_archive(path, create=False):
    """Archivio personale School: file 0600 creato da `school_personale`."""
    return archive(path, create)


def node_env():
    return {"LANG": "C.UTF-8", "TZ": "Europe/Rome", "TMPDIR": tempfile.gettempdir()}


def finance_command(node, db):
    """`scrivi.ts esegui` con il modello dei permessi di Node: lettura del codice, del database (e dei file accanto,
    solo per controllarne lo stato) e della cartella dei backup, scrittura solo nella cartella dei backup (`/*`: Node
    la vuole per una cartella che potrebbe non esistere ancora). Il database lo protegge `scrivi.ts` (node:sqlite non e'
    vincolato dal modello dei permessi)."""
    real = os.path.realpath(db)
    backup = os.path.join(os.path.dirname(os.path.dirname(real)), "backup")
    reads = [FINANCE, real] + [real + x for x in ("-wal", "-shm", "-journal")] + [backup, backup + "/*"]
    return [node, "--permission"] + ["--allow-fs-read=%s" % p for p in reads] + [
        "--allow-fs-write=%s" % backup, "--allow-fs-write=%s/*" % backup, SCRIVI, "esegui", "--db", real]


def fingerprint(op, i):
    return hashlib.sha256(json.dumps([op, i], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def stderr_log(line):
    print(line, file=sys.stderr, flush=True)


class Scrittore:
    """Esecutore delle operazioni sui dati REALI (Finance e School), una alla volta."""

    def __init__(self, db, school_path, node, run=subprocess.run, log=stderr_log, memory=500, settings_path=None,
                 notes_path=None):
        self.db, self.school_path, self.node, self.run, self.log = db, school_path, node, run, log
        self.settings_path, self.notes_path = settings_path, notes_path
        self.lock = threading.Lock()
        self.memory, self.done = memory, collections.OrderedDict()

    def locked(self, fn):
        """Esegue `fn` (es. la lettura del feed) senza sovrapporsi a una scrittura."""
        with self.lock:
            return fn()

    def capabilities(self):
        return {"schema": "cuore.scritture/1", "attive": True, "dati": "reali", "operazioni": list(OPS),
                "finance": {"backup": "verificato prima della prima scrittura del giorno"},
                "school": {"archivio": "personale, separato dal Registry"},
                "promemoria": {"impostazioni": self.settings_path is not None, "appunti_finance": self.notes_path is not None}}

    def execute(self, req):
        op, i = req["op"], req.get("input", {})
        rid = req.get("requestId")
        if op not in READ_OPS and not (isinstance(rid, str) and REQUEST_ID.match(rid)):
            return self._end(op, 400, {"ok": False, "errore": "requestId_mancante"})
        fp = fingerprint(op, i)
        with self.lock:
            if op not in READ_OPS and rid in self.done:
                seen_fp, status, doc = self.done[rid]
                if seen_fp != fp:
                    return self._end(op, 409, {"ok": False, "errore": "requestId_gia_usato"})
                return self._end(op, status, dict(doc, ripetuta=True))
            status, doc = self._school(op, i, rid) if op in SCHOOL_OPS else self._promemoria(op, i, rid) \
                if op in PROMEMORIA_OPS else self._finance(op, i, rid)
            if status == 200 and op not in READ_OPS:
                self.done[rid] = (fp, status, doc)
                while len(self.done) > self.memory:
                    self.done.popitem(last=False)
        return self._end(op, status, doc)

    def _school(self, op, i, rid):
        try:
            path = school_archive(self.school_path, create=op != "school_stato")
            return 200, {"ok": True, "risultato": school_op(path, op, i, richiesta=rid)}
        except Rifiuto as e:
            return 403, {"ok": False, "errore": "rifiutato", "motivo": str(e)}
        except school_personale.NonValido as e:
            return 422, {"ok": False, "errore": "non_valido", "messaggio": str(e)}

    def _promemoria(self, op, i, rid):
        try:
            if self.settings_path is None or self.notes_path is None:
                raise Rifiuto("promemoria e appunti non configurati in questo ponte")
            settings = archive(self.settings_path, create=op == "promemoria_imposta", label="impostazioni promemoria",
                               ext=".json", strict=False)
            notes = archive(self.notes_path, create=op == "appunti_scrivi", label="appunti Finance")
            return 200, {"ok": True, "risultato": promemoria_op(settings, notes, op, i, richiesta=rid)}
        except Rifiuto as e:
            return 403, {"ok": False, "errore": "rifiutato", "motivo": str(e)}
        except INPUT_ERRORS as e:
            return 422, {"ok": False, "errore": "non_valido", "messaggio": str(e)}

    def _finance(self, op, i, rid):
        if op == "aggiungi_manuale" and "requestId" not in i:
            i = dict(i, requestId=rid)
        code, doc = run_node(finance_command(self.node, self.db), json.dumps({"op": op, "input": i}), node_env(),
                             run=self.run)
        return node_status(code, doc)

    def _end(self, op, status, doc):
        extra = "".join(" %s=%s" % (k, doc[k]) for k in ("backup", "ripetuta") if k in doc)
        self.log("CUORE scrittura: op=%s esito=%d%s" % (op, status, extra))
        return status, doc
