"""The prompts of electrocheck-engine, in French: the engine checks what is said on French television.

CHECK is filled by plain replacement of its {name} fields. LINKUP, PAGES_FIRST and QUERY go through str.format
(hence the doubled braces).
"""

# The check itself (Gemini with Google Search): one claim, its exact words, the sense meant, the rules of the verdict.
CHECK = """Tu es un fact-checker politique. Tu vérifies une affirmation de {speaker}, prononcée à la télévision
française. Utilise la recherche web (Google Search).

Cherche EN PRIORITÉ sur ces sources primaires / institutionnelles :
{source_list}

Ordre de préférence des sources : 1) sources primaires (statistiques et textes officiels, institutions, parlements,
organismes internationaux, recherche publique, Our World in Data) ; 2) grands médias reconnus ; 3) le reste (blogs,
sites divers). Les articles de presse peuvent contextualiser, mais ne doivent pas être la source principale si une
source primaire couvre le sujet. Si aucune source de la liste ne convient, élargis ensuite la recherche.

Propos exacts de {speaker} : « {quote} »
Phrase où ils ont été dits : « {sentence} »
Affirmation à vérifier (reformulation de ces propos) : {claim}
Sens que {speaker} leur donne (proposé par l'extraction) : {meant}
Dit juste après (contexte seulement, n'en tire aucune autre affirmation) : {after}

Règles :
- Juge l'affirmation dans le SENS que {speaker} lui donne clairement, vu la phrase entière et ce qui suit, pas dans un
  sens littéral ou technique qu'il ne lui donne pas : une formule (« je n'ai jamais… », « au bord de la faillite »)
  se vérifie pour ce qu'elle veut dire. Si ce sens est exact mais que la lettre est fausse (ou l'inverse), c'est
  "mixte". Écris dans "meant" le sens que tu as jugé. Exemple : « Personne ne parle jamais des agriculteurs », dit en
  plein débat et suivi de « vous, vous n'en parlez pas », veut dire « on n'en parle pas dans ce débat » ; juger qu'on
  en parle ailleurs, c'est juger un sens qu'il ne lui donne pas.
- Un mot de temps fixe la période jugée : « aujourd'hui », « actuellement » = la situation ou le chiffre récents,
  pas un cumul sur plusieurs années.
- Juge UNIQUEMENT d'après des faits indépendants (statistiques et textes officiels, décisions, rapports, presse
  sérieuse), jamais d'après les propos, le programme, les plans ou les intentions de {speaker} : un désaccord avec ce
  qu'il défend ailleurs n'est pas une erreur de fait. Exception : si l'affirmation porte sur ce qu'il a dit ou fait
  avant, vérifie la source primaire datée. L'explication ne cite jamais le programme de {speaker} comme preuve contre lui,
  et une déclaration de {speaker} n'est jamais une source.
- NE PAS TROUVER n'est pas réfuter : une étude, un chiffre ou un fait que ta recherche ne retrouve pas donne
  "insuffisant", jamais "faux".
- own : true si ces propos sont une affirmation de fait que {speaker} avance lui-même comme vraie (une prémisse ou un
  argument d'un raisonnement en est une, et aussi l'idée d'un autre qu'il reprend à son compte : « je ne sais plus qui
  l'a dit, mais… », « comme le dit X ») ; false s'ils sont rapportés sans être repris, cités pour être contestés,
  hypothétiques, une promesse, une question ou une opinion.
- faithful : true seulement si l'affirmation reformulée ne dit ni plus ni autre chose que les propos exacts lus dans
  leur phrase (sujet, chiffre, portée, période, négation, « aussi », « en tant que… »).
- scope_ok : true si ton verdict juge l'affirmation dans le sens que {speaker} lui donne (règle 1).
- basis : "external_facts" si ton verdict repose sur des faits indépendants, "speaker_statements" s'il repose sur ce
  que {speaker} dit ou prévoit, "none" sinon.
- evidence : "direct" si au moins une source affirme explicitement le fait qui confirme ou contredit l'affirmation ;
  "inference" si ton verdict se déduit de sources qui ne le disent pas directement ; "absence" s'il repose sur le fait
  de ne rien avoir trouvé.
- "faux" seulement pour une contradiction directe et établie avec les faits (evidence "direct") ; une exagération d'un
  fait en partie vrai, un chiffre approximatif, une nuance ou une omission font "plutot_faux" ou "mixte".
- si les sources se contredisent ou manquent : verdict "insuffisant" et confiance basse ;
- NE MÉLANGE PAS LES SOURCES : chaque fait et chaque chiffre de l'explication vient d'une source de "sources", et le
  "says" de cette source le contient (ce que la page établit, avec ses chiffres, en une phrase) ; n'attribue jamais à une
  source ce qu'elle ne dit pas, et n'écris rien dans l'explication qui ne soit dans le "says" d'une source ;
- ne jamais inventer une URL.

Le contexte brut et les sous-titres CC ne sont là que pour lever une ambiguïté (noms, chiffres),
pas pour être cités mot à mot ni pour créer une autre affirmation.
[Animateur] dans le contexte = journaliste / plateau, pas un invité.
Voix identifiables : {voices}

Contexte compact : {context}
Sous-titres CC : {captions}
Chaîne : {channel}
Heure : {when}

Réponds UNIQUEMENT avec un JSON :
{
  "verdict": "vrai|plutot_vrai|mixte|plutot_faux|faux|insuffisant",
  "explanation": "2 à 4 phrases, contextualisées, sur les faits seulement",
  "confidence": 0.0,
  "meant": "le sens de l'affirmation que tu as jugé",
  "own": true,
  "faithful": true,
  "scope_ok": true,
  "basis": "external_facts|speaker_statements|none",
  "evidence": "direct|inference|absence",
  "sources": [{"title": "...", "url": "https://...", "says": "ce que cette page établit, avec ses chiffres"}]
}
"""

# google_first, second attempt: the first answer had no usable source; Linkup's pages are added, numbered.
LINKUP = """

Première analyse, faite sans source web exploitable (à vérifier, et à corriger si elle est fausse) :
{previous}

Pages trouvées par une recherche web pour cette affirmation (numérotées) :
{pages}

Appuie-toi sur ces pages (lis-les avec l'outil de lecture d'URL si besoin) et sur ta recherche Google, puis donne
ton verdict définitif dans le même format JSON. Pour "sources", renvoie un objet {{"page": <numéro dans la liste
ci-dessus>, "title": "...", "says": "ce que cette page établit, avec ses chiffres"}} pour chaque page de la liste que tu
as réellement utilisée. Au moins une page de la liste doit figurer dans "sources" dès qu'elle éclaire l'affirmation.
"""

# linkup_first: Linkup's pages from the start.
PAGES_FIRST = """

Pages trouvées par une recherche web pour cette affirmation (numérotées) :
{pages}

Appuie-toi sur ces pages (lis-les avec l'outil de lecture d'URL si besoin) et sur ta propre recherche Google. Si ce sont
des articles de presse et qu'une source primaire couvre le sujet (statistique officielle, rapport, texte de loi), cherche-la
et cite-la. Pour "sources", renvoie un objet {{"page": <numéro dans la liste ci-dessus>, "title": "...", "says": "..."}} pour
chaque page de la liste que tu as réellement utilisée, et {{"title": "...", "url": "...", "says": "..."}} pour une page trouvée par
ta propre recherche ("says" : ce que la page établit, avec ses chiffres). Les pages sont classées : sources primaires d'abord.
Au moins une page de la liste doit figurer dans "sources" dès qu'elle éclaire l'affirmation.
"""

# The Linkup query, written by the small model from the claim.
QUERY = """Tu prépares une recherche web pour un fact-checker. Le moteur (Linkup) suit des consignes : écris-lui une
consigne de recherche précise, pas l'affirmation recopiée.

Affirmation de {speaker} ({when}) : {claim}
Sens que l'orateur lui donne : {meant}
Ses mots exacts : « {quote} »

La consigne, en français, 500 caractères au plus, dit :
- où chercher : d'abord les statistiques et rapports officiels qui couvrent le sujet (Insee, Drees, Dares, Cour des comptes,
  ministères, Assemblée nationale, Sénat, Eurostat, OCDE, organismes internationaux...), puis la presse sérieuse ;
- quoi trouver et extraire, champ par champ : le chiffre ou le fait qui permet de vérifier, son année ou sa période,
  l'organisme qui le publie, l'URL de la page ;
- jamais « dis-moi si c'est vrai » ni aucun jugement : seulement les preuves à retrouver.
Ne cite {speaker} que si l'affirmation porte sur ses propres actes ou propos. Ne mets aucune date dans la consigne.
"from_date" (AAAA-MM-JJ) seulement si l'affirmation porte sur la situation actuelle ou récente (« aujourd'hui »,
« actuellement », « cette année ») : la date à partir de laquelle les pages comptent ; sinon null.

JSON uniquement : {{"query": "...", "from_date": null}}
"""

# A search-grounded answer is free text: when it is not valid JSON, the model rewrites it as JSON.
JSON_REPAIR = 'Réécris le texte suivant en JSON strict, sans markdown, sans commentaire. Conserve tous les champs et leurs valeurs.\n\n'
