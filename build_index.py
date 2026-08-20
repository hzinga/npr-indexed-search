"""
build_index.py - build a SQLite FTS5 index from the NPR corpus.

Inputs:
    data/npr_utterances.csv — one row per utterance
    data/npr_episodes.csv      — one row per conversation (program/date/title)

Output:
    data/npr_index.db          — episodes + utterances + FTS5 index

Code written with help of Claude.
"""

import json
import os
import sqlite3
import sys
import time

import pandas as pd
import numpy as np

# resolve paths relative to this file, so the script works from any cwd
BASE = os.path.dirname(os.path.abspath(__file__))

SOURCE_FILE = os.path.join(BASE, "data", "npr_utterances.csv")
EPISODE_FILE = os.path.join(BASE, "data", "data/npr_episodes.csv")
DB_PATH = os.path.join(BASE, "data", "npr_index.db")

COLUMNS = {
    "utterance_id": "utterance_id",
    "episode_id": "conversation_id",
    "turn_order": "turn_order",
    "speaker": "speaker",
    "speaker_id": "speaker_id",
    "speaker_type": "speaker_type",
    "text": "text",
}

REQUIRED = {"episode_id", "text"}

#---------------------------------------------------------------------------------------------
# Helper functions
#---------------------------------------------------------------------------------------------

"""
check_fts5 tries to create an FTS5 table. Works as an early check for if this Python's
sqlite3 supports FTS5. Especially important for Windows server.
"""
def check_fts5():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError as e:
        sys.exit(
            "FTS5 unavailable.\n"
            f"  sqlite {sqlite3.sqlite_version}: {e}\n"
        )
    finally:
        con.close()
    print("FTS5 available")

"""
read_any picks the right pandas reader based on the file type.
"""
def read_any(path):
    if not os.path.exists(path):
        sys.exit(f"Not found: {path}")
    if path.endswith(".csv"):
        return pd.read_csv(path)
    if path.endswith(".jsonl"):
        return pd.read_json(path, lines=True)
    if path.endswith(".json"):
        return pd.read_json(path)
    sys.exit(f"Unsupported file type: {path}")


"""
rows_of turns pandas NA/NaN/NaT into a real None, and converts numpy scalar types to
plain Python ones. Since NumPy 2, numpy integers support the buffer protocol, so sqlite3
stores them as BLOBs instead of INTEGERs -- silently, because SQLite columns have type
affinity rather than enforced types.
"""
def rows_of(df, cols):
    def clean(v):
        if v is None or pd.isna(v):
            return None
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
        if isinstance(v, np.bool_):
            return bool(v)
        return v

    out = []
    for r in df[cols].itertuples(index=False, name=None):
        out.append(tuple(clean(v) for v in r))
    return out

#---------------------------------------------------------------------------------------------
# Load
#---------------------------------------------------------------------------------------------

"""
load_episodes read the episodes CSV, fills in any absent columns, forces year/episode_id to
an integer/string, and exits if any episode_id is duplicated.
"""
def load_episodes():
    eps = read_any(EPISODE_FILE)
    print(f"[ok] {len(eps):,} episodes from {os.path.basename(EPISODE_FILE)}")
    for needed in ("episode_id", "program", "year", "date", "title"):
        if needed not in eps.columns:
            eps[needed] = None
    if "n_utterances" not in eps.columns:
        eps["n_utterances"] = None

    # Int64 (nullable) rather than int64, which cannot hold missing values
    eps["year"] = pd.to_numeric(eps["year"], errors="coerce").astype("Int64")
    eps["episode_id"] = eps["episode_id"].astype(str)

    # CHECK: implement check for duplicates to prevent errors with SQL joining
    dupes = int(eps["episode_id"].duplicated().sum())
    if dupes:
        sys.exit(f"Duplicate episodes: {dupes}")
    return eps

"""
load_utterances maps column names, distinguishes required fields from optional, drops
rows with empty text, and computes word_count.
"""
def load_utterances():
    df = read_any(SOURCE_FILE)
    print(f"[ok] {len(df):,} utterances from {os.path.basename(SOURCE_FILE)}")
    print(f"columns: {list(df.columns)}")

    out = pd.DataFrame(index=df.index)
    for canonical, source in COLUMNS.items():
        if source and source in df.columns:
            out[canonical] = df[source]
        elif canonical in REQUIRED:
            sys.exit(
                f"Required column '{source}' (-> {canonical}) not found.\n"
                f"Available: {list(df.columns)}\nEdit COLUMNS at the top."
            )
        else:
            out[canonical] = None
            print(f"[warn] no '{source}' column -> {canonical} will be NULL")

    # cast the join key on both sides, so "312460" never fails to match 312460
    out["episode_id"] = out["episode_id"].astype(str)
    out["turn_order"] = pd.to_numeric(out["turn_order"], errors="coerce").astype("Int64")
    out["text"] = out["text"].fillna("").astype(str).str.strip()

    # CHECK: dropping empty rows keeps meaningless entires out of index
    dropped = int((out["text"] == "").sum())
    out = out[out["text"] != ""].copy()

    if dropped:
        print(f"[note] dropped {dropped:,} rows with empty text")

    # calculate word count for later one-word turns
    out["word_count"] = out["text"].str.split().str.len()
    return out

#---------------------------------------------------------------------------------------------
# Schema
#---------------------------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE episodes (
    episode_id      TEXT PRIMARY KEY,
    program         TEXT,
    year            INTEGER,
    date            TEXT,           -- ISO 'YYYY-MM-DD'; sorts chronologically
    title           TEXT,
    n_utterances    INTEGER
);

CREATE TABLE utterances (
    id              INTEGER PRIMARY KEY,    -- FTS5 content_rowid
    utterance_id    TEXT,
    episode_id      TEXT REFERENCES episodes(episode_id),
    turn_order      INTEGER,                -- position in the interview
    speaker         TEXT,
    speaker_id      TEXT,
    speaker_type    TEXT,
    word_count      INTEGER,
    text            TEXT NOT NULL
);

-- External content: the index stores word->row only, and reads the text
-- from 'utterances'. Porter stemmer deliberately left off -- stemming would
-- make "run" match "running", contradicting the exact whole-word behaviour
-- the Explorer documents.

CREATE VIRTUAL TABLE utterances_fts USING fts5(
    text,
    content         = 'utterances',
    content_rowid   = 'id',
    tokenize        = "unicode61 remove_diacritics 2"
);

CREATE INDEX idx_ep_program ON episodes(program);
CREATE INDEX idx_ep_year ON episodes(year);
CREATE INDEX idx_ep_date ON episodes(date);
-- covers episode_id lookups too, since it is the leftmost column
CREATE INDEX idx_utt_ep_order ON utterances(episode_id, turn_order);
CREATE INDEX idx_utt_sptype ON utterances(speaker_type);
"""

#---------------------------------------------------------------------------------------------
# Build
#---------------------------------------------------------------------------------------------

"""
build creates the schema in a temp file, inserts episodes and utterances, and builds
the FTS index in one pass.
"""
def build(eps, df, db_path):
    # STEP 1: name a temp file. clear remnants of failed run if applicable.
    tmp = db_path + ".tmp"
    for leftover in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    print("[ok] schema created")

    # STEP 2: insert episodes (first, because utterances reference them)
    ep_cols = ["episode_id", "program", "year", "date", "title", "n_utterances"]
    with con:
        con.executemany(
            "INSERT INTO episodes (episode_id, program, year, date, title, n_utterances) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows_of(eps, ep_cols),
        )
    print(f"[ok] inserted {len(eps):,} episodes")

    # STEP 3: insert utterances
    ut_cols = ["utterance_id", "episode_id", "turn_order", "speaker",
               "speaker_id", "speaker_type", "word_count", "text"]
    t0 = time.time()
    with con:
        con.executemany(
            "INSERT INTO utterances (utterance_id, episode_id, turn_order, speaker, "
            "speaker_id, speaker_type, word_count, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows_of(df, ut_cols),
        )
    print(f"[ok] inserted {len(df):,} utterances in {time.time()-t0:.1f}s")

    # STEP 4: populate the FTS index. It starts empty and stays empty without
    # 'rebuild' -- the build would succeed and every search return nothing.
    t0 = time.time()
    with con:
        con.execute("INSERT INTO utterances_fts(utterances_fts) VALUES('rebuild')")
        con.execute("INSERT INTO utterances_fts(utterances_fts) VALUES('optimize')")
    print(f"[ok] FTS index built in {time.time()-t0:.1f}s")

    con.execute("ANALYZE")
    con.execute("PRAGMA journal_mode = DELETE")     # ship one portable file
    con.close()

    # atomic swap: nothing ever sees a half-built index
    os.replace(tmp, db_path)
    print(f"[ok] wrote {db_path} ({os.path.getsize(db_path)/1e6:.1f} MB)")


#---------------------------------------------------------------------------------------------
# Verify
#---------------------------------------------------------------------------------------------

def report(db_path):
    con = sqlite3.connect(db_path)
    one = lambda sql: con.execute(sql).fetchone()[0]

    n_utt = one("SELECT COUNT(*) FROM utterances")
    stats = {
        "episodes":         one("SELECT COUNT(*) FROM episodes"),
        "utterances":       n_utt,
        "speakers":         one("SELECT COUNT(DISTINCT speaker_id) FROM utterances"),
        "total_words":      one("SELECT SUM(word_count) FROM utterances"),
        "date_range":       con.execute(
            "SELECT MIN(date), MAX(date) FROM episodes WHERE date IS NOT NULL"
        ).fetchone(),
        "db_size_mb":       round(os.path.getsize(db_path) / 1e6, 1),
    }
    print("\n--- index summary ---")
    print(json.dumps(stats, indent=2, default=str))

    print("\ntop programs:")
    for prog, n in con.execute(
        "SELECT e.program, COUNT(*) FROM utterances u "
        "JOIN episodes e ON e.episode_id = u.episode_id "
        "GROUP BY e.program ORDER BY 2 DESC LIMIT 10"
    ):
        print(f"{prog}: {n:,} utterances")

    print("\nspeaker_type:")
    for st, n in con.execute(
        "SELECT speaker_type, COUNT(*) FROM utterances "
        "GROUP BY speaker_type ORDER BY 2 DESC"
    ):
        print(f"{st}: {n:,}")

    print("\n--- integrity ---")

    # CHECK: utterances whose episode_id has no matching episode. Must be 0,
    # otherwise every filtered search silently drops rows.
    orphans = one("""
        SELECT COUNT(*) FROM utterances u
        LEFT JOIN episodes e ON e.episode_id = u.episode_id
        WHERE e.episode_id IS NULL
    """)
    print(f"orphan utterances (no matching episode): {orphans:,} "
          f"({100*orphans/n_utt:.2f}%)")
    if orphans:
        print("  ^ the two extractions disagree on the id. Fix before "
              "trusting any filtered search.")

    # CHECK: the reverse direction -- episodes the utterance export missed
    empty_eps = one("""
        SELECT COUNT(*) FROM episodes e
        LEFT JOIN utterances u ON u.episode_id = e.episode_id
        WHERE u.episode_id IS NULL
    """)
    print(f"episodes with zero utterances: {empty_eps:,}")

    # CHECK: compare against ConvoKit's own per-episode counts, to catch a
    # partial export -- which otherwise looks completely normal
    expected = one("SELECT SUM(n_utterances) FROM episodes")
    if expected:
        diff = n_utt - expected
        print(f"utterances found vs. ConvoKit's per-episode counts: "
              f"{n_utt:,} vs {expected:,} ({diff:+,})")
        if abs(diff) > 0.01 * expected:
            print(" ^ >1% off: your utterances CSV is a partial export. "
                  "Check before building anything on it.")

    # CHECK: does the index actually return anything? An unpopulated FTS
    # table fails silently rather than erroring.
    hits = one("SELECT COUNT(*) FROM utterances_fts WHERE utterances_fts MATCH '\"the\"'")
    print(f"smoke test - utterances containing \"the\": {hits:,}")
    if hits == 0:
        print("  ^ index is empty. The 'rebuild' step did not run.")

        # CHECK: SQLite affinity does not enforce types -- numpy scalars land as
        # BLOBs without error. Verify what actually got stored.
        print("\ncolumn types:")
        for tbl, col in (("utterances", "turn_order"), ("utterances", "word_count"),
                         ("episodes", "year"), ("episodes", "n_utterances")):
            types = [r[0] for r in con.execute(f"SELECT DISTINCT typeof({col}) FROM {tbl}")]
            print(f"  {tbl}.{col}: {types}")
            if any(t not in ("integer", "null") for t in types):
                print("    ^ not stored as INTEGER — numeric comparisons will fail.")

    con.close()

if __name__ == "__main__":
    check_fts5()
    build(load_episodes(), load_utterances(), DB_PATH)
    report(DB_PATH)