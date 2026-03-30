import asyncio, json, sqlite3, subprocess, sys, os
import html as html_lib
import secrets
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DAEMON_DIR = os.path.expanduser("~/.signal_daemon")
DB_PATH = os.path.join(DAEMON_DIR, "messages.db")
SIGNAL_CLI = "signal-cli"
PORT = 6000
CONFIRM_PORT = 7000  # human-facing confirmation UI — keep this port away from untrusted software

# In-memory store of pending outbound messages awaiting confirmation
# { token: { account, to, message, created_at } }
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(DAEMON_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            timestamp INTEGER,
            source TEXT,
            source_name TEXT,
            group_id TEXT,
            message TEXT,
            raw_json TEXT,
            received_at TEXT,
            UNIQUE(timestamp, source)
        )
    """)
    db.commit()
    return db

def get_account(db):
    row = db.execute("SELECT value FROM config WHERE key='account'").fetchone()
    return row[0] if row else None

def set_account(db, account):
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('account', ?)", (account,))
    db.commit()

# ── Poller ────────────────────────────────────────────────────────────────────

def receive_messages(account):
    result = subprocess.run(
        [SIGNAL_CLI, "-a", account, "--output=json", "receive"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"signal-cli error: {result.stderr}")
        return []

    envelopes = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            envelopes.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"Failed to parse line: {e}\n  {line}")
    return envelopes

def store_envelopes(db, envelopes):
    count = 0
    for envelope in envelopes:
        env = envelope.get("envelope", {})
        data = env.get("dataMessage", {})
        group_info = data.get("groupInfo") or {}
        try:
            db.execute(
                """INSERT OR IGNORE INTO messages
                   (timestamp, source, source_name, group_id, message, raw_json, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    env.get("timestamp"),
                    env.get("source"),
                    env.get("sourceName"),
                    group_info.get("groupId"),
                    data.get("message"),
                    json.dumps(envelope),
                    datetime.utcnow().isoformat(),
                )
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                count += 1
        except sqlite3.Error as e:
            print(f"DB error: {e}")
    db.commit()
    return count

async def poll_loop(account, interval_seconds=60):
    while True:
        print(f"[{datetime.utcnow().isoformat()}] Polling for messages...")
        db = sqlite3.connect(DB_PATH)
        envelopes = receive_messages(account)
        count = store_envelopes(db, envelopes)
        print(f"  Stored {count} new message(s) ({len(envelopes)} envelope(s) received)")
        db.close()
        await asyncio.sleep(interval_seconds)

# ── Sending ───────────────────────────────────────────────────────────────────

def send_to_self(account, message):
    result = subprocess.run(
        [SIGNAL_CLI, "-a", account, "send", "--note-to-self", "-m", message],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return True

def send_to_number(account, number, message):
    result = subprocess.run(
        [SIGNAL_CLI, "-a", account, "send", "-m", message, number],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return True

# ── HTTP query ────────────────────────────────────────────────────────────────

def query_messages(sender=None, group=None, since=None, until=None):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    clauses = ["message IS NOT NULL"]
    params = []

    if sender:
        clauses.append("(source = ? OR source_name = ?)")
        params.extend([sender, sender])
    if group:
        clauses.append("group_id = ?")
        params.append(group)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)

    sql = f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY timestamp ASC"
    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]

# ── Main API server (port 6000) ───────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def __init__(self, account, *args, **kwargs):
        self.account = account
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat()}] {' '.join(str(a) for a in args)}")

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/messages":
            qs = parse_qs(parsed.query)
            def first(key):
                return qs[key][0] if key in qs else None

            try:
                since = int(first("since")) if first("since") else None
                until = int(first("until")) if first("until") else None
            except ValueError:
                self.send_json({"error": "'since' and 'until' must be Unix timestamps in milliseconds"}, 400)
                return

            messages = query_messages(
                sender=first("sender"),
                group=first("group"),
                since=since,
                until=until,
            )
            self.send_json({"count": len(messages), "messages": messages})

        elif parsed.path == "/send":
            qs = parse_qs(parsed.query)
            def first(key):
                return qs[key][0] if key in qs else None

            to = first("to")
            message = first("message")
            if not to or not message:
                self.send_json({"error": "missing 'to' or 'message' query parameter"}, 400)
                return

            # Sending to self: send immediately
            if to.strip() == self.account.strip():
                try:
                    send_to_self(self.account, message)
                    self.send_json({"ok": True, "to": to, "message": message})
                except RuntimeError as e:
                    self.send_json({"error": str(e)}, 500)
            else:
                # Sending to someone else: require human confirmation
                token = secrets.token_urlsafe(32)
                with _pending_lock:
                    _pending[token] = {
                        "account": self.account,
                        "to": to,
                        "message": message,
                        "created_at": datetime.utcnow().isoformat(),
                    }
                confirm_url = f"http://localhost:{CONFIRM_PORT}/confirm?token={token}"
                print(f"[{datetime.now().isoformat()}] Confirmation required: {confirm_url}")
                self.send_json({
                    "pending": True,
                    "confirm_url": confirm_url,
                    "to": to,
                    "message": message,
                    "note": "Open confirm_url in your browser to approve or deny this message.",
                })

        else:
            self.send_json({"error": "Not found"}, 404)


# ── Confirmation server (port 7000, human-facing) ─────────────────────────────

def _page(title: str, body_content: str) -> str:
    e = html_lib.escape
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{e(title)}</title>
<style>
  body {{ font-family: sans-serif; max-width: 640px; margin: 4em auto; padding: 0 1em; color: #111; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.5em 0; }}
  td {{ padding: 10px 12px; vertical-align: top; }}
  tr:nth-child(even) {{ background: #f5f5f5; }}
  .label {{ font-weight: bold; width: 80px; }}
  .body-text {{ white-space: pre-wrap; font-family: monospace; font-size: 0.9em; }}
  .actions {{ display: flex; gap: 1em; margin-top: 2em; }}
  a.btn {{ padding: 12px 28px; text-decoration: none; border-radius: 6px; font-size: 1.05em; color: white; }}
  a.send {{ background: #2563eb; }}
  a.deny {{ background: #dc2626; }}
  .meta {{ color: #888; font-size: 0.85em; margin-top: 2em; }}
</style>
</head><body>
{body_content}
</body></html>"""


class ConfirmHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat()}] confirm: {' '.join(str(a) for a in args)}")

    def send_html(self, content: str, status: int = 200) -> None:
        data = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        token = qs.get("token", [None])[0]
        e = html_lib.escape

        if parsed.path == "/confirm":
            if not token:
                self.send_html(_page("Error", "<h2>Missing token</h2>"), 400)
                return
            with _pending_lock:
                pending = _pending.get(token)
            if not pending:
                self.send_html(_page("Not found", "<h2>&#x274C; Not found</h2><p>This link is invalid or has already been used.</p>"), 404)
                return
            body_content = f"""
<h2>&#x1F4F1; Confirm outbound Signal message</h2>
<table>
  <tr><td class="label">From</td><td>{e(pending['account'])}</td></tr>
  <tr><td class="label">To</td><td>{e(pending['to'])}</td></tr>
  <tr><td class="label">Message</td><td class="body-text">{e(pending['message'])}</td></tr>
</table>
<div class="actions">
  <a class="btn send" href="/approve?token={e(token)}">&#x2714; Send</a>
  <a class="btn deny" href="/deny?token={e(token)}">&#x2716; Don't send</a>
</div>
<p class="meta">Requested at {e(pending['created_at'])}</p>"""
            self.send_html(_page("Confirm send", body_content))

        elif parsed.path == "/approve":
            if not token:
                self.send_html(_page("Error", "<h2>Missing token</h2>"), 400)
                return
            with _pending_lock:
                pending = _pending.pop(token, None)
            if not pending:
                self.send_html(_page("Already handled", "<h2>&#x274C; Already handled</h2><p>This link is invalid or has already been used.</p>"), 404)
                return
            try:
                send_to_number(pending["account"], pending["to"], pending["message"])
                print(f"[{datetime.now().isoformat()}] Confirmed and sent to {pending['to']}")
                body_content = f"<h2>&#x2705; Message sent</h2><p>Sent to <strong>{e(pending['to'])}</strong>.</p>"
                self.send_html(_page("Sent", body_content))
            except Exception as ex:
                body_content = f"<h2>&#x274C; Send failed</h2><pre>{e(str(ex))}</pre>"
                self.send_html(_page("Error", body_content), 500)

        elif parsed.path == "/deny":
            if not token:
                self.send_html(_page("Error", "<h2>Missing token</h2>"), 400)
                return
            with _pending_lock:
                pending = _pending.pop(token, None)
            if not pending:
                self.send_html(_page("Already handled", "<h2>&#x274C; Already handled</h2><p>This link is invalid or has already been used.</p>"), 404)
                return
            print(f"[{datetime.now().isoformat()}] Denied send to {pending['to']}")
            body_content = f"<h2>&#x1F6AB; Cancelled</h2><p>The message to <strong>{e(pending['to'])}</strong> was not sent.</p>"
            self.send_html(_page("Cancelled", body_content))

        else:
            self.send_html(_page("Not found", "<h1>Not found</h1>"), 404)


def run_server(account):
    def make_handler(*args, **kwargs):
        return Handler(account, *args, **kwargs)
    print(f"API server listening on http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), make_handler).serve_forever()


def run_confirm_server() -> None:
    print(f"Confirmation server listening on http://localhost:{CONFIRM_PORT} (human-facing)")
    HTTPServer(("127.0.0.1", CONFIRM_PORT), ConfirmHandler).serve_forever()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = init_db()
    account = get_account(db)

    if not account:
        if len(sys.argv) < 2:
            print("Error: no account number stored. Please run:")
            print("  python daemon.py +1XXXXXXXXXX")
            sys.exit(1)
        account = sys.argv[1]
        set_account(db, account)
        print(f"Account saved: {account}")

    db.close()
    print(f"Using account: {account}")

    threading.Thread(target=run_server, args=(account,), daemon=True).start()
    threading.Thread(target=run_confirm_server, daemon=True).start()
    asyncio.run(poll_loop(account))
