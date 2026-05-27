#!/usr/bin/env python3
"""Sync coded translation XML entries into Oracle using insert-only history semantics."""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import oracledb
except ImportError:
    oracledb = None


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslationRow:
    translation_name: str
    entry_key_text: str
    short_name: str | None
    text_value: str | None
    text1_value: str | None
    image_name: str | None
    default_image_name: str | None
    read_only_value: str


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
        help="Parse XML and print insert SQL that would run (no Oracle connection).",
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
    parser.add_argument(
        "--table-name",
        default=None,
        help=(
            "Target Oracle table name (optionally schema-qualified), "
            "e.g. ADMS_CODED_TRANSLATION or GMSSI_USER2.ADMS_CODED_TRANSLATION "
            "(or use ORACLE_DB_TABLE env var)"
        ),
    )
    return parser.parse_args()


def validate_table_name(table_name: str) -> str:
    pattern = r"^[A-Za-z][A-Za-z0-9_$#]*(\.[A-Za-z][A-Za-z0-9_$#]*)?$"
    if not re.fullmatch(pattern, table_name):
        raise ValueError(
            "Invalid table name. Use unquoted Oracle identifiers, optionally schema-qualified. "
            "Examples: ADMS_CODED_TRANSLATION or GMSSI_USER2.ADMS_CODED_TRANSLATION"
        )
    return table_name.upper()


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

        for entry in trans.findall("ct:Entries/ct:CodedTranslationEntry", ns):
            key_text = text_or_none(entry.find("ct:Key", ns))
            if key_text is None:
                raise ValueError(f"Translation '{name}' has an entry with no Key")

            row = TranslationRow(
                translation_name=name,
                entry_key_text=key_text,
                short_name=text_or_none(entry.find("ct:ShortName", ns)),
                text_value=text_or_none(entry.find("ct:Text", ns)),
                text1_value=text_or_none(entry.find("ct:Text1", ns)),
                image_name=text_or_none(entry.find("ct:ImageName", ns)),
                default_image_name=default_image_name,
                read_only_value=read_only_text,
            )
            rows.append(row)

    if not rows:
        raise ValueError("No translation entries found in XML")

    return rows


def insert_rows(
    cursor: Any,
    rows: list[TranslationRow],
    table_name: str,
) -> None:
    insert_sql = get_insert_sql(table_name)

    bind_rows = []
    for row in rows:
        bind_rows.append(
            {
                "translation_name": row.translation_name,
                "entry_key_text": row.entry_key_text,
                "short_name": row.short_name,
                "text_value": row.text_value,
                "text1_value": row.text1_value,
                "image_name": row.image_name,
                "default_image_name": row.default_image_name,
                "read_only_value": row.read_only_value,
            }
        )

    cursor.executemany(insert_sql, bind_rows)


def fetch_latest_rows(cursor: Any, table_name: str) -> dict[tuple[str, str], TranslationRow]:
    # Pull latest row per (translation_name, entry_key_text) to detect deltas.
    select_sql = """
     SELECT NAME,
         "KEY" AS KEY_TEXT,
         SHORTNAME,
         "TEXT" AS TEXT_VALUE,
         TEXT1,
         IMAGENAME,
         DEFAULTIMAGENAME,
         READONLY
          FROM (
         SELECT NAME,
             "KEY",
             SHORTNAME,
             "TEXT",
             TEXT1,
             IMAGENAME,
             DEFAULTIMAGENAME,
             READONLY,
                   ROW_NUMBER() OVER (
            PARTITION BY NAME, "KEY"
            ORDER BY DATEADDED DESC, ROWID DESC
                   ) AS rn
                    FROM {table_name}
          )
         WHERE rn = 1
        """.format(table_name=table_name)
    cursor.execute(select_sql)

    latest: dict[tuple[str, str], TranslationRow] = {}
    for (
        translation_name,
        entry_key_text,
        short_name,
        text_value,
        text1_value,
        image_name,
        default_image_name,
        read_only_value,
    ) in cursor:
        latest[(translation_name, entry_key_text)] = TranslationRow(
            translation_name=translation_name,
            entry_key_text=entry_key_text,
            short_name=short_name,
            text_value=text_value,
            text1_value=text1_value,
            image_name=image_name,
            default_image_name=default_image_name,
            read_only_value=(read_only_value or "").lower(),
        )
    return latest


def is_row_changed(new_row: TranslationRow, old_row: TranslationRow) -> bool:
    return (
        new_row.short_name != old_row.short_name
        or new_row.text_value != old_row.text_value
        or new_row.text1_value != old_row.text1_value
        or new_row.image_name != old_row.image_name
        or new_row.default_image_name != old_row.default_image_name
        or new_row.read_only_value != old_row.read_only_value
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


def get_insert_sql(table_name: str) -> str:
    return """
        INSERT INTO {table_name} (
            NAME,
            DEFAULTIMAGENAME,
            READONLY,
            "KEY",
            IMAGENAME,
            SHORTNAME,
            "TEXT",
            TEXT1,
            DATEADDED
        ) VALUES (
            :translation_name,
            :default_image_name,
            :read_only_value,
            :entry_key_text,
            :image_name,
            :short_name,
            :text_value,
            :text1_value,
            SYSTIMESTAMP
        )
        """.format(table_name=table_name)

def print_dry_run_sql(
    parsed_rows: int | None,
    source_commit: str | None,
    sync_run_id: str,
    table_name: str,
) -> None:
    print("-- DRY RUN: No database connection will be made")
    if parsed_rows is None:
        print("-- Parsed rows: n/a (XML parse warning; SQL shown anyway)")
    else:
        print(f"-- Parsed rows: {parsed_rows}")
    print(f"-- source_commit: {source_commit or 'n/a'}")
    print(f"-- sync_run_id: {sync_run_id}")
    print(f"-- table_name: {table_name}")

    print("\n-- Insert SQL")
    print(get_insert_sql(table_name).strip())



def get_db_config(args, prefix: str = ""):
    return {
        "user": getattr(args, f"{prefix}user") or os.environ.get(f"ORACLE_{prefix.upper()}USER"),
        "password": getattr(args, f"{prefix}password") or os.environ.get(f"ORACLE_{prefix.upper()}PASSWORD"),
        "dsn": getattr(args, f"{prefix}dsn") or os.environ.get(f"ORACLE_{prefix.upper()}DSN"),
    }


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def main() -> int:
    configure_logging()
    args = parse_args()

    # Primary DB
    db1 = get_db_config(args, "db_")
    # Secondary DB
    db2 = get_db_config(args, "db2_")

    source_commit = args.source_commit or os.environ.get("GIT_COMMIT")
    sync_run_id = args.sync_run_id or str(uuid.uuid4())
    since_commit = args.since_commit or os.environ.get("GIT_PREVIOUS_SUCCESSFUL_COMMIT")
    current_commit = args.current_commit or source_commit
    raw_table_name = args.table_name or os.environ.get("ORACLE_DB_TABLE") or "ADMS_CODED_TRANSLATION"
    try:
        table_name = validate_table_name(raw_table_name)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    xml_path = Path(args.xml_file)
    if not xml_path.exists():
        LOGGER.error("XML file not found: %s", xml_path)
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
            LOGGER.exception("Failed git diff gate")
            return 2

        if not xml_changed:
            LOGGER.info(
                "No XML changes detected between commits "
                f"{since_commit}..{current_commit}; skipping sync."
            )
            return 0

    if args.dry_run_sql:
        parsed_rows: int | None = None
        try:
            parsed_rows = len(parse_xml(xml_path))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("XML parse failed during dry-run: %s", exc)
        print_dry_run_sql(
            parsed_rows,
            source_commit,
            sync_run_id,
            table_name,
        )
        return 0

    try:
        rows = parse_xml(xml_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Failed to parse XML")
        return 2

    if not db1["user"] or not db1["password"] or not db1["dsn"]:
        LOGGER.error(
            "Missing primary DB connection value: provide --db-user/--db-password/--db-dsn "
            "or ORACLE_DB_USER/ORACLE_DB_PASSWORD/ORACLE_DB_DSN"
        )
        return 2

    if oracledb is None:
        LOGGER.error(
            "Python package 'oracledb' is not installed. Install requirements.txt before DB sync."
        )
        return 2

    # Write to both DBs
    results = []
    for idx, db in enumerate([db1, db2], 1):
        if not db["user"] or not db["password"] or not db["dsn"]:
            if idx == 1:
                LOGGER.error("Primary DB config missing, aborting.")
                return 2
            else:
                LOGGER.warning("Secondary DB config missing, skipping.")
                results.append((idx, False, "missing config"))
                continue
        try:
            with oracledb.connect(user=db["user"], password=db["password"], dsn=db["dsn"]) as conn:
                with conn.cursor() as cursor:
                    rows_to_insert = changed_rows_only(rows, fetch_latest_rows(cursor, table_name))
                    if rows_to_insert:
                        insert_rows(cursor, rows_to_insert, table_name)
                conn.commit()
            results.append((idx, True, str(len(rows_to_insert))))
        except Exception as exc:
            LOGGER.exception("DB%s sync failed", idx)
            results.append((idx, False, str(exc)))

    ok_count = sum(1 for _, ok, _ in results if ok)
    if ok_count == 0:
        LOGGER.error("Sync failed: could not write to either DB.")
        return 1

    inserted_db1 = int(results[0][2]) if results and results[0][1] else 0
    inserted_db2 = int(results[1][2]) if len(results) > 1 and results[1][1] else 0
    total_rows_added = inserted_db1 + inserted_db2

    LOGGER.info(
        f"Sync complete: parsed={len(rows)} rows_added_total={total_rows_added} inserted_db1={inserted_db1} "
        f"inserted_db2={inserted_db2} sync_run_id={sync_run_id} "
        f"xml={xml_path} source_commit={source_commit or 'n/a'} "
        f"db1={'ok' if results[0][1] else 'fail'} db2={'ok' if len(results)>1 and results[1][1] else 'fail'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
