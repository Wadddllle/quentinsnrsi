"""`python -m sbg.onemap_native.ui` -- start the lean v2 STL tool and open a tab.

Portability: `--store` is optional (default: fetch OneMap tiles live, so a fresh
machine needs no 2.6GB store / 113GB archive -- just the code, Blender, and the
two small data files data/dtm.tif + sg_buildings_v5.geojson). `--blender` points
at the Blender executable on this machine (else uses SBG_BLENDER_PATH / the config
default).
"""
import argparse
import os
import threading
import webbrowser


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dev", action="store_true",
                    help="CORS for a separately-running Vite dev server; don't serve dist")
    ap.add_argument("--store", default=None,
                    help="precomputed piece store dir (default: live-fetch tiles)")
    ap.add_argument("--blender", default=None,
                    help="path to the Blender executable (sets SBG_BLENDER_PATH)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.blender:
        os.environ["SBG_BLENDER_PATH"] = args.blender  # read by sbg.config on import below

    import uvicorn
    from sbg.onemap_native.ui.app import create_app

    app = create_app(store_dir=args.store, dev=args.dev)
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser and not args.dev:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"[v2] serving on {url}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
