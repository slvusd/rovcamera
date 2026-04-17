from flask import Flask, render_template

app = Flask(__name__)

#MEDIAMTX_HOST = "192.168.3.41"
MEDIAMTX_HOST = "192.168.0.8"
MEDIAMTX_PORT = 8889

@app.route("/")
def index():
    return render_template("index.html", host=MEDIAMTX_HOST, port=MEDIAMTX_PORT)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
