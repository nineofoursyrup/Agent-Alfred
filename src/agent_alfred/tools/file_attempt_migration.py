"""Forward migration: legacy NULL identity does not prove create was unattempted."""


def migrate_file_attempts(conn):
    conn.execute(
        "ALTER TABLE file_operations ADD COLUMN publication_attempted INTEGER "
        "NOT NULL DEFAULT 1 CHECK(publication_attempted IN (0,1))"
    )
