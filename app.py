"""
NPR Transcript Explorer. 

Modified app that searches for terms using an index, instead of a brute force scan.

Lets users filter by program, speaker type, and year then search transcripts by
exact word/phrase match. Results show the host/guest exchange around each match
with distinct styling per role and a highlighted match term. Users can scroll within
a transcript to view multiple matches, expand into the single host/guest interaction
associated with a match, page through results, view matched episodes' metadata
(title, date, program), and export full transcript text for further analysis.

Code written with the help of Claude.
"""

import csv
import io
import re
import sqlite3
from pathlib import Path
import streamlit as st
from streamlit_scroll_to_top import scroll_to_here

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "search_applied" not in st.session_state:
    st.session_state.search_applied = False
if "page_number" not in st.session_state:
    st.session_state.page_number = 1
if "scroll_to_top" not in st.session_state:
    st.session_state.scroll_to_top = False
if "query_was_corrected" not in st.session_state:
    st.session_state.query_was_corrected = False

DB_PATH = Path(__file__).parent / "data" / "npr_index.db"


@st.cache_resource
def get_connection():
    """One shared connection per session process; sqlite3 objects aren't
    hashable/picklable, so this must be cache_resource, not cache_data."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data
def load_filter_options():
    """Distinct filter values, pulled once per session and cached (episodes
    table is small enough that this is cheap, and it never changes at runtime)."""
    conn = get_connection()
    programs = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT program FROM episodes WHERE program IS NOT NULL ORDER BY program"
        ).fetchall()
    ]
    years = [
        str(r[0]) for r in conn.execute(
            "SELECT DISTINCT year FROM episodes WHERE year IS NOT NULL ORDER BY year DESC"
        ).fetchall()
    ]
    speaker_types = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT speaker_type FROM utterances "
            "WHERE speaker_type IS NOT NULL ORDER BY speaker_type"
        ).fetchall()
    ]
    return programs, years, speaker_types


@st.cache_data
def get_episode(episode_id):
    """Full episode record + ordered utterance list, keyed by episode_id.
    Needed (not just the matching rows) because turn-grouping, single-exchange
    context, and full-transcript download all require every utterance, not
    just the ones that matched the search."""
    conn = get_connection()
    erow = conn.execute(
        "SELECT episode_id, program, title, date FROM episodes WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    if erow is None:
        return None
    urows = conn.execute(
        "SELECT id, speaker_type, text FROM utterances WHERE episode_id = ? ORDER BY turn_order",
        (episode_id,),
    ).fetchall()
    return {
        "id": erow["episode_id"],
        "program": erow["program"],
        "title": erow["title"],
        "date": erow["date"],
        "utterances": [
            {"id": r["id"], "speaker_type": r["speaker_type"], "text": r["text"]}
            for r in urows
        ],
    }


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------
st.set_page_config(page_title="NPR Transcript Explorer", layout="wide")

# Scroll-to-top must run AFTER set_page_config, since set_page_config has to
# be the first Streamlit command executed in the script.
if st.session_state.scroll_to_top:
    scroll_to_here(0, key="top")  # 0 = instant scroll, no animation delay
    st.session_state.scroll_to_top = False

st.markdown("""
<style>
mark {
    background-color: #fde047;
    color: #1e293b;
    padding: 0 3px;
    border-radius: 3px;
    font-weight: 600;
}
.role-host {
    border-left: 4px solid #D8BFD8;
    background-color: #F5F5F5;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    margin-bottom: 0.4rem;
}
.role-guest {
    border-left: 4px solid #ADD8E6;
    background-color: #F5F5F5;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    margin-bottom: 0.4rem;
}
</style>
""", unsafe_allow_html=True)

st.title("NPR Transcript Explorer")
st.caption("Exact word search across a sample of over 22,000 NPR interview transcripts.")

with st.expander("ℹ️ How to use this tool"):
    st.markdown(
        """
        1. **Set filters** in the sidebar to narrow results.
        2. **Enter a search term** below, then click **Apply**.
        3. If a transcript has several matches, they'll appear in a scrollable box —
           scroll within it to see them all.
        4. Click **View full exchange** on any result to see the host/guest interaction
           associated with that match.
        5. Click **🔍️** on an episode to see its title, air date, and download the full transcript.
        6. Use **Save episodes** to export matching episodes' metadata for further analysis.

        Search is automatically **case-insensitive** and matches **whole words only**, including
        any special characters.
        """
    )

if not DB_PATH.exists():
    st.error(
        f"Couldn't find `{DB_PATH}`. "
        "Run the indexing step first to generate it."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Load data context
# ---------------------------------------------------------------------------
programs, years, speaker_types = load_filter_options()

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
    "if", "in", "into", "is", "it", "no", "not", "of", "on", "or",
    "such", "that", "the", "their", "then", "there", "these",
    "they", "this", "to", "was", "will", "with", "he", "she",
    "you", "i", "we", "his", "her", "its", "them",
}
MIN_QUERY_LENGTH = 3


def build_pattern(query: str):
    if not query:
        return None
    escaped = re.escape(query)
    escaped = r"\b" + escaped + r"\b"
    return re.compile(escaped, re.IGNORECASE)

def normalize_query(raw_query: str) -> str:
    """Collapse repeated whitespace and strip stray leading/trailing punctuation
    (e.g. an accidental trailing comma or period), without touching punctuation in
    the middle of a phrase, since that could be intentional (e.g. "U.S." or "Mr. Smith")."""
    collapsed = re.sub(r"\s+", " ", raw_query).strip()
    return collapsed.strip(".,;:!?'\"")

def highlight(text: str, pattern) -> str:
    if pattern is None:
        return text
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", text)


def role_class(speaker_type: str) -> str:
    if speaker_type == "host":
        return "role-host"
    elif speaker_type == "guest":
        return "role-guest"
    return ""


def build_transcript_text(convo) -> str:
    """Plain-text version of the full transcript, role labels only (no speaker names),
    consistent with how matches are displayed elsewhere in the app."""
    lines = []
    for utt in convo["utterances"]:
        role = (utt.get("speaker_type") or "unknown").capitalize()
        lines.append(f"{role}: {utt['text']}")
    return "\n\n".join(lines)


def get_turns(convo):
    """Group consecutive utterances by the same speaker into a single 'turn'."""
    utts = convo["utterances"]
    turns = []
    current = [0]
    for i in range(1, len(utts)):
        if utts[i].get("speaker_type") == utts[i - 1].get("speaker_type"):
            current.append(i)
        else:
            turns.append(current)
            current = [i]
    turns.append(current)
    return turns


def get_single_exchange(convo, turns, utt_index: int):
    """Return just the one host/guest interaction (two turns) containing the match."""
    utts = convo["utterances"]
    turn_idx = next(i for i, t in enumerate(turns) if utt_index in t)
    matched_speaker = utts[turns[turn_idx][0]].get("speaker_type")

    if matched_speaker == "guest" and turn_idx > 0:
        pair = [turn_idx - 1, turn_idx]
    elif matched_speaker == "host" and turn_idx < len(turns) - 1:
        pair = [turn_idx, turn_idx + 1]
    elif turn_idx > 0:
        pair = [turn_idx - 1, turn_idx]
    elif turn_idx < len(turns) - 1:
        pair = [turn_idx, turn_idx + 1]
    else:
        pair = [turn_idx]

    indices = [i for t in pair for i in turns[t]]
    return [utts[i] for i in indices]

# ---------------------------------------------------------------------------
# Cached search — only re-runs when query/filters actually change
# ---------------------------------------------------------------------------
def build_fts_query(query: str) -> str:
    """Wrap the (already normalized) query as an FTS5 phrase so multi-word
    queries require exact adjacent-token order, matching the old regex's
    literal-substring behavior. Internal double quotes must be doubled per
    FTS5 string-escaping rules."""
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


@st.cache_data
def run_search(query, program_filter, speaker_type_filter, year_filter):
    """Filtering happens in SQL now (program/year/speaker_type), and FTS5 finds
    matching rows via its index instead of scanning every transcript in Python.
    `pattern` is still built and returned purely for highlighting matched text
    in the UI, not for finding matches."""
    conn = get_connection()
    pattern = build_pattern(query)

    sql = """
        SELECT u.id, u.episode_id, u.speaker_type, u.text
        FROM utterances_fts
        JOIN utterances u ON u.id = utterances_fts.rowid
        JOIN episodes e ON e.episode_id = u.episode_id
        WHERE utterances_fts MATCH ?
    """
    params = [build_fts_query(query)]

    if program_filter:
        sql += f" AND e.program IN ({','.join('?' for _ in program_filter)})"
        params.extend(program_filter)
    if speaker_type_filter:
        sql += f" AND u.speaker_type IN ({','.join('?' for _ in speaker_type_filter)})"
        params.extend(speaker_type_filter)
    if year_filter:
        sql += f" AND e.year IN ({','.join('?' for _ in year_filter)})"
        params.extend(int(y) for y in year_filter)

    sql += " ORDER BY u.episode_id, u.turn_order"

    grouped = {}
    for row in conn.execute(sql, params).fetchall():
        utt = {"id": row["id"], "speaker_type": row["speaker_type"], "text": row["text"]}
        grouped.setdefault(row["episode_id"], {"matches": []})
        grouped[row["episode_id"]]["matches"].append(utt)

    return pattern, grouped
# ---------------------------------------------------------------------------
# Filter selector (sidebar)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        "<div style='font-weight:600;color:#1e293b;margin-bottom:0.35rem;'>Filters</div>",
        unsafe_allow_html=True,
    )
    st.multiselect("Programs", programs, key="program_filter_widget")
    st.multiselect("Speakers", speaker_types, key="speaker_type_filter_widget")
    st.multiselect("Years", years, key="year_filter_widget")

# ---------------------------------------------------------------------------
# Search (main body)
# ---------------------------------------------------------------------------
st.markdown(
    "<div style='font-weight:600;color:#1e293b;margin-bottom:0.35rem;'>Search</div>",
    unsafe_allow_html=True,
)
with st.form("search_form"):
    query = st.text_input(
        "Search term or phrase",
        label_visibility="collapsed",
        placeholder="Enter a word or phrase to search for...",
    )
    submitted = st.form_submit_button("Apply")

if submitted:
    normalized_query = normalize_query(query)
    cleaned_query = normalized_query.lower()
    words = cleaned_query.split()

    if not cleaned_query:
        st.warning("Enter a search term before applying.")
    elif len(cleaned_query) < MIN_QUERY_LENGTH:
        st.warning(f"Search term must be at least {MIN_QUERY_LENGTH} characters.")
    elif words and all(w in STOPWORDS for w in words):
        st.warning(
            "Your search is made up entirely of very common words "
            "(e.g. \"the,\" \"of,\" \"and\"). Try adding a more specific term."
        )
    else:
        st.session_state.search_applied = True
        st.session_state.query = normalized_query
        st.session_state.query_was_corrected = normalized_query != query.strip()
        st.session_state.original_query_input = query.strip()
        st.session_state.program_filter = st.session_state.program_filter_widget
        st.session_state.speaker_type_filter = st.session_state.speaker_type_filter_widget
        st.session_state.year_filter = st.session_state.year_filter_widget
        st.session_state.page_number = 1  # reset to page 1 on every new search

st.divider()

# ---------------------------------------------------------------------------
# Guard: don't proceed until a valid search has actually been applied
# ---------------------------------------------------------------------------
if not st.session_state.search_applied or "query" not in st.session_state:
    st.session_state.search_applied = False
    st.info("Apply filters and/or enter a search term above, then click **Apply**.")
    st.stop()

# ---------------------------------------------------------------------------
# Run the (cached) search
# ---------------------------------------------------------------------------
query = st.session_state.query
program_filter = tuple(st.session_state.program_filter)
speaker_type_filter = tuple(st.session_state.speaker_type_filter)
year_filter = tuple(st.session_state.year_filter)

pattern, grouped = run_search(query, program_filter, speaker_type_filter, year_filter)

n_matches = sum(len(g["matches"]) for g in grouped.values())
n_episodes = len(grouped)
st.subheader(f"Found **{n_matches}** results across **{n_episodes}** transcripts.")

if st.session_state.get("query_was_corrected"):
    st.caption(
        f"Showing results for \"{st.session_state.query}\""
    )

# ---------------------------------------------------------------------------
# Results / save episodes
# ---------------------------------------------------------------------------
if n_matches == 0:
    st.warning("No matches found. Try a different term or fewer filters.")
else:
    # Save episodes: export metadata only for every episode with at least one match
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["episode_id", "program", "title", "date", "num_utterances", "num_matches"])
    for episode_id, group in grouped.items():
        convo = get_episode(episode_id)
        writer.writerow([
            episode_id,
            convo.get("program") or "",
            convo.get("title") or "",
            convo.get("date") or "",
            len(convo["utterances"]),
            len(group["matches"]),
        ])

    st.download_button(
        "Save episodes (CSV)",
        data=buffer.getvalue(),
        file_name="npr_matching_episodes_metadata.csv",
        mime="text/csv",
        help="Exports episode-level metadata (no transcript text) for every episode containing at least one match.",
    )

    sorted_groups = sorted(grouped.items(), key=lambda item: len(item[1]["matches"]), reverse=True)

    PAGE_SIZE = 7
    total_pages = max(1, (len(sorted_groups) + PAGE_SIZE - 1) // PAGE_SIZE)
    st.session_state.page_number = min(st.session_state.page_number, total_pages)

    start = (st.session_state.page_number - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_groups = sorted_groups[start:end]

    for episode_id, group in page_groups:
        convo = get_episode(episode_id)
        matches = group["matches"]
        program = convo.get("program") or "Unknown program"
        turns = get_turns(convo)  # computed once, reused for every match in this episode

        # Match rows only carry (id, speaker_type, text) from the FTS query;
        # look up each match's position in the full transcript by primary key,
        # since turn_order has gaps and can't be used as a direct list index.
        id_to_idx = {utt["id"]: i for i, utt in enumerate(convo["utterances"])}

        with st.container(border=True):
            header_col, meta_col = st.columns([10, 1], vertical_alignment="center")
            with header_col:
                st.markdown(f"**Episode {episode_id} · {program}**  — {len(matches)} match(es)")
            with meta_col:
                with st.popover("🔍", help="View episode metadata"):
                    if convo.get("title"):
                        st.markdown(f"**Title:** {convo['title']}")
                    if convo.get("date"):
                        st.markdown(f"**Date:** {convo['date']}")
                    st.markdown(f"**Program:** {program}")

                    st.download_button(
                        "Download full transcript (.txt)",
                        data=build_transcript_text(convo),
                        file_name=f"episode_{episode_id}_transcript.txt",
                        mime="text/plain",
                        key=f"download_transcript_{episode_id}",
                    )

            # Cap the height once there are enough matches to create noise;
            # short match lists render without a forced scrollbar.
            use_scroll_box = len(matches) > 3
            matches_area = st.container(height=500) if use_scroll_box else st.container()

            with matches_area:
                for utt in matches:
                    idx = id_to_idx[utt["id"]]
                    role = utt.get("speaker_type") or "unknown"
                    css_class = role_class(role)
                    text_html = highlight(utt["text"], pattern)
                    position_pct = round((idx + 1) / len(convo["utterances"]) * 100)
                    position_label = (
                        f"<span style='color:#94a3b8;font-size:0.8em;'>{position_pct}% through transcript</span>"
                    )
                    st.markdown(
                        f"<div class='{css_class}'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                        f"<strong>{role.capitalize()}</strong>{position_label}"
                        f"</div>"
                        f"{text_html}</div>",
                        unsafe_allow_html=True,
                    )

                    with st.expander("View full exchange"):
                        exchange = get_single_exchange(convo, turns, idx)
                        for exch_utt in exchange:
                            exch_role = exch_utt.get("speaker_type") or "unknown"
                            exch_css_class = role_class(exch_role)
                            is_match = exch_utt["id"] == utt["id"]
                            exch_text_html = highlight(exch_utt["text"], pattern) if is_match else exch_utt["text"]
                            st.markdown(
                                f"<div class='{exch_css_class}'><strong>{exch_role.capitalize()}</strong><br>{exch_text_html}</div>",
                                unsafe_allow_html=True,
                            )

                    st.markdown("<hr style='margin:0.5rem 0;opacity:0.3'>", unsafe_allow_html=True)

        # Pagination controls — nested inside `else`, so total_pages always exists here
    if total_pages > 1:
        st.divider()


        def go_to_previous_page():
            st.session_state.page_number -= 1
            st.session_state.scroll_to_top = True


        def go_to_next_page():
            st.session_state.page_number += 1
            st.session_state.scroll_to_top = True


        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            st.button(
                "← Previous",
                disabled=st.session_state.page_number <= 1,
                on_click=go_to_previous_page,
            )
        with col2:
            st.markdown(
                f"<p style='text-align:center;color:#64748b;'>Page {st.session_state.page_number} of {total_pages}</p>",
                unsafe_allow_html=True,
            )
        with col3:
            st.button(
                "Next →",
                disabled=st.session_state.page_number >= total_pages,
                on_click=go_to_next_page,
            )