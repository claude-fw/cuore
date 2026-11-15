"""L3 DERIVATO per Google Classroom: `work_view` = un compito + la sua consegna + corso, anni scolastici, perimetro,
materia.
Modulo PURO e deterministico, solo libreria standard. Base per School Context, agenti di materia e
dashboard CUORE.

Regole fisse (verificate dai test):
  * nessuna rete, nessun orologio di sistema: `now` e' SEMPRE un input esplicito con fuso orario;
  * l'input non viene mai modificato; l'output e' ordinato e riproducibile byte per byte;
  * ogni work_view separa 4 gruppi:
      fonte         dati riportati dalla fonte (L1/L2), senza ricalcoli (es. `late` e' il dato API);
      derivato      informazioni calcolate da regole dichiarate (scadenza, stato_operativo, anno, proposte materia);
      manuale       decisioni future del livello manuale (L-MAN): in assenza di `manual` sono vuote;
      da_verificare anomalie dichiarate, mai corrette in silenzio;
  * compito e consegna si uniscono SOLO con (courseId, courseWorkId); consegne orfane o multiple sono riportate;
  * nessuna proposta di materia diventa una materia confermata: la decisione viene solo da `manual`;
  * gli anni scolastici sono DEDUZIONI con livello di certezza e candidati, mai presentate come dati, e sono DUE:
      anno_compito  dalle sole evidenze del compito (creazione, scadenza): l'anno del corso NON viene mai usato;
      anno_corso    contesto (un corso puo' essere riusato per piu' anni): non decide nulla sul compito;
  * perimetro operativo = anni dichiarati in CLASSI (3a e 4a classe). E' un'ETICHETTA: nessun record viene escluso,
    cancellato o riscritto; `operational_views` produce una vista filtrata senza toccare il risultato completo;
  * `stato_operativo` e' una classificazione tecnica: 'senza_scadenza' non significa "da fare",
    'scaduto_non_consegnato' non significa "da recuperare" (Classroom non dice se la consegna era richiesta).

Interfaccia per il futuro livello manuale (struttura semplice, nessun file creato qui):
    manual = {"course_subjects":    {course_id: [{"subject": key, "ruolo": "primary"|"secondary"}]},
              "course_school_year": {course_id: "2026-27"},      # -> manuale.anno_corso
              "work_school_year":   {work_uid: "2026-27"},       # -> manuale.anno_compito (prevale nel perimetro)
              "work_subject":       {work_uid: key}}
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re

import cls_registry as CL
import cvv_subject as subj

DERIVE_REV = "d2"          # da incrementare a ogni cambio di LOGICA (le costanti sotto entrano gia' nell'hash)
LOCAL_TZ = CL.LOCAL_TZ
SCHOOL_YEAR_START_MONTH = 9                       # anno scolastico: 1 settembre -> 31 agosto
TWO_DIGIT_YEAR_RANGE = (20, 39)                   # "24/25" accettato solo come 2020..2039 (evita "10/11" ecc.)
SUBMITTED = {"TURNED_IN": "consegnato", "STUDENT_EDITED_AFTER_TURN_IN": "consegnato", "RETURNED": "restituito"}
NOT_SUBMITTED = ("NEW", "CREATED", "RECLAIMED_BY_STUDENT")
# Dichiarazione dell'UTENTE (2026-10-03), non deducibile dai dati: anno scolastico -> classe. Solo questi anni
# formano il perimetro OPERATIVO; gli altri restano disponibili come storico. Da aggiornare a ogni nuovo anno.
CLASSI = {"2025-26": "3a classe", "2026-27": "4a classe"}
STATI_OPERATIVI = ("aperto", "scaduto_non_consegnato", "senza_scadenza", "consegnato", "restituito", "bozza",
                   "rimosso", "da_verificare")
_RULES = {"rev": DERIVE_REV, "tz": LOCAL_TZ, "sy_start": SCHOOL_YEAR_START_MONTH, "yy": TWO_DIGIT_YEAR_RANGE,
          "submitted": SUBMITTED, "not_submitted": NOT_SUBMITTED, "stati": STATI_OPERATIVI,
          "date_only": "fine giornata locale 23:59:59", "subject_rx": subj.EXPLICIT, "classi": CLASSI,
          "anno_compito": "creazione+scadenza del compito; accordo=alta, una sola=media, disaccordo=conflitto",
          "anno_corso": "nome=alta (creazione solo candidato), sola creazione=bassa"}
DERIVE_VERSION = "cls-%s-%s" % (DERIVE_REV, hashlib.sha256(
    (json.dumps(_RULES, sort_keys=True, ensure_ascii=False) + CL.interp_version()).encode("utf-8")).hexdigest()[:8])

NOTA_STATO = {
    "senza_scadenza": "nessuna scadenza in Classroom: NON significa 'da fare'",
    "scaduto_non_consegnato": "classificazione tecnica: Classroom non dice se la consegna era richiesta",
}


# ------------------------------------------------------------------------------------------------ tempo
def _zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo(LOCAL_TZ)


def parse_now(now):
    """`now` esplicito: datetime con fuso o stringa ISO con offset. Un orario senza fuso e' rifiutato."""
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now deve essere un datetime con fuso orario (es. '2026-10-03T21:00:00+02:00')")
    return now


def _school_year_of(d):
    y = d.year if d.month >= SCHOOL_YEAR_START_MONTH else d.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


# ----------------------------------------------------------------------------------------- record input
def _raw(rec):
    return rec["raw"] if isinstance(rec, dict) and "raw" in rec else rec


def _prov(rec, kind):
    if rec is None:
        return None
    raw = _raw(rec)
    return {"kind": kind, "uid": CL.UID_PREFIX + CL.raw_source_uid(kind, raw),
            "version": rec.get("version") if "raw" in rec else None,
            "content_hash": rec.get("content_hash") if "raw" in rec else None,
            "stato_registro": rec.get("source_status") if "raw" in rec else None}


# -------------------------------------------------------------------------------------- regole singole
def deadline(due, now):
    """L2 `due` (o None) -> blocco derivato `scadenza`. Confronto: data+ora esatta, oppure fine giornata locale."""
    now = parse_now(now)
    if not due:
        return {"presente": False, "precisione": None, "momento_locale": None, "data_locale": None,
                "giorni_mancanti": None, "passata": None, "nota": None}
    tz = _zone()
    today_local = now.astimezone(tz).date()
    if due.get("at_utc"):
        moment = dt.datetime.fromisoformat(due["at_utc"].replace("Z", "+00:00"))
        loc = moment.astimezone(tz)
        return {"presente": True, "precisione": "data_e_ora", "momento_locale": loc.strftime("%Y-%m-%dT%H:%M"),
                "data_locale": loc.date().isoformat(), "giorni_mancanti": (loc.date() - today_local).days,
                "passata": now >= moment, "nota": None}
    d = dt.date.fromisoformat(due["date_local"])
    end = dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)
    return {"presente": True, "precisione": "solo_data", "momento_locale": None, "data_locale": d.isoformat(),
            "giorni_mancanti": (d - today_local).days, "passata": now > end,
            "nota": "data senza ora: confrontata come fine giornata locale (23:59:59 %s)" % LOCAL_TZ}


def operational_state(work_state, submissions, scadenza):
    """-> (stato_operativo, motivo, note[]). `submissions`: le consegne trovate per il compito (0, 1 o piu')."""
    if work_state == "DELETED":
        return "rimosso", "compito DELETED in Classroom", []
    if work_state == "DRAFT":
        return "bozza", "compito DRAFT in Classroom", []
    if len(submissions) == 0:
        return "da_verificare", "nessuna consegna trovata per questo compito", []
    if len(submissions) > 1:
        return "da_verificare", "consegne multiple per lo stesso compito (%d): nessuna scelta" % len(submissions), []
    st = _raw(submissions[0]).get("state")
    if st in SUBMITTED:
        notes = ["consegna modificata dopo l'invio (STUDENT_EDITED_AFTER_TURN_IN)"] \
            if st == "STUDENT_EDITED_AFTER_TURN_IN" else []
        return SUBMITTED[st], "consegna %s" % st, notes
    if st in NOT_SUBMITTED:
        notes = ["consegna ritirata dallo studente (RECLAIMED_BY_STUDENT)"] if st == "RECLAIMED_BY_STUDENT" else []
        if not scadenza["presente"]:
            return "senza_scadenza", "consegna %s, nessuna scadenza" % st, notes + [NOTA_STATO["senza_scadenza"]]
        if scadenza["passata"]:
            return ("scaduto_non_consegnato", "consegna %s, scadenza passata" % st,
                    notes + [NOTA_STATO["scaduto_non_consegnato"]])
        return "aperto", "consegna %s, scadenza non ancora passata" % st, notes
    return "da_verificare", "stato consegna sconosciuto o assente: %r" % (st,), []


_YEAR_PAIR = re.compile(r"(?<!\d)(\d{4}|\d{2})\s*[/\-]\s*(\d{4}|\d{2})(?!\d)")


def _years_in_name(name):
    out = []
    for a, b in _YEAR_PAIR.findall(name or ""):
        if len(a) == 2:
            if len(b) != 2 or not (TWO_DIGIT_YEAR_RANGE[0] <= int(a) <= TWO_DIGIT_YEAR_RANGE[1]):
                continue
            y1 = 2000 + int(a)
        else:
            y1 = int(a)
        y2 = int(b) if len(b) == 4 else (y1 // 100) * 100 + int(b)
        if y2 == y1 + 1:
            val = "%d-%02d" % (y1, y2 % 100)
            if val not in out:
                out.append(val)
    return out


def _local_date(ts):
    if not (isinstance(ts, str) and len(ts) >= 10):
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(_zone()).date()
    except ValueError:
        return None


def course_year(course_raw):
    """anno_corso: CONTESTO, mai usato per il compito. Il nome del corso prevale; la data di creazione e' solo un
    candidato (un corso viene spesso riusato per piu' anni). -> {valore, certezza, fonte, candidati, nota}."""
    if course_raw is None:
        return {"valore": None, "certezza": "sconosciuta", "fonte": None, "candidati": [],
                "nota": "corso non presente nel registro"}
    cands = [{"valore": v, "fonte": "nome_corso", "evidenza": "il nome del corso contiene '%s'" % v}
             for v in _years_in_name(course_raw.get("name"))]
    created = _local_date(course_raw.get("creationTime"))
    if created:
        cands.append({"valore": _school_year_of(created), "fonte": "creazione_corso",
                      "evidenza": "corso creato il %s: indica solo quando e' nato il corso" % created})
    nota = "deduzione, non dato della fonte; contesto del corso, non anno dei compiti"
    by_name = [c for c in cands if c["fonte"] == "nome_corso"]
    if len(by_name) > 1:
        return {"valore": None, "certezza": "conflitto", "fonte": None, "candidati": cands,
                "nota": nota + ": il nome contiene piu' anni diversi"}
    if by_name:
        return {"valore": by_name[0]["valore"], "certezza": "alta", "fonte": "nome_corso", "candidati": cands, "nota": nota}
    if created:
        return {"valore": cands[-1]["valore"], "certezza": "bassa", "fonte": "creazione_corso", "candidati": cands,
                "nota": nota + ": solo anno di creazione (il corso puo' essere stato riusato)"}
    return {"valore": None, "certezza": "sconosciuta", "fonte": None, "candidati": [], "nota": nota + ": nessuna evidenza"}


def work_year(work_raw, due):
    """anno_compito dalle SOLE evidenze del compito: data di creazione e scadenza (locali). L'anno del corso non e'
    mai usato. Accordo -> alta; una sola evidenza -> media; disaccordo -> conflitto (nessun valore)."""
    cands = []
    created = _local_date(work_raw.get("creationTime"))
    if created:
        cands.append({"valore": _school_year_of(created), "fonte": "creazione_compito",
                      "evidenza": "compito creato il %s" % created})
    if due and due.get("data_locale"):
        d = dt.date.fromisoformat(due["data_locale"])
        cands.append({"valore": _school_year_of(d), "fonte": "scadenza_compito", "evidenza": "scadenza il %s" % d})
    nota = "deduzione dalle date del compito, non dato della fonte"
    vals = sorted({c["valore"] for c in cands})
    if not cands:
        return {"valore": None, "certezza": "sconosciuta", "fonte": None, "candidati": [], "nota": nota + ": nessuna data"}
    if len(vals) > 1:
        return {"valore": None, "certezza": "conflitto", "fonte": None, "candidati": cands,
                "nota": nota + ": creazione e scadenza in anni scolastici diversi (es. compiti per le vacanze)"}
    return {"valore": vals[0], "certezza": "alta" if len(cands) == 2 else "media",
            "fonte": "+".join(c["fonte"] for c in cands), "candidati": cands, "nota": nota}


def perimeter(anno_compito, manual_year=None):
    """Perimetro OPERATIVO (CLASSI) vs storico. Etichetta pura: non esclude nulla.
    Decisione manuale -> prevale. Anno determinato -> operativo/storico. Conflitto: operativo se TUTTI i candidati
    sono nel perimetro, storico se nessuno, altrimenti da_verificare. Anno sconosciuto -> da_verificare."""
    if manual_year:
        val = "operativo" if manual_year in CLASSI else "storico"
        return {"valore": val, "anno": manual_year, "classe": CLASSI.get(manual_year), "motivo": "anno del compito deciso nel livello manuale"}
    if anno_compito["valore"]:
        y = anno_compito["valore"]
        val = "operativo" if y in CLASSI else "storico"
        return {"valore": val, "anno": y, "classe": CLASSI.get(y),
                "motivo": "anno del compito %s (%s)" % (y, "nel perimetro" if y in CLASSI else "fuori dal perimetro")}
    cand = {c["valore"] for c in anno_compito["candidati"]}
    if cand and cand <= set(CLASSI):
        return {"valore": "operativo", "anno": None, "classe": None,
                "motivo": "anno del compito incerto, ma tutti i candidati sono nel perimetro"}
    if cand and not (cand & set(CLASSI)):
        return {"valore": "storico", "anno": None, "classe": None,
                "motivo": "anno del compito incerto, ma nessun candidato e' nel perimetro"}
    return {"valore": "da_verificare", "anno": None, "classe": None,
            "motivo": "anno del compito non determinabile rispetto al perimetro"}


def subject_proposals(course_raw):
    """PROPOSTE di materia dal solo nome del corso (citazione esplicita). Mai una decisione."""
    if course_raw is None:
        return []
    n = subj.norm(course_raw.get("name"))
    return [{"subject": key, "origine": "nome_corso", "evidenza": "il nome del corso cita la materia",
             "stato": "proposto"} for key, rx in sorted(subj._EXPL.items()) if rx.search(n)]


def _manual_subject(manual, course_id, work_uid):
    conf = [dict(x, origine="manuale") for x in (manual.get("course_subjects") or {}).get(course_id) or []]
    conf.sort(key=lambda x: (x.get("subject") or "", x.get("ruolo") or ""))
    override = (manual.get("work_subject") or {}).get(work_uid)
    if override:
        return {"stato": "override", "confermate": conf, "override": override, "decisione": override}
    if len(conf) == 1:
        return {"stato": "confermata", "confermate": conf, "override": None, "decisione": conf[0]["subject"]}
    if len(conf) > 1:
        return {"stato": "ambigua_confermata", "confermate": conf, "override": None, "decisione": None}
    return {"stato": "non_assegnata", "confermate": [], "override": None, "decisione": None}


# ---------------------------------------------------------------------------------------------- vista
def build_work_views(courses, works, submissions, now, manual=None):
    """courses/works/submissions: record del registro ({raw, version, content_hash, source_status}) o RAW puri.
    -> {derive_version, riferimento_temporale, work_views[], consegne_orfane[], manuale_non_risolto[], conteggi}."""
    now = parse_now(now)
    courses, works, submissions = copy.deepcopy(courses), copy.deepcopy(works), copy.deepcopy(submissions)
    manual = copy.deepcopy(manual) or {}
    course_by_id = {}
    for c in courses:
        course_by_id.setdefault(_raw(c).get("id"), []).append(c)
    subs_by_key = {}
    for s in submissions:
        r = _raw(s)
        subs_by_key.setdefault((r.get("courseId"), r.get("courseWorkId")), []).append(s)
    work_keys = set()
    views = []
    for w in works:
        raw = _raw(w)
        n = CL.normalize_course_work(raw)
        key = (raw.get("courseId"), raw.get("id"))
        work_keys.add(key)
        subs = sorted(subs_by_key.get(key, []), key=lambda s: str(_raw(s).get("id")))
        crs_list = course_by_id.get(raw.get("courseId"), [])
        crs = crs_list[0] if len(crs_list) == 1 else None
        craw = _raw(crs) if crs else None
        todo = []
        scad = deadline(n["due"], now)
        stato, motivo, note = operational_state(raw.get("state"), subs, scad)
        sub_raw = _raw(subs[0]) if len(subs) == 1 else None
        consegna = {"presente": len(subs) == 1, "numero_trovate": len(subs),
                    "candidati": [CL.UID_PREFIX + CL.raw_source_uid("submission", _raw(s)) for s in subs] if len(subs) > 1 else [],
                    "uid": CL.UID_PREFIX + CL.raw_source_uid("submission", sub_raw) if sub_raw else None,
                    "stato_fonte": sub_raw.get("state") if sub_raw else None,
                    "late": sub_raw.get("late") if sub_raw else None,
                    "assigned_points": sub_raw.get("assignedGrade") if sub_raw else None,
                    "draft_points": sub_raw.get("draftGrade") if sub_raw else None,
                    "aggiornata": sub_raw.get("updateTime") if sub_raw else None,
                    "nota_punti": "punteggio Classroom, NON un voto scolastico"}
        if stato == "da_verificare":
            todo.append(motivo)
        if len(crs_list) > 1:
            todo.append("piu' corsi con lo stesso id %s: nessuno scelto" % raw.get("courseId"))
        elif crs is None:
            todo.append("corso %s non presente nel registro" % raw.get("courseId"))
        cstate = craw.get("courseState") if craw else None
        attivo = None if cstate not in CL.KNOWN["course_state"] else cstate == "ACTIVE"
        if craw and attivo is None:
            todo.append("stato del corso sconosciuto: %r" % (cstate,))
        for rec, what in ((w, "compito"), (sub_raw and subs[0], "consegna"), (crs, "corso")):
            if rec and "raw" in rec and rec.get("source_status") not in (None, "present"):
                todo.append("%s non piu' presente nella fonte (stato registro: %s)" % (what, rec["source_status"]))
        todo.extend("compito: %s" % x for x in n["interpreted"]["da_verificare"])
        uid = n["uid"]
        anno_corso = course_year(craw) if craw else {"valore": None, "certezza": "sconosciuta", "fonte": None,
                                                     "candidati": [], "nota": "corso non presente nel registro"}
        anno_compito = work_year(raw, scad)
        man_work_year = (manual.get("work_school_year") or {}).get(uid)
        perim = perimeter(anno_compito, man_work_year)
        if anno_compito["certezza"] in ("conflitto", "sconosciuta"):
            todo.append("anno del compito %s: %s" % (anno_compito["certezza"], anno_compito["nota"]))
        if anno_corso["certezza"] == "conflitto":
            todo.append("anno del corso in conflitto: %s" % anno_corso["nota"])
        if perim["valore"] == "da_verificare":
            todo.append("perimetro: %s" % perim["motivo"])
        mat = _manual_subject(manual, raw.get("courseId"), uid)
        views.append({
            "uid": uid, "derive_version": DERIVE_VERSION, "riferimento_temporale": now.isoformat(),
            "fonte": {
                "compito": {"id": raw.get("id"), "course_id": raw.get("courseId"), "titolo": raw.get("title"),
                            "descrizione": raw.get("description"), "tipo": raw.get("workType"),
                            "stato_fonte": raw.get("state"), "max_points": raw.get("maxPoints"),
                            "topic_id": raw.get("topicId"), "materiali": n["materials"], "link": raw.get("alternateLink"),
                            "creato": raw.get("creationTime"), "aggiornato": raw.get("updateTime")},
                "consegna": consegna,
                "corso": {"presente": crs is not None, "id": raw.get("courseId"),
                          "nome": craw.get("name") if craw else None, "stato_fonte": cstate},
            },
            "derivato": {"scadenza": scad, "stato_operativo": stato, "motivo_stato": motivo, "note_stato": note,
                         "corso_attivo": attivo, "anno_compito": anno_compito, "anno_corso": anno_corso,
                         "perimetro": perim,
                         "materia_proposte": subject_proposals(craw)},
            "manuale": {"materia": mat, "anno_compito": man_work_year,
                        "anno_corso": (manual.get("course_school_year") or {}).get(raw.get("courseId"))},
            "da_verificare": todo,
            "provenienza": {"course_work": _prov(w, "course_work"),
                            "submission": _prov(subs[0], "submission") if len(subs) == 1 else None,
                            "course": _prov(crs, "course")},
        })
    views.sort(key=lambda v: v["uid"])
    orphans = sorted(({"uid": CL.UID_PREFIX + CL.raw_source_uid("submission", _raw(s)),
                       "course_id": _raw(s).get("courseId"), "course_work_id": _raw(s).get("courseWorkId"),
                       "stato_fonte": _raw(s).get("state"), "nota": "consegna senza compito nel registro: riportata, non scartata"}
                      for key, lst in subs_by_key.items() if key not in work_keys for s in lst), key=lambda o: o["uid"])
    uids = {v["uid"] for v in views}
    unresolved = sorted({cid for sec in ("course_subjects", "course_school_year") for cid in (manual.get(sec) or {})
                         if cid not in course_by_id} | {u for sec in ("work_subject", "work_school_year")
                                                        for u in (manual.get(sec) or {}) if u not in uids})
    count = lambda f: dict(sorted(_count(f(v) for v in views).items(), key=lambda kv: str(kv[0])))
    return {"derive_version": DERIVE_VERSION, "riferimento_temporale": now.isoformat(),
            "work_views": views, "consegne_orfane": orphans,
            "manuale_non_risolto": [{"riferimento": u, "nota": "decisione manuale su un elemento assente dal registro"}
                                    for u in unresolved],
            "conteggi": {"compiti": len(views), "consegne_orfane": len(orphans),
                         "stato_operativo": count(lambda v: v["derivato"]["stato_operativo"]),
                         "perimetro": count(lambda v: v["derivato"]["perimetro"]["valore"])},
            "perimetro_operativo": {"classi": dict(CLASSI),
                                    "anno_corrente": _school_year_of(now.astimezone(_zone()).date()),
                                    "anno_corrente_nel_perimetro": _school_year_of(now.astimezone(_zone()).date()) in CLASSI}}


def operational_views(result, include_da_verificare=False):
    """Vista OPERATIVA (perimetro CLASSI) di un risultato di build_work_views. Non distruttiva: restituisce un nuovo
    dict; il risultato completo, lo storico e il registro restano intatti."""
    keep = {"operativo"} | ({"da_verificare"} if include_da_verificare else set())
    out = copy.deepcopy(result)
    out["work_views"] = [v for v in out["work_views"] if v["derivato"]["perimetro"]["valore"] in keep]
    out["vista"] = {"tipo": "operativa", "include_da_verificare": include_da_verificare,
                    "esclusi_storico": sum(1 for v in result["work_views"] if v["derivato"]["perimetro"]["valore"] == "storico"),
                    "esclusi_da_verificare": 0 if include_da_verificare else
                    sum(1 for v in result["work_views"] if v["derivato"]["perimetro"]["valore"] == "da_verificare")}
    return out


def _count(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def from_registry(reg_or_path, now, manual=None):
    """SOLA LETTURA. Accetta un Registry gia' aperto o il percorso di un registro su disco (aperto come copia in
    memoria con Registry.from_snapshot: nessun byte del file viene scritto). Usa la versione CORRENTE di ogni record."""
    import registry as R
    own = not isinstance(reg_or_path, R.Registry)
    reg = R.Registry.from_snapshot(reg_or_path) if own else reg_or_path
    try:
        def recs(kind):
            return [reg.get_record(CL.SOURCE, kind, r["source_uid"])
                    for r in reg.list_records(source=CL.SOURCE, kind=kind)]
        return build_work_views(recs("course"), recs("course_work"), recs("submission"), now, manual=manual)
    finally:
        if own:
            reg.close()
