"""Test del probe contro un 'Google finto' locale (nessuna rete reale, nessun account)."""
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import classroom_probe as cp  # noqa: E402

AT = "AT-SECRET-123"
RT = "RT-SECRET-456"
CODE = "AUTHCODE-789"
CSEC = "CLIENT-SECRET-XYZ"
CID = "123.apps.googleusercontent.com"
ACCOUNT = "studente.test@scuola.example"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        S = self.server
        S.requests.append(("POST", self.path, form))
        if self.path == "/token":
            if S.token_error:
                return self._send(400, S.token_error)
            ok = (form.get("code") == CODE and form.get("client_secret") == CSEC
                  and cp.pkce_challenge(form.get("code_verifier", "")) == S.challenge
                  and form.get("redirect_uri") == S.redirect_uri)
            if not ok:
                return self._send(400, {"error": "invalid_grant", "error_description": "Bad Request"})
            tok = {"access_token": AT, "expires_in": 3599, "token_type": "Bearer", "scope": S.scope}
            if S.with_refresh:
                tok["refresh_token"] = RT
            return self._send(200, tok)
        if self.path == "/revoke":
            return self._send(S.revoke_status, {})
        self._send(404, {})

    def do_GET(self):  # noqa: N802
        S = self.server
        S.requests.append(("GET", self.path, {"auth": self.headers.get("Authorization")}))
        u = urllib.parse.urlparse(self.path)
        if u.path != "/courses":
            return self._send(404, {})
        if self.headers.get("Authorization") != f"Bearer {AT}":
            return self._send(401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "no"}})
        if S.api_error:
            return self._send(*S.api_error)
        tok = urllib.parse.parse_qs(u.query).get("pageToken", [None])[0]
        return self._send(200, S.pages[tok])


class MockGoogle:
    def __init__(self, **kw):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Mock)
        s = self.srv
        s.requests, s.challenge, s.redirect_uri = [], None, None
        s.scope = kw.get("scope", cp.SCOPE)
        s.token_error = kw.get("token_error")
        s.api_error = kw.get("api_error")
        s.with_refresh = kw.get("with_refresh", False)
        s.revoke_status = kw.get("revoke_status", 200)
        s.pages = kw.get("pages", {None: {"courses": [
            {"id": "1", "name": "Matematica 3A", "section": "A", "courseState": "ACTIVE", "ownerId": "SECRET-OWNER"},
            {"id": "2", "name": "Storia", "courseState": "ACTIVE"}], "nextPageToken": "P2"},
            "P2": {"courses": [{"id": "3", "name": "Inglese", "section": "B", "courseState": "ARCHIVED"}]}})
        self.base = f"http://127.0.0.1:{s.server_address[1]}"
        self.endpoints = cp.Endpoints(auth=self.base + "/auth", token=self.base + "/token",
                                      revoke=self.base + "/revoke", courses=self.base + "/courses")
        self.t = threading.Thread(target=s.serve_forever, daemon=True)
        self.t.start()
        self.auth_params = None

    @property
    def requests(self):
        return self.srv.requests

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def browser(self, mode="ok", pre=None):
        """Finto browser: riceve l'URL di autorizzazione e 'torna' sul redirect."""
        def open_url(url):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            self.auth_params = q
            self.srv.challenge = q["code_challenge"]
            self.srv.redirect_uri = q["redirect_uri"]
            if mode == "none":
                return True

            def go():
                if pre:
                    try:
                        _NOPROXY.open(q["redirect_uri"].replace("/callback", pre), timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
                state = "WRONG" if mode == "badstate" else q["state"]
                if mode == "denied":
                    extra = {"error": "access_denied"}
                elif mode == "admin":
                    extra = {"error": "admin_policy_enforced"}
                else:
                    extra = {"code": CODE}
                extra["state"] = state
                _NOPROXY.open(q["redirect_uri"] + "?" + urllib.parse.urlencode(extra), timeout=5).read()
            threading.Thread(target=go, daemon=True).start()
            return True
        return open_url


class ProbeTests(unittest.TestCase):
    def go(self, mode="ok", mock_kw=None, timeout=10, pre=None):
        m = MockGoogle(**(mock_kw or {}))
        self.addCleanup(m.close)
        lines = []
        res = cp.run(CID, CSEC, ACCOUNT, out=lines.append, open_url=m.browser(mode, pre),
                     timeout=timeout, endpoints=m.endpoints)
        return m, res, "\n".join(lines)

    def posts(self, m, path):
        return [r for r in m.requests if r[0] == "POST" and r[1] == path]

    # ---- successo
    def test_success_paginated(self):
        m, res, out = self.go()
        self.assertTrue(res["ok"])
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual([c["id"] for c in res["courses"]], ["1", "2", "3"])
        self.assertIn("Matematica 3A", out)
        self.assertIn("Corsi restituiti: 3", out)
        self.assertEqual(len(self.posts(m, "/revoke")), 1)
        self.assertTrue(res["revoked"])

    def test_only_selected_fields_are_kept(self):
        _, res, out = self.go()
        self.assertNotIn("SECRET-OWNER", out)
        self.assertNotIn("ownerId", json.dumps(res))

    def test_empty_course_list_is_valid_success(self):
        _, res, out = self.go(mock_kw={"pages": {None: {}}})
        self.assertTrue(res["ok"])
        self.assertEqual(res["courses"], [])
        self.assertIn("Corsi restituiti: 0", out)

    def test_auth_url_is_minimal_and_pkce(self):
        m, res, _ = self.go()
        q = m.auth_params
        self.assertEqual(q["scope"], cp.SCOPE)
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["login_hint"], ACCOUNT)
        self.assertTrue(q["redirect_uri"].startswith("http://127.0.0.1:"))
        for forbidden in ("access_type", "include_granted_scopes", "prompt"):
            self.assertNotIn(forbidden, q)

    def test_only_get_on_classroom_api_and_no_other_endpoints(self):
        m, _, _ = self.go()
        api = [r for r in m.requests if r[1].startswith("/courses")]
        self.assertTrue(api and all(r[0] == "GET" for r in api))
        self.assertEqual({r[1].split("?")[0] for r in m.requests}, {"/token", "/revoke", "/courses"})

    def test_no_secrets_in_output_and_no_files_written(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                _, res, out = self.go(mock_kw={"with_refresh": True})
                left = os.listdir(d)
            finally:
                os.chdir(old)
        for s in (AT, RT, CODE, CSEC):
            self.assertNotIn(s, out)
            self.assertNotIn(s, json.dumps(res))
        self.assertEqual(left, [])

    def test_refresh_token_if_returned_is_revoked_not_kept(self):
        m, res, _ = self.go(mock_kw={"with_refresh": True})
        self.assertEqual(self.posts(m, "/revoke")[0][2]["token"], RT)
        self.assertNotIn("refresh_token", json.dumps(res))

    def test_favicon_request_is_ignored(self):
        _, res, _ = self.go(pre="/favicon.ico")
        self.assertTrue(res["ok"])

    # ---- fallimenti
    def test_consent_denied(self):
        m, res, out = self.go("denied")
        self.assertEqual(res["outcome"], "consent_error")
        self.assertEqual(res["error"], "access_denied")
        self.assertIn("error=access_denied", out)
        self.assertEqual(self.posts(m, "/token"), [])

    def test_admin_policy_enforced(self):
        m, res, out = self.go("admin")
        self.assertEqual(res["error"], "admin_policy_enforced")
        self.assertIn("admin_policy_enforced", out)
        self.assertIn("probabile", out.lower())
        self.assertEqual(self.posts(m, "/token"), [])

    def test_state_mismatch_stops_before_token_exchange(self):
        m, res, _ = self.go("badstate")
        self.assertEqual(res["outcome"], "state_mismatch")
        self.assertEqual(self.posts(m, "/token"), [])

    def test_token_error_reported_exactly(self):
        m, res, out = self.go(mock_kw={"token_error": {"error": "invalid_client", "error_description": "Unauthorized"}})
        self.assertEqual(res["outcome"], "token_error")
        self.assertIn("error=invalid_client", out)
        self.assertIn("error_description=Unauthorized", out)
        self.assertFalse([r for r in m.requests if r[1].startswith("/courses")])

    def test_api_not_enabled_403_and_token_still_revoked(self):
        err = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                               "message": "Google Classroom API has not been used in project 1 before or it is disabled.",
                               "details": [{"reason": "SERVICE_DISABLED"}]}})
        m, res, out = self.go(mock_kw={"api_error": err})
        self.assertEqual(res["outcome"], "api_error")
        self.assertEqual(res["status"], 403)
        self.assertIn("PERMISSION_DENIED", out)
        self.assertIn("SERVICE_DISABLED", out)
        self.assertEqual(len(self.posts(m, "/revoke")), 1)

    def test_extra_scope_granted_stops_and_revokes(self):
        extra = cp.SCOPE + " https://www.googleapis.com/auth/drive.readonly"
        m, res, out = self.go(mock_kw={"scope": extra})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertFalse([r for r in m.requests if r[1].startswith("/courses")])
        self.assertEqual(len(self.posts(m, "/revoke")), 1)

    def test_timeout(self):
        m, res, out = self.go("none", timeout=1)
        self.assertEqual(res["outcome"], "timeout")
        self.assertEqual(self.posts(m, "/token"), [])
        self.assertIn("Accesso bloccato", out)

    def test_revoke_failure_does_not_change_outcome_but_is_reported(self):
        _, res, out = self.go(mock_kw={"revoke_status": 400})
        self.assertTrue(res["ok"])
        self.assertFalse(res["revoked"])
        self.assertIn("revoca token non confermata", out)

    # ---- proprieta' statiche
    def test_pkce_rfc7636_vector(self):
        self.assertEqual(cp.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_verifier_length_in_rfc_range(self):
        self.assertTrue(43 <= len(cp.new_verifier()) <= 128)

    def test_single_scope_in_source(self):
        here = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(here, "classroom_probe.py")).read()
        self.assertEqual(cp.SCOPE, "https://www.googleapis.com/auth/classroom.courses.readonly")
        self.assertEqual(len(re.findall(r"googleapis\.com/auth/", src)), 1)
        self.assertNotIn("auth/drive", src)  # nessuno scope Drive
        self.assertEqual(re.findall(r"auth/classroom[\w.]*", src), ["auth/classroom.courses.readonly"])
        self.assertNotIn("\"access_type\"", src)

    def test_setup_script_is_valid_and_does_not_leak_secret(self):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "setup_keychain.sh")
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        src = open(path).read()
        self.assertIn("-s \"$SERVICE\" -a client_secret", src)
        self.assertNotRegex(src, r"client_secret[^\n]*-w\s+\"?\$")  # il secret non passa da argv
        self.assertNotIn("echo \"$SECRET", src)


if __name__ == "__main__":
    unittest.main()
