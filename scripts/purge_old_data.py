#!/usr/bin/env python3
"""Minimal data-retention tool: permanently deletes conversations (and
their message history) that haven't been touched in N days.

This is the *mechanism*, not the *policy* — how long to keep customer
conversation data, whether "inactive" is the right criterion, and whether
deletion requests need a faster path than a scheduled purge are business
and legal decisions Alpha Fitness needs to make (Kenya's Data Protection
Act 2019 is the relevant law for a Kenyan business; GDPR applies too for
any EU/UK customers). This script just carries out whatever number you
give it.

Usage:
    python scripts/purge_old_data.py --days 180            # delete, after confirming
    python scripts/purge_old_data.py --days 180 --dry-run  # see what WOULD be deleted
    python scripts/purge_old_data.py --days 180 --yes      # skip the confirmation prompt (cron/CI use)
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.storage import db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, required=True, help="Delete conversations inactive longer than this many days.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without deleting it.")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    args = parser.parse_args()

    cutoff = time.time() - (args.days * 86400)
    db.init_db(settings.database_path)

    if args.dry_run:
        with db._connect(settings.database_path) as conn:
            rows = conn.execute(
                "SELECT phone_number, updated_at FROM conversations WHERE updated_at < ?", (cutoff,)
            ).fetchall()
        print(f"Would delete {len(rows)} conversation(s) inactive since before {args.days} days ago:")
        for r in rows:
            age_days = (time.time() - r["updated_at"]) / 86400
            print(f"  {r['phone_number']}  (inactive {age_days:.0f} days)")
        return

    if not args.yes:
        answer = input(f"This will PERMANENTLY delete all conversations inactive for {args.days}+ days. Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return

    result = db.purge_older_than(settings.database_path, cutoff)
    print(f"Deleted {result['deleted_conversations']} conversation(s).")


if __name__ == "__main__":
    main()
