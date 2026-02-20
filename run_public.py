from flask import Flask
from pyngrok import ngrok
import threading

from app import app  # import your Flask app

def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=True)

if __name__ == "__main__":
    # Open tunnel
    public_url = ngrok.connect(5000)
    print(f" Public URL: {public_url}")

    # Run Flask in a thread
    threading.Thread(target=run_flask).start()
