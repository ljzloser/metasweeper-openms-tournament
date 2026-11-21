from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from openms_tournament.client import (
    OpenmsClient, OpenmsError, Participant, Tournament, parse_time,
)

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, **kwargs):
        request = requests.Request(
            "GET", url, params=kwargs["params"], headers=kwargs["headers"],
        ).prepare()
        self.requests.append((request, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        response = requests.Response()
        response.status_code = result if isinstance(result, int) else 200
        response.url = request.url
        response.request = request
        response._content = result if isinstance(result, bytes) else json.dumps(result).encode()
        response._content_consumed = True
        return response


def detail(identifier=30, kind="w", week=47):
    return {"id": identifier, "subclass": kind, "state": "n",
            "start_time": "2026-11-16T00:00:00Z" if week == 47 else "2026-11-23T00:00:00Z",
            "end_time": "2026-11-23T00:00:00Z" if week == 47 else "2026-11-30T00:00:00Z",
            "data": {"year": 2026, "week": week}}


def row(user=7, token="race-7", tournament=30, **extra):
    return {"user_id": user, "tournament_id": tournament, "token": token,
            "start_time": "2026-11-20T00:00:00Z", "end_time": None, **extra}


def test_snapshot_uses_one_normal_batch_and_configured_user_id():
    http = FakeHttp(
        [detail(), detail(31, "e", 48), {**detail(29), 'end_time': NOW.isoformat()}], [row()],
    )
    snapshot = OpenmsClient(request_get=http).snapshot(7, now=NOW)
    assert snapshot.user_id == 7
    assert [(item.id, item.phase) for item in snapshot.tournaments] == [(30, "ongoing"), (31, "preparing")]
    assert snapshot.tournaments[0].title == "2026 年第 47 周周赛"
    assert snapshot.tournaments[1].title == "2026 年第 48 周效率周赛"
    assert snapshot.tournaments[0].starts_at == datetime(2026, 11, 16, tzinfo=timezone.utc)
    assert parse_qs(urlsplit(http.requests[0][0].url).query) == {"category": ["normal"]}
    assert snapshot.participant_for(30).token == "race-7"
    assert snapshot.participant_for(31) is None
    assert len(http.requests) == 2
    assert not any('get_news' in request.url or '/get?' in request.url for request, _ in http.requests)
    for request, kwargs in http.requests:
        assert request.method == "GET"
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        assert kwargs["timeout"] == 8


def test_no_user_id_still_shows_tournaments():
    http = FakeHttp([detail()])
    snapshot = OpenmsClient(request_get=http).snapshot(now=NOW)
    assert len(snapshot.tournaments) == 1 and snapshot.user_id is None
    assert len(http.requests) == 1


def test_empty_batch_preserves_configured_user_id():
    http = FakeHttp([])
    snapshot = OpenmsClient(request_get=http).snapshot(7, now=NOW)
    assert not snapshot.tournaments and snapshot.user_id == 7


def test_batch_already_has_details_without_individual_detail_requests():
    http = FakeHttp([detail()])
    snapshot = OpenmsClient(request_get=http).snapshot(now=NOW)
    assert snapshot.tournaments[0].id == 30
    assert len(http.requests) == 1
    assert http.requests[0][0].url.endswith('/api/tournament/get_list?category=normal')


@pytest.mark.parametrize('field', ['state', 'subclass', 'start_time', 'end_time', 'data'])
def test_partial_batch_response_is_an_error_instead_of_an_empty_cleanup_scope(field):
    item = detail()
    del item[field]
    with pytest.raises(OpenmsError, match='缺少详情'):
        OpenmsClient(request_get=FakeHttp([item])).snapshot(now=NOW)


@pytest.mark.parametrize("user_id", [-1, True, False, "7", None, 1.5])
def test_invalid_user_ids_are_rejected_before_network_access(user_id):
    http = FakeHttp()
    with pytest.raises(OpenmsError, match="用户 ID"):
        OpenmsClient(request_get=http).snapshot(user_id)
    assert not http.requests


def test_participant_matches_user_not_position_in_list():
    http = FakeHttp([row(user=8, token="other"), row()])
    participant = OpenmsClient(request_get=http).participant(30, 7)
    assert participant.user_id == 7 and participant.token == "race-7"
    assert participant.is_active(NOW)


def personal_video(**changes):
    result = dict(id=123, player_id=7, level='i', mode='00', state='c', timems=20000, bv=30, cl=40)
    result.update(changes)
    return result


def test_personal_videos_queries_selected_tournament_for_exact_user():
    http = FakeHttp([personal_video()])
    result = OpenmsClient(request_get=http).personal_videos(30, 7)
    assert result[0].id == 123 and result[0].clicks == 40
    assert http.requests[0][0].path_url == '/api/tournament/get_videos/participant?tournament_id=30&user_id=7'


@pytest.mark.parametrize('rows', [
    [personal_video(player_id=8)], [personal_video(id=True)],
    [personal_video(timems=-1)], [personal_video(cl=True)], [personal_video(bv=3.5)],
    [personal_video(mode=0)], [personal_video(), personal_video()], {}, [None],
])
def test_personal_video_query_rejects_other_accounts_or_invalid_statistics(rows):
    with pytest.raises(OpenmsError):
        OpenmsClient(request_get=FakeHttp(rows)).personal_videos(30, 7)


def test_personal_video_missing_clicks_does_not_block_time_scoring():
    result = OpenmsClient(request_get=FakeHttp([personal_video(cl=None)])).personal_videos(30, 7)
    assert result[0].clicks is None and result[0].seconds == 20


def test_not_registered_does_not_return_another_users_token():
    assert OpenmsClient(request_get=FakeHttp([row(user=8)])).participant(30, 7) is None


def test_active_participants_returns_only_tokens_for_current_personal_windows():
    now = datetime.now(timezone.utc)
    tournaments = tuple(Tournament(identifier, f"race-{identifier}", "ongoing", None, None)
                        for identifier in (30, 31, 32))
    http = FakeHttp(
        [row(tournament=30, start_time=(now - timedelta(minutes=10)).isoformat(),
             end_time=(now + timedelta(minutes=50)).isoformat())],
        [row(tournament=31, token="second-token", start_time=(now - timedelta(minutes=5)).isoformat(),
             end_time=(now + timedelta(minutes=55)).isoformat())],
        [row(tournament=32, end_time=(now - timedelta(seconds=1)).isoformat())],
    )
    participants = OpenmsClient(request_get=http).active_participants(tournaments, 7)
    assert [(item.tournament_id, item.token) for item in participants] == [(30, "race-7"), (31, "second-token")]
    assert len(http.requests) == 3


@pytest.mark.parametrize("token", ["first,second", "contains whitespace"])
def test_active_participants_rejects_tokens_that_break_comma_format(token):
    tournament = Tournament(30, "race", "ongoing", None, None)
    with pytest.raises(OpenmsError, match="包含逗号或空白"):
        OpenmsClient(request_get=FakeHttp([row(token=token)])).active_participants((tournament,), 7)


@pytest.mark.parametrize(("subclass", "expected_path", "form"), [
    ("w", "/api/tournament/weekly/participant", {"id": 30}),
    ("e", "/api/tournament/weekly-eff/participant", {"id": 30}),
    ("g", "/api/tournament/gsc/participant", {"order": 12}),
])
def test_check_in_uses_the_correct_openms_form_endpoint(subclass, expected_path, form):
    from test_auth import FakeSession

    session = FakeSession((200, {"type": "success"}), (200, [row()]))
    tournament = Tournament(30, "event", "ongoing", None, None, subclass, 12 if subclass == "g" else None)
    result = OpenmsClient(session=session).check_in(tournament, 7)
    assert result.token == "race-7"
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == "https://openms.top" + expected_path
    assert kwargs["data"] == form


def test_check_in_rejects_preparing_tournaments_before_posting():
    from test_auth import FakeSession

    session = FakeSession()
    tournament = Tournament(30, "event", "preparing", None, None)
    with pytest.raises(OpenmsError, match="进行中"):
        OpenmsClient(session=session).check_in(tournament, 7)
    assert not session.calls


@pytest.mark.parametrize("rows, message", [
    ([row(), row()], "多条"),
    ([row(tournament=31)], "比赛不匹配"),
    ([row(token=" value ")], "标识格式"),
    ([row(token=None)], "标识格式"),
    ([row(token="x" * 81)], "标识格式"),
    ({"items": []}, "响应格式"),
])
def test_malformed_participants_are_rejected(rows, message):
    with pytest.raises(OpenmsError, match=message):
        OpenmsClient(request_get=FakeHttp(rows)).participant(30, 7)


def test_personal_and_tournament_windows_include_start_and_exclude_end():
    before, after = NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)
    assert Participant(7, 30, "token", NOW, after).is_active(NOW)
    assert not Participant(7, 30, "token", before, NOW).is_active(NOW)
    assert not Participant(7, 30, "token", after, None).is_active(NOW)
    assert not Participant(7, 30, "", None, None).is_active(NOW)
    assert Tournament(30, "weekly", "ongoing", NOW, after).is_ongoing(NOW)
    assert not Tournament(30, "weekly", "ongoing", before, NOW).is_ongoing(NOW)
    assert not Tournament(30, "weekly", "preparing", before, after).is_ongoing(NOW)


@pytest.mark.parametrize("failure, message", [
    (429, "频繁"),
    (403, "拒绝"),
    (500, "HTTP 500"),
    (requests.ConnectionError("offline"), "网络"),
    (requests.Timeout(), "网络"),
    (b"<html>bad gateway</html>", "JSON"),
])
def test_request_failures_have_readable_messages(failure, message):
    with pytest.raises(OpenmsError, match=message):
        OpenmsClient(request_get=FakeHttp(failure)).snapshot()


@pytest.mark.parametrize("value", ["invalid", "2026-11-21T00:00:00", 123])
def test_malformed_times_are_not_treated_as_unlimited_windows(value):
    with pytest.raises(OpenmsError, match="时间格式"):
        parse_time(value)


def test_duplicate_batch_ids_are_rejected():
    http = FakeHttp([detail(), detail()])
    with pytest.raises(OpenmsError, match="重复"):
        OpenmsClient(request_get=http).snapshot()


def test_quick_snapshot_and_selection_reuse_queries_but_application_lookup_is_fresh():
    http = FakeHttp([detail()], [row()], [row(token='latest')])
    client = OpenmsClient(request_get=http)
    first = client.snapshot(7, now=NOW)
    second = client.snapshot(7, now=NOW)
    assert first.tournaments == second.tournaments
    assert client.participant(30, 7).token == 'race-7'
    assert len(http.requests) == 2
    assert client.participant(30, 7, fresh=True).token == 'latest'
    assert len(http.requests) == 3


def test_checkin_uses_complete_post_response_without_followup_get():
    from test_auth import FakeSession

    session = FakeSession((200, row()))
    result = OpenmsClient(session=session).check_in(Tournament(30, '周赛', 'ongoing', None, None), 7)
    assert result.token == 'race-7'
    assert len(session.calls) == 1 and session.calls[0][0] == 'POST'


def test_checkin_response_cannot_return_another_accounts_token():
    from test_auth import FakeSession

    with pytest.raises(OpenmsError, match='账号不匹配'):
        OpenmsClient(session=FakeSession((200, row(user=8)))).check_in(
            Tournament(30, '周赛', 'ongoing', None, None), 7)


def test_participant_batch_cache_filters_by_user_and_expires():
    http = FakeHttp([row(), row(user=8, token='other')], [row(token='updated')])
    client = OpenmsClient(request_get=http)
    assert client.participant(30, 7).token == 'race-7'
    assert client.participant(30, 8).token == 'other'
    assert len(http.requests) == 1
    client._policy.wait(3)
    assert client.participant(30, 7).token == 'updated'


def test_personal_videos_are_fetched_once_per_batch_not_once_per_replay():
    rows = [personal_video(id=index) for index in range(1, 201)]
    http = FakeHttp(rows)
    client = OpenmsClient(request_get=http)
    assert len(client.personal_videos(30, 7)) == 200
    assert len(client.personal_videos(30, 7)) == 200
    assert len(http.requests) == 1
