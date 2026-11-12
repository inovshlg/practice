"""Start the web app:  python run.py   (then open http://127.0.0.1:5000)"""
import logging

from app.config import SETTINGS
from app.web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()

if __name__ == "__main__":
    # threaded so the live engine, downloads and the UI run side by side; no reloader (it would
    # start a second process and a second trading engine)
    app.run(host=SETTINGS.host, port=SETTINGS.port, debug=False, threaded=True, use_reloader=False)
