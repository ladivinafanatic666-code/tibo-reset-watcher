"""Read the documented, public TIBO community feed (not a full X timeline)."""

import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


STATUS_URL = "https://tibo.cc/api/v1/status"
HISTORY_URL = "https://tibo.cc/api/v1/resets?limit=100"
DOCUMENTATION_URL = "https://tibo.cc/guide/api"
DIRECT_STATUS_URL = "https://codex-resets.com/api/v1/status"
DIRECT_HISTORY_URL = "https://codex-resets.com/api/v1/resets?limit=100"


def _payload(response, label, expected_type):
    if not isinstance(response, dict):
        raise RuntimeError(f"The community {label} response has an invalid JSON structure; read progress was not updated.")
    if response.get("error"):
        raise RuntimeError(f"The community {label} response reports a data error. Try again later.")
    if response.get("stale") is not False:
        raise RuntimeError(f"The community {label} cache is stale or its freshness is unknown. Try again later.")
    data = response.get("data")
    if not isinstance(data, expected_type):
        raise RuntimeError(f"The community {label} response has no valid data; read progress was not updated.")
    return data


def _metadata(document):
    fetched_at = document.get("fetched_at") or document.get("last_successful_update")
    return (
        "Community aggregator: https://tibo.cc; original data: https://codex-resets.com.\n"
        f"Last successful community fetch: {fetched_at or 'not provided'}; response stale=false.\n"
        "Community records may be delayed or incomplete. A preview does not confirm a completed reset, "
        "and a public record does not verify your personal quota."
    )


def _direct_payload(response, label, expected_type):
    if not isinstance(response, dict) or response.get("error") or response.get("stale") is True:
        raise RuntimeError(f"The direct community {label} response contains an error or invalid structure; read progress was not updated.")
    meta = response.get("meta")
    if not isinstance(meta, dict) or meta.get("api_version") != "v1":
        raise RuntimeError(f"The direct community {label} response has no valid v1 metadata; read progress was not updated.")
    generated_at = meta.get("generated_at")
    try:
        timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("missing timezone")
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError(f"The direct community {label} response timestamp is invalid; read progress was not updated.") from None
    data = response.get("data")
    if not isinstance(data, expected_type):
        raise RuntimeError(f"The direct community {label} response has no valid data; read progress was not updated.")
    return data


def _direct_metadata(document):
    retrieved_at = datetime.now(timezone.utc).isoformat()
    generated_at = document["meta"]["generated_at"]
    return (
        "Direct community source: https://codex-resets.com; API: https://codex-resets.com/api/docs.\n"
        f"Response retrieved at: {retrieved_at}; source response generated at: {generated_at}.\n"
        "The response generation time is not the event time or the source data update time. "
        "The source provides no independent freshness information; freshness is unknown.\n"
        "Community records may be delayed or incomplete. A preview does not confirm a completed reset, "
        "and a public record does not verify your personal quota."
    )


def _post(record, context, source_metadata, kind, provider):
    if not isinstance(record, dict):
        raise RuntimeError("A community record has an invalid structure; read progress was not updated.")
    source = record.get("source")
    if not isinstance(source, dict):
        return None
    author = source.get("author")
    if not isinstance(author, str) or author.lstrip("@").casefold() != "thsottiaux":
        return None
    original_url = source.get("url")
    if not isinstance(original_url, str):
        return None
    try:
        url = urlsplit(original_url)
        if url.scheme != "https" or url.netloc.casefold() not in ("x.com", "www.x.com"):
            return None
        matched = re.fullmatch(r"/thsottiaux/status/([0-9]{1,25})/?", url.path, flags=re.IGNORECASE)
        if not matched:
            return None
    except ValueError:
        return None
    post_id = matched.group(1)
    record_id = record.get("id")
    if record_id is not None and str(record_id).isdigit() and str(record_id) != post_id:
        raise RuntimeError("A community post ID does not match its original URL; read progress was not updated.")
    text = record.get("text")
    created_at = record.get("announced_at")
    if not isinstance(text, str) or not text.strip() or not isinstance(created_at, str) or not created_at.strip():
        raise RuntimeError("A community post is missing its text or publication time; read progress was not updated.")
    _publication_time(created_at)
    return {
        "id": post_id,
        "text": text,
        "url": f"https://x.com/thsottiaux/status/{post_id}",
        "created_at": created_at,
        "matches": True,
        "context": context,
        "source_metadata": source_metadata,
        "provider": provider,
        "kind": kind,
    }


def _publication_time(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("missing timezone")
        return timestamp
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("A community post has an invalid publication time; read progress was not updated.") from None


def _status_candidates(status):
    candidates = []
    for field, context, kind in (("latest_reset", "Community-recorded reset announcement", "reset_completed"), ("scheduled_reset", "Community-recorded reset preview (completion not yet confirmed)", "reset_scheduled")):
        if field not in status:
            raise RuntimeError(f"The community status response is missing {field}; read progress was not updated.")
        record = status[field]
        if record is not None:
            candidates.append((record, context, kind))
    return candidates


def _fetch_provider(fetch_json_callback, status_url, history_url, payload_reader, metadata_reader, provider):
    status_response = fetch_json_callback(status_url)
    status = payload_reader(status_response, "status", dict)
    # Validate each successful response before the next request. A malformed
    # primary record must not be hidden by a later history connection failure.
    status_metadata = metadata_reader(status_response)
    candidates = [_post(record, context, status_metadata, kind, provider)
                  for record, context, kind in _status_candidates(status)]
    history_response = fetch_json_callback(history_url)
    history = payload_reader(history_response, "history", list)
    history_metadata = metadata_reader(history_response)
    candidates.extend(_post(record, "Community-recorded reset announcement", history_metadata, "reset_completed", provider)
                      for record in history)
    posts = {}
    for post in candidates:
        if post is not None:
            previous = posts.get(post["id"])
            # Status and history can briefly disagree during reclassification.
            # Preserve a completed record so completed-only consumers see it.
            if previous is None or (previous["kind"] == "reset_scheduled" and post["kind"] == "reset_completed"):
                posts[post["id"]] = post
    if not posts:
        raise RuntimeError("The community source has no verifiable original posts by @thsottiaux; read progress was not updated.")
    return sorted(posts.values(), key=lambda post: (_publication_time(post["created_at"]), int(post["id"])))


def fetch_posts(fetch_json_callback):
    """Read verified community records, with network-only direct-source fallback.

    HTTP denials/rate limits and invalid or stale primary data fail closed.
    Only a connection failure switches BOTH datasets to the documented source;
    the same IDs preserve the caller's existing baseline and delivery progress.
    """
    try:
        return _fetch_provider(fetch_json_callback, STATUS_URL, HISTORY_URL,
                               _payload, _metadata, "tibo.cc")
    except HTTPError:
        raise
    except (URLError, TimeoutError, ConnectionError):
        return _fetch_provider(fetch_json_callback, DIRECT_STATUS_URL, DIRECT_HISTORY_URL,
                               _direct_payload, _direct_metadata, "codex-resets.com")
