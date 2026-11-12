"""Desktop entry: WoW Invest in its own window (pywebview, WebView2 on Windows), no browser.
Closing the window quits the program."""
import logging
import os
import subprocess
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


def _toast(text: str):
    """A Windows tray balloon, through PowerShell so no extra package is needed."""
    text = "".join(ch for ch in text if ch.isalnum() or ch in " +,.:-")  # nothing that could end the quoted string
    script = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
              f"$n.Icon = New-Object System.Drawing.Icon('{ICON}'); $n.Visible = $true; "
              f"$n.ShowBalloonTip(10000, 'WoW Invest', '{text}', 'None'); Start-Sleep 10; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        log.exception("could not show a notification")


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
        scanner.notify = _toast
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
