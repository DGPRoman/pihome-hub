# pihome-hub

HTTP control plane for a Raspberry Pi wired to relay-switched circuits.

It exposes relays as a small REST API, accepts readings pushed by ESP32 sensors, and
runs declarative automation rules — the sort that turns on outdoor lights when a motion
sensor fires, but only after dark. It runs on a Raspberry Pi Zero 2 W that stays powered
around the clock, and it is deliberately small enough to read in one sitting.

> **Status: functional and deployable.** Relay control, sensor ingestion and automation
> all work and are covered by tests, and two files provision a Raspberry Pi. See
> [Deployment](#deployment), [Architecture](docs/architecture.md),
> [Troubleshooting](docs/troubleshooting.md) and [Moving to another
> Pi](docs/migration.md).

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
Python is 3.11 — that is the floor the project supports. It also checks the two files in
[`deploy/`](deploy/), which Python tooling does not read: `systemd-analyze verify` over the
unit, because systemd ignores a directive it cannot spell, and `shellcheck` over the
install script.

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
| `PIHOME_DATABASE_PATH` | `$STATE_DIRECTORY/hub.db` | Accounts. Follows the unit's `StateDirectory=`; falls back to `var/hub.db` off systemd |
| `PIHOME_SESSION_LIFETIME_SECONDS` | `2592000` | How long a login lasts (30 days), from when it happened rather than from the last request |
| `PIHOME_SESSION_COOKIE_SECURE` | `false` | `Secure` on the session cookie. Only true behind a TLS proxy — over plain HTTP the browser would never send it |
| `PIHOME_AUTH_MAX_FAILURES` | `10` | Failed auth attempts per client before 429 |
| `PIHOME_AUTH_FAILURE_WINDOW_SECONDS` | `300` | Window those failures are counted over |

Relay wiring lives in its own file, because it describes a house rather than a
process. Copy [`config/relays.example.yaml`](config/relays.example.yaml) to
`config/relays.yaml` — the real file is git-ignored.

## Accounts

Accounts live in the SQLite file the unit's `StateDirectory=` decides, and are managed
with `pihome-hub-admin` rather than over HTTP — the first admin cannot be created through
an API that requires an admin.

```bash
pihome-hub-admin create roman --role admin   # prompts for the password, twice
pihome-hub-admin list
pihome-hub-admin passwd roman                # also ends that account's open sessions
pihome-hub-admin role anna operator
pihome-hub-admin disable guest               # its sessions stop at once; the password is kept
pihome-hub-admin delete guest
```

| Role | May |
| --- | --- |
| `admin` | everything, including creating and removing accounts |
| `operator` | switch relays and read everything — the everyday account |
| `viewer` | read only: sees what the house is doing, changes nothing |

On a Pi the database belongs to the service account, so run the tool as that account:

```bash
sudo -u pihome pihome-hub-admin list
```

As root it would leave behind a root-owned database that the service cannot write, and
systemd does not repair that — so it refuses, and names the account to use instead.

Three things it will not do. It will not take a password as an argument: a command line is
visible in `ps` to every account on the machine and lands in shell history. Passwords are
read from the terminal, or from stdin when there is no terminal, which is what a
provisioning script wants:

```bash
echo "$PASSWORD" | pihome-hub-admin create anna --role operator
```

It will not accept a password under 12 characters. And it will not delete, disable or
demote the last enabled admin — none of the three is undoable through any interface this
service offers, and the fix would be editing SQLite by hand over SSH.

Sessions last 30 days from the moment of login rather than from the last request, which
keeps the read path free of database writes — a Pi runs on an SD card. Disabling, demoting
or deleting an account takes effect on its next request, because resolving a session
re-reads the account rather than trusting what was true when it was opened. A password
change is the one that has to be said out loud, which is why `passwd` ends the sessions
itself.

**Nothing logs in over HTTP yet.** Accounts and sessions are stored, and both can be
managed on the Pi; the endpoints that issue and accept a session are the next piece of
work.

## API

Everything under `/v1` requires an `X-API-Key` header, except the login route — which is
what makes it the way in. Idempotent operations use `PUT`; `toggle` is a `POST`, since
replaying it does not produce the same result twice.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness. Unauthenticated |
| `POST` | `/v1/session` | Log in — body `{"username": …, "password": …}`. Sets the session cookie |
| `GET` | `/v1/session` | Who the cookie says you are. `401` if it says nothing usable |
| `DELETE` | `/v1/session` | Log out. `204` either way |
| `GET` | `/v1/relays` | Every relay and its state |
| `PUT` | `/v1/relays` | Set every relay to the same state — body `{"on": true}` |
| `POST` | `/v1/relays/toggle` | Invert every relay independently |
| `GET` | `/v1/relays/{id}` | Read one relay |
| `PUT` | `/v1/relays/{id}` | Set one relay — body `{"on": false}` |
| `POST` | `/v1/relays/{id}/toggle` | Invert one relay |
| `GET` | `/v1/sensors` | Every sensor, its latest reading, and whether it is stale |
| `GET` | `/v1/sensors/{id}` | Read one sensor |
| `POST` | `/v1/sensors/{id}/readings` | Push a reading — **sensor key**, not the relay key |
| `GET` | `/v1/automation/rules` | Every configured rule, including the disabled ones |

```console
$ curl -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays
{"relays":[{"id":"porch-light","label":"Porch light","on":false}]}

$ curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays/porch-light/toggle
{"id":"porch-light","label":"Porch light","on":true}
```

Bodies are validated strictly: `{"on": "yes"}` is a `422`, not a guess. An unknown
relay id is a `404`, a bad or missing key is a `401`, and repeated failures earn a `429`.

### Logging in

```console
$ curl -c jar -X POST -H 'Content-Type: application/json' \
       -d '{"username":"roman","password":"…"}' http://127.0.0.1:5002/v1/session
{"username":"roman","role":"admin","expires_at":"2026-09-16T07:24:26.297982Z"}

$ curl -b jar http://127.0.0.1:5002/v1/session
{"username":"roman","role":"admin","expires_at":"2026-09-16T07:24:26.297982Z"}

$ curl -b jar -X DELETE http://127.0.0.1:5002/v1/session    # 204, cookie cleared
```

The token is in the cookie and nowhere else — not the body, not a header. The cookie is
`HttpOnly`, so a cross-site scripting bug in a web client cannot read a credential that
outlives the page, and `SameSite=Strict`, which is the whole of the cross-site request
forgery defence; see [SECURITY.md](SECURITY.md).

Every kind of wrong answers the same `401` with the same wording: no such account, wrong
password, and disabled account are not distinguished, because which one it was is not the
caller's to learn. Failed attempts are counted in their own bucket, so somebody guessing
at the login form cannot lock out the firmware.

**Sessions do not yet grant anything but `/v1/session`.** The relay and sensor routes still
require their API key. Accepting a session on them, with the roles it carries, is the next
piece of work.

### The two keys

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

Two files provision a Pi: the unit,
[`deploy/pihome-hub.service`](deploy/pihome-hub.service), and the script that installs it,
[`deploy/install.sh`](deploy/install.sh).

```bash
sudo git clone https://github.com/DGPRoman/pihome-hub.git /opt/pihome-hub
sudo /opt/pihome-hub/deploy/install.sh
```

That creates the service account, builds a virtualenv in the checkout, generates both API
keys, copies the example wiring, and enables the unit. Describe your own wiring, then
start it:

```bash
sudoedit /etc/pihome-hub/relays.yaml
sudo systemctl start pihome-hub.service
sudo grep PIHOME_RELAY_API_KEY /etc/pihome-hub/hub.env
```

Upgrading is `git -C /opt/pihome-hub pull` and the same script again. It rewrites neither
a generated key nor an edited YAML file, and on a host that is already configured it
restarts the service and waits for `/health` before claiming success. Replacing the Pi
rather than upgrading it: [Moving to another Pi](docs/migration.md).

| Path | Ownership | Holds |
| --- | --- | --- |
| `/opt/pihome-hub` | `root` | the checkout and its `.venv` |
| `/etc/pihome-hub/hub.env` | `root:root`, `600` | both API keys and every `PIHOME_*` setting |
| `/etc/pihome-hub/relays.yaml` | `root:pihome`, `640` | the wiring |
| `/etc/systemd/system/pihome-hub.service` | `root` | the unit |

**Two permissions, because two different things read them.** `hub.env` is opened by systemd
as PID 1, which passes the values in as environment — so the service account never needs
the secrets and does not get them. The YAML is read by the process itself, so its
directory is group-readable and the file is group-owned by `pihome`.

**The script reads the unit rather than repeating it.** Paths, the service account and the
GPIO group are parsed out of `pihome-hub.service`, so editing the unit is enough and the
two cannot disagree about where anything lives.

**Real pins are opted into by hardware, not by a flag.** The script installs the `rpi`
extra and selects the `gpiozero` backend when `/dev/gpiochip0` is there, and the mock
backend when it is not — so the same command provisions a Pi and a test box. A host with
GPIO chips but no `gpiochip0` (a Pi 5 numbers them differently) is an error rather than a
silent downgrade to the mock.

**A fresh install is enabled but not started.** The example wiring names pins chosen for
somebody else's board, and starting on it would close relays at random. So the first run
stops after `systemctl enable` and says what to edit; the run after that starts the
service.

**A bad configuration stops the service instead of looping.** Every settings variable and
every configuration file is read before the process starts listening, and any of them
being wrong exits 2 — a code the unit refuses to restart on. So the one line naming the
offending variable or rule stays at the end of the journal rather than scrolling past
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
├── admin.py           pihome-hub-admin — account management at a terminal
├── app.py             ASGI application factory
├── config.py          settings and validation
├── security.py        API-key authentication, two scopes
├── ratelimit.py       failure counting behind the 429
├── logging.py         stdout logging, text or JSON
├── api/
│   ├── system.py      /health — unversioned, unauthenticated
│   └── v1/            relay, sensor and automation routes (authenticated)
├── relays/
│   ├── backend.py     RelayBackend protocol — the hardware seam
│   ├── mock.py        in-memory backend for development, tests and CI
│   ├── gpio.py        real backend via gpiozero (needs the 'rpi' extra)
│   └── service.py     logical on/off/toggle over configured relays
├── sensors/           declared devices and the latest reading from each, in memory
├── automation/        rules, the engine that applies them, and sunrise/sunset
├── accounts/          users, roles, and scrypt password hashing
└── storage/           the SQLite file: connection pragmas and schema versioning
config/                relays.example.yaml — copy and edit; the real file is ignored
deploy/                pihome-hub.service and install.sh — provisioning a Pi
docs/                  architecture.md, troubleshooting.md, migration.md
tests/                 runs without hardware, against the mock backend
```

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Project scaffold, configuration, logging, `/health`, CI | ✅ done |
| 2 | Relay backend interface, `gpiozero` and mock implementations, relay service | ✅ done |
| 3 | `/v1` REST API, API-key authentication | ✅ done |
| 4 | Sensor ingestion, declarative automation rules, sun-based conditions | ✅ done |
| 5 | systemd unit, install script, deployment hardening | ✅ done |
| 6 | Architecture, installation, migration and troubleshooting docs | ✅ done |
| 7 | Accounts, roles and sessions | in progress |

Phase 7 is what unblocks the [web client's](https://github.com/DGPRoman/pihome-hub-web)
own roadmap. Storage, password hashing, the user store and `pihome-hub-admin` are in
place; the session endpoints and role enforcement are not yet.

## License

[MIT](LICENSE)
