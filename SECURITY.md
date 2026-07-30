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
| Example credentials reaching production | Startup validation rejects keys shorter than 32 characters and keys that still look like the shipped example |
| Secrets in the repository | Credentials live only in `.env`, which is git-ignored; no key, address or coordinate is present in source |
| Route map disclosure | OpenAPI and Swagger UI are off unless `PIHOME_DOCS_ENABLED=true` |
| Stack fingerprinting | The `Server` response header is suppressed |
| Accidental network exposure | The default bind address is `127.0.0.1`; reaching the service from another host is an explicit operator decision |

**Not defended against — read this before exposing the service**

- **No transport encryption.** The service speaks plain HTTP. The API key travels in a
  header, so anyone able to observe the connection can replay it. Do not expose it
  directly to the internet.
- **No protection against a compromised client.** A key held by a phone or a sensor is
  a key an attacker who owns that device also holds.
- **No multi-user model.** There are two roles, not user accounts, and no audit trail
  of who acted.
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
