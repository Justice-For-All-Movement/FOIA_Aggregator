#!/bin/sh
set -eu

DB_PATH="${FOIA_DB_PATH:-/data/foia_archive.db}"
FILES_DIR="${FOIA_FILES_DIR:-/data/files}"
DB_DIR="$(dirname "$DB_PATH")"

mkdir -p "$DB_DIR" "$FILES_DIR"
chown -R foia:foia "$DB_DIR" "$FILES_DIR"

is_true() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

# The bootstrap outcome is passed to the app, which reports it on /healthz and
# shows an "archive not connected" notice instead of crash-looping.
FOIA_ARCHIVE_BOOTSTRAP_RESULT=disabled
if is_true "${FOIA_BOOTSTRAP_DB_FROM_B2:-false}"; then
  if [ -s "$DB_PATH" ]; then
    FOIA_ARCHIVE_BOOTSTRAP_RESULT=existing
  elif [ -z "${FOIA_S3_ACCESS_KEY_ID:-${B2_KEY_ID:-}}" ] || [ -z "${FOIA_S3_SECRET_ACCESS_KEY:-${B2_APPLICATION_KEY:-}}" ]; then
    echo "Object storage credentials are not configured; starting with the archive not connected."
    FOIA_ARCHIVE_BOOTSTRAP_RESULT=skipped_no_credentials
  else
    echo "No local archive database found; restoring newest verified B2 snapshot."
    if gosu foia python /app/main.py bootstrap-db --destination "$DB_PATH"; then
      if [ -s "$DB_PATH" ]; then
        FOIA_ARCHIVE_BOOTSTRAP_RESULT=restored
      else
        FOIA_ARCHIVE_BOOTSTRAP_RESULT=no_backup
      fi
    else
      echo "Verified B2 snapshot restore failed; starting with the archive not connected."
      FOIA_ARCHIVE_BOOTSTRAP_RESULT=failed
    fi
  fi
fi

# Zero-storage mode: without a restored database, install the newest public
# catalog snapshot (metadata + extracted text) that the scheduled GitHub
# Actions crawl publishes as a release asset of this repository. Set
# FOIA_GITHUB_SNAPSHOT_REPO to an empty string to disable.
SNAPSHOT_REPO="${FOIA_GITHUB_SNAPSHOT_REPO-Justice-For-All-Movement/FOIA_Aggregator}"
if [ ! -s "$DB_PATH" ] && [ -n "$SNAPSHOT_REPO" ]; then
  echo "No archive database; trying the latest published catalog snapshot from $SNAPSHOT_REPO."
  if gosu foia python /app/main.py bootstrap-github-snapshot --repo "$SNAPSHOT_REPO" --destination "$DB_PATH"; then
    if [ -s "$DB_PATH" ]; then
      FOIA_ARCHIVE_BOOTSTRAP_RESULT=github_snapshot
    fi
  else
    echo "Catalog snapshot not available; continuing without it."
  fi
fi

if is_true "${FOIA_BOOTSTRAP_DB_FROM_B2:-false}"; then
  if is_true "${FOIA_REQUIRE_ARCHIVE:-false}" && [ ! -s "$DB_PATH" ]; then
    echo "FOIA_REQUIRE_ARCHIVE is set and no archive snapshot is available; exiting." >&2
    exit 1
  fi
fi
export FOIA_ARCHIVE_BOOTSTRAP_RESULT

exec gosu foia "$@"
