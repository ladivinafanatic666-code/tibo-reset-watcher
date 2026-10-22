"""Tibo reset email watcher. Python 3.12+, standard library only."""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "source": "community", "interval_seconds": 300,
    "recipient": "your-email@example.com",
    "smtp_host": "smtp.gmail.com", "smtp_port": 465,
    "smtp_security": "SSL", "smtp_user": "your-email@example.com",
    "event_mode": "completed", "email_enabled": True,
}
LOG = logging.getLogger("reset-watcher")
RESET_RE = re.compile(r"\bresets?\b|\u91cd\u7f6e", re.I)


def data_dir():
    return Path(os.environ["RESET_DATA_DIR"]) if os.environ.get("RESET_DATA_DIR") else ROOT / "data"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default):
    if not path.exists():
        return default
    # Never silently discard broken delivery state: doing so can lose pending mail.
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_config():
    cfg = DEFAULT_CONFIG | read_json(ROOT / "config.json", {})
    overrides = {"RESET_SOURCE": "source", "RESET_EVENT_MODE": "event_mode",
                 "ALERT_TO": "recipient", "SMTP_USER": "smtp_user", "SMTP_HOST": "smtp_host",
                 "SMTP_SECURITY": "smtp_security", "SMTP_PORT": "smtp_port",
                 "RESET_INTERVAL_SECONDS": "interval_seconds", "RESET_EMAIL_ENABLED": "email_enabled"}
    for env, key in overrides.items():
        if env not in os.environ:
            continue
        value = os.environ[env].strip()
        if key in ("smtp_port", "interval_seconds"):
            value = int(value)
        elif key == "email_enabled":
            if value.lower() not in ("true", "false", "1", "0"):
                raise ValueError("RESET_EMAIL_ENABLED must be true or false.")
            value = value.lower() in ("true", "1")
        cfg[key] = value
    return cfg


def validate_config(cfg, need_mail=True):
    if cfg.get("source") not in ("community", "x_api"):
        raise ValueError("Choose the community source or the official X API.")
    if cfg.get("event_mode", "completed") not in ("completed", "all"):
        raise ValueError("event_mode must be completed or all.")
    if not isinstance(cfg.get("email_enabled", True), bool):
        raise ValueError("email_enabled must be a boolean.")
    interval = cfg.get("interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, int) or not 60 <= interval <= 86400:
        raise ValueError("The check interval must be an integer from 60 to 86400 seconds.")
    for key in ("recipient", "smtp_user"):
        value = cfg.get(key, "")
        if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
            raise ValueError("The recipient and sender must each be a single, complete email address.")
    if not cfg.get("smtp_host") or re.search(r"[\s/\\]", cfg["smtp_host"]):
        raise ValueError("Enter an SMTP hostname, such as smtp.gmail.com.")
    port = cfg.get("smtp_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The SMTP port must be an integer from 1 to 65535.")
    if cfg.get("smtp_security") not in ("SSL", "STARTTLS"):
        raise ValueError("Email connections must use SSL or STARTTLS.")
    if cfg["source"] == "x_api" and not get_secret("x_bearer_token"):
        raise ValueError("The official X API requires a Bearer Token.")
    if need_mail and cfg.get("email_enabled", True) and not get_secret("smtp_password"):
        raise ValueError("Configure an email app password or SMTP authorization token.")


def save_config(cfg):
    safe = {key: cfg.get(key, value) for key, value in DEFAULT_CONFIG.items()}
    validate_config(safe, need_mail=False)
    write_json(ROOT / "config.json", safe)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Local credential storage requires Windows account encryption. Use environment variables on other systems.")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    input_blob = _Blob(len(data), buffer)
    output_blob = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        func = crypt.CryptUnprotectData
        func.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = func(ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob))
    else:
        func = crypt.CryptProtectData
        func.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = func(ctypes.byref(input_blob), "Tibo reset watcher", None, None, None, 1,
                  ctypes.byref(output_blob))
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows account encryption failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def set_secret(name, value):
    if name not in ("smtp_password", "x_bearer_token"):
        raise ValueError("Unknown credential name")
    path = data_dir() / "secrets.json"
    secrets = read_json(path, {})
    if value:
        secrets[name] = base64.b64encode(_dpapi(value.encode("utf-8"))).decode("ascii")
    else:
        secrets.pop(name, None)
    write_json(path, secrets)


def get_secret(name):
    env_name = {"smtp_password": "SMTP_PASSWORD", "x_bearer_token": "X_BEARER_TOKEN"}.get(name)
    if env_name and env_name in os.environ:
        return os.environ[env_name]
    encrypted = read_json(data_dir() / "secrets.json", {}).get(name)
    if not encrypted:
        return ""
    return _dpapi(base64.b64decode(encrypted), decrypt=True).decode("utf-8")


def setup_logging():
    if LOG.handlers:
        return
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "watcher.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def error_text(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Email authentication failed. For Gmail, use an app password after enabling two-step verification. Other providers may require an SMTP authorization token."
    if isinstance(exc, urllib.error.HTTPError):
        return {401: "Source authentication failed. Check the X Bearer Token.",
                402: "The X API account may need credits or API access. Check the developer console.",
                403: "The source denied access. Check API permissions or try again later.",
                429: "The source rate limit was reached. The watcher will retry later."}.get(
                    exc.code, f"The source returned HTTP {exc.code}. Try again later.")
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "Connection failed. Check the network, system proxy, and access to the source or email server."
    if isinstance(exc, smtplib.SMTPException):
        return "The email server did not confirm delivery. Check the email settings. Pending notifications will be retained for retry."
    if isinstance(exc, json.JSONDecodeError):
        return "A data file or source response contains invalid JSON. Check the log. Keep state.json to preserve pending notifications."
    if isinstance(exc, (ValueError, RuntimeError)):
        return str(exc)
    if isinstance(exc, OSError):
        return f"A system or connection operation failed (error {getattr(exc, 'winerror', None) or exc.errno}). Check the network and file permissions."
    return f"The operation failed ({type(exc).__name__}). Check the settings and retry."


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "TiboResetEmailWatcher/1.0 (personal-notifications)",
        "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise RuntimeError("The source response is too large. This check was stopped.")
    return json.loads(raw.decode("utf-8-sig"))


def fetch_x(cfg, cursor=None):
    token = get_secret("x_bearer_token")
    if not token:
        raise ValueError("Configure an official X API Bearer Token first.")
    params = {
        "query": "from:thsottiaux -is:retweet", "max_results": "100",
        "tweet.fields": "created_at,referenced_tweets,edit_history_tweet_ids,note_tweet",
        "expansions": "referenced_tweets.id",
    }
    if cursor:
        params["since_id"] = str(cursor)
    posts, newest = [], cursor
    for page in range(20):
        result = fetch_json("https://api.x.com/2/tweets/search/recent?" +
                            urllib.parse.urlencode(params), {"Authorization": "Bearer " + token})
        if "errors" in result:
            raise RuntimeError("X returned partial query errors. Progress was not saved. Check API permissions and retry.")
        if not isinstance(result.get("meta"), dict):
            raise RuntimeError("The X response format has changed. Progress was not saved.")
        parents = {p["id"]: p for p in result.get("includes", {}).get("tweets", [])}
        for item in result.get("data", []):
            post_id = str(item["id"])
            newest = str(max(int(newest or 0), int(post_id)))
            text = item.get("note_tweet", {}).get("text") or item["text"]
            contexts = []
            for ref in item.get("referenced_tweets", []):
                parent = parents.get(ref["id"], {})
                parent_text = parent.get("note_tweet", {}).get("text") or parent.get("text", "")
                if parent_text and RESET_RE.search(parent_text):
                    contexts.append(parent_text)
            posts.append({"id": str(item.get("edit_history_tweet_ids", [post_id])[0]),
                          "text": text, "created_at": item.get("created_at", ""),
                          "url": f"https://x.com/thsottiaux/status/{post_id}",
                          "context": "\n\n".join(contexts),
                          "kind": classify_x_post(text, "\n".join(contexts)),
                          "matches": bool(RESET_RE.search(text) or contexts)})
        next_token = result["meta"].get("next_token")
        if not next_token:
            return posts, newest
        params["next_token"] = next_token
    raise RuntimeError("X returned more than 20 pages. Progress was not advanced. Contact the maintainer.")


def fetch_community(cfg):
    # The free source adapter is kept separate so API changes are easy to repair.
    from community import fetch_posts
    return fetch_posts(fetch_json), None


def fetch_posts(cfg, cursor=None):
    return fetch_x(cfg, cursor) if cfg["source"] == "x_api" else fetch_community(cfg)


def classify_x_post(text, context=""):
    """Conservative local rule, never promises to infer every implicit announcement."""
    if not RESET_RE.search(text + "\n" + context):
        return "unrelated"
    # Inspect assertions, not URL query strings or an unrelated "enjoy".
    body = re.sub(r"https?://\S+", "", text).replace("\u2019", "'")
    if re.search(r"[?\uff1f]|\b(?:no|not|never|without|whether|if|haven't|hasn't|isn't|aren't|didn't|"
                 r"can't|cannot|won't|wasn't|weren't)\b|\u5c1a\u672a|\u8fd8\u672a|\u8fd8\u6ca1|\u6ca1\u6709|\u5e76\u672a|\u672a\u80fd|"
                 r"\u4e0d\u4f1a|\u4e0d\u662f|\u672a\u91cd\u7f6e|\u672a\u5b8c\u6210|\u672a\u4f20\u64ad|\u662f\u5426|\u5417", body, re.I) or re.match(
                     r"\s*(?:has|have|is|are|was|were|did|does|do|can|could|will|would|when|why|how)\b", body, re.I):
        return "reset_related"
    if re.fullmatch(r"\s*(?:done|all done|done and dusted)[.!\s]*", body, re.I) and RESET_RE.search(context):
        return "reset_completed"
    future = re.compile(r"\b(?:tomorrow|soon|coming|will|would|may|might|could|should|"
                        r"planned|planning|expect|expected|possible|maybe|next)\b|\u5373\u5c06|\u660e\u5929|\u9884\u8ba1|\u53ef\u80fd", re.I)
    linking = r"(?:\s+(?:is|are|was|were|has|have|been|now|all|fully|successfully|already|just|finally)){0,7}"
    completion = re.compile(
        r"\bresets?\b\s*[:\uff1a-]?" + linking + r"\s+(?:propagated|landed|completed|complete|applied|live|done)\b|"
        r"\b(?:completed|applied|propagated)\s+(?:the\s+)?(?:weekly\s+)?resets?\b"
        r"(?!\s+(?:documentation|docs|guide|example|tests?|button|feature|implementation|jokes?)\b)|"
        r"\u5df2(?:\u5168\u90e8)?\u91cd\u7f6e|\u91cd\u7f6e(?:\u5df2)?\u5b8c\u6210|\u5df2\u5b8c\u6210\u91cd\u7f6e", re.I)
    grant = re.compile(
        r"\b(?:added|granted)\s+(?:(?:a|an|one|another|new|free|banked|weekly|usage|additional)\s+){0,5}"
        r"resets?\s+(?:to|for)\b[^.!?\n]{0,100}\b(?:accounts?|users?|everyone|everybody|you|all)\b", re.I)
    for clause in re.split(r"[.!;,\n]+", body):
        if RESET_RE.search(clause) and not future.search(clause):
            if completion.search(clause) or grant.search(clause):
                return "reset_completed"
    if future.search(body):
        return "reset_scheduled"
    return "reset_related"


def eligible_posts(cfg, posts):
    if cfg.get("event_mode", "completed") == "completed":
        return [p for p in posts if p.get("kind", "reset_completed") == "reset_completed"]
    return posts


def preview_source(cfg):
    validate_config(cfg, need_mail=False)
    posts, _ = fetch_posts(cfg)
    matched = [p for p in eligible_posts(cfg, posts) if p.get("matches", True)]
    return {"source": cfg["source"], "fetched": len(posts), "matched": len(matched),
            "providers": sorted({p["provider"] for p in posts if p.get("provider")}),
            "posts": sorted(matched, key=lambda p: p.get("created_at", ""), reverse=True)[:5],
            "detail": "Community records may be delayed or incomplete. Notifications include original post links for verification." if cfg["source"] == "community"
                      else "Queries posts from the last 7 days. Completed mode requires an explicit reset completion statement; local rules may miss some posts."}


def build_message(cfg, post=None, test=False):
    message = EmailMessage()
    message["From"] = cfg["smtp_user"]
    message["To"] = cfg["recipient"]
    message["Date"] = formatdate(localtime=False)
    if test:
        message["Subject"] = "[Tibo reset alert] Test email"
        message["Message-ID"] = f"<test-{time.time_ns()}@reset-watcher.local>"
        message.set_content("Email alerts are configured successfully.\n\n"
                            "In the desktop app, click Start monitor to begin monitoring, "
                            "or enable the scheduled workflow for cloud monitoring.\n"
                            "The first successful check records a baseline without sending historical posts.\n"
                            "Desktop monitoring requires the computer to remain on, signed in, and online. "
                            "Community records may be delayed or incomplete.\n")
        return message
    message["Subject"] = ("[Tibo reset alert] New reset announcement recorded" if post.get("kind", "reset_completed") == "reset_completed"
                          else "[Tibo reset alert] New reset-related post")
    digest = hashlib.sha256((cfg["recipient"] + post["id"]).encode()).hexdigest()[:32]
    message["Message-ID"] = f"<reset-{digest}@reset-watcher.local>"
    source = "Official X API" if post.get("source") == "x_api" else "Community records (unofficial; may be delayed)"
    body = (f"A new reset-related post from Tibo @thsottiaux was detected.\n\n"
            f"Published at: {post.get('created_at') or 'Not provided by the source'}\n"
            f"Detected at: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
            f"Source: {source}\n\n{post['text']}\n\nOriginal post: {post['url']}\n")
    if post.get("source_metadata"):
        body += f"\nSource details: {post['source_metadata']}\n"
    if post.get("context"):
        body += f"\nRelated context:\n{post['context']}\n"
    body += ("\nAlerts may describe a preview, discussion, or completion announcement. "
             "They do not verify your personal quota. Check the original post and your account.\n")
    message.set_content(body)
    return message


def send_email(cfg, message):
    password = get_secret("smtp_password")
    if not password:
        raise ValueError("No email app password or SMTP authorization token is configured.")
    if cfg["smtp_host"].lower() == "smtp.gmail.com":
        password = password.replace(" ", "")
    context = ssl.create_default_context()
    if cfg["smtp_security"] == "SSL":
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30, context=context)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
    # QUIT failure after DATA has been accepted must not cause an immediate resend.
    try:
        if cfg["smtp_security"] == "STARTTLS":
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(cfg["smtp_user"], password)
        refused = server.send_message(message)
        if refused:
            raise RuntimeError("The email server refused the recipient. The notification will be retained for retry.")
    finally:
        try:
            server.quit()
        except (OSError, smtplib.SMTPException):
            server.close()


def send_test_email(cfg):
    validate_config(cfg)
    send_email(cfg, build_message(cfg, test=True))


class SingleInstance:
    """OS file lock: released automatically on crashes, also used by --once."""
    def __enter__(self):
        path = data_dir() / "worker.lock"
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
            raise RuntimeError("The watcher is already running. Do not start another instance.") from None
        return self

    def __exit__(self, *_):
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _run_once(cfg):
    validate_config(cfg)
    path = data_dir() / "state.json"
    state = read_json(path, {"version": 1, "sources": {}, "pending": [], "delivered": []})
    if state.get("version") != 1:
        raise RuntimeError("This state file version is unsupported. Back up the data directory and contact the maintainer.")
    source = cfg["source"]
    source_state = state["sources"].get(source, {})
    result = {"fetched": 0, "matched": 0, "new": 0, "sent": 0,
              "baseline": not source_state.get("initialized", False)}
    # Retry the durable queue even when the source is temporarily unavailable.
    fetch_error = None
    try:
        posts, cursor = fetch_posts(cfg, source_state.get("cursor"))
        fetched_count = len(posts)
        posts = eligible_posts(cfg, posts)
    except Exception as exc:
        fetch_error = exc
    else:
        seen = set(source_state.get("seen", []))
        delivered = set(state["delivered"])
        queued = {p["id"] for p in state["pending"]}
        result["fetched"] = fetched_count
        result["matched"] = sum(bool(p.get("matches", True)) for p in posts)
        # On the first successful fetch, baseline the history rather than sending it.
        if not result["baseline"]:
            for post in sorted(posts, key=lambda p: p.get("created_at", "")):
                if post.get("matches", True) and post["id"] not in seen | delivered | queued:
                    state["pending"].append(post | {"source": source})
                    queued.add(post["id"])
                    result["new"] += 1
        seen.update(p["id"] for p in posts)
        state["sources"][source] = {"initialized": True, "seen": sorted(seen),
                                    "cursor": cursor}
        # Persist new items and progress together BEFORE attempting SMTP.
        write_json(path, state)
    for post in list(state["pending"]):
        # This is the independent integration boundary for a future Codex consumer.
        from event_protocol import make_event
        event = make_event(post)
        event_path = data_dir() / "events" / (event["id"] + ".json")
        if not event_path.exists():
            write_json(event_path, event)
        if cfg.get("email_enabled", True):
            send_email(cfg, build_message(cfg, post))
        state["pending"].remove(post)
        if post["id"] not in state["delivered"]:
            state["delivered"].append(post["id"])
        write_json(path, state)
        if cfg.get("email_enabled", True):
            result["sent"] += 1
        LOG.info("Event processed id=%s source=%s email=%s", post["id"], post.get("source"), cfg.get("email_enabled", True))
    if fetch_error:
        raise fetch_error
    LOG.info("Check complete source=%s fetched=%d matched=%d new=%d sent=%d baseline=%s",
             source, result["fetched"], result["matched"], result["new"], result["sent"], result["baseline"])
    return result


def run_once(cfg):
    with SingleInstance():
        return _run_once(cfg)


def run_loop():
    setup_logging()
    status_path = data_dir() / "status.json"
    stop_path = data_dir() / "stop.request"
    with SingleInstance():
        stop_path.unlink(missing_ok=True)
        status = {"running": True, "pid": os.getpid(), "started_at": now_iso(), "error": ""}
        write_json(status_path, status)
        LOG.info("Background monitoring started")
        failures = 0
        try:
            while not stop_path.exists():
                interval = 300
                retry_after = 0
                try:
                    cfg = load_config()
                    interval = cfg.get("interval_seconds", 300)
                    result = _run_once(cfg)
                    failures = 0
                    status.update(result, checked_at=now_iso(), error="", source=cfg["source"])
                except Exception as exc:
                    failures += 1
                    status.update(checked_at=now_iso(), error=error_text(exc))
                    LOG.error("Check failed %s: %s", type(exc).__name__, error_text(exc))
                    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                        try:
                            retry_after = max(0, int(exc.headers.get("Retry-After", "0")),
                                              int(exc.headers.get("x-rate-limit-reset", "0")) - int(time.time()))
                        except (TypeError, ValueError):
                            retry_after = 900
                interval = interval if isinstance(interval, int) and 60 <= interval <= 86400 else 300
                delay = min(max(interval, 60 * 2 ** min(failures, 6)), 3600) if failures else interval
                delay = max(delay, min(retry_after, 86400))
                status["heartbeat_at"] = now_iso()
                status["next_check_at"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                write_json(status_path, status)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not stop_path.exists():
                    time.sleep(1)
                    if int(time.monotonic()) % 15 == 0:
                        status["heartbeat_at"] = now_iso()
                        write_json(status_path, status)
        finally:
            stop_path.unlink(missing_ok=True)
            status.update(running=False, stopped_at=now_iso())
            write_json(status_path, status)
            LOG.info("Background monitoring stopped")


def main():
    parser = argparse.ArgumentParser(description="Tibo reset email alerts")
    parser.add_argument("--loop", action="store_true", help="Monitor continuously in the background")
    parser.add_argument("--once", action="store_true", help="Check once and process configured notifications")
    parser.add_argument("--preview", action="store_true", help="Preview the source without sending email or advancing progress")
    parser.add_argument("--test-email", action="store_true", help="Send a test email")
    args = parser.parse_args()
    setup_logging()
    try:
        if args.loop:
            run_loop()
        elif args.once:
            print(json.dumps(run_once(load_config()), ensure_ascii=False, indent=2))
        elif args.preview:
            print(json.dumps(preview_source(load_config()), ensure_ascii=False, indent=2))
        elif args.test_email:
            send_test_email(load_config())
            print("The email server accepted the test message. Check your inbox and spam folder.")
        else:
            parser.print_help()
    except Exception as exc:
        LOG.error("Operation failed %s: %s", type(exc).__name__, error_text(exc))
        if sys.stderr:
            print(error_text(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
