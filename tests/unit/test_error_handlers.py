"""Unit tests for the global DBAPIError handler (no DB, no Docker).

A NUL byte in a request string (percent-00 path param, \\u0000 body member)
reaches Postgres and dies with SQLSTATE 22021 (character_not_in_repertoire) —
a client input problem, answered 422 at one global point. Every other
DBAPIError must re-raise so the loud-500 posture (traceback via
ServerErrorMiddleware) is preserved.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import DBAPIError

from geoid.api.errors import register_exception_handlers

pytestmark = pytest.mark.unit


class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("boom")
        self.sqlstate = sqlstate


def _dbapi_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT ...", {}, _FakeDriverError(sqlstate))


@pytest.fixture
def dbapi_handler():
    app = FastAPI()
    register_exception_handlers(app)
    return app.exception_handlers[DBAPIError]


async def test_nul_byte_sqlstate_maps_to_422(dbapi_handler):
    response = await dbapi_handler(None, _dbapi_error("22021"))
    assert response.status_code == 422
    body = json.loads(response.body)
    assert body == {"code": 422, "message": "invalid characters in input (NUL)"}


async def test_other_sqlstate_reraises_original_exception(dbapi_handler):
    exc = _dbapi_error("58030")
    with pytest.raises(DBAPIError) as exc_info:
        await dbapi_handler(None, exc)
    assert exc_info.value is exc
