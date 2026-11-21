"""Conservative pacing and server-directed cooldown for one OpenMS session."""
from collections import defaultdict, deque
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time


class OpenmsError(Exception):
    """Request or data error safe to display in the plugin UI."""


class RateLimitError(OpenmsError):
    def __init__(self, retry_after: float, *, local: bool = False):
        self.retry_after = max(1.0, retry_after)
        self.local = local
        super().__init__(f'{"已达到本地请求上限" if local else "OpenMS 请求过于频繁"}，请等待 {math.ceil(self.retry_after)} 秒后重试。')


class RequestPolicy:
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time, wait=time.sleep):
        self.clock = clock
        self.wall_clock = wall_clock
        self.wait = wait
        self._next_request = 0.0
        self._endpoint_next: dict[str, float] = {}
        self._hourly: dict[str, deque] = defaultdict(deque)
        self._cooldown_until = 0.0
        self._rate_limit_streak = 0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self.clock())

    def before_request(self, path: str):
        if self.cooldown_remaining() > 0:
            raise RateLimitError(self.cooldown_remaining())
        now = self.clock()
        if path in ('/userprofile/login/', '/userprofile/refresh_captcha/'):
            history = self._hourly[path]
            while history and history[0] <= now - 3600:
                history.popleft()
            if len(history) >= 60:
                raise RateLimitError(history[0] + 3600 - now, local=True)
        # All network requests share 1/s pacing. Efficiency check-in is at most 30/min.
        ready = max(self._next_request, self._endpoint_next.get(path, 0))
        while self.clock() < ready:
            self.wait(min(1.0, ready - self.clock()))
        now = self.clock()
        self._next_request = now + 1.0
        if path == '/api/tournament/weekly-eff/participant':
            self._endpoint_next[path] = now + 2.1
        if path in ('/userprofile/login/', '/userprofile/refresh_captcha/'):
            self._hourly[path].append(now)

    def rate_limited(self, retry_after: str | None) -> float:
        self._rate_limit_streak += 1
        delay = None
        if retry_after:
            try:
                numeric = float(retry_after)
                if math.isfinite(numeric) and numeric >= 0:
                    delay = numeric
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    delay = max(0.0, date.timestamp() - self.wall_clock())
                except (TypeError, ValueError, OverflowError):
                    pass
        if delay is None:
            delay = min(300.0, 30.0 * 2 ** min(self._rate_limit_streak - 1, 4))
        self._cooldown_until = max(self._cooldown_until, self.clock() + max(1.0, delay))
        return self.cooldown_remaining()

    def succeeded(self):
        self._rate_limit_streak = 0
