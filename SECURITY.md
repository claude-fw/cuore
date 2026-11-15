# Security policy

CUORE handles school records and bank data, so security reports are welcome.

**Please do not open a public issue for a vulnerability.** Use GitHub's private reporting instead:
the repository's **Security** tab → **Report a vulnerability**.

Useful to include: the affected file or flow, steps to reproduce, and the impact you expect.
This is a personal project maintained in spare time: expect a reply within a few days, not hours.

## Scope and design guarantees (what a report can challenge)
- Every request to the Worker must carry a valid Cloudflare Access token for the configured team and application, and
  the Worker checks the e-mail again against `ACCESS_ALLOWED_EMAILS` (two barriers). Missing configuration = 503.
- The Mac talks to the cloud only with Access service tokens, each limited to its own path (school feed, backups).
- School credentials (ClasseViva, Google) never leave the Mac's Keychain; only a derived feed is sent.
- Bank access is read-only (PSD2 through Enable Banking); bank keys live only in Worker secrets.
- Backups are encrypted (RSA-OAEP + AES-GCM) for the Mac key and an offline recovery key, and signed (ECDSA): the cloud
  cannot read them.
- Writes that change data or configuration from the assistant are proposals until the owner approves them.

If you deploy your own copy, you are responsible for your Cloudflare, Google and bank configuration and secrets.
