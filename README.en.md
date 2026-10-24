# SSLTool

A flat-design SSL / SSH / SFTP desktop client (Python + PyQt5).

> 中文版：[README.md](README.md)

## Features

- **SSH terminal**: password / private-key login, a full VT100/VT220 terminal built on pyte (ANSI colors), alternate screen support (`vim` / `less` / `top` and other full-screen programs display correctly), automatic local-echo deduplication
- **Terminal experience**: session logs (auto-saved to `~/.ssltool/logs/` with timestamps, toggleable per tab), in-terminal search (Ctrl+F), URL detection (Ctrl+click opens in browser, right-click menu to copy / open), bracketed paste (multi-line paste is sent as a single unit, so lines are not executed one-by-one as commands)
- **SSL channel**: raw TLS text console (telnet-over-TLS style); after connecting, shows the server certificate's SHA-256 fingerprint, TLS version and cipher suite; optional certificate verification and client certificates
- **SFTP file manager**: directory browsing, **multi-select batch upload / download** (with recursive folder transfer and overall progress), **resumable download** (resume from the local byte offset after interruption, skip targets that are already complete), **drag-and-drop upload / download**, **cancelable transfers**, right-click menu (download / copy path / rename / delete), new-folder creation, **SFTP read-only mode** (upload / delete / modify disabled)
- **SSH jump host (ProxyJump)**: connect to the target server through a jump host (both SSH terminal and SFTP); the jump host's key goes through the same TOFU confirmation, and no credentials are sent to the jump host before confirmation
- **SSH port forwarding**: local forward (-L), remote forward (-R), dynamic SOCKS5 proxy (-D), one rule per line in the session settings; results are printed in the terminal after connecting
- **Session management**: connection profiles are stored in `~/.ssltool/sessions.json`; **session groups** (collapsible tree view), **search filtering**, and **import / export** (JSON format; on import, sessions with the same ID are merged / updated); passwords and key passphrases go to the system keychain (keyring), falling back to `~/.ssltool/secrets.json` when no backend is available — on Windows the fallback is encrypted with DPAPI (Windows Data Protection API) so only the current Windows user can decrypt it; on other platforms it degrades to an obfuscated format (not recommended; install a keyring backend instead)
- **Dark theme**: menu "View → Dark Theme" (Ctrl+D) one-click toggle, preference is saved

## Security

- **SSH host key verification (TOFU)**: before connecting, a key-exchange-only probe connection retrieves the server host key and compares it against `~/.ssltool/known_hosts.json` — **before any password or private-key signature is sent**. The first connection pops up a fingerprint confirmation dialog (compare it with the fingerprint published by your administrator); if a recorded key changes, a prominent warning appears (possible man-in-the-middle attack); after the authenticated connection is established, the actual key fingerprint is re-verified, preventing two TCP connections from being hijacked separately
- **Credential encryption**: fallback-stored passwords are DPAPI-encrypted on Windows (see above)
- **SSL certificate details**: after connecting, shows the server certificate's SHA-256 fingerprint, subject / issuer / serial / SAN, validity period, plus expiry (<30 days) and already-expired warnings

## Installation

1. Install Python 3.8+
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python main.py` (or double-click `run.bat`)

## Building an executable

Double-click `build.bat` (uses PyInstaller; output goes to `dist\SSLTool`).

## Usage

1. Use "Quick Connect" on the left: enter host / port / username, choose the protocol and click "Connect" — or save a session with "New Session"
2. If the SSH password is not saved, you are prompted before connecting; check "Remember password" to store it in the system keychain
3. Private keys support OpenSSH / PEM formats (RSA / Ed25519 / ECDSA / DSA); `.ppk` is not supported (a Paramiko limitation); keys with passphrases prompt on connect
4. The SSL channel is a raw TLS text console with local echo enabled by default, suitable for TLS-based text services (e.g. an `openssl s_server` demo service)
5. The terminal enables "local echo" by default: keystrokes appear locally at once and duplicated server echoes are removed — every character is displayed exactly once whether or not the server echoes; if the display is still wrong, uncheck "Local echo" in the tab's top-left corner
6. If the program exits abnormally, check `~/.ssltool/error.log` for crash records
7. SFTP drag-and-drop: drop files / folders from your file manager onto the right-side remote tree to upload; drag remote items onto the left-side local tree to download them into that folder
8. During a transfer, click "Cancel" on the progress dialog to stop the current batch; remaining unstarted transfers are discarded
9. Menu "View → Dark Theme" (Ctrl+D) toggles dark mode and persists across restarts
10. SSH port forwarding: in the "New / Edit Session" dialog's "Port forwarding" box, write one rule per line in OpenSSH style (`0` as the port means auto-assign):
    - `L:127.0.0.1:8080:10.0.0.5:80` — local port 8080 → server-reachable 10.0.0.5:80 (local forward)
    - `R:127.0.0.1:0:127.0.0.1:3306` — a random server port → local 3306 (remote forward)
    - `D:127.0.0.1:1080` — local SOCKS5 proxy (CONNECT only, no auth)
    After connecting, the actual listening port of each forward is printed in the terminal; all are closed on disconnect.
11. Jump host (ProxyJump): fill in the jump host's address / port / username and auth method (password or private key) in the "Jump host" area of the "New / Edit Session" dialog — filling in an address enables it; both the SSH terminal and SFTP connect through the jump host. The jump host password is asked at connect time and never saved
12. SFTP read-only mode: when a session has "SFTP read-only mode" checked, upload / new folder / rename / delete and drag-and-drop are all disabled — browse and download only
13. SFTP resumable download: when downloading a file whose local counterpart already exists, you are asked to "resume / overwrite / cancel"; resuming continues from the existing local byte count (directory batch downloads ask per file; already-complete targets are skipped automatically)
14. Session groups and search: set a "Group" when creating / editing a session (leave empty for ungrouped); the left session list shows groups as a collapsible tree; the search box above the list filters by name / host in real time
15. Import / export: menu "Session → Export Sessions…" exports all sessions (without passwords) as a JSON backup; "Import Sessions…" reads a JSON file, updating sessions with the same ID and appending new ones

## Terminal shortcuts

| Keys | Action |
| --- | --- |
| Ctrl+C | Send interrupt signal `\x03` (copy when text is selected) |
| Ctrl+Shift+C | Copy selected text |
| Ctrl+V / Ctrl+Shift+V | Paste and send (automatic bracketed paste) |
| Ctrl+F | In-terminal search (Enter next, Shift+Enter previous, Esc close) |
| Ctrl+click URL | Open the link in the system browser |
| Ctrl+A | Select all |
| Arrow keys / Home / End / Ins / Del | Send the corresponding escape sequences |
| PgUp / PgDn | Scroll back through the history buffer |
| Shift+PgUp / Shift+PgDn | Send `\x1b[5~` / `\x1b[6~` escape sequences |

## Notes and limitations

- The terminal is based on pyte's VT100/VT220 emulation: xterm 16/256 colors, bold / underline / reverse video, scrollback buffer, and the alternate screen (alt-screen, 1049 / 47 modes — full-screen programs like `vim`, `less`, `top` run in a separate screen buffer and the main screen is restored on exit)
- SSH host keys use a Trust-On-First-Use (TOFU) policy: the first connection requires manual fingerprint confirmation and key changes produce a warning (see "Security"); confirmations are stored in `~/.ssltool/known_hosts.json`
- Port forwarding rules do not support IPv6 literal addresses; SOCKS5 supports only the CONNECT command with no authentication
- Jump host authentication supports only password and private key; the jump host password is never saved (asked each time); both the target's and the jump host's host keys go through the TOFU confirmation flow

## Project layout

```
SSLTool/
├── main.py                    # entry point
├── requirements.txt
├── run.bat / build.bat
├── app/
│   ├── config.py              # session and credential storage
│   ├── theme.py               # flat QSS theme and app icon
│   ├── core/
│   │   ├── workers.py         # SSH / SSL connection threads (cert details + jump host)
│   │   ├── forwarders.py      # SSH port forwarding (-L / -R / -D SOCKS5)
│   │   ├── hostkey.py         # host key TOFU probe / verify / known_hosts storage
│   │   ├── jump.py            # jump host connection and direct-tcpip channels
│   │   ├── session_log.py     # session logs (logs dir, timestamped filenames)
│   │   └── sftp_worker.py     # SFTP connection thread (resumable download)
│   └── ui/
│       ├── terminal_widget.py # full VT terminal widget on pyte (alt-screen / search / URL / bracketed paste)
│       ├── terminal_tab.py    # terminal / SSL console tab
│       ├── sftp_tab.py        # SFTP file manager tab (drag-and-drop / cancelable)
│       ├── hostkey_dialog.py  # host key confirmation / change warning dialogs
│       ├── connect_dialog.py  # new / edit session dialog
│       └── main_window.py     # main window
└── tests/
    ├── smoke_test.py          # offscreen smoke tests (89 checks)
    ├── gui_boot.py            # main window startup self-check
    └── e2e_test.py            # end-to-end tests with an embedded SSH server (echo / dedup / scrolling / dialog / port forwarding / TOFU / terminal experience / jump host / resume, 11 tests)
```
