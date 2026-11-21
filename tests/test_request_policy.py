from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from openms_tournament.request_policy import RateLimitError, RequestPolicy


def clocked_policy():
    clock = [10000.0]
    waits = []

    def wait(delay):
        waits.append(delay)
        clock[0] += delay

    return RequestPolicy(clock=lambda: clock[0], wall_clock=lambda: clock[0], wait=wait), clock, waits


def test_global_network_pacing_applies_across_endpoints_and_waits_only_in_small_steps():
    policy, clock, waits = clocked_policy()
    stamps = []
    for path in ['/api/common/uploadvideo', '/api/tournament/get_list', '/api/common/loginauto']:
        policy.before_request(path)
        stamps.append(clock[0])
    assert stamps == [10000, 10001, 10002]
    assert all(delay <= 1 for delay in waits)


def test_efficiency_checkin_stays_below_thirty_requests_per_minute():
    policy, clock, waits = clocked_policy()
    stamps = []
    for _ in range(31):
        policy.before_request('/api/tournament/weekly-eff/participant')
        stamps.append(clock[0])
    assert stamps[30] - stamps[0] >= 60
    assert all(delay <= 1 for delay in waits)


@pytest.mark.parametrize('path', ['/userprofile/login/', '/userprofile/refresh_captcha/'])
def test_login_and_captcha_hourly_budgets_do_not_block_video_uploads(path):
    policy, clock, _ = clocked_policy()
    for _ in range(60):
        policy.before_request(path)
    with pytest.raises(RateLimitError) as caught:
        policy.before_request(path)
    assert caught.value.local and caught.value.retry_after > 3500
    policy.before_request('/api/common/uploadvideo')
    clock[0] = 13601
    policy.before_request(path)


@pytest.mark.parametrize('value,expected', [('90', 90), ('1.5', 1.5), ('0', 1), ('bad', 30), ('NaN', 30), ('-1', 30)])
def test_retry_after_seconds_and_invalid_values(value, expected):
    policy, _, _ = clocked_policy()
    assert policy.rate_limited(value) == expected


def test_retry_after_accepts_http_date_without_capping_server_wait_time():
    policy, _, _ = clocked_policy()
    timestamp = format_datetime(datetime.fromtimestamp(10000 + 3600, timezone.utc), usegmt=True)
    assert policy.rate_limited(timestamp) == 3600
    with pytest.raises(RateLimitError) as caught:
        policy.before_request('/api/common/uploadvideo')
    assert caught.value.retry_after == 3600


def test_missing_retry_after_exponentially_backs_off_and_success_resets_streak():
    policy, clock, _ = clocked_policy()
    for expected in (30, 60, 120, 240, 300):
        assert policy.rate_limited(None) == expected
        clock[0] += expected
    policy.succeeded()
    assert policy.rate_limited(None) == 30


def test_cooldown_fail_fast_does_not_sleep_for_server_retry_after_or_send_request():
    policy, _, waits = clocked_policy()
    policy.rate_limited('600')
    for _ in range(3):
        with pytest.raises(RateLimitError):
            policy.before_request('/api/tournament/get_list')
    assert waits == []
