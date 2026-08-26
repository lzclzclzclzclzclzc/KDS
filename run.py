from app import create_app
from app.config import HOST, PORT

app = create_app()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False, threaded=True)

