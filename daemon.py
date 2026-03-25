import asyncio, json, sqlite3, subprocess, sys, os
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

DAEMON_DIR = os.path.expanduser("~/.signal_daemon")
DB_PATH = os.path.join(DAEMON_DIR, "messages.db")
SIGNAL_CLI = "signal-cli"
PORT = 6000

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

# ── HTTP Server ───────────────────────────────────────────────────────────────

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

class Handler(BaseHTTPRequestHandler):
    def __init__(self, account, *args, **kwargs):
        self.account = account
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        print(f"[{datetime.now().isoformat()}] {args[0]} {args[1]} {args[2]}")

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

        elif parsed.path == "/send_to_self":
            qs = parse_qs(parsed.query)
            message = qs["message"][0] if "message" in qs else None
            if not message:
                self.send_json({"error": "missing 'message' query parameter"}, 400)
                return
            try:
                send_to_self(self.account, message)
                self.send_json({"ok": True, "message": message})
            except RuntimeError as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_json({"error": "Not found"}, 404)

def run_server(account):
    def make_handler(*args, **kwargs):
        Handler(account, *args, **kwargs)
    print(f"HTTP server listening on http://localhost:{PORT}")
    HTTPServer(("", PORT), make_handler).serve_forever()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = init_db()
    account = get_account(db)

    if not account:
        if len(sys.argv) < 2:
            print("Error: no account number stored. Please run:")
            print("  python signal.py +1XXXXXXXXXX")
            sys.exit(1)
        account = sys.argv[1]
        set_account(db, account)
        print(f"Account saved: {account}")

    db.close()
    print(f"Using account: {account}")

    threading.Thread(target=run_server, args=(account,), daemon=True).start()
    asyncio.run(poll_loop(account))
