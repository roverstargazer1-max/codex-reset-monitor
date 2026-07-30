from __future__ import annotations

import unittest
from datetime import datetime, timezone

from monitor import Forecast, Monitor, MonitorState


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
FORECAST_LOW = Forecast(80, "2026-07-30T07:00:00Z", "2026-07-29T04:09:02Z")
FORECAST_HIGH = Forecast(81, "2026-07-30T07:00:00Z", "2026-07-29T04:09:02Z")


class FakeStore:
    def __init__(self, state: MonitorState | None = None) -> None:
        self.state = state or MonitorState()
        self.saves: list[tuple[MonitorState, str]] = []

    def load(self) -> MonitorState:
        return MonitorState.from_dict(self.state.to_dict())

    def save(self, state: MonitorState, message: str) -> None:
        self.state = MonitorState.from_dict(state.to_dict())
        self.saves.append((self.state, message))


class FakeNotifier:
    def __init__(self) -> None:
        self.probability_alerts: list[int] = []
        self.failure_alerts: list[int] = []
        self.recovery_alerts: list[int] = []

    def send_probability_alert(self, forecast: Forecast, threshold: int, checked_at: datetime) -> None:
        self.probability_alerts.append(forecast.probability_24h)

    def send_failure_alert(self, error: Exception, failures: int, checked_at: datetime) -> None:
        self.failure_alerts.append(failures)

    def send_recovery_alert(self, previous_failures: int, checked_at: datetime) -> None:
        self.recovery_alerts.append(previous_failures)


class MonitorTests(unittest.TestCase):
    def make_monitor(self, store: FakeStore, notifier: FakeNotifier, result) -> Monitor:
        def fetcher():
            if isinstance(result, Exception):
                raise result
            return result

        return Monitor(store, notifier, forecast_fetcher=fetcher, clock=lambda: NOW)

    def test_exactly_80_does_not_alert(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        result = self.make_monitor(store, notifier, FORECAST_LOW).run_once()
        self.assertEqual(result, 0)
        self.assertEqual(notifier.probability_alerts, [])
        self.assertFalse(store.state.above_threshold)

    def test_first_high_value_alerts(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_HIGH).run_once()
        self.assertEqual(notifier.probability_alerts, [81])
        self.assertTrue(store.state.above_threshold)

    def test_sustained_high_value_does_not_repeat(self) -> None:
        state = MonitorState(initialized=True, above_threshold=True)
        store, notifier = FakeStore(state), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_HIGH).run_once()
        self.assertEqual(notifier.probability_alerts, [])

    def test_drop_then_cross_alerts_again(self) -> None:
        state = MonitorState(initialized=True, above_threshold=True)
        store, notifier = FakeStore(state), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_LOW).run_once()
        self.make_monitor(store, notifier, FORECAST_HIGH).run_once()
        self.assertEqual(notifier.probability_alerts, [81])

    def test_failure_alert_is_sent_on_third_failure_only(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        for _ in range(4):
            self.make_monitor(store, notifier, RuntimeError("offline")).run_once()
        self.assertEqual(notifier.failure_alerts, [3])
        self.assertEqual(store.state.consecutive_failures, 4)
        self.assertTrue(store.state.failure_alert_sent)

    def test_recovery_is_sent_after_alerted_failure(self) -> None:
        state = MonitorState(
            initialized=True,
            above_threshold=False,
            consecutive_failures=3,
            failure_alert_sent=True,
        )
        store, notifier = FakeStore(state), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_LOW).run_once()
        self.assertEqual(notifier.recovery_alerts, [3])
        self.assertEqual(store.state.consecutive_failures, 0)
        self.assertFalse(store.state.failure_alert_sent)


if __name__ == "__main__":
    unittest.main()
