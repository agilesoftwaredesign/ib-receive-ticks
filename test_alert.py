"""Unit tests for alert.py (TWS-is-up check) and the `alerts:` config section.

    ./venv/bin/python -m unittest test_alert -v
"""

import os
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from alert import KIND_DAILY, KIND_LONG, DownMonitor, build_message, send_email
from config import ENV_SMTP_PASSWORD, Alerts, Smtp, load_config

BERLIN = ZoneInfo("Europe/Berlin")


def local(h, m=0, day=10):
    """A moment on 2026-11-`day` in Berlin (winter time, no DST surprises), as UTC."""
    return datetime(2026, 11, day, h, m, tzinfo=BERLIN).astimezone(timezone.utc)


def _alerts(**kw):
    return Alerts(
        smtp=Smtp(host="smtp.example.com", user="u", password="secret"),
        email_to=["a@example.com", "b@example.com"],
        email_from="ticks@example.com",
        **kw,
    )


class TestDownMonitor(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.mon = DownMonitor(
            _alerts(), BERLIN, lambda kind, since, now: self.sent.append(kind)
        )

    def kinds(self):
        return list(self.sent)

    def test_connected_never_alerts(self):
        for i in range(100):
            self.mon.tick(True, local(12) + timedelta(hours=i))
        self.assertEqual(self.kinds(), [])

    def test_long_outage_alerts_once_after_30_min(self):
        t0 = local(12)
        self.mon.tick(False, t0)
        self.mon.tick(False, t0 + timedelta(minutes=29, seconds=59))
        self.assertEqual(self.kinds(), [])
        self.mon.tick(False, t0 + timedelta(minutes=30))
        self.assertEqual(self.kinds(), [KIND_LONG])
        for m in range(31, 14 * 60, 7):  # ... never again, however long it lasts (until 01:00 next day)
            self.mon.tick(False, t0 + timedelta(minutes=m))
        self.assertEqual(self.kinds(), [KIND_LONG, KIND_DAILY])  # daily is a separate alert, next one is 01:00

    def test_short_outage_does_not_alert_and_resets_the_timer(self):
        t0 = local(12)
        self.mon.tick(False, t0)
        self.mon.tick(True, t0 + timedelta(minutes=20))
        self.mon.tick(False, t0 + timedelta(minutes=25))  # new outage, timer restarts
        self.mon.tick(False, t0 + timedelta(minutes=50))  # 25 min into it
        self.assertEqual(self.kinds(), [])
        self.mon.tick(False, t0 + timedelta(minutes=55))
        self.assertEqual(self.kinds(), [KIND_LONG])

    def test_reconnect_rearms_long_alert(self):
        t0 = local(12)
        self.mon.tick(False, t0)
        self.mon.tick(False, t0 + timedelta(minutes=31))
        self.mon.tick(True, t0 + timedelta(minutes=32))
        self.mon.tick(False, t0 + timedelta(minutes=40))
        self.mon.tick(False, t0 + timedelta(minutes=70))
        self.assertEqual(self.kinds(), [KIND_LONG, KIND_LONG])

    def test_daily_check_alerts_once_when_down_at_0100(self):
        self.mon.tick(False, local(0, 50))
        self.mon.tick(False, local(0, 59, 10))
        self.assertEqual(self.kinds(), [])
        self.mon.tick(False, local(1, 0))
        self.assertEqual(self.kinds(), [KIND_DAILY])  # only 10 min down: no long alert
        self.mon.tick(False, local(1, 10))
        self.mon.tick(False, local(1, 15))
        self.assertEqual(self.kinds(), [KIND_DAILY])
        self.mon.tick(False, local(1, 20))  # 30 min down: now the long alert, daily stays at one
        self.mon.tick(False, local(1, 40))
        self.assertEqual(self.kinds(), [KIND_DAILY, KIND_LONG])

    def test_three_day_outage_sends_long_then_one_daily_per_night(self):
        t = local(12, 0)  # down since 12:00 on the 10th
        while t < local(23, 0, day=13):  # tick every 10 s for 3+ days
            self.mon.tick(False, t)
            t += timedelta(seconds=10)
        # 12:30 long, then 01:00 on the 11th, 12th and 13th
        self.assertEqual(self.kinds(), [KIND_LONG] + [KIND_DAILY] * 3)

    def test_daily_check_ignores_connection_that_is_up_at_0100(self):
        self.mon.tick(False, local(0, 40))
        self.mon.tick(True, local(0, 50))
        self.mon.tick(True, local(1, 0))
        self.mon.tick(True, local(1, 5))
        self.assertEqual(self.kinds(), [])

    def test_outage_starting_after_0100_waits_for_next_day(self):
        self.mon.tick(False, local(1, 5))
        self.mon.tick(False, local(12, 0))
        self.assertEqual(self.kinds(), [KIND_LONG])  # not daily: 01:00 had passed
        self.mon.tick(False, local(1, 0, day=11))
        self.assertEqual(self.kinds(), [KIND_LONG, KIND_DAILY])

    def test_daily_restarts_with_the_next_outage(self):
        self.mon.tick(False, local(0, 30))
        self.mon.tick(False, local(1, 0))
        self.mon.tick(True, local(1, 5))
        self.mon.tick(False, local(23, 0))
        self.mon.tick(False, local(1, 0, day=11))
        # 00:30-01:00 is 30 min down (long + daily), the second outage 23:00-01:00 again both
        self.assertEqual(self.kinds(), [KIND_LONG, KIND_DAILY] * 2)

    def test_outage_starting_in_the_evening_alerts_at_0100(self):
        self.mon.tick(False, local(23, 0))
        self.assertEqual(self.kinds(), [])
        self.mon.tick(False, local(1, 0, day=11))
        self.assertEqual(self.kinds(), [KIND_LONG, KIND_DAILY])

    def test_both_alerts_can_fire_on_the_same_tick(self):
        self.mon.tick(False, local(0, 20))
        self.mon.tick(False, local(1, 0))  # 40 min down and 01:00
        self.assertEqual(self.kinds(), [KIND_LONG, KIND_DAILY])

    def test_failing_send_is_not_retried_and_does_not_raise(self):
        calls = []

        def boom(kind, since, now):
            calls.append(kind)
            raise OSError("smtp down")

        mon = DownMonitor(_alerts(), BERLIN, boom)
        t0 = local(12)
        mon.tick(False, t0)
        mon.tick(False, t0 + timedelta(minutes=31))
        mon.tick(False, t0 + timedelta(minutes=32))
        self.assertEqual(calls, [KIND_LONG])

    def test_configurable_times(self):
        sent = []
        mon = DownMonitor(
            _alerts(down_after_min=5, daily_check_time=time(3, 30)),
            BERLIN,
            lambda kind, since, now: sent.append(kind),
        )
        mon.tick(False, local(3, 20))
        mon.tick(False, local(3, 25))
        self.assertEqual(sent, [KIND_LONG])
        mon.tick(False, local(3, 30))
        self.assertEqual(sent, [KIND_LONG, KIND_DAILY])


class TestEmail(unittest.TestCase):
    def test_message_has_recipients_subject_and_no_password(self):
        a = _alerts()
        msg = build_message(
            a, KIND_LONG, "127.0.0.1", 7496, local(12), local(12, 31), BERLIN, 5
        )
        self.assertEqual(msg["To"], "a@example.com, b@example.com")
        self.assertEqual(msg["From"], "ticks@example.com")
        self.assertEqual(msg["Subject"], a.email_subject)
        body = msg.get_content()
        self.assertIn("127.0.0.1:7496", body)
        self.assertIn("31 min", body)
        self.assertNotIn("secret", body)

    def test_daily_message_names_the_check_time(self):
        msg = build_message(
            _alerts(), KIND_DAILY, "h", 1, local(0, 30), local(1, 0), BERLIN, 5
        )
        self.assertIn("01:00 Europe/Berlin", msg.get_content())

    def test_send_starttls_login_and_send(self):
        a = _alerts()
        msg = build_message(a, KIND_LONG, "h", 1, local(12), local(13), BERLIN, 5)
        with mock.patch("alert.smtplib.SMTP") as smtp:
            send_email(a, msg)
        server = smtp.return_value
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("u", "secret")
        server.send_message.assert_called_once_with(msg)

    def test_send_ssl_without_login(self):
        a = _alerts()
        a.smtp.security, a.smtp.port, a.smtp.user = "ssl", 465, None
        msg = build_message(a, KIND_LONG, "h", 1, local(12), local(13), BERLIN, 5)
        with mock.patch("alert.smtplib.SMTP_SSL") as smtp:
            send_email(a, msg)
        server = smtp.return_value
        server.starttls.assert_not_called()
        server.login.assert_not_called()
        server.send_message.assert_called_once_with(msg)


BASE = """
instruments:
  - symbol: AAPL
"""


class TestAlertsConfig(unittest.TestCase):
    def load(self, alerts_yaml, env=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.yaml"
            path.write_text(BASE + alerts_yaml)
            clean = {k: v for k, v in os.environ.items() if k != ENV_SMTP_PASSWORD}
            with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
                return load_config(path)

    GOOD = """
alerts:
  smtp: {host: smtp.example.com, user: u, password: from-file}
  email_to: a@example.com
  email_from: ticks@example.com
"""

    def test_no_section_disables_the_check(self):
        self.assertIsNone(self.load("").alerts)

    def test_defaults(self):
        a = self.load(self.GOOD).alerts
        self.assertEqual(a.email_to, ["a@example.com"])
        self.assertEqual((a.down_after_min, a.daily_check_time), (30, time(1, 0)))
        self.assertEqual((a.smtp.port, a.smtp.security), (587, "starttls"))

    def test_env_password_wins_over_file(self):
        a = self.load(self.GOOD, {ENV_SMTP_PASSWORD: "from-env"}).alerts
        self.assertEqual(a.smtp.password, "from-env")
        self.assertEqual(self.load(self.GOOD).alerts.smtp.password, "from-file")

    def test_user_without_any_password_is_rejected(self):
        yaml = self.GOOD.replace(", password: from-file", "")
        with self.assertRaisesRegex(ValueError, ENV_SMTP_PASSWORD):
            self.load(yaml)

    def test_custom_values(self):
        yaml = self.GOOD.replace(
            "  email_from", '  daily_check_time: "02:15"\n  down_after_min: 45\n  email_from'
        )
        a = self.load(yaml).alerts
        self.assertEqual((a.down_after_min, a.daily_check_time), (45, time(2, 15)))

    def test_unquoted_time_is_explained(self):
        yaml = self.GOOD + "  daily_check_time: 1:00\n"
        with self.assertRaisesRegex(ValueError, "quoted"):
            self.load(yaml)

    def test_validation_errors(self):
        for bad, msg in [
            (self.GOOD.replace("smtp.example.com", ""), "smtp.host"),
            (self.GOOD.replace("  email_to: a@example.com\n", ""), "email_to"),
            (self.GOOD.replace("  email_from: ticks@example.com\n", ""), "email_from"),
            (self.GOOD + "  down_after_min: 0\n", "down_after_min"),
            (self.GOOD + "  daily_check_time: '25:00'\n", "HH:MM"),
            (self.GOOD + "  typo: 1\n", "unknown key"),
            (self.GOOD.replace("host:", "security: tls, host:"), "security"),
        ]:
            with self.subTest(msg=msg), self.assertRaisesRegex(ValueError, msg):
                self.load(bad)


if __name__ == "__main__":
    unittest.main()
