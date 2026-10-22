"""One scheduled cloud check. All credentials come from environment / CI secrets."""
import argparse
import json
import os
import sys

import watcher


def main():
    parser = argparse.ArgumentParser(description="Independent reset event detector; does not start Codex")
    parser.add_argument("--preview", action="store_true", help="Read the source without sending email or changing delivery progress")
    args = parser.parse_args()
    try:
        watcher.setup_logging()
        cfg = watcher.load_config()
        if cfg.get("email_enabled", True) and not args.preview:
            missing = [key for key in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_TO") if not os.environ.get(key)]
            if missing:
                raise ValueError("Configure these GitHub Actions secrets first: " + ", ".join(missing))
        result = watcher.preview_source(cfg) if args.preview else watcher.run_once(cfg)
        # Summary contains only counters. No recipients, tokens or passwords in CI output.
        if args.preview:
            result = {key: value for key, value in result.items() if key != "posts"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(watcher.error_text(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
