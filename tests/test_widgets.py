from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QPixmap

from openms_tournament.client import Captcha, LoginUser, Participant, Snapshot, Tournament
from openms_tournament.widgets import TournamentWidget, format_time
from shared_types import GameContextResult


def test_widget_selection_copy_and_unsaved_identity_invalidation(qtbot):
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    tournament = Tournament(30, "周赛", "ongoing", None, None)
    snapshot = Snapshot((tournament,), 7, datetime.now(timezone.utc))
    widget.show_snapshot(snapshot)
    selected = []
    widget.tournament_selected.connect(selected.append)
    widget.table.selectRow(0)
    assert selected == [30]
    widget.show_participant(tournament, Participant(7, 30, "race-token", None, None))
    assert widget.table.item(0, 2).text() == "race-token"
    widget.show_game_context(GameContextResult(
        request_id="ctx", success=True, game_status="ready", mode=0,
        is_fair=False, board_constraint=False, race_identifier="old,overall"))
    assert widget.copy_button.isEnabled()
    assert not widget.apply_button.isEnabled()
    widget.set_authorized(True)
    assert widget.apply_button.isEnabled()
    assert widget.permission_label.isHidden()
    widget.copy_button.click()
    assert QApplication.clipboard().text() == "old,overall"
    widget.user_id_edit.setValue(8)
    assert not widget.copy_button.isEnabled() and not widget.apply_button.isEnabled()
    saved = []
    widget.user_id_saved.connect(saved.append)
    widget.save_button.click()
    assert saved == [8]


def test_authorization_status_is_explicit_and_updates_live(qtbot):
    widget = TournamentWidget()
    qtbot.addWidget(widget)
    assert "未授权" in widget.permission_label.text()
    assert "无法自动或手动应用比赛标识" in widget.permission_label.text()
    assert widget.condition_row.indexOf(widget.permission_label) >= 0
    assert widget.condition_row.indexOf(widget.condition_button) >= 0
    assert widget.layout().indexOf(widget.condition_panel) == 2
    assert not widget.condition_panel.isHidden()
    assert widget.condition_button.isHidden()
    widget.set_authorized(True)
    assert widget.permission_label.isHidden()
    assert widget.condition_panel.isHidden()
    widget.set_authorized(False)
    assert "未授权" in widget.permission_label.text()
    assert "#b42318" in widget.permission_label.styleSheet()
    assert not widget.permission_label.isHidden()
    assert not widget.condition_panel.isHidden()


def test_upload_history_distinguishes_duplicate_and_success_and_retries_only_when_enabled(qtbot, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    widget = TournamentWidget()
    qtbot.addWidget(widget)
    assert not widget.auto_upload_checkbox.isChecked()
    assert not widget.retry_uploads_button.isEnabled()
    widget.show_login_user(LoginUser(7, "tester"))
    widget.set_auto_upload(True)
    assert widget.retry_uploads_button.isEnabled()
    history_requested, retries, opened = [], [], []
    widget.upload_history_requested.connect(lambda: history_requested.append(True))
    widget.retry_uploads_requested.connect(lambda: retries.append(True))
    widget.upload_history_button.click()
    assert history_requested and not widget.upload_dialog.isHidden()
    widget.set_upload_records([
        dict(created=0, user_id=7, filename="duplicate.evf", status="duplicate", video_id=None, message="已移除"),
        dict(created=0, user_id=7, filename="success.evf", status="succeeded", video_id=123, message="网站状态：标识不匹配"),
    ])
    widget.upload_table.selectRow(0)
    assert widget.upload_table.item(0, 3).text() == "网站已存在"
    assert not widget.open_uploaded_video_button.isEnabled()
    widget.upload_table.selectRow(1)
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    widget.open_uploaded_video_button.click()
    assert opened == ["https://openms.top/video/123"]
    widget.retry_uploads_button.click()
    assert retries == [True]
    widget.set_auto_upload(False)
    assert not widget.retry_uploads_button.isEnabled()


def test_busy_error_and_expired_participant_disable_application(qtbot):
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    tournament = Tournament(30, "周赛", "ongoing", None, None)
    widget.set_authorized(True)
    widget.show_participant(tournament, Participant(7, 30, "token", None, None))
    widget.set_current_identifier("current-total")
    widget.set_busy(True)
    assert not widget.apply_button.isEnabled()
    widget.show_error("network failure")
    assert not widget._token and widget.current_identifier_edit.text() == "current-total"
    assert "network failure" in widget.status_label.text()
    widget.show_participant(tournament, Participant(
        7, 30, "old-token", None, datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert widget.copy_button.isEnabled() and not widget.apply_button.isEnabled()
    assert "有效参赛时间" in widget.window_label.text()
    widget.show_participant(tournament, None)
    assert not widget._token and "尚未报名" in widget.window_label.text()


def test_times_are_rendered_in_beijing_time_without_tzdata():
    assert format_time(datetime(2026, 11, 23, tzinfo=timezone.utc)) == "2026-11-23 08:00"
    assert format_time(None) == "—"


def test_checkin_column_requires_login_confirms_start_and_shows_countdown(qtbot, monkeypatch):
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    widget.show_login_user(LoginUser(7, "tester"))
    tournament = Tournament(30, "周赛", "ongoing", None, None)
    widget.show_snapshot(Snapshot((tournament,), 7, datetime.now(timezone.utc), ((30, None),)))
    assert widget.table.columnCount() == 9
    assert widget.table.horizontalHeaderItem(2).text() == "比赛标识"
    assert widget.table.horizontalHeaderItem(3).text() == "我已打卡"
    assert widget.table.horizontalHeaderItem(4).text() == "打卡倒计时"
    assert widget.table.horizontalHeaderItem(7).text() == "当前成绩"
    assert widget.table.horizontalHeaderItem(8).text() == "预计成绩"
    assert widget.table.item(0, 2).text() == "—"
    cell = widget.table.cellWidget(0, 3)
    button = cell.findChild(__import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton, "checkInButton")
    assert not button.isHidden() and button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    selected = []
    widget.check_in_requested.connect(selected.append)
    widget._confirm_checkin(30)
    assert selected == [30]

    participant = Participant(7, 30, "token", datetime.now(timezone.utc),
                              datetime.now(timezone.utc) + timedelta(seconds=65))
    widget.show_participant(tournament, participant)
    assert cell.findChild(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel,
                          "attendanceStatus").text() == "已打卡"
    assert widget.table.item(0, 2).text() == "token"
    assert widget.table.item(0, 4).text() in {"00:01:04", "00:01:05"}
    assert button.isHidden()


def test_tournament_tokens_are_in_rows_and_bottom_field_shows_host_total(qtbot):
    first = Tournament(30, "第一场", "ongoing", None, None)
    second = Tournament(31, "第二场", "ongoing", None, None)
    participant_one = Participant(7, 30, "TOKEN1", None, None)
    participant_two = Participant(7, 31, "TOKEN2", None, None)
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    widget.show_snapshot(Snapshot(
        (first, second), 7, datetime.now(timezone.utc),
        ((30, participant_one), (31, participant_two))))
    assert [widget.table.item(row, 2).text() for row in range(2)] == ["TOKEN1", "TOKEN2"]
    widget.show_game_context(GameContextResult(
        request_id="ctx", success=True, game_status="ready", mode=0,
        is_fair=False, race_identifier="TOKEN1,TOKEN2"))
    assert widget.current_identifier_edit.text() == "TOKEN1,TOKEN2"
    assert widget.copy_button.isEnabled()
    widget.copy_button.click()
    assert QApplication.clipboard().text() == "TOKEN1,TOKEN2"


def test_missing_user_identity_does_not_claim_unchecked_status(qtbot):
    widget = TournamentWidget()
    qtbot.addWidget(widget)
    tournament = Tournament(30, "周赛", "ongoing", None, None)
    widget.show_snapshot(Snapshot((tournament,), None, datetime.now(timezone.utc)))
    cell = widget.table.cellWidget(0, 3)
    status = cell.findChild(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel,
                            "attendanceStatus")
    assert status.text() == "—"


def test_check_in_all_confirmation_and_default_switches(qtbot, monkeypatch):
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    widget.show_login_user(LoginUser(7, "tester"))
    live = Tournament(30, "本周竞速赛", "ongoing", None, None)
    waiting = Tournament(31, "下周竞速赛", "preparing", None, None)
    widget.show_snapshot(Snapshot((live, waiting), 7, datetime.now(timezone.utc), ((30, None),)))
    assert widget.auto_manage_checkbox.isChecked()
    assert widget.check_in_all_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    requests = []
    widget.check_in_all_requested.connect(lambda: requests.append(True))
    widget._confirm_check_in_all()
    assert requests == [True]
    widget.set_checkin_all_busy(True)
    assert not widget.check_in_all_button.isEnabled()


def test_rules_panel_uses_host_fairness_mode_controls_and_constraint(qtbot):
    from shared_types import ControlPermissions, GameContextResult

    widget = TournamentWidget()
    qtbot.addWidget(widget)
    widget.show_game_context(GameContextResult(
        request_id="ctx", success=True, game_status="playing", mode=0,
        mode_name="标准", is_fair=False, board_constraint=True,
        permissions=ControlPermissions(allowed_controls=["mouse_click", "board_update"])))
    assert not widget.condition_button.isHidden()
    assert "点击查看原因和处理方案" in widget.condition_button.text()
    assert widget.condition_button.toolTip() == ""
    assert "当前对局不公平" in widget._condition_details[0]
    assert "启用了预设局面约束" in "\n".join(widget._condition_details)
    assert "mouse_click" in "\n".join(widget._condition_details)


def test_ready_game_does_not_present_prestart_fairness_as_unfair(qtbot):
    from shared_types import GameContextResult

    widget = TournamentWidget()
    qtbot.addWidget(widget)
    widget.show_game_context(GameContextResult(
        request_id="ctx", success=True, game_status="ready", mode=0,
        is_fair=False, board_constraint=False))
    assert widget.condition_button.isHidden()


def test_unfair_game_shows_compact_button_and_fair_game_hides_it(qtbot, monkeypatch):
    from shared_types import GameContextResult

    widget = TournamentWidget()
    qtbot.addWidget(widget)
    widget.show_game_context(GameContextResult(
        request_id="ctx", success=True, game_status="playing", mode=0,
        mode_name="标准", is_fair=False, board_constraint=False))
    assert not widget.condition_button.isHidden()
    assert widget.condition_button.text() == "⚠ 当前对局不公平 · 点击查看原因和处理方案"
    assert widget.condition_button.toolTip() == ""
    assert not widget.permission_label.isHidden()
    widget.set_authorized(True)
    assert widget.permission_label.isHidden()
    assert not widget.condition_button.isHidden()
    assert not widget.condition_panel.isHidden()
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[2]))
    widget.condition_button.click()
    assert "原因：主进程判定当前对局不公平。" in messages[0]
    assert "处理：结束当前对局" in messages[0]
    widget.show_game_context(GameContextResult(
        request_id="ctx2", success=True, game_status="playing", mode=0,
        mode_name="标准", is_fair=True, board_constraint=False))
    assert widget.condition_button.isHidden()
    assert widget.condition_panel.isHidden()
    widget.set_authorized(False)
    assert not widget.permission_label.isHidden()
    assert not widget.condition_panel.isHidden()
    assert widget.condition_button.isHidden()


def test_login_ui_requires_manual_captcha_and_clears_password_on_submit(qtbot):
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    assert widget.login_dialog.isHidden()
    widget.open_login_button.click()
    assert not widget.login_dialog.isHidden()
    widget.username_edit.setText("tester")
    widget.password_edit.setText("secret")
    widget.captcha_edit.setText("1234")
    assert not widget.login_button.isEnabled()
    image = QPixmap(80, 32)
    image.fill()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    widget.show_captcha(Captcha("abcd1234", bytes(data)))
    widget.captcha_edit.setText("1234")
    assert widget.login_button.isEnabled()
    values = []
    widget.login_requested.connect(lambda *args: values.append(args))
    widget.login_button.click()
    assert values == [("tester", "secret", "1234")]
    assert not widget.password_edit.text() and not widget.captcha_edit.text()
    assert not widget.login_button.isEnabled()
    assert widget.login_dialog.isHidden()


def test_username_is_prefilled_saved_and_updated_after_login(qtbot):
    widget = TournamentWidget(7, username="saved-user")
    qtbot.addWidget(widget)
    widget.open_login_button.click()
    assert widget.username_edit.text() == "saved-user"
    saved = []
    widget.username_saved.connect(saved.append)
    widget.username_edit.setText("edited-user")
    widget.username_edit.editingFinished.emit()
    assert saved == ["edited-user"]
    widget.show_login_user(LoginUser(7, "confirmed-user"))
    assert widget.username_edit.text() == "confirmed-user"
    assert widget.login_dialog.isHidden()


def test_logged_in_user_locks_identity_and_logout_restores_manual_config(qtbot):
    widget = TournamentWidget(7)
    qtbot.addWidget(widget)
    assert widget.login_dialog.isHidden()
    assert not widget.open_login_button.isHidden()
    widget.show_login_user(LoginUser(8, "tester"))
    assert widget.login_dialog.isHidden()
    assert widget.open_login_button.isHidden()
    assert widget.identity_panel.isHidden()
    assert widget.user_id_edit.value() == 8
    assert not widget.user_id_edit.isEnabled() and not widget.save_button.isEnabled()
    assert widget.logout_button.isEnabled() and not widget.captcha_button.isEnabled()
    assert "tester" in widget.auth_label.text()
    widget.set_auth_busy(True)
    assert not widget.logout_button.isEnabled() and not widget.refresh_button.isEnabled()
    widget.set_auth_busy(False)
    widget.show_login_user(None)
    assert widget.user_id_edit.isEnabled() and widget.captcha_button.isEnabled()
    assert widget.login_dialog.isHidden()
    assert not widget.open_login_button.isHidden()
    assert not widget.identity_panel.isHidden()
    assert not widget.logout_button.isEnabled()
