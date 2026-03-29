import os
from pathlib import Path


TEST_DB = Path(__file__).resolve().parents[1] / "data" / "test.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["FANBOX_DASHBOARD_DB"] = str(TEST_DB)
