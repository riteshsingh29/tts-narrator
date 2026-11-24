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
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(MUSIC_DIR, exist_ok=True)

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

# ---------- Modes: tempo + ffmpeg EQ + pause scaling ----------
MODES = {
    "adventure": {"label": "Adventure", "tempo": 1.05, "af": "bass=g=6,highpass=f=150,acompressor", "pause": 0.8, "music": 0.50},
    "focus":     {"label": "Focus",     "tempo": 0.92, "af": "bass=g=5,highpass=f=180",            "pause": 1.0, "music": 0.38},
    "calm":      {"label": "Bedtime",   "tempo": 0.82, "af": "bass=g=4,lowpass=f=7000,highpass=f=120", "pause": 1.5, "music": 0.45},
    "documentary": {"label": "Documentary", "tempo": 0.97, "af": "bass=g=3,highpass=f=140,acompressor", "pause": 1.1, "music": 0.42},
    "energetic": {"label": "Energetic", "tempo": 1.18, "af": "treble=g=3,highpass=f=200",          "pause": 0.6, "music": 0.42},
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

@app.get("/")
def index():
    narrators = {k: v for k, v in NARRATORS.items() if k in AVAILABLE}
    return render_template_string(PAGE, narrators=narrators, modes=MODES)

@app.post("/narrate")
def narrate():
    body = request.get_json(force=True)
    text = (body.get("text") or "").strip()
    speaker = body.get("narrator")
    mode_key = body.get("mode")
    mode = MODES.get(mode_key)
    use_music = bool(body.get("music")) and mode is not None and "music" in mode
    try:
        music_level = min(max(float(body.get("music_volume", 50)), 0), 100) / 50  # 50 = mode default
    except (TypeError, ValueError):
        music_level = 1.0
    if not text:
        return jsonify(error="Please enter some text."), 400
    if speaker not in AVAILABLE or mode is None:
        return jsonify(error="Invalid narrator or mode."), 400
    if len(text) > 20000:
        return jsonify(error="Text too long (max 20,000 characters)."), 400

    chunks = create_chunks(split_sentences(clean_text(text)))
    job = uuid.uuid4().hex[:10]
    out_path = os.path.join(OUT_DIR, f"{job}.wav")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for i, chunk in enumerate(chunks):
                raw = os.path.join(tmp, f"raw_{i}.wav")
                final = os.path.join(tmp, f"chunk_{i}.wav")
                silence = os.path.join(tmp, f"silence_{i}.wav")
                tts.tts_to_file(text=chunk, speaker=speaker, file_path=raw)
                ffmpeg("-i", raw, "-af", f"atempo={mode['tempo']},{mode['af']}", "-ar", "22050", "-ac", "1", final)
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
</style></head><body><main>
<h1>Narrator Studio</h1><p class="sub">Paste text, pick a narrator and a mode, and listen.</p>

<div class="card"><label class="h" for="narrator">Narrator</label>
<select id="narrator">{% for k,v in narrators.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select></div>

<div class="card"><span class="h" style="display:block;font-weight:600;margin-bottom:10px">Mode</span>
<div class="modes">{% for k,m in modes.items() %}
<label><input type="radio" name="mode" value="{{k}}" data-music="{{1 if m.music else 0}}" {% if k=='focus' %}checked{% endif %}><span>{{m.label}}{% if m.music %} ♪{% endif %}</span></label>{% endfor %}</div>
<div id="musicRow" class="music">
  <label><input type="checkbox" id="music" checked> Background music</label>
  <label class="vol">Volume <input type="range" id="musicVol" min="0" max="100" value="50"></label>
</div>
<p id="noMusic" class="hint" hidden>This mode has no background music.</p></div>

<div class="card"><label class="h" for="text">Text</label>
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
