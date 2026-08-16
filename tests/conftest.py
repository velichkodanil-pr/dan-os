import os
import subprocess
import sys

# Environment MUST be set before importing app modules (settings is import-time).
# DANOS_TEST_DB lets parallel test runs use isolated databases (CI / local fan-out).
TEST_DB = os.environ.get(
    "DANOS_TEST_DB", "postgresql://postgres:postgres@localhost:5432/danos_test")
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "EXTRACTOR": "mock",
    "TRANSCRIBER": "mock",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "OWNER_TELEGRAM_ID": "111",
    "TELEGRAM_BOT_TOKEN": "",
    "TZ_NAME": "Europe/Kyiv",
    "CHAT_MODEL": "mock",
})

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db as database  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated():
    """Migration smoke test: alembic upgrade head must succeed on a fresh DB."""
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"],
                   env=env, capture_output=True)
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    yield


@pytest.fixture(autouse=True)
async def fresh_engine():
    """Engine per test: pytest-asyncio runs each test in its own event loop."""
    database.init_engine()
    yield
    async with database.session() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        await db.commit()
    await database.engine.dispose()


@pytest.fixture
async def db():
    async with database.session() as session:
        yield session
