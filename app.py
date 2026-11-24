"""Narrator Studio: paste text, pick a narrator and a mode, get narrated audio.

Uses Coqui TTS (VCTK/VITS voices), ffmpeg voice filters, punctuation-aware
pauses and per-mode background music. See README.md for setup.
"""
import os
import re
import subprocess
import glob
import tempfile
import uuid
import wave

from flask import Flask, jsonify, render_template_string, request, send_file

# Make espeak-ng visible to Coqui TTS on Windows even if it isn't on PATH.
for _d in (r"C:\Program Files\eSpeak NG", r"C:\Program Files (x86)\eSpeak NG"):
    if os.path.isfile(os.path.join(_d, "espeak-ng.exe")) and _d not in os.environ["PATH"]:
        os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", os.path.join(_d, "libespeak-ng.dll"))

from TTS.api import TTS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "Narrations")
MUSIC_DIR = os.path.join(BASE_DIR, "Music")  # optional: put adventure.mp3 / focus.mp3 / calm.mp3 here
VOICES_DIR = os.path.join(BASE_DIR, "Voices")  # your own voice samples (cloned with XTTS v2)
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(VOICES_DIR, exist_ok=True)

# ---------- Narrators (VCTK speaker ids; p230 is the voice Agent.py uses) ----------
# Coqui's VCTK speaker ids don't match the dataset's gender metadata, so these
# were chosen by measuring each voice's median pitch (Hz, shown in comments).
NARRATORS = {
    "p230": "Classic – male (Agent default)",  # 101
    "p226": "Oliver – male",                   # 114
    "p241": "Edmund – low male",               # 92
    "p254": "Simon – deep male",               # 78
    "p267": "Hugh – deep male",                # 81
    "p225": "Aria – female",                   # 189
    "p270": "Fiona – female",                  # 218
    "p300": "Clara – female",                  # 227
    "p243": "Maya – high female",              # 245
    "p248": "Isla – high female",              # 248
}

# Styled voices: a base speaker with its pitch shifted (pitch < 1 = deeper).
VOICE_VARIANTS = {
    "baritone": {"label": "Baritone – cinematic deep male", "speaker": "p241", "pitch": 0.90},
}

def import_dropped_voices():
    """Convert audio files dropped straight into Voices/ (mp3, m4a...) to the WAV format
    the cloning model uses. The original file is kept; a clean .wav is made beside it."""
    for f in sorted(os.listdir(VOICES_DIR)):
        stem, ext = os.path.splitext(f)
        if ext.lower().lstrip(".") not in {"mp3", "m4a", "ogg", "flac", "webm", "aac", "wav"}:
            continue
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:40] or "my-voice"
        dest = os.path.join(VOICES_DIR, f"{name}.wav")
        if ext.lower() == ".wav" or os.path.exists(dest):
            continue
        try:
            ffmpeg("-i", os.path.join(VOICES_DIR, f), "-t", "30", "-ar", "22050", "-ac", "1", dest)
        except Exception as e:
            print(f"Could not import voice sample {f}: {e}")

def list_cloned_voices():
    """Voice samples saved in Voices/, as {narrator_key: label}."""
    import_dropped_voices()
    voices = {}
    for f in sorted(os.listdir(VOICES_DIR)):
        name, ext = os.path.splitext(f)
        if ext.lower() == ".wav":
            voices[f"clone:{f}"] = f"{name.replace('-', ' ').replace('_', ' ').title()} (your voice)"
    return voices

def resolve_voice(key):
    """Return {"engine", "speaker"|"wav", "pitch"} for a narrator key, or None if unknown."""
    if isinstance(key, str) and key.startswith("clone:"):
        path = os.path.abspath(os.path.join(VOICES_DIR, key[len("clone:"):]))
        if os.path.dirname(path) == os.path.abspath(VOICES_DIR) and os.path.isfile(path):
            return {"engine": "xtts", "wav": path, "pitch": 1.0}
        return None
    if key in VOICE_VARIANTS:
        v = VOICE_VARIANTS[key]
        if v["speaker"] in AVAILABLE:
            return {"engine": "vctk", "speaker": v["speaker"], "pitch": v["pitch"]}
        return None
    if key in NARRATORS and key in AVAILABLE:
        return {"engine": "vctk", "speaker": key, "pitch": 1.0}
    return None

_xtts = None
def get_xtts():
    """Load the voice-cloning model on first use (~1.8 GB download, then cached)."""
    global _xtts
    if _xtts is None:
        # Coqui asks for license acceptance before downloading XTTS. The page makes the
        # user confirm this (non-commercial license) before they can add a voice.
        os.environ["COQUI_TOS_AGREED"] = "1"
        # PyTorch 2.6+ only unpickles allow-listed classes. Allow just the XTTS config classes
        # (the official Coqui checkpoint) instead of turning the safety check off.
        import torch
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import XttsArgs, XttsAudioConfig
        from TTS.config.shared_configs import BaseDatasetConfig
        torch.serialization.add_safe_globals([XttsConfig, XttsArgs, XttsAudioConfig, BaseDatasetConfig])

        # Newer torchaudio can't read audio files without the extra torchcodec package, so
        # have XTTS read the reference clip with soundfile (already installed) instead.
        import soundfile as sf
        import torchaudio
        import TTS.tts.models.xtts as xtts_module

        def load_audio(path, sampling_rate):
            data, sr = sf.read(path, dtype="float32", always_2d=True)
            audio = torch.from_numpy(data.T).mean(dim=0, keepdim=True)
            if sr != sampling_rate:
                audio = torchaudio.functional.resample(audio, sr, sampling_rate)
            return audio.clip_(-1, 1)

        xtts_module.load_audio = load_audio
        print("Loading voice-cloning model (XTTS v2). The first time this downloads ~1.8 GB...")
        _xtts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    return _xtts

def voice_filter(tempo, pitch, af):
    """ffmpeg chain: optional pitch shift (speed-corrected), tempo, then mode EQ."""
    if pitch == 1.0:
        return f"atempo={tempo},{af}"
    return f"asetrate={int(22050 * pitch)},aresample=22050,atempo={tempo / pitch:.4f},{af}"

# ---------- Modes: tempo + ffmpeg EQ + pause scaling ----------
MODES = {
    "adventure": {"label": "Adventure", "tempo": 1.05, "af": "bass=g=6,highpass=f=150,acompressor", "pause": 0.8, "music": 0.50},
    "focus":     {"label": "Focus",     "tempo": 0.92, "af": "bass=g=5,highpass=f=180",            "pause": 1.0, "music": 0.38},
    "calm":      {"label": "Bedtime",   "tempo": 0.82, "af": "bass=g=4,lowpass=f=7000,highpass=f=120", "pause": 1.5, "music": 0.45},
    "documentary": {"label": "Documentary", "tempo": 0.97, "af": "bass=g=3,highpass=f=140,acompressor", "pause": 1.1, "music": 0.42},
    "energetic": {"label": "Energetic", "tempo": 1.18, "af": "treble=g=3,highpass=f=200",          "pause": 0.6, "music": 0.42},
    # Slow, weighty delivery with chest resonance, a little hall reverb and long pauses
    "dramatic":  {"label": "Dramatic",  "tempo": 0.86,
                  "af": "highpass=f=60,bass=g=6:f=120,equalizer=f=250:t=q:w=1:g=2,treble=g=-2,"
                        "aecho=0.8:0.7:45|90:0.22|0.12,acompressor=threshold=0.1:ratio=3",
                  "pause": 1.8, "music": 0.42},
}

# ---------- Helpers (from Agent.py) ----------
def clean_text(text):
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"([.,!?])", r"\1 ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def split_sentences(text):
    return re.split(r"(?<=[.!?])\s+", text)

def create_chunks(sentences, max_chars=160):
    chunks, current = [], ""
    for s in sentences:
        if not s:
            continue
        if len(current) + len(s) < max_chars:
            current += " " + s
        else:
            if current.strip():
                chunks.append(current.strip())
            current = s
    if current.strip():
        chunks.append(current.strip())
    return chunks

def get_pause_duration(text):
    if text.endswith("."):
        return 0.5
    if text.endswith(","):
        return 0.25
    if text.endswith(("?", "!")):
        return 0.6
    return 0.2

def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)

# ---------- Background music ----------
# Generated ambient beds (ffmpeg lavfi graphs), used when no file is in Music/.
SYNTH_MUSIC = {
    # Low epic drone + "da-DUM" war-drum pulse
    "adventure": (
        "aevalsrc='0.22*sin(2*PI*55*t)+0.14*sin(2*PI*82.41*t)"
        "+0.10*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.2*t))"
        "+0.05*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*0.05*t))"
        "+0.5*sin(2*PI*60*mod(t,0.8))*exp(-9*mod(t,0.8))"
        "+0.3*sin(2*PI*60*mod(t+0.4,1.6))*exp(-9*mod(t+0.4,1.6))'"
        ":s=44100,lowpass=f=3000[out0]"
    ),
    # Brown noise + soft slowly breathing A-minor pad
    "focus": (
        "anoisesrc=color=brown:amplitude=0.6:sample_rate=44100,lowpass=f=400[n];"
        "aevalsrc='0.12*(sin(2*PI*220*t)+sin(2*PI*261.63*t)+sin(2*PI*329.63*t))"
        "*(0.6+0.4*sin(2*PI*0.07*t))':s=44100[p];"
        "[n][p]amix=inputs=2:normalize=0[out0]"
    ),
    # Gentle ocean waves + G-major pad + music-box chimes
    "calm": (
        "anoisesrc=color=pink:amplitude=0.35:sample_rate=44100,lowpass=f=900,tremolo=f=0.1:d=0.85[n];"
        "aevalsrc='0.1*(sin(2*PI*196*t)+sin(2*PI*246.94*t)+sin(2*PI*293.66*t))"
        "*(0.5+0.5*sin(2*PI*0.08*t))"
        "+0.06*sin(2*PI*587.33*mod(t,2.4))*exp(-3*mod(t,2.4))"
        "+0.05*sin(2*PI*493.88*mod(t+1.2,2.4))*exp(-3*mod(t+1.2,2.4))':s=44100[p];"
        "[n][p]amix=inputs=2:normalize=0[out0]"
    ),
    # Cinematic D-minor string pad + low drone + sparse, reflective piano notes
    "documentary": (
        "aevalsrc='0.10*(sin(2*PI*146.83*t)+sin(2*PI*174.61*t)+sin(2*PI*220*t))"
        "*(0.55+0.45*sin(2*PI*0.06*t))"
        "+0.12*sin(2*PI*73.42*t)"
        "+0.14*sin(2*PI*293.66*mod(t,3))*exp(-2.5*mod(t,3))"
        "+0.10*sin(2*PI*220*mod(t+1.5,3))*exp(-2.5*mod(t+1.5,3))"
        "+0.08*sin(2*PI*349.23*mod(t+0.75,6))*exp(-2*mod(t+0.75,6))'"
        ":s=44100,lowpass=f=4000[out0]"
    ),
    # Upbeat 120 BPM: punchy kick, off-beat hi-hats, pulsing bass (A / F), bright pad
    "energetic": (
        "aevalsrc='0.6*sin(2*PI*60*mod(t,0.5))*exp(-12*mod(t,0.5))"
        "+0.22*sin(2*PI*if(lt(mod(t,4),2),110,87.31)*mod(t,0.25))*sin(PI*mod(t,0.25)/0.25)"
        "+0.05*(sin(2*PI*440*t)+sin(2*PI*659.25*t))*(0.6+0.4*sin(2*PI*2*t))'"
        ":s=44100[k];"
        "aevalsrc='(2*random(0)-1)*0.3*exp(-35*mod(t+0.25,0.5))':s=44100,highpass=f=6000[h];"
        "[k][h]amix=inputs=2:normalize=0[out0]"
    ),
    # Epic low strings (C minor) swelling slowly + deep timpani hits + distant horn
    "dramatic": (
        "aevalsrc='0.11*(sin(2*PI*65.41*t)+sin(2*PI*130.81*t)+sin(2*PI*155.56*t)+sin(2*PI*196*t))"
        "*(0.5+0.5*sin(2*PI*0.05*t))"
        "+0.55*sin(2*PI*48*mod(t,2.4))*exp(-5*mod(t,2.4))"
        "+0.35*sin(2*PI*48*mod(t+0.6,4.8))*exp(-5*mod(t+0.6,4.8))"
        "+0.05*sin(2*PI*261.63*t)*(0.5+0.5*sin(2*PI*0.033*t))'"
        ":s=44100,lowpass=f=2500,aecho=0.8:0.6:120:0.3[out0]"
    ),
}

def music_file(mode_key):
    for ext in ("mp3", "wav", "m4a", "ogg", "flac"):
        hits = glob.glob(os.path.join(MUSIC_DIR, f"{mode_key}.{ext}"))
        if hits:
            return hits[0]
    return None

def wav_duration(path):
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()

def mix_music(voice_path, mode_key, volume, out_path):
    """Lay a looped music bed under the voice, ducking it while the narrator speaks."""
    total = wav_duration(voice_path) + 2.0  # 2s tail for the music to fade out
    custom = music_file(mode_key)
    if custom:
        music_in = ["-stream_loop", "-1", "-i", custom]
    else:
        music_in = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", SYNTH_MUSIC[mode_key]]
    graph = (
        "[0:a]aresample=44100,aformat=channel_layouts=stereo,apad=pad_dur=2,asplit=2[v][sc];"
        f"[1:a]aresample=44100,aformat=channel_layouts=stereo,volume={volume:.3f},"
        f"afade=t=in:d=2,afade=t=out:st={max(total - 3, 0):.2f}:d=3[m];"
        "[m][sc]sidechaincompress=threshold=0.03:ratio=4:attack=20:release=500[duck];"
        "[v][duck]amix=inputs=2:duration=first:normalize=0[out]"
    )
    ffmpeg("-i", voice_path, *music_in, "-filter_complex", graph,
           "-map", "[out]", "-t", f"{total:.2f}", out_path)

# ---------- Model ----------
print("Loading TTS model...")
tts = TTS("tts_models/en/vctk/vits")
AVAILABLE = set(tts.speakers or [])

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # voice sample uploads

@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="File too large (max 30 MB). A 10-30 second clip is plenty."), 413

@app.get("/")
def index():
    narrators = {k: v for k, v in NARRATORS.items() if k in AVAILABLE}
    narrators.update({k: v["label"] for k, v in VOICE_VARIANTS.items() if v["speaker"] in AVAILABLE})
    cloned = list_cloned_voices()
    narrators.update(cloned)
    return render_template_string(PAGE, narrators=narrators, modes=MODES, cloned=cloned)

@app.post("/voices")
def add_voice():
    """Save an uploaded recording of the user's own voice as a 22.05 kHz mono WAV (max 30 s)."""
    sample = request.files.get("sample")
    if not sample or not sample.filename:
        return jsonify(error="Choose an audio file first."), 400
    stem, ext = os.path.splitext(sample.filename)
    if ext.lower().lstrip(".") not in {"wav", "mp3", "m4a", "ogg", "flac", "webm", "aac"}:
        return jsonify(error="Use a .wav, .mp3, .m4a, .ogg, .flac or .webm recording."), 400
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", (request.form.get("name") or stem).strip()).strip("-")[:40] or "my-voice"
    dest = os.path.join(VOICES_DIR, f"{name}.wav")
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(VOICES_DIR, f"{name}-{n}.wav")
        n += 1
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "upload" + ext.lower())
        sample.save(src)
        try:
            ffmpeg("-i", src, "-t", "30", "-ar", "22050", "-ac", "1", dest)
        except Exception:
            return jsonify(error="Couldn't read that audio file. Try a plain WAV or MP3."), 400
    if wav_duration(dest) < 3:
        os.remove(dest)
        return jsonify(error="That clip is too short. Record at least 6 seconds (10-30 is ideal)."), 400
    fname = os.path.basename(dest)
    return jsonify(key=f"clone:{fname}", label=list_cloned_voices()[f"clone:{fname}"], seconds=round(wav_duration(dest), 1))

@app.delete("/voices/<fname>")
def delete_voice(fname):
    path = resolve_voice(f"clone:{fname}")
    if not path:
        return jsonify(error="Voice not found."), 404
    os.remove(path["wav"])
    return jsonify(ok=True)

@app.post("/narrate")
def narrate():
    body = request.get_json(force=True)
    text = (body.get("text") or "").strip()
    voice = resolve_voice(body.get("narrator"))
    mode_key = body.get("mode")
    mode = MODES.get(mode_key)
    use_music = bool(body.get("music")) and mode is not None and "music" in mode
    try:
        music_level = min(max(float(body.get("music_volume", 50)), 0), 100) / 50  # 50 = mode default
    except (TypeError, ValueError):
        music_level = 1.0
    if not text:
        return jsonify(error="Please enter some text."), 400
    if voice is None or mode is None:
        return jsonify(error="Invalid narrator or mode."), 400
    if len(text) > 20000:
        return jsonify(error="Text too long (max 20,000 characters)."), 400

    chunks = create_chunks(split_sentences(clean_text(text)))
    job = uuid.uuid4().hex[:10]
    out_path = os.path.join(OUT_DIR, f"{job}.wav")

    try:
        engine = get_xtts() if voice["engine"] == "xtts" else tts
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for i, chunk in enumerate(chunks):
                raw = os.path.join(tmp, f"raw_{i}.wav")
                final = os.path.join(tmp, f"chunk_{i}.wav")
                silence = os.path.join(tmp, f"silence_{i}.wav")
                if voice["engine"] == "xtts":
                    engine.tts_to_file(text=chunk, speaker_wav=voice["wav"], language="en", file_path=raw)
                else:
                    engine.tts_to_file(text=chunk, speaker=voice["speaker"], file_path=raw)
                ffmpeg("-i", raw, "-af", voice_filter(mode["tempo"], voice["pitch"], mode["af"]), "-ar", "22050", "-ac", "1", final)
                ffmpeg("-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                       "-t", str(round(get_pause_duration(chunk) * mode["pause"], 2)), silence)
                parts += [final, silence]

            concat = os.path.join(tmp, "concat.txt")
            with open(concat, "w") as f:
                for p in parts:
                    f.write(f"file '{p.replace(os.sep, '/')}'\n")
            if use_music and music_level > 0:
                voice = os.path.join(tmp, "voice.wav")
                ffmpeg("-f", "concat", "-safe", "0", "-i", concat, voice)
                mix_music(voice, mode_key, mode["music"] * music_level, out_path)
            else:
                ffmpeg("-f", "concat", "-safe", "0", "-i", concat, out_path)
    except Exception as e:
        return jsonify(error=f"Generation failed: {e}"), 500

    return jsonify(url=f"/audio/{job}.wav", chunks=len(chunks))

@app.get("/audio/<name>")
def audio(name):
    if not re.fullmatch(r"[0-9a-f]{10}\.wav", name):
        return "Not found", 404
    return send_file(os.path.join(OUT_DIR, name), mimetype="audio/wav")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Narrator Studio</title>
<style>
:root{--bg:#f5f4f0;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e2e0d9;--acc:#c2410c;--accink:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#f0eee8;--mute:#9a988f;--line:#333230;--acc:#f97316;--accink:#141413}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px}
h1{margin:0 0 4px;font-size:1.7rem}.sub{color:var(--mute);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px}
label.h{display:block;font-weight:600;margin-bottom:10px}
select,textarea{width:100%;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit}
textarea{min-height:200px;resize:vertical}
.modes{display:flex;flex-wrap:wrap;gap:8px}
.modes input{position:absolute;opacity:0}
.modes span{display:inline-block;padding:8px 14px;border:1px solid var(--line);border-radius:999px;cursor:pointer}
.modes input:checked+span{background:var(--acc);color:var(--accink);border-color:var(--acc)}
.modes input:focus-visible+span{outline:2px solid var(--acc);outline-offset:2px}
button{background:var(--acc);color:var(--accink);border:0;border-radius:10px;padding:12px 22px;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
#msg{color:var(--mute);margin-left:12px}#msg.err{color:#dc2626}
audio{width:100%;margin-top:6px}
.music{display:flex;flex-wrap:wrap;gap:16px;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.music .vol{display:flex;align-items:center;gap:8px;color:var(--mute)}
.music input[type=range]{accent-color:var(--acc);width:160px}
.music input[type=checkbox]{accent-color:var(--acc)}
.hint{color:var(--mute);margin:14px 0 0;font-size:.9rem}
details.clone summary{cursor:pointer;font-weight:600}
.clone .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px}
.clone input[type=text]{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;width:180px}
.clone input[type=file]{color:var(--mute);max-width:100%}
.clone .consent{display:flex;gap:8px;align-items:flex-start;margin-top:14px;font-size:.9rem;color:var(--mute)}
.clone .consent input,.clone input[type=checkbox]{accent-color:var(--acc);margin-top:4px}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
button.rec{border-color:#dc2626;color:#dc2626}
#voiceList{list-style:none;margin:14px 0 0;padding:0}
#voiceList li{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid var(--line)}
#voiceList button{padding:4px 10px;font-size:.85rem}
#cmsg{margin-left:4px;color:var(--mute)}#cmsg.err{color:#dc2626}
</style></head><body><main>
<h1>Narrator Studio</h1><p class="sub">Paste text, pick a narrator and a mode, and listen.</p>

<div class="card"><label class="h" for="narrator">Narrator</label>
<select id="narrator">{% for k,v in narrators.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select>

<details class="clone" style="margin-top:16px" {% if cloned %}open{% endif %}>
<summary>Use your own voice</summary>
<p class="hint" style="margin-top:10px">Record or upload 10–30 seconds of yourself reading naturally in a quiet room (no music or echo). It's saved only on this computer.
The first narration with your voice downloads a ~1.8 GB model and is slower than the built-in voices.</p>
<div class="row">
  <button type="button" id="rec" class="ghost">● Record</button>
  <input type="file" id="sample" accept="audio/*">
  <input type="text" id="vname" placeholder="Name (optional)" maxlength="40">
  <button type="button" id="addVoice">Add my voice</button><span id="cmsg"></span>
</div>
<label class="consent"><input type="checkbox" id="consent"><span>This is my own voice (or I have the speaker's permission to clone it). I understand the voice-cloning model is licensed for <b>non-commercial use only</b>.</span></label>
<ul id="voiceList">{% for k,v in cloned.items() %}<li data-key="{{k}}"><span>{{v}}</span><button type="button" class="ghost" data-del="{{k}}">Remove</button></li>{% endfor %}</ul>
</details></div>

<div class="card"><span class="h" style="display:block;font-weight:600;margin-bottom:10px">Mode</span>
<div class="modes">{% for k,m in modes.items() %}
<label><input type="radio" name="mode" value="{{k}}" data-music="{{1 if m.music else 0}}" {% if k=='focus' %}checked{% endif %}><span>{{m.label}}{% if m.music %} ♪{% endif %}</span></label>{% endfor %}</div>
<div id="musicRow" class="music">
  <label><input type="checkbox" id="music" checked> Background music</label>
  <label class="vol">Volume <input type="range" id="musicVol" min="0" max="100" value="50"></label>
</div>
<p id="noMusic" class="hint" hidden>This mode has no background music.</p></div>

<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
<label class="h" for="text" style="margin:0">Text</label>
<button type="button" id="sample-text" class="ghost" style="padding:6px 12px;font-size:.9rem">Use sample text</button></div>
<textarea id="text" placeholder="Paste or type what you want narrated…"></textarea></div>

<div><button id="go">Narrate</button><span id="msg"></span></div>
<div class="card" id="out" hidden style="margin-top:16px"><audio id="player" controls></audio>
<a id="dl" download>Download</a></div>

<script>
const $=id=>document.getElementById(id);
$('go').onclick=async()=>{
  const text=$('text').value.trim(); if(!text){setMsg('Enter some text first.',1);return}
  $('go').disabled=true; setMsg('Generating… long text takes a while.');
  try{
    const r=await fetch('/narrate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,narrator:$('narrator').value,mode:document.querySelector('input[name=mode]:checked').value,
        music:$('music').checked,music_volume:+$('musicVol').value})});
    const d=await r.json(); if(!r.ok) throw new Error(d.error);
    $('player').src=d.url; $('dl').href=d.url; $('out').hidden=false; $('player').play(); setMsg('Done.');
  }catch(e){setMsg(e.message,1)} finally{$('go').disabled=false}
};
const SAMPLE_TEXT="The great white shark (Carcharodon carcharias) is a large shark species. It is robustly built, with a grayish upperside and a white underside. Inhabiting tropical and temperate waters, it can be found near coasts and in the open ocean. Populations are most concentrated in the Pacific and Atlantic of North America, and the waters of southern Africa and Oceania, migrating between the coast, the open ocean, and continents.";
$('sample-text').onclick=()=>{$('text').value=SAMPLE_TEXT;$('text').focus()};

/* ---- your own voice: record or upload, then add ---- */
let recBlob=null, recorder=null, recChunks=[], recTimer=null;
const cmsg=(t,err)=>{$('cmsg').textContent=t;$('cmsg').className=err?'err':''};
$('rec').onclick=async()=>{
  if(recorder&&recorder.state==='recording'){recorder.stop();return}
  try{
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    recorder=new MediaRecorder(stream); recChunks=[];
    recorder.ondataavailable=e=>recChunks.push(e.data);
    recorder.onstop=()=>{
      stream.getTracks().forEach(t=>t.stop()); clearTimeout(recTimer);
      recBlob=new Blob(recChunks,{type:recorder.mimeType||'audio/webm'});
      $('rec').textContent='● Re-record'; $('rec').classList.remove('rec');
      $('sample').value=''; cmsg('Recorded. Click "Add my voice".');
    };
    recorder.start(); $('rec').textContent='■ Stop'; $('rec').classList.add('rec'); cmsg('Recording… read a few sentences aloud.');
    recTimer=setTimeout(()=>recorder.state==='recording'&&recorder.stop(),30000);
  }catch(e){cmsg('Microphone unavailable: '+e.message+'. You can upload a file instead.',1)}
};
$('sample').onchange=()=>{recBlob=null};
$('addVoice').onclick=async()=>{
  if(!$('consent').checked){cmsg('Please confirm the checkbox first.',1);return}
  const f=$('sample').files[0];
  if(!recBlob&&!f){cmsg('Record or choose an audio file first.',1);return}
  const fd=new FormData();
  fd.append('sample',recBlob||f,recBlob?'recording.webm':f.name); fd.append('name',$('vname').value);
  $('addVoice').disabled=true; cmsg('Saving…');
  try{
    const r=await fetch('/voices',{method:'POST',body:fd}); const d=await r.json(); if(!r.ok) throw new Error(d.error);
    const o=new Option(d.label,d.key); $('narrator').add(o); $('narrator').value=d.key;
    const li=document.createElement('li'); li.dataset.key=d.key;
    li.innerHTML='<span></span><button type="button" class="ghost">Remove</button>';
    li.firstChild.textContent=d.label; li.lastChild.dataset.del=d.key; $('voiceList').append(li);
    recBlob=null; $('sample').value=''; $('vname').value=''; $('rec').textContent='● Record';
    cmsg('Added ('+d.seconds+' s) and selected as your narrator.');
  }catch(e){cmsg(e.message,1)} finally{$('addVoice').disabled=false}
};
$('voiceList').onclick=async e=>{
  const key=e.target.dataset&&e.target.dataset.del; if(!key) return;
  const r=await fetch('/voices/'+encodeURIComponent(key.slice(6)),{method:'DELETE'});
  if(r.ok){[...$('narrator').options].forEach(o=>o.value===key&&o.remove()); e.target.closest('li').remove(); cmsg('Removed.')}
};

function syncMusic(){
  const has=document.querySelector('input[name=mode]:checked').dataset.music==='1';
  $('musicRow').hidden=!has; $('noMusic').hidden=has;
  $('musicVol').disabled=!$('music').checked;
}
document.querySelectorAll('input[name=mode]').forEach(r=>r.onchange=syncMusic);
$('music').onchange=syncMusic; syncMusic();
function setMsg(t,err){$('msg').textContent=t;$('msg').className=err?'err':''}
</script></main></body></html>"""

if __name__ == "__main__":
    import threading, webbrowser
    port = int(os.environ.get("PORT", 5000))
    print(f"\nNarrator Studio running at http://127.0.0.1:{port}  (Ctrl+C to stop)\n")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False)
