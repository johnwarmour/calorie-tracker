"""
Migrates existing daily_log rows to the new user-scoped schema.
- Adds user_id, id columns if missing
- Assigns all existing rows to the specified user
- Fixes primary key and constraints
"""
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

TARGET_USER = "jwarmour"

conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

try:
    # 1. Get the user id
    cur.execute("SELECT id FROM users WHERE name = %s", (TARGET_USER,))
    row = cur.fetchone()
    if not row:
        print(f"User '{TARGET_USER}' not found. Create them via the app first.")
        exit(1)
    user_id = row["id"]
    print(f"Found user '{TARGET_USER}' with id={user_id}")

    # 2. Add id column (SERIAL primary key replacement) if missing
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'daily_log' AND column_name = 'id'
    """)
    if not cur.fetchone():
        print("Adding id column...")
        cur.execute("ALTER TABLE daily_log ADD COLUMN id SERIAL")

    # 3. Add user_id column if missing
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'daily_log' AND column_name = 'user_id'
    """)
    if not cur.fetchone():
        print("Adding user_id column...")
        cur.execute("ALTER TABLE daily_log ADD COLUMN user_id INTEGER")

    # 4. Assign all un-owned rows to the target user
    cur.execute("UPDATE daily_log SET user_id = %s WHERE user_id IS NULL", (user_id,))
    print(f"Assigned {cur.rowcount} row(s) to '{TARGET_USER}'")

    # 5. Fix primary key: drop old (date) PK, add PK on id
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'daily_log' AND constraint_type = 'PRIMARY KEY'
    """)
    pk = cur.fetchone()
    if pk:
        pk_name = pk["constraint_name"]
        # If PK is still on date (old schema), replace it
        cur.execute("""
            SELECT column_name FROM information_schema.key_column_usage
            WHERE constraint_name = %s AND table_name = 'daily_log'
        """, (pk_name,))
        pk_cols = [r["column_name"] for r in cur.fetchall()]
        if pk_cols == ["date"]:
            print("Replacing old primary key (date) with id...")
            cur.execute(f"ALTER TABLE daily_log DROP CONSTRAINT {pk_name}")
            cur.execute("ALTER TABLE daily_log ADD PRIMARY KEY (id)")
    else:
        print("Adding primary key on id...")
        cur.execute("ALTER TABLE daily_log ADD PRIMARY KEY (id)")

    # 6. Add UNIQUE (date, user_id) if missing
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'daily_log' AND constraint_type = 'UNIQUE'
          AND constraint_name = 'daily_log_date_user_id_key'
    """)
    if not cur.fetchone():
        print("Adding UNIQUE (date, user_id) constraint...")
        cur.execute("ALTER TABLE daily_log ADD CONSTRAINT daily_log_date_user_id_key UNIQUE (date, user_id)")

    # 7. Add foreign key on user_id if missing
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'daily_log' AND constraint_type = 'FOREIGN KEY'
          AND constraint_name = 'daily_log_user_id_fkey'
    """)
    if not cur.fetchone():
        print("Adding foreign key on user_id...")
        cur.execute("""
            ALTER TABLE daily_log ADD CONSTRAINT daily_log_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        """)

    conn.commit()
    print("Migration complete.")
except Exception as e:
    conn.rollback()
    print(f"Migration failed: {e}")
    raise
finally:
    cur.close()
    conn.close()
