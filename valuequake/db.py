import os
from pathlib import Path

from sqlalchemy import Engine, create_engine


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or os.environ["DATABASE_URL"])


def apply_schema(engine: Engine) -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
