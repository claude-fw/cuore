"""Materiali acquisiti e studio sui materiali (M19 + M20) - schema cloud.school.material_store/1 e cloud.school.study_material/1.

Perche' esiste: il catalogo M15 identifica i materiali citati dalle fonti, ma l'assistente di studio non ne ha mai visto il
contenuto. Il connettore Google Drive gia' autorizzato (account della scuola) legge i file Drive allegati a Classroom. Questo modulo:

- REGISTRA il testo letto dall'agente con il connettore, solo per un riferimento che esiste nel catalogo M15 (perimetro operativo)
  e solo se l'id Drive coincide con quello del catalogo: nessuna fonte non autorizzata, nessuna attribuzione inventata;
- conserva le versioni in un archivio LOCALE SEPARATO (materials.sqlite3, accanto al Registry ma mai dentro): append-only
  (UPDATE/DELETE bloccati da trigger), chiave stabile = id del riferimento M15, versione nuova solo se il testo o la data di
  modifica Drive cambiano. L'originale resta su Drive: qui c'e' la sua rappresentazione testuale con hash e metadati;
- compone una sessione di studio M13 su quel testo con la provenienza corretta ("materiale scolastico acquisito", non "fornito
  dall'utente"), senza modificare M13: ne riusa azioni, divieti e frasi, e aggiunge le frasi del materiale.

Il Registry resta la fonte di verita' dei dati scolastici; questo archivio non duplica nulla del Registry (solo testo e metadati
del file). Acquisito non significa studiato. Il collegamento a una verifica e' solo quello di M5 con il suo stato.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3

import cls_derive as D
import school_context as SC
import school_materials_catalog as MC
import school_study_session as SS

SCHEMA = "cloud.school.material_store/1"
STUDY_SCHEMA = "cloud.school.study_material/1"
STORE_REV = 1
DEFAULT_STORE = os.path.join("~", "Library", "Application Support", "Cloud", "school", "materials.sqlite3")
METHOD = "connettore_google_drive"
TEXT_MAX = 2_000_000
# tipi che il connettore Drive sa rendere in testo (read_file_content)
MIME_OK = ("application/pdf", "application/vnd.google-apps.document", "application/vnd.google-apps.presentation",
           "application/vnd.google-apps.spreadsheet", "application/msword",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "application/vnd.openxmlformats-officedocument.presentationml.presentation",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.oasis.opendocument.text",
           "application/vnd.oasis.opendocument.presentation", "application/vnd.oasis.opendocument.spreadsheet",
           "application/x-vnd.oasis.opendocument.text", "image/png", "image/jpeg", "image/jpg")
META_KEYS = ("id", "title", "mimeType", "modifiedTime", "owner", "fileSize")
ACQ_STATES = ("acquisito", "da_acquisire", "non_acquisibile")

DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_id TEXT NOT NULL, drive_file_id TEXT NOT NULL, version INTEGER NOT NULL,
  sha256 TEXT NOT NULL, chars INTEGER NOT NULL, text TEXT NOT NULL,
  title TEXT, mime TEXT NOT NULL, owner TEXT, modified_time TEXT, size_bytes INTEGER,
  catalog_title TEXT, parent_uid TEXT NOT NULL, catalog_version TEXT NOT NULL,
  acquired_at TEXT NOT NULL, method TEXT NOT NULL,
  UNIQUE (ref_id, version), UNIQUE (ref_id, sha256, modified_time));
CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def store_version():
    return "r%d-%s" % (STORE_REV, hashlib.sha256(json.dumps([DDL, MIME_OK, META_KEYS, ACQ_STATES, METHOD]).encode()).hexdigest()[:6])


def default_store():
    return os.path.expanduser(DEFAULT_STORE)


def _sha(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _open(path, create):
    if path != ":memory:" and not os.path.isfile(path):
        if not create:
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if path != ":memory:":
        os.chmod(path, 0o600)                       # come il Registry: contenuti scolastici leggibili solo dall'utente
    if create:
        db.executescript(DDL)
        db.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('schema', ?)", (SCHEMA,))
        db.commit()
    return db


def _versions(db, ref_id):
    if db is None:
        return []
    return [dict(r) for r in db.execute("SELECT * FROM versions WHERE ref_id = ? ORDER BY version", (ref_id,))]


# ------------------------------------------------------------------------------------------------ catalogo M15
def _catalog(reg, now_dt, manual, subjects, materia=None):
    """Tutti i riferimenti M15 del perimetro operativo (dettaglio), con la versione del catalogo."""
    kw = dict(manual=manual, subjects=subjects, perimetro="operativo", limite=MC.LIMIT_MAX, dettaglio=True)
    if materia:
        kw["materia"] = materia
    first = MC.build(reg, now_dt, **kw)
    refs = list(first["riferimenti"])
    for p in range(2, first["sintesi"]["pagine"] + 1):
        refs += MC.build(reg, now_dt, pagina=p, **kw)["riferimenti"]
    return refs, first["provenance"]["catalog_version"]


def _associations(r):
    out = []
    for a in r["associazioni_m5"]:
        con = a["con"] if isinstance(a["con"], list) else [a["con"]]
        out.append({"relazione": a["relazione"], "stato": a["stato"], "regola": a.get("regola"), "con": [c["uid"] for c in con]})
    return out


def _parent(r):
    p = r["padre"]
    return {"uid": p["uid"], "tipo": p["tipo"], "titolo": p["titolo"] or p.get("testo"), "data": p["data"],
            "corso": (p.get("corso") or {}).get("nome")}


def _public(v, with_text):
    out = {"versione": v["version"], "sha256": v["sha256"], "lunghezza": v["chars"], "titolo_drive": v["title"], "mime": v["mime"],
           "proprietario": v["owner"], "modificato_su_drive": v["modified_time"], "dimensione_byte": v["size_bytes"],
           "acquisito_il": v["acquired_at"], "metodo": v["method"], "versione_catalogo": v["catalog_version"]}
    if with_text:
        out["testo"] = v["text"]
    return out


# ------------------------------------------------------------------------------------------------ acquisizione
def _meta(metadati):
    if not isinstance(metadati, dict):
        raise ValueError("metadati_drive obbligatori: l'oggetto restituito da get_file_metadata del connettore Drive")
    m = {k: metadati.get(k) for k in META_KEYS}
    if not (isinstance(m["id"], str) and m["id"].strip()):
        raise ValueError("metadati_drive.id mancante")
    if not (isinstance(m["mimeType"], str) and m["mimeType"]):
        raise ValueError("metadati_drive.mimeType mancante")
    size = m["fileSize"]
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise ValueError("metadati_drive.fileSize non numerico: %r" % (size,))
    m["fileSize"] = size
    return m


def acquire(store_path, reg, now_dt, riferimento, testo, metadati_drive, manual=None, *, subjects):
    """Registra il testo di un materiale Drive del catalogo. Esito: acquisito | invariato | nuova_versione | rifiutato.
    Un rifiuto non scrive nulla. `now_dt` letto UNA volta dal chiamante."""
    if not (isinstance(riferimento, str) and riferimento.strip()):
        raise ValueError("riferimento obbligatorio: id esatto di un riferimento del catalogo M15")
    if not isinstance(testo, str):
        raise ValueError("testo obbligatorio: il contenuto restituito da read_file_content del connettore Drive")
    meta = _meta(metadati_drive)
    ref_id = riferimento.strip()

    def refuse(code, msg):
        return {"schema": SCHEMA, "esito": "rifiutato", "motivo": code, "messaggio": msg, "riferimento": ref_id, "scritture": False}
    if not testo.strip():
        return refuse("testo_vuoto", "il connettore non ha restituito testo: nulla da registrare (il documento resta da acquisire)")
    if len(testo) > TEXT_MAX:
        return refuse("testo_troppo_lungo", "testo oltre %d caratteri: non registrato" % TEXT_MAX)
    refs, cat_ver = _catalog(reg, now_dt, manual, subjects)
    ref = next((r for r in refs if r["id"] == ref_id), None)
    if ref is None:
        return refuse("non_nel_catalogo", "riferimento assente dal catalogo M15 del perimetro operativo: si acquisisce solo da fonti "
                                         "scolastiche autorizzate gia' identificate")
    if ref["tipo_risorsa"] != "drive_file" or not ref.get("drive_file_id"):
        return refuse("non_acquisibile", "il riferimento non e' un file Drive (%s): il connettore non lo legge" % ref["tipo_risorsa"])
    if ref["stato_materiale"] != "identificato_non_acquisito":
        return refuse("non_disponibile", "il catalogo segnala il riferimento come %s" % ref["stato_materiale"])
    if meta["id"] != ref["drive_file_id"]:
        return refuse("id_drive_diverso", "metadati di un altro file (%s) rispetto al catalogo (%s)" % (meta["id"], ref["drive_file_id"]))
    if meta["mimeType"] not in MIME_OK:
        return refuse("formato_non_leggibile", "formato %s non reso in testo dal connettore" % meta["mimeType"])

    sha = _sha(testo)
    acquired_at = D.parse_now(now_dt).isoformat()
    db = _open(store_path, create=True)
    try:
        prev = _versions(db, ref_id)
        same = next((v for v in prev if v["sha256"] == sha and v["modified_time"] == meta["modifiedTime"]), None)
        if same is not None:
            esito, row = "invariato", same
        else:
            ver = (prev[-1]["version"] + 1) if prev else 1
            db.execute("INSERT INTO versions (ref_id, drive_file_id, version, sha256, chars, text, title, mime, owner, modified_time, "
                       "size_bytes, catalog_title, parent_uid, catalog_version, acquired_at, method) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ref_id, meta["id"], ver, sha, len(testo), testo, meta["title"], meta["mimeType"], meta["owner"],
                        meta["modifiedTime"], meta["fileSize"], ref["titolo"], ref["padre"]["uid"], cat_ver,
                        acquired_at, METHOD))
            db.commit()
            esito = "nuova_versione" if prev else "acquisito"
            row = _versions(db, ref_id)[-1]
    finally:
        db.close()
    avvisi = []
    if meta["title"] and ref["titolo"] and meta["title"] != ref["titolo"]:
        avvisi.append({"codice": "titolo_diverso", "messaggio": "titolo su Drive (%s) diverso dal titolo citato (%s): stesso file per id"
                                                              % (meta["title"], ref["titolo"])})
    return {"schema": SCHEMA, "esito": esito, "riferimento": ref_id, "drive_file_id": meta["id"], "scritture": esito != "invariato",
            "versione": _public(row, False), "attribuzione": ref["attribuzione"], "padre": _parent(ref),
            "associazioni_m5": _associations(ref), "avvisi": avvisi,
            "provenance": {"schema": SCHEMA, "store_version": store_version(), "catalog_version": cat_ver, "archivio": store_path}}


# ------------------------------------------------------------------------------------------------ stato
def _matches_obj(r, obiettivo):
    return any(obiettivo in a["con"] for a in _associations(r))


def status(store_path, reg, now_dt, materia=None, obiettivo=None, riferimento=None, manual=None, *, subjects):
    """Per i riferimenti M15 (materia e/o obiettivo, oppure uno solo): acquisito | da_acquisire | non_acquisibile. Sola lettura."""
    key = SC.resolve_subject(materia.strip(), subjects) if materia else None
    refs, cat_ver = _catalog(reg, now_dt, manual, subjects, materia=key)
    if riferimento:
        refs = [r for r in refs if r["id"] == riferimento]
    if obiettivo:
        refs = [r for r in refs if _matches_obj(r, obiettivo)]
    db = _open(store_path, create=False)
    try:
        items = []
        for r in sorted(refs, key=lambda r: (r["padre"]["data"] or "", r["id"]), reverse=True):
            vs = _versions(db, r["id"])
            if vs:
                st, extra = "acquisito", {"ultima_versione": _public(vs[-1], False), "versioni": len(vs)}
            elif r["tipo_risorsa"] == "drive_file" and r.get("drive_file_id") and r["stato_materiale"] == "identificato_non_acquisito":
                st, extra = "da_acquisire", {"come": "get_file_metadata + read_file_content del connettore Google Drive con fileId=%s, "
                                                    "poi school_material_acquire_v1" % r["drive_file_id"]}
            else:
                st, extra = "non_acquisibile", {"motivo": "%s: non leggibile dal connettore Drive" % r["tipo_risorsa"]
                                                if r["tipo_risorsa"] != "drive_file" else r["stato_materiale"]}
            items.append(dict({"riferimento": r["id"], "titolo": r["titolo"], "tipo_risorsa": r["tipo_risorsa"],
                               "drive_file_id": r.get("drive_file_id"), "acquisizione": st, "attribuzione": r["attribuzione"],
                               "padre": _parent(r), "associazioni_m5": _associations(r)}, **extra))
    finally:
        if db is not None:
            db.close()
    counts = {s: sum(1 for i in items if i["acquisizione"] == s) for s in ACQ_STATES}
    return {"schema": SCHEMA, "ambito": {"materia": key, "obiettivo": obiettivo, "riferimento": riferimento},
            "sintesi": dict(counts, totale=len(items)), "materiali": items,
            "regole_agente": ["Acquisito significa che il testo del file e' disponibile in locale: NON che sia stato studiato.",
                              "Si acquisisce solo un riferimento 'da_acquisire' (file Drive del catalogo), mai altri file o URL.",
                              "Il collegamento a una verifica e' solo quello di associazioni_m5, con il suo stato: non e' il programma."],
            "provenance": {"schema": SCHEMA, "store_version": store_version(), "catalog_version": cat_ver, "archivio": store_path,
                           "archivio_presente": db is not None}}


# ------------------------------------------------------------------------------------------------ studio sul materiale (M20)
def study(store_path, reg, now_dt, materia, modalita, riferimento, obiettivo=None, risposta_utente=None, manual=None, *, subjects,
          source_path=None):
    """Sessione M13 sul testo acquisito di un materiale, con provenienza 'materiale_scolastico_acquisito'. Sola lettura."""
    if not (isinstance(riferimento, str) and riferimento.strip()):
        raise ValueError("riferimento obbligatorio: id esatto di un materiale acquisito")
    key = SC.resolve_subject(materia.strip(), subjects) if isinstance(materia, str) and materia.strip() else None
    if key is None:
        raise ValueError("materia obbligatoria: %r" % (materia,))
    ref_id = riferimento.strip()
    refs, cat_ver = _catalog(reg, now_dt, manual, subjects)
    ref = next((r for r in refs if r["id"] == ref_id), None)
    db = _open(store_path, create=False)
    try:
        vs = _versions(db, ref_id)
    finally:
        if db is not None:
            db.close()
    base = {"schema": STUDY_SCHEMA, "materia": key, "modalita": modalita, "riferimento": ref_id,
            "provenance": {"schema": STUDY_SCHEMA, "store_version": store_version(), "catalog_version": cat_ver, "archivio": store_path}}

    def stop(code, msg, **extra):
        return dict(base, esito={"stato": code, "messaggio": msg}, **extra)
    if ref is None:
        return stop("riferimento_non_nel_catalogo", "riferimento assente dal catalogo M15 del perimetro operativo")
    if not vs:
        return stop("materiale_non_acquisito", "testo non ancora acquisito: leggilo con il connettore Drive (fileId=%s) e registralo con "
                                               "school_material_acquire_v1" % ref.get("drive_file_id"))
    a = ref["attribuzione"]
    if a["stato"] == "determinata" and a["materia"] == key:
        attr = "confermata"
    elif a["stato"] == "possibile" and key in a["candidati"]:
        attr = "possibile"
    else:
        return stop("materia_non_corrispondente", "il materiale non e' attribuito a %s (attribuzione: %s, materia: %s, candidati: %s)"
                    % (key, a["stato"], a["materia"], ", ".join(a["candidati"]) or "-"))
    v = vs[-1]
    contenuto = {"testo": v["text"]}
    if risposta_utente is not None:
        contenuto["risposta_utente"] = risposta_utente
    m13 = SS.build(reg, key, modalita, contenuto, now_dt, manual=manual, obiettivo=obiettivo, subjects=subjects, source_path=source_path)
    parts = []
    for p in m13["contenuto"]["parti"]:
        if p["id"] == "utente":
            if p["sha256"] != v["sha256"]:
                raise RuntimeError("integrita': il testo passato a M13 non corrisponde alla versione acquisita")
            p = dict(p, id="materiale", provenienza="materiale_scolastico_acquisito",
                     verifica="id Drive corrispondente al catalogo M15; testo acquisito dall'agente con il connettore Drive")
        parts.append(p)
    relation = None
    if obiettivo:
        rel = [x for x in _associations(ref) if obiettivo in x["con"]]
        relation = rel or "nessuna_associazione_m5"
    frasi = [f for f in m13["sessione"]["frasi_obbligatorie"] if f["codice"] != "contenuto_utente"]
    frasi.insert(0, {"codice": "materiale", "frase": "Il contenuto e' il testo del materiale scolastico '%s' (%s, %s), acquisito il %s: "
                                                   "e' materiale della scuola, non il programma di una verifica."
                                                   % (ref["titolo"], _parent(ref)["corso"] or "corso non indicato", v["owner"] or
                                                      "autore non indicato", v["acquired_at"][:10])})
    frasi.append({"codice": "acquisito_non_studiato", "frase": "Il materiale e' disponibile, non e' detto che sia stato studiato."})
    if attr == "possibile":
        frasi.append({"codice": "attribuzione_possibile", "frase": "L'attribuzione del materiale a %s e' possibile, non confermata." % key})
    if obiettivo:
        frasi.append({"codice": "relazione_obiettivo",
                      "frase": ("Il collegamento con %s e' quello di M5 (%s): non stabilisce gli argomenti della verifica." % (
                          obiettivo, ", ".join("%s %s" % (x["relazione"], x["stato"]) for x in relation)))
                      if isinstance(relation, list) else
                      "Nessun collegamento M5 tra questo materiale e %s: non presentarlo come materiale della verifica." % obiettivo})
    sess = dict(m13["sessione"], frasi_obbligatorie=frasi, etichette_provenienza=dict(
        m13["sessione"]["etichette_provenienza"], materiale_scolastico_acquisito="testo di un file Drive citato da Classroom e "
        "identificato nel catalogo M15, acquisito con il connettore Drive: fonte scolastica, rappresentazione testuale dell'originale "
        "(formule e impaginazione possono essere imperfette)"))
    return dict(base, esito=m13["esito"], materiale={
        "titolo": ref["titolo"], "tipo_risorsa": ref["tipo_risorsa"], "drive_file_id": ref["drive_file_id"], "url": ref["url"],
        "attribuzione": dict(a, verifica=attr), "padre": _parent(ref), "associazioni_m5": _associations(ref),
        "relazione_obiettivo": relation, "versione": _public(v, False), "versioni_disponibili": len(vs)},
        contenuto={"parti": parts, "testo": v["text"], "risposta_utente": m13["contenuto"]["risposta_utente"],
                   "sha256_complessivo": m13["contenuto"]["sha256_complessivo"]},
        contesto_scolastico=m13["contesto_scolastico"], sessione=sess,
        regole_agente=["Etichetta il contenuto tratto dal materiale come materiale_scolastico_acquisito, citando titolo e corso."] +
        [r for r in m13["regole_agente"] if "contenuto_fornito_utente" not in r] +
        ["Etichetta ogni parte della risposta: materiale_scolastico_acquisito, contenuto_fornito_utente (risposta_utente) o "
         "conoscenza_generale.", "Non dire che l'utente ha studiato il materiale: acquisito non significa studiato."],
        provenance=dict(base["provenance"], m13=m13["provenance"]))
