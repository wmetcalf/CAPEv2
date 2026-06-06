import pytest

from lib.cuckoo.core.database import init_database, reset_database_FOR_TESTING_ONLY


@pytest.fixture
def cape_db():
    """Initialize the SQLAlchemy CAPE database (in-memory) for web-view tests
    that exercise the global `db` proxy (e.g. report/submit views). Named
    distinctly from `db` to avoid colliding with pytest-django's `db` fixture
    (which only sets up the Django DB, not CAPE's SQLAlchemy database)."""
    reset_database_FOR_TESTING_ONLY()
    try:
        init_database(dsn="sqlite://")
        yield
    finally:
        reset_database_FOR_TESTING_ONLY()
