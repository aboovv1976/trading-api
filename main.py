import os
from flask import Flask, request, jsonify

app = Flask(__name__)

TV_WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET")

@app.route("/api/v1/tv-alert-x7k9m2p", methods=["POST"])
def tradingview_webhook():
    if not TV_WEBHOOK_SECRET:
        return jsonify({"error": "Server misconfigured"}), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    # Validate secret
    if data.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    signal = data.get("signal")       # "green" or "red"
    ticker = data.get("ticker")
    price = data.get("price")
    interval = data.get("interval")
    time = data.get("time")

    # TODO: process alert, call Grok, send notification, etc.
    print(f"Alert: {ticker} {signal} @ {price} ({interval})")

    return jsonify({"status": "received"}), 200