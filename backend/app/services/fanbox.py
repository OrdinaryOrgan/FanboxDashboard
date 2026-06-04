from __future__ import annotations

import asyncio
import ctypes
import re
import shutil
import socket
import threading
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies

from app.core.default_settings import default_setting_value
from app.db.models import PostStatus, RefreshMode, Settings

PAGE_FETCH_CONCURRENCY = 4
PAGE_FETCH_LIMIT = 100
LOGIN_WINDOW_TIMEOUT_SECONDS = 180
LOGIN_WINDOW_POLL_INTERVAL_MS = 1500
DEFAULT_CREATOR_URL = str(default_setting_value("creator_url"))
LOGIN_PAGE_URL = "https://www.fanbox.cc/login"
AUTH_CHECK_URL = "https://www.fanbox.cc/notifications"
AUTH_CHECK_TIMEOUT_MS = 20000
PERSISTENT_CONTEXT_LAUNCH_RETRY_DELAYS_SECONDS = (0.35, 0.9, 1.8)
CHROMIUM_WINDOW_CLASSES = {"Chrome_WidgetWin_0", "Chrome_WidgetWin_1"}
PLAYWRIGHT_PROXY_BYPASS = "127.0.0.1,localhost,::1"
PROXY_CONNECT_TIMEOUT_SECONDS = 0.35
_PROFILE_CONTEXT_LOCKS: dict[str, asyncio.Lock] = {}
_PROFILE_CONTEXT_LOCKS_GUARD = threading.Lock()


class FanboxAuthError(RuntimeError):
    pass


class FanboxConfigurationError(RuntimeError):
    pass


class FanboxDependencyError(RuntimeError):
    pass


@dataclass
class ScrapedPost:
    post_id: str
    title: str
    detail_url: str
    published_at: datetime | None
    mega_url: str | None
    status: str
    cover_url: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ExistingPostSnapshot:
    title: str
    published_at: datetime | None
    detail_url: str
    mega_url: str | None
    cover_url: str | None = None


def _extract_post_id(url: str) -> str:
    match = re.search(r"/posts/(\d+)", url)
    if match:
        return match.group(1)
    return url


def _storage_state_path(settings: Settings) -> Path:
    return Path(settings.profile_dir) / "storage_state.json"


async def inspect_auth_status(settings: Settings) -> tuple[bool, str | None]:
    profile_dir = Path(settings.profile_dir)
    if not profile_dir.exists() or not any(profile_dir.iterdir()):
        return False, "No persistent profile found. Please login first."

    if not _storage_state_path(settings).exists():
        return False, "No exported storage state found. Please re-open the login window once."

    async_playwright = await _get_async_playwright()
    async with _acquire_profile_context_lock(profile_dir):
        async with async_playwright() as playwright:
            context = await _launch_persistent_context_with_retry(
                playwright,
                profile_dir=profile_dir,
                channel=settings.playwright_channel,
                headless=True,
            )
            try:
                authenticated, reason = await _probe_fanbox_login_state(context)
                if authenticated:
                    return True, "Persistent profile, exported state, and Fanbox login are valid."
                return False, reason or "Stored browser state is present, but Fanbox login could not be verified."
            finally:
                await context.close()


async def logout_fanbox(settings: Settings) -> str:
    profile_dir = Path(settings.profile_dir)
    async with _acquire_profile_context_lock(profile_dir):
        if profile_dir.exists():
            if profile_dir.is_dir():
                shutil.rmtree(profile_dir)
            else:
                profile_dir.unlink()
    return "Fanbox login state cleared."


async def open_login_window(settings: Settings) -> str:
    async_playwright = await _get_async_playwright()
    profile_dir = Path(settings.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_options = {
        "user_data_dir": str(profile_dir),
        "channel": settings.playwright_channel,
        "headless": False,
    }
    existing_window_handles = set()

    if _is_windows_desktop():
        launch_options["args"] = ["--start-maximized"]
        launch_options["no_viewport"] = True
        existing_window_handles = _snapshot_chromium_window_handles()

    async with _acquire_profile_context_lock(profile_dir):
        async with async_playwright() as playwright:
            context = await _launch_persistent_context_with_retry(playwright, **launch_options)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await _focus_new_chromium_window(page, existing_window_handles)
                await page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded")
                await _wait_for_login_completion(context, page, timeout_seconds=LOGIN_WINDOW_TIMEOUT_SECONDS)
                await page.wait_for_timeout(800)
                await context.storage_state(path=str(_storage_state_path(settings)))
            except Exception as exc:
                if _is_closed_browser_error(exc):
                    await _export_storage_state_after_login_window_closed(playwright, settings, profile_dir)
                else:
                    raise
            finally:
                await _close_context_safely(context)
    return "Login completed and exported persistent state."


async def refresh_posts(
    settings: Settings,
    mode: RefreshMode = RefreshMode.FULL,
    limit: int | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    existing_posts: Mapping[str, ExistingPostSnapshot] | None = None,
) -> list[ScrapedPost]:
    profile_dir = Path(settings.profile_dir)
    if not profile_dir.exists() or not any(profile_dir.iterdir()):
        raise FanboxAuthError("No persistent profile found. Please login first.")
    creator_url = settings.creator_url.strip()
    _validate_creator_url_for_refresh(creator_url)

    async_playwright = await _get_async_playwright()
    async with _acquire_profile_context_lock(profile_dir):
        async with async_playwright() as playwright:
            context = await _launch_persistent_context_with_retry(
                playwright,
                profile_dir=profile_dir,
                channel=settings.playwright_channel,
                headless=True,
            )
            try:
                authenticated, reason = await _probe_fanbox_login_state(context)
                if not authenticated:
                    raise FanboxAuthError(reason or "Current auth state could not be verified.")

                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(creator_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)
                paginate_urls = await _fetch_paginated_api_urls(context, creator_url)
                if not paginate_urls:
                    raise FanboxConfigurationError("当前 Fanbox 页面地址无法读取帖子分页信息，请确认填写的是创作者主页。")

                async def fetch_api_page(_, creator_url: str, page_number: int) -> tuple[list[ScrapedPost], int | None]:
                    if page_number < 1 or page_number > len(paginate_urls):
                        return [], len(paginate_urls)
                    posts = await _fetch_posts_from_api_url(context, creator_url, paginate_urls[page_number - 1])
                    return posts, len(paginate_urls)

                if mode == RefreshMode.INCREMENTAL:
                    posts = await _collect_posts_incremental(
                        context,
                        creator_url,
                        existing_posts=existing_posts or {},
                        limit=limit,
                        progress_callback=progress_callback,
                        fetch_page_fn=fetch_api_page,
                    )
                else:
                    posts = await _collect_posts_full(
                        context,
                        creator_url,
                        limit=limit,
                        concurrency=PAGE_FETCH_CONCURRENCY,
                        progress_callback=progress_callback,
                        fetch_page_fn=fetch_api_page,
                    )
                if posts:
                    return posts
                raise FanboxConfigurationError("当前 Fanbox 页面地址没有解析到帖子内容，请检查是否填写了正确的创作者主页。")
            finally:
                await context.storage_state(path=str(_storage_state_path(settings)))
                await context.close()


async def export_storage_state(settings: Settings) -> str:
    async_playwright = await _get_async_playwright()
    profile_dir = Path(settings.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with _acquire_profile_context_lock(profile_dir):
        async with async_playwright() as playwright:
            context = await _launch_persistent_context_with_retry(
                playwright,
                profile_dir=profile_dir,
                channel=settings.playwright_channel,
                headless=False,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)
                await context.storage_state(path=str(_storage_state_path(settings)))
            finally:
                await context.close()
    return str(_storage_state_path(settings))


async def _collect_posts_from_list(page, limit: int | None = None) -> list[ScrapedPost]:
    posts = await _collect_posts_from_cards(page, limit)
    if posts:
        return posts
    return await _collect_posts_from_anchors(page, limit)


async def _collect_posts_from_anchors(page, limit: int | None = None) -> list[ScrapedPost]:
    anchors = page.locator("a[href*='/posts/']")
    count = await anchors.count()
    results: list[ScrapedPost] = []
    seen_ids: set[str] = set()

    for index in range(count):
        anchor = anchors.nth(index)
        href = await anchor.get_attribute("href")
        if not href:
            continue
        detail_url = urljoin(page.url, href)
        post_id = _extract_post_id(detail_url)
        if post_id in seen_ids:
            continue

        text = (await anchor.inner_text()).strip()
        parsed = _parse_post_summary_text(text)
        if parsed is None:
            continue

        seen_ids.add(post_id)
        results.append(
            ScrapedPost(
                post_id=post_id,
                title=parsed["title"],
                detail_url=detail_url,
                published_at=parsed["published_at"],
                mega_url=parsed["mega_url"],
                cover_url=None,
                status=PostStatus.MISSING_LOCAL.value if parsed["mega_url"] else PostStatus.FAILED_PARSE.value,
                last_error=None if parsed["mega_url"] else "No MEGA link found in list item.",
            )
        )
        if limit is not None and len(results) >= limit:
            break

    return results


async def _collect_posts_from_cards(page, limit: int | None = None) -> list[ScrapedPost]:
    cover_links = page.locator("a[class*='PostCover__StyledLink']")
    cards = page.locator("div[class*='CardPostItem__WhiteBox']")
    count = min(await cover_links.count(), await cards.count())
    results: list[ScrapedPost] = []
    seen_ids: set[str] = set()

    for index in range(count):
        cover_link = cover_links.nth(index)
        card = cards.nth(index)
        href = await cover_link.get_attribute("href")
        if not href:
            continue

        detail_url = urljoin(page.url, href)
        post_id = _extract_post_id(detail_url)
        if post_id in seen_ids:
            continue

        title = await _inner_text_or_none(card.locator("div[class*='styled__Title']").first)
        if not title:
            title = f"Post {post_id}"

        published_raw = await _inner_text_or_none(card.locator("div[class*='styled__PublishedDatetime']").first)
        published_at = _parse_local_datetime(published_raw) if published_raw else None

        excerpt = await _inner_text_or_none(card.locator("div[class*='CardPostItem__CardExcerpt']").first)
        if not excerpt:
            excerpt = await card.inner_text()
        mega_url = _extract_mega_url_from_text(excerpt)
        if not mega_url:
            continue

        seen_ids.add(post_id)
        results.append(
            ScrapedPost(
                post_id=post_id,
                title=title,
                detail_url=detail_url,
                published_at=published_at,
                mega_url=mega_url,
                cover_url=await _extract_cover_url(cover_link),
                status=PostStatus.MISSING_LOCAL.value,
                last_error=None,
            )
        )
        if limit is not None and len(results) >= limit:
            break

    return results


async def _fetch_posts_page(context, creator_url: str, page_number: int) -> tuple[list[ScrapedPost], int | None]:
    page = await context.new_page()
    try:
        target_url = _build_posts_page_url(creator_url, page_number)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        await _ensure_authenticated(page)
        return await _collect_posts_from_list(page), await _extract_total_pages_hint(page, creator_url)
    finally:
        await page.close()


async def _fetch_paginated_api_urls(context, creator_url: str) -> list[str]:
    try:
        creator_id = _extract_creator_id(creator_url)
    except FanboxDependencyError as exc:
        raise FanboxConfigurationError("当前 Fanbox 页面地址无法识别创作者信息，请确认填写的是创作者主页。") from exc
    payload = await _fetch_api_json(
        context,
        creator_url,
        f"https://api.fanbox.cc/post.paginateCreator?creatorId={creator_id}&sort=newest",
    )
    body = payload.get("body")
    if not isinstance(body, list):
        return []
    return [str(url) for url in body if isinstance(url, str) and url]


async def _fetch_posts_from_api_url(context, creator_url: str, api_url: str) -> list[ScrapedPost]:
    payload = await _fetch_api_json(context, creator_url, api_url)
    body = payload.get("body")
    if not isinstance(body, list):
        return []
    results: list[ScrapedPost] = []
    seen_ids: set[str] = set()
    for item in body:
        post = _parse_api_post_item(item, creator_url)
        if post is None or post.post_id in seen_ids:
            continue
        seen_ids.add(post.post_id)
        results.append(post)
    return results


async def _fetch_api_json(context, creator_url: str, api_url: str) -> dict:
    parts = urlsplit(creator_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        response = await context.request.get(
            api_url,
            headers={
                "Accept": "application/json",
                "Origin": origin,
                "Referer": _build_posts_page_url(creator_url, 1),
            },
        )
    except Exception as exc:
        raise FanboxAuthError(f"Fanbox API request failed: {_redact_sensitive_request_details(str(exc))}") from exc
    payload = await response.json()
    if response.status >= 400:
        raise FanboxAuthError(f"Fanbox API request failed: {response.status}")
    if not isinstance(payload, dict):
        raise FanboxAuthError("Fanbox API returned an unexpected payload.")
    return payload


def _parse_api_post_item(item: object, creator_url: str) -> ScrapedPost | None:
    if not isinstance(item, dict):
        return None

    post_id = str(item.get("id") or "").strip()
    if not post_id:
        return None

    excerpt = item.get("excerpt")
    mega_url = _extract_mega_url_from_text(excerpt) if isinstance(excerpt, str) else None
    if not mega_url:
        return None

    title = str(item.get("title") or f"Post {post_id}").strip() or f"Post {post_id}"
    published_at = _parse_api_datetime(item.get("publishedDatetime"))
    cover_url = _extract_cover_url_from_api_item(item)

    return ScrapedPost(
        post_id=post_id,
        title=title,
        detail_url=_build_post_detail_url(creator_url, post_id),
        published_at=published_at,
        mega_url=mega_url,
        status=PostStatus.MISSING_LOCAL.value,
        cover_url=cover_url,
        last_error=None,
    )


async def _collect_posts_incremental(
    context,
    creator_url: str,
    existing_posts: Mapping[str, ExistingPostSnapshot],
    limit: int | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    fetch_page_fn: Callable[[object, str, int], Awaitable[tuple[list[ScrapedPost], int | None]]] = _fetch_posts_page,
) -> list[ScrapedPost]:
    results: list[ScrapedPost] = []
    seen_ids: set[str] = set()
    processed_pages = 0
    discovered_total = 1
    current_page = 1
    consecutive_known_pages = 0

    while current_page <= min(discovered_total, PAGE_FETCH_LIMIT):
        page_posts, page_total_hint = await fetch_page_fn(context, creator_url, current_page)
        processed_pages += 1
        if page_total_hint is not None:
            discovered_total = max(discovered_total, page_total_hint)
        if progress_callback is not None:
            progress_callback(processed_pages, None)

        if _is_known_page(page_posts, existing_posts):
            consecutive_known_pages += 1
        else:
            consecutive_known_pages = 0

        for post in page_posts:
            if post.post_id in seen_ids:
                continue
            seen_ids.add(post.post_id)
            results.append(post)
            if limit is not None and len(results) >= limit:
                return results

        if consecutive_known_pages >= 2:
            break
        current_page += 1

    return results


async def _collect_posts_full(
    context,
    creator_url: str,
    limit: int | None = None,
    concurrency: int = PAGE_FETCH_CONCURRENCY,
    progress_callback: Callable[[int, int | None], None] | None = None,
    fetch_page_fn: Callable[[object, str, int], Awaitable[tuple[list[ScrapedPost], int | None]]] = _fetch_posts_page,
) -> list[ScrapedPost]:
    first_page_posts, first_page_total_hint = await fetch_page_fn(context, creator_url, 1)
    discovered_total = max(1, first_page_total_hint or 1)
    results: list[ScrapedPost] = []
    seen_ids: set[str] = set()
    processed_pages = 1

    if progress_callback is not None:
        progress_callback(processed_pages, discovered_total)
    _append_unique_posts(results, seen_ids, first_page_posts)
    if limit is not None and len(results) >= limit:
        return results[:limit]

    next_page = 2
    while next_page <= min(discovered_total, PAGE_FETCH_LIMIT):
        batch = list(range(next_page, min(next_page + concurrency, discovered_total + 1, PAGE_FETCH_LIMIT + 1)))
        batch_results = await asyncio.gather(*[fetch_page_fn(context, creator_url, page_number) for page_number in batch])
        for page_posts, page_total_hint in batch_results:
            processed_pages += 1
            if page_total_hint is not None:
                discovered_total = max(discovered_total, page_total_hint)
            if progress_callback is not None:
                progress_callback(processed_pages, discovered_total)
            _append_unique_posts(results, seen_ids, page_posts)
            if limit is not None and len(results) >= limit:
                return results[:limit]
        next_page = batch[-1] + 1

    return results


def _page_number_batches(max_pages: int, batch_size: int) -> list[list[int]]:
    safe_batch_size = max(1, batch_size)
    return [
        list(range(start_page, min(start_page + safe_batch_size, max_pages + 1)))
        for start_page in range(1, max_pages + 1, safe_batch_size)
    ]


def _append_unique_posts(results: list[ScrapedPost], seen_ids: set[str], posts: list[ScrapedPost]) -> None:
    for post in posts:
        if post.post_id in seen_ids:
            continue
        seen_ids.add(post.post_id)
        results.append(post)


def _is_known_page(page_posts: list[ScrapedPost], existing_posts: Mapping[str, ExistingPostSnapshot]) -> bool:
    return all(_matches_existing_post(post, existing_posts) for post in page_posts)


def _matches_existing_post(post: ScrapedPost, existing_posts: Mapping[str, ExistingPostSnapshot]) -> bool:
    existing = existing_posts.get(post.post_id)
    if existing is None:
        return False
    return (
        existing.title == post.title
        and existing.published_at == post.published_at
        and existing.detail_url == post.detail_url
        and existing.cover_url == post.cover_url
        and existing.mega_url == post.mega_url
    )


def _build_posts_page_url(creator_url: str, page_number: int) -> str:
    parts = urlsplit(creator_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["sort"] = "newest"
    if page_number > 1:
        query["page"] = str(page_number)
    else:
        query.pop("page", None)

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def _extract_total_pages_hint(page, creator_url: str) -> int | None:
    anchors = page.locator("a[href*='page=']")
    count = await anchors.count()
    max_page: int | None = 1

    for index in range(count):
        href = await anchors.nth(index).get_attribute("href")
        if not href:
            continue
        candidate = _extract_page_number(urljoin(page.url, href), creator_url)
        if candidate is not None:
            max_page = max(max_page or 1, candidate)

    return max_page


def _extract_page_number(url: str, creator_url: str) -> int | None:
    target_parts = urlsplit(url)
    base_parts = urlsplit(creator_url)
    if target_parts.path != base_parts.path:
        return None
    query = dict(parse_qsl(target_parts.query, keep_blank_values=True))
    if "page" not in query:
        return 1
    try:
        return int(query["page"])
    except ValueError:
        return None


def _extract_creator_id(creator_url: str) -> str:
    parts = urlsplit(creator_url)
    host = parts.netloc.split(":", 1)[0]
    if host.endswith(".fanbox.cc"):
        creator_id = host[: -len(".fanbox.cc")]
        if creator_id:
            return creator_id

    path_parts = [segment for segment in parts.path.split("/") if segment]
    if path_parts:
        return path_parts[0].lstrip("@")

    raise FanboxDependencyError(f"Unable to extract creator id from URL: {creator_url}")


def _build_post_detail_url(creator_url: str, post_id: str) -> str:
    parts = urlsplit(creator_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/posts/{post_id}", "", ""))


def _parse_api_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone().replace(tzinfo=None)


def _extract_cover_url_from_api_item(item: Mapping[str, object]) -> str | None:
    cover = item.get("cover")
    if isinstance(cover, Mapping):
        url = cover.get("url")
        if isinstance(url, str) and url:
            return url
    return None


async def _collect_posts_from_details(page, limit: int | None = None) -> list[ScrapedPost]:
    anchors = page.locator("a[href*='/posts/']")
    count = await anchors.count()
    detail_urls: list[str] = []
    seen: set[str] = set()

    for index in range(count):
        href = await anchors.nth(index).get_attribute("href")
        if not href:
            continue
        detail_url = urljoin(page.url, href)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        detail_urls.append(detail_url)
        if limit is not None and len(detail_urls) >= limit:
            break

    return [await _scrape_post_detail(page.context, url) for url in detail_urls]


async def _scrape_post_detail(context, detail_url: str) -> ScrapedPost:
    detail_page = await context.new_page()
    try:
        await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
        await detail_page.wait_for_timeout(3000)
        await _dismiss_age_confirmation(detail_page)
        await detail_page.wait_for_timeout(3000)
        title = await _extract_title(detail_page)
        published_at = await _extract_published_at(detail_page)
        mega_url = await _extract_mega_url(detail_page)
        if mega_url:
            status = PostStatus.MISSING_LOCAL.value
            error = None
        else:
            status = PostStatus.FAILED_PARSE.value
            error = "No MEGA link found in post detail page."
        return ScrapedPost(
            post_id=_extract_post_id(detail_url),
            title=title,
            detail_url=detail_url,
            published_at=published_at,
            mega_url=mega_url,
            status=status,
            last_error=error,
        )
    finally:
        await detail_page.close()


def _parse_post_summary_text(text: str) -> dict[str, object] | None:
    condensed = re.sub(r"\s+", " ", text).strip()
    if "mega." not in condensed:
        return None

    published_match = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2})", condensed)
    mega_match = re.search(r"(https://mega\.[^\s]+)", condensed)
    if not mega_match:
        return None

    published_at = None
    if published_match:
        published_at = _parse_local_datetime(published_match.group(1))

    working = condensed
    if published_match:
        working = working.replace(published_match.group(1), "", 1).strip()
    working = re.sub(r"^[¥\d,]+(?:円|日元|円/月|日元/月)?", "", working).strip()
    mega_in_working = re.search(r"(https://mega\.[^\s]+)", working)
    if "DL" in working:
        title_part = working.split("DL", 1)[0].strip()
    elif mega_in_working:
        title_part = working[: mega_in_working.start()].strip()
    else:
        title_part = working.strip()
    title_part = re.sub(r"^[¥\d,]+(?:円|日元)?", "", title_part).strip()
    title_part = re.sub(r"\s+", " ", title_part)
    if not title_part:
        title_part = "Untitled post"

    return {
        "title": title_part,
        "published_at": published_at,
        "mega_url": mega_match.group(1),
    }


def _extract_mega_url_from_text(text: str) -> str | None:
    match = re.search(r"(https://mega\.[^\s]+)", text)
    if match:
        return match.group(1)
    return None


async def _inner_text_or_none(locator) -> str | None:
    if await locator.count() == 0:
        return None
    text = (await locator.inner_text()).strip()
    return text or None


async def _extract_cover_url(cover_link) -> str | None:
    return await cover_link.evaluate(
        """node => {
            const img = node.querySelector('img');
            if (!img) return null;
            if (img.currentSrc) return img.currentSrc;
            const direct = img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-original');
            if (direct) return direct;
            const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset');
            if (!srcset) return null;
            const first = srcset.split(',')[0]?.trim().split(' ')[0];
            return first || null;
        }"""
    )


def _parse_local_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y年%m月%d日 %H:%M")
    except ValueError:
        try:
            return datetime.strptime(value, "%Y年%-m月%-d日 %H:%M")
        except ValueError:
            return None


async def _extract_published_at(page) -> datetime | None:
    time_locator = page.locator("time")
    if await time_locator.count() == 0:
        return None
    value = await time_locator.first.get_attribute("datetime")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _extract_title(page) -> str:
    title_locator = page.locator("h1")
    count = await title_locator.count()
    for index in range(count):
        text = (await title_locator.nth(index).inner_text()).strip()
        if text and text != "Siu":
            return text
    title = await page.title()
    return title.replace(" | Fanbox", "").strip() or _extract_post_id(page.url)


async def _extract_mega_url(page) -> str | None:
    body_text = await page.locator("body").inner_text()
    mega_match = re.search(r"(https://mega\.[^\s]+)", body_text)
    if mega_match:
        return mega_match.group(1)

    selectors = ["a[href*='mega.nz']", "a[href*='mega.io']", "a[href*='mega.co.nz']"]
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            href = await locator.first.get_attribute("href")
            if href:
                return href
    return None


async def _ensure_authenticated(page) -> None:
    current_url = page.url.lower()
    if "login" in current_url or "accounts.pixiv.net" in current_url:
        raise FanboxAuthError("Current login state is invalid. Please login to Fanbox again.")

    body_text = await page.locator("body").inner_text()
    login_markers = [
        ("Login", "Sign up"),
        ("ログイン", "新規登録"),
        ("登录", "注册"),
        ("登录", "现在注册"),
    ]
    for left, right in login_markers:
        if left in body_text and right in body_text:
            raise FanboxAuthError("Current auth state is not logged in. Please refresh the dedicated login window.")


def _is_login_redirect_url(url: str) -> bool:
    current_url = url.lower()
    return "accounts.pixiv.net/login" in current_url or "www.fanbox.cc/login" in current_url


def _validate_creator_url_for_refresh(creator_url: str) -> None:
    if not creator_url or creator_url == DEFAULT_CREATOR_URL:
        raise FanboxConfigurationError("尚未配置 Fanbox 页面地址，请先在设置中填写你的创作者主页。")

    parts = urlsplit(creator_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise FanboxConfigurationError("当前 Fanbox 页面地址格式无效，请重新填写完整的创作者主页链接。")


async def _probe_fanbox_login_state(context, page=None) -> tuple[bool, str | None]:
    probe_page = page
    close_probe_page = False
    try:
        if probe_page is None:
            probe_page = await context.new_page()
            close_probe_page = True
            response = await probe_page.goto(
                AUTH_CHECK_URL,
                wait_until="domcontentloaded",
                timeout=AUTH_CHECK_TIMEOUT_MS,
            )
            if _is_login_redirect_url(probe_page.url):
                return False, "Fanbox redirected to the Pixiv login page."
            if response is not None and response.status >= 400:
                return False, f"Fanbox auth check returned HTTP {response.status}."
        elif _is_login_redirect_url(probe_page.url):
            return False, "Fanbox redirected to the Pixiv login page."

        await _dismiss_age_confirmation(probe_page)
        await _ensure_authenticated(probe_page)
        return True, None
    except FanboxAuthError as exc:
        return False, _redact_sensitive_request_details(str(exc))
    except Exception as exc:
        return False, f"Unable to verify Fanbox login state: {_redact_sensitive_request_details(str(exc))}"
    finally:
        if close_probe_page and probe_page is not None:
            await _close_page_safely(probe_page)


async def _wait_for_login_completion(context, page, timeout_seconds: int) -> None:
    deadline = asyncio.get_running_loop().time() + max(1, timeout_seconds)
    last_error: str | None = None

    while asyncio.get_running_loop().time() < deadline:
        await _dismiss_age_confirmation(page)
        authenticated, reason = await _probe_fanbox_login_state(context, page)
        if authenticated:
            return
        last_error = reason
        await page.wait_for_timeout(LOGIN_WINDOW_POLL_INTERVAL_MS)

    detail = f" {last_error}" if last_error else ""
    raise FanboxAuthError(f"Login was not completed within {timeout_seconds} seconds.{detail}")


async def _export_storage_state_after_login_window_closed(playwright, settings: Settings, profile_dir: Path) -> None:
    context = await _launch_persistent_context_with_retry(
        playwright,
        profile_dir=profile_dir,
        channel=settings.playwright_channel,
        headless=True,
    )
    try:
        authenticated, reason = await _probe_fanbox_login_state(context)
        if not authenticated:
            detail = f" {reason}" if reason else ""
            raise FanboxAuthError(f"Unable to verify Fanbox login state after the login window closed.{detail}")
        await context.storage_state(path=str(_storage_state_path(settings)))
    finally:
        await _close_context_safely(context)


async def _dismiss_age_confirmation(page) -> None:
    modal = page.locator("div.ConfirmAdultContentModal__ButtonWrapper-sc-10ovg9m-5")
    if await modal.count() == 0:
        return
    yes_button = modal.locator("button").last
    if await yes_button.count():
        await yes_button.click(force=True)


async def _close_context_safely(context) -> None:
    try:
        await context.close()
    except Exception:
        return


async def _close_page_safely(page) -> None:
    try:
        await page.close()
    except Exception:
        return


def _redact_sensitive_request_details(message: str) -> str:
    return re.sub(r"(?im)(^\s*-\s*cookie:\s*).*$", r"\1[redacted]", message)


def _is_closed_browser_error(exc: Exception) -> bool:
    message = str(exc).lower()
    closed_markers = [
        "target page, context or browser has been closed",
        "target page has been closed",
        "context has been closed",
        "browser has been closed",
        "target closed",
    ]
    return any(marker in message for marker in closed_markers)


def _is_windows_desktop() -> bool:
    return hasattr(ctypes, "windll")


def _snapshot_chromium_window_handles() -> set[int]:
    return {hwnd for hwnd, _ in _list_visible_chromium_windows()}


def _list_visible_chromium_windows() -> list[tuple[int, str]]:
    if not _is_windows_desktop():
        return []

    user32 = ctypes.windll.user32
    windows: list[tuple[int, str]] = []
    enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True

        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        if class_buffer.value not in CHROMIUM_WINDOW_CLASSES:
            return True

        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True

        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
        windows.append((int(hwnd), title_buffer.value))
        return True

    callback = enum_windows_proc(_callback)
    user32.EnumWindows(callback, 0)
    return windows


async def _focus_new_chromium_window(page, existing_window_handles: set[int]) -> None:
    if not _is_windows_desktop():
        return

    hwnd = await _find_new_chromium_window(existing_window_handles, "")
    if hwnd is not None:
        _maximize_and_bring_window_to_front(hwnd)
        try:
            await page.bring_to_front()
        except Exception:
            pass


async def _find_new_chromium_window(
    existing_window_handles: set[int],
    expected_title: str,
    attempts: int = 20,
    delay_ms: int = 100,
) -> int | None:
    title_match = expected_title.casefold()
    fallback_hwnd: int | None = None

    for _ in range(max(1, attempts)):
        new_windows = [
            (hwnd, title)
            for hwnd, title in _list_visible_chromium_windows()
            if hwnd not in existing_window_handles
        ]
        if new_windows:
            if title_match:
                for hwnd, title in new_windows:
                    if title_match in title.casefold():
                        return hwnd
            if len(new_windows) == 1:
                return new_windows[0][0]
            fallback_hwnd = new_windows[0][0]
        await asyncio.sleep(delay_ms / 1000)

    return fallback_hwnd


def _maximize_and_bring_window_to_front(hwnd: int) -> None:
    if not _is_windows_desktop():
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    sw_restore = 9
    sw_maximize = 3
    swp_nosize = 0x0001
    swp_nomove = 0x0002
    hwnd_topmost = -1
    hwnd_notopmost = -2

    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    if foreground_thread:
        user32.AttachThreadInput(foreground_thread, current_thread, True)
    if target_thread:
        user32.AttachThreadInput(target_thread, current_thread, True)

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, sw_restore)
        if not user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, sw_maximize)
        user32.BringWindowToTop(hwnd)
        user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, swp_nomove | swp_nosize)
        user32.SetWindowPos(hwnd, hwnd_notopmost, 0, 0, 0, 0, swp_nomove | swp_nosize)
        user32.SetForegroundWindow(hwnd)
    finally:
        if target_thread:
            user32.AttachThreadInput(target_thread, current_thread, False)
        if foreground_thread:
            user32.AttachThreadInput(foreground_thread, current_thread, False)


async def _get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FanboxDependencyError(
            "Playwright is not installed. Run `python -m pip install -r backend/requirements.txt` and `python -m playwright install chromium`."
        ) from exc
    return async_playwright


@asynccontextmanager
async def _acquire_profile_context_lock(profile_dir: Path):
    lock = _profile_context_lock_for(profile_dir)
    async with lock:
        yield


def _profile_context_lock_for(profile_dir: Path) -> asyncio.Lock:
    key = str(profile_dir.resolve()).lower()
    with _PROFILE_CONTEXT_LOCKS_GUARD:
        lock = _PROFILE_CONTEXT_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PROFILE_CONTEXT_LOCKS[key] = lock
        return lock


async def _launch_persistent_context_with_retry(
    playwright,
    *,
    profile_dir: Path | None = None,
    channel: str,
    headless: bool,
    **extra_launch_options,
):
    launch_options = {
        "channel": channel,
        "headless": headless,
        **extra_launch_options,
    }
    if profile_dir is not None:
        launch_options["user_data_dir"] = str(profile_dir)
    if "proxy" not in launch_options:
        proxy_settings = _resolve_playwright_proxy_settings()
        if proxy_settings is not None:
            launch_options["proxy"] = proxy_settings

    for attempt, retry_delay in enumerate((0.0, *PERSISTENT_CONTEXT_LAUNCH_RETRY_DELAYS_SECONDS), start=1):
        if retry_delay > 0:
            await asyncio.sleep(retry_delay)
        try:
            return await playwright.chromium.launch_persistent_context(**launch_options)
        except Exception as exc:
            if attempt > len(PERSISTENT_CONTEXT_LAUNCH_RETRY_DELAYS_SECONDS) or not _is_retryable_persistent_context_error(exc):
                raise


def _resolve_playwright_proxy_settings() -> dict[str, str] | None:
    proxy_url = _normalize_proxy_server(getproxies().get("https") or getproxies().get("http"))
    if proxy_url is None:
        return None
    if not _is_proxy_endpoint_available(proxy_url):
        return None
    return {
        "server": proxy_url,
        "bypass": PLAYWRIGHT_PROXY_BYPASS,
    }


def _normalize_proxy_server(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    candidate = proxy_url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlsplit(candidate)
    if not parsed.hostname or parsed.port is None:
        return None
    return candidate


def _is_proxy_endpoint_available(proxy_url: str) -> bool:
    parsed = urlsplit(proxy_url)
    if not parsed.hostname or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=PROXY_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _is_retryable_persistent_context_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "launch_persistent_context" in message and (
        "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "user data directory is already in use" in message
        or "profile appears to be in use" in message
    )
