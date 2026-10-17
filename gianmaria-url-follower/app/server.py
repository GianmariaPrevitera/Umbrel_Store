#!/usr/bin/env python3
"""URL Follower - backend.

Serve la pagina statica (cartella www) e l'endpoint  GET /api/follow?url=...
che segue i reindirizzamenti di un link e restituisce ogni passaggio.
Usa solo la libreria standard di Python: nessuna dipendenza da installare.
"""
import http.client
import ipaddress
import json
import mimetypes
import os
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

PORT = int(os.environ.get("PORT", "8080"))
WWW_DIR = os.path.realpath(os.environ.get("WWW_DIR", "/www"))
# Solo per i test in locale: consente indirizzi privati/loopback.
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"

MAX_HOPS = 10          # passaggi massimi per ogni richiesta
HOP_TIMEOUT = 8        # secondi per ogni passaggio
TOTAL_TIMEOUT = 25     # secondi per l'intero percorso
MAX_URL_LENGTH = 2048
REDIRECT_CODES = (301, 302, 303, 307, 308)
USER_AGENT = "URL-Follower/0.2 (+https://github.com/GianmariaPrevitera/Umbrel_Store)"

# Al massimo 8 percorsi seguiti contemporaneamente.
slots = threading.BoundedSemaphore(8)


class FollowError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_public(host, port):
    """Risolve il nome e restituisce un IP pubblico; rifiuta reti private/locali.

    L'IP restituito viene poi usato per la connessione, così il controllo
    non può essere aggirato cambiando la risposta DNS tra una richiesta e l'altra.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise FollowError(
            "dns",
            "Impossibile trovare il sito: il nome non esiste o non è raggiungibile. "
            "Controlla l'indirizzo e riprova.",
        )
    chosen = None
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ALLOW_PRIVATE and not ip.is_global:
            raise FollowError(
                "blocked",
                "Indirizzo non consentito: punta a una rete privata o locale. "
                "URL Follower segue solo indirizzi pubblici di Internet.",
            )
        if chosen is None:
            chosen = sockaddr[0]
    return chosen


def open_connection(scheme, host, port, ip):
    """Connessione HTTP(S) diretta all'IP già verificato, con nome host corretto."""
    if scheme == "https":
        ctx = ssl.create_default_context()

        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                sock = socket.create_connection((ip, self.port), self.timeout)
                self.sock = ctx.wrap_socket(sock, server_hostname=self.host)

        return PinnedHTTPS(host, port, timeout=HOP_TIMEOUT, context=ctx)

    class PinnedHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((ip, self.port), self.timeout)

    return PinnedHTTP(host, port, timeout=HOP_TIMEOUT)


def fetch_status(url):
    """Esegue una GET senza leggere il corpo; restituisce (status, reason, location)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FollowError("scheme", "Sono supportati solo indirizzi che iniziano con http o https.")
    host = parts.hostname
    if not host:
        raise FollowError("invalid", "Indirizzo non valido. Incolla un link completo, per esempio https://esempio.it.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise FollowError("invalid", "Indirizzo non valido: la porta indicata non è corretta.")

    ip = resolve_public(host, port)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    conn = open_connection(parts.scheme, host, port, ip)
    try:
        conn.request(
            "GET",
            path,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"},
        )
        resp = conn.getresponse()
        return resp.status, resp.reason, resp.getheader("Location")
    except ssl.SSLError:
        raise FollowError("tls", "Il certificato di sicurezza del sito non è valido, quindi non è stato seguito.")
    except TimeoutError:
        raise FollowError("timeout", "Il sito non ha risposto entro %d secondi. Riprova più tardi." % HOP_TIMEOUT)
    except (OSError, http.client.HTTPException):
        raise FollowError("connect", "Impossibile collegarsi al sito. Controlla l'indirizzo e riprova.")
    finally:
        conn.close()


def follow(raw):
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    started = time.monotonic()
    hops, seen, error = [], set(), None

    try:
        while True:
            if len(hops) >= MAX_HOPS:
                raise FollowError("too_many", "Troppi passaggi (oltre %d): il percorso potrebbe essere un ciclo." % MAX_HOPS)
            if time.monotonic() - started > TOTAL_TIMEOUT:
                raise FollowError("timeout", "Il percorso richiede troppo tempo. Riprova più tardi.")
            key = url.split("#", 1)[0]
            if key in seen:
                raise FollowError("loop", "Il link torna a un indirizzo già visitato: è un ciclo di reindirizzamenti.")
            seen.add(key)

            status, reason, location = fetch_status(url)
            hops.append({"n": len(hops) + 1, "url": url, "status": status, "reason": reason})
            if status in REDIRECT_CODES and location:
                url = urljoin(url, location)
                continue
            break
    except FollowError as exc:
        error = {"code": exc.code, "message": exc.message}

    last = hops[-1] if hops else None
    return {
        "input": raw,
        "hops": hops,
        "final": last["url"] if (last and not error) else None,
        "ok": bool(last and not error and last["status"] < 400),
        "error": error,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; script-src 'self'; frame-ancestors 'self'"
    ),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "URLFollower/0.2"

    def _send(self, code, body, content_type, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/follow":
            return self.api_follow(parts)
        if parts.path == "/api/health":
            return self._json(200, {"status": "ok"})
        return self.static(parts.path)

    def api_follow(self, parts):
        raw = parse_qs(parts.query).get("url", [""])[0].strip()
        if not raw or len(raw) > MAX_URL_LENGTH:
            return self._json(400, {"hops": [], "error": {
                "code": "invalid",
                "message": "Incolla un indirizzo da seguire, per esempio https://bit.ly/esempio."}})
        if not slots.acquire(blocking=False):
            return self._json(429, {"hops": [], "error": {
                "code": "busy",
                "message": "Ci sono troppe richieste in corso. Riprova tra qualche secondo."}})
        try:
            self._json(200, follow(raw))
        finally:
            slots.release()

    def static(self, path):
        rel = unquote(path).lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        full = os.path.realpath(os.path.join(WWW_DIR, rel))
        if not full.startswith(WWW_DIR + os.sep) or not os.path.isfile(full):
            return self._send(404, "Pagina non trovata".encode("utf-8"), "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as handle:
            self._send(200, handle.read(), ctype)


if __name__ == "__main__":
    print("URL Follower in ascolto sulla porta %d (www: %s)" % (PORT, WWW_DIR), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
