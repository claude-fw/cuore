"""Corso Classroom -> materia/e. Modulo PURO (nessuna rete, nessun file), solo libreria standard.

Due cose distinte:
  * PROPOSTA automatica: NON persistente, ricalcolata qui a ogni chiamata dalle evidenze E1/E2/E3;
  * CONFERMA manuale: persistente, SOLO in `school_manual.json` (subjects.<materia>.classroom_course_ids), scritta
    dall'utente. Una proposta non diventa MAI una conferma: un corso entra nel contesto di una materia solo se il suo
    course_id e' nella lista manuale di quella materia.

Evidenze (solo dati verificabili, parole intere, nessun confronto approssimativo):
  E1  campo `subject` del corso Classroom (compilato dal docente);
  E2  nome del corso; "lettere" da sola e' evidenza DEBOLE (italiano + latino);
  E3  cognome di un docente ClasseViva presente come parola nel nome del corso -> materie di quel docente
      (escluse civica/studio, comuni a tutti i docenti).
Locuzioni riconosciute PRIMA delle parole singole: "storia dell'arte" -> arte (mai storia), "scienze motorie" /
"educazione fisica" -> motorie (mai scienze/fisica), "educazione civica" -> civica, "studio assistito" -> studio.

Categorie per corso: confirmed | proposed | excluded | non_attributed | ambiguous.
"""
from __future__ import annotations

import re

import cvv_subject as subj

# Materie del futuro sistema di agenti (decisione dell'utente, 2026-10-03) e materie escluse.
AGENT_SUBJECTS = ("italiano", "latino", "inglese", "storia", "filosofia", "matematica", "fisica", "scienze")
EXCLUDED_SUBJECTS = ("arte", "motorie", "civica", "religione", "studio")
CATEGORIES = ("confirmed", "proposed", "excluded", "non_attributed", "ambiguous")
MANUAL_SCHEMA = "cloud.school.manual/1"

# Locuzioni (testo normalizzato) da riconoscere e RIMUOVERE prima delle parole singole.
PHRASES = (
    (r"\b(disegno e )?storia dell'? ?arte\b", ("arte",)),
    (r"\bscienze motorie\b", ("motorie",)),
    (r"\beducazione fisica\b", ("motorie",)),
    (r"\beducazione civica\b", ("civica",)),
    (r"\bstudio assistito\b", ("studio",)),
    (r"\bscienze naturali\b", ("scienze",)),
    (r"\breligione cattolica\b", ("religione",)),
)
# Parole intere -> materie.
WORDS = {
    "matematica": ("matematica",), "mate": ("matematica",), "fisica": ("fisica",),
    "italiano": ("italiano",), "latino": ("latino",), "inglese": ("inglese",), "english": ("inglese",),
    "storia": ("storia",), "filosofia": ("filosofia",), "scienze": ("scienze",),
    "religione": ("religione",), "arte": ("arte",), "disegno": ("arte",), "motorie": ("motorie",),
    "civica": ("civica",),
}
WEAK_WORDS = {"lettere": ("italiano", "latino")}
TEACHER_SHARED = ("civica", "studio")          # materie di tutti i docenti: mai dedotte dal docente
MIN_SURNAME = 3


def _norm(text):
    return subj.norm(text or "")


def _scan(text):
    """-> (materie da locuzioni, parole riconosciute, materie deboli). Parole intere su testo normalizzato."""
    t = _norm(text)
    found, words = [], []
    for rx, keys in PHRASES:
        for m in re.finditer(rx, t):
            found.extend(keys)
            words.append(m.group(0))
        t = re.sub(rx, " ", t)
    weak = []
    for tok in re.findall(r"[a-z0-9]+", t):
        if tok in WORDS:
            found.extend(WORDS[tok])
            words.append(tok)
        elif tok in WEAK_WORDS:
            weak.extend(WEAK_WORDS[tok])
            words.append(tok)
    uniq = lambda xs: sorted(set(xs))           # noqa: E731
    return uniq(found), uniq(words), uniq(weak)


def _surname(teacher):
    """ClasseViva 'COGNOME NOME' -> cognome normalizzato (senza apostrofi), o None se troppo corto."""
    toks = re.findall(r"[a-z0-9]+", _norm(teacher).replace("'", ""))
    return toks[0] if toks and len(toks[0]) >= MIN_SURNAME else None


def teacher_index(subjects):
    """build_subjects(...) di ClasseViva -> {cognome: {"docente", "materie"}} (civica/studio esclusi)."""
    idx = {}
    for s in subjects or []:
        if s.get("key") in TEACHER_SHARED or not s.get("key"):
            continue
        for name in s.get("docenti_nomi") or []:
            sur = _surname(name)
            if sur:
                e = idx.setdefault(sur, {"docente": name, "materie": set()})
                e["materie"].add(s["key"])
    return idx


def evidence(course_raw, teachers=None):
    """Evidenze E1/E2/E3 per un corso (RAW Classroom). Nessuna decisione."""
    out = []
    if course_raw.get("subject"):
        keys, words, weak = _scan(course_raw["subject"])
        if keys or weak:
            out.append({"regola": "E1", "fonte": "campo subject di Classroom", "testo": course_raw["subject"],
                        "parole": words, "materie": keys or weak, "forza": "forte" if keys else "debole"})
    name = course_raw.get("name") or ""
    keys, words, weak = _scan(name)
    if keys:
        out.append({"regola": "E2", "fonte": "nome del corso", "testo": name,
                    "parole": [w for w in words if w not in WEAK_WORDS], "materie": keys, "forza": "forte"})
    if weak:
        out.append({"regola": "E2", "fonte": "nome del corso (parola generica)", "testo": name,
                    "parole": [w for w in words if w in WEAK_WORDS], "materie": weak, "forza": "debole"})
    name_toks = set(re.findall(r"[a-z0-9]+", _norm(name).replace("'", "")))
    for sur, t in sorted((teachers or {}).items()):
        if sur in name_toks:
            out.append({"regola": "E3", "fonte": "cognome del docente nel nome del corso",
                        "testo": "%s -> docente ClasseViva %s" % (sur, t["docente"]),
                        "parole": [sur], "materie": sorted(t["materie"]), "forza": "forte"})
    return out


def _decide(evs):
    """Evidenze -> (categoria automatica, materie proposte, motivazione). Mai 'confirmed'."""
    if not evs:
        return "non_attributed", [], "nessuna evidenza verificabile"
    strong = [e for e in evs if e["forza"] == "forte"]
    use = strong or evs
    sets = {e["regola"] + ":" + e["fonte"]: set(e["materie"]) for e in use}
    allk = set().union(*sets.values())
    agent, excl = allk & set(AGENT_SUBJECTS), allk & set(EXCLUDED_SUBJECTS)
    e1 = [set(e["materie"]) for e in use if e["regola"] == "E1"]
    e2 = [set(e["materie"]) for e in use if e["regola"] == "E2"]
    if e1 and e2 and not (set().union(*e1) & set().union(*e2)):
        return "ambiguous", sorted(agent), "E1 e E2 indicano materie diverse"
    if agent and excl:
        return "ambiguous", sorted(agent), "evidenze miste tra materie con agente ed escluse (%s)" % ", ".join(sorted(excl))
    if excl and not agent:
        return "excluded", [], "materia esclusa dal sistema agenti: %s" % ", ".join(sorted(excl))
    regs = sorted({e["regola"] for e in use})
    weak = "" if strong else " (solo evidenza debole)"
    return "proposed", sorted(agent), "evidenze %s%s" % ("+".join(regs), weak)


def validate_manual(manual):
    """school_manual.json (dict) -> ({materia: [course_id]}, avvisi). Solo le 8 materie; campi extra ignorati."""
    warnings = []
    subjects = (manual or {}).get("subjects") or {}
    if not isinstance(subjects, dict):
        return {}, ["school_manual.json: 'subjects' non e' un oggetto: ignorato"]
    out = {}
    for key, entry in sorted(subjects.items()):
        if key not in AGENT_SUBJECTS:
            warnings.append("school_manual.json: materia '%s' non ammessa (escluse o sconosciute): ignorata" % key)
            continue
        ids = (entry or {}).get("classroom_course_ids") or []
        if not isinstance(ids, list):
            warnings.append("school_manual.json: %s.classroom_course_ids non e' una lista: ignorato" % key)
            continue
        out[key] = sorted({str(x) for x in ids if str(x).strip()})
    return out, warnings


def confirmed_map(manual):
    """{course_id: set(materie confermate)} dalla sola lista manuale."""
    by_subject, _ = validate_manual(manual)
    out = {}
    for key, ids in by_subject.items():
        for cid in ids:
            out.setdefault(cid, set()).add(key)
    return out


def classify(courses, subjects=None, manual=None):
    """courses: RAW Classroom (o record con 'raw'). subjects: build_subjects di ClasseViva (per E3).
    manual: dict di school_manual.json (o None). -> {corsi[], conteggi, avvisi}. Puro e deterministico."""
    teachers = teacher_index(subjects)
    conf = confirmed_map(manual)
    _, warnings = validate_manual(manual)
    rows = []
    for c in courses:
        raw = c["raw"] if isinstance(c, dict) and "raw" in c else c
        cid = str(raw.get("id"))
        evs = evidence(raw, teachers)
        cat, keys, why = _decide(evs)
        confirmed = sorted(conf.get(cid, ()))
        if confirmed:
            cat, why = "confirmed", "confermato manualmente in school_manual.json"
        rows.append({"course_id": cid, "nome": raw.get("name"), "stato": raw.get("courseState"),
                     "subject_classroom": raw.get("subject"), "categoria": cat,
                     "materie_proposte": keys, "materie_confermate": confirmed,
                     "condiviso": len(confirmed or keys) > 1, "motivazione": why, "evidenze": evs})
    rows.sort(key=lambda r: (CATEGORIES.index(r["categoria"]), r["nome"] or "", r["course_id"]))
    known = {r["course_id"] for r in rows}
    for cid in sorted(conf):
        if cid not in known:
            warnings.append("school_manual.json: corso %s confermato ma assente dal registro" % cid)
    counts = {k: sum(1 for r in rows if r["categoria"] == k) for k in CATEGORIES}
    return {"corsi": rows, "conteggi": counts, "avvisi": warnings, "manuale_presente": bool(manual),
            "nota": "le proposte sono ricalcolate e NON persistenti; solo school_manual.json conferma un corso"}


def proposed_for(report, key):
    """course_id PROPOSTI (o ambigui) per la materia `key`, esclusi quelli gia' confermati per `key`."""
    return sorted(r["course_id"] for r in report["corsi"]
                  if r["categoria"] in ("proposed", "ambiguous", "confirmed") and key in r["materie_proposte"]
                  and key not in r["materie_confermate"])
