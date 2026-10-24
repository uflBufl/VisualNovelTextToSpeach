import os
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from vntts import generation_database


def replace_generation_state(path: Path, state: dict[str, object]) -> None:
    """Publish a complete fixture, including deliberate queue-authority changes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".sqlite3", delete=False) as stream:
        temporary = Path(stream.name)
    temporary.unlink()
    try:
        generation_database.write_state(temporary, state)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def corrupt_generation_item(path: Path, queue_id: str, item: dict[str, object]) -> None:
    """Inject malformed evidence directly, bypassing normal writer admission."""
    import json
    import sqlite3

    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE items SET payload=?,status=?,review_status=? WHERE queue_id=?",
            (
                json.dumps(item, ensure_ascii=True),
                item["status"],
                item["review_status"],
                queue_id,
            ),
        )
        connection.commit()
