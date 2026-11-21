from datetime import datetime, timezone

from PySide6.QtGui import QDesktopServices

from openms_tournament.analysis import ScoreVideo, analyze_personal
from openms_tournament.analysis_dialog import PersonalAnalysisDialog
from openms_tournament.client import LoginUser, Snapshot, Tournament
from openms_tournament.widgets import TournamentWidget


def test_dialog_shows_counted_and_default_slots_and_links_to_only_real_videos(qtbot, monkeypatch):
    tournament = Tournament(30, '测试周赛', 'ongoing', None, None)
    dialog = PersonalAnalysisDialog()
    qtbot.addWidget(dialog)
    dialog.begin(tournament, 7)
    result = analyze_personal(tournament, 7, (ScoreVideo(123, 'i', '00', 'c', 20000, 30, 30),))
    dialog.show_analysis(result)
    assert '740.000 秒' in dialog.total_label.text()
    assert '计入 1 / 7' in dialog.count_label.text()
    assert dialog.slot_table.rowCount() == 7
    assert dialog.slot_table.item(1, 5).text() == '补位（规则上限）'
    urls = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: urls.append(url.toString()))
    dialog._open_video(dialog.slot_table.item(0, 0))
    dialog._open_video(dialog.slot_table.item(1, 0))
    assert urls == ['https://openms.top/video/123']
    dialog.show_error('network down')
    assert dialog.slot_table.rowCount() == 0 and dialog.total_label.text() == ''
    assert dialog.status_label.text() == 'network down'


def test_dialog_does_not_display_late_result_for_different_account_or_race(qtbot):
    tournament = Tournament(30, '测试周赛', 'ongoing', None, None)
    dialog = PersonalAnalysisDialog()
    qtbot.addWidget(dialog)
    dialog.begin(tournament, 8)
    dialog.show_analysis(analyze_personal(tournament, 7, ()))
    assert dialog.total_label.text() == ''


def test_analysis_requires_login_and_selection_and_clears_on_scope_or_account_change(qtbot):
    tournament = Tournament(30, '测试周赛', 'ongoing', None, None)
    widget = TournamentWidget()
    qtbot.addWidget(widget)
    widget.show_snapshot(Snapshot((tournament,), 7, datetime.now(timezone.utc)))
    widget.table.selectRow(0)
    assert not widget.analysis_button.isEnabled()
    widget.show_login_user(LoginUser(7, 'tester'))
    assert widget.analysis_button.isEnabled()
    requests = []
    widget.analysis_requested.connect(requests.append)
    widget.analysis_button.click()
    assert requests == [30]
    widget.show_analysis(analyze_personal(tournament, 7, ()))
    assert widget.analysis_dialog.total_label.text()
    widget.show_login_user(LoginUser(8, 'other'))
    assert widget.analysis_dialog.total_label.text() == ''
    widget.begin_analysis(tournament, 8)
    widget.show_analysis(analyze_personal(tournament, 8, ()))
    widget.show_snapshot(Snapshot((), 8, datetime.now(timezone.utc)))
    assert widget.analysis_dialog.total_label.text() == ''


def test_gsc_references_are_shown_separately_from_main_time_score(qtbot):
    tournament = Tournament(30, '金羊杯', 'ongoing', None, None, 'g')
    dialog = PersonalAnalysisDialog()
    qtbot.addWidget(dialog)
    dialog.begin(tournament, 7)
    dialog.show_analysis(analyze_personal(tournament, 7, ()))
    assert dialog.tabs.isTabVisible(2)
    assert dialog.reference_table.rowCount() == 6
    assert '2120.000 秒' in dialog.total_label.text()
