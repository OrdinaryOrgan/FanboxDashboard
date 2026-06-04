import asyncio
from pathlib import Path

from app.db.models import Settings
from app.services.fanbox import (
    AUTH_CHECK_TIMEOUT_MS,
    AUTH_CHECK_URL,
    LOGIN_PAGE_URL,
    FanboxAuthError,
    _acquire_profile_context_lock,
    _find_new_chromium_window,
    _launch_persistent_context_with_retry,
    _normalize_proxy_server,
    _probe_fanbox_login_state,
    _redact_sensitive_request_details,
    inspect_auth_status,
    logout_fanbox,
    open_login_window,
    _wait_for_login_completion,
)


class _FakePage:
    def __init__(self, initial_url: str = "about:blank", sleep_seconds: float = 0.0) -> None:
        self.url = initial_url
        self._sleep_seconds = sleep_seconds

    async def wait_for_timeout(self, _: int) -> None:
        if self._sleep_seconds > 0:
            await asyncio.sleep(self._sleep_seconds)


def test_wait_for_login_completion_returns_once_page_is_authenticated(monkeypatch) -> None:
    async def _noop(_page) -> None:
        return None

    responses = [
        (False, "Fanbox redirected to the Pixiv login page."),
        (True, None),
    ]

    async def fake_probe(_context, _page) -> tuple[bool, str | None]:
        return responses.pop(0)

    monkeypatch.setattr("app.services.fanbox._dismiss_age_confirmation", _noop)
    monkeypatch.setattr("app.services.fanbox._probe_fanbox_login_state", fake_probe)
    page = _FakePage("https://accounts.pixiv.net/login")

    asyncio.run(_wait_for_login_completion(object(), page, timeout_seconds=2))

    assert page.url == "https://accounts.pixiv.net/login"


def test_wait_for_login_completion_raises_after_timeout(monkeypatch) -> None:
    async def _noop(_page) -> None:
        return None

    async def fake_probe(_context, _page) -> tuple[bool, str | None]:
        return False, "Fanbox redirected to the Pixiv login page."

    monkeypatch.setattr("app.services.fanbox._dismiss_age_confirmation", _noop)
    monkeypatch.setattr("app.services.fanbox._probe_fanbox_login_state", fake_probe)
    page = _FakePage("https://accounts.pixiv.net/login", sleep_seconds=0.02)

    try:
        asyncio.run(_wait_for_login_completion(object(), page, timeout_seconds=0.01))
    except FanboxAuthError as exc:
        assert "Login was not completed within" in str(exc)
        assert "Fanbox redirected to the Pixiv login page." in str(exc)
    else:
        raise AssertionError("Expected FanboxAuthError to be raised after the timeout.")


def test_find_new_chromium_window_prefers_matching_title(monkeypatch) -> None:
    responses = [
        [(1, "Existing browser")],
        [(1, "Existing browser"), (2, "PIXIV Login")],
    ]

    def fake_list_visible_chromium_windows():
        if responses:
            return responses.pop(0)
        return [(1, "Existing browser"), (2, "PIXIV Login")]

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.services.fanbox._list_visible_chromium_windows", fake_list_visible_chromium_windows)
    monkeypatch.setattr("app.services.fanbox.asyncio.sleep", fake_sleep)

    hwnd = asyncio.run(_find_new_chromium_window({1}, "pixiv"))

    assert hwnd == 2


class _FakeOpenLoginPage:
    def __init__(
        self,
        initial_url: str = "about:blank",
        body_text: str = "Notifications",
        goto_result_url: str | None = None,
        goto_status: int = 200,
    ) -> None:
        self.url = initial_url
        self.body_text = body_text
        self.goto_result_url = goto_result_url
        self.goto_status = goto_status
        self.goto_calls: list[tuple[str, str, int | None]] = []
        self.call_order: list[str] = []
        self.bring_to_front_calls = 0
        self.closed = False

    async def goto(self, url: str, wait_until: str, timeout: int | None = None):
        self.call_order.append("goto")
        self.goto_calls.append((url, wait_until, timeout))
        self.url = self.goto_result_url or url
        return _FakePageResponse(self.goto_status)

    async def bring_to_front(self) -> None:
        self.call_order.append("bring_to_front")
        self.bring_to_front_calls += 1

    async def title(self) -> str:
        return "PIXIV Login"

    async def wait_for_timeout(self, _: int) -> None:
        return None

    def locator(self, selector: str):
        if selector == "body":
            return _FakeBodyLocator(self)
        return _FakeEmptyLocator()

    async def close(self) -> None:
        self.closed = True


class _FakePageResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeBodyLocator:
    def __init__(self, page: _FakeOpenLoginPage) -> None:
        self.page = page

    async def inner_text(self) -> str:
        return self.page.body_text


class _FakeEmptyLocator:
    @property
    def last(self):
        return self

    async def count(self) -> int:
        return 0

    async def click(self, force: bool = False) -> None:
        return None


class _FakeOpenLoginContext:
    def __init__(self, page: _FakeOpenLoginPage) -> None:
        self.page = page
        self.pages: list[_FakeOpenLoginPage] = []
        self.storage_state_paths: list[str] = []
        self.closed = False
        self.request = None

    async def new_page(self) -> _FakeOpenLoginPage:
        self.pages.append(self.page)
        return self.page

    async def storage_state(self, path: str) -> None:
        self.storage_state_paths.append(path)

    async def close(self) -> None:
        self.closed = True


class _FakeOpenLoginPlaywright:
    def __init__(self, context: _FakeOpenLoginContext) -> None:
        self.context = context
        self.launch_kwargs: dict[str, object] | None = None
        self.chromium = self

    async def launch_persistent_context(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.context


class _FakeAsyncPlaywrightFactory:
    def __init__(self, playwright: _FakeOpenLoginPlaywright) -> None:
        self.playwright = playwright

    def __call__(self):
        return self

    async def __aenter__(self) -> _FakeOpenLoginPlaywright:
        return self.playwright

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeRequestResponse:
    def __init__(self, url: str, status: int = 200) -> None:
        self.url = url
        self.status = status


class _FakeRequestContext:
    def __init__(self, responses: list[_FakeRequestResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, int, bool]] = []

    async def get(self, url: str, timeout: int, fail_on_status_code: bool) -> _FakeRequestResponse:
        self.calls.append((url, timeout, fail_on_status_code))
        if self._responses:
            return self._responses.pop(0)
        return _FakeRequestResponse(url)


def test_open_login_window_uses_windows_maximize_and_focus(monkeypatch, tmp_path: Path) -> None:
    page = _FakeOpenLoginPage()
    context = _FakeOpenLoginContext(page)
    playwright = _FakeOpenLoginPlaywright(context)
    focus_calls: list[tuple[object, set[int]]] = []
    wait_calls: list[tuple[object, int]] = []
    storage_state_path = tmp_path / "storage_state.json"

    async def fake_get_async_playwright():
        return _FakeAsyncPlaywrightFactory(playwright)

    async def fake_focus_new_chromium_window(page_obj, handles: set[int]) -> None:
        page_obj.call_order.append("focus_helper")
        focus_calls.append((page_obj, handles))

    async def fake_wait_for_login_completion(context_obj, page_obj, timeout_seconds: int) -> None:
        page_obj.call_order.append("wait_for_login_completion")
        wait_calls.append((context_obj, page_obj, timeout_seconds))

    monkeypatch.setattr("app.services.fanbox._get_async_playwright", fake_get_async_playwright)
    monkeypatch.setattr("app.services.fanbox._is_windows_desktop", lambda: True)
    monkeypatch.setattr("app.services.fanbox._snapshot_chromium_window_handles", lambda: {11, 22})
    monkeypatch.setattr("app.services.fanbox._focus_new_chromium_window", fake_focus_new_chromium_window)
    monkeypatch.setattr("app.services.fanbox._wait_for_login_completion", fake_wait_for_login_completion)
    monkeypatch.setattr("app.services.fanbox._storage_state_path", lambda _settings: storage_state_path)

    settings = Settings(
        creator_url="https://www.fanbox.cc/",
        profile_dir=str(tmp_path / "profile"),
        playwright_channel="msedge",
    )

    message = asyncio.run(open_login_window(settings))

    assert message == "Login completed and exported persistent state."
    assert playwright.launch_kwargs is not None
    assert playwright.launch_kwargs["headless"] is False
    assert playwright.launch_kwargs["channel"] == "msedge"
    assert playwright.launch_kwargs["no_viewport"] is True
    assert playwright.launch_kwargs["args"] == ["--start-maximized"]
    assert page.call_order[:3] == ["focus_helper", "goto", "wait_for_login_completion"]
    assert page.goto_calls == [(LOGIN_PAGE_URL, "domcontentloaded", None)]
    assert focus_calls == [(page, {11, 22})]
    assert wait_calls == [(context, page, 180)]
    assert context.storage_state_paths == [str(storage_state_path)]
    assert context.closed is True


def test_open_login_window_recovers_storage_state_after_closed_window(monkeypatch, tmp_path: Path) -> None:
    page = _FakeOpenLoginPage()
    context = _FakeOpenLoginContext(page)
    playwright = _FakeOpenLoginPlaywright(context)
    recover_calls: list[tuple[object, str]] = []

    async def fake_get_async_playwright():
        return _FakeAsyncPlaywrightFactory(playwright)

    async def fake_wait_for_login_completion(_context_obj, _page_obj, timeout_seconds: int) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    async def fake_recover(playwright_obj, _settings, profile_dir: Path) -> None:
        recover_calls.append((playwright_obj, str(profile_dir)))

    monkeypatch.setattr("app.services.fanbox._get_async_playwright", fake_get_async_playwright)
    monkeypatch.setattr("app.services.fanbox._is_windows_desktop", lambda: False)
    monkeypatch.setattr("app.services.fanbox._wait_for_login_completion", fake_wait_for_login_completion)
    monkeypatch.setattr("app.services.fanbox._export_storage_state_after_login_window_closed", fake_recover)

    settings = Settings(
        creator_url="https://www.fanbox.cc/",
        profile_dir=str(tmp_path / "profile"),
        playwright_channel="msedge",
    )

    message = asyncio.run(open_login_window(settings))

    assert message == "Login completed and exported persistent state."
    assert recover_calls == [(playwright, str(tmp_path / "profile"))]
    assert context.closed is True


def test_inspect_auth_status_verifies_fanbox_session(monkeypatch, tmp_path: Path) -> None:
    page = _FakeOpenLoginPage(goto_result_url=AUTH_CHECK_URL, body_text="Notifications")
    context = _FakeOpenLoginContext(page)
    playwright = _FakeOpenLoginPlaywright(context)
    storage_state_path = tmp_path / "profile" / "storage_state.json"
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text("{}", encoding="utf-8")
    (storage_state_path.parent / "Preferences").write_text("{}", encoding="utf-8")

    async def fake_get_async_playwright():
        return _FakeAsyncPlaywrightFactory(playwright)

    monkeypatch.setattr("app.services.fanbox._get_async_playwright", fake_get_async_playwright)

    settings = Settings(
        creator_url="https://www.fanbox.cc/",
        profile_dir=str(storage_state_path.parent),
        playwright_channel="msedge",
    )

    authenticated, reason = asyncio.run(inspect_auth_status(settings))

    assert authenticated is True
    assert reason == "Persistent profile, exported state, and Fanbox login are valid."
    assert playwright.launch_kwargs is not None
    assert playwright.launch_kwargs["headless"] is True
    assert page.goto_calls == [(AUTH_CHECK_URL, "domcontentloaded", AUTH_CHECK_TIMEOUT_MS)]
    assert page.closed is True
    assert context.closed is True


def test_probe_fanbox_login_state_uses_page_navigation() -> None:
    page = _FakeOpenLoginPage(goto_result_url=AUTH_CHECK_URL, body_text="Notifications")
    context = _FakeOpenLoginContext(page)

    authenticated, reason = asyncio.run(_probe_fanbox_login_state(context))
    assert authenticated is True
    assert reason is None
    assert page.goto_calls == [(AUTH_CHECK_URL, "domcontentloaded", AUTH_CHECK_TIMEOUT_MS)]
    assert page.closed is True


def test_probe_fanbox_login_state_reports_redirected_login_page() -> None:
    page = _FakeOpenLoginPage(goto_result_url="https://accounts.pixiv.net/login?prompt=select_account")
    context = _FakeOpenLoginContext(page)

    authenticated, reason = asyncio.run(_probe_fanbox_login_state(context))

    assert authenticated is False
    assert reason == "Fanbox redirected to the Pixiv login page."
    assert page.closed is True


def test_redact_sensitive_request_details_removes_cookie_header() -> None:
    message = "Call log:\n  - cookie: FANBOXSESSID=secret; cf_clearance=secret\n  - accept: */*"

    redacted = _redact_sensitive_request_details(message)

    assert "FANBOXSESSID" not in redacted
    assert "cf_clearance" not in redacted
    assert "  - cookie: [redacted]" in redacted


class _RetryableLaunchError(RuntimeError):
    pass


class _FakeRetryChromium:
    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeRetryPlaywright:
    def __init__(self, chromium: _FakeRetryChromium) -> None:
        self.chromium = chromium


def test_launch_persistent_context_with_retry_retries_closed_browser(monkeypatch, tmp_path: Path) -> None:
    chromium = _FakeRetryChromium(
        [
            _RetryableLaunchError("BrowserType.launch_persistent_context: Target page, context or browser has been closed"),
            object(),
        ]
    )
    playwright = _FakeRetryPlaywright(chromium)
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("app.services.fanbox.asyncio.sleep", fake_sleep)

    context = asyncio.run(
        _launch_persistent_context_with_retry(
            playwright,
            profile_dir=tmp_path / "profile",
            channel="msedge",
            headless=True,
        )
    )

    assert context is not None
    assert len(chromium.calls) == 2
    assert sleep_calls == [0.35]


def test_launch_persistent_context_with_retry_uses_detected_proxy(monkeypatch, tmp_path: Path) -> None:
    chromium = _FakeRetryChromium([object()])
    playwright = _FakeRetryPlaywright(chromium)
    proxy = {"server": "http://127.0.0.1:7890", "bypass": "127.0.0.1,localhost,::1"}

    monkeypatch.setattr("app.services.fanbox._resolve_playwright_proxy_settings", lambda: proxy)

    context = asyncio.run(
        _launch_persistent_context_with_retry(
            playwright,
            profile_dir=tmp_path / "profile",
            channel="msedge",
            headless=True,
        )
    )

    assert context is not None
    assert chromium.calls[0]["proxy"] == proxy


def test_launch_persistent_context_with_retry_skips_proxy_when_unavailable(monkeypatch, tmp_path: Path) -> None:
    chromium = _FakeRetryChromium([object()])
    playwright = _FakeRetryPlaywright(chromium)

    monkeypatch.setattr("app.services.fanbox._resolve_playwright_proxy_settings", lambda: None)

    context = asyncio.run(
        _launch_persistent_context_with_retry(
            playwright,
            profile_dir=tmp_path / "profile",
            channel="msedge",
            headless=True,
        )
    )

    assert context is not None
    assert "proxy" not in chromium.calls[0]


def test_normalize_proxy_server_accepts_host_port() -> None:
    assert _normalize_proxy_server("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert _normalize_proxy_server("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert _normalize_proxy_server("") is None


def test_profile_context_lock_serializes_same_profile(tmp_path: Path) -> None:
    events: list[str] = []
    profile_dir = tmp_path / "profile"

    async def worker(name: str, delay: float) -> None:
        async with _acquire_profile_context_lock(profile_dir):
            events.append(f"{name}:enter")
            await asyncio.sleep(delay)
            events.append(f"{name}:exit")

    async def run_workers() -> None:
        await asyncio.gather(worker("first", 0.01), worker("second", 0.0))

    asyncio.run(run_workers())

    assert events == ["first:enter", "first:exit", "second:enter", "second:exit"]


def test_logout_fanbox_removes_profile_directory(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "storage_state.json").write_text("{}", encoding="utf-8")
    (profile_dir / "Preferences").write_text("{}", encoding="utf-8")

    settings = Settings(
        creator_url="https://www.fanbox.cc/",
        profile_dir=str(profile_dir),
        playwright_channel="msedge",
    )

    message = asyncio.run(logout_fanbox(settings))

    assert message == "Fanbox login state cleared."
    assert profile_dir.exists() is False
