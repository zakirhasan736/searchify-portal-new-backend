from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
    pool_timeout=10,
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)",
    "CREATE INDEX IF NOT EXISTS ix_feature_records_customer_kind_id ON feature_records (customer_id, kind, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_feature_records_customer_kind_title ON feature_records (customer_id, kind, title)",
    "CREATE INDEX IF NOT EXISTS ix_site_changes_customer_status ON site_changes (customer_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_site_changes_customer_created ON site_changes (customer_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_cms_connections_customer_provider ON cms_connections (customer_id, provider)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_customer_created ON jobs (customer_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_google_connections_status ON google_connections (status)",
    "CREATE INDEX IF NOT EXISTS ix_google_connections_customer ON google_connections (customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_feature_records_kind_created ON feature_records (kind, created_at DESC)",
]


COLUMN_SQL = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(40) DEFAULT 'starter'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS site_limit INTEGER DEFAULT 5",
    "ALTER TABLE site_changes ADD COLUMN IF NOT EXISTS approved_by INTEGER",
    "ALTER TABLE site_changes ADD COLUMN IF NOT EXISTS approved_by_name VARCHAR(120) DEFAULT ''",
    "ALTER TABLE site_changes ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP",
]


def ensure_indexes() -> None:
    try:
        with engine.begin() as conn:
            for stmt in COLUMN_SQL + INDEX_SQL:
                conn.execute(text(stmt))
    except Exception as exc:
        print(f"ensure_indexes skipped: {exc}")
