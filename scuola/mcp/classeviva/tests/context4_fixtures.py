"""Fixture SINTETICHE per M7 (school_context/4): riusano le fixture M5/M6 e aggiungono SOLO cio' che serve alle sezioni operative
(consegne Classroom per gli stati aperto/scaduto/senza scadenza, verifiche annunciate con data, un corso non confermato, una circolare
senza titolo, un'assenza docente). Inventate, nessun dato reale. NOW = 2026-11-01T12:00:00+02:00 (fixture M5)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import cls_registry as CL
import comms_fixtures as CF
import links_fixtures as LF
from links_fixtures import NOW, SUBJECTS, MANUAL, ann, course, work, topic  # noqa: F401
from comms_fixtures import notice

COURSES = LF.COURSES + [course("C-OLD", "Matematica 3S 2025/26")]                          # proposto per matematica, NON confermato

WORKS = LF.WORKS + [
    work("C-EN", "w-en-open", "Essay on Brexit", "2026-10-29", due="2026-11-07"),          # aperto (futuro)
    work("C-EN", "w-en-late", "Reading comprehension", "2026-10-18", due="2026-10-23"),     # scaduto non consegnato da 9 giorni
    work("C-EN", "w-en-old", "Vocabulary list", "2026-09-29", due="2026-10-08"),            # scaduto da 24 giorni: fuori finestra
    work("C-EN", "w-en-nodue", "Listening practice", "2026-10-30"),                          # senza scadenza
    work("C-EN", "w-en-done", "Grammar sheet", "2026-10-29", due="2026-11-05"),             # consegnato
    work("C-MF", "w-mat-open", "Esercizi sulle derivate", "2026-10-30", "tM", due="2026-11-09"),   # aperto, topic Matematica
    work("C-MF", "w-mf-open", "Scheda di laboratorio", "2026-10-30", due="2026-11-08"),     # aperto, corso condiviso SENZA topic
    work("C-OLD", "w-old", "Esercizi vecchio corso", "2026-10-29", due="2026-11-17"),       # corso non confermato: non considerato
]
_S = {"w-en-open": "CREATED", "w-en-late": "CREATED", "w-en-old": "CREATED", "w-en-nodue": "NEW", "w-en-done": "TURNED_IN",
      "w-mat-open": "CREATED", "w-mf-open": "CREATED", "w-hw1": "TURNED_IN", "w-hw2": "CREATED", "w-old": "CREATED"}
SUBMISSIONS = [{"courseId": w["courseId"], "courseWorkId": w["id"], "id": "s-" + w["id"], "userId": "5001", "state": _S[w["id"]],
                "late": False, "updateTime": "2026-10-30T10:00:00Z"} for w in WORKS if w["id"] in _S]

ANNOUNCEMENTS = CF.ANNOUNCEMENTS + [
    ann("C-EN", "a-en-ver", "Verifica del 6 novembre 2026 sui phrasal verbs", "2026-10-30"),              # M6 verifica V1, futura
    ann("C-EN", "a-en-prep", "Ripassate per il test del 30 ottobre 2026", "2026-10-26"),                  # M5 prepara probabile -> 1000107
    ann("C-EN", "a-en-vold", "Verifica del 18 ottobre 2026 sul passato", "2026-10-13"),                 # verifica oltre 7 giorni
    ann("C-EN", "a-en-rel", "Verifica per lunedi sui verbi", "2026-10-30"),                              # verifica senza data (relativo)
    ann("C-SC", "a-sci-ass", "Non c'e la professoressa Galli, ci saro io. Portate il quaderno.", "2026-10-31"),
    ann("C-OLD", "a-old-mat", "Esercizi di matematica per la prossima lezione", "2026-10-29"),            # corso non confermato
]

NOTICES = CF.NOTICES + [
    notice(1013, "", "2026-10-30", num=25),                                                              # circolare senza titolo
    notice(1014, "Calendario annuale delle attivita", "2026-08-20", num=2, valid_from="2026-08-20", valid_to="2027-01-28"),
    notice(1015, "Orario provvisorio", "2026-08-25", num=3),                                             # oltre 30 giorni, senza validita'
]


def make_registry(path=":memory:", clock=None, reverse=False, notices=None, announcements=None, works=None, courses=None,
                  submissions=None, **kw):
    """Registro M6 (circolari + annunci + fixture M5) + corsi/compiti/consegne M7; reverse=True inserisce tutto in ordine inverso."""
    reg = CF.make_registry(path, clock=clock, reverse=reverse, notices=NOTICES if notices is None else notices,
                           announcements=ANNOUNCEMENTS if announcements is None else announcements,
                           works=WORKS if works is None else works, courses=COURSES if courses is None else courses, **kw)
    o = (lambda xs: list(reversed(xs))) if reverse else (lambda xs: list(xs))
    CL.sync_kind(reg, "submission", lambda: [dict(s) for s in o(SUBMISSIONS if submissions is None else submissions)])
    return reg
