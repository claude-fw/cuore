"""M2 - Attribuzione materia (docente -> materia + EVIDENZA) per lezioni e agenda ClasseViva. Pura (nessuna rete, nessuna scrittura).

Principi:
  * il fatto resta separato dall'interpretazione: 'materia_api' (il subjectDesc originale) non viene mai modificato;
    la materia derivata e' un'interpretazione con criterio, confidenza ed evidenze;
  * mai inventare una materia: se l'evidenza non basta lo stato e' 'da_verificare', con i candidati;
  * un conflitto fra subjectDesc e testo e' REGISTRATO in 'conflitti' ma non cambia la materia ne' lo stato;
  * l'evidenza incrociata lezione->evento e' solo CORROBORATIVA: mai l'unica base di un'attribuzione;
  * il mapping docente->materie viene da /subjects di ClasseViva (cvv_subject.build_subjects), non e' duplicato qui;
  * la logica testuale per gli eventi e' quella gia' verificata di cvv_subject.resolve_subject (riusata, non riscritta).
Il risultato vive sotto 'interpreted.attribuzione' con 'attr_version' propria: non entra in raw, content_hash ne' interp_version.
"""
from __future__ import annotations

import collections

import cvv_client as cv
import cvv_subject as S

ATTR_RULES_REV = 1
# Indizi tematici AGGIUNTIVI usati SOLO per rilevare conflitti subjectDesc/testo nelle lezioni.
# Il resolver degli eventi (cvv_subject.TOPICS) resta intatto: M2 e' isolato da quella logica.
ATTR_EXTRA_TOPICS = {"fisica": r"gravitazion|energia potenziale"}
ST_OK, ST_CHECK = "determinata", "da_verificare"


import re  # noqa: E402
_EXTRA = {k: re.compile(v) for k, v in ATTR_EXTRA_TOPICS.items()}


def attr_version():
    """'r<rev>-<impronta delle tabelle>': cambia da sola se cambia una tabella di cvv_subject."""
    return cv.interp_version(ATTR_RULES_REV, S.SUBJECT_KEYS, S.EXPLICIT, S.TOPICS, ATTR_EXTRA_TOPICS,
                             list(S.NEVER_BY_TEACHER), S.ALLOW_UNIQUE_TEACHER)


def _ev(regola, fonte, testo, esito=None):
    e = {"regola": regola, "fonte": fonte, "testo": testo}
    if esito:
        e["esito"] = esito
    return e


def _out(api, derivata, stato, criterio, conf, candidati, evidenze, conflitti=None, avvisi=None):
    return {"attr_version": attr_version(), "materia_api": api, "materia_derivata": derivata, "stato": stato,
            "criterio": criterio, "confidenza": conf, "candidati": list(candidati), "evidenze": evidenze,
            "conflitti": conflitti or [], "avvisi": avvisi or []}


def _official_by_desc(subjects):
    return {S.norm(s["desc"]): s for s in subjects if s.get("desc")}


def _teacher_subjects(author, subjects, include_shared=False):
    a = S.norm(author)
    return [s for s in subjects if a and a in s["docenti"] and (include_shared or s["key"] not in S.NEVER_BY_TEACHER)]


def _topic_keys(text, keys):
    t = S.norm(text)
    hits = {}
    for k in keys:
        m = None
        for rx in (S._TOP.get(k), _EXTRA.get(k)):
            m = rx.search(t) if rx else None
            if m:
                break
        if m:
            hits[k] = m.group(0)
    return hits


# ---------------------------------------------------------------- lezioni
def attribute_lesson(raw, subjects):
    api = (raw.get("subjectDesc") or "").strip() or None
    author = (raw.get("authorName") or "").strip() or None
    arg = raw.get("lessonArg") or ""
    ev = []
    avvisi = []
    if raw.get("lessonType") and S.norm(raw.get("lessonType")) == "supplenza":
        avvisi.append("tipo lezione 'Supplenza': il docente potrebbe non essere quello titolare della classe")
    teach_all = _teacher_subjects(author, subjects, include_shared=True)
    if author:
        ev.append(_ev("L0", "authorName", author, "docente in /subjects: %s" % ("si" if teach_all else "NO")))
    if not api:
        cand = sorted(s["desc"] for s in _teacher_subjects(author, subjects))
        return _out(None, None, ST_CHECK, "subjectDesc mancante", "nessuna", cand,
                    ev + [_ev("L1", "subjectDesc", "", "assente")])
    official = _official_by_desc(subjects).get(S.norm(api))
    if official is None:
        cand = sorted(s["desc"] for s in _teacher_subjects(author, subjects))
        return _out(api, None, ST_CHECK, "subjectDesc non e' una materia ufficiale", "nessuna", cand,
                    ev + [_ev("L1", "subjectDesc", api, "non presente in /subjects")], avvisi=avvisi)
    ev.append(_ev("L1", "subjectDesc", api, "materia ufficiale (%s)" % official["key"]))
    if not teach_all:
        return _out(api, None, ST_CHECK, "docente non presente in /subjects", "nessuna", [official["desc"]], ev, avvisi=avvisi)
    if S.norm(author) not in official["docenti"]:
        return _out(api, None, ST_CHECK, "il docente non risulta tra i docenti di quella materia", "nessuna",
                    sorted(s["desc"] for s in teach_all), ev + [_ev("L2", "/subjects", official["desc"], "coppia docente-materia assente")],
                    avvisi=avvisi)
    ev.append(_ev("L2", "/subjects", "%s insegna %s" % (author, official["desc"]), "coppia docente-materia presente"))
    conflitti = []
    if official["key"] not in S.NEVER_BY_TEACHER and arg.strip():
        own = {s["key"] for s in _teacher_subjects(author, subjects)}
        hits = _topic_keys(arg, own)
        if hits and official["key"] not in hits:
            for k, w in sorted(hits.items()):
                conflitti.append({"tipo": "subjectDesc_vs_testo", "materia_api": official["key"], "materia_testo": k,
                                  "indizio": w, "nota": "argomento tipico di un'altra materia del docente; materia NON modificata"})
            ev.append(_ev("L3", "lessonArg", arg.strip(), "contrasta: argomenti di %s" % sorted(hits)))
        elif hits:
            ev.append(_ev("L3", "lessonArg", arg.strip(), "coerente (%s)" % sorted(hits)))
    return _out(api, official["desc"], ST_OK, "api:subjectDesc ufficiale", "alta", [official["desc"]], ev, conflitti, avvisi)


# ---------------------------------------------------------------- agenda
def lessons_index(lesson_raws):
    """(data, docente normalizzato) -> elenco delle sue lezioni di quel giorno [{uid, ora, materia, tipo}]."""
    idx = collections.defaultdict(list)
    for r in lesson_raws:
        if r.get("evtDate") and r.get("authorName") and r.get("subjectDesc"):
            idx[(str(r["evtDate"])[:10], S.norm(r["authorName"]))].append(
                {"uid": cv.normalize_lesson(r)["uid"], "ora": r.get("evtHPos"), "materia": r["subjectDesc"].strip(),
                 "tipo": r.get("lessonType")})
    return idx


def _subjects_of(lessons):
    return {x["materia"] for x in lessons}


def attribute_event(normalized_event, subjects, lessons_idx=None):
    """normalized_event: cv.normalize_event(raw). Base testuale = cvv_subject.resolve_subject (invariata)."""
    n = normalized_event
    r = S.resolve_subject(n.get("author"), n.get("text"), subjects, n.get("subject_api"))
    derivata = r["subject"]
    ev = [_ev("A0", "authorName", n.get("author") or "", "docente in /subjects: %s" % (
        "si" if _teacher_subjects(n.get("author"), subjects, True) else "NO"))]
    crit = r["subject_criterion"]
    if n.get("subject_api"):
        ev.append(_ev("A1", "subjectDesc", n["subject_api"], "fornito dall'API"))
    elif crit.startswith("testo:nome-materia"):
        ev.append(_ev("A2", "text", n.get("text") or "", "cita il nome della materia"))
    elif crit.startswith("docente-unico"):
        ev.append(_ev("A3", "/subjects", n.get("author") or "", "il docente insegna una sola materia"))
    elif crit.startswith("testo:argomento"):
        ev.append(_ev("A4", "text", n.get("text") or "", crit))
    else:
        ev.append(_ev("A5", "text", n.get("text") or "", crit))
    conflitti, avvisi = [], []
    stato = ST_OK if derivata else ST_CHECK
    if "conflitto" in crit or "contrastanti" in crit:
        conflitti.append({"tipo": "testo_vs_docente", "nota": crit})
    # evidenza incrociata: SOLO corroborativa (usata_per_assegnare e' sempre False)
    if lessons_idx is not None and n.get("author") and n.get("date"):
        lez = lessons_idx.get((n["date"][:10], S.norm(n["author"]))) or []
        les = _subjects_of(lez)

        def a6(esito, codice):
            e = _ev("A6", "lessons", "lezioni del docente il %s: %s" % (n["date"][:10], sorted(les)) if les
                    else "nessuna lezione del docente in quel giorno", esito)
            e.update(esito_codice=codice, lezioni_usate=lez, materie_lezioni=sorted(les), usata_per_assegnare=False)
            ev.append(e)
        if not les:
            a6("nessuna corroborazione", "assente")
        elif derivata:
            if derivata in les:
                a6("concorde", "concorde")
            else:
                conflitti.append({"tipo": "evento_vs_lezioni", "materia_derivata": derivata, "materie_lezioni": sorted(les),
                                  "nota": "nessuna lezione del docente quel giorno nella materia derivata; materia NON modificata"})
                a6("discorde", "discordante")
        else:
            a6("informativa: non usata per assegnare la materia", "informativa")
    return _out(n.get("subject_api"), derivata, stato, crit, r["subject_confidence"], r["subject_candidates"], ev, conflitti, avvisi)


def with_attribution(normalized, attribution):
    """Copia del normalizzato con 'interpreted.attribuzione' aggiunta: non altera gli altri campi."""
    out = dict(normalized)
    out["interpreted"] = dict(normalized.get("interpreted") or {}, attribuzione=attribution)
    return out


# ---------------------------------------------------------------- report
def _slim(uid, date, who, a):
    return {"uid": uid, "data": date, "docente": who, "materia_api": a["materia_api"], "materia_derivata": a["materia_derivata"],
            "criterio": a["criterio"], "confidenza": a["confidenza"], "candidati": a["candidati"],
            "conflitti": a["conflitti"], "avvisi": a["avvisi"]}


def _full(uid, date, who, a):
    d = _slim(uid, date, who, a)
    d["stato"] = a["stato"]
    d["evidenze"] = a["evidenze"]
    return d


def build_report(lesson_raws, event_raws, subjects, dettaglio=False):
    """Report di attribuzione su record gia' letti (sola lettura). dettaglio=True: evidenze complete dei record rilevanti
    (lezioni da_verificare/conflitto/avviso; tutti gli eventi, con l'evidenza A6) e riepilogo per docente-materia."""
    idx = lessons_index(lesson_raws)
    rep = {"attr_version": attr_version(), "dettaglio": bool(dettaglio)}
    for kind, items in (("lesson", [(r, cv.normalize_lesson(r)) for r in lesson_raws]),
                        ("agenda_event", [(r, cv.normalize_event(r)) for r in event_raws])):
        rows, fulls = [], []
        for raw, n in items:
            a = attribute_lesson(raw, subjects) if kind == "lesson" else attribute_event(n, subjects, idx)
            who = raw.get("authorName")
            rows.append(_slim(n["uid"], n.get("date"), who, a))
            fulls.append(_full(n["uid"], n.get("date"), who, a))
        stato = collections.Counter(ST_OK if r["materia_derivata"] else ST_CHECK for r in rows)
        rep[kind] = {
            "totale": len(rows), "per_stato": dict(stato),
            "per_criterio": dict(collections.Counter(r["criterio"].split(" (")[0] for r in rows)),
            "per_confidenza": dict(collections.Counter(r["confidenza"] for r in rows)),
            "da_verificare": [r for r in rows if not r["materia_derivata"]],
            "conflitti": [r for r in rows if r["conflitti"]],
            "avvisi": [r for r in rows if r["avvisi"]],
            "attribuzioni_media_confidenza": [r for r in rows if r["materia_derivata"] and r["confidenza"] != "alta"],
        }
        if dettaglio:
            if kind == "lesson":
                rel = [f for f in fulls if not f["materia_derivata"] or f["conflitti"] or f["avvisi"]]
                rep[kind]["dettaglio_record_rilevanti"] = rel
                rep[kind]["record_senza_rilievi"] = len(fulls) - len(rel)
                rep[kind]["per_docente_materia"] = sorted(
                    ({"docente": r["docente"], "materia_api": r["materia_api"], "materia_derivata": r["materia_derivata"], "n": c}
                     for r, c in ((dict(docente=k[0], materia_api=k[1], materia_derivata=k[2]), c) for k, c in collections.Counter(
                         (r["docente"], r["materia_api"], r["materia_derivata"]) for r in rows).items())),
                    key=lambda x: (str(x["docente"]), str(x["materia_api"])))
            else:
                rep[kind]["dettaglio_record"] = fulls
                a6 = collections.Counter(e.get("esito_codice") for f in fulls for e in f["evidenze"] if e["regola"] == "A6")
                rep[kind]["evidenza_A6_riepilogo"] = dict(a6)
                rep[kind]["evidenza_A6_nota"] = ("corroborativa: usata_per_assegnare e' sempre False; "
                                                 "la lezione non assegna mai da sola una materia")
    return rep
