import unittest
from datetime import datetime, timedelta, timezone

from ekko.core.models import (FeedbackObject, Lineage, Reply, Source,
                              pseudonymize_author)
from ekko.core.scoring import compute_score

NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
_counter = [0]


def fo(stars: float, days_ago: int, source=Source.GOOGLE, reply=False):
    _counter[0] += 1
    i = _counter[0]
    return FeedbackObject(
        id=f"t{i}", source=source, source_native_id=f"n{i}",
        business_id="b1", author_hash=pseudonymize_author(f"a{i}"),
        rating=FeedbackObject.normalize_rating(stars),
        published_at=NOW - timedelta(days=days_ago),
        reply=Reply(text="grazie") if reply else None,
        lineage=Lineage(connector="test", run_id="r1", license="demo"),
    )


class TestScoring(unittest.TestCase):
    def test_empty_is_neutral(self):
        b = compute_score([], now=NOW)
        self.assertEqual(b.score, 50.0)
        self.assertEqual(b.n_feedback, 0)

    def test_good_reviews_beat_bad(self):
        good = compute_score([fo(5, d) for d in range(0, 200, 10)], now=NOW)
        bad = compute_score([fo(1, d) for d in range(0, 200, 10)], now=NOW)
        self.assertGreater(good.score, bad.score)
        self.assertGreater(good.score, 60)
        self.assertLess(bad.score, 45)

    def test_bayes_shrinks_low_volume(self):
        one = compute_score([fo(5, 5)], now=NOW)
        many = compute_score([fo(5, d) for d in range(0, 300, 5)], now=NOW)
        # una sola recensione 5* non deve valere quanto 60 recensioni 5*
        self.assertLess(one.base_component, many.base_component)

    def test_recency_decay(self):
        recent_bad = compute_score(
            [fo(5, 400) for _ in range(20)] +
            [fo(1, d) for d in range(0, 30, 3)], now=NOW)
        old_bad = compute_score(
            [fo(1, 400 + d) for d in range(20)] +
            [fo(5, d) for d in range(0, 30, 3)], now=NOW)
        # le recensioni negative recenti pesano più di quelle vecchie
        self.assertGreater(old_bad.score, recent_bad.score)

    def test_trend_detection(self):
        improving = [fo(2, 90 + d) for d in range(0, 90, 10)] + \
                    [fo(5, d) for d in range(0, 90, 10)]
        b = compute_score(improving, now=NOW)
        self.assertGreater(b.trend_delta, 0)
        self.assertGreater(b.trend_component, 50)

    def test_engagement_component(self):
        with_replies = compute_score(
            [fo(4, 10, reply=True) for _ in range(10)], now=NOW)
        without = compute_score([fo(4, 10) for _ in range(10)], now=NOW)
        self.assertGreater(with_replies.score, without.score)

    def test_breakdown_has_sources(self):
        b = compute_score([fo(4, 5), fo(2, 8, source=Source.TRUSTPILOT)], now=NOW)
        self.assertEqual({s.source for s in b.by_source}, {"google", "trustpilot"})


if __name__ == "__main__":
    unittest.main()
