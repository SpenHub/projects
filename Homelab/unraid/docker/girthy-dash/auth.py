"""Who may change things (delete releases, start scans).

Reads are open. Writes are allowed when either:
  * the request comes straight from a trusted network (LAN / Tailscale) and did not
    pass through Cloudflare, or
  * it carries a valid Cloudflare Access JWT (RS256, verified against the team's
    published keys, with issuer / expiry / optional audience checks).
Every write must also send `X-Girthy-Dash: 1`; browsers can't add custom headers
cross-site without a CORS preflight we never answer, which blocks CSRF.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("girthy-dash.auth")

SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class AccessVerifier:
    def __init__(self, team, audience=""):
        self.team = team
        self.issuer = f"https://{team}.cloudflareaccess.com" if team else ""
        self.audience = audience
        self.keys = {}
        self.fetched = 0
        self.lock = threading.Lock()

    def _keys(self, force=False):
        with self.lock:
            if force or time.time() - self.fetched > 3600:
                url = f"{self.issuer}/cdn-cgi/access/certs"
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = json.load(r)
                keys = {}
                for k in data.get("keys", []):
                    if k.get("kty") == "RSA" and k.get("kid"):
                        keys[k["kid"]] = (int.from_bytes(b64d(k["n"]), "big"), int.from_bytes(b64d(k["e"]), "big"))
                self.keys, self.fetched = keys, time.time()
            return self.keys

    def verify(self, token):
        """Returns the authenticated email, or raises ValueError."""
        if not self.team:
            raise ValueError("Cloudflare Access team is not configured")
        try:
            h64, p64, s64 = token.split(".")
            header, payload = json.loads(b64d(h64)), json.loads(b64d(p64))
        except Exception:
            raise ValueError("malformed Access token") from None
        if header.get("alg") != "RS256":
            raise ValueError("unexpected token algorithm")
        key = self._keys().get(header.get("kid")) or self._keys(force=True).get(header.get("kid"))
        if not key:
            raise ValueError("unknown signing key")
        n, e = key
        k = (n.bit_length() + 7) // 8
        em = pow(int.from_bytes(b64d(s64), "big"), e, n).to_bytes(k, "big")
        digest = hashlib.sha256(f"{h64}.{p64}".encode()).digest()
        pad = k - 3 - len(SHA256_DIGEST_INFO) - len(digest)
        expected = b"\x00\x01" + b"\xff" * pad + b"\x00" + SHA256_DIGEST_INFO + digest
        if pad < 8 or not hmac.compare_digest(em, expected):
            raise ValueError("bad token signature")
        now = time.time()
        if payload.get("exp", 0) < now - 30 or payload.get("nbf", 0) > now + 30:
            raise ValueError("token expired")
        if payload.get("iss") != self.issuer:
            raise ValueError("wrong token issuer")
        if self.audience:
            aud = payload.get("aud")
            aud = aud if isinstance(aud, list) else [aud]
            if self.audience not in aud:
                raise ValueError("token is for a different Access application")
        return payload.get("email") or payload.get("common_name") or "service token"


class Guard:
    def __init__(self, trusted_nets, team, audience=""):
        self.trusted = [ipaddress.ip_network(n.strip()) for n in trusted_nets.split(",") if n.strip()]
        self.access = AccessVerifier(team, audience)

    def check(self, handler, require_header=True):
        """Returns (allowed, who_or_reason)."""
        if require_header and handler.headers.get("X-Girthy-Dash") != "1":
            return False, "missing X-Girthy-Dash header"
        jwt = handler.headers.get("Cf-Access-Jwt-Assertion")
        if jwt:
            try:
                return True, self.access.verify(jwt)
            except Exception as e:
                log.warning("rejected Access token: %s", e)
                return False, f"Cloudflare Access token rejected: {e}"
        via_cloudflare = bool(handler.headers.get("Cf-Ray") or handler.headers.get("Cf-Connecting-Ip"))
        try:
            ip = ipaddress.ip_address(handler.client_address[0])
        except ValueError:
            return False, "unknown client address"
        if not via_cloudflare and any(ip in n for n in self.trusted):
            return True, f"LAN ({ip})"
        if via_cloudflare:
            return False, "Changes over the internet need Cloudflare Access in front of this site."
        return False, f"Changes aren't allowed from {ip}."
