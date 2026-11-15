"""Fixture SINTETICHE per M5 (collegamenti ClasseViva <-> Classroom): inventate, con la FORMA dei casi reali analizzati
(corsi condivisi, verifiche con data esplicita, trappola dell'anno precedente, annunci ambigui). Nessun dato reale."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import cls_registry as CL
import cvv_registry as CR
import cvv_subject as subj
import registry as R

NOW = "2026-11-01T12:00:00+02:00"
SUBJECTS = subj.build_subjects([
    {"id": 1, "description": "MATEMATICA", "teachers": [{"teacherName": "ROSSI MARIO"}]},
    {"id": 2, "description": "FISICA", "teachers": [{"teacherName": "ROSSI MARIO"}]},
    {"id": 3, "description": "STORIA", "teachers": [{"teacherName": "VERDI ANNA"}]},
    {"id": 4, "description": "FILOSOFIA", "teachers": [{"teacherName": "VERDI ANNA"}]},
    {"id": 5, "description": "LINGUA E LETTERATURA ITALIANA", "teachers": [{"teacherName": "BIANCHI LUCA"}]},
    {"id": 6, "description": "LINGUA E CULTURA LATINA", "teachers": [{"teacherName": "BIANCHI LUCA"}]},
    {"id": 7, "description": "LINGUA E CULTURA STRANIERA 1", "teachers": [{"teacherName": "NERI SARA"}]},
    {"id": 8, "description": "SCIENZE NATURALI", "teachers": [{"teacherName": "GALLI PAOLO"}]},
])
# corsi: C-MF (mate+fisica) e C-SF (storia+filosofia) e C-LE (italiano+latino) CONDIVISI; C-EN e C-SC monomateria; C-X non confermato
MANUAL = {"subjects": {
    "matematica": {"classroom_course_ids": ["C-MF"]}, "fisica": {"classroom_course_ids": ["C-MF"]},
    "storia": {"classroom_course_ids": ["C-SF"]}, "filosofia": {"classroom_course_ids": ["C-SF"]},
    "italiano": {"classroom_course_ids": ["C-LE"]}, "latino": {"classroom_course_ids": ["C-LE"]},
    "inglese": {"classroom_course_ids": ["C-EN"]}, "scienze": {"classroom_course_ids": ["C-SC"]}}}


def ev(eid, text, author, day, code="AGNT"):
    return {"evtId": eid, "evtCode": code, "notes": text, "evtDatetimeBegin": day + "T10:00:00",
            "evtDatetimeEnd": day + "T11:00:00", "isFullDay": False, "authorName": author, "classDesc": "4S"}


def lesson(eid, subj_desc, who, day, arg, hpos=2, typ="Lezione"):
    return {"evtId": eid, "evtDate": day, "evtHPos": hpos, "authorName": who, "subjectDesc": subj_desc,
            "lessonType": typ, "lessonArg": arg, "evtDuration": 1}


def course(cid, name):
    return {"id": cid, "name": name, "courseState": "ACTIVE", "creationTime": "2026-09-29T07:00:00Z", "updateTime": "2026-09-29T07:00:00Z"}


def work(cid, wid, title, created, topic=None, due=None, desc=None):
    w = {"courseId": cid, "id": wid, "title": title, "state": "PUBLISHED", "workType": "ASSIGNMENT",
         "creationTime": created + "T10:00:00Z", "updateTime": created + "T10:00:00Z"}
    if topic:
        w["topicId"] = topic
    if desc:
        w["description"] = desc
    if due:
        y, m, d = due.split("-")
        w["dueDate"] = {"year": int(y), "month": int(m), "day": int(d)}
        w["dueTime"] = {"hours": 12, "minutes": 0}
    return w


def material(cid, mid, title, created, topic=None):
    m = {"courseId": cid, "id": mid, "title": title, "state": "PUBLISHED", "creationTime": created + "T10:00:00Z",
         "updateTime": created + "T10:00:00Z"}
    if topic:
        m["topicId"] = topic
    return m


def ann(cid, aid, text, created):
    return {"courseId": cid, "id": aid, "text": text, "state": "PUBLISHED", "creationTime": created + "T10:00:00Z",
            "updateTime": created + "T10:00:00Z"}


def topic(cid, tid, name):
    return {"courseId": cid, "topicId": tid, "name": name, "updateTime": "2026-09-29T09:00:00Z"}


EVENTS = [
    ev(1000101, "Verifica Matematica", "ROSSI MARIO", "2026-11-02"),
    ev(1000102, "Verifica di fisica", "ROSSI MARIO", "2026-11-04"),
    ev(1000106, "Verifica di storia: Luigi XIV, Colbert, mercantilismo e assolutismo", "VERDI ANNA", "2026-10-29"),
    ev(1000105, "Verifica di filosofia: Kant e illuminismo", "VERDI ANNA", "2026-10-20"),
    ev(1000108, "Interrogazioni programmate", "NERI SARA", "2026-10-29"),
    ev(1000107, "Reading and vocabulary test", "NERI SARA", "2026-10-30"),
    ev(1000103, "VERIFICA DI SCIENZE: stechiometria delle reazioni chimiche", "GALLI PAOLO", "2026-10-27"),
    ev(1000104, "Verifica su Ariosto", "BIANCHI LUCA", "2026-11-09"),
    ev(1000109, "Tema in classe", "BIANCHI LUCA", "2026-11-03"),
    ev(3000001, "Esercizi di matematica: disequazioni irrazionali pag. 10", "ROSSI MARIO", "2026-11-06", code="AGHW"),
    ev(3000002, "Esercizi di matematica: equazioni logaritmiche", "ROSSI MARIO", "2026-11-11", code="AGHW"),
]
LESSONS = [
    lesson(1, "MATEMATICA", "ROSSI MARIO", "2026-10-19", "Equazioni e disequazioni irrazionali"),
    lesson(2, "MATEMATICA", "ROSSI MARIO", "2026-10-20", "Funzione irrazionale"),
    lesson(3, "STORIA", "VERDI ANNA", "2026-10-29", "Verifica: Luigi XIV, mercantilismo", typ="Compito in classe"),
    lesson(4, "MATEMATICA", "ROSSI MARIO", "2026-10-22", "Integrali indefiniti"),
    lesson(5, "MATEMATICA", "ROSSI MARIO", "2026-10-24", "Integrali definiti"),
]
COURSES = [course("C-MF", "4Z Mate & Fisica (prova) 2026/27"), course("C-SF", "Storia e Filosofia (prova) - 4Z"),
           course("C-LE", "Lettere - Bianchi"), course("C-EN", "4X - Inglese"), course("C-SC", "SCIENZE 4 (prova)"),
           course("C-X", "Arte 4Z")]
TOPICS = [topic("C-MF", "tM", "Matematica"), topic("C-MF", "tF", "Fisica"), topic("C-MF", "tL", "Laboratorio"),
          topic("C-SF", "tS", "Storia"), topic("C-SF", "tFi", "Filosofia"),
          topic("C-LE", "tScr", "Scrittura"), topic("C-LE", "tLat", "Latino")]
WORKS = [
    work("C-MF", "w-sim", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-10-27", "tM"),
    work("C-MF", "w-fis", "Esercizi per prepararsi alla verifica del 4 novembre 2026", "2026-10-28", "tF"),
    work("C-MF", "w-fis-old", "Esercizi per prepararsi alla verifica del 3 ottobre", "2025-09-30", "tF"),          # TRAPPOLA: anno precedente
    work("C-MF", "w-nosubj", "Esercizi per la verifica del 4 novembre", "2026-10-29"),                                 # corso condiviso senza topic
    work("C-MF", "w-cross", "Quiz sulla verifica del 2 novembre 2026", "2026-10-29", "tF"),                            # topic fisica, data della verifica di mate
    work("C-MF", "w-pasqua", "COMPITI VACANZE - Per lunedi 13 Aprile - Mate e Fisica", "2026-03-30"),                  # anno precedente
    work("C-MF", "w-hw1", "Disequazioni irrazionali", "2026-10-30", "tM", due="2026-11-06"),                         # R6
    work("C-MF", "w-hw2", "Equazioni logaritmiche", "2026-11-02", "tM", due="2026-11-13"),                            # RC3 (scadenza diversa)
]
MATERIALS = [
    material("C-SF", "m-sto1", "Il mercantilismo francese", "2026-10-13", "tS"),
    material("C-SF", "m-sto2", "Assolutismo o monarchia costituzionale", "2026-10-21", "tS"),
    material("C-SF", "m-sto3", "Luigi XIV", "2026-10-13", "tS"),                                                        # 1 parola corta: debole -> nessun link
    material("C-SF", "m-fil", "Illuminismo - Testi", "2026-10-13", "tFi"),
    material("C-SF", "m-fil-old", "Kant - La critica", "2026-04-10", "tFi"),                                  # anno precedente
    material("C-MF", "m-mat1", "Soluzione disequazione irrazionale", "2026-10-20", "tM"),
    material("C-MF", "m-mat2", "capitolo 4 paragrafo 3 slide di sintesi", "2026-10-20", "tM"),
    material("C-MF", "m-int", "Integrali", "2026-10-23", "tM"),                                                        # pareggio tra due lezioni
]
ANNOUNCEMENTS = [
    ann("C-EN", "a-ing", "Esercizi per giovedi 29/10 ripasso generale per verifica", "2026-10-26"),
    ann("C-SC", "a-sci1", "Presentazione stechiometria delle reazioni chimiche", "2026-10-22"),
    ann("C-SC", "a-sci2", "Per lunedi studiate la stechiometria", "2026-10-23"),
    ann("C-LE", "a-ita", "Ariosto: leggete il canto primo", "2026-10-27"),
    ann("C-X", "a-x", "Verifica del 2 novembre 2026", "2026-10-28"),                                                   # corso NON confermato
]


def make_registry(path=":memory:", clock=None, reverse=False, events=None, lessons=None, courses=None, topics=None, works=None,
                  materials=None, announcements=None):
    """Registro (in memoria o su disco) con le fixture; reverse=True inserisce tutto in ordine inverso (test di determinismo)."""
    o = (lambda xs: list(reversed(xs))) if reverse else (lambda xs: list(xs))
    pick = lambda given, dflt: dflt if given is None else given          # noqa: E731
    reg = R.Registry(path, clock=clock)
    CR.sync_kind(reg, "agenda_event", lambda: [dict(e) for e in o(pick(events, EVENTS))])
    CR.sync_kind(reg, "lesson", lambda: [dict(x) for x in o(pick(lessons, LESSONS))])
    CL.sync_kind(reg, "course", lambda: [dict(c) for c in o(pick(courses, COURSES))])
    CL.sync_kind(reg, "topic", lambda: [dict(t) for t in o(pick(topics, TOPICS))])
    CL.sync_kind(reg, "course_work", lambda: [dict(w) for w in o(pick(works, WORKS))])
    CL.sync_kind(reg, "material", lambda: [dict(m) for m in o(pick(materials, MATERIALS))])
    CL.sync_kind(reg, "announcement", lambda: [dict(a) for a in o(pick(announcements, ANNOUNCEMENTS))])
    return reg
