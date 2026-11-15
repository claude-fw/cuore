"""M5 - COLLEGAMENTI E CONFLITTI DERIVATI tra ClasseViva e Google Classroom: `cloud.school.links/1`.
Modulo PURO e SOLA LETTURA (nessuna rete, nessuna scrittura, nessun orologio: `now` arriva dal chiamante).

Il Registry resta l'UNICA fonte canonica. Questo modulo non scrive nulla, non modifica ne' duplica i record originali (li cita con
uid + versione + hash) e non crea tabelle: i collegamenti sono INTERPRETAZIONI ricalcolate a ogni lettura, con una loro versione
(`link_version`). Tre livelli sempre separati: `dati` (canonico, citato), `interpretazione` (il collegamento: relazione, stato,
regola, prove), `freschezza` (operativo: eta' delle letture da cui dipende, M3).

Principi (non negoziabili):
  * NESSUN collegamento e' certo: Classroom non contiene riferimenti a id ClasseViva. Lo stato `confermato` e' RISERVATO (valore
    dello schema) e il motore automatico non lo produce MAI; in M5 v1 non esistono conferme manuali;
  * data, materia, docente, titolo/argomento sono EVIDENZE, non verita': ogni collegamento elenca le prove (codice, esito, peso);
  * i falsi positivi sono piu' gravi dei falsi negativi: nel dubbio nessun collegamento, o `non_determinabile`, o `conflitto`;
  * un `conflitto` riporta i valori delle due fonti e i candidati: NON sceglie e NON modifica nessuna fonte;
  * criteri espliciti e deterministici: stesso Registry + stesso `now` -> stesso JSON, indipendente dall'ordine di lettura;
  * la materia di un elemento Classroom e' `determinata` solo se il corso e' monomateria CONFERMATO oppure il topic nomina UNA sola
    materia del corso condiviso; altrimenti `da_verificare` e il collegamento non puo' superare `non_determinabile`;
  * relazioni: `prepara` (un elemento Classroom prepara una verifica: NON e' identita'), `stesso_argomento`, `stessa_scadenza`.
    Nessuna identita' automatica. Le relazioni interne a ClasseViva e le date relative ("per lunedi") sono fuori da M5 v1.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import re

import cls_derive as D
import cls_registry as CL
import cls_subjects as CS
import cvv_attribution as CA
import cvv_client as cv
import cvv_registry as CR
import cvv_subject as S
import it_dates as ID
import registry as R
import school_common as CM
import school_context3 as C3

SCHEMA = "cloud.school.links/1"
EXPLAIN_SCHEMA = "cloud.school.links.explain/1"
LINK_RULES_REV = 1

WINDOW_PREPARA = 21            # giorni: l'elemento Classroom e' pubblicato da 0 a 21 giorni PRIMA della data dell'evento
WINDOW_LESSON = 3              # giorni: |pubblicazione - data lezione| <= 3
DATE_CONFLICT_DAYS = 14        # data attribuita alla verifica diversa da quella dell'evento, entro 14 giorni
DEADLINE_CONFLICT_DAYS = 7     # scadenza Classroom diversa dalla data del compito ClasseViva, entro 7 giorni
MIN_TOKEN = 5                  # lunghezza minima di una parola significativa (prima dello stemming)
LONG_STEM = 6                  # un solo stem in comune basta (peso 'media') se lungo almeno cosi'
TEXT_CAP = 400
LIMITS = {"collegamenti": 200, "conflitti": 100, "non_determinabili": 100}

STATES = ("confermato", "probabile", "proposto", "conflitto", "non_determinabile")
RESERVED_STATES = ("confermato",)              # mai prodotti dal motore automatico
RELATIONS = ("prepara", "stesso_argomento", "stessa_scadenza")
SOURCE_KINDS = ("course_work", "material", "announcement")
_FRESH_ORDER = CM.FRESH_ORDER                  # alias di compatibilita' (primitiva in school_common)

STOP = """dello della delle degli negli nello nella nelle sullo sulla sulle sugli dalla dalle dallo dagli anche quindi dopo prima
questo questa questi queste quello quella essere stato stata stati state sono siamo avere fatto fatta fatti fare fanno dove quando
come cosa cose molto tutti tutto tutta tutte ogni altro altra altri altre sempre ancora oppure invece perche pero cioe dunque
quale quali verso circa sopra sotto sulla contro senza durante mentre ecco vostro vostra nostro nostra loro""".split()
GENERIC = """verifica verifiche test interrogazione interrogazioni compito compiti esercizi esercizio ripasso ripassare ripassate
simulazione preparazione prepara classe classi lezione lezioni capitolo capitoli paragrafo paragrafi pagina pagine slide slides
materiale materiali studiare studiate studio svolgere svolgete leggere leggete guardare guardate scritto scritta scritte orale orali
casa domani oggi lunedi martedi mercoledi giovedi venerdi sabato domenica gennaio febbraio marzo aprile maggio giugno luglio agosto
settembre ottobre novembre dicembre ragazzi buongiorno buonasera ricordo ricordate allego allegato allegati portate argomento
argomenti tema temi soluzione soluzioni inizio inizia fine prova prove classroom libro libri testo testi lavoro lavori attivita
pronti pronto settimana prossima prossimo prossimi mattina pomeriggio ultimo ultima ultimi ultime nuovo nuova nuovi nuove scuola
docente professoressa professore compagni consegna consegnare consegne link file video documento documenti foto immagine immagini
interattive interattivo interattivi domanda domande risposta risposte quiz appunti
matematica fisica italiano latino inglese storia filosofia scienze scienza lingua cultura straniera letteratura laboratorio""".split()

_TEST = CM.TEST_RE                              # alias di compatibilita' (primitive in school_common)
_ATTR = CM.DATE_ROLE_RE


def stem(tok):
    """Stemming MINIMO e deterministico: toglie UNA vocale finale (a/e/i/o) se la parola ha almeno 6 lettere."""
    return tok[:-1] if len(tok) >= 6 and tok[-1] in "aeio" else tok


_GEN = set(STOP) | set(GENERIC)
_GEN_STEMS = {stem(w) for w in _GEN}


def specific_stems(text):
    """Insieme degli stem 'specifici' del testo: parole >= MIN_TOKEN lettere, non stopword, non termini generici (verifica,
    compiti, nomi di materia...). Deterministico; i limiti (plurali irregolari, sinonimi) producono falsi NEGATIVI, non positivi."""
    out = set()
    for tok in re.findall(r"[a-z]+", S.norm(text)):
        if len(tok) < MIN_TOKEN or tok in _GEN:
            continue
        s = stem(tok)
        if s not in _GEN_STEMS:
            out.add(s)
    return out


def link_version():
    """'r<rev>-<impronta delle tabelle>': cambia da sola se cambiano finestre, liste o soglie."""
    return cv.interp_version(LINK_RULES_REV, [WINDOW_PREPARA, WINDOW_LESSON, DATE_CONFLICT_DAYS, DEADLINE_CONFLICT_DAYS,
                                              MIN_TOKEN, LONG_STEM], sorted(STOP), sorted(GENERIC), _TEST.pattern,
                             _ATTR.pattern, list(STATES), list(RELATIONS))


_sha = CM.short_hash


def _d(iso):
    return dt.date.fromisoformat(iso)


def _cap(s):
    return CM.cap_text(s, TEXT_CAP)


# ------------------------------------------------------------------------------------------------ materia
def item_subject(course_subjects, topic_name):
    """Materia di un elemento Classroom di un corso CONFERMATO. `course_subjects` = materie confermate del corso.
    Mai inventata: determinata solo se corso monomateria o topic che nomina UNA sola materia del corso."""
    cs = sorted(course_subjects)
    if len(cs) == 1:
        return {"key": cs[0], "stato": "determinata", "candidati": cs, "criterio": "corso monomateria confermato"}
    if topic_name:
        found, _, _ = CS._scan(topic_name)
        inside = sorted(set(found) & set(cs))
        if len(found) == 1 and len(inside) == 1:
            return {"key": inside[0], "stato": "determinata", "candidati": cs,
                    "criterio": "corso condiviso: il topic '%s' nomina una sola materia del corso" % topic_name}
        why = "il topic '%s' non nomina una materia del corso" % topic_name if not found else \
            "il topic '%s' non individua una sola materia del corso" % topic_name
    else:
        why = "elemento senza topic (gli annunci non hanno topic)"
    return {"key": None, "stato": "da_verificare", "candidati": cs,
            "criterio": "corso condiviso (%s): %s" % ("/".join(cs), why)}


def event_subject(attr, d2k):
    """Attribuzione M2 -> {key, stato, candidati}. Determinata solo se M2 la determina e la materia e' nel catalogo."""
    key = d2k.get(S.norm(attr["materia_derivata"] or "")) if attr["stato"] == CA.ST_OK else None
    cand = sorted({d2k.get(S.norm(c), c if c in d2k.values() else None) for c in attr["candidati"]} - {None})
    if key:
        return {"key": key, "stato": "determinata", "candidati": [key], "criterio": "M2: %s" % attr["criterio"]}
    return {"key": None, "stato": "da_verificare", "candidati": cand, "criterio": "M2 non determina la materia: %s" % attr["criterio"]}


def compat(a, b):
    """'uguale' | 'indeterminata' | 'incompatibile' tra due materie {key, candidati}. Incompatibile = veto."""
    if a["key"] and b["key"]:
        return "uguale" if a["key"] == b["key"] else "incompatibile"
    ca = {a["key"]} if a["key"] else set(a["candidati"])
    cb = {b["key"]} if b["key"] else set(b["candidati"])
    if not ca or not cb:
        return "indeterminata"
    return "indeterminata" if ca & cb else "incompatibile"


# ------------------------------------------------------------------------------------------------ raccolta (sola lettura)
_attributed = CM.date_attributed


def collect(reg, subjects, manual=None):
    """Legge dal Registry (solo lettura) e prepara le strutture di lavoro. Ritorna dict con events, lessons, items e conteggi."""
    confirmed = CS.confirmed_map(manual or {})
    d2k = C3._desc_to_key(subjects)
    diag = collections.Counter()

    lessons_full, gone_l = C3._present(reg, CR.SOURCE, "lesson")
    events_full, gone_e = C3._present(reg, CR.SOURCE, "agenda_event")
    idx = CA.lessons_index([f["raw"] for f in lessons_full])

    events = []
    for f in events_full:
        n = CR.normalized("agenda_event", f["raw"])
        if not n.get("date"):
            diag["eventi_senza_data"] += 1
            continue
        a = CA.attribute_event(n, subjects, idx)
        events.append({"uid": n["uid"], "kind": n["kind"], "date": n["date"][:10], "sy": ID.school_year_start(n["date"][:10]),
                       "text": n["text"] or "", "author": n.get("author"), "subject_api": n.get("subject_api"),
                       "subject": event_subject(a, d2k), "attr": a, "cite": C3._cite(f, n["uid"]),
                       "stems": specific_stems(n["text"])})
    lessons = []
    for f in lessons_full:
        raw = f["raw"]
        day = str(raw.get("evtDate") or "")[:10]
        arg = (raw.get("lessonArg") or "").strip()
        if not day:
            diag["lezioni_senza_data"] += 1
            continue
        uid = cv.normalize_lesson(raw)["uid"]
        a = CA.attribute_lesson(raw, subjects)
        lessons.append({"uid": uid, "date": day, "sy": ID.school_year_start(day), "argomento": arg, "author": raw.get("authorName"),
                        "tipo": raw.get("lessonType"), "subject_api": raw.get("subjectDesc"), "subject": event_subject(a, d2k),
                        "attr": a, "cite": C3._cite(f, uid), "stems": specific_stems(arg),
                        "group": (day, S.norm(arg))})

    courses = {}
    for f in reg.list_records(source=CL.SOURCE, kind="course", status=R.PRESENT):
        full = reg.get_record(CL.SOURCE, "course", f["source_uid"])
        courses[str(full["raw"].get("id"))] = full["raw"]
    topics = {}
    for f in reg.list_records(source=CL.SOURCE, kind="topic", status=R.PRESENT):
        raw = reg.get_record(CL.SOURCE, "topic", f["source_uid"])["raw"]
        topics[(raw.get("courseId"), raw.get("topicId"))] = raw.get("name")

    items = []
    for kind in SOURCE_KINDS:
        full_list, gone = C3._present(reg, CL.SOURCE, kind)
        diag["classroom_scomparsi_dalla_fonte"] += gone
        for full in full_list:
            raw = full["raw"]
            if raw.get("state") != "PUBLISHED":
                diag["classroom_non_pubblicati"] += 1
                continue
            cid = str(raw.get("courseId"))
            if cid not in confirmed:
                diag["classroom_corsi_non_confermati"] += 1
                continue
            n = CL.normalized(kind, raw)
            created = D._local_date(raw.get("creationTime"))
            if created is None:
                diag["classroom_senza_data_creazione"] += 1
                continue
            created = created.isoformat()
            sy = ID.school_year_start(created)
            tname = topics.get((raw.get("courseId"), raw.get("topicId"))) if raw.get("topicId") else None
            title = n.get("title") if kind != "announcement" else None
            body = n.get("description") if kind != "announcement" else n.get("text")
            testo = "\n".join(x for x in (title, body) if x) or ""
            tn = S.norm(testo)
            dates = []
            for d in ID.parse_dates(testo, sy):
                d = dict(d)
                d["attribuita"] = _attributed(tn, d)
                dates.append(d)
            due = ((n.get("due") or {}).get("date_local")) if kind == "course_work" else None
            items.append({"uid": n["uid"], "tipo": kind, "course_id": cid, "course_name": (courses.get(cid) or {}).get("name"),
                          "topic_name": tname, "titolo": title, "testo": testo, "created": created, "sy": sy, "due": due,
                          "subject": item_subject(confirmed[cid], tname), "cite": C3._cite(full, n["uid"]),
                          "stems": specific_stems(testo), "test": bool(_TEST.search(tn)), "dates": dates,
                          "work_type": n.get("work_type") if kind == "course_work" else None})
    for lst in (events, lessons, items):
        lst.sort(key=lambda x: x["uid"])
    return {"events": events, "lessons": lessons, "items": items, "diag": diag, "confirmed": confirmed,
            "gone": {"agenda_event": gone_e, "lesson": gone_l}}


# ------------------------------------------------------------------------------------------------ valutazione
_p = CM.prove


def _k6(author, course_name):
    sur = CS._surname(author)
    if sur and sur in set(re.findall(r"[a-z0-9]+", S.norm(course_name or "").replace("'", ""))):
        return [_p("K6", "docente", "a_favore", "corroborativa",
                   "il cognome '%s' del docente ClasseViva compare nel nome del corso Classroom (non conta per le soglie)" % sur)]
    return []


def _media(shared):
    return len(shared) >= 2 or (len(shared) == 1 and len(next(iter(shared))) >= LONG_STEM)


def _k3(shared):
    if not shared:
        return _p("K3", "argomento", "neutro", "debole", "nessuna parola specifica in comune")
    peso = "media" if _media(shared) else "debole"
    return _p("K3", "argomento", "a_favore", peso, "parole specifiche in comune (stem): %s" % ", ".join(sorted(shared)))


def _subject_prove(comp, a, b):
    ka = a["key"] or "?/".join(a["candidati"]) or "sconosciuta"
    kb = b["key"] or "?/".join(b["candidati"]) or "sconosciuta"
    if comp == "uguale":
        return _p("K2", "materia", "a_favore", "necessaria", "materia determinata e uguale su entrambi i lati: %s" % ka)
    return _p("K2", "materia", "neutro", "necessaria", "materia NON determinata su almeno un lato (%s vs %s): collegamento "
              "non possibile oltre 'non_determinabile'" % (ka, kb))


def _eval_event_item(e, it, stats):
    """Valuta (evento ClasseViva, elemento Classroom). -> None se vetato, altrimenti dict di valutazione."""
    if e["sy"] != it["sy"]:
        stats["veto_anno_scolastico_diverso"] += 1
        return None
    delta = (_d(e["date"]) - _d(it["created"])).days
    if delta < 0:
        stats["veto_elemento_posteriore"] += 1
        return None
    if delta > WINDOW_PREPARA:
        stats["veto_fuori_finestra"] += 1
        return None
    comp = compat(e["subject"], it["subject"])
    if comp == "incompatibile":
        stats["veto_materia_incompatibile"] += 1
        return None
    prove = [_subject_prove(comp, e["subject"], it["subject"]),
             _p("K4", "finestra temporale", "a_favore", "debole",
                "elemento pubblicato %d giorni prima della data dell'evento (finestra %d)" % (delta, WINDOW_PREPARA))]
    eq_forte = eq_media = False
    diff_attr = []
    for d in it["dates"]:
        if d["data"] is None:
            continue
        if d["stato"] == "weekday_incoerente":
            prove.append(_p("K1", "data esplicita", "neutro", "debole", "'%s' ha il giorno della settimana incoerente: NON usata" % d["testo"]))
            continue
        if d["data"] == e["date"]:
            if d["attribuita"] and e["kind"] == "verifica":
                eq_forte = True
                prove.append(_p("K1", "data esplicita", "a_favore", "forte", "'%s' (%s%s) e' attribuita alla verifica ed e' uguale alla data dell'evento" % (
                    d["testo"], d["data"], "" if d["anno_esplicito"] else ", anno dedotto dall'anno scolastico")))
            else:
                eq_media = True
                prove.append(_p("K1", "data esplicita", "a_favore", "media", "'%s' (%s) e' uguale alla data dell'evento, ma il testo non la "
                                "attribuisce a una verifica: ruolo della data incerto" % (d["testo"], d["data"])))
        elif d["attribuita"] and e["kind"] == "verifica":
            diff_attr.append(d)
            prove.append(_p("K1", "data esplicita", "contro", "media", "'%s' (%s) e' attribuita a una verifica ma e' DIVERSA dalla data dell'evento (%s)" % (
                d["testo"], d["data"], e["date"])))
    k1 = "forte" if eq_forte else ("media" if eq_media else None)
    k5 = bool(it["test"] and e["kind"] == "verifica")
    if k5:
        prove.append(_p("K5", "tipo compatibile", "a_favore", "media", "il testo Classroom cita una verifica/test e l'evento e' una verifica"))
    shared = e["stems"] & it["stems"]
    prove.append(_k3(shared))
    prove += _k6(e["author"], it["course_name"])
    media = _media(shared)
    if k1 == "forte":
        tier = 3
    elif (k1 == "media" and (k5 or media)) or media:
        tier = 2
    elif k5:
        tier = 1
    else:
        tier = 0
    if tier == 1 and diff_attr:
        tier = 0                       # il testo attribuisce la verifica a un'ALTRA data: la sola menzione non basta (falso positivo)
    return {"tier": tier, "n": len(shared), "k1": k1, "k5": k5, "media": media, "shared": sorted(shared), "comp": comp,
            "prove": prove, "diff_attr": diff_attr, "delta": delta, "e": e,
            "relazione": "prepara" if (k5 or k1 == "forte") else "stesso_argomento"}


def _rule_event(ev):
    t = ev["tier"]
    if t == 3:
        return "R1", "probabile", "data esplicita attribuita alla verifica, uguale alla data dell'evento, stessa materia, finestra rispettata, candidato unico"
    if t == 2 and ev["k1"] == "media":
        return "R2", "proposto", "data uguale ma non attribuita alla verifica + (menzione di verifica | argomento in comune), stessa materia, finestra rispettata, candidato unico"
    if t == 2:
        return "R3", "proposto", "argomento in comune (peso medio), stessa materia, finestra rispettata, candidato unico; nessuna data esplicita"
    return "R4", "proposto", "il testo Classroom cita una verifica, stessa materia, finestra rispettata, unica verifica candidata; nessuna data ne' argomento in comune"


def _eval_lesson_item(le, it, stats):
    if le["sy"] != it["sy"]:
        stats["veto_anno_scolastico_diverso"] += 1
        return None
    delta = (_d(it["created"]) - _d(le["date"])).days
    if abs(delta) > WINDOW_LESSON:
        stats["veto_fuori_finestra"] += 1
        return None
    comp = compat(le["subject"], it["subject"])
    if comp == "incompatibile":
        stats["veto_materia_incompatibile"] += 1
        return None
    shared = le["stems"] & it["stems"]
    if not _media(shared):
        return None
    prove = [_subject_prove(comp, le["subject"], it["subject"]),
             _p("K4", "finestra temporale", "a_favore", "debole", "elemento pubblicato %+d giorni rispetto alla lezione (finestra +/-%d)" % (delta, WINDOW_LESSON)),
             _k3(shared)] + _k6(le["author"], it["course_name"])
    return {"tier": 2, "n": len(shared), "shared": sorted(shared), "comp": comp, "prove": prove, "le": le, "delta": delta,
            "relazione": "stesso_argomento"}


def _eval_deadline(e, it, stats):
    if it["due"] is None:
        return None
    if ID.school_year_start(it["due"]) != e["sy"] or it["sy"] != e["sy"]:
        stats["veto_anno_scolastico_diverso"] += 1
        return None
    comp = compat(e["subject"], it["subject"])
    if comp == "incompatibile":
        stats["veto_materia_incompatibile"] += 1
        return None
    shared = e["stems"] & it["stems"]
    delta = (_d(it["due"]) - _d(e["date"])).days
    prove = [_subject_prove(comp, e["subject"], it["subject"]), _k3(shared)] + _k6(e["author"], it["course_name"])
    eq = delta == 0
    prove.append(_p("K1", "scadenza strutturata", "a_favore" if eq else "contro", "forte" if eq else "media",
                    "scadenza Classroom %s %s la data del compito ClasseViva %s" % (it["due"], "uguale a" if eq else "diversa da", e["date"])))
    if eq and _media(shared):
        tier = 3
    elif eq and shared:
        tier = 2
    else:
        tier = 0
    return {"tier": tier, "n": len(shared), "shared": sorted(shared), "comp": comp, "prove": prove, "e": e, "delta": delta,
            "eq": eq, "media": _media(shared), "relazione": "stessa_scadenza"}


# ------------------------------------------------------------------------------------------------ lati e oggetti di output
def _side_event(e):
    return {"sistema": "classeviva", "tipo": "agenda_event", "uid": e["uid"],
            "dati": {"data": e["date"], "testo": e["text"], "docente": e["author"], "materia_api": e["subject_api"]},
            "interpretazione": {"tipo_dedotto": e["kind"], "materia": e["subject"]["key"], "stato_materia": e["subject"]["stato"],
                                "candidati": e["subject"]["candidati"], "criterio": e["subject"]["criterio"],
                                "attr_version": e["attr"]["attr_version"], "conflitti_m2": len(e["attr"]["conflitti"])},
            "fonte": e["cite"]}


def _side_lesson(le):
    return {"sistema": "classeviva", "tipo": "lesson", "uid": le["uid"],
            "dati": {"data": le["date"], "argomento": le["argomento"] or None, "docente": le["author"],
                     "materia_api": le["subject_api"], "tipo_lezione": le["tipo"]},
            "interpretazione": {"materia": le["subject"]["key"], "stato_materia": le["subject"]["stato"],
                                "candidati": le["subject"]["candidati"], "criterio": le["subject"]["criterio"],
                                "attr_version": le["attr"]["attr_version"], "conflitti_m2": len(le["attr"]["conflitti"])},
            "fonte": le["cite"]}


def _side_item(it):
    txt, cut = _cap(it["testo"])
    return {"sistema": "classroom", "tipo": it["tipo"], "uid": it["uid"],
            "dati": {"corso": {"id": it["course_id"], "nome": it["course_name"]}, "topic": it["topic_name"],
                     "titolo": it["titolo"], "testo": txt, "testo_troncato": cut, "data_creazione": it["created"],
                     "scadenza": it["due"]},
            "interpretazione": {"materia": it["subject"]["key"], "stato_materia": it["subject"]["stato"],
                                "candidati": it["subject"]["candidati"], "criterio": it["subject"]["criterio"]},
            "fonte": it["cite"]}


def _materie(*subs):
    out = set()
    for s in subs:
        out |= {s["key"]} if s["key"] else set(s["candidati"])
    return sorted(out)


def _mk_link(ver, rel, stato, regola, motiv, a, b, prove, mancanti, materie):
    return {"link_id": _sha(ver, rel, a["uid"], a["fonte"]["content_hash"], b["uid"], b["fonte"]["content_hash"]),
            "relazione": rel, "stato": stato, "a": a, "b": b,
            "interpretazione": {"link_version": ver, "regola": regola, "motivazione": motiv, "prove": prove,
                                "informazioni_mancanti": mancanti, "materie_coinvolte": materie,
                                "nota": "interpretazione derivata: i record originali non sono stati modificati"},
            "_a_section": "agenda" if a["tipo"] == "agenda_event" else "lezioni"}


def _mk_conflict(ver, tipo, rel, regola, motiv, elemento, candidati, materie, valori=None):
    cand_ids = sorted((c["lato"]["uid"], c["lato"]["fonte"]["content_hash"]) for c in candidati)
    out = {"conflitto_id": _sha(ver, tipo, rel, elemento["uid"], elemento["fonte"]["content_hash"], *[x for p in cand_ids for x in p]),
           "tipo": tipo, "relazione": rel, "stato": "conflitto", "elemento": elemento,
           "candidati": sorted(candidati, key=lambda c: c["lato"]["uid"]),
           "interpretazione": {"link_version": ver, "regola": regola, "motivazione": motiv, "materie_coinvolte": materie,
                               "nota": "NESSUN candidato e' stato scelto e NESSUNA fonte e' stata modificata: il conflitto va risolto dall'utente"},
           "_a_section": "agenda" if any(c["lato"]["tipo"] == "agenda_event" for c in candidati) else "lezioni"}
    if valori:
        out["valori"] = valori
    return out


def _mk_nondet(ver, rel, a, b, motiv, tier, prove, materie):
    return {"relazione": rel, "stato": "non_determinabile", "a": a, "b": b,
            "interpretazione": {"link_version": ver, "regola": "RN1", "motivazione": motiv,
                                "evidenza_residua": {3: "forte", 2: "media"}.get(tier, "debole"), "prove": prove,
                                "materie_coinvolte": materie,
                                "nota": "nessun collegamento: la materia di almeno un lato non e' determinata; non e' un fatto"},
            "_a_section": "agenda" if a["tipo"] == "agenda_event" else "lezioni"}


def _k7(n_val, n_top):
    return _p("K7", "unicita'", "a_favore", "necessaria", "unico candidato di livello massimo tra %d valutati" % n_val if n_top == 1
              else "%d candidati di pari livello" % n_top)


# ------------------------------------------------------------------------------------------------ motore
def compute(prep):
    """Regole -> {collegamenti, conflitti, non_determinabili, veti}. Deterministico, nessuna mutazione dell'ingresso."""
    ver = link_version()
    stats = collections.Counter()
    links, confl, nondet = [], [], []
    ev_a = [e for e in prep["events"] if e["kind"] in ("verifica", "evento")]
    ev_c = [e for e in prep["events"] if e["kind"] == "compito"]
    stats["eventi_non_collegabili_in_v1"] = len(prep["events"]) - len(ev_a) - len(ev_c)

    for it in prep["items"]:
        b = _side_item(it)
        # --- famiglia A: evento verifica/evento <-> elemento Classroom
        evs = [r for r in (_eval_event_item(e, it, stats) for e in ev_a) if r and r["tier"] >= 1]
        det = [r for r in evs if r["comp"] == "uguale"]
        for r in evs:
            if r["comp"] == "indeterminata" and r["tier"] >= 2:
                a = _side_event(r["e"])
                nondet.append(_mk_nondet(ver, r["relazione"], a, b,
                                         "evidenza %s ma materia non determinata: evento (%s) vs elemento Classroom (%s)" % (
                                             {3: "forte", 2: "media"}[r["tier"]], r["e"]["subject"]["criterio"], it["subject"]["criterio"]),
                                         r["tier"], r["prove"], _materie(r["e"]["subject"], it["subject"])))
        if not any(r["tier"] == 3 for r in det):
            disc = [r for r in det if r["e"]["kind"] == "verifica" and r["media"] and any(
                abs((_d(d["data"]) - _d(r["e"]["date"])).days) <= DATE_CONFLICT_DAYS for d in r["diff_attr"])]
            for r in disc:
                det.remove(r)
                dd = sorted({d["data"] for d in r["diff_attr"]})
                confl.append(_mk_conflict(
                    ver, "data_discordante", "prepara", "RC3",
                    "stesso argomento e stessa materia, ma Classroom attribuisce la verifica a %s mentre ClasseViva la data al %s" % (
                        ", ".join(dd), r["e"]["date"]), b,
                    [{"lato": _side_event(r["e"]), "prove": r["prove"]}], _materie(r["e"]["subject"], it["subject"]),
                    {"classeviva": r["e"]["date"], "classroom": dd}))
        if det:
            top = max((r["tier"], r["n"]) for r in det)
            tops = [r for r in det if (r["tier"], r["n"]) == top]
            if len(tops) > 1:
                confl.append(_mk_conflict(
                    ver, "candidati_multipli", tops[0]["relazione"], "RC1",
                    "l'elemento Classroom e' compatibile con %d eventi ClasseViva con la stessa forza di evidenza: nessuno scelto" % len(tops),
                    b, [{"lato": _side_event(r["e"]), "prove": r["prove"]} for r in tops], _materie(it["subject"], *[r["e"]["subject"] for r in tops])))
            else:
                r = tops[0]
                rc2 = []
                if r["tier"] == 2 and r["k1"] == "media" and not r["media"] and it["test"]:
                    rc2 = [x for x in det if x is not r and x["k5"] and x["e"]["date"] != r["e"]["date"]]
                if rc2:
                    cs = [r] + rc2
                    confl.append(_mk_conflict(
                        ver, "segnali_discordanti", "prepara", "RC2",
                        "la data nel testo (non attribuita) punta all'evento del %s, ma il testo cita una verifica che corrisponde anche a "
                        "evento/i del %s: due segnali puntano a eventi diversi, nessuno scelto" % (
                            r["e"]["date"], ", ".join(sorted(x["e"]["date"] for x in rc2))),
                        b, [{"lato": _side_event(x["e"]), "prove": x["prove"]} for x in cs],
                        _materie(it["subject"], *[x["e"]["subject"] for x in cs])))
                else:
                    regola, stato, motiv = _rule_event(r)
                    prove = r["prove"] + [_k7(len(det), 1)]
                    miss = ["nessun riferimento strutturato tra ClasseViva e Classroom: il collegamento non puo' essere certo",
                            "conferma manuale non prevista in M5 v1"]
                    if r["relazione"] == "prepara" and r["tier"] < 3:
                        miss.append("nessuna data esplicita attribuita alla verifica nel testo Classroom")
                    links.append(_mk_link(ver, r["relazione"], stato, regola, motiv, _side_event(r["e"]), b, prove, miss,
                                          _materie(r["e"]["subject"], it["subject"])))

        # --- famiglia B: lezione <-> elemento Classroom (solo argomento, mai 'probabile')
        lres = [r for r in (_eval_lesson_item(le, it, stats) for le in prep["lessons"]) if r]
        ldet = [r for r in lres if r["comp"] == "uguale"]
        for r in lres:
            if r["comp"] == "indeterminata":
                nondet.append(_mk_nondet(ver, "stesso_argomento", _side_lesson(r["le"]), b,
                                         "argomento in comune ma materia non determinata: lezione (%s) vs elemento Classroom (%s)" % (
                                             r["le"]["subject"]["criterio"], it["subject"]["criterio"]),
                                         2, r["prove"], _materie(r["le"]["subject"], it["subject"])))
        if ldet:
            topn = max(r["n"] for r in ldet)
            tops = [r for r in ldet if r["n"] == topn]
            groups = sorted({r["le"]["group"] for r in tops})
            if len(groups) > 1:
                confl.append(_mk_conflict(
                    ver, "candidati_multipli", "stesso_argomento", "RC1",
                    "l'elemento Classroom e' compatibile con lezioni di %d argomenti/giorni diversi con la stessa forza di evidenza: nessuna scelta" % len(groups),
                    b, [{"lato": _side_lesson(r["le"]), "prove": r["prove"]} for r in tops],
                    _materie(it["subject"], *[r["le"]["subject"] for r in tops])))
            else:
                for r in tops:
                    links.append(_mk_link(
                        ver, "stesso_argomento", "proposto", "R5",
                        "argomento in comune (peso medio) tra lezione ed elemento Classroom, stessa materia, pubblicazione entro +/-%d giorni, "
                        "migliore corrispondenza unica" % WINDOW_LESSON, _side_lesson(r["le"]), b,
                        r["prove"] + [_k7(len(ldet), 1)],
                        ["nessun riferimento strutturato tra ClasseViva e Classroom: il collegamento non puo' essere certo",
                         "conferma manuale non prevista in M5 v1"], _materie(r["le"]["subject"], it["subject"])))

        # --- famiglia C: compito ClasseViva <-> scadenza Classroom
        if it["due"]:
            dres = [r for r in (_eval_deadline(e, it, stats) for e in ev_c) if r]
            ddet = [r for r in dres if r["comp"] == "uguale"]
            for r in dres:
                if r["comp"] == "indeterminata" and r["tier"] >= 2:
                    nondet.append(_mk_nondet(ver, "stessa_scadenza", _side_event(r["e"]), b,
                                             "scadenza uguale e argomento in comune ma materia non determinata", r["tier"], r["prove"],
                                             _materie(r["e"]["subject"], it["subject"])))
            good = [r for r in ddet if r["tier"] >= 2]
            if not good:
                for r in ddet:
                    if r["tier"] == 0 and r["media"] and not r["eq"] and abs(r["delta"]) <= DEADLINE_CONFLICT_DAYS:
                        confl.append(_mk_conflict(
                            ver, "data_discordante", "stessa_scadenza", "RC3",
                            "stesso argomento e stessa materia, ma la scadenza Classroom e' %s e la data del compito ClasseViva e' %s" % (
                                it["due"], r["e"]["date"]), b, [{"lato": _side_event(r["e"]), "prove": r["prove"]}],
                            _materie(r["e"]["subject"], it["subject"]), {"classeviva": r["e"]["date"], "classroom": [it["due"]]}))
            else:
                top = max((r["tier"], r["n"]) for r in good)
                tops = [r for r in good if (r["tier"], r["n"]) == top]
                if len(tops) > 1:
                    confl.append(_mk_conflict(
                        ver, "candidati_multipli", "stessa_scadenza", "RC1",
                        "la scadenza Classroom e' compatibile con %d compiti ClasseViva con la stessa forza di evidenza: nessuno scelto" % len(tops),
                        b, [{"lato": _side_event(r["e"]), "prove": r["prove"]} for r in tops],
                        _materie(it["subject"], *[r["e"]["subject"] for r in tops])))
                else:
                    r = tops[0]
                    reg_, stato, motiv = (("R6", "probabile", "scadenza strutturata uguale alla data del compito + argomento in comune (peso medio), "
                                           "stessa materia, candidato unico") if r["tier"] == 3 else
                                          ("R7", "proposto", "scadenza strutturata uguale alla data del compito + argomento in comune debole, "
                                           "stessa materia, candidato unico"))
                    links.append(_mk_link(ver, "stessa_scadenza", stato, reg_, motiv, _side_event(r["e"]), b,
                                          r["prove"] + [_k7(len(good), 1)],
                                          ["nessun riferimento strutturato tra ClasseViva e Classroom: il collegamento non puo' essere certo",
                                           "conferma manuale non prevista in M5 v1"], _materie(r["e"]["subject"], it["subject"])))

    links.sort(key=lambda x: (x["relazione"], x["a"]["uid"], x["b"]["uid"]))
    confl.sort(key=lambda x: (x["tipo"], x["elemento"]["uid"], x["conflitto_id"]))
    nondet.sort(key=lambda x: (x["relazione"], x["a"]["uid"], x["b"]["uid"]))
    assert all(x["stato"] in ("probabile", "proposto") for x in links)       # il motore non produce MAI 'confermato'
    return {"collegamenti": links, "conflitti": confl, "non_determinabili": nondet, "veti": dict(sorted(stats.items())),
            "link_version": ver}


# ------------------------------------------------------------------------------------------------ documento
LEVELS = {
    "dati": "canonico: campi della fonte copiati in 'dati' (testi lunghi troncati con flag) e citati in 'fonte' con uid + registry_version + content_hash",
    "interpretazione": "derivato: relazione, stato, regola, prove (codice/esito/peso), materia dei due lati; e' una lettura, non un fatto",
    "freschezza": "operativo: eta' e stato delle ultime letture da cui il collegamento dipende (M3); 'aggiornato' NON garantisce correttezza",
}
CRITERI = {
    "stati": {"confermato": "RISERVATO: solo una conferma manuale futura potra' produrlo; il motore automatico non lo produce mai (M5 v1: nessuna conferma manuale)",
              "probabile": "evidenza forte e candidato unico (regole R1, R6)",
              "proposto": "evidenza media e candidato unico (regole R2, R3, R4, R5, R7): da verificare",
              "conflitto": "segnali o candidati in contrasto (RC1 candidati multipli, RC2 segnali discordanti, RC3 data discordante): nessuna scelta, nessuna fonte modificata",
              "non_determinabile": "evidenza presente ma materia di un lato non determinata (RN1): nessun collegamento"},
    "relazioni": {"prepara": "un elemento Classroom prepara una verifica ClasseViva (NON e' identita')",
                  "stesso_argomento": "stesso argomento nella stessa materia (evento o lezione <-> elemento Classroom)",
                  "stessa_scadenza": "compito ClasseViva <-> compito Classroom con scadenza strutturata uguale"},
    "finestre_giorni": {"prepara": WINDOW_PREPARA, "lezione_materiale": WINDOW_LESSON, "conflitto_data_verifica": DATE_CONFLICT_DAYS,
                        "conflitto_scadenza": DEADLINE_CONFLICT_DAYS},
    "segnali": {"K1": "data esplicita (testo) o scadenza strutturata uguale alla data ClasseViva", "K2": "materia determinata e uguale (necessaria)",
                "K3": "parole specifiche in comune (media: >=2 stem, oppure 1 stem di >=%d lettere)" % LONG_STEM,
                "K4": "finestra temporale", "K5": "il testo Classroom cita una verifica e l'evento e' una verifica",
                "K6": "docente (solo corroborativa)", "K7": "unicita' del candidato di livello massimo"},
    "veti": ["anno scolastico diverso", "elemento Classroom pubblicato dopo la data dell'evento", "fuori finestra", "materia incompatibile"],
    "regole": {
        "R1": "probabile: data esplicita attribuita alla verifica == data evento + K2 + K4 + K7",
        "R2": "proposto: data uguale non attribuita + (K5 o K3 media) + K2 + K4 + K7",
        "R3": "proposto: K3 media + K2 + K4 + K7 (nessuna data)",
        "R4": "proposto: solo K5 (cita una verifica) + K2 + K4 + unica verifica candidata",
        "R5": "proposto: lezione, K3 media + K2 + finestra +/-3 giorni + migliore corrispondenza unica",
        "R6": "probabile: scadenza strutturata == data compito + K3 media + K2 + K7",
        "R7": "proposto: scadenza strutturata == data compito + K3 debole + K2 + K7",
        "RC1": "conflitto: >=2 candidati di pari livello", "RC2": "conflitto: la data punta a un evento, la parola 'verifica' a un altro",
        "RC3": "conflitto: stesso argomento, data (attribuita/scadenza) diversa tra le fonti",
        "RN1": "non_determinabile: evidenza ma materia non determinata"},
    "fuori_da_M5_v1": ["conferme manuali", "relazioni interne a ClasseViva (evento<->lezione)", "date relative ('per lunedi')",
                       "mappa manuale topic->materia", "annunci/bacheca ClasseViva (M6)", "consegne e voti"],
}
RULES_FOR_AGENTS = [
    "I collegamenti sono INTERPRETAZIONI derivate, non fatti: Classroom non contiene riferimenti a id ClasseViva. Presentali come tali.",
    "'probabile' e 'proposto' non sono 'confermato': dillo; 'confermato' non esiste in M5 v1.",
    "'prepara' non significa che l'elemento Classroom E' la verifica: significa che la prepara.",
    "Un 'conflitto' va mostrato con entrambi i valori: non scegliere un lato e non dire che una fonte ha torto.",
    "'non_determinabile' = nessun collegamento: non completare la materia mancante e non dedurla.",
    "Controlla 'freschezza': se un lato e' stale o senza_lettura dichiaralo; 'aggiornato' non garantisce correttezza.",
    "Per ogni affermazione cita uid + registry_version + content_hash di entrambi i lati (campo 'fonte').",
]


_worst = CM.worst_freshness


def _strip(o):
    return {k: v for k, v in o.items() if not k.startswith("_")}


def _resolve_materia(materia, subjects):
    if materia is None:
        return None
    if not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia non valida: %r" % (materia,))
    import school_context as SC
    return SC.resolve_subject(materia, subjects)


def _build(reg, subjects, now, manual=None, materia=None, stato=None, relazione=None, soglia_ore=None, source_path=None,
           limits=None):
    key = _resolve_materia(materia, subjects)
    if stato is not None and stato not in STATES:
        raise ValueError("stato non valido: %r (ammessi: %s)" % (stato, ", ".join(STATES)))
    if relazione is not None and relazione not in RELATIONS:
        raise ValueError("relazione non valida: %r (ammesse: %s)" % (relazione, ", ".join(RELATIONS)))
    now_dt = D.parse_now(now)
    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)
    prep = collect(reg, subjects, manual)
    res = compute(prep)

    kinds = {"agenda": C3.SECTION_KINDS["agenda"], "lezioni": C3.SECTION_KINDS["lezioni"],
             "classroom": [(CL.SOURCE, k) for k in ("course", "course_work", "material", "announcement", "topic")]}
    fsec = {n: C3.section_freshness(fresh, k) for n, k in kinds.items()}
    warn, miss = [], []
    for n, f in fsec.items():
        C3._freshness_warnings(warn, n, f)

    def sel(rows):
        out = []
        for x in rows:
            if key and key not in x["interpretazione"]["materie_coinvolte"]:
                continue
            if relazione and x["relazione"] != relazione:
                continue
            if stato and x["stato"] != stato:
                continue
            out.append(x)
        return out

    def fin(rows):
        out = []
        for x in rows:
            x = dict(x)
            x["freschezza"] = {"stato": _worst(fsec[x["_a_section"]]["stato"], fsec["classroom"]["stato"]),
                               "nota": C3.FRESH_NOTE}
            out.append(_strip(x))
        return out

    lim = dict(LIMITS, **(limits or {}))
    parts = {}
    for name, rows in (("collegamenti", res["collegamenti"]), ("conflitti", res["conflitti"]), ("non_determinabili", res["non_determinabili"])):
        s = sel(rows)
        shown, meta = C3.SC._capped(s, lim[name], "operativo", name) if lim[name] else (s, {"troncato": False})
        parts[name] = (fin(shown), s, meta)
        C3._truncation_warning(warn, "collegamenti", name, meta)
    shown_l, all_l = parts["collegamenti"][0], parts["collegamenti"][1]
    shown_c, all_c = parts["conflitti"][0], parts["conflitti"][1]
    shown_n, all_n = parts["non_determinabili"][0], parts["non_determinabili"][1]

    if all_c:
        C3._w(warn, "conflitti_presenti", "attenzione", "collegamenti",
              "%d conflitti tra le fonti o tra candidati: nessun candidato scelto, nessuna fonte modificata" % len(all_c),
              [x["conflitto_id"] for x in all_c])
    if all_n:
        C3._w(warn, "collegamenti_non_determinabili", "info", "collegamenti",
              "%d casi con evidenza ma materia non determinata (corsi condivisi senza topic o attribuzione M2 incerta): nessun collegamento" % len(all_n))
    stale_links = [x["link_id"] for x in shown_l if x["freschezza"]["stato"] != "aggiornato"]
    if stale_links:
        C3._w(warn, "collegamenti_con_fonte_non_aggiornata", "attenzione", "collegamenti",
              "collegamenti calcolati su letture stale o senza lettura: verifica la freschezza", stale_links)

    d = prep["diag"]
    n_ann_shared = sum(1 for it in prep["items"] if it["tipo"] == "announcement" and it["subject"]["stato"] != "determinata")
    if not prep["confirmed"]:
        miss.append({"sezione": "classroom", "messaggio": "nessun corso Classroom CONFERMATO (school_manual.json): nessun elemento Classroom e' stato considerato"})
    if d["classroom_corsi_non_confermati"]:
        miss.append({"sezione": "classroom", "messaggio": "%d elementi Classroom di corsi NON confermati non sono stati considerati" % d["classroom_corsi_non_confermati"]})
    if n_ann_shared:
        miss.append({"sezione": "classroom", "messaggio": "%d annunci di corsi condivisi non hanno topic: la materia non e' determinabile, al massimo 'non_determinabile'" % n_ann_shared})
    if not prep["events"] and not prep["lessons"]:
        miss.append({"sezione": "classeviva", "messaggio": "nessun evento di agenda ne' lezione ClasseViva nel registro"})
    miss.append({"sezione": "conferme", "messaggio": "M5 v1 non prevede conferme manuali: nessun collegamento puo' essere 'confermato'"})
    miss.append({"sezione": "date", "messaggio": "le date relative (es. 'per lunedi') non sono interpretate; solo date esplicite"})

    def count(rows, k):
        c = collections.Counter(x[k] for x in rows)
        return dict(sorted(c.items()))
    per_stato = {s: 0 for s in STATES}
    for x in all_l:
        per_stato[x["stato"]] += 1
    per_stato["conflitto"] = len(all_c)
    per_stato["non_determinabile"] = len(all_n)
    return {
        "schema": SCHEMA,
        "natura": "interpretazione derivata e in SOLA LETTURA: il Registry e' l'unica fonte canonica; nessun record originale e' stato modificato",
        "link_version": res["link_version"], "riferimento_temporale": now_dt.isoformat(),
        "livelli": LEVELS, "criteri": CRITERI,
        "filtri": {"materia": key, "stato": stato, "relazione": relazione},
        "collegamenti": shown_l, "conflitti": shown_c, "non_determinabili": shown_n,
        "riepilogo": {"per_stato": per_stato, "collegamenti_per_relazione": count(all_l, "relazione"),
                      "conflitti_per_tipo": count(all_c, "tipo"),
                      "elementi_considerati": {"eventi_classeviva": len(prep["events"]), "lezioni_classeviva": len(prep["lessons"]),
                                               "elementi_classroom": len(prep["items"]),
                                               "classroom_corsi_confermati": len(prep["confirmed"])},
                      "esclusi": {k: v for k, v in sorted(d.items())},
                      "veti_su_coppie": res["veti"]},
        "freschezza": {"calcolata_a": fresh["calcolato_a"], "soglia_ore": fresh["soglia_ore"],
                       "per_sezione": {n: f["stato"] for n, f in fsec.items()}, "dettaglio": fsec, "nota": C3.FRESH_NOTE},
        "warning": warn, "informazioni_mancanti": miss,
        "provenienza": {"schema_registro": R._version_of(reg.db), "link_version": res["link_version"], "attr_version": CA.attr_version(),
                        "derive_version": D.DERIVE_VERSION, "registro_migrato_in_memoria": bool(getattr(reg, "migrated_in_memory", False)),
                        "sorgente": source_path},
        "regole_per_gli_agenti": RULES_FOR_AGENTS,
    }, prep


def build(reg, subjects, now, manual=None, materia=None, stato=None, relazione=None, soglia_ore=None, source_path=None, limits=None):
    """cloud.school.links/1. Sola lettura; stesso registro + stesso now -> stesso JSON."""
    return _build(reg, subjects, now, manual, materia, stato, relazione, soglia_ore, source_path, limits)[0]


def explain(reg, subjects, now, target, manual=None, soglia_ore=None, source_path=None):
    """cloud.school.links.explain/1: tutto cio' che riguarda un uid ClasseViva/Classroom, un link_id o un conflitto_id."""
    if not (isinstance(target, str) and target.strip()):
        raise ValueError("target non valido: indica un uid (cvv:..., cvv:lesson:..., cls:...), un link_id o un conflitto_id")
    target = target.strip()
    doc, prep = _build(reg, subjects, now, manual, soglia_ore=soglia_ore, source_path=source_path, limits={k: 0 for k in LIMITS})
    uids = {x["uid"]: "evento ClasseViva (agenda)" for x in prep["events"]}
    uids.update({x["uid"]: "lezione ClasseViva" for x in prep["lessons"]})
    uids.update({x["uid"]: "elemento Classroom (%s)" % x["tipo"] for x in prep["items"]})
    L = [x for x in doc["collegamenti"] if target in (x["link_id"], x["a"]["uid"], x["b"]["uid"])]
    C = [x for x in doc["conflitti"] if target in (x["conflitto_id"], x["elemento"]["uid"]) or
         any(target == c["lato"]["uid"] for c in x["candidati"])]
    N = [x for x in doc["non_determinabili"] if target in (x["a"]["uid"], x["b"]["uid"])]
    if target in uids:
        tipo = "uid"
    elif L or C:
        tipo = "link_id" if any(x["link_id"] == target for x in L) else "conflitto_id"
    else:
        tipo = "sconosciuto"
    if tipo == "sconosciuto":
        esito = ("target non trovato tra gli elementi considerati: puo' non esistere, essere scomparso dalla fonte, non essere collegabile "
                 "(kind non previsto o corso Classroom non confermato) o essere fuori dal perimetro")
    elif not (L or C or N):
        esito = "elemento considerato ma NESSUN collegamento, conflitto o caso non determinabile: evidenze insufficienti (e' il risultato prudente)"
    else:
        esito = "%d collegamenti, %d conflitti, %d casi non determinabili" % (len(L), len(C), len(N))
    return {"schema": EXPLAIN_SCHEMA, "richiesto": target, "tipo_richiesta": tipo,
            "elemento_considerato": uids.get(target), "esito": esito,
            "collegamenti": L, "conflitti": C, "non_determinabili": N,
            "link_version": doc["link_version"], "riferimento_temporale": doc["riferimento_temporale"],
            "freschezza": doc["freschezza"], "warning": [w for w in doc["warning"] if w["codice"] in (
                "sezione_stale", "sezione_senza_lettura", "dati_legacy", "ultima_lettura_con_rifiuti")],
            "provenienza": doc["provenienza"], "criteri": doc["criteri"], "regole_per_gli_agenti": RULES_FOR_AGENTS}


def build_from_path(path, subjects, now, manual_path=None, materia=None, stato=None, relazione=None, soglia_ore=None):
    import os
    import school_context as SC
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, subjects, now, manual=SC.load_manual(manual_path)["dati"], materia=materia, stato=stato,
                     relazione=relazione, soglia_ore=soglia_ore, source_path=os.path.abspath(path))
    finally:
        reg.close()


def explain_from_path(path, subjects, now, target, manual_path=None, soglia_ore=None):
    import os
    import school_context as SC
    reg = R.Registry.from_snapshot(path)
    try:
        return explain(reg, subjects, now, target, manual=SC.load_manual(manual_path)["dati"], soglia_ore=soglia_ore,
                       source_path=os.path.abspath(path))
    finally:
        reg.close()
