"""electrocheck-engine, with Gemini and Linkup replaced by scripted answers: where the sources come from, how the verdict is
parsed, and what the code does with the checker's own flags."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine  # noqa: E402

HOMEPAGE = [{"title": "Sénat", "url": "https://www.senat.fr"}]                 # what the model writes from memory
FLAGS = {"own": True, "faithful": True, "scope_ok": True, "basis": "external_facts", "evidence": "direct"}
FIRST = {"verdict": "faux", "explanation": "de mémoire", "confidence": 0.6, "sources": HOMEPAGE, **FLAGS}
PAGES = [{"type": "text", "name": "Franceinfo", "url": "https://www.franceinfo.fr/a-1.html", "content": "texte un"},
         {"type": "text", "name": "Le Monde", "url": "https://www.lemonde.fr/b-é2.html", "content": "texte deux"}]
GROUNDING = [{"title": "g", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X"}]
CLAIM = {"id": "c1", "claim": "Le chômage a baissé.", "context": "le chômage a baissé", "asserted_at": "2026-10-01T20:00:00+00:00"}


def informed(sources, verdict="plutot_faux"):
    return {"verdict": verdict, "explanation": "lu sur les pages", "confidence": 0.9, "sources": sources, **FLAGS}


def reply(payload, grounding=(), tokens=(100, 50), queries=0, text=None):
    """A Gemini response as the SDK returns it."""
    meta = SimpleNamespace(web_search_queries=[f"q{i}" for i in range(queries)],
                           grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri=g["url"], title=g["title"])) for g in grounding])
    return SimpleNamespace(text=json.dumps(payload) if text is None else text, candidates=[SimpleNamespace(grounding_metadata=meta)],
                           usage_metadata=SimpleNamespace(prompt_token_count=tokens[0], tool_use_prompt_token_count=0,
                                                          candidates_token_count=tokens[1], thoughts_token_count=0))


class Gemini:
    """Scripted answers: the check model's in order, the query model's always the same."""

    def __init__(self, *checks, query=None):
        self.checks, self.query, self.calls, self.query_prompts = list(checks), query, [], []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self.generate_content))

    async def generate_content(self, *, model, contents, config):
        if model == engine.QUERY_MODEL:
            self.query_prompts.append(contents)
            if isinstance(self.query, Exception):
                raise self.query
            return reply(self.query or {"query": ""})
        tools = config.tools or []
        self.calls.append({"prompt": contents, "search": bool(tools), "url_context": len(tools) == 2,
                           "thinking": level(config)})
        outcome = self.checks.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def level(config):
    """The thinking level a call asked for, "high" or None."""
    return str(getattr(config.thinking_config.thinking_level, "value", config.thinking_config.thinking_level)).lower() if config.thinking_config else None


@pytest.fixture
def run(monkeypatch):
    """run(gemini, pages=None, linkup_key="k", **claim) -> (status, answer, Linkup request bodies)."""
    def go(fake, pages=None, linkup_key="k", check_order="google_first", linkup_status=200, **fields):
        searched = []

        async def post(self, url, **kw):
            searched.append(kw["json"])
            return httpx.Response(linkup_status, json={"results": pages or []}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        monkeypatch.setattr(engine, "client", fake)
        monkeypatch.setattr(engine, "LINKUP_KEY", linkup_key)
        monkeypatch.setattr(engine, "CHECK_ORDER", check_order)
        response = TestClient(engine.app, raise_server_exceptions=False).post("/check", json={**CLAIM, **fields})
        return response.status_code, response.json(), searched
    return go


def urls(answer):
    return [s["url"] for s in answer["sources"]]


# ---------------------------------------------------------------- google_first: Gemini, then Linkup, then none

def test_gemini_that_searched_gives_its_links_and_linkup_is_not_called(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=2))
    _, answer, searched = run(g, pages=PAGES)
    assert len(g.calls) == 1 and searched == [] and answer["linkup_searches"] == 0
    assert urls(answer) == [GROUNDING[0]["url"]]                                  # the homepage the model wrote is gone


def test_no_usable_link_goes_through_linkup_and_pages_are_cited_by_number_or_url(run):
    cited = informed([{"page": 1, "title": "x"},                                              # by number
                      {"title": "Monde", "url": "https://lemonde.fr/b-%C3%A92.html/"},         # by URL, spelled differently
                      {"title": "inventée", "url": "https://www.invented.example/page"},       # not in the list: dropped
                      {"page": 99}, {"page": 0}, {"page": True}])                              # out of range: ignored
    g = Gemini(reply(FIRST), reply(cited, tokens=(400, 120)))
    _, answer, searched = run(g, pages=PAGES)
    assert [b["q"] for b in searched] == ["Le chômage a baissé."] and answer["linkup_searches"] == 1 and len(g.calls) == 2
    first_call, second_call = g.calls
    assert second_call["search"] and second_call["url_context"] and second_call["thinking"] is None and not first_call["url_context"]
    assert "de mémoire" in second_call["prompt"] and "1. [grand média] Franceinfo" in second_call["prompt"]
    assert "https://www.lemonde.fr/b-é2.html" in second_call["prompt"]
    assert urls(answer) == ["https://www.franceinfo.fr/a-1.html", "https://www.lemonde.fr/b-é2.html"]
    assert answer["verdict"] == "plutot_faux" and answer["explanation"] == "lu sur les pages"     # the informed answer wins
    assert (answer["input_tokens"], answer["output_tokens"]) == (500, 170)                       # both calls are counted


def test_no_page_cited_means_one_more_try_thinking_harder(run):
    g = Gemini(reply(FIRST), reply(informed([{"title": "x", "url": "https://x.example/p"}])),
               reply(informed([{"page": 2}]), tokens=(300, 900)))
    _, answer, _ = run(g, pages=PAGES)
    assert [c["thinking"] for c in g.calls] == [None, None, "high"] and g.calls[1]["prompt"] == g.calls[2]["prompt"]
    assert urls(answer) == ["https://www.lemonde.fr/b-é2.html"] and answer["input_tokens"] == 500
    assert answer["attempts"] == [                                                  # every answer is kept, the replaced ones too
        {"call": "gemini_1", "thinking": None, "verdict": "faux", "confidence": 0.6, "usable_sources": 0},
        {"call": "gemini_2", "thinking": None, "verdict": "plutot_faux", "confidence": 0.9, "usable_sources": 0},
        {"call": "gemini_3", "thinking": "high", "verdict": "plutot_faux", "confidence": 0.9, "usable_sources": 1}]


def test_nothing_cited_even_thinking_harder_leaves_no_source(run):
    nothing = informed([{"title": "x", "url": "https://x.example/p"}])
    g = Gemini(reply(FIRST), reply(nothing), reply(nothing))
    _, answer, _ = run(g, pages=PAGES)
    assert len(g.calls) == 3 and answer["sources"] == [] and answer["explanation"] == "lu sur les pages"


def test_urls_the_model_found_by_its_own_search_still_count_without_duplicating_a_cited_page(run):
    answer_ = informed([{"page": 2}, {"title": "Monde", "url": "https://lemonde.fr/b-%C3%A92.html"},
                        {"title": "x", "url": "https://x.example/p"}])
    g = Gemini(reply(FIRST), reply(answer_, queries=3))
    _, answer, _ = run(g, pages=PAGES)
    assert len(g.calls) == 2 and answer["search_queries"] == 3
    assert urls(answer) == ["https://www.lemonde.fr/b-é2.html", "https://x.example/p"]


def test_without_a_linkup_key_unsearched_urls_are_dropped_and_there_is_no_second_call(run):
    g = Gemini(reply(FIRST))
    _, answer, searched = run(g, pages=PAGES, linkup_key="", check_order="linkup_first")
    assert len(g.calls) == 1 and searched == [] and answer["sources"] == [] and answer["verdict"] == "faux"
    assert answer["check_order"] == "google_first"


def test_linkup_finding_nothing_still_tries_once_more_thinking_harder_on_the_plain_prompt(run):
    g = Gemini(reply(FIRST), reply(FIRST, GROUNDING, queries=2))
    _, answer, searched = run(g, pages=[])
    assert len(searched) == 1 and [c["thinking"] for c in g.calls] == [None, "high"]
    assert "Pages trouvées" not in g.calls[1]["prompt"] and urls(answer) == [GROUNDING[0]["url"]]


def test_failed_attempts_fall_through_and_keep_the_first_answer(run):
    g = Gemini(reply(FIRST), RuntimeError("quota"), RuntimeError("quota"))
    _, answer, _ = run(g, pages=PAGES)
    assert len(g.calls) == 3 and answer["sources"] == [] and answer["explanation"] == "de mémoire" and answer["input_tokens"] == 100
    assert set(answer["timings"]) == {"gemini_1", "query", "linkup", "gemini_2", "gemini_3"}       # a failed call is timed too
    assert [a.get("failed") for a in answer["attempts"]] == [None, "quota", "quota"]


def test_a_first_answer_that_is_not_an_object_is_an_error(run):
    status, answer, _ = run(Gemini(reply(["pas", "un objet"])))
    assert status == 500 and answer == {"error": "Le fact-check n'a pas renvoyé un objet JSON"}


# ---------------------------------------------------------------- linkup_first: Linkup's pages from the start

FAURE = {"claim": "Aujourd'hui, l'Europe est seule à financer l'Ukraine.", "speaker": "Olivier Faure",
         "quote": "Aujourd'hui nous sommes les seuls à financer l'effort ukrainien", "asserted_at": "2026-10-04T20:00:00+00:00"}
QUERY = {"query": "Trouve les données du Kiel Institute sur l'aide à l'Ukraine par pays en 2026 : montants, période, URL.",
         "from_date": "2025-10-01"}


def test_linkup_first_searches_with_the_written_query_and_asks_gemini_once(run):
    g = Gemini(reply(informed([{"page": 1}]), tokens=(400, 120), queries=1), query=QUERY)
    _, answer, searched = run(g, pages=PAGES, check_order="linkup_first", **FAURE)
    assert [(b["q"], b.get("fromDate")) for b in searched] == [(QUERY["query"], "2025-10-01")] and len(g.calls) == 1
    call = g.calls[0]
    assert call["search"] and call["url_context"] and call["thinking"] is None
    assert "Première analyse" not in call["prompt"] and "1. [grand média] Franceinfo" in call["prompt"] and "source primaire" in call["prompt"]
    assert urls(answer) == ["https://www.franceinfo.fr/a-1.html"]
    assert (answer["check_order"], answer["linkup_query"], answer["linkup_from_date"]) == ("linkup_first", QUERY["query"], "2025-10-01")
    assert set(answer["timings"]) == {"query", "linkup", "gemini_1"} and answer["input_tokens"] == 400     # the query's tokens apart


def test_linkup_first_tries_once_more_thinking_harder_when_no_page_is_cited(run):
    g = Gemini(reply(informed([{"title": "x", "url": "https://x.example/p"}])), reply(informed([{"page": 2}]), tokens=(300, 900)))
    _, answer, _ = run(g, pages=PAGES, check_order="linkup_first", **FAURE)
    assert [c["thinking"] for c in g.calls] == [None, "high"] and g.calls[0]["prompt"] == g.calls[1]["prompt"]
    assert urls(answer) == ["https://www.lemonde.fr/b-é2.html"] and answer["input_tokens"] == 400
    assert [(a["call"], a["thinking"], a["usable_sources"]) for a in answer["attempts"]] == [("gemini_1", None, 0), ("gemini_2", "high", 1)]


def test_linkup_first_with_no_page_falls_back_to_gemini_alone(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=2))
    _, answer, searched = run(g, pages=[], check_order="linkup_first", **FAURE)
    assert len(searched) == 1 and len(g.calls) == 1 and "Pages trouvées" not in g.calls[0]["prompt"]
    assert not g.calls[0]["url_context"] and urls(answer) == [GROUNDING[0]["url"]]


def test_linkup_first_failing_twice_is_an_error_not_a_verdict(run):
    status, answer, _ = run(Gemini(RuntimeError("quota"), RuntimeError("quota")), pages=PAGES, check_order="linkup_first", **FAURE)
    assert status == 500 and answer == {"error": "Le fact-check n'a pas abouti (aucune réponse exploitable)"}


def test_the_order_asked_in_the_request_wins_over_the_setting(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=2))
    _, answer, searched = run(g, pages=PAGES, check_order="google_first", **{**FAURE, "order": "linkup_first"})
    assert answer["check_order"] == "linkup_first" and len(searched) == 1


# ---------------------------------------------------------------- the Linkup query and the search request

def test_the_query_is_written_by_the_small_model_and_falls_back_to_the_claim(run):
    good = {"query": "Trouve la facture pétrolière annuelle de la France (SDES, douanes) : montant, année, URL.", "from_date": None}
    g = Gemini(reply(FIRST, GROUNDING, queries=1), query=good)
    _, answer, searched = run(g, check_order="linkup_first", **{**FAURE, "speaker": "Raphaël Glucksmann"})
    assert searched[0]["q"] == good["query"] and "fromDate" not in searched[0] and answer["linkup_from_date"] is None
    assert "Raphaël Glucksmann" in g.query_prompts[0] and "AAAA-MM-JJ" in g.query_prompts[0] and "2026-10-04" in g.query_prompts[0]
    for query in (RuntimeError("down"), {"query": "trop court"}):
        _, answer, searched = run(Gemini(reply(FIRST, GROUNDING, queries=1), query=query), check_order="linkup_first", **FAURE)
        assert searched[0]["q"] == FAURE["claim"] == answer["linkup_query"]
    for bad in ("2026-10-05", "2015-01-01", "hier", "2026-02-30"):                      # after the claim, too old, not a date
        _, answer, searched = run(Gemini(reply(FIRST, GROUNDING, queries=1), query={**good, "from_date": bad}), check_order="linkup_first", **FAURE)
        assert answer["linkup_from_date"] is None and "fromDate" not in searched[0]


def test_the_search_excludes_social_networks_and_keeps_text_pages_with_real_urls(run):
    results = [{"type": "text", "name": f"p{i}", "url": f"https://a.example/{i}", "content": "x " * 900} for i in range(16)]
    results += [{"type": "image", "name": "i", "url": "https://a.example/i.png"}, {"type": "text", "name": "bad", "url": "javascript:x"}]
    g = Gemini(reply(informed([{"page": 12}])), query=QUERY)
    _, answer, searched = run(g, pages=results, check_order="linkup_first", **FAURE)
    body = searched[0]
    assert body["depth"] == "standard" and body["outputType"] == "searchResults" and body["maxResults"] == 15
    assert "youtube.com" in body["excludeDomains"]
    prompt = g.calls[0]["prompt"]
    assert "12. [autre source] p11" in prompt and "13. [" not in prompt
    assert "x " * 299 in prompt and "x " * 301 not in prompt                     # 600 characters of each page
    assert urls(answer) == ["https://a.example/11"]


def test_a_failed_search_is_no_evidence_not_an_error(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=1), query=QUERY)
    status, answer, _ = run(g, pages=PAGES, check_order="linkup_first", linkup_status=500, **FAURE)
    assert status == 200 and answer["linkup_searches"] == 1 and "Pages trouvées" not in g.calls[0]["prompt"]


def test_linkup_pages_come_best_tier_first_and_say_which_tier_they_are(run):
    results = [{"type": "text", "name": n, "url": u, "content": "c"} for n, u in (
        ("Blog", "https://monblog.example/a"), ("Le Monde", "https://www.lemonde.fr/b"), ("Insee", "https://www.insee.fr/fr/statistiques/1"),
        ("Drees", "https://drees.solidarites-sante.gouv.fr/x"), ("Autre", "https://autre.example/c"))]
    g = Gemini(reply(informed([{"page": 1}])), query=QUERY)
    run(g, pages=results, check_order="linkup_first", **FAURE)
    prompt = g.calls[0]["prompt"]
    assert "1. [source primaire] Insee" in prompt and "2. [source primaire] Drees" in prompt
    assert "3. [grand média] Le Monde" in prompt and "4. [autre source] Blog" in prompt and "5. [autre source] Autre" in prompt


def test_every_source_carries_its_tier_and_what_it_says(run):
    assert engine.tier("https://www.insee.fr/x") == 1 and engine.tier("https://ourworldindata.org/x") == 1
    assert engine.tier("https://x.gouv.fr/y") == 1 and engine.tier("https://www.lemonde.fr/x") == 2 and engine.tier("https://monblog.example/x") == 3
    assert engine.tier("https://vertexaisearch.cloud.google.com/grounding-api-redirect/X", "insee.fr") == 1   # grounding: the site is the title
    answer_ = informed([{"page": 1, "says": "le chômage est à 7,3 %"},
                        {"title": "Insee", "url": "https://www.insee.fr/fr/stat/2", "says": "7,3 % au 2e trimestre"}])
    _, answer, _ = run(Gemini(reply(FIRST), reply(answer_, queries=2)), pages=PAGES)
    assert [(s["domain"], s["tier"], s["says"]) for s in answer["sources"]] == [("insee.fr", 1, "7,3 % au 2e trimestre"),
                                                                                 ("franceinfo.fr", 2, "le chômage est à 7,3 %")]


# ---------------------------------------------------------------- the checker's flags and what the verdict rests on

@pytest.mark.parametrize("flags, guard", [
    ({"own": False}, "not_own_assertion"),
    ({"own": None}, "own_missing"),
    ({"faithful": False}, "not_faithful"),
    ({"faithful": "oui"}, None),                                       # a string yes is a yes
    ({"faithful": "peut-être"}, "faithful_missing"),
    ({"scope_ok": False}, "scope_not_meant"),
    ({"scope_ok": None}, "scope_missing"),
    ({"basis": "speaker_statements"}, "basis:speaker_statements"),
    ({"basis": "none"}, "basis:none"),
    ({"basis": None}, "basis:missing"),
])
def test_the_flags_force_insuffisant_and_a_missing_flag_is_not_a_yes(run, flags, guard):
    payload = {k: v for k, v in {**FIRST, **flags}.items() if v is not None}
    _, answer, _ = run(Gemini(reply(payload, GROUNDING, queries=2)))
    assert answer["guard"] == guard and answer["verdict"] == ("faux" if guard is None else "insuffisant")
    if guard:
        assert answer["review_status"] == "needs_review"


def test_an_insuffisant_verdict_needs_no_flags_and_is_not_blamed(run):
    _, answer, _ = run(Gemini(reply({"verdict": "insuffisant", "explanation": "x", "confidence": 0.2, "sources": []}, GROUNDING, queries=1)))
    assert answer["verdict"] == "insuffisant" and answer["guard"] is None


@pytest.mark.parametrize("verdict, evidence, expected, guard, calibration", [
    ("faux", "direct", "faux", None, None),
    ("faux", "inference", "plutot_faux", None, "faux->plutot_faux (evidence: inference)"),   # one notch lower
    ("faux", "absence", "insuffisant", "evidence:absence", None),                          # not found is not refuted
    ("plutot_faux", "absence", "insuffisant", "evidence:absence", None),
    ("mixte", None, "insuffisant", "evidence:missing", None),                              # a negative needs its basis
    ("plutot_faux", "inference", "plutot_faux", None, None),
    ("vrai", None, "vrai", None, None),                                                    # a positive wrongs nobody
    ("vrai", "absence", "insuffisant", "evidence:absence", None),
])
def test_what_a_verdict_rests_on_decides_how_far_it_can_go(run, verdict, evidence, expected, guard, calibration):
    payload = {k: v for k, v in {**FIRST, "verdict": verdict, "evidence": evidence}.items() if v is not None}
    _, answer, _ = run(Gemini(reply(payload, GROUNDING, queries=2)))
    assert (answer["verdict"], answer["guard"], answer["calibration"], answer["evidence"]) == (expected, guard, calibration, evidence)


@pytest.mark.parametrize("written, verdict", [("Plutôt vrai", "plutot_vrai"), ("mostly-false", "plutot_faux"), ("???", "insuffisant")])
def test_the_verdict_is_read_however_the_model_writes_it(run, written, verdict):
    _, answer, _ = run(Gemini(reply({**FIRST, "verdict": written}, GROUNDING, queries=1)))
    assert answer["verdict"] == verdict


@pytest.mark.parametrize("written, confidence, status", [("72%", 0.72, "pending"), (0.4, 0.4, "needs_review"), (85, 0.85, "pending"),
                                                         ("", 0.0, "needs_review"), ("haute", 0.0, "needs_review")])
def test_the_confidence_is_read_as_a_fraction(run, written, confidence, status):
    _, answer, _ = run(Gemini(reply({**FIRST, "confidence": written}, GROUNDING, queries=1)))
    assert (answer["confidence"], answer["review_status"]) == (confidence, status)


# ---------------------------------------------------------------- what the check reads

def test_the_prompt_carries_the_speaker_the_quote_the_sentence_the_sense_and_what_came_after(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=1))
    run(g, linkup_key="", speaker="Olivier Faure", quote="Je n'ai jamais parlé d'alliance avec Jean-Luc Mélenchon",
        sentence="Je n'ai jamais parlé d'alliance avec Jean-Luc Mélenchon, mais c'est le débat qui s'installe.",
        meant="pas dans ce débat", after="[Animateur] C'est vous qui l'avez mis sur la table. [Olivier Faure] Je n'en ai pas parlé.",
        captions="Je n'ai jamais parlé d'alliance", voices=["Olivier Faure", " "], channel="France 2")
    prompt = g.calls[0]["prompt"]
    assert "Propos exacts de Olivier Faure : « Je n'ai jamais parlé d'alliance avec Jean-Luc Mélenchon »" in prompt
    assert "mais c'est le débat qui s'installe" in prompt and "pas dans ce débat" in prompt and "Je n'en ai pas parlé." in prompt
    assert "Voix identifiables : Olivier Faure\n" in prompt and "Chaîne : France 2" in prompt and "Heure : 22:00" in prompt
    assert "Sous-titres CC : Je n'ai jamais parlé d'alliance" in prompt and "- INSEE (insee.fr) — https://www.insee.fr" in prompt
    assert "{" not in prompt.split("Réponds UNIQUEMENT")[0]


def test_missing_fields_take_their_defaults(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=1))
    _, answer, _ = run(g, linkup_key="")
    prompt = g.calls[0]["prompt"]
    assert "fact-checker politique. Tu vérifies une affirmation de l'orateur" in prompt and "Propos exacts de l'orateur : « — »" in prompt
    assert "(pas d'autre sens que le sens littéral)" in prompt and "Liste des voix identifiables non disponible." in prompt
    assert "Chaîne : inconnue" in prompt and answer["meant"] == ""


def test_long_context_is_cut_at_a_word(run):
    g = Gemini(reply(FIRST, GROUNDING, queries=1))
    run(g, linkup_key="", context="mot " * 300, after="  ")
    assert "Contexte compact : " + ("mot " * 129).strip() + "…\n" in g.calls[0]["prompt"]
    assert "Dit juste après (contexte seulement, n'en tire aucune autre affirmation) : —" in g.calls[0]["prompt"]


# ---------------------------------------------------------------- Gemini's answers

def test_search_queries_are_counted_once_each_and_chunks_without_queries_count_one(monkeypatch):
    meta = SimpleNamespace(web_search_queries=["chômage France 2026", "", "chômage France 2026", "INSEE chômage"], grounding_chunks=[])
    with_queries = SimpleNamespace(text='{"a": 1}', candidates=[SimpleNamespace(grounding_metadata=meta)], usage_metadata=None)
    chunks_only = reply({"a": 1}, GROUNDING)
    nothing = SimpleNamespace(text='{"a": 1}', candidates=[], usage_metadata=None)
    results = []
    for response in (with_queries, chunks_only, nothing):
        monkeypatch.setattr(engine, "client", SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda r=response, **kw: _now(r)))))
        results.append(_run_gemini(search=True)["searches"])
    assert results == [2, 1, 0]


async def _now(value):
    return value


def _run_gemini(**kw):
    import asyncio
    return asyncio.run(engine.gemini("p", "m", **kw))


GOOD = '{"query": "La CIA n\'a pas informé les services français."}'
STRAY = '{\n  "query": "a"\n  >]}'                                   # a real glitch of a JSON-only answer


def _scripted(monkeypatch, *texts):
    calls = []

    async def generate_content(**kwargs):
        calls.append(kwargs)
        return reply(None, text=texts[len(calls) - 1], tokens=(10, 5))
    monkeypatch.setattr(engine, "client", SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))))
    return calls


def test_a_broken_json_only_answer_is_asked_again_and_both_are_counted(monkeypatch):
    calls = _scripted(monkeypatch, STRAY, GOOD)
    result = _run_gemini()
    assert len(calls) == 2 and result["payload"]["query"].startswith("La CIA") and (result["input_tokens"], result["output_tokens"]) == (20, 10)
    assert calls[0]["config"].response_mime_type == "application/json" and not calls[0]["config"].tools


def test_an_empty_json_only_answer_is_asked_again_and_two_bad_ones_give_up(monkeypatch):
    calls = _scripted(monkeypatch, "", GOOD)
    assert _run_gemini()["payload"] and len(calls) == 2
    calls = _scripted(monkeypatch, STRAY, STRAY)
    with pytest.raises(ValueError):
        _run_gemini()
    assert len(calls) == 2


def test_a_search_answer_that_is_not_json_is_rewritten_as_json(monkeypatch):
    calls = _scripted(monkeypatch, "Voici mon analyse : verdict faux, sans JSON.", '{"verdict": "faux"}')
    result = _run_gemini(search=True, url_context=True, thinking="high")
    assert result["payload"] == {"verdict": "faux"} and result["input_tokens"] == 20
    assert len(calls[0]["config"].tools) == 2 and level(calls[0]["config"]) == "high"
    assert calls[1]["contents"].startswith("Réécris le texte suivant en JSON strict") and calls[1]["config"].temperature == 0


def test_json_inside_a_fence_or_prose_is_read():
    assert engine.parse_json('```json\n{"a": 1}\n```') == {"a": 1} and engine.parse_json('Voici : {"a": 1} fin') == {"a": 1}


# ---------------------------------------------------------------- the service

def test_the_page_is_served_and_a_request_without_a_claim_or_a_key_is_refused(monkeypatch):
    web = TestClient(engine.app, raise_server_exceptions=False)
    assert web.get("/").status_code == 200 and "<form" in web.get("/").text
    monkeypatch.setattr(engine, "client", Gemini())
    assert web.post("/check", json={"claim": " "}).json() == {"error": "claim manquant"}
    monkeypatch.setattr(engine, "client", None)
    assert web.post("/check", json=CLAIM).json() == {"error": "Clé Gemini absente : renseigner API_GEMINI dans .env"}
