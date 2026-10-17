from __future__ import annotations

import json
import re
import sqlite3
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "smartqueue.db"
HOST = "127.0.0.1"
PORT = 8000
SERVICE_MINUTES = 10
SERVICES = {
    "Banking Services",
    "Healthcare",
    "Public Services",
    "Customer Support",
    "General Enquiry",
}


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    connection = connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    with database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY,
                token_number TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Waiting'
                    CHECK (status IN ('Waiting', 'Serving', 'Completed'))
            )
            """
        )


def token_record(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "token_number": row["token_number"],
        "name": row["name"],
        "phone": row["phone"],
        "service": row["service"],
        "created_at": row["created_at"],
        "status": row["status"],
    }


def queue_snapshot() -> dict[str, object]:
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM tokens ORDER BY id ASC"
        ).fetchall()

    tokens = [token_record(row) for row in rows]
    waiting = [token for token in tokens if token["status"] == "Waiting"]
    serving = [token for token in tokens if token["status"] == "Serving"]
    completed = [token for token in tokens if token["status"] == "Completed"]

    positions = {
        token["id"]: position
        for position, token in enumerate(waiting, start=1)
    }
    for token in tokens:
        token["queue_position"] = positions.get(token["id"], 0)
        token["estimated_wait_minutes"] = (
            positions.get(token["id"], 0) * SERVICE_MINUTES
        )

    return {
        "tokens": list(reversed(tokens)),
        "queue": serving + waiting,
        "currently_serving": serving[0] if serving else None,
        "waiting_count": len(waiting),
        "serving_count": len(serving),
        "completed_count": len(completed),
        "service_minutes": SERVICE_MINUTES,
    }


class SmartQueueHandler(BaseHTTPRequestHandler):
    server_version = "SmartQueue/1.0"

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send request data as JSON.")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("The request size is invalid.") from error

        if length <= 0 or length > 16_384:
            raise ValueError("The request must contain valid JSON data.")

        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("The request contains invalid JSON.") from error

        if not isinstance(payload, dict):
            raise ValueError("The request must be a JSON object.")

        return payload

    def do_GET(self) -> None:
        path = urlsplit(self.path).path

        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return

        if path == "/api/tokens":
            self.send_json(200, queue_snapshot())
            return

        if path == "/":
            self.serve_file(BASE_DIR / "templates" / "index.html", "text/html; charset=utf-8")
            return

        static_files = {
            "/static/app.css": ("app.css", "text/css; charset=utf-8"),
            "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        if path in static_files:
            filename, content_type = static_files[path]
            self.serve_file(BASE_DIR / "static" / filename, content_type)
            return

        self.send_json(404, {"message": "Page not found."})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/tokens":
            self.send_json(404, {"message": "Endpoint not found."})
            return

        try:
            payload = self.read_json()
            name = payload.get("name")
            phone = payload.get("phone")
            service = payload.get("service")
            if not isinstance(name, str) or not 2 <= len(name.strip()) <= 80:
                raise ValueError("Enter a name between 2 and 80 characters.")

            if not isinstance(phone, str) or not re.fullmatch(r"[6-9]\d{9}", phone):
                raise ValueError("Enter a valid 10-digit Indian mobile number.")
            if not isinstance(service, str) or service not in SERVICES:
                raise ValueError("Select a valid service.")

            with database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                token_id = connection.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM tokens"
                ).fetchone()[0]
                token_number = f"SQ-{token_id:04d}"
                connection.execute(
                    """
                    INSERT INTO tokens
                        (id, token_number, name, phone, service, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_id,
                        token_number,
                        name.strip(),
                        phone,
                        service,
                        time.time_ns() // 1_000_000,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tokens WHERE id = ?", (token_id,)
                ).fetchone()

            self.send_json(201, {"token": token_record(row)})
        except ValueError as error:
            self.send_json(400, {"message": str(error)})
        except sqlite3.Error:
            self.send_json(500, {"message": "The token could not be saved. Please try again."})

    def do_PATCH(self) -> None:
        match = re.fullmatch(r"/api/tokens/(\d+)/status", urlsplit(self.path).path)
        if not match:
            self.send_json(404, {"message": "Endpoint not found."})
            return

        try:
            payload = self.read_json()
            status = payload.get("status")
            if not isinstance(status, str) or status not in {"Serving", "Completed"}:
                raise ValueError("Choose a valid queue action.")

            token_id = int(match.group(1))
            with database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM tokens WHERE id = ?", (token_id,)
                ).fetchone()
                if row is None:
                    self.send_json(404, {"message": "Token not found."})
                    return

                if status == "Serving":
                    if row["status"] != "Waiting":
                        self.send_json(
                            409,
                            {"message": "Only waiting tokens can be called to the counter."},
                        )
                        return
                    active = connection.execute(
                        "SELECT 1 FROM tokens WHERE status = 'Serving' LIMIT 1"
                    ).fetchone()
                    if active:
                        self.send_json(
                            409,
                            {"message": "Complete the token being served before calling another."},
                        )
                        return
                elif row["status"] != "Serving":
                    self.send_json(
                        409,
                        {"message": "Only the token being served can be completed."},
                    )
                    return

                connection.execute(
                    "UPDATE tokens SET status = ? WHERE id = ?",
                    (status, token_id),
                )
                updated = connection.execute(
                    "SELECT * FROM tokens WHERE id = ?", (token_id,)
                ).fetchone()

            self.send_json(200, {"token": token_record(updated)})
        except ValueError as error:
            self.send_json(400, {"message": str(error)})
        except sqlite3.Error:
            self.send_json(500, {"message": "The queue could not be updated. Please try again."})

    def serve_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json(404, {"message": "Website file not found."})
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} - {format % args}")


def main() -> None:
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), SmartQueueHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"SmartQueue is running at {url}")
    print("Keep this window open while using the website. Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSmartQueue server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
