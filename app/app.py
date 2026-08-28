import glob
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import json
import sqlite3
import zipfile
from datetime import datetime

import requests
from flask import Flask, request, jsonify, render_template, send_from_directory, abort
from PIL import Image

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_DIR = os.environ.get("SCAN_DIR", "/scans")
JOB_DIR = "/tmp/scannerjobs"
SCANNER_IP = os.environ.get("SCANNER_IP", "192.168.1.107")
SCANNER_DEVICE = os.environ.get("SCANNER_DEVICE", "").strip()
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("AUTH_PASS", "").strip()
SCAN_TIMEOUT = int(os.environ.get("SCAN_TIMEOUT", "180"))
ADF_TIMEOUT = int(os.environ.get("ADF_TIMEOUT", "300"))

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "").strip().rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "").strip()
PAPERLESS_USER = os.environ.get("PAPERLESS_USER", "").strip()
PAPERLESS_PASS = os.environ.get("PAPERLESS_PASS", "").strip()

os.makedirs(SCAN_DIR, exist_ok=True)
os.makedirs(JOB_DIR, exist_ok=True)
THUMB_DIR = os.path.join(SCAN_DIR, ".thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)
DB_PATH = os.path.join(SCAN_DIR, ".catalog.db")

_SCHEMA = """CREATE TABLE IF NOT EXISTS files(
    name TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    pages INTEGER,
    thumb_ok INTEGER DEFAULT 0,
    paperless_id TEXT,
    paperless_ts REAL
);"""


def db():
    """Kurze SQLite-Verbindung (Katalog fuer Galerie + Previews + Paperless-Status)."""
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(_SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
    if "paperless_id" not in cols:
        c.execute("ALTER TABLE files ADD COLUMN paperless_id TEXT")
    if "paperless_ts" not in cols:
        c.execute("ALTER TABLE files ADD COLUMN paperless_ts REAL")
    c.commit()
    return c

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

scan_lock = threading.Lock()
jobs = {}  # job_id -> {"device":..., "dir":..., "pages": [...]}
duplex_jobs = {}  # job_id -> {"dir":..., "front_pages": [...], "ts":...}
DEVICE_CACHE = {"ts": 0, "list": []}
OPT_CACHE_FILE = os.path.join(SCAN_DIR, ".device_options.json")
OPT_CACHE_TTL = 3600
OPT_DISK_TTL = 86400
_opt_cache = {"device": None, "ts": 0.0, "opts": None}

# Autoscans: lauffaehiger Background-Poller, der bei Papier im ADF automatisch scannt
AUTO_BASE_INTERVAL = float(os.environ.get("AUTO_INTERVAL", "2.5"))
auto_cfg = {
    "enabled": True,
    "interval": AUTO_BASE_INTERVAL,
    "last": None,        # {"file":..., "pages":..., "ts":...}
    "state": "idle",     # idle | scanning | error
    "paperless": bool(PAPERLESS_URL),   # ADF-Scans direkt an Paperless senden
}
last_ui_settings = {"mode": "Gray", "resolution": 300, "x": 210.0, "y": 297.0}

MODEL_SLUGS = [
    "officejet_4500_g510g-m",
    "officejet_4500_g510g",
    "officejet_4500_g510n",
    "officejet_4500_g510a",
    "officejet_4500",
]


def proc_running(name):
    rc, _, _ = run(["sh", "-c", "ps -eo comm= | grep -x %s" % name], timeout=5)
    return rc == 0


def ensure_services():
    """hpaio braucht D-Bus Systembus + Avahi. Bei Containern mit persistentem
    /run koennen stale Sockets zurueckbleiben; dann sauber neu starten."""
    started = False
    if not proc_running("dbus-daemon"):
        subprocess.run(["mkdir", "-p", "/run/dbus"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["rm", "-f", "/run/dbus/pid", "/run/dbus/system_bus_socket"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["dbus-daemon", "--system", "--fork"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True
    if not proc_running("avahi-daemon"):
        subprocess.run(["avahi-daemon", "--no-drop-root", "-D"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True
    if started:
        time.sleep(1)


def log(msg):
    print("[scanner] %s" % msg, flush=True)


def run(cmd, timeout=SCAN_TIMEOUT):
    """Run a command, return (rc, stdout, stderr).

    Laeuft in einer eigenen Prozesssession (start_new_session), damit bei Timeout
    der ganze Prozessbaum (inkl. HPLIP/hpaio-Helfer) gekillt wird. Andernfalls
    bleibt das Netz-Geraet gesperrt und alle folgenden Scans haengen ebenfalls ab."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, start_new_session=True)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        # Kind + Prozessbaum hart killen, damit das Gerat wieder freigegeben wird
        try:
            os.killpg(os.getpgid(e.pid), 9)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        return -1, "", "timeout after %ss" % timeout
    except FileNotFoundError:
        return -2, "", "command not found"


def detect_device():
    """Find the hpaio device URI for the network scanner."""
    ensure_services()
    if SCANNER_DEVICE:
        log("using configured device %s" % SCANNER_DEVICE)
        return SCANNER_DEVICE

    # 1) Discovery via scanimage -L
    rc, out, err = run(["scanimage", "-L"], timeout=20)
    devs = [l for l in out.splitlines() if l.strip().startswith("device `")]
    if devs:
        m = re.search(r"device `([^']+)'", devs[0])
        if m:
            uri = m.group(1)
            if "hpaio" in uri and SCANNER_IP:
                uri = re.sub(r"\?ip=[^&]+", "?ip=%s" % SCANNER_IP, uri)
            log("discovered device %s" % uri)
            return uri

    # 2) Modell-Slug mit expliziter IP probieren (Routing ueber Subnetze moeglich)
    if SCANNER_IP:
        for slug in MODEL_SLUGS:
            uri = "hpaio:/net/%s?ip=%s" % (slug, SCANNER_IP)
            rc, out, err = run(["scanimage", "-d", uri, "--help"], timeout=15)
            if rc == 0:
                log("device works via slug %s" % uri)
                return uri
    return None


def list_devices(force=False):
    """Return list of discovered SANE devices as dicts."""
    now = time.time()
    if not force and DEVICE_CACHE["list"] and now - DEVICE_CACHE["ts"] < 30:
        return DEVICE_CACHE["list"]
    rc, out, err = run(["scanimage", "-L"], timeout=30)
    devs = []
    for line in out.splitlines():
        m = re.match(r"device `([^']+)' is a (.+)", line.strip())
        if m:
            devs.append({"id": m.group(1), "desc": m.group(2)})
    if not devs and detect_device():
        devs = [{"id": detect_device(), "desc": "HP Officejet 4500 All-in-One"}]
    DEVICE_CACHE.update({"ts": now, "list": devs})
    return devs


def parse_device_options(out):
    opts = {"modes": [], "resolutions": [], "sources": [], "max_x": None, "max_y": None}
    for line in out.splitlines():
        m = re.match(r"^\s*--mode\s+([\w|]+)", line)
        if m:
            opts["modes"] = m.group(1).split("|")
            continue
        m = re.match(r"^\s*--resolution\s+([0-9|]+)", line)
        if m:
            opts["resolutions"] = [int(x) for x in m.group(1).split("|")]
            continue
        m = re.match(r"^\s*--source\s+([\w|]+)", line)
        if m:
            opts["sources"] = m.group(1).split("|")
            continue
        m = re.match(r"^\s*-x\s+0\.\.<*([0-9.]+)>*mm", line)
        if m and opts["max_x"] is None:
            opts["max_x"] = float(m.group(1))
            continue
        m = re.match(r"^\s*-y\s+0\.\.<*([0-9.]+)>*mm", line)
        if m and opts["max_y"] is None:
            opts["max_y"] = float(m.group(1))
    return opts


def _opt_cache_valid(cache, device, ttl):
    return cache["device"] == device and cache["opts"] is not None and time.time() - cache["ts"] < ttl


def _opt_cache_load_disk(device):
    """Wiederherstellen des Optionen- und Geräte-Caches aus der Datei (überlebt Restarts)."""
    global _opt_cache
    try:
        with open(OPT_CACHE_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    now = time.time()
    if data.get("device") == device and data.get("opts") and now - data.get("ts", 0) < OPT_DISK_TTL:
        _opt_cache = {"device": device, "ts": data["ts"], "opts": data["opts"]}
        log("loaded cached device options")


def _opt_cache_save_disk(output):
    try:
        with open(OPT_CACHE_FILE, "w") as f:
            json.dump({"device": output["device"], "ts": output["ts"], "opts": output["opts"]}, f)
    except Exception:
        pass


def _opt_cache_warm(device):
    """Hintergrund-Warmup: scanimage --help läuft einmal, dann ist /api/config sofort schnell."""
    try:
        if device:
            device_options(device)
    except Exception:
        pass


def normalize_mode(device, mode, opts):
    """Fuehrt einen nicht vom Gerat akzeptierten Scan-Betriebsmodus auf einen
    gultigen zuruck (z. B. 'Gray', wenn das Gerat nur 'Lineart|Color' kennt).
    Ohne opts wird unveraendert zurueckgegeben."""
    if not opts or not opts.get("modes"):
        return mode
    available = [m.lower() for m in opts["modes"]]
    if mode and mode.lower() in available:
        return mode
    # Naechstbesten passenden Vorschlag, ansonsten Gerat-Default (erster Eintrag)
    want = (mode or "Gray").lower()
    for pref in ("gray", "grey", "monochrome", "normal", "color", "colour", "lineart", "photo"):
        if pref in available and (pref in want or want in pref):
            return next(m for m in opts["modes"] if m.lower() == pref)
    return opts["modes"][0]


def device_options(device):
    """Parse scanimage --help into structured options (gecacht, da der Probe ~15 s dauert)."""
    global _opt_cache
    if _opt_cache_valid(_opt_cache, device, OPT_CACHE_TTL):
        return _opt_cache["opts"], None
    _opt_cache_load_disk(device)
    if _opt_cache_valid(_opt_cache, device, OPT_CACHE_TTL):
        return _opt_cache["opts"], None

    rc, out, err = run(["scanimage", "-d", device, "--help"], timeout=20)
    if rc != 0:
        return {}, "scanimage --help failed: %s" % err.strip()

    opts = parse_device_options(out)
    entry = {"device": device, "ts": time.time(), "opts": opts}
    _opt_cache = entry
    _opt_cache_save_disk(entry)
    return opts, None


def pick_device():
    """Best-effort: return current active device (configured or detected)."""
    return SCANNER_DEVICE or detect_device()


def scan_image(device, mode, resolution, source, fmt, width_mm, height_mm):
    """Run scanimage; returns (ok, path, error)."""
    out_path = tempfile.mktemp(suffix=".%s" % fmt, dir=JOB_DIR)
    sfmt = fmt

    opts, _ = device_options(device)
    mode = normalize_mode(device, mode, opts)
    use_mode = bool(opts.get("modes")) and mode.lower() in [m.lower() for m in opts["modes"]]

    cmd = ["scanimage", "-d", device]
    if use_mode:
        cmd += ["--mode", mode]
    if resolution:
        cmd += ["--resolution", str(resolution)]
    if source:
        cmd += ["--source", source]
    cmd += ["--compression", "None"]
    cmd += ["-x", "%.2f" % width_mm, "-y", "%.2f" % height_mm]
    cmd += ["--format", sfmt, "-o", out_path]

    log("running: %s" % " ".join(cmd))
    with scan_lock:
        rc, so, err = run(cmd)
    if rc != 0:
        if os.path.exists(out_path):
            os.remove(out_path)
        return False, None, err.strip() or "scanimage rc=%s" % rc
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return False, None, "leere Ausgabe vom Scanner"
    return True, out_path, None


def adf_scan_pages(device, mode, resolution, width_mm, height_mm, jdir):
    """Ein kompletter ADF-Durchzug in jdir. Returns (ok, [png_paths], err)."""
    opts, _ = device_options(device)
    mode = normalize_mode(device, mode, opts)
    use_mode = bool(opts.get("modes")) and mode.lower() in [m.lower() for m in opts["modes"]]
    pattern = os.path.join(jdir, "page%d.png")
    cmd = ["scanimage", "-d", device]
    if use_mode:
        cmd += ["--mode", mode]
    cmd += ["--resolution", str(resolution), "--source", "ADF",
            "--batch-scan=yes", "--batch=%s" % pattern, "--format", "png",
            "--compression", "None",
            "-x", "%.2f" % width_mm, "-y", "%.2f" % height_mm]
    log("ADF running: %s" % " ".join(cmd))
    with scan_lock:
        rc, so, err = run(cmd, timeout=ADF_TIMEOUT)
    pages = sorted(glob.glob(os.path.join(jdir, "page*.png")),
                   key=lambda p: int(re.search(r"page(\d+)\.png", p).group(1)))
    if not pages:
        return False, None, "Keine Seiten gescannt. Ist Papier im ADF? (%s)" % (err.strip() or "rc=%s" % rc)
    return True, pages, None


def scan_adf(device, mode, resolution, width_mm, height_mm, fmt):
    """Scan all pages from the ADF in one continuous pass."""
    jdir = tempfile.mkdtemp(dir=JOB_DIR)
    try:
        ok, pages, err = adf_scan_pages(device, mode, resolution, width_mm, height_mm, jdir)
        if not ok:
            return False, None, err, 0
        if fmt == "pdf":
            dest = os.path.join(SCAN_DIR, "scan_%s.pdf" % time.strftime("%Y%m%d_%H%M%S"))
            try:
                pptos_merge_pdf(pages, dest)
            except Exception as e:
                return False, None, "PDF-Erstellung fehlgeschlagen: %s" % e
        else:
            dest = os.path.join(SCAN_DIR, "scan_%s.zip" % time.strftime("%Y%m%d_%H%M%S"))
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
                for i, p in enumerate(pages, 1):
                    z.write(p, "page%d.png" % i)
        return True, dest, None, len(pages)
    finally:
        shutil_rmtree(jdir)


def ensure_pdf_page(src):
    """Convert scanned image into a consistent PNG for PDF assembly."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        png = tempfile.mktemp(suffix=".png", dir=JOB_DIR)
        im.save(png, "PNG")
        return png


def pptos_merge_pdf(pngs, dest):
    """Merge list of PNGs into a PDF using Pillow."""
    imgs = [Image.open(p).convert("RGB") for p in pngs]
    first, rest = imgs[0], imgs[1:]
    first.save(dest, "PDF", resolution=100.0, save_all=True, append_images=rest)
    for i in imgs:
        i.close()
    return dest


def pdf_page_count(path):
    try:
        with Image.open(path) as im:
            return im.n_frames
    except Exception:
        pass
    try:
        rc, out, _ = run(["pdfinfo", path], timeout=15)
        if rc == 0:
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def ensure_preview(src, max_w=400):
    """Vorschau/Thumbnail fuer Bild ODER PDF (Seite 1). Disk-Cache ueberlebt
    Restarts und aendert sich nur bei neuer Datei (Name+mtime)."""
    key = "%s_%d.jpg" % (os.path.basename(src).replace(".", "_"), int(os.path.getmtime(src)))
    cache = os.path.join(THUMB_DIR, key)
    if os.path.exists(cache):
        return cache
    is_pdf = src.lower().endswith(".pdf")
    tmp = os.path.join(THUMB_DIR, "tmp_%d_%s" % (int(time.time() * 1000), key))
    try:
        if is_pdf:
            prefix = os.path.join(THUMB_DIR, "pvp%dx" % int(time.time() * 1000))
            rc, _, err = run(["pdftoppm", "-jpeg", "-r", "60", "-f", "1", "-l", "1", src, prefix], timeout=60)
            outs = sorted(glob.glob(prefix + "*.jpg"))
            if rc != 0 or not outs:
                return None
            os.replace(outs[0], tmp)
        else:
            with Image.open(src) as im:
                im.thumbnail((max_w, max_w * 2))
                im.convert("RGB").save(tmp, "JPEG", quality=80)
    except Exception as e:
        log("preview failed for %s: %s" % (os.path.basename(src), e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    try:
        os.replace(tmp, cache)
    except OSError:
        return tmp
    return cache


def cleanup_old_jobs(max_age=3600):
    now = time.time()
    for jid, j in list(jobs.items()):
        if now - j["ts"] > max_age:
            try:
                shutil_rmtree(j["dir"])
            except Exception:
                pass
            jobs.pop(jid, None)
    for jid, j in list(duplex_jobs.items()):
        if now - j["ts"] > max_age:
            try:
                shutil_rmtree(j["dir"])
            except Exception:
                pass
            duplex_jobs.pop(jid, None)


def shutil_rmtree(d):
    import shutil

    shutil.rmtree(d, ignore_errors=True)


def scan_history_sync():
    """Verzeichnis mit SQLite-Katalog abgleichen; neu/geaendert = Preview ausstehend."""
    c = db()
    try:
        names = {f for f in os.listdir(SCAN_DIR) if not f.startswith(".")}
        cur = c.execute("SELECT name, size, mtime, pages, thumb_ok FROM files")
        rows = {r[0]: r for r in cur.fetchall()}
        for name in rows:
            if name not in names:
                c.execute("DELETE FROM files WHERE name=?", (name,))
        for name in names:
            p = os.path.join(SCAN_DIR, name)
            try:
                size = os.path.getsize(p)
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            r = rows.get(name)
            changed = (not r or r[1] != size or abs(r[2] - mtime) > 0.001
                       or (name.lower().endswith(".pdf") and r[3] is None))
            if changed:
                try:
                    pages = pdf_page_count(p) if name.lower().endswith(".pdf") else 1
                except Exception:
                    pages = None
                c.execute("INSERT OR REPLACE INTO files(name,size,mtime,pages,thumb_ok) VALUES(?,?,?,?,0)",
                          (name, size, mtime, pages))
        c.commit()
        cur = c.execute("SELECT name,size,mtime,pages,thumb_ok,paperless_id,paperless_ts FROM files ORDER BY mtime DESC")
        return cur.fetchall()
    finally:
        c.close()


def mark_paperless(name, doc_id):
    """Paperless-Status einer Datei im Katalog vermerken (wird in Galerie angezeigt)."""
    try:
        c = db()
        now = time.time()
        c.execute(
            "INSERT INTO files(name,size,mtime,pages,thumb_ok,paperless_id,paperless_ts) "
            "VALUES(?,0,?,1,0,?,?) "
            "ON CONFLICT(name) DO UPDATE SET paperless_id=excluded.paperless_id, paperless_ts=excluded.paperless_ts",
            (name, now, str(doc_id) if doc_id else None, now))
        c.commit()
        c.close()
    except Exception as e:
        log("mark paperless failed: %s" % e)


def paperless_upload(name, title="", created=""):
    """Eine gescannte Datei an Paperless-ngx ueber die API senden.
    Returns (ok, err, doc_id)."""
    if not PAPERLESS_URL:
        return False, "Paperless ist nicht konfiguriert.", None
    full = os.path.join(SCAN_DIR, name)
    if not name or not os.path.isfile(full):
        return False, "Datei nicht gefunden.", None

    mime = "application/pdf" if name.lower().endswith(".pdf") else "image/%s" % (
        "png" if name.lower().endswith(".png") else "jpeg")
    headers = {}
    if PAPERLESS_TOKEN:
        headers["Authorization"] = "Token %s" % PAPERLESS_TOKEN

    if not created:
        created = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    payload = {"title": title or name, "created": created}

    try:
        kwargs = dict(
            params={"format": "json"},
            headers=headers,
            files={"document": (name, open(full, "rb"), mime)},
            data=payload,
            timeout=180,
        )
        if PAPERLESS_USER and PAPERLESS_PASS:
            kwargs["auth"] = (PAPERLESS_USER, PAPERLESS_PASS)
        resp = requests.post(PAPERLESS_URL + "/api/documents/post_document/", **kwargs)
    except requests.RequestException as e:
        return False, "Verbindung zu Paperless fehlgeschlagen: %s" % e, None

    if resp.status_code in (200, 201):
        doc_id = None
        try:
            data = resp.json()
            if isinstance(data, str):
                doc_id = data
            elif isinstance(data, dict):
                doc_id = data.get("id")
        except ValueError:
            pass
        log("paperless upload ok: %s %s" % (name, resp.status_code))
        mark_paperless(name, doc_id)
        return True, "ok", doc_id

    log("paperless upload failed: %s %s" % (name, resp.status_code))
    return False, "Paperless antwortete mit Status %s: %s" % (resp.status_code, resp.text[:300]), None


def preview_worker():
    """Hintergrund: Vorschauen fuer neue/geaenderte Dateien vorrendern."""
    scan_history_sync()
    while True:
        time.sleep(2)
        c = db()
        try:
            pending = [r[0] for r in c.execute("SELECT name FROM files WHERE thumb_ok=0").fetchall()]
        finally:
            c.close()
        for name in pending:
            full = os.path.join(SCAN_DIR, name)
            if not os.path.isfile(full):
                continue
            ok = ensure_preview(full) is not None
            c = db()
            try:
                c.execute("UPDATE files SET thumb_ok=? WHERE name=?", (1 if ok else 2, name))
                c.commit()
            finally:
                c.close()
        time.sleep(1)


def lock_busy():
    """True wenn gerade ein anderer Scan laeuft (non-blocking)."""
    got = scan_lock.acquire(blocking=False)
    if got:
        scan_lock.release()
        return False
    return True


def auto_scan_once():
    """Ein ADF-Versuch aus dem Autoscan-Loop. Wenn Papier da ist, wir der ganze
    Stapel als PDF gescannt; sonst passiert nichts."""
    device = pick_device()
    if not device or lock_busy():
        return False
    s = last_ui_settings
    auto_cfg["state"] = "scanning"
    try:
        # scan_adf sichert sich selbst mit scan_lock; hier nicht nochmal halten (nicht reentrant)
        ok, dest, err, n_pages = scan_adf(device, s["mode"], s["resolution"], s["x"], s["y"], "pdf")
    except Exception as e:
        log("autoscan exception: %s" % e)
        auto_cfg["state"] = "error"
        return False
    auto_cfg["state"] = "idle"
    if ok:
        fn = os.path.basename(dest)
        auto_cfg["last"] = {"file": fn, "pages": n_pages, "ts": time.time()}
        log("autoscan: %s (%d Seiten)" % (fn, n_pages))
        if auto_cfg["paperless"] and PAPERLESS_URL:
            pok, perr, pid = paperless_upload(fn)
            if pok:
                log("autoscan -> paperless %s (doc %s)" % (fn, pid))
            else:
                log("autoscan -> paperless fehlgeschlagen: %s" % perr)
        return True
    return False


def auto_scan_loop():
    """Background-Loop: pollt den ADF; laeuft gluecklich, wenn Papier eingelegt wird.
    Backoff bei anhaltend leerem Einzug, sofort schnell nach Erfolg."""
    empties = 0
    while True:
        wait = auto_cfg["interval"]
        if empties > 0:
            wait *= min(12, 1 + empties // 4)  # max. ~12x Intervall
        time.sleep(wait)
        if not auto_cfg["enabled"]:
            continue
        if auto_scan_once():
            empties = 0
        else:
            empties += 1


@app.before_request
def auth_check():
    if AUTH_USER and AUTH_PASS:
        a = request.authorization
        if not a or a.username != AUTH_USER or a.password != AUTH_PASS:
            resp = jsonify({"error": "unauthorized"})
            resp.status_code = 401
            resp.headers["WWW-Authenticate"] = 'Basic realm="scanner"'
            return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/devices")
def api_devices():
    return jsonify({"devices": list_devices(force=request.args.get("refresh") == "1")})


@app.route("/api/config")
def api_config():
    ensure_services()
    device = pick_device()
    if not device:
        return jsonify({"device": None, "error": "Kein Scanner gefunden.", "paperless": bool(PAPERLESS_URL)})
    opts, err = device_options(device)
    return jsonify({
        "device": device,
        "options": opts,
        "paperless": bool(PAPERLESS_URL),
        "error": err,
        "autoscan": {
            "enabled": auto_cfg["enabled"],
            "interval": auto_cfg["interval"],
            "state": auto_cfg["state"],
            "last": auto_cfg["last"],
        },
    })


@app.route("/api/autoscan")
def api_autoscan():
    import copy

    return jsonify({"enabled": auto_cfg["enabled"], "interval": auto_cfg["interval"],
                    "paperless": auto_cfg["paperless"],
                    "state": auto_cfg["state"], "last": copy.deepcopy(auto_cfg["last"])})


@app.route("/api/autoscan", methods=["POST"])
def api_autoscan_set():
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        auto_cfg["enabled"] = bool(data["enabled"])
    if "interval" in data:
        try:
            v = float(data["interval"])
            if 1 <= v <= 60:
                auto_cfg["interval"] = v
        except (TypeError, ValueError):
            pass
    if "paperless" in data:
        auto_cfg["paperless"] = bool(data["paperless"])
    return api_autoscan()


@app.route("/api/scan", methods=["POST"])
def api_scan():
    cleanup_old_jobs()
    ensure_services()
    data = request.get_json(silent=True) or {}
    device = (data.get("device") or SCANNER_DEVICE or detect_device())
    if not device:
        return jsonify({"error": "Kein Scanner gefunden."}), 400

    mode = str(data.get("mode") or "Gray")
    resolution = int(data.get("resolution") or 300)
    source = str(data.get("source") or "")
    fmt = str(data.get("format") or "pdf").lower()
    width_mm = float(data.get("width") or 210)
    height_mm = float(data.get("height") or 297)
    session = str(data.get("session") or "")
    duplex = bool(data.get("duplex"))
    if source != "ADF":
        last_ui_settings.update(mode=mode, resolution=resolution, x=width_mm, y=height_mm)

    if source == "ADF":
        last_ui_settings.update(mode=mode, resolution=resolution, x=width_mm, y=height_mm, duplex=duplex)
        ok, dest, err, n_pages = scan_adf(device, mode, resolution, width_mm, height_mm, fmt, duplex)
        if not ok:
            return jsonify({"error": err}), 500
        return jsonify({
            "success": True,
            "file": os.path.basename(dest),
            "url": "/scans/%s" % os.path.basename(dest),
            "adf": True,
            "pages": n_pages,
        })

    if fmt == "pdf":
        scan_fmt = "png"
    else:
        scan_fmt = fmt

    if session and session in jobs:
        job = jobs[session]
    else:
        if fmt == "pdf":
            jdir = tempfile.mkdtemp(dir=JOB_DIR)
            jid = os.path.basename(jdir)
            job = {"id": jid, "device": device, "dir": jdir, "pages": [], "ts": time.time()}
            jobs[jid] = job
            session = jid
        else:
            job = None

    ok, scanfile, err = scan_image(device, mode, resolution, source or None, scan_fmt, width_mm, height_mm)
    if not ok:
        return jsonify({"error": err}), 500

    if fmt == "pdf":
        page = ensure_pdf_page(scanfile)
        job["pages"].append(page)
        jobs[session]["ts"] = time.time()
        return jsonify({
            "success": True,
            "session": session,
            "pages": len(job["pages"]),
            "preview": "/api/preview?session=%s&page=%d" % (session, len(job["pages"])),
        })

    # Single image result
    import shutil

    dest = os.path.join(SCAN_DIR, "scan_%s.%s" % (time.strftime("%Y%m%d_%H%M%S"), fmt))
    shutil.move(scanfile, dest)
    return jsonify({
        "success": True,
        "file": os.path.basename(dest),
        "url": "/scans/%s" % os.path.basename(dest),
        "thumb": "/scans/thumb_%s" % os.path.basename(dest),
    })


@app.route("/api/finish", methods=["POST"])
def api_finish():
    data = request.get_json(silent=True) or {}
    session = str(data.get("session") or "")
    if session not in jobs:
        return jsonify({"error": "Sitzung nicht gefunden."}), 404
    job = jobs.pop(session)
    if not job["pages"]:
        return jsonify({"error": "Keine Seiten vorhanden."}), 400

    dest = os.path.join(SCAN_DIR, "scan_%s.pdf" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        pptos_merge_pdf(job["pages"], dest)
    finally:
        shutil_rmtree(job["dir"])
    return jsonify({
        "success": True,
        "file": os.path.basename(dest),
        "url": "/scans/%s" % os.path.basename(dest),
        "pages": len(job["pages"]),
    })


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    data = request.get_json(silent=True) or {}
    session = str(data.get("session") or "")
    if session in jobs:
        job = jobs.pop(session)
        shutil_rmtree(job["dir"])
    if session in duplex_jobs:
        job = duplex_jobs.pop(session)
        shutil_rmtree(job["dir"])
    return jsonify({"success": True})


def weave_duplex(front_pages, back_pages):
    """Vorder- und Rueckseiten zu je einem Blatt zusammenweben.

    Der 2. ADF-Durchzug (nach dem Wenden des Stapels) liefert die Rueckseiten
    in umgekehrter Reihenfolge, ohne dass der Stapel sortiert werden muss.
    Reversed + paarweise zusammengelegt -> front[0], back[0], front[1], ..."""
    backs = list(reversed(back_pages))
    n = max(len(front_pages), len(backs))
    order = []
    for i in range(n):
        if i < len(front_pages):
            order.append(front_pages[i])
        if i < len(backs):
            order.append(backs[i])
    return order


@app.route("/api/duplex/start", methods=["POST"])
def api_duplex_start():
    """Schritt 1: alle Vorderseiten aus dem ADF ziehen. Ergebnis wird als
    duplex_job vorgemerkt, bis 'back' aufgerufen wird."""
    cleanup_old_jobs()
    ensure_services()
    data = request.get_json(silent=True) or {}
    device = (data.get("device") or SCANNER_DEVICE or detect_device())
    if not device:
        return jsonify({"error": "Kein Scanner gefunden."}), 400

    mode = str(data.get("mode") or "Gray")
    resolution = int(data.get("resolution") or 300)
    width_mm = float(data.get("width") or 210)
    height_mm = float(data.get("height") or 297)
    if lock_busy():
        return jsonify({"error": "Ein anderer Scan laeuft gerade."}), 409

    jdir = tempfile.mkdtemp(dir=JOB_DIR)
    ok, pages, err = adf_scan_pages(device, mode, resolution, width_mm, height_mm, jdir)
    if not ok:
        shutil_rmtree(jdir)
        return jsonify({"error": err}), 500

    jid = os.path.basename(jdir)
    duplex_jobs[jid] = {
        "device": device, "mode": mode, "resolution": resolution,
        "width": width_mm, "height": height_mm,
        "front_pages": pages, "ts": time.time(),
    }
    return jsonify({
        "success": True,
        "session": jid,
        "pages": len(pages),
        "message": "Vorderseiten gescannt. Stapel umdrehen und erneut einlegen, dann Rueckseiten scannen lassen.",
    })


@app.route("/api/duplex/back", methods=["POST"])
def api_duplex_back():
    """Schritt 2: Rueckseiten ziehen, mit Vorderseiten verweben und als PDF
    zusammenfuegen."""
    ensure_services()
    data = request.get_json(silent=True) or {}
    session = str(data.get("session") or "")
    job = duplex_jobs.get(session)
    if not job:
        return jsonify({"error": "Duplex-Session nicht (mehr) vorhanden."}), 404
    if lock_busy():
        return jsonify({"error": "Ein anderer Scan laeuft gerade."}), 409

    jdir2 = tempfile.mkdtemp(dir=JOB_DIR)
    try:
        ok, back_pages, err = adf_scan_pages(
            job["device"], job["mode"], job["resolution"],
            job["width"], job["height"], jdir2)
        if not ok:
            return jsonify({"error": err}), 500

        order = weave_duplex(job["front_pages"], back_pages)
        n_front = len(job["front_pages"])
        dest = os.path.join(SCAN_DIR, "scan_%s.pdf" % time.strftime("%Y%m%d_%H%M%S"))
        try:
            pptos_merge_pdf(order, dest)
        except Exception as e:
            return jsonify({"error": "PDF-Erstellung fehlgeschlagen: %s" % e}), 500

        fn = os.path.basename(dest)
        automsg = ""
        if auto_cfg["paperless"] and PAPERLESS_URL:
            pok, perr, pid = paperless_upload(fn)
            if pok:
                automsg = "paperless (doc %s)" % pid
                log("duplex -> paperless %s (doc %s)" % (fn, pid))
            else:
                log("duplex -> paperless fehlgeschlagen: %s" % perr)
                automsg = "Paperless-Fehler: %s" % perr
        duplex_jobs.pop(session, None)
        return jsonify({
            "success": True,
            "file": fn,
            "url": "/scans/%s" % fn,
            "adf": True,
            "pages": n_front,
            "duplex": True,
            "paperless": automsg,
        })
    finally:
        shutil_rmtree(jdir2)


@app.route("/api/preview")
def api_preview():
    session = request.args.get("session", "")
    page = int(request.args.get("page", 1))
    if session not in jobs:
        abort(404)
    job = jobs[session]
    if page < 1 or page > len(job["pages"]):
        abort(404)
    thumb = ensure_preview(job["pages"][page - 1])
    if not thumb:
        abort(404)
    return send_from_directory(os.path.dirname(thumb), os.path.basename(thumb))


@app.route("/api/paperless/send", methods=["POST"])
def api_paperless_send():
    if not PAPERLESS_URL:
        return jsonify({"error": "Paperless ist nicht konfiguriert."}), 400
    data = request.get_json(silent=True) or {}
    name = os.path.basename(str(data.get("file") or ""))
    title = str(data.get("title") or "").strip()
    created = str(data.get("created") or "").strip()

    ok, err, doc_id = paperless_upload(name, title, created)
    if ok:
        return jsonify({"success": True, "id": doc_id, "file": name})
    if err == "Datei nicht gefunden.":
        return jsonify({"error": err}), 404
    return jsonify({"error": err}), 502


@app.route("/scans/<path:name>")
def scans(name):
    if name.startswith("thumb_"):
        real = name[len("thumb_"):]
        full = os.path.join(SCAN_DIR, real)
        if os.path.exists(full):
            thumb = ensure_preview(full)
            if not thumb:
                abort(404)
            resp = send_from_directory(os.path.dirname(thumb), os.path.basename(thumb))
            resp.headers["Cache-Control"] = "public, max-age=3600"
            return resp
        abort(404)
    resp = send_from_directory(SCAN_DIR, name)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@app.route("/scans/")
def scan_history():
    try:
        rows = scan_history_sync()
        return jsonify({"files": [
            {"name": n, "size": s, "mtime": m, "pages": p, "thumb": t == 1,
             "paperless_id": pl_id, "paperless_ts": pl_ts}
            for (n, s, m, p, t, pl_id, pl_ts) in rows
        ]})
    except Exception as e:
        log("catalog sync failed: %s" % e)
        files = []
        for f in sorted(os.listdir(SCAN_DIR), reverse=True):
            if f.startswith("."):
                continue
            p = os.path.join(SCAN_DIR, f)
            files.append({"name": f, "size": os.path.getsize(p), "mtime": os.path.getmtime(p),
                          "pages": None, "thumb": False, "paperless_id": None, "paperless_ts": None})
        return jsonify({"files": files})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json(silent=True) or {}
    name = os.path.basename(str(data.get("file") or ""))
    if not name or name.startswith("."):
        return jsonify({"error": "Ungueltiger Dateiname."}), 400
    full = os.path.join(SCAN_DIR, name)
    if not os.path.isfile(full):
        return jsonify({"error": "Datei nicht gefunden."}), 404
    try:
        os.remove(full)
        key_prefix = name.replace(".", "_")
        for t in os.listdir(THUMB_DIR):
            if key_prefix + "_" in t:
                try:
                    os.remove(os.path.join(THUMB_DIR, t))
                except OSError:
                    pass
        c = db()
        c.execute("DELETE FROM files WHERE name=?", (name,))
        c.commit()
        c.close()
        log("deleted scan: %s" % name)
        return jsonify({"success": True, "file": name})
    except OSError as e:
        return jsonify({"error": "Loeschen fehlgeschlagen: %s" % e}), 500


@app.route("/gallery")
def gallery():
    return render_template("gallery.html")


if __name__ == "__main__":
    ensure_services()
    dev = SCANNER_DEVICE or detect_device()
    log("active device: %s" % (dev or "NICHT GEFUNDEN"))
    if dev:
        t = threading.Thread(target=_opt_cache_warm, args=(dev,), daemon=True)
        t.start()
        threading.Thread(target=auto_scan_loop, daemon=True).start()
        threading.Thread(target=preview_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=False)