# Model budget boundaries

`budget.daily_tokens_per_agent` is a reported-usage guard, not a currency limit or a guarantee about a
provider's bill. Zero disables it. Usage is shared across that person's models and rooms for the company day.

The runtime checks the guard before a turn and before every additional model attempt, including JSON
repair, file-read follow-ups, transient retries and action-repair calls. Once recorded usage reaches the
limit, no further calls in that turn start. A partially executed turn is not automatically queued for replay:
its previous action results remain authoritative and the owner must inspect them before continuing. New
messages received when the budget is already exhausted retain the existing queue behavior.

Provider-reported cached input is clamped to the reported input count; negative token values cannot reduce
usage. The existing cache discount is an internal estimate, **not provider-specific billing accounting**.
An in-flight call can exceed the remaining budget, and failures/CLI calls without token usage cannot be
accurately charged by this guard. Multiple independent runtime processes are not a transactional spend
reservation system. Use provider-side spending limits for financial enforcement.

Alternate providers are used only when explicitly listed in a model's `fallbacks` (at most two, no nesting).
Quota/rate-limit, overload and temporary connection failures qualify; auth, permission, policy and invalid
configuration failures do not. A successful response is never retried against another model. Fallback applies
only to inference, not already-executed actions or coding jobs. A chain is one bounded inference attempt;
failed providers may have consumed unreported tokens before a backup succeeds. Each new turn/retry starts at
the primary; there is no persistent quota-reset cache or account-rotation mechanism.

The James daily budget still stops work even if a free provider is configured: it is not bypassed. With
fallback enabled, CLI calls start fresh with James's context so a vendor session cannot miss backup turns.
OpenRouter `free_only` checks the model ID and endpoint and sends zero input/output price ceilings; a free-only
primary cannot have paid backups. Gemini free quota is account-dependent and is not a zero-cost guarantee.
Use provider-side spending controls and review data policies before selecting any free service. No live
provider probe is performed automatically when a route is saved.
