"""Gemeinsame Test-Hilfen: app.py ohne Flask/requests/Pillow importierbar machen."""
import importlib.util
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIR = tempfile.mkdtemp(prefix="scanner-test-")
os.environ["SCAN_DIR"] = SCAN_DIR
os.environ["SCANNER_DEVICE"] = "test:dev"
os.environ["PAPERLESS_URL"] = ""


class _Flask:
    def __init__(self, *args, **kwargs):
        self.config = {}

    def route(self, *args, **kwargs):
        return lambda fn: fn

    def before_request(self, *args, **kwargs):
        return lambda fn: fn

    def run(self, *args, **kwargs):
        pass


flask_stub = types.ModuleType("flask")
flask_stub.Flask = _Flask
for _name in ("request", "jsonify", "render_template", "send_from_directory", "abort", "Response"):
    setattr(flask_stub, _name, object())
sys.modules["flask"] = flask_stub

sys.modules["requests"] = types.ModuleType("requests")
pil_stub = types.ModuleType("PIL")
pil_stub.Image = types.ModuleType("PIL.Image")
sys.modules["PIL"] = pil_stub
sys.modules["PIL.Image"] = pil_stub.Image


@pytest.fixture(scope="session")
def scanner():
    spec = importlib.util.spec_from_file_location(
        "scanner_app", os.path.join(ROOT, "app", "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
