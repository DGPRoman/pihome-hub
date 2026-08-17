# Security

This service switches mains-voltage circuits in a physical home. A compromise is not
an information leak — it is someone else operating your house. The notes below describe
what the project defends against, what it deliberately does not, and how to deploy it
without creating an easy target.

## Reporting a vulnerability

Please report privately rather than opening a public issue: use
[GitHub's private vulnerability reporting](https://github.com/DGPRoman/pihome-hub/security/advisories/new)
on this repository. A response should be expected within a week. This is a personal
project with no commercial support and no bug bounty.

## Threat model

**Defended against**

| Threat | Mitigation |
| --- | --- |
| Unauthenticated relay control | Every `/v1` route requires an API key; `/health` is the only unauthenticated endpoint and returns no build detail |
| Credential theft from a sensor device | Sensor ingestion and relay control use separate keys, so a key recovered from firmware cannot switch relays |
| Timing attacks on key comparison | Keys are compared with `secrets.compare_digest` |
| Online key guessing | Failed attempts are counted per client and per scope over a sliding window; exhausting the allowance returns `429`. See the limits of this below |
| Route map disclosure through the docs | `/docs` and `/openapi.json` carry no credential, so enabling them is refused unless the service is bound to loopback |
| Probing for which endpoint exists | A missing key and a wrong key return an identical `401` body, and no error echoes either the supplied or the expected key |
| Session replay from a stolen database | Only the SHA-256 of each token is stored, so the rows cannot be presented as cookies |
| A session outliving the account behind it | Resolving one re-reads the account, so disabling, demoting or deleting takes effect on the next request rather than at expiry |
| A changed password leaving old logins alive | `pihome-hub-admin passwd` ends that account's sessions and reports how many |
| Password recovery from a stolen database | Passwords are stored as salted `scrypt` hashes and never reversibly; `hub.db` is `0600` inside a `0700` state directory that `StateDirectoryMode=` sets |
| Username enumeration through an account list | `pihome-hub-admin` is a local command, not a route. No HTTP endpoint reveals which accounts exist |
| A password on a command line | The admin tool refuses to take one as an argument — `ps` shows every argument to every account on the machine, and shell history keeps it |
| Example credentials reaching production | Startup validation rejects keys shorter than 32 characters and keys that still look like the shipped example |
| Secrets in the repository | Credentials live only in `.env`, which is git-ignored; no key, address or coordinate is present in source |
| Route map disclosure | OpenAPI and Swagger UI are off unless `PIHOME_DOCS_ENABLED=true` |
| Stack fingerprinting | The `Server` response header is suppressed |
| Accidental network exposure | The default bind address is `127.0.0.1`; reaching the service from another host is an explicit operator decision |

**Not defended against — read this before exposing the service**

- **No transport encryption.** The service speaks plain HTTP. The API key travels in a
  header, so anyone able to observe the connection can replay it. Do not expose it
  directly to the internet.
- **Rate limiting slows guessing; it does not stop a determined attacker.** The counter
  is keyed on the peer address — collapsed to the `/64` for IPv6, since a routed
  allocation holds about 1.8×10¹⁹ addresses that would each otherwise earn a fresh
  allowance. An attacker with many unrelated networks still gets one allowance per
  network. The table of tracked clients is capped to bound memory, and eviction drops
  the fewest-failures entry first so churn cannot push a blocked client out, but the cap
  is still a cap. The real defence is a key with enough entropy to be unguessable and a
  service that is not reachable from the internet — not this limiter.
- **Key length is checked; key entropy is not.** A 32-character passphrase passes
  validation and may carry far less entropy than 32 random characters. Generate keys
  with `secrets.token_urlsafe`, as `.env.example` shows.
- **No trusted-proxy support.** `X-Forwarded-For` is deliberately ignored, because
  honouring it unconditionally would let any caller forge its own identity and bypass
  the limiter. Behind a reverse proxy every request therefore looks like it comes from
  the proxy, and rate limiting belongs in the proxy instead.
- **No protection against a compromised client.** A key held by a phone or a sensor is
  a key an attacker who owns that device also holds.
- **Offline guessing of a stolen hash is slowed, not prevented.** `scrypt` at 16 MiB is
  a deliberate compromise for a board with 512 MB of RAM, not the strongest setting
  available. A weak password in a stolen database is still a weak password; the 12-
  character minimum is a floor, not a guarantee.
- **Nothing reaches accounts or sessions over HTTP yet.** Both are stored, and both can
  be managed locally; the endpoints that would issue and accept a session, and the role
  checks on routes, are still to come. Until then the two API keys are the whole of the
  access control, and a role recorded against an account restricts nothing.
- **A session cannot be revoked from another device.** `pihome-hub-admin` on the Pi can
  end them — by changing the password, or by disabling the account — but there is no
  "sign out everywhere" for someone holding only a phone.
- **A session lasts 30 days from login regardless of use.** A token copied off a device
  stays valid for the remainder of that window unless the password is changed or the
  account disabled. Shorten `PIHOME_SESSION_LIFETIME_SECONDS` if that trade is wrong for
  your household.
- **No audit trail.** Nothing records who acted, only what the service did.
- **Physical access wins.** Anyone at the distribution board does not need this API.

## Deploying safely

Pick one of these. The first is both simpler and stronger.

1. **Private network (recommended).** Keep the service on `127.0.0.1` or a LAN address
   and reach it over WireGuard or Tailscale. No port forwarding, no certificates, no
   domain, and the API key stops being the only thing between the internet and your
   lights.
2. **TLS reverse proxy.** Terminate HTTPS in Caddy or nginx and forward to the service
   on loopback. This needs a hostname — a publicly trusted certificate cannot be
   obtained for a bare IP address through the usual automated flow.

Either way:

- Keep `PIHOME_HOST=127.0.0.1` whenever something else fronts the service.
- `chmod 600 .env`, owned by the service user.
- Enable unattended security upgrades on the host. Dependencies are pinned for
  reproducibility, which means updates are a deliberate act — do them.
- Rotate a key by editing `.env` and restarting the unit. Remember that clients
  embedding the old key need rebuilding.

## Handling secrets in this repository

- Never commit `.env`. It is git-ignored, and `.env.example` carries placeholders that
  the application actively refuses to accept.
- Keep host addresses, GPS coordinates and network topology in configuration, not in
  source. `config/*.yaml` is git-ignored for exactly this reason; only
  `*.example.yaml` templates are tracked.
- If a secret is ever committed, treat it as public permanently and rotate it.
  Rewriting history does not retract what has already been fetched, forked or indexed.
