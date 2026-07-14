"""Entrypoint: .venv/bin/python -m sbg.ui

Starts the local server and opens a browser tab. No separate packaging or
hosting -- each scientist runs their own instance against their own local
SBG data, matching the "share the file via Drive" usage model this was
designed around (see the project plan, Phase 6).
"""
import argparse
import threading
import webbrowser

import uvicorn

from sbg.ui.app import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dev", action="store_true", help="Enable CORS for a separately-running Vite dev server; skip serving webui/dist/")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    ap.add_argument("--dataset", help="Path to an SBG CityJSON to load instead of data/sbg.city.json (e.g. a small test file for fast iteration)")
    args = ap.parse_args()

    app = create_app(dev=args.dev, dataset_path=args.dataset)

    if not args.no_browser:
        url = f"http://{args.host}:{args.port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
