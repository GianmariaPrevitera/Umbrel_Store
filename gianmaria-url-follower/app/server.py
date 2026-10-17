#!/usr/bin/env python3
"""URL Follower - backend.

Serve la pagina statica (cartella www), l'endpoint GET /api/follow?url=...
(segue i reindirizzamenti di un link) e l'elenco degli URL monitorati,
salvato in DATA_DIR/urls.json.
Usa solo la libreria standard di Python: nessuna dipendenza da installare.
"""
import datetime
import http.client
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


# ---------------------------------------------------------------- URL monitorati

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "urls.json")
MAX_URLS = 100
MAX_BODY = 4096

db_lock = threading.Lock()
urls = []
persistent = True


def load_urls():
    global urls
    try:
        with open(DB_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        urls = [u for u in data if isinstance(u, dict) and u.get("id") and u.get("url")]
    except FileNotFoundError:
        urls = []
    except (OSError, ValueError) as exc:
        print("Impossibile leggere %s: %s" % (DB_FILE, exc), flush=True)
        urls = []


def save_urls():
    """Salva l'elenco su disco. Chiamare con db_lock già acquisito."""
    global persistent
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(urls, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, DB_FILE)
        persistent = True
    except OSError as exc:
        persistent = False
        print("Impossibile salvare %s: %s" % (DB_FILE, exc), flush=True)


def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise FollowError("invalid", "Incolla un indirizzo da monitorare, per esempio https://esempio.it.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise FollowError("scheme", "Sono supportati solo indirizzi che iniziano con http o https.")
    try:
        parts.port
    except ValueError:
        raise FollowError("invalid", "Indirizzo non valido: la porta indicata non è corretta.")
    if not parts.hostname:
        raise FollowError("invalid", "Indirizzo non valido. Incolla un link completo, per esempio https://esempio.it.")
    return raw


def compute_check(url):
    """Segue l'URL e restituisce i campi di stato aggiornati (None se il server è troppo occupato)."""
    if not slots.acquire(timeout=60):
        return None
    try:
        res = follow(url)
    finally:
        slots.release()
    last = res["hops"][-1] if res["hops"] else None
    return {
        "status": "online" if res["ok"] else "offline",
        "code": last["status"] if last else None,
        "hops": [h["status"] for h in res["hops"]],
        "final_url": res["final"],
        "error": res["error"]["message"] if res["error"] else None,
        "last_check": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def snapshot():
    with db_lock:
        return {"urls": [dict(u) for u in urls], "persistent": persistent}


# ---------------------------------------------------------------- server HTTP

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; script-src 'self'; frame-ancestors 'self'"
    ),
}


def error_payload(code, message):
    return {"hops": [], "error": {"code": code, "message": message}}


class Handler(BaseHTTPRequestHandler):
    server_version = "URLFollower/0.3"

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

    # ---- GET
    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/follow":
            return self.api_follow(parts)
        if parts.path == "/api/urls":
            return self._json(200, snapshot())
        if parts.path == "/api/health":
            return self._json(200, {"status": "ok"})
        return self.static(parts.path)

    def api_follow(self, parts):
        raw = parse_qs(parts.query).get("url", [""])[0].strip()
        if not raw or len(raw) > MAX_URL_LENGTH:
            return self._json(400, error_payload("invalid", "Incolla un indirizzo da seguire, per esempio https://bit.ly/esempio."))
        if not slots.acquire(blocking=False):
            return self._json(429, error_payload("busy", "Ci sono troppe richieste in corso. Riprova tra qualche secondo."))
        try:
            self._json(200, follow(raw))
        finally:
            slots.release()

    # ---- POST / DELETE (modificano i dati)
    def do_POST(self):
        self.mutate("POST")

    def do_DELETE(self):
        self.mutate("DELETE")

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise FollowError("invalid", "Richiesta troppo grande.")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise FollowError("invalid", "Richiesta non valida.")
        return data if isinstance(data, dict) else {}

    def mutate(self, method):
        # Header personalizzato: impedisce che altri siti, aperti nel tuo browser,
        # modifichino l'elenco (richiederebbe una richiesta preliminare che qui non è accettata).
        if self.headers.get("X-URL-Follower") != "1":
            return self._json(403, error_payload("forbidden", "Richiesta non consentita."))
        segs = [s for s in urlsplit(self.path).path.split("/") if s]
        try:
            if method == "POST" and segs == ["api", "urls"]:
                return self.add_url()
            if method == "POST" and segs == ["api", "check-all"]:
                return self.check_all()
            if method == "POST" and len(segs) == 4 and segs[:2] == ["api", "urls"] and segs[3] == "check":
                return self.check_one(segs[2])
            if method == "DELETE" and len(segs) == 3 and segs[:2] == ["api", "urls"]:
                return self.delete_url(segs[2])
        except FollowError as exc:
            return self._json(400, error_payload(exc.code, exc.message))
        return self._json(404, error_payload("not_found", "Operazione non trovata."))

    def add_url(self):
        url = normalize_url(self.read_json().get("url"))
        with db_lock:
            if any(u["url"] == url for u in urls):
                return self._json(409, error_payload("exists", "Questo indirizzo è già nell'elenco."))
            if len(urls) >= MAX_URLS:
                return self._json(400, error_payload("limit", "Hai raggiunto il limite di %d indirizzi: eliminane qualcuno." % MAX_URLS))
            item = {"id": secrets.token_hex(4), "url": url, "status": None, "code": None,
                    "hops": [], "final_url": None, "error": None, "last_check": None}
            urls.append(item)
            save_urls()
        fields = compute_check(url)
        with db_lock:
            if fields:
                item.update(fields)
                save_urls()
            return self._json(201, dict(item))

    def check_one(self, item_id):
        with db_lock:
            item = next((u for u in urls if u["id"] == item_id), None)
            url = item["url"] if item else None
        if not item:
            return self._json(404, error_payload("not_found", "Indirizzo non trovato: potrebbe essere già stato eliminato."))
        fields = compute_check(url)
        if fields is None:
            return self._json(429, error_payload("busy", "Ci sono troppe richieste in corso. Riprova tra qualche secondo."))
        with db_lock:
            item.update(fields)
            save_urls()
            return self._json(200, dict(item))

    def check_all(self):
        with db_lock:
            items = list(urls)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda it: compute_check(it["url"]), items))
        with db_lock:
            for item, fields in zip(items, results):
                if fields:
                    item.update(fields)
            save_urls()
        return self._json(200, snapshot())

    def delete_url(self, item_id):
        with db_lock:
            before = len(urls)
            urls[:] = [u for u in urls if u["id"] != item_id]
            if len(urls) == before:
                return self._json(404, error_payload("not_found", "Indirizzo non trovato: potrebbe essere già stato eliminato."))
            save_urls()
        return self._json(200, {"ok": True})

    # ---- file statici
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
    load_urls()
    print("URL Follower in ascolto sulla porta %d (www: %s, dati: %s)" % (PORT, WWW_DIR, DATA_DIR), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
