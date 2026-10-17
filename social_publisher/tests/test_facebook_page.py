import unittest

from naijalearn_publisher.config import FacebookSettings
from naijalearn_publisher.destinations.base import PublishError
from naijalearn_publisher.destinations.facebook_page import FacebookPagePublisher
from naijalearn_publisher.http import TransportError

from .fakes import FakeHttp, ok

SETTINGS = FacebookSettings(page_id="123", page_token="PAGE-TOKEN", graph_version="v25.0")


def err(code, status=400, **extra):
    return ok({"error": {"message": f"boom {code}", "code": code, "fbtrace_id": "T1", **extra}}, status)


class FacebookPageTests(unittest.TestCase):
    def publisher(self, *responses):
        http = FakeHttp(*responses)
        return FacebookPagePublisher(SETTINGS, http=http), http

    def test_text_post_hits_feed_with_token_in_body_not_url(self):
        pub, http = self.publisher(ok({"id": "123_456"}))
        result = pub.publish({"kind": "text", "message": "Hello Naija"})
        call = http.calls[0]
        self.assertEqual(call["url"], "https://graph.facebook.com/v25.0/123/feed")
        self.assertEqual(call["form"], {"message": "Hello Naija", "access_token": "PAGE-TOKEN"})
        self.assertNotIn("PAGE-TOKEN", call["url"])
        self.assertEqual(result.external_id, "123_456")
        self.assertEqual(result.permalink, "https://www.facebook.com/123_456")

    def test_link_post(self):
        pub, http = self.publisher(ok({"id": "1_2"}))
        pub.publish({"kind": "link", "message": "Read", "link_url": "https://naijalearn.example/x"})
        self.assertEqual(http.calls[0]["form"]["link"], "https://naijalearn.example/x")

    def test_photo_post_uses_photos_endpoint_and_prefers_post_id(self):
        pub, http = self.publisher(ok({"id": "photo9", "post_id": "123_789"}))
        result = pub.publish({"kind": "photo", "message": "Cap", "image_url": "https://cdn.example/a.jpg"})
        self.assertTrue(http.calls[0]["url"].endswith("/123/photos"))
        self.assertEqual(http.calls[0]["form"]["caption"], "Cap")
        self.assertEqual(result.external_id, "123_789")

    def test_expired_token_is_permanent(self):
        pub, _ = self.publisher(err(190))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, 190)
        self.assertEqual(ctx.exception.trace_id, "T1")

    def test_rate_limit_is_retryable_with_long_wait(self):
        pub, _ = self.publisher(err(4))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.retry_after_seconds, 3600)

    def test_spam_block_and_permission_errors_are_not_retried(self):
        for code in (368, 200, 10, 100):
            pub, _ = self.publisher(err(code))
            with self.assertRaises(PublishError) as ctx:
                pub.publish({"kind": "text", "message": "x"})
            self.assertFalse(ctx.exception.retryable, code)

    def test_server_error_and_transient_flag_retry(self):
        pub, _ = self.publisher(ok({"error": {"message": "oops", "code": 999}}, 503))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertTrue(ctx.exception.retryable)
        pub, _ = self.publisher(err(999, is_transient=True))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertTrue(ctx.exception.retryable)

    def test_timeout_after_send_is_not_retried_to_avoid_duplicates(self):
        pub, _ = self.publisher(TransportError("timed out", ambiguous=True))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("Check the Page", str(ctx.exception))

    def test_dns_failure_before_send_is_retryable(self):
        pub, _ = self.publisher(TransportError("gaierror", ambiguous=False))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertTrue(ctx.exception.retryable)

    def test_success_without_id_is_flagged_not_retried(self):
        pub, _ = self.publisher(ok({}))
        with self.assertRaises(PublishError) as ctx:
            pub.publish({"kind": "text", "message": "x"})
        self.assertFalse(ctx.exception.retryable)

    def test_invalid_kind_and_missing_fields(self):
        pub, http = self.publisher()
        for post in ({"kind": "reel", "message": "x"}, {"kind": "link", "message": "x"},
                     {"kind": "photo", "message": "x"}):
            with self.assertRaises(PublishError):
                pub.publish(post)
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
