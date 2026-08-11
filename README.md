# pihome-hub

HTTP control plane for a Raspberry Pi wired to relay-switched circuits.

It exposes relays as a small REST API, accepts readings pushed by ESP32 sensors, and
runs declarative automation rules — the sort that turns on outdoor lights when a motion
sensor fires, but only after dark. It runs on a Raspberry Pi Zero 2 W that stays powered
around the clock, and it is deliberately small enough to read in one sitting.

> **Status: functional, deployment in progress.** Relay control, sensor ingestion and
> automation all work and are covered by tests. There is a systemd unit — see
> [Deployment](#deployment) — but no install script yet, so provisioning a Pi is still
> a manual walk through those steps. See [Roadmap](#roadmap).

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

**Startup and shutdown behaviour is declared, not emergent.** Each relay says what should
happen to it when the process starts (`initial_state`) and as it stops (`shutdown_state`).
A service restart is not, by itself, a reason for the lights to go out.

With one honest caveat, documented rather than glossed over: while the service is not
running it does not own the pins. Releasing a GPIO pin returns it to an input with no
pull, so a stopped service leaves each relay following its board's idle level. That is a
property of the hardware, not something software can override — so `shutdown_state`
governs the window before release, and `preserve` is only as trustworthy as the board's
idle pull. [`config/relays.example.yaml`](config/relays.example.yaml) explains what to
set if you have not measured yours.

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
Python is 3.11 — that is the floor the project supports. It also runs
`systemd-analyze verify` over the unit file, because systemd ignores a directive it does
not recognise and pytest cannot tell a real one from a typo.

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
| `PIHOME_DOCS_ENABLED` | `false` | Serve Swagger UI and the OpenAPI schema. Refused unless bound to loopback |
| `PIHOME_GPIO_BACKEND` | `mock` | `mock` or `gpiozero` — driving real pins is explicit |
| `PIHOME_RELAY_CONFIG_PATH` | `config/relays.yaml` | Relay wiring |
| `PIHOME_SENSOR_CONFIG_PATH` | `config/sensors.yaml` | Sensor devices (optional) |
| `PIHOME_AUTOMATION_CONFIG_PATH` | `config/automation.yaml` | Automation rules (optional) |
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
| `GET` | `/v1/sensors` | Every sensor, its latest reading, and whether it is stale |
| `GET` | `/v1/sensors/{id}` | Read one sensor |
| `POST` | `/v1/sensors/{id}/readings` | Push a reading — **sensor key**, not the relay key |

```console
$ curl -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays
{"relays":[{"id":"porch-light","label":"Porch light","on":false}]}

$ curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays/porch-light/toggle
{"id":"porch-light","label":"Porch light","on":true}
```

Bodies are validated strictly: `{"on": "yes"}` is a `422`, not a guess. An unknown
relay id is a `404`, a bad or missing key is a `401`, and repeated failures earn a `429`.

The two keys divide along a real boundary rather than a decorative one. Firmware pushes
readings and can do nothing else — it cannot read the state of the house, and it cannot
drive a relay directly. The phone reads and controls, and cannot forge a motion event to
reach a relay through an automation rule.

```console
$ curl -X POST -H "X-API-Key: $SENSOR_KEY" -H 'Content-Type: application/json' \
       -d '{"motion": true, "temperature": 21.5}' \
       http://127.0.0.1:5002/v1/sensors/porch-motion/readings
# 202, empty body — the sensor key does not grant reads

$ curl -H "X-API-Key: $RELAY_KEY" http://127.0.0.1:5002/v1/sensors/porch-motion
{"id":"porch-motion","label":"Porch motion","stale":false,"motion":true,...}
```

## Automation

Rules live in [`config/automation.yaml`](config/automation.example.yaml) as data, not code:

```yaml
rules:
  - id: porch-motion-light
    when: {device: porch-motion, motion: true}
    only_after_dark: true
    then: {relay: porch-light, state: on, hold_seconds: 60}
```

Sunrise and sunset come from a `location` block, so the coordinates of a house stay in
its own git-ignored config. Continued motion restarts the countdown rather than queueing
another timer, so a light stays on while someone is still there. Holds are asyncio tasks
that are cancelled on shutdown — nothing is left scheduled by a process that has exited.

Every id a rule names is checked at startup: a rule pointing at a relay or device that
does not exist stops the service with a message naming the rule, rather than failing
silently the first time someone walks past the sensor.

## Deployment

[`deploy/pihome-hub.service`](deploy/pihome-hub.service) runs the service under systemd.
It expects the code at `/opt/pihome-hub` and its environment at
`/etc/pihome-hub/hub.env`; edit the unit if you want other paths.

A service account first. It needs no home, no shell and no group of its own beyond the
one `useradd` makes — the unit grants `gpio` itself, so that membership is not something
to remember here:

```bash
sudo useradd --system --shell /usr/sbin/nologin pihome
```

Then the code, installed rather than linked, so that pip byte-compiles it once instead of
the service recompiling on every start against a read-only filesystem:

```bash
sudo git clone https://github.com/DGPRoman/pihome-hub.git /opt/pihome-hub
sudo python3 -m venv /opt/pihome-hub/.venv
sudo /opt/pihome-hub/.venv/bin/pip install '/opt/pihome-hub[rpi]'
```

Then the environment. systemd reads this file as PID 1 and passes the values in, so it
stays root-owned and the service account never gets to read it:

```bash
sudo install -d -m 700 /etc/pihome-hub
sudo install -m 600 /dev/null /etc/pihome-hub/hub.env
sudoedit /etc/pihome-hub/hub.env
```

```ini
PIHOME_RELAY_API_KEY=paste-a-generated-key
PIHOME_SENSOR_API_KEY=paste-a-different-generated-key
PIHOME_GPIO_BACKEND=gpiozero
PIHOME_RELAY_CONFIG_PATH=/etc/pihome-hub/relays.yaml
PIHOME_SENSOR_CONFIG_PATH=/etc/pihome-hub/sensors.yaml
PIHOME_AUTOMATION_CONFIG_PATH=/etc/pihome-hub/automation.yaml
```

Bare `KEY=value` lines: systemd is not a shell, so quotes end up in the value and `$FOO`
is not expanded. Copy the YAML files next to it, then start the service:

```bash
sudo cp /opt/pihome-hub/deploy/pihome-hub.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pihome-hub

curl -fsS http://127.0.0.1:5002/health
journalctl -u pihome-hub -n 20
```

Upgrading is `git -C /opt/pihome-hub pull`, the same `pip install`, then
`systemctl restart pihome-hub`.

**A bad configuration stops the service instead of looping.** The process exits 2 when
settings do not validate, and the unit refuses to restart on that code, so the message
naming the offending variable stays at the end of the journal rather than scrolling past
every five seconds.

**The sandbox is tight, and two options are deliberately missing from it.** The
filesystem is read-only with no writable exception, capabilities are dropped entirely,
syscalls are filtered to `@system-service`, and `/dev` is denied except the one GPIO
character device. `PrivateDevices=` and `ProcSubset=` are absent on purpose: the first
hides `/dev/gpiochip0`, the second hides the `/proc/device-tree` that gpiozero reads to
identify the board. A test asserts neither gets enabled. Review the rest with
`systemd-analyze security pihome-hub`.

Two things the unit does not solve. `SupplementaryGroups=gpio` assumes that group exists,
which it does on Raspberry Pi OS and often does not elsewhere. And the service still binds
loopback: reaching it from the LAN means setting `PIHOME_HOST`, and since it speaks plain
HTTP with a static key, that should mean a VPN or a reverse proxy terminating TLS, not an
open port.

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
deploy/                pihome-hub.service — the systemd unit
tests/                 runs without hardware, against the mock backend
```

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Project scaffold, configuration, logging, `/health`, CI | ✅ done |
| 2 | Relay backend interface, `gpiozero` and mock implementations, relay service | ✅ done |
| 3 | `/v1` REST API, API-key authentication | ✅ done |
| 4 | Sensor ingestion, declarative automation rules, sun-based conditions | ✅ done |
| 5 | systemd unit, install script, deployment hardening | unit done, script next |
| 6 | Architecture, installation, migration and troubleshooting docs | |

## License

[MIT](LICENSE)
