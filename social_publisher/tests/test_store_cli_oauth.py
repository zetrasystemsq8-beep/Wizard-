import unittest
from datetime import datetime, timedelta, timezone

from naijalearn_publisher.cli import build_parser, build_post_fields, content_hash, parse_when
from naijalearn_publisher.config import ConfigError, load_facebook_settings, load_worker_settings
from naijalearn_publisher.destinations import UnsupportedDestination, build_publisher, supported_destinations
from naijalearn_publisher.doctor import run_doctor
from naijalearn_publisher.oauth_connect import (OAuthError, build_authorize_url, exchange_for_page_token,
                                                parse_callback)
from naijalearn_publisher.store import StoreError, SupabaseStore

from .fakes import FakeHttp, ok

WHEN = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


class StoreTests(unittest.TestCase):
    def test_claim_calls_rpc_with_service_key_headers(self):
        http = FakeHttp(ok([{"id": "p1"}]))
        store = SupabaseStore("https://x.supabase.co/", "sb_secret_abc", http=http)
        rows = store.claim_due("facebook_page", 1, 15)
        call = http.calls[0]
        self.assertEqual(rows, [{"id": "p1"}])
        self.assertEqual(call["url"], "https://x.supabase.co/rest/v1/rpc/social_claim_due_posts")
        self.assertEqual(call["json_body"], {"p_destination": "facebook_page", "p_limit": 1,
                                             "p_lock_timeout_minutes": 15})
        self.assertEqual(call["headers"], {"apikey": "sb_secret_abc"})

    def test_legacy_jwt_key_also_sent_as_bearer(self):
        http = FakeHttp(ok([]))
        SupabaseStore("https://x.supabase.co", "eyJhbGciOi.payload.sig", http=http).ping()
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer eyJhbGciOi.payload.sig")

    def test_set_status_returns_none_when_no_row_matches(self):
        store = SupabaseStore("https://x.supabase.co", "k", http=FakeHttp(ok([])))
        self.assertIsNone(store.set_status("id", ["draft"], "approved"))

    def test_retry_and_fail_only_touch_rows_still_publishing(self):
        http = FakeHttp(ok([]), ok([]))
        store = SupabaseStore("https://x.supabase.co", "k", http=http)
        store.schedule_retry("p1", "2026-10-20T09:00:00+00:00", "e")
        store.mark_failed("p1", "e")
        for call in http.calls:
            self.assertEqual(call["query"]["status"], "eq.publishing")

    def test_http_errors_raise_store_error(self):
        store = SupabaseStore("https://x.supabase.co", "k", http=FakeHttp(ok({"message": "denied"}, 401)))
        with self.assertRaises(StoreError):
            store.ping()


class CliTests(unittest.TestCase):
    def test_kind_inference(self):
        self.assertEqual(build_post_fields(message="a", when=WHEN)["kind"], "text")
        self.assertEqual(build_post_fields(message="a", when=WHEN, link_url="https://a.example")["kind"], "link")
        self.assertEqual(build_post_fields(message="a", when=WHEN, image_url="https://a.example/i.jpg")["kind"], "photo")

    def test_validation(self):
        bad = [dict(message="  "), dict(message="a", link_url="http://insecure.example"),
               dict(message="a", link_url="https://a.example", image_url="https://a.example/i.jpg"),
               dict(message="x" * 63001)]
        for kwargs in bad:
            with self.assertRaises(ValueError):
                build_post_fields(when=WHEN, **kwargs)

    def test_facebook_groups_is_rejected(self):
        with self.assertRaises(UnsupportedDestination):
            build_post_fields(message="a", when=WHEN, destination="facebook_group")
        with self.assertRaises(UnsupportedDestination) as ctx:
            build_publisher("facebook_group", {})
        self.assertIn("Groups", str(ctx.exception))
        self.assertEqual(supported_destinations(), ["facebook_page"])

    def test_parse_when_requires_offset_and_converts_to_utc(self):
        self.assertEqual(parse_when("2026-10-20T09:00:00+01:00"), WHEN)
        with self.assertRaises(ValueError):
            parse_when("2026-10-20T09:00:00")
        with self.assertRaises(ValueError):
            parse_when("tomorrow")

    def test_content_hash_ignores_case_and_whitespace(self):
        a = content_hash("facebook_page", "Hello   World", None, None)
        self.assertEqual(a, content_hash("facebook_page", "hello world", None, None))
        self.assertNotEqual(a, content_hash("facebook_page", "hello world", "https://a.example", None))

    def test_parser_accepts_known_commands(self):
        args = build_parser().parse_args(["add", "--message", "hi", "--at", "2026-10-20T09:00:00+01:00"])
        self.assertEqual(args.destination, "facebook_page")


class ConfigTests(unittest.TestCase):
    def test_missing_secret_is_a_clear_error(self):
        with self.assertRaises(ConfigError):
            load_facebook_settings({"FB_PAGE_ID": "1"})

    def test_bad_values_rejected(self):
        with self.assertRaises(ConfigError):
            load_worker_settings({"PUBLISH_MAX_PER_DAY": "lots"})
        with self.assertRaises(ConfigError):
            load_facebook_settings({"FB_PAGE_ID": "1", "FB_PAGE_ACCESS_TOKEN": "t", "FB_GRAPH_VERSION": "latest"})

    def test_defaults(self):
        settings = load_worker_settings({})
        self.assertEqual((settings.max_per_day, settings.min_interval_seconds), (3, 3600))


class OAuthTests(unittest.TestCase):
    def test_authorize_url_requests_only_page_permissions_and_state(self):
        url = build_authorize_url("APP", "http://localhost:8765/callback", "STATE", "v25.0")
        self.assertIn("pages_manage_posts", url)
        self.assertIn("state=STATE", url)
        self.assertNotIn("publish_to_groups", url)

    def test_callback_validates_state(self):
        self.assertEqual(parse_callback("/callback?code=C&state=S", "S"), "C")
        with self.assertRaises(OAuthError):
            parse_callback("/callback?code=C&state=EVIL", "S")
        with self.assertRaises(OAuthError):
            parse_callback("/callback?error=access_denied&error_description=User+denied&state=S", "S")
        with self.assertRaises(OAuthError):
            parse_callback("/callback?state=S", "S")

    def test_exchange_returns_selected_page_token(self):
        http = FakeHttp(ok({"access_token": "short"}), ok({"access_token": "long"}),
                        ok({"data": [{"id": "1", "name": "A", "access_token": "PT1", "tasks": ["CREATE_CONTENT"]},
                                     {"id": "2", "name": "B", "access_token": "PT2", "tasks": []}]}))
        page = exchange_for_page_token(http, version="v25.0", app_id="a", app_secret="s",
                                       redirect_uri="http://localhost:8765/callback", code="c", page_id="1")
        self.assertEqual(page["access_token"], "PT1")
        self.assertEqual(http.calls[1]["query"]["grant_type"], "fb_exchange_token")

    def test_exchange_requires_page_choice_when_ambiguous(self):
        http = FakeHttp(ok({"access_token": "s"}), ok({"access_token": "l"}),
                        ok({"data": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]}))
        with self.assertRaises(OAuthError):
            exchange_for_page_token(http, version="v25.0", app_id="a", app_secret="s",
                                    redirect_uri="r", code="c", page_id=None)


class DoctorTests(unittest.TestCase):
    ENV = {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_KEY": "k", "FB_PAGE_ID": "1",
           "FB_PAGE_ACCESS_TOKEN": "PT", "FB_APP_ID": "a", "FB_APP_SECRET": "s"}

    def test_healthy_setup(self):
        http = FakeHttp(ok([]), ok({"id": "1", "name": "NaijaLearn"}),
                        ok({"data": {"is_valid": True, "scopes": ["pages_manage_posts"], "expires_at": 0}}))
        lines = []
        self.assertEqual(run_doctor(self.ENV, http=http, out=lines.append), 0)
        self.assertTrue(any("no expiry" in line for line in lines))

    def test_missing_scope_fails(self):
        http = FakeHttp(ok([]), ok({"id": "1", "name": "N"}),
                        ok({"data": {"is_valid": True, "scopes": ["pages_show_list"], "expires_at": 0}}))
        self.assertEqual(run_doctor(self.ENV, http=http, out=lambda _: None), 1)


if __name__ == "__main__":
    unittest.main()
