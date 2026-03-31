import asyncio

from app.services.fanbox import FanboxAuthError, _wait_for_login_completion


class _FakeBodyLocator:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def inner_text(self) -> str:
        return self._page.states[self._page.index][1]


class _FakePage:
    def __init__(self, states: list[tuple[str, str]], sleep_seconds: float = 0.0) -> None:
        self.states = states
        self.index = 0
        self.url = states[0][0]
        self._sleep_seconds = sleep_seconds

    def locator(self, selector: str) -> _FakeBodyLocator:
        assert selector == "body"
        return _FakeBodyLocator(self)

    async def wait_for_timeout(self, _: int) -> None:
        if self._sleep_seconds > 0:
            await asyncio.sleep(self._sleep_seconds)
        if self.index < len(self.states) - 1:
            self.index += 1
            self.url = self.states[self.index][0]


def test_wait_for_login_completion_returns_once_page_is_authenticated(monkeypatch) -> None:
    async def _noop(_page) -> None:
        return None

    monkeypatch.setattr("app.services.fanbox._dismiss_age_confirmation", _noop)
    page = _FakePage(
        [
            ("https://accounts.pixiv.net/login", "Login Sign up"),
            ("https://siu.fanbox.cc/posts", "Fanbox creator page"),
        ]
    )

    asyncio.run(_wait_for_login_completion(page, timeout_seconds=2))

    assert page.url == "https://siu.fanbox.cc/posts"


def test_wait_for_login_completion_raises_after_timeout(monkeypatch) -> None:
    async def _noop(_page) -> None:
        return None

    monkeypatch.setattr("app.services.fanbox._dismiss_age_confirmation", _noop)
    page = _FakePage(
        [("https://accounts.pixiv.net/login", "Login Sign up")],
        sleep_seconds=0.02,
    )

    try:
        asyncio.run(_wait_for_login_completion(page, timeout_seconds=0.01))
    except FanboxAuthError as exc:
        assert "Login was not completed within" in str(exc)
    else:
        raise AssertionError("Expected FanboxAuthError to be raised after the timeout.")
