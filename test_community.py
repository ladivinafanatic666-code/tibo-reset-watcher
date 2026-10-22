import copy
from contextlib import redirect_stderr
import io
import socket
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

import community
import cloud_watch
import event_protocol


def post(post_id, text="Done", author="thsottiaux"):
    return {
        "id": post_id,
        "text": text,
        "announced_at": "2026-10-22T06:55:54.000Z",
        "source": {
            "author": author,
            "type": "x_post",
            "url": f"https://x.com/{author}/status/{post_id}",
        },
    }


class CommunityFeedTests(unittest.TestCase):
    def setUp(self):
        self.status = {
            "data": {"latest_reset": post("100"), "scheduled_reset": post("200", "Lands tomorrow before 10am PST."), "active_watch": post("300", "We expect reset")},
            "stale": False,
            "error": None,
            "fetched_at": "2026-10-22T07:00:00Z",
        }
        self.history = {"data": [post("100"), post("50", "Reseted")], "stale": False, "error": None}
        meta = {"api_version": "v1", "generated_at": "2026-10-22T14:00:00Z"}
        self.direct_status = {"data": copy.deepcopy(self.status["data"]), "meta": copy.deepcopy(meta)}
        self.direct_history = {"data": copy.deepcopy(self.history["data"]), "meta": copy.deepcopy(meta)}

    def fetch(self, url):
        return copy.deepcopy(self.status if url == community.STATUS_URL else self.history)

    def test_contextual_previews_and_done_deduplicate_without_predictions(self):
        posts = community.fetch_posts(self.fetch)
        self.assertEqual({p["id"] for p in posts}, {"50", "100", "200"})
        preview = next(p for p in posts if p["id"] == "200")
        self.assertTrue(preview["matches"])
        self.assertIn("completion not yet confirmed", preview["context"])
        self.assertEqual(preview["kind"], "reset_scheduled")
        self.assertEqual(next(p for p in posts if p["id"] == "100")["kind"], "reset_completed")
        self.assertIn("https://codex-resets.com", preview["source_metadata"])

    def test_either_dataset_stale_or_missing_freshness_fails(self):
        for dataset in (self.status, self.history):
            for value in (True, None):
                with self.subTest(dataset=dataset, value=value):
                    dataset["stale"] = value
                    with self.assertRaises(RuntimeError):
                        community.fetch_posts(self.fetch)
            dataset["stale"] = False

    def test_missing_or_null_data_and_error_fail(self):
        for document in ({"stale": False}, {"data": None, "stale": False}, {"data": {}, "stale": False, "error": {"code": "upstream_error"}}):
            with self.subTest(document=document):
                self.status = document
                with self.assertRaises(RuntimeError):
                    community.fetch_posts(self.fetch)

    def test_other_authors_unverified_links_and_observations_excluded(self):
        self.history["data"].append(post("400", author="somebody"))
        no_author = post("500")
        del no_author["source"]["author"]
        self.history["data"].append(no_author)
        for bad_url in ("https://evil.com/thsottiaux/status/600", "https://x.com.evil.com/thsottiaux/status/600", "https://x.com/other/status/600", "http://x.com/thsottiaux/status/600"):
            invalid = post("600")
            invalid["source"]["url"] = bad_url
            self.history["data"].append(invalid)
        self.assertEqual({p["id"] for p in community.fetch_posts(self.fetch)}, {"50", "100", "200"})

    def test_post_id_disagreement_is_not_silently_baselined(self):
        self.status["data"]["latest_reset"]["source"]["url"] = "https://x.com/thsottiaux/status/999"
        with self.assertRaises(RuntimeError):
            community.fetch_posts(self.fetch)

    def test_empty_or_entirely_filtered_source_fails(self):
        self.status["data"]["latest_reset"] = None
        self.status["data"]["scheduled_reset"] = None
        self.history["data"] = [post("400", author="someoneelse")]
        with self.assertRaises(RuntimeError):
            community.fetch_posts(self.fetch)

    def direct_fetch(self, url):
        if url == community.DIRECT_STATUS_URL:
            return copy.deepcopy(self.direct_status)
        if url == community.DIRECT_HISTORY_URL:
            return copy.deepcopy(self.direct_history)
        raise URLError(socket.gaierror(-3, "Temporary failure in name resolution"))

    def test_dns_failure_uses_direct_source_without_inventing_freshness(self):
        fetch = mock.Mock(side_effect=self.direct_fetch)
        posts = community.fetch_posts(fetch)
        self.assertEqual(fetch.call_args_list, [mock.call(community.STATUS_URL),
                         mock.call(community.DIRECT_STATUS_URL), mock.call(community.DIRECT_HISTORY_URL)])
        self.assertEqual({p["id"] for p in posts}, {"50", "100", "200"})
        self.assertEqual({p["provider"] for p in posts}, {"codex-resets.com"})
        self.assertEqual(next(p for p in posts if p["id"] == "200")["kind"], "reset_scheduled")
        for record in posts:
            self.assertIn("freshness is unknown", record["source_metadata"])
            self.assertIn("response generation time is not the event time", record["source_metadata"])
            self.assertNotIn("stale=false", record["source_metadata"])

    def test_history_connection_failure_reloads_both_direct_datasets(self):
        def fetch(url):
            return self.fetch(url) if url == community.STATUS_URL else self.direct_fetch(url)
        observed = mock.Mock(side_effect=fetch)
        posts = community.fetch_posts(observed)
        self.assertEqual(observed.call_args_list, [mock.call(community.STATUS_URL), mock.call(community.HISTORY_URL),
                         mock.call(community.DIRECT_STATUS_URL), mock.call(community.DIRECT_HISTORY_URL)])
        self.assertTrue(all(p["provider"] == "codex-resets.com" for p in posts))

    def test_http_denial_or_rate_limit_does_not_use_fallback(self):
        for code in (401, 403, 429):
            with self.subTest(code=code):
                fetch = mock.Mock(side_effect=HTTPError(community.STATUS_URL, code, "denied", {}, None))
                with self.assertRaises(HTTPError):
                    community.fetch_posts(fetch)
                self.assertEqual(fetch.call_args_list, [mock.call(community.STATUS_URL)])

    def test_stale_or_malformed_primary_data_does_not_use_fallback(self):
        invalid_documents = [self.status | {"stale": True}, self.status | {"data": None}]
        mismatch = copy.deepcopy(self.status)
        mismatch["data"]["latest_reset"]["source"]["url"] = "https://x.com/thsottiaux/status/999"
        invalid_documents.append(mismatch)
        for document in invalid_documents:
            with self.subTest(document=document):
                fetch = mock.Mock(return_value=document)
                with self.assertRaises(RuntimeError):
                    community.fetch_posts(fetch)
                self.assertEqual(fetch.call_args_list, [mock.call(community.STATUS_URL)])

    def test_malformed_direct_metadata_and_payload_fail_closed(self):
        invalid = [
            {"data": None, "meta": self.direct_status["meta"]},
            {"data": self.direct_status["data"]},
            self.direct_status | {"error": {"code": "upstream_error"}},
            self.direct_status | {"stale": True},
            self.direct_status | {"meta": {"api_version": "v2", "generated_at": "2026-10-22T14:00:00Z"}},
            self.direct_status | {"meta": {"api_version": "v1", "generated_at": "invalid"}},
            self.direct_status | {"meta": {"api_version": "v1", "generated_at": "2026-10-22T14:00:00"}},
        ]
        for document in invalid:
            with self.subTest(document=document):
                self.direct_status = document
                with self.assertRaises(RuntimeError):
                    community.fetch_posts(self.direct_fetch)

    def test_direct_records_keep_author_link_checks_and_ignore_predictions(self):
        self.direct_history["data"].extend([post("400", author="other"), post("500")])
        self.direct_history["data"][-1]["source"]["url"] = "https://evil.example/thsottiaux/status/500"
        posts = community.fetch_posts(self.direct_fetch)
        self.assertEqual({p["id"] for p in posts}, {"50", "100", "200"})
        self.direct_status["data"]["latest_reset"]["source"]["url"] = "https://x.com/thsottiaux/status/999"
        with self.assertRaises(RuntimeError):
            community.fetch_posts(self.direct_fetch)

    def test_completed_record_wins_when_status_still_lists_same_post_as_scheduled(self):
        self.history["data"].append(post("200", "Reset completed."))
        self.direct_history["data"].append(post("200", "Reset completed."))
        for fetch in (self.fetch, self.direct_fetch):
            with self.subTest(provider=fetch.__name__):
                records = community.fetch_posts(fetch)
                record = next(p for p in records if p["id"] == "200")
                self.assertEqual(record["kind"], "reset_completed")
                self.assertEqual(record["text"], "Reset completed.")

    def test_verified_url_variants_are_canonical_for_event_consumers(self):
        self.status["data"]["latest_reset"]["source"]["url"] = "https://www.x.com/Thsottiaux/status/100/?s=20"
        record = next(p for p in community.fetch_posts(self.fetch) if p["id"] == "100")
        self.assertEqual(record["url"], "https://x.com/thsottiaux/status/100")
        self.assertEqual(event_protocol.make_event(record)["post_url"], record["url"])

    def test_invalid_publication_time_fails_before_advancing_progress(self):
        for value in ("not-a-date", "2026-10-22T12:00:00", "2026-02-30T12:00:00Z"):
            with self.subTest(value=value):
                self.status["data"]["latest_reset"]["announced_at"] = value
                with self.assertRaisesRegex(RuntimeError, "invalid publication time"):
                    community.fetch_posts(self.fetch)

    def test_publication_sort_uses_instant_instead_of_timezone_text(self):
        self.status["data"]["latest_reset"]["announced_at"] = "2026-10-22T07:00:00Z"
        self.status["data"]["scheduled_reset"]["announced_at"] = "2026-10-22T08:00:00+02:00"
        self.history["data"] = []
        self.assertEqual([p["id"] for p in community.fetch_posts(self.fetch)], ["200", "100"])

    def test_non_ascii_post_id_is_not_accepted_as_verified_x_post(self):
        self.history["data"].append(post("\uff11\uff12\uff13"))
        self.assertNotIn("\uff11\uff12\uff13", {p["id"] for p in community.fetch_posts(self.fetch)})


class EventProtocolTests(unittest.TestCase):
    def test_event_ids_match_the_receiver_ascii_id_contract(self):
        for post_id in ("\uff11\uff12\uff13", "12345678901234567890123456", "invalid"):
            with self.subTest(post_id=post_id):
                with self.assertRaisesRegex(ValueError, "ASCII digits"):
                    event_protocol.make_event({"id": post_id, "url": "unused", "text": "unused"})
        event = event_protocol.make_event({"id": "123", "url": "https://x.com/thsottiaux/status/123", "text": "Done"})
        self.assertEqual(event["id"], "reset_completed_123")
        self.assertEqual(event["schema_version"], 1)


class CloudCliTests(unittest.TestCase):
    def test_logging_failure_returns_a_concise_error_without_running_a_check(self):
        output = io.StringIO()
        with mock.patch("sys.argv", ["cloud_watch.py"]), \
                mock.patch.object(cloud_watch.watcher, "setup_logging", side_effect=OSError("unavailable")), \
                mock.patch.object(cloud_watch.watcher, "error_text", return_value="Logging unavailable."), \
                mock.patch.object(cloud_watch.watcher, "run_once") as check, \
                redirect_stderr(output):
            self.assertEqual(cloud_watch.main(), 1)
        self.assertEqual(output.getvalue().strip(), "Logging unavailable.")
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
