"""Client Google Classroom API v1: SOLA LETTURA (solo metodo GET), stdlib.

* Il token arriva da un `token_provider(force_refresh=False) -> str` iniettato (vedi cls_auth.py): questo modulo non
  conosce ne' salva credenziali.
* Il trasporto HTTP e' iniettabile (test senza rete).
* Ritenta (con backoff) solo su 429/5xx; su 401 prova UNA volta a rinnovare il token.
* Gli errori sono classificati (ClassroomApiError.kind) per distinguere: API non abilitata, scope insufficiente,
  permesso negato (policy del dominio), limite di quota, ecc.
* I record restituiti sono i JSON ESATTI dell'API (nessuna aggiunta/rimozione di campi).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://classroom.googleapis.com/v1"
RETRY_STATUS = (429, 500, 502, 503, 504)
ALL_COURSE_STATES = ("ACTIVE", "ARCHIVED", "PROVISIONED", "DECLINED", "SUSPENDED")


class ClassroomApiError(Exception):
    def __init__(self, status, kind, message, reason=None, path=None):
        super().__init__("HTTP %s [%s] %s%s" % (status, kind, message, " (reason=%s)" % reason if reason else ""))
        self.status, self.kind, self.reason, self.path = status, kind, reason, path


def classify(status, payload):
    """(kind, reason, message) da una risposta d'errore Google."""
    err = payload.get("error") if isinstance(payload, dict) else None
    msg, reason, gstatus = None, None, None
    if isinstance(err, dict):
        msg, gstatus = err.get("message"), err.get("status")
        for d in err.get("details") or []:
            if isinstance(d, dict) and d.get("reason"):
                reason = d["reason"]
                break
        if not reason:
            for e in err.get("errors") or []:
                if isinstance(e, dict) and e.get("reason"):
                    reason = e["reason"]
                    break
    elif isinstance(err, str):
        msg, reason = payload.get("error_description"), err
    low = ("%s %s" % (msg or "", reason or "")).lower()
    if reason in ("SERVICE_DISABLED", "accessNotConfigured") or "has not been used in project" in low:
        kind = "api_disabled"
    elif reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT" or "insufficient authentication scopes" in low:
        kind = "scope_insufficient"
    elif status == 401:
        kind = "auth"
    elif status == 403:
        kind = "permission"
    elif status == 404:
        kind = "not_found"
    elif status == 429 or gstatus == "RESOURCE_EXHAUSTED":
        kind = "rate_limit"
    elif status >= 500:
        kind = "server"
    else:
        kind = "other"
    return kind, reason, msg or "errore senza messaggio"


def urllib_transport(method, url, headers, timeout=30):
    """-> (status, json|None, headers dict). Non solleva su errori HTTP."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers or {})
    try:
        return status, (json.loads(raw.decode("utf-8")) if raw else None), hdrs
    except ValueError:
        return status, None, hdrs


class ClassroomClient:
    def __init__(self, token_provider, transport=None, sleep=time.sleep, max_retries=4, page_size=100, max_pages=500):
        self.token_provider = token_provider
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.max_retries, self.page_size, self.max_pages = max_retries, page_size, max_pages
        self.requests_made = 0

    # ---- basso livello: SOLO GET ------------------------------------------------------------------
    def _get(self, path, params=None):
        q = urllib.parse.urlencode(params or {}, doseq=True)
        url = BASE + path + ("?" + q if q else "")
        refreshed = False
        for attempt in range(self.max_retries + 1):
            headers = {"Authorization": "Bearer " + self.token_provider(False), "Accept": "application/json"}
            self.requests_made += 1
            status, payload, hdrs = self.transport("GET", url, headers)
            if status == 200 and isinstance(payload, dict):
                return payload
            if status == 200:
                return {}
            if status == 401 and not refreshed:
                refreshed = True
                self.token_provider(True)
                continue
            if status in RETRY_STATUS and attempt < self.max_retries:
                ra = (hdrs or {}).get("Retry-After")
                try:
                    delay = min(float(ra), 60.0)
                except (TypeError, ValueError):
                    delay = min(2.0 ** attempt, 30.0)
                self.sleep(delay)
                continue
            kind, reason, msg = classify(status, payload)
            raise ClassroomApiError(status, kind, msg, reason, path)
        raise ClassroomApiError(0, "other", "tentativi esauriti", None, path)

    def _list(self, path, key, params=None):
        out, token, pages = [], None, 0
        while True:
            p = dict(params or {})
            p["pageSize"] = self.page_size
            if token:
                p["pageToken"] = token
            data = self._get(path, p)
            out.extend(data.get(key) or [])
            token = data.get("nextPageToken")
            pages += 1
            if not token:
                return out
            if pages >= self.max_pages:
                raise ClassroomApiError(0, "other", "troppe pagine (%d): lettura interrotta, non parziale" % pages, None, path)

    # ---- risorse (record JSON esatti) -------------------------------------------------------------
    def courses(self, states=ALL_COURSE_STATES):
        return self._list("/courses", "courses", {"courseStates": list(states)})

    def course_work(self, course_id):
        return self._list("/courses/%s/courseWork" % urllib.parse.quote(course_id, safe=""), "courseWork")

    def materials(self, course_id):
        return self._list("/courses/%s/courseWorkMaterials" % urllib.parse.quote(course_id, safe=""),
                          "courseWorkMaterial")

    def announcements(self, course_id):
        return self._list("/courses/%s/announcements" % urllib.parse.quote(course_id, safe=""), "announcements")

    def topics(self, course_id):
        return self._list("/courses/%s/topics" % urllib.parse.quote(course_id, safe=""), "topic")

    def submissions(self, course_id):
        # courseWorkId='-' = tutte le consegne del corso; userId=me = solo le proprie (uno studente vede solo queste).
        return self._list("/courses/%s/courseWork/-/studentSubmissions" % urllib.parse.quote(course_id, safe=""),
                          "studentSubmissions", {"userId": "me"})


CONTENT_STATES = ("ACTIVE", "ARCHIVED")


def make_fetchers(client, kinds=None, content_states=CONTENT_STATES):
    """{kind: (fetch, finestra)} per cvv_dryrun/cvv_sync. L'elenco corsi e' letto UNA volta per esecuzione.
    Qualsiasi errore su un corso fa fallire l'intero tipo (lettura mai parziale): il registro non marca nulla missing."""
    cache = {}

    def all_courses(refresh=False):
        if refresh or "c" not in cache:
            cache["c"] = client.courses()
        return cache["c"]

    def courses_fetch():            # il tipo 'course' rilegge SEMPRE (utile al secondo giro del dry-run)
        return all_courses(refresh=True)

    def per_course(method):
        def fetch():
            out = []
            for c in all_courses():
                if c.get("courseState") in content_states:
                    out.extend(getattr(client, method)(c["id"]))
            return out
        return fetch

    table = {"course": courses_fetch, "course_work": per_course("course_work"), "material": per_course("materials"),
             "announcement": per_course("announcements"), "topic": per_course("topics"),
             "submission": per_course("submissions")}
    return {k: (fn, None) for k, fn in table.items() if kinds is None or k in kinds}
