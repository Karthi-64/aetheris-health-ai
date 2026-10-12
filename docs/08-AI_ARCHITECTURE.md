# 08 — AI Architecture

AI is not a module in Aetheris. It is a **platform capability** every module can use. This document describes how the AI layer is structured, how modules invoke it, and the safety guarantees around it.

> **Read §0 first.** This document describes both what is built and what is planned. Every section below is labelled **Implemented**, **Partly implemented** or **Planned — not built**. Only the parts labelled implemented exist in the code today.

---

## 0. Implementation Status (as of 2026-10-06)

**One AI capability exists: appointment slot recommendation.** A member of staff can ask for one suggested free slot in a doctor's day; the suggestion is advisory and a person books the appointment. Everything else in this document is design intent.

### Implemented today

| Area | What exists | Where |
|---|---|---|
| AI runtime | One process-wide runtime built from settings, with no network call at startup; AI off is a normal state | `app/ai/runtime.py`, `app/core/lifecycle.py` |
| Provider abstraction | `AIProvider` interface, provider registry, hint → model mapping | `app/ai/providers/base.py`, `app/ai/providers/__init__.py`, `app/ai/constants.py` |
| Provider | **Groq only**, over `httpx` against its OpenAI-compatible chat-completions API. No vendor SDK is installed | `app/ai/providers/groq.py` |
| Model | `openai/gpt-oss-20b` (a reasoning model) for the `fast` hint; overridable with `AI_FAST_MODEL` | `app/ai/constants.py`, `app/core/config.py` |
| AI service | One provider call per request under a total deadline, a concurrency cap, one log line per call | `app/ai/services/ai_service.py` |
| Prompt registry | Versioned YAML templates loaded when the runtime is built | `app/ai/prompts/registry.py` |
| Slot recommendation | `POST /api/v1/appointments/recommend-slot`, prompt `appointment.recommend_slot` v2.0.1 | `app/services/slot_ranker.py`, `app/services/appointment_service.py`, `app/api/v1/appointments.py` |
| Structured output + validation | Strict JSON Schema sent to the provider; the reply is validated again on the server and the slot re-checked as free | `app/services/slot_ranker.py`, `app/services/appointment_service.py` |
| Access control | Two permissions, a per-hospital feature flag, a capability read for the UI | `app/api/v1/appointments.py`, `app/core/feature_flags.py`, `app/api/v1/hospitals.py` |
| Typed failures | `AI_NOT_CONFIGURED`, `AI_PROVIDER_UNAVAILABLE`, `AI_PROVIDER_TIMEOUT`, `AI_RESPONSE_INVALID` (all HTTP 503) | `app/ai/errors.py`, `app/core/error_codes.py` |
| Human confirmation | "Use this slot" selects the slot in the picker; the ordinary "Book" button books it | `frontend/src/components/appointments/SlotSuggestion.tsx`, `BookAppointmentDialog.tsx` |

### Not built (planned)

- **Other providers.** `anthropic.py`, `openai.py` and `ollama.py` are stubs whose methods raise `NotImplementedError`; none is registered by the runtime. There is no Gemini adapter file.
- **Retry and provider fallback.** A call is made once. The registry has `set_fallback_order` / `resolve_with_fallback` methods, but nothing calls them.
- **Streaming.** `AIService.complete` and `GroqProvider.complete` reject `stream=True`.
- **Function calling / tools.** A `ToolRegistry` class exists in `app/ai/tools/__init__.py` with no tools registered; the Groq adapter rejects a non-empty `tools` list. No model call can trigger an action.
- **Conversational memory, RAG, MCP, agents.** `app/ai/memory/`, `app/ai/context/` and `app/mcp/` contain only a package docstring. There is no `app/ai/agents/`.
- **The `ai_interactions` table.** It does not exist. The record of a call is one structured log line (§12).
- **Budgets and cost controls.** No per-hospital or per-user AI budget is enforced. Budget constants and a `BudgetExceededError` class exist; nothing uses them.
- **Evaluation.** There is no golden set and no evaluator. `app/ai/evaluation/golden_sets/` is empty.
- **Embeddings.** Every adapter's `embed()` raises `NotImplementedError`.
- **Other use cases.** Chat assistant, patient summaries, clinical drafting, invoice explanation, extraction and Q&A are not built. A `patient/summarize.yaml` template is on disk and is loaded, but no code calls it.

### Scope, stated plainly

The current feature picks a time from a schedule. The model is given no clinical information and no patient data, and is instructed to make no clinical judgement. The feature changes nothing in the database and returns no score or confidence value. Its output is a suggestion a person may ignore.

---

## 1. Design Goals

These are the goals the layer is designed towards. The status column says how far the code has got.

| # | Goal | Status today |
|---|---|---|
| 1 | **Provider-agnostic** — swap Anthropic ↔ Groq ↔ OpenAI ↔ Gemini ↔ self-hosted without touching business logic | Interface in place; one provider (Groq) connected |
| 2 | **Composable** — services request AI capabilities, not raw model calls | Implemented for the one capability (`AISlotRanker` behind the `SlotRanker` protocol) |
| 3 | **Observable** — every AI call logged with tokens, latency, provider | Implemented as a structured log line; no table, no dashboards. Cost is estimated only for models in the price table (§12) |
| 4 | **Safe** — AI never touches the database directly; AI outputs are validated | Implemented for the one capability (§11) |
| 5 | **Testable** — prompts are versioned, evaluated with golden sets | Prompts are versioned; golden-set evaluation is planned |
| 6 | **Cost-controlled** — budgets per hospital, per user, per use case | Planned. Today: a per-user rate limit and a process-wide concurrency cap only (§13) |
| 7 | **Future-ready** — MCP-native, RAG-ready, agentic-workflow-ready | Planned |

## 2. Directory Layout

What is on disk today:

```
app/ai/
├── runtime.py                # composition root: builds the one runtime from settings
├── errors.py                 # typed AI errors (all map to HTTP 503)
├── constants.py              # ModelHint, hint → model mapping, token/temperature defaults
├── providers/
│   ├── base.py               # AIProvider interface + normalised types
│   ├── __init__.py           # AIProviderRegistry
│   ├── groq.py               # IMPLEMENTED — httpx, OpenAI-compatible API
│   ├── anthropic.py          # stub — raises NotImplementedError
│   ├── openai.py             # stub — raises NotImplementedError
│   └── ollama.py             # stub — raises NotImplementedError
├── prompts/
│   ├── registry.py           # loads templates from disk
│   └── templates/
│       ├── appointment/
│       │   └── recommend_slot.yaml   # the one prompt in use (v2.0.1)
│       ├── patient/
│       │   └── summarize.yaml        # on disk, not called by any code
│       ├── billing/                  # empty
│       └── reports/                  # empty
├── services/
│   └── ai_service.py         # AIService: the single entry point to a provider
├── tools/
│   └── __init__.py           # ToolRegistry class; no tools registered
├── memory/                   # placeholder package (docstring only)
├── context/                  # placeholder package (docstring only)
└── evaluation/
    └── golden_sets/          # empty
```

The use-case adapter for the one capability lives with its module, not in `app/ai/`: `app/services/slot_ranker.py`.

**Planned, not on disk:** `providers/gemini.py`; `services/summarization.py`, `extraction.py`, `recommendation.py`, `qa.py`; `agents/`; `tools/patient_tools.py`, `tools/appointment_tools.py`; `memory/session_memory.py`, `memory/long_term.py`; `context/vector_store.py`, `context/retriever.py`; `evaluation/evaluators.py`.

## 3. Provider Interface

**Status: implemented (interface); one adapter implemented.**

Every provider implements the same async interface. This is what will let us swap providers without touching callers.

```python
# app/ai/providers/base.py

class AIProvider(ABC):
    name: str

    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        tools: list[ToolDefinition] | None = None,
        stream: bool = False,
        *,
        response_schema: ResponseSchema | None = None,
        timeout_seconds: float | None = None,
    ) -> AIResponse | AsyncIterator[AIChunk]: ...

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]: ...

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> Decimal: ...
```

`AIResponse`, `AIChunk`, `Message`, `ToolDefinition`, `ToolCall` and `ResponseSchema` are internal types that normalise provider-specific shapes. `ResponseSchema` carries a JSON Schema the reply must satisfy; an adapter uses it however its provider supports structured output. The `tools`, `stream` and `embed` parts of the interface are declared for future use — no adapter implements them today.

### 3.1 The Groq provider (the only connected provider)

`app/ai/providers/groq.py`:

- Talks to Groq's OpenAI-compatible HTTP API (`POST {GROQ_BASE_URL}/chat/completions`) with `httpx`. **No vendor SDK is installed** — `backend/pyproject.toml` lists `httpx` and no `groq`, `openai` or `anthropic` package.
- Makes **exactly one HTTP request per call**: no retry, no redirect following, no tools, no streaming. `stream=True` or a non-empty `tools` list raises `NotImplementedError`; only `system`, `user` and `assistant` messages are accepted.
- Sends generic OpenAI-compatible keys only: `model`, `messages`, `temperature`, `max_completion_tokens`, `stream: false`, and `response_format` when a schema is given (§5.3).
- Holds the API key as a `SecretStr` and places it only in the `Authorization` header of each request. The HTTP client is created with `trust_env=False` and `follow_redirects=False`, so proxy variables and redirects cannot change where the key is sent.
- Reads from a reply only `choices[0].message.content`, `finish_reason`, `model` and the two token counts. A reasoning model's `message.reasoning` is never read. From an error body only `error.code` and `error.type` are read, and only when they look like identifiers; `error.message` is ignored.
- Turns every failure into a typed error from `app/ai/errors.py` (§15).

### 3.2 Stub adapters

`anthropic.py`, `openai.py` and `ollama.py` define classes that satisfy the interface but raise `NotImplementedError` from `complete()` and `embed()`. The runtime never constructs or registers them. Connecting one means writing the adapter, adding its settings, and registering it in `build_ai_runtime` — nothing in `AIService` or its callers changes. A Gemini adapter is planned and has no file yet.

### 3.3 The runtime

`app/ai/runtime.py` is the composition root:

- `build_ai_runtime(settings)` loads the prompt templates, builds an `AIService`, and — when AI is configured — constructs the `GroqProvider` and registers it. It performs **no network I/O**. Nothing validates the key or the model against Groq at startup; a wrong key or a retired model is discovered on the first real call and reported as a typed error.
- **AI is off unless `GROQ_API_KEY` is set.** The runtime is "not configured" when `AI_ENABLED` is `false` (kill switch), the key is missing or blank, the key is malformed, or the slot-recommendation prompt template is missing. The application still starts and every non-AI module works; an AI call raises `AINotConfiguredError`.
- `get_ai_runtime()` builds the runtime once per process and never raises: a failed build is logged with the exception's class name and cached as a disabled runtime, so an AI problem cannot break a non-AI request.
- The application lifespan (`app/core/lifecycle.py`) builds the runtime at startup only so the status line (`ai_runtime_configured` / `ai_runtime_disabled`) is logged, and closes the provider's HTTP client on shutdown.

Settings (`app/core/config.py`, "AI runtime" block):

| Setting | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | unset | Groq API key. Unset, empty or whitespace-only means AI is not configured |
| `AI_ENABLED` | unset | Kill switch. Unset = on when a key is present; `false` = off even with a key |
| `AI_FAST_MODEL` | unset | Model the `fast` hint resolves to. Unset = the repository mapping (§4.2) |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Must be `https` (or `http` to localhost), with no credentials, query or fragment |
| `AI_REQUEST_TIMEOUT_SECONDS` | `8` (max 30) | Total deadline for one model call |
| `GROQ_STRICT_JSON_SCHEMA` | `true` | Send a strict JSON Schema as `response_format`; `false` requests plain JSON mode |
| `AI_MAX_CONCURRENT_CALLS` | `4` (1–64) | Provider calls this process may have in flight |

## 4. Prompt Management

**Status: implemented.** Prompts are **not hardcoded strings**. They live as versioned YAML files, loaded by `PromptRegistry` when the runtime is built.

The one prompt in use:

```yaml
# app/ai/prompts/templates/appointment/recommend_slot.yaml (abridged)
id: appointment.recommend_slot
version: 2.0.1
description: Choose one free appointment slot from a doctor's day, from a server-computed list
model_hint: fast   # → maps to a real model at runtime
system: |
  You help a hospital receptionist choose an appointment time.
  You are shown one doctor's day. Choose exactly one of the free slots.
  ...
user: |
  Date: {{ weekday }} {{ date }}. All times are local hospital time on a 24-hour clock.

  The doctor's day has {{ candidate_count }} free slots. ...
  {{ slot_lines }}

  Reply with the JSON object only.
```

Notes on what the registry actually does:

- A template must have `id`, `system` and `user`; `version`, `description`, `model_hint` and `input_schema` are optional. A file that fails to parse or lacks a required field stops the load.
- Rendering is plain `{{ name }}` substitution. **Jinja filters and expressions are not supported**, and `input_schema` is stored but not enforced.
- `patient/summarize.yaml` (v1.0.0) is loaded too, but nothing calls it; patient summaries are not built.

### 4.1 Prompt Versioning

- `id` is stable; `version` bumps on any change
- Every AI call records `(prompt_id, prompt_version)` in its `ai_interaction` log line (§12). Recording to an `ai_interactions` table is planned
- Tying evaluation golden sets to `(prompt_id, prompt_version)` is planned (§14)

### 4.2 Model Hinting

Prompts declare a **capability hint** (`fast`, `deep`, `cheap`, `local`), not a concrete model. The provider registry maps hints to actual models. The mapping in `app/ai/constants.py` (`DEFAULT_HINT_MAPPING`):

| Hint | Provider | Model | Usable today? |
|---|---|---|---|
| `fast` | `groq` | `openai/gpt-oss-20b` | **Yes** — the only working hint |
| `deep` | `anthropic` | `claude-sonnet-4-20250514` | No — adapter is a stub and is not registered |
| `cheap` | `openai` | `gpt-4o-mini` | No — adapter is a stub and is not registered |
| `local` | `ollama` | `qwen2.5:14b` | No — adapter is a stub and is not registered |

A call with a hint whose provider is not registered fails with `AI_NOT_CONFIGURED`.

- The previous `fast` model, `llama-3.1-70b-versatile`, was decommissioned by Groq; its API answers HTTP 400 `model_decommissioned` for it.
- `openai/gpt-oss-20b` is a **reasoning model**: its completion tokens include reasoning tokens, so a caller's `max_tokens` must leave room for both the reasoning and the answer.
- Override the `fast` model per deployment with the `AI_FAST_MODEL` setting — no code change. Only the `fast` hint has a setting today.

## 5. AI Service

**Status: implemented** (`AIService.complete`). The specialised services sketched in earlier versions of this document (`SummarizationService` and others) are planned.

Modules never call providers directly. They call `AIService` in `app/ai/services/ai_service.py`, normally through a small use-case adapter.

`AIService.complete(...)` does the following, in order:

1. Resolves the hint to a `(provider, model)` pair. No registered provider → `AINotConfiguredError`.
2. Applies the caller's `max_tokens` / `temperature`, or the per-hint defaults.
3. Checks the concurrency cap (`AI_MAX_CONCURRENT_CALLS`). At the cap the call is **refused immediately, never queued** → `AIProviderRateLimitedError` (reported as `AI_PROVIDER_UNAVAILABLE`).
4. Calls the provider **at most once**, inside `asyncio.timeout(deadline)` — a total deadline (`AI_REQUEST_TIMEOUT_SECONDS`, default 8 s). There is no retry, no fallback provider and no tool loop.
5. Emits exactly one `ai_interaction` log line, on success and on every failure path (§12).

It does **not** check a budget and does **not** write a database row; both are planned.

### 5.1 The slot-recommendation workflow

The real caller is `AISlotRanker` (`app/services/slot_ranker.py`), which satisfies the `SlotRanker` protocol that `AppointmentService` depends on. The appointment service does not know which model or provider answered.

```
POST /api/v1/appointments/recommend-slot   { patient_id, doctor_id, date }
  │  route: require appointment.recommend_slot AND doctor.availability.read
  ▼
AppointmentService.recommend_slots
  1. Hospital flag feature.ai.slot_recommendation on?      no → 403 FEATURE_DISABLED
  2. AI configured on this server?                         no → 503 AI_NOT_CONFIGURED
  3. Patient and doctor valid in this hospital?            no → 422
  4. Compute the doctor's day with the same slot generator the slot picker uses.
     Free, not-yet-started slots get opaque ids S1, S2, …; the rest carry no id.
  5. No free slot?                                         → 200 status: no_free_slots (no model call)
  6. End the read transaction, then one model call via AISlotRanker.
  7. Validate the reply (§5.3); map the id back to a slot the server holds.
  8. Re-check with fresh queries that the slot is still free → else 503 AI_RESPONSE_INVALID
  ▼
200 status: recommended  { slot_start, slot_end, doctor_id, reason }
```

Nothing is written and nothing is reserved. The booking that may follow is the ordinary `POST /appointments`, with all of its own validation; the database exclusion constraint remains the real guarantee against a double booking.

**Human confirmation.** In the booking dialog the suggestion is optional and labelled as an AI suggestion to review. Pressing **"Use this slot"** only selects that slot in the existing picker, exactly as a click would; nothing is booked until a member of staff presses **"Book"**. If the suggestion fails, the picker still works and the person chooses a slot by hand.

### 5.2 Context boundaries — what reaches the model

Every value rendered into the prompt is produced by the server.

| Sent to the model | Never sent |
|---|---|
| The date and its weekday | Patient identity or any patient data (the `patient_id` in the request is validated and then not used in the prompt) |
| The doctor's day as clock times (`HH:MM-HH:MM`, hospital-local) | Doctor identity, name or specialty |
| An opaque id (`S1`, `S2`, …) on each free slot | Hospital identity, user identity, any database id |
| Unavailable times, marked `unavailable` with no id and no reason | Why a time is unavailable (who is booked, leave, etc.) |
| The count of free slots | Free text typed by anyone |

The opaque ids exist only between the server and the model for one request. The day is capped at 144 slots.

### 5.3 Structured output and validation

- **Schema sent to the provider.** A JSON Schema is built per request: an object with `slot_id` (an `enum` of exactly the ids offered) and `reason`, no other properties. With `GROQ_STRICT_JSON_SCHEMA=true` (the default) it is sent as a strict `json_schema` `response_format`; with `false`, plain JSON mode is requested instead.
- **The schema is not the check.** Whatever the provider was asked to enforce, the server validates the reply itself and repairs nothing:
  - a reply cut off at the token limit is rejected (`truncated`);
  - a reply that is not JSON is rejected (`not_json`) — no code-fence stripping, no brace hunting;
  - a reply of the wrong shape is rejected (`schema_invalid`) — strict Pydantic model, extra fields forbidden;
  - an id that was not offered is rejected (`unknown_candidate`) — checked in the ranker and again in the appointment service.
- **Each rejection is `AI_RESPONSE_INVALID`.** So is a chosen slot that is no longer free when re-checked (`slot_no_longer_free`).
- **The reason is untrusted text.** Non-printable characters are removed, whitespace is collapsed, it is cut to 200 characters, and a reason that cites a candidate id (for example "S5") is withheld altogether — the slot is still returned. The frontend renders it as plain text.
- **Call parameters.** `temperature=0.0`, `max_tokens=1024` (room for the reasoning model's reasoning tokens plus the short answer).
- There is no score, ranking or confidence value in the response: the model returns one choice.

### 5.4 Permissions, tenant isolation and the feature flag

- **Permissions.** The route requires **both** `appointment.recommend_slot` and `doctor.availability.read`. The seed grants `appointment.recommend_slot` to Super Admin, Hospital Admin and Receptionist.
- **Tenant isolation.** The hospital comes from the authenticated user. The patient, doctor, availability, leave and appointment reads are all scoped to that `hospital_id`; a patient or doctor from another hospital fails validation before any model call. `hospital_id` is passed to the AI layer for log attribution only — it is not in the prompt.
- **Feature flag.** `feature.ai.slot_recommendation` lives in the hospital's `settings` JSONB and is on only when the stored value is exactly `true` (`app/core/feature_flags.py`). It is checked first, so a hospital without the feature gets `403 FEATURE_DISABLED` and learns nothing else. The seed defaults the flag on for the demo hospital unless it was set explicitly.
- **Capability read.** `GET /api/v1/hospitals/current/feature-flags` returns one boolean per known flag. `available` is true only when the hospital's flag is on **and** the server has AI configured; it calls no provider. The frontend uses it, together with the two permissions, to decide whether to show the suggestion button.
- **Audit.** The endpoint is read-only — it creates and changes nothing — so it produces no audit-log entry. The booking that follows is audited like any other.

## 6. Function Calling / Tools

**Status: planned — not built.** No model call uses tools today, no tool is registered, and the Groq adapter rejects a tools list. `app/ai/tools/__init__.py` contains an empty `ToolRegistry` and nothing else. The design below is the intent for when AI needs to trigger actions.

When AI needs to trigger actions (book an appointment from a chat request, look up a patient by phone), it will do so through **typed tools** that wrap existing services.

```python
# PLANNED — app/ai/tools/appointment_tools.py does not exist yet

APPOINTMENT_LOOKUP = ToolDefinition(
    name="lookup_appointments",
    description="Look up appointments for a patient within a date range.",
    input_schema={
        "type": "object",
        "properties": {
            "patient_id": {"type": "string", "format": "uuid"},
            "start_date": {"type": "string", "format": "date"},
            "end_date": {"type": "string", "format": "date"},
        },
        "required": ["patient_id", "start_date", "end_date"],
    },
)

async def handle_appointment_lookup(args: dict, actor: User, appt_service):
    validated = AppointmentLookupInput(**args)
    return await appt_service.list_for_patient(
        patient_id=validated.patient_id,
        start=validated.start_date,
        end=validated.end_date,
        actor=actor,  # permission checks apply
    )
```

Rules the design commits to:
- Every tool wraps an existing service method
- Tool arguments are validated against a Pydantic schema before service invocation
- Tool execution runs under the calling user's identity — same permission checks apply
- No tool can escalate privileges, bypass tenancy, or write raw SQL
- Destructive tools require explicit confirmation in the calling context

## 7. Conversational Memory

**Status: planned — not built.** There is no chat assistant and no memory store; `app/ai/memory/` is a placeholder package.

Design intent for the chat AI Assistant — memory per-session, per-user, per-hospital:

- Redis-backed session store, keyed by `(hospital_id, user_id, session_id)`
- TTL: 12 hours idle
- Bounded window: last N turns; older turns get compressed to a summary
- Memory never contains other patients' or other hospitals' data
- Long-term user memory (v2.2) is opt-in per user, retention-limited, and marked in prompts

## 8. RAG (v2.1+)

**Status: planned — not built.** `app/ai/context/` is a placeholder package; `pgvector` is not installed and no embedding call works.

Design intent — Retrieval-Augmented Generation over **approved hospital knowledge sources**:

- Hospital SOPs, drug references, policy documents
- Patient records **only for the retrieving user's authorized scope**
- Embeddings stored in `pgvector` on the same PostgreSQL
- Retrieval queries carry `hospital_id` and permission context; the vector store filters accordingly
- No cross-tenant retrieval, ever

Chunking, embedding model choice, and re-ranking approach documented in the retriever module when we ship it.

## 9. MCP Integration (v2.1+)

**Status: planned — not built.** `app/mcp/` is an empty package; there is no `app/mcp/tools.py`.

Model Context Protocol would let future AI agents (both ours and third-party) talk to Aetheris capabilities safely. Design intent:

- Every MCP tool wraps an existing service method
- MCP tools are registered in `app/mcp/tools.py`
- MCP calls run under an OAuth-authenticated identity with scoped permissions
- Every MCP call is audited (`actor_type = "ai_agent"` in audit logs)

MCP is the eventual surface for third parties to build agents against Aetheris without us shipping SDKs for every language.

## 10. Streaming

**Status: planned — not built.** `AIService.complete` raises on `stream=True` and the Groq adapter does not implement streaming. The one AI endpoint returns a single JSON response.

Design intent — long AI responses stream to the client via SSE:

- Service starts the provider call in streaming mode
- Each chunk yields to an async generator
- The API layer wraps the generator as an SSE stream
- Final chunk carries `total_tokens`, `cost_usd`, `prompt_id`, `model`

## 11. AI Safety Guarantees

These are enforced at the code level, not by prompt engineering alone. The table describes how each holds **for the one capability that exists**; future capabilities must meet the same bar.

| Guarantee | Enforcement today |
|---|---|
| AI never writes to the database | Providers have no DB session. The recommend-slot path performs reads only; nothing is booked or reserved |
| AI never takes an action | No tools exist. The model returns one id; a member of staff presses "Use this slot" and then "Book" |
| AI never bypasses authentication or authorization | The route requires two permissions and the hospital flag before any model call |
| AI output never bypasses validation | The reply is parsed strictly, checked against the ids offered, and the slot re-checked as free; an invalid reply is an error, not a repaired answer |
| AI never sees data outside the caller's scope | The prompt contains only the date and one doctor's schedule as clock times — no patient, doctor, hospital or user data (§5.2) |
| AI is given nothing clinical to judge | The model sees a schedule of clock times and nothing else, and the prompt also instructs it to make no clinical judgement; the feature chooses a time from a schedule |
| AI cannot execute arbitrary code | No code-execution tool; no SQL tool; no eval |
| Secrets and model text stay out of logs and responses | Error messages are static; logs carry identifiers and counts only (§12) |

These are properties of how the code is built. This document makes no claim about the quality of the model's choices; the suggestion is advice for a person to review.

Planned, for clinical-adjacent features that do not exist yet: a "decision support only" disclaimer on every such output, and scope-filtered retrieval for RAG.

## 12. Observability

**Status: partly implemented — structured logs only.** There is **no `ai_interactions` table**, no cost dashboard and no metrics. An `AIInteractionLog` class exists as a shape for a future row; nothing instantiates or persists it.

What exists today:

**One `ai_interaction` log line per call** (`AIService._log_interaction`), INFO on success and WARNING otherwise, always with the same keys:

- `module`, `use_case`, `prompt_id`, `prompt_version`
- `provider`, `model`, `response_model`, `structured_output`
- `input_tokens`, `output_tokens`, `latency_ms`, `finish_reason`
- `status` (`success` / `error` / `timeout` / `not_configured`), `error_kind`, `error_type`
- `cost_estimate_usd`, `cost_known`
- `actor_id`, `hospital_id`, `request_id`

Other AI log events: `ai_runtime_configured` / `ai_runtime_disabled` at startup (with the reason), `ai_provider_error` from the Groq adapter (failure kind, HTTP status, sanitised provider error code, and an operator hint such as which setting to check), and `appointment.recommend_slot` / `appointment.slot_recommendation_rejected` from the workflow.

**What is never logged:** the API key, the prompt text, the model's answer or reason, the provider's error message, or exception text. Only the class name of an exception is recorded. The base URL is logged as host only.

**Cost.** `MODEL_CATALOG` in `app/ai/constants.py` is the one place that says which provider serves a model and what it costs; the per-token price tables are derived from it. The current model (`openai/gpt-oss-20b`) is listed with its price marked unavailable — no figure from Groq's pricing page has been recorded — so its calls log `cost_estimate_usd: null` with `cost_known: false`, never a zero. Token counts are logged and are the reliable usage figure today. A model set through `AI_FAST_MODEL` that the catalog does not list still works and also logs an unknown cost.

Planned:
- An `ai_interactions` table with one row per call
- Dashboards (v2.1): cost by hospital / module / use case, latency percentiles per model, error rate per provider
- Prompt version rollout tracking (v2.2)

## 13. Cost Controls

**Status: mostly planned.** No budget of any kind is enforced.

Implemented today:
- **Per-user per-minute rate limit on AI endpoints** — `RATE_LIMIT_AI_PER_MIN` (default 30) applies to the recommend-slot path in the rate-limit middleware; over the limit is HTTP 429
- **Process-wide concurrency cap** — `AI_MAX_CONCURRENT_CALLS` (default 4); a call above the cap is refused at once, not queued
- **One call per request, bounded output** — no retry multiplies cost; the slot recommendation caps completion tokens at 1024
- **No model call when there is nothing to choose** — a day with no free slot answers without calling the provider

Planned — not built:
- Per-hospital monthly AI budget (soft cap → warning, hard cap → throttling)
- Per-user daily AI budget for chat use cases
- Caching for idempotent requests (input hash → response)
- Batching for embedding jobs
- Hospital Admin configuration of budgets

## 14. Evaluation Harness

**Status: planned — not built.** There is no golden set, no evaluator and no CI evaluation step. `app/ai/evaluation/golden_sets/` is empty. The slot-recommendation prompt is covered by ordinary automated tests that run against a fake HTTP transport, not by a model-quality evaluation.

Design intent — every prompt with a `version` change goes through eval before rollout:

- Golden set per prompt version, e.g. `app/ai/evaluation/golden_sets/<prompt>/<version>.jsonl`
- Evaluators: automated (structure, key facts, LLM-as-judge) + spot-check by clinicians where the use case is clinical
- CI runs eval on prompt-version changes and blocks on regression thresholds

## 15. Failure Modes

**Status: implemented.** Every failure is explicit and typed. There is **no retry and no fallback provider**: a failed call fails the request, and the person chooses a slot by hand.

All AI errors are HTTP **503**; clients tell them apart by error code.

| Error code | HTTP | When |
|---|---|---|
| `AI_NOT_CONFIGURED` | 503 | No key, kill switch off, malformed key, missing prompt template, or a hint whose provider is not registered |
| `AI_PROVIDER_UNAVAILABLE` | 503 | Groq unreachable or answered with an error: connection failure, 401/403 (credentials), 429 (provider rate limit), model not served (`model_not_found`, `model_decommissioned`), 404, other 4xx/5xx — and this process being at its own concurrency cap |
| `AI_PROVIDER_TIMEOUT` | 503 | The connect or read timed out, or the total deadline (`AI_REQUEST_TIMEOUT_SECONDS`) passed |
| `AI_RESPONSE_INVALID` | 503 | Groq answered but the answer could not be used: malformed or empty body, truncated, not JSON, wrong shape, an id that was not offered, the provider's own `json_validate_failed`, or the chosen slot no longer free |
| `FEATURE_DISABLED` | 403 | The hospital's `feature.ai.slot_recommendation` flag is not on (not an AI error; checked before anything else) |

Related non-AI responses on the same endpoint: 403 `PERMISSION_DENIED`, 422 for an invalid patient or doctor, 429 from the rate limiter.

Rules the error handling follows:

- **Messages are static.** The message returned to the browser never contains anything dynamic. The variable part of a failure is a short machine `kind` that goes to the logs only.
- **Typed errors are raised outside `except` blocks**, so the original exception — which can hold the request with its `Authorization` header, or the model's reply — is not attached to the error that propagates.
- **A bug is not dressed up as an outage.** An unexpected exception in our own code surfaces as a 500, not as "provider unavailable".
- **AI failure never blocks the non-AI path.** Booking by hand is unaffected in every case above; the frontend shows a specific message and stops re-asking when asking again cannot help (not configured, not enabled, no permission).

Planned — not built: retry with backoff, fallback to a secondary provider, streaming resume, and surfacing tool-call failures back to the model.

## 16. Model Choice Guidance

Only the first row describes something that runs today. The rest is guidance for use cases that are **not built**, and their hints resolve to stub adapters (§4.2).

| Use case | Hint | Status | Reasoning |
|---|---|---|---|
| Appointment slot recommendation | `fast` | **Implemented** — Groq `openai/gpt-oss-20b` | Latency matters; the task is a small constrained choice |
| Patient summary (routine) | `fast` | Planned | Latency matters |
| Clinical draft note | `deep` | Planned | Quality matters |
| Invoice explanation | `cheap` | Planned | Volume high, quality forgiving |
| Chat assistant | `fast`, with `deep` for complex asks | Planned | UX responsiveness |
| Structured extraction | `deep` | Planned | Reliability of structured output |
| RAG synthesis | `deep` | Planned | Long context, faithfulness |
| Embeddings | provider default | Planned | Consistency |

## 17. Do / Don't

Rules for anyone adding an AI capability. Where a rule depends on something not yet built, it says so.

**Do**
- Add prompt version bumps for any wording change
- Go through `AIService` so every call is logged once
- Send the model the minimum it needs — no PII where it is not needed; prefer opaque ids over real ones
- Ask for structured output **and** validate the reply on the server; reject, never repair
- Re-check any model-chosen value against current data before returning it
- Keep a person in the loop for anything that changes data
- Fail explicitly with a typed error and leave the manual path working
- Test with malformed, truncated and out-of-range model replies

**Don't**
- Put prompts in Python source
- Call a provider outside `app/ai/providers/`
- Give AI raw DB access
- Let AI outputs bypass validation
- Log prompt text, model output, keys or full patient records
- Ship a new clinical-adjacent use case without an eval set (the harness is planned — build it first)
- Assume a fallback exists: today there is one provider and no retry

## 18. Roadmap Alignment

**Where we are (2026-10-06):** provider abstraction, prompt registry, one connected provider (Groq), one capability (appointment slot recommendation, human-confirmed), log-based observability. The scope is intentionally narrow.

Still planned, in the order originally set out:

- **v2.0 (MVP), remaining:** function calling, streaming, the `ai_interactions` table, cost controls and budgets, a second provider with fallback
- **v2.1:** MCP tool surface, RAG for hospital knowledge, evaluation harness in CI, agent scaffolding
- **v2.2:** multi-provider routing on real-time cost/latency, RAG over patient records with strict scope, opt-in user memory
- **v2.3:** multi-agent workflows, administrative agents with a human in the loop by default

---

*AI is the platform differentiator, but "AI" is not the answer to every product problem. Ship AI where it makes a workflow better. Everywhere else, ship good software.*
