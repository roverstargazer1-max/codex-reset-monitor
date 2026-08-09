from __future__ import annotations

import argparse
import base64
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Callable


FORECAST_URL = "https://codex-reset.com/api/forecast"
SITE_URL = "https://codex-reset.com/"
BEIJING = timezone(timedelta(hours=8))
STATE_PATH = "monitor-state.json"
STATE_BRANCH = "monitor-state"
HEARTBEAT_INTERVAL = timedelta(days=30)


class MonitorError(RuntimeError):
    """Base error for expected monitor failures."""


class ForecastError(MonitorError):
    """Raised when the forecast response is unavailable or invalid."""


class StateStoreError(MonitorError):
    """Raised when monitor state cannot be read or written."""


@dataclass(frozen=True)
class Forecast:
    probability_24h: int
    updated_at: str
    last_reset_at: str


@dataclass
class MonitorState:
    schema_version: int = 2
    initialized: bool = False
    above_threshold: bool = False
    last_observed_reset_at: str | None = None
    consecutive_failures: int = 0
    failure_alert_sent: bool = False
    last_heartbeat_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MonitorState":
        last_observed_reset_at = raw.get("last_observed_reset_at")
        return cls(
            schema_version=max(2, int(raw.get("schema_version", 1))),
            initialized=bool(raw.get("initialized", False)),
            above_threshold=bool(raw.get("above_threshold", False)),
            last_observed_reset_at=last_observed_reset_at if isinstance(last_observed_reset_at, str) else None,
            consecutive_failures=max(0, int(raw.get("consecutive_failures", 0))),
            failure_alert_sent=bool(raw.get("failure_alert_sent", False)),
            last_heartbeat_at=raw.get("last_heartbeat_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_beijing(value: str | datetime) -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    return dt.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"


def heartbeat_due(state: MonitorState, now: datetime) -> bool:
    if not state.last_heartbeat_at:
        return True
    try:
        return now - parse_iso(state.last_heartbeat_at) >= HEARTBEAT_INTERVAL
    except (TypeError, ValueError):
        return True


def reset_is_newer(previous_reset_at: str | None, current_reset_at: str) -> bool:
    if not previous_reset_at:
        return False
    try:
        return parse_iso(current_reset_at) > parse_iso(previous_reset_at)
    except (TypeError, ValueError):
        return False


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
    allow_404: bool = False,
) -> dict[str, Any] | None:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", "User-Agent": "codex-reset-monitor/1.0"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise MonitorError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MonitorError(f"Request failed for {url}: {exc}") from exc
    if not content:
        return {}
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Invalid JSON returned by {url}") from exc
    if not isinstance(parsed, dict):
        raise MonitorError(f"Expected a JSON object from {url}")
    return parsed


def fetch_forecast(*, attempts: int = 3, timeout: int = 15) -> Forecast:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload = _json_request(FORECAST_URL, timeout=timeout)
            if payload is None:
                raise ForecastError("Forecast endpoint returned no data")
            probabilities = payload.get("probabilities")
            if not isinstance(probabilities, dict):
                raise ForecastError("Missing probabilities object")
            probability = probabilities.get("rounded_24h")
            if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                raise ForecastError("Missing numeric probabilities.rounded_24h")
            probability_int = int(probability)
            if not 0 <= probability_int <= 100:
                raise ForecastError("probabilities.rounded_24h is outside 0..100")
            updated_at = payload.get("updated_at")
            last_reset_at = payload.get("last_reset_at")
            if not isinstance(updated_at, str) or not isinstance(last_reset_at, str):
                raise ForecastError("Missing updated_at or last_reset_at")
            parse_iso(updated_at)
            parse_iso(last_reset_at)
            return Forecast(probability_int, updated_at, last_reset_at)
        except (MonitorError, ValueError, TypeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    raise ForecastError(f"Forecast request failed after {attempts} attempts: {last_error}")


class GmailNotifier:
    def __init__(self, address: str, app_password: str) -> None:
        self.address = address.strip()
        self.app_password = app_password.replace(" ", "").strip()
        if not self.address or not self.app_password:
            raise MonitorError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required")

    @classmethod
    def from_env(cls) -> "GmailNotifier":
        return cls(os.getenv("GMAIL_ADDRESS", ""), os.getenv("GMAIL_APP_PASSWORD", ""))

    def _send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.address
        message["To"] = self.address
        message["Subject"] = subject
        message.set_content(body)

        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(self.address, self.app_password)
            smtp.send_message(message)

    def send_probability_alert(self, forecast: Forecast, threshold: int, checked_at: datetime) -> None:
        subject = f"【Codex Reset 提醒】未来24小时重置概率已升至 {forecast.probability_24h}%"
        body = "\n".join(
            [
                "Codex Reset 概率已越过提醒阈值。",
                "",
                f"当前未来24小时概率：{forecast.probability_24h}%",
                f"触发阈值：严格大于 {threshold}%",
                f"检查时间：{format_beijing(checked_at)}",
                f"网站数据更新时间：{format_beijing(forecast.updated_at)}",
                f"最近一次全局重置时间：{format_beijing(forecast.last_reset_at)}",
                f"查看网站：{SITE_URL}",
            ]
        )
        self._send(subject, body)

    def send_reset_alert(self, forecast: Forecast, checked_at: datetime) -> None:
        subject = "[Codex Reset \u63d0\u9192] \u68c0\u6d4b\u5230\u5168\u5c40\u989d\u5ea6\u521a\u521a\u91cd\u7f6e"
        body = "\n".join(
            [
                "Codex Reset \u7684\u201c\u8ddd\u4e0a\u6b21\u5168\u5c40\u91cd\u7f6e\u65f6\u95f4\u201d\u5df2\u56de\u9000\uff0c\u8bf4\u660e\u68c0\u6d4b\u5230\u65b0\u7684\u5168\u5c40\u91cd\u7f6e\u3002",
                "",
                f"\u672c\u6b21\u5168\u5c40\u91cd\u7f6e\u65f6\u95f4\uff1a{format_beijing(forecast.last_reset_at)}",
                f"\u68c0\u6d4b\u65f6\u95f4\uff1a{format_beijing(checked_at)}",
                f"\u67e5\u770b\u7f51\u7ad9\uff1a{SITE_URL}",
            ]
        )
        self._send(subject, body)

    def send_failure_alert(self, error: Exception, failures: int, checked_at: datetime) -> None:
        subject = "【Codex Reset 监控故障】连续3次检查失败"
        body = "\n".join(
            [
                "Codex Reset 监控已连续 3 次或以上检查失败。",
                "",
                f"连续失败次数：{failures}",
                f"检查时间：{format_beijing(checked_at)}",
                f"最近错误：{error}",
                f"数据接口：{FORECAST_URL}",
            ]
        )
        self._send(subject, body)

    def send_recovery_alert(self, previous_failures: int, checked_at: datetime) -> None:
        subject = "【Codex Reset 监控恢复】接口已恢复正常"
        body = "\n".join(
            [
                "Codex Reset 监控接口已经恢复。",
                "",
                f"此前连续失败次数：{previous_failures}",
                f"恢复时间：{format_beijing(checked_at)}",
                f"数据接口：{FORECAST_URL}",
            ]
        )
        self._send(subject, body)

    def send_test_email(self, checked_at: datetime) -> None:
        subject = "【Codex Reset 测试】邮件通知配置成功"
        body = "\n".join(
            [
                "这是一封手动测试邮件。",
                "",
                "Gmail SMTP 与 GitHub Actions Secrets 配置有效。",
                f"测试时间：{format_beijing(checked_at)}",
                "本次测试不会修改正式监控状态。",
            ]
        )
        self._send(subject, body)


class GitHubStateStore:
    def __init__(self, repository: str, token: str, branch: str = STATE_BRANCH) -> None:
        self.repository = repository.strip()
        self.token = token.strip()
        self.branch = branch
        self.file_sha: str | None = None
        if not self.repository or "/" not in self.repository or not self.token:
            raise StateStoreError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
        self.api_base = f"https://api.github.com/repos/{self.repository}"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
        }

    @classmethod
    def from_env(cls) -> "GitHubStateStore":
        return cls(
            os.getenv("GITHUB_REPOSITORY", ""),
            os.getenv("GITHUB_TOKEN", ""),
            os.getenv("STATE_BRANCH", STATE_BRANCH),
        )

    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        try:
            return _json_request(
                f"{self.api_base}{path}",
                method=method,
                payload=payload,
                headers=self.headers,
                allow_404=allow_404,
            )
        except MonitorError as exc:
            raise StateStoreError(str(exc)) from exc

    def load(self) -> MonitorState:
        branch = urllib.parse.quote(self.branch, safe="")
        path = urllib.parse.quote(STATE_PATH, safe="/")
        data = self._api(f"/contents/{path}?ref={branch}", allow_404=True)
        if data is None:
            self.file_sha = None
            return MonitorState()
        try:
            encoded = str(data["content"]).replace("\n", "")
            raw = json.loads(base64.b64decode(encoded).decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state is not an object")
            self.file_sha = str(data["sha"])
            return MonitorState.from_dict(raw)
        except (KeyError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateStoreError("Invalid monitor state file") from exc

    def _ensure_branch(self) -> None:
        branch_ref = urllib.parse.quote(f"heads/{self.branch}", safe="/")
        if self._api(f"/git/ref/{branch_ref}", allow_404=True) is not None:
            return
        repo = self._api("")
        if repo is None or not isinstance(repo.get("default_branch"), str):
            raise StateStoreError("Could not determine the default branch")
        default_ref = urllib.parse.quote(f"heads/{repo['default_branch']}", safe="/")
        ref_data = self._api(f"/git/ref/{default_ref}")
        try:
            base_sha = str(ref_data["object"]["sha"])  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise StateStoreError("Could not determine the default branch SHA") from exc
        try:
            self._api(
                "/git/refs",
                method="POST",
                payload={"ref": f"refs/heads/{self.branch}", "sha": base_sha},
            )
        except StateStoreError as exc:
            # A concurrent run may have created the branch after our first check.
            if self._api(f"/git/ref/{branch_ref}", allow_404=True) is None:
                raise exc

    def save(self, state: MonitorState, message: str) -> None:
        self._ensure_branch()
        encoded = base64.b64encode(
            (json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).decode("ascii")
        payload: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": self.branch,
        }
        if self.file_sha:
            payload["sha"] = self.file_sha
        path = urllib.parse.quote(STATE_PATH, safe="/")
        result = self._api(f"/contents/{path}", method="PUT", payload=payload)
        try:
            self.file_sha = str(result["content"]["sha"])  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise StateStoreError("GitHub did not return the saved state SHA") from exc


class Monitor:
    def __init__(
        self,
        store: Any,
        notifier: Any,
        *,
        threshold: int = 80,
        forecast_fetcher: Callable[[], Forecast] = fetch_forecast,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.threshold = threshold
        self.forecast_fetcher = forecast_fetcher
        self.clock = clock

    def _save_if_changed(self, state: MonitorState, original: dict[str, Any], now: datetime) -> None:
        heartbeat = heartbeat_due(state, now)
        if heartbeat:
            state.last_heartbeat_at = iso_utc(now)
        if state.to_dict() != original:
            message = "[monitor] heartbeat" if heartbeat and state.to_dict() == {**original, "last_heartbeat_at": state.last_heartbeat_at} else "[monitor] update state"
            self.store.save(state, message)

    def run_once(self) -> int:
        state = self.store.load()
        original = state.to_dict()
        now = self.clock()

        try:
            forecast = self.forecast_fetcher()
        except Exception as exc:  # Error is persisted so failures can be counted across runs.
            state.consecutive_failures += 1
            email_error: Exception | None = None
            if state.consecutive_failures >= 3 and not state.failure_alert_sent:
                try:
                    self.notifier.send_failure_alert(exc, state.consecutive_failures, now)
                    state.failure_alert_sent = True
                except Exception as mail_exc:
                    email_error = mail_exc
            self._save_if_changed(state, original, now)
            print(f"Forecast check failed ({state.consecutive_failures} consecutive): {exc}", file=sys.stderr)
            if email_error:
                print(f"Failure email could not be sent: {email_error}", file=sys.stderr)
            return 1

        previous_failures = state.consecutive_failures
        state.consecutive_failures = 0
        if state.failure_alert_sent:
            try:
                self.notifier.send_recovery_alert(previous_failures, now)
                state.failure_alert_sent = False
            except Exception as exc:
                self._save_if_changed(state, original, now)
                print(f"Recovery email could not be sent: {exc}", file=sys.stderr)
                return 1
        if reset_is_newer(state.last_observed_reset_at, forecast.last_reset_at):
            try:
                self.notifier.send_reset_alert(forecast, now)
            except Exception as exc:
                self._save_if_changed(state, original, now)
                print(f"Reset alert email could not be sent: {exc}", file=sys.stderr)
                return 1

        if not state.last_observed_reset_at or reset_is_newer(
            state.last_observed_reset_at, forecast.last_reset_at
        ):
            state.last_observed_reset_at = forecast.last_reset_at


        is_above = forecast.probability_24h > self.threshold
        should_alert = is_above and (not state.initialized or not state.above_threshold)
        if should_alert:
            try:
                self.notifier.send_probability_alert(forecast, self.threshold, now)
            except Exception as exc:
                self._save_if_changed(state, original, now)
                print(f"Probability alert could not be sent: {exc}", file=sys.stderr)
                return 1

        state.initialized = True
        state.above_threshold = is_above
        self._save_if_changed(state, original, now)
        print(
            f"Checked {format_beijing(now)}: rounded_24h={forecast.probability_24h}% "
            f"threshold=>{self.threshold}% alerted={should_alert}"
        )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Codex Reset forecast probability")
    parser.add_argument("--send-test", action="store_true", help="Send a test email without changing monitor state")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    notifier = GmailNotifier.from_env()
    if args.send_test:
        notifier.send_test_email(utc_now())
        print("Test email sent; monitor state was not changed.")
        return 0

    threshold = int(os.getenv("ALERT_THRESHOLD", "80"))
    if not 0 <= threshold < 100:
        raise MonitorError("ALERT_THRESHOLD must be between 0 and 99")
    store = GitHubStateStore.from_env()
    return Monitor(store, notifier, threshold=threshold).run_once()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MonitorError, smtplib.SMTPException, OSError, ValueError) as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
