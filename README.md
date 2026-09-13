# Commonplace — local-first AI wiki

An evidence-backed personal wiki inspired by Karpathy's LLM Wiki pattern: preserve originals, compile reusable concept pages, query the accumulated knowledge, and audit it over time. SQLite is the authority for transactional revisions; readable Markdown is materialized from committed state.

## Run

Requires Python 3.12 and Node.js/npm. From this directory:

```sh
./setup.sh
.venv/bin/python start.py
```

Open http://127.0.0.1:8000. For development use `.venv/bin/python start.py --dev` and http://127.0.0.1:5173. Do not open `frontend/index.html` as a file. Keep the server bound to loopback; this is a personal application without multi-user authentication.

## Model setup

External models are **off by default**, including for an existing workspace that has no explicit switch setting. The switch in Settings saves immediately. While off, the backend blocks inference, retry attempts, model-list connection tests, OAuth authorization, token exchange, and token refresh. A request already dispatched cannot be recalled. This is a model-network gate, not a firewall: importing a web URL still downloads that URL. Reading, manual editing, extraction, and local search work without AI.

Select a provider, enter its API key and exact model identifier, save, then explicitly enable external models. Provider credentials and model overrides are separate. The connection check lists models without sending documents; it does not establish inference permissions or billing access. AI operations may incur provider charges.

| Provider | Default base URL | Key environment variable |
| --- | --- | --- |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| Qwen | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| Kimi | `https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` |
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| Grok | `https://api.x.ai/v1` | `XAI_API_KEY` |
| Custom | Set your own OpenAI-compatible base URL | `CUSTOM_API_KEY` |

Qwen's region/workspace endpoint is editable; use the endpoint matching your key. Custom APIs must support `/chat/completions`, JSON output, and the configured model's requested token limits. Custom authentication supports API keys, no authentication for a local server, or OAuth. HTTPS is required except for loopback HTTP. Changing a custom or Qwen endpoint clears its saved key; enter the key for the new destination. Imported documents and model output cannot create providers, enable external access, or edit credentials. A trusted user can configure custom model endpoints in Settings.

Keys entered through Settings are saved in the project's `.env` with mode 0600; they are not returned by the settings API. OAuth tokens are stored in the project’s `.credentials/<workspace-hash>/oauth.json`, outside `local-data/`. The private directory uses mode 0700 and token files use mode 0600. This is restricted local file storage, not macOS Keychain encryption. Do not share `.env` or `.credentials/`. Knowledge data contains originals, extractions, revisions, indexes, and processing metadata, not application-managed credentials. Legacy OAuth files are migrated automatically on startup: bytes are written and verified privately before the old file is removed. If a current token already exists, the legacy copy is retained privately without replacing it. Moving a knowledge directory changes its credential identity and requires reconnecting.

### OAuth

Custom OAuth implements Authorization Code + PKCE S256 for a **registered public client**, with browser-bound single-use state, a ten-minute login lifetime, server-side token storage, automatic refresh, and local disconnect. Enter the authorization URL, token URL, client ID, API scopes, and optional audience. Register this exact callback:

`http://127.0.0.1:8000/api/oauth/callback`

Use the app at `127.0.0.1`, not `localhost`, for the login flow. Port 8000 must be running. Changing the custom API or OAuth configuration invalidates saved tokens. OAuth credentials are excluded from backups. Disconnect removes local credentials; revoke the authorization at your provider if you also want to end its server-side grant. Clients requiring a client secret, device flow, and arbitrary provider-specific protocols are not implemented.

**Direct ChatGPT and Grok subscription OAuth are not integrated.** No third-party subscription OAuth client registration was supplied or established for this application. OpenAI and Grok entries use their documented API-key interfaces. A ChatGPT/Codex login is not a general OpenAI API credential. Custom OAuth can connect a compatible, registered API/gateway; it does not bypass these provider requirements.

Provider references: [Qwen](https://www.alibabacloud.com/help/en/model-studio/first-api-call-to-qwen), [Kimi](https://platform.kimi.ai/docs/api/overview), [OpenAI authentication](https://learn.chatgpt.com/docs/auth), [Grok API](https://docs.x.ai/developers/quickstart).

## Workflow

1. Add text, Markdown, a text PDF, HTML, or a public URL. Originals are content-hashed and preserved; extraction versions retain stable evidence passages. Scanned PDFs need OCR elsewhere.
2. Compile a source. The model plans changes and then proposes validated blocks. Sources never grant model instructions authority. Calls have bounded retries and per-job token/call limits.
3. Read concept pages, follow citations to exact quotes and extraction versions, inspect history/diffs, and protect pages or sections. Human notes survive subsequent compilation.
4. Research across local wiki/source search. Answers carry passage evidence and page revision references. Save useful answers through the compiler.
5. Run Wiki health. Deterministic checks work locally; optional semantic review uses the configured external model. Activity shows jobs, usage, errors, and staged proposals.

Review-first mode stages changes for explicit application. Concurrent page edits invalidate stale proposals. A database commit is atomic; Markdown files are refreshed afterward and repaired on restart if interrupted. External Markdown edits are preserved as recovery copies, not silently imported.

Three clearly synthetic sources are available through Sources → load sample sources. No fake provider is available in the production application. The scripted provider used by tests is only a test fixture.

## Backup and recovery

```sh
.venv/bin/python manage.py backup /absolute/path/commonplace-backup.zip
.venv/bin/python manage.py restore /absolute/path/commonplace-backup.zip /absolute/path/new-workspace
.venv/bin/python manage.py repair
.venv/bin/python manage.py status
```

Full backups contain a consistent SQLite snapshot, originals, extraction snapshots, rules, and integrity hashes. Restore only accepts a new destination. Set `APP_DATA_DIR` in `.env` to use the restored workspace; review the restored rules before copying them into `config/wiki-rules.md`. Provider keys and OAuth tokens are excluded. Markdown export in Settings is a readable export, not a full backup.

## Validation and limits

```sh
.venv/bin/pytest -q
npm --prefix frontend run build
```

Tests cover immutable evidence, transactional/stale updates, locks, crash materialization, extraction, URL restrictions, research/compiler workflows, backup restoration, provider routing, the external-access switch, and mocked OAuth exchange/refresh. Browser inspection covers the settings form and disabled controls. Live provider inference and real registered OAuth login have not been tested without user credentials.

Current retrieval uses SQLite FTS and bounded context, not vector search. OCR, multi-user access, scheduled maintenance, and provider-specific subscription bridges are outside this implementation. Conservative byte-based token reservations may reject large inputs before a provider's actual token limit. Provider/model compatibility and semantic output quality still require evaluation with the chosen real model. The system validates evidence identity and quote matching; it cannot prove that every generated interpretation is correct.

## Reset for a different vertical

Settings → Start a new vertical → Reset app requires typing RESET. It deletes all stored sources, extracted text, pages, revisions, answers, jobs, activity, issues, indexes, and recovery copies. Export/full-backup first. Running processing blocks reset. External models are always turned off afterward. The optional checkbox also restores settings defaults and removes saved API keys for this installation and OAuth tokens for this workspace. Otherwise provider configuration is kept. App code, trusted rules, and exports/backups outside the workspace remain. This is logical deletion, not a promise of forensic erasure or removal from external backups. Interrupted cleanup is completed on startup before jobs run.

### Citation selection

Model-generated research, long-source analysis and page drafts select request-scoped evidence IDs. The application builds these IDs from exact saved source excerpts and resolves them back to the existing passage-ID/quote format before validation or review. Models no longer have to reproduce quotation text. Existing multi-sentence citations remain selectable so updates can retain prior evidence. Unknown IDs are rejected; citations cannot reach outside the supplied context.

This prevents quote-copy errors caused by capitalization, punctuation, Unicode or Markdown. It does not prove that a claim follows from the selected excerpt: review the claim and citation together. Original extractions, existing citations and the final exact-substring storage check are retained.
