"""Conversation time is independent of filesystem change detection."""
from datetime import datetime, timezone
import math


def activity_fields(timestamps, fallback):
    """Use the latest valid message timestamp, falling back to source modification time."""
    values = []
    for value in timestamps:
        try:
            if isinstance(value, str):
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                value = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
            value = float(value)
            if math.isfinite(value) and value > 0:
                values.append(value)
        except (TypeError, ValueError, OverflowError):
            continue
    latest = max(values, default=fallback)
    return {"activity_at": latest,
            "activity_at_iso": datetime.fromtimestamp(latest, timezone.utc).isoformat()}


def activity_time(meta):
    return meta.get("activity_at") or meta.get("mtime") or 0
