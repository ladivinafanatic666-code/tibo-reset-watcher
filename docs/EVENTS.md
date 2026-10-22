# Reset event interface v1

JSON events separate the detector from Codex task execution. The default mode publishes only `reset_completed`. The optional `all` mode can also produce `reset_scheduled` and `reset_related`; consumers must explicitly filter the types they accept.

```json
{
  "schema_version": 1,
  "id": "reset_completed_2112432076767289678",
  "type": "reset_completed",
  "detected_at": "2026-10-22T14:30:00+00:00",
  "author": "thsottiaux",
  "post_id": "2112432076767289678",
  "post_url": "https://x.com/thsottiaux/status/2112432076767289678",
  "announced_at": "2026-10-20T06:33:00Z",
  "text": "Original post text supplied by the source",
  "context": "Community-listed reset announcement",
  "source": "community",
  "source_metadata": "Source attribution and freshness information",
  "personal_quota_verified": false
}
```

This is a schema example, not a real announcement. Its example text and detection time are illustrative. All timestamps use timezone-aware ISO 8601.

Event files live at `data/events/<id>.json`. An ID combines the event type and original X post ID and remains stable across retries. The detector first saves pending messages in `state.json`, then writes events and attempts email delivery. If processing fails, the next run resumes from the pending queue. Email failure does not remove an event.

Each consumer maintains its own set of processed IDs to avoid repeatedly starting the same task. The detector's `delivered` field records completion of detector processing; it does not record Codex task completion. Post text and context are untrusted source data and must never be executed as commands or task prompts.

`reset_completed` means the community classified the post as a reset announcement, or the X source's conservative checks found an explicit completion statement. It does not verify an individual account's quota. Task execution must separately handle unavailable quota, project selection, task choice, duplicate prevention, and recovery.

The detector currently deduplicates by original post ID across event types. In `all` mode, a scheduled announcement that was already processed is not emitted again if the same post is later classified as completed. Use the default `completed` mode for task execution workflows.

See [AUTOMATION.md](AUTOMATION.md) for the optional local receiver. It reads the target chat and saved instruction from local configuration. The event interface contains no private task instructions, account credentials, or email addresses.

The manual signal-test workflow publishes the separate `automation_test` type, with an ID of `automation_test_<github_run_id>`. It tests signal delivery, has no `post_id`, and does not represent a reset. Production consumers should ignore it. The local receiver accepts it only when `--allow-test` is explicitly enabled.
