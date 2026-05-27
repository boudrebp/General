# Coded Translation Sync

This folder contains a Jenkins + Python flow to monitor a source `CodedTranslationConfig.xml` file in GitHub and upsert values into Oracle.

## What it does

1. Jenkins polls the repository every 15 minutes.
2. Jenkins checks out the repository from GitHub and resolves the configured XML path from that checkout.
3. Jenkins runs the Python sync script with commit range inputs.
4. The script performs a Git diff gate for the XML file and skips DB work if no XML changes are detected.
5. When changed, the script parses XML `CodedTranslation` + `CodedTranslationEntry` nodes.
6. The script compares parsed rows to the latest DB snapshot and inserts only new/changed rows.
7. New rows are inserted with `DATEADDED` for historical tracking.

## Jenkins Credentials Required

Create these credentials in Jenkins:

- `git_token` (for repository checkout)
- `oracle-db-user-pass` (Username with password)
- `oracle-db-dsn` (Secret text) with value like `host:1521/service_name`

Optional secondary DB credentials (when `ENABLE_SECONDARY_DB=true`):

- `oracle-db2-user-pass` (Username with password)
- `oracle-db2-dsn` (Secret text) with value like `host:1521/service_name`

## Jenkins Parameters

The included `Jenkinsfile` supports these parameters:

- `REPO_URL` (default `https://github.gnscet.com/sce-grid/GridMS-adms-dev`)
- `REPO_BRANCH` (default `master`)
- `TARGET_FILE` (default `sce/config/shared/CodedTranslationConfig.xml`)
- `ENABLE_SECONDARY_DB` (default `false`)

`TARGET_FILE` is the XML file path inside the checked out GitHub repository at runtime.

The `Jenkinsfile` also sets this environment variable:

- `ORACLE_DB_TABLE` (default `ADMS_CODED_TRANSLATION`)


## DB Table

Use one of these options:

- Run [sql/coded_translation_entries.sql](sql/coded_translation_entries.sql) before using the sync script

**Table name:** `ADMS_CODED_TRANSLATION`

Key columns:

- `NAME`
- `KEY` (stored as text; supports values like `NOP`)
- `DATEADDED`

## Local Test

For local testing, you can point the script at the sample XML included in this folder:

```bash
python3 -m pip install -r coded_translation_sync/requirements.txt
export ORACLE_DB_USER='your_user'
export ORACLE_DB_PASSWORD='your_password'
export ORACLE_DB_DSN='host:1521/service_name'

python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file coded_translation_sync/CodedTranslationConfigSample.xml \
  --table-name ADMS_CODED_TRANSLATION
```

You can target a schema-qualified table when needed:

```bash
python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file coded_translation_sync/CodedTranslationConfigSample.xml \
  --table-name GMSSI_USER2.ADMS_CODED_TRANSLATION
```

## Dry Run (No Oracle Connection)

Use dry-run mode to print SQL without connecting to Oracle. The sample XML is suitable for this local check:

```bash
python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file coded_translation_sync/CodedTranslationConfigSample.xml \
  --dry-run-sql
```

## Git Diff Gate (Optional)

To skip DB sync when the XML file did not change between commits, provide commit bounds:

```bash
python3 coded_translation_sync/scripts/sync_coded_translation.py \
  --xml-file sce/config/shared/CodedTranslationConfig.xml \
  --since-commit "$GIT_PREVIOUS_SUCCESSFUL_COMMIT" \
  --current-commit "$GIT_COMMIT" \
  --table-name ADMS_CODED_TRANSLATION \
  --repo-dir .
```

Behavior:

- If XML is unchanged in that commit range, script exits successfully and skips DB work.
- If XML changed, script continues with parse + DB delta insert logic.

## Jenkins Sync Execution

The `Jenkinsfile` executes an inline Python script (via `python3 - <<'PY'`) with git gating and Oracle sync logic.

This avoids needing to create or copy `coded_translation_sync/scripts/sync_coded_translation.py` into the Jenkins workspace at runtime.

During Jenkins execution, `TARGET_FILE` is read from the checked-out GitHub repository content, not from the local sample XML in this folder.

## Notes

- Data model is append-only: no updates and no deletes.
- Only changed/new rows are inserted on each successful sync run.
- `DATEADDED` stores insert timestamp for each historical record.
- Query latest value per key using max `DATEADDED`.
