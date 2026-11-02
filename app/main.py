"""Rawaj Pay transfer API. See README.md for the endpoints, errors and rules."""

import hashlib
import json
import uuid
from typing import Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import Account, IdempotencyRecord, Transfer
from app.settings import Settings


# ---------------------------------------------------------
# Rate Limiter Implementation
# ---------------------------------------------------------
class TokenBucketRateLimiter:
    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens: Dict[str, float] = {}
        self.last_update: Dict[str, float] = {}

    def allow_request(self, key: str, current_time: float) -> bool:
        if key not in self.tokens:
            self.tokens[key] = self.capacity
            self.last_update[key] = current_time

        elapsed = current_time - self.last_update[key]
        self.last_update[key] = current_time

        self.tokens[key] = min(self.capacity, self.tokens[key] + elapsed * self.refill_rate)

        if self.tokens[key] >= 1.0:
            self.tokens[key] -= 1.0
            return True
        return False


# ---------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------
class CreateTransferRequest(BaseModel):
    from_account_id: str
    to_account_id: str
    amount_piastres: int = Field(gt=0)
    memo: Optional[str] = Field(default=None, max_length=140)


# ---------------------------------------------------------
# Application Factory
# ---------------------------------------------------------
def create_app(settings: Settings = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    app = FastAPI()

    # Engine and Session Factory setup
    engine = make_engine(settings.database_url)
    app.state.engine = engine
    session_factory = make_session_factory(engine)

    rate_limiter = TokenBucketRateLimiter(
        capacity=float(settings.rate_limit_capacity),
        refill_rate=float(settings.rate_limit_refill_per_second),
    )

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def authenticate_customer(authorization: Optional[str]) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization header",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization.split(" ", 1)[1]
        customer_id = settings.api_tokens.get(token)
        if not customer_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return customer_id

    # 1. GET Account Details Endpoint
    @app.get("/v1/accounts/{account_id}")
    def get_account(
        account_id: str,
        authorization: Optional[str] = Header(None, alias="Authorization"),
    ):
        customer_id = authenticate_customer(authorization)

        db: Session = next(get_db())
        try:
            acc = db.execute(
                select(Account).where(Account.id == account_id)
            ).scalar_one_or_none()

            if not acc or acc.owner_id != customer_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Account not found",
                )

            return {
                "id": acc.id,
                "owner_id": acc.owner_id,
                "balance_piastres": acc.balance_piastres,
                "currency": acc.currency,
            }
        finally:
            db.close()

    # 2. POST Transfers Endpoint
    @app.post("/v1/transfers", status_code=status.HTTP_201_CREATED)
    def create_transfer(
        payload: CreateTransferRequest,
        authorization: Optional[str] = Header(None, alias="Authorization"),
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
        x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
    ):
        # Authenticate User
        customer_id = authenticate_customer(authorization)

        # Catch idempotency key
        key = idempotency_key or x_idempotency_key

        # Rate Limiting Check
        current_time = settings.clock()
        if not rate_limiter.allow_request(customer_id, current_time):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )

        # Check maximum transfer limit
        if payload.amount_piastres > settings.max_transfer_piastres:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer amount exceeds maximum limit",
            )

        # Create sorted deterministic JSON hash for Idempotency
        payload_dict = payload.model_dump()
        raw_json_str = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(raw_json_str.encode("utf-8")).hexdigest()

        db: Session = next(get_db())

        try:
            # Check Idempotency Key FIRST
            if key:
                stmt = select(IdempotencyRecord).where(
                    IdempotencyRecord.customer_id == customer_id,
                    IdempotencyRecord.key == key,
                )
                existing_record = db.execute(stmt).scalar_one_or_none()

                if existing_record:
                    if existing_record.request_hash != request_hash:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Idempotency key payload mismatch",
                        )

                    # إضافة Header Idempotent-Replayed: true في حالة إعادة الطلب
                    return Response(
                        content=existing_record.response_body,
                        status_code=existing_record.response_status,
                        headers={"idempotent-replayed": "true"},
                        media_type="application/json",
                    )

            if payload.from_account_id == payload.to_account_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot transfer money to the same account",
                )

            # Sort IDs to prevent deadlock
            first_id, second_id = sorted([payload.from_account_id, payload.to_account_id])

            acc1 = db.execute(
                select(Account).where(Account.id == first_id).with_for_update()
            ).scalar_one_or_none()

            acc2 = db.execute(
                select(Account).where(Account.id == second_id).with_for_update()
            ).scalar_one_or_none()

            if not acc1 or not acc2:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="One or both accounts were not found",
                )

            from_acc = acc1 if acc1.id == payload.from_account_id else acc2
            to_acc = acc2 if acc2.id == payload.to_account_id else acc1

            if from_acc.owner_id != customer_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Account not found",
                )

            if from_acc.currency != to_acc.currency:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Currency mismatch between accounts",
                )

            if from_acc.balance_piastres < payload.amount_piastres:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Insufficient balance",
                )

            # Perform Transfer
            from_acc.balance_piastres -= payload.amount_piastres
            to_acc.balance_piastres += payload.amount_piastres

            transfer_id = f"trf_{uuid.uuid4().hex[:16]}"
            transfer = Transfer(
                id=transfer_id,
                from_account_id=payload.from_account_id,
                to_account_id=payload.to_account_id,
                amount_piastres=payload.amount_piastres,
                memo=payload.memo,
                status="completed",
            )
            db.add(transfer)

            response_data = {
                "id": transfer.id,
                "from_account_id": transfer.from_account_id,
                "to_account_id": transfer.to_account_id,
                "amount_piastres": transfer.amount_piastres,
                "memo": transfer.memo,
                "status": transfer.status,
            }
            response_json_str = json.dumps(response_data)

            if key:
                idem_record = IdempotencyRecord(
                    customer_id=customer_id,
                    key=key,
                    request_hash=request_hash,
                    response_status=status.HTTP_201_CREATED,
                    response_body=response_json_str,
                )
                db.add(idem_record)

            db.commit()

            return Response(
                content=response_json_str,
                status_code=status.HTTP_201_CREATED,
                media_type="application/json",
            )

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            )
        finally:
            db.close()

    return app


app = create_app()