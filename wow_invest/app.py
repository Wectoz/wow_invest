"""Desktop entry: WoW Invest in its own window (pywebview, WebView2 on Windows), no browser.
Closing the window quits the program."""
import logging
import os
import threading
from pathlib import Path

from wow_invest import api, scanner, web

ICON = Path(__file__).parent / "static" / "favicon.ico"
log = logging.getLogger(__name__)


def _brand_window(window):
    """Windows: show our icon on the window and taskbar instead of Python's."""
    try:
        import clr  # pythonnet, which pywebview uses on Windows
        clr.AddReference("System.Drawing")
        from System import Action
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(str(ICON)))))
    except Exception:
        log.exception("could not set the window icon")


def default_db_path() -> str:
    base = os.environ.get("LOCALAPPDATA")
    return str(Path(base) / "WowInvest" / "wow_invest.db") if base else "data/wow_invest.db"


def run(webview=None, db_path: str | None = None) -> int:
    if webview is None:
        import webview  # only the desktop build needs it
    db_path = db_path or default_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(Path(db_path).with_name("app.log")), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    server = web.make_server(db_path, 0)  # any free port; only our window talks to it
    serving = threading.Thread(target=server.serve_forever, name="wow-invest-server", daemon=True)
    serving.start()
    threading.Thread(target=scanner.loop, args=(api.Client, db_path, stop), daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if os.name == "nt":  # own taskbar group, so Windows doesn't file the window under Python
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WowInvest.App")
    window = webview.create_window("WoW Invest", url, width=1240, height=960, background_color="#14100b")
    if os.name == "nt":
        window.events.shown += lambda: _brand_window(window)
    log.info("window opened on %s", url)
    try:
        webview.start()  # blocks until the window is closed
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)
    return 0
