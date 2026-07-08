from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row

from .config import postgres_dsn


class Database:
    def __init__(self, url: str):
        self.url = postgres_dsn(url)

    @asynccontextmanager
    async def transaction(self):
        async with (
            await psycopg.AsyncConnection.connect(
                self.url, row_factory=dict_row, connect_timeout=5, options="-c timezone=UTC"
            ) as connection,
            connection.transaction(),
        ):
            yield connection

    async def fetch_one(self, query, parameters=()):
        async with self.transaction() as connection:
            return await (await connection.execute(query, parameters)).fetchone()

    async def fetch_all(self, query, parameters=()):
        async with self.transaction() as connection:
            return await (await connection.execute(query, parameters)).fetchall()
