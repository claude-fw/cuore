"""Controlli dell'ambiente sintetico di CUORE Finance, lato Python (D3a, decisione C-10).

Stesse regole di `finance/src/synthetic/guard.ts`, per il futuro ponte di prova (D3b). NON e' importato dal ponte reale
(`serve.py`), che resta in sola lettura sui dati reali. Fail-closed: ogni dubbio e' un rifiuto (`ProvaRifiutata`).

- cartella esplicita e assoluta (nessun percorso predefinito, nessuna variabile d'ambiente `CUORE_*`);
- realpath sotto la cartella temporanea dell'utente e fuori da dati reali (`~/Library/Application Support/Cloud`,
  `/CUORE`), iCloud (`~/Library/Mobile Documents`, `~/Library/CloudStorage`) e repository;
- nessun collegamento simbolico; database con un solo collegamento fisico; proprietario e permessi privati;
- marcatore `meta.dataset = 'sintetico'` e id della sessione letti in sola lettura (`mode=ro`);
- porta del futuro ponte di prova diversa da quella del ponte reale (8765).
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
FINANCE = os.path.join(REPO_ROOT, "finance")
SYNTHETIC_DB = "finance-sintetico.sqlite3"
SESSION_FILE = "sessione.json"
SESSION_SCHEMA = "cuore.prova.sessione/1"
MARKER = "sintetico"
REAL_BRIDGE_PORT = 8765
DEFAULT_PROVA_PORT = 8766


class ProvaRifiutata(Exception):
    """Rifiuto di sicurezza: la cartella o il database non sono una copia sintetica verificata."""


def default_roots():
    home = os.path.expanduser("~")
    lib = os.path.join(home, "Library")
    return {"allowed": tempfile.gettempdir(),
            "forbidden": [os.path.join(lib, "Application Support", "Cloud"),
                          os.path.join(lib, "Application Support", "CUORE"),
                          os.path.join(lib, "Mobile Documents"), os.path.join(lib, "CloudStorage"), REPO_ROOT]}


def _inside(child, parent):
    rel = os.path.relpath(child, parent)
    return rel == "." or not (rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel))


def _private(path, kind):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise ProvaRifiutata("collegamento simbolico non ammesso: %s" % path)
    if (kind == "dir" and not stat.S_ISDIR(st.st_mode)) or (kind == "file" and not stat.S_ISREG(st.st_mode)):
        raise ProvaRifiutata("tipo di file inatteso: %s" % path)
    if st.st_uid != os.getuid():
        raise ProvaRifiutata("proprietario diverso: %s" % path)
    if st.st_mode & 0o077:
        raise ProvaRifiutata("permessi troppo aperti (%o): %s" % (st.st_mode & 0o777, path))
    if kind == "file" and st.st_nlink != 1:
        raise ProvaRifiutata("file con piu' collegamenti fisici: %s" % path)


def check_data_dir(path, roots=None):
    """Realpath della cartella di una sessione esistente, oppure `ProvaRifiutata`."""
    roots = roots or default_roots()
    if not isinstance(path, str) or not path.strip():
        raise ProvaRifiutata("cartella dati obbligatoria")
    if not os.path.isabs(path):
        raise ProvaRifiutata("percorso assoluto obbligatorio: %s" % path)
    if os.path.islink(path):
        raise ProvaRifiutata("collegamento simbolico non ammesso: %s" % path)
    if not os.path.isdir(path):
        raise ProvaRifiutata("cartella inesistente: %s" % path)
    try:
        real = os.path.realpath(path, strict=True) if _supports_strict() else os.path.realpath(path)
    except OSError:
        raise ProvaRifiutata("percorso non verificabile: %s" % path)
    for f in roots["forbidden"]:
        for candidate in {f, os.path.realpath(f)}:
            if _inside(real, candidate):
                raise ProvaRifiutata("percorso vietato (dati reali, iCloud o repository): %s" % real)
    allowed = os.path.realpath(roots["allowed"])
    if not _inside(real, allowed) or real == allowed:
        raise ProvaRifiutata("percorso fuori dalla cartella temporanea dell'utente: %s" % real)
    _private(real, "dir")
    return real


def _supports_strict():
    import inspect
    return "strict" in inspect.signature(os.path.realpath).parameters


def read_session(real_dir):
    p = os.path.join(real_dir, SESSION_FILE)
    if not os.path.lexists(p):
        raise ProvaRifiutata("file di sessione assente")
    _private(p, "file")
    try:
        with open(p, encoding="utf-8") as fh:
            s = json.load(fh)
    except (OSError, ValueError):
        raise ProvaRifiutata("file di sessione illeggibile")
    if not isinstance(s, dict) or s.get("schema") != SESSION_SCHEMA or not isinstance(s.get("id"), str) \
            or s.get("database") != SYNTHETIC_DB or s.get("stato") not in ("aperta", "chiusa"):
        raise ProvaRifiutata("file di sessione non valido")
    return s


def check_marker(real_dir, session):
    """Marcatore e id della sessione letti in SOLA LETTURA; rifiuto se assenti, errati o non leggibili."""
    db = os.path.join(real_dir, SYNTHETIC_DB)
    if not os.path.lexists(db):
        raise ProvaRifiutata("database sintetico assente")
    _private(db, "file")
    if os.path.dirname(os.path.realpath(db)) != real_dir:
        raise ProvaRifiutata("database fuori dalla cartella della sessione")
    # Il database e' in modalita' WAL: in sola lettura (`mode=ro`) SQLite dovrebbe creare il -shm e rifiuta. Come il
    # ponte Money: `immutable=1` (nessun file creato), ammesso solo con -wal assente o vuoto (dati tutti nel file).
    wal = db + "-wal"
    if os.path.lexists(wal) and (os.path.islink(wal) or os.path.getsize(wal) > 0):
        raise ProvaRifiutata("-wal non vuoto: database in uso, marcatore non verificabile ora")
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % quote(db), uri=True)
        try:
            rows = dict(con.execute("SELECT key, value FROM meta WHERE key IN ('dataset', 'sessione')").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        raise ProvaRifiutata("marcatore non verificabile")
    if rows.get("dataset") != MARKER:
        raise ProvaRifiutata("marcatore sintetico assente o errato: il database non e' una copia di prova")
    if rows.get("sessione") != session["id"]:
        raise ProvaRifiutata("il database appartiene a un'altra sessione")
    return db


def verify(path, roots=None):
    """Verifica completa in sola lettura: cartella, sessione, marcatore."""
    real = check_data_dir(path, roots)
    session = read_session(real)
    db = check_marker(real, session)
    return {"cartella": real, "database": db, "sessione": session["id"], "stato": session["stato"]}


def check_port(port):
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ProvaRifiutata("porta non valida: %r" % (port,))
    if port == REAL_BRIDGE_PORT:
        raise ProvaRifiutata("la porta %d e' del ponte reale" % REAL_BRIDGE_PORT)
    return port


def node_env():
    """Ambiente ridotto dei processi Node: nessuna variabile ereditata, salvo la cartella temporanea, che deve essere la
    stessa di Python perche' i due controlli (questo e `guard.ts`) ammettano la stessa radice."""
    return {"LANG": "C.UTF-8", "TZ": "Europe/Rome", "TMPDIR": tempfile.gettempdir()}


def _tmp_reads():
    """Sola lettura della radice temporanea (data e realpath): serve al controllo del percorso in `guard.ts`."""
    t = tempfile.gettempdir()
    return ["--allow-fs-read=%s" % p for p in sorted({t, os.path.realpath(t)})]


def write_command(node, path, roots=None):
    """Comando del processo di scrittura separato (finance/scripts/prova.ts esegui) per una sessione VERIFICATA, da
    lanciare con `node_env()`. I permessi di Node limitano i file letti e scritti dal processo (scrittura solo nella
    cartella della sessione); il database lo proteggono i controlli sopra (node:sqlite non e' vincolato dal modello dei
    permessi)."""
    info = verify(path, roots)
    if info["stato"] != "aperta":
        raise ProvaRifiutata("sessione chiusa: nessuna scrittura")
    real = info["cartella"]
    return [node, "--permission", "--allow-fs-read=%s" % FINANCE, "--allow-fs-read=%s" % real] + _tmp_reads() + [
            "--allow-fs-write=%s" % real, os.path.join(FINANCE, "scripts", "prova.ts"), "esegui", "--dati", real]


def money_command(node, path, today, roots=None):
    """Comando del feed Money della copia sintetica (`prova_money.ts`): solo lettura, nessuna scrittura via fs."""
    real = verify(path, roots)["cartella"]
    return [node, "--permission", "--allow-fs-read=%s" % FINANCE, "--allow-fs-read=%s" % HERE,
            "--allow-fs-read=%s" % real] + _tmp_reads() + [
            os.path.join(HERE, "prova_money.ts"), "--dati", real, "--today", today]


def create_command(node, path, roots=None):
    """Comando che crea una sessione NUOVA (`prova.ts crea`): cartella inesistente, genitore sotto la radice temporanea.
    Scrittura ammessa solo nella cartella da creare (`<cartella>/*`: per una cartella che ancora non esiste Node vuole
    il carattere jolly, verificato il 01/11 con Node 24.21)."""
    roots = roots or default_roots()
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ProvaRifiutata("percorso assoluto obbligatorio: %r" % (path,))
    if os.path.lexists(path):
        raise ProvaRifiutata("la cartella esiste gia': nessuna sovrascrittura (%s)" % path)
    parent = os.path.realpath(os.path.dirname(path))
    allowed = os.path.realpath(roots["allowed"])
    if not _inside(parent, allowed) or any(_inside(parent, c) for f in roots["forbidden"] for c in {f, os.path.realpath(f)}):
        raise ProvaRifiutata("percorso fuori dalla cartella temporanea dell'utente: %s" % path)
    target = os.path.join(parent, os.path.basename(path))
    return [node, "--permission", "--allow-fs-read=%s" % FINANCE] + _tmp_reads() + [
            "--allow-fs-write=%s/*" % target, os.path.join(FINANCE, "scripts", "prova.ts"), "crea", "--dati", target]
