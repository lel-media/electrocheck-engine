# electrocheck-engine

The verdict engine of [Electrocheck](https://lel.media/electrocheck/), the live fact-checking of French political television by
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

1. Get a Gemini API key at <https://aistudio.google.com/apikey> (required) and a Linkup key at <https://linkup.so>
   (recommended). The engine runs without Linkup, on Gemini's own Google Search alone, but Gemini decides by itself whether
   to search and often answers from memory: many checks then end with no usable source. Google AI Studio and Linkup both
   have a limited free tier: you can create the keys and try the engine without paying.
2. Copy `.env.example` to `.env` next to `engine.py` and fill it in:

| Setting | Default | What it does |
|---|---|---|
| `API_GEMINI` | (required) | Gemini API key |
| `API_LINKUP` | empty | Linkup API key, recommended; empty = `google_first` only, with Gemini's own search |
| `CHECK_ORDER` | `google_first` | `linkup_first` or `google_first` (a request can ask for either) |
| `GEMINI_MODEL` | `gemini-3.8-flash` | the model that checks |
| `GEMINI_PREFLIGHT_MODEL` | `gemini-3.5-flash-lite` | the model that writes the Linkup query |
| `GEMINI_API_VERSION` | `v1` | Gemini API version |
| `CHECK_LOG` | `logs` | folder of the journal of the checks (see below); empty = none |
| `HOST`, `PORT` | `127.0.0.1`, `8050` | where the service listens |

## Run

Windows (PowerShell):

```powershell
python -m venv .venv
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
about 10 seconds to two minutes.

## API

`POST /check` with a JSON body. Only `claim` is required; the more context, the better the check.

| Field | What it is |
|---|---|
| `claim` | the claim to check, as one self-contained sentence |
| `speaker` | who said it |
| `quote` | their exact words |
| `sentence` | the whole sentence around the exact words (and the one before) |
| `meant` | the sense the speaker clearly gives their words, when it is not the literal one |
| `before` | what was said right before (context only: the question asked, the topic) |
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

## The journal: why a verdict came out

The answer is kept lean; the reasoning is in the journal. Each check appends one JSON line to
`logs/checks-YYYY-MM-DD.jsonl` (set `CHECK_LOG` to move it, or to empty to switch it off):

| Key | What it holds |
|---|---|
| `request` | the request as received: replay it with `curl` |
| `id`, `at`, `seconds`, `order`, `model`, `prompts` | your id, when, how long, which order, which model, a short hash of the prompts in use |
| `linkup` | the query written for Linkup and its date filter, how long it took, an error if any, the pages found (url, tier) |
| `attempts` | every Gemini check call, the replaced ones included: thinking level, seconds, whether it really searched, its verdict, confidence, explanation, flags (`own`, `faithful`, `scope_ok`, `basis`, `evidence`) and the sources it gave, or why it failed |
| `decision` | which call won, the verdict asked and the verdict returned, `guard` and `calibration`, the flags and the sources kept |
| `error` | what went wrong, when the check failed |

A line is about 5 KB. A write that fails never stops a check. To keep the folder small, delete the old files
(`find logs -mtime +30 -delete`).

## Tests

```sh
pip install pytest
python -m pytest tests
```

Gemini and Linkup are replaced by scripted answers: the tests make no network call and need no key.

## License

MIT, see [LICENSE](LICENSE).

---

## Version française

Le moteur de verdict d'[Electrocheck](https://lel.media/electrocheck/), le fact-checking en direct de la télévision politique
française par Les Électrons Libres. Il reçoit une affirmation prononcée à l'antenne et décide si elle est vraie, sur quelles
sources, et avec quel degré de certitude.

C'est un petit service HTTP : un fichier Python pour le code (`engine.py`), un pour les prompts (`prompts.py`) et une page pour
l'essayer (`index.html`). Les prompts sont en français, puisque le moteur vérifie ce qui se dit à la télévision française.

### Ce qu'il décide, et comment

L'entrée est une affirmation déjà extraite de la transcription, dont on a prouvé qu'il s'agit des propres mots de l'orateur
(cette partie, la transcription en direct et l'extraction, n'est pas dans ce dépôt).

1. **Les sources.** Une vérification, c'est 1 à 3 appels à Gemini avec Google Search, plus une recherche web
   [Linkup](https://linkup.so) quand une clé Linkup est fournie. Deux ordres possibles, choisis par `CHECK_ORDER` :
   - `linkup_first` : un petit modèle transforme l'affirmation en consigne de recherche ; les pages de Linkup, sources
     primaires en tête, sont données à Gemini, qui les cite par leur numéro. S'il ne cite aucune page exploitable, on lui
     redemande une fois, avec une réflexion plus poussée.
   - `google_first` : Gemini avec Google Search seul. S'il ne donne aucun lien exploitable, on lui ajoute les pages de Linkup
     avec sa première réponse, puis on redemande une fois avec une réflexion plus poussée.

   Une source exploitable est une page, jamais la page d'accueil d'un site, et jamais un lien que le modèle a écrit de
   mémoire sans avoir cherché. Les sources sont classées : 1 primaires (statistiques officielles, institutions, parlements,
   organismes internationaux, recherche publique), 2 grands médias, 3 le reste. Les réseaux sociaux et les sites de vidéo
   sont exclus de la recherche.
2. **La réponse du modèle.** Un verdict (`vrai`, `plutot_vrai`, `mixte`, `plutot_faux`, `faux`, `insuffisant`), une
   explication dont chaque fait vient d'une source citée, et des indicateurs sur la façon dont il a jugé (lus par le code,
   non renvoyés) :
   - `own` : ces mots sont une affirmation propre de l'orateur, pas une citation qu'il conteste, une hypothèse ou une
     promesse ;
   - `faithful` : l'affirmation ne dit ni plus ni moins que les mots exacts ;
   - `scope_ok` : le verdict juge le sens que l'orateur a voulu donner, pas une lecture littérale qu'il n'entendait pas ;
   - `basis` : le verdict repose sur des faits indépendants, jamais sur le programme ou les déclarations de l'orateur ;
   - `evidence` : une source le dit directement, le verdict est déduit, ou rien n'a été trouvé.
3. **Le code a le dernier mot.** Tout indicateur faux ou absent transforme le verdict en `insuffisant` (`guard` dit
   pourquoi). Ne pas trouver, ce n'est pas réfuter : un verdict qui repose sur une absence de résultat devient
   `insuffisant`, un verdict négatif exige une preuve directe ou déduite, et un `faux` déduit est abaissé à `plutot_faux`.

### Installation

Python 3.12 ou plus récent.

1. Obtenez une clé d'API Gemini sur <https://aistudio.google.com/apikey> (obligatoire) et une clé Linkup sur
   <https://linkup.so> (recommandée). Le moteur fonctionne sans Linkup, avec la seule recherche Google de Gemini, mais
   Gemini décide seul s'il cherche et répond souvent de mémoire : beaucoup de vérifications finissent alors sans source
   exploitable. Google AI Studio et Linkup ont tous deux une offre gratuite limitée : vous pouvez créer les clés et essayer
   le moteur sans rien payer.
2. Copiez `.env.example` en `.env` à côté de `engine.py` et remplissez-le :

| Réglage | Par défaut | Rôle |
|---|---|---|
| `API_GEMINI` | (obligatoire) | clé d'API Gemini |
| `API_LINKUP` | vide | clé d'API Linkup, recommandée ; vide = `google_first` uniquement, avec la recherche de Gemini seule |
| `CHECK_ORDER` | `google_first` | `linkup_first` ou `google_first` (une requête peut demander l'un ou l'autre) |
| `GEMINI_MODEL` | `gemini-3.8-flash` | le modèle qui vérifie |
| `GEMINI_PREFLIGHT_MODEL` | `gemini-3.5-flash-lite` | le modèle qui écrit la requête Linkup |
| `GEMINI_API_VERSION` | `v1` | version de l'API Gemini |
| `CHECK_LOG` | `logs` | dossier du journal des vérifications (voir plus bas) ; vide = aucun |
| `HOST`, `PORT` | `127.0.0.1`, `8050` | où le service écoute |

### Lancement

Windows (PowerShell) :

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # puis modifiez .env
python engine.py
```

Linux / macOS :

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # puis modifiez .env
python engine.py
```

Ouvrez ensuite <http://127.0.0.1:8050/> pour essayer une affirmation (« Exemple » en remplit une vraie), ou appelez l'API.
Une vérification prend d'environ 10 secondes à deux minutes.

### API

`POST /check` avec un corps JSON. Seul `claim` est obligatoire ; plus il y a de contexte, meilleure est la vérification.

| Champ | Contenu |
|---|---|
| `claim` | l'affirmation à vérifier, en une phrase autonome |
| `speaker` | qui l'a dite |
| `quote` | ses mots exacts |
| `sentence` | la phrase entière autour des mots exacts (et celle d'avant) |
| `meant` | le sens que l'orateur donne clairement à ses mots, quand ce n'est pas le sens littéral |
| `before` | ce qui a été dit juste avant (contexte seulement : la question posée, le sujet) |
| `after` | ce qui a été dit juste après (contexte seulement) |
| `context` | la transcription brute autour de l'affirmation |
| `captions` | les sous-titres TV du passage |
| `voices` | les noms que la transcription peut attribuer |
| `channel` | la chaîne de télévision |
| `asserted_at` | quand elle a été dite, au format ISO 8601 |
| `order` | `linkup_first` ou `google_first`, sinon `CHECK_ORDER` |

```sh
curl -s http://127.0.0.1:8050/check -H 'Content-Type: application/json' -d '{
  "claim": "La proportion d'\''élèves issus de milieux défavorisés est de 43 % dans les collèges publics et de 18 % dans les collèges privés.",
  "speaker": "Raphaël Glucksmann",
  "quote": "dans un collège public aujourd'\''hui, il y a 43 pourcents d'\''élèves issus de milieux défavorisés, dans un collège privé, c'\''est 18 pourcents",
  "channel": "France 2",
  "asserted_at": "2026-10-01T19:06:30Z"
}'
```

La réponse :

| Champ | Contenu |
|---|---|
| `verdict` | `vrai`, `plutot_vrai`, `mixte`, `plutot_faux`, `faux` ou `insuffisant` |
| `explanation` | 2 à 4 phrases, sur les faits uniquement |
| `confidence` | de 0 à 1, telle que le modèle l'a donnée |
| `meant` | le sens de l'affirmation qui a été jugé |
| `guard` | pourquoi le verdict a été ramené à `insuffisant` (un indicateur, ou ce que les preuves permettent), sinon `null` |
| `sources` | `[{title, url, domain, tier, says}]`, les meilleures d'abord ; `says` est ce que la page établit |
| `attempts` | chaque appel de vérification à Gemini, y compris ceux qui ont été remplacés : `{call, thinking, verdict, confidence, usable_sources}`, ou `{call, thinking, failed}` |

Une erreur renvoie HTTP 500 avec `{"error": "..."}` (400 quand `claim` manque).

### Le journal : pourquoi un verdict est sorti ainsi

La réponse reste légère ; le raisonnement est dans le journal. Chaque vérification ajoute une ligne JSON à
`logs/checks-AAAA-MM-JJ.jsonl` (`CHECK_LOG` le déplace, ou le désactive s'il est vide) :

| Clé | Contenu |
|---|---|
| `request` | la requête telle que reçue : rejouable avec `curl` |
| `id`, `at`, `seconds`, `order`, `model`, `prompts` | votre identifiant, quand, combien de temps, quel ordre, quel modèle, une empreinte courte des prompts utilisés |
| `linkup` | la requête écrite pour Linkup et son filtre de date, sa durée, une erreur éventuelle, les pages trouvées (url, rang) |
| `attempts` | chaque appel de vérification à Gemini, y compris ceux qui ont été remplacés : niveau de réflexion, durée, a-t-il vraiment cherché, son verdict, sa confiance, son explication, ses indicateurs (`own`, `faithful`, `scope_ok`, `basis`, `evidence`) et les sources données, ou pourquoi il a échoué |
| `decision` | quel appel l'a emporté, le verdict demandé et le verdict rendu, `guard` et `calibration`, les indicateurs et les sources retenues |
| `error` | ce qui a échoué, si la vérification a échoué |

Une ligne fait environ 5 Ko. Une écriture qui échoue n'arrête jamais une vérification. Pour garder un dossier léger,
supprimez les anciens fichiers (`find logs -mtime +30 -delete`).

### Tests

```sh
pip install pytest
python -m pytest tests
```

Gemini et Linkup sont remplacés par des réponses scriptées : les tests ne font aucun appel réseau et n'ont besoin d'aucune clé.

### Licence

MIT, voir [LICENSE](LICENSE).
