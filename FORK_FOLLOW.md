# FORK_FOLLOW.md — veille sur le réseau de forks

Suivi de ce qui a été construit dans les forks de l'upstream **[Thysrael/Horizon](https://github.com/Thysrael/Horizon)**,
et de ce qui vaut la peine d'être récupéré dans ce fork (`SebHeuze/Horizon`).

- **Date du scan** : 2026-08-14
- **Base de comparaison** : `Thysrael/Horizon@main`
- **Méthode** : `GET /repos/Thysrael/Horizon/forks` (toutes les pages), puis
  `GET /repos/Thysrael/Horizon/compare/main...<owner>:<repo>:<default_branch>` sur chaque fork
  ayant été poussé après sa création.

## Volumétrie

| Étape | Nombre |
|---|---|
| Forks totaux | 1 362 |
| Avec un push après la création | 500 |
| Réellement en avance sur `main` upstream | 401 |
| Dont les modifs ne sont pas que du contenu généré (`docs/_posts/`, `data/summaries/`…) | 388 |
| Avec ≥ 3 fichiers `src/`, `profiles/`, `tests/` touchés | **138** |

**~90 % du réseau est du bruit** : changement de provider (DeepSeek / GLM / Gemini / MiniMax / SiliconFlow),
configuration d'un webhook Feishu ou Telegram, ajustement de l'horaire du cron, ajout de flux RSS perso.
Tout cela est déjà supporté en amont — les plateformes `feishu`, `lark`, `dingtalk`, `slack`, `discord`,
`generic` sont déjà dans `src/models.py:466`, et tous les scrapers (`gdelt`, `google_news`, `ossinsight`,
`twitter_playwright`…) existent déjà.

---

## Tier 1 — petit, propre, directement récupérable

### 1. Telegram comme plateforme de *livraison*

**Fork** : [SCSI-9/Horizon](https://github.com/SCSI-9/Horizon)
**Taille** : `+136/-4` dans `src/services/webhook.py`, `+3/-3` dans `src/models.py`

C'est le trou n°1 du projet. Telegram existe en tant que **source** (`src/scrapers/telegram.py`) mais pas
en tant que `webhook.platform`. Résultat : ~40 forks bricolent chacun leur `send_telegram.py` dans le
workflow Actions.

Ce que fait le patch :

- ajoute `"telegram"` aux plateformes autorisées et `"html"` aux layouts (`layout` + `fallback_layout`)
- `WebhookNotifier.notify()` retourne le `message_id` renvoyé par l'API Telegram
- envoie d'abord les items, collecte les `message_id`, puis envoie le sommaire avec des **deep links `t.me`
  vers chaque message** — gère `@channel`, les supergroupes `-100…`, et retourne `None` si non-linkable
- `_prepare_variables_for_body()` saute le nettoyage Markdown quand le contenu est en mode HTML

### 2. Déduplication inter-jours

**Fork** : [Yihaaaaaan/Personal-OS-Horizon](https://github.com/Yihaaaaaan/Personal-OS-Horizon)
**Taille** : ~100 lignes, 3 commits

Upstream ne déduplique qu'*à l'intérieur* d'un run. Ce fork ajoute `data/sent_history.json` :

- fenêtre glissante de 7 jours, purge à la lecture, écriture atomique via `_atomic_write_text()`
- `StorageManager.load_sent_history()` / `save_sent_history()`
- filtrage inséré juste après `merge_cross_source_duplicates()` dans `orchestrator.run()`
- réutilise le `_deduplication_url_key()` existant (wrappé en `_deduplication_url_key_str()`)

### 3. `DryRunAIClient`

**Fork** : [smth4nth/Horizon](https://github.com/smth4nth/Horizon)
**Taille** : 69 lignes (`src/ai/dry_run.py`) + 2 fichiers de tests

Un `AIClient` qui renvoie des réponses mock plausibles selon le prompt système
(dedup → `{"duplicates": []}`, enrichment → artefact complet bidon, traduction → passthrough…).
Permet de faire tourner **tout le pipeline de bout en bout, à coût zéro**.
Particulièrement utile pour itérer sur les templates email (`src/services/email_render.py`) sans cramer de tokens.

### 4. Budget d'enrichment (top-N)

**Fork** : [colaman7014/Horizon](https://github.com/colaman7014/Horizon)
**Taille** : 20 lignes (`src/ai/enrichment_policy.py`)

`select_items_for_enrichment(items, top_n)` — n'enrichit que les N premiers (les items sont déjà triés
par score), `None` = comportement actuel. L'enrichment est la passe la plus chère du pipeline.
Le fork ajoute aussi des timings par étape et des caps pré-AI.

### 5. Compat modèles à raisonnement

**Forks** : [HankunYu/Horizon](https://github.com/HankunYu/Horizon), [TNCNBO/Horizon](https://github.com/TNCNBO/Horizon)

Support de `max_completion_tokens` (au lieu de `max_tokens`) et suppression de
`response_format: json_object` pour les modèles gpt-5.x / série reasoning.

---

## Tier 2 — vraies features, à évaluer

| Fork | Apport |
|---|---|
| [whjwjx/Horizon](https://github.com/whjwjx/Horizon) ✅ *repris — `src/discovery/`, `docs/discovery.md`* | **Découverte automatique de sources RSS** : `src/discovery/` (448 lignes), recherche web + AI pour proposer de nouvelles sources selon les centres d'intérêt, en excluant celles déjà abonnées. Workflow hebdo + rapport `docs/discovered-sources.md` + notification webhook. Le plus original du réseau. |
| [prokiki/horizon](https://github.com/prokiki/horizon) | `src/feed.py` — **sortie RSS/Atom du digest** (`docs/feed-en.xml`, `feed-zh.xml`), inexistante en amont. Plus `src/storage/cache.py` (cache SQLite), concurrence, suivi de tendances. |
| [wdf0512/Horizon](https://github.com/wdf0512/Horizon) | Export **Obsidian + Notion** (`scripts/to_obsidian_vault.py`, `to_notion.py`, `to_blog_post.py`), outil MCP `hz_search_history` (recherche par mots-clés sur fenêtre de dates), « config packs » réutilisables. |
| [xiwenran/Horizon](https://github.com/xiwenran/Horizon) | Variante Obsidian plus propre : `src/services/obsidian_export.py` **avec tests**, + archivage du site par jour sur 30 jours. Voir aussi [darrenye-ops](https://github.com/darrenye-ops/Horizon) (publication idempotente et atomique) et [Wkkkkk](https://github.com/Wkkkkk/Horizon) (lien « Save to Obsidian » opt-in dans le digest). |
| [ChiosYang/Horizon](https://github.com/ChiosYang/Horizon) | **Routing de modèle par étape** — modèle bon marché pour la classification, modèle fort pour l'enrichment. Plus métriques de perf par étape et pipelines de domaine parallèles. |
| [MrR0990/Horizon](https://github.com/MrR0990/Horizon) | `scoring_prompt` **par catégorie** (rubrique de notation spécialisée par thème) + `shown_articles.json` pour ne pas re-recommander. |
| [yonoel/Horizon](https://github.com/yonoel/Horizon) | **Multi-webhook** (plusieurs destinations en parallèle) + scoring personnalisé, avec specs et tests. |
| [michaelluetw-bit](https://github.com/michaelluetw-bit/Horizon), [ohxiyu](https://github.com/ohxiyu/bmtnews), [Wkkkkk](https://github.com/Wkkkkk/Horizon) | **Watchdog de planification** : GitHub Actions saute régulièrement des crons ; ces forks détectent le run manquant et le rattrapent (+ watchdog du déploiement Pages). |

---

## Tier 3 — gros chantiers (inspiration, pas merge)

- **[AntonMiklushov/Horizon](https://github.com/AntonMiklushov/Horizon)** — l'idée la plus utile *pour un
  mainteneur de fork* : tout ce qui est spécifique au fork vit dans un paquet séparé `src/horizon_ext/`
  (pipeline, rendering, web, personal), ce qui garde les merges upstream propres. Inclut un dashboard web
  local (FastAPI + Jinja) et des tests de « packaging safety ».
- **[ChiosYang/Horizon](https://github.com/ChiosYang/Horizon)** — `src/configuration/` + `src/config_ui/` :
  éditeur de config web local avec diff, patch, backups et redaction des secrets.
- **[izzhackt/Horizon](https://github.com/izzhackt/Horizon)** — persistance SQLite, clustering par
  embeddings locaux, « LLM preflight gate » (vérifie que le provider répond avant de lancer le run).
- **[Golden0Voyager/Horizon](https://github.com/Golden0Voyager/Horizon)** — le plus rigoureux côté qualité :
  ruff + mypy + coverage `fail_under = 85` en CI, ~40 fichiers de tests ajoutés. Aussi **Reddit OAuth2**
  (vs. l'API publique fragile) et un `provider_chain` avec fallback.
- **[AntonL9vov/InfoService](https://github.com/AntonL9vov/InfoService)** — transformé en SaaS multi-tenant :
  PostgreSQL, clés LLM chiffrées par utilisateur (BYOK), bot Telegram comme UI, scheduling cron durable
  avec claiming. Autre produit, mais les patterns d'isolation tenant et de scheduling sont solides.
- **[Nerakolox/Feedra-Horizon](https://github.com/Nerakolox/Feedra-Horizon)** — console web FastAPI + React
  + Caddy, export image des briefings, page de stats.
- **[ohxiyu/bmtnews](https://github.com/ohxiyu/bmtnews)** — refonte complète en site de news crypto :
  `src/weekly.py`, `src/archive.py`, `src/editorial.py`, `src/api_output.py`, `src/schedule_watchdog.py`,
  livraison X + Telegram.

---

## Vérification des trous côté upstream

Confirmé absent de ce repo au moment du scan (`grep` sur `src/`, `data/config.example.json`, `docs/`) :

| Fonctionnalité | Marqueur cherché | Présent ? |
|---|---|---|
| Dédup inter-jours | `sent_history`, `seen_items` | non |
| Budget d'enrichment | `enrich_top_n`, `top_n` | non |
| Client dry-run | `DryRunAIClient` | non |
| Flux RSS en sortie | `feed.xml` | non (seulement en exemple d'URL) |
| Prompt de scoring par catégorie | `scoring_prompt` | non |
| Historique des articles montrés | `shown_articles` | non |
| Telegram en plateforme webhook | `telegram` dans `webhook.py` | non (source uniquement) |

---

## Ordre de récupération recommandé

1. **DryRunAIClient** — débloque l'itération sur les templates email sans coût
2. **Dédup inter-jours** — petit, isolé, gain immédiat sur la qualité du digest
3. **Telegram delivery** — comble le trou le plus visible du projet

Les trois sont petits, testables hors-ligne (`uv run pytest`) et sans conflit avec l'existant.

---

## Limites du scan

- Seule la **branche par défaut** de chaque fork a été comparée — du travail sur une branche de feature
  non mergée a pu échapper au scan.
- 3 forks ont échoué à la comparaison (repos vides ou supprimés).
- Le filtre « poussé après la création » exclut les forks modifiés dans les 2 minutes suivant leur création.
- **Licence** : la plupart de ces forks n'ont pas de licence explicite au-delà de celle héritée de l'upstream.
  Vérifier l'attribution avant de reprendre du code.

## Reproduire le scan

```bash
gh auth login                      # nécessaire : 1 362 forks > limite anonyme de 60 req/h
gh api repos/Thysrael/Horizon --jq '.forks_count'
gh api "repos/Thysrael/Horizon/forks?per_page=100&page=1" --jq '.[].full_name'
gh api "repos/Thysrael/Horizon/compare/main...OWNER:REPO:BRANCH" \
  --jq '{ahead: .ahead_by, commits: [.commits[].commit.message], files: [.files[].filename]}'
```
