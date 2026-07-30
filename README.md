# pihome-hub

HTTP control plane for a Raspberry Pi wired to relay-switched circuits.

It exposes relays as a small REST API, accepts readings pushed by ESP32 sensors, and
runs declarative automation rules — the sort that turns on outdoor lights when a motion
sensor fires, but only after dark. It runs on a Raspberry Pi Zero 2 W that stays powered
around the clock, and it is deliberately small enough to read in one sitting.

> **Status: early.** The scaffold, configuration and operational endpoints are in place.
> Relay control, sensor ingestion and automation are landing next — see [Roadmap](#roadmap).

## Why this exists

I wanted the lights in my yard on a switch that lives in my pocket. Off-the-shelf home
automation platforms solve that, and a great deal more that I do not need, on hardware
that has more memory than a Pi Zero. This is the smallest thing that does the job
properly: pinned dependencies, a mock hardware backend so it is testable on a laptop,
secure defaults, and enough documentation to move it to a new Pi without remembering
anything.

## Design notes

**Hardware access sits behind a backend interface.** One implementation drives real GPIO
pins; another is a mock. The service, its tests and its CI run identically against the
mock, so nothing about developing this requires a Raspberry Pi.

**Relay polarity is configuration, not code.** Active-low relay boards are the norm and
the inversion is easy to get subtly wrong. Each relay declares its own `active_low`, and
the raw pin level never escapes the backend — the API speaks only in logical states.

**Restarts do not disturb the house.** Each relay declares what should happen to it when
the process starts: preserve the current state, or force a known one. A service restart
is not a reason for the lights to go out.

**Secure by default.** Loopback bind address, no OpenAPI schema, no `Server` header, two
independent API keys, and startup validation that rejects a key still carrying the
example value. See [SECURITY.md](SECURITY.md).

## Quick start (development)

No Raspberry Pi required.

```bash
git clone https://github.com/DGPRoman/pihome-hub.git
cd pihome-hub

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
# Generate the two keys the service needs:
python -c "import secrets; print(secrets.token_urlsafe(48))"
# ...then paste them into .env

PIHOME_DOCS_ENABLED=true pihome-hub
```

Then `curl http://127.0.0.1:5002/health`, and open <http://127.0.0.1:5002/docs> for the
API browser.

On a Raspberry Pi, add the hardware extra: `pip install -e '.[rpi]'`.

## Development

```bash
ruff format .          # format
ruff check .           # lint
mypy                   # type-check (strict)
pytest                 # test
```

CI runs all four on Python 3.11–3.13. The Pi Zero 2 W runs Raspberry Pi OS, whose system
Python is 3.11 — that is the floor the project supports.

## Configuration

Everything is read from environment variables prefixed `PIHOME_`, or from a `.env` file
beside the project. [`.env.example`](.env.example) documents each one.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PIHOME_RELAY_API_KEY` | *required* | Authenticates relay control clients |
| `PIHOME_SENSOR_API_KEY` | *required* | Authenticates sensor devices pushing readings |
| `PIHOME_HOST` | `127.0.0.1` | Bind address |
| `PIHOME_PORT` | `5002` | Bind port |
| `PIHOME_LOG_LEVEL` | `INFO` | Root log level |
| `PIHOME_LOG_JSON` | `false` | One JSON object per log record |
| `PIHOME_ACCESS_LOG` | `false` | Log every HTTP request |
| `PIHOME_DOCS_ENABLED` | `false` | Serve Swagger UI and the OpenAPI schema |

## Project layout

```
src/pihome_hub/
├── __main__.py        entry point the systemd unit runs
├── app.py             ASGI application factory
├── config.py          settings and validation
├── logging.py         stdout logging, text or JSON
└── api/
    ├── system.py      /health — unversioned, unauthenticated
    └── v1/            relay and sensor routes (authenticated)
tests/                 runs without hardware, against the mock backend
```

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Project scaffold, configuration, logging, `/health`, CI | ✅ done |
| 2 | Relay backend interface, `gpiozero` and mock implementations, relay service | next |
| 3 | `/v1` REST API, API-key authentication, legacy compatibility shim | |
| 4 | Sensor ingestion, declarative automation rules, sun-based conditions | |
| 5 | systemd unit, install script, deployment hardening | |
| 6 | Architecture, installation, migration and troubleshooting docs | |

## Relationship to the previous version

This replaces an earlier private project of mine that grew organically and accumulated
the usual problems: credentials in source, no tests, business logic entangled with pin
access, and relay polarity handled inconsistently in five places. Rather than refactor it
in place, I rewrote it — the API is versioned this time, and a compatibility shim keeps
the existing Android client working while it is migrated.

## License

[MIT](LICENSE)
