from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import Mock

from loguru import logger
import pytest

from lib_zmq_plugins.shared.base import CommandResponse
from plugin_sdk.control_auth import ControlAuthorizationManager
from shared_types import GameContextResult, SetRaceIdentifierCommand
from shared_types.events import GameFinishedEvent

from openms_tournament.client import Captcha, LoginUser, OpenmsError, Participant, Snapshot, Tournament, UploadError, UploadResult
from openms_tournament.plugin import OpenmsTournamentPlugin, TournamentConfig
from openms_tournament.uploads import UploadQueue
from openms_tournament.analysis import ScoreVideo
from openms_tournament.widgets import ANALYSIS_COLUMN


def snapshot(user_id=7):
    return Snapshot((Tournament(30, "当前周赛", "ongoing", None, None),
                     Tournament(31, "下周周赛", "preparing", None, None)),
                    user_id, datetime.now(timezone.utc))


@pytest.fixture
def factory(qapp, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr("plugin_manager.app_paths.get_plugin_data_dir", lambda *_: tmp_path)
    monkeypatch.setattr("plugin_manager.logging_setup.get_plugin_logger", lambda *_, **__: (logger, None))
    monkeypatch.setattr("plugin_manager.logging_setup.set_plugin_log_level", lambda *_, **__: None)
    monkeypatch.setattr(ControlAuthorizationManager, "_instance", None)
    auth = ControlAuthorizationManager(tmp_path / "auth")
    auth.authorize(SetRaceIdentifierCommand, "openms_tournament")
    plugins = []

    def create():
        plugin = OpenmsTournamentPlugin(OpenmsTournamentPlugin.plugin_info())
        plugins.append(plugin)
        return plugin

    yield create
    for plugin in plugins:
        if plugin._thread.isRunning():
            plugin.shutdown()
            qtbot.waitUntil(lambda: not plugin._thread.isRunning(), timeout=5000)
        else:
            plugin.on_shutdown()
        if plugin._widget is not None:
            plugin.close_widget()
    qapp.processEvents()


def prepare(factory, monkeypatch):
    plugin = factory()
    plugin._stopping = False
    plugin._snapshot = snapshot()
    plugin._selected = plugin._snapshot.tournaments[0]
    plugin._participant = Participant(7, 30, "token", None, None)
    events = []
    monkeypatch.setattr(plugin, "_ui", lambda *args: events.append(args))
    monkeypatch.setattr(plugin, "query", lambda *_args, **_kwargs: GameContextResult(
        request_id="context", success=True, game_status="ready", mode=0,
        is_fair=True, board_constraint=False, race_identifier=""))
    return plugin, events


def prepare_upload(factory, monkeypatch, tmp_path):
    plugin, events = prepare(factory, monkeypatch)
    plugin.other_info.auto_upload = True
    plugin._login_user = LoginUser(7, "tester")
    plugin._snapshot = Snapshot(plugin._snapshot.tournaments, 7, plugin._snapshot.fetched_at,
                                ((30, Participant(7, 30, "TOKEN", None, None)),))
    plugin._uploads = UploadQueue(tmp_path / "uploads.sqlite3")
    plugin._executor = Mock()
    monkeypatch.setattr(plugin, "_submit", Mock(return_value=True))
    return plugin, events


def winning_event(**changes):
    values = dict(game_state=6, mode=0, is_fair=True, is_official=True,
                  raw_data=b"original-replay", race_identifier="TOKEN")
    values.update(changes)
    return GameFinishedEvent(**values)


@pytest.mark.parametrize("changes", [
    {"game_state": 5}, {"game_state": 0}, {"game_state": 8}, {"mode": 5},
    {"is_fair": False}, {"is_official": False}, {"raw_data": b""}, {"race_identifier": "WRONG"},
])
def test_auto_upload_only_queues_matching_official_fair_standard_wins(factory, monkeypatch, tmp_path, changes):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._on_game_finished(winning_event(**changes))
    assert plugin._uploads.records() == []
    plugin._submit.assert_not_called()


def test_duplicate_response_discards_queued_bytes_without_retry(factory, monkeypatch, tmp_path):
    plugin, events = prepare_upload(factory, monkeypatch, tmp_path)
    saved_game = tmp_path / "saved-game.evf"
    saved_game.write_bytes(b"original-replay")
    plugin._on_game_finished(winning_event())
    assert plugin._upload_current.raw_data == saved_game.read_bytes()
    assert plugin._submit.call_args.args[0] == "upload"
    plugin._on_finished(plugin._epoch, "upload", UploadResult("duplicate"), None)
    plugin._upload_after = 0
    plugin._process_upload_queue()
    plugin._on_game_finished(winning_event())
    plugin._retry_uploads()
    assert plugin._uploads.records()[0]["status"] == "duplicate"
    assert plugin._uploads.records()[0]["video_id"] is None
    assert plugin._uploads.next_job(7) is None
    assert saved_game.read_bytes() == b"original-replay"
    assert plugin._submit.call_count == 1
    assert any("不再重试" in str(item) for item in events)


def test_success_keeps_website_state_instead_of_claiming_tournament_credit(factory, monkeypatch, tmp_path):
    plugin, events = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._on_game_finished(winning_event())
    plugin._on_finished(plugin._epoch, "upload", UploadResult("succeeded", 123, "d"), None)
    record = plugin._uploads.records()[0]
    assert record["status"] == "succeeded" and record["video_id"] == 123 and record["website_state"] == "d"
    assert "玩家标识不匹配" in record["message"] and "以网站为准" in record["message"]


def test_lost_response_then_collision_stops_retrying(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._on_game_finished(winning_event())
    plugin._on_finished(plugin._epoch, "upload", None, UploadError("timeout", retryable=True))
    assert plugin._uploads.records()[0]["status"] == "retry"
    job = plugin._uploads.next_job(7, datetime.now(timezone.utc).timestamp() + 11)
    plugin._upload_current = job
    plugin._uploads.start(job)
    plugin._on_finished(plugin._epoch, "upload", UploadResult("duplicate"), None)
    assert plugin._uploads.next_job(7) is None
    assert plugin._uploads.records()[0]["status"] == "duplicate"


def test_login_expiration_pauses_queue_until_account_is_verified_again(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._on_game_finished(winning_event())
    plugin._on_finished(plugin._epoch, "upload", None, UploadError("login expired", needs_login=True))
    assert plugin._login_user is None
    assert plugin._uploads.next_job(7).raw_data == b"original-replay"
    plugin._upload_after = 0
    plugin._process_upload_queue()
    assert plugin._submit.call_count == 1


def test_rate_limited_upload_pauses_the_entire_queue_not_just_one_replay(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._on_game_finished(winning_event())
    plugin._on_finished(plugin._epoch, 'upload', None,
                        UploadError('wait', retryable=True, retry_after=600, rate_limited=True))
    plugin._uploads.enqueue(7, b'another', ('TOKEN',), None, (30,))
    plugin._process_upload_queue()
    assert plugin._submit.call_count == 1
    assert len(plugin._uploads.records()) == 2


def test_checkin_many_stops_on_rate_limit_instead_of_posting_remaining_matches():
    from openms_tournament.request_policy import RateLimitError

    client = Mock()
    matches = tuple(Tournament(index, f'Match {index}', 'ongoing', None, None) for index in (30, 31, 32))
    first = Participant(7, 30, 'FIRST', None, None)
    client.check_in.side_effect = [first, RateLimitError(120)]
    succeeded, failed = OpenmsTournamentPlugin._check_in_many(client, matches, 7)
    assert succeeded == ((matches[0], first),)
    assert [match.id for match, _ in failed] == [31, 32]
    assert client.check_in.call_count == 2
    client.participant.assert_not_called()


def test_existing_requests_do_not_drop_finished_replays_or_race_login(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._auth_busy = True
    plugin._on_game_finished(winning_event())
    assert plugin._uploads.next_job(7) is not None
    plugin._submit.assert_not_called()
    plugin._auth_busy = False
    plugin._process_upload_queue()
    assert plugin._submit.call_count == 1


def test_account_switch_does_not_upload_previous_users_queue(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._uploads.enqueue(8, b"another-user", ("TOKEN",), None)
    plugin._process_upload_queue()
    plugin._submit.assert_not_called()
    assert plugin._uploads.next_job(8) is not None


def test_multiple_tournament_tokens_upload_one_replay_before_latest_valid_deadline(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    second = Tournament(32, "第二场", "ongoing", None, now + timedelta(hours=2))
    plugin._snapshot = Snapshot((*plugin._snapshot.tournaments, second), 7, now, (
        (30, Participant(7, 30, "TOKEN", None, now + timedelta(hours=1))),
        (32, Participant(7, 32, "SECOND", None, now + timedelta(hours=2))),
    ))
    plugin._on_game_finished(winning_event(race_identifier="TOKEN,SECOND,TOKEN"))
    assert plugin._upload_current.tokens == ("TOKEN", "SECOND")
    assert plugin._upload_current.deadline == second.ends_at.timestamp()
    assert len(plugin._uploads.records()) == 1


def test_real_background_upload_completes_via_queued_signal(factory, monkeypatch, tmp_path, qtbot):
    plugin = factory()
    plugin.other_info.user_id = 7
    plugin.other_info.auto_upload = True
    plugin.other_info.auto_manage_identifier = False
    calls = []

    def upload(data, filename):
        calls.append((data, filename, get_ident()))
        return UploadResult("duplicate")

    current = snapshot(7)
    current = Snapshot(current.tournaments, 7, current.fetched_at, ((30, Participant(7, 30, "TOKEN", None, None)),))
    client = Mock(upload_video=upload)
    client.load_cookies.return_value = True
    client.current_user.return_value = LoginUser(7, "tester")
    client.snapshot.return_value = current
    monkeypatch.setattr(plugin, "_client_for_request", lambda: client)
    monkeypatch.setattr(plugin, "query", lambda *_args, **_kwargs: GameContextResult(
        request_id="context", success=True, game_status="ready", mode=0, is_fair=True))
    plugin.initialize()
    qtbot.waitUntil(lambda: plugin.is_ready and plugin._snapshot is not None)
    plugin._enqueue_event(lambda _: plugin._on_game_finished(winning_event()), None)
    qtbot.waitUntil(lambda: bool(plugin._uploads.records()) and plugin._uploads.records()[0]["status"] == "duplicate")
    assert calls[0][0] == b"original-replay" and calls[0][2] != get_ident()
    assert plugin._future is None


def test_successful_empty_snapshot_deletes_all_old_upload_records(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._uploads.enqueue(7, b'old', ('OLD',), None, (29,))
    monkeypatch.setattr(plugin, '_refresh_game_context', Mock())
    plugin._on_finished(plugin._epoch, 'snapshot', Snapshot((), None, datetime.now(timezone.utc)), None)
    assert plugin._uploads.records() == []


def test_personal_analysis_always_uses_logged_in_account_not_manual_user_id(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = LoginUser(7, 'tester')
    plugin.other_info.user_id = 99
    client = Mock()
    client.personal_videos.return_value = (ScoreVideo(123, 'i', '00', 'c', 20000, 30, 30),)
    monkeypatch.setattr(plugin, '_client_for_request', lambda: client)
    monkeypatch.setattr(plugin, '_submit', lambda kind, work: plugin._on_finished(plugin._epoch, kind, work(), None) or True)
    plugin._analyze_personal(30)
    client.personal_videos.assert_called_once_with(30, 7)
    result = next(args[1] for args in events if args[0] == 'show_analysis')
    assert result.user_id == 7 and result.total == 740


@pytest.mark.parametrize('condition', ['logged_out', 'busy', 'unknown_tournament'])
def test_personal_analysis_preconditions_do_not_send_network_request(factory, monkeypatch, condition):
    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = None if condition == 'logged_out' else LoginUser(7, 'tester')
    plugin._auth_busy = condition == 'busy'
    submit = Mock()
    monkeypatch.setattr(plugin, '_submit', submit)
    plugin._analyze_personal(999 if condition == 'unknown_tournament' else 30)
    submit.assert_not_called()
    assert events[-1][0] == 'show_analysis_error'


def test_personal_analysis_error_does_not_erase_participation_or_claim_empty_score(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = LoginUser(7, 'tester')
    plugin._analysis_target = (30, 7)
    participant = plugin._participant
    plugin._on_finished(plugin._epoch, 'analysis', None, OpenmsError('network down'))
    assert plugin._participant is participant
    assert events[-1] == ('show_analysis_error', 'network down')
    assert not any(args[0] == 'show_analysis' for args in events)


def test_personal_analysis_data_is_cleared_when_login_or_tournament_scope_changes(factory, monkeypatch):
    from openms_tournament.analysis import analyze_personal

    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = LoginUser(8, 'other')
    plugin._analysis_target = (30, 7)
    result = analyze_personal(plugin._snapshot.tournaments[0], 7, ())
    plugin._on_finished(plugin._epoch, 'analysis', result, None)
    assert events[-1] == ('clear_analysis',)


def test_personal_analysis_query_and_computation_run_off_ui_thread(factory, monkeypatch, qtbot):
    plugin = factory()
    plugin.other_info.user_id = 7
    plugin.other_info.auto_manage_identifier = False
    calls = []

    def personal_videos(tournament_id, user_id):
        calls.append((tournament_id, user_id, get_ident()))
        return (ScoreVideo(123, 'i', '00', 'c', 20000, 30, 30),)

    client = Mock(personal_videos=personal_videos)
    client.load_cookies.return_value = True
    client.current_user.return_value = LoginUser(7, 'tester')
    client.snapshot.return_value = snapshot(7)
    client.participant.return_value = Participant(7, 30, 'TOKEN', None, None)
    monkeypatch.setattr(plugin, '_client_for_request', lambda: client)
    monkeypatch.setattr(plugin, 'query', lambda *_args, **_kwargs: GameContextResult(
        request_id='context', success=True, game_status='ready', mode=0, is_fair=True))
    plugin.initialize()
    qtbot.waitUntil(lambda: plugin.is_ready and plugin._snapshot is not None)
    widget = plugin.widget
    qtbot.waitUntil(lambda: widget.table.rowCount() == 2)
    widget.table.selectRow(0)
    button = widget.table.cellWidget(0, ANALYSIS_COLUMN)
    qtbot.waitUntil(lambda: plugin._participant is not None and plugin._future is None and button.isEnabled())
    button.click()
    qtbot.waitUntil(lambda: '740.000' in widget.analysis_dialog.total_label.text())
    assert calls == [(30, 7, calls[0][2])]
    assert calls[0][2] != get_ident()
    assert widget.analysis_dialog.video_table.rowCount() == 1


def test_failed_snapshot_never_deletes_uploads_based_on_missing_tournament_list(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._uploads.enqueue(7, b'old', ('OLD',), None, (29,))
    plugin._on_finished(plugin._epoch, 'snapshot', None, OpenmsError('network down'))
    assert len(plugin._uploads.records()) == 1


def test_disabled_uploads_and_logged_out_user_still_purge_deadline_records(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._uploads.enqueue(8, b'old', ('OLD',), 1, (29,))
    plugin.other_info.auto_upload = False
    plugin._login_user = None
    plugin._snapshot = None
    plugin._process_upload_queue()
    assert plugin._uploads.records() == []
    plugin._submit.assert_not_called()


def test_remaining_upload_records_keep_tournament_refresh_enabled_when_both_options_are_off(factory, monkeypatch, tmp_path):
    plugin, _ = prepare_upload(factory, monkeypatch, tmp_path)
    plugin._uploads.enqueue(7, b'current', ('TOKEN',), None, (30,))
    plugin.other_info.auto_upload = plugin.other_info.auto_manage_identifier = False
    refresh = Mock()
    monkeypatch.setattr(plugin, '_refresh', refresh)
    monkeypatch.setattr(plugin, '_refresh_game_context', Mock())
    plugin._on_poll_timeout()
    refresh.assert_called_once()


def test_config_is_saved_and_reloaded(factory, qapp):
    plugin = factory()
    plugin._save_user_id(7)
    qapp.processEvents()
    assert plugin.other_info.user_id == 7
    saved = json.loads(plugin.config_file_path.read_text(encoding="utf-8"))
    assert saved["user_id"] == 7
    assert factory().other_info.user_id == 7


def test_apply_rechecks_participant_then_sends_only_race_identifier(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    latest = Participant(7, 30, "new-token", None, None)
    client = Mock()
    client.active_participants.return_value = (latest,)
    monkeypatch.setattr(plugin, "_client_for_request", lambda: client)
    monkeypatch.setattr(plugin, "_submit", lambda kind, work: plugin._on_finished(plugin._epoch, kind, work(), None))
    request = Mock(return_value=CommandResponse("r", True))
    monkeypatch.setattr(plugin, "request", request)
    plugin._apply()
    client.active_participants.assert_called_once_with(plugin._snapshot.tournaments, 7)
    command = request.call_args.args[0]
    assert isinstance(command, SetRaceIdentifierCommand) and command.race_identifier == "new-token"
    assert "1 场当前参赛比赛" in events[-1][-1]


def test_apply_joins_all_active_tokens_with_plain_commas(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    selected = plugin._selected
    plugin._snapshot = Snapshot(
        (selected, Tournament(32, "第二场比赛", "ongoing", None, None)), 7,
        plugin._snapshot.fetched_at,
    )
    plugin._selected = selected
    plugin._participant = Participant(7, selected.id, "old-token", None, None)
    first = Participant(7, selected.id, "current-one", None, None)
    second = Participant(7, 32, "current-two", None, None)
    client = Mock()
    client.active_participants.return_value = (first, second)
    monkeypatch.setattr(plugin, "_client_for_request", lambda: client)
    monkeypatch.setattr(plugin, "_submit", lambda kind, work: plugin._on_finished(plugin._epoch, kind, work(), None))
    request = Mock(return_value=CommandResponse("r", True))
    monkeypatch.setattr(plugin, "request", request)

    plugin._apply()

    command = request.call_args.args[0]
    assert command.race_identifier == "current-one,current-two"
    assert "2 场当前参赛比赛" in events[-1][-1]


@pytest.mark.parametrize("condition", ["no_auth", "no_match", "expired"])
def test_apply_preconditions_prevent_network_and_commands(factory, monkeypatch, condition):
    plugin, events = prepare(factory, monkeypatch)
    if condition == "no_auth":
        monkeypatch.setattr(plugin, "has_control_auth", lambda _: False)
    elif condition == "no_match":
        plugin._participant = None
    else:
        plugin._participant = Participant(7, 30, "old", None, datetime(2000, 1, 1, tzinfo=timezone.utc))
    submit, request = Mock(), Mock()
    monkeypatch.setattr(plugin, "_submit", submit)
    monkeypatch.setattr(plugin, "request", request)
    plugin._apply()
    submit.assert_not_called()
    request.assert_not_called()
    assert events[-1][0] == "show_error"


def test_late_network_result_cannot_restore_previous_selection(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._epoch = 5
    plugin._on_finished(4, "participant", Participant(7, 99, "stale", None, None), None)
    assert not events and plugin._participant.token == "token"
    plugin._select_tournament(31)
    assert plugin._epoch == 6 and plugin._participant is None
    assert "尚未开始" in events[-1][-1]
    plugin._on_finished(5, "participant", Participant(7, 30, "stale", None, None), None)
    assert plugin._participant is None


@pytest.mark.parametrize("response, message", [
    (None, "未收到响应"),
    (CommandResponse("r", False, error="请在 ready 时设置"), "ready"),
])
def test_command_failure_is_shown_without_claiming_success(factory, monkeypatch, response, message):
    plugin, events = prepare(factory, monkeypatch)
    monkeypatch.setattr(plugin, "request", Mock(return_value=response))
    plugin._on_finished(plugin._epoch, "apply", (plugin._participant,), None)
    assert events[-1][:2] == ("set_busy", False) and message in events[-1][-1]


def test_auth_revoked_during_http_lookup_does_not_send_command(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    monkeypatch.setattr(plugin, "has_control_auth", lambda _: False)
    request = Mock()
    monkeypatch.setattr(plugin, "request", request)
    plugin._on_finished(plugin._epoch, "apply", (plugin._participant,), None)
    request.assert_not_called()
    assert "撤销" in events[-1][-1]


def test_http_error_invalidates_previously_loaded_token(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._on_finished(plugin._epoch, "participant", None, OpenmsError("请稍后刷新"))
    assert plugin._participant is None and events[-1] == ("show_error", "请稍后刷新")


def test_plugin_lifecycle_and_real_queued_ui_flow(factory, monkeypatch, qtbot):
    plugin = factory()
    main_thread = get_ident()
    network_threads = []
    commands = []

    class FakeClient:
        def load_cookies(self, _path):
            return False

        def snapshot(self, user_id):
            network_threads.append(get_ident())
            return snapshot(user_id)

        def participant(self, tournament_id, user_id):
            network_threads.append(get_ident())
            return Participant(user_id, tournament_id, "live-token", None, None)

        def active_participants(self, tournaments, user_id):
            network_threads.append(get_ident())
            return (Participant(user_id, 30, "live-token", None, None),)

    def request(command):
        commands.append((command, get_ident()))
        return CommandResponse("done", True)

    monkeypatch.setattr(plugin, "_client_for_request", FakeClient)
    monkeypatch.setattr(plugin, "query", lambda *_args, **_kwargs: GameContextResult(
        request_id="context", success=True, game_status="ready", mode=0,
        is_fair=True, board_constraint=False, race_identifier=""))
    monkeypatch.setattr(plugin, "request", request)
    plugin.other_info.user_id = 7
    plugin.initialize()
    qtbot.waitUntil(lambda: plugin.is_ready and plugin._snapshot is not None)
    widget = plugin.widget
    qtbot.waitUntil(lambda: widget.table.rowCount() == 2)
    widget.table.selectRow(0)
    qtbot.waitUntil(lambda: widget.table.item(0, 2).text() == "live-token")
    assert widget.apply_button.isEnabled()
    widget.apply_button.click()
    qtbot.waitUntil(lambda: bool(commands))
    qtbot.waitUntil(lambda: "1 场当前参赛比赛" in widget.status_label.text())
    assert commands[0][0].race_identifier == "live-token"
    assert widget.current_identifier_edit.text() == "live-token"
    assert commands[0][1] != main_thread
    assert all(thread != main_thread for thread in network_threads)
    plugin.close_widget()
    reopened = plugin.ensure_widget()
    assert reopened is not widget
    qtbot.waitUntil(lambda: reopened.table.rowCount() == 2)


def test_shutdown_does_not_wait_for_network_or_accept_late_results(factory, monkeypatch, qtbot):
    plugin = factory()
    started, release = Event(), Event()

    class SlowClient:
        def load_cookies(self, _path):
            return False

        def snapshot(self, _user_id):
            started.set()
            release.wait(timeout=5)
            return snapshot()

    monkeypatch.setattr(plugin, "_client_for_request", SlowClient)
    try:
        plugin.initialize()
        qtbot.waitUntil(started.is_set)
        plugin.shutdown()
        qtbot.waitUntil(lambda: not plugin._thread.isRunning(), timeout=2000)
        assert plugin._stopping and plugin._snapshot is None
    finally:
        release.set()


def test_host_loader_finds_exactly_one_plugin(factory, monkeypatch):
    from plugin_manager.plugin_loader import PluginLoader

    root = Path(__file__).resolve().parents[1]
    loader = PluginLoader([root])
    entries = [(path, name) for path, name in loader.discover_plugins() if name == "openms_tournament"]
    assert len(entries) == 1
    module = loader.load_module(*entries[0])
    assert loader.get_plugin_classes(module) == [OpenmsTournamentPlugin]


def test_plugin_reuses_session_and_never_persists_credentials(factory):
    plugin = factory()
    assert plugin._client_for_request() is plugin._client_for_request()
    fields = plugin.other_info.get_fields()
    assert set(fields) == {
        "user_id", "timeout_seconds", "username", "login_expiry_days",
        "auto_manage_identifier", "auto_upload",
    }
    assert fields["login_expiry_days"].default == 30
    assert fields["login_expiry_days"].min_value == 7
    assert fields["login_expiry_days"].max_value == 90
    assert fields["auto_manage_identifier"].default is True
    assert fields["auto_upload"].default is False
    assert TournamentConfig().username == ""


def test_expiry_days_from_old_settings_are_clamped_to_new_minimum(factory, monkeypatch):
    plugin = factory()
    object.__getattribute__(plugin.other_info, "_values")["login_expiry_days"] = 6
    monkeypatch.setattr(plugin, "_submit_auth", Mock())
    plugin.on_initialized()
    assert plugin.other_info.login_expiry_days == 7
    plugin.on_shutdown()


def test_login_result_sets_user_id_and_saves_only_public_configuration(factory, monkeypatch, qapp):
    plugin, events = prepare(factory, monkeypatch)
    monkeypatch.setattr(plugin, "_refresh", Mock())
    plugin._auth_busy = True
    plugin._on_auth_finished(plugin._auth_epoch, "login", LoginUser(8, "tester"), None)
    qapp.processEvents()
    assert plugin._login_user == LoginUser(8, "tester") and not plugin._auth_busy
    assert plugin.other_info.user_id == 8
    stored = json.loads(plugin.config_file_path.read_text(encoding="utf-8"))
    assert stored == {
        "user_id": 8, "timeout_seconds": 8, "username": "tester", "login_expiry_days": 30,
        "auto_manage_identifier": True, "auto_upload": False,
    }
    assert ("show_login_user", LoginUser(8, "tester")) in events


def test_auth_actions_do_not_cancel_or_race_with_each_other(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._auth_busy = True
    submit = Mock()
    monkeypatch.setattr(plugin, "_submit", submit)
    plugin._refresh()
    plugin._select_tournament(30)
    plugin._apply()
    submit.assert_not_called()


def test_checkin_uses_logged_in_identity_and_updates_snapshot(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = LoginUser(7, "tester")
    plugin._snapshot = Snapshot(plugin._snapshot.tournaments, 7, plugin._snapshot.fetched_at,
                                ((30, None),))
    started = Participant(7, 30, "checked-in-token", datetime.now(timezone.utc),
                          datetime.now(timezone.utc) + timedelta(hours=2))
    client = Mock()
    client.check_in.return_value = started
    monkeypatch.setattr(plugin, "_client_for_request", lambda: client)
    monkeypatch.setattr(plugin, "_submit", lambda kind, work: plugin._on_finished(plugin._epoch, kind, work(), None))
    plugin._check_in(30)
    client.check_in.assert_called_once_with(plugin._selected, 7)
    assert plugin._snapshot.participant_for(30) == started
    assert plugin._checkin_id is None
    assert ("set_busy", False, "打卡成功，个人参赛计时已开始。") in events


def test_checkin_requires_login_and_does_not_repeat_existing_registration(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    client = Mock()
    monkeypatch.setattr(plugin, "_client_for_request", lambda: client)
    plugin._check_in(30)
    client.check_in.assert_not_called()
    plugin._login_user = LoginUser(7, "tester")
    plugin._snapshot = Snapshot(plugin._snapshot.tournaments, 7, plugin._snapshot.fetched_at,
                                ((30, Participant(7, 30, "token", None, None)),))
    plugin._check_in(30)
    client.check_in.assert_not_called()
    assert "已经打卡" in events[-1][-1]


def test_check_in_all_processes_every_unregistered_live_tournament_and_applies_tokens(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = LoginUser(7, "tester")
    current = plugin._snapshot.tournaments[0]
    another = Tournament(32, "第二场进行中", "ongoing", None, None)
    already = Participant(7, 33, "existing", None, None)
    other_live = Tournament(33, "已参加的比赛", "ongoing", None, None)
    plugin._snapshot = Snapshot((current, another, other_live), 7,
                                plugin._snapshot.fetched_at, ((30, None), (32, None), (33, already)))
    started = Participant(7, 30, "new-one", None, None)
    started_two = Participant(7, 32, "new-two", None, None)
    client = Mock()
    client.check_in.side_effect = [started, started_two]
    client.active_participants.return_value = (started, started_two, already)
    monkeypatch.setattr(plugin, "_client_for_request", lambda: client)
    monkeypatch.setattr(plugin, "_submit", lambda kind, work: plugin._on_finished(plugin._epoch, kind, work(), None))
    request = Mock(return_value=CommandResponse("r", True))
    monkeypatch.setattr(plugin, "request", request)

    plugin._check_in_all()

    assert [call.args[0].id for call in client.check_in.call_args_list] == [30, 32]
    assert plugin._snapshot.participant_for(30) == started
    assert plugin._snapshot.participant_for(32) == started_two
    assert request.call_args.args[0].race_identifier == "new-one,new-two,existing"
    assert not plugin._checkin_all_busy
    assert any("成功 2 场" in event[-1] for event in events if event[0] == "set_busy")


def test_auto_maintenance_only_writes_on_ready_and_only_when_identifier_differs(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    first = Participant(7, 30, "token-a", None, None)
    second_tournament = Tournament(32, "第二场", "ongoing", None, None)
    second = Participant(7, 32, "token-b", None, None)
    plugin._snapshot = Snapshot((*plugin._snapshot.tournaments[:1], second_tournament), 7,
                                plugin._snapshot.fetched_at, ((30, first), (32, second)))
    plugin._selected = plugin._snapshot.tournaments[0]
    request = Mock(return_value=CommandResponse("r", True))
    monkeypatch.setattr(plugin, "request", request)

    plugin._auto_apply_from_snapshot(GameContextResult(
        request_id="ctx", success=True, game_status="playing", mode=0,
        is_fair=False, race_identifier="old"))
    request.assert_not_called()
    plugin._auto_apply_from_snapshot(GameContextResult(
        request_id="ctx", success=True, game_status="ready", mode=0,
        is_fair=True, race_identifier="token-a,token-b"))
    request.assert_not_called()
    plugin._auto_apply_from_snapshot(GameContextResult(
        request_id="ctx", success=True, game_status="ready", mode=0,
        is_fair=True, race_identifier="old"))
    assert request.call_args.args[0].race_identifier == "token-a,token-b"


def test_auto_maintenance_toggle_off_prevents_background_identifier_updates(factory, monkeypatch):
    plugin, _events = prepare(factory, monkeypatch)
    object.__getattribute__(plugin.other_info, "_values")["auto_manage_identifier"] = False
    request = Mock()
    monkeypatch.setattr(plugin, "request", request)
    plugin._auto_apply_from_snapshot(GameContextResult(
        request_id="ctx", success=True, game_status="ready", mode=0,
        is_fair=True, race_identifier="old"))
    request.assert_not_called()


def test_expired_session_and_logout_clear_authenticated_identity(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._login_user = LoginUser(7, "tester")
    refresh = Mock()
    monkeypatch.setattr(plugin, "_refresh", refresh)
    plugin._on_auth_finished(plugin._auth_epoch, "check", None, None)
    assert plugin._login_user is None and any(event[:2] == ("show_login_user", None) for event in events)
    refresh.assert_called_once()


def test_failed_login_invalidates_captcha_and_never_logs_password(factory, monkeypatch):
    plugin, events = prepare(factory, monkeypatch)
    plugin._captcha_key = "old-key"
    logs = []
    monkeypatch.setattr(plugin.logger, "warning", logs.append)
    plugin._on_auth_finished(plugin._auth_epoch, "login", None, OpenmsError("用户名或密码不正确。"))
    assert not plugin._captcha_key and ("invalidate_captcha",) in events
    assert logs == ["OpenMS authentication login: OpenmsError"]


def test_login_runs_in_network_worker_and_cookie_identity_reaches_widget(factory, monkeypatch, qtbot):
    plugin = factory()
    calls = []

    class FakeClient:
        def snapshot(self, user_id):
            return snapshot(user_id)

        def captcha(self):
            return Captcha("abcd1234", b"png")

        def login(self, username, password, captcha, key, *, expiry_days=0):
            calls.append((username, password, captcha, key, get_ident()))
            return LoginUser(8, "tester")

        def delete_cookies(self, path=None, *, strict=False):
            return None

        def load_cookies(self, path=None):
            return False

        def save_cookies(self, path=None):
            return None

        def current_user(self):
            return None

        def logout(self):
            return None

    monkeypatch.setattr(plugin, "_client_for_request", FakeClient)
    plugin.initialize()
    qtbot.waitUntil(lambda: plugin.is_ready and plugin._snapshot is not None)
    plugin.call_in_plugin_thread(plugin._refresh_captcha)
    qtbot.waitUntil(lambda: plugin._captcha_key == "abcd1234")
    plugin.call_in_plugin_thread(plugin._login, "tester", "secret", "1234")
    qtbot.waitUntil(lambda: plugin._login_user is not None)
    qtbot.waitUntil(lambda: "tester" in plugin.widget.auth_label.text())
    assert calls[0][:4] == ("tester", "secret", "1234", "abcd1234")
    assert calls[0][4] != get_ident()
    assert plugin.other_info.user_id == 8
    qtbot.waitUntil(lambda: plugin._snapshot is not None and plugin._snapshot.user_id == 8)
    assert not plugin.widget.user_id_edit.isEnabled()
    plugin.call_in_plugin_thread(plugin._check_login)
    qtbot.waitUntil(lambda: plugin._login_user is None)
    qtbot.waitUntil(lambda: plugin.widget.user_id_edit.isEnabled())
