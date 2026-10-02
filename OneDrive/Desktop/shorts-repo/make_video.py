#!/usr/bin/env python3
"""Gera um YouTube Short vertical (720x1280) a partir de um guião.

Pipeline: edge-tts (narração + tempos das palavras) -> legendas ASS ->
clips verticais do Pexels -> música de fundo (pasta music/) -> ffmpeg.
O ficheiro final fica abaixo de 5 MB (limite do plano gratuito do Make).

Entrada: variável de ambiente PAYLOAD (JSON) com:
  guiao, titulo, descricao, tags, palavras_chave, linha, voz (opcional)
Saída: out/short.mp4 e out/meta.json
"""
import asyncio
import glob
import json
import os
import pathlib
import random
import re
import subprocess

import requests

W, H, FPS = 720, 1280, 25
MAX_BYTES = 4_800_000      # Make (plano grátis) aceita ficheiros até 5 MB
AUDIO_KBPS = 56
OUT, TMP = pathlib.Path("out"), pathlib.Path("tmp")


def run(cmd):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd[:12]), "...", flush=True)
    subprocess.run(cmd, check=True)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,|;]", str(v or "")) if x.strip()]


# --------------------------------------------------------------- narração
async def synth(text, voice, mp3):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    events = []
    with open(mp3, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("WordBoundary", "SentenceBoundary"):
                s = ch["offset"] / 1e7            # unidades de 100 ns
                events.append((s, s + ch["duration"] / 1e7, ch["text"], ch["type"]))
    return events


def to_words(events):
    """Devolve [(início, fim, palavra)]. Se o serviço só enviar frases,
    reparte o tempo da frase pelas palavras (proporcional ao tamanho)."""
    words = []
    for s, e, text, kind in events:
        toks = text.split()
        if not toks:
            continue
        if kind == "WordBoundary" and len(toks) == 1:
            words.append((s, e, toks[0]))
            continue
        total, cur = sum(len(t) for t in toks), s
        for t in toks:
            d = (e - s) * len(t) / total
            words.append((cur, cur + d, t))
            cur += d
    return words


# --------------------------------------------------------------- legendas
def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def chunks(words, max_words=3, max_chars=20):
    out, cur = [], []
    for w in words:
        cur.append(w)
        txt = " ".join(x[2] for x in cur)
        if len(cur) >= max_words or len(txt) >= max_chars or re.search(r"[.!?;:]$", w[2]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def clean(t):
    return re.sub(r"[{}\\]", "", t).upper()


def write_ass(words, path):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,40,40,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    flat = [(i, w) for i, c in enumerate(chunks(words)) for w in c]
    for n, (ci, w) in enumerate(flat):
        group = [x for j, x in flat if j == ci]
        nxt = flat[n + 1][1][0] if n + 1 < len(flat) else None
        end = nxt if nxt is not None and nxt - w[1] < 0.5 else w[1] + 0.15
        text = " ".join(
            ("{\\c&H00FFFF&}" + clean(x[2]) + "{\\c&HFFFFFF&}") if x is w else clean(x[2])
            for x in group)
        lines.append(f"Dialogue: 0,{ass_time(w[0])},{ass_time(end)},Default,,0,0,0,,{text}")
    pathlib.Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------- vídeos de fundo
def pick_file(video):
    files = [f for f in video.get("video_files", [])
             if f.get("file_type") == "video/mp4" and f.get("height", 0) >= f.get("width", 1)]
    files.sort(key=lambda f: abs(f.get("width", 0) - W))
    return files[0] if files else None


def normalize(src, dst, seconds):
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-t", f"{seconds:.2f}", "-an",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", dst])


def fetch_clips(keywords, need):
    key = os.environ["PEXELS_API_KEY"]
    kws = keywords or ["nature"]
    seen, clips, got, attempt = set(), [], 0.0, 0
    while got < need and attempt < len(kws) * 4 + 4:
        kw, page = kws[attempt % len(kws)], 1 + attempt // len(kws)
        attempt += 1
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": key},
                         params={"query": kw, "orientation": "portrait",
                                 "per_page": 10, "page": page}, timeout=30)
        r.raise_for_status()
        vids = r.json().get("videos", [])
        random.shuffle(vids)
        for v in vids[:2]:                         # no máx. 2 clips por pesquisa
            f = pick_file(v)
            if not f or v["id"] in seen or got >= need:
                continue
            seen.add(v["id"])
            raw = TMP / f"raw_{v['id']}.mp4"
            with requests.get(f["link"], stream=True, timeout=60) as d:
                d.raise_for_status()
                with open(raw, "wb") as fh:
                    for part in d.iter_content(1 << 20):
                        fh.write(part)
            seg = min(float(v.get("duration", 6)), random.uniform(4, 7))
            dst = TMP / f"clip_{len(clips):02d}.mp4"
            normalize(raw, dst, seg)
            clips.append(dst)
            got += seg
            raw.unlink(missing_ok=True)
    if got < need:
        raise RuntimeError(f"Só encontrei {got:.0f}s de vídeo; preciso de {need:.0f}s")
    return clips


# ------------------------------------------------------------ render final
def encode(clips, voice, music, ass, dur, out):
    lst = TMP / "clips.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    vkbps = int(MAX_BYTES * 8 / 1000 / dur * 0.93 - AUDIO_KBPS)
    for _ in range(5):
        fc = f"[0:v]ass={ass}[v];"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", lst, "-i", voice]
        if music:
            cmd += ["-i", music]
            fc += (f"[1:a]apad,atrim=0:{dur:.2f}[voz];"
                   f"[2:a]aloop=loop=-1:size=2147483647,atrim=0:{dur:.2f},volume=0.10,"
                   f"afade=t=out:st={max(dur - 2, 0):.2f}:d=2[mus];"
                   f"[voz][mus]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            fc += f"[1:a]apad,atrim=0:{dur:.2f}[a]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "slow", "-b:v", f"{vkbps}k",
                "-maxrate", f"{int(vkbps * 1.15)}k", "-bufsize", f"{vkbps * 2}k",
                "-pix_fmt", "yuv420p", "-r", FPS, "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
                "-ac", "1", "-ar", "44100", "-movflags", "+faststart", "-t", f"{dur:.2f}", out]
        run(cmd)
        size = os.path.getsize(out)
        print(f"tamanho: {size/1e6:.2f} MB a {vkbps} kbps", flush=True)
        if size <= MAX_BYTES:
            return size
        vkbps = int(vkbps * 0.85)
    raise RuntimeError("Não consegui ficar abaixo de 5 MB; encurta o guião.")


def main():
    p = json.loads(os.environ["PAYLOAD"])
    script = re.sub(r"\s+", " ", p["guiao"]).strip()
    voice = p.get("voz") or "pt-PT-RaquelNeural"
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)

    mp3 = TMP / "voz.mp3"
    words = to_words(asyncio.run(synth(script, voice, mp3)))
    if not words:
        raise RuntimeError("O TTS não devolveu tempos das palavras.")
    dur = duration(mp3) + 0.7
    print(f"narração: {dur:.1f}s, {len(words)} palavras")

    ass = TMP / "subs.ass"
    write_ass(words, ass)
    clips = fetch_clips(as_list(p.get("palavras_chave")), dur + 1)

    tracks = [t for ext in ("mp3", "m4a", "wav", "ogg") for t in glob.glob(f"music/*.{ext}")]
    music = random.choice(tracks) if tracks else None
    print("música:", music)

    final = OUT / "short.mp4"
    size = encode(clips, mp3, music, ass, dur, final)

    tags = as_list(p.get("tags"))
    hashtags = " ".join("#" + re.sub(r"\W+", "", t) for t in tags[:5])
    title = (p.get("titulo") or "Short")[:88].strip()
    meta = {
        "titulo": f"{title} #Shorts",
        "descricao": f"{p.get('descricao', '')}\n\n{hashtags} #Shorts\n\nVídeos: Pexels.com",
        "tags": tags,
        "linha": p.get("linha"),
        "duracao": round(dur, 1),
        "bytes": size,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("OK", meta["titulo"])


if __name__ == "__main__":
    main()
"# shorts-auto" 
