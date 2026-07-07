"""
app.py — Flask додаток
"""

import os
import logging
from flask import Flask, render_template, request, jsonify
from chipex_client import (
    lookup_vehicle,
    diagnose,
    ChipexLookupError,
    ChipexAuthError,
    ChipexNotFoundError,
    ChipexNetworkError,
)

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)


def format_error(error: ChipexLookupError) -> dict:
    """Перетворює помилку у user-friendly формат."""
    if isinstance(error, ChipexAuthError):
        return {
            "title": "Access Denied",
            "message": "The chipex.co.uk server blocked our request. This is due to bot protection. Please try again later.",
        }
    if isinstance(error, ChipexNotFoundError):
        return {
            "title": "Not Found",
            "message": "This registration number was not found in the database.",
        }
    if isinstance(error, ChipexNetworkError):
        return {
            "title": "Connection Problem",
            "message": "Could not connect to chipex.co.uk. Please try again later.",
        }
    return {
        "title": "Error",
        "message": "An unexpected error occurred. Please try again.",
    }


@app.route("/", methods=["GET", "POST"])
def index():
    vehicle = None
    error_info = None
    reg_number = ""

    if request.method == "POST":
        reg_number = request.form.get("reg_number", "").strip()
        if not reg_number:
            error_info = {"title": "Invalid Input", "message": "Please enter a registration number."}
        else:
            try:
                vehicle = lookup_vehicle(reg_number)
            except ChipexLookupError as exc:
                error_info = format_error(exc)
                app.logger.error(f"Lookup failed for '{reg_number}': {type(exc).__name__}: {exc}")

    return render_template("index.html", vehicle=vehicle, error=error_info, reg_number=reg_number)


@app.route("/api/lookup/<reg_number>")
def api_lookup(reg_number: str):
    try:
        vehicle = lookup_vehicle(reg_number)
        return jsonify({"success": True, "data": vehicle.to_dict()})
    except ChipexLookupError as exc:
        return jsonify({"success": False, "error": format_error(exc)}), exc.status_code or 500


@app.route("/diagnose")
def diagnose_endpoint():
    """Діагностика."""
    try:
        return jsonify(diagnose())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
