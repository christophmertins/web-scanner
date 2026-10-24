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
from flask import Flask, request, jsonify, render_template, send_from_directory, abort, Response
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
duplex_results = {}  # job_id -> fertiges Ergebnis, bis die UI es abgeholt hat
DEVICE_CACHE = {"ts": 0, "list": []}
OPT_CACHE_FILE = os.path.join(SCAN_DIR, ".device_options.json")
OPT_CACHE_TTL = 3600
OPT_DISK_TTL = 7 * 86400  # Geraeteoptionen aendern sich praktisch nie
# Zeit, die eine Duplex-Session (Vorderseiten gescannt) auf die Rueckseiten wartet.
# Danach verfaellt sie, damit der Autoscan nicht dauerhaft blockiert bleibt.
DUPLEX_TTL = int(os.environ.get("DUPLEX_TTL", "900"))
# Wie lange ein fertiges Duplex-Ergebnis fuer die UI abrufbar bleibt
DUPLEX_RESULT_TTL = int(os.environ.get("DUPLEX_RESULT_TTL", "600"))
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

AUTO_STATE_FILE = os.path.join(SCAN_DIR, ".autoscan.json")


def _load_auto_state():
    """Vom Nutzer gesetzten Autoscan-Zustand ueber Neustarts hinweg behalten."""
    try:
        with open(AUTO_STATE_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    if "enabled" in data:
        auto_cfg["enabled"] = bool(data["enabled"])
    if "paperless" in data:
        auto_cfg["paperless"] = bool(data["paperless"])
    try:
        if "interval" in data:
            auto_cfg["interval"] = float(data["interval"])
    except (TypeError, ValueError):
        pass


def _save_auto_state():
    try:
        with open(AUTO_STATE_FILE, "w") as f:
            json.dump({"enabled": auto_cfg["enabled"],
                       "interval": auto_cfg["interval"],
                       "paperless": auto_cfg["paperless"]}, f)
    except Exception as e:
        log("autoscan state save failed: %s" % e)


_load_auto_state()

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
        devs = [{"id": detect_device(), "desc": "Netzwerk-Scanner"}],
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


def _options_usable(opts):
    """Wurden ueberhaupt Geraeteoptionen erkannt?

    Ist der Scanner belegt (z. B. waehrend eines ADF-Scans), antwortet
    `scanimage --help` mit rc=0, aber nur mit dem generischen Usage-Text.
    Solch ein leeres Ergebnis darf nie gecacht werden, sonst fehlen Quellen
    (ADF) und damit der Duplex-Knopf fuer die ganze Cache-Laufzeit."""
    if not opts:
        return False
    return bool(opts.get("modes") or opts.get("resolutions") or opts.get("sources")
                or opts.get("max_x") or opts.get("max_y"))


def _opt_cache_valid(cache, device, ttl):
    return (cache["device"] == device and _options_usable(cache["opts"])
            and time.time() - cache["ts"] < ttl)


def _opt_cache_load_disk(device):
    """Wiederherstellen des Optionen- und Geräte-Caches aus der Datei (überlebt Restarts)."""
    global _opt_cache
    try:
        with open(OPT_CACHE_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    now = time.time()
    if (data.get("device") == device and _options_usable(data.get("opts"))
            and now - data.get("ts", 0) < OPT_DISK_TTL):
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

    # Selbst ein alter Cache ist besser als gar keine Optionen (ADF/Duplex!).
    cached = None
    if _opt_cache.get("device") == device and _options_usable(_opt_cache.get("opts")):
        cached = _opt_cache["opts"]

    # Probe nur, wenn der Scanner frei ist. Sonst liefert `scanimage --help`
    # rc=0 mit dem generischen Usage-Text - und waehrend eines Scans ist das
    # Geraet praktisch immer belegt (Autoscan-Polling).
    got_lock = scan_lock.acquire(timeout=5)
    if not got_lock:
        if cached:
            log("scanner belegt - nutze gecachte Geraeteoptionen")
            return cached, None
        return {}, "Scanner belegt - Geraeteoptionen konnten nicht gelesen werden."
    try:
        rc, out, err = run(["scanimage", "-d", device, "--help"], timeout=20)
    finally:
        scan_lock.release()

    if rc != 0:
        if cached:
            return cached, None
        return {}, "scanimage --help failed: %s" % err.strip()

    opts = parse_device_options(out)
    if not _options_usable(opts):
        log("scanimage --help ohne Geraeteoptionen: %s" % err.strip())
        if cached:
            return cached, None
        return {}, "Scanner belegt? Geraeteoptionen nicht lesbar (%s)" % (err.strip() or "keine Angabe")

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


def prune_duplex_jobs(max_age=None):
    """Wartende Duplex-Sessions (Vorderseiten liegen bereit) verfallen lassen.

    Ohne Ablauf blockiert eine vergessene Session den Autoscan dauerhaft:
    auto_scan_once steigt aus, solange duplex_jobs gefuellt ist."""
    max_age = DUPLEX_TTL if max_age is None else max_age
    now = time.time()
    for jid, j in list(duplex_jobs.items()):
        if now - j.get("ts", 0) > max_age:
            # Job-Dicts fuehren kein "dir"; die Seiten liegen unter JOB_DIR/<jid>.
            shutil_rmtree(j.get("dir") or os.path.join(JOB_DIR, jid))
            duplex_jobs.pop(jid, None)
            log("duplex session %s abgelaufen" % jid)
    for jid, res in list(duplex_results.items()):
        if now - res.get("ts", 0) > DUPLEX_RESULT_TTL:
            duplex_results.pop(jid, None)


def pending_duplex():
    """Die juengste wartende Duplex-Session (oder None)."""
    prune_duplex_jobs()
    if not duplex_jobs:
        return None
    jid, job = max(duplex_jobs.items(), key=lambda kv: kv[1].get("ts", 0))
    return {
        "session": jid,
        "pages": len(job.get("front_pages") or []),
        "age": int(time.time() - job.get("ts", 0)),
        "ttl": DUPLEX_TTL,
    }


def cleanup_old_jobs(max_age=3600):
    now = time.time()
    for jid, j in list(jobs.items()):
        if now - j["ts"] > max_age:
            try:
                shutil_rmtree(j["dir"])
            except Exception:
                pass
            jobs.pop(jid, None)
    prune_duplex_jobs(max_age)


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


def auto_paperless(name):
    """Fertiges PDF automatisch nach Paperless schieben (wenn konfiguriert).

    Returns Meldung fuer die UI: "" (deaktiviert), "paperless (doc <id>)"
    oder "Paperless-Fehler: <text>"."""
    if not name or not name.lower().endswith(".pdf"):
        return ""
    if not (auto_cfg["paperless"] and PAPERLESS_URL):
        return ""
    ok, err, doc_id = paperless_upload(name)
    if ok:
        log("auto -> paperless %s (doc %s)" % (name, doc_id))
        return "paperless (doc %s)" % doc_id
    log("auto -> paperless fehlgeschlagen: %s" % err)
    return "Paperless-Fehler: %s" % err


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


def wait_scan_free(timeout=25):
    """Wartet, bis die Scansperre frei ist (z. B. Autoscan laeuft gerade).
    Returns True wenn frei, False bei Timeout."""
    t0 = time.time()
    while lock_busy():
        if time.time() - t0 > timeout:
            return False
        time.sleep(0.5)
    return True


def auto_scan_once():
    """Ein ADF-Versuch aus dem Autoscan-Loop. Wenn Papier da ist, wir der ganze
    Stapel als PDF gescannt; sonst passiert nichts."""
    device = pick_device()
    if not device or lock_busy():
        return False
    prune_duplex_jobs()
    if duplex_jobs:
        # Pseudo-Duplex laeuft (zwischen Vorder- und Rueckseiten): nicht
        # eingreifen, sonst wird der gewendete Stapel weggescannt.
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
        auto_paperless(fn)
        return True
    return False


def auto_scan_loop():
    """Background-Loop: pollt den ADF; laeuft gluecklich, wenn Papier eingelegt wird.
    Backoff bei anhaltend leerem Einzug, sofort schnell nach Erfolg.

    Liegt eine Duplex-Session auf Rueckseiten, hat sie Vorrang: sobald Papier
    im Einzug liegt, wird der zweite Durchzug automatisch gestartet."""
    empties = 0
    while True:
        wait = auto_cfg["interval"]
        if empties > 0:
            wait *= min(12, 1 + empties // 4)  # max. ~12x Intervall
        time.sleep(wait)
        prune_duplex_jobs()
        if duplex_jobs:
            if auto_back_once():
                empties = 0
            continue
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


@app.route("/api/health")
def api_health():
    health = {"status": "ok", "timestamp": time.time(), "scanner": "unknown", "catalog": "ok"}
    try:
        c = db()
        c.execute("SELECT 1")
        c.close()
    except Exception as e:
        health["catalog"] = "error: %s" % e
        health["status"] = "degraded"
    device = pick_device()
    if device:
        health["scanner"] = device
        if lock_busy():
            health["scanner_busy"] = True
    else:
        health["scanner"] = "not found"
        health["status"] = "degraded"
    return jsonify(health)


@app.route("/api/ocr", methods=["POST"])
def api_ocr():
    data = request.get_json(silent=True) or {}
    name = os.path.basename(str(data.get("file") or ""))
    lang = str(data.get("lang") or "deu+eng").strip()
    if not name:
        return jsonify({"error": "Kein Dateiname angegeben."}), 400
    full = os.path.join(SCAN_DIR, name)
    if not os.path.isfile(full):
        return jsonify({"error": "Datei nicht gefunden: %s" % name}), 404
    if not lang:
        lang = "deu+eng"
    tmp = None
    try:
        if name.lower().endswith(".pdf"):
            tmp = tempfile.mktemp(suffix=".png", dir=JOB_DIR)
            rc, _, err = run(["pdftoppm", "-jpeg", "-r", "300", "-f", "1", "-l", "1", full, tmp[:-4]], timeout=60)
            if rc != 0 or not glob.glob(tmp[:-4] + "*.jpg"):
                return jsonify({"error": "PDF-Konvertierung fehlgeschlagen: %s" % (err.strip() or "unbekannt")}), 500
            inputs = sorted(glob.glob(tmp[:-4] + "*.jpg"))
        else:
            inputs = [full]
        all_text = []
        total_conf = 0.0
        total_chars = 0
        for img_path in inputs:
            out_base = tempfile.mktemp(suffix="", dir=JOB_DIR)
            cmd = ["tesseract", img_path, out_base, "-l", lang, "--oem", "1", "--psm", "3",
                   "tsv", "txt"]
            rc, so, err = run(cmd, timeout=120)
            if rc != 0:
                return jsonify({"error": "Tesseract fehlgeschlagen: %s" % (err.strip() or "rc=%s" % rc)}), 500
            txt_path = out_base + ".txt"
            tsv_path = out_base + ".tsv"
            text = ""
            if os.path.exists(txt_path):
                with open(txt_path) as f:
                    text = f.read().strip()
                try:
                    os.remove(txt_path)
                except OSError:
                    pass
            conf = 0.0
            chars = 0
            if os.path.exists(tsv_path):
                try:
                    with open(tsv_path) as f:
                        for line in f:
                            parts = line.strip().split("\t")
                            if len(parts) >= 11 and parts[0] == "word":
                                c = float(parts[10]) if parts[10] != "-1" else 0.0
                                l = len(parts[11]) if len(parts) > 11 else 0
                                if l > 0:
                                    conf += c * l
                                    chars += l
                except Exception:
                    pass
                try:
                    os.remove(tsv_path)
                except OSError:
                    pass
            all_text.append(text)
            total_conf += conf
            total_chars += chars
        full_text = "\n\n".join(t for t in all_text if t)
        avg_conf = (total_conf / total_chars) if total_chars > 0 else 0.0
        return jsonify({
            "success": True,
            "file": name,
            "lang": lang,
            "pages": len(inputs),
            "text": full_text,
            "confidence": round(avg_conf, 1),
            "chars": len(full_text),
        })
    finally:
        if tmp:
            prefix = tmp[:-4]
            for f in glob.glob(prefix + "*"):
                try:
                    os.remove(f)
                except OSError:
                    pass


SWAGGER_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Web-Scanner – API Dokumentation</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
<style>body{margin:0;background:#0f141b} .topbar{display:none}</style>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({
  url: "/api/openapi.json",
  dom_id: "#swagger-ui",
  theme: "dark",
  deepLinking: true,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  layout: "BaseLayout"
});
</script>
</body>
</html>"""


@app.route("/api/docs")
def api_docs():
    return Response(SWAGGER_HTML, content_type="text/html; charset=utf-8")


@app.route("/api/openapi.json")
def api_openapi():
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "Web-Scanner API",
            "version": "1.0.0",
            "description": "Web-API fuer den Netzwerk-Scanner (SANE/HPLIP). "
                           "Scans als PDF/JPEG/PNG, ADF, Duplex, Paperless-Upload, OCR.",
        },
        "servers": [{"url": "/"}],
        "paths": {
            "/api/health": {
                "get": {
                    "summary": "Health-Check",
                    "operationId": "health",
                    "tags": ["System"],
                    "responses": {"200": {"description": "Service ist erreichbar"}},
                },
            },
            "/api/config": {
                "get": {
                    "summary": "Scanner-Konfiguration abrufen",
                    "operationId": "getConfig",
                    "tags": ["Scanner"],
                    "responses": {"200": {"description": "Geräteoptionen und Status"}},
                },
            },
            "/api/devices": {
                "get": {
                    "summary": "Gefundene SANE-Geräte auflisten",
                    "operationId": "listDevices",
                    "tags": ["Scanner"],
                    "parameters": [
                        {"name": "refresh", "in": "query", "schema": {"type": "string", "enum": ["0", "1"]},
                         "description": "Cache ignorieren und Neu-Suche starten"},
                    ],
                    "responses": {"200": {"description": "Geräteliste"}},
                },
            },
            "/api/scan": {
                "post": {
                    "summary": "Scan starten (Einzelseite oder ADF)",
                    "operationId": "scan",
                    "tags": ["Scan"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "device": {"type": "string", "description": "SANE-Device-URI"},
                                        "mode": {"type": "string", "enum": ["Color", "Gray", "Lineart"], "default": "Gray"},
                                        "resolution": {"type": "integer", "default": 300},
                                        "source": {"type": "string", "enum": ["Flatbed", "ADF", "Auto"], "default": "Flatbed"},
                                        "format": {"type": "string", "enum": ["pdf", "jpeg", "png"], "default": "pdf"},
                                        "width": {"type": "number", "default": 210},
                                        "height": {"type": "number", "default": 297},
                                        "session": {"type": "string", "description": "Session-ID fuer mehrseitigen PDF-Scan"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {
                        "200": {"description": "Scan erfolgreich (Datei oder Session-Fortschritt)"},
                        "400": {"description": "Kein Scanner gefunden"},
                        "500": {"description": "Scan fehlgeschlagen"},
                    },
                },
            },
            "/api/finish": {
                "post": {
                    "summary": "Mehrseitigen PDF-Scan abschliessen",
                    "operationId": "finishPdf",
                    "tags": ["Scan"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"session": {"type": "string"}}}}},
                    },
                    "responses": {"200": {"description": "PDF-Datei erstellt"}, "404": {"description": "Session nicht gefunden"}},
                },
            },
            "/api/cancel": {
                "post": {
                    "summary": "Scan-Jobs abbrechen",
                    "operationId": "cancelJob",
                    "tags": ["Scan"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"session": {"type": "string"}}}}},
                    },
                    "responses": {"200": {"description": "Abgebrochen"}},
                },
            },
            "/api/duplex/start": {
                "post": {
                    "summary": "Duplex-Scan: Vorderseiten aus dem ADF ziehen",
                    "operationId": "duplexStart",
                    "tags": ["Scan"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "device": {"type": "string"},
                                        "mode": {"type": "string"},
                                        "resolution": {"type": "integer"},
                                        "width": {"type": "number"},
                                        "height": {"type": "number"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Vorderseiten gescannt"}, "409": {"description": "Scanner beschaeftigt"}},
                },
            },
            "/api/duplex/back": {
                "post": {
                    "summary": "Duplex-Scan: Rueckseiten scannen und mit Vorderseiten verweben",
                    "operationId": "duplexBack",
                    "tags": ["Scan"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"session": {"type": "string"}}}}},
                    },
                    "responses": {"200": {"description": "Duplex-PDF erstellt"}, "404": {"description": "Session nicht gefunden"}},
                },
            },
            "/api/duplex/status": {
                "get": {
                    "summary": "Status einer Duplex-Session (waiting/scanning/done)",
                    "operationId": "duplexStatus",
                    "tags": ["Scan"],
                    "parameters": [{"name": "session", "in": "query", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Status der Session"}, "404": {"description": "Session unbekannt oder abgelaufen"}},
                },
            },
            "/api/preview_scan": {
                "post": {
                    "summary": "Schnell-Vorschau (Low-Res-Scan)",
                    "operationId": "previewScan",
                    "tags": ["Scan"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "device": {"type": "string"},
                                        "width": {"type": "number"},
                                        "height": {"type": "number"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Vorschaubild"}, "409": {"description": "Scanner beschaeftigt"}},
                },
            },
            "/api/preview": {
                "get": {
                    "summary": "Vorschaubild einer PDF-Seite",
                    "operationId": "preview",
                    "tags": ["Scan"],
                    "parameters": [
                        {"name": "session", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}},
                    ],
                    "responses": {"200": {"description": "JPEG-Vorschaubild"}, "404": {"description": "Nicht gefunden"}},
                },
            },
            "/api/preview_img": {
                "get": {
                    "summary": "Temporaeres Vorschaubild abrufen",
                    "operationId": "previewImg",
                    "tags": ["Scan"],
                    "parameters": [
                        {"name": "file", "in": "query", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "Bild"}, "404": {"description": "Nicht gefunden"}},
                },
            },
            "/api/autoscan": {
                "get": {
                    "summary": "Autoscan-Status abrufen",
                    "operationId": "getAutoscan",
                    "tags": ["Autoscan"],
                    "responses": {"200": {"description": "Status und letzter Scan"}},
                },
                "post": {
                    "summary": "Autoscan konfigurieren",
                    "operationId": "setAutoscan",
                    "tags": ["Autoscan"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "enabled": {"type": "boolean"},
                                        "interval": {"type": "number", "minimum": 1, "maximum": 60},
                                        "paperless": {"type": "boolean"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Aktualisierter Status"}},
                },
            },
            "/api/paperless/send": {
                "post": {
                    "summary": "Datei an Paperless-ngx senden",
                    "operationId": "paperlessSend",
                    "tags": ["Paperless"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["file"],
                                    "properties": {
                                        "file": {"type": "string", "description": "Dateiname im /scans-Verzeichnis"},
                                        "title": {"type": "string", "description": "Titel in Paperless"},
                                        "created": {"type": "string", "description": "ISO-8601 Datum"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Erfolgreich gesendet"}, "404": {"description": "Datei nicht gefunden"}, "502": {"description": "Paperless-Fehler"}},
                },
            },
            "/api/ocr": {
                "post": {
                    "summary": "OCR-Texterkennung (tesseract)",
                    "operationId": "ocr",
                    "tags": ["OCR"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["file"],
                                    "properties": {
                                        "file": {"type": "string", "description": "Dateiname im /scans-Verzeichnis"},
                                        "lang": {"type": "string", "default": "deu+eng",
                                                  "description": "tesseract-Sprache(n), z.B. deu, eng, deu+eng"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {
                        "200": {"description": "Erkannter Text mit Konfidenz"},
                        "404": {"description": "Datei nicht gefunden"},
                        "500": {"description": "OCR fehlgeschlagen"},
                    },
                },
            },
            "/api/delete": {
                "post": {
                    "summary": "Scan loeschen",
                    "operationId": "deleteScan",
                    "tags": ["Galerie"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"file": {"type": "string"}}}}},
                    },
                    "responses": {"200": {"description": "Geloescht"}, "404": {"description": "Nicht gefunden"}},
                },
            },
            "/scans/": {
                "get": {
                    "summary": "Alle Scans als JSON-Liste",
                    "operationId": "listScans",
                    "tags": ["Galerie"],
                    "responses": {"200": {"description": "Dateiliste mit Metadaten"}},
                },
            },
            "/gallery": {
                "get": {
                    "summary": "Galerie-Seite (HTML)",
                    "operationId": "gallery",
                    "tags": ["UI"],
                    "responses": {"200": {"description": "HTML-Seite"}},
                },
            },
        },
    }
    return jsonify(spec)


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
    opts, opts_err = device_options(device)
    return jsonify({
        "device": device,
        "options": opts,
        "options_error": opts_err,
        "paperless": bool(PAPERLESS_URL),
        "error": None,
        "duplex": pending_duplex(),
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
                    "paused": "duplex" if pending_duplex() else None,
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
    _save_auto_state()
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
    if source != "ADF":
        last_ui_settings.update(mode=mode, resolution=resolution, x=width_mm, y=height_mm)

    if source == "ADF":
        last_ui_settings.update(mode=mode, resolution=resolution, x=width_mm, y=height_mm)
        ok, dest, err, n_pages = scan_adf(device, mode, resolution, width_mm, height_mm, fmt)
        if not ok:
            return jsonify({"error": err}), 500
        fn = os.path.basename(dest)
        return jsonify({
            "success": True,
            "file": fn,
            "url": "/scans/%s" % fn,
            "adf": True,
            "pages": n_pages,
            "paperless": auto_paperless(fn),
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
    fn = os.path.basename(dest)
    return jsonify({
        "success": True,
        "file": fn,
        "url": "/scans/%s" % fn,
        "pages": len(job["pages"]),
        "paperless": auto_paperless(fn),
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
    pending = pending_duplex()
    if pending:
        return jsonify({
            "error": "Duplex-Session läuft bereits: erst Rückseiten scannen oder abbrechen.",
            **pending,
        }), 409
    if not wait_scan_free():
        return jsonify({"error": "Scanner ist noch beschaeftigt, bitte erneut versuchen."}), 409

    jdir = tempfile.mkdtemp(dir=JOB_DIR)
    ok, pages, err = adf_scan_pages(device, mode, resolution, width_mm, height_mm, jdir)
    if not ok:
        shutil_rmtree(jdir)
        return jsonify({"error": err}), 500

    jid = os.path.basename(jdir)
    duplex_jobs[jid] = {
        "device": device, "mode": mode, "resolution": resolution,
        "width": width_mm, "height": height_mm,
        "front_pages": pages, "ts": time.time(), "state": "waiting",
    }
    return jsonify({
        "success": True,
        "session": jid,
        "pages": len(pages),
        "message": "Vorderseiten gescannt. Stapel umdrehen und einlegen - die Rueckseiten werden automatisch gescannt.",
    })


def finish_duplex(session):
    """Rueckseiten ziehen, mit Vorderseiten verweben, PDF bauen und hochladen.

    Returns (payload, http_status). Bei leerem ADF bleibt die Session bestehen
    (state wieder "waiting"), damit der Auto-Start weiter versuchen kann."""
    job = duplex_jobs.get(session)
    if not job:
        return {"error": "Duplex-Session nicht (mehr) vorhanden.", "phase": "gone"}, 404
    if job.get("state") == "scanning":
        return {"error": "Rückseiten werden bereits gescannt.", "phase": "scanning"}, 409

    job["state"] = "scanning"
    jdir2 = tempfile.mkdtemp(dir=JOB_DIR)
    try:
        ok, back_pages, err = adf_scan_pages(
            job["device"], job["mode"], job["resolution"],
            job["width"], job["height"], jdir2)
        if not ok:
            job["state"] = "waiting"
            return {"error": err, "phase": "waiting"}, 500

        order = weave_duplex(job["front_pages"], back_pages)
        n_front = len(job["front_pages"])
        dest = os.path.join(SCAN_DIR, "scan_%s.pdf" % time.strftime("%Y%m%d_%H%M%S"))
        try:
            pptos_merge_pdf(order, dest)
        except Exception as e:
            job["state"] = "waiting"
            return {"error": "PDF-Erstellung fehlgeschlagen: %s" % e, "phase": "waiting"}, 500

        fn = os.path.basename(dest)
        duplex_jobs.pop(session, None)
        payload = {
            "success": True,
            "file": fn,
            "url": "/scans/%s" % fn,
            "adf": True,
            "pages": n_front,
            "duplex": True,
            "paperless": auto_paperless(fn),
        }
        duplex_results[session] = dict(payload, ts=time.time())
        log("duplex fertig: %s (%d Blaetter)" % (fn, n_front))
        return payload, 200
    finally:
        shutil_rmtree(jdir2)


def auto_back_once():
    """Rueckseiten automatisch ziehen, sobald Papier im ADF liegt.

    Wird vom Hintergrund-Loop aufgerufen, solange eine Duplex-Session auf ihre
    Rueckseiten wartet. Leerer Einzug ist kein Fehler: dann bleibt die Session
    wartend und der naechste Versuch kommt nach dem Backoff."""
    pending = pending_duplex()
    if not pending or lock_busy():
        return False
    payload, status = finish_duplex(pending["session"])
    if status == 200:
        return True
    return False


@app.route("/api/duplex/status")
def api_duplex_status():
    """Status einer Duplex-Session fuer die UI (wartend/scannend/fertig)."""
    session = str(request.args.get("session") or "")
    prune_duplex_jobs()
    res = duplex_results.get(session)
    if res and time.time() - res.get("ts", 0) < DUPLEX_RESULT_TTL:
        return jsonify(dict(res, phase="done"))
    job = duplex_jobs.get(session)
    if job:
        return jsonify({
            "phase": job.get("state") or "waiting",
            "session": session,
            "pages": len(job.get("front_pages") or []),
            "ttl": DUPLEX_TTL,
        })
    return jsonify({"phase": "gone", "session": session}), 404


@app.route("/api/duplex/back", methods=["POST"])
def api_duplex_back():
    """Schritt 2 manuell ausloesen (der Auto-Start macht das normalerweise
    selbst, sobald Papier im Einzug liegt)."""
    ensure_services()
    data = request.get_json(silent=True) or {}
    session = str(data.get("session") or "")
    if session not in duplex_jobs:
        return jsonify({"error": "Duplex-Session nicht (mehr) vorhanden."}), 404
    if not wait_scan_free():
        return jsonify({"error": "Scanner ist noch beschaeftigt, bitte erneut versuchen."}), 409

    payload, status = finish_duplex(session)
    if status == 500:
        payload["error"] = payload.get("error") or "Rückseiten konnten nicht gescannt werden."
    return jsonify(payload), status


def rgb_to_png(path):
    """Wandelt ein Bild nach RGB-PNG (fuer PDF-Zusammenbau / Vorschau)."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        png = tempfile.mktemp(suffix=".png", dir=JOB_DIR)
        im.save(png, "PNG")
        return png


@app.route("/api/preview_scan", methods=["POST"])
def api_preview_scan():
    """Schnell-Vorschau vor dem Scan: Low-Res-Flatbed-Scan, wird als Bild
    zurueckgegeben. Kein Live-Stream, aber schneller Blick auf den Inhalt."""
    if lock_busy():
        return jsonify({"error": "Scanner ist gerade beschaeftigt, bitte kurz warten."}), 409
    ensure_services()
    data = request.get_json(silent=True) or {}
    device = (data.get("device") or SCANNER_DEVICE or detect_device())
    if not device:
        return jsonify({"error": "Kein Scanner gefunden."}), 400

    width_mm = float(data.get("width") or 210)
    height_mm = float(data.get("height") or 297)

    # Vorschau nur als PNG; Low-Res (72 dpi) fuer Schnelligkeit.
    png = tempfile.mktemp(suffix=".png", dir=JOB_DIR)
    cmd = ["scanimage", "-d", device, "--resolution", "72",
           "--mode", "Color",
           "--compression", "None",
           "-x", "%.2f" % width_mm, "-y", "%.2f" % height_mm,
           "--format", "png", "-o", png]
    log("preview running: %s" % " ".join(cmd))
    with scan_lock:
        rc, so, err = run(cmd, timeout=SCAN_TIMEOUT)
    if rc != 0:
        if os.path.exists(png):
            os.remove(png)
        return jsonify({"error": err.strip() or "Vorschau-Scan fehlgeschlagen (rc=%s)" % rc}), 500
    if not os.path.exists(png) or os.path.getsize(png) == 0:
        return jsonify({"error": "Vorschau-Scan lieferte kein Bild."}), 500

    try:
        thumb = ensure_preview(png)
    except Exception as e:
        thumb = None
    if not thumb:
        try:
            thumb = rgb_to_png(png)
        except Exception:
            thumb = png
    return jsonify({
        "success": True,
        "thumb": "/api/preview_img?file=%s" % os.path.basename(thumb),
    })


@app.route("/api/preview_img")
def api_preview_img():
    """Liefert ein temporaeres Vorschaubild (auch aus JOB_DIR)."""
    name = os.path.basename(request.args.get("file", ""))
    for base in (JOB_DIR, THUMB_DIR):
        full = os.path.join(base, name)
        if os.path.isfile(full):
            return send_from_directory(base, name)
    abort(404)


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