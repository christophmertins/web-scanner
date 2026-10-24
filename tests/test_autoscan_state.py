"""Autoscan-Zustand (an/aus) muss einen Container-Neustart ueberleben."""


def test_autoscan_zustand_wird_gespeichert_und_geladen(scanner, monkeypatch):
    monkeypatch.setitem(scanner.auto_cfg, "enabled", False)
    monkeypatch.setitem(scanner.auto_cfg, "interval", 7.5)
    monkeypatch.setitem(scanner.auto_cfg, "paperless", False)
    scanner._save_auto_state()

    # Neustart simulieren: Defaults, dann laden
    monkeypatch.setitem(scanner.auto_cfg, "enabled", True)
    monkeypatch.setitem(scanner.auto_cfg, "interval", 2.5)
    monkeypatch.setitem(scanner.auto_cfg, "paperless", True)
    scanner._load_auto_state()

    assert scanner.auto_cfg["enabled"] is False
    assert scanner.auto_cfg["interval"] == 7.5
    assert scanner.auto_cfg["paperless"] is False


def test_fehlende_datei_laesst_defaults_stehen(scanner, monkeypatch):
    import os
    try:
        os.remove(scanner.AUTO_STATE_FILE)
    except OSError:
        pass
    monkeypatch.setitem(scanner.auto_cfg, "enabled", True)
    scanner._load_auto_state()
    assert scanner.auto_cfg["enabled"] is True
