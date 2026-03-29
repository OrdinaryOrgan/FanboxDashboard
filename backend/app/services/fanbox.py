from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.db.models import PostStatus, RefreshMode, Settings

PAGE_FETCH_CONCURRENCY = 4
PAGE_FETCH_LIMIT = 100


class FanboxAuthError(RuntimeError):
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
    return True, "Persistent profile and exported state are present."


async def open_login_window(settings: Settings) -> str:
    async_playwright = await _get_async_playwright()
    profile_dir = Path(settings.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=settings.playwright_channel,
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(settings.creator_url, wait_until="domcontentloaded")
            await asyncio.sleep(180)
            await context.storage_state(path=str(_storage_state_path(settings)))
        finally:
            await context.close()
    return "Login window opened. Please finish login within 3 minutes."


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

    async_playwright = await _get_async_playwright()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=settings.playwright_channel,
            headless=True,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(settings.creator_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            await _ensure_authenticated(page)
            paginate_urls = await _fetch_paginated_api_urls(context, settings.creator_url)
            if not paginate_urls:
                raise FanboxAuthError("No Fanbox API pagination entries were returned.")

            async def fetch_api_page(_, creator_url: str, page_number: int) -> tuple[list[ScrapedPost], int | None]:
                if page_number < 1 or page_number > len(paginate_urls):
                    return [], len(paginate_urls)
                posts = await _fetch_posts_from_api_url(context, creator_url, paginate_urls[page_number - 1])
                return posts, len(paginate_urls)

            if mode == RefreshMode.INCREMENTAL:
                posts = await _collect_posts_incremental(
                    context,
                    settings.creator_url,
                    existing_posts=existing_posts or {},
                    limit=limit,
                    progress_callback=progress_callback,
                    fetch_page_fn=fetch_api_page,
                )
            else:
                posts = await _collect_posts_full(
                    context,
                    settings.creator_url,
                    limit=limit,
                    concurrency=PAGE_FETCH_CONCURRENCY,
                    progress_callback=progress_callback,
                    fetch_page_fn=fetch_api_page,
                )
            if posts:
                return posts
            raise FanboxAuthError("No posts were parsed from the list page.")
        finally:
            await context.storage_state(path=str(_storage_state_path(settings)))
            await context.close()


async def export_storage_state(settings: Settings) -> str:
    async_playwright = await _get_async_playwright()
    profile_dir = Path(settings.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=settings.playwright_channel,
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(settings.creator_url, wait_until="domcontentloaded", timeout=20000)
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
    creator_id = _extract_creator_id(creator_url)
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
    response = await context.request.get(
        api_url,
        headers={
            "Accept": "application/json",
            "Origin": origin,
            "Referer": _build_posts_page_url(creator_url, 1),
        },
    )
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


async def _dismiss_age_confirmation(page) -> None:
    modal = page.locator("div.ConfirmAdultContentModal__ButtonWrapper-sc-10ovg9m-5")
    if await modal.count() == 0:
        return
    yes_button = modal.locator("button").last
    if await yes_button.count():
        await yes_button.click(force=True)


async def _get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FanboxDependencyError(
            "Playwright is not installed. Run `python -m pip install -r backend/requirements.txt` and `python -m playwright install chromium`."
        ) from exc
    return async_playwright
