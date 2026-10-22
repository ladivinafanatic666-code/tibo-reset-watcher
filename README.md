# Tibo Reset Watcher

Watch public Codex reset announcements from Tibo (`@thsottiaux`), publish JSON events, and receive email alerts. Run the monitor on GitHub Actions or use the optional Windows settings window. Requires Python 3.12 and its standard library; no third-party Python packages are needed.

An optional local Codex receiver can send a saved instruction to a selected chat when GitHub publishes a new event. Email monitoring and task execution keep separate progress records.

This is an independent community project, with no affiliation with or endorsement from OpenAI, X, or Tibo. It does not increase, reset, or bypass account limits.

## Default behavior

- Request a check every five minutes, accepting completed reset announcements and ignoring forecasts or scheduled resets.
- Establish a baseline on the first successful check without emailing historical posts. Deduplicate later announcements by their original post ID.
- Publish a `reset_completed` event for each new announcement. Keep failed email deliveries in a durable retry queue.
- Store GitHub Actions progress and public events on a separate `reset-watcher-state` branch.

## Set up GitHub Actions

Put the source on your repository's default branch, including `.github/workflows/`. Open **Settings > Secrets and variables > Actions > Secrets** and add these repository secrets:

| Secret | Value |
| --- | --- |
| `ALERT_TO` | Recipient email address |
| `SMTP_USER` | Full sender email address; it may match the recipient |
| `SMTP_PASSWORD` | Email app password or SMTP authorization code |

**Keep all three values in Secrets. Do not put them in source files, the README, ordinary Variables, or issues.**

The defaults use Gmail: `smtp.gmail.com`, port `465`, and `SSL`. Enable two-step verification and create an [app password](https://myaccount.google.com/apppasswords); do not use your regular account password. See [Google's instructions](https://support.google.com/accounts/answer/185833?hl=en).

For another email provider, set these non-secret Actions **Variables**:

| Variable | Default or accepted values |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465`; STARTTLS usually uses `587` |
| `SMTP_SECURITY` | `SSL` or `STARTTLS` |
| `RESET_SOURCE` | `community` (default) or `x_api` |

The `x_api` source also requires the `X_BEARER_TOKEN` secret and an X API account with the necessary access and available usage.

Open **Actions > Tibo reset alerts > Run workflow**. Enable `test_email` to send a test message before the first check. Check the run results, your inbox, and your spam folder. An ordinary first check does not send historical announcements.

The workflow needs `contents: write` to update its state branch. Repository rules or organization policies must allow that branch to be updated. Actions usage and billing depend on your repository, account, and actual usage.

## Privacy in a public repository

Source files, commit metadata, and workflow logs are public. This project reads email credentials from environment variables and does not write recipient addresses, sender addresses, or app passwords to its state, events, or normal logs.

Local `config.json`, `data/`, and `__pycache__/` are excluded by `.gitignore`. Browser uploads do not apply `.gitignore`; upload source files only. Run `python prepare_upload.py` to generate a source-only folder and ZIP. Use GitHub's `noreply` address for your Git commit identity.

The state branch contains public posts, events, and deduplication or delivery progress, without email credentials. Do not delete that branch or `state.json` to troubleshoot a temporary connection problem: doing so loses progress.

## Sources and limitations

| Source | Method and coverage |
| --- | --- |
| Free community feed | Read the [tibo.cc API](https://tibo.cc/guide/api), with a connection-failure fallback to its [public upstream API](https://codex-resets.com/api/docs). The upstream data comes from [codex-resets.com](https://codex-resets.com); original post authors and links are checked. |
| Official X API | Use Recent Search with `from:thsottiaux -is:retweet`, covering the last seven days. Conservative wording and direct-reply context checks identify completion; some announcements may be missed. |

Community records may be delayed, incomplete, or misclassified. They do not cover every relevant X post. Stale, empty, or malformed primary data and HTTP denials or rate limits fail visibly. Only a network connection failure switches both status and history to the upstream source, preserving original post IDs and existing progress. The upstream supplies response generation times, without independent data freshness information; alerts disclose this limitation. A public reset announcement does not verify that any particular account has received additional quota.

GitHub schedules can be delayed or skipped and do not guarantee immediate alerts. Schedules on inactive public repositories may be disabled. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Email can be delayed or filtered as spam. A crash after sending an email but before saving delivery progress can still cause a duplicate on retry.

## Event interface

New events are saved as `data/events/reset_completed_<post_id>.json`. See [docs/EVENTS.md](docs/EVENTS.md) for the schema and consumption rules. Consumers filter event types and maintain their own processed-ID records. Email failure does not revoke an event. The detector and local Codex receiver run independently.

## Run a saved Codex instruction

The optional `codex_bridge.py` receiver reads new events from the GitHub state branch and submits a locally saved instruction to a selected chat through `codex queue`. The computer and Codex must remain available. GitHub email monitoring continues independently of the local computer.

Polling reads Git data without making model calls; new events submit instructions. Setup, startup commands, and the GitHub integration test are documented in [docs/AUTOMATION.md](docs/AUTOMATION.md). Chat IDs, instructions, and the Codex executable path stay in the ignored local directory. Public test events contain only generic event metadata. Test events are ignored unless `--allow-test` is explicitly enabled.

## Optional Windows settings window

Install Python 3.12, then double-click `start.cmd`. Enter your email settings, save them, preview the source, send a test email, and start the background monitor. Monitoring continues after the settings window closes. Reopen the window and select **Stop monitor** to stop it.

The computer must be on, signed in, connected, and awake. The mail server must be reachable; an HTTP system proxy does not automatically proxy SMTP. App passwords are encrypted locally with Windows DPAPI for the current account. Login startup is optional and can be enabled in the window. Cloud and local email monitors have separate state, so running both may deliver two copies of an alert.

## Development checks

```powershell
python -m unittest discover -v
python app.py --self-test         # Windows UI check
python cloud_watch.py --preview   # Read-only source check; no email or progress changes
```

Offline tests do not contact real networks or mailboxes. `RESET_DATA_DIR` can override the monitor's data directory; environment variables take precedence over local configuration. An outage longer than a source's history window may leave gaps that cannot be recovered.
