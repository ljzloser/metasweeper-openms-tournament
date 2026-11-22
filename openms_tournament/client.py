"""使用宿主内置 requests 调用 OpenMS 查询、登录、打卡和录像上传接口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
import json
import os
from pathlib import Path, PureWindowsPath
import re
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable
from copy import deepcopy

import requests
from .request_policy import OpenmsError, RateLimitError, RequestPolicy

if TYPE_CHECKING:
    from .analysis import PersonalAnalysis

BASE_URL = "https://openms.top"
KEYRING_SERVICE = "metasweeper-openms-tournament"
KEYRING_ACCOUNT = "openms-cookie-session"


class UploadError(OpenmsError):
    def __init__(self, message: str, *, retryable: bool = False, needs_login: bool = False,
                 retry_after: float | None = None, rate_limited: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.needs_login = needs_login
        self.retry_after = retry_after
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class UploadResult:
    status: str
    video_id: int | None = None
    state: str = ""


@dataclass(frozen=True)
class LoginUser:
    id: int
    username: str


@dataclass(frozen=True)
class Captcha:
    key: str = field(repr=False)
    image: bytes = field(repr=False)


def positive_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OpenmsError("OpenMS 返回了无效的 ID。")
    return value


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenmsError("OpenMS 返回的比赛时间格式不正确。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        return parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise OpenmsError("OpenMS 返回的比赛时间格式不正确。") from exc


@dataclass(frozen=True)
class Tournament:
    id: int
    title: str
    phase: str
    starts_at: datetime | None
    ends_at: datetime | None
    subclass: str = "w"
    order: int | None = None
    tournament_format: str = "c"

    def is_ongoing(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return (self.phase == "ongoing"
                and (self.starts_at is None or now >= self.starts_at)
                and (self.ends_at is None or now < self.ends_at))


@dataclass(frozen=True)
class Participant:
    user_id: int
    tournament_id: int
    token: str
    starts_at: datetime | None
    ends_at: datetime | None

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return (bool(self.token)
                and (self.starts_at is None or now >= self.starts_at)
                and (self.ends_at is None or now < self.ends_at))


@dataclass(frozen=True)
class Snapshot:
    tournaments: tuple[Tournament, ...]
    user_id: int | None
    fetched_at: datetime
    participants: tuple[tuple[int, Participant | None], ...] = ()
    analyses: tuple[PersonalAnalysis, ...] = ()

    def participant_for(self, tournament_id: int) -> Participant | None:
        return next((participant for item_id, participant in self.participants
                     if item_id == tournament_id), None)


def tournament_title(item: dict[str, Any]) -> str:
    data = item.get("data")
    data = data if isinstance(data, dict) else {}
    for key in ("title", "name"):
        title = data.get(key)
        if isinstance(title, str) and title.strip():
            return title.strip()
    kind = item.get("subclass")
    year, week = data.get("year"), data.get("week")
    if kind in ("w", "e") and isinstance(year, int) and isinstance(week, int):
        label = "效率周赛" if kind == "e" else "周赛"
        return f"{year} 年第 {week} 周{label}"
    if kind == "g":
        return f"GSC 比赛 #{item['id']}"
    return f"比赛 #{item['id']}"


class OpenmsClient:
    """比赛查询、官网登录及通过系统凭据库可选地持久化 Cookie。"""

    def __init__(self, *, timeout: float = 8.0, request_get: Callable | None = None,
                 session: requests.Session | None = None):
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._request_get = request_get
        self._lock = RLock()
        self._policy = RequestPolicy()
        self._get_cache: dict[tuple, tuple[float, Any]] = {}

    def close(self):
        with self._lock:
            self._session.cookies.clear()
            self._get_cache.clear()
            self._session.close()

    def _request(self, method: str, path: str, *, image: bool = False, replay: bool = False, **kwargs) -> Any:
        try:
            with self._lock:
                self._policy.before_request('/api/video/download/' if replay else path)
                if method != 'GET':
                    self._get_cache.clear()
                request = (self._request_get or self._session.get) if method == "GET" else self._session.post
                with request(
                    BASE_URL + path, timeout=self.timeout, allow_redirects=False,
                    headers={"Accept": "image/png" if image else "application/octet-stream" if replay else "application/json",
                             "User-Agent": "MetaSweeper-OpenmsTournament/1.0",
                             "Origin": BASE_URL, "Referer": BASE_URL + "/"},
                    **kwargs,
                ) as response:
                    if response.status_code == 429:
                        raise RateLimitError(self._policy.rate_limited(response.headers.get('Retry-After')))
                    response.raise_for_status()
                    self._policy.succeeded()
                    if 300 <= response.status_code < 400:
                        raise OpenmsError("OpenMS 接口发生重定向，请更新插件后重试。")
                    if image:
                        if not response.content.startswith(b"\x89PNG\r\n\x1a\n") or len(response.content) > 1_000_000:
                            raise OpenmsError("OpenMS 返回的验证码图片格式不正确。")
                        return response.content
                    if replay:
                        return self._read_replay(response)
                    if path == "/userprofile/logout/":
                        return None
                    return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if path == "/api/common/uploadvideo":
                payload = None
                if exc.response is not None:
                    try:
                        payload = exc.response.json()
                    except ValueError:
                        pass
                if status == 409 and payload == {"type": "error", "object": "file", "category": "collision"}:
                    return payload
                identity_required = isinstance(payload, dict) and payload.get("category") == "identity_access_required"
                message = {
                    400: "录像文件或上传参数无效。", 402: "OpenMS 录像上传配额已用完。",
                    403: "登录失效或账号禁止上传，请检查登录状态和账号权限。",
                    401: "登录已失效，请重新登录。", 413: "录像文件超过网站大小限制。",
                    429: "上传过于频繁，稍后自动重试。",
                }.get(status, f"录像上传失败（HTTP {status}）。")
                if identity_required:
                    message = "该账号需要完成 OpenMS 实名或玩家认证后才能上传录像。"
                raise UploadError(message, retryable=status == 429 or status >= 500,
                                  needs_login=status in (401, 403) and not identity_required) from exc
            if status == 403 and exc.response is not None:
                try:
                    payload = exc.response.json()
                except (ValueError, requests.exceptions.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("category") == "identity_access_required":
                    raise OpenmsError("该账号需要完成 OpenMS 实名或玩家认证后才能打卡。") from exc
            messages = {
                400: "提交的登录信息格式不正确，请重新填写。",
                401: "OpenMS 要求登录，公开接口可能已变更。",
                403: "OpenMS 拒绝访问；请确认已登录，且账号符合该比赛的参赛条件。",
                404: "OpenMS 找不到该记录或接口。",
                429: "请求过于频繁，请稍后刷新。",
            }
            raise OpenmsError(messages.get(status, f"OpenMS 请求失败（HTTP {status}）。")) from exc
        except (requests.exceptions.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OpenmsError("OpenMS 返回了无效的 JSON 数据。") from exc
        except requests.RequestException as exc:
            raise OpenmsError("无法连接 OpenMS，请检查网络后重试。") from exc

    def cooldown_remaining(self) -> float:
        return self._policy.cooldown_remaining()

    def download_replay(self, video_id: int) -> tuple[str, bytes]:
        return self._request('GET', f'/api/video/download/{positive_id(video_id)}', replay=True, stream=True)

    @staticmethod
    def _read_replay(response: requests.Response) -> tuple[str, bytes]:
        media_type = response.headers.get('Content-Type', '').partition(';')[0].strip().lower()
        disposition = Message()
        disposition['Content-Disposition'] = response.headers.get('Content-Disposition', '')
        suffix = PureWindowsPath(disposition.get_filename() or '').suffix.lower()
        if (suffix not in {'.avf', '.evf', '.evfs', '.mvf', '.rmv'}
                or media_type.startswith('text/') or media_type.endswith(('json', 'xml'))):
            raise OpenmsError('OpenMS 返回的文件不是支持的扫雷录像。')
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            content.extend(chunk)
            if len(content) > 32 * 1024 * 1024:
                raise OpenmsError('OpenMS 录像超过 32 MiB，无法打开。')
        if not content:
            raise OpenmsError('OpenMS 返回了空录像。')
        return suffix, bytes(content)

    def _get(self, path: str, *, fresh: bool = False, **params: Any) -> Any:
        ttl = {'/api/tournament/get_list': 3.0, '/api/tournament/participants': 2.0,
               '/api/tournament/get_videos/participant': 3.0}.get(path, 0)
        key = (path, tuple(sorted(params.items())))
        with self._lock:
            cached = self._get_cache.get(key)
            if not fresh and ttl and cached is not None and self._policy.clock() - cached[0] < ttl:
                return deepcopy(cached[1])
            body = self._request("GET", path, params=params)
            if ttl:
                # A short cache only coalesces near-simultaneous UI requests; it never stores errors.
                self._get_cache = {item: value for item, value in self._get_cache.items()
                                   if self._policy.clock() - value[0] < 3}
                self._get_cache[key] = (self._policy.clock(), deepcopy(body))
            return body

    def upload_video(self, raw_data: bytes, filename: str) -> UploadResult:
        if not raw_data:
            raise UploadError("录像数据为空，无法上传。")
        try:
            body = self._request("POST", "/api/common/uploadvideo",
                                 files={"file": (filename, raw_data, "application/octet-stream")})
        except RateLimitError as exc:
            raise UploadError(str(exc), retryable=True, retry_after=exc.retry_after, rate_limited=True) from exc
        except UploadError:
            raise
        except OpenmsError as exc:
            # The server may have received the file even if its response was lost.
            raise UploadError(str(exc), retryable=True) from exc
        if isinstance(body, dict) and body.get("type") == "error":
            if body.get("object") == "file" and body.get("category") == "collision":
                return UploadResult("duplicate")
            messages = {
                ("file", "incomplete"): "录像不完整，网站拒绝上传。",
                ("identifier", "verify"): "录像玩家标识未通过网站检查。",
                ("userprofile", "identity_access_required"): "请先完成 OpenMS 实名或玩家认证。",
            }
            key = (body.get("object"), body.get("category"))
            message = messages.get(key, "网站拒绝了这份录像。") if all(isinstance(value, str) for value in key) else "网站拒绝了这份录像。"
            raise UploadError(message)
        if (not isinstance(body, dict) or body.get("type") != "success"
                or body.get("object") != "videomodel" or not isinstance(body.get("data"), dict)):
            raise UploadError("网站未返回有效的上传结果，稍后重新确认。", retryable=True)
        data = body["data"]
        if not isinstance(data.get("state"), str) or not data["state"]:
            raise UploadError("网站未返回录像状态，稍后重新确认。", retryable=True)
        try:
            video_id = positive_id(data.get("id"))
        except OpenmsError as exc:
            raise UploadError("网站未返回录像 ID，稍后重新确认。", retryable=True) from exc
        return UploadResult("succeeded", video_id, data["state"])

    @staticmethod
    def _login_user(body: Any) -> LoginUser:
        if not isinstance(body, dict) or not isinstance(body.get("username"), str):
            raise OpenmsError("OpenMS 返回的登录用户格式不正确。")
        return LoginUser(positive_id(body.get("id")), body["username"])

    def current_user(self) -> LoginUser | None:
        body = self._get("/api/common/loginauto")
        if not isinstance(body, dict) or "user" not in body:
            raise OpenmsError("OpenMS 返回的登录状态格式不正确。")
        if body["user"] is None:
            with self._lock:
                self._session.cookies.clear()
                self._get_cache.clear()
            return None
        return self._login_user(body["user"])

    def captcha(self) -> Captcha:
        body = self._get("/userprofile/refresh_captcha/")
        key = body.get("hashkey") if isinstance(body, dict) else None
        if (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9]{8,512}", key)
                or body.get("status") != 100):
            raise OpenmsError("OpenMS 返回的验证码信息格式不正确。")
        image = self._request("GET", f"/userprofile/captcha/image/{key}/", image=True)
        return Captcha(key, image)

    def login(self, username: str, password: str, captcha: str, key: str,
              *, expiry_days: int = 7) -> LoginUser:
        if not username.strip() or not password or not captcha.strip():
            raise OpenmsError("请填写用户名、密码和图片验证码。")
        if not re.fullmatch(r"[a-zA-Z0-9]{8,512}", key):
            raise OpenmsError("请先刷新图片验证码。")
        if (isinstance(expiry_days, bool) or not isinstance(expiry_days, int)
                or not 7 <= expiry_days <= 90):
            raise OpenmsError("保持登录有效期必须是 7 到 90 天之间的整数。")
        # 官网表单以天为单位设置会话过期时间；插件只接受持续登录的有效范围。
        body = self._request("POST", "/userprofile/login/", data={
            "username": username.strip(), "password": password,
            "captcha": captcha.strip(), "hashkey": key, "set_expiry": expiry_days,
        })
        if isinstance(body, dict) and body.get("type") == "error":
            messages = {"captcha": "图片验证码错误或已过期，请刷新后重试。",
                        "password": "用户名或密码不正确。"}
            raise OpenmsError(messages.get(body.get("category"), "登录失败，请稍后重试。"))
        try:
            if not isinstance(body, dict) or body.get("type") != "success":
                raise OpenmsError("OpenMS 返回的登录响应格式不正确。")
            user = self._login_user(body.get("user"))
            verified = self.current_user()
            if verified is None or verified.id != user.id:
                raise OpenmsError("登录会话验证失败，请重新登录。")
            return verified
        except OpenmsError:
            with self._lock:
                self._session.cookies.clear()
                self._get_cache.clear()
            raise

    def logout(self):
        try:
            self._get("/userprofile/logout/")
        finally:
            with self._lock:
                self._session.cookies.clear()
                self._get_cache.clear()

    @staticmethod
    def _unprotect_legacy_cookie(data: bytes) -> bytes:
        """Read the previous Windows-only encrypted cookie file for migration."""
        marker = b"OPENMS-COOKIE-DPAPI-1\n"
        if not data.startswith(marker) or os.name != "nt":
            raise OpenmsError("保存的 OpenMS 登录会话无法解密。")
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        encrypted = data[len(marker):]
        source_buffer = ctypes.create_string_buffer(encrypted)
        source = DataBlob(len(encrypted), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
        target = DataBlob()
        description = wintypes.LPWSTR()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                                               ctypes.POINTER(DataBlob), wintypes.LPVOID,
                                               wintypes.LPVOID, wintypes.DWORD,
                                               ctypes.POINTER(DataBlob)]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        if not crypt32.CryptUnprotectData(ctypes.byref(source), ctypes.byref(description), None,
                                          None, None, 1, ctypes.byref(target)):
            raise OpenmsError("保存的 OpenMS 登录会话已损坏或无法解密。")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)
            if description:
                kernel32.LocalFree(description)

    @staticmethod
    def _credential_keyring():
        try:
            import keyring
            backend = keyring.get_keyring()
            if getattr(backend, "priority", 0) <= 0:
                raise OpenmsError("系统没有可用的安全凭据库，无法保持登录。")
            return keyring
        except OpenmsError:
            raise
        except Exception as exc:
            raise OpenmsError("无法访问系统凭据库，无法保持登录。") from exc

    def _cookie_rows(self) -> list[dict[str, Any]]:
        cookies = []
        for cookie in self._session.cookies:
            if cookie.domain.lstrip(".").lower() != "openms.top":
                continue
            cookies.append({"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                            "path": cookie.path, "secure": cookie.secure, "expires": cookie.expires,
                            "rest": cookie._rest})
        if not cookies:
            raise OpenmsError("OpenMS 没有返回可保存的登录 Cookie。")
        return cookies

    def _restore_cookie_rows(self, rows: Any) -> bool:
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("invalid cookie list")
        cookies = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("value"), str)
                    or not isinstance(row.get("domain"), str)
                    or row["domain"].lstrip(".").lower() != "openms.top"):
                raise ValueError("invalid cookie")
            cookie = requests.cookies.create_cookie(
                name=row["name"], value=row["value"], domain=row["domain"],
                path=row.get("path", "/"), secure=bool(row.get("secure")),
                expires=row.get("expires"), rest=row.get("rest", {}),
            )
            cookies.append(cookie)
        self._session.cookies.clear()
        for cookie in cookies:
            self._session.cookies.set_cookie(cookie)
        return True

    def save_cookies(self, legacy_path: Path | None = None):
        keyring = self._credential_keyring()
        value = json.dumps(self._cookie_rows(), ensure_ascii=False, separators=(",", ":"))
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, value)
        except Exception as exc:
            raise OpenmsError("无法将 OpenMS Cookie 保存到系统凭据库。") from exc
        self._delete_legacy_cookie_file(legacy_path)

    def load_cookies(self, legacy_path: Path | None = None) -> bool:
        self._get_cache.clear()
        keyring = self._credential_keyring()
        try:
            value = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception as exc:
            raise OpenmsError("无法读取系统凭据库中的 OpenMS 登录会话。") from exc
        if value is None and legacy_path is not None and legacy_path.is_file():
            try:
                rows = json.loads(self._unprotect_legacy_cookie(legacy_path.read_bytes()).decode("utf-8"))
                self._restore_cookie_rows(rows)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, OpenmsError):
                self._session.cookies.clear()
                self.delete_cookies(legacy_path)
                return False
            self.save_cookies(legacy_path)
            return True
        if value is None:
            return False
        try:
            self._restore_cookie_rows(json.loads(value))
            return True
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            self._session.cookies.clear()
            self.delete_cookies(legacy_path)
            return False

    def delete_cookies(self, legacy_path: Path | None = None, *, strict: bool = False):
        try:
            keyring = self._credential_keyring()
            value = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            if value is not None:
                try:
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
                except keyring.errors.PasswordDeleteError:
                    pass
        except OpenmsError:
            if strict or legacy_path is None:
                raise
        except Exception as exc:
            raise OpenmsError("无法删除系统凭据库中的 OpenMS 登录会话。") from exc
        finally:
            self._delete_legacy_cookie_file(legacy_path)

    @staticmethod
    def _delete_legacy_cookie_file(legacy_path: Path | None):
        if legacy_path is None:
            return
        try:
            legacy_path.unlink(missing_ok=True)
        except OSError:
            pass

    def snapshot(self, user_id: int = 0, *, now: datetime | None = None) -> Snapshot:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
            raise OpenmsError("OpenMS 用户 ID 必须是正整数（0 表示未配置）。")
        now = now or datetime.now(timezone.utc)
        # The normal list is one batch containing the same cached tournaments as get_news,
        # with full details. Do not fetch all historical competitions or each detail separately.
        items = self._get("/api/tournament/get_list", category="normal")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise OpenmsError("OpenMS 的比赛列表响应格式已变更。")
        tournaments: list[Tournament] = []
        seen = set()
        for item in items:
            if not all(key in item for key in ('id', 'state', 'subclass', 'start_time', 'end_time', 'data')):
                raise OpenmsError('OpenMS 的比赛列表缺少详情字段，已停止刷新，避免错误清理比赛数据。')
            item_id = positive_id(item.get('id'))
            if item_id in seen:
                raise OpenmsError("OpenMS 返回了重复的比赛记录，请稍后刷新。")
            seen.add(item_id)
            starts, ends = parse_time(item.get('start_time')), parse_time(item.get('end_time'))
            if item.get('state') != 'n' or starts is None:
                continue
            if starts > now:
                phase = 'preparing'
            elif ends is not None and now < ends:
                phase = 'ongoing'
            else:
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            subclass = item.get("subclass")
            order = data.get("order") if subclass == "g" else None
            if subclass not in ("w", "e", "g") or (subclass == "g" and (isinstance(order, bool) or not isinstance(order, int) or order <= 0)):
                raise OpenmsError("OpenMS 返回了无效的比赛类型或届数。")
            tournaments.append(Tournament(item_id, tournament_title(item), phase, starts, ends,
                                          subclass, order, str(data.get("tournament_format", "c"))))
        tournaments.sort(key=lambda tournament: (tournament.phase != 'ongoing', tournament.starts_at, tournament.id))
        participants = ()
        if user_id:
            participants = tuple((t.id, self.participant(t.id, user_id) if t.phase == "ongoing" else None)
                                 for t in tournaments)
        return Snapshot(tuple(tournaments), user_id or None, now, participants)

    def participant(self, tournament_id: int, user_id: int, *, fresh: bool = False) -> Participant | None:
        positive_id(tournament_id)
        positive_id(user_id)
        rows = self._get("/api/tournament/participants", tournament_id=tournament_id, fresh=fresh)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise OpenmsError("OpenMS 的参赛者响应格式已变更。")
        matches = [row for row in rows if row.get("user_id") == user_id]
        if not matches:
            return None
        if len(matches) != 1:
            raise OpenmsError("该用户存在多条参赛记录，请前往 OpenMS 确认。")
        return self._participant_from_row(matches[0], tournament_id, user_id)

    @staticmethod
    def _participant_from_row(row: dict, tournament_id: int, user_id: int) -> Participant:
        if row.get('user_id') != user_id:
            raise OpenmsError('OpenMS 返回的参赛记录与账号不匹配。')
        if row.get("tournament_id") != tournament_id:
            raise OpenmsError("OpenMS 返回的参赛记录与比赛不匹配。")
        token = row.get("token")
        if not isinstance(token, str) or token != token.strip() or len(token) > 80:
            raise OpenmsError("OpenMS 返回的比赛标识格式不正确。")
        return Participant(user_id, tournament_id, token,
                           parse_time(row.get("start_time")), parse_time(row.get("end_time")))

    def personal_videos(self, tournament_id: int, user_id: int):
        from .analysis import ScoreVideo

        positive_id(tournament_id)
        positive_id(user_id)
        rows = self._get('/api/tournament/get_videos/participant', tournament_id=tournament_id, user_id=user_id)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise OpenmsError('OpenMS 返回的个人比赛录像列表格式不正确。')
        videos = []
        seen = set()
        for row in rows:
            video_id = positive_id(row.get('id'))
            if video_id in seen:
                raise OpenmsError('网站返回了重复录像 ID，无法可靠计算成绩。')
            seen.add(video_id)
            if positive_id(row.get('player_id', row.get('player'))) != user_id:
                raise OpenmsError('网站返回了其他账号的录像，已停止个人成绩分析。')
            if any(not isinstance(row.get(key), str) for key in ('level', 'mode', 'state')):
                raise OpenmsError('网站返回的录像难度、模式或审核状态无效。')
            if any(type(row.get(key)) is not int or row[key] < 0 for key in ('timems', 'bv')):
                raise OpenmsError('网站返回的录像用时或 3BV 无效。')
            clicks = row.get('cl')
            if clicks is not None and (type(clicks) is not int or clicks < 0):
                raise OpenmsError('网站返回的录像点击数无效。')
            videos.append(ScoreVideo(video_id, row['level'], row['mode'], row['state'],
                                     row['timems'], row['bv'], clicks))
        return tuple(videos)

    def active_participants(self, tournaments: tuple[Tournament, ...], user_id: int) -> tuple[Participant, ...]:
        """Refresh ongoing participation records and return only currently usable tokens."""
        positive_id(user_id)
        now = datetime.now(timezone.utc)
        active: list[Participant] = []
        for tournament in tournaments:
            if not tournament.is_ongoing(now):
                continue
            participant = self.participant(tournament.id, user_id, fresh=True)
            if participant is None or not participant.is_active(now):
                continue
            if "," in participant.token or any(character.isspace() for character in participant.token):
                raise OpenmsError("OpenMS 比赛标识包含逗号或空白字符，无法安全合并。")
            active.append(participant)
        return tuple(active)

    def check_in(self, tournament: Tournament, user_id: int) -> Participant | None:
        positive_id(tournament.id)
        positive_id(user_id)
        if not tournament.is_ongoing():
            raise OpenmsError("只有进行中的比赛可以打卡。")
        if tournament.subclass == "g":
            if tournament.order is None:
                raise OpenmsError("OpenMS 返回的 GSC 届数无效。")
            path, data = "/api/tournament/gsc/participant", {"order": tournament.order}
        else:
            path = ("/api/tournament/weekly-eff/participant" if tournament.subclass == "e"
                    else "/api/tournament/weekly/participant")
            data = {"id": tournament.id}
        body = self._request("POST", path, data=data)
        if isinstance(body, dict) and all(key in body for key in ('user_id', 'tournament_id', 'token')):
            return self._participant_from_row(body, tournament.id, user_id)
        return self.participant(tournament.id, user_id, fresh=True)
