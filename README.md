# NeuralScreen AMD

> [!IMPORTANT]
> **This is an experimental build.** The AMD neural pass has never run on a
> real Radeon: it was written against the specifications of two working
> implementations and verified by compilation and tests, but there was no AMD
> card on this bench. **The point of this release is to collect logs from real
> cards**, so the launch can be brought up to working order.
>
> Run it on your Radeon and attach to an issue: `NeuralScreen.log` next to
> the program, `native\probe_amd.log` (the probe, `native\probe_amd.exe`) and
> your card model with the driver version.
>
> The logs are written always, with nothing to turn on: every refusal names
> itself (`native/AMD.md` explains each line), and even "nothing worked" is a
> result - the log shows exactly where it stopped.

**DLSS 5-class neural rendering on an AMD Radeon, applied to your whole
Windows desktop in real time.** Everything on screen — games, video, photos —
goes through the same neural network that DLSS 5 games use, and comes back
sharper.

> The guide below gets you running. How it works and what was measured:
> **[TECHNICAL.md](TECHNICAL.md)**. Русская версия:
> **[README.ru.md](README.ru.md)** / **[TECHNICAL.ru.md](TECHNICAL.ru.md)**.

> **Notice.** Not affiliated with NVIDIA or AMD. NVIDIA, DLSS and the NVIDIA
> logo are NVIDIA Corporation's trademarks. The neural pass drives a
> third-party runtime that is not bundled — its licence forbids it; see
> `native/AMD.md`. Rights holders: say the word and it ships without it.

| Menu, light | Menu, dark | Settings | Window list |
|---|---|---|---|
| ![light](https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-main-light.png) | ![dark](https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-main-dark.png) | ![settings](https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-settings.png) | ![windows](https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-windows.png) |

*One menu inside the overlay; the **Before / after wipe** slider splits the
screen down the middle to show what the effect does.*

## What you need

- **Windows 11**, or Windows 10 — reported working.
- **An AMD Radeon RX 7000 / 9000 card** (RDNA3 / RDNA4) — this is the card
  the neural pass runs on. Anything else, RX 6000 included, is not
  supported: the program starts and says so in its menu (**card not
  supported**), and the picture stays unprocessed.

  | Cards | Status |
  |---|---|
  | **RX 9000 / 7000** (RDNA4 / RDNA3) | ✅ supported; ~30 ms per megapixel on a 9070 XT, slower on RDNA3 (no FP8 — the network runs in FP16) |
  | **RX 6000** (RDNA2) and older | ❌ card not supported — the program works, processing does not |
  | **NVIDIA RTX** | ❌ not used by this build — see the main project branch |

- **AMD Adrenalin 26.1.1 or newer, and Windows up to date.** Not a formality:
  the pass needs the HIP 7 runtime that ships with the driver, and an old
  driver is the commonest reason it refuses to start.
- **The neural runtime** — third-party, installed once by hand: step by step
  in **[native/AMD.md](native/AMD.md)**. Without it the program still starts
  and logs what is missing.
- **Nothing else installed.** The release archive brings its own Python.

## Install

1. Download the archive from [Releases](https://github.com/perseval-BLR/DLSS5-NeuralScreen/releases)
   and unpack it anywhere.
2. Set up the neural runtime — **[native/AMD.md](native/AMD.md)**, two commands.
3. Run **`NeuralScreen.exe`**.

Windows will probably warn you about an unknown publisher — the program is not
signed with a paid certificate. Click *More info* → *Run anyway*, or use
`NeuralScreen.vbs` next to it.

There is no installer: to remove the program, delete the folder. Autostart is
the one thing written outside it — turn it off before you move or delete it.

> **Do not use it in competitive online games.** A fullscreen overlay over a
> game is what anti-cheat systems look for.

## Using it

The program sits in the tray and draws over your desktop. Press **Num2** for
the menu. The hotkeys are on the numpad, so **Num Lock has to be on**.

| Key | What it does |
|---|---|
| **Num2** | open / close the menu |
| **Num1** | neural rendering on / off |
| **Num3** | screenshot |
| **Num0** | start / stop recording, with sound |
| **Num4** / **Num6** | processing resolution down / up |
| **Num5** | capture the window under the cursor |
| **Ctrl+Alt+Q** | quit |

Every key can be reassigned in the menu, under the sliders icon. While the
menu is open it takes the mouse and keyboard, so it works on top of a game;
closed, clicks go straight through it.

### Whole screen or one window

The whole screen is the default. **Source**, second in the menu, switches
between **Fullscreen** and **Window mode**; choosing the second opens the list
of windows, and hovering a row highlights that window. **Num5** is the
shortcut when the window is already in front of you: point at it and press.
The overlay follows the window as it moves, and resizing it — a video going
fullscreen, a different player size — reconfigures the worker in place, with
no black moment. Minimising the window pauses processing.

## The menu

The dot next to your graphics card is green when neural rendering really runs
on it, red when it is not.

- **Source** — the whole screen or one window, and which window.
- **Profile** — how strong the effect is, from *Faithful* to *Extreme*;
  *Natural* by default. The four sliders underneath are the same thing in
  detail. **Save preset** stores the current values under a name and puts it
  in the Profile list; **Delete preset** removes it. Dark scenes are
  brightened automatically so shadows keep their detail. A profile moves the
  sliders only — the model below stays where you put it.
- **Model** — *which* network produces the picture, as opposed to how
  strongly. Three of them, and they are three different outputs rather than
  three strengths: **Default** suits a desktop, **Natural** and **Cinematic**
  are tuned for games and soften photographs and small text. A saved preset
  keeps the model it was saved with.
- **Before / after wipe** — leaves the left part of the screen unprocessed so
  you can see what the effect is doing. Back to 0 when done.
- **Boost** — on by default. The network runs at a reduced resolution and a
  slider under the switch chooses which. The picture stays sharp — the
  network's result is composed onto your original frame, so text and edges
  keep full resolution. On AMD this is the main speed lever: the network
  costs about 30 ms per megapixel. Turn it off to compare.
- **DLSS 4.5 FG** — Frame Generation: **not functional on this build.** It
  rides on NVIDIA's optical-flow runtime, which a Radeon does not have; the
  switch stays off.

Everything else is behind the sliders icon: which monitor is processed and
which card does it, HDR compatibility, the screenshot folder, Spout2 output,
the recording indicator, leaving an unchanged screen alone, the key
assignments, the theme — and the language, of which there are **12**: English,
Russian, French, German, Spanish, Italian, Portuguese, Polish, Ukrainian,
Chinese, Japanese and Korean.

## The runtime

The neural pass drives a third-party runtime that is not bundled — its
licence forbids redistribution. How to prepare it: **[native/AMD.md](native/AMD.md)**.

## Recording and screenshots

**Num0** records what you see, with system sound, into an MP4 in
`recordings`. **Num3** saves a screenshot. The menu appears in both if it is
open, on purpose. A red dot with a timer sits in the corner while recording
(it can be turned off in the settings).

Screenshots open a **Save As** dialog; set **Screenshot folder...** in the
settings once and it will start there every time.

**Recording externally:**

- **OBS (recommended):** turn on **Spout2 output (OBS)** in the settings, then
  add a **Spout2 Capture** source in OBS. Works in any mode.
- **NVIDIA App:** it has no Spout input, so use one-window mode — pick the
  window, record, then switch back to **Fullscreen**. In that mode the overlay
  is visible to screen capture; in full-screen mode it hides itself.

## If something is not working

**Nothing appears after launch.** Check `NeuralScreen.log` next to the
program — it names the cause. The commonest is a missing
`native\nvngx_dlssnr.dll`.

**The overlay is invisible in a game.** True fullscreen cannot have anything
drawn over it — a Windows rule. Switch the game to *borderless*.

**The menu pointer is missing or frozen.** A fullscreen game hides the system
cursor, and the overlay only shows that one. Borderless fixes it.

**Everything is too bright and the sliders do nothing.** HDR is on for that
display. Turn it off (Win+Alt+B), or try **HDR compatibility** in the
settings — it is experimental; see [HDR setup](https://github.com/perseval-BLR/DLSS5-NeuralScreen/blob/main/docs/HDR.md).

**A key does nothing.** Something else claimed it; reassign it in the menu.

## Known limitations

- **True fullscreen games** cannot have an overlay drawn over them — borderless or windowed only.
- **HDR displays:** experimental, and off until you turn on **HDR compatibility** (settings, CAPTURE). Recording and Spout exports stay SDR. See [HDR setup and limitations](https://github.com/perseval-BLR/DLSS5-NeuralScreen/blob/main/docs/HDR.md).
- **Windows 10 and two NVIDIA cards are experimental** — built or fixed from user logs rather than tested here. Reports welcome.
- **A rotated display:** 180° is turned back over on capture; 90° and 270° are not handled yet and come out with the sides swapped.
- **Pipeline latency** is 40–60 ms (17-20ms with Boost Mode) — fine interactively, not competitively; **processing resolution is capped at 2560×1440**, output is always your full native resolution.
- **Window mode:** the hard blink of the panel over the picture and the drag
  stutter are fixed in 1.11.0; focus/taskbar polish (the overlay dropping
  behind on the first focus change) is still in progress.

## License

The code here is MIT. NVIDIA's runtimes ship unmodified and remain NVIDIA's
property: `nvngx_dlssnr.dll` is the leaked 310.8.0 build (sm_75/86/89/120
kernels, RTX 20-50), `nvngx_dlssg.dll` is the public 310.9.1.0
redistributable — both as received, no guarantees, research-only. Interface
faces: IBM Plex (OFL-1.1, `fonts/OFL.txt`).
