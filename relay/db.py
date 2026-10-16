from importlib.resources import files

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(database_url: str) -> ConnectionPool:
    # UTC sessions: timestamps come back in UTC whatever the server or laptop time zone is.
    kwargs = {"row_factory": dict_row, "options": "-c TimeZone=UTC"}
    return ConnectionPool(database_url, kwargs=kwargs, min_size=1, max_size=5, open=True)


def apply_schema(pool: ConnectionPool) -> None:
    sql = files("relay").joinpath("schema.sql").read_text()
    with pool.connection() as conn:
        conn.execute(sql)
