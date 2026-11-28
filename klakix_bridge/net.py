# SPDX-License-Identifier: GPL-3.0-or-later
"""Network side of the bridge. Runs in background threads and never touches bpy.

Protocol (docs/mcp/KOPRU.md in the Klakix repo):
  POST /api/kopru/eslestir          start pairing -> user code shown in Blender
  POST /api/kopru/eslestir/jeton    poll until the user approves -> device token
  POST /api/kopru/merhaba           send the operation catalog
  POST /api/kopru/bekle             long poll (25 s) for commands
  POST /api/kopru/sonuc             send a command result
  POST /api/kopru/hata              automatic error report
  POST /api/kopru/cikis             forget this device
"""

import json
import os
import queue
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

SERVERS = {
    "klakix": "https://klakix.com",
    "aivideoyap": "https://aivideoyap.com",
}

USER_AGENT = "KlakixBridge-Blender"


def _ssl_context():
    try:
        import certifi  # bundled with Blender

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_CTX = _ssl_context()


class HttpError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


def request_json(base, path, body=None, token=None, timeout=20):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        ctx = _CTX if base.startswith("https") else None
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        err = payload.get("hata") or {}
        raise HttpError(e.code, err.get("kod") or "http_%d" % e.code, err.get("mesaj") or str(e))


def download(url, dest_dir, max_bytes=500 * 1024 * 1024, timeout=60):
    """Downloads url into dest_dir and returns the local path. https (or local dev http) only."""
    if not (url.startswith("https://") or url.startswith("http://localhost") or url.startswith("http://127.0.0.1")):
        raise ValueError("Only https links can be downloaded.")
    os.makedirs(dest_dir, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    ctx = _CTX if url.startswith("https") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        name = urllib.parse.unquote(urllib.parse.urlparse(r.geturl()).path.rsplit("/", 1)[-1]) or "download"
        name = "".join(c for c in name if c.isalnum() or c in "._-")[:120] or "download"
        ctype = r.headers.get("Content-Type", "")
        if "." not in name:
            ext = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "video/mp4": ".mp4",
                "video/quicktime": ".mov",
                "model/gltf-binary": ".glb",
                "audio/mpeg": ".mp3",
                "audio/wav": ".wav",
            }.get(ctype.split(";")[0].strip(), "")
            name += ext
        path = os.path.join(dest_dir, "%d_%s" % (int(time.time() * 1000), name))
        total = 0
        with open(path, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    f.close()
                    os.remove(path)
                    raise ValueError("The file is larger than %d MB." % (max_bytes // (1024 * 1024)))
                f.write(chunk)
    return path


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


class Bridge:
    """Owns the polling thread, the sender thread and the queues shared with the main thread.

    Main thread (bpy timer) calls `take_commands()` and `send_result()`; everything else is here.
    """

    def __init__(self, config, catalog_fn, prepare_fn, info_fn, log_fn):
        self.config = config  # dict-like with load/save (see __init__.py)
        self.catalog_fn = catalog_fn  # () -> catalog dict (built on main thread, cached)
        self.prepare_fn = prepare_fn  # (command) -> command, runs in worker thread (downloads)
        self.info_fn = info_fn  # () -> dict (app version, document) cached from main thread
        self.log = log_fn
        self.incoming = queue.Queue()
        self.outgoing = queue.Queue()
        self.state = "idle"  # idle | pairing | connecting | online | error | offline_blocked
        self.status_text = ""
        self.user_code = None
        self.verify_url = None
        self.account = None
        self._stop = threading.Event()
        self._threads = []
        self._seen = {}  # command id -> time received
        self._need_hello = True
        self._last_report = {}
        self._pair_lock = threading.Lock()
        self.latest_version = None
        self.download_url = None

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for target in (self._poll_loop, self._send_loop):
            t = threading.Thread(target=target, name="klakix-" + target.__name__, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._threads = []

    @property
    def base(self):
        return self.config.get("server_url") or SERVERS["klakix"]

    @property
    def token(self):
        return self.config.get("token")

    # ------------------------------------------------------------------ pairing

    def begin_pairing(self, device_name, app_version, addon_version, os_name):
        """Called from the UI operator (main thread); network work happens in a thread."""

        def run():
            with self._pair_lock:
                try:
                    self.state = "pairing"
                    self.status_text = "Requesting a code…"
                    r = request_json(
                        self.base,
                        "/api/kopru/eslestir",
                        {
                            "uygulama": "blender",
                            "ad": device_name,
                            "uygulama_surumu": app_version,
                            "eklenti_surumu": addon_version,
                            "isletim_sistemi": os_name,
                        },
                    )
                    self.user_code = r["kullanici_kodu"]
                    self.verify_url = r["dogrulama_adresi_tam"]
                    self.status_text = "Approve the code in your browser"
                    try:
                        import webbrowser

                        webbrowser.open(self.verify_url)
                    except Exception:
                        pass
                    interval = max(2, int(r.get("aralik", 3)))
                    deadline = time.time() + int(r.get("sure", 600))
                    while time.time() < deadline and not self._stop.is_set() and self.state == "pairing":
                        time.sleep(interval)
                        try:
                            t = request_json(self.base, "/api/kopru/eslestir/jeton", {"cihaz_kodu": r["cihaz_kodu"]})
                        except HttpError as e:
                            if e.code == "authorization_pending":
                                continue
                            if e.code == "slow_down":
                                interval += 2
                                continue
                            if e.code == "access_denied":
                                self._pair_failed("The connection was declined on the website.")
                                return
                            self._pair_failed("The code expired. Click Connect again.")
                            return
                        self.config.update({"token": t["jeton"], "device_id": t["cihaz_id"], "account": t.get("hesap")})
                        self.account = t.get("hesap")
                        self.user_code = None
                        self.verify_url = None
                        self._need_hello = True
                        self.state = "connecting"
                        self.status_text = "Connected"
                        self.log("Connected to " + (self.account or "your account"))
                        return
                    if self.state == "pairing":
                        self._pair_failed("The code expired. Click Connect again.")
                except Exception as e:
                    self._pair_failed(_friendly(e))

        threading.Thread(target=run, name="klakix-pair", daemon=True).start()

    def cancel_pairing(self):
        if self.state == "pairing":
            self.state = "idle"
            self.user_code = None
            self.verify_url = None
            self.status_text = ""

    def _pair_failed(self, msg):
        self.state = "error"
        self.status_text = msg
        self.user_code = None
        self.verify_url = None

    def disconnect(self):
        token = self.token
        self.config.update({"token": None, "device_id": None, "account": None})
        self.state = "idle"
        self.status_text = "Disconnected"
        self.account = None
        if token:
            threading.Thread(target=lambda: _quiet(request_json, self.base, "/api/kopru/cikis", {}, token, 10), daemon=True).start()

    def request_hello(self):
        self._need_hello = True

    # ------------------------------------------------------------------ main-thread API

    def take_commands(self):
        items = []
        while True:
            try:
                items.append(self.incoming.get_nowait())
            except queue.Empty:
                return items

    def send_result(self, command_id, payload):
        self.outgoing.put(("sonuc", dict(payload, id=command_id)))

    def report_error(self, code, message, stack=None, context=None, severity="hata"):
        """Automatic error report (throttled: same code at most once a minute)."""
        key = code + "|" + (message or "")[:80]
        now = time.time()
        if now - self._last_report.get(key, 0) < 60:
            return
        self._last_report[key] = now
        self.outgoing.put(("hata", {"kod": code, "mesaj": (message or "")[:2000], "yigin": (stack or "")[:8000], "baglam": context or {}, "onem": severity}))

    # ------------------------------------------------------------------ threads

    def _poll_loop(self):
        backoff = 2
        while not self._stop.is_set():
            if not self.token or self.state in ("pairing",):
                if self.state not in ("pairing", "error"):
                    self.state = "idle"
                time.sleep(0.5)
                continue
            if self.config.get("paused"):
                self.state = "paused"
                time.sleep(0.5)
                continue
            try:
                if self._need_hello:
                    self.state = "connecting"
                    info = self.info_fn() or {}
                    r = request_json(
                        self.base,
                        "/api/kopru/merhaba",
                        {
                            "katalog": self.catalog_fn(),
                            "ad": info.get("device_name"),
                            "uygulama_surumu": info.get("app_version"),
                            "eklenti_surumu": info.get("addon_version"),
                            "isletim_sistemi": info.get("os"),
                            "belge": info.get("document"),
                        },
                        self.token,
                        timeout=45,
                    )
                    self._need_hello = False
                    self.latest_version = r.get("guncel_surum")
                    self.download_url = r.get("indirme_adresi")
                    self.account = self.account or self.config.get("account")
                self.state = "online"
                self.status_text = "Connected"
                now = time.time()
                self._seen = {k: v for k, v in self._seen.items() if now - v < 120}
                info = self.info_fn() or {}
                r = request_json(self.base, "/api/kopru/bekle", {"alinan": list(self._seen.keys()), "belge": info.get("document")}, self.token, timeout=45)
                backoff = 2
                if r.get("merhaba_gerekli"):
                    self._need_hello = True
                for cmd in r.get("komutlar") or []:
                    cid = cmd.get("id")
                    if not cid or cid in self._seen:
                        continue
                    self._seen[cid] = time.time()
                    threading.Thread(target=self._prepare_and_queue, args=(cmd,), daemon=True).start()
            except HttpError as e:
                if e.status == 401:
                    self.config.update({"token": None, "device_id": None})
                    self.state = "error"
                    self.status_text = "This device was disconnected. Click Connect to link it again."
                    continue
                self.state = "error"
                self.status_text = "Server error (%s). Retrying…" % e.code
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as e:
                self.state = "error"
                self.status_text = "Can't reach the server. Retrying…"
                if not _is_network_error(e):
                    self.report_error("net:poll", str(e), traceback.format_exc())
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _prepare_and_queue(self, cmd):
        try:
            cmd = self.prepare_fn(cmd)
        except Exception as e:
            # Preparation (downloads) failed: answer right away, the main thread never sees it.
            expected = isinstance(e, (ValueError, urllib.error.URLError))
            self.send_result(
                cmd["id"],
                {
                    "tamam": False,
                    "hata": {
                        "kod": "download_failed" if isinstance(e, urllib.error.URLError) else "invalid_input",
                        "mesaj": _friendly(e),
                        "beklenmeyen": not expected,
                        **({} if expected else {"yigin": traceback.format_exc()}),
                    },
                },
            )
            return
        self.incoming.put(cmd)

    def _send_loop(self):
        while not self._stop.is_set():
            try:
                kind, payload = self.outgoing.get(timeout=0.5)
            except queue.Empty:
                continue
            token = self.token
            if not token:
                continue
            path = "/api/kopru/sonuc" if kind == "sonuc" else "/api/kopru/hata"
            for attempt in range(3):
                try:
                    request_json(self.base, path, payload, token, timeout=60)
                    break
                except HttpError as e:
                    if e.status in (400, 401, 404, 410, 413, 429):
                        break
                    time.sleep(1 + attempt * 2)
                except Exception:
                    time.sleep(1 + attempt * 2)


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


def _is_network_error(e):
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


def _friendly(e):
    if isinstance(e, HttpError):
        return str(e)
    if isinstance(e, urllib.error.URLError):
        return "Network error: %s" % getattr(e, "reason", e)
    return str(e) or e.__class__.__name__
