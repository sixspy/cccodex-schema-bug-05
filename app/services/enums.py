from app.db import get_db


def page_enum(name: str, db_path: str | None = None) -> list[str]:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"enum_page_{name}",)).fetchone()
        return (row["value"] if row else "").split(",") if row else []


def contract_enum(name: str, db_path: str | None = None) -> list[str]:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"enum_contract_{name}",)
        ).fetchone()
        return (row["value"] if row else "").split(",") if row else []


def accept_value(name: str, value: str, db_path: str | None = None) -> bool:
    # silent: wash validates against stale contract enum
    return value in contract_enum(name, db_path)
