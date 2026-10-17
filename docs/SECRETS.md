# Credential storage

This is the first storage layer, not a complete credential broker or agent sandbox.
Existing installations stay on `env`: `.env` is plaintext with mode 0600 and the process environment is shared.
Nothing is automatically migrated, removed, or enabled.

## Opt-in macOS Keychain

Install the optional dependency into the same Python environment that runs James:

```sh
pip install 'james-monitoring[keychain]'
```

In `config.yaml`, choose a stable, unique namespace for this company:

```yaml
secrets:
  backend: keychain
  namespace: my-company-production
```

Keep existing `api_key_env`, `bot_token_env`, and `app_token_env` names: in Keychain mode they are credential
references, not environment lookups. A reference is 3–64 uppercase letters/numbers/underscores, beginning
with a letter. Values are nonempty single-line strings. The Keychain service is
`james-monitoring:<namespace>` and each reference is an account under that service. Use different namespaces
for different companies/environments. Preserve the namespace when moving the configuration.

The backend is selected directly from `keyring.backends.macOS.Keyring`; automatic backend selection and
third-party plaintext backends are not used. A missing entry stays missing. Denied access, an unavailable
backend or a non-macOS host fails without falling back to `.env` or environment values. Install the extra and
grant native Keychain access before enabling this mode; unlock Keychain if reads fail.
If the store is locked or unavailable, console startup and read-only connection/team views remain available.
They distinguish `unavailable` from `missing`; new connector startup still fails closed. Model readiness
reports the storage problem instead of raising an unhandled error. Unlock the store and retry; restart
services that failed to initialise. Existing authenticated clients are not revoked by locking the store.

Existing API-key, Telegram and Slack save flows now use the selected backend. Model probes use an isolated
temporary key instead of changing the process environment. Company, person, project and project-person model
configurations all resolve credentials through the selected store. Keychain values are not copied into
`os.environ`, config files or James backups. Third-party CLI login sessions are still managed by those CLIs,
not this store. The console sign-in key is also outside this store.

## Owner-only management API

After setup, `GET /api/secrets` returns backend, namespace and credential-reference statuses
(`configured`, `missing`, `unavailable`), never values. This explicit check reads Keychain entries and may
trigger native permission prompts. It is not polled automatically.

`POST /api/secrets` accepts `{"name": "OPENAI_API_KEY", "secret": "..."}` with the same authenticated
`X-JM-Key` header as other console routes. Only configured references or known API-provider references can be
saved. The audit entry retains only the reference name. There is no reveal or deletion endpoint. The response
flags when a restart is required: already-created SDK clients or transports may retain their old credential.
The existing model-key route retains its admin permission but can write only model credential references.
The dedicated Settings management screen and per-agent credential grants are follow-up work.

## Migration, revocation and recovery

Stop James before switching an existing installation. Save credentials into the chosen native namespace,
restart and verify each connector. Old `.env` files are intentionally left untouched: review and remove old
plaintext copies separately, including backups. Restart clears values previously inherited into the process.
Changing the backend does not revoke a credential at its provider. Rotate/revoke at the provider when needed,
then update storage and restart clients; in-flight operations are not cancelled by a storage write.

GitHub contains neither the native credential values nor a recoverable Keychain backup. On another machine,
restore/reissue the credentials separately before resuming outbound work. This release does not automate
restore, secret export, switching backends or multi-credential atomic rotation. A partial multi-key save may
need checking before retry. Linux/server vault support is not implemented; do not enable Keychain there.

## Security boundary

Native storage protects credentials at rest; it does not isolate arbitrary code running as the same OS user.
Coding subprocesses now receive the existing environment allowlist instead of the whole parent environment,
but still retain permitted HOME/config/proxy settings and OS-user privileges. A coding agent can still access
files or native credentials available to that OS user. Use separate OS identities/containers and scoped
connector permissions before treating untrusted code as contained. Legacy env mode retains shared environment
semantics. Existing SDK clients can hold decrypted credentials in memory. Do not claim this is a full vault,
per-agent authorisation system, or complete secret redaction guarantee.

Backend behavior and macOS access-control caveats: [keyring documentation](https://keyring.readthedocs.io/en/latest/).
