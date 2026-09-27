"""CLI entrypoint for rolling back a single migration.

Disaster recovery only -- see rollback_migration()'s own docstring in
ehr/db/migrations.py for why this is meant to be paired with rolling the
*application code* back too (e.g. via git), not used standalone against a
running app on today's code, which still expects whatever the rollback just
removed.

Usage: python -m ehr.db.rollback <migration_id>
"""
import sys

from ehr.db.migrations import rollback_migration
from ehr.models.database import engine


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m ehr.db.rollback <migration_id>")
        sys.exit(1)
    migration_id = sys.argv[1]
    try:
        rollback_migration(engine, migration_id)
    except ValueError as e:
        print(f"Rollback failed: {e}")
        sys.exit(1)
    print(f"Rolled back {migration_id}.")


if __name__ == "__main__":
    main()
