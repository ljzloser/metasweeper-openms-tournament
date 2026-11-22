"""Compact personal score summary and auditable scoring slots."""
from __future__ import annotations
from datetime import timedelta, timezone

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .analysis import LEVEL_NAMES, STATE_NAMES, PersonalAnalysis, ScoreVideo
from .client import Tournament
from .ui_icons import icon_button, ui_icon


class PersonalAnalysisDialog(QDialog):
    refresh_requested = Signal(int)
    replay_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('我的比赛成绩')
        self.setWindowIcon(ui_icon('score'))
        self.resize(1160, 640)
        self.tournament_id: int | None = None
        self.user_id: int | None = None
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        self.heading_label = QLabel('请选择比赛')
        self.heading_label.setWordWrap(True)
        self.heading_label.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.heading_label, 1)
        self.refresh_button = icon_button('刷新我的成绩', 'refresh')
        self.refresh_button.clicked.connect(self._refresh)
        self.refresh_button.setEnabled(False)
        heading.addWidget(self.refresh_button)
        layout.addLayout(heading)
        self.total_label = QLabel()
        # Keep the label's foreground from the active palette. A stylesheet on
        # this label caused Qt to fall back to black text in some dark themes.
        score_font = QFont(self.total_label.font())
        score_font.setPixelSize(20)
        score_font.setWeight(QFont.Weight.DemiBold)
        self.total_label.setFont(score_font)
        layout.addWidget(self.total_label)
        self.rule_label = QLabel()
        self.rule_label.setWordWrap(True)
        layout.addWidget(self.rule_label)
        self.count_label = QLabel()
        self.count_label.setWordWrap(True)
        layout.addWidget(self.count_label)
        self.tabs = QTabWidget()
        comparison = QSplitter(Qt.Orientation.Horizontal)
        comparison.setChildrenCollapsible(False)
        overview = QWidget(comparison)
        overview_layout = QVBoxLayout(overview)
        overview_layout.addWidget(QLabel('当前计分明细'))
        group_headers = ['难度', '计入 / 需求', '补位', '最好', '边界（含补位）', '小计']
        slot_headers = ['难度', '序号', '用时（秒）', 'IOE', '本局计分', '录像 / 补位']
        self.group_table = self._table(group_headers)
        overview_layout.addWidget(self.group_table)
        self.slot_table = self._table(slot_headers)
        overview_layout.addWidget(self.slot_table, 1)
        estimate = QWidget(comparison)
        estimate_layout = QVBoxLayout(estimate)
        self.estimate_title = QLabel('预计成绩')
        estimate_font = self.estimate_title.font()
        estimate_font.setBold(True)
        self.estimate_title.setFont(estimate_font)
        estimate_layout.addWidget(self.estimate_title)
        self.estimated_group_table = self._table(group_headers)
        self.estimated_slot_table = self._table(slot_headers)
        estimate_layout.addWidget(self.estimated_group_table)
        estimate_layout.addWidget(self.estimated_slot_table, 1)
        comparison.setSizes([570, 570])
        self.tabs.addTab(comparison, ui_icon('score'), '计分明细')
        self.video_table = self._table(['录像 ID', '难度', '模式', '网站状态', '用时（秒）', '3BV', 'IOE', '计分', '计入情况 / 原因'])
        self.tabs.addTab(self.video_table, ui_icon('upload'), '已提交录像')
        reference_panel = QWidget()
        reference_layout = QVBoxLayout(reference_panel)
        reference_hint = QLabel('金羊杯参考统计：3BV/s、STNB 各自独立取最高的若干局，缺项按 0 补足；不参与官方用时总成绩。')
        reference_hint.setWordWrap(True)
        reference_layout.addWidget(reference_hint)
        self.reference_table = self._table(['难度', '指标', '取用局数', '补位', '合计', '取用录像 ID'])
        reference_layout.addWidget(self.reference_table)
        self.tabs.addTab(reference_panel, ui_icon('info'), '金羊杯参考指标')
        self.tabs.setTabVisible(2, False)
        layout.addWidget(self.tabs, 1)
        self.status_label = QLabel('登录后可读取网站关联到自己的比赛录像。')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        self.slot_table.itemDoubleClicked.connect(self._open_video)
        self.estimated_slot_table.itemDoubleClicked.connect(self._open_video)
        self.video_table.itemDoubleClicked.connect(self._open_video)
        footer = QHBoxLayout()
        footer.addWidget(QLabel('双击计分项或录像行，可在元扫雷中播放。'))
        footer.addStretch(1)
        close_button = icon_button('关闭', 'close')
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)
        self._apply_theme_colors()

    def changeEvent(self, event: QEvent):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._apply_theme_colors()

    def _apply_theme_colors(self):
        if not hasattr(self, 'status_label'):
            return
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        if self.status_label.property('error'):
            error_color = '#ff8a80' if dark else '#b42318'
            self.status_label.setStyleSheet(f'color: {error_color}; font-weight: 600;')
        else:
            self.status_label.setStyleSheet('')

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        return table

    @staticmethod
    def _fill(table: QTableWidget, rows: list[tuple[list[str], ScoreVideo | None]]):
        table.setRowCount(len(rows))
        for row, (values, video) in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setData(Qt.ItemDataRole.UserRole, video)
                table.setItem(row, column, item)

    def clear(self):
        self.tournament_id = self.user_id = None
        self._clear_scores()
        self.heading_label.setText('请选择比赛')
        self.status_label.setProperty('error', False)
        self._apply_theme_colors()
        self.status_label.setText('登录后可读取网站关联到自己的比赛录像。')
        self.tabs.setTabVisible(2, False)
        self.refresh_button.setEnabled(False)

    def begin(self, tournament: Tournament, user_id: int):
        self.clear()
        self.tournament_id, self.user_id = tournament.id, user_id
        self.heading_label.setText(f'{tournament.title} · OpenMS 用户 #{user_id}')
        self.status_label.setProperty('error', False)
        self._apply_theme_colors()
        self.status_label.setText('正在读取网站已提交录像并计算个人成绩…')

    def show_error(self, message: str):
        self._clear_scores()
        self.show_replay_status(message, error=True)
        self.refresh_button.setEnabled(self.tournament_id is not None)

    def _clear_scores(self):
        for table in (self.group_table, self.slot_table, self.video_table, self.reference_table,
                      self.estimated_group_table, self.estimated_slot_table):
            table.setRowCount(0)
        for label in (self.total_label, self.rule_label, self.count_label):
            label.clear()
        self.estimate_title.setText('预计成绩')

    def show_replay_status(self, message: str, error: bool = False):
        self.status_label.setProperty('error', error)
        self._apply_theme_colors()
        self.status_label.setText(message)

    def _fill_scores(self, group_table, slot_table, score_groups, unit):
        groups, slots = [], []
        for group in score_groups:
            groups.append(([LEVEL_NAMES[group.level], f'{group.selected_count} / {len(group.slots)}',
                            str(len(group.slots) - group.selected_count), f'{group.slots[0].value:.3f}',
                            f'{max(slot.value for slot in group.slots):.3f}', f'{group.total:.3f}{unit}'], None))
            for index, slot in enumerate(group.slots, 1):
                video = slot.video
                if video is None:
                    source = '补位（规则上限）'
                elif video.id < 0:
                    source = f'本地记录 #{-video.id}'
                else:
                    state = STATE_NAMES.get(video.state, video.state)
                    source = f'#{video.id}' if video.state == 'c' else f'#{video.id} · {state}'
                slots.append(([LEVEL_NAMES[group.level], str(index), f'{video.seconds:.3f}' if video else '—',
                               f'{video.ioe:.3f}' if video and video.ioe is not None else '—',
                               f'{slot.value:.3f}{unit}', source], video))
        self._fill(group_table, groups)
        group_table.setFixedHeight(group_table.horizontalHeader().height()
                                   + sum(group_table.rowHeight(row) for row in range(group_table.rowCount()))
                                   + 2 * group_table.frameWidth())
        self._fill(slot_table, slots)

    def show_analysis(self, result: PersonalAnalysis):
        if (self.tournament_id, self.user_id) != (result.tournament.id, result.user_id):
            return
        efficiency = result.tournament.subclass == 'e'
        unit = '' if efficiency else ' 秒'
        current = f'{result.total:.3f}{unit}' if not result.website_error else '暂不可用'
        projected = result.estimated_total
        estimate = f'{projected:.3f}{unit}' if projected is not None else '暂不可用'
        self.total_label.setText(f'当前{"效率总分" if efficiency else "总成绩"}：{current}')
        self.estimate_title.setText(f'预计成绩：{estimate}')
        rules = {
            'w': '竞速周赛：取最快的 5 局中级和 2 局高级；中级须低于 60 秒，高级须低于 240 秒，缺项按上限补足。',
            'e': '效率周赛：每局计分 = 用时（秒）÷ IOE²，IOE = 3BV ÷ 点击数。取中级最低 5 局、高级最低 2 局；缺项按 60 / 240 补足。计算使用原始精度。',
            'g': '金羊杯：取最快的初级 20 局、中级 12 局、高级 5 局；3BV 下限为 10 / 30 / 100。缺项按 10 / 60 / 240 秒补足。',
        }
        self.rule_label.setText('当前成绩：' + rules[result.tournament.subclass] + ' 仅计入网站审核通过的标准模式录像。')
        counted = sum(video.counted for video in result.videos)
        required = sum(len(group.slots) for group in result.groups)
        self.count_label.setText('' if result.website_error else
                                 f'网站已关联录像 {len(result.videos)} 局 · 计入 {counted} / {required} 局 · 补位 {required - counted} 项')
        self._fill_scores(self.group_table, self.slot_table, result.groups, unit)
        self._fill_scores(self.estimated_group_table, self.estimated_slot_table,
                          result.estimated_groups, unit)
        rows = []
        for assessment in result.videos:
            video = assessment.video
            rows.append(([str(video.id), LEVEL_NAMES.get(video.level, video.level),
                          '标准' if video.mode == '00' else video.mode, STATE_NAMES.get(video.state, video.state),
                          f'{video.seconds:.3f}', str(video.bv), f'{video.ioe:.3f}' if video.ioe is not None else '—',
                          f'{assessment.value:.3f}' if assessment.value is not None else '—', assessment.reason], video))
        self._fill(self.video_table, rows)
        self._fill(self.reference_table, [([LEVEL_NAMES[group.level], group.metric, str(len(group.values)),
                                           str(group.missing), f'{group.total:.3f}',
                                           ', '.join(f'#{video_id}' for video_id, _ in group.values)], None)
                                         for group in result.references])
        self.tabs.setTabVisible(2, bool(result.references))
        website_status = result.website_error or (
            f'网站读取时间：{result.fetched_at.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")}（北京时间）。')
        local_status = result.estimate_error or '预计成绩使用插件保存的本地胜局，忽略上传、审核及参赛资格。'
        self.show_replay_status(f'{local_status}\n{website_status}')
        self.refresh_button.setEnabled(True)

    def _refresh(self):
        if self.tournament_id is not None:
            self.refresh_requested.emit(self.tournament_id)

    def _open_video(self, item: QTableWidgetItem):
        video = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(video, ScoreVideo):
            self.replay_requested.emit(video)
