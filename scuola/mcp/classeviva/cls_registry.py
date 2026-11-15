"""Adattatore GOOGLE CLASSROOM -> registro locale (registry.py).

Classroom e' una fonte SEPARATA da ClasseViva: stesso registro generico, `source = "classroom"`, nessuna fusione
automatica di record tra le due fonti (eventuali collegamenti andranno proposti e verificati, mai dedotti qui).

Percorso di un record:  RAW (record esatto dell'API, mai modificato)  ->  registro (identita', hash, versioni, stato)
                        ->  NORMALIZZATO (questo modulo)  ->  PROIEZIONE (payload neutro).

Schemi: documentazione ufficiale Classroom API v1 (courses, courseWork, courseWorkMaterials, announcements, topics,
studentSubmissions). Nessun campo inventato: cio' che l'API non da' resta None.

'kind' = tipo di risorsa. Identita' (source_uid): corso -> "<courseId>"; le altre -> "<courseId>/<id>" (gli id Classroom
sono unici solo dentro il corso). ID normalizzato (uid) = "cls:" + source_uid.
"""
from __future__ import annotations

import copy
import hashlib

import registry as reg  # noqa: F401  (stessa interfaccia dell'adattatore ClasseViva)

SOURCE = "classroom"
UID_PREFIX = "cls:"
ALL_KINDS = ("course", "course_work", "material", "announcement", "topic", "submission")

# Scope di SOLA LETTURA necessari per ciascun tipo (il probe iniziale usa solo il primo).
SCOPES = {
    "course": "https://www.googleapis.com/auth/classroom.courses.readonly",
    "course_work": "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "submission": "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "material": "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "announcement": "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "topic": "https://www.googleapis.com/auth/classroom.topics.readonly",
}

LOCAL_TZ = "Europe/Rome"
KNOWN = {
    "course_state": {"ACTIVE", "ARCHIVED", "PROVISIONED", "DECLINED", "SUSPENDED"},
    "work_state": {"PUBLISHED", "DRAFT", "DELETED"},
    "work_type": {"ASSIGNMENT", "SHORT_ANSWER_QUESTION", "MULTIPLE_CHOICE_QUESTION"},
    "submission_state": {"NEW", "CREATED", "TURNED_IN", "RETURNED", "RECLAIMED_BY_STUDENT",
                         "STUDENT_EDITED_AFTER_TURN_IN"},
}

# Campo non-ID da alterare nel dry-run (solo su copie in memoria).
SIM_FIELD = {"course": "name", "course_work": "title", "material": "title", "announcement": "text",
             "topic": "name", "submission": "state"}

# Campi volatili. VERIFICATO dal vivo (dry-run con second_read + confronto di due letture): l'unico campo
# che cambia tra due letture ravvicinate e' `thumbnailUrl` dei file Drive (materials[].driveFile.driveFile e
# assignmentSubmission.attachments[].driveFile), rigenerato da Google a ogni GET. Escluso dall'hash a qualsiasi
# profondita' (registry.strip_volatile); resta nel RAW salvato. updateTime cambia solo quando cambia la risorsa.
VOLATILE = {k: ("**.thumbnailUrl",) for k in ALL_KINDS}

_REV = "cls-r1"


def _interp(*tables):
    h = hashlib.sha1(repr(tables).encode()).hexdigest()[:8]
    return "%s-%s" % (_REV, h)


def interp_version():
    return _interp(LOCAL_TZ, sorted((k, sorted(v)) for k, v in KNOWN.items()))


KINDS = {}  # riempito in fondo: kind -> {"normalize", "interp_version"}


# ---------------------------------------------------------------- identita'
def source_uid_of(uid):
    return uid[len(UID_PREFIX):] if uid.startswith(UID_PREFIX) else uid


def legacy_uid_of(source_uid):
    return None          # Classroom non ha ID storici


def raw_source_uid(kind, raw):
    """source_uid dal RAW. ValueError se mancano gli id: un record senza identita' non entra nel registro."""
    def need(k):
        v = raw.get(k)
        if not isinstance(v, str) or not v:
            raise ValueError("record Classroom '%s' senza %s" % (kind, k))
        return v
    if kind == "course":
        return need("id")
    if kind == "topic":
        return "%s/%s" % (need("courseId"), need("topicId"))
    return "%s/%s" % (need("courseId"), need("id"))


# ------------------------------------------------------------------- utilita'
def _date_of_ts(ts):
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 and ts[4] == "-" else None


def _due(raw):
    """dueDate/dueTime (ora in UTC secondo la documentazione) -> date UTC e locali. None se non c'e' scadenza."""
    d, t = raw.get("dueDate"), raw.get("dueTime")
    if not isinstance(d, dict) or not d.get("year"):
        return None
    date = "%04d-%02d-%02d" % (d["year"], d.get("month") or 1, d.get("day") or 1)
    out = {"date_utc": date, "time_utc": None, "at_utc": None, "date_local": date, "at_local": None, "tz": LOCAL_TZ}
    if isinstance(t, dict):
        hh, mm = t.get("hours") or 0, t.get("minutes") or 0
        out["time_utc"] = "%02d:%02d" % (hh, mm)
        out["at_utc"] = "%sT%02d:%02d:00Z" % (date, hh, mm)
        try:
            import datetime as dt
            from zoneinfo import ZoneInfo
            utc = dt.datetime(d["year"], d.get("month") or 1, d.get("day") or 1, hh, mm, tzinfo=dt.timezone.utc)
            loc = utc.astimezone(ZoneInfo(LOCAL_TZ))
            out["date_local"] = loc.strftime("%Y-%m-%d")
            out["at_local"] = loc.strftime("%Y-%m-%dT%H:%M")
        except Exception:      # noqa: BLE001 - fuso non disponibile: si resta sui valori UTC, dichiarato
            out["tz"] = None
    return out


def _materials(raw):
    out = []
    for m in raw.get("materials") or []:
        if not isinstance(m, dict):
            continue
        if "driveFile" in m:
            sd = m["driveFile"] if isinstance(m["driveFile"], dict) else {}
            df = sd.get("driveFile") if isinstance(sd.get("driveFile"), dict) else {}
            out.append({"type": "drive_file", "id": df.get("id"), "title": df.get("title"),
                        "url": df.get("alternateLink"), "share_mode": sd.get("shareMode")})
        elif "youtubeVideo" in m:
            y = m["youtubeVideo"] if isinstance(m["youtubeVideo"], dict) else {}
            out.append({"type": "youtube", "id": y.get("id"), "title": y.get("title"), "url": y.get("alternateLink")})
        elif "link" in m:
            lk = m["link"] if isinstance(m["link"], dict) else {}
            entry = {"type": "link", "id": None, "title": lk.get("title"), "url": lk.get("url")}
            if antibot_title(lk.get("title")):      # titolo di una pagina anti-bot: non e' il titolo del link
                entry.update(title=None, titolo_non_determinabile="titolo_antibot_della_fonte")
            out.append(entry)
        elif "form" in m:
            f = m["form"] if isinstance(m["form"], dict) else {}
            out.append({"type": "form", "id": None, "title": f.get("title"), "url": f.get("formUrl")})
        elif "gem" in m or "notebook" in m:
            key = "gem" if "gem" in m else "notebook"
            g = m[key] if isinstance(m[key], dict) else {}
            out.append({"type": key, "id": g.get("id"), "title": g.get("title"), "url": g.get("url")})
        else:
            out.append({"type": "unknown", "id": None, "title": None, "url": None, "keys": sorted(m)})
    return out


def _flags(raw, state_key, state_set, extra=()):
    """Motivi per cui il record merita un controllo umano (valori che l'adattatore non conosce)."""
    why = []
    st = raw.get(state_key)
    if st is not None and st not in state_set:
        why.append("%s sconosciuto: %s" % (state_key, st))
    for k, allowed in extra:
        v = raw.get(k)
        if v is not None and v not in allowed:
            why.append("%s sconosciuto: %s" % (k, v))
    return why


def _base(kind, raw, source_uid):
    return {"uid": UID_PREFIX + source_uid, "kind": kind, "source": SOURCE, "interp_version": interp_version(),
            "hash": None}


# ------------------------------------------------------------- normalizzatori
def normalize_course(raw):
    n = _base("course", raw, raw_source_uid("course", raw))
    n.update(id=raw.get("id"), name=raw.get("name"), section=raw.get("section"), room=raw.get("room"),
             subject_field=raw.get("subject"), description_heading=raw.get("descriptionHeading"),
             description=raw.get("description"), state=raw.get("courseState"), owner_id=raw.get("ownerId"),
             created=raw.get("creationTime"), updated=raw.get("updateTime"), alternate_link=raw.get("alternateLink"),
             date=_date_of_ts(raw.get("creationTime")),
             interpreted={"da_verificare": _flags(raw, "courseState", KNOWN["course_state"]),
                          "nota": "nessun collegamento automatico a una materia di ClasseViva"})
    return n


def normalize_course_work(raw):
    n = _base("course_work", raw, raw_source_uid("course_work", raw))
    due = _due(raw)
    n.update(course_id=raw.get("courseId"), id=raw.get("id"), title=raw.get("title"),
             description=raw.get("description"), state=raw.get("state"), work_type=raw.get("workType"),
             max_points=raw.get("maxPoints"), topic_id=raw.get("topicId"), materials=_materials(raw),
             created=raw.get("creationTime"), updated=raw.get("updateTime"), scheduled=raw.get("scheduledTime"),
             alternate_link=raw.get("alternateLink"), due=due,
             date=(due or {}).get("date_local") or _date_of_ts(raw.get("creationTime")),
             interpreted={"da_verificare": _flags(raw, "state", KNOWN["work_state"],
                                                  [("workType", KNOWN["work_type"])]) +
                          ["materiale di tipo sconosciuto"] * any(m["type"] == "unknown" for m in _materials(raw)),
                          "nota": "dueTime e' in UTC secondo la documentazione: due.at_local e' la conversione in %s"
                                  % LOCAL_TZ})
    return n


def normalize_material(raw):
    n = _base("material", raw, raw_source_uid("material", raw))
    n.update(course_id=raw.get("courseId"), id=raw.get("id"), title=raw.get("title"),
             description=raw.get("description"), state=raw.get("state"), topic_id=raw.get("topicId"),
             materials=_materials(raw), created=raw.get("creationTime"), updated=raw.get("updateTime"),
             scheduled=raw.get("scheduledTime"), alternate_link=raw.get("alternateLink"),
             date=_date_of_ts(raw.get("creationTime")),
             interpreted={"da_verificare": _flags(raw, "state", KNOWN["work_state"]) +
                          ["materiale di tipo sconosciuto"] * any(m["type"] == "unknown" for m in _materials(raw)),
                          "nota": "il contenuto dei file Drive NON e' incluso: l'API da' solo i metadati"})
    return n


def normalize_announcement(raw):
    n = _base("announcement", raw, raw_source_uid("announcement", raw))
    n.update(course_id=raw.get("courseId"), id=raw.get("id"), text=raw.get("text"), state=raw.get("state"),
             materials=_materials(raw), created=raw.get("creationTime"), updated=raw.get("updateTime"),
             scheduled=raw.get("scheduledTime"), alternate_link=raw.get("alternateLink"),
             date=_date_of_ts(raw.get("creationTime")),
             interpreted={"da_verificare": _flags(raw, "state", KNOWN["work_state"])})
    return n


def normalize_topic(raw):
    n = _base("topic", raw, raw_source_uid("topic", raw))
    n.update(course_id=raw.get("courseId"), topic_id=raw.get("topicId"), name=raw.get("name"),
             updated=raw.get("updateTime"), date=_date_of_ts(raw.get("updateTime")),
             interpreted={"da_verificare": []})
    return n


def normalize_submission(raw):
    n = _base("submission", raw, raw_source_uid("submission", raw))
    n.update(course_id=raw.get("courseId"), course_work_id=raw.get("courseWorkId"), id=raw.get("id"),
             state=raw.get("state"), late=raw.get("late"), work_type=raw.get("courseWorkType"),
             assigned_points=raw.get("assignedGrade"), draft_points=raw.get("draftGrade"),
             created=raw.get("creationTime"), updated=raw.get("updateTime"), alternate_link=raw.get("alternateLink"),
             date=_date_of_ts(raw.get("updateTime") or raw.get("creationTime")),
             interpreted={"da_verificare": _flags(raw, "state", KNOWN["submission_state"],
                                                  [("courseWorkType", KNOWN["work_type"])]),
                          "nota": "assigned_points e' un PUNTEGGIO sul compito (rispetto a max_points), NON un voto "
                                  "scolastico; la sua visibilita' allo studente va verificata con dati reali"})
    return n


KINDS.update({
    "course": {"normalize": normalize_course, "interp_version": interp_version},
    "course_work": {"normalize": normalize_course_work, "interp_version": interp_version},
    "material": {"normalize": normalize_material, "interp_version": interp_version},
    "announcement": {"normalize": normalize_announcement, "interp_version": interp_version},
    "topic": {"normalize": normalize_topic, "interp_version": interp_version},
    "submission": {"normalize": normalize_submission, "interp_version": interp_version},
})


# ------------------------------------------------------------ interfaccia comune
def to_items(kind, raws):
    """Record grezzi Classroom -> item per il registro (identita' dagli id esatti del RAW)."""
    normalize = KINDS[kind]["normalize"]
    items = []
    for raw in raws:
        n = normalize(raw)
        items.append({"source_uid": source_uid_of(n["uid"]), "legacy_uid": None, "date": n.get("date"), "raw": raw})
    return items


# Risposte DEGRADATE (verificato dal vivo: due letture a pochi secondi -> pochi file Drive su centinaia): a volte
# Classroom restituisce un file Drive SENZA `title` e con `alternateLink` nella forma `https://drive.google.com/open?id=<id>`
# invece di `.../file/d/<id>/view?...`, poi torna alla forma completa. Non e' una modifica della risorsa. Prima dell'ingest,
# l'entry degradata viene ricostruita dalla versione GIA' REGISTRATA dello stesso record, solo se ha lo stesso id file e un
# titolo: niente versioni fantasma, niente dati completi sovrascritti. Le modifiche reali (titolo presente e diverso, file
# aggiunti/tolti, ogni altro campo) restano tali. Senza una versione precedente con titolo nulla viene inventato: avviso.
_DEGRADED_LINK = "https://drive.google.com/open?id=%s"


def _drive_files(raw):
    """Gli oggetti driveFile (dict, modificabili) di un record grezzo: materials[].driveFile.driveFile e
    assignmentSubmission.attachments[].driveFile."""
    out = []
    for m in raw.get("materials") or []:
        sd = m.get("driveFile") if isinstance(m, dict) else None
        df = sd.get("driveFile") if isinstance(sd, dict) else None
        if isinstance(df, dict):
            out.append(df)
    sub = raw.get("assignmentSubmission")
    for a in (sub.get("attachments") or []) if isinstance(sub, dict) else []:
        df = a.get("driveFile") if isinstance(a, dict) else None
        if isinstance(df, dict):
            out.append(df)
    return out


def _degraded(df):
    return isinstance(df.get("id"), str) and df.get("id") and "title" not in df


def repair_degraded(kind, raw, previous_raw):
    """(raw ricostruito, n riparati, n non ricostruibili). Non modifica `raw`: lavora su una copia solo se serve."""
    live = [df for df in _drive_files(raw) if _degraded(df)]
    if not live:
        return raw, 0, 0
    known = {}
    for df in _drive_files(previous_raw) if isinstance(previous_raw, dict) else []:
        if isinstance(df.get("id"), str) and isinstance(df.get("title"), str):
            known.setdefault(df["id"], df)
    out = copy.deepcopy(raw)
    fixed = missing = 0
    for df in _drive_files(out):
        if not _degraded(df):
            continue
        prev = known.get(df["id"])
        if prev is None:
            missing += 1
            continue
        df["title"] = prev["title"]
        if df.get("alternateLink") in (None, _DEGRADED_LINK % df["id"]) and "alternateLink" in prev:
            df["alternateLink"] = prev["alternateLink"]
        fixed += 1
    return out, fixed, missing


# LINK DEGRADATI (verificato dal vivo, un annuncio): un materiale `link` con titolo
# valido gia' registrato e' stato restituito prima SENZA `title` (e senza thumbnailUrl), poi con title "Just a moment..."
# (titolo della pagina di attesa anti-bot che il servizio di anteprima di Google ha letto al posto della pagina vera).
# Non e' una modifica della risorsa. Identita' di un link dentro il record = url (i link non hanno id). Regola: se il link
# letto e' degradato (titolo assente, vuoto o di una pagina anti-bot) ed esiste nella versione registrata UN SOLO link con
# lo stesso url, i campi del titolo (title, thumbnailUrl) tornano quelli registrati: titolo valido -> ripristinato;
# registrato anch'esso degradato -> resta com'era (degradato->degradato non e' un cambiamento). Url diverso, titolo valido
# diverso, link aggiunti/tolti = modifiche reali. Senza versione registrata: si registra cio' che e' stato letto, mai un
# titolo inventato; il normalizzatore non espone il titolo anti-bot (title None + titolo_non_determinabile) e c'e' un avviso.
# Pagine anti-bot: SOLO titoli esatti noti (dopo normalizzazione di spazi, maiuscole e ellissi), mai parole isolate:
# "Just a moment in history" resta un titolo valido.
_ANTIBOT_TITLES = frozenset({
    "just a moment...", "just a moment", "one moment, please...", "please wait...",
    "attention required! | cloudflare", "please wait... | cloudflare", "just a moment... | cloudflare",
    "ddos-guard", "ddos-guard | checking your browser", "verifying you are human", "verify you are human",
    "checking your browser before accessing", "checking your browser...", "checking your browser",
})


def antibot_title(title):
    """True se il titolo e' quello di una pagina di verifica anti-bot (non il titolo della risorsa)."""
    if not isinstance(title, str):
        return False
    t = " ".join(title.replace("…", "...").split()).lower()
    if t in _ANTIBOT_TITLES:
        return True
    return t.startswith("checking your browser before accessing ")


def _links(raw):
    """Gli oggetti link (dict, modificabili) di un record grezzo: materials[].link e assignmentSubmission.attachments[].link."""
    out = []
    for m in raw.get("materials") or []:
        lk = m.get("link") if isinstance(m, dict) else None
        if isinstance(lk, dict):
            out.append(lk)
    sub = raw.get("assignmentSubmission")
    for a in (sub.get("attachments") or []) if isinstance(sub, dict) else []:
        lk = a.get("link") if isinstance(a, dict) else None
        if isinstance(lk, dict):
            out.append(lk)
    return out


def _link_degraded(lk):
    t = lk.get("title")
    return isinstance(lk.get("url"), str) and bool(lk.get("url")) and (
        t is None or (isinstance(t, str) and not t.strip()) or antibot_title(t))


_LINK_TITLE_FIELDS = ("title", "thumbnailUrl")


def repair_links(kind, raw, previous_raw):
    """(raw ricostruito, n ripristinati, n non determinabili). Non modifica `raw`: lavora su una copia solo se serve.
    non determinabili = link degradati senza un titolo valido registrato per lo stesso url (lasciati come letti, oppure
    riportati alla stessa forma degradata gia' registrata: nessuna versione fantasma)."""
    if not any(_link_degraded(lk) for lk in _links(raw)):
        return raw, 0, 0
    prev_by_url = {}
    for lk in _links(previous_raw) if isinstance(previous_raw, dict) else []:
        if isinstance(lk.get("url"), str):
            prev_by_url.setdefault(lk["url"], []).append(lk)
    out = copy.deepcopy(raw)
    live_urls = [lk.get("url") for lk in _links(out)]
    fixed = undetermined = 0
    for lk in _links(out):
        if not _link_degraded(lk):
            continue
        prevs = prev_by_url.get(lk["url"], [])
        if len(prevs) != 1 or live_urls.count(lk["url"]) != 1:     # nessuno storico o abbinamento non univoco
            undetermined += 1
            continue
        prev = prevs[0]
        if _link_degraded(prev) and (prev.get("title") == lk.get("title") and
                                     prev.get("thumbnailUrl") == lk.get("thumbnailUrl")):
            undetermined += 1                 # gia' identico alla forma registrata
            continue
        for f in _LINK_TITLE_FIELDS:
            if f in prev:
                lk[f] = prev[f]
            else:
                lk.pop(f, None)
        if _link_degraded(prev):
            undetermined += 1                 # riportato alla forma degradata registrata: nessuna versione fantasma
        else:
            fixed += 1
    return out, fixed, undetermined


def repair(kind, raw, previous_raw):
    """Entrambe le riparazioni (file Drive e link) -> (raw, {drive_fixed, drive_missing, link_fixed, link_undetermined})."""
    r1, f1, m1 = repair_degraded(kind, raw, previous_raw)
    r2, f2, m2 = repair_links(kind, r1, previous_raw)
    return r2, {"drive_fixed": f1, "drive_missing": m1, "link_fixed": f2, "link_undetermined": m2}


def _needs_previous(raw):
    return any(_degraded(df) for df in _drive_files(raw)) or any(_link_degraded(lk) for lk in _links(raw))


def _repair_all(registry, kind, raws, stats=None):
    out = []
    for raw in raws:
        prev = None
        if _needs_previous(raw):
            rec = registry.get_record(SOURCE, kind, raw_source_uid(kind, raw))
            prev = rec["raw"] if rec else None
        fixed_raw, s = repair(kind, raw, prev)
        if stats is not None:
            for k, v in s.items():
                stats[k] += v
            stats["drive_records"] += 1 if s["drive_fixed"] else 0
            stats["link_records"] += 1 if s["link_fixed"] else 0
        out.append(fixed_raw)
    return out


def sync_kind(registry, kind, fetch, window=None, **kw):
    """fetch() -> lista di record GREZZI. Errori (rete, auth, scope) -> run 'error', nessun record toccato.
    Le entry Drive e i link degradati sono ricostruiti dalla versione registrata (repair) e segnalati negli avvisi."""
    stats = {"drive_fixed": 0, "drive_missing": 0, "drive_records": 0, "link_fixed": 0, "link_undetermined": 0,
             "link_records": 0}

    def items():
        return to_items(kind, _repair_all(registry, kind, fetch(), stats))
    res = registry.run_source(SOURCE, kind, items, window=window, volatile=VOLATILE.get(kind, ()), **kw)
    w = []
    if stats["drive_fixed"]:
        w.append("%d file Drive in risposta degradata (senza titolo) in %d record: titolo e link ricostruiti "
                 "dalla versione registrata, nessuna nuova versione per questo" % (stats["drive_fixed"], stats["drive_records"]))
    if stats["drive_missing"]:
        w.append("%d file Drive senza titolo e senza versione registrata con titolo: lasciati come letti "
                 "(nessun valore inventato)" % stats["drive_missing"])
    if stats["link_fixed"]:
        w.append("%d link in risposta degradata (titolo assente o di una pagina anti-bot) in %d record: titolo ripristinato "
                 "dalla versione registrata, nessuna nuova versione per questo" % (stats["link_fixed"], stats["link_records"]))
    if stats["link_undetermined"]:
        w.append("%d link con titolo NON DETERMINABILE (assente o di una pagina anti-bot, senza titolo valido registrato per "
                 "lo stesso url): nessun titolo inventato" % stats["link_undetermined"])
    if w:
        if res.get("warnings") is None:
            res["warnings"] = []
        res["warnings"].extend(w)
    return res


def verify_items(kind, raws, registry):
    """Item con cui la verifica post-scrittura confronta il registro: stesse riparazioni della sincronizzazione, risolte
    sul registro appena scritto (prima la verifica segnalava come diversi i record con entry degradate ricostruite)."""
    return to_items(kind, _repair_all(registry, kind, raws))


def normalized(kind, raw):
    return KINDS[kind]["normalize"](raw)
