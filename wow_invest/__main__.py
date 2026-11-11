import argparse
import logging
import threading

from wow_invest import api, scanner, web


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wow_invest")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="data/wow_invest.db")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    threading.Thread(target=scanner.loop, args=(api.Client, args.db, stop), daemon=True).start()
    server = web.make_server(args.db, args.port)
    print(f"Open http://localhost:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
