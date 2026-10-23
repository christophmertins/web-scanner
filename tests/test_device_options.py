"""Regressionstests fuer das Einlesen der Geraeteoptionen.

Hintergrund: Ist der Scanner gerade belegt (z. B. waehrend eines ADF-Scans),
liefert `scanimage -d <dev> --help` den Rueckgabecode 0, aber nur den
generischen Usage-Text statt der Geraeteoptionen. Wurde das als gueltiges
Ergebnis gecacht, fehlten in /api/config die Quellen (ADF) fuer eine Stunde -
und damit auch der Duplex-Knopf.
"""
import json
import os
import time

GENERIC_USAGE = """Usage: scanimage [OPTION]...

Start image acquisition on a scanner device and write image data to
standard output.

-d, --device-name=DEVICE   use a given scanner device (e.g. hp:/dev/scanner)
-L, --list-devices         show available scanner devices
"""

REAL_OPTIONS = """Output format is not set, using pnm as a default.

All options specific to device `test:dev':
    --mode Lineart|Gray|Color [Color]
        Selects the scan mode (e.g. lineart, monochrome, or color).
    --resolution 100|150|200|300|600dpi [100]
        Sets the resolution of the scanned image.
    --source Auto|Flatbed|ADF [Auto] [advanced]
        Selects the scan source (such as a document-feeder).
    -x 0..215.9mm [215.9]
        Width of scan-area.
    -y 0..381mm [381]
        Height of scan-area.
"""


def _fake_run(cmd, timeout=0):
    return 0, GENERIC_USAGE, ("Output format is not set, using pnm as a default.\n"
                              "scanimage: open of device test:dev failed: Invalid argument")


def _real_run(cmd, timeout=0):
    return 0, REAL_OPTIONS, "Output format is not set, using pnm as a default."


def _reset(scanner, monkeypatch, run=None):
    monkeypatch.setattr(scanner, "run", run or _fake_run)
    scanner._opt_cache = {"device": None, "ts": 0.0, "opts": None}
    if os.path.exists(scanner.OPT_CACHE_FILE):
        os.remove(scanner.OPT_CACHE_FILE)


def test_belegter_scanner_wird_nicht_gecacht(scanner, monkeypatch):
    _reset(scanner, monkeypatch)
    opts, err = scanner.device_options("test:dev")

    assert err, "Belegter Scanner muss als Fehler gemeldet werden"
    assert not opts.get("sources")
    assert not os.path.exists(scanner.OPT_CACHE_FILE), "Leere Optionen duerfen nicht gecacht werden"


def test_naechster_probe_liefert_adf(scanner, monkeypatch):
    _reset(scanner, monkeypatch)
    scanner.device_options("test:dev")  # belegt -> Fehler, kein Cache

    monkeypatch.setattr(scanner, "run", _real_run)
    opts, err = scanner.device_options("test:dev")

    assert err is None
    assert "ADF" in opts["sources"]
    assert os.path.exists(scanner.OPT_CACHE_FILE)


def test_vergifteter_cache_auf_platte_wird_ignoriert(scanner, monkeypatch):
    _reset(scanner, monkeypatch, run=_real_run)
    with open(scanner.OPT_CACHE_FILE, "w") as f:
        json.dump({"device": "test:dev", "ts": time.time(),
                   "opts": {"modes": [], "resolutions": [], "sources": [],
                            "max_x": None, "max_y": None}}, f)
    scanner._opt_cache = {"device": None, "ts": 0.0, "opts": None}

    opts, err = scanner.device_options("test:dev")

    assert err is None, "Statt leerem Cache muss neu gelesen werden"
    assert "ADF" in opts["sources"]


def test_guter_cache_ueberlebt_belegten_probe(scanner, monkeypatch):
    _reset(scanner, monkeypatch, run=_real_run)
    scanner.device_options("test:dev")  # guter Cache
    cached = json.load(open(scanner.OPT_CACHE_FILE))

    # Speicher-Cache und Platten-Cache "altern" lassen, dann belegter Probe
    scanner._opt_cache["ts"] = time.time() - scanner.OPT_CACHE_TTL - 1
    os.remove(scanner.OPT_CACHE_FILE)
    monkeypatch.setattr(scanner, "run", _fake_run)
    opts, err = scanner.device_options("test:dev")

    assert err
    assert not os.path.exists(scanner.OPT_CACHE_FILE), "Guter Cache darf nicht durch leeren ersetzt werden"
    assert "ADF" in cached["opts"]["sources"]
