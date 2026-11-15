#!/usr/bin/env python3
"""INVIO DEL FEED SCHOOL AL CLOUD (blocco E3, decisione del 26/10: opzione A / S1) — SPENTO PER DIFETTO.

School resta sul Mac: qui si costruisce il feed `cuore.school.feed/1` con il codice esistente del ponte
(`app/bridge/cuore_school.build_feed`, sola lettura del Registry, rete bloccata durante la lettura), lo si MINIMIZZA
(solo i campi che l'interfaccia mostra: elenco unico in `cloud/src/school-feed-campi.json`, lo stesso che il cloud usa
per rifiutare tutto il resto) e lo si invia con `POST <url>` al Durable Object School, autenticandosi con il token di
servizio di Cloudflare Access del Mac (`mac-school`: intestazioni `CF-Access-Client-Id/Secret`). Nessuna credenziale
ClasseViva o Classroom esce dal Mac.

Attivazione SOLO con una configurazione esplicita, che oggi non esiste: il file `cuore-cloud.json` nella cartella di
stato del job (accanto a `state.json`), con permessi 600:
    {"url": "https://<worker>/api/school", "token_file": "/percorso/assoluto/mac-school.json"}
e il file del token (permessi 600, del proprio utente, fuori dal repository e fuori da iCloud):
    {"client_id": "<id>.access", "client_secret": "<segreto>"}
`"attivo": false` nella configurazione sospende l'invio senza cancellarla. Senza configurazione: nulla, nemmeno una riga
di log (una sola verifica dell'esistenza del file). Si invia solo dopo un giro `--commit` (un dry-run non scrive mai
fuori dal Mac). Un errore (configurazione, rete, risposta del cloud) non solleva mai eccezioni e non cambia l'esito del
job: si registra `invio_cloud` nel log con un motivo a vocabolario fisso e si riprova al giro successivo.

Uso a mano (non invia nulla): `python3 school_cloud_push.py prova [--db ...] [--state-dir ...]` = configurazione
presente?, dimensione e conformita' del feed minimizzato (solo conteggi, nessun dato scolastico).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
BRIDGE = os.path.join(REPO, "app", "bridge")
RULES_FILE = os.path.join(REPO, "cloud", "src", "school-feed-campi.json")
CONFIG_NAME = "cuore-cloud.json"
TIMEOUT_S = 20
USER_AGENT = "cuore-school-job/1"
CLIENT_ID = re.compile(r"^[A-Za-z0-9]{8,64}\.access$")
CLIENT_SECRET = re.compile(r"^[A-Za-z0-9._~+/=-]{16,256}$")
REPLY_CODE = re.compile(r"^[a-z_]{1,40}$")


class ConfigError(Exception):
    """Configurazione presente ma non utilizzabile: `code` a vocabolario fisso (mai percorsi o valori nel log)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def load_rules(path=RULES_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------------------------------------ configurazione
def config_path(state_dir):
    return os.path.join(state_dir, CONFIG_NAME)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(url):
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 300:
        raise ConfigError("url_non_valido")
    u = urlsplit(url)
    host = u.hostname or ""
    if u.username or u.password or u.query or u.fragment or not host or not u.path.endswith("/api/school"):
        raise ConfigError("url_non_valido")
    if u.scheme != "https" and not (u.scheme == "http" and _loopback(host)):   # in chiaro solo verso questo Mac
        raise ConfigError("url_non_valido")
    return url


def load_config(state_dir):
    """None = invio spento (nessun file, o `"attivo": false`). Altrimenti {url, token_file}; ConfigError se non valida."""
    p = config_path(state_dir)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raise ConfigError("configurazione_illeggibile") from None
    if not isinstance(raw, dict) or set(raw) - {"url", "token_file", "attivo"} or raw.get("attivo", True) not in (True, False):
        raise ConfigError("configurazione_non_valida")
    if raw.get("attivo") is False:
        return None
    tf = raw.get("token_file")
    if not isinstance(tf, str) or not os.path.isabs(tf):
        raise ConfigError("configurazione_non_valida")
    return {"url": _check_url(raw.get("url")), "token_file": tf}


def _unsafe_place(path):
    real = os.path.realpath(path)
    return real.startswith(os.path.realpath(REPO) + os.sep) or "/Mobile Documents/" in real


def load_token(path):
    """Token di servizio del Mac: file regolare (non un collegamento), del proprio utente, permessi 600, fuori dal
    repository e da iCloud; solo `client_id` e `client_secret`. Il segreto non va mai nel log."""
    try:
        st = os.lstat(path)
    except OSError:
        raise ConfigError("token_assente") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ConfigError("permessi_token")
    if _unsafe_place(path):
        raise ConfigError("token_in_posto_non_sicuro")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raise ConfigError("token_illeggibile") from None
    if not isinstance(raw, dict) or set(raw) != {"client_id", "client_secret"} \
            or not CLIENT_ID.match(str(raw["client_id"])) or not CLIENT_SECRET.match(str(raw["client_secret"])):
        raise ConfigError("token_non_valido")
    return raw["client_id"], raw["client_secret"]


# ------------------------------------------------------------------------------------------------ minimizzazione
def _only(d, keys):
    return {k: d[k] for k in keys if k in d}


def minimize(feed, rules=None):
    """Solo cio' che l'interfaccia mostra (elenco in `school-feed-campi.json`): via le pagine non mostrate, i testi fissi per
    gli agenti (natura, regole_agente, navigazione, stati_blocco, grafici_assenti), il nome del file del Registry
    (`provenance.sorgente`), gli elenchi di uid della provenienza (i dati che l'interfaccia usa hanno gia' i loro uid),
    il tipo d'eccezione negli errori (`dettaglio`), `fonte` e `motore`. Contenuto dei blocchi e dei grafici invariato."""
    r = rules or load_rules()
    c = r["campi"]
    out = _only(feed, c["feed"])
    out["errori"] = [_only(e, c["errore"]) for e in feed.get("errori") or [] if isinstance(e, dict)]
    pages = {}
    for name in r["pagine"]:
        doc = (feed.get("pagine") or {}).get(name)
        if not isinstance(doc, dict):
            continue
        p = _only(doc, c["pagina"])
        p["provenance"] = _only(doc.get("provenance") or {}, c["provenance"])
        p["blocchi"] = [dict(_only(b, c["blocco"]), provenienza=_only(b.get("provenienza") or {}, c["provenienza"]))
                        for b in doc.get("blocchi") or [] if isinstance(b, dict)]
        p["grafici"] = [dict(_only(g, c["grafico"]), provenienza=_only(g.get("provenienza") or {}, c["provenienza"]))
                        for g in doc.get("grafici") or [] if isinstance(g, dict)]
        pages[name] = p
    out["pagine"] = pages
    return out


def sensitive_keys(v, rules=None, where="feed", out=None):
    """Percorsi delle chiavi che sembrano credenziali (stessa espressione del cloud): con anche una sola, niente invio."""
    rx = re.compile((rules or load_rules())["chiavi_vietate"], re.I)
    out = [] if out is None else out
    stack = [(v, where)]
    while stack:
        x, w = stack.pop()
        if isinstance(x, dict):
            for k, y in x.items():
                if rx.search(str(k)):
                    out.append(w)
                stack.append((y, "%s.%s" % (w, str(k)[:40])))
        elif isinstance(x, list):
            stack.extend((y, "%s[%d]" % (w, i)) for i, y in enumerate(x))
    return out


def _bridge():
    if BRIDGE not in sys.path:
        sys.path.insert(0, BRIDGE)
    import cuore_school_contract as K  # noqa: E402  (solo quando l'invio e' configurato)
    import cuore_school as CS  # noqa: E402
    return CS, K


def build_minimized(registry_path=None, now=None, subjects_file=None, manual_path=None, rules=None):
    """Feed del ponte (stesso codice di `/api/school`) gia' minimizzato."""
    CS, _ = _bridge()
    return minimize(CS.build_feed(registry_path, now, subjects_file=subjects_file, manual_path=manual_path), rules)


def check(feed, rules=None):
    """(byte del JSON compatto, problemi): contratto del ponte (`validate_feed`) + chiavi sensibili + dimensione."""
    r = rules or load_rules()
    _, K = _bridge()
    body = json.dumps(feed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    probs = K.validate_feed(feed) + ["chiave sensibile in %s" % w for w in sensitive_keys(feed, r)]
    if len(body) > r["max_byte"]:
        probs.append("troppo grande: %d byte (massimo %d)" % (len(body), r["max_byte"]))
    return body, probs


# ------------------------------------------------------------------------------------------------ invio
def send(url, client_id, client_secret, body, timeout=TIMEOUT_S):
    """Un solo tentativo (il giro successivo riprova), nessun reindirizzamento seguito (Access che rimanda al login =
    token non valido), risposta letta al massimo per 4 KiB. Mai eccezioni: esito e motivo a vocabolario fisso."""
    import socket
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    handlers = [NoRedirect()]
    if _loopback(urlsplit(url).hostname or ""):
        handlers.append(urllib.request.ProxyHandler({}))           # verso questo Mac mai attraverso un proxy
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": USER_AGENT, "CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET", headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(4096)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(4096)
        except Exception:  # noqa: BLE001
            raw = b""
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        return {"esito": "errore", "motivo": "rete", "dettaglio": type(getattr(e, "reason", e)).__name__[:40]}
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        reply = {}
    code = reply.get("errore") if isinstance(reply, dict) else None
    code = code if isinstance(code, str) and REPLY_CODE.match(code) else None
    out = {"http": status}
    if body is None:                                                  # lettura (richiesta di giro): serve la risposta
        out["risposta"] = reply if isinstance(reply, dict) else {}
    if status == 200:
        out.update(esito="inviato", ripetuto=bool(isinstance(reply, dict) and reply.get("ripetuto")))
    elif 300 <= status < 400:
        out.update(esito="errore", motivo="reindirizzato")              # Access: token di servizio non accettato
    elif status in (401, 403):
        out.update(esito="errore", motivo="non_autorizzato")
    elif status in (400, 409, 413, 415, 422):
        out.update(esito="rifiutato", motivo=code or "rifiutato")
    else:
        out.update(esito="errore", motivo=code or "errore_server")
    return out


def pending_request(state_dir, sender=send):
    """Richiesta di giro lasciata dal telefono («Sincronizza tutto»): istante ISO, oppure None (invio spento, nessuna
    richiesta, rete o cloud non raggiungibili). Mai eccezioni: nel dubbio decidono gli orari."""
    try:
        cfg = load_config(state_dir)
        if cfg is None:
            return None
        client_id, client_secret = load_token(cfg["token_file"])
        r = sender(cfg["url"] + "/richiesta", client_id, client_secret, None)
        v = ((r.get("risposta") or {}).get("risultato") or {}).get("richiesta") if r.get("http") == 200 else None
        return v if isinstance(v, str) else None
    except Exception:  # noqa: BLE001
        return None


def job_step(state_dir, registry_path=None, log=None, build=None, sender=send, rules=None):
    """Passo FACOLTATIVO alla fine di un giro `--commit` di school_job. Senza configurazione restituisce None e non fa
    nient'altro. Altrimenti costruisce, controlla e invia; restituisce l'esito (mai eccezioni, mai il segreto)."""
    try:
        cfg = load_config(state_dir)
        if cfg is None:
            return None
        res = _push(cfg, registry_path, build, sender, rules)
    except ConfigError as e:
        res = {"esito": "errore", "motivo": e.code}
    except Exception as e:  # noqa: BLE001 - un difetto qui non deve mai toccare il job
        res = {"esito": "errore", "motivo": "imprevisto", "dettaglio": type(e).__name__[:40]}
    if log is not None:
        log("invio_cloud", **res)
    return res


def _push(cfg, registry_path, build, sender, rules):
    client_id, client_secret = load_token(cfg["token_file"])
    r = rules or load_rules()
    feed = (build or (lambda: build_minimized(registry_path, rules=r)))()
    if feed.get("esito") == "non_disponibile":           # nessuna pagina: resta nel cloud l'ultimo feed buono, con la sua eta'
        return {"esito": "non_inviato", "motivo": "feed_non_disponibile"}
    body, probs = check(feed, r)
    if probs:
        return {"esito": "non_inviato", "motivo": "troppo_grande" if len(body) > r["max_byte"] else "feed_non_conforme",
                "byte": len(body), "problemi": len(probs)}
    return dict(sender(cfg["url"], client_id, client_secret, body), byte=len(body))


# ------------------------------------------------------------------------------------------------ prova a mano
def main(argv=None):
    ap = argparse.ArgumentParser(prog="school_cloud_push.py", description="Feed School per il cloud: prova senza invio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prova", help="costruisce e controlla il feed minimizzato; non invia nulla")
    p.add_argument("--db", default=None)
    p.add_argument("--state-dir", default=None)
    args = ap.parse_args(argv)
    if args.state_dir is None:
        import school_job as J
        args.state_dir = J.default_state_dir()
    try:
        cfg = load_config(args.state_dir)
        conf = "presente" if cfg else "assente o sospesa (invio spento)"
    except ConfigError as e:
        conf = "non valida (%s)" % e.code
    feed = build_minimized(args.db)
    body, probs = check(feed)
    print(json.dumps({"configurazione": conf, "esito_feed": feed.get("esito"), "pagine": sorted(feed.get("pagine") or {}),
                      "byte": len(body), "massimo_byte": load_rules()["max_byte"], "problemi": probs[:10]},
                     ensure_ascii=False, indent=1))
    return 0 if not probs else 1


if __name__ == "__main__":
    sys.exit(main())
