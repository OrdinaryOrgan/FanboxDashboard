import asyncio
from datetime import datetime

from app.services.fanbox import (
    ExistingPostSnapshot,
    ScrapedPost,
    _build_post_detail_url,
    _build_posts_page_url,
    _collect_posts_incremental,
    _extract_cover_url_from_api_item,
    _extract_creator_id,
    _extract_mega_url_from_text,
    _parse_api_post_item,
    _page_number_batches,
    _parse_post_summary_text,
)


def test_parse_post_summary_text_extracts_fields() -> None:
    text = (
        "2026年3月22日 08:00 500日元 信濃③ DL "
        "https://mega.nz/file/wUkQSRgI#OIKVoPiP2rQSTZKRhF6cVt9gyTqoUp6eTKpqOKRQX2A 9 89"
    )

    parsed = _parse_post_summary_text(text)

    assert parsed is not None
    assert parsed["title"] == "信濃③"
    assert parsed["mega_url"] == "https://mega.nz/file/wUkQSRgI#OIKVoPiP2rQSTZKRhF6cVt9gyTqoUp6eTKpqOKRQX2A"
    assert parsed["published_at"] == datetime(2026, 3, 22, 8, 0)


def test_parse_post_summary_text_accepts_mega_without_dl_prefix() -> None:
    text = (
        "2021年1月8日 00:58 500日元 イラストリアス輪姦② "
        "https://mega.nz/file/9BtTyRBI#hsFs7bmZqG2sepSoKu0RLbxeRw0tRIhPqgRRHrFx6vk 4 197"
    )

    parsed = _parse_post_summary_text(text)

    assert parsed is not None
    assert parsed["title"] == "イラストリアス輪姦②"
    assert parsed["mega_url"] == "https://mega.nz/file/9BtTyRBI#hsFs7bmZqG2sepSoKu0RLbxeRw0tRIhPqgRRHrFx6vk"
    assert parsed["published_at"] == datetime(2021, 1, 8, 0, 58)


def test_parse_post_summary_text_returns_none_without_mega() -> None:
    assert _parse_post_summary_text("2026年3月22日 08:00 500日元 信濃③") is None


def test_extract_mega_url_from_text_returns_first_mega_link() -> None:
    assert _extract_mega_url_from_text("preview https://mega.nz/file/test#abc trailing") == "https://mega.nz/file/test#abc"


def test_extract_creator_id_from_subdomain_url() -> None:
    assert _extract_creator_id("https://siu.fanbox.cc/posts") == "siu"


def test_build_post_detail_url_uses_same_host() -> None:
    assert _build_post_detail_url("https://siu.fanbox.cc/posts?sort=newest", "11560240") == "https://siu.fanbox.cc/posts/11560240"


def test_extract_cover_url_from_api_item() -> None:
    assert (
        _extract_cover_url_from_api_item({"cover": {"type": "cover_image", "url": "https://example.com/cover.jpg"}})
        == "https://example.com/cover.jpg"
    )


def test_parse_api_post_item_returns_cover_and_mega() -> None:
    post = _parse_api_post_item(
        {
            "id": "11560240",
            "title": "淇℃績鈶?",
            "publishedDatetime": "2026-03-23T00:00:00+09:00",
            "cover": {"type": "cover_image", "url": "https://example.com/cover.jpg"},
            "excerpt": "DL\\nhttps://mega.nz/file/wUkQSRgI#abc",
        },
        "https://siu.fanbox.cc/posts",
    )

    assert post is not None
    assert post.post_id == "11560240"
    assert post.cover_url == "https://example.com/cover.jpg"
    assert post.mega_url == "https://mega.nz/file/wUkQSRgI#abc"
    assert post.detail_url == "https://siu.fanbox.cc/posts/11560240"


def test_parse_api_post_item_converts_published_datetime_to_local_time() -> None:
    post = _parse_api_post_item(
        {
            "id": "11143157",
            "title": "Sample",
            "publishedDatetime": "2026-01-01T00:00:11+09:00",
            "cover": {"type": "cover_image", "url": "https://example.com/cover.jpg"},
            "excerpt": "https://mega.nz/file/test#abc",
        },
        "https://siu.fanbox.cc/posts",
    )

    assert post is not None
    assert post.published_at == datetime.fromisoformat("2026-01-01T00:00:11+09:00").astimezone().replace(tzinfo=None)


def test_build_posts_page_url_uses_newest_sort_and_page() -> None:
    assert _build_posts_page_url("https://siu.fanbox.cc/posts", 1) == "https://siu.fanbox.cc/posts?sort=newest"
    assert (
        _build_posts_page_url("https://siu.fanbox.cc/posts", 2)
        == "https://siu.fanbox.cc/posts?sort=newest&page=2"
    )


def test_build_posts_page_url_preserves_other_query_params() -> None:
    assert (
        _build_posts_page_url("https://siu.fanbox.cc/posts?foo=bar&page=7", 3)
        == "https://siu.fanbox.cc/posts?foo=bar&page=3&sort=newest"
    )


def test_page_number_batches_groups_pages_by_batch_size() -> None:
    assert _page_number_batches(10, 4) == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]


def test_incremental_refresh_stops_after_two_known_pages() -> None:
    existing_posts = {
        "p1": ExistingPostSnapshot("A", datetime(2026, 3, 1, 8, 0), "https://example/p1", "https://mega.nz/file/1"),
        "p2": ExistingPostSnapshot("B", datetime(2026, 3, 1, 8, 0), "https://example/p2", "https://mega.nz/file/2"),
    }
    calls: list[int] = []

    async def fake_fetch_page(_, __, page_number: int):
        calls.append(page_number)
        if page_number == 1:
            return (
                [ScrapedPost("p1", "A", "https://example/p1", datetime(2026, 3, 1, 8, 0), "https://mega.nz/file/1", "missing_local")],
                5,
            )
        if page_number == 2:
            return (
                [ScrapedPost("p2", "B", "https://example/p2", datetime(2026, 3, 1, 8, 0), "https://mega.nz/file/2", "missing_local")],
                5,
            )
        raise AssertionError("Incremental refresh should stop after two known pages.")

    results = asyncio.run(
        _collect_posts_incremental(None, "https://siu.fanbox.cc/posts", existing_posts, fetch_page_fn=fake_fetch_page)
    )

    assert calls == [1, 2]
    assert [post.post_id for post in results] == ["p1", "p2"]


def test_incremental_refresh_continues_when_second_page_has_new_post() -> None:
    existing_posts = {
        "p1": ExistingPostSnapshot("A", datetime(2026, 3, 1, 8, 0), "https://example/p1", "https://mega.nz/file/1"),
        "p3": ExistingPostSnapshot("C", datetime(2026, 3, 1, 8, 0), "https://example/p3", "https://mega.nz/file/3"),
        "p4": ExistingPostSnapshot("D", datetime(2026, 3, 1, 8, 0), "https://example/p4", "https://mega.nz/file/4"),
    }
    calls: list[int] = []

    async def fake_fetch_page(_, __, page_number: int):
        calls.append(page_number)
        if page_number == 1:
            return (
                [ScrapedPost("p1", "A", "https://example/p1", datetime(2026, 3, 1, 8, 0), "https://mega.nz/file/1", "missing_local")],
                5,
            )
        if page_number == 2:
            return (
                [ScrapedPost("p2", "NEW", "https://example/p2", datetime(2026, 3, 2, 8, 0), "https://mega.nz/file/2", "missing_local")],
                5,
            )
        if page_number == 3:
            return (
                [ScrapedPost("p3", "C", "https://example/p3", datetime(2026, 3, 1, 8, 0), "https://mega.nz/file/3", "missing_local")],
                5,
            )
        if page_number == 4:
            return (
                [ScrapedPost("p4", "D", "https://example/p4", datetime(2026, 3, 1, 8, 0), "https://mega.nz/file/4", "missing_local")],
                5,
            )
        raise AssertionError("Incremental refresh should stop after the next two known pages.")

    results = asyncio.run(
        _collect_posts_incremental(None, "https://siu.fanbox.cc/posts", existing_posts, fetch_page_fn=fake_fetch_page)
    )

    assert calls == [1, 2, 3, 4]
    assert [post.post_id for post in results] == ["p1", "p2", "p3", "p4"]
