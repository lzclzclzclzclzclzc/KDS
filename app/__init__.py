from flask import Flask

from app.config import DATA_DIR, DB_PATH
from app.db import init_db, mark_stale_running_conversations


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["JSON_AS_ASCII"] = False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    mark_stale_running_conversations()

    from app.routes.api import api_bp

    app.register_blueprint(api_bp)

    return app
