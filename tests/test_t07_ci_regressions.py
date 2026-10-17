"""CI contracts: chronological DDL and infrastructure-free HTTP collection."""

from io import StringIO
import os
from pathlib import Path
import re
import subprocess
import sys

from alembic import command
from alembic.config import Config


def test_initial_turn_ddl_does_not_reference_future_quota_tables():
    output = StringIO()
    command.upgrade(Config("alembic.ini", output_buffer=output), "head", sql=True)
    sql = output.getvalue()
    initial_turn = re.search(r"CREATE TABLE nlp_turns \((.*?)\n\)", sql, re.DOTALL)
    assert initial_turn is not None
    assert "quota_reservation_id" not in initial_turn.group(1)
    assert "nlp_model_quota_reservations" not in initial_turn.group(1)
    assert sql.index("CREATE TABLE nlp_model_quota_reservations") < sql.index(
        "ALTER TABLE nlp_turns ADD COLUMN quota_reservation_id"
    )


def test_http_smoke_collection_needs_no_external_database_configuration():
    env = {**os.environ, "RUN_API_HTTP": "1"}
    for key in ("NLP_AGENT_DATABASE_URL", "DATABASE_URL", "API_HTTP_DATABASE_URL"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/api_http", "--collect-only",
         "-q", "-m", "api_smoke"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
