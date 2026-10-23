"""Regressionstests fuer wartende Duplex-Sessions.

Hintergrund: Nach dem Vorderseiten-Durchzug bleibt eine Session liegen, bis
die Rueckseiten gescannt werden. Der Autoscan pausiert absichtlich so lange.
Ohne Ablauf blockierte eine vergessene Session den Autoscan dauerhaft.
"""
import os
import time


def _job(scanner, front_pages=("page1.png",), age=0):
    jid = "job%d" % int(time.time() * 1000)
    jdir = os.path.join(scanner.JOB_DIR, jid)
    os.makedirs(jdir, exist_ok=True)
    for p in front_pages:
        with open(os.path.join(jdir, p), "w") as f:
            f.write("x")
    scanner.duplex_jobs[jid] = {
        "device": "test:dev", "mode": "Gray", "resolution": 300,
        "width": 210.0, "height": 297.0,
        "front_pages": [os.path.join(jdir, p) for p in front_pages],
        "ts": time.time() - age,
    }
    return jid, jdir


def test_keine_session(scanner):
    scanner.duplex_jobs.clear()
    assert scanner.pending_duplex() is None


def test_pending_session_meldet_seitenzahl(scanner):
    scanner.duplex_jobs.clear()
    jid, _ = _job(scanner, ("page1.png", "page2.png", "page3.png"))
    info = scanner.pending_duplex()
    assert info["session"] == jid
    assert info["pages"] == 3
    assert info["ttl"] == scanner.DUPLEX_TTL


def test_juengste_session_gewinnt(scanner):
    scanner.duplex_jobs.clear()
    _job(scanner, age=120)
    jid2, _ = _job(scanner, age=5)
    assert scanner.pending_duplex()["session"] == jid2


def test_abgelaufene_session_wird_entfernt(scanner):
    scanner.duplex_jobs.clear()
    jid, jdir = _job(scanner, age=scanner.DUPLEX_TTL + 5)
    assert scanner.pending_duplex() is None
    assert jid not in scanner.duplex_jobs
    assert not os.path.exists(jdir), "Seiten der abgelaufenen Session muessen weg"


def test_frische_session_blockiert_autoscan(scanner, monkeypatch):
    scanner.duplex_jobs.clear()
    _job(scanner)
    calls = []
    monkeypatch.setattr(scanner, "pick_device", lambda: "test:dev")
    monkeypatch.setattr(scanner, "lock_busy", lambda: False)
    monkeypatch.setattr(scanner, "scan_adf", lambda *a, **k: calls.append(1) or (True, "/tmp/x.pdf", None, 1))

    assert scanner.auto_scan_once() is False
    assert calls == [], "Waehrend einer Duplex-Session darf der Autoscan nicht scannen"


def test_abgelaufene_session_blockiert_autoscan_nicht(scanner, monkeypatch):
    scanner.duplex_jobs.clear()
    jid, _ = _job(scanner, age=scanner.DUPLEX_TTL + 5)
    calls = []
    monkeypatch.setattr(scanner, "pick_device", lambda: "test:dev")
    monkeypatch.setattr(scanner, "lock_busy", lambda: False)
    monkeypatch.setattr(scanner, "scan_adf", lambda *a, **k: calls.append(1) or (True, "/tmp/x.pdf", None, 1))

    assert scanner.auto_scan_once() is True
    assert calls == [1]
    assert jid not in scanner.duplex_jobs
