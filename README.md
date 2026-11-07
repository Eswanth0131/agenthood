# Agenthood

Agenthood is a two-sided marketplace for buying and selling executable AI and tool work. Its music studio is a seven-role example of a buyer commissioning a project and a platform seller supplying specialists.

## Run locally

Use Python 3.11+ on macOS or Linux. The music studio uses macOS speech for synthesized lyrics. When speech is unavailable it renders a labeled wordless guide, so the full workflow also runs on Linux.

```bash
cd agenthood
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app:app --env-file .env --port 8001
```

Open [Agenthood](http://127.0.0.1:8001/), [Music Studio](http://127.0.0.1:8001/studio), or [API docs](http://127.0.0.1:8001/docs). Port 8001 lets you run this copy alongside an existing studio on port 8000. Start the server from the extracted folder so the relative paths in `.env` resolve there.

## Try a sale

1. Register as a **seller**. Create a draft with a name, capabilities, price in simulated cents, concurrent job slots, and the **Text analysis** tool.
2. Save the draft, click **Verify execution**, then check **Published** and save. The local tool probe runs before the listing can receive quotes. Changing the tool or endpoint requires another probe.
3. Open a private browser window and register as a **buyer** with a different email. Select the listing, enter source text such as “apple apple orange pear,” and request a quote. The buyer sets the maximum price for that job.
4. Hire from the quote. The dispatcher computes word counts and term frequencies for the supplied text. Open the job and wait for the draft.
5. Request one revision with replacement source text, or approve the draft and let the worker produce the final report. Accept the final delivery and leave a rating. The seller sees job status, contract terms and the review in their dashboard.
6. Check the account ledger. The completed job contributes to simulated settlement. Verified revenue stays at zero.

A platform **Text Analyzer** listing is ready on a fresh install. With a configured inference provider, **Model Writer** is also available.

## Execution and contracts

- **Tools** run application code on the brief. The text-analysis result is labeled **Algorithmic text analysis**. A revision reruns the tool against replacement text. A seller can also list the configured model or an approved endpoint.
- **Models** send real Chat Completions requests. Set `DARKBLOOM_BASE_URL`, `DARKBLOOM_MODEL` and, if required, `DARKBLOOM_API_KEY` for the existing Darkbloom-compatible adapter. Otherwise set `OLLAMA_MODEL` and optionally `OLLAMA_BASE_URL` for Ollama. Provider failures surface as job failures. Marketplace model jobs require explicit processing consent.
- **Endpoint responses** are labeled separately. Verification proves control of a reachable endpoint that understands the signed challenge. Its answers and capability claims are seller supplied.
- **Quotes** expire in 10 minutes. Hiring locks the listing revision, capabilities, execution adapter, scope, budget and fixed price in the contract. Listing edits invalidate unused quotes. Retrying a hire with the same accepted quote returns the same job. Capacity is enforced per listing in a database transaction.
- **Progress** follows queued → running → awaiting approval → queued → running → delivered → completed. One optional draft revision runs through the same dispatcher. Approvals and acceptance refer to a specific delivery ID. Buyers can cancel queued jobs or jobs at a checkpoint. An idle hold expires after 24 hours. Failure, cancellation, expiration and completion release capacity. Queued jobs survive restart. An interrupted running job fails with an audit event to avoid silently repeating a remote call.
- **History** records hire, start, delivery, buyer decisions, failure and settlement events. Buyers can open earlier deliveries. Seller dashboards omit buyer briefs, results and private feedback. A completed job can receive one review, with comments visible to its buyer and seller and aggregate ratings on the catalog.
- **Private notes** are owned by buyer account ID. Only the buyer can list, create and delete them. Up to four current notes are included in a model request if the buyer opted in for the specific job. Notes are omitted from seller endpoint requests, the public catalog and Cognee sync. Buyers should include information in briefs and reviews only if they intend the designated recipient to see it.
- **Payments** are simulation-only. No real charges, transfers or payouts occur. The ledger has separate simulated and verified fields, and verified ingestion is disabled. Earlier payment attestations and generic Stripe links cannot create verified revenue here. A real-money rollout requires a processor integration with authenticated events, amount and currency reconciliation, replay protection, and refund handling.

The dispatcher executes one marketplace stage at a time per database. Slot counts limit committed work, including jobs waiting for a buyer. This MVP runs a bounded draft, revision and final workflow with buyer decisions.

## Register a seller endpoint

The operator assigns a stable key in `AGENTHOOD_ENDPOINTS`, a JSON object mapping keys to `{"url":"https://seller.example/work","secret":"a-unique-random-secret-of-at-least-32-characters"}`. Use operator-vetted HTTPS destinations with controlled DNS and network egress. Marketplace users supply the key and proof of its secret. They cannot enter arbitrary dispatch URLs. The app disables redirects and inherited proxy settings for seller calls. Rotating the URL or secret invalidates verification and prevents old jobs from dispatching to a different endpoint.

Both operations receive a POST with `Authorization: Bearer <secret>`:

| Operation | Request | Response |
| --- | --- | --- |
| Verify | `{"operation":"verify","nonce":"<random hex>"}` | `{"signature":"<hex HMAC-SHA256 of nonce using secret>"}` |
| Execute | `operation`, `job_id`, `execution_id`, `phase`, `brief`, `previous_output`, `feedback` | `{"output":"<non-empty plain-text deliverable>"}` |

The phase is `draft`, `revision`, or `final`. Use `execution_id` to deduplicate endpoint work. An output is limited to 16,000 characters. The buyer sees the returned text as text, with an endpoint provenance label.

For a local example, run this in the activated environment in a separate terminal:

```bash
export AGENTHOOD_EXAMPLE_SECRET=local-demo-secret-replace-before-sharing
python -m uvicorn example_endpoint:app --host 127.0.0.1 --port 8091
```

Set the following in the marketplace's `.env`, then restart the marketplace:

```dotenv
AGENTHOOD_ALLOW_LOCAL_ENDPOINTS=true
AGENTHOOD_ENDPOINTS='{"sample":{"url":"http://127.0.0.1:8091/work","secret":"local-demo-secret-replace-before-sharing"}}'
```

Choose **Approved endpoint** on a seller listing, enter `sample`, save, verify using the secret above, and publish. The buyer needs to check processing consent on the quote. The example endpoint computes a text report using the submitted brief.

## Music Studio

Sign in as a buyer and open **Music Studio**. Start with a song title, brief and internal capacity budget. Click the hire button for each role and approve its delivery to move on. The studio manager selects platform specialists using quote, capacity and aggregate review ratings.

| Specialist | Deliverables |
| --- | --- |
| Lyricist | Verse, build and chorus draft |
| Composer | Melody and chord audio, MIDI and score |
| Arranger | Arrangement audio and section notes |
| Instrumentalist | Drums, bass and synth keys stems, backing and MIDI |
| Singer | ACE-Step sung take with backing when configured, or a labeled local speech sketch or wordless guide |
| Producer | Stereo master of the approved model take, or a mix from local stems and the vocal guide |
| Marketing | Release copy and promotion checklist |

You can request revisions at checkpoints, compare earlier takes, play the audio, and download the studio bundle. The studio is a separate, platform-operated catalog with its own audio artifact contracts and budget. Third-party marketplace endpoints deliver text and do not join the studio pipeline.

With processing consent, **Auto** selects the configured Darkbloom-compatible provider or Ollama for lyrics and marketing. With neither configured or without consent, those roles use labeled offline templates. Selecting a live provider explicitly requires consent. Model failures leave the stage retryable. The composer, arranger and instrumentalist produce labeled deterministic sketches. The singer can call ACE-Step. Without a model endpoint, it uses macOS system speech and a MIDI-controlled vocoder. If the speech engine is missing or silent, the studio labels the resulting melody audio as a wordless fallback and mixes it into the track. Use **Add lyric vocals** once speech is available to replace the guide and remix. Set `AGENTHOOD_VOICE` to an installed voice if desired. List available voices with `say -v '?'`.

Studio review scores affect aggregate ratings and prices. Written notes stay private to the account and may inform a later studio model call for the same buyer and specialist with processing consent. The optional Cognee integration remains available with a separate installation and `ENABLE_COGNEE=true`. It attempts to ingest the numeric rating and specialist ID, and records the sync outcome in the studio audit trail. Retrieval uses SQLite. The package includes the existing inference and Cognee adapters. Tinfoil hosting is optional and requires a separate GPU deployment. Superlinked ranking is outside this build.

### Bring over an existing studio

Stop the old app before copying its SQLite database and artifacts. Back up the old installation, then copy its `agenthood.db` and `music_outputs/` into the new folder. Copy over the provider settings you use into the new `.env`. Start the new server to migrate the copied database and register the intended buyer. Legacy projects had no authenticated owner. Assign each project after checking its ID and intended buyer:

```bash
AGENTHOOD_DB=agenthood.db python claim_studio.py PROJECT_ID buyer@example.com
```

This local operator command only assigns unowned projects. Legacy projects default to offline execution until a new project is created with processing consent. Old buyer labels, shared notes and payment records do not grant access or create verified revenue. For older deliveries that already reached the singer stage, the studio's lyric-vocal upgrade can generate the synthesized vocal and remix with the original roles.

## Tests and deployment scope

```bash
python -m pytest -q
```

Coverage includes account and role checks, cross-account job/file/note access, CSRF, stale quotes, concurrent capacity reservation, retrying the same quote, revisions and stale approvals, simulation accounting, restart recovery, endpoint challenge and execution payloads, inference prompts, and the seven-stage studio. Provider HTTP responses and the speech engine are substituted in tests. The synthesis, dispatcher and database transactions execute normally.

Use one server process per database. For an internet-facing deployment, put the app behind an HTTPS reverse proxy with request-size and rate limits, set `AGENTHOOD_ORIGIN` and `AGENTHOOD_SECURE_COOKIES=true`, and restrict outbound traffic to operator-approved provider hosts. Accounts use password-based sessions. Email verification, password recovery, distributed workers and real-money payments are outside this MVP.

## Hosted singing with Tinfoil

The optional [Tinfoil renderer setup](deploy/tinfoil/README.md) builds ACE-Step with
verified model weights on a GitHub runner, then deploys it to a Tinfoil GPU Container.
It uses an attested connection and requires explicit project-owner consent for each
hosted take. For local use, set `AGENTHOOD_SONG_URL=http://127.0.0.1:8085` once a
local ACE-Step server is ready.
