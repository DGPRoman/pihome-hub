# Troubleshooting

Symptoms, what they mean, and what to change. Every message quoted here was produced by
this version of the service; none are paraphrased. The symptoms that involve real GPIO
hardware are reasoned from the code rather than reproduced, and are marked as such —
see [what is not verified](#what-is-not-verified).

## Start here

```bash
systemctl status pihome-hub                       # running, and why not
journalctl -u pihome-hub -n 50 --no-pager         # the last thing it said
curl -s http://127.0.0.1:5002/health              # {"status":"ok"} — no key needed
```

The startup line is worth reading before anything else. It reports what the service
actually loaded, which is usually the answer:

```
pihome_hub.app: pihome-hub starting version='0.1.0' backend='mock' relays=2 sensors=2
automation_rules=2 docs_enabled=False
```

## Reading the exit code

```bash
systemctl show -p ExecMainCode -p ExecMainStatus pihome-hub
```

`ExecMainCode` says how the process ended — exited, or killed by a signal — and
`ExecMainStatus` carries the code or the signal number.

| Exited with | Meaning | What systemd does |
| --- | --- | --- |
| `2` | Configuration is wrong. Every setting and file is read before the port is bound, so this is always reported as one readable line, never a traceback | Nothing. `RestartPreventExitStatus=2` — a restart cannot fix a typo, and a crash loop would bury the line explaining it |
| `3` | The process started but uvicorn could not run — most often the port is already bound | Restarts after 5s. Correct: that condition can clear on its own |
| killed by `SIGTERM` | A clean `systemctl stop`. Shutdown still runs to completion, so `shutdown_state` is applied and pending holds are cancelled | Nothing. `SIGTERM` counts as a success |

## The service will not start

Match the line, not the exit code — they all exit 2.

| Journal line | Cause | Fix |
| --- | --- | --- |
| `configuration is invalid` then `PIHOME_RELAY_API_KEY: Field required` | A key is unset | Generate one into `/etc/pihome-hub/hub.env` |
| `API key looks like example config (contains 'change-me')` | The example value survived | Same, with a real value |
| `API key must be at least 32 characters, got 5` | Too short to be worth having | Same |
| `docs_enabled is true while bound to '0.0.0.0'` | Docs would be served unauthenticated to the network | Leave `PIHOME_DOCS_ENABLED=false`, and reach `/docs` over `ssh -L 5002:127.0.0.1:5002 pi` |
| `could not read relay config at …: No such file or directory` | `relays.yaml` is missing, or unreadable by the service account | Check `PIHOME_RELAY_CONFIG_PATH`, then the [permissions](#permissions) below |
| `… must contain a top-level 'relays' list` | The file parses but is not a relay config | Compare against `config/relays.example.yaml` |
| `invalid YAML in …: while parsing a block mapping` | Broken YAML. The line and column follow the message | Fix at the position quoted |
| `invalid relay configuration in …: Input should be less than or equal to 27` | A pin outside the BCM range | Use a real BCM number, not a physical pin number |
| `pin 17 is used by both 'a' and 'b'` | Two relays on one pin | Give each its own |
| `duplicate relay id 'a'` | Two relays share an id | Ids address relays over HTTP; they must be unique |
| `automation rule 'r1' triggers on device 'ghost', which is not configured. Known devices: []` | A rule names a sensor that does not exist. The known ids are listed for comparison | Fix the id in `automation.yaml`, or add the device to `sensors.yaml` |
| `automation rule 'r1' targets relay 'gate-light', which is not configured` | Same, for the relay side | As above |
| `automation rule 'r1' uses only_after_dark but no location is set` | Darkness needs coordinates | Add a `location` block to `automation.yaml` |
| `PIHOME_GPIO_BACKEND=gpiozero was requested but gpiozero is not importable` | The hardware extra is not installed | `pip install '.[rpi]'` inside `/opt/pihome-hub/.venv` |

An empty relay list is **not** an error: a hub with `relays: []` starts and serves an
empty collection. That is a configured hub with nothing wired, which is different from a
hub that could not be read.

### Permissions

The two files in `/etc/pihome-hub` are read by two different processes, so they do not
share a mode. Getting this wrong reads as "no such file or directory" for the YAML.

```bash
sudo ls -la /etc/pihome-hub
```

| Path | Should be | Read by |
| --- | --- | --- |
| `/etc/pihome-hub` | `750`, `root:pihome` | traversed by the service account |
| `hub.env` | `600`, `root:root` | systemd, as PID 1 |
| `relays.yaml` | `640`, `root:pihome` | the service process, as `pihome` |

`hub.env` stays root-only because systemd opens it and passes the values in as
environment; the service account never needs the secrets. The YAML is opened by the
process itself, so the directory has to be traversable by `pihome` and the file readable
by it. `deploy/install.sh` sets all three; re-running it repairs them.

## It starts, but nothing switches

**Check the backend first.** `backend='mock'` in the startup line means the service is
switching nothing at all — the mock keeps state in memory and answers as if it worked.
This is the default on purpose: an auto-detecting default would fall back to the mock when
the GPIO library failed to load, leaving you convinced the relays are being driven.

```bash
grep PIHOME_GPIO_BACKEND /etc/pihome-hub/hub.env   # gpiozero, on a Pi
ls /dev/gpiochip*                                  # gpiochip0 is what the unit allows
```

| Symptom | Cause |
| --- | --- |
| `could not claim pin 17 for relay 'porch-light': …` at startup | The pin is held by something else, the service user is not in `gpio`, or the extra is missing. The message names all three |
| The unit starts on a Pi 5 but no pin responds | The unit's `DeviceAllow=/dev/gpiochip0` is hard-coded, and a Pi 5 numbers its chips differently. Check `ls /dev/gpiochip*` and edit the unit |
| Every relay is inverted | `active_low` does not match the board. Active-low boards are the norm |
| One relay is inverted and the config looks right | An unrecognised key is currently accepted in silence, so `active-low: true` reads as nothing at all. Check the spelling of every key against `config/relays.example.yaml` |
| Relays change state while the service is stopped | Expected, and not fixable in software. A released GPIO pin returns to an input with no pull, so a stopped service leaves each relay following the board's idle level. `shutdown_state` governs only the moment before release |

## The API refuses the request

| Status | Body | Meaning |
| --- | --- | --- |
| `401` | `{"detail":"Invalid or missing API key"}` | Missing key, wrong key, or the right key for the wrong scope. The three are deliberately indistinguishable to the caller |
| `404` | `{"detail":"no relay configured with id 'nope'"}` | Unknown id |
| `422` | `{"detail":[{"type":"bool_type","loc":["body","on"],…}]}` | `{"on": "yes"}` is refused rather than guessed |
| `429` | `{"detail":"Too many failed authentication attempts"}` | More than `PIHOME_AUTH_MAX_FAILURES` failures from this client within the window |

Two keys, and the split is real: the sensor key pushes readings and can do nothing else,
and the relay key cannot forge a reading. A sensor key on `GET /v1/relays` is a `401`, not
a `403` — the service does not confirm that a key is valid but under-privileged.

The limiter counts per client **and per scope**, which is visible in the journal:

```
pihome_hub.security: authentication failed client='relay:127.0.0.1' scope='relay'
path='/v1/relays' key_present=True recent_failures=2
pihome_hub.security: authentication attempt rejected: too many recent failures
client='relay:127.0.0.1' scope='relay' path='/v1/relays'
```

So a phone retrying a stale key locks out relay reads while sensors keep reporting
normally. Wait out `PIHOME_AUTH_FAILURE_WINDOW_SECONDS` (300 by default) or restart the
service — the counters are in memory.

## A sensor reads wrong

Never reported, stale, and reporting nothing are three different states, and the API keeps
them apart. Flattening them into defaults is how an unplugged sensor comes to read as a
quiet room.

```jsonc
// configured, never pushed: nothing is known
{"id":"porch-motion","stale":true,"last_seen":null,"motion":null}

// fresh, and reporting no motion: something is known, and it is "no"
{"id":"porch-motion","stale":false,"last_seen":"2026-08-12T06:51:36.559660Z","motion":false}

// stale, with history: last known values, explicitly too old to trust
{"id":"porch-motion","stale":true,"last_seen":"2026-08-12T06:51:36.559660Z","motion":false}
```

A device that is always `stale` with `last_seen: null` has never reached the hub at all.
Check the firmware's key and URL, then look for the push in the journal:

```
pihome_hub.api.v1.sensors: reading recorded device_id='porch-motion' motion=True
temperature=18.5 humidity=None rules_fired=[] stale=False
```

`rules_fired=[]` on that line means the reading arrived and no rule matched it, which
moves the problem to the next section.

## A rule does not fire

Every id a rule names is checked at startup, so a typo stops the service rather than
failing quietly at 3am. If the service is running, the ids are sound and the cause is one
of these:

| Cause | How to tell |
| --- | --- |
| The reading carried no motion field | `handle_reading` ignores a reading whose `motion` is `null`. The recorded line shows `motion=None` |
| The rule wants the other edge | `when: {motion: true}` fires on arrival, not on departure |
| It is not dark yet | Raise the log level and look for `rule skipped: not dark yet` |
| The rule is disabled | `GET /v1/automation/rules` still lists it, with `"enabled":false` — a disabled rule is reported, not omitted |

```bash
PIHOME_LOG_LEVEL=DEBUG   # in hub.env; sun times and skipped rules are logged at DEBUG
```

A rule that fires logs `automation rule fired`, and a hold logs `automation hold expired`
when it reverts. Continued motion restarts the countdown rather than queueing a second
timer, so a light stays on while someone is still there — one relay has at most one
pending revert.

## The web app shows nothing

[pihome-hub-web](https://github.com/DGPRoman/pihome-hub-web) talks to this service through
its dev-server proxy, which attaches the key in Node.

| Symptom | Cause |
| --- | --- |
| Both panels report a rejected key | `PIHOME_RELAY_API_KEY` in the web app's own `.env` is unset or stale. It is read in `vite.config.ts`, not by the browser |
| Everything reports the hub did not answer | The hub is not running, or `PIHOME_HUB_ORIGIN` points elsewhere |
| Panels load but sensors say "No readings yet" | Nothing has pushed. That is the hub being honest, not a client bug |

## What is not verified

Stated so the rest can be trusted:

- **Real GPIO output.** `src/pihome_hub/relays/gpio.py` has been checked against
  gpiozero's documented constructors and never run against relays. Treat a first
  deployment as a test. Anything above that involves a claimed pin is reasoned from the
  code, not reproduced.
- **Pi 5 chip numbering.** That `DeviceAllow=/dev/gpiochip0` is wrong there follows from
  the unit being hard-coded; which chip is right on that board is not something I have
  checked.
- **The `gpio` group.** `SupplementaryGroups=gpio` assumes a group that Raspberry Pi OS
  has and many distributions do not. `deploy/install.sh` refuses to continue without it.
