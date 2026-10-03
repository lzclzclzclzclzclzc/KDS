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
    from app.orchestration.team_ownership import acquire_team_owner
    app.extensions['team_owner'] = acquire_team_owner(DB_PATH)
    init_db(DB_PATH)
    mark_stale_running_conversations()
    from app.repositories.orchestration import OrchestrationRepository
    OrchestrationRepository(DB_PATH).recover()
    from app.repositories.teams import TeamRepository
    team_repository = TeamRepository(DB_PATH)
    team_repository.recover()
    team_repository.recover_auxiliary()

    from app.routes.api import api_bp

    app.register_blueprint(api_bp)
    from app.routes.team_api import team_api_bp
    app.register_blueprint(team_api_bp)

    @app.get('/teams')
    def teams_page():
        from flask import render_template
        return render_template('team.html')

    return app
