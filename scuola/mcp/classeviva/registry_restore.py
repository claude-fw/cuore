"""Verifica e ripristino SU COPIA degli snapshot del Registry Scuola (solo libreria standard).

Gli snapshot sono quelli creati da `cvv_sync._backup` in `<cartella del Registry>/backups/registry-*.sqlite3` (prima di
una scrittura che modifica righe esistenti o di una migrazione; 'premigrazione' = schema v1). Qui:
  - `verify(path)`: integrita', versione di schema e righe per tabella, in sola lettura (immutable=1, nessun file ausiliario);
  - `restore_copy(snapshot, dest)`: scrive un file NUOVO con il contenuto dello snapshot e lo verifica (stesse righe, stessa
    impronta logica). Rifiuta un `dest` gia' esistente, uguale allo snapshot o uguale al Registry predefinito: non
    sovrascrive MAI nulla. Sostituire il Registry reale resta un'operazione manuale, a job fermo;
  - `compare(older, newer)`: il Registry e' append-only (trigger: versions, events e runs immutabili, records mai
    cancellati), quindi ogni riga di uno snapshot deve esistere IDENTICA in un Registry piu' recente. Le differenze sono
    elencate solo come conteggi.
Nessun dato scolastico in uscita: solo nomi di file, conteggi, impronte ed esiti.

Uso: python3 registry_restore.py verifica <snapshot>
     python3 registry_restore.py ripristina <snapshot> <nuovo_file>
     python3 registry_restore.py confronta <piu_vecchio> <piu_recente>
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import registry as R  # noqa: E402

TABLES = ("records", "versions", "events", "runs", "projections")
KEYS = {"records": ("source", "kind", "source_uid"), "versions": ("source", "kind", "source_uid", "version"),
        "events": ("id",), "runs": ("run_id",), "projections": ("source", "kind", "source_uid", "target")}
# righe che non cambiano mai dopo la scrittura (trigger): devono restare identiche in ogni Registry successivo
IMMUTABLE = ("versions", "events", "runs")
FILE_MODE = 0o600


def _ro(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    wal = path + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        raise R.RegistryError("-wal non vuoto: il file potrebbe essere incompleto (scrittore attivo o crash)")
    return sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(os.path.abspath(path)), uri=True)


def _columns(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def _rows(conn, table, cols):
    return conn.execute("SELECT %s FROM %s ORDER BY %s" % (", ".join(cols), table, ", ".join(KEYS[table])))


def _digest(conn):
    """Impronta logica per tabella (sha256 delle righe in ordine di chiave, tutte le colonne)."""
    out = {}
    for t in TABLES:
        h = hashlib.sha256()
        cols = _columns(conn, t)
        h.update(json.dumps(cols).encode())
        for row in _rows(conn, t, cols):
            h.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
        out[t] = h.hexdigest()[:16]
    return out


def verify(path):
    """Stato di uno snapshot o di un Registry, in sola lettura. `ok` = integrita' ok, schema noto, tabelle presenti."""
    out = {"file": os.path.basename(path), "integrity_check": None, "schema_version": None, "righe": {}, "ok": False}
    try:
        conn = _ro(path)
    except (OSError, R.RegistryError) as e:
        out["errore"] = type(e).__name__
        return out
    try:
        out["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        out["schema_version"] = R._version_of(conn)
        out["righe"] = {t: conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in TABLES}
        out["impronta"] = _digest(conn)
    except sqlite3.DatabaseError as e:
        out["errore"] = type(e).__name__
        return out
    finally:
        conn.close()
    out["ok"] = out["integrity_check"] == "ok" and out["schema_version"] in (1, R.SCHEMA_VERSION)
    if out["ok"] and out["schema_version"] != R.SCHEMA_VERSION:
        out["nota"] = "schema v%s: va migrato esplicitamente prima dell'uso" % out["schema_version"]
    return out


def restore_copy(snapshot, dest):
    """Ripristina `snapshot` in un file NUOVO `dest` (0600) e lo verifica. Mai sovrascritture."""
    snapshot, dest = os.path.abspath(snapshot), os.path.abspath(dest)
    if os.path.exists(dest) or os.path.exists(dest + "-wal") or os.path.exists(dest + ".in-corso"):
        raise FileExistsError("destinazione gia' esistente: nessuna sovrascrittura")
    if dest == snapshot or os.path.realpath(dest) == os.path.realpath(R.default_path()):
        raise ValueError("destinazione non ammessa: deve essere un file nuovo, diverso dallo snapshot e dal Registry reale")
    if not os.path.isdir(os.path.dirname(dest)):
        raise FileNotFoundError("cartella di destinazione inesistente")
    before = verify(snapshot)
    if not before["ok"]:
        raise R.RegistryError("snapshot non valido: %s" % {k: before.get(k) for k in ("integrity_check", "schema_version", "errore")})
    work = dest + ".in-corso"
    src = _ro(snapshot)
    try:
        out = sqlite3.connect(work)
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")         # file unico e autosufficiente, nessun -wal in sospeso
        finally:
            out.close()
    finally:
        src.close()
    os.chmod(work, FILE_MODE)
    after = verify(work)
    same = after["ok"] and after["righe"] == before["righe"] and after.get("impronta") == before.get("impronta")
    if not same:
        os.replace(work, work + ".scartato")                  # conservato per l'analisi, mai usato
        raise R.RegistryError("copia ripristinata diversa dallo snapshot")
    os.rename(work, dest)
    after["file"] = os.path.basename(dest)
    return {"snapshot": before, "copia": after, "contenuto_identico": True}


def compare(older, newer):
    """Verifica append-only: ogni riga di `older` esiste in `newer`. Solo conteggi."""
    a, b = _ro(older), _ro(newer)
    out = {"vecchio": os.path.basename(older), "recente": os.path.basename(newer), "tabelle": {}}
    try:
        for t in TABLES:
            cols = [c for c in _columns(a, t) if c in set(_columns(b, t))]   # v1 -> v2: solo le colonne comuni
            ra = {tuple(r[:len(KEYS[t])]): tuple(r) for r in _rows(a, t, list(KEYS[t]) + cols)}
            rb = {tuple(r[:len(KEYS[t])]): tuple(r) for r in _rows(b, t, list(KEYS[t]) + cols)}
            missing = [k for k in ra if k not in rb]
            changed = [k for k in ra if k in rb and ra[k] != rb[k]]
            row = {"righe_vecchio": len(ra), "righe_recente": len(rb), "mancanti_nel_recente": len(missing),
                   "modificate": len(changed), "solo_nel_recente": len(rb.keys() - ra.keys())}
            if t == "records":                                   # records si aggiornano: conta solo le regressioni
                vi = cols.index("current_version") + len(KEYS[t])
                row["modificate"] = 0
                row["versione_regredita"] = sum(1 for k in ra if k in rb and rb[k][vi] < ra[k][vi])
            out["tabelle"][t] = row
    finally:
        a.close()
        b.close()
    out["coerente"] = all(r["mancanti_nel_recente"] == 0 and r["modificate"] == 0 and not r.get("versione_regredita")
                          for t, r in out["tabelle"].items() if t in IMMUTABLE + ("records",))
    return out


def main(argv):
    if len(argv) >= 2 and argv[0] == "verifica":
        res = verify(argv[1])
    elif len(argv) >= 3 and argv[0] == "ripristina":
        res = restore_copy(argv[1], argv[2])
    elif len(argv) >= 3 and argv[0] == "confronta":
        res = compare(argv[1], argv[2])
    else:
        print(__doc__.split("Uso:")[1].strip())
        return 64
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0 if res.get("ok", res.get("coerente", res.get("contenuto_identico"))) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
