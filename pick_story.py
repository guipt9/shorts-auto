#!/usr/bin/env python3
"""Fase A1 - escolhe a história do dia e escreve payload.json para o make_video.py.

Pilares: 'factos' (reais), 'historias' (ficção assumida) e 'noticias' (temas em alta,
só com o que está nas manchetes das fontes; sem notícias adequadas, cai para 'factos').

Variáveis de ambiente:
  GEMINI_API_KEY   (obrigatória)
  PILAR            auto | factos | historias | noticias   (por defeito: auto)
  TEMA             tema manual (opcional)
  PREVIEW          true -> não grava histórico nem marca ideias como usadas

Saídas: payload.json, data/*.json (banco e histórico), resumo no GITHUB_STEP_SUMMARY
e (no GitHub) as saídas skip / pilar / titulo.
"""
import datetime
import json
import xml.etree.ElementTree as ET
import os
import pathlib
import random
import re
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
API = "https://generativelanguage.googleapis.com/v1beta"
_last_call = 0.0
_modelos = None      # modelos a tentar, do preferido para o menos preferido
_idx = 0             # modelo atual (fica "colado" ao que funcionar)
_chamadas = 0        # pedidos feitos ao Gemini nesta execução
_esgotado = False    # True quando todos os modelos falharam numa chamada

STOP = {"the", "a", "an", "of", "in", "on", "at", "to", "is", "are", "was", "were", "and",
        "or", "that", "it", "its", "for", "by", "as", "with", "from", "than", "this", "has", "have"}

# ----------------------------------------------------------- ganchos
HOOKS = {
    "contradicao": "Open with a surprising comparison that contradicts intuition (pattern: 'X is older than Y' / 'X is bigger than Y').",
    "parece_falso": "Open by saying it sounds fake but is real (e.g. 'This sounds fake, but it is real.'), then state the fact right away.",
    "numero": "Open with one shocking number or measurement, then say what it means.",
    "misterio": "Open with an unexplained event or a question nobody can answer yet.",
    "segredo": "Open with something nobody ever tells you about a familiar thing.",
    "aposta": "Open in the middle of danger or high stakes: one wrong move and everything is lost.",
    "e_se": "Open with a vivid what-if question about tomorrow morning.",
    "ultimo": "Open with someone who was the only one, the last one or the first one to do something strange.",
    "agora": "Open with what just happened, in plain words, as if telling a friend the news (no exaggeration).",
    "porque_importa": "Open with why this matters in everyday life, then reveal the news.",
}
HOOKS_POR_PILAR = {
    "factos": ["contradicao", "parece_falso", "numero", "misterio", "segredo"],
    "historias": ["misterio", "aposta", "e_se", "ultimo", "segredo"],
    "noticias": ["agora", "porque_importa", "numero"],
}

REGRAS_PILAR = {
    "factos": (
        "Pillar FACTS: every claim must be TRUE and verifiable. If you are not sure about a detail "
        "(number, date, name), leave it out or hedge it ('reportedly', 'by some estimates'). "
        "No medical advice, no claims about living people, no politics."),
    "historias": (
        "Pillar STORY: this is FICTION. Frame it as a story within the first two scenes "
        "(e.g. 'Imagine...', 'Here is a short story.', 'Legend says...'). Build suspense toward a twist "
        "that feels earned. No real people, brands, real events or real tragedies. "
        "No gore, no sexual content."),
    "noticias": (
        "Pillar NEWS: a short explainer of a topic that is trending right now. Use ONLY the facts in the "
        "SOURCE MATERIAL below. Never add numbers, names, quotes, causes, dates or predictions that are not "
        "in it. If the material is thin, say less and keep it short; never fill gaps with guesses. "
        "Attribute once, in plain words: 'According to <one source name from the material>'. Neutral tone, "
        "no opinions, no sensational claims, no blame on named people, never name private individuals. "
        "Explain in simple words why people are talking about it. Image terms must be generic, lowercase "
        "and photographable (e.g. 'stadium crowd', 'stock chart'), never a person's name, team, logo or brand."),
}


def log(*a):
    print(*a, flush=True)


def hoje():
    """Data de hoje em UTC (o mesmo relógio dos agendamentos do GitHub)."""
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def enviados_hoje(hist):
    return [u for u in hist.get("usados", []) if u.get("data") == hoje() and u.get("estado") == "enviado"]


def concluir():
    """Marca como 'enviado' o vídeo de hoje, depois de o Make ter sido avisado com sucesso."""
    hist = load_json(DATA / "historico.json", {"usados": []})
    titulo = os.environ.get("TITULO", "")
    for u in reversed(hist.get("usados", [])):
        if u.get("estado") == "pendente" and (not titulo or u.get("titulo") == titulo):
            u["estado"] = "enviado"
            save_json(DATA / "historico.json", hist)
            log("Marcado como enviado:", u.get("titulo"))
            return 0
    log("Nada pendente para marcar.")
    return 0


def load_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, obj):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def set_output(name, value):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if not m:
            raise
        return json.loads(m.group(1))


# ----------------------------------------------------------------- Gemini
def _headers():
    return {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}


def listar_modelos():
    """(normais, lites): modelos Flash estáveis da tua chave, do mais recente para o mais antigo."""
    r = requests.get(f"{API}/models", params={"pageSize": 200}, headers=_headers(), timeout=60)
    r.raise_for_status()
    nomes = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    ok = [n for n in nomes if "flash" in n and
          not re.search(r"image|tts|live|audio|embed|exp|preview|robotics|computer|thinking|omni|latest|veo|imagen", n)]

    def versao(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0

    normais = sorted([n for n in ok if "lite" not in n], key=versao, reverse=True)
    lites = sorted([n for n in ok if "lite" in n], key=versao, reverse=True)
    return normais, lites


def modelos_ordenados(cfg):
    """Ordem de tentativa: último modelo que funcionou, o da config, depois o resto."""
    global _modelos
    if _modelos is None:
        try:
            normais, lites = listar_modelos()
        except Exception as e:
            log("Não consegui listar os modelos:", e)
            normais, lites = [cfg.get("modelo") or "gemini-2.5-flash"], []
        todos = (lites + normais) if cfg.get("preferir_lite") else (normais + lites)
        if not todos:
            raise RuntimeError("Não encontrei nenhum modelo Flash disponível na tua chave do Gemini.")
        ordem = []
        for m in (load_json(DATA / "modelo.json", {}).get("modelo"), cfg.get("modelo")):
            if m and m in todos and m not in ordem:
                ordem.append(m)
        ordem += [m for m in todos if m not in ordem]
        _modelos = ordem
        log("Modelos por ordem de tentativa:", ", ".join(ordem))
    return _modelos


def gemini(prompt, cfg, temperature=0.9):
    """Chama o Gemini e devolve o JSON. Se um modelo estiver sobrecarregado (503), sem quota
    (429) ou inexistente (404), passa ao modelo seguinte e continua daí."""
    global _last_call, _idx, _chamadas, _esgotado
    if _esgotado:
        raise RuntimeError("O Gemini ficou indisponível nesta execução (todos os modelos falharam).")
    modelos = modelos_ordenados(cfg)
    falhas, ultimo = 0, None

    def seguinte(motivo):
        global _idx
        _idx = (_idx + 1) % len(modelos)
        log(f"{motivo}; a passar ao modelo {modelos[_idx]}")

    for _ in range(2 * len(modelos) + 4):
        modelo = modelos[_idx]
        espera = cfg.get("pausa_entre_chamadas_s", 6) - (time.time() - _last_call)
        if espera > 0:
            time.sleep(espera)
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature,
                                     "responseMimeType": "application/json"}}
        r = requests.post(f"{API}/models/{modelo}:generateContent",
                          headers=_headers(), json=body, timeout=180)
        _last_call = time.time()
        _chamadas += 1
        ultimo = f"{r.status_code} ({modelo})"
        if r.status_code == 404:
            seguinte(f"Modelo {modelo} indisponível")
            falhas = 0
            continue
        if r.status_code == 429 and re.search(r"PerDay|per day|daily", r.text, re.I):
            seguinte(f"Quota diária de {modelo} esgotada")
            falhas = 0
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            falhas += 1
            if falhas >= 2 and len(modelos) > 1:
                seguinte(f"{modelo} devolveu {r.status_code} duas vezes")
                falhas = 0
            else:
                log(f"{modelo} devolveu {r.status_code}; nova tentativa em 15s")
                time.sleep(15)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini {r.status_code} ({modelo}): {r.text[:300]}")
        falhas = 0
        data = r.json()
        try:
            partes = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            log("Resposta vazia ou bloqueada:", str(data)[:300])
            continue
        texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
        try:
            return parse_json(texto)
        except Exception:
            log("JSON inválido; a repetir...")
            prompt += "\n\nReturn ONLY valid JSON, nothing else."
    _esgotado = True
    raise RuntimeError(f"O Gemini não devolveu uma resposta utilizável (último estado: {ultimo}; "
                       f"modelos tentados: {', '.join(modelos)}). Se forem 503, é falta de capacidade "
                       "do lado da Google (tenta mais tarde); se forem 429, a quota gratuita acabou.")


def guardar_modelo():
    """Guarda o modelo que funcionou, para a próxima execução começar por ele."""
    if _modelos:
        save_json(DATA / "modelo.json", {"modelo": _modelos[_idx],
                                         "data": datetime.date.today().isoformat()})


def info_gemini():
    return f"Gemini: {_chamadas} pedidos nesta execução · modelo {(_modelos or ['?'])[_idx]}"


# ------------------------------------------------------------ ideias/banco
def norm(s):
    return set(re.findall(r"[a-z0-9]+", s.lower())) - STOP


def parecido(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    return len(na & nb) / len(na | nb) >= 0.6


def repetido(ideia, ja_usadas):
    return any(parecido(ideia, u) for u in ja_usadas)


def escolher_pilar(cfg, forcado):
    ativos = [(k, v.get("peso", 1)) for k, v in cfg["pilares"].items()
              if v.get("ativo") and k in HOOKS_POR_PILAR]
    if forcado in HOOKS_POR_PILAR:
        if forcado not in [k for k, _ in ativos]:
            raise RuntimeError(f"O pilar '{forcado}' está desativado no config.json.")
        return forcado
    return random.choices([k for k, _ in ativos], weights=[w for _, w in ativos])[0]


def ideias_manuais(pilar):
    p = DATA / "ideias.txt"
    if not p.exists():
        return []
    out = []
    for linha in p.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        m = re.match(r"^(factos|historias)\s*:\s*(.+)$", linha, re.I)
        if m:
            if m.group(1).lower() == pilar:
                out.append(m.group(2).strip())
        else:
            out.append(linha)
    return out


def brainstorm(pilar, cfg, evitar, n=40):
    proib = ", ".join(cfg.get("palavras_proibidas", []))
    ev = "; ".join(evitar[-60:]) or "none"
    if pilar == "factos":
        cats = ", ".join(cfg.get("categorias_factos", []))
        prompt = (
            "TASK: BRAINSTORM\n"
            "You are a researcher for a YouTube Shorts channel about surprising TRUE facts.\n"
            f"Give me {n} different topic ideas. Each must be a real, verifiable fact or phenomenon that "
            "sounds almost too strange to be true, can be explained in 20 seconds, and is easy to illustrate "
            "with stock photos.\n"
            f"Spread them across these categories: {cats}.\n"
            f"Avoid: {proib}. Avoid death, tragedy, politics, religion, health advice, sexuality, celebrities and brands.\n"
            f"Do NOT repeat or paraphrase these already used ideas: {ev}\n"
            f'Return ONLY JSON: a list of {n} objects like {{"ideia": "<one sentence stating the fact>", "categoria": "<category>"}}.')
    else:
        prompt = (
            "TASK: BRAINSTORM\n"
            "You are a story editor for a YouTube Shorts channel of fictional 20-second micro-stories and "
            "what-if scenarios.\n"
            f"Give me {n} different premises. Each one needs a clear twist or reveal, an everyday or unusual "
            "setting, and must be easy to illustrate with stock photos. Mix genres: mystery, sci-fi, what-if, "
            "legend, light horror, mild comedy.\n"
            f"Avoid: {proib}. No real people, brands, real events, real tragedies, gore or sexual content.\n"
            f"Do NOT repeat or paraphrase these already used premises: {ev}\n"
            f'Return ONLY JSON: a list of {n} objects like {{"ideia": "<one sentence premise including the twist>", "categoria": "<genre>"}}.')
    res = gemini(prompt, cfg, temperature=1.0)
    return [x for x in res if isinstance(x, dict) and x.get("ideia")]


def candidatos(pilar, cfg, hist, tema):
    usadas = [u.get("tema", "") for u in hist.get("usados", [])]
    if tema:
        return [{"ideia": tema, "categoria": ""}], "manual"
    for ideia in ideias_manuais(pilar):
        if not repetido(ideia, usadas):
            return [{"ideia": ideia, "categoria": ""}], "ideias.txt"
    banco_path = DATA / f"banco_{pilar}.json"
    banco = load_json(banco_path, [])

    def livres():
        return [b for b in banco if not b.get("usada") and not repetido(b["ideia"], usadas)]

    if len(livres()) < 8:
        log("Banco quase vazio: a pedir novas ideias ao Gemini...")
        existentes = [b["ideia"] for b in banco] + usadas
        for novo in brainstorm(pilar, cfg, existentes):
            if not repetido(novo["ideia"], existentes):
                banco.append({"ideia": novo["ideia"].strip(),
                              "categoria": str(novo.get("categoria", "")).strip(), "usada": False})
                existentes.append(novo["ideia"])
        save_json(banco_path, banco)
    disp = livres()
    if not disp:
        raise RuntimeError("Não consegui gerar ideias novas.")
    return random.sample(disp, min(12, len(disp))), "banco"


def pontuar(cands, cfg):
    """Uma chamada: o Gemini dá notas e o código escolhe a melhor."""
    if len(cands) == 1:
        return cands[0]
    lista = "\n".join(f"{i}: {c['ideia']}" for i, c in enumerate(cands))
    prompt = (
        "TASK: SCORE\n"
        "Rate each candidate for a 20-25 second YouTube Short, from 0 to 10 on: "
        "gancho (strength of the hook in the first 3 seconds), emocao (surprise, awe, fear or humor), "
        "universal (broad appeal), visual (easy to illustrate with stock photos), "
        "curto (can be told in 60-75 words). Be harsh and use the whole scale.\n"
        f"Candidates:\n{lista}\n"
        'Return ONLY JSON: a list of objects like {"i": <number>, "gancho": n, "emocao": n, '
        '"universal": n, "visual": n, "curto": n}.')
    try:
        notas = gemini(prompt, cfg, temperature=0.3)
        total = {}
        for n in notas:
            i = int(n["i"])
            if 0 <= i < len(cands):
                total[i] = sum(float(n.get(k, 0)) for k in ("gancho", "emocao", "universal", "visual", "curto"))
        if total:
            melhor = max(total, key=total.get)
            log(f"Pontuação: melhor ideia #{melhor} com {total[melhor]:.0f}/50")
            return cands[melhor]
    except Exception as e:                      # a pontuação nunca deve parar o dia
        log("Pontuação falhou, escolha aleatória:", e)
    return random.choice(cands)


class SemNoticias(Exception):
    """Hoje não há notícias adequadas (ou o feed falhou): o dia cai para o pilar 'factos'."""


UA = {"User-Agent": "Mozilla/5.0 (compatible; ShortsBot/1.0)"}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _texto(el, nome):
    for ch in el.iter():
        if _local(ch.tag) == nome and (ch.text or "").strip():
            return ch.text.strip()
    return ""


def _trafego(txt):
    n = re.sub(r"[^\d]", "", txt or "")
    return int(n) if n else 0


def buscar_trends(geo):
    """Pesquisas em alta (RSS público do Google Trends) com as manchetes associadas."""
    r = requests.get("https://trends.google.com/trending/rss", params={"geo": geo}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for item in ET.fromstring(r.content).iter("item"):
        query = _texto(item, "title")
        fontes = []
        for ni in item:
            if _local(ni.tag) == "news_item":
                t = _texto(ni, "news_item_title")
                if t:
                    fontes.append({"titulo": t, "fonte": _texto(ni, "news_item_source"),
                                   "trecho": _texto(ni, "news_item_snippet"),
                                   "url": _texto(ni, "news_item_url")})
        if query:
            out.append({"query": query, "trafego": _trafego(_texto(item, "approx_traffic")), "fontes": fontes})
    return out


def buscar_headlines(query, geo="US", n=4):
    """Manchetes recentes do Google News (RSS) para um tema; falha em silêncio."""
    try:
        r = requests.get("https://news.google.com/rss/search",
                         params={"q": f"{query} when:2d", "hl": "en-US", "gl": geo, "ceid": f"{geo}:en"},
                         headers=UA, timeout=30)
        r.raise_for_status()
        out = []
        for item in ET.fromstring(r.content).iter("item"):
            t = _texto(item, "title")
            fonte = _texto(item, "source")
            if " - " in t:
                t, resto = t.rsplit(" - ", 1)
                fonte = fonte or resto
            if t:
                out.append({"titulo": t.strip(), "fonte": fonte, "trecho": "", "url": _texto(item, "link")})
            if len(out) >= n:
                break
        return out
    except Exception as e:
        log("Google News indisponível:", e)
        return []


def _proibido(texto, cfg):
    palavras = cfg.get("palavras_proibidas", []) + cfg.get("palavras_proibidas_noticias", [])
    return next((w for w in palavras if re.search(rf"\b{re.escape(w.lower())}\b", texto.lower())), None)


def candidatos_noticias(cfg, hist, tema=""):
    nc = cfg.get("noticias", {})
    geos = nc.get("geos", ["US"])
    usadas = [u.get("tema", "") for u in hist.get("usados", [])]
    if tema:
        fontes = buscar_headlines(tema, geos[0])
        if not fontes:
            raise SemNoticias(f"não encontrei manchetes para '{tema}'")
        trends = [{"query": tema, "trafego": 0, "fontes": fontes}]
        origem = "manual+google news"
    else:
        trends, origem = [], "trends:" + ",".join(geos)
        for geo in geos:
            try:
                trends += buscar_trends(geo)
            except Exception as e:
                log(f"Google Trends ({geo}) falhou:", e)
        if not trends:
            raise SemNoticias("o feed do Google Trends não respondeu")
    cands = []
    for t in sorted(trends, key=lambda x: x["trafego"], reverse=True):
        if len(t["fontes"]) < 2 and nc.get("headlines_extra", True):
            vistos = {f["titulo"] for f in t["fontes"]}
            t["fontes"] += [f for f in buscar_headlines(t["query"], geos[0]) if f["titulo"] not in vistos]
        t["fontes"] = t["fontes"][:4]
        bloco = t["query"] + " " + " ".join(f["titulo"] for f in t["fontes"])
        if not t["fontes"]:
            continue
        if repetido(t["query"], usadas):
            log(f"Notícia repetida, ignorada: {t['query']}")
            continue
        w = _proibido(bloco, cfg)
        if w:
            log(f"Notícia filtrada ('{w}'): {t['query']}")
            continue
        cands.append({"ideia": t["query"], "categoria": "news", "fontes": t["fontes"],
                      "trafego": t["trafego"]})
    cands = cands[: nc.get("max_candidatos", 10)]
    if not cands:
        raise SemNoticias("nenhum tema em alta passou nos filtros")
    return cands, origem


def pontuar_noticias(cands, cfg):
    """Uma chamada: nota 0-10 e 'seguro' 0/1. Devolve a melhor segura, ou None."""
    blocos = []
    for i, c in enumerate(cands):
        manchetes = " | ".join(f"{f['titulo']} ({f['fonte']})" for f in c["fontes"])
        blocos.append(f"{i}: {c['ideia']} -- headlines: {manchetes}")
    prompt = (
        "TASK: SCORE\n"
        "You pick the best trending news topic for a 20-25 second YouTube Short from the candidates below.\n"
        "Rate each from 0 to 10 on: gancho (strength of the hook), emocao (surprise, awe, humor), "
        "universal (broad appeal), visual (easy to illustrate with generic stock photos), "
        "curto (the headlines are enough to tell it honestly in 40-70 words). Be harsh.\n"
        "Also give 'seguro': 1 only if the topic is safe and suitable, 0 if it involves death, injury, "
        "tragedy, disaster, crime, violence, war, politics, elections, health scares, scandals, lawsuits, "
        "sexual content, minors, betting, or named private individuals.\n"
        "Candidates:\n" + "\n".join(blocos) + "\n"
        'Return ONLY JSON: a list of objects like {"i": <number>, "gancho": n, "emocao": n, "universal": n, '
        '"visual": n, "curto": n, "seguro": 0 or 1}.')
    minimo = cfg.get("noticias", {}).get("nota_minima", 25)
    try:
        notas = gemini(prompt, cfg, temperature=0.3)
        total = {}
        for n in notas:
            i = int(n["i"])
            if 0 <= i < len(cands) and int(n.get("seguro", 0)) == 1:
                total[i] = sum(float(n.get(k, 0)) for k in ("gancho", "emocao", "universal", "visual", "curto"))
        total = {i: v for i, v in total.items() if v >= minimo}
        if total:
            melhor = max(total, key=total.get)
            log(f"Notícias: melhor #{melhor} com {total[melhor]:.0f}/50 (mínimo {minimo})")
            return cands[melhor]
    except Exception as e:
        log("A pontuação das notícias falhou:", e)
    return None


def escolher_gancho(pilar, hist):
    recentes = [u.get("gancho") for u in hist.get("usados", [])[-3:]]
    opcoes = [h for h in HOOKS_POR_PILAR[pilar] if h not in recentes] or HOOKS_POR_PILAR[pilar]
    return random.choice(opcoes)


# --------------------------------------------------- escrita e verificação
def limites(cfg, pilar):
    return cfg.get("limites_noticias", cfg["limites"]) if pilar == "noticias" else cfg["limites"]


def bloco_fontes(fontes):
    if not fontes:
        return ""
    linhas = [f"- {f['titulo']} (source: {f['fonte'] or 'unknown'})" + (f" {f['trecho']}" if f.get("trecho") else "")
              for f in fontes]
    return "SOURCE MATERIAL (the ONLY facts you may use):\n" + "\n".join(linhas) + "\n"


def escrever(pilar, ideia, gancho, cfg, feedback=None, fontes=None):
    L = limites(cfg, pilar)
    fb = (f"\nFIX THESE PROBLEMS FROM THE PREVIOUS ATTEMPT: {feedback}\n" if feedback else "")
    prompt = (
        "TASK: WRITE\n"
        "You write scripts for a YouTube Shorts channel. Language: American English, spoken style, "
        "short punchy sentences.\n"
        f"Premise: {ideia}\n"
        f"{bloco_fontes(fontes)}"
        f"Hook style for scene 1: {HOOKS[gancho]}\n"
        f"{REGRAS_PILAR[pilar]}\n\n"
        "FORMAT - the script is a JSON list of scenes. Each scene is an object with two keys:\n"
        '  "texto": ONE short spoken sentence,\n'
        '  "imagem": an English image search term (1-3 words) or an empty string "".\n'
        f"- {L['cenas_min']}-{L['cenas_max']} scenes, {L['palavras_min']}-{L['palavras_max']} words in total "
        "(count them!), 5-11 words per scene.\n"
        "- Scene 1 is the hook: at most 10 words, no greeting, no 'did you know', no 'today', no 'in this video'.\n"
        "- Scene 2 opens a loop: a promise or tease that is paid off near the end.\n"
        "- A twist or reveal in the scene before the last.\n"
        "- The LAST scene must be an unfinished sentence that flows into scene 1 when the video restarts "
        "(e.g. it ends with 'and that is why'). Read the last scene followed by scene 1 out loud: together they "
        "must sound like ONE natural sentence. The last scene has an empty imagem.\n"
        f"- Image terms only in {L['imagens_min']}-{L['imagens_max']} scenes (hook, key reveal, twist); all other "
        "scenes have an empty imagem. Concrete photographable things only (e.g. 'octopus underwater', "
        "'roman coin'): prefer the main subject (animal, place, object, landscape) that a stock-photo site "
        "would surely have, and avoid gross, medical or overly specific terms (use 'wombat', not "
        "'wombat poop'); never abstract words, people's names or brands.\n"
        "- Write numbers as digits (3,000, not three thousand).\n"
        "- Never use double quotes inside a sentence, no emojis, and no | or // characters.\n"
        f"{fb}\n"
        "OUTPUT: ONLY JSON with keys:\n"
        f'  "titulo": curiosity title, max {L["titulo_max"]} characters, no lies, no promise you cannot pay off,\n'
        '  "descricao": 1-2 sentences in English describing the video (no hashtags),\n'
        '  "tags": list of 5 lowercase English keywords,\n'
        '  "cenas": the list of scene objects described above.')
    return gemini(prompt, cfg, temperature=0.9)


def cenas_legiveis(p):
    linhas = []
    for i, (t, k) in enumerate(parse_cenas(p.get("cenas", "")), 1):
        linhas.append(f"Scene {i}: {t}" + (f"  [image: {k}]" if k else ""))
    return "\n".join(linhas)


def verificar(pilar, ideia, p, cfg, fontes=None):
    meta = {"titulo": p.get("titulo"), "descricao": p.get("descricao"), "tags": p.get("tags")}
    prompt = (
        "TASK: VERIFY\n"
        "You are a strict fact-checker and editor for a YouTube Shorts script.\n"
        f"Pillar: {pilar}\nPremise: {ideia}\n"
        f"{bloco_fontes(fontes)}"
        f"Title and description (JSON): {json.dumps(meta, ensure_ascii=False)}\n"
        f"Scenes:\n{cenas_legiveis(p)}\n"
        "Check:\n"
        "1) FACTS pillar: is every factual claim true? List doubtful or false claims. "
        "STORY pillar: is it clearly framed as fiction, with no real people, brands or real tragedies? "
        "NEWS pillar: is EVERY claim supported by the SOURCE MATERIAL (no added numbers, names, causes, "
        "quotes or predictions)? Is it attributed ('According to ...')? Neutral, no speculation, no blame "
        "on named people, no private individuals?\n"
        "2) Is scene 1 a strong hook of at most 10 words with no greeting?\n"
        "3) Does the last scene end as an unfinished sentence that flows into scene 1?\n"
        "Do NOT comment on formatting or word counts: other code already checks those.\n"
        'If everything is fine return {"aprovado": true, "problemas": [], "versao_corrigida": null}.\n'
        'Otherwise return {"aprovado": false, "problemas": ["..."], "versao_corrigida": {"titulo": "...", '
        '"descricao": "...", "tags": ["..."], "cenas": [{"texto": "...", "imagem": "..."}, ...]} with every '
        "problem fixed (same number of scenes, same rules), or null if it cannot be fixed}.\n"
        "Return ONLY JSON.")
    return gemini(prompt, cfg, temperature=0.2)


# ------------------------------------------------------------- validação
def limpar_texto(s):
    s = str(s or "")
    s = re.sub(r'["“”„]', "", s)
    return re.sub(r"\s+", " ", s).strip()


def sem_delimitadores(t):
    return re.sub(r"\s+", " ", re.sub(r"\||//", " ", t)).strip()


def cenas_para_texto(c):
    if isinstance(c, list):
        partes = []
        for x in c:
            if isinstance(x, dict):
                t = sem_delimitadores(limpar_texto(x.get("texto") or x.get("text")))
                k = sem_delimitadores(limpar_texto(x.get("imagem") or x.get("image")))
                partes.append(f"{t} | {k}" if k else f"{t} |")
            else:
                partes.append(f"{sem_delimitadores(limpar_texto(x))} |")
        return " // ".join(partes)
    return limpar_texto(c)


def parse_cenas(s):
    out = []
    for parte in s.split("//"):
        texto, _, termo = parte.partition("|")
        out.append((texto.strip(), termo.strip()))
    return out


def sanear(p):
    if not isinstance(p, dict):
        return {}
    tags = p.get("tags")
    if isinstance(tags, str):
        tags = re.split(r"[,;]", tags)
    tags = [re.sub(r"[^a-z0-9 ]", "", limpar_texto(t).lower().lstrip("#")).strip() for t in (tags or [])]
    return {"titulo": limpar_texto(p.get("titulo")), "descricao": limpar_texto(p.get("descricao")),
            "tags": [t for t in tags if t][:5], "cenas": cenas_para_texto(p.get("cenas"))}


def validar(p, cfg, pilar="factos"):
    L = limites(cfg, pilar)
    prob = []
    if not p.get("titulo"):
        prob.append("missing title")
    elif len(p["titulo"]) > L["titulo_max"]:
        prob.append(f"title longer than {L['titulo_max']} characters")
    if not p.get("descricao"):
        prob.append("missing description")
    if len(p.get("tags", [])) < 3:
        prob.append("need at least 3 tags")
    cenas = parse_cenas(p.get("cenas", ""))
    if not (L["cenas_min"] <= len(cenas) <= L["cenas_max"]):
        prob.append(f"need {L['cenas_min']}-{L['cenas_max']} scenes, got {len(cenas)}")
    total, com_img = 0, 0
    for i, (t, k) in enumerate(cenas):
        n = len(t.split())
        total += n
        if not t:
            prob.append(f"scene {i + 1} is empty")
        elif n < 2 or n > 16:
            prob.append(f"scene {i + 1} has {n} words (use 5-11)")
        if "|" in k:
            prob.append(f"scene {i + 1} has an extra | character")
        if k:
            com_img += 1
            if len(k.split()) > 3 or not re.match(r"^[A-Za-z][A-Za-z '\-]*$", k):
                prob.append(f"scene {i + 1} image term must be 1-3 plain English words")
            elif pilar == "noticias" and k != k.lower():
                prob.append(f"scene {i + 1} image term must be generic and lowercase (no names)")
        if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", t):
            prob.append(f"scene {i + 1} contains an emoji")
    if cenas:
        if len(cenas[0][0].split()) > 10:
            prob.append("scene 1 (the hook) must have at most 10 words")
        if re.search(r"did you know|in this video|^today", cenas[0][0], re.I):
            prob.append("scene 1 must not use 'did you know', 'today' or 'in this video'")
        if cenas[-1][1]:
            prob.append("the last scene must have NO image term")
    if not (L["palavras_min"] <= total <= L["palavras_max"]):
        prob.append(f"total words must be {L['palavras_min']}-{L['palavras_max']}, got {total}")
    if not (L["imagens_min"] <= com_img <= L["imagens_max"]):
        prob.append(f"need image terms in {L['imagens_min']}-{L['imagens_max']} scenes, got {com_img}")
    texto_all = f"{p.get('titulo', '')} {p.get('descricao', '')} {p.get('cenas', '')}".lower()
    if pilar == "noticias" and not re.search(r"according to|reported by|reports? (?:say|from)", texto_all):
        prob.append("attribute the news once with 'According to <source>'")
    palavras = cfg.get("palavras_proibidas", []) + (cfg.get("palavras_proibidas_noticias", []) if pilar == "noticias" else [])
    for w in palavras:
        if re.search(rf"\b{re.escape(w.lower())}\b", texto_all):
            prob.append(f"contains banned word '{w}'")
    return prob


def montar_payload(p, pilar, cfg, ideia, gancho, fontes=None):
    cenas = parse_cenas(p["cenas"])
    cenas_txt = " // ".join(f"{t} | {k}" if k else f"{t} |" for t, k in cenas)
    desc = p["descricao"]
    if pilar == "historias" and not desc.lower().startswith("fictional story"):
        desc = "Fictional story. " + desc
    nomes = []
    for f in fontes or []:
        if f.get("fonte") and f["fonte"] not in nomes:
            nomes.append(f["fonte"])
    if pilar == "noticias":
        desc += "\n\nBased on news reports" + (f" from {', '.join(nomes[:3])}" if nomes else "") + \
                ". Details may change."
    return {"titulo": p["titulo"], "descricao": desc, "tags": p["tags"], "cenas": cenas_txt,
            "linha": pilar, "voz": cfg.get("voz"), "ritmo": cfg.get("ritmo"),
            "tema": ideia, "gancho": gancho, "fontes": nomes[:3]}


# --------------------------------------------------------------- resumo
def resumo(payload, pilar, origem, preview, extra=""):
    linhas = [f"## {payload['titulo']}", "",
              f"**Pilar:** {pilar} · **Origem do tema:** {origem} · **Gancho:** {payload['gancho']}"
              f" · **Modo:** {'pré-visualização' if preview else 'real'}", "",
              f"**Tema:** {payload['tema']}", "", f"**Descrição:** {payload['descricao']}", "",
              f"**Tags:** {', '.join(payload['tags'])}", "", "| # | Frase | Imagem |", "|---|---|---|"]
    for i, (t, k) in enumerate(parse_cenas(payload["cenas"]), 1):
        linhas.append(f"| {i} | {t} | {k or '—'} |")
    texto = "\n".join(linhas) + ("\n\n" + extra if extra else "") + "\n"
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(texto)
    log("\n" + texto)


# ------------------------------------------------------------------ main
def main():
    cfg = load_json(ROOT / "config.json", None)
    if not cfg:
        raise RuntimeError("Falta o config.json.")
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("Falta o segredo GEMINI_API_KEY (Settings > Secrets > Actions).")
    preview = os.environ.get("PREVIEW", "").lower() == "true"
    forcado = (os.environ.get("PILAR") or "auto").lower()
    tema = (os.environ.get("TEMA") or "").strip()
    hist = load_json(DATA / "historico.json", {"usados": []})

    agendado = os.environ.get("AGENDADO", "").lower() == "true"
    por_dia = cfg.get("videos_por_dia", 1)
    if agendado and not preview and len(enviados_hoje(hist)) >= por_dia:
        msg = f"Já foram enviados {len(enviados_hoje(hist))} vídeo(s) hoje (limite {por_dia}); esta execução de reserva não faz nada."
        log(msg)
        set_output("skip", "true")
        f = os.environ.get("GITHUB_STEP_SUMMARY")
        if f:
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"## Execução de reserva dispensada\n\n{msg}\n")
        return 0

    pilar = escolher_pilar(cfg, forcado)
    nota, fontes, escolha = "", None, None
    if pilar == "noticias":
        try:
            cands, origem = candidatos_noticias(cfg, hist, tema)
            escolha = pontuar_noticias(cands, cfg)
            if escolha is None:
                raise SemNoticias("nenhuma notícia segura e forte o suficiente")
            fontes = escolha["fontes"]
        except SemNoticias as e:
            nota = f"Sem notícias adequadas hoje ({e}); usei o pilar 'factos'."
            log(nota)
            pilar, tema = "factos", ""
    log(f"Pilar: {pilar} | pré-visualização: {preview}")
    if escolha is None:
        cands, origem = candidatos(pilar, cfg, hist, tema)
        escolha = pontuar(cands, cfg)
    ideia = escolha["ideia"]
    gancho = escolher_gancho(pilar, hist)
    log(f"Tema ({origem}): {ideia} | gancho: {gancho}")

    final, feedback = None, None
    for tentativa in range(1, 4):
        log(f"--- Escrita, tentativa {tentativa}")
        p = sanear(escrever(pilar, ideia, gancho, cfg, feedback, fontes))
        prob = validar(p, cfg, pilar)
        if prob:
            feedback = "; ".join(prob)
            log("Rejeitado pelo código:", feedback)
            continue
        qa = verificar(pilar, ideia, p, cfg, fontes)
        if qa.get("aprovado"):
            final = p
            break
        corr = sanear(qa.get("versao_corrigida")) if qa.get("versao_corrigida") else None
        if corr and not validar(corr, cfg, pilar):
            log("Verificação pediu correções e a versão corrigida foi aceite:", qa.get("problemas"))
            final = corr
            break
        feedback = "; ".join(str(x) for x in qa.get("problemas", [])) or "the fact-check was not satisfied"
        log("Rejeitado na verificação:", feedback)

    if not final:
        log("Nenhuma versão passou nas verificações: o dia é saltado (nada será publicado).")
        set_output("skip", "true")
        f = os.environ.get("GITHUB_STEP_SUMMARY")
        if f:
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"## Dia saltado\n\nTema: {ideia}\n\nÚltimos problemas: {feedback}\n\n{info_gemini()}\n")
        guardar_modelo()
        return 0

    payload = montar_payload(final, pilar, cfg, ideia, gancho, fontes)
    (ROOT / "payload.json").write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    extra = info_gemini()
    if nota:
        extra = f"⚠️ {nota}\n\n" + extra
    if fontes:
        extra = "**Fontes:** " + " · ".join(f"{f['titulo']} ({f['fonte']})" for f in fontes) + "\n\n" + extra
    resumo(payload, pilar, origem, preview, extra=extra)
    guardar_modelo()
    set_output("skip", "false")
    set_output("pilar", pilar)
    set_output("titulo", payload["titulo"])

    if not preview:
        hist.setdefault("usados", []).append({
            "data": hoje(), "pilar": pilar, "tema": ideia, "gancho": gancho,
            "titulo": payload["titulo"], "estado": "pendente"})
        hist["usados"] = hist["usados"][-400:]
        save_json(DATA / "historico.json", hist)
        if pilar != "noticias":
            banco_path = DATA / f"banco_{pilar}.json"
            banco = load_json(banco_path, [])
            for b in banco:
                if b.get("ideia") == ideia:
                    b["usada"] = True
            save_json(banco_path, banco)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(concluir() if "--concluir" in sys.argv else main())
    except RuntimeError as e:
        print(f"ERRO: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
