# NPR Transcript Explorer

A searchable interface for NPR interview transcripts, built as a prototype for 
an eventual search tool for the American Voices Project corpus. Supports filtered full-text search (program, 
speaker type, year) across transcripts using a SQLite FTS5 index.

## Stack
- Streamlit (frontend)
- SQLite + FTS5 (full-text search index)
- Python

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

## Data Extraction

The corpus comes from ConvoKit's NPR-2P dataset, extracted via Google Colab 
(to avoid local storage constraints) and downloaded as CSVs.

### 1. Install ConvoKit and load the corpus
Run in a Colab notebook:
```python
!pip install convokit -q

from convokit import Corpus, download

corpus = Corpus(filename=download("npr-2p-corpus"))
print(corpus.print_summary_stats())
```

### 2. Extract utterances
```python
import pandas as pd

rows = []
for utt in corpus.iter_utterances():
    sm = utt.speaker.meta
    rows.append({
        "utterance_id":    utt.id,
        "conversation_id": utt.conversation_id,
        "turn_order":      utt.meta.get("order"),
        "speaker":         sm.get("name"),
        "speaker_id":      utt.speaker.id,
        "speaker_type":    sm.get("type"),
        "legacy_episode":  utt.meta.get("episode"),
        "text":            utt.text,
    })
u = pd.DataFrame(rows)
```

### 3. Extract episode metadata
```python
ep = pd.DataFrame([{
    "episode_id":   cv.id,
    "program":      cv.meta.get("program"),
    "date":         str(cv.meta.get("date")),
    "year":         int(str(cv.meta.get("date"))[:4]),
    "title":        cv.meta.get("title"),
    "n_utterances": len(cv.get_utterance_ids()),
} for cv in corpus.iter_conversations()])
```

### 4. Save and download
```python
ep.to_csv("npr_episodes.csv", index=False)
u.to_csv("npr_utterances.csv", index=False)  # NOTE: no gzip, to match build_index.py's expected input

from google.colab import files
files.download("npr_episodes.csv")
files.download("npr_utterances.csv")
```

Place both downloaded CSVs into the `data/` folder before running `build_index.py`.


## Running search tool

### 1. Build the index
```bash
python build_index.py
```

This generates `data/npr_index.db`.

### 2. Run the app

```bash
streamlit run app.py
```