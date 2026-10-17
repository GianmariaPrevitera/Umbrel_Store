#!/usr/bin/env python3
"""URL Follower - monitor di URL con pannello amministrativo.

Pagine e API:
  GET  /                    pannello amministrativo (login con la password
                            mostrata da Umbrel tra le credenziali dell'app)
  GET  /api=<chiave>        testo semplice: gli URL online, uno per riga.
                            Chiave non valida o utente non disponibile:
                            pagina web vuota, sempre con risposta 200.
  /api/...                  API JSON usate dal pannello (solo amministratori)

Controllo periodico: un thread interno controlla tutti gli URL ogni
CHECK_INTERVAL_MINUTES minuti (0 = disattivato). In alternativa:
  python app.py --check-urls        controlla subito tutti gli URL
  python app.py --show-admin-keys   mostra le chiavi degli amministratori

Usa solo la libreria standard di Python: nessuna dipendenza da installare.
"""
import base64
import contextlib
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urljoin, urlsplit

# ============================================================
# CONFIGURAZIONE
# ============================================================

PORT = int(os.environ.get("PORT", "8080"))
WWW_DIR = os.path.realpath(os.environ.get("WWW_DIR", "/www"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DATABASE = os.path.join(DATA_DIR, "app.db")

# Account principale. La sua password è APP_PASSWORD, la password che Umbrel
# mostra tra le credenziali dell'app (deterministicPassword nel manifest).
ADMIN_USERNAME = os.environ.get("INITIAL_USERNAME", "Admin")
ADMIN_PASSWORD = os.environ.get("APP_PASSWORD", "")

try:
    CHECK_INTERVAL = float(os.environ.get("CHECK_INTERVAL_MINUTES", "10")) * 60
except ValueError:
    CHECK_INTERVAL = 600.0

# SOLO PER I TEST AUTOMATICI: consente indirizzi privati e porte qualsiasi.
# Non va mai impostata in produzione.
TEST_MODE = os.environ.get("URLF_TEST_MODE") == "1"

# Alfabeto senza caratteri ambigui (I, O, l, 0, 1), come nel vecchio progetto.
API_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
API_KEY_LENGTH = 9
KEY_FORMAT = re.compile(r"[A-Za-z0-9]{9}")

URL_CHECK_TIMEOUT = 10
USER_AGENT = "URL-Follower/1.0"
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
ALLOWED_URL_PORTS = {80, 443}

MAX_BODY = 4096
SESSION_SECONDS = 12 * 3600
SESSION_SECRET = b""  # impostato all'avvio

# Al massimo 8 controlli in uscita contemporanei.
check_slots = threading.BoundedSemaphore(8)


class BusyError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ============================================================
# PROTEZIONE SSRF
# ============================================================

def is_safe_ip(ip_string):
    """True solo per indirizzi IP globalmente instradabili.

    Bloccati: loopback, privati, link-local, multicast, riservati,
    non specificati e altre reti non globali.
    """
    if TEST_MODE:
        return True
    try:
        ip = ipaddress.ip_address(str(ip_string).split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global da solo lascia passare gli indirizzi multicast (es. 224.0.0.1).
    return ip.is_global and not ip.is_multicast


def resolve_hostname(hostname, port):
    """Risolve il nome e verifica TUTTI gli indirizzi restituiti.

    Restituisce l'IP da usare per la connessione, oppure None se anche un
    solo indirizzo non è globalmente instradabile.
    """
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return None
    if not results:
        return None
    addresses = []
    for result in results:
        sockaddr = result[4]
        if not sockaddr or not is_safe_ip(sockaddr[0]):
            return None
        addresses.append((result[0], sockaddr[0]))
    for family, address in addresses:
        if family == socket.AF_INET:
            return address
    return addresses[0][1]


def inspect_url(url):
    """Valida un URL e restituisce (schema, host, porta, ip, percorso), o None.

    Controlla: tipo, lunghezza, caratteri di controllo, schema http/https,
    host presente, porte 80/443, indirizzi IP e nomi che risolvono verso
    reti non pubbliche.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        return None
    if re.search(r"[\s\x00-\x1f\x7f]", url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if not TEST_MODE and port not in ALLOWED_URL_PORTS:
        return None

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not is_safe_ip(str(literal)):
            return None
        ip = str(literal)
    else:
        if not TEST_MODE and host.rstrip(".").lower() in ("localhost", "localhost.localdomain"):
            return None
        ip = resolve_hostname(host, port)
        if ip is None:
            return None

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    path = quote(path, safe="/?&=%:@!$'()*+,;-._~")
    return parts.scheme, host, port, ip, path


def validate_monitor_url(url):
    return inspect_url(url) is not None


def open_connection(scheme, host, port, ip):
    """Connessione diretta all'IP già verificato, con il nome host corretto.

    Così la risposta DNS non può cambiare tra il controllo e la richiesta
    (DNS rebinding).
    """
    if scheme == "https":
        ctx = ssl.create_default_context()

        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                sock = socket.create_connection((ip, self.port), self.timeout)
                self.sock = ctx.wrap_socket(sock, server_hostname=self.host)

        return PinnedHTTPS(host, port, timeout=URL_CHECK_TIMEOUT, context=ctx)

    class PinnedHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((ip, self.port), self.timeout)

    return PinnedHTTP(host, port, timeout=URL_CHECK_TIMEOUT)


def request_status(info):
    """GET senza leggere il corpo: restituisce (status, intestazione Location)."""
    scheme, host, port, ip, path = info
    conn = open_connection(scheme, host, port, ip)
    try:
        conn.request("GET", path, headers={
            "User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
        response = conn.getresponse()
        return response.status, response.getheader("Location")
    finally:
        conn.close()


# ============================================================
# CONTROLLO DEGLI URL
# ============================================================

def check_url_status(url):
    """Controlla un URL applicando la protezione SSRF a ogni reindirizzamento.

    I reindirizzamenti sono gestiti a mano: ogni destinazione viene validata
    prima della richiesta successiva. Restituisce (stato, url_finale), con
    stato 1 = online, 0 = offline. Una destinazione rifiutata dalla
    protezione SSRF non viene mai adottata come URL finale.
    """
    current_url = url
    last_valid = url
    for _ in range(MAX_REDIRECTS + 1):
        info = inspect_url(current_url)
        if info is None:
            return 0, last_valid
        last_valid = current_url
        try:
            status, location = request_status(info)
        except Exception:
            return 0, current_url

        if status in (301, 302, 303, 307, 308):
            if not location:
                return 0, current_url
            try:
                current_url = urljoin(current_url, location)
            except ValueError:
                return 0, current_url
            continue

        return (1 if 200 <= status < 400 else 0), current_url

    # Troppi reindirizzamenti.
    return 0, last_valid


def checked(url):
    """check_url_status con limite ai controlli contemporanei."""
    if not check_slots.acquire(timeout=60):
        raise BusyError()
    try:
        return check_url_status(url)
    finally:
        check_slots.release()


def update_url_result(url_id, original_url, stato, final_url, last_check):
    """Salva il risultato. Se l'URL è stato spostato lo aggiorna alla destinazione."""
    with db() as conn:
        if final_url and final_url != original_url:
            exists = conn.execute("SELECT 1 FROM urls WHERE url = ?", (final_url,)).fetchone()
            if exists is None:
                try:
                    conn.execute(
                        "UPDATE urls SET url = ?, stato = ?, last_check = ? WHERE id = ?",
                        (final_url, stato, last_check, url_id))
                    return final_url
                except sqlite3.IntegrityError:
                    pass
        conn.execute("UPDATE urls SET stato = ?, last_check = ? WHERE id = ?",
                     (stato, last_check, url_id))
        return original_url


def check_single_url(url_id):
    with db() as conn:
        row = conn.execute("SELECT url FROM urls WHERE id = ?", (url_id,)).fetchone()
    if row is None:
        return None
    original = row["url"]
    stato, final_url = checked(original)
    last_check = now_iso()
    resulting = update_url_result(url_id, original, stato, final_url, last_check)
    return {"id": url_id, "url": resulting, "original_url": original,
            "stato": stato, "last_check": last_check}


def check_all_urls():
    initialize_database()
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM urls ORDER BY url")]
    for url_id in ids:
        try:
            check_single_url(url_id)
        except Exception as error:
            print("Errore controllo URL %s: %s" % (url_id, error), file=sys.stderr, flush=True)


check_all_lock = threading.Lock()


def scheduler_loop():
    time.sleep(30)
    while True:
        if check_all_lock.acquire(blocking=False):
            try:
                check_all_urls()
            except Exception as error:
                print("Errore nel controllo periodico: %s" % error, file=sys.stderr, flush=True)
            finally:
                check_all_lock.release()
        time.sleep(CHECK_INTERVAL)


# ============================================================
# LIMITE DI RICHIESTE
# ============================================================

RATE_LIMITS = {
    "login": (10, 60),
    "public": (60, 60),
    "url_check": (30, 60),
    "check_all": (3, 60),
}
rate_store = {}
rate_lock = threading.Lock()


def rate_limit_hit(name, client):
    """Finestra fissa in memoria. Restituisce 0 se ok, altrimenti i secondi di attesa."""
    limit, window = RATE_LIMITS[name]
    now = time.monotonic()
    key = (name, client)
    with rate_lock:
        entry = rate_store.get(key)
        if entry is None or now - entry["start"] >= window:
            rate_store[key] = {"start": now, "count": 1}
            return 0
        if entry["count"] >= limit:
            return max(1, int(window - (now - entry["start"])))
        entry["count"] += 1
        return 0


def cleanup_rate_limit_store():
    while True:
        time.sleep(300)
        now = time.monotonic()
        with rate_lock:
            for key in [k for k, e in rate_store.items() if now - e["start"] > 600]:
                rate_store.pop(key, None)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def db():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def generate_api_key():
    return "".join(secrets.choice(API_KEY_ALPHABET) for _ in range(API_KEY_LENGTH))


def generate_unique_api_key(conn):
    while True:
        api_key = generate_api_key()
        if conn.execute("SELECT 1 FROM users WHERE apikey = ?", (api_key,)).fetchone() is None:
            return api_key


def initialize_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nomeutente TEXT UNIQUE NOT NULL,
                apikey TEXT UNIQUE NOT NULL,
                admin INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 1
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                stato INTEGER NOT NULL DEFAULT 0,
                last_check TEXT
            )""")
        has_admin = conn.execute("SELECT 1 FROM users WHERE admin = 1").fetchone()
        if has_admin is None:
            api_key = generate_unique_api_key(conn)
            conn.execute(
                "INSERT OR IGNORE INTO users (nomeutente, apikey, admin, available) VALUES (?, ?, 1, 1)",
                (ADMIN_USERNAME, api_key))
            if ADMIN_PASSWORD:
                print("Creato l'amministratore '%s'." % ADMIN_USERNAME, flush=True)
            else:
                # Senza APP_PASSWORD (esecuzione fuori da Umbrel) si entra con la chiave API.
                print("Creato l'amministratore '%s'. Chiave API: %s" % (ADMIN_USERNAME, api_key), flush=True)


def ensure_owner_admin():
    """L'account principale esiste ed è amministratore (chi conosce la password di Umbrel non resta mai fuori)."""
    with db() as conn:
        row = conn.execute("SELECT id, admin FROM users WHERE nomeutente = ?", (ADMIN_USERNAME,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (nomeutente, apikey, admin, available) VALUES (?, ?, 1, 1)",
                         (ADMIN_USERNAME, generate_unique_api_key(conn)))
        elif row["admin"] != 1:
            conn.execute("UPDATE users SET admin = 1 WHERE id = ?", (row["id"],))
    return ADMIN_USERNAME


def get_user_by_api_key(api_key):
    if not isinstance(api_key, str) or not KEY_FORMAT.fullmatch(api_key):
        return None
    with db() as conn:
        return conn.execute(
            "SELECT id, nomeutente, apikey, admin, available FROM users WHERE apikey = ?",
            (api_key,)).fetchone()


# ============================================================
# SESSIONI (cookie firmato; amministratore verificato nel database a ogni richiesta)
# ============================================================

def load_secret():
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError as error:
        print("Impossibile salvare la chiave di sessione (%s): le sessioni "
              "scadranno al riavvio." % error, file=sys.stderr, flush=True)
    return data


def sign(raw):
    return b64(hmac.new(SESSION_SECRET, raw.encode("ascii"), hashlib.sha256).digest())


def make_session(username):
    raw = b64(json.dumps({"u": username, "exp": int(time.time()) + SESSION_SECONDS}).encode("utf-8"))
    return raw + "." + sign(raw)


def read_session(cookie_header):
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        value = jar["session"].value
        raw, signature = value.split(".", 1)
        if not hmac.compare_digest(signature, sign(raw)):
            return None
        payload = json.loads(unb64(raw).decode("utf-8"))
        if payload["exp"] < time.time():
            return None
        return payload["u"]
    except (CookieError, KeyError, ValueError, TypeError, UnicodeError):
        return None


# ============================================================
# RISPOSTE E ROUTING
# ============================================================

BLANK_PAGE = ('<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">'
              "<title></title></head><body></body></html>")

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}
CSP_PAGE = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "base-uri 'none'; frame-ancestors 'none'")
CSP_ADMIN = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
             "img-src data:; connect-src 'self'; form-action 'self'; "
             "base-uri 'none'; frame-ancestors 'none'")

ROUTES = [
    ("GET", r"/", "h_page", "public"),
    ("GET", r"/app\.js", "h_app_js", "public"),
    ("GET", r"/api=(.*)", "h_public_list", "public"),
    ("POST", r"/api/login", "h_login", "login"),
    ("POST", r"/api/logout", "h_logout", None),
    ("GET", r"/api/urls", "h_urls_list", None),
    ("POST", r"/api/urls", "h_urls_add", "url_check"),
    ("POST", r"/api/urls/check-all", "h_urls_check_all", "check_all"),
    ("DELETE", r"/api/urls/(\d+)", "h_urls_delete", None),
    ("POST", r"/api/urls/(\d+)/check", "h_urls_check", "url_check"),
    ("GET", r"/api/users", "h_users_list", None),
    ("POST", r"/api/users", "h_users_add", None),
    ("PATCH", r"/api/users/(\d+)", "h_users_update", None),
    ("DELETE", r"/api/users/(\d+)", "h_users_delete", None),
]
ROUTES = [(m, re.compile(p), h, limit) for m, p, h, limit in ROUTES]

BOOLEAN_VALUES = (0, 1, True, False)


class Handler(BaseHTTPRequestHandler):
    server_version = "URLFollower/1.0"
    timeout = 15  # contro i client lenti

    # ---- infrastruttura
    def log_message(self, fmt, *args):
        line = re.sub(r"/api=[^ ]*", "/api=*********", fmt % args)  # le chiavi non finiscono nei log
        sys.stderr.write("%s - %s\n" % (self.client_address[0], line))
        sys.stderr.flush()

    def resolve_client(self):
        """Indirizzo del client per il limite di richieste.

        Dietro il proxy di Umbrel tutte le richieste arrivano dall'IP del
        proxy. X-Forwarded-For è considerato solo se la connessione arriva da
        una rete privata (il proxy) e si usa l'ultimo valore, aggiunto dal
        proxy stesso e non falsificabile dal client.
        """
        peer = self.client_address[0]
        self.trusted_proxy = False
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not peer_ip.is_global:
            self.trusted_proxy = True
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                try:
                    return str(ipaddress.ip_address(forwarded.split(",")[-1].strip()))
                except ValueError:
                    pass
        return peer

    def send_body(self, code, body, ctype, csp=CSP_PAGE, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        for name, value in BASE_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, code, text, extra=None):
        self.send_body(code, text.encode("utf-8"), "text/plain; charset=utf-8", extra=extra)

    def send_json(self, code, payload, extra=None):
        self.send_body(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra=extra)

    def send_file(self, name, ctype, csp=CSP_PAGE):
        try:
            with open(os.path.join(WWW_DIR, name), "rb") as handle:
                body = handle.read()
        except OSError as error:
            print("File %s non leggibile: %s" % (name, error), file=sys.stderr, flush=True)
            return self.send_text(404, "Not Found")
        self.send_body(200, body, ctype, csp=csp)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Richiesta non valida")
        if length > MAX_BODY:
            raise ApiError(413, "Richiesta troppo grande")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ApiError(400, "Richiesta non valida")
        return data if isinstance(data, dict) else {}

    def current_admin(self):
        username = read_session(self.headers.get("Cookie"))
        if not username:
            return None
        with db() as conn:
            row = conn.execute(
                "SELECT id, nomeutente, admin FROM users WHERE nomeutente = ?", (username,)).fetchone()
        return row if row is not None and row["admin"] == 1 else None

    def require_admin(self):
        admin = self.current_admin()
        if admin is None:
            raise ApiError(401, "Non autorizzato")
        return admin

    # ---- dispatch
    def do_GET(self):
        self.dispatch("GET")

    do_HEAD = do_GET

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            self.route("GET" if method == "HEAD" else method)
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except BusyError:
            self.send_json(429, {"error": "Troppi controlli in corso. Riprova tra qualche secondo."},
                           extra={"Retry-After": "10"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:
            print("Errore interno: %r" % error, file=sys.stderr, flush=True)
            try:
                self.send_text(500, "Internal Server Error")
            except OSError:
                pass

    def route(self, method):
        path = urlsplit(self.path).path
        client = self.resolve_client()
        path_known = False
        for route_method, pattern, handler, limit in ROUTES:
            match = pattern.fullmatch(path)
            if not match:
                continue
            path_known = True
            if route_method != method:
                continue
            # Le operazioni che modificano dati richiedono un'intestazione
            # personalizzata: un altro sito aperto nel browser non può inviarla.
            if method != "GET" and self.headers.get("X-URL-Follower") != "1":
                raise ApiError(403, "Richiesta non consentita")
            if limit:
                retry_after = rate_limit_hit(limit, client)
                if retry_after:
                    return self.send_json(429, {"error": "Rate limit superato"},
                                          extra={"Retry-After": str(retry_after)})
            return getattr(self, handler)(*match.groups())
        if path_known:
            return self.send_text(405, "Method Not Allowed")
        return self.send_text(404, "Not Found")

    # ---- pagine
    def h_page(self):
        self.send_file("index.html", "text/html; charset=utf-8", csp=CSP_ADMIN)

    def h_app_js(self):
        self.send_file("app.js", "application/javascript; charset=utf-8", csp=CSP_ADMIN)

    def h_public_list(self, key):
        user = get_user_by_api_key(key)
        if user is None or user["available"] != 1:
            return self.send_body(200, BLANK_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        with db() as conn:
            rows = conn.execute("SELECT url FROM urls WHERE stato = 1 ORDER BY url").fetchall()
        self.send_text(200, "\n".join(row["url"] for row in rows))

    # ---- accesso
    def h_login(self):
        credential = self.read_json().get("password")
        if not isinstance(credential, str) or not credential or len(credential) > 256:
            raise ApiError(401, "Password non valida")
        if ADMIN_PASSWORD and hmac.compare_digest(credential.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
            # Password mostrata da Umbrel: accesso come account principale.
            username = ensure_owner_admin()
        else:
            # Chiave API di un amministratore.
            user = get_user_by_api_key(credential)
            if user is None:
                raise ApiError(401, "Password non valida")
            # "available" non viene controllato qui: un amministratore non
            # disponibile può comunque entrare nel pannello.
            if user["admin"] != 1:
                raise ApiError(403, "Accesso amministrativo non autorizzato")
            username = user["nomeutente"]
        cookie = "session=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
            make_session(username), SESSION_SECONDS)
        if self.trusted_proxy and self.headers.get("X-Forwarded-Proto", "").split(",")[-1].strip() == "https":
            cookie += "; Secure"
        self.send_json(200, {"success": True, "username": username}, extra={"Set-Cookie": cookie})

    def h_logout(self):
        self.send_json(200, {"success": True}, extra={
            "Set-Cookie": "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    # ---- URL monitorati
    def urls_payload(self):
        with db() as conn:
            rows = conn.execute("SELECT id, url, stato, last_check FROM urls ORDER BY url").fetchall()
        return {"urls": [dict(row) for row in rows]}

    def h_urls_list(self):
        self.require_admin()
        self.send_json(200, self.urls_payload())

    def h_urls_add(self):
        self.require_admin()
        url = self.read_json().get("url", "")
        if not isinstance(url, str) or not url.strip():
            raise ApiError(400, "URL mancante")
        url = url.strip()
        if not validate_monitor_url(url):
            raise ApiError(400, "URL non valido o non consentito")
        stato, final_url = checked(url)
        last_check = now_iso()
        try:
            with db() as conn:
                cursor = conn.execute("INSERT INTO urls (url, stato, last_check) VALUES (?, ?, ?)",
                                      (final_url, stato, last_check))
                new_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(409, "URL già presente")
        self.send_json(201, {"success": True, "id": new_id, "url": final_url,
                             "stato": stato, "last_check": last_check})

    def h_urls_delete(self, url_id):
        self.require_admin()
        with db() as conn:
            cursor = conn.execute("DELETE FROM urls WHERE id = ?", (int(url_id),))
        if cursor.rowcount == 0:
            raise ApiError(404, "URL non trovato")
        self.send_json(200, {"success": True})

    def h_urls_check(self, url_id):
        self.require_admin()
        result = check_single_url(int(url_id))
        if result is None:
            raise ApiError(404, "URL non trovato")
        result["success"] = True
        self.send_json(200, result)

    def h_urls_check_all(self):
        self.require_admin()
        with db() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM urls ORDER BY url")]

        def safe_check(url_id):
            try:
                check_single_url(url_id)
            except Exception as error:
                print("Errore controllo URL %s: %s" % (url_id, error), file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(safe_check, ids))
        self.send_json(200, self.urls_payload())

    # ---- utenti
    def h_users_list(self):
        admin = self.require_admin()
        with db() as conn:
            rows = conn.execute(
                "SELECT id, nomeutente, apikey, admin, available FROM users ORDER BY nomeutente").fetchall()
        users = []
        for row in rows:
            # Le chiavi degli altri amministratori non vengono mai mostrate.
            visible = row["nomeutente"] == admin["nomeutente"] or row["admin"] == 0
            users.append({"id": row["id"], "nomeutente": row["nomeutente"],
                          "apikey": row["apikey"] if visible else "*********",
                          "admin": row["admin"], "available": row["available"]})
        self.send_json(200, {"users": users})

    def h_users_add(self):
        self.require_admin()
        data = self.read_json()
        username = data.get("username", "")
        admin = data.get("admin", 0)
        available = data.get("available", 1)
        if not isinstance(username, str) or not username.strip():
            raise ApiError(400, "Nome utente mancante")
        username = username.strip()
        if len(username) > 50:
            raise ApiError(400, "Il nome utente è troppo lungo")
        if admin not in BOOLEAN_VALUES:
            raise ApiError(400, "Valore amministratore non valido")
        if available not in BOOLEAN_VALUES:
            raise ApiError(400, "Valore disponibilità non valido")
        admin = 1 if admin else 0
        available = 1 if available else 0
        try:
            with db() as conn:
                api_key = generate_unique_api_key(conn)
                cursor = conn.execute(
                    "INSERT INTO users (nomeutente, apikey, admin, available) VALUES (?, ?, ?, ?)",
                    (username, api_key, admin, available))
                new_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(409, "Nome utente già presente")
        self.send_json(201, {"success": True, "id": new_id, "username": username,
                             "apikey": api_key, "admin": admin, "available": available})

    def h_users_update(self, user_id):
        self.require_admin()
        data = self.read_json()
        if "admin" not in data and "available" not in data:
            raise ApiError(400, "Nessun valore da modificare")
        with db() as conn:
            user = conn.execute("SELECT id, nomeutente, admin, available FROM users WHERE id = ?",
                                (int(user_id),)).fetchone()
            if user is None:
                raise ApiError(404, "Utente non trovato")
            new_admin, new_available = user["admin"], user["available"]
            if "admin" in data:
                if data["admin"] not in BOOLEAN_VALUES:
                    raise ApiError(400, "Valore amministratore non valido")
                new_admin = 1 if data["admin"] else 0
            if "available" in data:
                if data["available"] not in BOOLEAN_VALUES:
                    raise ApiError(400, "Valore disponibilità non valido")
                new_available = 1 if data["available"] else 0
            # Protezione dell'ultimo amministratore.
            if user["admin"] == 1 and new_admin == 0:
                count = conn.execute("SELECT COUNT(*) FROM users WHERE admin = 1").fetchone()[0]
                if count <= 1:
                    raise ApiError(400, "Non puoi rimuovere l'ultimo amministratore")
            conn.execute("UPDATE users SET admin = ?, available = ? WHERE id = ?",
                         (new_admin, new_available, user["id"]))
        self.send_json(200, {"success": True, "username": user["nomeutente"],
                             "admin": new_admin, "available": new_available})

    def h_users_delete(self, user_id):
        current = self.require_admin()
        with db() as conn:
            row = conn.execute("SELECT id, nomeutente, admin FROM users WHERE id = ?",
                               (int(user_id),)).fetchone()
            if row is None:
                raise ApiError(404, "Utente non trovato")
            if row["nomeutente"] == current["nomeutente"]:
                raise ApiError(400, "Non puoi eliminare l'utente attualmente autenticato")
            if row["admin"] == 1:
                count = conn.execute("SELECT COUNT(*) FROM users WHERE admin = 1").fetchone()[0]
                if count <= 1:
                    raise ApiError(400, "Non puoi eliminare l'ultimo amministratore")
            conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        self.send_json(200, {"success": True})


# ============================================================
# AVVIO
# ============================================================

def main():
    global SESSION_SECRET
    try:
        initialize_database()
    except (OSError, sqlite3.Error) as error:
        print("Impossibile usare la cartella dati %s: %s" % (DATA_DIR, error), file=sys.stderr, flush=True)
        sys.exit(1)

    if "--check-urls" in sys.argv:
        check_all_urls()
        return
    if "--show-admin-keys" in sys.argv:
        with db() as conn:
            for row in conn.execute("SELECT nomeutente, apikey FROM users WHERE admin = 1 ORDER BY nomeutente"):
                print("%s: %s" % (row["nomeutente"], row["apikey"]))
        return

    SESSION_SECRET = load_secret()
    threading.Thread(target=cleanup_rate_limit_store, daemon=True).start()
    if CHECK_INTERVAL > 0:
        threading.Thread(target=scheduler_loop, daemon=True).start()
    print("URL Follower in ascolto sulla porta %d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
