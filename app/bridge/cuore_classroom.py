"""Indice Classroom per la ricerca di CUORE (`cuore.school.classroom_index/1`): corsi, argomenti, compiti, annunci e
riferimenti ai materiali (file Drive, link, video, allegati delle circolari) come li conosce il motore School.

Perche' esiste: il feed `cuore.school.feed/1` espone della pagina Materiali solo i riepiloghi, quindi la ricerca non
poteva trovare un singolo file (diagnosi del 25/10: 0 titoli su 923 del catalogo presenti nel feed). Qui si riusano SENZA
nuova logica di dominio il catalogo M15 (`school_materials_catalog.collect`: titolo, link originale, corso, argomento,
attribuzione) e le normalizzazioni Classroom (`cls_registry.normalized`).

Sola lettura come il feed: copia in memoria del Registry, rete bloccata, file confrontati prima e dopo (un file cambiato
durante la lettura = nessuna voce). Il CONTENUTO dei file non e' letto qui: l'autorizzazione Classroom copre solo i
metadati (nessuno scope Drive); un contenuto e' «letto» solo se il testo e' nell'archivio M19 (`materials.sqlite3`,
acquisito con il connettore Google Drive dell'agente), aperto immutable e solo per sapere QUALI riferimenti ha.
Link originali: solo https. Mai uid del Registry, hash o versioni nelle voci.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cuore_school as CSC  # noqa: E402   (sys.path del motore, rete bloccata, materie)
import cls_registry as CL  # noqa: E402
import registry as R  # noqa: E402
import school_common as CM  # noqa: E402
import school_context as SC  # noqa: E402
import school_materials_catalog as MC  # noqa: E402

SCHEMA = "cuore.school.classroom_index/1"
TEXT_CAP = 200
RESOURCE_LABEL = {"drive_file": "file Drive", "link": "link", "youtube": "video YouTube", "form": "modulo",
                  "allegato_classeviva": "allegato di circolare", "unknown": "materiale"}
CONTENT_NOTE = ("contenuto dei file non letto dalla sincronizzazione: l'autorizzazione Classroom copre solo i metadati "
                "(nessuno scope Drive). Un testo e' disponibile solo se acquisito con il connettore Google Drive nell'archivio "
                "dei materiali (M19)")


def store_path():
    import school_material_store as MS
    return os.path.expanduser(MS.DEFAULT_STORE)


def acquired_refs(path=None):
    """Riferimenti del catalogo con un testo nell'archivio M19 (solo gli id, mai il testo). None = archivio assente."""
    p = path or store_path()
    if not os.path.isfile(p):
        return None
    con = sqlite3.connect("file:%s?mode=ro&immutable=1" % p, uri=True)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT ref_id FROM versions")}
    finally:
        con.close()


def inputs_state(registry_path=None, store=None):
    p = store or store_path()
    st = os.stat(p) if os.path.isfile(p) else None
    return (CSC.inputs_state(registry_path), (st.st_size, st.st_mtime_ns) if st else None)


def _https(u):
    return u if isinstance(u, str) and u.startswith("https://") and len(u) <= 2048 else None


def _item(tipo, titolo, data, link, rotta, **det):
    return {"tipo": tipo, "titolo": titolo, "data": data, "link": _https(link), "rotta": rotta,
            **{k: v for k, v in det.items() if v not in (None, "", [])}}


def _attribution(a):
    if a["stato"] == "determinata":
        return a["materia"]
    if a["stato"] == "possibile":
        return "possibile: " + ", ".join(a["candidati"]) if a["candidati"] else None
    return None


def entries(reg, subjects, manual, year, acquired):
    out = []
    courses, topics = {}, {}
    for f in reg.list_records(source=CL.SOURCE, kind="course", status=R.PRESENT):
        n = CL.normalized("course", reg.get_record(CL.SOURCE, "course", f["source_uid"])["raw"])
        courses[str(n.get("id"))] = n.get("name")
        if n.get("name"):
            out.append(_item("corso Classroom", n["name"], n.get("date"), n.get("alternate_link"), "school/materiali",
                             sezione=n.get("section")))
    for f in reg.list_records(source=CL.SOURCE, kind="topic", status=R.PRESENT):
        n = CL.normalized("topic", reg.get_record(CL.SOURCE, "topic", f["source_uid"])["raw"])
        topics[(str(n.get("course_id")), n.get("topic_id"))] = n.get("name")
        if n.get("name"):
            out.append(_item("argomento Classroom", n["name"], n.get("date"), None, "school/materiali",
                             corso=courses.get(str(n.get("course_id")))))
    for kind, label, rotta in (("course_work", "compito Classroom", "school/compiti"),
                               ("announcement", "annuncio Classroom", "school/calendario")):
        for f in reg.list_records(source=CL.SOURCE, kind=kind, status=R.PRESENT):
            n = CL.normalized(kind, reg.get_record(CL.SOURCE, kind, f["source_uid"])["raw"])
            if n.get("state") not in (None, "PUBLISHED"):
                continue
            text = CM.cap_text(n.get("text") if kind == "announcement" else n.get("description") or "", TEXT_CAP)[0] or None
            title = n.get("title") if kind == "course_work" else CM.cap_text(n.get("text") or "", 90)[0]
            if not title:
                continue
            day = n.get("date")
            out.append(_item(label, title, day, n.get("alternate_link"), rotta, corso=courses.get(str(n.get("course_id"))),
                             argomento=topics.get((str(n.get("course_id")), n.get("topic_id"))),
                             testo=text if kind == "course_work" else None, allegati=len(n.get("materials") or []) or None,
                             perimetro="operativo" if MC._sy(day) == year else "storico"))
    for r in MC.collect(reg, subjects, manual):
        if r["stato_materiale"] != "identificato_non_acquisito":
            continue
        p = r["padre"]
        title = r["titolo"] or p["titolo"] or p["testo"]
        if not title:
            continue
        out.append(_item(RESOURCE_LABEL.get(r["tipo_risorsa"], "materiale"), title, p["data"], r["url"], "school/materiali",
                         corso=(p["corso"] or {}).get("nome"), argomento=p["topic"], materia=_attribution(r["attribuzione"]),
                         origine={"material": "materiale", "course_work": "compito", "announcement": "annuncio",
                                  "notice": "circolare ClasseViva"}.get(p["tipo"]),
                         elemento=p["titolo"] if p["titolo"] != title else None,
                         contenuto="letto (archivio materiali)" if acquired and r["id"] in acquired else "non letto",
                         perimetro="operativo" if p["anno"] == year else "storico"))
    return out


def build_index(registry_path=None, now=None, store=None, subjects=None, manual=None):
    """Indice in sola lettura. Mai eccezioni: un errore diventa `errori` con il solo codice e il tipo. `subjects` e
    `manual` (test): al posto dell'istantanea delle materie e del file delle conferme reali."""
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(ZoneInfo(CSC.ZONE)).replace(microsecond=0)
    path = registry_path or R.default_path()
    doc = {"schema": SCHEMA, "generato": now.isoformat(), "voci": [], "errori": [], "sola_lettura_verificata": False,
           "contenuti": {"nota": CONTENT_NOTE, "letti": None}}
    if not os.path.isfile(path):
        doc["errori"].append({"codice": "registro_assente"})
        return doc
    try:
        subjects = subjects if subjects is not None else CSC.load_subjects()
        manual = manual if manual is not None else SC.load_manual(SC.manual_default_path())["dati"]
        acquired = acquired_refs(store)
    except Exception as e:      # noqa: BLE001
        doc["errori"].append({"codice": "fonti_non_disponibili", "tipo": type(e).__name__})
        return doc
    with CSC._LOCK, CSC.no_network():
        before = inputs_state(path, store)
        try:
            reg = R.Registry.from_snapshot(path)
            try:
                voci = entries(reg, subjects, manual, MC._sy(now.date().isoformat()), acquired)
            finally:
                reg.close()
        except Exception as e:      # noqa: BLE001 - errore del motore: dichiarato, nessuna voce
            doc["errori"].append({"codice": "errore_motore", "tipo": type(e).__name__})
            return doc
        if inputs_state(path, store) != before:
            doc["errori"].append({"codice": "sola_lettura_violata"})
            return doc
    doc["voci"], doc["sola_lettura_verificata"] = voci, True
    doc["contenuti"]["letti"] = len(acquired) if acquired is not None else 0
    counts = {}
    for v in voci:
        counts[v["tipo"]] = counts.get(v["tipo"], 0) + 1
    doc["conteggi"] = dict(sorted(counts.items()))
    return doc
