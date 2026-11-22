"""比赛列表及比赛标识操作界面。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QHideEvent, QPalette, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .client import Captcha, LoginUser, Participant, Snapshot, Tournament
from .analysis import PersonalAnalysis, supports_scoring
from .analysis_dialog import PersonalAnalysisDialog
from .ui_icons import icon_button, tournament_icon, ui_icon

CHINA_TIME = timezone(timedelta(hours=8))
ANALYSIS_COLUMN = 9


def format_time(value: datetime | None) -> str:
    return value.astimezone(CHINA_TIME).strftime("%Y-%m-%d %H:%M") if value else "—"


class UploadNotice(QLabel):
    def __init__(self):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet(
            "QLabel { color: palette(text); background: palette(alternate-base); "
            "border: 1px solid palette(mid); border-radius: 4px; padding: 4px 8px; }")
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(5000)
        self._hide_timer.timeout.connect(self._dismiss)
        self.hide()

    def show_message(self, message: str):
        self.setText(message)
        self.show()
        if self.isVisible() and not self.window().isMinimized():
            self._hide_timer.start()

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        if self.text():
            self._hide_timer.start()

    def hideEvent(self, event: QHideEvent):
        self._hide_timer.stop()
        super().hideEvent(event)

    def _dismiss(self):
        self.clear()
        self.hide()


class TournamentWidget(QWidget):
    refresh_requested = Signal()
    user_id_saved = Signal(int)
    tournament_selected = Signal(int)
    apply_requested = Signal()
    captcha_requested = Signal()
    login_requested = Signal(str, str, str)
    login_check_requested = Signal()
    logout_requested = Signal()
    check_in_requested = Signal(int)
    check_in_all_requested = Signal()
    auto_manage_changed = Signal(bool)
    username_saved = Signal(str)
    auto_upload_changed = Signal(bool)
    upload_history_requested = Signal()
    manual_upload_requested = Signal()
    manual_replay_selected = Signal(str)
    retry_uploads_requested = Signal()
    analysis_requested = Signal(int)
    replay_requested = Signal(object)

    def __init__(self, user_id: int = 0, username: str = "", auto_manage: bool = True, auto_upload: bool = False):
        super().__init__()
        self.setMinimumSize(720, 440)
        self._saved_user_id = user_id
        self._token = ""
        self._current_identifier = ""
        self._identifier_available = False
        self._can_apply = False
        self._authorized = False
        self._busy = False
        self._auth_busy = False
        self._login_user: LoginUser | None = None
        self._has_captcha = False
        self._participants: dict[int, Participant | None] = {}
        self._tournaments: dict[int, Tournament] = {}
        self._personal_scores: dict[int, PersonalAnalysis] = {}
        self._has_participant_identity = False
        self._checkin_busy: set[int] = set()
        self._checkin_all_busy = False
        self._condition_details: list[str] = []
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self._upload_notice = UploadNotice()
        layout.addWidget(self._upload_notice)
        heading_row = QHBoxLayout()
        heading_row.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(tournament_icon().pixmap(QSize(34, 34), self.devicePixelRatioF()))
        heading_row.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("OpenMS 比赛")
        title_font = QFont(title.font())
        title_font.setPixelSize(22)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        title_block.addWidget(title)
        title_block.addWidget(QLabel("北京时间（UTC+8）"))
        heading_row.addLayout(title_block)
        heading_row.addStretch(1)
        account_block = QVBoxLayout()
        account_block.setSpacing(4)
        heading_row.addLayout(account_block)
        layout.addLayout(heading_row)
        self.condition_panel = QWidget(self)
        self.condition_row = QHBoxLayout(self.condition_panel)
        self.condition_row.setContentsMargins(0, 0, 0, 0)
        self.permission_label = QLabel()
        self.permission_label.setWordWrap(True)
        self.condition_row.addWidget(self.permission_label, 1)
        self.condition_button = QPushButton("⚠ 对局条件异常 · 点击查看原因和处理方案")
        self.condition_button.setObjectName("conditionButton")
        self.condition_button.clicked.connect(self._show_condition_details)
        self.condition_row.addWidget(self.condition_button)
        layout.addWidget(self.condition_panel)
        self.login_dialog = QDialog(self)
        self.login_dialog.setWindowTitle("登录 OpenMS")
        self.login_dialog.setWindowIcon(tournament_icon())
        self.login_dialog.setModal(True)
        login_dialog_layout = QVBoxLayout(self.login_dialog)
        login_dialog_layout.addWidget(QLabel("使用 OpenMS 账号登录。密码仅用于本次登录，不会保存。"))
        credentials_form = QFormLayout()
        self.username_edit = QLineEdit()
        self.username_edit.setText(username)
        self.username_edit.setPlaceholderText("OpenMS 用户名")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("密码（仅用于本次登录）")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        credentials_form.addRow("账号", self.username_edit)
        credentials_form.addRow("密码", self.password_edit)
        login_dialog_layout.addLayout(credentials_form)
        captcha_row = QHBoxLayout()
        self.captcha_image = QLabel("点击刷新验证码")
        self.captcha_image.setFixedSize(140, 42)
        self.captcha_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        captcha_row.addWidget(self.captcha_image)
        self.captcha_edit = QLineEdit()
        self.captcha_edit.setPlaceholderText("图片验证码")
        self.captcha_edit.setMaxLength(6)
        self.captcha_edit.setMaximumWidth(100)
        captcha_row.addWidget(self.captcha_edit)
        self.captcha_button = icon_button("刷新验证码", "refresh")
        self.captcha_button.clicked.connect(self.captcha_requested)
        captcha_row.addWidget(self.captcha_button)
        captcha_row.addStretch(1)
        login_dialog_layout.addLayout(captcha_row)
        dialog_buttons = QHBoxLayout()
        dialog_buttons.addStretch(1)
        self.login_button = icon_button("登录", "authorize")
        self.login_button.clicked.connect(self._login)
        dialog_buttons.addWidget(self.login_button)
        self.cancel_login_button = icon_button("取消", "cancel")
        self.cancel_login_button.clicked.connect(self.login_dialog.reject)
        dialog_buttons.addWidget(self.cancel_login_button)
        login_dialog_layout.addLayout(dialog_buttons)
        auth_row = QHBoxLayout()
        self.auth_label = QLabel("未登录 · 可填写用户 ID 匿名浏览；不会保存密码。")
        self.auth_label.setTextFormat(Qt.TextFormat.PlainText)
        self.auth_label.setWordWrap(True)
        self.auth_label.setMinimumWidth(245)
        self.auth_label.setMaximumWidth(360)
        account_block.addWidget(self.auth_label)
        self.open_login_button = icon_button("登录 OpenMS", "authorize")
        self.open_login_button.clicked.connect(self._open_login_dialog)
        auth_row.addWidget(self.open_login_button)
        self.login_check_button = icon_button("检查登录状态", "refresh")
        self.login_check_button.clicked.connect(self.login_check_requested)
        auth_row.addWidget(self.login_check_button)
        self.logout_button = icon_button("退出登录", "window-close")
        self.logout_button.clicked.connect(self.logout_requested)
        auth_row.addWidget(self.logout_button)
        auth_row.addStretch(1)
        account_block.addLayout(auth_row)
        self.identity_panel = QWidget(self)
        identity_row = QHBoxLayout(self.identity_panel)
        identity_row.setContentsMargins(0, 0, 0, 0)
        self.user_id_label = QLabel("OpenMS 用户 ID")
        identity_row.addWidget(self.user_id_label)
        self.user_id_edit = QSpinBox()
        self.user_id_edit.setRange(0, 2_147_483_647)
        self.user_id_edit.setSpecialValueText("未配置")
        self.user_id_edit.setValue(user_id)
        self.user_id_edit.setToolTip("填写 OpenMS 个人主页对应的数字用户 ID；0 表示未配置。")
        identity_row.addWidget(self.user_id_edit)
        self.save_button = icon_button("保存并刷新", "confirm")
        self.save_button.clicked.connect(lambda: self.user_id_saved.emit(self.user_id_edit.value()))
        identity_row.addWidget(self.save_button)
        identity_row.addStretch(1)
        layout.addWidget(self.identity_panel)
        toolbar = QFrame()
        toolbar.setFrameShape(QFrame.Shape.StyledPanel)
        options_row = QHBoxLayout(toolbar)
        options_row.setContentsMargins(8, 4, 8, 4)
        options_row.setSpacing(8)
        self.refresh_button = icon_button("刷新比赛", "refresh")
        self.refresh_button.clicked.connect(self.refresh_requested)
        options_row.addWidget(self.refresh_button)
        self.check_in_all_button = icon_button("一键打卡", "confirm")
        self.check_in_all_button.setToolTip("登录后逐场打卡所有可参加的进行中比赛。")
        self.check_in_all_button.clicked.connect(self._confirm_check_in_all)
        options_row.addWidget(self.check_in_all_button)
        self.manual_upload_button = icon_button("手动上传", "upload")
        self.manual_upload_button.setToolTip("选择插件已保存的比赛录像。关闭自动上传时也可使用。")
        self.manual_upload_button.clicked.connect(self.manual_upload_requested)
        options_row.addWidget(self.manual_upload_button)
        self.upload_history_button = icon_button("上传记录", "log")
        self.upload_history_button.clicked.connect(self._open_upload_history)
        options_row.addWidget(self.upload_history_button)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        options_row.addWidget(divider)
        self.auto_manage_checkbox = QCheckBox("自动维护比赛标识")
        self.auto_manage_checkbox.setToolTip(
            "每 5 分钟检查一次已打卡比赛；回到待开局状态时也会检查并应用有效标识。")
        self.auto_manage_checkbox.setChecked(auto_manage)
        self.auto_manage_checkbox.toggled.connect(self.auto_manage_changed)
        options_row.addWidget(self.auto_manage_checkbox)
        self.auto_upload_checkbox = QCheckBox("自动上传录像")
        self.auto_upload_checkbox.setChecked(auto_upload)
        self.auto_upload_checkbox.setToolTip("上传开启后完成的公平正式标准模式比赛胜局；重复录像自动从队列移除。")
        self.auto_upload_checkbox.toggled.connect(self.auto_upload_changed)
        self.auto_upload_checkbox.toggled.connect(self._update_actions)
        options_row.addWidget(self.auto_upload_checkbox)
        options_row.addStretch(1)
        layout.addWidget(toolbar)
        self.analysis_dialog = PersonalAnalysisDialog(self)
        self.analysis_dialog.refresh_requested.connect(self.analysis_requested)
        self.analysis_dialog.replay_requested.connect(self.replay_requested)
        self.upload_dialog = QDialog(self)
        self.upload_dialog.setWindowTitle("比赛录像上传记录")
        self.upload_dialog.setWindowIcon(ui_icon("upload"))
        self.upload_dialog.resize(880, 420)
        upload_layout = QVBoxLayout(self.upload_dialog)
        upload_hint = QLabel(
            "“网站已存在”会丢弃队列副本，不再重试。超出当前比赛范围或参赛上传窗口的数据和记录会全部删除。"
            "上传成功与比赛是否计分分开，请以网站结果为准。")
        upload_hint.setWordWrap(True)
        upload_layout.addWidget(upload_hint)
        self.upload_table = QTableWidget(0, 6)
        self.upload_table.setHorizontalHeaderLabels(["加入时间", "账号 ID", "录像", "上传状态", "网站录像 ID", "说明"])
        self.upload_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.upload_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.upload_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.upload_table.verticalHeader().hide()
        self.upload_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.upload_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.upload_table.setAlternatingRowColors(True)
        upload_layout.addWidget(self.upload_table, 1)
        upload_buttons = QHBoxLayout()
        self.retry_uploads_button = icon_button("重试失败项", "refresh")
        self.retry_uploads_button.setToolTip("重试当前登录账号上传失败的录像。")
        self.retry_uploads_button.clicked.connect(self.retry_uploads_requested)
        upload_buttons.addWidget(self.retry_uploads_button)
        upload_buttons.addStretch(1)
        close_uploads_button = icon_button("关闭", "close")
        close_uploads_button.clicked.connect(self.upload_dialog.close)
        upload_buttons.addWidget(close_uploads_button)
        upload_layout.addLayout(upload_buttons)
        self.table = QTableWidget(0, ANALYSIS_COLUMN + 1)
        self.table.setHorizontalHeaderLabels(
            ["状态", "比赛", "比赛标识", "我已打卡", "打卡倒计时", "开始时间", "结束时间", "当前成绩", "预计成绩", "操作"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().resizeSection(2, 112)
        self.table.itemSelectionChanged.connect(self._select)
        self.table.itemDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table, 1)
        self.window_label = QLabel("选中比赛查看个人参赛时间。")
        self.window_label.setWordWrap(True)
        self.window_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.window_label)
        token_row = QHBoxLayout()
        token_row.addWidget(QLabel("当前总比赛标识"))
        self.current_identifier_edit = QLineEdit()
        self.current_identifier_edit.setReadOnly(True)
        self.current_identifier_edit.addAction(ui_icon("tag"), QLineEdit.ActionPosition.LeadingPosition)
        self.current_identifier_edit.setPlaceholderText("读取主程序当前设置")
        self.current_identifier_edit.setText("等待读取主程序标识")
        token_row.addWidget(self.current_identifier_edit, 1)
        self.copy_button = icon_button("复制总标识", "copy")
        self.copy_button.clicked.connect(self._copy)
        token_row.addWidget(self.copy_button)
        self.apply_button = icon_button("设置到元扫雷", "settings")
        self.apply_button.setToolTip(
            "重新核对所有当前有效参赛比赛，并将各场比赛标识用英文半角逗号连接（不加空格）后设置。")
        self.apply_button.clicked.connect(self.apply_requested)
        token_row.addWidget(self.apply_button)
        layout.addLayout(token_row)
        self.status_label = QLabel("等待刷新。")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        self.user_id_edit.valueChanged.connect(self._update_actions)
        self.username_edit.editingFinished.connect(
            lambda: self.username_saved.emit(self.username_edit.text().strip()))
        for edit in (self.username_edit, self.password_edit, self.captcha_edit):
            edit.textChanged.connect(self._update_actions)
        self.captcha_edit.returnPressed.connect(self.login_button.click)
        self.set_authorized(False)
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_countdowns)
        self._countdown_timer.start()

    def changeEvent(self, event: QEvent):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._apply_alert_colors()

    def _apply_alert_colors(self):
        if not hasattr(self, 'condition_button'):
            return
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        if dark:
            text, border, background = "#ffaaa3", "#75413d", "#392725"
        else:
            text, border, background = "#9f1d14", "#e7aaa4", "#fff1ef"
        self.permission_label.setStyleSheet(
            "QLabel { "
            f"color: {text}; border: 1px solid {border}; background: {background}; "
            "font-weight: 700; padding: 3px 8px; border-radius: 4px; }")
        self.condition_button.setStyleSheet(
            "QPushButton#conditionButton { "
            f"color: {text}; border: 1px solid {border}; background: {background}; "
            "font-weight: 600; padding: 3px 8px; border-radius: 4px; }")

    def _update_actions(self, *_):
        saved = self.user_id_edit.value() == self._saved_user_id
        self.copy_button.setEnabled(
            bool(self._current_identifier) and self._identifier_available and saved
            and not self._busy and not self._auth_busy)
        self.apply_button.setEnabled(
            bool(self._token) and saved and self._can_apply and self._authorized
            and not self._busy and not self._auth_busy)
        logged_in = self._login_user is not None
        self.analysis_dialog.refresh_button.setEnabled(
            logged_in and self.analysis_dialog.tournament_id is not None and not self._busy and not self._auth_busy)
        self.user_id_edit.setEnabled(not logged_in and not self._auth_busy)
        self.save_button.setEnabled(not logged_in and not self._auth_busy)
        self.refresh_button.setEnabled(not self._busy and not self._auth_busy)
        self.check_in_all_button.setEnabled(
            bool(self._login_user and self._has_participant_identity and self._checkin_candidates())
            and not self._busy and not self._auth_busy and not self._checkin_all_busy)
        self.auto_manage_checkbox.setEnabled(not self._auth_busy)
        self.auto_upload_checkbox.setEnabled(not self._auth_busy)
        self.manual_upload_button.setEnabled(logged_in and not self._auth_busy and not self._busy)
        self.retry_uploads_button.setEnabled(logged_in and not self._auth_busy and not self._busy)
        self.table.setEnabled(not self._busy and not self._auth_busy)
        for edit in (self.username_edit, self.password_edit, self.captcha_edit):
            edit.setEnabled(not logged_in and not self._auth_busy)
        self.captcha_button.setEnabled(not logged_in and not self._auth_busy)
        self.login_button.setEnabled(
            not logged_in and not self._auth_busy and self._has_captcha
            and bool(self.username_edit.text().strip()) and bool(self.password_edit.text())
            and bool(self.captcha_edit.text().strip()))
        self.login_check_button.setEnabled(not self._auth_busy)
        self.logout_button.setEnabled(logged_in and not self._auth_busy)
        self.login_check_button.setVisible(logged_in)
        self.logout_button.setVisible(logged_in)
        self.open_login_button.setVisible(not logged_in)
        self.open_login_button.setEnabled(not self._auth_busy)
        self.identity_panel.setVisible(not logged_in)
        for row in range(self.table.rowCount()):
            identity_item = self.table.item(row, 0)
            cell = self.table.cellWidget(row, 3)
            if identity_item is None or cell is None:
                continue
            tournament_id = identity_item.data(Qt.ItemDataRole.UserRole)
            tournament = self._tournaments.get(tournament_id)
            self._update_analysis_button(row, tournament)
            participant = self._participants.get(tournament_id)
            button = cell.findChild(QPushButton, "checkInButton")
            if button is not None:
                can_check_in = bool(tournament and tournament.is_ongoing()
                                    and self._has_participant_identity and participant is None)
                button.setVisible(can_check_in)
                button.setEnabled(can_check_in and logged_in and tournament_id not in self._checkin_busy
                                  and not self._busy and not self._auth_busy)

    def _login(self):
        if not self.login_button.isEnabled():
            return
        username, password, captcha = (
            self.username_edit.text().strip(), self.password_edit.text(), self.captcha_edit.text().strip())
        self.password_edit.clear()
        self.captcha_edit.clear()
        self.login_dialog.accept()
        self.username_saved.emit(username)
        self.login_requested.emit(username, password, captcha)

    def _open_login_dialog(self):
        if self._login_user is None and not self._auth_busy:
            self.login_dialog.open()

    def set_username(self, username: str):
        self.username_edit.setText(username)

    def set_auth_busy(self, busy: bool, message: str = ""):
        self._auth_busy = busy
        if message:
            self.auth_label.setText(message)
        self._update_actions()

    def invalidate_captcha(self):
        self._has_captcha = False
        self.captcha_edit.clear()
        self.captcha_image.clear()
        self.captcha_image.setText("请刷新验证码")
        self._update_actions()

    def show_captcha(self, captcha: Captcha):
        self.invalidate_captcha()
        pixmap = QPixmap()
        if not pixmap.loadFromData(captcha.image, "PNG"):
            self.auth_label.setText("验证码图片无法显示，请刷新。")
            return
        self.captcha_image.setPixmap(pixmap.scaled(
            self.captcha_image.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._has_captcha = True
        self._update_actions()

    def show_login_user(self, user: LoginUser | None, message: str = ""):
        changed = self._login_user != user
        if changed:
            self.clear_analysis()
        self._login_user = user
        if changed:
            self.show_scores(())
        self.password_edit.clear()
        self.invalidate_captcha()
        if user:
            self.set_username(user.username)
            self.set_user_id(user.id)
            text = f"已登录：{user.username} · OpenMS 用户 #{user.id}"
        else:
            text = "未登录 · 可填写用户 ID 匿名浏览；不会保存密码。"
        self.auth_label.setText(message or text)
        if user is not None:
            self.login_dialog.hide()
        self._update_actions()

    def set_user_id(self, user_id: int):
        self._saved_user_id = user_id
        self.user_id_edit.setValue(user_id)
        self.clear_token()

    def set_authorized(self, authorized: bool):
        self._authorized = authorized
        self._apply_alert_colors()
        self.permission_label.setText(
            ""
            if authorized else
            "未授权「设置比赛标识」，无法自动或手动应用比赛标识；请在插件管理器的控制授权中授权。")
        self._update_condition_visibility()
        self._update_actions()

    def _update_condition_visibility(self):
        self.permission_label.setVisible(not self._authorized)
        self.condition_button.setVisible(bool(self._condition_details))
        self.condition_panel.setVisible(not self._authorized or bool(self._condition_details))

    def set_auto_manage(self, enabled: bool):
        self.auto_manage_checkbox.blockSignals(True)
        self.auto_manage_checkbox.setChecked(enabled)
        self.auto_manage_checkbox.blockSignals(False)

    def set_auto_upload(self, enabled: bool):
        self.auto_upload_checkbox.blockSignals(True)
        self.auto_upload_checkbox.setChecked(enabled)
        self.auto_upload_checkbox.blockSignals(False)
        self._update_actions()

    def _open_upload_history(self):
        self.upload_dialog.show()
        self.upload_dialog.raise_()
        self.upload_history_requested.emit()

    def choose_manual_upload(self, choices: list[tuple[str, tuple[str, ...]]]):
        if not choices:
            QMessageBox.information(self, "手动上传", "当前没有可手动上传的本地比赛录像。"
                                    "需要已打卡且仍在个人上传时间内的正式公平标准模式胜局。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("手动上传比赛录像")
        dialog.setWindowIcon(ui_icon("upload"))
        dialog.resize(680, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("选择要提交到 OpenMS 的本地比赛录像。"))

        headers = ("本地记录", "难度", "用时（秒）", "3BV", "比赛")
        table = QTableWidget(len(choices), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        for row, (digest, values) in enumerate(choices):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, digest)
                if column == len(headers) - 1:
                    item.setToolTip(value)
                table.setItem(row, column, item)
        layout.addWidget(table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = icon_button("取消", "cancel")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        submit = icon_button("加入上传队列", "upload")
        submit.setEnabled(False)
        submit.setDefault(True)
        submit.clicked.connect(dialog.accept)
        table.itemSelectionChanged.connect(lambda: submit.setEnabled(bool(table.selectedItems())))
        buttons.addWidget(submit)
        layout.addLayout(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = table.selectedItems()
            if selected:
                digest = table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
                self.manual_replay_selected.emit(digest)
        dialog.deleteLater()

    @property
    def upload_notice_message(self) -> str:
        return self._upload_notice.text()

    def show_upload_notice(self, message: str):
        self._upload_notice.show_message(message)

    def _analysis_hint(self, tournament: Tournament | None) -> str:
        if tournament is None or not tournament.is_ongoing():
            return "比赛进行中可查看我的成绩。"
        if not supports_scoring(tournament):
            return "暂不支持这场比赛的成绩分析。"
        if self._login_user is None:
            return "登录 OpenMS 后可查看我的成绩。"
        if self._busy or self._auth_busy:
            return "请等待当前请求完成。"
        return ""

    def _update_analysis_button(self, row: int, tournament: Tournament | None):
        button = self.table.cellWidget(row, ANALYSIS_COLUMN)
        if button is not None:
            hint = self._analysis_hint(tournament)
            button.setEnabled(not hint)
            button.setToolTip(hint or "查看本场比赛的当前成绩和本地预计成绩。")

    def _open_analysis(self, tournament_id: int):
        tournament = self._tournaments.get(tournament_id)
        user = self._login_user
        if tournament is None or user is None or self._analysis_hint(tournament):
            return
        self.begin_analysis(tournament, user.id)
        self.analysis_dialog.show()
        self.analysis_dialog.raise_()
        self.analysis_requested.emit(tournament_id)

    def begin_analysis(self, tournament: Tournament, user_id: int):
        self.analysis_dialog.begin(tournament, user_id)

    def show_analysis(self, result):
        self.analysis_dialog.show_analysis(result)

    def show_analysis_error(self, message: str):
        self.analysis_dialog.show_error(message)

    def show_replay_status(self, message: str, error: bool = False):
        self.analysis_dialog.show_replay_status(message, error)

    def show_scores(self, scores: tuple[PersonalAnalysis, ...]):
        self._personal_scores = {item.tournament.id: item for item in scores
                                 if self._login_user and item.user_id == self._login_user.id}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                self._update_score_row(row, item.data(Qt.ItemDataRole.UserRole))

    def _update_score_row(self, row: int, tournament_id: int):
        tournament = self._tournaments.get(tournament_id)
        score = self._personal_scores.get(tournament_id) if tournament and tournament.is_ongoing() else None
        unit = " 秒" if tournament and tournament.subclass != "e" else ""
        current = score.total if score and not score.website_error else None
        estimated = score.estimated_total if score else None
        for column, value in ((7, current), (8, estimated)):
            item = self.table.item(row, column)
            if item is not None:
                item.setText(f"{value:.3f}{unit}" if value is not None else "—")

    def clear_analysis(self):
        self.analysis_dialog.clear()

    def set_upload_records(self, records: list[dict]):
        selected_id = None
        selected_row = self.upload_table.currentRow()
        if selected_row >= 0 and self.upload_table.item(selected_row, 2) is not None:
            selected_id = self.upload_table.item(selected_row, 2).text()
        statuses = {"pending": "待上传", "uploading": "上传中", "retry": "等待重试",
                    "failed": "失败", "expired": "已过期", "succeeded": "上传成功", "duplicate": "网站已存在"}
        pending = sum(record["status"] in ("pending", "retry", "uploading") for record in records)
        self.upload_history_button.setText(f"上传记录（待传 {pending}）" if pending else "上传记录")
        self.upload_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = (format_time(datetime.fromtimestamp(record["created"], timezone.utc)),
                      str(record["user_id"]), record["filename"], statuses.get(record["status"], record["status"]),
                      str(record["video_id"]) if record["video_id"] else "—", record["message"])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.upload_table.setItem(row, column, item)
            if record["filename"] == selected_id:
                self.upload_table.selectRow(row)

    def show_game_context(self, context):
        if context is None or not getattr(context, "success", False):
            self._condition_details = []
            self._update_condition_visibility()
            self.set_current_identifier(None)
            return
        self.set_current_identifier(getattr(context, "race_identifier", "") or "")
        mode_ok = getattr(context, "mode", None) == 0
        fair = getattr(context, "is_fair", None)
        constrained = bool(getattr(context, "board_constraint", False))
        permissions = getattr(context, "permissions", None)
        controls = list(getattr(permissions, "allowed_controls", []) or [])
        status = str(getattr(context, "game_status", "") or "未知")
        reasons = []
        details = []
        # ready 状态下主进程的 is_fair 尚未代表一局实际成绩，不显示为不公平。
        if status != "ready" and fair is False:
            reasons.append("当前对局不公平")
            details.append(
                "原因：主进程判定当前对局不公平。\n"
                "处理：结束当前对局，关闭会影响公平性的辅助或控制设置，再重新开始一局。")
        if not mode_ok:
            reasons.append("非标准模式")
            details.append(
                f"原因：当前模式为“{getattr(context, 'mode_name', '') or context.mode}”，不是标准模式。\n"
                "处理：切换到标准模式，并重新开始一局。")
        if constrained:
            reasons.append("局面约束已启用")
            details.append(
                "原因：启用了预设局面约束。\n"
                "处理：关闭局面约束，并重新开始一局。")
        if controls:
            reasons.append("作弊控制已启用")
            details.append(
                "原因：高级设置开放了控制项：" + ", ".join(controls) + "。\n"
                "处理：在高级设置中关闭这些控制项，并重新开始一局。")
        if not reasons:
            self._condition_details = []
            self._update_condition_visibility()
            return
        self._condition_details = details
        self.condition_button.setText(
            "⚠ " + (reasons[0] if len(reasons) == 1 else "对局条件异常")
            + " · 点击查看原因和处理方案")
        self._update_condition_visibility()

    def _show_condition_details(self):
        if self._condition_details:
            QMessageBox.warning(
                self, "比赛条件检查",
                "检测到以下问题：\n\n" + "\n\n".join(self._condition_details),
            )

    def _checkin_candidates(self):
        return [tournament for tournament in self._tournaments.values()
                if tournament.is_ongoing() and self._participants.get(tournament.id) is None]

    def set_checkin_all_busy(self, busy: bool):
        self._checkin_all_busy = busy
        self._update_actions()

    def clear_token(self, message: str = ""):
        self._token = ""
        self._can_apply = False
        if message:
            self.window_label.setText(message)
        self._update_actions()

    def set_current_identifier(self, identifier: str | None):
        self._identifier_available = identifier is not None
        self._current_identifier = identifier or ""
        if identifier is None:
            self.current_identifier_edit.setText("无法读取主程序标识")
        else:
            self.current_identifier_edit.setText(identifier or "（空）")
        self._update_actions()

    def set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_actions()

    def show_snapshot(self, snapshot: Snapshot):
        selected_id = None
        row_current = self.table.currentRow()
        if row_current >= 0 and self.table.item(row_current, 0) is not None:
            selected_id = self.table.item(row_current, 0).data(Qt.ItemDataRole.UserRole)
        self._participants = dict(snapshot.participants)
        self._tournaments = {item.id: item for item in snapshot.tournaments}
        if self.analysis_dialog.tournament_id not in self._tournaments:
            self.clear_analysis()
        self._has_participant_identity = snapshot.user_id is not None
        self._checkin_busy.clear()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for tournament in snapshot.tournaments:
                row = self.table.rowCount()
                self.table.insertRow(row)
                phase = "进行中" if tournament.phase == "ongoing" else "待开始"
                attendance = self._participants.get(tournament.id)
                texts = (phase, f"{tournament.title}  ·  #{tournament.id}",
                         self._participant_token_text(attendance), "", "",
                         format_time(tournament.starts_at), format_time(tournament.ends_at), "—", "—")
                for column, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, tournament.id)
                    self.table.setItem(row, column, item)
                analysis_button = icon_button("我的成绩", "score")
                analysis_button.clicked.connect(lambda _checked=False, item_id=tournament.id: self._open_analysis(item_id))
                self.table.setCellWidget(row, ANALYSIS_COLUMN, analysis_button)
                token_item = self.table.item(row, 2)
                if attendance is not None and attendance.token:
                    token_item.setToolTip(f"比赛标识：{attendance.token}\n双击复制。")
                elif attendance is not None:
                    token_item.setToolTip("比赛标识会在个人参赛时间开始后公开。")
                cell = QWidget(self.table)
                cell_layout = QHBoxLayout(cell)
                cell_layout.setContentsMargins(4, 0, 4, 0)
                can_check_in = tournament.is_ongoing()
                status_text = (self._attendance_text(attendance)
                               if self._has_participant_identity and can_check_in else "—")
                status = QLabel(status_text)
                status.setObjectName("attendanceStatus")
                cell_layout.addWidget(status)
                checkin = icon_button("打卡", "confirm")
                checkin.setObjectName("checkInButton")
                checkin.setProperty("tournament_id", tournament.id)
                checkin.setVisible(can_check_in and attendance is None)
                checkin.setEnabled(can_check_in and self._login_user is not None and self._has_participant_identity)
                checkin.setToolTip("登录 OpenMS 后可打卡；打卡将开始个人参赛计时。")
                checkin.clicked.connect(lambda _checked=False, item_id=tournament.id: self._confirm_checkin(item_id))
                cell_layout.addWidget(checkin)
                self.table.setCellWidget(row, 3, cell)
                self._update_countdown_row(row, tournament.id)
                self._update_score_row(row, tournament.id)
        finally:
            self.table.blockSignals(False)
        if selected_id is not None:
            selected_row = self._row_for_tournament(selected_id)
            if selected_row >= 0:
                self.table.blockSignals(True)
                self.table.selectRow(selected_row)
                self.table.blockSignals(False)
        self.clear_token("选中比赛查看个人参赛时间。")
        self.set_busy(False, f"已刷新 {len(snapshot.tournaments)} 场比赛 · {format_time(snapshot.fetched_at)}"
                      if snapshot.tournaments else "当前没有进行中或待开始的比赛。")

    def show_participant(self, tournament: Tournament, participant: Participant | None):
        self._participants[tournament.id] = participant
        self._update_attendance_row(tournament.id, participant)
        if participant is None:
            self.clear_token("该用户尚未报名这场比赛，请确认用户 ID，或在 OpenMS 网站报名后刷新。")
        else:
            self._token = participant.token
            row = self._row_for_tournament(tournament.id)
            if row >= 0:
                token_item = self.table.item(row, 2)
                if token_item is not None:
                    token_item.setText(self._participant_token_text(participant))
                    token_item.setToolTip(
                        f"比赛标识：{participant.token}\n双击复制。" if participant.token
                        else "比赛标识会在个人参赛时间开始后公开。")
            self._can_apply = tournament.is_ongoing() and participant.is_active()
            window = f"个人参赛时间：{format_time(participant.starts_at)} 至 {format_time(participant.ends_at)}"
            if not participant.token:
                window += "。比赛标识尚未公开。"
            elif not self._can_apply:
                window += "。当前不在有效参赛时间内。"
            self.window_label.setText(window)
        self.set_busy(False, "参赛信息已更新。")

    def show_error(self, message: str):
        self.clear_token()
        self.set_busy(False, message)

    @staticmethod
    def _attendance_text(participant: Participant | None) -> str:
        return "已打卡" if participant is not None else "未打卡"

    @staticmethod
    def _participant_token_text(participant: Participant | None) -> str:
        if participant is None:
            return "—"
        return participant.token or "未公开"

    def _row_for_tournament(self, tournament_id: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == tournament_id:
                return row
        return -1

    def _update_attendance_row(self, tournament_id: int, participant: Participant | None):
        row = self._row_for_tournament(tournament_id)
        if row < 0:
            return
        token_item = self.table.item(row, 2)
        if token_item is not None:
            token_item.setText(self._participant_token_text(participant))
            token_item.setToolTip(
                f"比赛标识：{participant.token}\n双击复制。" if participant and participant.token
                else "比赛标识会在个人参赛时间开始后公开。" if participant else "")
        cell = self.table.cellWidget(row, 3)
        if cell is None:
            return
        status = cell.findChild(QLabel, "attendanceStatus")
        button = cell.findChild(QPushButton, "checkInButton")
        tournament = self._tournaments.get(tournament_id)
        if status:
            status.setText(self._attendance_text(participant)
                           if self._has_participant_identity and tournament and tournament.is_ongoing() else "—")
        if button:
            can_check_in = bool(tournament and tournament.is_ongoing() and self._has_participant_identity)
            button.setVisible(can_check_in and participant is None)
            button.setEnabled(can_check_in and participant is None and tournament_id not in self._checkin_busy
                              and self._login_user is not None and not self._busy and not self._auth_busy)
        self._update_countdown_row(row, tournament_id)

    def _update_countdown_row(self, row: int, tournament_id: int):
        item = self.table.item(row, 4)
        if item is None:
            return
        participant = self._participants.get(tournament_id)
        if participant is None or participant.ends_at is None:
            text = "—"
        else:
            remaining = (participant.ends_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                text = "已结束"
            else:
                seconds = int(remaining)
                hours, seconds = divmod(seconds, 3600)
                minutes, seconds = divmod(seconds, 60)
                text = f"{hours:02}:{minutes:02}:{seconds:02}"
        item.setText(text)

    def _update_countdowns(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                tournament_id = item.data(Qt.ItemDataRole.UserRole)
                self._update_countdown_row(row, tournament_id)
                self._update_score_row(row, tournament_id)
                self._update_analysis_button(row, self._tournaments.get(tournament_id))

    def set_checkin_busy(self, tournament_id: int, busy: bool):
        if busy:
            self._checkin_busy.add(tournament_id)
        else:
            self._checkin_busy.discard(tournament_id)
        participant = self._participants.get(tournament_id)
        self._update_attendance_row(tournament_id, participant)

    def _confirm_check_in_all(self):
        if self._login_user is None or self._busy or self._auth_busy or self._checkin_all_busy:
            return
        tournaments = self._checkin_candidates()
        if not tournaments:
            self.status_label.setText("当前没有尚未打卡且可参加的进行中比赛。")
            return
        names = "\n".join(f"• {item.title}" for item in tournaments)
        answer = QMessageBox.question(
            self, "打卡所有可参加比赛",
            f"本次将打卡 {len(tournaments)} 场比赛：\n{names}\n\n"
            "打卡会立即开始每场比赛的个人参赛计时，部分比赛的录像上传窗口有限且不能撤回。"
            "完成后会尝试设置当前有效的比赛标识。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.check_in_all_requested.emit()

    def _confirm_checkin(self, tournament_id: int):
        if self._login_user is None or self._busy or self._auth_busy:
            return
        answer = QMessageBox.question(
            self, "开始比赛打卡",
            "打卡会立即开始个人参赛计时。周赛和效率周赛通常有两小时录像上传时间，开始后无法撤回。现在继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.check_in_requested.emit(tournament_id)

    def _select(self):
        row = self.table.currentRow()
        if row >= 0:
            item = self.table.item(row, 0)
            self.clear_token("正在读取参赛信息…")
            self.tournament_selected.emit(item.data(Qt.ItemDataRole.UserRole))

    def _copy(self):
        if self.copy_button.isEnabled():
            QApplication.clipboard().setText(self._current_identifier)
            self.status_label.setText("已复制当前总比赛标识。")

    def _on_row_double_clicked(self, item: QTableWidgetItem):
        if item.column() == 2 and item.text() not in ("", "—", "未公开"):
            QApplication.clipboard().setText(item.text())
            self.status_label.setText("已复制该场比赛标识。")
            return
        self._open_analysis(item.data(Qt.ItemDataRole.UserRole))
