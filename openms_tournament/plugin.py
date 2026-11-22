"""宿主插件适配；OpenMS 请求不使用生成客户端或服务插件。"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from sqlite3 import Error as SQLiteError
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtNetwork import QLocalSocket

from plugin_sdk import BasePlugin, BoolConfig, IntConfig, OtherInfoBase, PluginInfo, TextConfig, WindowMode
from shared_types import (
    GameStatusChangeEvent, GetGameContextQuery,
    GameContextResult, SetRaceIdentifierCommand, GameBoardState,
)
from shared_types.events import GameFinishedEvent

from .client import LoginUser, OpenmsClient, OpenmsError, Participant, Snapshot, Tournament, UploadError
from .uploads import UploadJob, UploadQueue
from .analysis import LEVEL_NAMES, STATE_NAMES, PersonalAnalysis, ScoreVideo, analyze_personal, estimate_local, supports_scoring
from .request_policy import RateLimitError
from .widgets import TournamentWidget


class TournamentConfig(OtherInfoBase):
    user_id = IntConfig(
        0, "OpenMS 用户 ID", min_value=0, max_value=2_147_483_647,
        description="OpenMS 个人主页对应的数字用户 ID；0 表示未配置，仍可浏览比赛。",
    )
    timeout_seconds = IntConfig(8, "请求超时（秒）", min_value=3, max_value=30)
    username = TextConfig("", "OpenMS 用户名", description="登录后自动保存，方便下次填写。")
    login_expiry_days = IntConfig(
        30, "保持登录天数", min_value=7, max_value=90,
        description="OpenMS 支持 7–90 天，默认 30 天。",
    )
    auto_manage_identifier = BoolConfig(
        True, "自动维护比赛标识",
        description="每 5 分钟检查已打卡比赛，并在主程序待开局时自动设置有效标识。",
    )
    auto_upload = BoolConfig(
        False, "自动上传比赛录像",
        description="上传开启后完成的公平正式标准模式比赛胜局；网站已存在时丢弃待上传副本。",
    )


class OpenmsTournamentPlugin(BasePlugin[TournamentConfig]):
    _finished = Signal(int, str, object, object)
    _auth_finished = Signal(int, str, object, object)
    _auto_check_interval_ms = 5 * 60 * 1000

    @classmethod
    def plugin_info(cls) -> PluginInfo:
        return PluginInfo(
            name="openms_tournament", display_name="OpenMS 比赛", version="1.12.4",
            description="查看比赛、批量打卡、维护比赛标识、检查对局条件、上传录像并分析本人比赛成绩。",
            window_mode=WindowMode.TAB, other_info=TournamentConfig,
            icon=QIcon(str(Path(__file__).with_name("icon.svg"))),
            required_controls=[SetRaceIdentifierCommand],
        )

    def __init__(self, info: PluginInfo):
        super().__init__(info)
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future | None = None
        self._epoch = 0
        self._stopping = True
        self._snapshot: Snapshot | None = None
        self._selected: Tournament | None = None
        self._participant: Participant | None = None
        self._http: OpenmsClient | None = None
        self._login_user: LoginUser | None = None
        self._captcha_key = ""
        self._auth_epoch = 0
        self._auth_busy = False
        self._auth_future: Future | None = None
        self._checkin_id: int | None = None
        self._checkin_all_busy = False
        self._refresh_selection_id: int | None = None
        self._explicit_apply_all_pending = False
        self._auto_retry_after = 0.0
        self._latest_game_context: GameContextResult | None = None
        self._network_kind: str | None = None
        self._poll_timer: QTimer | None = None
        self._context_poll_timer: QTimer | None = None
        self._context_debounce: QTimer | None = None
        self._upload_timer: QTimer | None = None
        self._uploads: UploadQueue | None = None
        self._upload_current: UploadJob | None = None
        self._upload_after = 0.0
        self._upload_record_cache: list[dict] | None = None
        self._analysis_target: tuple[int, int] | None = None
        self._scores: dict[int, PersonalAnalysis] = {}
        self._replay_directory: TemporaryDirectory | None = None
        self._pending_upload_notice = ""
        self._finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._auth_finished.connect(self._on_auth_finished, Qt.ConnectionType.QueuedConnection)
        self.config_changed.connect(self._config_changed)

    def _setup_subscriptions(self):
        self.subscribe(GameStatusChangeEvent, self._on_game_status_change)
        self.subscribe(GameFinishedEvent, self._on_game_finished)

    def _create_widget(self):
        widget = TournamentWidget(
            self._login_user.id if self._login_user else self.other_info.user_id,
            username=self.other_info.username,
            auto_manage=self.other_info.auto_manage_identifier,
            auto_upload=self.other_info.auto_upload,
        )
        widget.refresh_requested.connect(self._refresh)
        widget.user_id_saved.connect(self._save_user_id)
        widget.tournament_selected.connect(self._select_tournament)
        widget.apply_requested.connect(self._apply)
        widget.captcha_requested.connect(self._refresh_captcha)
        widget.login_requested.connect(self._login)
        widget.login_check_requested.connect(self._check_login)
        widget.logout_requested.connect(self._logout)
        widget.check_in_requested.connect(self._check_in)
        widget.check_in_all_requested.connect(self._check_in_all)
        widget.auto_manage_changed.connect(self._set_auto_manage_identifier)
        widget.username_saved.connect(self._save_username)
        widget.auto_upload_changed.connect(self._set_auto_upload)
        widget.upload_history_requested.connect(self._show_upload_records)
        widget.manual_upload_requested.connect(self._show_manual_uploads)
        widget.manual_replay_selected.connect(self._queue_manual_upload)
        widget.retry_uploads_requested.connect(self._retry_uploads)
        widget.analysis_requested.connect(self._analyze_personal)
        widget.replay_requested.connect(self._open_personal_replay)
        widget.show_login_user(self._login_user)
        widget.set_auth_busy(self._auth_busy)
        widget.set_authorized(self.has_control_auth(SetRaceIdentifierCommand))
        if self._uploads is not None:
            widget.set_upload_records(self._uploads.records())
        widget.show_scores(tuple(self._scores.values()))
        if self._pending_upload_notice:
            widget.show_upload_notice(self._pending_upload_notice)
            self._pending_upload_notice = ""
        return widget

    def on_widget_created(self, widget):
        self._enqueue_event(lambda _: (self._refresh(), self._refresh_game_context()), None)

    def on_widget_closed(self, widget):
        if isinstance(widget, TournamentWidget) and not self._stopping:
            self._pending_upload_notice = widget.upload_notice_message

    def on_initialized(self):
        self._stopping = False
        self._future = self._auth_future = None
        self._upload_current = None
        self._upload_after = 0.0
        self._upload_record_cache = None
        self._scores.clear()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openms-tournament")
        self._uploads = UploadQueue(Path(self.data_dir) / "uploads.sqlite3")
        self._upload_timer = QTimer(self)
        self._upload_timer.setInterval(1000)
        self._upload_timer.timeout.connect(self._process_upload_queue)
        self._upload_timer.start()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._auto_check_interval_ms)
        self._poll_timer.timeout.connect(self._on_poll_timeout)
        self._poll_timer.start()
        self._context_debounce = QTimer(self)
        self._context_debounce.setSingleShot(True)
        self._context_debounce.setInterval(250)
        self._context_debounce.timeout.connect(self._on_context_debounce)
        self._context_poll_timer = QTimer(self)
        self._context_poll_timer.setInterval(3000)
        self._context_poll_timer.timeout.connect(self._refresh_game_context)
        self._context_poll_timer.start()
        if self.other_info.login_expiry_days < 7:
            self.other_info.login_expiry_days = 7
            self.save_config()
        self._submit_auth("restore", self._restore_session)

    @property
    def _cookie_path(self) -> Path:
        return Path(self.data_dir) / "openms-session.bin"

    def _restore_session(self):
        client = self._client_for_request()
        if not client.load_cookies(self._cookie_path):
            return None
        user = client.current_user()
        if user is None:
            client.delete_cookies(self._cookie_path)
        else:
            try:
                client.save_cookies(self._cookie_path)
            except OpenmsError as exc:
                return user, str(exc)
        return user

    def on_shutdown(self):
        self._stopping = True
        self._pending_upload_notice = ""
        if self._upload_timer is not None:
            self._upload_timer.stop()
            self._upload_timer = None
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        if self._context_debounce is not None:
            self._context_debounce.stop()
            self._context_debounce = None
        if self._context_poll_timer is not None:
            self._context_poll_timer.stop()
            self._context_poll_timer = None
        self._epoch += 1
        self._auth_epoch += 1
        if self._future:
            self._future.cancel()
        if self._auth_future:
            self._auth_future.cancel()
        replay_directory, self._replay_directory = self._replay_directory, None
        if self._executor:
            # close 排在尚未完成的网络操作后，避免主线程等待或跨线程关闭 Session。
            if self._http is not None:
                self._executor.submit(self._http.close)
            if replay_directory is not None:
                self._executor.submit(replay_directory.cleanup)
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None
        else:
            if self._http is not None:
                self._http.close()
            if replay_directory is not None:
                replay_directory.cleanup()
        self._http = None
        self._login_user = None
        self._captcha_key = ""
        self._auth_busy = False
        self._snapshot = self._selected = self._participant = None

    def _ui(self, method: str, *args):
        self.run_on_widget(lambda widget: getattr(widget, method)(*args))

    def _client_for_request(self):
        if self._http is None:
            self._http = OpenmsClient(timeout=float(self.other_info.timeout_seconds))
        self._http.timeout = float(self.other_info.timeout_seconds)
        return self._http

    def _submit_auth(self, kind: str, work: Callable):
        if (self._stopping or self._executor is None or self._auth_busy
                or self._future is not None):
            if self._future is not None:
                self._ui("set_auth_busy", False, "请等待当前比赛请求完成后再操作登录。")
            return
        self._auth_busy = True
        self._auth_epoch += 1
        epoch = self._auth_epoch
        self._epoch += 1
        if self._future:
            self._future.cancel()
        self._snapshot = self._selected = self._participant = None
        self._analysis_target = None
        self._scores.clear()
        self._ui("clear_analysis")
        self._ui("show_scores", ())
        self._ui("clear_token")
        self._ui("set_auth_busy", True, {
            "captcha": "正在加载图片验证码…", "login": "正在登录…",
            "check": "正在验证登录会话…", "logout": "正在退出登录…",
            "restore": "正在恢复并验证登录会话…",
            "save": "正在保存登录会话到系统凭据库…",
        }[kind])
        self._auth_future = self._executor.submit(work)

        def done(future):
            if future.cancelled() or self._stopping:
                return
            try:
                result, error = future.result(), None
            except Exception as exc:
                result, error = None, exc
            try:
                self._auth_finished.emit(epoch, kind, result, error)
            except RuntimeError:
                pass

        self._auth_future.add_done_callback(done)

    @Slot()
    def _refresh_captcha(self):
        if self._stopping or self._auth_busy or self._login_user:
            return
        self._captcha_key = ""
        self._ui("invalidate_captcha")
        self._submit_auth("captcha", self._client_for_request().captcha)

    @Slot(str, str, str)
    def _login(self, username: str, password: str, captcha: str):
        if self._stopping or self._auth_busy or self._login_user:
            return
        key, self._captcha_key = self._captcha_key, ""
        self._ui("invalidate_captcha")
        client = self._client_for_request()
        expiry_days = self.other_info.login_expiry_days
        self._save_username(username)

        def login_and_store():
            user = client.login(username, password, captcha, key, expiry_days=expiry_days)
            try:
                client.save_cookies(self._cookie_path)
                return user, ""
            except OpenmsError as exc:
                return user, str(exc)

        self._submit_auth("login", login_and_store)

    @Slot()
    def _check_login(self):
        if self._stopping or self._auth_busy:
            return
        client = self._client_for_request()

        def check_and_store():
            user = client.current_user()
            if user is None:
                try:
                    client.delete_cookies(self._cookie_path, strict=True)
                except OpenmsError as exc:
                    return None, str(exc)
            else:
                try:
                    client.save_cookies(self._cookie_path)
                except OpenmsError as exc:
                    return user, str(exc)
            return user, ""

        self._submit_auth("check", check_and_store)

    @Slot()
    def _logout(self):
        if self._stopping or self._auth_busy:
            return
        client = self._client_for_request()

        def logout_and_forget():
            try:
                client.logout()
            finally:
                client.delete_cookies(self._cookie_path, strict=True)

        self._submit_auth("logout", logout_and_forget)

    @Slot(int)
    def _check_in(self, tournament_id: int):
        if (self._stopping or self._auth_busy or self._snapshot is None
                or self._snapshot.user_id is None or self._login_user is None):
            return
        if self._future is not None:
            self._ui("show_error", "当前请求尚未完成，请稍后再试。")
            return
        tournament = next((item for item in self._snapshot.tournaments if item.id == tournament_id), None)
        if tournament is None or not tournament.is_ongoing():
            self._ui("show_error", "该比赛已经结束或找不到，请刷新。")
            return
        if self._snapshot.participant_for(tournament_id) is not None or self._checkin_id is not None:
            self._ui("show_error", "这场比赛已经打卡。")
            return
        self._selected = tournament
        self._participant = None
        self._checkin_id = tournament_id
        self._ui("set_checkin_busy", tournament_id, True)
        client = self._client_for_request()
        user_id = self._snapshot.user_id
        self._submit("checkin", lambda: client.check_in(tournament, user_id))

    @Slot()
    def _check_in_all(self):
        if (self._stopping or self._auth_busy or self._snapshot is None
                or self._snapshot.user_id is None or self._login_user is None):
            self._ui("show_error", "请先登录 OpenMS 并刷新当前比赛。")
            return
        if self._checkin_all_busy or self._future is not None:
            self._ui("show_error", "当前请求尚未完成，请稍后再试。")
            return
        candidates = tuple(
            tournament for tournament in self._snapshot.tournaments
            if tournament.is_ongoing() and self._snapshot.participant_for(tournament.id) is None
        )
        if not candidates:
            self._ui("show_error", "当前没有尚未打卡且可参加的进行中比赛。")
            return
        user_id = self._snapshot.user_id
        self._checkin_all_busy = True
        self._ui("set_checkin_all_busy", True)
        client = self._client_for_request()
        self._submit("checkin_all", lambda: self._check_in_many(client, candidates, user_id))

    @staticmethod
    def _check_in_many(client: OpenmsClient, tournaments: tuple[Tournament, ...], user_id: int):
        succeeded: list[tuple[Tournament, Participant]] = []
        failed: list[tuple[Tournament, str]] = []
        for index, tournament in enumerate(tournaments):
            try:
                participant = client.check_in(tournament, user_id)
                if participant is None:
                    failed.append((tournament, "OpenMS 未返回参赛记录。"))
                else:
                    succeeded.append((tournament, participant))
            except RateLimitError as exc:
                failed.append((tournament, str(exc)))
                failed.extend((remaining, "因限流暂停后续打卡；冷却结束后可重新操作。")
                              for remaining in tournaments[index + 1:])
                break
            except Exception as exc:
                # POST 可能已生效，即使随后读取响应失败；查询一次，避免误报为未打卡。
                try:
                    participant = client.participant(tournament.id, user_id)
                except Exception:
                    participant = None
                if participant is not None:
                    succeeded.append((tournament, participant))
                else:
                    failed.append((tournament, str(exc) if isinstance(exc, OpenmsError)
                                   else "请求失败，请稍后检查打卡状态。"))
        return tuple(succeeded), tuple(failed)

    @Slot(int, str, object, object)
    def _on_auth_finished(self, epoch, kind, result, error):
        if self._stopping or epoch != self._auth_epoch:
            return
        self._auth_busy = False
        self._ui("set_auth_busy", False)
        self._ui("set_busy", False)
        if error is not None:
            self.logger.warning(f"OpenMS authentication {kind}: {type(error).__name__}")
            message = str(error) if isinstance(error, OpenmsError) else "登录服务请求失败，请稍后重试。"
            if kind == "logout":
                self._login_user = None
                self._ui("show_login_user", None, "登录状态已在插件中清除；凭据库清理或服务器退出未确认，请检查系统凭据库和 OpenMS 网站。")
            elif kind == "restore":
                self._ui("show_login_user", None, f"登录会话恢复失败：{message}")
                self._refresh()
            else:
                self._ui("set_auth_busy", False, message)
            if kind == "login":
                self._captcha_key = ""
                self._ui("invalidate_captcha")
            return
        if kind == "captcha":
            self._captcha_key = result.key
            self._ui("show_captcha", result)
            self._ui("set_auth_busy", False, "请输入图片验证码后登录；密码不会保存。")
            return
        persistence_warning = ""
        if kind in ("login", "check", "restore") and isinstance(result, tuple) and len(result) == 2:
            result, persistence_warning = result
        if kind in ("login", "check", "restore"):
            self._login_user = result
        elif kind == "logout":
            self._login_user = None
        self._captcha_key = ""
        if self._login_user and self.other_info.username != self._login_user.username:
            self.other_info.username = self._login_user.username
            self.save_config()
        if persistence_warning:
            self._ui("show_login_user", self._login_user, persistence_warning)
        else:
            self._ui("show_login_user", self._login_user)
        if self._login_user:
            if self.other_info.user_id != self._login_user.id:
                self.other_info.user_id = self._login_user.id
                self.save_config()
                return  # config_changed 会刷新列表。
        self._refresh()

    def _submit(self, kind: str, work: Callable):
        if self._stopping or self._executor is None or self._auth_busy:
            return False
        if self._future is not None:
            return False
        self._epoch += 1
        epoch = self._epoch
        self._network_kind = kind
        self._ui("set_busy", True, {
            "snapshot": "正在刷新比赛和打卡状态…",
            "checkin": "正在向 OpenMS 提交打卡…",
            "checkin_all": "正在逐场打卡；每场结果会分别确认…",
            "upload": "正在上传比赛录像…",
            "analysis": "正在读取我的比赛成绩…",
            "replay": "正在准备比赛录像…",
        }.get(kind, "正在核对参赛信息…"))
        self._future = self._executor.submit(work)

        def done(future):
            if future.cancelled() or self._stopping:
                return
            try:
                result, error = future.result(), None
            except Exception as exc:
                result, error = None, exc
            try:
                self._finished.emit(epoch, kind, result, error)
            except RuntimeError:
                pass  # 宿主可能已经销毁插件 QObject。

        self._future.add_done_callback(done)
        return True

    @Slot()
    def _refresh(self):
        if self._stopping or self._auth_busy:
            return
        if self._future is not None:
            self._ui("set_busy", True, "当前 OpenMS 请求尚未完成，请稍后刷新。")
            return
        self._refresh_selection_id = self._selected.id if self._selected else None
        self._participant = None
        self._ui("clear_token", "正在刷新比赛和打卡状态…")
        user_id = self._login_user.id if self._login_user else self.other_info.user_id
        client = self._client_for_request()
        include_scores = self._login_user is not None
        self._submit("snapshot", lambda: self._read_snapshot(client, user_id, include_scores))

    def _read_snapshot(self, client: OpenmsClient, user_id: int, include_scores: bool) -> Snapshot:
        snapshot = client.snapshot(user_id)
        if not include_scores:
            return snapshot
        analyses = []
        for tournament in snapshot.tournaments:
            participant = snapshot.participant_for(tournament.id)
            if tournament.is_ongoing() and supports_scoring(tournament) and participant is not None:
                analyses.append(self._read_personal_score(client, tournament, user_id))
        return replace(snapshot, analyses=tuple(analyses))

    def _read_personal_score(self, client: OpenmsClient, tournament: Tournament, user_id: int) -> PersonalAnalysis:
        try:
            videos = client.personal_videos(tournament.id, user_id)
            result = analyze_personal(tournament, user_id, videos)
        except OpenmsError as exc:
            return PersonalAnalysis(tournament, user_id, datetime.now(timezone.utc), (), (), website_error=str(exc))
        if self._uploads is not None:
            try:
                self._uploads.restore_legacy_scores(user_id, tournament.id, videos)
            except SQLiteError as exc:
                self.logger.warning(f"OpenMS 补录旧版战绩失败: {exc}")
        return result

    def _publish_scores(self, *, update_dialog: bool = True):
        if self._login_user is None or self._snapshot is None:
            self._scores.clear()
        else:
            active = {item.id for item in self._snapshot.tournaments if item.is_ongoing()}
            for tournament_id, score in tuple(self._scores.items()):
                if tournament_id not in active or score.user_id != self._login_user.id:
                    self._scores.pop(tournament_id)
                    continue
                if self._uploads is not None:
                    try:
                        wins = self._uploads.score_videos(score.user_id, tournament_id)
                    except SQLiteError:
                        self._scores[tournament_id] = replace(
                            score, estimated_groups=(), estimate_error="本地成绩暂时无法读取，请稍后重试。")
                    else:
                        self._scores[tournament_id] = replace(
                            score, estimated_groups=estimate_local(score.tournament, wins), estimate_error="")
        self._ui("show_scores", tuple(self._scores.values()))
        if update_dialog and self._analysis_target is not None:
            tournament_id, user_id = self._analysis_target
            score = self._scores.get(tournament_id)
            if score is not None and score.user_id == user_id:
                self._ui("show_analysis", score)

    @Slot(bool)
    def _set_auto_manage_identifier(self, enabled: bool):
        if self.other_info.auto_manage_identifier == enabled:
            return
        self.other_info.auto_manage_identifier = enabled
        self.save_config()
        if enabled:
            self._ui("set_busy", False, "自动维护已开启；正在检查当前比赛标识。")
            self._refresh_game_context()
            self._on_poll_timeout()
        else:
            self._ui("set_busy", False, "自动维护已关闭；不会再后台修改比赛标识。")

    @Slot(bool)
    def _on_poll_timeout(self):
        has_upload_records = self._uploads is not None and bool(self._uploads.records())
        if (self._stopping or self._auth_busy
                or not (self.other_info.auto_manage_identifier or self.other_info.auto_upload
                        or has_upload_records or self._login_user is not None)):
            return
        if self._future is not None:
            return
        self._refresh()
        self._refresh_game_context()

    @Slot(object)
    def _on_game_status_change(self, _event):
        self._schedule_context_refresh()

    @Slot(int)
    def _analyze_personal(self, tournament_id: int):
        if self._stopping:
            return
        if self._login_user is None:
            self._ui("show_analysis_error", "请先登录 OpenMS，只能分析当前登录账号的比赛成绩。")
            return
        if self._auth_busy or self._future is not None:
            self._ui("show_analysis_error", "当前请求尚未完成，请稍后点击刷新我的成绩。")
            return
        tournament = next((item for item in self._snapshot.tournaments if item.id == tournament_id), None) if self._snapshot else None
        if tournament is None or not tournament.is_ongoing() or not supports_scoring(tournament):
            self._ui("clear_analysis")
            self._ui("show_analysis_error", "该比赛已不在当前比赛范围内，请刷新比赛列表。")
            return
        user_id = self._login_user.id
        self._analysis_target = (tournament_id, user_id)
        self._ui("begin_analysis", tournament, user_id)
        client = self._client_for_request()
        if not self._submit("analysis", lambda: self._read_personal_score(client, tournament, user_id)):
            self._ui("show_analysis_error", "暂时无法开始查询，请稍后重试。")

    @Slot(object)
    def _open_personal_replay(self, video: ScoreVideo):
        if self._stopping or self._login_user is None or self._analysis_target is None:
            return
        if self._future is not None or self._auth_busy:
            self._ui("show_replay_status", "当前请求尚未完成，请稍后双击录像。", True)
            return
        try:
            if self._replay_directory is None:
                self._replay_directory = TemporaryDirectory(prefix="openms-replays-")
        except OSError:
            self._ui("show_replay_status", "无法创建本地录像临时目录。", True)
            return
        directory = Path(self._replay_directory.name)
        client = self._client_for_request()
        user_id = self._login_user.id
        queue = self._uploads

        def prepare():
            website_id = video.id
            raw = None
            if video.local_digest:
                raw, website_id = queue.local_replay(user_id, video.local_digest) if queue is not None else (None, None)
                if raw is None and website_id is None:
                    raise OpenmsError("这局保留了本地对局数据，但录像文件已清理或未保存。")
            if raw is None:
                name = f"openms-{website_id}"
                existing = next(directory.glob(f"{name}.*"), None)
                if existing is not None:
                    return existing, self._send_replay_to_host(existing)
                suffix, raw = client.download_replay(website_id)
            else:
                suffix, name = ".evf", f"local-{video.local_digest}"
            path = directory / f"{name}{suffix}"
            try:
                path.write_bytes(raw)
            except OSError:
                path.unlink(missing_ok=True)
                raise
            return path, self._send_replay_to_host(path)

        if self._submit("replay", prepare):
            self._ui("show_replay_status", "正在准备录像…")

    @staticmethod
    def _send_replay_to_host(path: Path) -> bool:
        socket = QLocalSocket()
        socket.connectToServer("MineSweeperServer")
        if not socket.waitForConnected(500):
            return False
        payload = str(path).encode("utf-8")
        if socket.write(payload) != len(payload):
            return False
        if socket.bytesToWrite() and not socket.waitForBytesWritten(500):
            return False
        if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            socket.waitForDisconnected(1500)
        return True

    @Slot(bool)
    def _set_auto_upload(self, enabled: bool):
        if self.other_info.auto_upload != enabled:
            self.other_info.auto_upload = enabled
            self.save_config()
        if enabled:
            self._refresh()

    def _tournament_tokens(self, *, for_upload: bool):
        now = datetime.now(timezone.utc)
        if (self._snapshot is None or self._login_user is None
                or self._snapshot.user_id != self._login_user.id):
            return {}
        return {participant.token: (tournament, participant)
                for tournament in self._snapshot.tournaments if tournament.is_ongoing(now)
                if (participant := self._snapshot.participant_for(tournament.id)) is not None
                and participant.user_id == self._login_user.id and participant.token
                and (not for_upload or participant.is_active(now))}

    def _upload_tokens(self):
        return self._tournament_tokens(for_upload=True)

    def _on_game_finished(self, event: GameFinishedEvent):
        if (self._stopping or self._login_user is None or self._uploads is None
                or event.game_state not in (GameBoardState.Win.value, GameBoardState.Jowin.value)):
            return
        identifiers = tuple(dict.fromkeys(token.strip() for token in event.race_identifier.split(',') if token.strip()))
        local_matches = self._tournament_tokens(for_upload=False)
        tournaments = {local_matches[token][0].id: local_matches[token][0]
                       for token in identifiers if token in local_matches}
        uploadable = (event.game_state == GameBoardState.Win.value and event.mode == 0
                      and event.is_fair and event.is_official and bool(event.raw_data))
        available = self._upload_tokens() if self.other_info.auto_upload and uploadable else {}
        tokens = tuple(token for token in identifiers if token in available)
        raw = bytes(event.raw_data) if event.raw_data else None
        level = {3: "b", 4: "i", 5: "e"}.get(event.level)
        timems = round(event.rtime * 1000) if isfinite(event.rtime) and event.rtime > 0 else None
        user_id = self._login_user.id
        try:
            if tournaments and level and timems is not None:
                end_times = [item.ends_at.timestamp() if item.ends_at is not None else None
                             for item in tournaments.values()]
                identity = raw or repr((event.start_time, event.end_time, event.mode, level, timems,
                                        event.bbbv, event.cl, identifiers)).encode("utf-8")
                if self._uploads.record_win(
                        user_id, sha256(identity).hexdigest(), tuple(tournaments),
                        None if None in end_times else max(end_times), level, timems, event.bbbv, event.cl,
                        None if tokens else raw, uploadable=uploadable):
                    for tournament in tournaments.values():
                        if supports_scoring(tournament) and tournament.id not in self._scores:
                            self._scores[tournament.id] = PersonalAnalysis(
                                tournament, user_id, datetime.now(timezone.utc), (), (),
                                website_error="网站成绩尚未读取，请刷新。")
                    self._publish_scores(update_dialog=self._network_kind not in ("analysis", "replay"))
            if not tokens or raw is None:
                return
            if self._enqueue_upload(user_id, raw, tokens, available, level=level, timems=timems,
                                    bv=event.bbbv, clicks=event.cl):
                self._show_upload_records()
                self._process_upload_queue()
        except Exception:
            self.logger.exception("Failed to save the OpenMS upload outbox")
            self._ui("set_busy", False, "无法保存比赛对局，请检查插件数据目录和磁盘空间。")

    def _enqueue_upload(self, user_id: int, raw: bytes, tokens: tuple[str, ...],
                        available: dict[str, tuple[Tournament, Participant]], *, level: str | None,
                        timems: int | None, bv: int, clicks: int | None, manual: bool = False) -> bool:
        if self._uploads is None:
            return False
        deadlines = []
        for token in tokens:
            tournament, participant = available[token]
            limits = [value.timestamp() for value in (tournament.ends_at, participant.ends_at) if value is not None]
            deadlines.append(min(limits) if limits else None)
        return self._uploads.enqueue(
            user_id, raw, tokens, None if None in deadlines else max(deadlines),
            tuple(dict.fromkeys(available[token][0].id for token in tokens)),
            level=level, timems=timems, bv=bv, clicks=clicks, manual=manual)

    @Slot()
    def _show_upload_records(self):
        if self._uploads is not None:
            records = self._uploads.records()
            if records != self._upload_record_cache:
                self._upload_record_cache = records
                self._ui("set_upload_records", records)

    @Slot()
    def _show_manual_uploads(self):
        if self._stopping or self._auth_busy or self._uploads is None or self._login_user is None:
            return
        active = {tournament.id: tournament.title for tournament, _ in self._upload_tokens().values()}
        try:
            rows = self._uploads.manual_candidates(self._login_user.id, set(active))
        except SQLiteError:
            self._ui("set_busy", self._future is not None, "读取本地比赛录像失败，请稍后重试。")
            return
        choices = []
        for row in rows:
            titles = "、".join(active[tournament_id] for tournament_id in row["tournament_ids"])
            level = LEVEL_NAMES.get(row["level"], "未知难度")
            values = (f"#{row['rowid']}", level, f"{row['timems'] / 1000:.3f}", str(row["bv"]), titles)
            choices.append((row["digest"], values))
        self._ui("choose_manual_upload", choices)

    @Slot(str)
    def _queue_manual_upload(self, digest: str):
        if self._stopping or self._auth_busy or self._uploads is None or self._login_user is None:
            return
        user_id = self._login_user.id
        available = self._upload_tokens()
        try:
            candidates = self._uploads.manual_candidates(
                user_id, {tournament.id for tournament, _ in available.values()})
            raw, _ = self._uploads.local_replay(user_id, digest)
        except SQLiteError:
            self._ui("set_busy", self._future is not None, "读取本地比赛录像失败，请稍后重试。")
            return
        selected = next((row for row in candidates if row["digest"] == digest), None)
        if selected is None:
            self._ui("set_busy", self._future is not None, "这份录像已入队、已上传或已超出参赛上传时间。")
            return
        if not raw:
            self._ui("set_busy", self._future is not None, "这份录像的原始文件已不可用。")
            return
        try:
            from ms_toollib import EvfVideo

            replay = EvfVideo("manual-upload.evf", raw)
            replay.parse()
            uploadable = replay.is_completed and replay.mode == 0 and replay.is_fair and replay.is_official
            identifiers = {identifier.strip() for identifier in replay.race_identifier.split(",")}
        except Exception as exc:
            self.logger.warning(f"OpenMS 手动上传无法读取 EVF: {type(exc).__name__}")
            self._ui("set_busy", self._future is not None, "无法读取这份 EVF 录像。")
            return
        if not uploadable:
            self._ui("set_busy", self._future is not None, "手动上传需要标准模式的正式公平胜局。")
            return
        available = self._upload_tokens()
        tokens = tuple(token for token, (tournament, _) in available.items()
                       if token in identifiers and tournament.id in selected["tournament_ids"])
        if not tokens:
            self._ui("set_busy", self._future is not None, "录像标识与当前有效参赛记录不匹配。")
            return
        try:
            queued = self._enqueue_upload(user_id, raw, tokens, available, manual=True, level=selected["level"],
                                          timems=selected["timems"], bv=selected["bv"], clicks=selected["clicks"])
        except SQLiteError:
            self._ui("set_busy", self._future is not None, "无法加入上传队列，请检查插件数据目录。")
            return
        if not queued:
            self._ui("set_busy", self._future is not None, "这份录像已经在上传队列中或已上传。")
            return
        self._ui("set_busy", self._future is not None, "已加入手动上传队列。")
        self._show_upload_records()
        self._process_upload_queue()

    @Slot()
    def _retry_uploads(self):
        if self._uploads is not None and self._login_user is not None and not self._auth_busy:
            self._uploads.retry_failed(self._login_user.id)
            self._process_upload_queue()
            self._show_upload_records()

    def _cleanup_uploads(self, snapshot: Snapshot | None = None):
        if self._uploads is None:
            return
        self._uploads.purge_expired()
        if snapshot is not None:
            now = datetime.now(timezone.utc)
            ids = {tournament.id for tournament in snapshot.tournaments
                   if tournament.phase in ("ongoing", "preparing")
                   and (tournament.ends_at is None or now < tournament.ends_at)}
            token_tournaments = {participant.token: tournament_id
                                 for tournament_id, participant in snapshot.participants
                                 if participant is not None and participant.user_id == snapshot.user_id}
            self._uploads.retain_tournaments(ids, user_id=snapshot.user_id,
                                             token_tournaments=token_tournaments,
                                             tournament_deadlines={item.id: item.ends_at.timestamp()
                                                                   for item in snapshot.tournaments
                                                                   if item.ends_at is not None})
        self._show_upload_records()

    @Slot()
    def _process_upload_queue(self):
        if self._stopping:
            return
        # Cleanup is independent of upload consent, login state and network availability.
        self._cleanup_uploads()
        if (self._stopping or self._login_user is None
                or self._auth_busy or self._future is not None or self._executor is None
                or self._uploads is None or monotonic() < self._upload_after or self._snapshot is None):
            return
        if self._snapshot.user_id != self._login_user.id:
            return
        if self._http is not None and self._http.cooldown_remaining() > 0:
            return
        job = self._uploads.next_job(self._login_user.id, include_auto=self.other_info.auto_upload)
        if job is None:
            self._show_upload_records()
            return
        if not set(job.tokens).intersection(self._upload_tokens()):
            self._uploads.discard(job)
            self._show_upload_records()
            return
        self._upload_current = job
        self._uploads.start(job)
        client = self._client_for_request()
        if not self._submit("upload", lambda: client.upload_video(job.raw_data, job.filename)):
            self._uploads.fail(job, "等待当前请求完成", retryable=True)
            self._upload_current = None
        self._show_upload_records()

    def _on_upload_finished(self, result, error):
        job, self._upload_current = self._upload_current, None
        self._upload_after = monotonic() + 1
        if job is None or self._uploads is None:
            return
        if error is not None:
            message = str(error) if isinstance(error, OpenmsError) else "上传请求失败，请稍后重试。"
            needs_login = isinstance(error, UploadError) and error.needs_login
            delay = error.retry_after if isinstance(error, UploadError) else None
            if delay is not None:
                self._upload_after = monotonic() + max(1, delay)
            self._uploads.fail(job, message, retryable=isinstance(error, UploadError) and error.retryable,
                               needs_login=needs_login, retry_after=delay,
                               rate_limited=isinstance(error, UploadError) and error.rate_limited)
            if needs_login:
                self._login_user = None
                self._snapshot = self._selected = self._participant = None
                self._analysis_target = None
                self._scores.clear()
                self._ui("show_login_user", None, message)
                self._ui("clear_token")
        elif result.status == "duplicate":
            message = "网站已存在该录像，已从待上传队列移除，不再重试。"
            self._uploads.finish(job, "duplicate", message)
        else:
            website_state = STATE_NAMES.get(result.state, result.state or "未返回")
            details = [f"录像 #{result.video_id}", "胜利"]
            if job.level in LEVEL_NAMES:
                details.insert(1, LEVEL_NAMES[job.level])
            if job.timems is not None:
                details.append(f"{job.timems / 1000:.3f} 秒")
            if job.bv is not None:
                details.append(f"3BV {job.bv}")
            details.append(f"网站状态：{website_state}")
            message = "比赛录像上传成功 · " + " · ".join(details)
            self._uploads.finish(job, "succeeded", message, video_id=result.video_id, state=result.state)
            self.run_on_gui(self._show_upload_notice, message)
            message = "上传记录已更新。"
        self._ui("set_busy", False, message)
        self._cleanup_uploads()

    def _show_upload_notice(self, message: str):
        if self._stopping:
            return
        widget = self.widget
        if isinstance(widget, TournamentWidget):
            widget.show_upload_notice(message)
        else:
            self._pending_upload_notice = message

    def _schedule_context_refresh(self):
        if self._context_debounce is not None and not self._stopping:
            self._context_debounce.start()

    @Slot()
    def _on_context_debounce(self):
        self._refresh_game_context()

    def _refresh_game_context(self):
        return self._read_game_context(auto_apply=True)

    def _read_game_context(self, *, auto_apply: bool) -> GameContextResult | None:
        if self._stopping:
            return None
        try:
            result = self.query(GetGameContextQuery(), timeout=3.0)
        except Exception as exc:
            self.logger.debug(f"OpenMS host context query: {type(exc).__name__}")
            result = None
        if isinstance(result, GameContextResult) and result.success:
            self._latest_game_context = result
            self._ui("show_game_context", result)
            if self._explicit_apply_all_pending:
                if getattr(result, "game_status", "") == "ready":
                    self._explicit_apply_all_pending = False
                    self._apply_all_tokens()
                else:
                    self._ui("set_busy", False, "打卡已完成；待游戏结束并回到待开局状态后设置比赛标识。")
            elif auto_apply and self.other_info.auto_manage_identifier:
                self._auto_apply_from_snapshot(result)
            return result
        self._latest_game_context = None
        self._ui("show_game_context", None)
        return None

    def _auto_apply_from_snapshot(self, context: GameContextResult | None = None):
        if (self._stopping or not self.other_info.auto_manage_identifier
                or self._snapshot is None or self._snapshot.user_id is None):
            return
        if self._future is not None:
            return
        context = context or self._latest_game_context
        if context is None:
            return
        active = self._active_from_snapshot(self._snapshot)
        if not active:
            return  # 不清除用户手动设置的标识。
        if not self.has_control_auth(SetRaceIdentifierCommand):
            self._ui("set_authorized", False)
            self._ui("set_busy", False, "比赛标识控制尚未授权，自动维护等待授权。")
            return
        if getattr(context, "game_status", "") != "ready":
            self._ui("set_busy", False, "有效比赛标识已就绪；游戏结束并回到待开局状态后自动应用。")
            return
        target = ",".join(item.token for item in active)
        current = str(getattr(context, "race_identifier", "") or "")
        if current == target:
            self._auto_retry_after = 0.0
            return
        if monotonic() < self._auto_retry_after:
            return
        self._auto_retry_after = monotonic() + 30.0
        response = self.request(SetRaceIdentifierCommand(race_identifier=target))
        if response is None:
            self._ui("set_busy", False, "自动设置未收到响应，请检查主程序连接和控制授权。")
        elif not response.success:
            self._ui("set_busy", False, response.error or "自动设置比赛标识失败。")
        else:
            self._auto_retry_after = 0.0
            self._ui("set_current_identifier", target)
            self._ui("set_busy", False, f"已自动设置 {len(active)} 场当前有效比赛的标识。")

    def _apply_all_tokens(self):
        if (self._stopping or self._auth_busy or self._snapshot is None
                or self._snapshot.user_id is None):
            self._ui("show_error", "请先设置 OpenMS 用户 ID 并刷新比赛。")
            return
        if not self.has_control_auth(SetRaceIdentifierCommand):
            self._ui("set_authorized", False)
            self._ui("show_error", "请先在插件管理器中授权「设置比赛标识」。")
            return
        if self._future is not None:
            self._ui("show_error", "当前请求尚未完成，请稍后再试。")
            return
        client = self._client_for_request()
        self._submit("apply_all", lambda: client.active_participants(
            self._snapshot.tournaments, self._snapshot.user_id))

    @staticmethod
    def _active_from_snapshot(snapshot: Snapshot) -> tuple[Participant, ...]:
        now_active = []
        for tournament in snapshot.tournaments:
            participant = snapshot.participant_for(tournament.id)
            if (participant is not None and tournament.is_ongoing()
                    and participant.is_active()):
                if "," in participant.token or any(character.isspace() for character in participant.token):
                    continue
                now_active.append(participant)
        return tuple(now_active)

    @Slot(int)
    def _save_user_id(self, user_id: int):
        if self._auth_busy or self._login_user:
            return
        if user_id == self.other_info.user_id:
            self._refresh()
            return
        self.other_info.user_id = user_id
        self.save_config()

    @Slot(str)
    def _save_username(self, username: str):
        username = username.strip()
        if username == self.other_info.username:
            return
        self.other_info.username = username
        self.save_config()

    @Slot(str, object)
    def _config_changed(self, name: str, _value):
        if name == "user_id":
            self._analysis_target = None
            self._scores.clear()
            self._ui("clear_analysis")
            self._ui("show_scores", ())
            self._snapshot = self._selected = self._participant = None
            self._ui("clear_token", "用户 ID 已更改，正在重新读取该账号的参赛状态。")
            self._ui("set_user_id", self._login_user.id if self._login_user else self.other_info.user_id)
        if name == "username":
            self._ui("set_username", self.other_info.username)
        if name == "auto_upload":
            self._ui("set_auto_upload", self.other_info.auto_upload)
            if self.other_info.auto_upload:
                self._refresh()
        if name == "auto_manage_identifier":
            self._ui("set_auto_manage", self.other_info.auto_manage_identifier)
            if self.other_info.auto_manage_identifier:
                self._refresh_game_context()
        if name == "login_expiry_days":
            client = self._client_for_request()
            if self.other_info.login_expiry_days < 7:
                self.other_info.login_expiry_days = 7
                self.save_config()
            elif self._login_user:
                self._ui("set_auth_busy", False, "保持登录时长将在下次登录时生效；请重新登录以更新会话有效期。")
        if name in ("user_id", "timeout_seconds"):
            self._refresh()

    @Slot(int)
    def _select_tournament(self, tournament_id: int):
        if (self._stopping or self._auth_busy or self._snapshot is None
                or self._future is not None):
            return
        self._selected = next((item for item in self._snapshot.tournaments if item.id == tournament_id), None)
        self._participant = None
        # 即便这次选择无需联网，也必须使上一次选择的后台结果失效。
        self._epoch += 1
        if self._future:
            self._future.cancel()
        if self._selected is None:
            self._ui("show_error", "找不到该比赛，请刷新。")
        elif not self._selected.is_ongoing():
            self._ui("show_error", "该比赛尚未开始或已经结束，无法设置比赛标识。")
        elif self._snapshot.user_id is None:
            self._ui("show_error", "请填写并保存 OpenMS 用户 ID，以读取本人的参赛标识。")
        elif tournament_id in dict(self._snapshot.participants):
            self._participant = self._snapshot.participant_for(tournament_id)
            self._ui("show_participant", self._selected, self._participant)
        else:
            client = self._client_for_request()
            user_id = self._snapshot.user_id
            self._submit("participant", lambda: client.participant(tournament_id, user_id))

    @Slot()
    def _apply(self):
        if self._stopping or self._auth_busy or self._future is not None:
            return
        if not self.has_control_auth(SetRaceIdentifierCommand):
            self._ui("set_authorized", False)
            self._ui("show_error", "请先在插件管理器中授权「设置比赛标识」。")
            return
        if not self._snapshot or not self._selected or not self._participant:
            self._ui("show_error", "请先选中已报名的进行中比赛。")
            return
        if self._snapshot.user_id is None:
            self._ui("show_error", "请填写并保存 OpenMS 用户 ID。")
            return
        if not self._selected.is_ongoing() or not self._participant.is_active():
            self._ui("show_error", "当前不在有效参赛时间内，请刷新。")
            return
        # 应用前重新获取，避免把已过期或被更换的参赛标识写入下一局。
        client = self._client_for_request()
        tournaments, user_id = self._snapshot.tournaments, self._snapshot.user_id
        self._submit("apply", lambda: client.active_participants(tournaments, user_id))

    @Slot(int, str, object, object)
    def _on_finished(self, epoch, kind, result, error):
        if self._stopping:
            return
        if epoch == self._epoch:
            self._future = None
            self._network_kind = None
        if epoch != self._epoch:
            if kind == "checkin" and self._checkin_id is not None:
                tournament_id, self._checkin_id = self._checkin_id, None
                self._ui("set_checkin_busy", tournament_id, False)
            return
        if kind == "upload":
            self._on_upload_finished(result, error)
            return
        if kind == "replay":
            if error is None:
                path, sent = result
                if sent or QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    message = "已交给本地播放器打开。"
                else:
                    message = "无法打开录像，请启动元扫雷后重试。"
                    error = OpenmsError(message)
            else:
                message = str(error) if isinstance(error, OpenmsError) else "读取或保存录像失败，请重试。"
            self._ui("set_busy", False, message)
            self._ui("show_replay_status", message, error is not None)
            return
        if kind == "analysis":
            self._ui("set_busy", False, "个人成绩读取失败，请在成绩窗口重试。" if error is not None else "我的成绩已更新。")
            if self._login_user is None or self._analysis_target is None:
                self._ui("clear_analysis")
                return
            if error is not None:
                self._ui("show_analysis_error", str(error) if isinstance(error, OpenmsError)
                         else "读取个人比赛成绩失败，请稍后重试。")
            elif (result.tournament.id, result.user_id) == self._analysis_target and result.user_id == self._login_user.id:
                self._scores[result.tournament.id] = result
                self._publish_scores()
            else:
                self._ui("clear_analysis")
            return
        if error is not None:
            self._participant = None
            self.logger.warning(f"OpenMS {kind}: {type(error).__name__}")
            if kind == "checkin" and self._selected:
                self._checkin_id = None
                self._ui("set_checkin_busy", self._selected.id, False)
            if kind == "checkin_all":
                self._checkin_all_busy = False
                self._ui("set_checkin_all_busy", False)
            if kind == "snapshot" and self._poll_timer is not None:
                self._poll_timer.setInterval(min(
                    max(self._poll_timer.interval() * 2, self._auto_check_interval_ms),
                    30 * 60 * 1000,
                ))
                if isinstance(error, RateLimitError):
                    self._poll_timer.setInterval(max(self._poll_timer.interval(), int(error.retry_after * 1000) + 1000))
            self._ui("show_error", str(error) if isinstance(error, OpenmsError) else "读取失败，请稍后刷新。")
            return
        if kind == "snapshot":
            self._snapshot = result
            self._scores = {score.tournament.id: score for score in result.analyses}
            self._cleanup_uploads(result)
            if self._poll_timer is not None:
                self._poll_timer.setInterval(self._auto_check_interval_ms)
            self._ui("show_snapshot", result)
            self._publish_scores()
            if self._analysis_target and self._analysis_target[0] not in self._scores:
                tournament_id, _ = self._analysis_target
                if any(item.id == tournament_id and item.is_ongoing() for item in result.tournaments):
                    self._ui("show_analysis_error", "本次未能刷新该比赛成绩，请点击刷新我的成绩。")
                else:
                    self._analysis_target = None
                    self._ui("clear_analysis")
            self._ui("set_authorized", self.has_control_auth(SetRaceIdentifierCommand))
            selected_id = self._refresh_selection_id
            self._refresh_selection_id = None
            self._selected = next((item for item in result.tournaments if item.id == selected_id), None)
            self._participant = result.participant_for(selected_id) if selected_id else None
            if self._selected is not None:
                self._ui("show_participant", self._selected, self._participant)
            self._refresh_game_context()
            return
        if kind == "checkin":
            self._checkin_id = None
            if self._selected is None or result is None:
                if self._selected:
                    self._ui("set_checkin_busy", self._selected.id, False)
                self._ui("show_error", "OpenMS 未返回打卡记录；请检查账号认证状态或刷新。")
                return
            participants = dict(self._snapshot.participants)
            participants[self._selected.id] = result
            self._snapshot = Snapshot(self._snapshot.tournaments, self._snapshot.user_id,
                                      self._snapshot.fetched_at, tuple(participants.items()))
            self._participant = result
            self._ui("show_snapshot", self._snapshot)
            self._ui("show_participant", self._selected, result)
            self._ui("set_checkin_busy", self._selected.id, False)
            self._ui("set_busy", False, "打卡成功，个人参赛计时已开始。")
            self._refresh_game_context()
            return
        if kind == "checkin_all":
            self._checkin_all_busy = False
            self._ui("set_checkin_all_busy", False)
            succeeded, failed = result
            participants = dict(self._snapshot.participants)
            for tournament, participant in succeeded:
                participants[tournament.id] = participant
            self._snapshot = Snapshot(
                self._snapshot.tournaments, self._snapshot.user_id,
                self._snapshot.fetched_at, tuple(participants.items()))
            self._ui("show_snapshot", self._snapshot)
            succeeded_text = "、".join(item.title for item, _ in succeeded) or "无"
            failed_text = "；".join(f"{item.title}：{message}" for item, message in failed)
            message = f"打卡完成：成功 {len(succeeded)} 场（{succeeded_text}），失败 {len(failed)} 场。"
            if failed_text:
                message += f"失败原因：{failed_text}"
            self._ui("set_busy", False, message)
            if succeeded:
                self._explicit_apply_all_pending = True
            self._refresh_game_context()
            return
        if kind == "participant":
            self._participant = result
            self._ui("show_participant", self._selected, result)
            self._refresh_game_context()
            return
        if kind not in ("apply", "apply_all"):
            return

        active_participants = result
        selected_participant = next(
            (participant for participant in active_participants
             if self._selected and participant.tournament_id == self._selected.id),
            None,
        )
        active_ids = {tournament.id for tournament in self._snapshot.tournaments
                      if tournament.is_ongoing()}
        apply_all = kind == "apply_all"
        if ((not apply_all and (not selected_participant or not selected_participant.is_active()
                or not self._selected or not self._selected.is_ongoing()))
                or any(participant.tournament_id not in active_ids or not participant.is_active()
                       for participant in active_participants)
                or not self.has_control_auth(SetRaceIdentifierCommand)):
            self._ui("show_error", "参赛标识已不可用或控制授权已撤销，请刷新。")
            return
        participants_to_apply = active_participants
        if not participants_to_apply:
            self._ui("show_error", "没有可设置的有效参赛标识，请检查报名状态和参赛时间。")
            return
        self._participant = selected_participant
        if self._selected is not None and selected_participant is not None:
            self._ui("show_participant", self._selected, selected_participant)
        race_identifier = ",".join(participant.token for participant in participants_to_apply)
        context = self._read_game_context(auto_apply=False)
        if context is None:
            self._ui("show_error", "无法读取主程序状态，暂时不能确认是否可安全设置。")
            return
        if getattr(context, "game_status", "") != "ready":
            self._ui("set_busy", False, "比赛标识已核对；请在游戏结束并回到待开局状态后设置。")
            return
        self._ui("set_busy", True, "正在设置比赛标识…")
        response = self.request(SetRaceIdentifierCommand(race_identifier=race_identifier))
        if response is None:
            self._ui("set_busy", False, "设置未收到响应，请检查主程序连接和控制授权。")
        elif not response.success:
            self._ui("set_busy", False, response.error or "设置失败，请确认元扫雷处于待开局（ready）状态。")
        else:
            self._ui("set_current_identifier", race_identifier)
            self._ui("set_busy", False,
                     f"已设置 {len(participants_to_apply)} 场当前参赛比赛的标识（半角逗号分隔），可以开始下一局。")

    def on_control_auth_changed(self, command_type: type, granted: bool):
        if command_type is SetRaceIdentifierCommand:
            self._ui("set_authorized", granted)
            if granted and self.other_info.auto_manage_identifier:
                self._refresh_game_context()
