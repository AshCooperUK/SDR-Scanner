# SDr Scanner – Web-Based RTL-SDR Radio Scanner for Ubuntu and Debian

SDr Scanner is a receive-only software-defined radio scanner with a browser interface modelled on the Uniden UBC-355CLT. Plug a USB RTL-SDR receiver such as the Nooelec NESDR SMArt v5 into an Ubuntu or Debian desktop or headless server, then tune, scan and listen in your browser.

[Download v3.0](https://github.com/AshCooperUK/SDR-Scanner/releases/tag/v3.0) · [Install on Ubuntu or Debian](#install-on-ubuntu-or-debian) · [Quick start](#open-the-panel-and-tune-your-first-station) · [Control reference](#front-panel-controls)

![SDr Scanner v3.0 browser panel showing an FM station, signal meter and favourite channel buttons](docs/screenshots/scanner-panel.png)

## Features

- **Listen to broadcast FM with RDS.** Tune wide FM and display station names and RadioText when the station broadcasts them.
- **Scan AM airband, marine VHF, PMR446 and CB radio.** Use the preset banks or search from **100 kHz to 1.75 GHz** with compatible hardware. Save up to 124 favourite channels.
- **Record and replay radio audio.** Save MP3 clips when a signal opens the squelch, then play the latest recording from the panel.
- **Listen from another device.** Keep the USB receiver on your Linux host and open the panel over a private network or SSH tunnel without a scanner account or sign-in.

## Contents

- [Install on Ubuntu or Debian](#install-on-ubuntu-or-debian)
- [Open the panel and tune your first station](#open-the-panel-and-tune-your-first-station)
- [Front-panel controls](#front-panel-controls)
- [Scan banks, search and lockouts](#scan-banks-search-and-lockouts)
- [Favourites and programming](#favourites-and-programming)
- [RDS and Watch](#rds-and-watch)
- [Recording and playback](#recording-and-playback)
- [Close Call](#close-call)
- [Clock, band plans and memory reset](#clock-band-plans-and-memory-reset)
- [Service commands and backups](#service-commands-and-backups)
- [Access from another computer](#access-from-another-computer)
- [Troubleshooting](#troubleshooting)
- [Uniden control reference](#uniden-control-reference)

## Install on Ubuntu or Debian

Use Ubuntu 22.04 or newer, or Debian 12 or newer, with Python 3.10 or newer and systemd. The dongle plugs into this Linux machine. You can use the panel in a browser on Windows, macOS or Linux.

**Recommended receiver** is the **Nooelec NESDR SMArt v5 SDR** for **100 kHz–1.75 GHz** coverage. Below 25 MHz the app uses direct sampling, which requires a receiver that supports it and an antenna suited to the frequency. [Nooelec receiver specifications](https://www.nooelec.com/store/nesdr-smart-sdr.html)

### 1. Install the packages and clone v3.0

Run these commands on the Linux machine that will host the scanner.

```bash
sudo apt update
sudo apt install --no-install-recommends git ca-certificates python3 python3-numpy python3-scipy rtl-sdr libmp3lame0
git clone --branch v3.0 https://github.com/AshCooperUK/SDR-Scanner.git
cd SDR-Scanner
```

The `rtl-sdr` package supplies the receiver tools, library and USB device rules. If APT cannot find it on Ubuntu, run `sudo apt install --no-install-recommends software-properties-common`, then `sudo add-apt-repository universe` and retry the installation. [Ubuntu package](https://packages.ubuntu.com/jammy/rtl-sdr) · [Debian package](https://packages.debian.org/bookworm/rtl-sdr)

### 2. Choose headless server or desktop

Run the installer as your normal user.

**Headless server**

```bash
/usr/bin/python3 launcher.py install --headless
```

The server installation runs the background service without a desktop environment, a browser or an applications-menu entry. Open the panel from another computer using the [SSH tunnel instructions](#access-from-another-computer).

**Ubuntu or Debian desktop**

```bash
sudo apt install --no-install-recommends xdg-utils
/usr/bin/python3 launcher.py install --desktop
```

Open **SDR Scanner** from your applications menu or visit [the local panel](http://127.0.0.1:8766/). The menu icon and browser tab use the maintainer's GitHub avatar. On desktops that show application actions, right-click the menu entry and choose **Stop SDR Scanner** to stop the service for the current boot.

With no flag, `launcher.py install` adds the icon if it detects a local X11 or Wayland session. It skips the icon over SSH or in a text console. Both modes start and enable the same systemd user service. The installer enables user lingering so the service can start at boot before login. If your system requires administrator permission for this step, run `sudo loginctl enable-linger "$USER"` and check `loginctl show-user "$USER" -p Linger` for `Linger=yes`.

## Open the panel and tune your first station

1. Connect an antenna and plug the receiver into USB. Reconnect the dongle if it was plugged in when you installed `rtl-sdr`. Close any other SDR app using it.
2. Open [the panel](http://127.0.0.1:8766/) locally or follow [the remote access instructions](#access-from-another-computer). The app starts in clock standby.
3. Click **VOLUME** to power on and claim the receiver. Adjust the volume and turn **SQUELCH** down until you can hear the signal. Turning **VOLUME** to zero returns to clock standby and releases the receiver.
4. Click or hover over the LCD, type a local broadcast FM frequency in MHz such as `98.4`, and press **Enter**. The app tunes it in wide FM and enters **HOLD**. Turn on **RDS** to see the station details when available.
5. To stop **SCAN** or **SEARCH** on the current frequency, press **HOLD**. To resume, press **HOLD** again or use **SCAN** or **SEARCH** to select that operation.

### Tuning on the LCD

- **Type a frequency.** Enter MHz from `0.1` through `1750`, with up to six decimal places. Use **Backspace** to edit, **Enter** to tune and **Escape** to cancel. The segmented LCD displays up to five fractional digits.
- **Scroll a digit.** Point at a frequency digit and scroll up or down to add or subtract that place value. Scrolling up over the **2** in **12.3456** tunes **13.3456 MHz**. Scrolling down over the **9** in **98.7654** tunes **88.7654 MHz**. Scrolling elsewhere on the LCD steps by **100 Hz**. Wheel tuning enters **HOLD**.
- **See the current frequency.** Hover over or focus the LCD while **SCAN** or **SEARCH** advances to see the frequency being checked. **CH** identifies a memory slot or numbered preset. The LCD also shows active banks, **AM**, **NFM** or **WFM**, search direction, lockout and Close Call status.
- **Modulation follows the frequency or bank.** Direct tuning uses mono wide FM from **87.5 to just below 108 MHz**, AM below **25 MHz** and in the **108–137 MHz** and **225–400 MHz** air ranges, and the band-plan mode or narrow FM elsewhere. Tuning within the CB range after selecting a CB channel preserves that channel's AM or FM mode.

## Front-panel controls

Hover over a control for its tooltip. **Tab** moves keyboard focus, and **Space** or **Enter** presses focused buttons. Hold the key down for a long press. With a dial focused, use the arrow keys for small adjustments, **Page Up** or **Page Down** for larger adjustments, and **Home** or **End** for its limits. **Space** or **Enter** also switches **VOLUME** between reception and standby.

| Control | What it does |
| --- | --- |
| **VOLUME** | Click to switch between reception and the standby clock. Drag up or right, or scroll up, to increase the volume. Drag down or left, or scroll down, to lower it. At zero the app releases the USB receiver. |
| **SQUELCH** | Sets the signal level that opens audio, pauses scan or search, and triggers recording or Watch. Drag, click around the outer part of the dial or scroll for **0.5 dB** wheel steps. Fully counterclockwise opens the audio gate. |
| **HOLD** | Pauses on the current frequency or channel. Press again to resume the underlying scan or search. In programming, it cancels the pending operation. |
| **UP / DOWN** | In FAV, steps to the next or previous saved slot and holds it. In Search, sets the direction and moves past a hit. In Hold, steps channels or frequencies. During other automatic scans, **UP** skips past a hit. Hold an arrow for about a second for repeat stepping. |
| **MUTE** | Toggles sound and panel LEDs. The LCD stays readable at about 10% brightness. The receiver, scan, search and any armed recording continue. |
| **FAV / PMR 446 / AIR/MARINE / CB AM/FM** | Select banks and start scanning. **FAV** also selects the memory destination during programming. |
| **CLOSE CALL** | Tap to cycle nearby-signal modes. Hold for about 2½ seconds for Close Call Only. Tap again in Only mode to open setup. |
| **SEARCH** | Sweeps a frequency range and advances past a hit on a second press. |
| **L/O** | Tap for a temporary skip, double-tap for a permanent skip, or hold for about 2½ seconds to clear temporary skips and the current permanent lockout. |
| **BAND/STEP** | Tap to select a search range. Hold for about a second during Search to edit step sizes. It also backs out of Close Call setup or cancels a clock edit. |
| **PROG** | Starts or confirms memory programming. Hold for about 2½ seconds to toggle Program Lock in radio mode or set the clock in standby. It also confirms step, band-plan and Close Call settings. |
| **F01–F24** | Tap to recall, hold for about a second to save, or right-click to name. |
| **RDS** | Shows or hides broadcast station details and custom favourite names on the LCD. |
| **SCAN** | Starts or resumes scanning the selected banks or saved favourites. |
| **WATCH** | Checks F01–F24 for activity while you listen elsewhere. |
| **RECORD** | Tap to arm or stop recording, right-click to choose the server folder, or hold for about 2½ seconds to delete clips for the tuned frequency. |
| **PLAY LAST** | Loops the latest saved recording in this browser. Press again for live audio. |

The bar on the right side of the LCD estimates usable signal strength. Hover over it for raw RF power, usable power when it differs, and carrier offset when available. A strong off-frequency signal can raise raw power while the usable bar drops. In wide FM, the meter and squelch also account for the noise floor. For a quiet standby between transmissions, hold a frequency and raise **SQUELCH** until the idle noise just disappears.

The avatar in the bottom-right footer opens the maintainer's GitHub profile. [AshCooperUK on GitHub](https://github.com/AshCooperUK)

## Scan banks, search and lockouts

### Scan presets or favourites

Press a bank button to start scanning. Several banks can be enabled together. Press **SCAN** to resume the selected banks from Hold or Search. With no selected bank, **SCAN** can start your saved FAV list. If no eligible channels are available, the panel shows `no Ch`.

| Bank button | Selections |
| --- | --- |
| **FAV** | Saved favourites in slots 1–124. Tap again in Scan to disable the bank. |
| **PMR 446** | Eight original analogue PMR446 channels from **446.00625 to 446.09375 MHz** in **12.5 kHz** steps. Tap again in Scan to disable the bank. |
| **AIR/MARINE** | **Air AM + Marine NFM → Air AM → Marine NFM → Off**. Combined scan uses each channel's own mode and shows both AM and NFM. Air covers the **108–137 MHz** preset grid. Marine uses 57 international presets, with coast-station frequencies for duplex channels. |
| **CB AM/FM** | **CB AM + CB FM → CB AM → CB FM → Off**. The AM grid covers **25.000–27.995 MHz** and the FM grid **28.000–29.995 MHz**, both in **5 kHz** steps. |

While scanning, `SCAN` scrolls and the current bank label flashes. When reception ends, the scanner waits about two seconds for a reply before moving on. **HOLD** stays on the current channel after the signal ends. Disabling the last enabled bank returns to the last held frequency if one is available.

### Search frequencies and change the step

- Tap **SEARCH** from Scan to sweep **100 kHz–1.75 GHz**, starting from the current frequency in the selected direction. For example, from Scan, enter `102.4` on the LCD and then press **SEARCH** to begin from that part of the band. If you are already using a preset search range, select **ALL** for a full sweep. **UP** and **DOWN** change the direction while the sweep runs. **Auto** uses a **12.5 kHz** step for the full sweep.
- To pick a narrower range, tap **BAND/STEP** and use **UP**, **DOWN** or repeated **BAND/STEP** taps while the range label is on screen. The app offers **23 preset ranges** and **ALL**. Press **SEARCH** or wait about three seconds to begin. Press **SEARCH** again to move past a hit or **HOLD** to keep listening.
- To set spacing, hold **BAND/STEP** while in Search. Use the arrows to choose **Auto, 5, 6.25, 10 or 12.5 kHz**. Tap **BAND/STEP** inside this menu to switch to the Air spacing, then choose **8.33 or 12.5 kHz** with the arrows. Press **PROG** or wait about three seconds to save and restart the search at the range boundary. Air spacing also changes the Air bank grid.
- The sweep checks discrete frequencies. Smaller steps take longer, and coverage and retune speed depend on the receiver. For an exact frequency between grid points, use [LCD tuning](#tuning-on-the-lcd).

### Skip and restore channels

| L/O gesture | Result |
| --- | --- |
| **Tap** | Temporarily skips the current channel or frequency and shows `T L/O`. When held on a temporary skip, a tap restores it. Clock standby also clears temporary skips. |
| **Quick double-tap** | Permanently skips the item and shows `L/O`. The exclusion is saved across restarts. |
| **Hold for about 2½ seconds** | Clears **all temporary skips**. If the tuned item has a permanent lockout, it clears that item's permanent lockout too. |

To unlock a permanently excluded favourite, recall it directly or step to its saved slot, then hold **L/O**. For a skipped frequency, type its frequency into the LCD and hold **L/O**. Each saved FAV slot has its own exclusion. Factory presets and Search use shared frequency exclusions, with space for 100 temporary and 100 permanent frequencies. The oldest exclusion of that type drops out when the list is full.

## Favourites and programming

### Use F01–F24

1. Tune a frequency and press **HOLD** if you are scanning or searching.
2. Hold **F01–F24** for about a second to save the frequency and modulation to that slot. A lit amber LED shows the slot is saved. If a broadcast station name has already decoded with **RDS** enabled, it can fill in the favourite name automatically.
3. Tap a saved button to recall and hold it. A matching frequency lights green whenever the receiver tunes it, including when Scan or Search reaches it. Hover to see the name followed by the saved frequency.
4. To rename a slot, right-click its button and type in the LCD. **Enter** saves, **Escape** cancels, and saving an empty name clears it. The panel label stays **F01**, **F02** and so on.

### Save or erase a numbered slot

**Save to slot 99** works the same way as any of the 124 slots.

1. Tune and hold the frequency.
2. Tap **PROG**, then **FAV**. The LCD alternates between the source frequency and the destination slot. It starts at the first empty slot, or slot 1 when all slots are occupied.
3. Tap or hold **UP** or **DOWN** until the slot display shows `CH 99`. Slot selection wraps around. Tap **PROG** to save and hold that slot. **HOLD** cancels before writing.

Slots **1–24** are the same memories as **F01–F24**. The next 100 slots are available through **PROG** and the arrows. Existing Emergency memories from older versions map to **25–124**. While on a favourite, the arrows step through saved slots in either direction, including those beyond F24, and skip empty slots.

**Erase one slot.** Tap **PROG**, then hold **L/O** for about 2½ seconds **before** pressing **FAV**. This loads `000.0000` as the source. Select the slot and tap **PROG** to erase it. You can also clear an F01–F24 name separately through its right-click editor.

**Program Lock.** While powered on, hold **PROG** for about 2½ seconds to toggle the **PROG** memory-writing sequence. `Pg.L` means locked, and `OF Pg.L` means unlocked.

## RDS and Watch

**RDS.** Turn on **RDS** and hold a wide-FM station that broadcasts RDS. The LCD reserves two lines for a station name and broadcast text. Long text scrolls, and the LCD stays the same size. Allow several seconds for the data to decode. A custom F01–F24 name takes the place of the station-name line and can appear for any modulation when the tuned frequency matches. Turning **RDS** off hides broadcast details and custom names on the LCD. Saved names remain in favourite tooltips.

**WATCH.** Tap **WATCH** to check unlocked F01–F24 frequencies for signals above the current squelch setting. A flashing amber favourite LED means Watch detected a signal there. The frequency you are listening to remains green. Hover over **WATCH** to see which SDR it is using.

- With a second available RTL-SDR plugged into the host, Watch opens it automatically. The main receiver stays on the frequency you are listening to.
- With one SDR, nearby checks can use the current tuner capture. More distant checks can briefly retune and interrupt live audio. Close Call takes priority over those retunes. Watch checks are brief samples, so a short transmission can end between checks.

## Recording and playback

**Arm recording.** Tap **RECORD**. Its LED flashes red while capturing audio above squelch. A clip is finalised when the signal ends, the tuner changes frequency, or you stop recording. Recording uses the receiver audio even while the browser is muted. The app skips identifiable short, isolated AM or narrow-FM beep bursts. Clips with speech retain their original audio, including any courtesy tone.

**Choose a folder.** Right-click **RECORD**. In the browser picker, open a listed subfolder, use **Up**, or enter an absolute server path and select **Open**. Choose a writable location and click **Choose folder**. The picker works from a Windows browser connected to a headless Linux host, and the folders are on the host where the SDR is attached.

![Browser recording folder picker over the scanner panel](docs/screenshots/scanner-recording-folder.png)

Files go into `Recorded Audio/<frequency>mhz/HHMMSS-DDMMYYYY.mp3` under the chosen folder. The date and time use the server's local clock. For example, `Recorded Audio/128.1250mhz/143059-12112026.mp3`. If you choose an existing folder named `Recorded Audio`, the app uses it directly. The default is `recordings/Recorded Audio` beside the scanner state file.

**Play the latest clip.** Tap **PLAY LAST** to loop the latest finished clip across all frequencies in the selected folder. Playback takes over live sound and panel tones in that browser. The tuner and recording continue to operate. **MUTE** also silences playback. Tap **PLAY LAST** again to stop the loop and return to live audio.

**Delete a frequency's recordings.** Tune and hold the exact frequency, then hold **RECORD** for about 2½ seconds. This immediately deletes the scanner's clips for that frequency in the selected folder and removes the frequency folder if it is empty. Other frequencies and unrelated files are preserved. If recording is armed and the squelch is open when you delete, the app waits for that opening to end before recording there again.

## Close Call

Close Call uses the main SDR to detect strong nearby signals in selected frequency groups.

| Mode | How it behaves |
| --- | --- |
| **Off** | Normal scan, search or reception. |
| **Do Not Disturb** | Checks for nearby signals about every two seconds while the current channel is quiet. |
| **Priority** | Checks about every two seconds, including during reception. This can cause a brief audio gap. |
| **Only** | Checks for nearby signals by itself. The LCD shows dashes and a flashing Close Call icon while waiting. |

Tap **CLOSE CALL** to cycle **Off → Do Not Disturb → Priority → Off**. Hold it for about 2½ seconds for **Only**. Select **SCAN**, **SEARCH**, a bank or a frequency to leave Only and return to the previous Close Call mode.

On a hit, the LCD flashes `b1 Found` or the corresponding band number. Press **HOLD** once to reveal the frequency and again to stay on it. Use **PROG** to save the hit or **CLOSE CALL** to dismiss it and continue.

**Settings.** In Close Call Only, tap **CLOSE CALL** to open setup. Use **UP** and **DOWN** to choose `C-C.bnd` for the frequency groups or `C-C.ALt` for the alert beep. Press **PROG** to open the choice. For bands, choose a group with the arrows and press **PROG** again. Set `On` or `OFF` with the arrows and save with **PROG**. **BAND/STEP** goes back one level and **CLOSE CALL** exits setup. Turning all bands off re-enables **b1 Lo**.

| Group | Range in MHz |
| --- | --- |
| b1 Lo | 25.000–87.295 |
| b2 Air | 108.000–136.991667 |
| b3 Hi | 137.000–174.000 |
| b4 UHF | 406.000–512.000 |
| b5 800 | 806.000–960.000 |

## Clock, band plans and memory reset

![Scanner in clock standby with the 24-hour time on the LCD](docs/screenshots/scanner-clock.png)

**Set the clock.** Click **VOLUME** to enter standby, then hold **PROG** for about 2½ seconds. Tap **UP** or **DOWN** to adjust by one minute, or hold an arrow to repeat in 15-minute increments. **PROG** saves and **BAND/STEP** cancels. The edit times out after three minutes of inactivity. The clock follows the host's local time with your saved offset and the backlight fades after about three seconds in standby.

**Select a band plan.** In standby, hold **HOLD** and **DOWN** together while turning on **VOLUME**. Select **Plan 1** for the UK/international search grid or **Plan 2** for the German grid using **UP** and **DOWN**. Press **PROG** to save or **BAND/STEP** to cancel. Click **VOLUME** to begin reception. The preset search grids are listed in the [band-plan source](bandplan.py).

> **Memory reset erases your saved channels and names.** To reset, go to clock standby, hold **L/O** and **PROG** together, then turn on **VOLUME** while both are held. The panel briefly shows `Clear`. It erases programmed memories, favourite names and lockouts, and turns off Program Lock. Back up the state file first if you want to restore those memories later.

For either standby combination, you can hold the two buttons on a touchscreen while tapping **VOLUME**. On a keyboard, focus the first button and hold **Space**, use **Tab** to reach the second and hold **Enter**, then click **VOLUME** with the mouse before releasing both keys.

## Service commands and backups

Run these as the user who installed the scanner.

```bash
systemctl --user status local-sdr-scanner.service
systemctl --user stop local-sdr-scanner.service
systemctl --user start local-sdr-scanner.service
systemctl --user restart local-sdr-scanner.service
journalctl --user -u local-sdr-scanner.service -n 50
```

From the repo, `/usr/bin/python3 launcher.py open` starts the service if needed and opens the browser on a local desktop. On a headless session or over SSH, it prints the local URL. `/usr/bin/python3 launcher.py stop` stops the service for the current boot. To also disable future automatic starts, use `systemctl --user disable --now local-sdr-scanner.service`.

For a foreground session, stop the service, run `/usr/bin/python3 app.py` from the checkout, and open [the local panel](http://127.0.0.1:8766/). **Ctrl+C** ends the foreground session.

The default state file is `.scanner-state.json` in the checkout. It saves memories, names, permanent lockouts, clock and band settings, RDS and the recording destination. Back it up along with the recording folder before moving to a new checkout. Stop the service before replacing a state file, copy it into the new checkout, then run the appropriate installer command from there and restart the service to load that copy. Volume starts in clock standby each time the service starts.

## Access from another computer

### Use an SSH tunnel

With SSH access to the server, run this on the computer with the browser and leave the session open.

```bash
ssh -N -L 8766:127.0.0.1:8766 your-user@your-server
```

Open [the local panel](http://127.0.0.1:8766/) on that computer. Port **8766** on the client must be free. The receiver, scanner settings and recordings stay on the server.

### Use a private LAN or Tailscale address

The service listens on loopback by default. To use direct private-network HTTP, create a JSON file on the scanner host. Set `allowed_cidrs` to the permitted client addresses or ranges and `allowed_addresses` to the scanner's private IPv4 address or range. For example, if the host address is `192.168.1.20` and the client is on that LAN:

```json
{
  "allowed_cidrs": ["192.168.1.0/24"],
  "allowed_addresses": ["192.168.1.20/32"]
}
```

The file must be owned by the service user with permissions `0600`. Use `chmod 600 /absolute/path/to/remote.json`. Run `systemctl --user edit local-sdr-scanner.service` and save a drop-in with your actual paths.

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /absolute/path/to/SDR-Scanner/app.py --remote-config /absolute/path/to/remote.json
```

Restart the service, then open the numeric address and port in the browser. The example address is `http://192.168.1.20:8766/`. If the host uses a firewall, permit TCP **8766** from the configured client addresses. The app checks both the client and destination addresses. Keep them limited to devices you trust, since an allowed browser can use the controls and recordings without a scanner sign-in. On plain HTTP the app falls back to a browser audio processor when AudioWorklet is unavailable.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Clock on the LCD | Click **VOLUME** to leave standby. |
| Panel fails to load | Check the service status and journal. On a remote computer, verify the SSH tunnel or private IP and port. |
| `Err` when tuning | Confirm the SDR is plugged into the Linux host. Put the panel in standby before running the USB test below. Close other apps using the same dongle. |
| Silent or noisy reception | Interact with the panel to enable browser audio. Check **MUTE**, **VOLUME**, squelch, the antenna and the tuned frequency. Use the meter tooltip to check carrier offset, then tune the exact frequency or make a small wheel adjustment. |
| FM audio without RDS | Check for **WFM**, turn **RDS** on, hold the station for several seconds and check reception. A station needs to transmit RDS for the receiver to decode it. |
| Search stops on noise | Raise **SQUELCH** slightly. Try a narrower **BAND/STEP** range or change the spacing if you are looking for a specific channel grid. |
| No recording to play | Arm **RECORD**, wait for a signal to pass squelch and then end, and check that the chosen server folder is writable. The **PLAY LAST** tooltip reports when no finished recording is available. |

To check USB access, put the panel in clock standby and run `rtl_test -s 2400000` as your normal user. When it identifies the receiver and reads samples, press **Ctrl+C** so the scanner can reclaim the device. If the test reports permission denied, reconnect the dongle after installing `rtl-sdr`. If your distribution's USB rule assigns it to `plugdev`, run `sudo usermod -aG plugdev "$USER"` and sign out and back in.

## Uniden control reference

| Original UBC-355CLT control | SDR Scanner v3.0 |
| --- | --- |
| **VOLUME/CLOCK**, **SQ** | **VOLUME** retains reception, volume and standby clock. **SQUELCH** sets the receive gate. |
| **HOLD**, **▲**, **▼** | Hold and stepping remain. In FAV, the arrows recall saved memories and hold them. |
| **AIR/MARINE**, **CB AM/FM** | Retain both, either bank alone, and off. The Marine list is reconstructed from international channel allocations. |
| **FRN/PMR** | **PMR 446** retains the original PMR446 presets. Freenet is removed from the panel. |
| **PRIVATE**, **EMG** | Their panel positions now serve **MUTE** and **FAV**. The original Emergency presets were replaced by user favourites. |
| **SEARCH**, **BAND/STEP** | Retain band selection and step settings. **SEARCH** can also sweep the full SDR range. |
| **L/O**, **PROG** | Retain skips, programming, Program Lock and clock setting. In v3.0, holding **L/O** clears all temporary skips and just the current permanent lockout. |
| **Close Call** | Retains Do Not Disturb, Priority, Only, band selection and an alert toggle. Detection runs on the SDR. |
| **Segmented LCD** | Retains frequency, bank, channel and status indicators and the standby clock. Adds direct tuning, signal strength and RDS text. |

The original hardware and its operating manual are available here. The behaviour reference covers the original button sequences and tables in more detail. [Uniden UBC-355CLT product listing](https://www.hamradio.co.uk/receivers-and-scanners/all-receivers-and-scanners/uniden-ubc-355clt-base-scanner-pd-6780) · [Uniden owner's manual (PDF)](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf) · [Original hardware behaviour reference](reports/Uniden%20UBC355CLT%20behavior%20reference.md)
