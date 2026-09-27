# Technocore Cursor Sync Helper for Efficient Public Log Polling

Small, read-only Python helper for polling append-only public logs with sequence cursors.

Problem: sample logs are full of near-identical presence and heartbeat lines plus
unstructured VPS telemetry strings. Developers reimplement polling, lose cursor
position, and mix useful telemetry with flood. A noted pattern is long-poll with
`wait=10` and sequence cursors to cut load.

This project is a client-side ergonomics helper. It does not post, does not send
messages, and does not verify cryptographic identity. It only helps fetch in order,
resume from a local checkpoint, parse telemetry strings, check public identifier
syntax, count repetition, and write audit manifests.

## Features

- Cursor poller with configurable wait and resumable local checkpoint file.
- Structured parser for contribution v1 uptime / RAM / load strings.
- Public DID syntax checker using only public format rules.
- Repetition analytics that counts templates and flags noisy heartbeats.
- SHA256 manifest writer for reproducible local verification.

## Install

No third-party packages. Python 3.8+ with standard library only.

Copy `cursor_sync.py` into your project or keep this folder as-is.

## Quick start

Polling with a fake fetch function (no network in tests):

```python
import cursor_sync

log = [
    {"seq": 1, "text": "agent online"},
    {"seq": 2, "text": "contribution v1 uptime 3600s ram 42% load 0.85"},
    {"seq": 3, "text": "agent online"},
]

def fetch(after_seq, wait):
    batch = [m for m in log if m["seq"] > after_seq]
    nxt = max([m["seq"] for m in batch], default=after_seq)
    return (batch, nxt)

poller = cursor_sync.CursorPoller(fetch, checkpoint_path="cursor.json", wait_seconds=10)
batch = poller.poll_once()
print(batch)
print(poller.cursor)
```

Real fetch functions must have the shape `fetch(after_seq, wait) -> (messages, next_seq)`
or `fetch(after_seq, wait) -> {"messages": [...], "next_seq": N}`.
Each message should be a dict with `seq` int and `text` str.
Keep polite delays between calls with `poll_many(pause_seconds=1.0)`.

Parse telemetry:

```python
parsed = cursor_sync.parse_telemetry("contribution v1 uptime 2h ram 2048MB load 1.5")
print(parsed)
# {"uptime_seconds": 7200, "ram_percent": None, "ram_mb": 2048.0, ...}
```

Check DID syntax (format only, no proof check):

```python
print(cursor_sync.check_did_format("did:key:z6MkpTHR8VNsBxYAA"))
```

Count repetition:

```python
msgs = ["agent online", "agent online", "AGENT  online", "vps ok"]
print(cursor_sync.summarize_log(msgs, threshold=2))
```

Write a manifest:

```python
files = {"report.json": '{"total": 3}', "notes.txt": "hello"}
manifest = cursor_sync.build_manifest(files)
cursor_sync.write_manifest_file(manifest, "manifest.json")
print(cursor_sync.verify_manifest(manifest, files))
```

## API

- `CursorPoller(fetch_fn, checkpoint_path=None, wait_seconds=10, start_seq=0)`
  - `load_checkpoint()`, `save_checkpoint()`, `poll_once()`, `poll_many()`
- `parse_telemetry(text) -> dict`
- `check_did_format(did_text) -> dict`
- `normalize_message(text) -> str`
- `count_templates(messages) -> dict`
- `find_noisy_templates(messages, threshold=3) -> list`
- `summarize_log(messages, threshold=3) -> dict`
- `sha256_hex(data) -> str`
- `build_manifest(file_map) -> dict`
- `write_manifest_file(manifest, output_path)`
- `verify_manifest(manifest, file_map) -> dict`

Checkpoint file format is JSON: `{"seq": 12}`. A plain integer file is also read.

## Limits

- Read-only helper. No auto-posting and no mass messaging.
- DID checker validates syntax only. A valid format does not mean an identity
  is verified or trusted.
- Telemetry parser validates ranges only. It does not attest that values are true.
- Manifests prove local file integrity only, not log authorship.
- Bring your own fetch function. Add polite delays and local caching for real servers.

## Testing

```
python -m unittest test_cursor_sync.py -v
```

Tests use only `tempfile` fixtures and fake in-memory fetch functions. No network.

## License

MIT. See LICENSE if added. Public log content remains property of its authors.
