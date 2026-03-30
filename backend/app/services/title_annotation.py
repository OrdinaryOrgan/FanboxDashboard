from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy.orm import Session

from app.core.config import get_config
from app.db.models import Post, Settings, TitleAnnotationSource, TitleAnnotationStatus


KANA_CHAR_PATTERN = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
KANA_SPAN_PATTERN = re.compile(r"[\u3040-\u309F\u30A0-\u30FFー]+")


@dataclass(frozen=True)
class TitleAliasDictionary:
    version: int
    full_titles: dict[str, str]
    phrase_fragments: dict[str, str]
    fragments: dict[str, str]
    normalized_full_titles: dict[str, str]
    normalized_phrase_fragments: dict[str, tuple[str, str]]
    normalized_fragments: dict[str, str]


@dataclass(frozen=True)
class TitleAnnotationResult:
    annotation: str | None
    source: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class LlmConfig:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int
    max_tokens: int
    enabled: bool


class TitleAnnotationError(RuntimeError):
    pass


EMPTY_TITLE_ALIASES_PAYLOAD = {
    "version": 1,
    "full_titles": {},
    "phrase_fragments": {},
    "fragments": {},
}


def _load_title_aliases_payload(path: Path) -> dict[str, Any]:
    ensure_title_aliases_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return dict(EMPTY_TITLE_ALIASES_PAYLOAD)
    normalized_payload: dict[str, Any] = {
        "version": payload.get("version", 1),
        "full_titles": payload.get("full_titles", {}),
        "phrase_fragments": payload.get("phrase_fragments", {}),
        "fragments": payload.get("fragments", {}),
    }
    for key in ("full_titles", "phrase_fragments", "fragments"):
        if not isinstance(normalized_payload[key], dict):
            normalized_payload[key] = {}
    return normalized_payload


def build_display_title(title: str, annotation: str | None) -> str:
    if annotation and not _is_effectively_same_title(title, annotation):
        return f"{title}（{annotation}）"
    return title


def title_needs_annotation(title: str) -> bool:
    return bool(KANA_CHAR_PATTERN.search(title))


def resolve_dictionary_annotation(
    title: str,
    aliases: TitleAliasDictionary | None = None,
) -> TitleAnnotationResult | None:
    if not title_needs_annotation(title):
        return None
    aliases = aliases or load_title_aliases()
    return _resolve_from_dictionary(title, aliases)


def default_title_aliases_path() -> Path:
    return get_config().project_root / "backend" / "app" / "core" / "title_aliases.json"


def resolve_title_aliases_path(settings: Settings | None = None) -> Path:
    configured = (settings.title_aliases_path.strip() if settings and settings.title_aliases_path else "") or ""
    return Path(configured) if configured else default_title_aliases_path()


def ensure_title_aliases_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(EMPTY_TITLE_ALIASES_PAYLOAD, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_title_aliases_file(path: Path) -> Path:
    ensure_title_aliases_file(path)
    path.write_text(json.dumps(EMPTY_TITLE_ALIASES_PAYLOAD, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def upsert_title_alias_entries(path: Path, mode: str, updates: list[tuple[str, str]]) -> tuple[Path, int]:
    payload = _load_title_aliases_payload(path)
    section_map = {
        "full_title": "full_titles",
        "phrase_fragment": "phrase_fragments",
        "fragment": "fragments",
    }
    section_name = section_map[mode]
    section = payload[section_name]
    updated_count = 0
    for source, target in updates:
        source_value = source.strip()
        target_value = target.strip()
        if not source_value or not target_value:
            continue
        if section.get(source_value) == target_value:
            continue
        section[source_value] = target_value
        updated_count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, updated_count


def load_title_aliases(path: Path | None = None, settings: Settings | None = None) -> TitleAliasDictionary:
    alias_path = path or resolve_title_aliases_path(settings)
    if not alias_path.exists():
        return TitleAliasDictionary(
            version=1,
            full_titles={},
            phrase_fragments={},
            fragments={},
            normalized_full_titles={},
            normalized_phrase_fragments={},
            normalized_fragments={},
        )

    payload = json.loads(alias_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return TitleAliasDictionary(
            version=1,
            full_titles={},
            phrase_fragments={},
            fragments={},
            normalized_full_titles={},
            normalized_phrase_fragments={},
            normalized_fragments={},
        )

    full_titles = payload.get("full_titles", {})
    phrase_fragments = payload.get("phrase_fragments", {})
    fragments = payload.get("fragments", {})
    version = payload.get("version", 1)
    version_value = int(version) if isinstance(version, (int, str)) and str(version).isdigit() else 1
    full_titles_map = {str(k): str(v) for k, v in full_titles.items()} if isinstance(full_titles, dict) else {}
    phrase_fragments_map = (
        {str(k): str(v) for k, v in phrase_fragments.items()} if isinstance(phrase_fragments, dict) else {}
    )
    fragments_map = {str(k): str(v) for k, v in fragments.items()} if isinstance(fragments, dict) else {}
    return TitleAliasDictionary(
        version=version_value,
        full_titles=full_titles_map,
        phrase_fragments=phrase_fragments_map,
        fragments=fragments_map,
        normalized_full_titles={_normalized_text(key): value for key, value in full_titles_map.items()},
        normalized_phrase_fragments={
            _normalized_text(key): (key, value) for key, value in phrase_fragments_map.items()
        },
        normalized_fragments={_normalized_text(key): value for key, value in fragments_map.items()},
    )


def load_llm_config(settings: Settings | None = None) -> LlmConfig:
    api_key = (settings.llm_api_key.strip() if settings and settings.llm_api_key else "") or os.getenv(
        "FANBOX_LLM_API_KEY", ""
    ).strip() or None
    base_url = (
        settings.llm_base_url.strip()
        if settings and settings.llm_base_url
        else os.getenv("FANBOX_LLM_BASE_URL", "https://api.deepseek.com").strip()
    ).rstrip("/") or "https://api.deepseek.com"
    model = (
        settings.llm_model.strip()
        if settings and settings.llm_model
        else os.getenv("FANBOX_LLM_MODEL", "deepseek-chat").strip()
    ) or "deepseek-chat"
    timeout_raw = os.getenv("FANBOX_LLM_TIMEOUT_SECONDS", "20").strip()
    max_tokens_raw = os.getenv("FANBOX_LLM_MAX_TOKENS", "120").strip()
    enabled_raw = os.getenv("FANBOX_LLM_ENABLED", "true").strip().lower()
    try:
        timeout_seconds = max(5, int(timeout_raw))
    except ValueError:
        timeout_seconds = 20
    try:
        max_tokens = max(32, int(max_tokens_raw))
    except ValueError:
        max_tokens = 120
    enabled = settings.llm_enabled if settings is not None else enabled_raw not in {"0", "false", "off", "no"}
    return LlmConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        enabled=enabled,
    )


def resolve_title_annotation(
    title: str,
    aliases: TitleAliasDictionary | None = None,
    llm_config: LlmConfig | None = None,
) -> TitleAnnotationResult:
    if not title_needs_annotation(title):
        return TitleAnnotationResult(
            annotation=None,
            source=TitleAnnotationSource.NONE.value,
            status=TitleAnnotationStatus.SKIPPED.value,
        )

    aliases = aliases or load_title_aliases()
    dictionary_result = _resolve_from_dictionary(title, aliases)
    if dictionary_result is not None:
        return dictionary_result

    llm_config = llm_config or load_llm_config()
    if not llm_config.enabled or not llm_config.api_key:
        return TitleAnnotationResult(
            annotation=None,
            source=TitleAnnotationSource.NONE.value,
            status=TitleAnnotationStatus.FAILED.value,
            error="LLM title annotation is not configured.",
        )

    try:
        annotated_title = _annotate_title_with_llm(title, llm_config)
    except TitleAnnotationError as exc:
        return TitleAnnotationResult(
            annotation=None,
            source=TitleAnnotationSource.NONE.value,
            status=TitleAnnotationStatus.FAILED.value,
            error=str(exc),
        )

    if not _is_valid_annotation(title, annotated_title):
        return TitleAnnotationResult(
            annotation=None,
            source=TitleAnnotationSource.NONE.value,
            status=TitleAnnotationStatus.FAILED.value,
            error="LLM returned an invalid title annotation.",
        )

    return TitleAnnotationResult(
        annotation=annotated_title,
        source=TitleAnnotationSource.LLM.value,
        status=TitleAnnotationStatus.COMPLETED.value,
    )


def prepare_post_title_annotation(post: Post, force_reset: bool = False) -> bool:
    if not title_needs_annotation(post.title):
        changed = (
            post.title_annotation is not None
            or post.title_annotation_source != TitleAnnotationSource.NONE.value
            or post.title_annotation_status != TitleAnnotationStatus.SKIPPED.value
            or post.title_annotation_error is not None
        )
        post.title_annotation = None
        post.title_annotation_source = TitleAnnotationSource.NONE.value
        post.title_annotation_status = TitleAnnotationStatus.SKIPPED.value
        post.title_annotation_error = None
        post.title_annotation_updated_at = datetime.now(timezone.utc)
        return changed

    if (
        not force_reset
        and post.title_annotation
        and post.title_annotation_status == TitleAnnotationStatus.COMPLETED.value
        and not _is_effectively_same_title(post.title, post.title_annotation)
    ):
        return False

    post.title_annotation = None
    post.title_annotation_source = None
    post.title_annotation_status = TitleAnnotationStatus.PENDING.value
    post.title_annotation_error = None
    post.title_annotation_updated_at = None
    return True


def find_completed_annotation_for_same_title(session: Session, post: Post) -> TitleAnnotationResult | None:
    existing = (
        session.query(Post)
        .filter(
            Post.id != post.id,
            Post.title == post.title,
            Post.title_annotation_status == TitleAnnotationStatus.COMPLETED.value,
            Post.title_annotation.is_not(None),
        )
        .order_by(Post.title_annotation_updated_at.desc().nullslast(), Post.updated_at.desc())
        .first()
    )
    if existing is not None and existing.title_annotation and not _is_effectively_same_title(existing.title, existing.title_annotation):
        return TitleAnnotationResult(
            annotation=existing.title_annotation,
            source=TitleAnnotationSource.LLM_CACHED.value,
            status=TitleAnnotationStatus.COMPLETED.value,
        )

    current_key = _annotation_lookup_key(post.title)
    if not current_key:
        return None

    candidates = (
        session.query(Post)
        .filter(
            Post.id != post.id,
            Post.title_annotation_status == TitleAnnotationStatus.COMPLETED.value,
            Post.title_annotation.is_not(None),
        )
        .order_by(Post.title_annotation_updated_at.desc().nullslast(), Post.updated_at.desc())
        .all()
    )
    for candidate in candidates:
        if not candidate.title_annotation or _is_effectively_same_title(candidate.title, candidate.title_annotation):
            continue
        if _annotation_lookup_key(candidate.title) != current_key:
            continue
        core_annotation = _annotation_core_for_title(candidate.title, candidate.title_annotation)
        if not core_annotation:
            continue
        adjusted_annotation = _restore_non_kana_affixes(post.title, core_annotation)
        if _is_effectively_same_title(post.title, adjusted_annotation):
            continue
        return TitleAnnotationResult(
            annotation=adjusted_annotation,
            source=TitleAnnotationSource.LLM_CACHED.value,
            status=TitleAnnotationStatus.COMPLETED.value,
        )
    return None


def _resolve_from_dictionary(title: str, aliases: TitleAliasDictionary) -> TitleAnnotationResult | None:
    exact_match = aliases.full_titles.get(title)
    if exact_match:
        return TitleAnnotationResult(
            annotation=exact_match,
            source=TitleAnnotationSource.DICTIONARY_FULL.value,
            status=TitleAnnotationStatus.COMPLETED.value,
        )
    normalized_exact_match = aliases.normalized_full_titles.get(_normalized_text(title))
    if normalized_exact_match:
        return TitleAnnotationResult(
            annotation=normalized_exact_match,
            source=TitleAnnotationSource.DICTIONARY_FULL.value,
            status=TitleAnnotationStatus.COMPLETED.value,
        )

    return _resolve_from_dictionary_fragments(title, aliases)


def _resolve_from_dictionary_fragments(title: str, aliases: TitleAliasDictionary) -> TitleAnnotationResult | None:
    if not title_needs_annotation(title):
        return None

    segments: list[str] = []
    index = 0
    changed = False
    used_phrase = False

    while index < len(title):
        phrase_match = _find_longest_phrase_match(title, index, aliases.normalized_phrase_fragments)
        if phrase_match is not None:
            phrase, replacement = phrase_match
            segments.append(replacement)
            if replacement != phrase:
                changed = True
            used_phrase = True
            index += len(phrase)
            continue

        kana_match = KANA_SPAN_PATTERN.match(title, index)
        if kana_match:
            kana_span = kana_match.group(0)
            replacement = aliases.fragments.get(kana_span)
            if replacement is None:
                replacement = aliases.normalized_fragments.get(_normalized_text(kana_span))
            if replacement is None:
                return None
            segments.append(replacement)
            if replacement != kana_span:
                changed = True
            index = kana_match.end()
            continue

        segments.append(title[index])
        index += 1

    if not changed:
        return None
    return TitleAnnotationResult(
        annotation="".join(segments),
        source=(
            TitleAnnotationSource.DICTIONARY_PHRASE.value
            if used_phrase
            else TitleAnnotationSource.DICTIONARY_FRAGMENT.value
        ),
        status=TitleAnnotationStatus.COMPLETED.value,
    )


def _find_longest_phrase_match(title: str, start: int, phrase_fragments: dict[str, str]) -> tuple[str, str] | None:
    best: tuple[str, str] | None = None
    suffix = title[start:]
    normalized_suffix = _normalized_text(suffix)
    for normalized_phrase, payload in phrase_fragments.items():
        original_phrase, replacement = payload
        if not normalized_phrase:
            continue
        if not normalized_suffix.startswith(normalized_phrase):
            continue
        if best is None or len(normalized_phrase) > len(_normalized_text(best[0])):
            best = (original_phrase, replacement)
    return best


def _annotate_title_with_llm(title: str, config: LlmConfig) -> str:
    primary_result = _request_annotation_once(title, config, force_annotation=False)
    if _is_valid_annotation(title, primary_result):
        return primary_result

    fallback_result = _request_annotation_once(title, config, force_annotation=True)
    if not fallback_result.strip():
        raise TitleAnnotationError("LLM JSON response did not contain a valid annotated_title.")
    return fallback_result.strip()


def _request_annotation_once(title: str, config: LlmConfig, force_annotation: bool) -> str:
    request_body = {
        "model": config.model,
        "messages": _build_llm_messages(title, force_annotation=force_annotation),
        "stream": False,
        "temperature": 0.2,
        "max_tokens": config.max_tokens,
        "response_format": {"type": "json_object"},
    }
    response_payload = _post_llm_request(config, request_body)
    try:
        content = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TitleAnnotationError("LLM response payload was missing the message content.") from exc
    if not isinstance(content, str) or not content.strip():
        raise TitleAnnotationError("LLM returned empty content while generating title annotation.")
    parsed = _parse_json_content(content)
    annotated_title = parsed.get("annotated_title")
    if not isinstance(annotated_title, str) or not annotated_title.strip():
        raise TitleAnnotationError("LLM JSON response did not contain a valid annotated_title.")
    return _normalize_annotation_output(title, annotated_title.strip())


def _post_llm_request(config: LlmConfig, request_body: dict[str, Any]) -> dict[str, Any]:
    endpoint = f"{config.base_url}/chat/completions"
    request = urllib_request.Request(
        endpoint,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=config.timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise TitleAnnotationError(f"LLM request failed with HTTP {exc.code}: {error_body}") from exc
    except urllib_error.URLError as exc:
        raise TitleAnnotationError(f"LLM request failed: {exc.reason}") from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TitleAnnotationError("LLM response was not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise TitleAnnotationError("LLM response was not a JSON object.")
    return parsed


def _build_llm_messages(title: str, force_annotation: bool = False) -> list[dict[str, str]]:
    system_prompt = (
        "你是一个标题补注助手。任务不是全文翻译，而是把日文标题中包含的平假名和片假名部分，"
        "转换成简洁、自然、适合中文用户快速阅读的补注名称。"
        "这些标题大多来自游戏、动漫、二次元相关角色或专有名词；当假名部分明显是角色名、舰船名、作品常用专有名词时，"
        "优先使用中文语境里更常见、更自然的称呼。"
        "如果该名称在中文圈里通常直接使用英文或拉丁字母名称，而不是中文翻译，也可以直接保留常见英文名称。"
        "不要把角色昵称、专有名词、名字片段误翻成普通词义。"
        "标题里的 x、×、ｘ 常常只是多个角色名之间的连接符，遇到这种情况时请把连接符两侧的角色分别补注，并保留连接符本身。"
        "如果标题里出现括号中的作品简称、圈内缩写或系列名，例如 艦これ、アズレン、ブルアカ、NIKKE，这类内容通常是作品/系列上下文，"
        "应翻成中文圈常见的作品名，而不是并入角色名本身。"
        "例如：鈴谷(艦これ)① 的补注应接近 鈴谷(舰队Collection)①，而不是 鈴谷(舰铃谷)①。"
        "如果标题里同时包含角色名和描述性词语，例如 ズリ、セックス、チングリ、騎乗位、キャラ差分、メガネなし、正面差分、request，"
        "请为整个标题生成简洁自然的完整补注，不要只翻角色名后就把后面的描述省略掉。"
        "不要把原本已经存在的汉字角色名、标题主体或汉字短语改写成更长的作品说明，也不要把同一个汉字前缀重复两次。"
        "如果原标题里已经存在汉字角色名，例如 海夢、一之瀬、鈴谷，请保留这些汉字本身，不要把它们改写、扩写成完整人物介绍，也不要在连接符两侧再次重复一次。"
        "如果原标题中已经包含 emoji，例如 🐶，请保留 emoji 本身，不要再另外把它翻译成“狗”后和 emoji 同时重复出现。"
        "必须尽量保留原标题结构，不要改动原有汉字、数字、圈号、符号、遮罩字符。只转换需要补注的假名部分。"
        "annotated_title 必须是一个可以直接放进原标题后面括号里的独立补注字符串，不要返回原标题本身，不要返回“原标题（补注）”这种完整显示文本，也不要把补注插回原标题内部。"
        "如果结果仍然主要是日文平假名或片假名，或者只是把原始日文做了 Unicode 规范化，这种结果也视为无效。"
        "只输出 JSON。"
    )
    if force_annotation:
        user_prompt = (
            "请为下面的标题生成适合展示在原标题括号中的补注。\n"
            "这是第二次尝试：标题中明确包含日文假名，因此不要原样返回原标题。\n"
            "要求：\n"
            "1. 优先输出中文语境下更常见的角色名或专有名词称呼。\n"
            "2. 如果常见称呼本身就是英文或拉丁字母名称，可以直接使用该英文名称。\n"
            "3. 如果没有较稳定的常见称呼，再使用简洁自然的音译。\n"
            "4. 严禁把角色名或昵称翻译成普通词义。\n"
            "5. 如果标题里有 x、×、ｘ，把它当作角色连接符，分别补注两侧名称，并保留连接符。\n"
            "6. 如果标题里有括号中的作品简称、系列缩写或圈内简称，例如 艦これ，请把它翻成常见作品名，保留括号位置，不要并入角色名本身。\n"
            "7. 如果标题里包含 ズリ、セックス、チングリ、騎乗位、キャラ差分、request 等描述词，请把这些描述也用简洁自然的中文补出来，不要只输出角色名。\n"
            "8. 如果标题里有 Ver.、差分、正面、メガネなし 之类的变体后缀，返回的是整个标题对应的独立补注字符串，不要把补注插回原标题内部。\n"
            "9. 如果原标题里已经有 🐶 这种 emoji，请保留一次，不要同时输出“狗”和 emoji 造成重复。\n"
            "10. 如果原标题里已经有汉字角色名，例如 海夢，请保留原来的汉字，不要改写成 喜多川海夢 之类的完整名字，也不要在连接符后再次重复一次。\n"
            "11. 不要把已有汉字主体重复两次，也不要把汉字主体扩写成作品说明。\n"
            "12. 只改动假名部分，保留汉字、数字、圈号、符号原样。\n"
            "13. 对于 海夢xアスナ(キャラ差分) 这类标题，结果应接近 海夢x亚丝娜(角色差分)。\n"
            "14. 对于 海夢ズリ(キャラ差分) 这类标题，结果应接近 海夢自慰(角色差分) 或 海夢磨蹭(角色差分)，不要把 海夢 改写成完整作品说明。\n"
            "15. 不要返回纯日文片假名/平假名，也不要只做日文规范化。\n"
            "16. 不要解释，不要输出多余文本。\n"
            "17. 输出 JSON，格式为：{\"annotated_title\":\"...\",\"confidence\":\"high|medium|low\"}\n"
            f"标题：{title}"
        )
    else:
        user_prompt = (
            "请为下面的标题生成适合展示在原标题括号中的中文补注。\n"
            "要求：\n"
            "1. 优先输出中文语境下更常见的角色名或专有名词称呼。\n"
            "2. 如果常见称呼本身就是英文或拉丁字母名称，可以直接使用该英文名称。\n"
            "3. 如果没有较稳定的常见称呼，再使用简洁自然的音译，不要做普通词义翻译。\n"
            "4. 如果标题里有 x、×、ｘ，把它当作角色连接符，分别补注两侧名称，并保留连接符。\n"
            "5. 如果标题里有括号中的作品简称、系列缩写或圈内简称，例如 艦これ，请把它翻成常见作品名，保留括号位置，不要并入角色名本身。\n"
            "6. 如果标题里包含 ズリ、セックス、チングリ、騎乗位、キャラ差分、request 等描述词，请把这些描述也用简洁自然的中文补出来，不要只输出角色名。\n"
            "7. 如果原标题里已经有 🐶 这种 emoji，请保留一次，不要同时输出“狗”和 emoji 造成重复。\n"
            "8. 如果原标题里已经有汉字角色名，例如 海夢，请保留原来的汉字，不要改写成 喜多川海夢 之类的完整名字，也不要在连接符后再次重复一次。\n"
            "9. 不要把已有汉字主体重复两次，也不要把汉字主体扩写成作品说明。\n"
            "10. 返回的 annotated_title 必须是独立补注字符串，不能包含原标题，也不能是“原标题（补注）”或“原标题片段（补注）后缀”这种格式。\n"
            "11. 只改动假名部分，保留汉字、数字、圈号、符号原样。\n"
            "12. 对于 海夢xアスナ(キャラ差分) 这类标题，结果应接近 海夢x亚丝娜(角色差分)。\n"
            "13. 对于 海夢ズリ(キャラ差分) 这类标题，结果应接近 海夢自慰(角色差分) 或 海夢磨蹭(角色差分)。\n"
            "14. 不要返回纯日文片假名/平假名，也不要只做日文规范化。\n"
            "15. 不要解释，不要输出多余文本。\n"
            "16. 输出 JSON，格式为：{\"annotated_title\":\"...\",\"confidence\":\"high|medium|low\"}\n"
            f"标题：{title}"
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _parse_json_content(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()
            if stripped.lower().startswith("json"):
                stripped = stripped[4:].strip()
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise TitleAnnotationError("LLM content was JSON but not an object.")
    return parsed


def _is_valid_annotation(original_title: str, annotated_title: str) -> bool:
    if not annotated_title or _is_effectively_same_title(original_title, annotated_title):
        return False
    if len(annotated_title) > max(80, len(original_title) * 4):
        return False
    return _count_kana(annotated_title) < _count_kana(original_title)


def _normalize_annotation_output(original_title: str, annotated_title: str) -> str:
    normalized = annotated_title.strip()
    if not normalized:
        return normalized

    direct_parenthetical = _accept_direct_parenthesized_translation(original_title, normalized)
    if direct_parenthetical is not None:
        return direct_parenthetical

    wrapped_inner = _extract_wrapped_annotation(original_title, normalized)
    if wrapped_inner is not None:
        restored_parenthetical = _restore_parenthesized_kana_context(original_title, wrapped_inner)
        if restored_parenthetical is not None:
            return restored_parenthetical
        return _restore_non_kana_affixes(original_title, wrapped_inner)

    inline_inserted = _extract_inline_inserted_annotation(original_title, normalized)
    if inline_inserted is not None:
        return _collapse_duplicate_leading_affix(original_title, inline_inserted)

    full_width_prefix = f"{original_title}（"
    ascii_prefix = f"{original_title}("
    if normalized.startswith(full_width_prefix) and normalized.endswith("）"):
        inner = normalized[len(full_width_prefix) : -1].strip()
        restored_parenthetical = _restore_parenthesized_kana_context(original_title, inner)
        if restored_parenthetical is not None:
            return restored_parenthetical
        return _restore_non_kana_affixes(original_title, inner or normalized)
    if normalized.startswith(ascii_prefix) and normalized.endswith(")"):
        inner = normalized[len(ascii_prefix) : -1].strip()
        restored_parenthetical = _restore_parenthesized_kana_context(original_title, inner)
        if restored_parenthetical is not None:
            return restored_parenthetical
        return _restore_non_kana_affixes(original_title, inner or normalized)
    restored_parenthetical = _restore_parenthesized_kana_context(original_title, normalized)
    if restored_parenthetical is not None:
        return restored_parenthetical
    return _restore_non_kana_affixes(original_title, normalized)


def _accept_direct_parenthesized_translation(original_title: str, annotated_title: str) -> str | None:
    pattern = r"^(?P<prefix>.*\()(?P<inner>[^()]*)\)(?P<suffix>.*)$"
    original_match = re.fullmatch(pattern, original_title)
    annotated_match = re.fullmatch(pattern, annotated_title)
    if not original_match or not annotated_match:
        return None
    if _normalized_text(original_match.group("prefix")) != _normalized_text(annotated_match.group("prefix")):
        return None
    if _normalized_text(original_match.group("suffix")) != _normalized_text(annotated_match.group("suffix")):
        return None
    return annotated_title


def _extract_wrapped_annotation(original_title: str, annotated_title: str) -> str | None:
    for open_mark, close_mark in (("（", "）"), ("(", ")")):
        open_index = annotated_title.rfind(open_mark)
        close_index = annotated_title.rfind(close_mark)
        if open_index <= 0 or close_index <= open_index:
            continue
        outer = annotated_title[:open_index].strip()
        inner = annotated_title[open_index + 1 : close_index].strip()
        if inner and _is_effectively_same_title(original_title, outer):
            return inner
    return None


def _extract_inline_inserted_annotation(original_title: str, annotated_title: str) -> str | None:
    match = re.fullmatch(r"(?P<prefix>.+?)[（(](?P<inner>[^()（）]+)[）)](?P<suffix>.*)", annotated_title)
    if not match:
        return None

    prefix = match.group("prefix").strip()
    inner = match.group("inner").strip()
    suffix = match.group("suffix")
    if not inner:
        return None
    if _normalized_text(f"{prefix}{suffix}") != _normalized_text(original_title):
        return None
    rebuilt_prefix = _restore_non_kana_affixes(prefix, inner)
    return f"{rebuilt_prefix}{suffix}".strip()


def _restore_non_kana_affixes(original_title: str, annotated_title: str) -> str:
    restored = annotated_title
    leading_affix = _leading_non_kana_affix(original_title)
    trailing_affix = _trailing_non_kana_affix(original_title)

    if leading_affix and not _starts_with_equivalent_affix(restored, leading_affix):
        restored = f"{leading_affix}{restored}"
    if trailing_affix and _should_restore_trailing_affix(trailing_affix) and not _ends_with_equivalent_affix(restored, trailing_affix):
        restored = f"{restored}{trailing_affix}"
    return _collapse_duplicate_dog_suffix(original_title, _collapse_duplicate_leading_affix(original_title, restored))


def _restore_parenthesized_kana_context(original_title: str, annotated_title: str) -> str | None:
    match = re.fullmatch(r"(?P<prefix>.*\()(?P<inner>[^()]*(?:[\u3040-\u309F\u30A0-\u30FF])[^()]*)\)(?P<suffix>.*)", original_title)
    if not match:
        return None
    if any(mark in annotated_title for mark in ("(", ")", "（", "）")):
        return None
    rebuilt = f"{match.group('prefix')}{annotated_title}){match.group('suffix')}"
    return _collapse_duplicate_dog_suffix(original_title, _collapse_duplicate_leading_affix(original_title, rebuilt))


def _collapse_duplicate_leading_affix(original_title: str, annotated_title: str) -> str:
    leading_affix = _leading_non_kana_affix(original_title)
    if not leading_affix:
        return annotated_title
    doubled = f"{leading_affix}{leading_affix}"
    if annotated_title.startswith(doubled):
        return f"{leading_affix}{annotated_title[len(doubled):]}"
    return annotated_title


def _starts_with_equivalent_affix(value: str, affix: str) -> bool:
    if value.startswith(affix):
        return True
    normalized_value = _normalize_affix_equivalence(value[: len(affix) + 2])
    normalized_affix = _normalize_affix_equivalence(affix)
    return normalized_value.startswith(normalized_affix)


def _ends_with_equivalent_affix(value: str, affix: str) -> bool:
    if value.endswith(affix):
        return True
    normalized_value = _normalize_affix_equivalence(value[-(len(affix) + 2) :])
    normalized_affix = _normalize_affix_equivalence(affix)
    return normalized_value.endswith(normalized_affix)


def _normalize_affix_equivalence(value: str) -> str:
    normalized = _normalized_text(value)
    return normalized.replace("×", "x").replace("ｘ", "x")


def _should_restore_trailing_affix(affix: str) -> bool:
    if not affix:
        return False
    return not re.search(r"[\u4e00-\u9fff]", affix)


def _collapse_duplicate_dog_suffix(original_title: str, annotated_title: str) -> str:
    original_match = re.search(r"([x×ｘ])🐶+$", original_title)
    if not original_match:
        return annotated_title

    original_suffix = original_match.group(0)
    normalized = _normalized_text(annotated_title)
    normalized = re.sub(r"(?:狗|犬)+([x×ｘ])🐶+$", r"\1🐶", normalized)
    normalized = re.sub(r"([x×ｘ])🐶+(?:[x×ｘ])🐶+$", r"\1🐶", normalized)
    normalized = re.sub(r"([x×ｘ])([x×ｘ])🐶+$", r"\2🐶", normalized)

    if re.search(r"[x×ｘ]🐶+$", normalized):
        normalized = re.sub(r"[x×ｘ]🐶+$", original_suffix, normalized)
    return normalized


def _leading_non_kana_affix(value: str) -> str:
    index = 0
    while index < len(value) and not KANA_CHAR_PATTERN.match(value[index]):
        index += 1
    return value[:index]


def _trailing_non_kana_affix(value: str) -> str:
    index = len(value)
    while index > 0 and not KANA_CHAR_PATTERN.match(value[index - 1]):
        index -= 1
    return value[index:]


def _count_kana(value: str) -> int:
    return len(KANA_CHAR_PATTERN.findall(value))


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _is_effectively_same_title(original_title: str, annotated_title: str) -> bool:
    return _normalized_text(annotated_title) == _normalized_text(original_title)


def _annotation_lookup_key(title: str) -> str:
    return _normalized_text(_annotation_core_for_title(title, title))


def _annotation_core_for_title(original_title: str, annotated_title: str) -> str:
    leading_affix = _leading_non_kana_affix(original_title)
    trailing_affix = _trailing_non_kana_affix(original_title)
    core = annotated_title
    if leading_affix and core.startswith(leading_affix):
        core = core[len(leading_affix) :]
    if trailing_affix and core.endswith(trailing_affix):
        core = core[: -len(trailing_affix)]
    return core.strip()
