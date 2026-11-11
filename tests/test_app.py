import threading
import urllib.request

from wow_invest import app


class FakeWebview:
    def __init__(self):
        self.window = None

    def create_window(self, title, url, **kw):
        self.window = (title, url)

    def start(self, **kw):  # the real one blocks until the window closes; check the page is served meanwhile
        with urllib.request.urlopen(self.window[1]) as r:
            self.status = r.status


def test_window_shows_the_served_page_and_closing_it_stops_everything(tmp_path, monkeypatch):
    seen = {}

    def fake_loop(client_factory, db_path, stop):
        seen["db"] = db_path
        seen["stop"] = stop

    monkeypatch.setattr(app.scanner, "loop", fake_loop)
    wv = FakeWebview()
    db = str(tmp_path / "w" / "wow_invest.db")
    assert app.run(webview=wv, db_path=db) == 0
    title, url = wv.window
    assert title == "WoW Invest" and url.startswith("http://127.0.0.1:") and wv.status == 200
    assert seen["db"] == db and seen["stop"].is_set()
    assert not any(t.name == "wow-invest-server" for t in threading.enumerate())  # server thread ended


def test_default_db_path_uses_local_app_data_on_windows(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert app.default_db_path().replace("\\", "/").endswith("AppData/Local/WowInvest/wow_invest.db")
    monkeypatch.delenv("LOCALAPPDATA")
    assert app.default_db_path() == "data/wow_invest.db"
