"""Risoluzione della MATERIA di un evento di agenda. Pura (nessuna rete), solo stdlib.

Regola: mai inventare. La materia viene assegnata solo se un criterio verificabile la
determina; altrimenti il risultato e' "Materia da verificare" con i candidati.
Ogni risultato riporta il criterio usato, per poter correggere la logica in futuro.

Ordine dei criteri:
 1. api:subjectDesc          - ClasseViva fornisce la materia nell'evento (alta)
 2. testo:nome-materia       - il testo cita esplicitamente UNA materia del docente (alta)
 3. docente-unico            - il docente insegna una sola materia e il testo non la contraddice (media)
 4. testo:argomento          - il testo contiene argomenti tipici di UNA sola materia del docente (media)
 5. altrimenti               - "Materia da verificare" (ambiguo o in conflitto)
"""
from __future__ import annotations

import functools
import re
import unicodedata

LABEL_UNKNOWN = "Materia da verificare"
ALLOW_UNIQUE_TEACHER = True   # False => anche i docenti con una sola materia restano "da verificare"
NEVER_BY_TEACHER = ("civica", "studio")  # materie condivise da tutti i docenti: solo se citate nel testo


@functools.lru_cache(maxsize=1 << 16)   # funzione PURA testo -> testo: la cache non puo' restituire risultati obsoleti
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip()


# (chiave, regex sul NOME UFFICIALE della materia nel registro, normalizzato)
SUBJECT_KEYS = [
    ("religione", r"religione"), ("italiano", r"italiana"), ("latino", r"latina"),
    ("inglese", r"straniera"), ("storia", r"^storia$"), ("filosofia", r"filosofia"),
    ("matematica", r"matematica"), ("fisica", r"fisica"), ("scienze", r"scienze naturali"),
    ("arte", r"arte"), ("motorie", r"motorie"), ("civica", r"civica"), ("studio", r"studio assistito"),
]

# Nome della materia citato ESPLICITAMENTE nel testo (testo normalizzato).
EXPLICIT = {
    "religione": r"\breligione\b|\battivita alternativa\b",
    "italiano": r"\bitaliano\b",
    "latino": r"\blatino\b",
    "inglese": r"\binglese\b|\benglish\b",
    "storia": r"\bstoria\b(?!\s+dell'?\s?arte)",
    "filosofia": r"\bfilosofia\b",
    "matematica": r"\bmatematica\b",
    "fisica": r"(?<!educazione )\bfisica\b",
    "scienze": r"\bscienze\b(?! motorie)|\bchimica\b|\bbiologia\b",
    "arte": r"\bstoria dell'?\s?arte\b|\bdisegno\b|\barte\b",
    "motorie": r"\bscienze motorie\b|\beducazione fisica\b",
    "civica": r"\beducazione civica\b|\bcivica\b",
    "studio": r"\bstudio assistito\b",
}

# Argomenti tipici (usati SOLO per sciogliere docenti con piu' materie; confidenza media).
TOPICS = {
    "italiano": r"ariosto|dante|petrarca|boccaccio|machiavelli|guicciardini|\btasso\b|manzoni|leopardi|"
                r"foscolo|orlando furioso|divina commedia|analisi del testo|parafrasi|\btema\b|saggio breve|"
                r"letteratura|\bpoema\b|canzoniere",
    "latino": r"versione|traduzione|declinazion|coniugazion|ablativo|cicerone|virgilio|orazio|seneca|tacito|"
              r"\blivio\b|ovidio|catullo|lucrezio|sallustio|petronio|fedro|plauto|terenzio",
    "storia": r"riforma|controriforma|lutero|carlo v\b|medioevo|monarchi|scoperte geografiche|colonialismo",
    "filosofia": r"platone|aristotele|socrate|ellenismo|stoic|epicur|scettic|plotino|agostino|cartesio|\bkant\b|hegel",
    "matematica": r"algebra|equazion|disequazion|funzion|goniometri|trigonometri|geometria|logaritm|irrazional|"
                  r"derivat|\blimiti?\b|probabilit|vettor|parabola|ellisse|iperbole|circonferenza",
    "fisica": r"termodinamica|cinematica|dinamica|elettrostatica|elettricita|magnetismo|ottica|\bonde\b|"
              r"calore|temperatura|pressione|fluidi",
}
_EXPL = {k: re.compile(v) for k, v in EXPLICIT.items()}
_TOP = {k: re.compile(v) for k, v in TOPICS.items()}


def subject_key(official_name):
    n = norm(official_name)
    for key, rx in SUBJECT_KEYS:
        if re.search(rx, n):
            return key
    return None


def build_subjects(raw_subjects):
    """raw_subjects: elenco /subjects di ClasseViva -> [{id, desc, key, docenti(norm)}]."""
    out = []
    for s in raw_subjects:
        out.append({"id": s.get("id"), "desc": s.get("description"),
                    "key": subject_key(s.get("description")),
                    "docenti_nomi": [t.get("teacherName") for t in s.get("teachers", [])],
                    "docenti": [norm(t.get("teacherName")) for t in s.get("teachers", [])]})
    return out


def _result(subject, status, criterion, confidence, candidates):
    return {"subject": subject, "subject_status": status,
            "subject_label": subject if subject else LABEL_UNKNOWN,
            "subject_criterion": criterion, "subject_confidence": confidence,
            "subject_candidates": candidates}


def resolve_subject(author, text, subjects, api_subject=None):
    """Determina la materia o restituisce 'Materia da verificare'. Mai per supposizione libera."""
    if api_subject:
        return _result(api_subject.strip(), "determinata", "api:subjectDesc", "alta", [api_subject.strip()])
    t = norm(text)
    by_key = {s["key"]: s for s in subjects if s["key"]}
    expl = {k for k, rx in _EXPL.items() if rx.search(t)}
    # materie condivise da tutti (educazione civica, studio assistito) solo se citate nel testo
    cands = [s for s in subjects if norm(author) in s["docenti"]
             and (s["key"] not in NEVER_BY_TEACHER or s["key"] in expl)]
    cand_names = [s["desc"] for s in cands]
    cand_keys = {s["key"] for s in cands}

    if not cands:  # docente non in elenco (supplente, ecc.)
        if len(expl) == 1 and next(iter(expl)) in by_key:
            k = next(iter(expl))
            return _result(by_key[k]["desc"], "determinata",
                           "testo:nome-materia (docente non presente nell'elenco materie)", "media", [by_key[k]["desc"]])
        return _result(None, "da_verificare", "docente non riconosciuto e testo non decisivo", "nessuna",
                       sorted(by_key[k]["desc"] for k in expl if k in by_key))

    hit = expl & cand_keys
    if len(hit) == 1:
        k = next(iter(hit))
        # segnali contrastanti: nome di una materia + argomenti tipici di un'altra materia del docente
        other = [c for c in cand_keys - {k} if _TOP.get(c) and _TOP[c].search(t)]
        if other:
            return _result(None, "da_verificare",
                           "segnali contrastanti: nome materia %s ma argomenti di %s" % (k, sorted(other)),
                           "nessuna", cand_names)
        return _result(by_key[k]["desc"], "determinata", "testo:nome-materia", "alta", cand_names)
    if len(hit) > 1:
        return _result(None, "da_verificare", "il testo cita piu' materie del docente", "nessuna", cand_names)
    if expl:  # il testo cita una materia che il docente non insegna
        return _result(None, "da_verificare",
                       "conflitto: il testo cita %s ma il docente insegna %s" % (sorted(expl), sorted(cand_keys)),
                       "nessuna", cand_names)
    if len(cands) == 1:
        if ALLOW_UNIQUE_TEACHER:
            return _result(cands[0]["desc"], "determinata",
                           "docente-unico: insegna solo questa materia, il testo non la contraddice", "media", cand_names)
        return _result(None, "da_verificare", "docente con una sola materia (regola prudente attiva)", "nessuna", cand_names)
    topic = {}
    for s in cands:
        rx = _TOP.get(s["key"])
        m = rx.search(t) if rx else None
        if m:
            topic[s["key"]] = m.group(0)
    if len(topic) == 1:
        k, word = next(iter(topic.items()))
        return _result(by_key[k]["desc"], "determinata", "testo:argomento ('%s')" % word, "media", cand_names)
    return _result(None, "da_verificare",
                   "docente con piu' materie e testo non decisivo" if not topic else "argomenti di piu' materie nel testo",
                   "nessuna", cand_names)


def subject_filter(items, wanted):
    """Divide gli item in (corrispondenti, possibili): 'possibili' = materia da verificare ma il
    candidato include la materia richiesta. Niente corrispondenze per supposizione."""
    w = norm(wanted)
    if not w:
        return list(items), []

    ALIAS = {"inglese": "straniera", "english": "straniera", "lingua straniera": "straniera",
             "latino": "latina", "italiano": "italiana", "storia dell'arte": "arte",
             "arte": "arte", "ginnastica": "motorie", "educazione fisica": "motorie"}
    wk = ALIAS.get(w, w)

    def hits(name):
        n = norm(name)
        if not n:
            return False
        if wk != w:
            return wk in n
        if w == "storia":
            return bool(re.search(r"\bstoria\b(?!\s+dell'?\s?arte)", n)) and "arte" not in n.replace("storia", "")
        if w == "fisica":
            return "fisica" in n and "motori" not in n
        return w in n
    ok, maybe = [], []
    for it in items:
        if it.get("subject") and hits(it["subject"]):
            ok.append(it)
        elif not it.get("subject") and any(hits(c) for c in it.get("subject_candidates", [])):
            maybe.append(it)
    return ok, maybe
