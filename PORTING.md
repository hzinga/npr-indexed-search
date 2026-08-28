# Porting this tool to the American Voices Project corpus

The NPR Transcript Search is a prototype for an eventual AVP Word Search tool on
the Data Discovery Dashboard. This document maps what changes need to be made to this repo
in order to adapt it for the AVP.

## The AVP data

The NPR data is separated into individual utterances, with multiple utterances falling under one single
turn. Turn_order corresponds to the utterance number within a turn, and episode_order corresponds to the
turn number within the episode.

For example, the sentence "Hello. Good morning." would be broken down into two separate utterances.
"Hello." would have a turn_order value of 0 and an episode_order value of 1. "Good morning." would have a
turn_order value of 1 and an episode_order value of 1.

AVP turn IDs look like `337329_15_7_interviewer`. The four pieces correspond to the interview (`337329`),
the turn number (`15`), the pair number (`7`), and the speaker's role (`interviewer`). The text associated
with a turn consists of every utterance that belongs to that turn - it is not broken up into individual
utterances.

Any two turns from the same interview that share a pair number are the two halves of
one prompt and response, so `337329_15_7_interviewer` and `337329_16_7_interviewee`
are a single exchange.

## build_index.py

### Paths

[Lines 26–28](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L26-L28)
name the two input files and the database. The AVP spreadsheets live outside the
project folder, so the input directory comes from an environment variable:

```python
DATA_DIR     = os.environ.get("AVP_DATA_DIR", os.path.join(BASE, "data"))
SOURCE_FILE  = os.path.join(DATA_DIR, "speaker_turns.xlsx")
EPISODE_FILE = os.path.join(DATA_DIR, "simplified_data.xlsx")
DB_PATH      = os.path.join(BASE, "data", "avp_index.db")
```

`app.py` also declares `DB_PATH`
([line 37](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L37)),
so put all four in one module that both scripts import.

### The COLUMNS mapping

[Lines 30–40](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L30-L40)
map canonical names to spreadsheet columns. Only the values change:

```python
COLUMNS = {
    "utterance_id": "turn_id",
    "episode_id":   "interview",
    "turn_order":   "turn_number",
    "speaker":      "speaker",
    "speaker_id":   "speaker_num",
    "speaker_type": "speaker_role",
    "text":         "text",
}
```

Renaming the keys as well — `utterance` to `turn` throughout — touches the schema, all
four INSERT statements, every query in `app.py`, and the UI, so it's better done as its
own commit than folded into the port.

### Reading Excel

`read_any` ([lines 66–75](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L66-L75))
handles CSV, JSON, and JSONL. Add an Excel branch:

```python
if path.endswith((".xlsx", ".xlsm")):
    return pd.read_excel(path, dtype={"interview": str, "hhid": str})
```

The `dtype` keeps the join keys as text. Pandas guesses types per column, so the same
identifier can arrive as a string from one spreadsheet and a number from the other, at
which point the join matches nothing and raises no error. Adds `openpyxl`.

### Pulling out the pair number

`load_utterances`
([lines 132–164](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L132-L164))
should parse the pair number out of `turn_id` once, at build time:

```python
parts = out["utterance_id"].str.split("_")
out["pair_number"] = pd.to_numeric(parts.str[2], errors="coerce").astype("Int64")
```

Then check for nulls — a null means some `turn_id` didn't have four parts. If the
separate `pair_id` column already holds this number on its own, use it and skip the
parsing.

### load_episodes

[Lines 109–126](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L109-L126)
expect NPR's `episode_id`, `program`, `year`, `date`, and `title`. Replace these with
whichever `simplified_data` columns the app needs. `hhid` joins to `turns.interview`.

### The schema

[Lines 170–213](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L170-L213):
`episodes` becomes `metadata`, `utterances` becomes `turns`, `utterances_fts` becomes
`turns_fts`. Add `pair_number` to `turns` and index it:

```sql
CREATE INDEX idx_turns_pair ON turns(interview, pair_number);
```

Keep external-content FTS5. Turn the Porter stemmer on, in the `turns_fts` definition
([lines 200–205](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L200-L205)):

```sql
tokenize = "porter unicode61 remove_diacritics 2"
```

This allows turns with the search term as a stem to appear in search results, so
searching `worry` also returns `worried` and `worries`. This feature is not available in
the NPR word search. Porter strips suffixes rather than knowing English, so irregular
forms don't fold together — `run` matches `running` and `runs` but not `ran`.

Stemming also changes how matches are highlighted; see Highlighting below.

The remaining secondary indexes are built around NPR's filters. Replace them with one
index per AVP filter column.

### build

[Lines 223–272](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L223-L272):
update the column lists to match the schema and add `pair_number` to the turns insert.

Keep the `rebuild` call — without it the index stays empty, the build still reports
success, and every search returns nothing. Keep the atomic file swap.

### report

[Lines 279–363](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L279-L363)
run integrity checks after each build. Four to confirm on the first real run:

- **FTS5 exists.** `check_fts5` ([lines 50–61](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/build_index.py#L50-L61)) fails at startup rather than midway through a build. Run it before anything else on the server.
- **Zero orphan turns.** Turns whose interview id has no metadata row. Nonzero means those interviews vanish under any filter while still appearing in unfiltered searches.
- **`turn_number` is gapless.** Per interview, `MAX - MIN + 1` should equal the row count.
- **`pair_number` is complete.** Every turn has one, and no pair has more than two turns.

## app.py

### Filter options

`load_filter_options`
([lines 49–70](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L49-L70))
runs one `SELECT DISTINCT` per sidebar filter. The speaker role query ports directly.
The program and year queries get replaced per Adding a metadata filter below.

### Loading a full interview

`get_episode`
([lines 73–99](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L73-L99))
fetches an interview's metadata and all of its turns. Rename, and add `pair_number` to
the select list.

### Role styling

CSS at [lines 113–137](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L113-L137)
and `role_class` at
[lines 201–206](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L201-L206).
`speaker_role` has exactly two values, so this is a straight rename: `.role-host` to
`.role-interviewer`, `.role-guest` to `.role-interviewee`, and the string comparisons
follow.

### The exchange view: delete both functions

`get_turns` and `get_single_exchange`
([lines 219–252](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L219-L252))
infer pairing that the NPR corpus doesn't record. AVP records it, so both reduce to one
query:

```sql
SELECT speaker_role, text
FROM turns
WHERE interview = ? AND pair_number = ?
ORDER BY turn_number
```

### The position indicator

The `{id: index}` map at
[line 431](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L431)
exists because NPR's `turn_order` has gaps. AVP's `turn_number` is consecutive, so the
position becomes `turn_number / n_turns` — store `n_turns` per interview in `metadata`.

### run_search

[Lines 266–302](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L266-L302):
table and column names, plus one `if` block per filter. Keep the division of labour —
FTS5 finds the rows and SQL applies the filters.

### Highlighting

With stemming on, `build_pattern` and `highlight`
([lines 181–198](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L181-L198))
break. They build a `\bquery\b` regex, so a turn matched on `worried` for the query
`worry` comes back with nothing highlighted — the result looks like a false positive.

Use FTS5's own `highlight()` in `run_search` instead, which marks the tokens the index
actually matched:

```sql
SELECT highlight(turns_fts, 0, '<mark>', '</mark>') AS text
FROM turns_fts ...
```

Both regex functions can then go, along with the `pattern` value threaded through
`run_search` and the result loop
([lines 372](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L372)
and [463](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L463)).

Also update the help text
([lines 142–157](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L142-L157)),
which currently promises whole-word matching.

## Adding a metadata filter

NPR has three filters — program, speaker type, year. Each one touches six places, and
the pattern is the same for any `simplified_data` column, so adding or removing filters
later is mechanical rather than structural.

In `build_index.py`:

1. `load_episodes` — carry the column through from the spreadsheet
2. `SCHEMA` — add it to the `metadata` table, plus `CREATE INDEX` on it
3. `build` — add it to the metadata column list and INSERT

In `app.py`:

4. `load_filter_options` — a `SELECT DISTINCT` to populate the dropdown
5. the sidebar ([lines 306–313](https://github.com/hzinga/npr-indexed-search/blob/c6d7af92215a27437a1bbaf4dbc8c2053abdc670/app.py#L306-L313)) — an `st.multiselect`, and a matching `session_state` key
6. `run_search` — an `if` block adding `AND m.<column> IN (...)` to the SQL

Trace the existing `program` filter through those six points to see the shape before
adding the first new one.

There are important questions to consider when thinking of which variables to include for metadata
filtering. Starting with a handful of variables would make the most sense when initially trying to
adapt, but ideally, researchers would be able to filter on all of the variables in `simplified_data`
that they can filter on in the Data Discovery Dashboard.
