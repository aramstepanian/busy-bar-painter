# Busy Bar sky painter

A Python 3 painter for the BUSY Bar front display (72 by 16). It draws a sky that follows real sunrise and sunset for your location, a sun, a moon with the real lunar phase, constellations and stars, and weather: clear, clouds, fog, rain, and snow. The clock is 12-hour hour and minute only, such as `2:08`.

Each new frame is posted with the same element ids, so the bar overwrites the picture in place.

## Clone and run

```bash
git clone https://github.com/aramstepanian/busy-bar-painter.git
cd busy-bar-painter
./run.sh
```

`./run.sh` creates a `.venv` the first time, installs `requirements.txt`, and starts the painter. Python 3.9 or newer is required. The default transport is USB, so plug the bar in with a USB cable before you start.

Defaults are Santa Clara, CA (`37.3541`, `-121.9552`, `America/Los_Angeles`). To change them, copy the example file and edit it:

```bash
cp .env.example .env
```

The painter reads environment variables first. Anything still unset is filled from `.env` next to `animator.py`. `.env` is listed in `.gitignore`.

## Check that the bar answers over USB

```bash
curl -fsS --max-time 3 http://10.0.4.20/api/version
```

A JSON body such as `{"api_semver":"27.5.0"}` means the bar is on the USB link. `GET /api/status` is the fallback and returns firmware, power, and `system.api_semver`. Leave `GET /api/status/firmware` unused: on firmware 1.2.4 it accepts the TCP connection and then sends no bytes, so a client waits until it times out. Drawing uses `POST /api/display/draw`.

```bash
./run.sh --diagnose
```

That prints the version probe and one test draw, each with its HTTP status and body, then exits.

## If 10.0.4.20 does not respond

The USB cable presents the bar as a network device at `10.0.4.20`. macOS may ask you to allow that USB network device. Until you allow it, curl sits until it times out.

1. Unplug the bar and plug it back in.
2. When macOS asks to allow the USB network device, allow it.
3. Open System Settings, then Network, and confirm the new USB interface is connected.
4. Run the curl command again.

Leave `BUSY_PIN` empty for the USB cable. The bar's own docs treat USB as a trusted link and do not require the HTTP API PIN there. If a draw comes back HTTP 403, set `BUSY_PIN` to the PIN from the bar's local web interface (Network, HTTP API). The painter sends that value in the `X-API-Token` header.

USB frames go out one at a time, about every 2 seconds, and each draw waits up to 8 seconds. An unchanged frame is not sent again. One slow draw stays on that 2 second cadence. Two failures in a row switch to a longer wait.

## If weather goes blank or DNS fails

The Mac can lose DNS while the Busy Bar USB interface is first in the service order. Weather and the ISS lookup then fail with `nodename nor servname provided`. The painter keeps the last good weather in `~/.busy-bar-painter/weather.json` and keeps showing that temperature. It tries again every 5 minutes, and waits longer after each failure.

When that happens the log says this once:

`DNS failed. In System Settings > Network > Set Service Order, move the Busy Bar USB interface below Wi-Fi.`

## Corporate proxy

Some work Macs have no direct DNS or internet. System Settings shows a proxy auto-config (PAC) instead. Python then fails with `Errno 8` on `socket.gethostbyname`, and a plain `curl` to the weather service returns `000`.

Weather and the ISS honor `BUSY_PROXY`, then `HTTPS_PROXY`, then `HTTP_PROXY`. On a Mac, if none of those is set, the painter runs `scutil --proxy`, fetches that PAC file directly, and uses the first `PROXY host:port` line. The log names the proxy host once and does not print a username or password. Requests to `10.0.4.20`, `localhost`, and anything in `NO_PROXY` always go direct, including every draw to the bar.

If the proxy answers HTTP 407, set `BUSY_PROXY` to `http://user:password@host:port`. If certificate checks fail because the proxy intercepts HTTPS, set `BUSY_CA_BUNDLE` to your corporate root CA file. `BUSY_INSECURE_TLS=1` skips verification for weather and the ISS only. That setting is not recommended.

`./run.sh --diagnose` prints the version probe, one draw, and a weather fetch.

## Pause the hosted Fly painter

If a painter hosted on Fly is also drawing to this bar, pause that app before you start this one. Two painters writing the screen at the same time will take turns overwriting each other.

## Cloud mode

Set `BUSY_TRANSPORT=cloud` and `BUSY_BAR_TOKEN` to a bar-scope token from your BUSY account. The painter posts to `https://api.busy.app/busybar/display/draw` with `Authorization: Bearer` and a desktop Chrome user agent, which is what gets the request past Cloudflare. `BUSY_BAR_CLOUD_TOKEN` is still accepted when `BUSY_BAR_TOKEN` is empty.

## Environment

| Variable | Default | Role |
| --- | --- | --- |
| `BUSY_TRANSPORT` | `usb` | `usb` or `cloud` |
| `BUSY_HOST` | `http://10.0.4.20` | Bar origin on the USB link, such as `http://10.0.4.20` |
| `BUSY_PIN` | empty | Optional HTTP API PIN, sent as `X-API-Token` |
| `BUSY_BAR_TOKEN` | empty | Cloud token. Required for `cloud` |
| `BUSY_LAT` | `37.3541` | Latitude |
| `BUSY_LON` | `-121.9552` | Longitude |
| `BUSY_TZ` | `America/Los_Angeles` | IANA time zone for the clock and the sky |
| `BUSY_LABEL` | `Santa Clara, CA` | Place name written to the log |
| `BUSY_PROXY` | empty | Proxy for weather and the ISS. Wins over `HTTPS_PROXY` and `HTTP_PROXY` |
| `HTTPS_PROXY` | empty | Used when `BUSY_PROXY` is empty |
| `HTTP_PROXY` | empty | Used when `BUSY_PROXY` and `HTTPS_PROXY` are empty |
| `NO_PROXY` | `10.0.4.20,localhost,127.0.0.1` | Hosts that always connect directly. The bar is always direct |
| `BUSY_CA_BUNDLE` | empty | Corporate root CA file, when the proxy intercepts HTTPS |
| `BUSY_INSECURE_TLS` | empty | `1` skips TLS checks for weather and the ISS only. Not recommended |

Other commands:

A single draw timeout stays on the normal cadence. The longer wait starts after two failures in a row, and that timeout is logged once.

```bash
./run.sh --once       # draw one frame and exit
./run.sh --diagnose   # print /api/version, one draw, and a weather fetch
./run.sh --self-test  # check the sky and the request shape, no bar needed
./run.sh --preview    # write preview PNGs and open a local gallery
```

## Optional: start at login

This LaunchAgent is optional. Managed work laptops often block user LaunchAgents, so `./run.sh` in Terminal is the setup that works there.

Run `./run.sh` once so `.venv` exists. Then save the plist below as `~/Library/LaunchAgents/com.busybar.skypainter.plist`, replace `YOU` with your account name, and load it with `launchctl load ~/Library/LaunchAgents/com.busybar.skypainter.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.busybar.skypainter</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/busy-bar-painter/.venv/bin/python</string>
    <string>/Users/YOU/busy-bar-painter/animator.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/YOU/busy-bar-painter</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

Place a `.env` file in the repo directory if you need a PIN, a cloud token, or a different city. The script reads that file itself.
