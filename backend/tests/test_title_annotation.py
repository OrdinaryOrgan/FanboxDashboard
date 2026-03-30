from pathlib import Path
from types import SimpleNamespace

from app.db.models import TitleAnnotationSource, TitleAnnotationStatus
from app.services.title_annotation import (
    LlmConfig,
    TitleAnnotationError,
    _build_llm_messages,
    build_display_title,
    load_llm_config,
    load_title_aliases,
    resolve_title_annotation,
    title_needs_annotation,
)


def test_title_needs_annotation_detects_kana() -> None:
    assert title_needs_annotation("ベルファスト⑦") is True
    assert title_needs_annotation("癒○ちょこ①") is True
    assert title_needs_annotation("武蔵①") is False


def test_build_display_title_combines_original_and_annotation() -> None:
    assert build_display_title("ベルファスト⑦", "贝尔法斯特⑦") == "ベルファスト⑦（贝尔法斯特⑦）"
    assert build_display_title("武蔵①", None) == "武蔵①"


def test_build_display_title_ignores_annotation_when_only_unicode_normalization_differs() -> None:
    assert build_display_title("ジェーン・ドゥ②", "ジェーン・ドゥ②") == "ジェーン・ドゥ②"


def test_dictionary_full_title_match_wins(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        '{"version":1,"full_titles":{"ベルファスト⑦":"贝尔法斯特⑦"},"phrase_fragments":{},"fragments":{"ベルファスト":"不应命中"}}',
        encoding="utf-8",
    )

    result = resolve_title_annotation("ベルファスト⑦", aliases=load_title_aliases(alias_path))

    assert result.annotation == "贝尔法斯特⑦"
    assert result.source == TitleAnnotationSource.DICTIONARY_FULL.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_dictionary_full_title_match_handles_request_suffix_title(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        '{"version":1,"full_titles":{"インプラカブルxアルヴィト①request":"怨仇×阿尔维特①约稿"},"phrase_fragments":{},"fragments":{}}',
        encoding="utf-8",
    )

    result = resolve_title_annotation("インプラカブルxアルヴィト①request", aliases=load_title_aliases(alias_path))

    assert result.annotation == "怨仇×阿尔维特①约稿"
    assert result.source == TitleAnnotationSource.DICTIONARY_FULL.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_dictionary_fragments_replace_all_kana_spans(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        '{"version":1,"full_titles":{},"phrase_fragments":{},"fragments":{"アスナ":"阿斯娜"}}',
        encoding="utf-8",
    )

    result = resolve_title_annotation("一之瀬アスナ①", aliases=load_title_aliases(alias_path))

    assert result.annotation == "一之瀬阿斯娜①"
    assert result.source == TitleAnnotationSource.DICTIONARY_FRAGMENT.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_dictionary_fragments_preserve_masked_kanji_prefix_for_choco_titles(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        '{"version":1,"full_titles":{},"phrase_fragments":{},"fragments":{"ちょこ":"Choco"}}',
        encoding="utf-8",
    )

    result = resolve_title_annotation("癒○ちょこ①", aliases=load_title_aliases(alias_path))

    assert result.annotation == "癒○Choco①"
    assert result.source == TitleAnnotationSource.DICTIONARY_FRAGMENT.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_dictionary_phrase_fragments_take_priority_over_plain_fragments(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        (
            '{"version":1,"full_titles":{},"phrase_fragments":{"一之瀬アスナ":"一之瀬明日奈","調月リオ":"調月莉央"},'
            '"fragments":{"アスナ":"亚丝娜","リオ":"莉央"}}'
        ),
        encoding="utf-8",
    )

    result = resolve_title_annotation("一之瀬アスナx調月リオ①", aliases=load_title_aliases(alias_path))

    assert result.annotation == "一之瀬明日奈x調月莉央①"
    assert result.source == TitleAnnotationSource.DICTIONARY_PHRASE.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_dictionary_phrase_fragments_match_after_unicode_normalization(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        (
            '{"version":1,"full_titles":{},"phrase_fragments":{"一之瀬アスナ":"一之瀬明日奈"},'
            '"fragments":{"リオ":"莉央"}}'
        ),
        encoding="utf-8",
    )

    result = resolve_title_annotation("一之瀬アスナx調月リオ①", aliases=load_title_aliases(alias_path))

    assert result.annotation == "一之瀬明日奈x調月莉央①"
    assert result.source == TitleAnnotationSource.DICTIONARY_PHRASE.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_skips_pure_kanji() -> None:
    result = resolve_title_annotation("武蔵①")

    assert result.annotation is None
    assert result.source == TitleAnnotationSource.NONE.value
    assert result.status == TitleAnnotationStatus.SKIPPED.value


def test_resolve_title_annotation_returns_failed_when_llm_not_configured(tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key=None,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    result = resolve_title_annotation("ベルファスト⑦", aliases=load_title_aliases(alias_path), llm_config=llm_config)

    assert result.annotation is None
    assert result.status == TitleAnnotationStatus.FAILED.value
    assert "not configured" in (result.error or "")


def test_resolve_title_annotation_accepts_mocked_llm_result(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_annotate_title_with_llm(title: str, config: LlmConfig) -> str:
        assert title == "ベルファスト⑦"
        assert config.api_key == "test-key"
        return "贝尔法斯特⑦"

    monkeypatch.setattr("app.services.title_annotation._annotate_title_with_llm", fake_annotate_title_with_llm)

    result = resolve_title_annotation("ベルファスト⑦", aliases=load_title_aliases(alias_path), llm_config=llm_config)

    assert result.annotation == "贝尔法斯特⑦"
    assert result.source == TitleAnnotationSource.LLM.value
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_extracts_annotation_from_wrapped_llm_result(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"クラスヌィイ・カフカース①（红色高加索）","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "クラスヌィイ・カフカース①",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "红色高加索①"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_extracts_wrapped_annotation_when_prefix_only_differs_by_normalization(
    monkeypatch, tmp_path: Path
) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"ジェーン・ドゥ①（Jane Doe①）","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "ジェーン・ドゥ①",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "Jane Doe①"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_uses_fallback_when_primary_only_changes_unicode_normalization(
    monkeypatch, tmp_path: Path
) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )
    calls = {"count": 0}

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            content = '{"annotated_title":"ジェーン・ドゥ②","confidence":"medium"}'
        else:
            content = '{"annotated_title":"Jane Doe②","confidence":"medium"}'
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "ジェーン・ドゥ②",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert calls["count"] == 2
    assert result.annotation == "Jane Doe②"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_build_display_title_hides_annotation_when_only_suffix_variant_matches() -> None:
    assert build_display_title("ジェーン・ドゥ①", "ジェーン・ドゥ①") == "ジェーン・ドゥ①"


def test_resolve_title_annotation_restores_suffix_when_llm_omits_it(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"贝尔法斯特","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "ベルファスト⑦",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "贝尔法斯特⑦"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_extracts_inline_inserted_annotation(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"アカネ②（赤音）メガネなしVer.","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "アカネ②メガネなしVer.",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "赤音②メガネなしVer."
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_load_llm_config_prefers_database_settings(monkeypatch) -> None:
    monkeypatch.setenv("FANBOX_LLM_API_KEY", "env-key")
    monkeypatch.setenv("FANBOX_LLM_ENABLED", "false")

    settings = SimpleNamespace(
        llm_enabled=True,
        llm_api_key="db-key",
        llm_base_url="https://example.test/api",
        llm_model="deepseek-chat",
    )

    config = load_llm_config(settings)

    assert config.enabled is True
    assert config.api_key == "db-key"
    assert config.base_url == "https://example.test/api"
    assert config.model == "deepseek-chat"


def test_build_llm_messages_mentions_parenthesized_series_shorthand() -> None:
    messages = _build_llm_messages("鈴谷(艦これ)①")
    full_text = "\n".join(message["content"] for message in messages)

    assert "艦これ" in full_text
    assert "舰队Collection" in full_text


def test_resolve_title_annotation_restores_parenthesized_series_context(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"鈴谷(艦これ)①（舰队Collection）","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "鈴谷(艦これ)①",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "鈴谷(舰队Collection)①"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_accepts_direct_parenthesized_translation(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"鈴谷(舰队Collection)①","confidence":"high"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "鈴谷(艦これ)①",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "鈴谷(舰队Collection)①"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_restores_parenthesized_series_context_from_inner_only_result(
    monkeypatch, tmp_path: Path
) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"舰队Collection","confidence":"high"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "鈴谷(艦これ)②",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "鈴谷(舰队Collection)②"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_collapses_duplicate_leading_affix(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"一之瀬一之瀬明日奈×調月莉央①","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "一之瀬アスナx調月リオ①",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "一之瀬明日奈×調月莉央①"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_does_not_duplicate_equivalent_dog_suffix(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"光辉×🐶","confidence":"high"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "イラストリアスx🐶",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "光辉x🐶"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_collapses_literal_dog_plus_emoji_duplication(monkeypatch, tmp_path: Path) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"Premarton×狗x🐶","confidence":"high"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "プレマートンx🐶",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "Premartonx🐶"
    assert result.status == TitleAnnotationStatus.COMPLETED.value


def test_resolve_title_annotation_does_not_append_original_kanji_suffix_after_chinese_translation(
    monkeypatch, tmp_path: Path
) -> None:
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text('{"version":1,"full_titles":{},"fragments":{}}', encoding="utf-8")
    llm_config = LlmConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=10,
        max_tokens=120,
        enabled=True,
    )

    def fake_post_llm_request(config: LlmConfig, request_body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"annotated_title":"花子栗倒立骑乘位","confidence":"medium"}'
                    }
                }
            ]
        }

    monkeypatch.setattr("app.services.title_annotation._post_llm_request", fake_post_llm_request)
    result = resolve_title_annotation(
        "ハナコチングリ騎乗位",
        aliases=load_title_aliases(alias_path),
        llm_config=llm_config,
    )

    assert result.annotation == "花子栗倒立骑乘位"
    assert result.status == TitleAnnotationStatus.COMPLETED.value
