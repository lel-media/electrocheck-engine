# electrocheck-engine

The verdict engine of [Electrocheck](https://lel.media), the live fact-checking of French political television by
Les Électrons Libres. It takes a claim made on air and decides whether it is true, on which sources, and how sure it can be.

It is a small HTTP service: one Python file for the code (`engine.py`), one for the prompts (`prompts.py`), and a page to
try it (`index.html`). The prompts are in French, because the engine checks what is said on French television.

## What it decides, and how

The input is one claim already extracted from the transcript and proven to be the speaker's own words (that part, the
live transcription and the extraction, is not in this repository).

1. **Sources.** One check is 1 to 3 calls to Gemini with Google Search, plus a [Linkup](https://linkup.so) web search when a
   Linkup key is set. Two orders, chosen with `CHECK_ORDER`:
   - `linkup_first`: a small model turns the claim into a search instruction; Linkup's pages, primary sources first, are
     given to Gemini, which cites them by number. If it cites no usable page, it is asked once more, thinking harder.
   - `google_first`: Gemini with Google Search alone. If it gives no usable link, Linkup's pages are added with its first
     answer, then once more thinking harder.

   A usable source is a page, never a site's homepage, and never a link the model wrote from memory without searching.
   Sources are ranked: 1 primary (official statistics, institutions, parliaments, international bodies, public research),
   2 major media, 3 the rest. Social networks and video sites are excluded from the search.
2. **The model's answer.** A verdict (`vrai`, `plutot_vrai`, `mixte`, `plutot_faux`, `faux`, `insuffisant`), an
   explanation whose every fact comes from a cited source, and flags on how it judged (read by the code, not returned):
   - `own`: the words are the speaker's own assertion, not a quote they rebut, a hypothesis or a promise;
   - `faithful`: the claim says neither more nor less than the exact words;
   - `scope_ok`: the verdict judges the sense the speaker meant, not a literal reading they did not intend;
   - `basis`: the verdict rests on independent facts, never on the speaker's own programme or statements;
   - `evidence`: a source says it directly, the verdict is inferred, or nothing was found.
3. **The code has the last word.** Any flag that is false or missing turns the verdict into `insuffisant`
   (`guard` says why). Not finding is not refuting: a verdict resting on nothing found is `insuffisant`, a negative
   verdict needs direct or inferred evidence, and an inferred `faux` is lowered to `plutot_faux`.

## Setup

Python 3.12 or later.

1. Get a Gemini API key at <https://aistudio.google.com/apikey>. A Linkup key (<https://linkup.so>) is optional:
   without it, the engine relies on Gemini's own Google Search.
2. Copy `.env.example` to `.env` next to `engine.py` and fill it in:

| Setting | Default | What it does |
|---|---|---|
| `API_GEMINI` | (required) | Gemini API key |
| `API_LINKUP` | empty | Linkup API key; empty = `google_first` only |
| `CHECK_ORDER` | `google_first` | `linkup_first` or `google_first` (a request can ask for either) |
| `GEMINI_MODEL` | `gemini-3.8-flash` | the model that checks |
| `GEMINI_PREFLIGHT_MODEL` | `gemini-3.5-flash-lite` | the model that writes the Linkup query |
| `GEMINI_API_VERSION` | `v1` | Gemini API version |
| `HOST`, `PORT` | `127.0.0.1`, `8050` | where the service listens |

## Run

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then edit .env
python engine.py
```

Linux / macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
python engine.py
```

Then open <http://127.0.0.1:8050/> to try a claim ("Exemple" fills in a real one), or call the API. A check takes from
about 10 seconds to a minute.

## API

`POST /check` with a JSON body. Only `claim` is required; the more context, the better the check.

| Field | What it is |
|---|---|
| `claim` | the claim to check, as one self-contained sentence |
| `speaker` | who said it |
| `quote` | their exact words |
| `sentence` | the whole sentence around the exact words (and the one before) |
| `meant` | the sense the speaker clearly gives their words, when it is not the literal one |
| `after` | what was said right after (context only) |
| `context` | the raw transcript around the claim |
| `captions` | the TV captions of the passage |
| `voices` | the names the transcript can attribute |
| `channel` | the TV channel |
| `asserted_at` | when it was said, ISO 8601 |
| `order` | `linkup_first` or `google_first`, else `CHECK_ORDER` |

```sh
curl -s http://127.0.0.1:8050/check -H 'Content-Type: application/json' -d '{
  "claim": "La proportion d'\''élèves issus de milieux défavorisés est de 43 % dans les collèges publics et de 18 % dans les collèges privés.",
  "speaker": "Raphaël Glucksmann",
  "quote": "dans un collège public aujourd'\''hui, il y a 43 pourcents d'\''élèves issus de milieux défavorisés, dans un collège privé, c'\''est 18 pourcents",
  "channel": "France 2",
  "asserted_at": "2026-10-01T19:06:30Z"
}'
```

The answer:

| Field | What it is |
|---|---|
| `verdict` | `vrai`, `plutot_vrai`, `mixte`, `plutot_faux`, `faux` or `insuffisant` |
| `explanation` | 2 to 4 sentences, on the facts only |
| `confidence` | 0 to 1, as the model gave it |
| `meant` | the sense of the claim that was judged |
| `guard` | why the verdict was forced to `insuffisant` (a flag, or what the evidence allows), else `null` |
| `sources` | `[{title, url, domain, tier, says}]`, best first; `says` is what the page establishes |
| `attempts` | each Gemini check call, the replaced ones included: `{call, thinking, verdict, confidence, usable_sources}`, or `{call, thinking, failed}` |

An error answers HTTP 500 with `{"error": "..."}` (400 when `claim` is missing).

## Tests

```sh
pip install pytest
python -m pytest tests
```

Gemini and Linkup are replaced by scripted answers: the tests make no network call and need no key.

## License

MIT, see [LICENSE](LICENSE).
