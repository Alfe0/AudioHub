# 🎧 AudioHub (Beta)

**AudioHub** is a lightweight, zero-CPU background utility for Windows 10 & 11 that merges the best of **EarTrumpet** (per-application volume mixer flyout) and **SoundSwitch** (instant playback device switching via global hotkeys) into a single modern app.

---

## ✨ Features

* **🎚 Per-App Volume Mixer Flyout:** Control individual application volumes, system sounds, and master volume from a sleek, translucent Windows 11 flyout.
* **⇄ Instant Device Switching:** Cycle between active playback devices (Speakers, Headphones, Monitors) with a single global hotkey or 1-click button.
* **⚡ Zero-CPU Background Engine:** Uses native Win32 `RegisterHotKey` and blocking `GetMessageW` threads—consumes 0.0% CPU while idle in the system tray.
* **🎨 High-DPI & Custom Icons:** Extracts high-resolution 128×128 native & Microsoft Store (UWP) app logos, and lets you customize device icons (Speaker / Headphones / Monitor).
* **🖥️ Multi-Monitor Aware:** Automatically opens the mixer and OSD banner on the active monitor where your cursor is located.
* **🛡️ USB DAC Wake Protection:** Prevents USB audio interfaces (e.g., Edifier M60, Fosi Audio) from getting stuck in a silent state after switching endpoints.

---

## 🚀 Download & Run (No Python Required)

1. Go to the [**Releases**](../../releases) page.
2. Download **`AudioHub.exe`** from the latest release.
3. Run `AudioHub.exe` — it will start quietly in your System Tray.

### Default Shortcuts (Customizable inside Settings `⚙`):
* **Open Mixer Flyout:** `Alt + Q` (or any single/combo key)
* **Switch Playback Device:** `Alt + W`

---

## 🛠️ Run or Build from Source

Requires **Python 3.9+** on Windows 10/11.

```bash
pip install pycaw comtypes pywin32 pillow pystray psutil
python audio_hub.py<img width="537" height="694" alt="image" src="https://github.com/user-attachments/assets/e23d795a-4ee4-491f-95d9-49561846c598" />
<img width="503" height="474" alt="image" src="https://github.com/user-attachments/assets/463674b7-9133-4e84-b94a-2c9e01ec661d" />
