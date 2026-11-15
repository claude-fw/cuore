"""Ponte CUORE: accesso dalla rete privata tramite proxy locale (es. `tailscale serve`), spento per default.
Richieste simulate su 127.0.0.1 con le intestazioni che il proxy aggiungerebbe; nessuna rete reale, nessun dato reale."""
import http.client, json, os, sys, threading, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
import serve  # noqa: E402

TAILNET = "mac.esempio.ts.net"
OWNER = "titolare@esempio.it"


class AccessoPrivato(unittest.TestCase):
    def start(self, **kw):
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = serve.make_handler(port, feed_fn=lambda: {"schema": "s"},
                                                       money_fn=lambda: {"schema": "m"}, **kw)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertEqual(httpd.server_address[0], "127.0.0.1")              # sempre e solo loopback
        return port

    def get(self, port, path="/api/money", host=None, login=None, method="GET", extra=None):
        c = http.client.HTTPConnection(serve.BIND, port, timeout=10)
        h = {"Host": host or "127.0.0.1:%d" % port, **(extra or {})}
        if login is not None:
            h[serve.IDENTITY_HEADER] = login
        c.request(method, path, headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_spento_per_default(self):
        port = self.start()
        self.assertEqual(self.get(port), 200)                                # Mac: invariato
        self.assertEqual(self.get(port, host=TAILNET, login=OWNER), 403)     # rete privata: nessun accesso
        self.assertEqual(self.get(port, login=OWNER), 403)                   # intestazione d'identita' senza configurazione

    def test_utente_autorizzato(self):
        port = self.start(tailnet_host=TAILNET, tailnet_users=[OWNER])
        for path in ("/", "/api/school", "/api/money"):
            self.assertEqual(self.get(port, path, host=TAILNET, login=OWNER), 200, path)
        self.assertEqual(self.get(port, host=TAILNET + ":443", login=OWNER.upper()), 200)
        self.assertEqual(self.get(port), 200)                                # il Mac resta ammesso

    def test_utente_non_autorizzato_o_assente(self):
        port = self.start(tailnet_host=TAILNET, tailnet_users=[OWNER])
        self.assertEqual(self.get(port, host=TAILNET, login="altro@esempio.it"), 403)
        self.assertEqual(self.get(port, host=TAILNET), 403)                  # dispositivo senza identita' (es. con tag)
        self.assertEqual(self.get(port, login="altro@esempio.it"), 403)      # identita' estranea anche su host locale
        self.assertEqual(self.get(port, host="altro.esempio.ts.net", login=OWNER), 403)
        self.assertEqual(self.get(port, host="evil.example", login=OWNER), 403)

    def test_solo_lettura_anche_dalla_rete_privata(self):
        port = self.start(tailnet_host=TAILNET, tailnet_users=[OWNER])
        for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.get(port, host=TAILNET, login=OWNER, method=m), 405, m)

    def test_configurazione_senza_utenti_rifiutata(self):
        with self.assertRaises(ValueError):
            serve.make_handler(8765, feed_fn=lambda: {}, money_fn=lambda: {}, tailnet_host=TAILNET, tailnet_users=[])

    def test_richieste_cross_site_rifiutate(self):
        port = self.start(tailnet_host=TAILNET, tailnet_users=[OWNER])
        own, tail = {"host": None}, {"host": TAILNET, "login": OWNER}
        for where in (own, tail):
            for path in ("/", "/api/money", "/api/school"):
                self.assertEqual(self.get(port, path, extra={"Sec-Fetch-Site": "cross-site"}, **where), 403, (where, path))
                self.assertEqual(self.get(port, path, extra={"Sec-Fetch-Site": "same-site"}, **where), 403, (where, path))
                self.assertEqual(self.get(port, path, extra={"Origin": "https://evil.example"}, **where), 403, (where, path))
        ok = {"Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.get(port, extra={**ok, "Origin": "http://127.0.0.1:%d" % port}), 200)
        self.assertEqual(self.get(port, host=TAILNET, login=OWNER, extra={**ok, "Origin": "https://" + TAILNET}), 200)
        self.assertEqual(self.get(port, "/", extra={"Sec-Fetch-Site": "none"}), 200)       # indirizzo digitato o segnalibro
        self.assertEqual(self.get(port, extra={"Origin": "https://" + TAILNET}), 200)       # origine propria, se configurata

    def test_origine_della_rete_privata_solo_se_configurata(self):
        port = self.start()
        self.assertEqual(self.get(port, extra={"Origin": "https://" + TAILNET}), 403)

    def test_intestazioni_di_sicurezza(self):
        port = self.start(tailnet_host=TAILNET, tailnet_users=[OWNER])
        c = http.client.HTTPConnection(serve.BIND, port, timeout=10)
        c.request("GET", "/api/money", headers={"Host": TAILNET, serve.IDENTITY_HEADER: OWNER, "Origin": "https://evil.example"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))      # nessuna lettura cross-site
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))


if __name__ == "__main__":
    unittest.main()
