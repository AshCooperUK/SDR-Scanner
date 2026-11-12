# Uniden UBC355CLT controls and operating behavior

The European Uniden Bearcat UBC355CLT is a **receive-only analogue AM/FM desktop or vehicle scanner** with **13 buttons, two knobs, eight scan banks, 300 programmable memory slots, 23 search ranges and Close Call**. The five bank buttons scan enabled lists of preset and saved channels. `SEARCH` and `BAND/STEP` sweep a selected frequency range and `HOLD` keeps the current channel or frequency tuned. Close Call detects strong nearby signals, either alongside normal reception or in a dedicated mode. The volume knob switches the receiver into a 24-hour clock standby while DC power remains connected. This reference uses the English UBC355CLT manual for normal operation and separately identifies regional band-plan evidence and details that require checking on a physical unit. [Uniden UBC355CLT owner's manual, pp. 6–11, 18–29, 43–48, 58–59 and 64–65](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=6)

## The front panel has 13 keys and two knobs

The five bank keys start Scan. The volume knob contains the receiver's power switch. [Front-panel drawing, p. 18](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=18) · [Rear-panel drawing, p. 26](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=26)

| Physical control | Scan, Search and Hold | Alternate action |
| --- | --- | --- |
| **VOLUME/CLOCK** knob | Clockwise through the switch turns reception on. Further clockwise increases speaker volume. Fully counterclockwise switches reception off and shows the clock with DC still connected. | In Clock Standby the backlight goes out after 3 seconds. [Manual, pp. 25 and 58](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=25) |
| **SQ** knob | Sets the signal threshold for unmuting audio and stopping the scan or search. Fully counterclockwise opens squelch. Slowly turn clockwise until the noise just disappears for the documented starting point. | In Hold, the same gate mutes background noise until a signal opens it. [Manual, pp. 25–27](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=25) |
| **HOLD** | Freezes the displayed channel or frequency and shows `HOLD`. A second press resumes the underlying operation. | A documented first step when storing a hit or locating locked entries. A reported cold-start chord combines this with **▼** for regional plan selection. [Manual, pp. 32–34 and 40–41](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=32) |
| **▲** | On an automatic Scan stop, skips the hit and resumes. In Search it sets upward direction and can continue past a hit. In Hold it can manually step up through channels or frequencies, including locked entries. | While a range preview is on screen, moves through ranges. Hold longer than 1 second for fast stepping in Hold. Also navigates slot, Close Call and step menus and advances the clock in Clock Set. The manual also lists ▲ as a Scan Hold resume action, which needs verification. [Manual, pp. 19 and 32–41](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=19) |
| **▼** | In Search it sets downward direction and can continue past a hit. In Hold it manually steps down through channels or frequencies, including locked entries. | During range preview, moves through ranges. Hold longer than 1 second for fast stepping in Hold. Also navigates slot, Close Call and step menus and moves time backward in Clock Set. [Manual, pp. 19, 34–41 and 45–59](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=19) |
| **PRIVATE** | Enables or selects the user's Private scan bank. At least one programmed, unlocked entry is needed to scan it. | Selects Private as a save or erase destination. [Manual, pp. 19, 28 and 49–55](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=49) |
| **EMG** | Enables or selects Emergency. Factory entries are scanned before the programmable entries. | Selects Emergency as a save or erase destination. [Manual, pp. 19, 28–29 and 49–55](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=28) |
| **FRN/PMR** | Cycles **both → Freenet only → PMR only → both off**. Freenet factory entries precede its saved entries. | In programming, this key selects Freenet. [Manual, pp. 20, 29–30 and 52](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=20) |
| **AIR/MARINE** | Cycles **both → Air only → Marine only → both off**. | These are factory scan banks. [Manual, pp. 22, 30 and 50](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=22) |
| **CB AM/FM** | Cycles **both → CB AM only → CB FM/Amateur only → both off**. Each selection is a service-bank choice. | These are factory scan banks. [Manual, pp. 23, 30 and 50](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=23) |
| **SEARCH** | Starts a sweep of the selected range. When Search stops on a signal, pressing again continues without setting a lockout. | Starts Band Search before opening the step menu. [Manual, pp. 29, 34 and 57](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=29) |
| **BAND/STEP** | Enter or move through range selection. The abbreviated bounds appear for about 3 seconds, then Search starts. Tapping again or pressing an arrow during the preview selects another range. | Hold during Band Search to enter step settings. In that menu, tap to reach the Air step. Goes back in Close Call setup and cancels Clock Set. [Manual, pp. 24, 34–36, 45 and 57–59](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=24) |
| **L/O** | Tap to skip temporarily. Quick double-tap skips permanently. In Hold, browse to an excluded item and tap to restore it. | Hold longer than 2.5 seconds for bulk restore in the documented bank Hold procedure. In the channel erase procedure, holding this key loads a null frequency for programming. [Manual, pp. 37–43 and 53–55](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=37) |
| **PROG** | Starts programming the displayed frequency, then commits the selected slot. | Hold longer than 2.5 seconds in radio modes to toggle Program Lock. Selects or commits a Close Call setup choice, exits the step menu, and opens or saves Clock Set depending on how it is pressed. [Manual, pp. 45 and 49–59](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=49) |
| **Close Call** icon key | Short press cycles **Do Not Disturb → Priority → Off** according to the dedicated Close Call section. | Hold longer than 2.5 seconds for Close Call Only. Tap again from Only to enter setup. In setup, this key exits the menu selections. The front-panel summary presents a different order for Priority and Do Not Disturb. [Manual, pp. 21 and 43–48](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=43) |

The **backlit segmented LCD** shows a frequency, a channel slot, abbreviated range bounds, a menu value or the clock. During a running Scan the current bank flashes and `SCAN` scrolls right to left. Under explicit Hold, the bank indicator steadies and `HOLD` can appear alongside `SCAN`. In Search, ▲ and ▼ indicate sweep direction. Programming alternates the destination-slot display with the pending frequency and can flash `CH`. Lockout shows `T L/O` or `L/O`. Documented frequency examples include `149.025`, `127.81250` and the null value `000.0000`, so the displayed precision is an independent behavior to check. [Manual, pp. 18, 35–41 and 51–55](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=35)

Key entry has a confirmation tone in radio operation, including a confirmation on opening the step menu. Bulk unlock gives two beeps, and an entirely locked-out bank can give an error tone. Avera reports that ordinary button tones have no setting. Clock Mode is silent and the Close Call hit alert has its own setting. [Manual, pp. 39, 43, 47, 57–59 and 63](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=39) · [Avera model information](https://www.avera.eu/uniden-ubc355clt.html)

## Scan, Search and Hold tune differently

**Scan** traverses the channels in enabled banks and skips exclusions. Press the bank keys until the desired banks are enabled. On a hit, the radio stops and shows the tuned frequency. When the transmission ends it waits **2 seconds** for a reply, then resumes if the channel stays quiet. A reply during that window keeps it on the channel. **▲** advances past the hit without adding an exclusion. A bank key can leave Hold and start scanning that bank. Factory scan speed is **50 channels/second**. The order across enabled banks and what happens after disabling the final bank need hardware confirmation. [Manual, pp. 28–33 and 65](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=28)

**Search** advances on the grid for one of 23 predefined ranges. `BAND/STEP` recalls the last searched band and shows bounds such as `406:440`. Further presses, or ▲/▼ during the three-second preview, select a range. `SEARCH` sweeps the selected range. When it hits a signal it pauses, and `SEARCH` or an arrow can move past the hit. Uniden specifies **60 steps/second** and **180 steps/second Turbo Search for 5 kHz steps**. Search also waits 2 seconds after a transmission ends for it to resume before advancing. [Manual, pp. 6, 11, 24, 29, 34–36 and 65](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=29)

**Hold** is a deliberate user pause. An automatic stop on a live signal can end when the signal and reply delay end. Explicit Hold stays on the selected channel or frequency until user action, even after the signal drops. The manual's detailed lockout procedure needs both arrows to browse in Scan Hold. Its quick guide also lists ▲ as a way to resume Scan Hold. Manual browsing with both arrows in explicit Hold and skipping with ▲ on an automatic stop is a provisional interpretation worth checking on hardware. [Manual, pp. 19, 31–34 and 39–41](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=31)

**Step selection** starts by holding `BAND/STEP` during a sweep until the current step and confirmation tone appear. ▲/▼ select **Auto, 5, 6.25, 10 or 12.5 kHz** in the general menu. Tap `BAND/STEP` to reach the Air menu, where ▲/▼ select **8.33 or 12.5 kHz**. `PROG` exits to Band Search. The manual also mentions a three-second return to Band Search. The setting affects Search and Close Call, and the Air setting also affects the Air service bank. The published menu shows general and Air screens. The exact scope and retention of the general override need verification. [Manual, pp. 57–58](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=57)

**Clock Standby** has a 24-hour display and no keypad beeps. With the receiver off at the volume knob and external DC connected, hold `PROG` to light the display and make the time flash. Tap ▲/▼ to adjust it. Hold an arrow for longer than two seconds to repeat in **15-minute increments**. `BAND/STEP` cancels and `PROG` saves. The backlight goes out after three seconds. The manual gives a three-minute exit timeout following a keypress and leaves the short-tap increment and timeout save behavior unspecified. [Manual, pp. 58–59](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=58)

## Memory and Close Call use the same programming keys

### Store, erase or protect a slot

**Store a frequency.** Find a signal by scanning, searching, manual stepping or acknowledging a Close Call hit. Press `HOLD → PROG` to make `CH` flash, then `PRIVATE`, `EMG` or `FRN/PMR`. The display alternates the destination slot and the candidate frequency. The example starts at the lowest available FRN slot. Use ▲/▼ to pick another slot, then `PROG` to write it. The receiver enters Scan Hold at the saved slot. `HOLD` resumes the bank from that position. There is no numeric keypad. Choosing AIR/MARINE or CB AM/FM as a destination gives an error. The worked example shows FRN slot 6, which does not establish the numbering scheme for all slots. [Manual, pp. 24 and 48–53](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=49)

**Erase one slot.** Enter programming with `PROG`. While `CH` flashes and before selecting the destination bank, hold `L/O` for about **2.5 seconds** to load `000.0000`. Select the bank and slot using the bank key and ▲/▼. `PROG` writes the null value. Press the bank key to resume scanning. The erase example starts with an active scan and presses `PROG` directly. The troubleshooting section says to stop scanning or searching if `PROG` fails, and the ordinary save example starts in Hold. Direct `PROG` entry during a moving scan needs verification. [Manual, pp. 53–55 and 62](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=53)

**Program Lock.** Hold `PROG` for longer than **2.5 seconds** in Scan, Scan Hold, Band Search, Search Hold, Programming or Close Call. The status briefly appears before returning to the prior mode. The PDF text extraction reads the segmented status as `P 9. L` when enabling lock or rejecting a write and `OF P 9. L` when disabling it. The normal short press of `PROG` is also the confirmation action for settings menus. Whether Program Lock affects those confirmations needs a physical check. [Manual, pp. 45 and 56–58](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=56)

### Temporary and permanent skips have different lifetimes

**Single tap** adds a temporary exclusion and shows `T L/O`. From an automatic signal stop, scanning resumes. From deliberate Hold, the detailed instructions say it stays held until released. Off/on clears temporary exclusions. **Two quick taps** make an exclusion permanent and the indicator changes to `L/O`. The double-tap interval and permanent action in explicit Hold need verification. Enter Hold and step to a skipped item manually to display it. A tap on that locked item restores it. [Manual, pp. 37–42](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=37)

The scanner can lock each of its **300 writable slots**. It also has **100 temporary and 100 permanent frequency skips**. A 101st frequency of either type releases the oldest skip of that type. A factory preset skip follows that frequency through factory banks and Band Search. A saved slot containing the same frequency has its own lockout state. Avera describes the frequency skip list as shared with Close Call as well. Complete lockout is documented for PRIVATE, EMG, FRN, PMR and Marine. Selecting such a fully excluded bank produces an error tone. [Manual, pp. 38–39](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=38) · [Avera model information](https://www.avera.eu/uniden-ubc355clt.html)

**Bulk restore.** Select a bank, press `HOLD`, then hold `L/O` longer than **2.5 seconds** until two beeps. The manual describes this under permanent lockouts and explicitly says the operation in PRIVATE restores all its locked slots. The exact effect on temporary locks and on shared frequency skips elsewhere needs verification. [Manual, pp. 42–43](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=42)

### Close Call has three listening modes and an off state

| Close Call state | Action and timing |
| --- | --- |
| **Off** | Ordinary Scan or Search proceeds with no Close Call checks. |
| **Do Not Disturb** | The scanner has a check opportunity every **2 seconds** and skips the check during active reception, avoiding the periodic audio break. |
| **Priority** | It checks every **2 seconds**, including during reception. This can briefly interrupt the audio. |
| **Only** | Hold the Close Call key longer than **2.5 seconds**. Dashes and a flashing Close Call icon show that only nearby-signal detection is running. Its independent check cadence is unspecified. |

The detailed section gives **Off → Do Not Disturb → Priority → Off** on short presses. The front-panel overview starts with Priority and gives Only in the same list while describing it as a long press. Use the detailed procedure provisionally. On detection the scanner tunes to the hit and shows the band number and a flashing found indication. A keypress reveals the frequency. Use the normal save workflow to keep it in a memory slot. The exact LCD text, timeout and whether the acknowledgement key also performs its usual action are unverified. [Manual, pp. 21 and 43–48](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=43)

**Setup.** Enter Only, then tap Close Call. Use ▲/▼ to select either band inclusion or the alert setting, `PROG` to enter, arrows to browse and `PROG` to set. `BAND/STEP` returns one level. The Close Call key exits setup. Each detection group can be enabled or disabled. Disabling all groups re-enables **b1 Lo**. The alert toggle controls whether a detected hit beeps. [Manual, pp. 45–47](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=45)

| Close Call group | Coverage in MHz | Nominal sensitivity as published |
| --- | --- | ---: |
| b1 Lo, VHF Low | 25.0000–87.2950 | −54 dB |
| b2 Air, VHF Air | 108.0000–136.9916 | −61 dB |
| b3 Hi, VHF High | 137.0000–174.0000 | −63 dB |
| b4 UHF | 406.0000–512.0000 | −65 dB |
| b5 800 | 806.0000–960.0000 | −55 dB |

Uniden supplies no dB reference for those values, so the figures cannot serve as calibrated SDR detection thresholds. The Close Call groups are broader than the Search subranges. [Manual, pp. 12 and 46](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=12)

## Eight scan banks and two regional search grids

### Factory scan banks

| Bank | Factory contents | Writable slots |
| --- | --- | ---: |
| **EMG** | Presets vary by plan. The regional guide describes 24 church and mosque frequencies in Plan 1 and 4 m emergency-service frequencies in Plan 2. | 100 |
| **FRN** | Six Freenet presets, then programmed channels. | 100 |
| **PMR** | Eight analogue PMR446 presets. | 0 |
| **AIR** | 108.00000–136.99166 MHz AM at the default Air step. **3,480** positions fit this grid. The manual prints 3,480 in the feature list and 3,840 in the specifications. | 0 |
| **MRN** | 57 international VHF marine presets. The full sequence and duplex-frequency treatment are unverified. | 0 |
| **CB AM** | 600 AM presets. Regional documentation gives 25.000–27.995 MHz, consistent with 600 positions spaced by 5 kHz. | 0 |
| **CB FM / Amateur** | 400 FM presets. Regional documentation gives 28.000–29.995 MHz, consistent with 400 positions spaced by 5 kHz. Some German listings give a 26 MHz lower limit. | 0 |
| **PRIVATE** | User memories. An empty bank cannot scan. | 100 |

The capacity of **300 programmable slots** comes from EMG, FRN and PRIVATE. EMG and FRN factory entries scan before their writable slots. The exact factory EMG, Marine and CB sequences were unavailable in the published references. CB AM is its own scan bank even where a regional Search grid uses FM on the same frequency. [Uniden manual, pp. 6–10, 28–30 and 64–65](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=6) · [Regional UBC355CLT guide](https://www.vysilacky.com/upload/Eshop/Documents/IS/1120935/UNIDEN%20UBC%20355%20CLT.pdf) · [German listing with different CB FM limits](https://www.funktechnik-bielefeld.de/uniden-bearcat-ubc-355-clt-kompakter-mobil/stationsscanner)

**Freenet service centres in MHz** are 149.0250, 149.0375, 149.0500, 149.0875, 149.1000 and 149.1125. **The original eight PMR446 centres in MHz** are 446.00625, 446.01875, 446.03125, 446.04375, 446.05625, 446.06875, 446.08125 and 446.09375. These values reconstruct the documented six- and eight-frequency presets from service allocations. They do not establish the order in firmware. The Uniden manual directly confirms 149.025 MHz in its example. [Bundesnetzagentur Freenet allocation](https://bundesnetzagentur.de/DE/Fachthemen/Telekommunikation/Frequenzen/Allgemeinzuteilungen/_DL/MobilfunkDectWlanCBFunk/vfg45_2025.pdf?__blob=publicationFile&v=11#page=1) · [CEPT PMR446 frequency table](https://efis.cept.org/view/getOrEditFiles?id=38) · [Uniden manual, p. 51](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=51)

### The 23 Auto search ranges

Plan 1 is the UK/international table and matches the original English Uniden manual. Plan 2 is the German table in the regional UBC355CLT guide. A dash in the Plan 2 column carries the Plan 1 values forward. The short range labels are in MHz and are coarser than the actual grid limits. [Uniden manual, pp. 8–10 and 24](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=8) · [Regional UBC355CLT guide](https://www.vysilacky.com/upload/Eshop/Documents/IS/1120935/UNIDEN%20UBC%20355%20CLT.pdf) · [Albrecht AE355M cross-check, pp. 47–48](https://www.alan-electronics.de/alan/CustomUpload/374O357O340O370O356O369O350O320O363O352O365O/Gesamtanleitung_AE355M_DE_EN_FR_2020.pdf#page=47)

| LCD label | Plan 1 lower–upper in MHz | Step in kHz | Mode | Plan 2 changes |
| --- | --- | ---: | --- | --- |
| 25:30 | 25.00000–29.99500 | 5 | FM | AM |
| 30:50 | 30.00000–49.99500 | 5 | FM | — |
| 50:80 | 50.00000–79.99500 | 5 | FM | — |
| 80:83 | 80.00000–82.99500 | 5 | FM | — |
| 83:84 | 83.00000–84.01000 | 5 | FM | — |
| 84:87 | 84.01500–87.29500 | 5 | FM | 20 kHz step with 15 kHz offset |
| 108:137 | 108.00000–136.99166 | 8.33 | AM | — |
| 137:138 | 137.00000–137.99375 | 6.25 | FM | Upper 137.99500 MHz, step 5 kHz |
| 138:144 | 138.00000–143.99375 | 6.25 | FM | Upper 143.99500 MHz, step 5 kHz |
| 144:146 | 144.00000–145.99375 | 6.25 | FM | Upper 145.98750 MHz, step 12.5 kHz |
| 146:156 | 146.00000–155.99375 | 6.25 | FM | Upper 155.99000 MHz, step 10 kHz |
| 156:157 | 156.00000–157.43125 | 6.25 | FM | Upper 157.42500 MHz, step 12.5 kHz |
| 157:158 | 157.43750–157.99375 | 6.25 | FM | Upper 157.98750 MHz, step 12.5 kHz |
| 158:160 | 158.00000–160.59375 | 6.25 | FM | Upper 160.58750 MHz, step 12.5 kHz |
| 160:162 | 160.60000–162.02500 | 6.25 | FM | Step 12.5 kHz |
| 162:162 | 162.03125–162.59375 | 6.25 | FM | 162.03000–162.59000 MHz, step 10 kHz |
| 162:174 | 162.60000–174.00000 | 6.25 | FM | Step 10 kHz |
| 406:440 | 406.00000–439.99375 | 6.25 | FM | — |
| 440:450 | 440.00000–449.99375 | 6.25 | FM | — |
| 450:466 | 450.00000–465.99375 | 6.25 | FM | Upper 465.99000 MHz, step 10 kHz |
| 466:470 | 466.00000–469.99375 | 6.25 | FM | Upper 469.99000 MHz, step 10 kHz |
| 470:512 | 470.00000–512.00000 | 6.25 | FM | — |
| 806:960 | 806.00000–960.00000 | 12.5 | FM | — |

The Air grid fits **25 kHz ÷ 3**. Adding exactly 8.33 kHz at each step would drift by about 11.6 kHz by the last position. For default grids, use each range's own lower endpoint as the anchor and model LCD formatting separately from the tuned carrier. The German 20 kHz offset grid is an Auto result. The ordinary step menu offers the choices listed above. [Uniden manual, pp. 7–10 and 57–58](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=8) · [Regional UBC355CLT guide](https://www.vysilacky.com/upload/Eshop/Documents/IS/1120935/UNIDEN%20UBC%20355%20CLT.pdf)

### Two cold-start key combinations

| Action | Reported input and result | Evidence |
| --- | --- | --- |
| **Regional plan selection** | Remove external DC. Hold **HOLD + ▼** while reconnecting it. Select the plan with ▲/▼ and save with `PROG`. The regional guide says the scanner returns to Clock and the selection changes the default steps and EMG presets. | The regional UBC355CLT guide and a UBC355CLT owner describe the procedure. A related Albrecht AE355M manual corroborates it. Availability across CLT production runs is unverified. [Regional UBC355CLT guide](https://www.vysilacky.com/upload/Eshop/Documents/IS/1120935/UNIDEN%20UBC%20355%20CLT.pdf) · [UBC355CLT owner report](https://www.mwf-service.com/uniden-bearcat-ubc-355-clt-funkscanner-25-960-mhz.html) |
| **Clear Memory** | Remove external DC. Hold **L/O + PROG** while reconnecting it. The regional guide shows `Clear`. Memory is initialized and the current band plan is retained. | UBC355CLT-specific service instructions and distributor support. Confirm the exact settings cleared before treating this as a full factory reset. [Service note](https://www.servicedocs.com/ARTIKELEN/12700255940001.pdf) · [Avera model information](https://www.avera.eu/uniden-ubc355clt.html) · [Regional UBC355CLT guide](https://www.vysilacky.com/upload/Eshop/Documents/IS/1120935/UNIDEN%20UBC%20355%20CLT.pdf) |

The regional guide reports a one-second plan number display when entering Clock Set. The English manual describes the flashing time and leaves the plan display undocumented. A unit check can establish whether this display identifies the selected plan on the reference hardware. [Regional UBC355CLT guide](https://www.vysilacky.com/upload/Eshop/Documents/IS/1120935/UNIDEN%20UBC%20355%20CLT.pdf) · [English manual, pp. 58–59](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=58)

## The receiver and the parts that need measurement

The cabinet measures **132 × 142 × 42 mm** (width × depth × height) and weighs **640 g**. It has a fold-out desktop stand and a bracket for vehicle mounting. The rear **ANT** port is a **50 Ω BNC antenna input**, **EXT SP** connects an optional external speaker, and **DC 13.8V** takes the rated external supply. The manual lists a telescopic antenna, a vehicle/window antenna, a mains adapter, an accessory-socket vehicle lead and a hardwire DC lead in the package. Maximum specified audio output is **0.65 W**, with a specified operating range of **−20 to +60 °C**. The documented front and rear panels have no USB or serial programming interface. The stock operating modes cover conventional analogue reception, with no digital voice or trunk tracking controls in the documented interface. [Manual, pp. 12–17, 26 and 64–65](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=12)

| RF test band in MHz | Nominal sensitivity at 12 dB SINAD |
| --- | ---: |
| 25.000–27.995 | 0.4 µV |
| 28.000–69.9950 and 70.0000–87.2950 | 0.2 µV |
| 108.0000–136.9916 | 0.4 µV |
| 137.0000–147.9950 and 148.0000–174.0000 | 0.2 µV |
| 406.0000–512.0000 | 0.3 µV |
| 806.0000–960.0000 | 0.4 µV |

The published nominal hum and noise values are 48 dB at 25–27.995 MHz, 43 dB at 28–87.295 MHz, 48 dB at 108–136.9916 MHz, 43 dB at 137–147.995 and 148–174 MHz, 42 dB at 400–512 MHz as printed, and 44 dB at 806–960 MHz. The 400 MHz value belongs to this test specification. Band Search starts at 406 MHz in that segment. The manual also describes internally generated false stops called birdies. Raising squelch or pressing `SEARCH` can move past one. [Manual, pp. 10–12 and 64](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=11)

### Highest-value bench checks

| Check | Observable result to record |
| --- | --- |
| **Factory bank data** | Walk EMG, Marine and CB presets in Hold in order, including first and last entries, modulation and wrap. Compare Air at both steps and inspect the plan-specific EMG sets. |
| **Scan and Hold dispatch** | Compare ▲, ▼, `L/O`, `PROG`, the same bank key and a different bank key during a running scan, automatic hit and explicit Hold. Record bank order and what happens with zero enabled banks. |
| **Lockout identity and lifetime** | Time the double tap and trace which item it affects after scanning advances. Compare knob standby with DC loss. Check bulk restore on a shared frequency and separately on a saved duplicate. |
| **Programming edge cases** | Inspect slot numbering and initial selection with an empty or full bank. Try a duplicate frequency, overwriting a populated slot, cancelling, and attempting changes under Program Lock. |
| **Close Call details** | Record the actual short-press cycle, default group mask, display glyphs, hit timeout, first-key acknowledgement, mode after exit, and how priorities and lockouts interact. |
| **Timers, steps and retention** | Capture display blink and scroll timing, tone patterns, short clock increment, timeout behavior, grid boundaries with a non-default step, and which settings persist through standby, power loss and Clear Memory. |

For software, keep the receiver's measurement layer independent of front-panel state. For example, a single NESDR has about **2.4 MHz nominal instantaneous bandwidth**. Close Call over several widely separated RF groups would need time-sliced windows. A second tuner could sweep those windows in parallel while the first maintains the current audio. Detection coverage and interruption timing depend on the backend. The scanner's published **50, 60 and 180** rates are hardware targets. Actual SDR rates depend on retune latency, bandwidth, dwell and detection time. [Nooelec NESDR bandwidth](https://support.nooelec.com/hc/en-us/articles/360005805834-NESDR-Series) · [Uniden manual, pp. 43–48 and 65](https://www.rigpix.com/bearcat/ubc355clt_manual.pdf#page=65)
