# MinerTune

## ⚠️ USE AT YOUR OWN RISK ⚠️

> [!CAUTION]
> **MinerTune changes the voltage and clock settings of mining hardware. Incorrect
> use, overtuning, or hardware/firmware faults can DAMAGE OR DESTROY your miner,
> void your warranty, and in the worst case create FIRE or ELECTRICAL hazards.**
>
> This software is provided **"AS IS", without any warranty** (see [`LICENSE`](LICENSE)).
> The author (NFDiJee) accepts **NO liability** for any damage, loss, or harm resulting
> from its use. **You are solely responsible for what you do with your hardware.**
>
> - **Undervolting** is the safer direction; tuning **ABOVE stock** increases heat, wear and risk.
> - **Never** expose the control dashboard to the public internet.
> - Always keep the **emergency stop** reachable and monitor temperatures (**VR first**).
> - If you do not understand what frequency/voltage tuning does, do **NOT** use the
>   "allow above stock" option.

---

**MinerTune** is a self-hosted web control center for tuning ASIC miners that run
**Harlo-OS** (API v1). It measures your miner point by point, finds the minimum stable
core voltage (Vmin) for each frequency, and shows the best operating points for
efficiency, performance, or a target hashrate. Everything runs locally on your LAN.

> Harlo-OS is the first supported firmware; support for other firmwares may follow.
> "Harlo" is a third-party brand name. It is **not** part of the package name, and
> MinerTune is not affiliated with Harlo-OS or any miner vendor.

---

## ⚠️ Safety first

- **Set the control PIN immediately** after installation. Until a PIN is set, all
  write actions are locked (the emergency stop always works).
- **Never expose MinerTune to the internet.** No port forwarding, no public reverse proxy.
  It is meant for a trusted LAN only. Use a VPN if you need remote access.
- **Emergency stop:** the big red button (`ASICs OFF NOW`) switches mining off immediately,
  with no PIN and no confirmation. HARD temperature limits (ASIC 80 °C / VR 95 °C by
  default) trigger the same shutdown automatically.
- **Overclocking** (above stock frequency/voltage) needs an explicit opt-in and a
  confirmation *gate* when you start the sweep. MinerTune never sets values above the
  profile's `frequency_max` / `core_max`. That wall is checked before every single write.
- Sweeps use `save=false`: a reboot of the miner restores its saved settings.
  Saving a point permanently needs an extra confirmation.
- You tune your hardware at your own risk. See `LICENSE` (no warranty).

---

## Features

- **Connect & discover:** enter the miner IP or scan your subnet; "Identify" blinks the miner display.
- **Live view:** hashrate, wall power (measured or estimated), J/TH, ASIC/VR temperatures, input voltage.
- **Sweep modes**
  - *Efficiency*: lower frequency range, searches upward, stops once the valley is captured.
  - *Performance*: upper range down from stock, with a fast precheck descent.
  - *Full*: the complete band.
  - *Target hashrate*: a directed search around the frequency needed for your target TH/s.
- **Three-stage point evaluation:** a quick precheck, an early abort for underpowered
  points, and a full measurement window with a hit target. This keeps sweeps short without
  trusting marginal points.
- **Safeguards on every sample:** HARD/SOFT temperature limits, an input-voltage
  watchdog, a dead-man switch for API errors, and a hard wall at the profile limits.
  The miner is always reset to its start point when a sweep ends or stops.
- **Sweet spots:** best efficiency (min J/TH), performance knee, and best compromise.
- **Best point for a target hashrate:** pick from any finished run and apply it
  (temporarily, or permanently with extra confirmation).
- **History & export:** every run is stored as JSON; export to CSV, JSON, XLSX and PDF.
- **Languages:** English (default), German, Spanish, French, Italian, Portuguese, Dutch — switchable in the UI;
  more languages can be added as a file in `lang/` (see [`lang/GLOSSARY.md`](lang/GLOSSARY.md)).
- **Themes:** dark/light and accent colors.

### Screenshots

> _Placeholders – add images to `docs/screenshots/` and update the links._
>
> - `docs/screenshots/dashboard.png`: live view and matrix
> - `docs/screenshots/plan.png`: sweep planning
> - `docs/screenshots/history.png`: history, sweet spots and export

---

## Requirements

- Raspberry Pi OS, Debian or Ubuntu with **systemd**
- **Python ≥ 3.9** with `venv` (the installer adds `python3-venv` via apt if it is missing)
- Network access from the host to the miner (HTTP) and internet access during installation (PyPI)
- A miner running Harlo-OS with API v1 (`http://<miner-ip>/api/v1/status`)

## Installation

```bash
git clone https://github.com/NFDiJee/minertune.git
cd minertune
sudo ./install.sh
```

At the end, the installer prints the LAN URL, for example `http://192.168.1.20:8477`.
Open it, **set the PIN**, and enter your miner's IP.

The same steps work on all supported platforms:

| Platform | Notes |
|---|---|
| Raspberry Pi OS (Bookworm or newer) | Pi 3/4/5 and Zero 2 W are fine; the control center itself is light. |
| Debian 12/13 | `sudo apt install git python3 python3-venv` first on minimal installs. |
| Ubuntu 22.04/24.04 | same as Debian. |

Installer options (environment variables):

```bash
sudo INSTALL_DIR=/srv/minertune ./install.sh   # other install location (default /opt/minertune)
sudo MINERTUNE_PORT=8479 ./install.sh          # port for a newly created config.json
```

If port 8477 is already taken when the config is first created, the installer
automatically uses **8479** and tells you so.

### What goes where (`/opt/minertune`)

| Path | Owner / mode | Content |
|---|---|---|
| `/opt/minertune/` | `root:minertune`, `1775` (sticky) | program files (root-owned, read-only for the service) |
| `/opt/minertune/venv/` | `root` | Python virtual environment (openpyxl, reportlab) |
| `/opt/minertune/config.json` | `minertune`, **`0600`** | settings **incl. the control PIN**, created from `config.example.json` |
| `/opt/minertune/runs/` | `minertune`, `0750` | run results (JSON), live state |
| `/etc/systemd/system/minertune.service` | `root`, `0644` | systemd unit |

- The service runs as the system user **`minertune`**, which has no login shell and no home
  directory.
- The unit is hardened: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  `NoNewPrivileges`, IPv4/IPv6 sockets only, and write access only to `/opt/minertune`.
- To read the data as a regular user, use `sudo`, for example
  `sudo ls /opt/minertune/runs`.

---

## Usage

1. **Open** `http://<host>:8477` in a browser on your LAN.
2. **PIN:** on first start you'll see *Set control PIN* (at least 4 characters). The PIN is
   needed for everything that writes to the miner. Status, plan preview and
   emergency stop work without it.
3. **Connect:** enter the miner IP and click *Connect*, or use *Search miner* to scan the subnet.
4. **Plan a sweep:** choose the mode (*Efficiency / Performance / Full / Target hashrate*)
   and the resolution (fine/coarse). Optionally tick *full measure* (every point gets
   the full window). The plan shows the band, the order and the estimated duration.
   Plans above stock show the **GATE** warning and need confirmation.
5. **Start:** the matrix shows progress per frequency. Each voltage step is shown with its
   stage and reason. You can stop at any time; the miner is reset to its start point.
6. **Results:** sweet spots (efficiency, knee, compromise) appear in the history. Use
   *Find best point* with a target TH/s to get the recommended point (Vmin + reserve)
   and apply it.
7. **Export:** CSV / JSON / XLSX / PDF per run from the history.
8. **Language:** use the language selector in the header (English, German, Spanish, French,
   Italian, Portuguese, Dutch); the choice is
   remembered per browser. The default comes from `language` in `config.json`.

### Configuration (`config.json`)

All fields are listed in `config.example.json`. The most important ones:

| Key | Default | Meaning |
|---|---|---|
| `miner_url` | `http://192.168.1.100/api/v1` | miner API base (also set via *Connect*) |
| `bind_host` / `bind_port` | `0.0.0.0` / `8477` | where the web UI listens |
| `control_pin` | `CHANGEME` | `CHANGEME` = not set yet (write actions locked) |
| `window_min_s` / `target_hits` / `window_max_s` | 600 / 600 / 900 | measurement window per point |
| `freq_low_pct`, `floor_pct`, `*_step_factor` | 0.52, 0.75, 25/5 | search band relative to stock |
| `allow_above_stock`, `freq_high_pct` | `false`, 0.0 | overclocking (gate required) |
| `soft_*_c` / `hard_*_c` | 70/85, 80/95 | SOFT = discard point, HARD = emergency shutdown |
| `freq_verify_tol_mhz` | 2 | accepted difference between set and reported frequency (PLL rounding, e.g. 720 → 721) |
| `settle_ma_tol_frac` / `warmup_max_s` | 0.05 / 90 | settle = 30 s moving averages differ by ≤ 5 % (noise tolerant); max. warmup before measuring anyway |
| `language` | `en` | default UI language (`en`, `de`, `es`, `fr`, `it`, `pt`, `nl`) |

After editing, restart the service with `sudo systemctl restart minertune`.
If you only changed the PIN, `sudo systemctl reload minertune` is enough.
Forgot the PIN? Reset it to first-setup mode:

```bash
sudo -u minertune /opt/minertune/venv/bin/python /opt/minertune/control.py --reset-pin \
     --config /opt/minertune/config.json && sudo systemctl reload minertune
```

---

## Managing the service

```bash
systemctl status minertune          # state
sudo systemctl restart minertune    # restart (a running sweep is stopped and the miner reset)
sudo systemctl stop minertune
journalctl -u minertune -f          # live log
journalctl -u minertune -n 200      # last 200 lines
```

### Update

```bash
cd minertune
git pull
sudo ./install.sh     # replaces program files + dependencies, restarts the service
```

`config.json` and `runs/` are **never overwritten** by the installer, so your settings,
PIN and history stay.

### Uninstall

```bash
sudo ./deinstall.sh   # also available as /opt/minertune/deinstall.sh
```

This stops and disables the service and removes the unit. It then **asks separately**
whether to delete the run data, `config.json`, the program files and the `minertune`
user. The default for each question is *No*.

---

## Development

You can run it without installing:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.json config.json && chmod 600 config.json
venv/bin/python -u control.py          # UI on http://<host>:8477
```

`sweep_full.py --plan-only` prints the derived test plan (GET only, no writes).

---

## License

MinerTune is released under the **MIT License**. Copyright (c) 2026 NFDiJee. See [`LICENSE`](LICENSE).
Third-party components and their (MIT-compatible) licenses are listed in
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).

---

## Publishing on GitHub (maintainer notes)

The local repository is already initialized with an initial commit. To publish it:

1. Create a **new, empty** repository `minertune` on GitHub under `NFDiJee`. Do not add a
   README, license or `.gitignore` there.
2. Add the remote and push:

   ```bash
   # SSH (recommended, requires an SSH key added to your GitHub account)
   git remote add origin git@github.com:NFDiJee/minertune.git
   # or HTTPS (requires a personal access token when git asks for a password)
   # git remote add origin https://github.com/NFDiJee/minertune.git

   git branch -M main
   git push -u origin main
   ```

- You set up the SSH key or access token yourself. Never put tokens into the repository,
  scripts or remote URLs.
- **Never commit `config.json`** (it contains your PIN and miner address) **or `runs/`**
  (your measurement data). Both are in `.gitignore`. Check with `git status` before every
  commit.
