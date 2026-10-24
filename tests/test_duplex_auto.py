"""Tests fuer den automatischen Rueckseiten-Scan und den Auto-Upload."""
import os
import time


def _make_job(scanner, fronts=("page1.png", "page2.png")):
    jid = "auto%d" % int(time.time() * 1000)
    jdir = os.path.join(scanner.JOB_DIR, jid)
    os.makedirs(jdir, exist_ok=True)
    paths = []
    for p in fronts:
        path = os.path.join(jdir, p)
        with open(path, "w") as f:
            f.write("x")
        paths.append(path)
    scanner.duplex_jobs[jid] = {
        "device": "test:dev", "mode": "Gray", "resolution": 300,
        "width": 210.0, "height": 297.0,
        "front_pages": paths, "ts": time.time(), "state": "waiting",
    }
    return jid, jdir


def test_leerer_einzug_laesst_session_warten(scanner, monkeypatch):
    scanner.duplex_jobs.clear()
    jid, _ = _make_job(scanner)
    monkeypatch.setattr(scanner, "adf_scan_pages",
                        lambda *a, **k: (False, None, "Keine Seiten gescannt. Ist Papier im ADF?"))

    payload, status = scanner.finish_duplex(jid)

    assert status == 500
    assert payload["phase"] == "waiting"
    assert jid in scanner.duplex_jobs, "Session muss fuer den naechsten Versuch bleiben"
    assert scanner.duplex_jobs[jid]["state"] == "waiting"


def test_rueckseiten_werden_verwebt_und_hochgeladen(scanner, monkeypatch):
    scanner.duplex_jobs.clear()
    scanner.duplex_results.clear()
    jid, jdir = _make_job(scanner)

    backs = []
    for name in ("back1.png", "back2.png"):
        p = os.path.join(jdir, name)
        with open(p, "w") as f:
            f.write("y")
        backs.append(p)

    monkeypatch.setattr(scanner, "adf_scan_pages", lambda *a, **k: (True, backs, None))
    merged = {}

    def fake_merge(order, dest):
        merged["order"] = list(order)
        with open(dest, "w") as f:
            f.write("pdf")

    monkeypatch.setattr(scanner, "pptos_merge_pdf", fake_merge)
    monkeypatch.setattr(scanner, "auto_paperless", lambda name: "")

    payload, status = scanner.finish_duplex(jid)

    assert status == 200
    assert payload["duplex"] is True and payload["pages"] == 2
    assert jid not in scanner.duplex_jobs
    assert scanner.duplex_results[jid]["file"] == payload["file"]
    # Vorderseiten in Reihenfolge, Rueckseiten rueckwaerts dazwischen
    assert [os.path.basename(p) for p in merged["order"]] == [
        "page1.png", "back2.png", "page2.png", "back1.png"]


def test_auto_back_nutzt_wartende_session(scanner, monkeypatch):
    scanner.duplex_jobs.clear()
    _, _ = _make_job(scanner)
    monkeypatch.setattr(scanner, "lock_busy", lambda: False)
    called = []
    monkeypatch.setattr(scanner, "finish_duplex",
                        lambda jid: called.append(jid) or ({"success": True}, 200))

    assert scanner.auto_back_once() is True
    assert len(called) == 1


def test_auto_back_wartet_bei_belegtem_scanner(scanner, monkeypatch):
    scanner.duplex_jobs.clear()
    _, _ = _make_job(scanner)
    monkeypatch.setattr(scanner, "lock_busy", lambda: True)
    called = []
    monkeypatch.setattr(scanner, "finish_duplex",
                        lambda jid: called.append(jid) or ({"success": True}, 200))

    assert scanner.auto_back_once() is False
    assert called == []


def test_auto_upload_nur_fuer_pdfs_und_wenn_aktiv(scanner, monkeypatch):
    monkeypatch.setitem(scanner.auto_cfg, "paperless", True)
    monkeypatch.setattr(scanner, "PAPERLESS_URL", "https://paperless.example")
    uploaded = []
    monkeypatch.setattr(scanner, "paperless_upload",
                        lambda name: uploaded.append(name) or (True, "ok", "doc42"))

    assert scanner.auto_paperless("scan.pdf") == "paperless (doc doc42)"
    assert uploaded == ["scan.pdf"]
    assert scanner.auto_paperless("scan.png") == ""
    assert uploaded == ["scan.pdf"]


def test_auto_upload_aus_wenn_paperless_deaktiviert(scanner, monkeypatch):
    monkeypatch.setitem(scanner.auto_cfg, "paperless", False)
    monkeypatch.setattr(scanner, "PAPERLESS_URL", "https://paperless.example")
    monkeypatch.setattr(scanner, "paperless_upload", lambda name: (_ for _ in ()).throw(AssertionError()))

    assert scanner.auto_paperless("scan.pdf") == ""
