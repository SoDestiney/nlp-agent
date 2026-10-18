"""Real MySQL replay after T07 trigger DDL fails, using isolated shadow tables."""

import importlib
import os
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


@pytest.mark.skipif(not os.environ.get("NLP_AGENT_DATABASE_URL"), reason="requires CI MySQL")
def test_capacity_revision_recovers_after_trigger_permission_failure(monkeypatch):
    migration = importlib.import_module("migrations.versions.20261016_51_t07_turn_capacity")
    prefix = "t07_retry_" + uuid.uuid4().hex[:8]
    names = {"nlp_turns": prefix + "_turns", "nlp_runtime_capacity": prefix + "_capacity", "nlp_turn_user_capacity": prefix + "_users"}

    def rewrite(sql):
        for old, new in names.items():
            sql = sql.replace(old, new)
        return sql.replace("trg_" + names["nlp_turns"] + "_capacity_", prefix + "_trg_")

    engine = sa.create_engine(os.environ["NLP_AGENT_DATABASE_URL"].replace("mysql+aiomysql://", "mysql+pymysql://"))
    monkeypatch.setattr(migration, "context", SimpleNamespace(is_offline_mode=lambda: False))

    def apply(connection, fail=False):
        operations = Operations(MigrationContext.configure(connection))

        class Bind:
            def execute(self, sql, parameters):
                return connection.execute(sql, {k: rewrite(v) for k, v in parameters.items()})

        class Op:
            def get_bind(self):
                return Bind()

            def create_table(self, name, *columns, **kwargs):
                return operations.create_table(names[name], *columns, **kwargs)

            def execute(self, sql):
                sql = rewrite(str(sql))
                if fail and "CREATE TRIGGER" in sql:
                    # MySQL commits prior DDL/DML before rejecting CREATE TRIGGER.
                    connection.commit()
                    raise RuntimeError("injected MySQL 1419")
                return connection.execute(sa.text(sql))

        monkeypatch.setattr(migration, "op", Op())
        migration.upgrade()

    try:
        with engine.begin() as c:
            c.execute(sa.text(f"CREATE TABLE {names['nlp_turns']} (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), status VARCHAR(16))"))
            c.execute(sa.text(f"INSERT INTO {names['nlp_turns']} VALUES ('a','u','accepted'),('b','u','running')"))
        with engine.connect() as c, pytest.raises(RuntimeError, match="1419"):
            apply(c, fail=True)
        with engine.begin() as c:
            c.execute(sa.text(f"UPDATE {names['nlp_runtime_capacity']} SET queue_capacity=17,per_user_active_limit=9"))
        for _ in range(2):
            with engine.begin() as c:
                apply(c)
        with engine.connect() as c:
            assert tuple(c.execute(sa.text(f"SELECT queued_count,running_count,queue_capacity,per_user_active_limit FROM {names['nlp_runtime_capacity']}")).one()) == (1, 1, 17, 9)
            assert c.execute(sa.text(f"SELECT active_count FROM {names['nlp_turn_user_capacity']} WHERE user_id='u'")).scalar_one() == 2
            assert c.execute(sa.text("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME LIKE :prefix"), {"prefix": prefix + "_trg_%"}).scalar_one() == 3
    finally:
        with engine.begin() as c:
            for suffix in ("ai", "au", "ad"):
                c.execute(sa.text(f"DROP TRIGGER IF EXISTS {prefix}_trg_{suffix}"))
            for name in reversed(list(names.values())):
                c.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
        engine.dispose()
