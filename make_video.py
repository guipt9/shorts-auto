#!/usr/bin/env python3
"""Gera um YouTube Short vertical (720x1280) a partir de cenas.

Pipeline: edge-tts por cena (voz + tempos das palavras) -> legendas ASS ->
gameplay local (pasta fundo/) em ecrã inteiro -> imagens da história (Pixabay,
opcional) que aparecem no meio do ecrã com zoom lento, desaparecem e voltam
noutra cena -> efeitos sonoros (sfx/) + música (music/) -> ffmpeg.
O ficheiro final fica abaixo de 5 MB (limite do plano gratuito do Make).

Entrada: variável de ambiente PAYLOAD (JSON) com:
  titulo, descricao, tags, linha,
  cenas  -> texto "frase | termo de imagem em inglês // frase | termo // ..."
            (ou lista de {"texto":..., "imagem":...}),
  guiao  -> alternativa antiga (sem imagens; gameplay em ecrã inteiro),
  voz (opcional, por defeito en-US-AndrewNeural), ritmo (opcional, ex. "+8%")
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
OVL_W, OVL_H = 640, 480            # caixa das imagens (centrada)
OVL_Y = (H - OVL_H) // 2 - 40       # um pouco acima do centro; legendas ficam por baixo
BORDER = 0                          # sem moldura
MAX_SHOW = 3.6                      # segundos máximos com uma imagem no ecrã
TAIL = 0.3                          # cauda curta: facilita o loop
MAX_BYTES = 4_800_000      # Make (plano grátis) aceita ficheiros até 5 MB
AUDIO_KBPS = 56
DEFAULT_VOICE = "en-US-AndrewNeural"
DEFAULT_RATE = "+18%"
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


def files_in(folder, exts):
    return [f for ext in exts for f in glob.glob(f"{folder}/*.{ext}")]


# ----------------------------------------------------------------- cenas
def parse_scenes(p):
    """Devolve [(texto, termo_de_imagem)]."""
    cenas, scenes = p.get("cenas"), []
    if isinstance(cenas, list):
        for c in cenas:
            if isinstance(c, dict):
                t = str(c.get("texto") or c.get("text") or "").strip()
                img = str(c.get("imagem") or c.get("image") or "").strip()
            else:
                t, img = str(c).strip(), ""
            if t:
                scenes.append((t, img))
    elif isinstance(cenas, str) and cenas.strip():
        for part in cenas.split("//"):
            t, _, img = part.partition("|")
            t, img = t.strip(), img.strip()
            if t:
                scenes.append((t, img))
    if not scenes:
        script = re.sub(r"\s+", " ", str(p.get("guiao", ""))).strip()
        scenes = [(s.strip(), "") for s in re.split(r"(?<=[.!?])\s+", script) if s.strip()]
    if not scenes:
        raise RuntimeError("O payload não tem 'cenas' nem 'guiao'.")
    return scenes


# --------------------------------------------------------------- narração
async def synth(text, voice, rate, mp3):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
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


def build_voice(scenes, voice, rate):
    """Uma narração por cena (tempos exatos). Devolve (ficheiro, palavras, [(início, duração)])."""
    parts, words, offsets, t0 = [], [], [], 0.0
    for i, (text, _) in enumerate(scenes):
        mp3 = TMP / f"voz_{i:02d}.mp3"
        ws = to_words(asyncio.run(synth(text, voice, rate, mp3)))
        d = duration(mp3)
        if not ws:                                 # sem tempos: reparte por igual
            toks = text.split()
            ws = [(d * k / len(toks), d * (k + 1) / len(toks), t) for k, t in enumerate(toks)]
        words += [(s + t0, e + t0, w) for s, e, w in ws]
        offsets.append((t0, d))
        parts.append(mp3)
        t0 += d
    lst = TMP / "voz.txt"
    lst.write_text("".join(f"file '{m.resolve()}'\n" for m in parts))
    voice_file = TMP / "voz.m4a"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "1", voice_file])
    return voice_file, words, offsets


# --------------------------------------------------------------- legendas
def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def chunks(words, max_words=3, max_chars=16):
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
    # Alignment 2 = em baixo ao centro; MarginV 300 deixa o meio livre para as imagens
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,40,40,300,1

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


# ------------------------------------------------------ imagens da história
def fetch_image(term, idx, seen):
    """Procura uma foto no Pixabay (precisa de PIXABAY_API_KEY). Devolve caminho ou None."""
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key or not term:
        return None
    queries = [term] + ([term.split()[0]] if " " in term else [])
    for q in queries:
        try:
            r = requests.get("https://pixabay.com/api/",
                             params={"key": key, "q": q[:100], "image_type": "photo",
                                     "orientation": "horizontal", "safesearch": "true",
                                     "per_page": 5}, timeout=30)
            if r.status_code != 200:
                continue
            hits = [h for h in r.json().get("hits", []) if h["id"] not in seen][:3]
            random.shuffle(hits)
            for h in hits:
                url = h.get("largeImageURL") or h.get("webformatURL")
                if not url:
                    continue
                dst = TMP / f"img_{idx:02d}.jpg"
                with requests.get(url, stream=True, timeout=60) as d:
                    d.raise_for_status()
                    with open(dst, "wb") as fh:
                        for part in d.iter_content(1 << 20):
                            fh.write(part)
                seen.add(h["id"])
                return dst
        except Exception as e:                      # nunca deixa a imagem partir o vídeo
            print(f"imagem '{q}': {e}", flush=True)
    return None


def image_clip(img, dst, seconds, zoom_in):
    """Imagem com zoom lento (640x480), sem moldura."""
    frames = max(int(round(seconds * FPS)), 2)
    iw, ih = OVL_W - 2 * BORDER, OVL_H - 2 * BORDER
    z = f"1+0.15*on/{frames}" if zoom_in else f"1.15-0.15*on/{frames}"
    vf = (f"scale={iw * 2}:{ih * 2}:force_original_aspect_ratio=increase,"
          f"crop={iw * 2}:{ih * 2},"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={frames}:s={iw}x{ih}:fps={FPS},"
          "setsar=1,format=yuv420p")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", img, "-vf", vf, "-frames:v", frames,
         "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p", dst])


def build_overlays(scenes, offsets, dur):
    """Escolhe em que cenas aparece imagem (nunca em duas seguidas) e prepara os clips.
    Devolve [{"start", "end", "path"}]. Lista vazia = só gameplay."""
    seen, out, prev_shown = set(), [], False
    for i, (_, term) in enumerate(scenes):
        t0, d = offsets[i]
        if prev_shown or not term:
            prev_shown = False
            continue
        start = t0 + 0.05
        end = min(t0 + d, start + MAX_SHOW)
        if i == len(scenes) - 1:
            end = min(end, dur - 0.05)
        if end - start < 0.8:                       # cena curta demais para uma imagem
            prev_shown = False
            continue
        img = fetch_image(term, i, seen)
        if not img:
            prev_shown = False
            continue
        dst = TMP / f"ov_{i:02d}.mp4"
        image_clip(img, dst, end - start, zoom_in=(len(out) % 2 == 0))
        out.append({"start": start, "end": end, "path": dst})
        prev_shown = True
    if not out:
        print("sem imagens: só gameplay")
    return out


# --------------------------------------------------------- vídeos de fundo
def normalize(src, dst, seconds, start=0):
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", src,
         "-t", f"{seconds:.2f}", "-an",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
         "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p", dst])


def fetch_clips(need):
    """Escolhe trechos aleatórios dos clips de gameplay na pasta fundo/."""
    files = files_in("fundo", ("mp4", "mov", "mkv", "webm"))
    if not files:
        raise RuntimeError("Põe vídeos de jogos na pasta fundo/")
    clips, got = [], 0.0
    while got < need:
        src = random.choice(files)
        d = duration(src)
        seg = min(need - got, d, random.uniform(10, 20))
        start = random.uniform(0, max(d - seg, 0))
        dst = TMP / f"clip_{len(clips):02d}.mp4"
        normalize(src, dst, seg, start)
        clips.append(dst)
        got += seg
    return clips


# ------------------------------------------------------------ render final
def encode(clips, overlays, voice, music, sfx, ass, dur, out):
    lst = TMP / "clips.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    base = max(int(MAX_BYTES * 8 / 1000 / dur * 0.93 - AUDIO_KBPS), 200)
    fmt = "aformat=sample_rates=44100:channel_layouts=mono"

    for codec, crf0 in (("libx265", 21), ("libx264", 19)):   # HEVC dá melhor imagem aos mesmos MB
        vkbps, crf = base, crf0
        for _ in range(6):
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst]
            n, ov_i, mus_i, sfx_i = 1, [], None, []
            for ov in overlays:
                cmd += ["-i", ov["path"]]
                ov_i.append(n)
                n += 1
            cmd += ["-i", voice]
            voz_i, n = n, n + 1
            if music:
                cmd += ["-i", music]
                mus_i, n = n, n + 1
            for t, f, vol in sfx:
                cmd += ["-i", f]
                sfx_i.append((n, t, vol))
                n += 1

            # vídeo: gameplay em ecrã inteiro + imagens no meio (fade in/out)
            fc, cur = "", "[0:v]"
            for k, ov in enumerate(overlays):
                s_, e_ = ov["start"], ov["end"]
                fc += (f"[{ov_i[k]}:v]format=yuva420p,setpts=PTS+{s_:.3f}/TB,"
                       f"fade=t=in:st={s_:.3f}:d=0.2:alpha=1,"
                       f"fade=t=out:st={e_ - 0.2:.3f}:d=0.2:alpha=1[o{k}];"
                       f"{cur}[o{k}]overlay=x=(W-w)/2:y={OVL_Y}:"
                       f"enable='between(t,{s_:.3f},{e_:.3f})':format=auto[b{k}];")
                cur = f"[b{k}]"
            fc += f"{cur}format=yuv420p,ass={ass}[v];"

            labels = ["[voz]"]
            fc += f"[{voz_i}:a]{fmt},apad,atrim=0:{dur:.2f}[voz];"
            if mus_i is not None:
                fc += (f"[{mus_i}:a]{fmt},aloop=loop=-1:size=2147483647,atrim=0:{dur:.2f},"
                       f"volume=0.10,afade=t=out:st={max(dur - 1.5, 0):.2f}:d=1.5[mus];")
                labels.append("[mus]")
            for k, (i, t, vol) in enumerate(sfx_i):
                fc += (f"[{i}:a]{fmt},volume={vol},adelay={int(t * 1000)},"
                       f"apad,atrim=0:{dur:.2f}[s{k}];")
                labels.append(f"[s{k}]")
            if len(labels) > 1:
                # sem 'normalize=0' (só existe no ffmpeg 7.1+): compensa o 1/N do amix
                nl = len(labels)
                fc += ("".join(labels) +
                       f"amix=inputs={nl}:duration=longest:dropout_transition=0,"
                       f"volume={nl},alimiter=limit=0.95[a]")
            else:
                fc += "[voz]anull[a]"

            if codec == "libx265":
                vcodec = ["-c:v", "libx265", "-preset", "slow", "-tag:v", "hvc1",
                          "-x265-params", "log-level=error"]
            else:
                vcodec = ["-c:v", "libx264", "-preset", "slow"]
            cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", *vcodec,
                    "-crf", crf, "-maxrate", f"{vkbps}k",
                    "-bufsize", f"{vkbps * 2}k", "-pix_fmt", "yuv420p", "-r", FPS,
                    "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "1", "-ar", "44100",
                    "-movflags", "+faststart", "-t", f"{dur:.2f}", out]
            try:
                run(cmd)
            except subprocess.CalledProcessError:
                print(f"falhou com {codec}; a tentar o seguinte", flush=True)
                break
            size = os.path.getsize(out)
            print(f"{codec}: {size / 1e6:.2f} MB (crf {crf}, teto {vkbps} kbps)", flush=True)
            if size <= MAX_BYTES:
                return size
            vkbps, crf = int(vkbps * 0.9), crf + 2
    raise RuntimeError("Não consegui gerar o vídeo abaixo de 5 MB; encurta o guião.")


def main():
    p = json.loads(os.environ["PAYLOAD"])
    scenes = parse_scenes(p)
    voice = p.get("voz") or DEFAULT_VOICE
    rate = p.get("ritmo") or DEFAULT_RATE
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)

    voice_file, words, offsets = build_voice(scenes, voice, rate)
    dur = duration(voice_file) + TAIL
    print(f"narração: {dur:.1f}s, {len(scenes)} cenas, {len(words)} palavras")

    ass = TMP / "subs.ass"
    write_ass(words, ass)
    overlays = build_overlays(scenes, offsets, dur)
    clips = fetch_clips(dur + 1)

    tracks = files_in("music", ("mp3", "m4a", "wav", "ogg"))
    music = random.choice(tracks) if tracks else None
    print("música:", music)

    exts = ("wav", "mp3", "ogg", "m4a")
    intro_files = files_in("sfx/intro", exts)
    img_files = files_in("sfx/imagem", exts)
    if not intro_files and not img_files:           # compatibilidade com a pasta antiga
        img_files = files_in("sfx", exts)
    sfx = []
    if intro_files:                                 # efeito de captação no segundo 0
        sfx.append((0.0, random.choice(intro_files), 0.7))
    if img_files:                                   # mesmo som em todas as imagens do vídeo
        chosen = random.choice(img_files)
        sfx += [(max(ov["start"] - 0.03, 0), chosen, 0.4) for ov in overlays]
    print("efeitos:", len(sfx))

    final = OUT / "short.mp4"
    size = encode(clips, overlays, voice_file, music, sfx, ass, dur, final)

    tags = as_list(p.get("tags"))
    hashtags = " ".join("#" + re.sub(r"\W+", "", t) for t in tags[:5])
    title = (p.get("titulo") or "Short")[:88].strip()
    meta = {
        "titulo": f"{title} #Shorts",
        "descricao": f"{p.get('descricao', '')}\n\n{hashtags} #Shorts",
        "tags": tags,
        "linha": p.get("linha"),
        "duracao": round(dur, 1),
        "bytes": size,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("OK", meta["titulo"])


if __name__ == "__main__":
    main()
