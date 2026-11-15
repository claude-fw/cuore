"""Client ClasseViva minimale, SOLA LETTURA, solo libreria standard (Python >= 3.9).

API REST non ufficiale (ricostruita dalla community) di web.spaggiari.eu.
Credenziali: macOS Keychain (servizio "cloud.classeviva"); mai su disco, mai nei log.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import unicodedata
import urllib.error
import urllib.request

import cvv_subject as subj

BASE = os.environ.get("CLASSEVIVA_BASE", "https://web.spaggiari.eu/rest/v1")
KEYCHAIN_SERVICE = "cloud.classeviva"
# Intestazioni richieste dall'API (profilo app Android, poi profilo storico "zorro").
PROFILES = [
    {"User-Agent": "CVVS/std/4.1.7 Android/10", "Z-Dev-Apikey": "Tg1NWEwNGIgIC0K"},
    {"User-Agent": "zorro/1.0", "Z-Dev-Apikey": "+zorro+"},
]
TEST_RE = re.compile(
    r"\b(verific\w*|compit\w* in classe|interrogazion\w*|test|prova (scritta|orale|di)|"
    r"scritto|simulazione|esame|recupero)\b", re.I)


class CVError(Exception):
    pass


def norm(s):
    """minuscolo senza accenni/accenti, per confronti di materia."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def load_credentials():
    """Legge (utente, password) dal Keychain di macOS. Variabili d'ambiente solo per i test."""
    if os.environ.get("CLASSEVIVA_USER") and os.environ.get("CLASSEVIVA_PASS"):
        return os.environ["CLASSEVIVA_USER"], os.environ["CLASSEVIVA_PASS"]
    try:
        meta = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
                              capture_output=True, text=True, timeout=30)
        m = re.search(r'"acct"<blob>="([^"]*)"', meta.stdout)
        if meta.returncode != 0 or not m:
            raise CVError("Credenziali non trovate nel Keychain (servizio %s). "
                          "Esegui mcp/classeviva/setup.sh." % KEYCHAIN_SERVICE)
        user = m.group(1)
        pw = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                             "-a", user, "-w"], capture_output=True, text=True, timeout=60)
        if pw.returncode != 0:
            raise CVError("Accesso al Keychain negato o annullato.")
        return user, pw.stdout.rstrip("\n")
    except FileNotFoundError:
        raise CVError("Comando 'security' non disponibile (serve macOS).")


def school_year_range(ref):
    """Anno scolastico (1 settembre - 31 agosto) che contiene la data 'ref'. ClasseViva accetta
    solo date in questo intervallo (verificato: 'dates must be between 20260901 and 20270831')."""
    y = ref.year if ref.month >= 9 else ref.year - 1
    return dt.date(y, 9, 1), dt.date(y + 1, 8, 31)


def parse_valid_range(message):
    """Estrae l'intervallo valido dal messaggio d'errore di ClasseViva, se presente."""
    m = re.search(r"bet?wee?n (\d{8}) and (\d{8})", str(message))
    if not m:
        return None
    f = lambda t: dt.datetime.strptime(t, "%Y%m%d").date()
    return f(m.group(1)), f(m.group(2))


class Client:
    def __init__(self, user, password):
        self._user, self._pw = user, password
        self._token = None
        self._profile = None
        self.student_id = None
        self.first_name = None
        self.expire = None

    # ---- HTTP ----
    def _http(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(os.environ.get("CLASSEVIVA_BASE", BASE) + path, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")[:300]
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}
        except urllib.error.URLError as e:
            raise CVError("Rete non raggiungibile: %s" % e.reason)

    # ---- autenticazione ----
    def login(self):
        last = None
        for prof in PROFILES:
            code, js = self._http("POST", "/auth/login",
                                  {"ident": None, "pass": self._pw, "uid": self._user}, prof)
            if code == 200 and js.get("token"):
                self._token, self._profile = js["token"], prof
                ident = js.get("ident") or self._user
                self.student_id = re.sub(r"\D", "", ident) or re.sub(r"\D", "", self._user)
                self.first_name = js.get("firstName")
                self.expire = js.get("expire")
                return True
            last = (code, js)
        code, js = last
        hint = {401: "credenziali rifiutate", 403: "accesso API negato (account studente non abilitato?)",
                422: "credenziali non valide o account non abilitato all'API"}.get(code, "errore")
        raise CVError("Login ClasseViva fallito: HTTP %s - %s. %s" % (
            code, hint, str(js.get("info") or js.get("error") or "")[:160]))

    def _get(self, path, _retry=True):
        if not self._token:
            self.login()
        h = dict(self._profile, **{"Z-Auth-Token": self._token})
        code, js = self._http("GET", path, None, h)
        if code in (401, 403) and _retry:
            self._token = None
            return self._get(path, _retry=False)
        if code != 200:
            raise CVError("GET %s -> HTTP %s %s" % (path.split("?")[0], code,
                          str(js.get("info") or js.get("raw") or js)[:160]))
        return js

    # ---- dati grezzi ----
    def _ensure_login(self):
        if not self.student_id:
            self.login()

    def _list(self, path, key):
        """GET che esige la chiave attesa: una risposta senza chiave NON e' 'zero risultati'."""
        js = self._get(path)
        if not isinstance(js.get(key), list):
            raise CVError("Risposta inattesa da %s: chiave '%s' assente o non elenco (chiavi presenti: %s)" % (
                path.split("?")[0], key, ", ".join(sorted(js))[:120] or "nessuna"))
        return js[key]

    def agenda(self, begin, end, code="all"):
        self._ensure_login()
        """Agenda tra due date. code='all' oppure un codice evento (es. 'AGHW' = compiti).
        Finestre da <=31 giorni, unite senza duplicati."""
        out, seen, cur = [], set(), begin
        while cur <= end:
            stop = min(cur + dt.timedelta(days=30), end)
            for ev in self._list("/students/%s/agenda/%s/%s/%s" % (
                    self.student_id, code, cur.strftime("%Y%m%d"), stop.strftime("%Y%m%d")), "agenda"):
                if ev.get("evtId") in seen:
                    continue
                seen.add(ev.get("evtId"))
                out.append(ev)
            cur = stop + dt.timedelta(days=1)
        return out

    def lessons(self, begin, end):
        self._ensure_login()
        return self._list("/students/%s/lessons/%s/%s" % (
            self.student_id, begin.strftime("%Y%m%d"), end.strftime("%Y%m%d")), "lessons")

    def periods(self):
        self._ensure_login()
        return self._list("/students/%s/periods" % self.student_id, "periods")

    def subjects(self):
        self._ensure_login()
        return self._list("/students/%s/subjects" % self.student_id, "subjects")

    def grades(self):
        self._ensure_login()
        return self._list("/students/%s/grades" % self.student_id, "grades")

    def noticeboard(self):
        self._ensure_login()
        return self._list("/students/%s/noticeboard" % self.student_id, "items")

    def absences(self):
        self._ensure_login()
        return self._list("/students/%s/absences/details" % self.student_id, "events")


# ---- normalizzazione (pura, testabile senza rete) ----
def classify(code, text):
    """Tipo dedotto dall'evento. 'inferito' perché ClasseViva non ha un campo 'verifica'."""
    if code == "AGHW":
        return "compito"
    if TEST_RE.search(text or ""):
        return "verifica"
    return "evento"


def _iso(s):
    return (s or "")[:19]


def normalize_event(ev):
    text = re.sub(r"\s+", " ", (ev.get("notes") or "")).strip()
    code = ev.get("evtCode")
    kind = classify(code, text)
    subject = None  # la materia si risolve in enrich_event(); qui solo il dato grezzo dell'API
    subject_api = (ev.get("subjectDesc") or "").strip() or None
    begin = _iso(ev.get("evtDatetimeBegin"))
    item = {
        "uid": "cvv:%s" % ev["evtId"] if ev.get("evtId") is not None else None,
        "kind": kind,
        "kind_note": "dedotto da codice evento (%s) e testo" % code,
        "event_code": code,
        "subject_api": subject_api,
        "date": begin[:10],
        "start": begin,
        "end": _iso(ev.get("evtDatetimeEnd")),
        "all_day": bool(ev.get("isFullDay")),
        "text": text,
        "author": ev.get("authorName"),
        "class": ev.get("classDesc"),
        "mentions_test": bool(TEST_RE.search(text)),
    }
    if item["uid"] is None:  # fallback: identificatore secondario
        item["uid"] = "sec:" + hashlib.sha1(
            ("%s|%s|%s" % (norm(item["author"]), item["date"], norm(text))).encode()).hexdigest()[:16]
    item["hash"] = _hash(item)
    return item


ABSENCE_RAW_FIELDS = ("evtId", "evtCode", "evtDate", "evtHPos", "evtValue", "hoursAbsence",
                      "isJustified", "justifReasonCode", "justifReasonDesc", "webJustifStatus")
ABSENCE_UNVERIFIED = "Da verificare"
ABSENCE_SOURCE = "UI web ClasseViva, verificata sulla UI web"
# INTERPRETAZIONI VERIFICATE sulla UI web ufficiale. Tutto cio' che non e' qui resta 'Da verificare'.
ABSENCE_CODE_MAP = {"ABR0": "Entrata in ritardo", "ABA0": "Assenza"}
ABSENCE_WEBSTATUS_MAP = {1: "In approvazione", 0: "Da giustificare"}
ABSENCE_HOURS_LOST_CODES = ("ABR0",)   # evtValue = ore perse: verificato su UNA sola voce (30/09, '1 ora persa')


ABSENCE_RULES_REV = 1   # da incrementare a mano se cambia la LOGICA di interpret_absence (non le tabelle)


def interp_version(rules_rev, *tables):
    """Versione della logica interpretativa: 'r<rev>-<impronta delle tabelle>'. Le tabelle sono incluse
    automaticamente (se ne cambi una la versione cambia da sola); la revisione manuale copre i cambi di logica.
    Riutilizzabile per ogni altra interpretazione: basta passare revisione e tabelle."""
    blob = json.dumps(tables, sort_keys=True, ensure_ascii=False, default=str)
    return "r%d-%s" % (rules_rev, hashlib.sha1(blob.encode()).hexdigest()[:6])


def absence_interp_version():
    return interp_version(ABSENCE_RULES_REV, ABSENCE_CODE_MAP, ABSENCE_WEBSTATUS_MAP,
                          list(ABSENCE_HOURS_LOST_CODES), ABSENCE_UNVERIFIED)


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def interpret_absence(raw):
    """Interpretazione SOLO da tabelle verificate. Non modifica i dati grezzi."""
    code, ws, val = raw.get("evtCode"), raw.get("webJustifStatus"), raw.get("evtValue")
    unverified = []
    tipo = ABSENCE_CODE_MAP.get(code)
    if tipo is None:
        tipo = ABSENCE_UNVERIFIED
        unverified.append("codice %r non in tabella verificata" % code)
    stato = ABSENCE_WEBSTATUS_MAP.get(ws) if _is_num(ws) else None
    if stato is None:
        stato = ABSENCE_UNVERIFIED
        unverified.append("webJustifStatus %r non in tabella verificata" % (ws,))
    ore_perse = None
    if code in ABSENCE_HOURS_LOST_CODES and _is_num(val):
        ore_perse = val
    elif val is not None:
        unverified.append("evtValue %r non interpretato per il codice %r" % (val, code))
    return {"tipo": tipo, "stato_giustificazione": stato, "ore_perse": ore_perse,
            "fonte_interpretazione": ABSENCE_SOURCE, "da_verificare": unverified}


def _absence_name(raw, interp):
    try:
        y, m, d = str(raw["evtDate"]).split("-")
        data = "%s/%s/%s" % (d, m, y)
    except (KeyError, ValueError):
        data = str(raw.get("evtDate"))
    if interp["tipo"] == ABSENCE_UNVERIFIED:
        return "%s \u00b7 %s (da verificare)" % (raw.get("evtCode"), data)
    nome = "%s \u00b7 %s" % (interp["tipo"], data)
    if raw.get("evtCode") == "ABR0" and _is_num(raw.get("evtHPos")):
        nome += " \u00b7 %d\u00aa ora" % raw["evtHPos"]
    return nome


def normalize_absence(ev):
    """Quattro livelli separati: 'raw' (valori ESATTI di ClasseViva), 'interpreted' (solo tabelle verificate),
    'derived' (nome, ecc.).
    L'hash copre SOLO i dati grezzi."""
    raw = {k: ev.get(k) for k in ABSENCE_RAW_FIELDS}
    eid = raw["evtId"]
    uid = "cvv:%s" % eid if eid is not None else "sec:" + hashlib.sha1(
        ("%s|%s|%s" % (raw["evtCode"], raw["evtDate"], raw["evtHPos"])).encode()).hexdigest()[:16]
    digest = hashlib.sha1(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    interp = interpret_absence(raw)
    return {
        "uid": uid,
        "date": raw["evtDate"],
        "hash": digest,                                  # SOLO dati grezzi
        "interp_version": absence_interp_version(),      # versione della logica interpretativa (separata dall'hash)
        "raw": raw,
        "extra_fields_present": sorted(set(ev) - set(ABSENCE_RAW_FIELDS)),
        "interpreted": interp,
        "derived": {"nome": _absence_name(raw, interp)},
    }


# ---- Voti e comunicazioni: lettura GREZZA completa -------------------------------------------------
# I campi di questi record non sono ancora stati verificati su dati reali (voti: 0 presenti; bacheca: solo 8 titoli).
# Per questo 'raw' e' il record ESATTO di ClasseViva (tutti i campi, anche quelli non previsti) e l'hash lo copre intero.
GRADE_RULES_REV = 1
GRADE_TYPE_MAP = {}   # componentDesc -> tipo di valutazione: vuota finche' non verificata con dati reali
NOTICE_RULES_REV = 1
RAW_UNVERIFIED = "Da verificare"


def canonical_hash(raw):
    return hashlib.sha1(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def _fmt_date(iso):
    try:
        y, m, d = str(iso)[:10].split("-")
        return "%s/%s/%s" % (d, m, y)
    except ValueError:
        return str(iso)


def grade_interp_version():
    return interp_version(GRADE_RULES_REV, GRADE_TYPE_MAP, RAW_UNVERIFIED)


def notice_interp_version():
    return interp_version(NOTICE_RULES_REV, RAW_UNVERIFIED)


def normalize_grade(g):
    """raw = record ESATTO (nessun campo scartato); interpreted = solo tabelle verificate (oggi: nessuna);
    il peso non viene mai dedotto: resta quello che ClasseViva fornisce (raw.weightFactor)."""
    raw = dict(g)
    eid = raw.get("evtId")
    uid = "cvv:%s" % eid if eid is not None else "sec:" + hashlib.sha1(("%s|%s|%s|%s" % (
        raw.get("subjectDesc"), raw.get("evtDate"), raw.get("displayValue"), raw.get("componentDesc"))).encode()).hexdigest()[:16]
    unverified = []
    comp = raw.get("componentDesc")
    tipo = GRADE_TYPE_MAP.get(comp)
    if tipo is None:
        tipo = RAW_UNVERIFIED
        unverified.append("componentDesc %r non in tabella verificata" % (comp,))
    nome = " \u00b7 ".join(str(x) for x in (raw.get("subjectDesc"), raw.get("displayValue"), _fmt_date(raw.get("evtDate"))) if x)
    return {"uid": uid, "date": raw.get("evtDate"), "hash": canonical_hash(raw), "interp_version": grade_interp_version(),
            "raw": raw, "campi_presenti": sorted(raw),
            "interpreted": {"materia_chiave": subj.subject_key(raw.get("subjectDesc")), "tipo_valutazione": tipo,
                            "da_verificare": unverified},
            "derived": {"nome": nome or uid}}


def normalize_notice(n):
    raw = dict(n)
    pid = raw.get("pubId")
    uid = "cvv:%s" % pid if pid is not None else "sec:" + hashlib.sha1(
        ("%s|%s" % (raw.get("cntTitle"), raw.get("pubDT"))).encode()).hexdigest()[:16]
    cat = (raw.get("cntCategory") or "").strip()
    return {"uid": uid, "date": str(raw.get("pubDT") or "")[:10] or None, "hash": canonical_hash(raw),
            "interp_version": notice_interp_version(), "raw": raw, "campi_presenti": sorted(raw),
            "interpreted": {"categoria": cat or "Da classificare", "da_verificare": [] if cat else ["categoria non fornita da ClasseViva"]},
            "derived": {"nome": raw.get("cntTitle") or uid}}


LESSON_RULES_REV = 1


def lesson_interp_version():
    return interp_version(LESSON_RULES_REV, RAW_UNVERIFIED)


def normalize_lesson(raw_in):
    """Lezione svolta (endpoint /lessons). raw = record ESATTO di ClasseViva; l'hash lo copre intero, quindi un
    argomento compilato o corretto in un secondo momento e' un 'update' (versione 2) e non un duplicato.
    Identita': evtId dell'API (cvv:lesson:<evtId>). Senza evtId: chiave composta data|ora|docente|materia
    (l'argomento resta FUORI dalla chiave). Nessuna interpretazione inventata."""
    raw = dict(raw_in)
    eid = raw.get("evtId")
    if eid is not None:
        uid = "cvv:lesson:%s" % eid
    else:
        uid = "sec:lesson:" + hashlib.sha1(("%s|%s|%s|%s" % (
            raw.get("evtDate"), raw.get("evtHPos"), norm(raw.get("authorName")), norm(raw.get("subjectDesc")))
        ).encode()).hexdigest()[:16]
    arg = (raw.get("lessonArg") or "").strip()
    unverified = [] if arg else ["argomento non (ancora) compilato"]
    nome = " \u00b7 ".join(str(x) for x in (raw.get("subjectDesc"), _fmt_date(raw.get("evtDate")),
                                            "%s\u00aa ora" % raw["evtHPos"] if raw.get("evtHPos") is not None else None) if x)
    return {"uid": uid, "date": str(raw.get("evtDate") or "")[:10] or None, "hash": canonical_hash(raw),
            "interp_version": lesson_interp_version(), "raw": raw, "campi_presenti": sorted(raw),
            "interpreted": {"materia_chiave": subj.subject_key(raw.get("subjectDesc")), "da_verificare": unverified},
            "derived": {"nome": nome or uid}}


def _hash(item):
    """Impronta del contenuto: cambia se cambia la voce (compresa la materia risolta)."""
    return hashlib.sha1(json.dumps(
        [item["kind"], norm(item.get("subject")), item["date"], item["start"], item["end"], item["text"]],
        ensure_ascii=False).encode()).hexdigest()[:12]


def enrich_event(item, subjects):
    """Aggiunge materia e criterio (vedi cvv_subject) e ricalcola l'hash."""
    r = subj.resolve_subject(item.get("author"), item.get("text"), subjects, item.get("subject_api"))
    item.update(r)
    item["subject"] = r["subject"]
    item["hash"] = _hash(item)
    return item


