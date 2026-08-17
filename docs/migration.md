# Moving to another Pi

A working installation is a handful of files in `/etc/pihome-hub`, plus the database in
`/var/lib/pihome-hub`. The checkout, the virtualenv and the unit are all rebuilt by
[`deploy/install.sh`](../deploy/install.sh) on the new host, so nothing about them needs
to survive the move.

## What moves, and what must not

| Path | Move it? | Why |
| --- | --- | --- |
| `/etc/pihome-hub/hub.env` | yes — with one edit, below | The two API keys. Copying it means firmware and phones keep working unchanged |
| `/etc/pihome-hub/relays.yaml` | yes | The wiring. This describes the house, not the Pi |
| `/etc/pihome-hub/sensors.yaml` | yes, **if you have one** | See the warning below |
| `/etc/pihome-hub/automation.yaml` | yes, **if you have one** | Same |
| `/var/lib/pihome-hub/hub.db` | yes, once there are accounts | Users and their password hashes. Leaving it behind means every account has to be created again on the new Pi |
| `/opt/pihome-hub` | no | `git clone` it again |
| `/opt/pihome-hub/.venv` | **never** | A virtualenv hard-codes its own path and links against the Python that built it. Copying one between hosts is how you get an interpreter that half-works |
| `/etc/systemd/system/pihome-hub.service` | no | The installer writes it from the repository, so it can never lag behind the code |

> **The installer will not notice two of those missing.** It only ever creates
> `relays.yaml`, from the example. `sensors.yaml` and `automation.yaml` are optional by
> design — a path that does not exist means the feature is not in use — so a migration
> that forgets them produces a hub that starts cleanly, serves relays correctly, and has
> silently lost every automation rule. The startup line is the tell:
>
> ```
> pihome-hub starting … relays=2 sensors=0 automation_rules=0
> ```
>
> Compare those three numbers against the old Pi's before calling the move done.

## The order matters

`install.sh` behaves differently depending on whether it finds a configuration already
in place, and on a migration you get to choose which case it sees.

**Copy the configuration first** (recommended when the new Pi is already wired). The
script finds `relays.yaml`, treats the host as configured, and **starts the service at the
end of its run** — relays go to their `initial_state` as soon as it finishes.

**Run the installer first** (when the new Pi is not wired yet, or you want to test it on
the bench). It generates a throwaway pair of keys, copies the example wiring, enables the
unit and stops without starting anything. You then overwrite both files with the real ones
and start the service yourself.

Either is fine. Choosing the first while the relays are wired to something that must not
be switched at an arbitrary moment is not.

## Doing it

On the **old** Pi — stop it before the new one takes over. Two hubs answering for the same
house is confusing; two hubs holding GPIO pins is impossible, and the second one to start
simply fails to claim them.

```bash
sudo systemctl stop pihome-hub
sudo tar -czf ~/pihome-config.tar.gz -C /etc pihome-hub
sudo tar -czf ~/pihome-state.tar.gz -C /var/lib pihome-hub   # once there are accounts
```

Stopping first is not only about the relays: SQLite in WAL mode keeps `hub.db-wal`
alongside the database, and copying the pair from a running service can capture a
half-written transaction. A stopped service has checkpointed and removed it.

Copy it across, then on the **new** Pi:

```bash
sudo apt install -y git python3-venv
sudo git clone https://github.com/DGPRoman/pihome-hub.git /opt/pihome-hub

sudo tar -xzf pihome-config.tar.gz -C /etc
sudo tar -xzf pihome-state.tar.gz -C /var/lib     # if you took one
sudo /opt/pihome-hub/deploy/install.sh
```

If you restored a database, `chown -R pihome: /var/lib/pihome-hub` afterwards — systemd
creates that directory for the service account, and a file unpacked as `root` inside it
is one the service cannot write.

The archive carries its own ownership and modes, and the installer repairs the directory
anyway, so `/etc/pihome-hub` ends up `750 root:pihome` with `hub.env` at `600 root:root`
and the YAML at `640 root:pihome` regardless of how the copy went. If you moved the files
by some other route, check them against the table in
[the troubleshooting guide](./troubleshooting.md#permissions) — a directory the service
account cannot traverse reads as "no such file or directory".

### One edit to the copied `hub.env`

`PIHOME_GPIO_BACKEND` is written once, when the file is first created, from what the
installing host had. A copied file therefore carries the **old** Pi's answer, and the
installer will not correct it:

```bash
ls /dev/gpiochip*                                    # what this board actually has
sudo grep PIHOME_GPIO_BACKEND /etc/pihome-hub/hub.env
```

Set it to `gpiozero` on a Pi and `mock` anywhere else. Carrying `gpiozero` onto a machine
without GPIO stops the service with a message saying the extra is not importable — loudly,
which is the right failure. Carrying `mock` onto a real Pi is the dangerous direction: the
service starts, answers every request, and switches nothing.

On a Pi 5, also check `DeviceAllow=` in the unit: it names `/dev/gpiochip0`, and that board
numbers its chips differently.

## Verifying before you trust it

```bash
systemctl status pihome-hub
journalctl -u pihome-hub -n 20 --no-pager | grep starting
curl -s http://127.0.0.1:5002/health
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:5002/v1/relays
sudo -u pihome pihome-hub-admin list
```

Four things to confirm, in order of how easy they are to miss:

1. `relays=`, `sensors=` and `automation_rules=` in the startup line match the old Pi.
2. `backend='gpiozero'`, not `mock`.
3. Every account is listed. An empty list after a move that was supposed to carry
   `hub.db` means the archive was not unpacked, or was unpacked as the wrong user —
   `pihome-hub-admin` says which if it is the second.
4. A relay you can see actually moves — the one check that exercises the wiring rather
   than the configuration describing it.

Anything unexpected: [troubleshooting](./troubleshooting.md).

## Keeping or rotating the keys

Copying `hub.env` keeps both keys, which is usually what you want: every ESP32 keeps
reporting and no phone needs reconfiguring. It also means the old Pi's disk still holds
working credentials for the new one.

To rotate instead, generate a pair on the new host and reflash or reconfigure every client:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Rotate the sensor key on its own if the reason for the move is that the old Pi is going
somewhere you do not control — the sensor key is the one that has been copied onto
firmware and is hardest to account for.

## Decommissioning the old Pi

```bash
sudo systemctl disable --now pihome-hub
sudo rm /etc/pihome-hub/hub.env
sudo rm -rf /var/lib/pihome-hub          # password hashes
```

Disabling matters as much as stopping: a Pi that is plugged back in later, for any reason,
would otherwise start a second hub with valid keys for a house it no longer runs.

`rm`, deliberately, rather than `shred`. Overwriting a file in place assumes the storage
writes it back where it was, and an SD card's controller does not — wear levelling puts
the new data in a different cell and leaves the old one holding the key until it happens
to be reused. If the card is leaving your hands, the only honest options are to rotate
both keys or to reimage it.
