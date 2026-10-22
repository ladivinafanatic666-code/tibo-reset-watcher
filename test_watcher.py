"""Offline behavior checks. No network requests or real mail are made."""
import copy
import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse

import watcher as w


def post(post_id, text="A reset is coming", matches=True):
    return {"id": str(post_id), "text": text, "created_at": "2026-10-22T12:00:00Z",
            "url": f"https://x.com/thsottiaux/status/{post_id}", "matches": matches}


class CoreTests(unittest.TestCase):
    def setUp(self):
        # Tests must never consume real sender credentials from the environment.
        clean_env = {key: value for key, value in os.environ.items()
                     if not key.startswith(("RESET_", "SMTP_"))
                     and key not in ("X_BEARER_TOKEN", "ALERT_TO")}
        self.env_patch = mock.patch.dict(os.environ, clean_env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Keep fixtures inside the authorized workspace, including on Windows.
        self.temp = tempfile.TemporaryDirectory(prefix=".reset-watcher-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = mock.patch.object(w, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.secret_patch = mock.patch.object(w, "get_secret", return_value="test-only-placeholder")
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        self.cfg = copy.deepcopy(w.DEFAULT_CONFIG)

    def state(self):
        return w.read_json(self.root / "data" / "state.json", {})

    def check(self, posts, cursor=None, cfg=None):
        with mock.patch.object(w, "fetch_posts", return_value=(posts, cursor)), \
                mock.patch.object(w, "send_email") as send:
            result = w.run_once(cfg or self.cfg)
        return result, send

    def test_first_fetch_baselines_without_historical_mail(self):
        result, send = self.check([post("100"), post("101")])
        self.assertTrue(result["baseline"])
        self.assertEqual(result["fetched"], 2)
        send.assert_not_called()
        self.assertEqual(set(self.state()["sources"]["community"]["seen"]), {"100", "101"})

    def test_new_messages_send_once_after_restart(self):
        self.check([post("100")])
        result, send = self.check([post("100"), post("101"), post("102", "Unrelated", False)])
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(send.call_count, 1)
        self.assertIn("101", self.state()["delivered"])
        result, send = self.check([post("100"), post("101"), post("102", "Unrelated", False)])
        self.assertEqual(result["sent"], 0)
        send.assert_not_called()

    def test_delivered_id_deduplicates_across_sources(self):
        self.check([post("100")])
        self.check([post("100"), post("101")])
        api_cfg = self.cfg | {"source": "x_api"}
        self.check([post("100")], "100", api_cfg)
        result, send = self.check([post("101"), post("102")], "102", api_cfg)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(send.call_count, 1)
        self.assertIn("102", send.call_args.args[1].get_content())

    def test_failed_send_persists_queue_and_retries_when_source_is_down(self):
        self.check([post("100")])
        with mock.patch.object(w, "fetch_posts", return_value=([post("100"), post("101")], None)), \
                mock.patch.object(w, "send_email", side_effect=smtplib.SMTPServerDisconnected("offline")):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                w.run_once(self.cfg)
        self.assertEqual([p["id"] for p in self.state()["pending"]], ["101"])
        self.assertIn("101", self.state()["sources"]["community"]["seen"])
        failure = urllib.error.URLError("offline")
        with mock.patch.object(w, "fetch_posts", side_effect=failure), \
                mock.patch.object(w, "send_email") as send:
            with self.assertRaises(urllib.error.URLError):
                w.run_once(self.cfg)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.state()["pending"], [])
        self.assertIn("101", self.state()["delivered"])
        _, send = self.check([post("100"), post("101")])
        send.assert_not_called()

    def test_broken_state_is_not_reset_or_overwritten(self):
        path = self.root / "data" / "state.json"
        path.parent.mkdir()
        path.write_text("broken", encoding="utf-8")
        with mock.patch.object(w, "fetch_posts") as fetch:
            with self.assertRaises(json.JSONDecodeError):
                w.run_once(self.cfg)
        fetch.assert_not_called()
        self.assertEqual(path.read_text(), "broken")

    def test_x_partial_page_failure_does_not_advance_cursor(self):
        cfg = self.cfg | {"source": "x_api"}
        self.check([post("100")], "100", cfg)
        before = self.state()
        page = {"data": [{"id": "102", "text": "reset"}], "meta": {"next_token": "next"}}
        with mock.patch.object(w, "fetch_json", side_effect=[page, urllib.error.URLError("down")]), \
                mock.patch.object(w, "send_email") as send:
            with self.assertRaises(urllib.error.URLError):
                w.run_once(cfg)
        self.assertEqual(self.state(), before)
        send.assert_not_called()

    def test_x_done_reply_matches_reset_context_and_full_note_text(self):
        payload = {"data": [
            {"id": "103", "text": "Done", "created_at": "2026-10-22T12:00:00Z",
             "referenced_tweets": [{"id": "100", "type": "replied_to"}]},
            {"id": "104", "text": "truncated", "note_tweet": {"text": "The weekly reset is live"},
             "edit_history_tweet_ids": ["102", "104"]},
            {"id": "105", "text": "hello"}],
            "includes": {"tweets": [{"id": "100", "text": "A reset tomorrow?"}]},
            "meta": {"result_count": 3}}
        with mock.patch.object(w, "fetch_json", return_value=payload) as fetch:
            posts, cursor = w.fetch_x(self.cfg, "99")
        self.assertEqual(cursor, "105")
        self.assertTrue(posts[0]["matches"])
        self.assertIn("reset", posts[0]["context"])
        self.assertTrue(posts[1]["matches"])
        self.assertEqual(posts[1]["id"], "102")
        self.assertFalse(posts[2]["matches"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.call_args.args[0]).query)
        self.assertEqual(query["since_id"], ["99"])
        self.assertEqual(query["query"], ["from:thsottiaux -is:retweet"])

    def test_x_completion_requires_an_explicit_reset_assertion(self):
        announcements = [
            "Reset all propagated. Enjoy.",
            "Resets all propagated. That will be all.",
            "The weekly reset is live.",
            "Reset has now fully landed.",
            "The reset has been applied.",
            "We have completed the reset.",
            "We have added a banked reset to everyone's account.",
            "Granted a banked reset to all accounts.",
            "\u91cd\u7f6e\u5df2\u5b8c\u6210\u3002",
        ]
        for text in announcements:
            with self.subTest(text=text):
                self.assertEqual(w.classify_x_post(text), "reset_completed")

    def test_x_negations_and_questions_never_confirm_a_reset(self):
        non_announcements = [
            "No reset has landed yet.",
            "The reset has not propagated.",
            "The reset hasn't completed.",
            "The reset hasn\u2019t completed.",
            "Has the reset landed?",
            "Has the reset landed",
            "Is the reset live now",
            "Reset all propagated?",
            "\u91cd\u7f6e\u5b8c\u6210\u4e86\u5417\uff1f",
            "\u91cd\u7f6e\u5c1a\u672a\u5b8c\u6210\u3002",
        ]
        for text in non_announcements:
            with self.subTest(text=text):
                self.assertNotEqual(w.classify_x_post(text), "reset_completed")

    def test_x_discussion_and_documentation_do_not_confirm_a_reset(self):
        discussions = [
            "I enjoy reading reset jokes.",
            "Reset. Enjoy.",
            "We added reset documentation.",
            "We completed the reset documentation.",
            "Reset documentation is complete.",
            "Our deployment landed. Now let's discuss a reset.",
        ]
        for text in discussions:
            with self.subTest(text=text):
                self.assertEqual(w.classify_x_post(text), "reset_related")
        self.assertEqual(w.classify_x_post("Done.", "The reset is being prepared."), "reset_completed")
        self.assertEqual(w.classify_x_post("Done."), "unrelated")
        self.assertEqual(w.classify_x_post("Done.", "Documentation is ready."), "unrelated")

    def test_x_future_resets_are_not_completed(self):
        previews = [
            "Tomorrow reset will land.",
            "The reset will be applied soon.",
            "Maybe the reset is complete.",
            "A reset is planned for next week.",
            "\u660e\u5929\u91cd\u7f6e\u5b8c\u6210\u3002",
        ]
        for text in previews:
            with self.subTest(text=text):
                self.assertEqual(w.classify_x_post(text), "reset_scheduled")

    def test_preview_is_read_only(self):
        with mock.patch.object(w, "fetch_posts", return_value=([post("100")], None)), \
                mock.patch.object(w, "send_email") as send:
            result = w.preview_source(self.cfg)
        self.assertEqual(result["matched"], 1)
        self.assertFalse((self.root / "data" / "state.json").exists())
        send.assert_not_called()

    def test_message_has_recipient_original_link_and_stable_id(self):
        item = post("100") | {"source": "community", "context": "Reset discussion"}
        message = w.build_message(self.cfg, item)
        self.assertEqual(message["To"], self.cfg["recipient"])
        self.assertEqual(message["From"], self.cfg["smtp_user"])
        self.assertIn(item["url"], message.get_content())
        self.assertIn(item["context"], message.get_content())
        self.assertEqual(message["Message-ID"], w.build_message(self.cfg, item)["Message-ID"])

    def test_email_detection_time_is_explicit_utc(self):
        detected = w.datetime(2026, 10, 23, 1, 2, 3, tzinfo=w.timezone.utc)
        with mock.patch.object(w, "datetime") as clock:
            clock.now.return_value = detected
            message = w.build_message(self.cfg, post("100"))
        clock.now.assert_called_once_with(w.timezone.utc)
        self.assertIn("Detected at: 2026-10-23 01:02:03 UTC", message.get_content())
        self.assertTrue(str(message["Subject"]).startswith("[Tibo reset alert]"))

    def test_smtp_ssl_uses_verified_tls_and_does_not_resend_after_quit_failure(self):
        with mock.patch.object(w.smtplib, "SMTP_SSL") as smtp, \
                mock.patch.object(w.smtplib, "SMTP") as plain:
            server = smtp.return_value
            server.send_message.return_value = {}
            server.quit.side_effect = smtplib.SMTPServerDisconnected("closed")
            message = w.build_message(self.cfg, test=True)
            w.send_email(self.cfg, message)
        plain.assert_not_called()
        context = smtp.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, w.ssl.CERT_REQUIRED)
        server.send_message.assert_called_once_with(message)
        server.close.assert_called_once()

    def test_starttls_precedes_login(self):
        cfg = self.cfg | {"smtp_port": 587, "smtp_security": "STARTTLS"}
        with mock.patch.object(w.smtplib, "SMTP") as smtp:
            server = smtp.return_value
            server.send_message.return_value = {}
            w.send_email(cfg, w.build_message(cfg, test=True))
        names = [call[0] for call in server.mock_calls]
        self.assertLess(names.index("starttls"), names.index("login"))
        self.assertEqual(names[:3], ["ehlo", "starttls", "ehlo"])

    def test_singleton_prevents_overlap_and_releases(self):
        with w.SingleInstance():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with w.SingleInstance():
                    self.fail("second lock unexpectedly acquired")
        with w.SingleInstance():
            pass

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_credentials_are_windows_encrypted_and_round_trip(self):
        self.secret_patch.stop()
        value = "unit-test-value-only"
        w.set_secret("smtp_password", value)
        self.assertEqual(w.get_secret("smtp_password"), value)
        disk_text = (self.root / "data" / "secrets.json").read_text(encoding="utf-8")
        self.assertNotIn(value, disk_text)
        w.set_secret("smtp_password", "")
        self.assertEqual(w.get_secret("smtp_password"), "")

    def test_config_rejects_header_injection_and_plaintext_mail(self):
        for change in ({"recipient": "a@example.com\nBcc: bad@example.com"},
                       {"smtp_security": "NONE"}, {"interval_seconds": 0},
                       {"smtp_port": True}, {"smtp_port": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                w.validate_config(self.cfg | change)

    def test_completed_mode_ignores_previews_but_observes_same_id_completion(self):
        cfg = self.cfg | {"event_mode": "completed"}
        self.check([post("100") | {"kind": "reset_completed"}], cfg=cfg)
        preview = post("101", "Reset tomorrow") | {"kind": "reset_scheduled"}
        result, send = self.check([post("100"), preview], cfg=cfg)
        self.assertEqual(result["sent"], 0)
        send.assert_not_called()
        complete = preview | {"text": "Done", "kind": "reset_completed"}
        result, send = self.check([post("100"), complete], cfg=cfg)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(send.call_count, 1)
        _, send = self.check([post("100"), complete], cfg=cfg)
        send.assert_not_called()

    def test_email_disabled_still_writes_durable_event_without_smtp(self):
        cfg = self.cfg | {"email_enabled": False, "event_mode": "completed"}
        with mock.patch.object(w, "get_secret", return_value=""):
            self.check([post("100")], cfg=cfg)
            result, send = self.check([post("100"), post("101")], cfg=cfg)
        send.assert_not_called()
        self.assertEqual(result["new"], 1)
        events = list((self.root / "data" / "events").glob("*.json"))
        self.assertTrue(events, "new events must be available to downstream notification sinks")
        self.assertTrue(any("101" in path.read_text(encoding="utf-8") for path in events))

    def test_cloud_environment_overrides_local_configuration_with_typed_values(self):
        w.write_json(self.root / "config.json", self.cfg | {"smtp_port": 465, "interval_seconds": 300})
        changes = {"RESET_SOURCE": "x_api", "RESET_EVENT_MODE": "all",
                   "ALERT_TO": "receiver@example.com", "SMTP_USER": "sender@example.com",
                   "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
                   "SMTP_SECURITY": "STARTTLS", "RESET_INTERVAL_SECONDS": "600",
                   "RESET_EMAIL_ENABLED": "false"}
        with mock.patch.dict(os.environ, changes):
            cfg = w.load_config()
        self.assertEqual(cfg["source"], "x_api")
        self.assertEqual(cfg["event_mode"], "all")
        self.assertEqual(cfg["recipient"], "receiver@example.com")
        self.assertEqual(cfg["smtp_user"], "sender@example.com")
        self.assertEqual(cfg["smtp_host"], "smtp.example.com")
        self.assertEqual(cfg["smtp_security"], "STARTTLS")
        self.assertEqual(cfg["smtp_port"], 587)
        self.assertEqual(cfg["interval_seconds"], 600)
        self.assertIs(cfg["email_enabled"], False)

    def test_cloud_invalid_boolean_fails_instead_of_silently_enabling_mail(self):
        with mock.patch.dict(os.environ, {"RESET_EMAIL_ENABLED": "perhaps"}):
            with self.assertRaises(ValueError):
                w.load_config()

    def test_cloud_credentials_do_not_require_dpapi(self):
        self.secret_patch.stop()
        changes = {"SMTP_PASSWORD": "test-environment-mail", "X_BEARER_TOKEN": "test-environment-x"}
        with mock.patch.dict(os.environ, changes), mock.patch.object(w, "_dpapi") as dpapi:
            self.assertEqual(w.get_secret("smtp_password"), changes["SMTP_PASSWORD"])
            self.assertEqual(w.get_secret("x_bearer_token"), changes["X_BEARER_TOKEN"])
        dpapi.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
