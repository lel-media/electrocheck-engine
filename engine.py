"""electrocheck-engine: decides whether a factual claim made on French television is true, and on which sources.

POST /check takes one claim that was already extracted from the transcript and proven to be the speaker's own words, and
returns the verdict, its explanation, the sources it rests on and the flags the verdict was judged by. GET / is a page to try it.

One check is 1 to 3 Gemini calls with Google Search, plus one Linkup web search when a Linkup key is set:
  linkup_first  a small model writes a Linkup query; Linkup's pages (primary sources first) are given to Gemini, which cites
                them by number. No usable source: once more, thinking harder.
  google_first  Gemini with Google Search alone. No usable link: Linkup's pages and Gemini again with its first answer, then
                once more thinking harder.
A source is usable when it is a page, not a site's homepage. Still none: the verdict comes back with no source.

The model proposes, the code decides: a verdict whose own flags say the words are not the speaker's own assertion, that the
claim does not say exactly what they said, that it judged a sense the speaker did not mean, or that it rests on anything but
independent facts becomes "insuffisant". Not finding is not refuting: a negative verdict needs a source that says so
directly or by inference, and an inferred "faux" is lowered to "plutot_faux".
"""
from __future__ import annotations

import json
import logging
import os
import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from google import genai
from google.genai import types

import prompts

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env", override=False)
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("electrocheck")
log.setLevel(logging.INFO)                                          # the engine's own steps; the libraries' warnings only
logging.getLogger("google_genai").setLevel(logging.ERROR)           # its "automatic function calling" notice on every call

# ---------------------------------------------------------------- settings (.env or environment)

GEMINI_KEY = os.environ.get("API_GEMINI", "")
LINKUP_KEY = os.environ.get("API_LINKUP", "")                       # recommended: without it, Gemini's own search only
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")           # the check
QUERY_MODEL = os.environ.get("GEMINI_PREFLIGHT_MODEL", "gemini-3.5-flash-lite")   # the Linkup query
API_VERSION = os.environ.get("GEMINI_API_VERSION", "v1")
CHECK_ORDER = os.environ.get("CHECK_ORDER", "google_first")         # google_first | linkup_first
# One JSON line per check in CHECK_LOG/checks-<day>.jsonl: the request, the Linkup query and pages, every Gemini call (the
# replaced ones too) with its explanation, flags and sources, the decision and its steps. "" = no journal.
CHECK_LOG = os.environ.get("CHECK_LOG", str(HERE / "logs"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8050"))

client = genai.Client(api_key=GEMINI_KEY, http_options={"api_version": API_VERSION}) if GEMINI_KEY else None

# ---------------------------------------------------------------- sources

# Searched first; also listed in the check prompt.
CATALOG = (
    ("INSEE", "https://www.insee.fr"),
    ("OCDE", "https://www.oecd.org"),
    ("Eurostat", "https://ec.europa.eu/eurostat"),
    ("data.gouv.fr", "https://www.data.gouv.fr"),
    ("Assemblée nationale", "https://www.assemblee-nationale.fr"),
    ("Sénat", "https://www.senat.fr"),
    ("Cour des comptes", "https://www.ccomptes.fr"),
    ("Vie publique", "https://www.vie-publique.fr"),
    ("Légifrance", "https://www.legifrance.gouv.fr"),
    ("Gouvernement", "https://www.gouvernement.fr"),
    ("Ministère de l'Économie", "https://www.economie.gouv.fr"),
    ("Ministère du Travail", "https://travail-emploi.gouv.fr"),
    ("Ministère de l'Intérieur", "https://www.interieur.gouv.fr"),
    ("Ministère de l'Éducation", "https://www.education.gouv.fr"),
    ("Ministère de la Santé", "https://sante.gouv.fr"),
    ("Ministère de la Transition écologique", "https://www.ecologie.gouv.fr"),
    ("Banque de France", "https://www.banque-france.fr"),
    ("OFCE", "https://www.ofce.sciences-po.fr"),
    ("Our World in Data", "https://ourworldindata.org"),
    ("FMI", "https://www.imf.org"),
    ("Banque mondiale", "https://www.worldbank.org"),
)
CATALOG_DOMAINS = tuple((urlparse(url).hostname or "").removeprefix("www.") for _, url in CATALOG)
SOURCE_LIST = "\n".join(f"- {name} ({domain}) — {url}" for (name, url), domain in zip(CATALOG, CATALOG_DOMAINS))

# Source tiers: 1 primary (official statistics, institutions, parliaments, international bodies, public research, Our World in
# Data), 2 big newspapers and well-known media, 3 the rest. A domain matches itself and its subdomains.
FIRST_PARTY = tuple(url.split("//", 1)[1].split("/", 1)[0].removeprefix("www.") for _, url in CATALOG) + (
    "ec.europa.eu", "europa.eu", "dares.travail-emploi.gouv.fr", "drees.solidarites-sante.gouv.fr", "ined.fr", "inrae.fr",
    "cnrs.fr", "cepii.fr", "strategie.gouv.fr", "cae-eco.fr", "cor-retraites.fr", "ademe.fr", "conseil-constitutionnel.fr",
    "conseil-etat.fr", "elysee.fr", "info.gouv.fr", "service-public.fr", "fondsdereserve.fr", "securite-sociale.fr", "urssaf.fr",
    "francetravail.fr", "ifop.com", "elabe.fr", "ipsos.com", "un.org", "who.int", "ilo.org", "wto.org", "ecb.europa.eu",
    "kielinstitut.de", "ifw-kiel.de", "sipri.org", "iea.org", "europarl.europa.eu", "consilium.europa.eu",
)
FIRST_PARTY_SUFFIXES = (".gouv.fr", ".europa.eu", ".int", ".un.org", ".gov", ".gov.uk", ".admin.ch")
SECOND_PARTY = (
    "lemonde.fr", "lefigaro.fr", "liberation.fr", "lesechos.fr", "latribune.fr", "leparisien.fr", "ouest-france.fr", "la-croix.com",
    "humanite.fr", "lopinion.fr", "francetvinfo.fr", "franceinfo.fr", "radiofrance.fr", "franceinter.fr", "francebleu.fr",
    "france24.com", "rfi.fr", "tv5monde.com", "bfmtv.com", "tf1info.fr", "lci.fr", "francetelevisions.fr", "publicsenat.fr", "lcp.fr",
    "20minutes.fr", "lexpress.fr", "lepoint.fr", "nouvelobs.com", "marianne.net", "challenges.fr", "capital.fr", "mediapart.fr",
    "huffingtonpost.fr", "lejdd.fr", "europe1.fr", "rtl.fr", "sudouest.fr", "ladepeche.fr", "letelegramme.fr", "lavoixdunord.fr",
    "leprogres.fr", "ledauphine.com", "lindependant.fr", "midilibre.fr", "dna.fr", "estrepublicain.fr", "alternatives-economiques.fr",
    "usinenouvelle.com", "lemoniteur.fr", "contexte.com", "politico.eu", "euronews.com", "euractiv.fr", "euractiv.com", "afp.com",
    "reuters.com", "apnews.com", "bloomberg.com", "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com", "washingtonpost.com",
    "ft.com", "economist.com", "wsj.com", "spiegel.de", "lesoir.be", "rtbf.be", "letemps.ch", "courrierinternational.com",
    "slate.fr", "next.ink", "numerama.com", "lemondeinformatique.fr", "pv-magazine.fr", "connaissancedesenergies.org",
)
TIER_LABELS = {1: "source primaire", 2: "grand média", 3: "autre source"}

# Linkup (api.linkup.so), used as its best practices say: depth "standard" (one agentic pass, 1-3 s), raw search results for
# an LLM to read, the query an instruction rather than the claim pasted, dates in fromDate, many results and the best kept.
LINKUP_URL = "https://api.linkup.so/v1/search"
LINKUP_RESULTS = 15           # asked of Linkup
LINKUP_PAGES = 12             # given to Gemini, best tier first
SNIPPET_CHARS = 600           # of each page's text
QUERY_CHARS = 700
# nobody can cite these as a source (a video of the speaker saying the same thing is no evidence)
LINKUP_EXCLUDE = ["facebook.com", "x.com", "twitter.com", "instagram.com", "tiktok.com", "youtube.com", "dailymotion.com",
                  "linkedin.com", "video.lefigaro.fr"]

# ---------------------------------------------------------------- verdicts and text limits

ORDERS = ("google_first", "linkup_first")
VERDICTS = ("vrai", "plutot_vrai", "mixte", "plutot_faux", "faux", "insuffisant")
VERDICT_ALIASES = {"true": "vrai", "false": "faux", "mostly true": "plutot_vrai", "mostly false": "plutot_faux",
                   "plutôt vrai": "plutot_vrai", "plutot vrai": "plutot_vrai", "plutôt faux": "plutot_faux",
                   "plutot faux": "plutot_faux", "à relire": "insuffisant", "indetermine": "insuffisant", "indéterminé": "insuffisant"}
NEGATIVE = ("faux", "plutot_faux", "mixte")
CAPTION_CHARS = 800           # the captions given to the check
CONTEXT_CHARS = 800           # the raw transcript around the claim (a whole transcript window: up to about 750)
BEFORE_CHARS = 800            # what was said right before the claim: its end is kept
AFTER_CHARS = 800             # what was said right after the claim
SAYS_CHARS = 400              # what a source says, in the answer
JSON_ATTEMPTS = 2             # a JSON-only answer with a stray character is asked again: the same prompt almost always comes back clean
LEAN = ("call", "thinking", "verdict", "confidence", "usable_sources", "failed")      # what the answer keeps of each call
PROMPT_VERSION = hashlib.sha1("".join((prompts.CHECK, prompts.PAGES_FIRST, prompts.LINKUP, prompts.QUERY)).encode()).hexdigest()[:8]
PARIS = ZoneInfo("Europe/Paris")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre")


# ---------------------------------------------------------------- the few things done more than once

def compact(text: str, limit: int, keep_end: bool = False) -> str:
    """One line of at most `limit` characters, cut at a word, "…" where it was cut: its start is kept, or its end."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    if keep_end:
        return "…" + cleaned[-(limit - 1):].split(" ", 1)[-1]
    return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"


def parse_json(text: str):
    """The JSON of a model's answer, even inside a ```json fence or some prose around one object."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("Réponse LLM sans JSON exploitable")


async def gemini(prompt: str, model: str, *, search: bool = False, url_context: bool = False, thinking: str | None = None) -> dict:
    """One Gemini answer: {"payload", "grounding" (the links of its Google searches), "searched" (it really searched: only
    then do the URLs it writes count)}. With search the answer is free text that must hold JSON; without, JSON is enforced
    and asked again once when it comes back empty or broken."""
    config = {"temperature": 0.1}
    if thinking:
        config["thinking_config"] = types.ThinkingConfig(thinking_level=thinking.upper())
    if search:
        config["tools"] = [types.Tool(google_search=types.GoogleSearch())] + (
            [types.Tool(url_context=types.UrlContext())] if url_context else [])
    else:
        config["response_mime_type"] = "application/json"
    config = types.GenerateContentConfig(**config)
    if not search:
        for attempt in range(1, JSON_ATTEMPTS + 1):
            response = await client.aio.models.generate_content(model=model, contents=prompt, config=config)
            text = (response.text or "").strip()
            last = attempt == JSON_ATTEMPTS
            if not text:
                if last:
                    raise RuntimeError("Réponse Gemini vide")
                log.warning("Réponse Gemini vide, nouvel essai")
                continue
            try:
                return {"payload": parse_json(text), "grounding": [], "searched": False}
            except ValueError:
                if last:
                    raise
                log.warning("JSON invalide dans la réponse Gemini, nouvel essai (fin de réponse : %r)", text[-60:])

    response = await client.aio.models.generate_content(model=model, contents=prompt, config=config)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Réponse Gemini vide")
    grounding, searched = [], False
    for candidate in getattr(response, "candidates", None) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        if not meta:
            continue
        # a search query, or grounding links without any (they come from a search too)
        queries = [q for q in getattr(meta, "web_search_queries", None) or [] if str(q or "").strip()]
        searched = searched or bool(queries) or bool(getattr(meta, "grounding_chunks", None))
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            url = str(getattr(web, "uri", "") or "") if web else ""
            if url and url not in {g["url"] for g in grounding}:
                grounding.append({"title": str(getattr(web, "title", "") or url), "url": url})
    try:
        payload = parse_json(text)
    except ValueError:
        repaired = await client.aio.models.generate_content(
            model=model, contents=prompts.JSON_REPAIR + text,
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"))
        payload = parse_json((repaired.text or "").strip())
    return {"payload": payload, "grounding": grounding, "searched": searched}


def domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def tier(url: str, title: str = "") -> int:
    """1, 2 or 3 (see the tiers above). A Google grounding link (a vertexaisearch redirect) carries its site in the title."""
    host = domain(url)
    if host == "vertexaisearch.cloud.google.com":
        host = (title or "").strip().lower().removeprefix("www.")
    if any(host == d or host.endswith("." + d) for d in FIRST_PARTY) or host.endswith(FIRST_PARTY_SUFFIXES):
        return 1
    if any(host == d or host.endswith("." + d) for d in SECOND_PARTY):
        return 2
    return 3


def same_page(url: str) -> tuple[str, str, str]:
    """A page however the model spells its URL: host without www, decoded path without a trailing slash, query."""
    p = urlparse(url.strip())
    return (p.hostname or "").removeprefix("www."), unquote(p.path).rstrip("/"), unquote(p.query)


def usable(url: str) -> bool:
    """A page, not a site's homepage."""
    return bool(urlparse(url).path.strip("/"))


def merge_sources(written: list, grounding: list[dict]) -> list[dict]:
    """The sources of an answer, once each: the catalogue's first, then the others, then sorted by tier."""
    merged, seen = [], set()
    for raw in list(written or []) + grounding:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        title = str(raw.get("title") or url).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append({"title": title or url, "url": url, "domain": domain(url), "tier": tier(url, title),
                       "says": " ".join(str(raw.get("says") or "").split())[:SAYS_CHARS]})
    preferred = [s for s in merged if any(s["domain"] and s["domain"].endswith(d) for d in CATALOG_DOMAINS)]
    return sorted(preferred + [s for s in merged if s not in preferred], key=lambda s: s["tier"] or 3)


def describe(call: str, thinking: str | None, seconds: float, searched: bool, payload: dict, sources: list[dict]) -> dict:
    """What one Gemini check call answered, for the journal (the answer keeps only LEAN of it)."""
    return {"call": call, "thinking": thinking, "seconds": seconds, "searched": searched,
            **{k: payload.get(k) for k in ("verdict", "confidence", "explanation", "meant", "own", "faithful", "scope_ok", "basis", "evidence")},
            "sources": [{"url": src["url"], "tier": src["tier"], "says": src["says"]} for src in sources],
            "usable_sources": sum(usable(src["url"]) for src in sources)}


def flag(value) -> bool | None:
    """A true/false flag as the model writes it; None when absent or unreadable, which is never a yes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "vrai", "oui", "yes"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "faux", "non", "no"):
        return False
    return None


# ---------------------------------------------------------------- the service

app = FastAPI(title="electrocheck-engine", docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(Exception)
async def failed(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/")
async def page() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.post("/check")
async def check(claim: dict) -> dict:
    """One claim -> its verdict. Fields (only "claim" is required):
    id (yours, to find the check again in the journal), claim (the sentence to check), speaker, quote (their exact words),
    sentence (the whole sentence around them), meant (the sense they give their words, when not the literal one), before /
    after (what was said right before / after: context only), context (the raw transcript around), captions (the TV
    captions of the passage), voices (the names the transcript can attribute), channel, asserted_at (ISO 8601), order
    (google_first | linkup_first, else CHECK_ORDER)."""
    if client is None:
        raise RuntimeError("Clé Gemini absente : renseigner API_GEMINI dans .env")
    if not str(claim.get("claim") or "").strip():
        return JSONResponse({"error": "claim manquant"}, status_code=400)
    started = time.perf_counter()
    cid = str(claim.get("id") or "")
    attempts: list[dict] = []          # what each Gemini check call answered, the ones later replaced included
    # the journal line of this check: the request as received (replayable with curl), then everything that decided the answer
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "id": cid, "prompts": PROMPT_VERSION, "model": MODEL,
              "request": claim, "order": None, "linkup": None, "attempts": attempts, "decision": None, "error": None}
    try:
        text = str(claim["claim"])
        said = f"{cid} {text[:50]}".strip()                     # names the claim in the log
        speaker, quote, meant = claim.get("speaker") or "", claim.get("quote") or "", claim.get("meant") or ""
        before, after, captions = claim.get("before") or "", claim.get("after") or "", claim.get("captions") or ""
        voices = [str(v).strip() for v in claim.get("voices") or [] if str(v).strip()]
        asserted_at = datetime.fromisoformat(claim["asserted_at"]) if claim.get("asserted_at") else datetime.now(timezone.utc)
        day = (asserted_at if asserted_at.tzinfo else asserted_at.replace(tzinfo=timezone.utc)).astimezone(PARIS).date()
        order = claim.get("order") if claim.get("order") in ORDERS else CHECK_ORDER if CHECK_ORDER in ORDERS else "google_first"
        if not LINKUP_KEY:
            order = "google_first"                       # no Linkup: Gemini's own search is all there is
        record["order"] = order

        # the check prompt, filled field by field in this order
        prompt = prompts.CHECK
        for name, value in (
                ("captions", compact(captions, CAPTION_CHARS) if captions.strip() else "—"),
                ("voices", ", ".join(voices) or "Liste des voix identifiables non disponible."),
                ("source_list", SOURCE_LIST),
                ("speaker", speaker or "l'orateur"),
                ("quote", quote or "—"),
                ("sentence", claim.get("sentence") or quote or "—"),
                ("claim", text),
                ("meant", meant or "(pas d'autre sens que le sens littéral)"),
                ("after", compact(after, AFTER_CHARS) if after.strip() else "—"),
                ("before", compact(before, BEFORE_CHARS, keep_end=True) if before.strip() else "—"),
                ("context", compact(claim.get("context") or "", CONTEXT_CHARS)),
                ("channel", claim.get("channel") or "inconnue"),
                ("when", f"{day.day} {MONTHS[day.month - 1]} {day.year}")):         # "aujourd'hui" is judged against this day
            prompt = prompt.replace("{" + name + "}", str(value))

        async def find_pages() -> tuple[list[dict], str]:
            """Linkup's pages for this claim, best tier first, and their numbered list for the prompt."""
            # the query: an instruction written by the small model; the claim itself when it cannot be written
            query, from_date, written_by, query_error, linkup_error = text, None, "claim", None, None
            began = time.perf_counter()
            try:
                answer = await gemini(prompts.QUERY.format(speaker=speaker or "l'orateur", when=asserted_at.date().isoformat(),
                                                           claim=text, meant=meant or "(le sens littéral)", quote=quote or "—"),
                                      QUERY_MODEL)
            except Exception as exc:                                      # a better query is a bonus, never a failure
                log.warning("Linkup query not written (%r): searching with the claim itself", exc)
                query_error, answer = repr(exc)[:200], None
            payload = answer["payload"] if answer and isinstance(answer["payload"], dict) else {}
            written = " ".join(str(payload.get("query") or "").split())[:QUERY_CHARS]
            if len(written) >= 20:
                query, written_by = written, "llm"
                # from_date: a real day before the claim, at most ten years back (the claim is about the present)
                since_day = str(payload.get("from_date") or "").strip()
                if isinstance(payload.get("from_date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_day):
                    try:
                        since = date.fromisoformat(since_day)
                    except ValueError:
                        since = None
                    if since and asserted_at.date() - timedelta(days=3650) <= since < asserted_at.date():
                        from_date = since.isoformat()
            query_seconds = round(time.perf_counter() - began, 2)
            body = {"q": query, "depth": "standard", "outputType": "searchResults", "maxResults": LINKUP_RESULTS,
                    "excludeDomains": LINKUP_EXCLUDE}
            if from_date:
                body["fromDate"] = from_date
            began = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=20) as http:
                    r = await http.post(LINKUP_URL, headers={"Authorization": f"Bearer {LINKUP_KEY}"}, json=body)
                    r.raise_for_status()
                    results = r.json().get("results") or []
            except (httpx.HTTPError, ValueError) as exc:                  # a failed search is just no extra evidence
                log.warning("Linkup search failed: %r", exc)
                linkup_error, results = repr(exc)[:200], []
            pages = [{"title": str(item.get("name") or item.get("url") or "").strip(), "url": str(item.get("url") or ""),
                      "content": " ".join(str(item.get("content") or "").split())[:SNIPPET_CHARS]}
                     for item in results
                     if item.get("type", "text") == "text" and urlparse(str(item.get("url") or "")).scheme in ("http", "https")]
            pages = sorted(pages, key=lambda p: tier(p["url"]))[:LINKUP_PAGES]
            log.info("%s: Linkup found %d pages (query by %s)", said, len(pages), written_by)
            record["linkup"] = {"query": query, "from_date": from_date, "written_by": written_by, "query_error": query_error,
                                "query_seconds": query_seconds, "seconds": round(time.perf_counter() - began, 2),
                                "failed": linkup_error, "pages": [{"url": p["url"], "tier": tier(p["url"]), "title": p["title"]} for p in pages]}
            listing = "\n".join(f"{i}. [{TIER_LABELS[tier(p['url'])]}] {p['title']}\n   {p['url']}\n   {p['content']}"
                                for i, p in enumerate(pages, 1))
            return pages, listing

        async def attempt(label: str, full_prompt: str, thinking: str | None, pages: list[dict]) -> tuple[dict, list[dict]] | None:
            """One Gemini call with Google Search (and URL reading when there are pages): (answer, sources), None if it failed.
            The sources: the listed pages it cites (by number, or by URL however spelled; never a page we were not given),
            then, only if it really searched, the URLs it found itself, then its grounding links."""
            began = time.perf_counter()
            try:
                result = await gemini(full_prompt, MODEL, search=True, url_context=bool(pages), thinking=thinking)
            except Exception as exc:
                log.exception("%s: the check failed (%s, thinking %s)", said, label, thinking)
                attempts.append({"call": label, "thinking": thinking, "seconds": round(time.perf_counter() - began, 2),
                                 "failed": str(exc)[:200]})
                return None
            payload = result["payload"]
            if not isinstance(payload, dict):
                attempts.append({"call": label, "thinking": thinking, "seconds": round(time.perf_counter() - began, 2),
                                 "failed": "pas un objet JSON"})
                return None
            listed, cited = {same_page(p["url"]): p for p in pages}, []
            for s in payload.get("sources") or []:
                if not isinstance(s, dict):
                    continue
                n = s.get("page")
                page = (pages[n - 1] if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(pages)
                        else listed.get(same_page(str(s.get("url") or ""))))
                if page and page["url"] not in {c["url"] for c in cited}:
                    cited.append({"title": page["title"], "url": page["url"], "says": s.get("says") or ""})
            have = {same_page(c["url"]) for c in cited}
            written = [w for w in payload.get("sources") or []
                       if isinstance(w, dict) and same_page(str(w.get("url") or "")) not in have] if result["searched"] else []
            merged = merge_sources(cited + written, result["grounding"])
            attempts.append(describe(label, thinking, round(time.perf_counter() - began, 2), result["searched"], payload, merged))
            return payload, merged

        payload, sources, winner = None, [], None
        if order == "linkup_first":
            pages, listing = await find_pages()
            full_prompt = prompt + prompts.PAGES_FIRST.format(pages=listing) if pages else prompt
            for n, thinking in enumerate((None, "high"), 1):
                answer = await attempt(f"gemini_{n}", full_prompt, thinking, pages)
                if answer:
                    payload, sources, winner = *answer, f"gemini_{n}"
                    if any(usable(s["url"]) for s in sources):
                        break
                    log.info("%s: no usable source (linkup_first, thinking %s)", said, thinking)
        else:
            # 1) Gemini + Google Search. Without a real search, the URLs it writes are from memory: not sources.
            began = time.perf_counter()
            result = await gemini(prompt, MODEL, search=True)
            payload, winner = result["payload"], "gemini_1"
            if not isinstance(payload, dict):
                attempts.append({"call": "gemini_1", "thinking": None, "seconds": round(time.perf_counter() - began, 2),
                                 "failed": "pas un objet JSON"})
                raise RuntimeError("Le fact-check n'a pas renvoyé un objet JSON")
            sources = merge_sources(payload.get("sources") or [] if result["searched"] else [], result["grounding"])
            attempts.append(describe("gemini_1", None, round(time.perf_counter() - began, 2), result["searched"], payload, sources))
            # 2) no usable link: Linkup's pages, then Gemini again with its first answer and those pages, then once more thinking
            #    harder; with no pages at all, once more thinking harder on the plain prompt.
            if LINKUP_KEY and not any(usable(s["url"]) for s in sources):
                pages, listing = await find_pages()
                previous = {k: payload.get(k) for k in ("verdict", "explanation", "confidence")}
                informed = prompt + prompts.LINKUP.format(previous=json.dumps(previous, ensure_ascii=False), pages=listing)
                for n, (full_prompt, thinking) in enumerate([(informed, None), (informed, "high")] if pages else [(prompt, "high")], 2):
                    answer = await attempt(f"gemini_{n}", full_prompt, thinking, pages)
                    if not answer:
                        continue
                    payload, sources, winner = *answer, f"gemini_{n}"
                    if any(usable(s["url"]) for s in sources):
                        break
                    log.info("%s: no usable source after the Linkup attempt (thinking %s)", said, thinking)
        if payload is None:
            raise RuntimeError("Le fact-check n'a pas abouti (aucune réponse exploitable)")

        # 3) still nothing usable: no source at all, never a homepage, never an invented link
        sources = [s for s in sources if usable(s["url"])]

        raw = str(payload.get("verdict") or "insuffisant").strip().lower().replace("-", "_").replace(" ", "_")
        verdict = raw if raw in VERDICTS else VERDICT_ALIASES.get(raw.replace("_", " "), VERDICT_ALIASES.get(raw, "insuffisant"))
        confidence = payload.get("confidence")
        if isinstance(confidence, str):
            confidence = confidence.replace("%", "").replace(",", ".").strip()
        try:
            confidence = float(confidence) if confidence not in (None, "") else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence / 100 if confidence > 1 else confidence))
        own, faithful, scope_ok = (flag(payload.get(k)) for k in ("own", "faithful", "scope_ok"))
        basis = str(payload.get("basis") or "").strip().lower() or None
        evidence = str(payload.get("evidence") or "").strip().lower() or None

        # the checker's own flags can only lower a verdict, and a missing flag is not a yes ("insuffisant" needs none)
        asked, guard, calibration = verdict, None, None
        if verdict != "insuffisant":
            if own is not True:
                guard = "not_own_assertion" if own is False else "own_missing"
            elif faithful is not True:
                guard = "not_faithful" if faithful is False else "faithful_missing"
            elif scope_ok is not True:
                guard = "scope_not_meant" if scope_ok is False else "scope_missing"
            elif basis != "external_facts":
                guard = f"basis:{basis or 'missing'}"
        if guard:
            log.info("%s: verdict %s forced to insuffisant (%s)", said, verdict, guard)
            verdict = "insuffisant"
        elif verdict != "insuffisant":
            # what the verdict rests on decides how far it can go: not finding is not refuting, an inference is one notch lower
            if evidence == "absence":
                verdict, guard = "insuffisant", "evidence:absence"
            elif verdict in NEGATIVE and evidence not in ("direct", "inference"):
                verdict, guard = "insuffisant", f"evidence:{evidence or 'missing'}"
            elif verdict == "faux" and evidence == "inference":
                verdict, calibration = "plutot_faux", "faux->plutot_faux (evidence: inference)"
            if verdict != asked:
                log.info("%s: verdict %s -> %s (%s)", said, asked, verdict, guard or calibration)

        record["decision"] = {"winner": winner, "asked": asked, "verdict": verdict, "confidence": confidence, "guard": guard,
                              "calibration": calibration, "own": own, "faithful": faithful, "scope_ok": scope_ok, "basis": basis,
                              "evidence": evidence, "explanation": str(payload.get("explanation") or "").strip(),
                              "sources": [{"url": s["url"], "tier": s["tier"], "says": s["says"]} for s in sources]}
        log.info("%s: %s (confidence %.2f), %d source(s), %d call(s), %.1f s", said, verdict, confidence, len(sources), len(attempts),
                 time.perf_counter() - started)
        return {
            "verdict": verdict,
            "explanation": str(payload.get("explanation") or "").strip(),
            "confidence": confidence,
            "meant": str(payload.get("meant") or meant or "").strip(),
            "guard": guard,
            "sources": sources,
            "attempts": [{k: a[k] for k in LEAN if k in a} for a in attempts],
        }
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        raise
    finally:
        if CHECK_LOG:                                  # a full disk or a bad path must never stop a check
            record["seconds"] = round(time.perf_counter() - started, 2)
            try:
                os.makedirs(CHECK_LOG, exist_ok=True)
                with open(os.path.join(CHECK_LOG, f"checks-{datetime.now(PARIS).date().isoformat()}.jsonl"), "a", encoding="utf-8") as out:
                    out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                log.warning("check journal not written: %r", exc)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
