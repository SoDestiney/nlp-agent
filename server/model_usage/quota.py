"""Transactional token reservations and idempotent Provider usage events."""

from __future__ import annotations

import hashlib
import json
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession

from core.model_runtime.reporters import UsageEventConflictError
from core.model_runtime.usage import (
    CanonicalTokenUsage,
    InvocationOutcome,
    ModelInvocation,
)
from server.infrastructure.mysql.models import (
    ModelQuotaAccountModel,
    ModelQuotaReservationModel,
    ModelUsageEventModel,
)


def _utc_naive(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("model usage timestamps must be timezone-aware")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("model usage timestamps must use UTC")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _event_payload(
    invocation: ModelInvocation,
    usage: CanonicalTokenUsage,
    outcome: InvocationOutcome,
) -> dict[str, Any]:
    attribution = invocation.attribution
    identity = invocation.identity
    return {
        "operation_id": invocation.operation_id,
        "request_id": attribution.request_id,
        "reservation_id": attribution.reservation_id,
        "user_id": attribution.user_id,
        "workspace_id": attribution.workspace_id,
        "conversation_id": attribution.conversation_id,
        "turn_id": attribution.turn_id,
        "worker_id": attribution.worker_id,
        "parent_operation_id": attribution.parent_operation_id,
        "purpose": attribution.purpose,
        "provider": identity.provider,
        "provider_model": identity.provider_model,
        "model_profile": identity.model_profile,
        "preset": identity.preset,
        "route": identity.route,
        "pricing_key": identity.pricing_key,
        "pricing_version": getattr(identity, "pricing_version", None),
        "charge_units": None,
        "attempt": invocation.attempt,
        "fallback_index": invocation.fallback_index,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.total_tokens,
        "usage_source": usage.source,
        "provider_response_id": usage.provider_response_id,
        "outcome_status": outcome.status,
        "finish_reason": outcome.finish_reason,
        "error_kind": outcome.error_kind,
        "started_at": invocation.started_at.isoformat(),
        "completed_at": outcome.completed_at.isoformat(),
    }


class DurableQuotaUsageReporter:
    """Persist every model attempt before allowing the caller to continue."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def report(
        self,
        invocation: ModelInvocation,
        usage: CanonicalTokenUsage,
        outcome: InvocationOutcome,
    ) -> None:
        attribution = invocation.attribution
        user_id = attribution.user_id
        payload = _event_payload(invocation, usage, outcome)
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        async with self._session_factory.begin() as session:
            reservation = await self._lock_reservation(session, attribution.reservation_id)
            if attribution.reservation_id is not None and reservation is None:
                raise ValueError("model usage reservation does not exist")
            if reservation is not None:
                if reservation.user_id != user_id:
                    raise ValueError("model usage reservation belongs to a different user")
                if attribution.turn_id and reservation.turn_id != attribution.turn_id:
                    raise ValueError("model usage reservation belongs to a different Turn")

            await self._ensure_account(session, user_id)
            account = await session.scalar(
                select(ModelQuotaAccountModel)
                .where(ModelQuotaAccountModel.user_id == user_id)
                .with_for_update()
            )
            if account is None:
                raise RuntimeError("model quota account disappeared during usage reporting")

            existing = await session.get(ModelUsageEventModel, invocation.operation_id)
            if existing is not None:
                if existing.payload_sha256 != payload_sha256:
                    raise UsageEventConflictError(
                        f"Conflicting usage report for operation_id {invocation.operation_id!r}"
                    )
                return

            session.add(
                ModelUsageEventModel(
                    operation_id=invocation.operation_id,
                    request_id=attribution.request_id,
                    reservation_id=attribution.reservation_id,
                    user_id=user_id,
                    workspace_id=attribution.workspace_id,
                    conversation_id=attribution.conversation_id,
                    turn_id=attribution.turn_id,
                    worker_id=attribution.worker_id,
                    parent_operation_id=attribution.parent_operation_id,
                    purpose=attribution.purpose,
                    provider=invocation.identity.provider,
                    provider_model=invocation.identity.provider_model,
                    model_profile=invocation.identity.model_profile,
                    preset=invocation.identity.preset,
                    route=invocation.identity.route,
                    pricing_key=invocation.identity.pricing_key,
                    pricing_version=getattr(invocation.identity, "pricing_version", None),
                    charge_units=None,
                    attempt=invocation.attempt,
                    fallback_index=invocation.fallback_index,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    cache_write_input_tokens=usage.cache_write_input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_output_tokens=usage.reasoning_output_tokens,
                    total_tokens=usage.total_tokens,
                    usage_source=usage.source,
                    provider_response_id=(usage.provider_response_id or "")[:256] or None,
                    outcome_status=outcome.status,
                    finish_reason=(outcome.finish_reason or "")[:128] or None,
                    error_kind=(outcome.error_kind or "")[:128] or None,
                    started_at=_utc_naive(invocation.started_at),
                    completed_at=_utc_naive(outcome.completed_at),
                    payload_sha256=payload_sha256,
                )
            )
            await session.flush()

            if usage.source == "none":
                account.unknown_usage_events += 1
                if reservation is not None:
                    reservation.unknown_usage_events += 1
                    if reservation.status != "reserved":
                        reservation.status = "unknown_usage"
            else:
                account.consumed_tokens += usage.total_tokens
                account.used_tokens += usage.total_tokens
                if reservation is not None:
                    reservation.used_tokens += usage.total_tokens

    @staticmethod
    async def _ensure_account(session: AsyncSession, user_id: str) -> None:
        statement = mysql_insert(ModelQuotaAccountModel).values(user_id=user_id)
        await session.execute(
            statement.on_duplicate_key_update(user_id=statement.inserted.user_id)
        )

    @staticmethod
    async def _lock_reservation(
        session: AsyncSession, reservation_id: str | None
    ) -> ModelQuotaReservationModel | None:
        if reservation_id is None:
            return None
        return await session.scalar(
            select(ModelQuotaReservationModel)
            .where(ModelQuotaReservationModel.id == reservation_id)
            .with_for_update()
        )
