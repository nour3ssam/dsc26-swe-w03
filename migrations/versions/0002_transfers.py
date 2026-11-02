
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create transfers table
    op.create_table(
        'transfers',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('from_account_id', sa.String(length=32), nullable=False),
        sa.Column('to_account_id', sa.String(length=32), nullable=False),
        sa.Column('amount_piastres', sa.Integer(), nullable=False),
        sa.Column('memo', sa.String(length=140), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.CheckConstraint('amount_piastres > 0', name='ck_transfers_amount_positive'),
        sa.ForeignKeyConstraint(['from_account_id'], ['accounts.id'], ),
        sa.ForeignKeyConstraint(['to_account_id'], ['accounts.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    # 2. Create idempotency_records table
    op.create_table(
        'idempotency_records',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('customer_id', sa.String(length=32), nullable=False),
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('response_status', sa.Integer(), nullable=False),
        sa.Column('response_body', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('customer_id', 'key', name='uq_idempotency_customer_key')
    )


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_table('idempotency_records')
    op.drop_table('transfers')