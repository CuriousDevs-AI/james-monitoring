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

No alternate provider is silently selected when a budget/quota is exhausted. Free-model use still requires
explicit configuration and does not prove current capacity, availability, privacy suitability or zero cost.
Automatic fallback needs an owner-approved destination/data policy, per-provider limits and failure
classification before it is safe to enable. This increment does not enable it or run live provider probes.
