"""Fixture SINTETICHE per M6 (classificazione comunicazioni): inventate, con la FORMA dei casi reali (circolari con solo titolo,
annunci brevi, URL soli, riferimenti relativi, annunci di corsi condivisi). Riusa le fixture M5. Nessun dato reale."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import cls_registry as CL
import cvv_registry as CR
import registry as R
import links_fixtures as LF
from links_fixtures import NOW, SUBJECTS, MANUAL, ann, course  # noqa: F401


def notice(pid, title, day, num=None, cat="", att=None, valid_from=None, valid_to=None, in_range=True, read=False):
    return {"pubId": pid, "pubDT": day + "T09:00:00+02:00", "readStatus": read, "evtCode": "CF", "cntId": pid * 10, "cntValidFrom": valid_from,
            "cntValidTo": valid_to, "cntValidInRange": in_range, "cntStatus": "active", "cntTitle": title, "cntNum": num, "cntCategory": cat,
            "cntHasChanged": False, "cntHasAttach": bool(att), "attachments": [{"fileName": a, "attachNum": i + 1} for i, a in enumerate(att or [])]}


NOTICES = [
    notice(1001, "Corso di lingua - certificazione", "2026-10-26", num=119),
    notice(1002, "Progetto Pallavolo", "2026-10-23", num=117),
    notice(1003, "Uscita didattica al museo", "2026-10-18", num=115, att=["4 ALLEGATO"]),
    notice(1004, "Ricevimento genitori classi quarte", "2026-10-16", num=114),
    notice(1005, "Associazione culturale", "2026-10-08", num=112),
    notice(1006, "Avvisi di inizio anno", "2026-09-29", num=104, valid_to="2026-10-28", in_range=False),
    notice(1007, "Verifica di recupero del 9 novembre 2026 per le classi quarte", "2026-10-29", num=121),
    notice(1008, "Verifica annullata: sciopero del 9 novembre 2026", "2026-10-30", num=122),
    notice(1009, "Pomeriggi di studio - Verifica del 11 novembre e verifica del 12 novembre", "2026-10-31", num=123),
    notice(1010, "Circolare vecchia anno scorso: gita", "2025-04-02", num=103),          # anno scolastico 2024-25: STORICO
    notice(1011, "Assemblea degli studenti", "2026-10-20", num=118, cat="Organizzazione"),
    notice(1012, "Documento", "2026-10-30", num=124, att=["Modulo.pdf"]),                      # solo allegato: NON e' un indizio (e' la circolare)
]
ANNOUNCEMENTS = [
    ann("C-EN", "a-ing", "Esercizi per giovedi 29/10 ripasso generale per verifica", "2026-10-26"),
    ann("C-SC", "a-sci1", "Presentazione stechiometria delle reazioni chimiche", "2026-10-22"),
    ann("C-SC", "a-sci2", "Per lunedi studiate la stechiometria", "2026-10-23"),
    ann("C-LE", "a-ita", "Ariosto: leggete il canto primo", "2026-10-27"),
    ann("C-LE", "a-nome", "Ariosto", "2026-10-28"),                                                         # solo un nome
    ann("C-SF", "a-sost", "Non c'e la professoressa Verdi: sostituzione in classe", "2026-10-30"),
    ann("C-EN", "a-url", "https://example.org/video-grammar", "2026-10-29"),
    ann("C-MF", "a-ver", "Verifica del 4 novembre 2026: ricordate la calcolatrice", "2026-10-29"),
    ann("C-MF", "a-ver2", "Verifica del 2 novembre e verifica del 4 novembre", "2026-10-29"),
    ann("C-EN", "a-disc", "Verifica di storia sul Rinascimento", "2026-10-29"),                              # materia diversa dal corso
    ann("C-EN", "a-neg", "Nessuna verifica domani", "2026-10-29"),
    ann("C-SC", "a-two", "Progetto e laboratorio di chimica: ricevimento genitori", "2026-10-29"),             # attivita + organizzativa pari
    ann("C-X", "a-x", "Verifica del 2 novembre 2026", "2026-10-28"),                                          # corso NON confermato: annuncio comunque classificato
    ann("C-SC", "a-old", "Verifica di scienze del 3 ottobre", "2025-09-30"),                                 # anno scolastico 2025-26: operativo (3a)
    ann("C-SC", "a-sci-ver", "Ripasso per la verifica sulla stechiometria delle reazioni", "2026-10-23"),   # testo con 'verifica' + M5 prepara
    ann("C-SC", "a-sci-nome", "Stechiometria delle reazioni chimiche", "2026-10-24"),                      # nessun indizio, ma M5 collega
    dict(ann("C-EN", "a-nat", "Guardate qui", "2026-10-29"), materials=[{"driveFile": {"driveFile": {"id": "x1", "title": "grammar.pdf"}}}]),
    ann("C-SC", "a-hist", "Verifica di scienze", "2024-09-30"),                                              # 2024-25: storico
]


def make_registry(path=":memory:", clock=None, reverse=False, notices=None, announcements=None, **kw):
    """Registro con le fixture M5 + circolari e annunci M6; reverse=True inserisce tutto in ordine inverso."""
    anns = ANNOUNCEMENTS if announcements is None else announcements
    reg = LF.make_registry(path, clock=clock, reverse=reverse, announcements=anns, **kw)
    o = (lambda xs: list(reversed(xs))) if reverse else (lambda xs: list(xs))
    CR.sync_kind(reg, "notice", lambda: [dict(n) for n in o(NOTICES if notices is None else notices)])
    return reg
