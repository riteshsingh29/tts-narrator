# TTS Narrator

A small local web app: paste any text, pick a **narrator** and a **mode**, and get it read aloud. You can play it in the browser or download it as a WAV.

- **11 narrator voices** (6 male, including a deep cinematic baritone, and 5 female), from the open Coqui TTS VCTK/VITS model
- **6 modes:** Adventure, Focus, Bedtime, Documentary, Energetic and Dramatic. Each mode has its own speaking speed, voice tone, pause length and background music.
- **Background music** gets quieter under the voice, with a volume slider. It's generated automatically, or you can use your own tracks.
- **Use your own voice:** record or upload a short sample on the page and narrate in your voice (see below)
- Runs entirely on your computer. No API keys, no account and no internet connection after setup.

## Listen first

Sample clips of the same passage (about a great white shark) in different narrators and modes, each with its background music. They're in [`samples/`](samples/):

| Clip | Narrator | Mode |
|---|---|---|
| [baritone-dramatic.mp3](samples/baritone-dramatic.mp3) | Baritone – cinematic deep male | Dramatic |
| [fiona-bedtime.mp3](samples/fiona-bedtime.mp3) | Fiona – female | Bedtime |
| [oliver-adventure.mp3](samples/oliver-adventure.mp3) | Oliver – male | Adventure |
| [aria-focus.mp3](samples/aria-focus.mp3) | Aria – female | Focus |
| [hugh-documentary.mp3](samples/hugh-documentary.mp3) | Hugh – deep male | Documentary |
| [my-own-voice-documentary.mp3](samples/my-own-voice-documentary.mp3) | The author's own voice, cloned from a short recording | Documentary |

## Quick start (Windows)

```powershell
git clone https://github.com/riteshsingh29/tts-narrator.git
cd tts-narrator
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\run.ps1
```

`setup.ps1` installs anything that's missing (Python 3.11, ffmpeg and espeak-ng, using winget), creates a virtual environment and installs the Python packages. `run.ps1` starts the app and opens http://127.0.0.1:5000 in your browser.

> The first setup downloads PyTorch, which is large. The first run downloads the voice model (about 150 MB). After that, the app starts in a few seconds.

## Quick start (macOS / Linux)

```bash
git clone https://github.com/riteshsingh29/tts-narrator.git
cd tts-narrator
bash setup.sh          # tells you if Python 3.11, ffmpeg or espeak-ng are missing
.venv/bin/python app.py
```

## Requirements

| What | Why |
|---|---|
| Python **3.9 to 3.11** | Coqui TTS doesn't support Python 3.12 or newer |
| ffmpeg | Voice filters, pauses and the music mix |
| espeak-ng | Converts text into phonemes (speech sounds) for the voice model |

## Modes

| Mode | Voice | Background music |
|---|---|---|
| Adventure | A little faster, with more bass | Epic low drone with a war-drum beat |
| Focus | Steady, slightly slower | Brown noise with a soft chord |
| Bedtime | Slow, with long pauses | Ocean waves with a warm chord and music-box chimes |
| Documentary | Measured | Cinematic chord with sparse piano-like notes |
| Energetic | Fast and bright | Upbeat 120 BPM beat with a pulsing bass |
| Dramatic | Slow and weighty, with chest resonance, light reverb and long pauses | Epic low strings with deep timpani hits |

**Tip:** for a commanding, movie-trailer style narrator, use **Baritone – cinematic deep male** with **Dramatic** mode.

To use your own music, see [Music/README.md](Music/README.md).

## Narrate in your own voice

1. On the page, open **Use your own voice** under the narrator dropdown.
2. Click **Record** and read a few sentences aloud (or upload a recording). Aim for **10 to 30 seconds**, in a quiet room with no music or echo.
3. Tick the confirmation box and click **Add my voice**. It then appears in the narrator list and works with every mode.

Things to know:

- Uses Coqui's **XTTS v2** voice-cloning model. The first narration in your voice downloads it (about **1.8 GB**), then it's cached.
- It runs slower than the built-in voices, especially on a CPU.
- **License:** XTTS v2 is under the [Coqui Public Model License](https://coqui.ai/cpml), which allows **non-commercial use only**.
- Only clone your own voice, or a voice you have permission to use. Samples stay in the git-ignored `Voices/` folder on your computer and are never uploaded anywhere.
- Narration in your voice is English for now.

## Customising

Everything is in `app.py`:

- `NARRATORS`: which voices appear in the dropdown and their names
- `VOICE_VARIANTS`: styled voices built from a base speaker with its pitch shifted (`pitch` below 1 is deeper)
- `MODES`: speed (`tempo`), ffmpeg voice filter (`af`), pause length (`pause`) and music volume (`music`)
- `SYNTH_MUSIC`: the generated background music for each mode

## Troubleshooting

- **`No espeak backend found`**: install espeak-ng (`winget install eSpeak-NG.eSpeak-NG`), then open a new terminal.
- **`Could not find a version that satisfies the requirement TTS`**: your Python is too new. Use 3.11 (`setup.ps1` handles this).
- **`ffmpeg` not found**: install it, then open a new terminal so it's on your PATH.
- **Running scripts is disabled on this system**: run the command as shown above, with `-ExecutionPolicy Bypass`.
- **Your voice sounds off:** use a cleaner, longer sample (15 to 30 seconds, steady volume, no background noise), then remove and re-add it.
- **Port 5000 is already in use**: set another port with `$env:PORT=5050; .\run.ps1`.
