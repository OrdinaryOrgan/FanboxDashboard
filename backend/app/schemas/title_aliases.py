from __future__ import annotations

from pydantic import BaseModel


class TitleAliasesPayload(BaseModel):
    version: int = 1
    full_titles: dict[str, str]
    phrase_fragments: dict[str, str]
    fragments: dict[str, str]


class TitleAliasesImportResult(BaseModel):
    imported_path: str
    full_title_count: int
    phrase_fragment_count: int
    fragment_count: int
