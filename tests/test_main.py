import threading

from wow_invest import __main__ as m, api


def test_main_parses_args_and_starts_both(monkeypatch, tmp_path):
    calls = {}
    started = threading.Event()

    def fake_loop(client_factory, db_path, stop):
        calls["loop"] = (client_factory, db_path)
        started.set()
        stop.wait(5)
        calls["stopped"] = stop.is_set()

    class FakeServer:
        server_address = ("127.0.0.1", 4321)

        def serve_forever(self):
            started.wait(5)
            raise KeyboardInterrupt

        def server_close(self):
            calls["closed"] = True

    def fake_make_server(db_path, port):
        calls["server"] = (db_path, port)
        return FakeServer()

    monkeypatch.setattr(m.scanner, "loop", fake_loop)
    monkeypatch.setattr(m.web, "make_server", fake_make_server)
    db = str(tmp_path / "x.db")
    assert m.main(["--port", "0", "--db", db]) == 0
    assert calls["server"] == (db, 0)
    assert calls["loop"] == (api.Client, db)
    assert calls["closed"]
