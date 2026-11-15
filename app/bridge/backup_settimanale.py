"""CUORE · backup settimanale verificato di Finance e School (LaunchAgent cloud.cuore.backup-settimanale, domenica
prima della rotazione dei backup delle 12:00).

    python3 -B app/bridge/backup_settimanale.py [--db <database Finance>] [--school-personale <archivio .jsonl>]
                                                [--school-backup <cartella>] [--solo finance|school]

- Finance: `finance/scripts/backup-settimanale.ts` (stesso metodo e stesso schema dei nomi del backup prima della
  scrittura: `backup/<nome>-prima-di-settimanale-<istante>.sqlite3`, verificato, ruota con la politica C-08), in un
  processo Node con il modello dei permessi (scrittura solo nella cartella dei backup). Database predefinito: quello del
  ponte (`cuore_money.db_path()`).
- School: copia dell'archivio personale (predefinito `~/Library/Application Support/CUORE/school/personale.jsonl`) in
  `<cartella dell'archivio>/backup/personale-prima-di-settimanale-<istante>.jsonl` (0600, cartella 0700) con accanto
  l'impronta SHA-256 (`.sha256`, verificabile con `shasum -a 256 -c`). Verifica: impronte dell'originale prima e dopo e
  della copia tutte uguali, altrimenti la copia resta come `.non-verificato`. Archivio assente = niente da copiare.
  Le copie School sono piccole e non ruotano (nessuna cancellazione).
- Gli originali sono solo letti. Al massimo una copia al giorno per area (`presente` se c'e' gia').
- Uscita: una riga JSON con esiti e nomi dei file, mai dati. Codice 0 se tutto e' a posto, 1 altrimenti.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cuore_money  # noqa: E402
import scritture  # noqa: E402

SCRIPT = os.path.join(scritture.FINANCE, "scripts", "backup-settimanale.ts")
SCHOOL_PERSONALE = "~/Library/Application Support/CUORE/school/personale.jsonl"     # come serve.py
ZONE = "Europe/Rome"
SCHOOL_NAME = re.compile(r"^personale-prima-di-settimanale-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z\.jsonl$")


def stamp(now):
    """Istante come `stamp()` di Finance: ISO UTC con `-` al posto di `:` e `.`."""
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-") + "%03dZ" % (now.microsecond // 1000)


def _day(name):
    from zoneinfo import ZoneInfo
    m = SCHOOL_NAME.match(name)
    if not m:
        return None
    d, h, mi, s, ms = m.groups()
    return dt.datetime.fromisoformat("%sT%s:%s:%s.%s+00:00" % (d, h, mi, s, ms)).astimezone(ZoneInfo(ZONE)).date()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ Finance
def finance_command(node, db):
    """Come `scritture.finance_command`: lettura del codice, del database (e dei file accanto, solo per lo stato) e
    della cartella dei backup; scrittura solo nella cartella dei backup."""
    real = os.path.realpath(db)
    backup = os.path.join(os.path.dirname(os.path.dirname(real)), "backup")
    reads = [scritture.FINANCE, real] + [real + x for x in ("-wal", "-shm", "-journal")] + [backup, backup + "/*"]
    return [node, "--permission"] + ["--allow-fs-read=%s" % p for p in reads] + [
        "--allow-fs-write=%s" % backup, "--allow-fs-write=%s/*" % backup, SCRIPT, "--db", real]


def finance_backup(db):
    node, err = cuore_money._node()
    if err:
        return {"ok": False, "motivo": err}
    if not os.path.isfile(db):
        return {"ok": False, "motivo": "database Finance assente"}
    code, doc = scritture.run_node(finance_command(node, db), None, scritture.node_env())
    if not isinstance(doc, dict):
        return {"ok": False, "motivo": "risposta non valida (uscita %s)" % code}
    return {k: doc[k] for k in ("ok", "stato", "file", "motivo") if doc.get(k) is not None}


# ------------------------------------------------------------------------------------------------ School
def _check_dest(path):
    if not os.path.isabs(path):
        raise scritture.Rifiuto("--school-backup: percorso assoluto")
    lib = os.path.join(os.path.expanduser("~"), "Library")
    for f in (os.path.join(lib, "Mobile Documents"), os.path.join(lib, "CloudStorage"), scritture.REPO_ROOT):
        if scritture._inside(os.path.realpath(path), os.path.realpath(f)) or scritture._inside(path, f):
            raise scritture.Rifiuto("copie School in iCloud o nel repository: rifiutato")
    if not os.path.lexists(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise scritture.Rifiuto("cartella delle copie School non valida (collegamento, proprietario o permessi)")


def school_backup(src, dest=None, now=None, hash_copy=sha256):
    """Copia verificata dell'archivio personale School. `hash_copy` sostituibile solo nei test."""
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        src = scritture.school_archive(src)                # percorso, iCloud/repository, permessi privati
        if not os.path.isfile(src):
            return {"ok": True, "stato": "assente"}
        dest = dest or os.path.join(os.path.dirname(src), "backup")
        _check_dest(dest)
    except (scritture.Rifiuto, OSError) as e:
        return {"ok": False, "motivo": str(e) if isinstance(e, scritture.Rifiuto) else "cartella non accessibile (%s)" % type(e).__name__}
    today = now.astimezone(ZoneInfo(ZONE)).date()
    done = sorted(n for n in os.listdir(dest) if _day(n) == today)
    if done:
        return {"ok": True, "stato": "presente", "file": done[-1]}
    name = "personale-prima-di-settimanale-%s.jsonl" % stamp(now)
    final = os.path.join(dest, name)
    work = final + ".in-verifica"
    try:
        before = sha256(src)
        with open(src, "rb") as f:
            data = f.read()
        with os.fdopen(os.open(work, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        copy, after = hash_copy(work), sha256(src)
        if not before == hashlib.sha256(data).hexdigest() == copy == after:
            os.replace(work, final + ".non-verificato")
            return {"ok": False, "motivo": "impronta della copia diversa dall'archivio: copia non verificata"}
        os.replace(work, final)
        with os.fdopen(os.open(final + ".sha256", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (copy, name))
        return {"ok": True, "stato": "creato", "file": name}
    except OSError as e:
        if os.path.exists(work):
            os.replace(work, final + ".non-verificato")
        return {"ok": False, "motivo": "copia non riuscita (%s)" % type(e).__name__}


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="database Finance (predefinito: quello del ponte)")
    ap.add_argument("--school-personale", default=SCHOOL_PERSONALE, help="archivio personale School (.jsonl)")
    ap.add_argument("--school-backup", help="cartella delle copie School (predefinita: backup/ accanto all'archivio)")
    ap.add_argument("--solo", choices=("finance", "school"), help="una sola area")
    a = ap.parse_args(argv)
    res = {}
    if a.solo in (None, "finance"):
        res["finance"] = finance_backup(os.path.abspath(os.path.expanduser(a.db or cuore_money.db_path())))
    if a.solo in (None, "school"):
        res["school"] = school_backup(os.path.expanduser(a.school_personale),
                                      os.path.expanduser(a.school_backup) if a.school_backup else None)
    out(json.dumps(res, ensure_ascii=False))
    return 0 if all(r.get("ok") for r in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
