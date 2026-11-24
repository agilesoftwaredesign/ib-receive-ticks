"""TWS-is-up check: e-mail alerts when the connection to TWS / IB Gateway stays down (see `alerts:` in config.yaml)."""

import logging
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable
from zoneinfo import ZoneInfo

from config import Alerts

log = logging.getLogger("ib-receive-ticks")

KIND_LONG = "long"  # down for `down_after_min` minutes or longer
KIND_DAILY = "daily"  # still down at the daily check time
SMTP_TIMEOUT = 30  # seconds


class DownMonitor:
    """Tracks one outage at a time and calls `send(kind, down_since, now)`.

    The long-outage alert is sent once per outage. The daily alert is sent every time the daily check time
    passes while the outage lasts.

    Feed it with `tick(connected)` as often as convenient (every few seconds). An outage starts with the first
    tick that reports "not connected" and ends with the first tick that reports "connected"; only then is
    the long-outage alert armed again.
    """

    def __init__(
        self,
        alerts: Alerts,
        tz: ZoneInfo,
        send: Callable[[str, datetime, datetime], None],
    ):
        self.alerts, self.tz, self.send = alerts, tz, send
        self.down_since: datetime | None = None
        self.daily_due: datetime | None = None
        self.long_sent = False

    def _next_daily(self, since: datetime) -> datetime:
        """First occurrence of the daily check time (in `tz`) after `since`."""
        local = since.astimezone(self.tz)
        due = datetime.combine(local.date(), self.alerts.daily_check_time, self.tz)
        if due <= local:
            due = datetime.combine(
                local.date() + timedelta(days=1), self.alerts.daily_check_time, self.tz
            )
        return due

    def tick(self, connected: bool, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if connected:
            if self.down_since is not None:
                log.info("TWS reachable again, down-alerts re-armed")
            self.down_since = self.daily_due = None
            return
        if self.down_since is None:
            self.down_since = now
            self.daily_due = self._next_daily(now)
            self.long_sent = False
            log.warning("TWS is down")
        if (
            not self.long_sent
            and now - self.down_since >= timedelta(minutes=self.alerts.down_after_min)
        ):
            self.long_sent = True  # set first: a failing send must not be retried every tick
            self._send(KIND_LONG, now)
        if now >= self.daily_due:
            self.daily_due = self._next_daily(now)  # next day's check; set first like long_sent
            self._send(KIND_DAILY, now)

    def _send(self, kind: str, now: datetime) -> None:
        try:
            self.send(kind, self.down_since, now)
            log.warning(f"Down-alert '{kind}' sent")
        except Exception as e:  # noqa: BLE001 - an alert problem must never stop the data download
            log.error(f"Down-alert '{kind}' could not be sent: {e}")


def _fmt_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def build_message(
    alerts: Alerts,
    kind: str,
    host: str,
    port: int,
    down_since: datetime,
    now: datetime,
    tz: ZoneInfo,
    retry_min: float,
) -> EmailMessage:
    """The e-mail for one alert; contains no credentials."""
    since = down_since.astimezone(tz)
    if kind == KIND_LONG:
        reason = (
            f"The connection to TWS / IB Gateway at {host}:{port} has been down for "
            f"{_fmt_duration(now - down_since)} (since {since:%Y-%m-%d %H:%M} {since.tzname()})."
        )
    else:
        reason = (
            f"TWS / IB Gateway at {host}:{port} was still unreachable at the daily check "
            f"({alerts.daily_check_time:%H:%M} {tz.key}); down since {since:%Y-%m-%d %H:%M} {since.tzname()}."
        )
    footer = (
        "This alert is sent once per outage; it is re-armed after the connection has been re-established."
        if kind == KIND_LONG
        else f"This reminder is repeated every day at {alerts.daily_check_time:%H:%M} while TWS stays down."
    )
    body = f"""{reason}

While TWS is down no new data is received. ib-receive-ticks (running on {socket.gethostname()}) retries
every {retry_min:g} min and continues automatically once TWS is reachable; the update then starts from the newest
stored second, so the gap is filled.

Please check that TWS / IB Gateway is running and logged in (daily auto-restart or logoff, pending
two-factor confirmation, API enabled on port {port}).

{footer}
"""
    msg = EmailMessage()
    msg["Subject"] = alerts.email_subject
    msg["From"] = alerts.email_from
    msg["To"] = ", ".join(alerts.email_to)
    msg.set_content(body)
    return msg


def send_email(alerts: Alerts, msg: EmailMessage) -> None:
    """Deliver `msg` through the SMTP server of `alerts`; raises on any failure."""
    smtp = alerts.smtp
    if smtp.security == "ssl":
        server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=SMTP_TIMEOUT)
    else:
        server = smtplib.SMTP(smtp.host, smtp.port, timeout=SMTP_TIMEOUT)
    with server:
        if smtp.security == "starttls":
            server.starttls()
        if smtp.user:
            server.login(smtp.user, smtp.password)
        server.send_message(msg)
