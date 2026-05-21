#!/usr/bin/env python3
"""Sync coded translation XML entries into Oracle using insert-only history semantics."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import oracledb
except ImportError:
    oracledb = None


@dataclass(frozen=True)
class TranslationRow:
    translation_name: str
    entry_key: int | None
    entry_key_text: str
    short_name: str | None
    text_value: str | None
    text1_value: str | None
    image_name: str | None
    default_image_name: str | None
    read_only_flag: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Insert CodedTranslationConfig XML snapshots into an Oracle schema (dual-site supported)"
    )
    parser.add_argument(
        "--xml-file",
        required=True,
        help="Path to CodedTranslationConfig XML file",
    )
    # Primary DB
    parser.add_argument(
        "--db-user",
        default=None,
        help="Primary Oracle DB username (or use ORACLE_DB_USER env var)",
    )
    parser.add_argument(
        "--db-password",
        default=None,
        help="Primary Oracle DB password (or use ORACLE_DB_PASSWORD env var)",
    )
    parser.add_argument(
        "--db-dsn",
        default=None,
        help=(
            "Primary Oracle DSN, e.g. host:1521/service (or use ORACLE_DB_DSN env var)"
        ),
    )
    # Secondary DB
    parser.add_argument(
        "--db2-user",
        default=None,
        help="Secondary Oracle DB username (or use ORACLE_DB2_USER env var)",
    )
    parser.add_argument(
        "--db2-password",
        default=None,
        help="Secondary Oracle DB password (or use ORACLE_DB2_PASSWORD env var)",
    )
    parser.add_argument(
        "--db2-dsn",
        default=None,
        help=(
            "Secondary Oracle DSN, e.g. host:1521/service (or use ORACLE_DB2_DSN env var)"
        ),
    )
    parser.add_argument(
        "--create-table-if-missing",
        action="store_true",
        help="Create target table if it does not already exist",
    )
    parser.add_argument(
        "--source-commit",
        default=None,
        help="Optional Git commit SHA for traceability (or use GIT_COMMIT env var)",
    )
    parser.add_argument(
        "--sync-run-id",
        default=None,
        help="Optional run identifier to group inserted snapshot rows",
    )
    parser.add_argument(
        "--dry-run-sql",
        action="store_true",
        help=(
            "Parse XML and print SQL that would run (no Oracle connection). "
            "Use with --create-table-if-missing to show first-run DDL."
        ),
    )
    parser.add_argument(
        "--since-commit",
        default=None,
        help=(
            "Optional base commit SHA used for git diff gating "
            "(or use GIT_PREVIOUS_SUCCESSFUL_COMMIT env var)"
        ),
    )
    parser.add_argument(
        "--current-commit",
        default=None,
        help=(
            "Optional target commit SHA used for git diff gating "
            "(defaults to --source-commit or GIT_COMMIT env var)"
        ),
    )
    parser.add_argument(
        "--repo-dir",
        default=None,
        help="Optional git repository directory for diff checks (defaults to XML file parent)",
    )
    return parser.parse_args()


def text_or_none(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = (element.text or "").strip()
    return value or None


def parse_xml(xml_path: Path) -> list[TranslationRow]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Root and children use a default namespace in this XML.
    ns = {"ct": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {"ct": ""}

    rows: list[TranslationRow] = []
    for trans in root.findall(".//ct:CodedTranslation", ns):
        name = text_or_none(trans.find("ct:Name", ns))
        if not name:
            raise ValueError("A CodedTranslation node is missing Name")

        default_image_name = text_or_none(trans.find("ct:DefaultImageName", ns))
        read_only_text = (text_or_none(trans.find("ct:ReadOnly", ns)) or "false").lower()
        read_only_flag = 1 if read_only_text == "true" else 0

        for entry in trans.findall("ct:Entries/ct:CodedTranslationEntry", ns):
            key_text = text_or_none(entry.find("ct:Key", ns))
            if key_text is None:
                raise ValueError(f"Translation '{name}' has an entry with no Key")

            entry_key: int | None
            try:
                entry_key = int(key_text)
            except ValueError:
                entry_key = None

            row = TranslationRow(
                translation_name=name,
                entry_key=entry_key,
                entry_key_text=key_text,
                short_name=text_or_none(entry.find("ct:ShortName", ns)),
                text_value=text_or_none(entry.find("ct:Text", ns)),
                text1_value=text_or_none(entry.find("ct:Text1", ns)),
                image_name=text_or_none(entry.find("ct:ImageName", ns)),
                default_image_name=default_image_name,
                read_only_flag=read_only_flag,
            )
            rows.append(row)

    if not rows:
        raise ValueError("No translation entries found in XML")

    return rows


def get_create_table_plsql() -> str:
    return """
    DECLARE
      table_exists NUMBER := 0;
    BEGIN
      SELECT COUNT(1)
      INTO table_exists
      FROM user_tables
    WHERE table_name = 'ADMS_CODED_TRANSLATION_ENTRIES';

      IF table_exists = 0 THEN
                EXECUTE IMMEDIATE '
                    CREATE TABLE ADMS_CODED_TRANSLATION_ENTRIES (
                        record_id          NUMBER GENERATED BY DEFAULT AS IDENTITY,
            translation_name   VARCHAR2(200 CHAR) NOT NULL,
            entry_key          NUMBER(10),
            entry_key_text     VARCHAR2(200 CHAR) NOT NULL,
            short_name         VARCHAR2(400 CHAR),
            text_value         VARCHAR2(4000 CHAR),
            text1_value        VARCHAR2(4000 CHAR),
            image_name         VARCHAR2(400 CHAR),
            default_image_name VARCHAR2(400 CHAR),
            read_only_flag     NUMBER(1) DEFAULT 0 NOT NULL,
            source_system      VARCHAR2(100 CHAR) DEFAULT ''github'' NOT NULL,
                        source_commit      VARCHAR2(64 CHAR),
                        sync_run_id        VARCHAR2(36 CHAR) NOT NULL,
                        added_on           TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
                        CONSTRAINT PK_ADMS_CODED_TRANSLATION_ENTRIES PRIMARY KEY (record_id)
          )';

                EXECUTE IMMEDIATE '
                    CREATE INDEX IDX_ADMS_CODED_TRANSLATION_LOOKUP
                    ON ADMS_CODED_TRANSLATION_ENTRIES (translation_name, entry_key_text, added_on)';

                EXECUTE IMMEDIATE '
                    CREATE INDEX IDX_ADMS_CODED_TRANSLATION_RUN
                    ON ADMS_CODED_TRANSLATION_ENTRIES (sync_run_id)';
      END IF;
    END;
    """


def create_table(cursor: Any) -> None:
    plsql = get_create_table_plsql()
    cursor.execute(plsql)


def insert_rows(
    cursor: Any,
    rows: list[TranslationRow],
    source_commit: str | None,
    sync_run_id: str,
) -> None:
    insert_sql = get_insert_sql()

    bind_rows = []
    for row in rows:
        bind_rows.append(
            {
                "translation_name": row.translation_name,
                "entry_key": row.entry_key,
                "entry_key_text": row.entry_key_text,
                "short_name": row.short_name,
                "text_value": row.text_value,
                "text1_value": row.text1_value,
                "image_name": row.image_name,
                "default_image_name": row.default_image_name,
                "read_only_flag": row.read_only_flag,
                "source_commit": source_commit,
                "sync_run_id": sync_run_id,
            }
        )

    cursor.executemany(insert_sql, bind_rows)


def fetch_latest_rows(cursor: Any) -> dict[tuple[str, str], TranslationRow]:
    # Pull latest row per (translation_name, entry_key_text) to detect deltas.
    select_sql = """
        SELECT translation_name,
               entry_key,
               entry_key_text,
               short_name,
               text_value,
               text1_value,
               image_name,
               default_image_name,
               read_only_flag
          FROM (
            SELECT translation_name,
                   entry_key,
                   entry_key_text,
                   short_name,
                   text_value,
                   text1_value,
                   image_name,
                   default_image_name,
                   read_only_flag,
                   ROW_NUMBER() OVER (
                     PARTITION BY translation_name, entry_key_text
                     ORDER BY added_on DESC, record_id DESC
                   ) AS rn
              FROM ADMS_CODED_TRANSLATION_ENTRIES
          )
         WHERE rn = 1
    """
    cursor.execute(select_sql)

    latest: dict[tuple[str, str], TranslationRow] = {}
    for (
        translation_name,
        entry_key,
        entry_key_text,
        short_name,
        text_value,
        text1_value,
        image_name,
        default_image_name,
        read_only_flag,
    ) in cursor:
        latest[(translation_name, entry_key_text)] = TranslationRow(
            translation_name=translation_name,
            entry_key=entry_key,
            entry_key_text=entry_key_text,
            short_name=short_name,
            text_value=text_value,
            text1_value=text1_value,
            image_name=image_name,
            default_image_name=default_image_name,
            read_only_flag=read_only_flag,
        )
    return latest


def is_row_changed(new_row: TranslationRow, old_row: TranslationRow) -> bool:
    return (
        new_row.entry_key != old_row.entry_key
        or new_row.short_name != old_row.short_name
        or new_row.text_value != old_row.text_value
        or new_row.text1_value != old_row.text1_value
        or new_row.image_name != old_row.image_name
        or new_row.default_image_name != old_row.default_image_name
        or new_row.read_only_flag != old_row.read_only_flag
    )


def changed_rows_only(rows: list[TranslationRow], latest_rows: dict[tuple[str, str], TranslationRow]) -> list[TranslationRow]:
    changed: list[TranslationRow] = []
    for row in rows:
        key = (row.translation_name, row.entry_key_text)
        old_row = latest_rows.get(key)
        if old_row is None or is_row_changed(row, old_row):
            changed.append(row)
    return changed


def xml_changed_between_commits(
    repo_dir: Path,
    xml_path: Path,
    since_commit: str,
    current_commit: str,
) -> bool:
    repo_dir = repo_dir.resolve()
    xml_path = xml_path.resolve()

    try:
        rel_xml = str(xml_path.relative_to(repo_dir))
    except ValueError as exc:
        raise ValueError(f"XML file '{xml_path}' is not under repo dir '{repo_dir}'") from exc

    cmd = [
        "git",
        "-C",
        str(repo_dir),
        "diff",
        "--quiet",
        since_commit,
        current_commit,
        "--",
        rel_xml,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    stderr = (result.stderr or "").strip()
    raise RuntimeError(
        f"git diff failed (exit={result.returncode}) for {since_commit}..{current_commit}: {stderr}"
    )


def get_insert_sql() -> str:
    return """
        INSERT INTO ADMS_CODED_TRANSLATION_ENTRIES (
            translation_name,
            entry_key,
            entry_key_text,
            short_name,
            text_value,
            text1_value,
            image_name,
            default_image_name,
            read_only_flag,
            source_system,
            source_commit,
            sync_run_id,
            added_on
        ) VALUES (
            :translation_name,
            :entry_key,
            :entry_key_text,
            :short_name,
            :text_value,
            :text1_value,
            :image_name,
            :default_image_name,
            :read_only_flag,
            'github',
            :source_commit,
            :sync_run_id,
            SYSTIMESTAMP
        )
        """

def print_dry_run_sql(
    parsed_rows: int | None,
    source_commit: str | None,
    sync_run_id: str,
    create_table_if_missing: bool,
) -> None:
    print("-- DRY RUN: No database connection will be made")
    if parsed_rows is None:
        print("-- Parsed rows: n/a (XML parse warning; SQL shown anyway)")
    else:
        print(f"-- Parsed rows: {parsed_rows}")
    print(f"-- source_commit: {source_commit or 'n/a'}")
    print(f"-- sync_run_id: {sync_run_id}")

    if create_table_if_missing:
        print("\n-- First-run table creation SQL (assumes empty schema)")
        print(get_create_table_plsql().strip())

    print("\n-- Insert SQL")
    print(get_insert_sql().strip())



def get_db_config(args, prefix: str = ""):
    return {
        "user": getattr(args, f"{prefix}user") or os.environ.get(f"ORACLE_{prefix.upper()}USER"),
        "password": getattr(args, f"{prefix}password") or os.environ.get(f"ORACLE_{prefix.upper()}PASSWORD"),
        "dsn": getattr(args, f"{prefix}dsn") or os.environ.get(f"ORACLE_{prefix.upper()}DSN"),
    }

def main() -> int:
    args = parse_args()

    # Primary DB
    db1 = get_db_config(args, "db_")
    # Secondary DB
    db2 = get_db_config(args, "db2_")

    source_commit = args.source_commit or os.environ.get("GIT_COMMIT")
    sync_run_id = args.sync_run_id or str(uuid.uuid4())
    since_commit = args.since_commit or os.environ.get("GIT_PREVIOUS_SUCCESSFUL_COMMIT")
    current_commit = args.current_commit or source_commit

    xml_path = Path(args.xml_file)
    if not xml_path.exists():
        print(f"XML file not found: {xml_path}", file=sys.stderr)
        return 2

    if since_commit and current_commit:
        repo_dir = Path(args.repo_dir) if args.repo_dir else xml_path.parent
        try:
            xml_changed = xml_changed_between_commits(
                repo_dir=repo_dir,
                xml_path=xml_path,
                since_commit=since_commit,
                current_commit=current_commit,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Failed git diff gate: {exc}", file=sys.stderr)
            return 2

        if not xml_changed:
            print(
                "No XML changes detected between commits "
                f"{since_commit}..{current_commit}; skipping sync."
            )
            return 0

    if args.dry_run_sql:
        parsed_rows: int | None = None
        try:
            parsed_rows = len(parse_xml(xml_path))
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: XML parse failed during dry-run: {exc}", file=sys.stderr)
        print_dry_run_sql(parsed_rows, source_commit, sync_run_id, args.create_table_if_missing)
        return 0

    try:
        rows = parse_xml(xml_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to parse XML: {exc}", file=sys.stderr)
        return 2

    if not db1["user"] or not db1["password"] or not db1["dsn"]:
        print(
            "Missing primary DB connection value: provide --db-user/--db-password/--db-dsn "
            "or ORACLE_DB_USER/ORACLE_DB_PASSWORD/ORACLE_DB_DSN",
            file=sys.stderr,
        )
        return 2

    if oracledb is None:
        print(
            "Python package 'oracledb' is not installed. Install requirements.txt before DB sync.",
            file=sys.stderr,
        )
        return 2

    # Write to both DBs
    results = []
    for idx, db in enumerate([db1, db2], 1):
        if not db["user"] or not db["password"] or not db["dsn"]:
            if idx == 1:
                print("Primary DB config missing, aborting.", file=sys.stderr)
                return 2
            else:
                print("Secondary DB config missing, skipping.", file=sys.stderr)
                results.append((idx, False, "missing config"))
                continue
        try:
            with oracledb.connect(user=db["user"], password=db["password"], dsn=db["dsn"]) as conn:
                with conn.cursor() as cursor:
                    if args.create_table_if_missing:
                        create_table(cursor)
                    rows_to_insert = changed_rows_only(rows, fetch_latest_rows(cursor))
                    if rows_to_insert:
                        insert_rows(cursor, rows_to_insert, source_commit, sync_run_id)
                conn.commit()
            results.append((idx, True, str(len(rows_to_insert))))
        except Exception as exc:
            print(f"DB{idx} sync failed: {exc}", file=sys.stderr)
            results.append((idx, False, str(exc)))

    ok_count = sum(1 for _, ok, _ in results if ok)
    if ok_count == 0:
        print("Sync failed: could not write to either DB.", file=sys.stderr)
        return 1

    print(
        f"Sync complete: parsed={len(rows)} inserted_db1={results[0][2] if results and results[0][1] else '0'} "
        f"inserted_db2={results[1][2] if len(results)>1 and results[1][1] else '0'} sync_run_id={sync_run_id} "
        f"xml={xml_path} source_commit={source_commit or 'n/a'} "
        f"db1={'ok' if results[0][1] else 'fail'} db2={'ok' if len(results)>1 and results[1][1] else 'fail'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
