---
name: signal-daemon
description: "Use this skill whenever you need to read Signal messages or send a Signal message via the signal-daemon HTTP API. Triggers include: reading recent Signal messages, querying messages by sender or group, sending a message to self or others on Signal, or any interaction with the local signal-daemon running on port 6000."
---

# Signal Daemon HTTP API

## Overview

A local HTTP daemon runs on `http://localhost:6000` and exposes two endpoints:
- `GET /messages` — query received Signal messages from the local SQLite store
- `GET /send` — send a Signal message to self or another number

Timestamps are always in **milliseconds since Unix epoch**.

**Important:** Sending to self completes immediately. Sending to anyone else returns a `confirm_url` that **must be shown to the user** — the message is not sent until the user opens that URL in their browser and clicks "Send".

---

## Reading Messages

### All messages from the last hour
```bash
curl "http://localhost:6000/messages?since=$(python3 -c 'import time; print(int(time.time()*1000) - 3600000)')"
```

### All messages from the last 10 minutes
```bash
curl "http://localhost:6000/messages?since=$(python3 -c 'import time; print(int(time.time()*1000) - 600000)')"
```

### All messages from the last 24 hours
```bash
curl "http://localhost:6000/messages?since=$(python3 -c 'import time; print(int(time.time()*1000) - 86400000)')"
```

### Filter by sender (phone number or display name)
```bash
curl "http://localhost:6000/messages?sender=%2B1234567890"
```
Note: `+` must be URL-encoded as `%2B` in phone numbers.

### Filter by sender name
```bash
curl --get "http://localhost:6000/messages" --data-urlencode "sender=Alice"
```

### Filter by group ID
```bash
curl "http://localhost:6000/messages?group=AfL%2Fco87TsyfTv4FqgJfcF6rNWoRkO2CYLybn83tfTU%3D"
```

### Combine filters: sender + time window
```bash
curl --get "http://localhost:6000/messages" \
  --data-urlencode "sender=Alice" \
  --data-urlencode "since=$(python3 -c 'import time; print(int(time.time()*1000) - 3600000)')"
```

### Between two timestamps
```bash
curl "http://localhost:6000/messages?since=1774390000000&until=1774399000000"
```

### Response format
```json
{
  "count": 2,
  "messages": [
    {
      "id": 1,
      "timestamp": 1774397441614,
      "source": "+1234567890",
      "source_name": "Alice",
      "group_id": "AfL/co87TsyfTv4FqgJfcF6rNWoRkO2CYLybn83tfTU=",
      "message": "Hello there",
      "raw_json": "...",
      "received_at": "2026-03-25T02:00:00.000000"
    }
  ]
}
```

### Notes
- Only messages with non-null text are returned (attachments, reactions, and sync events are excluded)
- `timestamp` is when the message was sent (milliseconds)
- `received_at` is when the poller collected it (ISO 8601 UTC string)
- `group_id` is null for direct messages

---

## Sending Messages

### Send to self (Note to Self — no confirmation required)
```bash
curl --get "http://localhost:6000/send" \
  --data-urlencode "to=+1234567890" \
  --data-urlencode "message=Hello from the daemon"
```
When `to` matches your own account number, the message is sent immediately.

### Response format (sent immediately)
```json
{
  "ok": true,
  "to": "+1234567890",
  "message": "Hello from the daemon"
}
```

---

### Send to another person — requires user confirmation

```bash
curl --get "http://localhost:6000/send" \
  --data-urlencode "to=+1987654321" \
  --data-urlencode "message=Hello Alice"
```

### Response format (confirmation required)
```json
{
  "pending": true,
  "confirm_url": "http://localhost:7000/confirm?token=...",
  "to": "+1987654321",
  "message": "Hello Alice",
  "note": "Open confirm_url in your browser to approve or deny this message."
}
```

> **When you receive a `pending: true` response, you must present the `confirm_url` to the user.** The message has NOT been sent yet. The user must open the URL in their browser and click "Send" to approve, or "Don't send" to cancel. Do not silently discard the URL.

### Error response
```json
{
  "error": "some signal-cli error message"
}
```

---

## Query Parameter Reference

| Parameter | Endpoint  | Type   | Description                                                |
|-----------|-----------|--------|------------------------------------------------------------|
| `since`   | /messages | int    | Start timestamp in milliseconds (inclusive)                |
| `until`   | /messages | int    | End timestamp in milliseconds (inclusive)                  |
| `sender`  | /messages | string | Filter by phone number (e.g. +1234567890) or display name |
| `group`   | /messages | string | Filter by base64 group ID                                  |
| `to`      | /send     | string | Recipient phone number (required)                          |
| `message` | /send     | string | Message text (required)                                    |

All parameters are optional for `/messages`. `to` and `message` are required for `/send`.

---

## Timestamp Quick Reference

```bash
# Current time in ms
python3 -c 'import time; print(int(time.time()*1000))'

# 10 minutes ago
python3 -c 'import time; print(int(time.time()*1000) - 600000)'

# 1 hour ago
python3 -c 'import time; print(int(time.time()*1000) - 3600000)'

# 24 hours ago
python3 -c 'import time; print(int(time.time()*1000) - 86400000)'

# 7 days ago
python3 -c 'import time; print(int(time.time()*1000) - 604800000)'
```
