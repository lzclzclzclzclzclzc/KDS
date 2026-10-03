"""Local, fully offline preview with temporary business/checkpoint databases."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask,render_template,jsonify
from langgraph.checkpoint.memory import InMemorySaver
from app.repositories.teams import TeamRepository
from app.services.team_sessions import TeamSessionService,MockTeamExecutor
from app.routes.team_api import team_api_bp

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='kds-team-preview-') as directory:
        root=Path(__file__).resolve().parents[1]
        service=TeamSessionService(TeamRepository(Path(directory)/'business.db'),MockTeamExecutor(),checkpointer=InMemorySaver())
        app=Flask(__name__,template_folder=str(root/'app/templates'),static_folder=str(root/'app/static'),static_url_path='/static')
        app.extensions['team_service']=service
        app.register_blueprint(team_api_bp)
        @app.get('/teams')
        def team_page(): return render_template('team.html')
        @app.get('/api/configs')
        def configs(): return jsonify([])
        try:
            app.run(host='127.0.0.1',port=5017,threaded=True,use_reloader=False)
        finally:
            service.close()
