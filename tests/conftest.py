"""Fixtures compartilhadas. Testes de banco exigem um Postgres DESCARTÁVEL em
TEST_DATABASE_URL (o nome do banco precisa conter 'test': o fixture recria o schema
public a cada teste e se recusa a rodar em qualquer outro banco)."""
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def pdf_real_bytes():
    return (FIXTURES / "relatorio_defensivos_22_24.pdf").read_bytes()


@pytest.fixture(scope="session")
def base_xlsx_bytes():
    return (FIXTURES / "base_defensivos_21_09.xlsx").read_bytes()


@pytest.fixture
def banco(monkeypatch):
    from estoque import db

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("defina TEST_DATABASE_URL apontando para um Postgres descartável (nome do banco com 'test')")
    if "test" not in (make_url(url).database or "").lower():
        pytest.fail("TEST_DATABASE_URL deve apontar para um banco cujo nome contém 'test'")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    monkeypatch.setattr(db, "get_engine", lambda: eng)
    db.init_schema()
    yield db
    eng.dispose()
