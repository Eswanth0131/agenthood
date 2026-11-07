"""Assign a known legacy studio project to a buyer as the local database operator."""
import argparse

from app import init_db
from storage import connect

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_id')
    parser.add_argument('buyer_email')
    args = parser.parse_args()
    init_db()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        buyer = c.execute("SELECT id FROM users WHERE role='buyer' AND email=?",
                          (args.buyer_email.strip().lower(),)).fetchone()
        if not buyer:
            parser.error('Register the intended buyer account first.')
        if c.execute('''UPDATE music_projects SET owner_id=?
                       WHERE id=? AND owner_id IS NULL''', (buyer['id'], args.project_id)).rowcount != 1:
            parser.error('Project is missing or already owned.')
    print('Assigned legacy studio project to the buyer.')
