# SDR Scanner

A receive-only web scanner for a USB RTL-SDR dongle. It uses a photo-based front panel with bank scan, band search, Hold, Close Call, and 100 Hz fine tuning. The receiver and web server run on the Linux machine connected to the dongle.

## Recommended receiver

Use the **Nooelec NESDR SMArt v5 SDR** for the full **100 kHz–1.75 GHz** manual tuning range. This is the receiver used to test the scanner on Ubuntu. Below 25 MHz the v5 uses direct sampling and needs a suitable HF antenna. [Nooelec product specifications](https://www.nooelec.com/store/nesdr-smart-sdr.html)

## Fresh install on Ubuntu or Debian

Use Ubuntu 22.04 or newer, or Debian 12 or newer, with Python 3.10 or newer.

### 1. Install the system packages

```bash
sudo apt update
sudo apt install git python3 python3-numpy python3-scipy rtl-sdr xdg-utils
```

`rtl-sdr` installs the matching receiver library and USB device rules for your distribution. If Ubuntu cannot find the package, enable Universe and retry the install. [Ubuntu package](https://packages.ubuntu.com/noble/rtl-sdr) · [Debian package](https://packages.debian.org/bookworm/rtl-sdr)

```bash
sudo apt install software-properties-common
sudo add-apt-repository universe
sudo apt update
sudo apt install git python3 python3-numpy python3-scipy rtl-sdr xdg-utils
```

### 2. Clone the repository

```bash
git clone https://github.com/AshCooperUK/SDR-Scanner.git
cd SDR-Scanner
```

### 3. Install the applications menu icon

Run this from the cloned directory as your normal user.

```bash
/usr/bin/python3 launcher.py install
```

Open **SDR Scanner** from the applications menu to start the user service and open [the local panel](http://127.0.0.1:8766/). Right-click the icon and choose **Stop SDR Scanner** to stop the service and release the dongle. You can also run `/usr/bin/python3 launcher.py open` and `/usr/bin/python3 launcher.py stop` from the project directory.

### 4. Connect the receiver

Connect the antenna and plug the dongle into USB. If it was connected during package installation, unplug it and plug it back in to apply the USB rules. Close other applications using the dongle. Click **VOLUME/CLOCK** in the panel to power on and open the first RTL-SDR device. Click it again to enter Clock Standby and release the dongle.

For a foreground session, run `/usr/bin/python3 app.py` and open [the local panel](http://127.0.0.1:8766/) on the same machine. Press **Ctrl+C** in the terminal to stop that session.

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

Hover over or focus the LCD, type a frequency in **MHz**, and press **Enter** to tune and hold it. Enter any frequency from **`0.1`** (100 kHz) through **`1750`** (1.75 GHz) with suitable hardware. For example, enter **`98.4`** for broadcast FM, which automatically uses wide FM with mono audio. **Escape** cancels typing.

The scanner selects AM, narrow FM, or mono wide FM according to the frequency. Scroll over the LCD to fine-tune by **100 Hz per step**, for example from **446.0025 to 446.0026 MHz**. Press **HOLD** again to return to the previous scan or search.

Bank Scan and Band Search follow the reference panel's ranges and start with UK/international Plan 1. User memories and permanent lockouts are stored locally in `.scanner-state.json`. Factory Marine frequencies are reconstructed and the unpublished Plan 1 EMG presets are omitted. Its writable EMG slots still work. Close Call sweeps windows on the same USB tuner, so coverage, audio gaps and scan speed depend on hardware and signal conditions.

The [control reference](reports/Uniden%20UBC355CLT%20behavior%20reference.md) details the button sequences, band tables and behavior that remains provisional.

## Run v1.0 again

Create a separate checkout of the v1.0 tag from within the repository. Stop the current scanner before starting this checkout. Copy your existing `.scanner-state.json` into the v1.0 directory if you want to carry over your memories and settings.

```bash
git fetch --tags
git worktree add --detach ../SDR-Scanner-v1.0 v1.0
cd ../SDR-Scanner-v1.0
/usr/bin/python3 app.py
```
