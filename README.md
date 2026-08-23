# PS4 PKG Installer

Send `.pkg` files from your computer to a jailbroken PS4 over your local network, with real progress tracking, pause/resume and cancel.

Cross-platform (macOS, Windows, Linux), single Python file, one dependency.

**[Léeme en español](README.es.md)**

---

## Why another sender

There are good tools already — [DirectPackageInstaller](https://github.com/marcussacana/DirectPackageInstaller) and PS4 PKG Sender among them. This one exists because the usual "just run `python3 -m http.server`" advice **silently breaks installs**, and the failure looks like something else entirely.

Python's `SimpleHTTPRequestHandler` does not support HTTP `Range` requests. It answers `200 OK` with the whole file no matter what byte range you asked for.

The PS4 reads a package's metadata by requesting specific byte offsets inside it. From [`pkg.c`](https://github.com/Backporter/ps4_remote_pkg_installer-OOSDK) in the Remote PKG Installer:

```c
http_download_file(piece_urls[0], &param_sfo_data, &param_sfo_dl_size, NULL, param_sfo_offset);
```

`param.sfo` lives at an arbitrary offset — a few megabytes into the file. Ask a plain Python server for those bytes and it hands you the bytes from offset zero instead. The console then fails to parse the result and reports:

```
Unable to load system file object for package '...'
```

Which reads like a firmware or corruption problem. It is neither. Worse, `http.c` accepts the wrong status code without complaint:

```c
return (status_code == 200 || status_code == 206);
```

So nothing warns you. This tool ships an HTTP server that implements `Range` properly (`206 Partial Content`, `Content-Range`, `416` when unsatisfiable), which is what makes it work.

---

## Requirements

**On the PS4**

- A jailbroken console with GoldHEN.
- The **Remote Package Installer** homebrew installed — get the `.pkg` from **[pkg-zone (title ID `FLTZ00003`)](https://pkg-zone.com/details/FLTZ00003)**. Note that flatz's [GitHub repo](https://github.com/flatz/ps4_remote_pkg_installer) publishes no releases, only source, so the compiled package comes from homebrew stores. On recent firmware, prefer a build made with the [OOSDK](https://github.com/Backporter/ps4_remote_pkg_installer-OOSDK).

  Chicken-and-egg: you cannot install this one through itself. Use a USB stick, or GoldHEN's *Debug Settings → Package Installer → Install From HTTP* pointed at any local web server.
- That app **open and in the foreground**. This is not optional. Its own documentation is explicit:

  > To be able to use this tool for receiving commands you need to have this application in focus (not in a background, because PS4 will suspend it and it won't be possible to use network anymore).

  Press the PS button and the port dies.

**On your computer**

- Python 3.8+
- `flet` (installed below)
- Same LAN as the console. Wired beats Wi‑Fi by a lot for multi‑gigabyte packages.

---

## Install

### Prebuilt binary — no Python needed

Grab your platform's archive from the **[latest release](https://github.com/KonixDev/ps4-pkg-installer/releases/latest)**:

| Platform | File |
|---|---|
| macOS (Apple Silicon) | `ps4-pkg-installer-macos-arm64.zip` |
| Windows | `ps4-pkg-installer-windows-x64.zip` |
| Linux | `ps4-pkg-installer-linux-x64.tar.gz` |

Intel Macs are not covered by a prebuilt binary — GitHub's `macos-13` runners are being retired and queue indefinitely. Run from source, or build your own with the command further down; both work fine on Intel.

The macOS build is unsigned, so Gatekeeper will complain the first time. Right-click → *Open*, or:

```bash
xattr -dr com.apple.quarantine "PS4 PKG Installer.app"
```

### From source

```bash
git clone https://github.com/KonixDev/ps4-pkg-installer.git
cd ps4-pkg-installer
pip install -r requirements.txt
python3 ps4_pkg_installer.py
```

That is the whole setup. No Node, no .NET, no build step.

### Build it yourself

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name "PS4 PKG Installer" \
  --collect-all flet --collect-all flet_desktop ps4_pkg_installer.py
```

---

## How to use

**1. Point it at your packages.** Click *Change* and pick the folder. It searches subfolders too, so you can point it at one parent directory holding several games. Files that carry a `.pkg` extension but lack the PS4 package signature (`7F 43 4E 54`) are skipped and named in the console, so stray installers and build artifacts do not end up queued.

**2. Find the console.** Type its IP, or click the radar icon to sweep your subnet for anything listening on port 12800.

**3. Check the status chips.** Two of them, top right:

- `Server :8000` — the local file server. It starts by itself; there is no button. If the port is taken it tries the next ones and tells you which it settled on.
- `PS4 ready` — the console. See below.

**4. Got compressed releases?** If the folder holds `.rar`, `.zip` or `.7z` files, a banner shows up above the list with how many there are and how big they are. Type the password if the release needs one and hit *Extraer*: once it finishes, the `.pkg` files land in the list below on their own.

Multi-volume releases (`.part1.rar`, `.part2.rar`, …) count as a single entry — just keep every part in the same folder. If one is missing, the button stays disabled and the log names it. Free space is checked before writing a single byte, so a short disk stops the run instead of leaving a half-written `.pkg`. Deleting the archives afterwards is an opt-in checkbox, off by default.

There are two extractors inside. 7-Zip does the work and ships bundled, but it reads the headers of any RAR5 without implementing every compression codec: hand it a genuinely compressed RAR and it lists the contents perfectly, then dies on extraction with `Unsupported Method`. When that happens the run retries with `unar` (or `unrar`, if you have it), bundled as well. Most releases use RAR in *store* mode — a PKG is already compressed and encrypted, so recompressing buys nothing — but when a compressed one shows up, there is no opening it without the second engine.

**5. Tick the packages you want and press Install.**

Each row then shows its own state, progress bar, percentage, bytes transferred and ETA, with pause and cancel buttons. The console downloads several packages at once, so several rows advance together.

---

## Understanding the console status

Three states, and the difference matters:

| Chip | Meaning |
|---|---|
| `PS4 ready` | The app answered an actual API call. Good to go. |
| `PS4 stuck` | The port is open but the app is not answering. |
| `PS4 not responding` | Nothing on port 12800. |

**`PS4 stuck` is the one worth knowing about.** The Remote PKG Installer runs on `sandbird`, a single-threaded event loop, and it performs the whole metadata download *inside* the request handler. One request that hangs takes the server down for good — while the TCP port stays open, because the kernel completes the handshake whether or not the app is listening.

A plain TCP connect check reports "connected" against a completely dead app. This tool sends a real `POST /api/is_exists` instead, so it can tell the difference and tell you to restart the app on the console.

---

## Progress and control

`/api/install` returns a `task_id`. With it, the tool polls `/api/get_task_progress` for the console's own view of the transfer — bytes moved, remaining seconds, unpacking percentage — which is why the phases read *Preparing → Downloading → Installing* rather than one undifferentiated bar. *Installing* is `local_copy_percent`: the console unpacking after the bytes have arrived.

The same `task_id` drives `/api/pause_task`, `/api/resume_task` and `/api/stop_task`, which is where the per-row buttons and *Cancel all* come from.

**When the console goes quiet.** RPI serves the API and the PKG from the same thread, so while a large transfer is running `/api/get_task_progress` stops answering entirely — measured: twelve consecutive ten-second timeouts, zero replies, with the download moving along fine. Silence here is normal, not a lost task.

That is why progress has two sources. While the console answers, it wins. When it goes quiet, the bar switches to what the local HTTP server sees: every `Range` the PS4 requests carries the exact byte it has reached, and that source cannot saturate because it is your own machine. The row says *avance medido en el servidor local* and hides the ETA, which would be stale by then.

**One caveat:** RPI's responses are not valid JSON. It writes integers as hex literals:

```c
sb_writef(s, "{ \"status\": \"fail\", \"error_code\": 0x%08X }\n", code);
```

`0x80990085` is not JSON, so `json.loads()` dies on the `x` with `Expecting ',' delimiter: line 1 column 36 (char 35)` — and a genuine console error surfaces as a Python parse error instead. (DirectPackageInstaller sidesteps this by matching the string `"success"` rather than parsing.) This tool converts hex literals before parsing, so real error codes come through and get translated:

| Code | Meaning |
|---|---|
| `0x80990085` | Not enough free space on the PS4 |
| `0x80990088` | A task already exists for that content |
| `0x8099000E` | Content is already installed |

---

## Troubleshooting

**"Unable to load system file object"** — the console downloaded the package's `param.sfo` and could not parse it. Almost always a server not honouring `Range`, which is exactly what this tool fixes. If it persists with this tool, the `.pkg` itself is likely truncated or corrupt.

**Install times out with no download requests in the console log** — the app on the PS4 is stuck. Close it and reopen it. Click *Test* first; it will say so.

**Firmware mismatch** — a package built for a newer firmware than your console will be refused. Older packages run on newer firmware; the reverse does not. Note that this failure is *not* the "system file object" error above — that one happens earlier, while reading metadata.

**Transfer stalls partway** — do not let the computer sleep. On macOS, `caffeinate -i` while the transfer runs.

---

## Notes

Keep the app running for the whole transfer. Your machine is the file server; close it and the download dies. The console's download manager works in the background, so the PS4 side keeps going on its own once a task is registered.

Editing the source while a transfer is in flight is safe — Python reads the file once at startup and does not hold it open. Changes apply on the next run.

---

## Credits

- [flatz](https://github.com/flatz/ps4_remote_pkg_installer) — original Remote Package Installer
- [Backporter](https://github.com/Backporter/ps4_remote_pkg_installer-OOSDK) — OOSDK build for recent firmware
- [marcussacana](https://github.com/marcussacana/DirectPackageInstaller) — DirectPackageInstaller, the reference for correct `Range` handling on the server side

## Disclaimer

A file transfer utility for homebrew-enabled consoles. Use it with content you have the right to use. Nothing here circumvents any protection, and no copyrighted material is distributed with it.

## License

MIT — see [LICENSE](LICENSE).
