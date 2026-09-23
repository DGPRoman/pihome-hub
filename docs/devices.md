# HTTP devices

This is the contract between the hub and an HTTP device on the same network — the
half a device's firmware is written against. The device in mind while it was
written is [esp32c3-pc-power](https://github.com/DGPRoman/esp32c3-pc-power), a board
across a PC's front-panel header, and nothing here is specific to it.

A **device** is polled. A **sensor** pushes. That is the whole difference, and it
decides everything below: being the one who asks makes the hub the one who has to
decide when to stop waiting, and being the one who is asked makes the device the one
that has to say where it is.

## The two halves

**Which devices exist is declared** by whoever runs the hub, in the file
`PIHOME_DEVICE_CONFIG_PATH` names. Copy
[`config/devices.example.yaml`](../config/devices.example.yaml) to `devices.yaml`
beside it; the real file is git-ignored.

```yaml
devices:
  - id: workshop-pc
    label: "Workshop PC"
    kind: pc-power
```

**Where each one is, and what key to ask it with, is announced** by the device
itself. Neither is something a file written in advance can be right about: the
address comes from DHCP, and the key is generated on the device at first boot and
shown once.

An announcement naming an id that is not in that file is `404`. Declaring rather
than discovering is what keeps the announcement key from being a way to make the hub
start calling an address nobody chose.

| `kind` | Polled at | Device |
| --- | --- | --- |
| `pc-power` | `/v1/power` | [esp32c3-pc-power](https://github.com/DGPRoman/esp32c3-pc-power) |

The path belongs to the kind and not to the announcement. It is part of the device's
own published contract, and taking it over the wire would let whoever holds the
announcement key aim the hub's authenticated requests at a path of their choosing on
a host it already trusts.

## `POST /v1/devices/{id}/announcements`

Sent on boot, and again whenever the address changes. Authenticated with the hub's
**device key** in `X-API-Key` — `PIHOME_DEVICE_API_KEY`, which is not the relay key
and not the sensor key.

```http
POST /v1/devices/workshop-pc/announcements HTTP/1.1
X-API-Key: <the hub's device key>
Content-Type: application/json

{"address": "http://10.0.0.5", "api_key": "<this device's own key>", "firmware": "0.3.0"}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `address` | string | yes | Origin this device answers on. See the rules below |
| `api_key` | string | yes | The key the hub must present when polling this device. 8–512 characters |
| `firmware` | string | no | Whatever the device calls its build. Recorded, never interpreted. Up to 64 printable characters |

Unknown fields are refused rather than ignored, so a misspelled one is a `422` and
not a setting silently not applied.

| Status | When |
| --- | --- |
| `204` | Recorded. No body: the device key grants no reads, and the device already knows everything it just wrote |
| `401` | Wrong device key, or none configured on this hub |
| `404` | No device with that id is declared |
| `422` | The body, the address or the key is not one this hub will accept |
| `429` | Too many failed authentication attempts from this address |
| `503` | The hub could not write it down |

**An announcement clears everything the poller had recorded** — the last reading, the
last error, the run of failures. A device announces because it has just booted or
just moved, and both make the previous result a statement about a situation that no
longer holds.

Re-announcing an unchanged address is harmless and is the right thing to do on every
boot. `http://10.0.0.5:80/` and `http://10.0.0.5` are the same address, so a device
that spells it differently between builds does not read as having moved.

### What an address may be

Narrow on purpose. This is the one value in the system chosen by an outside party
that the hub then makes an authenticated request to.

* **`http` only.** These devices terminate no TLS and hold no certificate anybody
  could check. Accepting `https` would promise a guarantee that is not there.
* **An IP address, never a hostname.** A name is resolved at request time, by a
  resolver the hub does not control, to whatever the answer is then. Refusing names
  costs nothing here: the device announces its own address, which it knows.
* **A private address.** RFC 1918, IPv6 unique-local, link-local and loopback. A
  device is on the same network as the hub by construction.
* **Origin only.** No path, query, fragment or `user:password@`.

IPv6 is written with brackets: `http://[fd00::1]:8080`.

## What the hub then does

Every `PIHOME_DEVICE_POLL_SECONDS` (30 by default), for each device that has
announced:

```http
GET /v1/power HTTP/1.1
X-API-Key: <the key that device announced>
Accept: application/json
```

The device's own key, not the hub's. Each device generated its own.

For the answer to count, it must be `200`, a JSON **object**, and no larger than
4096 bytes. The body is counted as it arrives rather than trusted to
`Content-Length`, which is a claim by the party being bounded.

The whole exchange has `PIHOME_DEVICE_POLL_TIMEOUT_SECONDS` (5 by default) to
complete — not each read. A device answering one byte at a time cannot hold a poll
open longer than that.

Redirects are not followed. A redirect is the thing on the other end choosing the
hub's next connection, and the key would travel with it.

Anything else — a refused connection, a `401`, a body that is not JSON, one that
does not stop — is recorded as the device not answering, with the reason. Nothing is
skipped in silence.

## Reading it back

`GET /v1/devices` and `GET /v1/devices/{id}`, with the relay key or any session.

```json
{
  "id": "workshop-pc",
  "label": "Workshop PC",
  "kind": "pc-power",
  "address": "http://10.0.0.5",
  "firmware": "0.3.0",
  "announced_at": "2026-03-01T12:00:00+00:00",
  "reachable": true,
  "last_polled_at": "2026-03-01T12:30:00+00:00",
  "last_seen_at": "2026-03-01T12:30:00+00:00",
  "unreachable_since": null,
  "last_error": null,
  "state": {"state": "on", "pending": "none", "observed_at_ms": 412934, "uptime_ms": 498210}
}
```

`state` is the device's own status document, passed through exactly as it was
served. What the fields mean is the device's contract to state — for `pc-power`,
its own `http-api.md` — and re-declaring them here would make every
field it adds a change in two places instead of one.

Every declared device is listed, including ones that have never
announced. That device is the most interesting row in the list, not one to leave
out. It is why three of these fields have three states rather than two:

| | `null` | otherwise |
| --- | --- | --- |
| `address` | never announced | where it last said it was |
| `reachable` | never polled — **not** the same as polled and unreachable | whether the last poll succeeded |
| `unreachable_since` | it is answering | when the current run of failures began |

`unreachable_since` is the start of the run rather than the latest failure. One
dropped packet on wifi is ordinary; an hour of them is not, and only the second is
worth acting on.

A failed poll keeps the last `state` and leaves `last_seen_at` where it was, so a
client can show a reading with its age rather than showing nothing.

No key appears in any of this. The announced credential goes out only to the device
it came from.

## Keys, and rotating them

The hub keeps each announced key in its SQLite database, in the clear. It has to be
replayable on every poll, so a hash would be useless; the file is `0600` inside a
`0700` state directory, which is the protection.

Rotating a device's key means announcing again with the new one. Until then the hub
presents the old key, the device answers `401`, and it is reported as unreachable
with `answered 401` — which is the correct thing to see, and the thing to look for
when a device goes quiet right after a reflash.

Removing a device from the declaration drops its row at the next startup. The
row goes; the bytes are not scrubbed, because SQLite frees a page without
overwriting it. **Rotate the key on a device taken out of service** rather than
assuming it left with the row.

## What this does not do yet

Reads only. There is no route here that presses a button, and `POST /v1/power/press`
on a device is reachable only by whoever holds that device's key directly. Control
through the hub is a separate decision, with a separate set of questions about who
may make it, and it is not made here.
