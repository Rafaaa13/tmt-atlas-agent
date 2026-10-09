import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import connectors


RSS = b'''<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Mock News</title>
<item><title>First &amp;amp; story</title><pubDate>2026-10-08T09:00:00Z</pubDate><link>https://news.example.test/a#fragment</link></item>
<item><title>Duplicate URL</title><link>https://news.example.test/a</link></item>
<item><title>Unsafe item</title><link>javascript:alert(1)</link></item>
</channel></rss>'''

ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Mock</title>
<entry><title>Atom story</title><updated>2026-10-08T10:00:00Z</updated><link rel="alternate" href="https://news.example.test/atom" /></entry>
</feed>'''

JSON_FEED = json.dumps(
    {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "JSON Mock",
        "items": [
            {"id": "1", "title": "JSON story", "date_published": "2026-10-08T11:00:00Z", "url": "https://news.example.test/json"},
            {"id": "2", "title": "same", "url": "https://news.example.test/json"},
        ],
    }
).encode()


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class TestConnectors(unittest.TestCase):
    def test_default_config_is_disabled_and_contract_shape_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory)
            config = collector.config()
            self.assertEqual(
                set(config),
                {"feeds", "enabled", "interval_minutes", "last_run", "next_run", "last_error"},
            )
            self.assertFalse(config["enabled"])
            self.assertEqual(config["feeds"], [])
            self.assertEqual(config["interval_minutes"], 60)
            self.assertEqual(collector.inbox(), [])

    def test_configure_requires_global_and_per_feed_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory)
            with self.assertRaises(connectors.ConfigurationError):
                collector.configure({"enabled": True, "authorized": True, "feeds": ["https://news.example.test/rss"]})
            with self.assertRaises(connectors.ConfigurationError):
                collector.configure({"enabled": True, "feeds": [{"url": "https://news.example.test/rss", "authorized": True}]})
            with self.assertRaises(connectors.ConfigurationError):
                collector.configure({"enabled": True, "authorized": True, "feeds": [{"url": "https://news.example.test/rss"}]})
            configured = collector.configure(
                {
                    "enabled": True,
                    "authorized": True,
                    "interval_minutes": 15,
                    "feeds": [{"url": "https://news.example.test/rss", "authorized": True, "format": "rss"}],
                }
            )
            self.assertTrue(configured["enabled"])
            self.assertEqual(configured["interval_minutes"], 15)
            self.assertEqual(configured["feeds"][0]["format"], "rss")

    def test_url_policy_rejects_private_ips_credentials_fragments_and_non_https(self):
        bad_urls = [
            "http://news.example.test/feed",
            "https://user:pass@news.example.test/feed",
            "https://news.example.test/feed#fragment",
            "https://127.0.0.1/feed",
            "https://10.0.0.5/feed",
            "https://[::1]/feed",
            "https://localhost/feed",
            "https://news.example.test/feed?redirect=https%3A%2F%2F127.0.0.1",
        ]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(connectors.UnsafeURLError):
                connectors.parse_feed(b"<rss/>", url)

    def test_dns_rebinding_policy_checks_every_resolved_address(self):
        def resolver(_host, _port, **_kwargs):
            return [(None, None, None, None, ("93.184.216.34", 443)), (None, None, None, None, ("127.0.0.1", 443))]

        with self.assertRaises(connectors.UnsafeURLError):
            connectors._resolve_public_addresses("news.example.test", 443, resolver)

    def test_parse_rss_atom_and_json_feed_metadata_only_and_deduplicated(self):
        rss = connectors.parse_feed(RSS, "https://news.example.test/rss", "rss")
        self.assertEqual(len(rss), 2)
        self.assertEqual(rss[0]["title"], "First & story")
        self.assertEqual(rss[0]["url"], "https://news.example.test/a")
        self.assertFalse(rss[0]["facts_approved"])
        self.assertNotIn("description", rss[0])

        atom = connectors.parse_feed(ATOM, "https://news.example.test/atom-feed", "atom")
        self.assertEqual(atom[0]["source_title"], "Atom Mock")
        self.assertEqual(atom[0]["url"], "https://news.example.test/atom")

        json_items = connectors.parse_feed(JSON_FEED, "https://news.example.test/json-feed", "json")
        self.assertEqual(len(json_items), 1)
        self.assertEqual(json_items[0]["title"], "JSON story")

    def test_parser_rejects_oversize_and_unsafe_xml_entities(self):
        with self.assertRaises(connectors.FeedParseError):
            connectors.parse_feed(b"x" * (connectors.MAX_RESPONSE_BYTES + 1), "https://news.example.test/feed")
        with self.assertRaises(connectors.FeedParseError):
            connectors.parse_feed(b"<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><rss/>", "https://news.example.test/feed")

    def test_run_once_stages_items_deduplicates_and_sends_conditional_headers(self):
        calls = []

        def fetcher(url, headers):
            calls.append((url, headers))
            return connectors.FetchResult(200, {"ETag": '"abc"', "Last-Modified": "Wed, 08 Oct 2026 09:00:00 GMT"}, RSS)

        clock = MutableClock(datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=fetcher, clock=clock)
            collector.configure(
                {
                    "enabled": True,
                    "authorized": True,
                    "feeds": [{"url": "https://news.example.test/rss", "authorized": True, "format": "rss"}],
                }
            )
            first = collector.run_once()
            second = collector.run_once()
            self.assertEqual(len(first), 2)
            self.assertEqual(second, [])
            self.assertEqual(len(collector.inbox()), 2)
            self.assertFalse(any(item["facts_approved"] for item in collector.inbox()))
            self.assertEqual(calls[0][1], {})
            self.assertEqual(calls[1][1]["If-None-Match"], '"abc"')
            self.assertEqual(calls[1][1]["If-Modified-Since"], "Wed, 08 Oct 2026 09:00:00 GMT")
            state = json.loads(Path(directory, connectors.STATE_FILENAME).read_text())
            self.assertEqual(state["last_error"], None)
            self.assertTrue(state["next_run"])

    def test_redirect_and_oversize_responses_are_rejected_and_error_is_generic(self):
        for response in (
            connectors.FetchResult(302, {"Location": "https://elsewhere.example/"}, b""),
            connectors.FetchResult(200, {"Content-Length": str(connectors.MAX_RESPONSE_BYTES + 1)}, b""),
        ):
            with tempfile.TemporaryDirectory() as directory:
                collector = connectors.Collector(directory, fetcher=lambda *_args: response)
                collector.configure({"enabled": True, "authorized": True, "feeds": [{"url": "https://news.example.test/rss", "authorized": True}]})
                self.assertEqual(collector.run_once(), [])
                self.assertEqual(collector.config()["last_error"], "collection_error")
                self.assertNotIn("Location", json.dumps(json.loads(Path(directory, connectors.STATE_FILENAME).read_text())))

    def test_review_accepts_or_rejects_without_approving_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=lambda *_args: RSS)
            collector.configure({"enabled": True, "authorized": True, "feeds": [{"url": "https://news.example.test/rss", "authorized": True}]})
            item = collector.run_once()[0]
            reviewed = collector.review(item["id"], "accepted")
            self.assertEqual(reviewed["status"], "accepted")
            self.assertFalse(reviewed["facts_approved"])
            with self.assertRaises(ValueError):
                collector.review(item["id"], "approved")

    def test_tick_runs_only_when_due_and_does_not_replay_backlog(self):
        now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        clock = MutableClock(now)
        calls = []

        def fetcher(*_args):
            calls.append(1)
            return RSS

        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=fetcher, clock=clock)
            collector.configure({"enabled": True, "authorized": True, "interval_minutes": 15, "feeds": [{"url": "https://news.example.test/rss", "authorized": True}]})
            self.assertEqual(len(collector.tick()), 2)
            self.assertEqual(collector.tick(), [])
            self.assertEqual(len(calls), 1)
            clock.value = now + timedelta(minutes=15)
            self.assertEqual(collector.tick(), [])
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(collector.inbox()), 2)

    def test_cap_is_five_feeds_per_run(self):
        calls = []

        def fetcher(url, _headers):
            calls.append(url)
            return RSS

        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=fetcher)
            feeds = [{"url": f"https://news{i}.example.test/rss", "authorized": True} for i in range(6)]
            collector.configure({"enabled": True, "authorized": True, "feeds": feeds})
            collector.run_once()
            self.assertEqual(len(calls), connectors.MAX_FEEDS_PER_RUN)

    def test_run_scoped_fetches_only_scoped_feeds_and_preserves_run_once(self):
        calls = []

        def fetcher(url, _headers):
            calls.append(url)
            host = url.split("/")[2]
            body = (
                '<rss version="2.0"><channel><title>Feed</title>'
                f'<item><title>Story {host}</title><link>https://{host}/story</link></item>'
                "</channel></rss>"
            ).encode()
            return connectors.FetchResult(200, {}, body)

        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=fetcher)
            collector.configure(
                {
                    "enabled": True,
                    "authorized": True,
                    "feeds": [
                        {"url": "https://a.example.test/rss", "authorized": True},
                        {"url": "https://b.example.test/rss", "authorized": True},
                        {"url": "https://c.example.test/rss", "authorized": True},
                    ],
                }
            )
            staged = collector.run_scoped(["https://a.example.test/rss"])
            self.assertEqual(calls, ["https://a.example.test/rss"])
            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0]["source_url"], "https://a.example.test/rss")
            self.assertEqual([item["source_url"] for item in collector.inbox()], ["https://a.example.test/rss"])
            # A repeated scoped run deduplicates instead of re-staging.
            self.assertEqual(collector.run_scoped(["https://a.example.test/rss"]), [])
            # The global entry point still collects every enabled feed.
            global_staged = collector.run_once()
            self.assertEqual(
                sorted(item["source_url"] for item in global_staged),
                ["https://b.example.test/rss", "https://c.example.test/rss"],
            )
            self.assertEqual(sorted(set(calls[1:])), [
                "https://a.example.test/rss",
                "https://b.example.test/rss",
                "https://c.example.test/rss",
            ])

    def test_run_scoped_fails_closed_for_unconfigured_or_disabled_feeds(self):
        calls = []

        def fetcher(url, _headers):
            calls.append(url)
            return RSS

        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=fetcher)
            collector.configure(
                {
                    "enabled": True,
                    "authorized": True,
                    "feeds": [
                        {"url": "https://a.example.test/rss", "authorized": True},
                        {"url": "https://b.example.test/rss", "authorized": True, "enabled": False},
                    ],
                }
            )
            for scope in (
                ["https://unconfigured.example.test/rss"],
                ["https://a.example.test/rss", "https://unconfigured.example.test/rss"],
                ["https://a.example.test/rss/"],
                ["https://b.example.test/rss"],
            ):
                with self.subTest(scope=scope), self.assertRaises(connectors.ConfigurationError):
                    collector.run_scoped(scope)
            self.assertEqual(calls, [])
            self.assertEqual(collector.inbox(), [])

    def test_run_scoped_requires_an_enabled_authorized_collector(self):
        def fetcher(*_args):
            raise AssertionError("scoped run must not fetch when the collector is disabled")

        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=fetcher)
            collector.configure(
                {
                    "enabled": False,
                    "authorized": True,
                    "feeds": [{"url": "https://a.example.test/rss", "authorized": True}],
                }
            )
            with self.assertRaises(connectors.ConfigurationError):
                collector.run_scoped(["https://a.example.test/rss"])
            # The global entry point keeps its historical disabled no-op.
            self.assertEqual(collector.run_once(), [])

    def test_run_scoped_validates_scope_shape_and_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = connectors.Collector(directory, fetcher=lambda *_args: RSS)
            collector.configure(
                {
                    "enabled": True,
                    "authorized": True,
                    "feeds": [{"url": "https://a.example.test/rss", "authorized": True}],
                }
            )
            for scope in (
                None,
                "https://a.example.test/rss",
                [],
                ["not a url"],
                ["http://a.example.test/rss"],
                ["https://a.example.test/rss", "javascript:alert(1)"],
                123,
            ):
                with self.subTest(scope=scope), self.assertRaises(connectors.ConfigurationError):
                    collector.run_scoped(scope)
            # Duplicate entries collapse into one scoped fetch of that feed.
            staged = collector.run_scoped(["https://a.example.test/rss", "https://a.example.test/rss"])
            self.assertEqual(len(staged), 2)

    def test_atomic_state_survives_new_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            first = connectors.Collector(directory)
            first.configure({"enabled": False, "authorized": True, "feeds": [{"url": "https://news.example.test/rss", "authorized": True}]})
            second = connectors.Collector(directory)
            self.assertEqual(second.config()["feeds"][0]["url"], "https://news.example.test/rss")
            self.assertTrue(Path(directory, connectors.LOCK_FILENAME).exists())

    def test_normalize_quote_requires_explicit_finite_positive_snapshot_fields(self):
        payload = {
            "symbol": "NVDA",
            "price": 100.25,
            "shares": 123456,
            "currency": "usd",
            "observed_at": "2026-10-08T12:00:00Z",
            "source_url": "https://quotes.example.test/snapshot",
        }
        quote = connectors.normalize_quote(payload)
        self.assertEqual(quote["currency"], "USD")
        self.assertEqual(quote["data_status"], "manual_snapshot")
        self.assertFalse(quote["facts_approved"])
        for field, value in (("price", 0), ("price", float("nan")), ("shares", -1)):
            invalid = dict(payload, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                connectors.normalize_quote(invalid)
        with self.assertRaises(ValueError):
            connectors.normalize_quote(dict(payload, source_url="http://quotes.example.test/snapshot"))

    def test_alpha_vantage_adapter_is_optional_and_delayed(self):
        response = connectors.FetchResult(
            200,
            {},
            json.dumps({"Global Quote": {"05. price": "100.5", "06. volume": "1234", "07. latest trading day": "2026-10-07"}}).encode(),
        )
        with patch.object(connectors, "_ALPHA_CALL_TIMES", []):
            result = connectors.fetch_alpha_vantage_quote("NVDA", api_key="test-key", fetcher=lambda *_args: response)
        self.assertTrue(result["delayed"])
        self.assertEqual(result["observed_at"], "2026-10-07T00:00:00Z")
        self.assertEqual(result["provider"], "alpha_vantage")


if __name__ == "__main__":
    unittest.main()
