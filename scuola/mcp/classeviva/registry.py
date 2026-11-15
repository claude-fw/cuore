"""Registro locale SCUOLA: L1 RAW + versioni + stato dei record. Solo libreria standard (sqlite3), Python >= 3.9.

Questo modulo NON conosce le fonti (ClasseViva, Classroom): conserva record grezzi di QUALSIASI fonte.

Garanzie (verificate dai test):
  - RAW immutabile: ogni versione e' una riga append-only (trigger SQLite vietano UPDATE/DELETE). Il record
    grezzo e' salvato come testo JSON esatto, con i campi nell'ordine ricevuto.
  - Una nuova versione nasce SOLO se cambia il contenuto (content_hash). I campi "volatili" dichiarati dal chiamante
    (es. un flag di lettura) sono esclusi dall'hash e registrati a parte come STATO osservato, non come RAW.
    Un campo volatile "**.<nome>" e' escluso dall'hash a QUALSIASI profondita' (es. "**.thumbnailUrl", URL che la
    fonte rigenera a ogni lettura); non e' registrato come stato. Il RAW salvato resta comunque integro.
  - Nessuna cancellazione: un record sparito dalla fonte passa a 'missing_in_source' e resta con tutte le versioni.
    Se la fonte restituisce una lista sospettosamente ridotta, NON marca nulla e lo segnala.
  - Ogni ingest e' atomico (una transazione). Un errore di una fonte non tocca i record delle altre.
  - Identita' = (source, kind, source_uid). Il kind evita collisioni tra tipi diversi con lo stesso numero.
    L'ID storico (es. 'cvv:<id>') e' conservato in 'legacy_uid'.
  - Nessun campo manuale qui: le informazioni scritte dall'utente non vivono in questo registro.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import uuid

SCHEMA_VERSION = 2
# v2 (M3): runs con finished_at, duration_ms, mode, outcome + trigger di protezione. Migrazione ADDITIVA ed esplicita da v1.
STALE_AFTER_HOURS = 48          # soglia PROVVISORIA di freschezza (da confermare)
PRESENT = "present"
MISSING = "missing_in_source"
DEFAULT_DB = os.path.join("~", "Library", "Application Support", "Cloud", "school", "registry.sqlite3")


def default_path():
    """Percorso predefinito (FUORI da iCloud: un database vivo in iCloud rischia di corrompersi). Non crea nulla."""
    return os.path.expanduser(os.environ.get("CLOUD_SCHOOL_DB", DEFAULT_DB))


class RegistryError(Exception):
    pass


def canonical_json(obj):
    """JSON canonico (chiavi ordinate, nessuno spazio). Solleva TypeError se non e' JSON puro: niente conversioni silenziose."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


DEEP = "**."      # prefisso dei campi volatili esclusi a qualsiasi profondita'


def strip_volatile(raw, exclude=()):
    """Copia del record senza i campi volatili: "nome" = chiave di primo livello, "**.nome" = chiave a qualsiasi
    profondita' (dentro oggetti e liste). Non modifica `raw`."""
    if not isinstance(raw, dict):
        raise TypeError("il record grezzo deve essere un oggetto JSON")
    top = {e for e in exclude if not e.startswith(DEEP)}
    deep = {e[len(DEEP):] for e in exclude if e.startswith(DEEP)}
    if "" in deep:
        raise ValueError("campo volatile annidato senza nome: %r" % DEEP)
    body = {k: v for k, v in raw.items() if k not in top}
    if not deep:
        return body

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items() if k not in deep}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(body)


def content_hash(raw, exclude=()):
    """sha256 del record grezzo meno i campi volatili dichiarati (vedi strip_volatile)."""
    return hashlib.sha256(canonical_json(strip_volatile(raw, exclude)).encode("utf-8")).hexdigest()


def _utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS records(
  source TEXT NOT NULL, kind TEXT NOT NULL, source_uid TEXT NOT NULL,
  legacy_uid TEXT,
  item_date TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  source_status TEXT NOT NULL CHECK(source_status IN ('present','missing_in_source')),
  missing_since TEXT,
  current_version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  volatile_json TEXT, volatile_seen_at TEXT,
  PRIMARY KEY(source, kind, source_uid)
);
CREATE INDEX IF NOT EXISTS records_legacy ON records(source, kind, legacy_uid);

CREATE TABLE IF NOT EXISTS versions(
  source TEXT NOT NULL, kind TEXT NOT NULL, source_uid TEXT NOT NULL,
  version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  hash_excludes TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  captured_at TEXT NOT NULL, run_id TEXT NOT NULL,
  PRIMARY KEY(source, kind, source_uid, version),
  FOREIGN KEY(source, kind, source_uid) REFERENCES records(source, kind, source_uid)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
  started_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ok','error')),
  items_seen INTEGER, counts_json TEXT, warnings_json TEXT, error TEXT,
  finished_at TEXT, duration_ms INTEGER, mode TEXT, outcome TEXT
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, source_uid TEXT NOT NULL,
  event TEXT NOT NULL, at TEXT NOT NULL, detail_json TEXT
);

CREATE TABLE IF NOT EXISTS projections(
  source TEXT NOT NULL, kind TEXT NOT NULL, source_uid TEXT NOT NULL, target TEXT NOT NULL,
  target_ref TEXT, projected_hash TEXT NOT NULL, projected_interp_version TEXT,
  projected_status TEXT NOT NULL DEFAULT 'present', projected_at TEXT NOT NULL,
  PRIMARY KEY(source, kind, source_uid, target)
);

CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions
  BEGIN SELECT RAISE(ABORT, 'RAW immutabile: versions non si modifica'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions
  BEGIN SELECT RAISE(ABORT, 'RAW immutabile: versions non si cancella'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'storico eventi immutabile'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'storico eventi immutabile'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs
  BEGIN SELECT RAISE(ABORT, 'storico dei run immutabile'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
  BEGIN SELECT RAISE(ABORT, 'storico dei run immutabile'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
  BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione automatica dei record'); END;
"""


# ---- migrazione v1 -> v2 (additiva; mai implicita; lo snapshot e' a carico del chiamante) -------------------
_MIGRATE_V1_V2 = [
    "ALTER TABLE runs ADD COLUMN finished_at TEXT",
    "ALTER TABLE runs ADD COLUMN duration_ms INTEGER",
    "ALTER TABLE runs ADD COLUMN mode TEXT",
    "ALTER TABLE runs ADD COLUMN outcome TEXT",
    "CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs "
    "BEGIN SELECT RAISE(ABORT, 'storico dei run immutabile'); END",
    "CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs "
    "BEGIN SELECT RAISE(ABORT, 'storico dei run immutabile'); END",
]


def _version_of(conn):
    """Versione di schema memorizzata in una connessione sqlite3 (None se il file e' vuoto/non inizializzato)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else None


def stored_version(path):
    """Versione di schema di un file registro, letta in sola lettura (immutable=1: nessun file ausiliario)."""
    conn = sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(os.path.abspath(path)), uri=True)
    try:
        return _version_of(conn)
    finally:
        conn.close()


def _apply_v1_v2(conn):
    """Applica le istruzioni additive e porta la versione a 2. Chiamare dentro una transazione."""
    for stmt in _MIGRATE_V1_V2:
        conn.execute(stmt)
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migrated_v1_to_v2_at', ?)", (_utcnow(),))


def migrate_file(path):
    """MIGRAZIONE ESPLICITA di un file v1 -> v2 (una transazione, poi integrity_check). Ritorna un dettaglio.
    NON crea snapshot: lo deve fare il chiamante PRIMA (cvv_sync lo fa e lo verifica)."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        v = _version_of(conn)
        if v != 1:
            raise RegistryError("migrazione v1->v2 non applicabile: schema attuale v%s" % v)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _apply_v1_v2(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return {"da": 1, "a": SCHEMA_VERSION, "integrity_check": ic, "versione_dopo": _version_of(conn)}
    finally:
        conn.close()


def _derive_outcome(status, counts):
    """Esito di un run LEGACY (senza colonna outcome): derivato in lettura, mai riscritto."""
    if status == "error":
        return "error"
    c = counts or {}
    return "ok_con_rifiuti" if c.get("missing_refused") else "ok"


class Registry:
    def __init__(self, path=":memory:", clock=None):
        self.path = path
        self.clock = clock or _utcnow
        self.migrated_in_memory = False
        on_disk = path != ":memory:"
        new_file = on_disk and not os.path.exists(path)
        if on_disk:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)   # transazioni gestite a mano
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # M3: la versione si controlla PRIMA di eseguire lo schema, cosi' un file piu' vecchio non viene mai
        # modificato in modo implicito (la migrazione e' solo esplicita: migrate_file).
        v = _version_of(self.db)
        if v is not None and v != SCHEMA_VERSION:
            self.db.close()
            raise RegistryError("schema del registro v%s, atteso v%s: serve una migrazione esplicita"
                                % (v, SCHEMA_VERSION))
        if on_disk:
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if new_file:
            os.chmod(path, 0o600)       # dati scolastici: leggibile solo dal proprietario
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RegistryError("schema del registro v%s, atteso v%s: serve una migrazione esplicita"
                                % (row["value"], SCHEMA_VERSION))

    def close(self):
        self.db.close()

    # ---- copie IN MEMORIA (usate dal dry-run: non toccano mai il file originale) ---------------------
    def _check_schema_version(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            raise RegistryError("schema del registro non compatibile (atteso v%s)" % SCHEMA_VERSION)

    @classmethod
    def from_snapshot(cls, path, clock=None):
        """Copia IN MEMORIA di un registro su disco. Il file e' aperto con immutable=1: sola lettura, nessun file
        ausiliario (-wal/-shm/-journal) viene creato, nessun byte dell'originale cambia. Se esiste un -wal non vuoto
        (scrittore attivo o crash) la copia potrebbe essere incompleta: in quel caso si rifiuta."""
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        wal = path + "-wal"
        if os.path.exists(wal) and os.path.getsize(wal) > 0:
            raise RegistryError("il registro ha un -wal non vuoto: una copia in sola lettura potrebbe essere incompleta")
        reg = cls(":memory:", clock=clock)
        src = sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(os.path.abspath(path)), uri=True)
        try:
            src.backup(reg.db)
        finally:
            src.close()
        if _version_of(reg.db) == 1:          # copia IN MEMORIA di un file v1: si porta a v2 qui, il file non cambia
            reg.db.execute("BEGIN")
            _apply_v1_v2(reg.db)
            reg.db.execute("COMMIT")
            reg.migrated_in_memory = True
        reg._check_schema_version()
        return reg

    def clone_memory(self, clock=None):
        """Copia indipendente IN MEMORIA di questo registro (per simulazioni): l'originale non cambia."""
        reg = Registry(":memory:", clock=clock or self.clock)
        self.db.backup(reg.db)
        return reg

    # ---- scrittura -------------------------------------------------------------------------------
    def _event(self, run_id, source, kind, uid, event, now, detail=None):
        self.db.execute("INSERT INTO events(run_id,source,kind,source_uid,event,at,detail_json) VALUES(?,?,?,?,?,?,?)",
                        (run_id, source, kind, uid, event, now, canonical_json(detail) if detail else None))

    def ingest(self, source, kind, items, *, window=None, complete=True, volatile=(), min_ratio=0.5,
               allow_empty=False, run_id=None, hold=()):
        """Registra una lettura completa (o per finestra) di una fonte.

        items: [{"source_uid": str, "raw": dict, "date": 'AAAA-MM-GG' (opz.), "legacy_uid": str (opz.)}]
        window: (da, a) date ISO: i record con item_date nella finestra e assenti dalla lettura sono 'missing'.
        complete: False = lettura parziale (nessun record viene marcato missing).
        allow_empty: il chiamante dichiara di fidarsi della lettura: salta i controlli anti-anomalia (lista vuota,
        calo oltre min_ratio) e marca davvero come scomparsi i record assenti. Default False.
        volatile: campi esclusi dall'hash, registrati come stato osservato.
        hold: source_uid la cui identita' e' in verifica in questa lettura (es. corrispondenza ambigua): non vengono
        marcati come scomparsi. Default vuoto = comportamento invariato.
        Un item puo' portare "provenance" (dict): l'identita' e' stata riconciliata dall'adattatore; viene registrato
        l'evento 'reidentified' con quel dettaglio (spiega perche' il record mantiene il suo source_uid).
        Tutto o niente: una transazione. Input non valido => ValueError prima di scrivere qualsiasi cosa.
        """
        volatile = tuple(sorted(volatile))
        hold = frozenset(hold)
        items = list(items)
        seen = set()
        for it in items:
            uid, raw = it.get("source_uid"), it.get("raw")
            if not isinstance(uid, str) or not uid:
                raise ValueError("source_uid mancante o non testuale")
            if not isinstance(raw, dict):
                raise ValueError("raw deve essere un oggetto (%s)" % uid)
            if uid in seen:
                raise ValueError("source_uid duplicato nella lettura: %s" % uid)
            seen.add(uid)
            canonical_json(raw)          # fallisce subito se non e' JSON puro
            if it.get("provenance") is not None:
                canonical_json(it["provenance"])
        now = self.clock()
        t0 = time.perf_counter()
        run_id = run_id or uuid.uuid4().hex[:12]
        counts = {"create": 0, "update": 0, "unchanged": 0, "state_change": 0, "reappeared": 0,
                  "missing": 0, "missing_refused": 0}
        keys = {k: [] for k in counts}
        reidentified = []
        warnings = []
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for it in items:
                uid, raw = it["source_uid"], it["raw"]
                if it.get("provenance") is not None:
                    self._event(run_id, source, kind, uid, "reidentified", now, it["provenance"])
                    reidentified.append(uid)
                h = content_hash(raw, volatile)
                vol = {k: raw[k] for k in volatile if not k.startswith(DEEP) and k in raw}
                vol_json = canonical_json(vol) if vol else None
                date, legacy = it.get("date"), it.get("legacy_uid")
                row = db.execute("SELECT * FROM records WHERE source=? AND kind=? AND source_uid=?",
                                 (source, kind, uid)).fetchone()
                if row is None:
                    db.execute("INSERT INTO records(source,kind,source_uid,legacy_uid,item_date,first_seen,last_seen,"
                               "source_status,current_version,content_hash,volatile_json,volatile_seen_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (source, kind, uid, legacy, date, now, now, PRESENT, 1, h, vol_json,
                                now if vol_json else None))
                    self._add_version(source, kind, uid, 1, h, volatile, raw, now, run_id)
                    self._event(run_id, source, kind, uid, "create", now, {"version": 1, "content_hash": h})
                    counts["create"] += 1
                    keys["create"].append(uid)
                    continue
                was_missing = row["source_status"] == MISSING
                if row["content_hash"] != h:
                    v = row["current_version"] + 1
                    self._add_version(source, kind, uid, v, h, volatile, raw, now, run_id)
                    db.execute("UPDATE records SET current_version=?, content_hash=?, last_seen=?, source_status=?, "
                               "missing_since=NULL, item_date=COALESCE(?,item_date), legacy_uid=COALESCE(?,legacy_uid), "
                               "volatile_json=?, volatile_seen_at=? WHERE source=? AND kind=? AND source_uid=?",
                               (v, h, now, PRESENT, date, legacy, vol_json, now if vol_json else None,
                                source, kind, uid))
                    self._event(run_id, source, kind, uid, "update", now,
                                {"from_version": row["current_version"], "to_version": v,
                                 "old_hash": row["content_hash"], "new_hash": h})
                    counts["update"] += 1
                    keys["update"].append(uid)
                else:
                    db.execute("UPDATE records SET last_seen=?, source_status=?, missing_since=NULL, "
                               "item_date=COALESCE(?,item_date), legacy_uid=COALESCE(?,legacy_uid) "
                               "WHERE source=? AND kind=? AND source_uid=?",
                               (now, PRESENT, date, legacy, source, kind, uid))
                    if vol_json != row["volatile_json"]:
                        db.execute("UPDATE records SET volatile_json=?, volatile_seen_at=? "
                                   "WHERE source=? AND kind=? AND source_uid=?",
                                   (vol_json, now, source, kind, uid))
                        self._event(run_id, source, kind, uid, "state_change", now,
                                    {"old": row["volatile_json"], "new": vol_json})
                        counts["state_change"] += 1
                        keys["state_change"].append(uid)
                    else:
                        counts["unchanged"] += 1
                        keys["unchanged"].append(uid)
                if was_missing:
                    self._event(run_id, source, kind, uid, "reappeared", now, {"missing_since": row["missing_since"]})
                    counts["reappeared"] += 1
                    keys["reappeared"].append(uid)
            if complete:
                self._mark_missing(source, kind, seen, len(items), window, min_ratio, allow_empty,
                                   run_id, now, counts, keys, warnings, hold)
            # esito esplicito: 'rifiutato' = lettura vuota/anomala non applicata; 'ok_con_rifiuti' = applicata ma
            # con 'missing' rifiutato; 'ok' altrimenti. status resta ok|error (nessuna ricostruzione della tabella).
            applied = counts["create"] + counts["update"] + counts["state_change"] + counts["reappeared"] + counts["unchanged"]
            outcome = ("rifiutato" if counts["missing_refused"] and not applied
                       else "ok_con_rifiuti" if counts["missing_refused"] else "ok")
            db.execute("INSERT INTO runs(run_id,source,kind,started_at,status,items_seen,counts_json,warnings_json,"
                       "finished_at,duration_ms,mode,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, source, kind, now, "ok", len(items), canonical_json(counts),
                        canonical_json(warnings) if warnings else None, self.clock(),
                        int((time.perf_counter() - t0) * 1000), self._mode(), outcome))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        out = {"run_id": run_id, "status": "ok", "outcome": outcome, "items_seen": len(items), "counts": counts,
               "keys": keys, "warnings": warnings}
        if reidentified:
            out["reidentified"] = reidentified
        return out

    def _add_version(self, source, kind, uid, version, h, volatile, raw, now, run_id):
        self.db.execute("INSERT INTO versions(source,kind,source_uid,version,content_hash,hash_excludes,raw_json,"
                        "captured_at,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                        (source, kind, uid, version, h, json.dumps(list(volatile)),
                         json.dumps(raw, ensure_ascii=False), now, run_id))   # raw esatto, ordine dei campi originale

    def _mark_missing(self, source, kind, seen, n_items, window, min_ratio, allow_empty,
                      run_id, now, counts, keys, warnings, hold=frozenset()):
        rows = self.db.execute("SELECT source_uid, item_date FROM records WHERE source=? AND kind=? AND source_status=?",
                               (source, kind, PRESENT)).fetchall()
        cand = [r["source_uid"] for r in rows
                if (window is None or (r["item_date"] and window[0] <= r["item_date"] <= window[1]))
                and r["source_uid"] not in hold]
        gone = [u for u in cand if u not in seen]
        if not gone:
            return
        seen_in_scope = len(cand) - len(gone)
        suspicious = not allow_empty and (n_items == 0 or
                                          (len(cand) >= 2 and seen_in_scope < min_ratio * len(cand)))
        if suspicious:
            counts["missing_refused"] = len(gone)
            keys["missing_refused"] = gone
            warnings.append("lettura anomala (%d record letti, %d attesi nell'ambito): nessun record marcato come "
                            "scomparso" % (seen_in_scope, len(cand)))
            return
        for u in gone:
            self.db.execute("UPDATE records SET source_status=?, missing_since=? WHERE source=? AND kind=? AND source_uid=?",
                            (MISSING, now, source, kind, u))
            self._event(run_id, source, kind, u, "missing", now)
            counts["missing"] += 1
            keys["missing"].append(u)

    def _mode(self):
        """'commit' = registro su disco (scrittura reale); 'dry_run' = registro in memoria (copia/simulazione)."""
        return "dry_run" if self.path == ":memory:" else "commit"

    def record_failed_run(self, source, kind, error, run_id=None, duration_ms=None):
        run_id = run_id or uuid.uuid4().hex[:12]
        now = self.clock()
        self.db.execute("INSERT INTO runs(run_id,source,kind,started_at,status,error,finished_at,duration_ms,mode,outcome) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, source, kind, now, "error", str(error)[:300], now, duration_ms, self._mode(), "error"))
        return run_id

    def run_source(self, source, kind, fetch, **ingest_kwargs):
        """fetch() -> lista di item. Qualunque errore (rete, parsing, ingest) viene registrato come run 'error'
        e NON modifica alcun record: le altre fonti proseguono. Non solleva eccezioni di fonte."""
        t0 = time.perf_counter()
        try:
            items = fetch()
            return self.ingest(source, kind, items, **ingest_kwargs)
        except Exception as e:      # noqa: BLE001 - isolamento volutamente ampio
            msg = "%s: %s" % (type(e).__name__, str(e)[:250])
            rid = self.record_failed_run(source, kind, msg, duration_ms=int((time.perf_counter() - t0) * 1000))
            return {"run_id": rid, "status": "error", "outcome": "error", "error": msg, "counts": None}

    # ---- lettura ---------------------------------------------------------------------------------
    def get_record(self, source, kind, source_uid):
        r = self.db.execute("SELECT * FROM records WHERE source=? AND kind=? AND source_uid=?",
                            (source, kind, source_uid)).fetchone()
        if r is None:
            return None
        v = self.db.execute("SELECT * FROM versions WHERE source=? AND kind=? AND source_uid=? AND version=?",
                            (source, kind, source_uid, r["current_version"])).fetchone()
        return {"source": source, "kind": kind, "source_uid": source_uid, "legacy_uid": r["legacy_uid"],
                "item_date": r["item_date"], "source_status": r["source_status"], "missing_since": r["missing_since"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"], "version": r["current_version"],
                "content_hash": r["content_hash"], "captured_at": v["captured_at"],
                "hash_excludes": json.loads(v["hash_excludes"]), "raw": json.loads(v["raw_json"]),
                "raw_json": v["raw_json"],
                "volatile": json.loads(r["volatile_json"]) if r["volatile_json"] else None,
                "volatile_seen_at": r["volatile_seen_at"]}

    def find_by_legacy_uid(self, source, kind, legacy_uid):
        r = self.db.execute("SELECT source_uid FROM records WHERE source=? AND kind=? AND legacy_uid=?",
                            (source, kind, legacy_uid)).fetchone()
        return self.get_record(source, kind, r["source_uid"]) if r else None

    def get_versions(self, source, kind, source_uid):
        return [{"version": v["version"], "content_hash": v["content_hash"], "captured_at": v["captured_at"],
                 "run_id": v["run_id"], "raw": json.loads(v["raw_json"])}
                for v in self.db.execute("SELECT * FROM versions WHERE source=? AND kind=? AND source_uid=? "
                                         "ORDER BY version", (source, kind, source_uid))]

    def list_records(self, source=None, kind=None, status=None):
        q, a = "SELECT source,kind,source_uid,legacy_uid,item_date,source_status,current_version,content_hash " \
               "FROM records WHERE 1=1", []
        for col, val in (("source", source), ("kind", kind), ("source_status", status)):
            if val is not None:
                q += " AND %s=?" % col
                a.append(val)
        return [dict(r) for r in self.db.execute(q + " ORDER BY item_date, source_uid", a)]

    def events(self, source=None, kind=None, source_uid=None):
        q, a = "SELECT * FROM events WHERE 1=1", []
        for col, val in (("source", source), ("kind", kind), ("source_uid", source_uid)):
            if val is not None:
                q += " AND %s=?" % col
                a.append(val)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", a)]

    def runs(self):
        """Run in ordine cronologico. Per i run LEGACY (colonne v2 NULL) mode/outcome sono DERIVATI in lettura
        (mode='commit': erano scritti su disco; outcome da status e conteggi); la riga non viene mai riscritta."""
        out = []
        for r in self.db.execute("SELECT * FROM runs ORDER BY started_at, rowid"):
            d = dict(r)
            if d.get("mode") is None:
                d["mode"], d["legacy"] = "commit", True
            if d.get("outcome") is None:
                counts = json.loads(d["counts_json"]) if d.get("counts_json") else None
                d["outcome"] = _derive_outcome(d["status"], counts)
            out.append(d)
        return out

    def freshness(self, now, stale_after_hours=None):
        """FRESCHEZZA derivata dai run (sola lettura). Per ogni (source, kind): ultimo run, ultimo esito 'ok', ultimo
        errore, esito, eta' in ore, errori consecutivi, 'stale' e motivo. Conta solo i run 'commit' (quelli che hanno
        davvero aggiornato il registro). 'now' e' esplicito (ISO 8601): niente orologio qui."""
        limit = STALE_AFTER_HOURS if stale_after_hours is None else stale_after_hours
        now_dt = dt.datetime.fromisoformat(now)
        by = {}
        for r in self.runs():
            if r["mode"] == "commit":
                by.setdefault((r["source"], r["kind"]), []).append(r)
        kinds = {(r["source"], r["kind"]) for r in self.db.execute("SELECT DISTINCT source, kind FROM records")}
        out = {}
        for key in sorted(kinds | set(by)):
            rs = by.get(key, [])
            if not rs:
                out["%s/%s" % key] = {"ultimo_run": None, "stale": True, "motivo": "mai_sincronizzato"}
                continue
            last = rs[-1]
            oks = [r for r in rs if r["outcome"] in ("ok", "ok_con_rifiuti")]
            errs = [r for r in rs if r["outcome"] == "error"]
            consec = 0
            for r in reversed(rs):
                if r["outcome"] == "error":
                    consec += 1
                else:
                    break
            last_ok = oks[-1] if oks else None
            ref = last_ok["finished_at"] or last_ok["started_at"] if last_ok else None
            age = round((now_dt - dt.datetime.fromisoformat(ref)).total_seconds() / 3600, 2) if ref else None
            if last_ok is None:
                stale, why = True, "nessuna_lettura_riuscita"
            elif age is not None and age > limit:
                stale, why = True, "oltre_soglia"
            elif last["outcome"] == "error":
                stale, why = True, "ultimo_run_in_errore"
            elif last["outcome"] in ("rifiutato", "ok_con_rifiuti"):
                stale, why = False, "ultima_lettura_con_rifiuti"
            else:
                stale, why = False, "ok"
            out["%s/%s" % key] = {
                "ultimo_run": {"run_id": last["run_id"], "inizio": last["started_at"], "fine": last.get("finished_at"),
                               "durata_ms": last.get("duration_ms"), "esito": last["outcome"], "letti": last["items_seen"],
                               "legacy": bool(last.get("legacy"))},
                "ultima_lettura_riuscita": last_ok["started_at"] if last_ok else None,
                "ultimo_errore": {"inizio": errs[-1]["started_at"], "errore": errs[-1]["error"]} if errs else None,
                "errori_consecutivi": consec, "eta_ore": age, "soglia_ore": limit, "stale": stale, "motivo": why}
        return {"calcolato_a": now, "soglia_ore": limit, "per_kind": out,
                "qualcosa_stale": any(v["stale"] for v in out.values())}

    def verify_integrity(self):
        """Ricalcola gli hash dai RAW salvati. Ritorna la lista dei problemi (vuota = integro)."""
        problems = []
        for v in self.db.execute("SELECT * FROM versions"):
            try:
                calc = content_hash(json.loads(v["raw_json"]), json.loads(v["hash_excludes"]))
            except (TypeError, ValueError) as e:
                problems.append("%s/%s/%s v%s: raw illeggibile (%s)" % (v["source"], v["kind"], v["source_uid"], v["version"], e))
                continue
            if calc != v["content_hash"]:
                problems.append("%s/%s/%s v%s: hash non corrisponde al raw" % (v["source"], v["kind"], v["source_uid"], v["version"]))
        for r in self.db.execute("SELECT r.*, v.content_hash AS vh FROM records r LEFT JOIN versions v "
                                 "ON v.source=r.source AND v.kind=r.kind AND v.source_uid=r.source_uid "
                                 "AND v.version=r.current_version"):
            if r["vh"] != r["content_hash"]:
                problems.append("%s/%s/%s: versione corrente non coerente con il record" % (r["source"], r["kind"], r["source_uid"]))
        return problems

    # ---- proiezioni: tabella STORICA (scritture verso superfici esterne, oggi nessuna; CUORE non usa Notion) ---
    def record_projection(self, source, kind, source_uid, target, target_ref, projected_hash,
                          projected_interp_version=None, status="present"):
        self.db.execute(
            "INSERT INTO projections(source,kind,source_uid,target,target_ref,projected_hash,projected_interp_version,"
            "projected_status,projected_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,kind,source_uid,target) DO UPDATE SET target_ref=excluded.target_ref, "
            "projected_hash=excluded.projected_hash, projected_interp_version=excluded.projected_interp_version, "
            "projected_status=excluded.projected_status, projected_at=excluded.projected_at",
            (source, kind, source_uid, target, target_ref, projected_hash, projected_interp_version, status, self.clock()))

    def projection_plan(self, source, kind, target, interp_version=None):
        """Cosa andrebbe scritto sul target (solo piano, nessuna scrittura). Non propone MAI cancellazioni.
        create / update (dato cambiato) / reinterpret (solo logica cambiata) / unchanged / mark_missing."""
        proj = {r["source_uid"]: r for r in self.db.execute(
            "SELECT * FROM projections WHERE source=? AND kind=? AND target=?", (source, kind, target))}
        plan = {"create": [], "update": [], "reinterpret": [], "unchanged": [], "mark_missing": [],
                "note": "Nessuna cancellazione: i record scomparsi dalla fonte vanno solo marcati."}
        for r in self.db.execute("SELECT source_uid, content_hash, source_status FROM records WHERE source=? AND kind=? "
                                 "ORDER BY item_date, source_uid", (source, kind)):
            uid, p = r["source_uid"], proj.get(r["source_uid"])
            if r["source_status"] == MISSING:
                if p is not None and p["projected_status"] == "present":
                    plan["mark_missing"].append(uid)
            elif p is None:
                plan["create"].append(uid)
            elif p["projected_hash"] != r["content_hash"] or p["projected_status"] != "present":
                plan["update"].append(uid)
            elif interp_version and p["projected_interp_version"] != interp_version:
                plan["reinterpret"].append(uid)
            else:
                plan["unchanged"].append(uid)
        return plan
