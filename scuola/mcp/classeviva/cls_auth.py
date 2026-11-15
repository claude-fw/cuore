#!/usr/bin/env python3
"""Autenticazione Google Classroom per l'adattatore (SOLA LETTURA). Solo libreria standard.

Due parti:
  * token_provider(): usato dal client (cls_client) per ottenere un access token a breve durata, rinnovandolo con il
    REFRESH TOKEN custodito nel Keychain di macOS (servizio `cloud.classroom`). Nessun token su file, mai nei log.
  * CLI `authorize` / `status` / `revoke`: eseguita UNA VOLTA dall'utente (richiede il suo consenso nel browser).

Sicurezza:
  - scope = solo quelli di LETTURA elencati in ALLOWED_SCOPES; qualsiasi altro scope viene rifiutato PRIMA di aprire il
    browser, e il refresh token viene salvato solo se Google concede esattamente quelli richiesti (a meno degli alias
    documentati in SCOPE_ALIASES: Google puo' restituire un nome equivalente al posto di quello chiesto);
  - se e' richiesto lo scope dei compiti, subito dopo il consenso si fa una chiamata reale (GET) a courseWork.list:
    se fallisce, il token viene revocato e nulla viene salvato;
  - il refresh token passa al Keychain via `security -i` (stdin), non da riga di comando (non visibile in `ps`);
  - prima del consenso si fa una prova (scrittura/lettura/cancellazione) sul Keychain, cosi' un problema locale non
    viene scoperto DOPO aver ottenuto il token;
  - client id/secret: stesso servizio del probe (`cloud.classroom-probe`).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PROBE_SERVICE = "cloud.classroom-probe"      # client_id / client_secret (creati da setup_keychain.sh)
TOKEN_SERVICE = "cloud.classroom"            # refresh_token

ALLOWED_SCOPES = {
    "courses": "https://www.googleapis.com/auth/classroom.courses.readonly",
    "coursework": "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "submissions": "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "materials": "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "announcements": "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "topics": "https://www.googleapis.com/auth/classroom.topics.readonly",
}
# Coppie di scope EQUIVALENTI (stesso permesso, due nomi). Google li mostra come un'unica voce nel consenso e nel token
# ne restituisce uno solo: chiesti entrambi, arriva solo `student-submissions.me.readonly`. Fuori da queste coppie il
# confronto resta letterale. Lo scope e' comunque verificato con una chiamata reale (verify_coursework).
SCOPE_ALIASES = (
    ("https://www.googleapis.com/auth/classroom.coursework.me.readonly",
     "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly"),
)
ENDPOINTS = {"auth": "https://accounts.google.com/o/oauth2/v2/auth",
             "token": "https://oauth2.googleapis.com/token",
             "revoke": "https://oauth2.googleapis.com/revoke"}


class AuthError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# ------------------------------------------------------------------ Keychain
class MacKeychain:
    """Keychain di macOS tramite `security`. La scrittura usa `security -i` (valore da stdin, non da argv)."""

    def __init__(self, run=subprocess.run):
        self._run = run

    def get(self, service, account):
        try:
            r = self._run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                          capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        return (r.stdout.rstrip("\n") or None) if r.returncode == 0 else None

    @staticmethod
    def _q(value):
        if any(c in value for c in "\n\r\x00"):
            raise AuthError("keychain", "valore non valido per il Keychain")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def set(self, service, account, value, label="Cloud Classroom"):
        cmd = "add-generic-password -U -s %s -a %s -l %s -w %s\n" % (
            self._q(service), self._q(account), self._q(label), self._q(value))
        try:
            r = self._run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            raise AuthError("keychain", "Keychain non disponibile: %s" % type(e).__name__)
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise AuthError("keychain", "scrittura nel Keychain non riuscita")

    def delete(self, service, account):
        try:
            r = self._run(["security", "delete-generic-password", "-s", service, "-a", account],
                          capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.SubprocessError):
            return False
        return r.returncode == 0


def keychain_preflight(kc):
    """Scrive/legge/cancella un valore di prova: se fallisce, nessun consenso viene chiesto."""
    probe = "prova-" + secrets.token_hex(6)
    kc.set(TOKEN_SERVICE, "preflight", probe, label="Cloud Classroom (prova)")
    ok = kc.get(TOKEN_SERVICE, "preflight") == probe
    kc.delete(TOKEN_SERVICE, "preflight")
    if not ok:
        raise AuthError("keychain", "prova di lettura dal Keychain fallita")


# ------------------------------------------------------------------- HTTP
def _post(url, data, timeout=30):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError) as e:
        raise AuthError("network", "rete non raggiungibile: %s" % type(e).__name__)
    try:
        return status, (json.loads(raw.decode()) if raw else None)
    except ValueError:
        return status, None


def _oauth_error(status, payload):
    if isinstance(payload, dict) and payload.get("error"):
        return "%s%s" % (payload["error"], " (%s)" % payload["error_description"] if payload.get("error_description") else "")
    return "HTTP %s" % status


# ------------------------------------------------------------ token provider
def token_provider(keychain=None, post=_post, endpoints=ENDPOINTS, clock=time.monotonic):
    """provider(force_refresh=False) -> access token. Access token in memoria (mai su disco), rinnovato dal refresh token."""
    kc = keychain or MacKeychain()
    state = {"token": None, "exp": 0.0}

    def provider(force_refresh=False):
        if not force_refresh and state["token"] and clock() < state["exp"] - 60:
            return state["token"]
        rt = kc.get(TOKEN_SERVICE, "refresh_token")
        cid, csec = kc.get(PROBE_SERVICE, "client_id"), kc.get(PROBE_SERVICE, "client_secret")
        if not rt:
            raise AuthError("no_refresh_token", "Classroom non autorizzato: esegui `python3 cls_auth.py authorize`")
        if not (cid and csec):
            raise AuthError("no_client", "client id/secret mancanti nel Keychain (servizio %s)" % PROBE_SERVICE)
        status, tok = post(endpoints["token"], {"client_id": cid, "client_secret": csec, "refresh_token": rt,
                                                "grant_type": "refresh_token"})
        if status != 200 or not isinstance(tok, dict) or "access_token" not in tok:
            err = tok.get("error") if isinstance(tok, dict) else None
            kind = "invalid_grant" if err == "invalid_grant" else ("invalid_client" if err == "invalid_client" else "token_error")
            hint = (" — il consenso e' scaduto o revocato: riesegui `authorize` (con l'app in 'Testing' scade dopo 7 giorni)"
                    if kind == "invalid_grant" else "")
            raise AuthError(kind, "rinnovo token non riuscito: %s%s" % (_oauth_error(status, tok), hint))
        if tok.get("refresh_token") and tok["refresh_token"] != rt:
            kc.set(TOKEN_SERVICE, "refresh_token", tok["refresh_token"])
        state["token"] = tok["access_token"]
        state["exp"] = clock() + float(tok.get("expires_in") or 300)
        provider.last_scope = tok.get("scope")
        return state["token"]

    provider.last_scope = None
    return provider


# ----------------------------------------------------------------- authorize
def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class _CB(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        p = urllib.parse.urlparse(self.path)
        if p.path != "/callback":
            self.send_response(404); self.end_headers(); return
        self.server.result = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
        body = b"<html><body style='font-family:-apple-system,sans-serif;padding:3em'><h2>Risposta ricevuta</h2><p>Puoi chiudere questa scheda e tornare al Terminale.</p></body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def resolve_scopes(names):
    bad = [n for n in names if n not in ALLOWED_SCOPES]
    if bad:
        raise AuthError("scope", "scope non ammesso/sconosciuto: %s (ammessi: %s)" % (", ".join(bad), ", ".join(ALLOWED_SCOPES)))
    return [ALLOWED_SCOPES[n] for n in names]


def _equivalents(scope):
    eq = {scope}
    for pair in SCOPE_ALIASES:
        if scope in pair:
            eq.update(pair)
    return eq


def compare_scopes(requested, granted):
    """-> (mancanti, extra). Uno scope richiesto e' soddisfatto se Google concede lui o un suo alias (SCOPE_ALIASES);
    uno scope concesso e' ammesso solo se e' (o e' alias di) uno scope richiesto. Nessun'altra tolleranza."""
    req, got = set(requested), set(granted)
    missing = sorted(s for s in req if not (_equivalents(s) & got))
    extra = sorted(g for g in got if not (_equivalents(g) & req))
    return missing, extra


def verify_coursework(access_token, transport=None, sleep=time.sleep):
    """Chiamata REALE (solo GET) a courseWork.list su un corso dello studente. -> (ok, messaggio). Mai solleva."""
    import cls_client as C
    client = C.ClassroomClient(lambda force_refresh=False: access_token, transport=transport, sleep=sleep, max_retries=2)
    try:
        courses = []
        for state in ("ACTIVE", "ARCHIVED"):
            courses = client._get("/courses", {"studentId": "me", "courseStates": state, "pageSize": 5}).get("courses") or []
            if courses:
                break
        if not courses:
            return False, "nessun corso trovato: impossibile verificare courseWork.list"
        cid = courses[0].get("id")
        if not cid:
            return False, "corso senza id: impossibile verificare courseWork.list"
        client._get("/courses/%s/courseWork" % urllib.parse.quote(str(cid), safe=""), {"pageSize": 1})
    except C.ClassroomApiError as e:
        return False, "courseWork.list fallita: HTTP %s %s%s" % (e.status, e.kind, " (%s)" % e.reason if e.reason else "")
    except Exception as e:      # noqa: BLE001  (rete ecc.: la verifica fallisce, non il programma)
        return False, "courseWork.list non eseguita: %s" % type(e).__name__
    return True, "courseWork.list OK"


def authorize(account, names=tuple(ALLOWED_SCOPES), keychain=None, out=print, open_url=None, timeout=180,
              post=_post, endpoints=ENDPOINTS, transport=None):
    """Consenso (browser) -> refresh token nel Keychain. Ritorna un dict d'esito; non stampa mai token/secret."""
    kc = keychain or MacKeychain()
    scopes = resolve_scopes(names)
    cid, csec = kc.get(PROBE_SERVICE, "client_id"), kc.get(PROBE_SERVICE, "client_secret")
    if not (cid and csec):
        out("ERRORE: client id/secret mancanti nel Keychain: esegui prima setup_keychain.sh del probe.")
        return {"ok": False, "outcome": "no_client"}
    try:
        keychain_preflight(kc)
    except AuthError as e:
        out("ERRORE (prima del consenso, nulla e' stato autorizzato): %s" % e)
        return {"ok": False, "outcome": "keychain_error"}
    out("Prova Keychain superata.")

    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _CB)
    srv.result, srv.timeout = None, 1
    redirect = "http://127.0.0.1:%d/callback" % srv.server_address[1]
    params = {"client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(scopes),
              "state": state, "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
              "login_hint": account, "access_type": "offline", "prompt": "consent"}
    url = endpoints["auth"] + "?" + urllib.parse.urlencode(params)
    out("Scope richiesti (solo lettura):")
    for s in scopes:
        out("  - " + s)
    opened = False
    try:
        opened = bool((open_url or webbrowser.open)(url))
    except Exception:      # noqa: BLE001
        pass
    if not opened:
        out("Apri manualmente questo indirizzo:\n" + url)
    out("In attesa del consenso (max %ds)..." % timeout)
    deadline = time.monotonic() + timeout
    try:
        while srv.result is None and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    cb = srv.result
    if cb is None:
        out("ESITO: nessuna risposta entro il tempo limite (se il browser ha mostrato 'Accesso bloccato', copia il testo esatto).")
        return {"ok": False, "outcome": "timeout"}
    if cb.get("state") != state:
        out("ESITO: 'state' non corrispondente: interrotto per sicurezza.")
        return {"ok": False, "outcome": "state_mismatch"}
    if "error" in cb:
        out("ESITO: consenso non concesso. error=%s%s" % (cb["error"], " · %s" % cb["error_description"] if cb.get("error_description") else ""))
        return {"ok": False, "outcome": "consent_error", "error": cb["error"]}
    status, tok = post(endpoints["token"], {"code": cb.get("code", ""), "client_id": cid, "client_secret": csec,
                                            "redirect_uri": redirect, "grant_type": "authorization_code",
                                            "code_verifier": verifier})
    if status != 200 or not isinstance(tok, dict) or "access_token" not in tok:
        out("ESITO: scambio del codice fallito: " + _oauth_error(status, tok))
        return {"ok": False, "outcome": "token_error"}
    granted = sorted(set((tok.get("scope") or "").split()))
    rt = tok.get("refresh_token")
    missing, extra = compare_scopes(scopes, granted)
    if missing or extra:
        out("ESITO: scope concessi diversi da quelli richiesti (concessi: %s; mancanti: %s; non previsti: %s). "
            "Non salvo nulla e revoco." % (", ".join(granted), ", ".join(missing) or "-", ", ".join(extra) or "-"))
        post(endpoints["revoke"], {"token": rt or tok["access_token"]})
        return {"ok": False, "outcome": "scope_mismatch", "granted": granted, "missing": missing, "extra": extra}
    aliased = sorted(set(scopes) - set(granted))
    if aliased:
        out("Nota: Google ha restituito un alias equivalente per: " + ", ".join(aliased))
    if not rt:
        out("ESITO: Google non ha restituito un refresh token: nulla salvato.")
        post(endpoints["revoke"], {"token": tok["access_token"]})
        return {"ok": False, "outcome": "no_refresh_token"}
    if ALLOWED_SCOPES["coursework"] in scopes:
        ok, msg = verify_coursework(tok["access_token"], transport=transport)
        out("Verifica reale: " + msg)
        if not ok:
            out("ESITO: verifica courseWork.list fallita: non salvo nulla e revoco.")
            post(endpoints["revoke"], {"token": rt})
            return {"ok": False, "outcome": "coursework_check_failed", "granted": granted, "detail": msg}
    try:
        kc.set(TOKEN_SERVICE, "refresh_token", rt)
    except AuthError as e:
        out("ESITO: salvataggio nel Keychain fallito (%s): revoco il token." % e)
        post(endpoints["revoke"], {"token": rt})
        return {"ok": False, "outcome": "keychain_error"}
    out("ESITO: AUTORIZZATO. Refresh token salvato nel Keychain (servizio %s). Nessun token mostrato o scritto su file." % TOKEN_SERVICE)
    return {"ok": True, "outcome": "authorized", "granted": granted}


def status(keychain=None, online=False, out=print, post=_post, endpoints=ENDPOINTS):
    kc = keychain or MacKeychain()
    have = {"client_id": bool(kc.get(PROBE_SERVICE, "client_id")), "client_secret": bool(kc.get(PROBE_SERVICE, "client_secret")),
            "refresh_token": bool(kc.get(TOKEN_SERVICE, "refresh_token"))}
    out("Keychain: " + " · ".join("%s %s" % (k, "OK" if v else "MANCANTE") for k, v in have.items()))
    res = {"keychain": have}
    if online and have["refresh_token"]:
        p = token_provider(kc, post=post, endpoints=endpoints)
        try:
            p(True)
            res["scope"] = sorted((p.last_scope or "").split())
            out("Rinnovo token: OK. Scope attivi:\n  " + "\n  ".join(res["scope"]))
        except AuthError as e:
            res["errore"] = {"kind": e.kind, "messaggio": str(e)}
            out("Rinnovo token: FALLITO — %s" % e)
    return res


def revoke(keychain=None, out=print, post=_post, endpoints=ENDPOINTS):
    kc = keychain or MacKeychain()
    rt = kc.get(TOKEN_SERVICE, "refresh_token")
    if not rt:
        out("Nessun refresh token nel Keychain: niente da revocare.")
        return {"ok": True, "revoked": False}
    st, _ = post(endpoints["revoke"], {"token": rt})
    deleted = kc.delete(TOKEN_SERVICE, "refresh_token")
    out("Revoca presso Google: %s · Keychain: %s" % ("OK" if st == 200 else "HTTP %s" % st, "cancellato" if deleted else "NON cancellato"))
    return {"ok": st == 200 and deleted, "revoked": st == 200, "deleted": deleted}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autenticazione Google Classroom (sola lettura)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("authorize")
    a.add_argument("--account", required=True)
    a.add_argument("--scopes", default=",".join(ALLOWED_SCOPES), help="sottoinsieme di: " + ",".join(ALLOWED_SCOPES))
    a.add_argument("--timeout", type=int, default=180)
    s = sub.add_parser("status")
    s.add_argument("--online", action="store_true", help="prova davvero il rinnovo del token")
    sub.add_parser("revoke")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "authorize":
            return 0 if authorize(args.account, [x for x in args.scopes.split(",") if x], timeout=args.timeout)["ok"] else 1
        if args.cmd == "status":
            status(online=args.online)
            return 0
        return 0 if revoke()["ok"] else 1
    except AuthError as e:
        print("ERRORE: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
