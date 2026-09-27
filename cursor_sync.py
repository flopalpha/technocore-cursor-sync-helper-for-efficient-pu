"""Cursor sync helper for efficient public log polling."""

import hashlib
import json
import re
import time

_UPTIME_PATTERN = r"uptime\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)?"
_RAM_PATTERN = r"ram\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(%|percent|gb|g|mb|m)?"
_LOAD_PATTERN = r"load\s*[:=]?\s*(\d+(?:\.\d+)?)"


def _message_text(item):
    if isinstance(item, dict):
        value = item.get("text", "")
        if isinstance(value, str):
            return value
        return str(value)
    if isinstance(item, str):
        return item
    return str(item)


def normalize_message(text):
    if not isinstance(text, str):
        return ""
    lowered = text.lower().strip()
    collapsed = re.sub(r"\s+", " ", lowered)
    templated = re.sub(r"\d+", "<N>", collapsed)
    return templated


def count_templates(messages):
    counts = {}
    if not messages:
        return counts
    for item in messages:
        raw = _message_text(item)
        key = normalize_message(raw)
        counts[key] = counts.get(key, 0) + 1
    return counts


def find_noisy_templates(messages, threshold=3):
    try:
        limit = int(threshold)
    except (TypeError, ValueError):
        limit = 3
    if limit < 1:
        limit = 1
    counts = count_templates(messages)
    noisy = []
    for template, total in counts.items():
        if total >= limit:
            noisy.append((template, total))
    noisy.sort(key=lambda pair: pair[1], reverse=True)
    return noisy


def summarize_log(messages, threshold=3):
    items = list(messages) if messages else []
    total = len(items)
    counts = count_templates(items)
    unique = len(counts)
    noisy_pairs = find_noisy_templates(items, threshold)
    top = sorted(counts.items(), key=lambda pair: pair[1], reverse=True)[:5]
    top_list = []
    for template, num in top:
        top_list.append({"template": template, "count": num})
    noisy_list = []
    for template, num in noisy_pairs:
        noisy_list.append({"template": template, "count": num})
    if total > 0 and top:
        noise_ratio = top[0][1] / total
    else:
        noise_ratio = 0.0
    return {
        "total": total,
        "unique_templates": unique,
        "top_templates": top_list,
        "noisy": noisy_list,
        "noise_ratio": noise_ratio,
    }


def _uptime_to_seconds(value, unit):
    name = (unit or "s").lower()
    if name in ("s", "sec", "secs", "second", "seconds"):
        mult = 1
    elif name in ("m", "min", "mins", "minute", "minutes"):
        mult = 60
    elif name in ("h", "hr", "hrs", "hour", "hours"):
        mult = 3600
    elif name in ("d", "day", "days"):
        mult = 86400
    else:
        mult = 1
    return int(float(value) * mult)


def parse_telemetry(text):
    base = {
        "raw": text if isinstance(text, str) else "",
        "uptime_seconds": None,
        "ram_percent": None,
        "ram_mb": None,
        "load_1m": None,
        "has_contribution_tag": False,
        "valid": False,
        "errors": [],
    }
    if not isinstance(text, str) or not text.strip():
        base["errors"].append("empty input")
        return base
    if "contribution" in text.lower():
        base["has_contribution_tag"] = True
    found = re.search(_UPTIME_PATTERN, text, re.IGNORECASE)
    if found:
        try:
            secs = _uptime_to_seconds(found.group(1), found.group(2))
        except (TypeError, ValueError):
            secs = None
        if secs is None or secs < 0 or secs > 315360000:
            base["errors"].append("uptime out of range")
        else:
            base["uptime_seconds"] = secs
    else:
        base["errors"].append("missing uptime")
    found_ram = re.search(_RAM_PATTERN, text, re.IGNORECASE)
    if found_ram:
        try:
            number = float(found_ram.group(1))
        except (TypeError, ValueError):
            number = None
        unit = (found_ram.group(2) or "").lower()
        if number is None:
            base["errors"].append("bad ram value")
        elif unit in ("%", "percent"):
            if number < 0 or number > 100:
                base["errors"].append("ram percent out of range")
            else:
                base["ram_percent"] = float(number)
        elif unit in ("mb", "m"):
            if number < 0 or number > 1048576:
                base["errors"].append("ram mb out of range")
            else:
                base["ram_mb"] = float(number)
        elif unit in ("gb", "g"):
            mega = number * 1024.0
            if mega < 0 or mega > 1048576:
                base["errors"].append("ram mb out of range")
            else:
                base["ram_mb"] = float(mega)
        else:
            if number <= 100:
                base["ram_percent"] = float(number)
            else:
                if number > 1048576:
                    base["errors"].append("ram mb out of range")
                else:
                    base["ram_mb"] = float(number)
    else:
        base["errors"].append("missing ram")
    found_load = re.search(_LOAD_PATTERN, text, re.IGNORECASE)
    if found_load:
        try:
            load_value = float(found_load.group(1))
        except (TypeError, ValueError):
            load_value = None
        if load_value is None or load_value < 0 or load_value > 128:
            base["errors"].append("load out of range")
        else:
            base["load_1m"] = float(load_value)
    else:
        base["errors"].append("missing load")
    has_ram = base["ram_percent"] is not None or base["ram_mb"] is not None
    if base["uptime_seconds"] is not None and has_ram and base["load_1m"] is not None:
        if not base["errors"]:
            base["valid"] = True
    return base


def check_did_format(did_text):
    if not isinstance(did_text, str):
        return {"did": "", "valid": False, "method": None, "reason": "not a string"}
    value = did_text.strip()
    if not value.startswith("did:"):
        return {"did": value, "valid": False, "method": None, "reason": "must start with did:"}
    parts = value.split(":")
    if len(parts) < 3:
        return {"did": value, "valid": False, "method": None, "reason": "need method and identifier"}
    method = parts[1]
    ident = ":".join(parts[2:])
    if len(method) < 2 or len(method) > 20:
        return {"did": value, "valid": False, "method": None, "reason": "method length must be 2-20"}
    if re.match(r"^[a-z0-9]+$", method) is None:
        return {"did": value, "valid": False, "method": None, "reason": "method must be lowercase alphanumeric"}
    if " " in value or "\t" in value or "\n" in value:
        return {"did": value, "valid": False, "method": method, "reason": "whitespace not allowed"}
    if len(ident) < 8 or len(ident) > 128:
        return {"did": value, "valid": False, "method": method, "reason": "identifier length must be 8-128"}
    if re.match(r"^[A-Za-z0-9._\-:]+$", ident) is None:
        return {"did": value, "valid": False, "method": method, "reason": "identifier has bad characters"}
    return {"did": value, "valid": True, "method": method, "reason": "syntax ok; no proof checked"}


def sha256_hex(data):
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, bytes):
        raw = data
    else:
        raw = str(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_manifest(file_map):
    files = {}
    if not isinstance(file_map, dict):
        return {"version": 1, "files": files}
    for name in sorted(file_map.keys()):
        content = file_map[name]
        if isinstance(content, str):
            raw = content.encode("utf-8")
        elif isinstance(content, bytes):
            raw = content
        else:
            raw = str(content).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        files[name] = {"sha256": digest, "size": len(raw)}
    return {"version": 1, "files": files}


def write_manifest_file(manifest, output_path):
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return output_path


def verify_manifest(manifest, file_map):
    mismatched = []
    missing = []
    if not isinstance(manifest, dict):
        return {"ok": False, "mismatched": [], "missing": [], "extra": []}
    expected = manifest.get("files", {})
    if not isinstance(expected, dict):
        expected = {}
    if not isinstance(file_map, dict):
        file_map = {}
    for name in sorted(expected.keys()):
        if name not in file_map:
            missing.append(name)
            continue
        content = file_map[name]
        if isinstance(content, str):
            raw = content.encode("utf-8")
        elif isinstance(content, bytes):
            raw = content
        else:
            raw = str(content).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        want = expected[name]
        if not isinstance(want, dict):
            mismatched.append(name)
            continue
        if want.get("sha256") != digest or want.get("size") != len(raw):
            mismatched.append(name)
    extra = []
    for key in file_map.keys():
        if key not in expected:
            extra.append(key)
    extra.sort()
    ok = len(mismatched) == 0 and len(missing) == 0
    return {"ok": ok, "mismatched": mismatched, "missing": missing, "extra": extra}


class CursorPoller:
    def __init__(self, fetch_fn, checkpoint_path=None, wait_seconds=10, start_seq=0):
        self.fetch_fn = fetch_fn
        self.checkpoint_path = checkpoint_path
        try:
            wait_value = int(wait_seconds)
        except (TypeError, ValueError):
            wait_value = 10
        if wait_value < 1:
            wait_value = 1
        if wait_value > 60:
            wait_value = 60
        self.wait_seconds = wait_value
        try:
            start_value = int(start_seq)
        except (TypeError, ValueError):
            start_value = 0
        if start_value < 0:
            start_value = 0
        self.cursor = start_value
        if self.checkpoint_path is not None:
            self.cursor = self.load_checkpoint()

    def load_checkpoint(self):
        if self.checkpoint_path is None:
            return self.cursor
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            return self.cursor
        if not raw:
            return self.cursor
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                return max(self.cursor, int(raw))
            except (TypeError, ValueError):
                return self.cursor
        try:
            if isinstance(data, dict) and "seq" in data:
                value = int(data["seq"])
                if value > self.cursor:
                    return value
                return self.cursor
            if isinstance(data, int):
                if data > self.cursor:
                    return data
                return self.cursor
        except (TypeError, ValueError):
            return self.cursor
        return self.cursor

    def save_checkpoint(self):
        if self.checkpoint_path is None:
            return self.cursor
        try:
            with open(self.checkpoint_path, "w", encoding="utf-8") as handle:
                json.dump({"seq": self.cursor}, handle)
        except OSError:
            pass
        return self.cursor

    def poll_once(self):
        result = self.fetch_fn(self.cursor, self.wait_seconds)
        messages = []
        next_seq = self.cursor
        if isinstance(result, tuple) and len(result) == 2:
            messages, next_seq = result
        elif isinstance(result, dict):
            messages = result.get("messages", [])
            next_seq = result.get("next_seq", self.cursor)
        if messages is None:
            messages = []
        try:
            next_value = int(next_seq)
        except (TypeError, ValueError):
            next_value = self.cursor
        best = next_value
        for item in messages:
            if not isinstance(item, dict):
                continue
            try:
                seq = int(item.get("seq", best))
            except (TypeError, ValueError):
                continue
            if seq > best:
                best = seq
        if best > self.cursor:
            self.cursor = best
        self.save_checkpoint()
        return messages

    def poll_many(self, max_batches=3, pause_seconds=0.0):
        try:
            total = int(max_batches)
        except (TypeError, ValueError):
            total = 3
        if total < 1:
            total = 1
        collected = []
        for _ in range(total):
            batch = self.poll_once()
            collected.extend(batch)
            try:
                pause = float(pause_seconds)
            except (TypeError, ValueError):
                pause = 0.0
            if pause > 0:
                time.sleep(pause)
            if not batch:
                break
        return collected
