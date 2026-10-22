"""Public reset event contract; detection and future task execution are separate."""
from datetime import datetime, timezone
import re


def make_event(post):
    post_id = str(post["id"])
    if not re.fullmatch(r"[0-9]{1,25}", post_id):
        raise ValueError("The original post ID must contain 1 to 25 ASCII digits; the event cannot be saved.")
    kind = post.get("kind", "reset_completed")
    if kind not in ("reset_completed", "reset_scheduled", "reset_related"):
        raise ValueError("Unsupported event type.")
    return {
        "schema_version": 1,
        "id": f"{kind}_{post_id}",
        "type": kind,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "author": "thsottiaux",
        "post_id": post_id,
        "post_url": post["url"],
        "announced_at": post.get("created_at", ""),
        "text": post["text"],
        "context": post.get("context", ""),
        "source": post.get("source", "community"),
        "source_metadata": post.get("source_metadata", ""),
        "personal_quota_verified": False,
    }
