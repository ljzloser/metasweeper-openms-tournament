"""官网登录契约：表单、手动验证码、内存会话及错误处理。"""
import json
import sys
from types import SimpleNamespace
import pytest
import requests

from openms_tournament.client import BASE_URL, LoginUser, OpenmsClient, OpenmsError, UploadError, UploadResult

PNG = b"\x89PNG\r\n\x1a\nfixture"
KEY = "abcd1234" * 5


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("session_id", "test-session", domain="openms.top", path="/")
        self.closed = False

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, *extra = item
        response = requests.Response()
        response.status_code = status
        response.url = url
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        response._content_consumed = True
        if extra:
            response.headers.update(extra[0])
        return response

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def close(self):
        self.closed = True


def test_upload_posts_original_bytes_using_existing_cookie_session():
    session = FakeSession((200, {"type": "success", "object": "videomodel", "data": {"id": 123, "state": "d"}}))
    result = OpenmsClient(session=session).upload_video(b"original-evf", "demo.evf")
    assert result == UploadResult("succeeded", 123, "d")
    assert session.calls[0][:2] == ("POST", BASE_URL + "/api/common/uploadvideo")
    assert session.calls[0][2]["files"] == {"file": ("demo.evf", b"original-evf", "application/octet-stream")}
    assert session.cookies.get("session_id") == "test-session"


def test_test_suite_blocks_real_http_before_any_network_io():
    with pytest.raises(AssertionError, match="must not send real HTTP"):
        requests.post(BASE_URL + "/api/common/uploadvideo", files={"file": ("blocked.evf", b"never sent")})


@pytest.mark.parametrize("status", [200, 409])
def test_upload_collision_is_terminal_duplicate_without_fake_video_id(status):
    session = FakeSession((status, {"type": "error", "object": "file", "category": "collision"}))
    assert OpenmsClient(session=session).upload_video(b"replay", "demo.evf") == UploadResult("duplicate")
    assert len(session.calls) == 1


@pytest.mark.parametrize("status,retryable,needs_login", [
    (400, False, False), (401, False, True), (402, False, False), (403, False, True),
    (413, False, False), (429, True, False), (503, True, False),
])
def test_upload_http_failures_have_specific_retry_policy(status, retryable, needs_login):
    with pytest.raises(UploadError) as caught:
        OpenmsClient(session=FakeSession((status, b"server error"))).upload_video(b"replay", "demo.evf")
    assert caught.value.retryable == retryable
    assert caught.value.needs_login == needs_login


def test_upload_identity_error_does_not_clear_valid_login():
    session = FakeSession((403, {"category": "identity_access_required"}))
    with pytest.raises(UploadError, match="认证") as caught:
        OpenmsClient(session=session).upload_video(b"replay", "demo.evf")
    assert not caught.value.needs_login and not caught.value.retryable


@pytest.mark.parametrize("failure", [requests.Timeout("lost response"), (200, b"not JSON")])
def test_upload_lost_or_unreadable_response_can_be_retried(failure):
    with pytest.raises(UploadError) as caught:
        OpenmsClient(session=FakeSession(failure)).upload_video(b"replay", "demo.evf")
    assert caught.value.retryable


def test_upload_429_obeys_retry_after_and_blocks_all_live_requests_until_cooldown():
    from openms_tournament.request_policy import RateLimitError

    session = FakeSession((429, b'', {'Retry-After': '120'}), (200, {'user': None}))
    client = OpenmsClient(session=session)
    with pytest.raises(UploadError) as caught:
        client.upload_video(b'replay', 'demo.evf')
    assert caught.value.retry_after == 120 and caught.value.rate_limited
    with pytest.raises(RateLimitError):
        client.current_user()
    assert len(session.calls) == 1
    client._policy.wait(120)
    assert client.current_user() is None
    assert len(session.calls) == 2


def test_logout_clears_personal_query_cache_and_post_mutations_force_new_reads():
    from test_client import personal_video

    session = FakeSession((200, [personal_video()]), (200, b''), (200, [personal_video(id=124)]))
    client = OpenmsClient(session=session)
    assert client.personal_videos(30, 7)[0].id == 123
    client.logout()
    assert client.personal_videos(30, 7)[0].id == 124
    assert len(session.calls) == 3


def test_successful_upload_invalidates_personal_video_cache_immediately():
    from test_client import personal_video

    session = FakeSession(
        (200, [personal_video()]),
        (200, {'type': 'success', 'object': 'videomodel', 'data': {'id': 124, 'state': 'c'}}),
        (200, [personal_video(), personal_video(id=124)]),
    )
    client = OpenmsClient(session=session)
    assert len(client.personal_videos(30, 7)) == 1
    client.upload_video(b'new replay', 'new.evf')
    assert len(client.personal_videos(30, 7)) == 2
    assert len(session.calls) == 3


@pytest.mark.parametrize("body", [
    {"type": "error", "object": "file", "category": "incomplete"},
    {"type": "error", "object": "identifier", "category": "verify"},
    {"type": "error", "object": "identifier", "category": "collision"},
    {"type": "error", "object": [], "category": {}},
    {"type": "success", "object": "videomodel", "data": {"id": True, "state": "c"}},
    {"type": "success", "object": "videomodel", "data": {"id": 1}},
    {}, None,
])
def test_upload_never_treats_unknown_or_invalid_response_as_duplicate_or_success(body):
    with pytest.raises(UploadError):
        OpenmsClient(session=FakeSession((200, body))).upload_video(b"replay", "demo.evf")


def test_captcha_downloads_official_image_without_solving_it():
    session = FakeSession((200, {"status": 100, "hashkey": KEY}), (200, PNG))
    result = OpenmsClient(session=session).captcha()
    assert result.key == KEY and result.image == PNG
    assert [call[:2] for call in session.calls] == [
        ("GET", BASE_URL + "/userprofile/refresh_captcha/"),
        ("GET", BASE_URL + f"/userprofile/captcha/image/{KEY}/"),
    ]


def test_login_uses_documented_legacy_form_and_verifies_cookie_session():
    user = {"id": 7, "username": "测试用户"}
    session = FakeSession((200, {"type": "success", "user": user}), (200, {"user": user}))
    client = OpenmsClient(session=session)
    assert client.login(" 测试用户 ", " password ", " A1b2 ", KEY) == LoginUser(7, "测试用户")
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == BASE_URL + "/userprofile/login/"
    assert kwargs["data"] == {
        "username": "测试用户", "password": " password ", "captcha": "A1b2",
        "hashkey": KEY, "set_expiry": 7,
    }
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Origin"] == BASE_URL
    assert kwargs["headers"]["Referer"] == BASE_URL + "/"
    assert session.calls[1][0:2] == ("GET", BASE_URL + "/api/common/loginauto")
    assert session.cookies.get("session_id") == "test-session"


@pytest.mark.parametrize("expiry_days", [7, 8, 14, 30, 89, 90])
def test_remembered_login_uses_selected_server_session_expiry(expiry_days):
    user = {"id": 7, "username": "tester"}
    session = FakeSession((200, {"type": "success", "user": user}), (200, {"user": user}))
    OpenmsClient(session=session).login("tester", "secret", "1234", KEY, expiry_days=expiry_days)
    assert session.calls[0][2]["data"]["set_expiry"] == expiry_days


def test_cookie_persistence_uses_cross_platform_credential_store(tmp_path, monkeypatch):
    values = {}

    class PasswordDeleteError(Exception):
        pass

    fake_keyring = SimpleNamespace(
        get_keyring=lambda: SimpleNamespace(priority=1),
        set_password=lambda service, account, value: values.__setitem__((service, account), value),
        get_password=lambda service, account: values.get((service, account)),
        delete_password=lambda service, account: values.pop((service, account), None),
        errors=SimpleNamespace(PasswordDeleteError=PasswordDeleteError),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    source = FakeSession()
    path = tmp_path / "openms-session.bin"
    OpenmsClient(session=source).save_cookies(path)
    assert not path.exists()
    assert len(values) == 1

    target = FakeSession()
    target.cookies.clear()
    assert OpenmsClient(session=target).load_cookies(path)
    assert target.cookies.get("session_id") == "test-session"
    OpenmsClient(session=target).delete_cookies(path)
    assert values == {}


def test_login_rejects_expiry_values_not_offered_by_openms():
    for days in (0, 6, 91, True):
        session = FakeSession()
        with pytest.raises(OpenmsError, match="7 到 90 天"):
            OpenmsClient(session=session).login("tester", "secret", "1234", KEY, expiry_days=days)
        assert not session.calls


def test_missing_system_credential_backend_fails_persistence_without_writing_file(tmp_path, monkeypatch):
    fake_keyring = SimpleNamespace(get_keyring=lambda: SimpleNamespace(priority=0))
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    with pytest.raises(OpenmsError, match="安全凭据库"):
        OpenmsClient(session=FakeSession()).load_cookies(tmp_path / "session.bin")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("category, message", [("password", "用户名或密码"), ("captcha", "验证码"), ("unknown", "登录失败")])
def test_login_errors_are_stable_and_do_not_expose_server_payload(category, message):
    session = FakeSession((200, {"type": "error", "category": category, "detail": "sensitive detail"}))
    with pytest.raises(OpenmsError, match=message) as error:
        OpenmsClient(session=session).login("tester", "secret", "1234", KEY)
    assert "secret" not in str(error.value) and "sensitive detail" not in str(error.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("user", [None, {"id": 8, "username": "other"}])
def test_login_is_not_successful_until_the_same_session_is_verified(user):
    session = FakeSession(
        (200, {"type": "success", "user": {"id": 7, "username": "tester"}}), (200, {"user": user}))
    with pytest.raises(OpenmsError, match="会话验证失败"):
        OpenmsClient(session=session).login("tester", "secret", "1234", KEY)
    assert not session.cookies


@pytest.mark.parametrize("username, password, captcha, key", [
    ("", "secret", "1234", KEY), ("tester", "", "1234", KEY),
    ("tester", "secret", "", KEY), ("tester", "secret", "1234", "../untrusted"),
])
def test_invalid_login_inputs_never_send_credentials(username, password, captcha, key):
    session = FakeSession()
    with pytest.raises(OpenmsError):
        OpenmsClient(session=session).login(username, password, captcha, key)
    assert not session.calls


def test_expired_session_returns_guest_and_clears_local_cookies():
    session = FakeSession((200, {"user": None}))
    assert OpenmsClient(session=session).current_user() is None
    assert not session.cookies


def test_checkin_identity_requirement_has_an_actionable_error():
    session = FakeSession((403, {"type": "error", "category": "identity_access_required"}))
    with pytest.raises(OpenmsError, match="实名或玩家认证"):
        OpenmsClient(session=session)._request("POST", "/api/tournament/weekly/participant", data={"id": 30})


@pytest.mark.parametrize("response", [(200, b""), requests.ConnectionError("offline")])
def test_logout_always_forgets_local_cookie_even_if_server_unreachable(response):
    session = FakeSession(response)
    client = OpenmsClient(session=session)
    if isinstance(response, Exception):
        with pytest.raises(OpenmsError):
            client.logout()
    else:
        client.logout()
    assert not session.cookies
    assert session.calls[0][0:2] == ("GET", BASE_URL + "/userprofile/logout/")


def test_redirected_login_never_reposts_credentials_to_another_url():
    session = FakeSession((302, b""))
    with pytest.raises(OpenmsError, match="重定向"):
        OpenmsClient(session=session).login("tester", "secret", "1234", KEY)
    assert len(session.calls) == 1 and session.calls[0][2]["allow_redirects"] is False


@pytest.mark.parametrize("key, image", [("../path", PNG), (KEY, b"<html>bad image</html>")])
def test_captcha_rejects_path_injection_and_invalid_images(key, image):
    session = FakeSession((200, {"status": 100, "hashkey": key}), (200, image))
    with pytest.raises(OpenmsError):
        OpenmsClient(session=session).captcha()


def test_close_discards_memory_session():
    session = FakeSession()
    OpenmsClient(session=session).close()
    assert session.closed and not session.cookies
