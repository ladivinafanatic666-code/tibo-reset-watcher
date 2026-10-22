"""Local reset-event receiver. Remote JSON is data; only a local preset is queued."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent
EVENT_ID = re.compile(r"(?:reset_completed|automation_test)_[0-9]{1,25}\Z")


class BridgeError(RuntimeError):
    pass


def folder():
    return ROOT / "data" / "codex-bridge"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def validate_config(cfg):
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+\.git", cfg.get("repo_url", "")):
        raise BridgeError("repo_url must be an HTTPS GitHub repository without credentials.")
    try:
        if str(uuid.UUID(cfg.get("thread_id", ""))) != cfg["thread_id"].lower():
            raise ValueError()
    except (ValueError, TypeError, AttributeError, KeyError):
        raise BridgeError("thread_id must be an exact session UUID.") from None
    instruction = cfg.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 12000:
        raise BridgeError("instruction must be a nonempty local preset of at most 12000 characters.")
    interval = cfg.get("poll_seconds", 15)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 3600:
        raise BridgeError("poll_seconds must be an integer between 5 and 3600.")
    executable = cfg.get("codex_path", "")
    if not isinstance(executable, str) or not Path(executable).is_absolute() or not Path(executable).is_file():
        raise BridgeError("codex_path must identify an existing absolute local executable.")


def load_config():
    cfg = read_json(folder() / "config.json", {})
    if not isinstance(cfg, dict):
        raise BridgeError("Local bridge configuration is invalid.")
    cfg.setdefault("poll_seconds", 15)
    validate_config(cfg)
    return cfg


def _git(*args, timeout=45):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.interactive=false", *args],
                            capture_output=True, timeout=timeout, env=env)
    if result.returncode:
        raise BridgeError(f"Git source operation failed (exit {result.returncode}); no event was executed.")
    return result.stdout


def fetch_snapshot(cfg):
    """Fetch blobs from one branch; never check out or execute repository files."""
    validate_config(cfg)
    cache = folder() / "cache.git"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / "HEAD").exists():
        _git("init", "--bare", str(cache))
    prefix = ("--git-dir=" + str(cache),)
    _git(*prefix, "fetch", "--no-tags", "--depth=1", "--no-recurse-submodules",
         cfg["repo_url"], "refs/heads/reset-watcher-state")
    listing = _git(*prefix, "ls-tree", "-r", "-z", "--full-tree", "FETCH_HEAD", "--", "data/events/")
    snapshot = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, path_bytes = entry.split(b"\t", 1)
        path = path_bytes.decode("utf-8")
        relative = path.removeprefix("data/events/")
        if "/" in relative or not relative.endswith(".json"):
            continue
        mode, kind, _ = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise BridgeError("Event path is not a regular JSON blob.")
        if len(snapshot) >= 10000:
            raise BridgeError("Too many event files; refusing to advance the baseline.")
        content = _git(*prefix, "show", "FETCH_HEAD:" + path)
        if len(content) > 1_000_000:
            raise BridgeError("Event JSON is too large.")
        try:
            snapshot[relative[:-5]] = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise BridgeError("An event JSON blob is malformed; no progress was advanced.") from None
    return snapshot


def event_problem(event_id, event, allow_test):
    if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
        return "unsupported_event_filename"
    if not isinstance(event, dict) or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        return "unsupported_event_schema"
    if event.get("id") != event_id:
        return "event_id_filename_mismatch"
    kind, number = event_id.rsplit("_", 1)
    if event.get("type") != kind:
        return "event_type_mismatch"
    if kind == "automation_test":
        return None if allow_test else "test_events_disabled"
    if event.get("author") != "thsottiaux" or event.get("post_id") != number:
        return "unverified_reset_author_or_id"
    if event.get("post_url") != "https://x.com/thsottiaux/status/" + number:
        return "unverified_reset_url"
    return None


def queue_message(cfg):
    """The remote event is intentionally absent from this command's arguments."""
    result = subprocess.run([cfg["codex_path"], "queue", "--thread", cfg["thread_id"],
                             "--message", cfg["instruction"]], capture_output=True, timeout=30)
    if result.returncode:
        raise BridgeError(f"Codex queue did not confirm success (exit {result.returncode}).")
    output = result.stdout.decode("utf-8", errors="replace")
    matched = re.search(r"Queued message\s+([0-9a-fA-F-]{36})\b", output)
    if not matched:
        raise BridgeError("Codex did not return a queue acknowledgement; review this attempt before retrying.")
    try:
        return str(uuid.UUID(matched.group(1)))
    except ValueError:
        raise BridgeError("Codex returned an invalid queue acknowledgement; review this attempt before retrying.") from None


def process_snapshot(cfg, snapshot, allow_test=False, stop_after_one=False, queue_fn=None):
    """Durably claim before sending; ambiguous outcomes require manual review."""
    path = folder() / "state.json"
    state = read_json(path, {"version": 1, "initialized": False, "events": {}, "queued_count": 0})
    if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
            or type(state.get("initialized")) is not bool or type(state.get("queued_count")) is not int
            or state["queued_count"] < 0 or not isinstance(state.get("events"), dict)
            or any(not isinstance(record, dict) or record.get("status") not in
                   ("baseline", "blocked", "claimed", "needs_review", "queued")
                   for record in state["events"].values())):
        raise BridgeError("Bridge state is invalid; preserve it for review.")
    if not isinstance(snapshot, dict):
        raise BridgeError("Source snapshot is invalid.")
    result = {"initialized": True, "baseline": not state["initialized"], "queued": 0,
              "queued_count": state["queued_count"], "last_error": ""}
    # A crashed attempt might have reached Codex before its acknowledgement.
    for record in state["events"].values():
        if record.get("status") == "claimed":
            record.update(status="needs_review", reason="previous_attempt_unconfirmed")
    result["needs_review_count"] = sum(r.get("status") == "needs_review" for r in state["events"].values())
    if result["needs_review_count"]:
        result["last_error"] = "A previous queue attempt needs review; it will not be retried automatically."
    if not state["initialized"]:
        for event_id, event in snapshot.items():
            problem = event_problem(event_id, event, allow_test)
            state["events"][event_id] = {"status": "blocked" if problem else "baseline", "reason": problem}
        state["initialized"] = True
        write_json(path, state)
        return result
    write_json(path, state)
    for event_id, event in sorted(snapshot.items()):
        if event_id in state["events"]:
            continue
        problem = event_problem(event_id, event, allow_test)
        if problem:
            state["events"][event_id] = {"status": "blocked", "reason": problem}
            write_json(path, state)
            continue
        state["events"][event_id] = {"status": "claimed", "claimed_at": stamp()}
        write_json(path, state)
        result["last_event_id"] = event_id
        try:
            queue_id = (queue_fn or queue_message)(cfg)
        except Exception as exc:
            state["events"][event_id].update(status="needs_review", reason=type(exc).__name__)
            result["needs_review_count"] += 1
            result["last_error"] = "Queue attempt needs review; it will not be retried automatically."
            write_json(path, state)
            break
        state["events"][event_id].update(status="queued", queued_at=stamp(), queue_id=queue_id)
        state["queued_count"] += 1
        write_json(path, state)
        result.update(queued=result["queued"] + 1, queued_count=state["queued_count"], last_queue_id=queue_id)
        if stop_after_one:
            break
    return result


class SingleInstance:
    def __enter__(self):
        path = folder() / "receiver.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        if path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise BridgeError("A local receiver is already running.") from None
        return self

    def __exit__(self, *_):
        self.handle.close()


def run(cfg, watch=False, allow_test=False, stop_after_one=False, max_wait_seconds=None):
    validate_config(cfg)
    if max_wait_seconds is not None and max_wait_seconds <= 0:
        raise BridgeError("max_wait_seconds must be positive.")
    limit = max_wait_seconds if max_wait_seconds is not None else (600 if allow_test else None)
    status_path, stop_path = folder() / "status.json", folder() / "stop.request"
    status = {"running": True, "initialized": False, "heartbeat_at": stamp(), "last_error": "",
              "queued_count": 0, "exit_reason": None}
    started = time.monotonic()
    with SingleInstance():
        stop_path.unlink(missing_ok=True)
        try:
            while True:
                try:
                    result = process_snapshot(cfg, fetch_snapshot(cfg), allow_test, stop_after_one)
                    status.update(result)
                except Exception as exc:
                    status["last_error"] = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
                status["heartbeat_at"] = stamp()
                write_json(status_path, status)
                if stop_after_one and status.get("queued", 0):
                    status["exit_reason"] = "event_queued"
                    break
                if not watch:
                    status["exit_reason"] = "check_completed"
                    break
                if stop_path.exists():
                    status["exit_reason"] = "stop_requested"
                    break
                if limit is not None and time.monotonic() - started >= limit:
                    status["exit_reason"] = "timeout"
                    break
                deadline = time.monotonic() + cfg.get("poll_seconds", 15)
                while time.monotonic() < deadline and not stop_path.exists():
                    if limit is not None and time.monotonic() - started >= limit:
                        break
                    time.sleep(0.5)
        finally:
            status.update(running=False, heartbeat_at=stamp())
            write_json(status_path, status)
            stop_path.unlink(missing_ok=True)
    return status


def main():
    parser = argparse.ArgumentParser(description="Queue a local Codex preset after a new reset event.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--allow-test", action="store_true")
    parser.add_argument("--stop-after-one", action="store_true")
    parser.add_argument("--max-wait-seconds", type=int)
    args = parser.parse_args()
    try:
        result = run(load_config(), args.watch, args.allow_test, args.stop_after_one, args.max_wait_seconds)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("last_error") else (2 if result.get("exit_reason") == "timeout" else 0)
    except Exception as exc:
        print(str(exc) if isinstance(exc, BridgeError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
