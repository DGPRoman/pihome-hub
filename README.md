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
| `PIHOME_GPIO_BACKEND` | `mock` | `mock` or `gpiozero` — driving real pins is explicit |
| `PIHOME_RELAY_CONFIG_PATH` | `config/relays.yaml` | Relay wiring |
| `PIHOME_AUTH_MAX_FAILURES` | `10` | Failed auth attempts per client before 429 |
| `PIHOME_AUTH_FAILURE_WINDOW_SECONDS` | `300` | Window those failures are counted over |

Relay wiring lives in its own file, because it describes a house rather than a
process. Copy [`config/relays.example.yaml`](config/relays.example.yaml) to
`config/relays.yaml` — the real file is git-ignored.

## API

Everything under `/v1` requires an `X-API-Key` header. Idempotent operations use
`PUT`; `toggle` is a `POST`, since replaying it does not produce the same result twice.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness. The only unauthenticated endpoint |
| `GET` | `/v1/relays` | Every relay and its state |
| `PUT` | `/v1/relays` | Set every relay to the same state — body `{"on": true}` |
| `POST` | `/v1/relays/toggle` | Invert every relay independently |
| `GET` | `/v1/relays/{id}` | Read one relay |
| `PUT` | `/v1/relays/{id}` | Set one relay — body `{"on": false}` |
| `POST` | `/v1/relays/{id}/toggle` | Invert one relay |

```console
$ curl -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays
{"relays":[{"id":"porch-light","label":"Porch light","on":false}]}

$ curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays/porch-light/toggle
{"id":"porch-light","label":"Porch light","on":true}
```

Bodies are validated strictly: `{"on": "yes"}` is a `422`, not a guess. An unknown
relay id is a `404`, a bad or missing key is a `401`, and repeated failures earn a `429`.

## Project layout

```
src/pihome_hub/
├── __main__.py        entry point the systemd unit runs
├── app.py             ASGI application factory
├── config.py          settings and validation
├── logging.py         stdout logging, text or JSON
├── api/
│   ├── system.py      /health — unversioned, unauthenticated
│   └── v1/            relay and sensor routes (authenticated)
└── relays/
    ├── backend.py     RelayBackend protocol — the hardware seam
    ├── mock.py        in-memory backend for development, tests and CI
    ├── gpio.py        real backend via gpiozero (needs the 'rpi' extra)
    ├── models.py      RelayConfig: pin, polarity, startup behaviour
    ├── config.py      loads config/relays.yaml
    └── service.py     logical on/off/toggle over configured relays
config/                relays.example.yaml — copy and edit; the real file is ignored
tests/                 runs without hardware, against the mock backend
```

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Project scaffold, configuration, logging, `/health`, CI | ✅ done |
| 2 | Relay backend interface, `gpiozero` and mock implementations, relay service | ✅ done |
| 3 | `/v1` REST API, API-key authentication | ✅ done |
| 4 | Sensor ingestion, declarative automation rules, sun-based conditions | next |
| 5 | systemd unit, install script, deployment hardening | |
| 6 | Architecture, installation, migration and troubleshooting docs | |

## License

[MIT](LICENSE)
