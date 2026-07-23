from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from app.config import LogConfig


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_plaintext_regex(raw: str, log_config: LogConfig) -> dict:
    if log_config.pattern:
        m = re.match(log_config.pattern, raw)
        if m:
            groups = m.groupdict()
            return {
                "timestamp": groups.get("timestamp") or _now_iso(),
                "level": groups.get("level"),
                "message": groups.get("message", raw),
            }
    # No pattern configured, or this particular line didn't match it.
    # Still capture the line rather than drop it -- an unparseable line is
    # exactly the kind of thing you don't want to silently lose.
    return {"timestamp": _now_iso(), "level": None, "message": raw}


def parse_json_lines(raw: str, log_config: LogConfig) -> dict:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"timestamp": _now_iso(), "level": None, "message": raw}
    if not isinstance(obj, dict):
        return {"timestamp": _now_iso(), "level": None, "message": raw}
    timestamp = obj.get("timestamp") or obj.get("time") or obj.get("ts") or _now_iso()
    level = obj.get("level") or obj.get("lvl") or obj.get("severity")
    message = obj.get("message") or obj.get("msg") or raw
    return {"timestamp": str(timestamp), "level": level, "message": str(message)}


_PARSERS = {
    "plaintext_regex": parse_plaintext_regex,
    "json_lines": parse_json_lines,
}


def parse_line(raw: str, log_config: LogConfig) -> dict:
    parser_fn = _PARSERS.get(log_config.parser)
    if parser_fn is None:
        # Unknown parser name in config -- fail safe (store raw), don't crash
        # the watcher over a config typo.
        return {"timestamp": _now_iso(), "level": None, "message": raw}
    return parser_fn(raw, log_config)
