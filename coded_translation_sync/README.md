# Coded Translation Sync

This folder contains a Jenkins + Python flow to monitor `CodedTranslationConfigSample.xml` in GitHub and upsert values into Oracle.

## What it does

1. Jenkins polls the repository every 15 minutes.
2. Jenkins runs the Python sync script with commit range inputs.
3. The script performs a Git diff gate for the XML file and skips DB work if no XML changes are detected.
4. When changed, the script parses XML `CodedTranslation` + `CodedTranslationEntry` nodes.
5. The script compares parsed rows to the latest DB snapshot and inserts only new/changed rows.
6. Every inserted row is tagged with `sync_run_id` (and optional `source_commit`) for traceability.

## Jenkins Credentials Required

Create these credentials in Jenkins:

- `git_token` (for repository checkout)
- `gmssi_user2` (Global username with password for DB access)
- `oracle-db-dsn` (Secret text) with value `ALHXVS1ETOR01:1521/eta`

Optional secondary DB credentials (when `ENABLE_SECONDARY_DB=true`):

- `gmssi_user2` (same global username with password credential)
- `oracle-db2-dsn` (Secret text) with value like `host:1521/service_name`

## Jenkins Parameters

The included `Jenkinsfile` supports these parameters:

- `REPO_URL` (default `https://github.com/boudrebp/General.git`)
- `REPO_BRANCH` (default `main`)
- `TARGET_FILE` (default `coded_translation_sync/CodedTranslationConfigSample.xml`)
- `ENABLE_SECONDARY_DB` (default `false`)


## DB Table

Use one of these options:

- Let script auto-create table using `--create-table-if-missing`
- Or run [sql/coded_translation_entries.sql](sql/coded_translation_entries.sql) manually first

**Table name:** `ADMS_CODED_TRANSLATION_ENTRIES`

Key columns:

- `translation_name`
- `entry_key` (nullable numeric version of key)
- `entry_key_text` (required original key text, supports values like `NOP`)
- `added_on`

## Local Test

```bash
python3 -m pip install -r coded_translation_sync/requirements.txt
export ORACLE_DB_USER='your_user'
export ORACLE_DB_PASSWORD='your_password'
export ORACLE_DB_DSN='ALHXVS1ETOR01:1521/eta'

python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file coded_translation_sync/CodedTranslationConfigSample.xml \
  --create-table-if-missing
```

## Dry Run (No Oracle Connection)

Use dry-run mode to print SQL without connecting to Oracle:

```bash
python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file coded_translation_sync/CodedTranslationConfigSample.xml \
  --dry-run-sql \
  --create-table-if-missing
```

## Git Diff Gate (Optional)

To skip DB sync when the XML file did not change between commits, provide commit bounds:

```bash
python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file coded_translation_sync/CodedTranslationConfigSample.xml \
  --since-commit "$GIT_PREVIOUS_SUCCESSFUL_COMMIT" \
  --current-commit "$GIT_COMMIT" \
  --repo-dir .
```

Behavior:

- If XML is unchanged in that commit range, script exits successfully and skips DB work.
- If XML changed, script continues with parse + DB delta insert logic.

## Jenkins Sync Command

The `Jenkinsfile` executes the script with git gate + source commit:

```bash
python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file "$TARGET_FILE" \
  --create-table-if-missing \
  --since-commit "${GIT_PREVIOUS_SUCCESSFUL_COMMIT:-}" \
  --current-commit "${GIT_COMMIT:-}" \
  --repo-dir . \
  --source-commit "${GIT_COMMIT:-}"
```

## Notes

- Data model is append-only: no updates and no deletes.
- Only changed/new rows are inserted on each successful sync run.
- `added_on` stores insert timestamp for each historical record.
- Query latest value per key using max `added_on` (and `record_id` for tie-break where needed).
- `sync_run_id` groups inserted rows in a single sync run.
