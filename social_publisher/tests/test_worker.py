import unittest
from datetime import timedelta

from naijalearn_publisher.config import WorkerSettings
from naijalearn_publisher.worker import compute_backoff, run_once

from .fakes import NOW, FakePublisher, InMemoryStore, permanent, retryable

SETTINGS = WorkerSettings(max_per_run=3, min_interval_seconds=0, max_per_day=10,
                          retry_base_seconds=600, retry_cap_seconds=21600, lock_timeout_minutes=15)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = InMemoryStore(self.clock)

    def run_worker(self, publisher, settings=SETTINGS):
        return run_once(self.store, {"facebook_page": publisher}, settings, clock=self.clock, rng=lambda: 0.5)

    def test_publishes_due_approved_post_and_logs_attempt(self):
        self.store.add("p1")
        summary = self.run_worker(FakePublisher())
        self.assertEqual(summary.published, 1)
        post = self.store.posts["p1"]
        self.assertEqual((post["status"], post["external_id"], post["attempts"]), ("published", "page_1", 1))
        self.assertEqual(self.store.attempts[0]["outcome"], "success")

    def test_drafts_future_and_cancelled_posts_are_never_published(self):
        self.store.add("draft", status="draft")
        self.store.add("future", scheduled_offset=3600)
        self.store.add("cancelled", status="cancelled")
        publisher = FakePublisher()
        summary = self.run_worker(publisher)
        self.assertEqual((summary.published, publisher.published), (0, []))

    def test_retryable_error_schedules_backoff_then_succeeds(self):
        self.store.add("p1")
        publisher = FakePublisher(retryable("temporary"))
        summary = self.run_worker(publisher)
        self.assertEqual((summary.retried, summary.published), (1, 0))
        post = self.store.posts["p1"]
        self.assertEqual(post["status"], "approved")
        self.assertEqual(post["next_attempt_at"], NOW + timedelta(seconds=600))  # rng 0.5 => no jitter
        self.assertEqual(self.store.attempts[0]["outcome"], "retryable_error")
        # Not due yet: nothing happens.
        self.assertEqual(self.run_worker(publisher).published, 0)
        # After the backoff elapses the second attempt publishes.
        self.clock.now = NOW + timedelta(seconds=601)
        self.assertEqual(self.run_worker(publisher).published, 1)
        self.assertEqual(self.store.posts["p1"]["attempts"], 2)

    def test_permanent_error_fails_immediately(self):
        self.store.add("p1")
        summary = self.run_worker(FakePublisher(permanent("token expired", code=190)))
        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.store.posts["p1"]["status"], "failed")
        self.assertEqual(self.store.attempts[0]["error_code"], 190)
        self.assertEqual(self.store.attempts[0]["outcome"], "permanent_error")

    def test_retries_stop_at_max_attempts(self):
        self.store.add("p1", max_attempts=2)
        publisher = FakePublisher(retryable(), retryable())
        self.run_worker(publisher)
        self.clock.now = NOW + timedelta(hours=2)
        summary = self.run_worker(publisher)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.store.posts["p1"]["status"], "failed")

    def test_rate_limit_hint_extends_backoff(self):
        self.assertEqual(compute_backoff(1, 600, 21600, 3600, lambda: 0.5), 3600)
        self.assertEqual(compute_backoff(3, 600, 21600, None, lambda: 0.5), 2400)
        self.assertEqual(compute_backoff(20, 600, 21600, None, lambda: 0.5), 21600)

    def test_minimum_interval_blocks_new_posts(self):
        self.store.add("old", status="published", published_at=NOW - timedelta(minutes=10))
        self.store.add("p1")
        settings = WorkerSettings(3, 3600, 10, 600, 21600, 15)
        publisher = FakePublisher()
        summary = self.run_worker(publisher, settings)
        self.assertEqual(publisher.published, [])
        self.assertIn("minimum interval", summary.notes[0])
        self.assertEqual(self.store.posts["p1"]["status"], "approved")

    def test_daily_cap_blocks_new_posts(self):
        for i in range(2):
            self.store.add(f"old{i}", status="published", published_at=NOW - timedelta(hours=5 + i))
        self.store.add("p1")
        settings = WorkerSettings(3, 0, 2, 600, 21600, 15)
        publisher = FakePublisher()
        summary = self.run_worker(publisher, settings)
        self.assertEqual(publisher.published, [])
        self.assertIn("daily cap", summary.notes[0])

    def test_max_per_run_limits_batch(self):
        for i in range(5):
            self.store.add(f"p{i}", scheduled_offset=-100 + i)
        settings = WorkerSettings(2, 0, 10, 600, 21600, 15)
        summary = self.run_worker(FakePublisher(), settings)
        self.assertEqual(summary.published, 2)

    def test_unexpected_exception_marks_failed_without_retry(self):
        self.store.add("p1")
        summary = self.run_worker(FakePublisher(RuntimeError("bug")))
        self.assertEqual(summary.failed, 1)
        self.assertIn("Check the Page", self.store.posts["p1"]["last_error"])

    def test_secrets_are_redacted_from_stored_errors(self):
        self.store.add("p1")
        self.run_worker(FakePublisher(permanent("bad token SECRET-TOKEN rejected")))
        self.assertNotIn("SECRET-TOKEN", self.store.posts["p1"]["last_error"])
        self.assertNotIn("SECRET-TOKEN", self.store.attempts[0]["error_message"])

    def test_audit_log_failure_does_not_undo_publish(self):
        self.store.add("p1")

        def boom(**_):
            raise RuntimeError("db down")

        self.store.record_attempt = boom
        summary = self.run_worker(FakePublisher())
        self.assertEqual(summary.published, 1)
        self.assertEqual(self.store.posts["p1"]["status"], "published")


if __name__ == "__main__":
    unittest.main()
