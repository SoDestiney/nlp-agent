"""MySQL regressions for admission/reconcile races and delayed usage facts."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session

from core.model_runtime.usage import CanonicalTokenUsage, InvocationOutcome, ModelIdentity, ModelInvocation, UsageAttributionContext
from gateway.contracts import AdmissionLimitError, TurnStatus
from gateway.mysql_repository import MySQLGatewayRepository
from server.model_usage.quota import DurableQuotaUsageReporter
from ..support.resources import create_session

pytestmark = pytest.mark.api_async_core


def test_reconcile_cannot_erase_admission_committed_after_its_snapshot(authenticated_client, student_user, mysql_probe, api_http_environment):
    repo = MySQLGatewayRepository(api_http_environment.env["NLP_AGENT_DATABASE_URL"])
    conversation = create_session(authenticated_client, workspace_id=student_user.workspace_id)["session_id"]
    turn, reservation = str(uuid.uuid4()), str(uuid.uuid4())
    snapshot, resume = Event(), Event()

    def pause_candidates(_conn, _cursor, statement, _params, _context, _many):
        if "SELECT id,turn_id FROM nlp_model_quota_reservations" in statement:
            snapshot.set()
            assert resume.wait(10), "admission failed to finish while reconcile was paused"

    event.listen(repo._engine, "after_cursor_execute", pause_candidates)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(repo.reconcile_model_quota)
            try:
                assert snapshot.wait(10)
                repo.create_turn(turn_id=turn, session_id=conversation,
                    user_id=student_user.user_id, workspace_id=student_user.workspace_id,
                    input_text="concurrent quota reservation", idempotency_key=None,
                    quota_reservation_id=reservation, reserved_tokens=120)
            finally:
                resume.set()
            future.result(timeout=15)
        assert mysql_probe.scalar("SELECT reserved_tokens FROM nlp_model_quota_accounts WHERE user_id=:user", user=student_user.user_id) == mysql_probe.scalar("SELECT COALESCE(SUM(reserved_tokens),0) FROM nlp_model_quota_reservations WHERE user_id=:user AND status='reserved'", user=student_user.user_id)
        assert mysql_probe.scalar("SELECT reserved_tokens FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == 120
    finally:
        event.remove(repo._engine, "after_cursor_execute", pause_candidates)
        if repo.get_turn(turn) is not None:
            repo.update_turn(turn, TurnStatus.CANCELLED)
        mysql_probe.execute("DELETE FROM nlp_turn_events WHERE turn_id=:id", id=turn)
        mysql_probe.execute("DELETE FROM nlp_turns WHERE id=:id", id=turn)
        mysql_probe.execute("DELETE FROM nlp_model_quota_reservations WHERE id=:id", id=reservation)
        repo.close()


@pytest.mark.parametrize("report_failure", [False, True])
def test_terminal_turn_keeps_pending_budget_and_settles_late_usage_once(report_failure, authenticated_client, student_user, mysql_probe, api_http_environment, tmp_path):
    url = api_http_environment.env["NLP_AGENT_DATABASE_URL"]
    repo = MySQLGatewayRepository(url)
    conversation = create_session(authenticated_client, workspace_id=student_user.workspace_id)["session_id"]
    turn, reservation, operation = (str(uuid.uuid4()) for _ in range(3))
    repo.create_turn(turn_id=turn, session_id=conversation,
        user_id=student_user.user_id, workspace_id=student_user.workspace_id,
        input_text="late usage", idempotency_key=None,
        quota_reservation_id=reservation, reserved_tokens=1100)
    original_limit = mysql_probe.scalar("SELECT token_limit FROM nlp_model_quota_accounts WHERE user_id=:user", user=student_user.user_id)
    mysql_probe.execute("UPDATE nlp_model_quota_accounts SET token_limit=GREATEST(used_tokens,consumed_tokens)+reserved_tokens WHERE user_id=:user", user=student_user.user_id)

    async def scenario():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        reporter = DurableQuotaUsageReporter(factory, journal_directory=tmp_path)
        fail_commit = [False]

        def reject_reporting_commit(session):
            if session.bind is engine.sync_engine and fail_commit[0]:
                fail_commit[0] = False
                raise RuntimeError("injected reporting commit failure")

        event.listen(Session, "before_commit", reject_reporting_commit)
        call = ModelInvocation(operation_id=operation, started_at=datetime.now(timezone.utc), attempt=1, fallback_index=0,
            identity=ModelIdentity(provider="fake", provider_model="fake", preset="fake", context_window_tokens=1000, max_output_tokens=100),
            attribution=UsageAttributionContext(request_id=turn, user_id=student_user.user_id, workspace_id=student_user.workspace_id, turn_id=turn, reservation_id=reservation, purpose="coordinator"))
        usage = CanonicalTokenUsage(source="provider", input_tokens=100, total_tokens=100)
        result = InvocationOutcome(status="succeeded", completed_at=datetime.now(timezone.utc))
        try:
            await reporter.prepare(call)
            with pytest.raises(AdmissionLimitError):
                await reporter.prepare(call.model_copy(update={"operation_id": str(uuid.uuid4())}))
            if not report_failure:
                await asyncio.to_thread(repo.update_turn, turn, TurnStatus.CANCELLED)
            assert mysql_probe.scalar("SELECT status FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == "reserved"
            assert mysql_probe.scalar("SELECT pending_invocations FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == 1
            if report_failure:
                fail_commit[0] = True
                with pytest.raises(RuntimeError, match="reporting commit failure"):
                    await reporter.report(call, usage, result)
                assert mysql_probe.scalar("SELECT COUNT(*) FROM nlp_model_usage_events WHERE operation_id=:id", id=operation) == 0
                await asyncio.to_thread(repo.update_turn, turn, TurnStatus.FAILED)
                await asyncio.to_thread(repo.reconcile_model_quota)
                assert mysql_probe.scalar("SELECT status FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == "reserved"
                reporter = DurableQuotaUsageReporter(factory, journal_directory=tmp_path)
                assert await reporter.replay_pending() == {"replayed": 1, "failed": 0}
            else:
                await reporter.report(call, usage, result)
            await reporter.report(call, usage, result)
            assert mysql_probe.scalar("SELECT used_tokens FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == 100
            assert mysql_probe.scalar("SELECT settled_tokens FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == 100
            assert mysql_probe.scalar("SELECT status FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == "settled"
            assert mysql_probe.scalar("SELECT COUNT(*) FROM nlp_model_usage_events WHERE operation_id=:id", id=operation) == 1
            assert mysql_probe.scalar("SELECT pending_invocations FROM nlp_model_quota_reservations WHERE id=:id", id=reservation) == 0
        finally:
            event.remove(Session, "before_commit", reject_reporting_commit)
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        mysql_probe.execute("DELETE FROM nlp_model_usage_intents WHERE operation_id=:id", id=operation)
        mysql_probe.execute("DELETE FROM nlp_model_usage_events WHERE operation_id=:id", id=operation)
        mysql_probe.execute("UPDATE nlp_model_quota_reservations SET pending_invocations=0 WHERE id=:id", id=reservation)
        current = repo.get_turn(turn)
        if current is not None and current.status in {TurnStatus.ACCEPTED, TurnStatus.RUNNING}:
            repo.update_turn(turn, TurnStatus.CANCELLED)
        repo.reconcile_model_quota()
        mysql_probe.execute("DELETE FROM nlp_turn_events WHERE turn_id=:id", id=turn)
        mysql_probe.execute("DELETE FROM nlp_turns WHERE id=:id", id=turn)
        mysql_probe.execute("DELETE FROM nlp_model_quota_reservations WHERE id=:id", id=reservation)
        mysql_probe.execute("UPDATE nlp_model_quota_accounts SET token_limit=:limit WHERE user_id=:user", limit=original_limit, user=student_user.user_id)
        repo.close()
