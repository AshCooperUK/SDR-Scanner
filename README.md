# SDR Scanner

A receive-only web scanner for a USB RTL-SDR dongle. It uses a photo-based front panel with bank scan, band search, Hold, Close Call, and 100 Hz fine tuning. The receiver and web server run on the Linux machine connected to the dongle.

## Fresh install on Ubuntu or Debian

Use Ubuntu 22.04 or newer, or Debian 12 or newer, with Python 3.10 or newer. The app was tested on Ubuntu with a Nooelec NESDR SMArt v5.

### 1. Install the system packages

```bash
sudo apt update
sudo apt install git gh python3 python3-numpy python3-scipy rtl-sdr
```

`rtl-sdr` installs the matching receiver library and USB device rules for your distribution. If Ubuntu cannot find the package, enable Universe and retry the install. [Ubuntu package](https://packages.ubuntu.com/noble/rtl-sdr) · [Debian package](https://packages.debian.org/bookworm/rtl-sdr)

```bash
sudo apt install software-properties-common
sudo add-apt-repository universe
sudo apt update
sudo apt install git gh python3 python3-numpy python3-scipy rtl-sdr
```

### 2. Sign in to GitHub and clone the repository

The repository is private. Sign in with a GitHub account that has access. [GitHub CLI authentication](https://docs.github.com/en/github-cli/github-cli/authenticating-with-github-cli)

```bash
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git --hostname github.com
gh repo clone AshCooperUK/SDR-Scanner
cd SDR-Scanner
```

### 3. Connect the receiver and start the scanner

Connect the antenna and plug the dongle into USB. If it was connected during package installation, unplug it and plug it back in to apply the USB rules. Close other applications using the dongle.

```bash
/usr/bin/python3 app.py
```

Open [the local panel](http://127.0.0.1:8766/) in a browser on the same machine. Click **VOLUME/CLOCK** to power on and open the first RTL-SDR device. Click it again to release the dongle in Clock Standby. Press **Ctrl+C** in the terminal to stop the server.

### USB troubleshooting

If the scanner shows `Err` when you try to receive, put it in Clock Standby and test the dongle as your normal user.

```bash
rtl_test -s 2400000
```

After `rtl_test` identifies the dongle and starts reading samples, press **Ctrl+C** before powering on the scanner again. For a busy device, close other SDR or TV tuner applications and retry. For a permission error, reconnect the dongle after installing `rtl-sdr` and check that the packaged rules were applied. If the rules assign the device to `plugdev`, add your user to that group and sign out and back in.

```bash
sudo usermod -aG plugdev "$USER"
```

## Using the scanner

Click the **VOLUME/CLOCK** knob to switch on or off. Drag or scroll either knob to adjust it. Use **BAND/STEP** and the arrows to select a search range, then **HOLD** to stay on a frequency. **SQ** fully counterclockwise opens the audio gate. In Clock Standby, holding **PROG** opens clock setting. The panel also supports keyboard focus.

Scroll over the LCD to move the tuned frequency by **100 Hz per step**, for example from **446.0025 to 446.0026 MHz**. The scanner holds the result and stops at gaps in the supported ranges. Hover over or focus the LCD to type a frequency in MHz, then press **Enter** to tune and hold it. **Escape** cancels typing. Press **HOLD** again to return to the previous scan or search.

The scanner follows the reference panel's tuning limits and starts with UK/international Plan 1. User memories and permanent lockouts are stored locally in `.scanner-state.json`. Factory Marine frequencies are reconstructed and the unpublished Plan 1 EMG presets are omitted. Its writable EMG slots still work. Close Call sweeps windows on the same USB tuner, so coverage, audio gaps and scan speed depend on hardware and signal conditions.

The [control reference](reports/Uniden%20UBC355CLT%20behavior%20reference.md) details the button sequences, band tables and behavior that remains provisional.

## Run v1.0 again

Create a separate checkout of the v1.0 tag from within the repository. Stop the current scanner before starting this checkout. Copy your existing `.scanner-state.json` into the v1.0 directory if you want to carry over your memories and settings.

```bash
git fetch --tags
git worktree add --detach ../SDR-Scanner-v1.0 v1.0
cd ../SDR-Scanner-v1.0
/usr/bin/python3 app.py
```
