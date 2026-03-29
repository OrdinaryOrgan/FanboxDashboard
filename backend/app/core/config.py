from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    data_dir: Path
    database_path: Path


def get_config() -> AppConfig:
    project_root = Path(__file__).resolve().parents[3]
    data_dir = project_root / "backend" / "data"
    database_override = os.getenv("FANBOX_DASHBOARD_DB")
    database_path = Path(database_override) if database_override else data_dir / "fanbox_dashboard.db"
    return AppConfig(
        project_root=project_root,
        data_dir=data_dir,
        database_path=database_path,
    )
