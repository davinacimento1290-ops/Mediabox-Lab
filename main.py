# MEDIABOX - central de vídeos para Android e navegador
# O programa tenta instalar Flask, yt-dlp e requests se estiverem ausentes.

import os, re, sys, json, time, logging, threading, ipaddress, subprocess, html, secrets, hmac, base64, ast, shutil, tempfile
from urllib.parse import urlparse, urljoin, unquote, parse_qs, quote
from datetime import timedelta
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from mediabox_captcha import configurar_chave as configurar_chave_captcha, emitir_captcha, verificar_captcha

def instalar(modulo, pacote):
    try:
        __import__(modulo)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pacote])

instalar("flask", "Flask")
instalar("yt_dlp", "yt-dlp")
instalar("requests", "requests")

import requests
import yt_dlp
import imageio_ffmpeg
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for, send_from_directory, Response, stream_with_context

APP_NAME = "MEDIABOX"
PORTA = int(os.environ.get("PORT", "5000"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.environ.get("MEDIABOX_STORAGE_DIR"):
    PASTA = os.path.abspath(os.environ["MEDIABOX_STORAGE_DIR"])
elif os.path.isdir("/storage/emulated/0"):
    PASTA = "/storage/emulated/0/Movies"
else:
    PASTA = os.path.join(BASE_DIR, "media")

PASTA_LOGS = os.path.join(PASTA, "logs")
ARQUIVO_LOG = os.path.join(PASTA_LOGS, "servidor.log")
os.makedirs(PASTA, exist_ok=True)
os.makedirs(PASTA_LOGS, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(ARQUIVO_LOG, encoding="utf-8"),
              logging.StreamHandler()]
)
log = logging.getLogger("MEDIABOX")
app = Flask(__name__)
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediabox_auth.json")
SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediabox_session.key")

def carregar_chave():
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            chave = f.read().strip()
        if chave:
            return chave
    except OSError:
        pass
    chave = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(chave)
    return chave

app.secret_key = os.environ.get("MEDIABOX_SECRET_KEY") or carregar_chave()
configurar_chave_captcha(app.secret_key)

# Links de download assinados e temporários
DOWNLOAD_TOKEN_MAX_AGE = 15 * 60
download_signer = URLSafeTimedSerializer(
    app.secret_key,
    salt="mediabox-download-v1"
)

def validar_token_download(nome, token):
    if not nome or nome != os.path.basename(nome) or not token:
        return False

    try:
        dados = download_signer.loads(
            token,
            max_age=DOWNLOAD_TOKEN_MAX_AGE
        )
    except (BadSignature, SignatureExpired):
        return False

    nome_assinado = str(dados.get("nome", "")) if isinstance(dados, dict) else ""
    return hmac.compare_digest(nome_assinado.encode('utf-8'), nome.encode('utf-8'))
app.permanent_session_lifetime = timedelta(days=90)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("MEDIABOX_COOKIE_SECURE", "").lower() == "true"
)

# Acesso público sem conta individual. O registro interno serve apenas para auditoria.
CREDENCIAIS = {
    "usuario": "visitante",
    "senha_hash": generate_password_hash(secrets.token_urlsafe(32))
}

import admin_control
admin_control.inicializar(
    app,
    os.path.join(BASE_DIR, "mediabox_admin.sqlite3"),
    CREDENCIAIS
)
validar_usuario = admin_control.validar_usuario
registrar_evento = admin_control.registrar_evento

# Sistemas MEDIABOX: conquistas por usuário e prévia avançada local.
import mediabox_achievements_preview
import bluevision_reviews
bluevision_reviews.register(app, os.path.join(BASE_DIR, "mediabox_admin.sqlite3"), log, registrar_evento)
mediabox_achievements_preview.register(
    app, os.path.join(BASE_DIR, "mediabox_admin.sqlite3")
)
mediabox_achievements_preview_record = app.extensions["mediabox_achievement_record"]

@app.before_request
def preparar_sessao_publica():
    # O aplicativo abre diretamente; a área administrativa continua protegida
    # pelo fluxo independente de autenticação definido em admin_control.py.
    if request.endpoint == "entregar_arquivo":
        nome = (request.view_args or {}).get("nome", "")
        if validar_token_download(nome, request.args.get("token")):
            return None
        return jsonify({"erro": "Link de download inválido ou expirado."}), 403

    if not session.get("mediabox_autenticado"):
        session.permanent = True
        session["mediabox_autenticado"] = True
        session["mediabox_usuario"] = "visitante"


@app.get("/captcha/novo")
def captcha_novo():
    question, token = emitir_captcha(session)
    return jsonify({"question": question, "token": token, "expires_in": 180})


lock = threading.Lock()
preview_stream_lock = threading.Lock()
preview_streams = {}
PREVIEW_STREAM_TTL = 15 * 60
estado = {"ativo": False, "progresso": 0, "status": "Pronto para baixar",
          "arquivo": "", "erro": "", "plataforma": ""}

# Fila sequencial: reutiliza o mesmo downloader, FFmpeg e assinatura de links.
fila_downloads = []
fila_running = False
fila_paused = False
fila_current_id = None
MAX_FILA_BATCH = 20
MAX_FILA_TOTAL = 50
FORMATOS = {
    "mp4": "best[ext=mp4]/best",
    "melhor": "bestvideo+bestaudio/best",
    "menor": "worst[ext=mp4]/worst"
}
PLATAFORMAS = {"auto", "youtube", "instagram", "kwai", "tiktok", "outros"}
HEADERS_KWAI = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
}

def atualizar(**dados):
    with lock:
        estado.update(dados)

def validar_url(url):
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Cole um link válido começando com http:// ou https://.")
    host = p.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("Endereços locais não são permitidos.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Endereço IP privado não permitido.")
    except ValueError as e:
        if "não permitido" in str(e):
            raise
    return url

def detectar(url):
    host = (urlparse(url).hostname or "").lower()
    if any(x in host for x in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "youtube"
    if "instagram.com" in host:
        return "instagram"
    if host == "tiktok.com" or host.endswith(".tiktok.com") or host == "tiktokv.com" or host.endswith(".tiktokv.com"):
        return "tiktok"
    if "kwai.com" in host or "kwai-video.com" in host:
        return "kwai"
    return "outros"

def hook(d):
    if d.get("status") == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        baixado = d.get("downloaded_bytes") or 0
        pct_download = int(baixado * 100 / total) if total else 0
        pct_total = min(64, max(0, int(pct_download * 0.65)))
        atualizar(
            progresso=pct_total,
            status="Baixando vídeo..."
        )
    elif d.get("status") == "finished":
        atualizar(progresso=65, status="Download recebido. Preparando a marca d'água...")

def nome_seguro(nome):
    nome = re.sub(r'[\\/*?:"<>|]', "_", unquote(nome or "video"))
    return re.sub(r"\s+", " ", nome).strip(" .")[:120] or "video"


def extrair_kwai(html, pagina):
    """Usa os mesmos métodos do script de Kwai que o Davi enviou."""
    url_mp4 = None

    # Método 1: JSON de dados da página (window.pageData)
    if "window.pageData" in html:
        try:
            json_str = html.split("window.pageData")[1].split("=")[1].split(";</script>")[0].strip()
            dados = json.loads(json_str)
            url_mp4 = (
                (dados.get("video") or {}).get("srcNoMark")
                or ((dados.get("video") or {}).get("kwaiPhoto") or {}).get("mainM3u8Url")
            )
        except Exception:
            pass

    # Método 2: procurar uma URL que termine em .mp4
    if not url_mp4 and ".mp4" in html:
        partes = html.split(".mp4")
        for parte in partes[:-1]:
            trecho = parte.split('"')[-1].split("'")[-1]
            if trecho.startswith("http"):
                url_mp4 = trecho + ".mp4"
                break

    # Método 3: meta tags OpenGraph
    if not url_mp4:
        match_og = (
            re.search(r'property="og:video:url"\s+content="([^"]+)"', html)
            or re.search(r'property="og:video"\s+content="([^"]+)"', html)
        )
        if match_og:
            url_mp4 = match_og.group(1)

    if not url_mp4:
        return None

    return url_mp4.replace("\\u002F", "/").replace("\\", "").replace("&amp;", "&")



def extrair_titulo_kwai(html_pagina, url_pagina):
    """Tenta obter o título real do vídeo; usa um fallback seguro se não existir."""
    candidatos = []

    # OpenGraph: o Kwai pode variar a ordem dos atributos.
    padroes_meta = [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:title["\']',
    ]
    for padrao in padroes_meta:
        m = re.search(padrao, html_pagina, re.IGNORECASE | re.DOTALL)
        if m:
            candidatos.append(m.group(1))

    # Título da página como segunda opção.
    m = re.search(r'<title[^>]*>(.*?)</title>', html_pagina, re.IGNORECASE | re.DOTALL)
    if m:
        candidatos.append(m.group(1))

    # Campos de título/caption no JSON da página, quando disponíveis.
    if "window.pageData" in html_pagina:
        try:
            json_str = html_pagina.split("window.pageData", 1)[1].split("=", 1)[1].split(";</script>", 1)[0].strip()
            dados = json.loads(json_str)
            video = dados.get("video") or {}
            for chave in ("title", "caption", "description", "photoTitle"):
                if video.get(chave):
                    candidatos.append(str(video[chave]))
            for chave in ("title", "caption", "description"):
                if dados.get(chave):
                    candidatos.append(str(dados[chave]))
        except Exception:
            pass

    for candidato in candidatos:
        titulo = html.unescape(str(candidato))
        titulo = titulo.replace("\\u002F", "/").replace("\n", " ")
        titulo = re.sub(r"<[^>]+>", " ", titulo)
        titulo = re.sub(r"\s+", " ", titulo).strip(" .-_|")
        # Evita nomes genéricos ou títulos enormes/ruins vindos de SEO.
        if titulo and titulo.lower() not in {"kwai", "kwai - vídeos curtos", "vídeos curtos"}:
            titulo = nome_seguro(titulo)
            if titulo:
                return titulo

    # Se a página não expuser título, pelo menos usa o ID do vídeo.
    caminho = urlparse(url_pagina).path.rstrip("/")
    video_id = caminho.split("/")[-1] if caminho else ""
    if video_id.isdigit():
        return "Kwai_" + video_id
    return "video_kwai"

def baixar_kwai(url):
    atualizar(status="Buscando e processando o vídeo do Kwai...", progresso=2)
    session = requests.Session()
    session.headers.update(HEADERS_KWAI)

    resposta = session.get(url, allow_redirects=True, timeout=12)
    resposta.raise_for_status()
    # Alguns links compartilhados passam por kwai-share.onelink.me.
    # O destino real fica dentro do parâmetro af_sub1 (JSON codificado).
    url_extracao = resposta.url
    try:
        parametros = parse_qs(urlparse(resposta.url).query)
        dados_compartilhamento = json.loads(parametros.get("af_sub1", ["{}"])[0])
        destino = dados_compartilhamento.get("shareInfo", {}).get("target_url")
        if destino and urlparse(destino).scheme in {"http", "https"}:
            url_extracao = destino
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    # Quando o redirecionador não fornece o destino em af_sub1,
    # tenta recuperá-lo do parâmetro deep_link_value.
    if urlparse(url_extracao).hostname in {"kwai-share.onelink.me", "onelink.me"}:
        try:
            parametros = parse_qs(urlparse(resposta.url).query)
            deep_link = parametros.get("deep_link_value", [""])[0]
            destino = parse_qs(urlparse(deep_link).query).get("target_url", [""])[0]
            if destino.startswith(("https://", "http://")):
                url_extracao = destino
        except (ValueError, TypeError):
            pass

    pagina_kwai = resposta
    if url_extracao != resposta.url:
        try:
            pagina_kwai = session.get(url_extracao, timeout=12)
            pagina_kwai.raise_for_status()
        except requests.RequestException:
            pagina_kwai = resposta
            url_extracao = resposta.url

    titulo_video = extrair_titulo_kwai(pagina_kwai.text, url_extracao)
    url_mp4 = extrair_kwai(pagina_kwai.text, url_extracao)

    # Fallback: usa a página real do vídeo, não o redirecionador de anúncios.
    if not url_mp4:
        try:
            opcoes_kwai = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "format": "best[ext=mp4]/best",
                "http_headers": HEADERS_KWAI,
            }
            with yt_dlp.YoutubeDL(opcoes_kwai) as ydl:
                info_kwai = ydl.extract_info(url_extracao, download=False)

            if info_kwai and info_kwai.get("entries"):
                info_kwai = next(
                    (item for item in info_kwai["entries"] if item),
                    None
                )

            if info_kwai:
                url_mp4 = info_kwai.get("url")
                if not url_mp4:
                    formatos = [
                        item for item in info_kwai.get("formats", [])
                        if item.get("url") and item.get("vcodec") != "none"
                    ]
                    formatos_mp4 = [
                        item for item in formatos
                        if item.get("ext") == "mp4"
                    ]
                    candidatos = formatos_mp4 or formatos
                    if candidatos:
                        url_mp4 = candidatos[-1]["url"]

                titulo_extraido = info_kwai.get("title")
                if titulo_extraido and titulo_video in {"video_kwai"}:
                    titulo_video = nome_seguro(titulo_extraido)
        except Exception as erro_extracao:
            log.warning("Fallback yt-dlp do Kwai falhou: %s", erro_extracao)

    if not url_mp4:
        raise RuntimeError("O Kwai não revelou o arquivo de vídeo. Tente copiar novamente o link público e tente mais tarde.")

    session.headers["Referer"] = url_extracao
    atualizar(status="Baixando o vídeo do Kwai...", progresso=5)
    video_req = session.get(url_mp4, stream=True, timeout=15, allow_redirects=True)
    video_req.raise_for_status()

    if "text/html" in video_req.headers.get("Content-Type", "").lower():
        video_req.close()
        raise RuntimeError("O Kwai devolveu uma página HTML em vez do arquivo de vídeo.")

    # Salva na pasta configurada pelo MEDIABOX.
    caminho_arquivo = os.path.join(PASTA, nome_seguro(titulo_video) + ".mp4")
    raiz, ext = os.path.splitext(caminho_arquivo)
    contador = 1
    while os.path.exists(caminho_arquivo):
        caminho_arquivo = f"{raiz}_{contador}{ext}"
        contador += 1

    total = int(video_req.headers.get("Content-Length", "0") or 0)
    recebido = 0
    try:
        with open(caminho_arquivo, "wb") as arquivo:
            for chunk in video_req.iter_content(chunk_size=8192):
                if chunk:
                    arquivo.write(chunk)
                    recebido += len(chunk)
                    progresso = min(99, int(recebido * 100 / total)) if total else 0
                    status_txt = f"Baixando Kwai... {progresso}%" if total else f"Baixando Kwai... {recebido // 1048576} MB"
                    atualizar(progresso=progresso, status=status_txt)
    except Exception:
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)
        raise
    finally:
        video_req.close()

    if recebido == 0:
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)
        raise RuntimeError("O arquivo baixado está vazio.")

    log.info("Kwai salvo em %s", caminho_arquivo)
    return caminho_arquivo


def obter_logo_marca(tipo="dias_edits"):
    """Retorna a arte original escolhida para a marca d'água."""
    if tipo == "dias_edits":
        logo = os.path.join(BASE_DIR, "static", "dias_edits_watermark.jpg")
        if not os.path.isfile(logo) or os.path.getsize(logo) < 100:
            raise FileNotFoundError("A imagem original Dias Edits não foi encontrada em static/dias_edits_watermark.jpg.")
        return logo
    if tipo != "mediabox":
        raise ValueError("Tipo de marca d'água inválido.")
    origem = os.path.join(BASE_DIR, "powered_by_mediabox.png.b64")
    logo = os.path.join(BASE_DIR, "powered_by_mediabox.png")
    if not os.path.isfile(origem) or os.path.getsize(origem) == 0:
        raise FileNotFoundError("A arte Powered by MEDIABOX não foi encontrada no servidor.")
    with open(origem, "r", encoding="ascii") as arquivo:
        encoded = "".join(arquivo.read().split()).rstrip("=")
    encoded += "=" * (-len(encoded) % 4)
    try:
        dados = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise RuntimeError("A imagem Powered by MEDIABOX está inválida.") from exc
    if dados[:8] != bytes.fromhex("89504e470d0a1a0a"):
        raise RuntimeError("A arte Powered by MEDIABOX não é um PNG válido.")
    with open(logo, "wb") as arquivo:
        arquivo.write(dados)
    return logo

def aplicar_marca_dagua(caminho, duracao=None, posicao="inferior_direito", pos_x=88, pos_y=88, marca_tipo="dias_edits"):
    """Aplica a marca selecionada mantendo a posição personalizada."""
    logo = obter_logo_marca(marca_tipo)
    nome_marca = "Dias Edits" if marca_tipo == "dias_edits" else "MEDIABOX"
    if not os.path.isfile(logo):
        raise FileNotFoundError("A logo oficial MEDIABOX não foi encontrada no servidor.")
    caminho = os.path.abspath(caminho)
    if not os.path.isfile(caminho) or os.path.getsize(caminho) == 0:
        raise RuntimeError("O vídeo baixado não foi encontrado ou está vazio.")
    raiz, ext = os.path.splitext(caminho)
    destino = caminho if ext.lower() == ".mp4" else raiz + "_MEDIABOX.mp4"
    temporario = destino + ".watermark_tmp.mp4"
    log_ffmpeg = temporario + ".log"
    for auxiliar in (temporario, log_ffmpeg):
        if os.path.exists(auxiliar):
            os.remove(auxiliar)
    atualizar(progresso=80, status=f"Preparando marca d'água {nome_marca}...")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # Descobre a duração lendo apenas os metadados; isso permite calcular
    # a porcentagem real também em plataformas que não informam duration.
    if not duracao or float(duracao or 0) <= 0:
        try:
            metadados = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", caminho],
                capture_output=True, text=True, timeout=15
            )
            saida_meta = (metadados.stderr or "") + "\n" + (metadados.stdout or "")
            match_duracao = re.search(
                r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
                saida_meta
            )
            if match_duracao:
                hh, mm, ss = match_duracao.groups()
                duracao = int(hh) * 3600 + int(mm) * 60 + float(ss)
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
            log.warning("Não foi possível obter a duração do vídeo para o progresso.")
    atualizar(progresso=65, status=f"Aplicando marca d'água {nome_marca}...")
    try:
        pos_x = max(0.0, min(100.0, float(pos_x)))
        pos_y = max(0.0, min(100.0, float(pos_y)))
    except (TypeError, ValueError):
        pos_x, pos_y = 88.0, 88.0
    # Recorta 8% das bordas da arte e amplia a área visível para cerca de
    # 70% da largura do vídeo (máximo 760 px), mantendo a proporção original.
    # O mesmo recorte é reproduzido na prévia do site.
    pos_x_frac = pos_x / 100.0
    pos_y_frac = pos_y / 100.0
    filtro_video = (
        "[1:v]crop=w='iw*0.84':h='ih*0.84':x='iw*0.08':y='ih*0.08',"
        "scale=w='min(760\\,iw*0.70)':h=-1,"
        "format=rgba,colorchannelmixer=aa=0.92[wm];"
        f"[0:v][wm]overlay=x='(W-w)*{pos_x_frac:.5f}':"
        f"y='(H-h)*{pos_y_frac:.5f}'[outv]"
    )
    comando = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-threads", "2", "-i", caminho,
        "-loop", "1", "-threads", "1", "-i", logo,
        "-filter_complex_threads", "1",
        "-filter_complex", filtro_video,
        "-map", "[outv]", "-map", "0:a?",
        "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast",
        "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-shortest",
        "-stats_period", "0.25", "-progress", "pipe:1", temporario
    ]
    processo = None
    leitor = None
    try:
        with open(log_ffmpeg, "w", encoding="utf-8") as registro_ffmpeg:
            processo = subprocess.Popen(
                comando, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=registro_ffmpeg, text=True, encoding="utf-8",
                errors="replace", bufsize=1
            )
            def acompanhar_ffmpeg():
                if processo.stdout is None:
                    return
                ultimo_status = ""
                for linha in processo.stdout:
                    linha = linha.strip()
                    if not linha.startswith("out_time="):
                        continue
                    tempo = linha.split("=", 1)[1]
                    if not tempo or tempo == ultimo_status:
                        continue
                    ultimo_status = tempo
                    try:
                        partes = tempo.split(":")
                        segundos = float(partes[0]) * 3600 + float(partes[1]) * 60 + float(partes[2])
                        if duracao and float(duracao) > 0:
                            pct = 65 + int(min(1.0, segundos / float(duracao)) * 34)
                            pct = min(99, max(65, pct))
                            atualizar(progresso=pct, status=f"Aplicando marca d'água {nome_marca}...")
                        else:
                            atualizar(status=f"Aplicando marca d'água {nome_marca}...")
                    except (ValueError, IndexError, TypeError):
                        atualizar(status=f"Aplicando marca d'água {nome_marca}...")
            leitor = threading.Thread(target=acompanhar_ffmpeg, daemon=True)
            leitor.start()
            try:
                codigo = processo.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                processo.kill()
                processo.wait()
                raise RuntimeError("A aplicação da marca d'água excedeu 30 minutos. Tente um vídeo menor.")
            leitor.join(timeout=5)
        if codigo != 0:
            try:
                with open(log_ffmpeg, "r", encoding="utf-8", errors="replace") as arquivo_log:
                    arquivo_log.seek(max(0, os.path.getsize(log_ffmpeg) - 2400))
                    detalhe = arquivo_log.read()[-1200:]
            except OSError:
                detalhe = "Falha no FFmpeg sem detalhes adicionais."
            raise RuntimeError("Não foi possível aplicar a marca d'água: " + (detalhe.strip() or "FFmpeg retornou erro."))
        if not os.path.isfile(temporario) or os.path.getsize(temporario) == 0:
            raise RuntimeError("O FFmpeg não gerou o vídeo final.")
        os.replace(temporario, destino)
    finally:
        if processo is not None and processo.poll() is None:
            processo.kill()
            processo.wait()
        if leitor is not None and leitor.is_alive():
            leitor.join(timeout=2)
        for auxiliar in (temporario, log_ffmpeg):
            try:
                if os.path.exists(auxiliar):
                    os.remove(auxiliar)
            except OSError:
                log.warning("Não foi possível remover o temporário: %s", auxiliar)
    return destino

class YouTubeLogCapture:
    """Captura avisos específicos do yt-dlp sem ocultar os logs originais."""
    def __init__(self, logger_base):
        self.logger_base = logger_base
        self.rate_limited = False
        self.runtime_missing = False

    def debug(self, mensagem):
        self.logger_base.debug(str(mensagem))

    def info(self, mensagem):
        self.logger_base.info(str(mensagem))

    def warning(self, mensagem):
        texto = str(mensagem)
        texto_lower = texto.lower()
        if "http error 429" in texto_lower or "too many requests" in texto_lower:
            self.rate_limited = True
        if "no supported javascript runtime" in texto_lower:
            self.runtime_missing = True
        self.logger_base.warning(texto)

    def error(self, mensagem):
        self.logger_base.error(str(mensagem))


def baixar(url, formato, plataforma, usuario="desconhecido", marca_dagua=False, posicao_marca="inferior_direito", marca_x=88, marca_y=88, marca_tipo="dias_edits"):
    logger_youtube = None
    try:
        url = validar_url(url)
        real = detectar(url) if plataforma == "auto" else plataforma
        atualizar(plataforma=real, status=f"Preparando {real}...", progresso=0, arquivo="", erro="")
        if real == "kwai":
            caminho = baixar_kwai(url)
            if marca_dagua:
                caminho = aplicar_marca_dagua(caminho, posicao=posicao_marca, pos_x=marca_x, pos_y=marca_y, marca_tipo=marca_tipo)
            detalhe_marca = ("com marca d'água Dias Edits." if marca_tipo == "dias_edits" else "com marca d'água MEDIABOX.") if marca_dagua else "sem marca d'água."
            atualizar(
                ativo=False,
                progresso=100,
                status=f"Download concluído; {detalhe_marca}",
                arquivo=os.path.basename(caminho)
            )
            registrar_evento(usuario, "download", os.path.basename(caminho), real, "sucesso")
            mediabox_achievements_preview_record(usuario, "download")
            return
        opcoes = {
            "format": FORMATOS[formato],
            "outtmpl": os.path.join(PASTA, "%(title).100s_%(id)s.%(ext)s"),
            "noplaylist": True, "retries": 5, "fragment_retries": 5,
            "socket_timeout": 30, "continuedl": True, "overwrites": False,
            "progress_hooks": [hook], "quiet": True, "logger": log
        }
        # TikTok: o yt-dlp usa impersonação via curl_cffi quando disponível.
        # Não sobrescrever o User-Agent: ele precisa combinar com a impressão TLS
        # do navegador simulada pelo curl_cffi.

        # YouTube: recupera somente o perfil usado no primeiro teste do MEDIABOX 2.
        # Mantém a captura de logs do MEDIABOX e não altera as outras plataformas.
        if real == "youtube":
            logger_youtube = YouTubeLogCapture(log)
            opcoes["logger"] = logger_youtube
            opcoes["extractor_args"] = {
                "youtube": {
                    "player_client": ["android_vr"]
                }
            }
            # Mesmos parâmetros de conexão do código funcional do Pydroid 3.
            opcoes["socket_timeout"] = 30
            opcoes["retries"] = 5
            opcoes["fragment_retries"] = 5
            if formato == "mp4":
                opcoes["format"] = "best"

        atualizar(status=f"Conectando ao {real}...")
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((item for item in info["entries"] if item), None)
            if not isinstance(info, dict):
                raise RuntimeError("A plataforma não retornou metadados de um vídeo para download.")
            candidatos = [info.get("filepath"), info.get("_filename")]
            for item in info.get("requested_downloads") or []:
                candidatos.insert(0, item.get("filepath"))
            candidatos.append(ydl.prepare_filename(info))
        caminho_original = next(
            (p for p in candidatos if p and os.path.isfile(p)),
            None
        )
        if not caminho_original:
            raise RuntimeError("O download terminou, mas o arquivo de vídeo não foi localizado.")
        duracao_video = info.get("duration") if isinstance(info, dict) else None
        caminho = aplicar_marca_dagua(caminho_original, duracao=duracao_video, posicao=posicao_marca, pos_x=marca_x, pos_y=marca_y, marca_tipo=marca_tipo) if marca_dagua else caminho_original
        detalhe_marca = ("com marca d'água Dias Edits." if marca_tipo == "dias_edits" else "com marca d'água MEDIABOX.") if marca_dagua else "sem marca d'água."
        atualizar(
            ativo=False,
            progresso=100,
            status=f"Download concluído; {detalhe_marca}",
            arquivo=os.path.basename(caminho)
        )
        log.info("Download concluído: %s", caminho)
        registrar_evento(usuario, "download", os.path.basename(caminho), real, "sucesso")
        mediabox_achievements_preview_record(usuario, "download")
    except Exception as e:
        log.exception("Falha no download")
        mensagem = str(e) or "Erro desconhecido."
        texto = mensagem.lower()

        host = (urlparse(url).hostname or "").lower()
        eh_youtube = (
            real == "youtube" if "real" in locals()
            else any(x in host for x in ("youtube.com", "youtu.be"))
        )

        if real == "tiktok" and ("unexpected response from webpage request" in texto or "no video formats found" in texto or "challenge" in texto or "captcha" in texto or "403" in texto or "forbidden" in texto):
            status = "O TikTok bloqueou a solicitação do servidor."
            mensagem = (
                "O TikTok respondeu com uma página de verificação ou bloqueio, e o servidor "
                "não conseguiu extrair este vídeo. Isso pode depender do vídeo ou do acesso "
                "temporário do servidor. O diagnóstico técnico foi preservado abaixo."
            )
        elif eh_youtube and logger_youtube and logger_youtube.rate_limited:
            status = "Limite temporário do YouTube (HTTP 429)."
            mensagem = (
                "O YouTube limitou temporariamente as solicitações recebidas pelo "
                "servidor global do MEDIABOX (HTTP 429). O aplicativo agora espaça "
                "mais as requisições, mas o limite precisa ser liberado pela própria "
                "plataforma. Aguarde e tente novamente mais tarde."
            )
        elif eh_youtube and (
            "not available in your country" in texto
            or "not available in your region" in texto
            or "uploader has not made this video available" in texto
        ):
            status = "Vídeo indisponível na região do servidor."
            mensagem = (
                "O YouTube informou que este vídeo não está disponível "
                "na região do servidor global do MEDIABOX."
            )
        elif eh_youtube and (
            "sign in to confirm" in texto
            or "not a bot" in texto
            or "confirm you're not a bot" in texto
        ):
            status = "O YouTube exige verificação de acesso."
            mensagem = (
                "O YouTube exige uma verificação de acesso que o servidor não pode "
                "concluir. Abra o vídeo diretamente no YouTube e verifique se ele "
                "está acessível pela sua conta."
            )
        elif eh_youtube and logger_youtube and logger_youtube.runtime_missing:
            status = "Runtime JavaScript ausente no servidor."
            mensagem = (
                "O yt-dlp informou que não encontrou um runtime JavaScript compatível. "
                "A extração do YouTube pode ficar limitada; isso é uma configuração "
                "do servidor, não um problema do link."
            )
        elif eh_youtube and "this video is not available" in texto:
            status = "Vídeo não acessível pelo servidor."
            mensagem = (
                "O YouTube não disponibilizou este vídeo ao servidor. Ele pode estar "
                "privado, removido, sujeito a restrições ou temporariamente indisponível. "
                "Teste somente um vídeo público que abra normalmente no YouTube."
            )
        else:
            status = "O download falhou."

        atualizar(ativo=False, status=status, erro=mensagem)
        registrar_evento(
            usuario, "download", resultado="falha",
            platform=locals().get("real", plataforma),
            details=mensagem[:500]
        )




def gerar_miniatura_video(caminho_video):
    """Gera uma miniatura leve do vídeo sem fazer a conclusão depender dela."""
    nome = "mediabox_thumb_" + secrets.token_hex(12) + ".jpg"
    destino = os.path.join(PASTA, nome)
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        resultado = subprocess.run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.5", "-i", caminho_video,
                "-frames:v", "1", "-vf", "scale=480:-2",
                "-q:v", "5", destino
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=35,
            check=False
        )
        if (
            resultado.returncode == 0
            and os.path.isfile(destino)
            and os.path.getsize(destino) > 100
        ):
            return nome
    except Exception as exc:
        log.warning("Miniatura do vídeo não pôde ser gerada: %s", exc)

    try:
        if os.path.isfile(destino):
            os.remove(destino)
    except OSError:
        pass
    return ""




def processar_fila_downloads(usuario):
    global fila_running, fila_paused, fila_current_id

    try:
        while True:
            with lock:
                item = next(
                    (x for x in fila_downloads if x.get("status") == "pending"),
                    None
                )

                if item is None:
                    fila_running = False
                    fila_paused = False
                    fila_current_id = None
                    estado.update(
                        ativo=False,
                        status="Fila concluída. Confira os vídeos e as prévias abaixo.",
                        erro=""
                    )
                    return

                if fila_paused:
                    fila_running = False
                    fila_current_id = None
                    estado.update(
                        ativo=False,
                        status="Fila pausada. Toque em Retomar para continuar.",
                        erro=""
                    )
                    return

                item["status"] = "downloading"
                item["progresso"] = 0
                item["erro"] = ""
                item["arquivo"] = ""
                item["download_url"] = ""
                item["thumbnail"] = ""
                fila_current_id = item["id"]

                estado.update(
                    ativo=True,
                    progresso=0,
                    status="Iniciando item da fila...",
                    arquivo="",
                    erro="",
                    plataforma=item["plataforma"]
                )

            try:
                baixar(
                    item["url"],
                    item["formato"],
                    item["plataforma"],
                    usuario,
                    item["marca_dagua"],
                    item["posicao_marca"],
                    item.get("marca_x", 88),
                    item.get("marca_y", 88)
                )
            except Exception as exc:
                log.exception("Erro inesperado no item da fila MEDIABOX")
                atualizar(ativo=False, status="Download da fila falhou.", erro=str(exc)[:500])

            with lock:
                resultado = dict(estado)

            nome = resultado.get("arquivo", "")
            caminho = os.path.join(PASTA, nome) if nome else ""
            arquivo_ok = (
                bool(nome)
                and nome == os.path.basename(nome)
                and os.path.isfile(caminho)
                and os.path.getsize(caminho) > 0
                and not resultado.get("erro")
            )

            miniatura = gerar_miniatura_video(caminho) if arquivo_ok else ""

            with lock:
                if arquivo_ok:
                    item.update(
                        status="done",
                        progresso=100,
                        arquivo=nome,
                        thumbnail=miniatura,
                        erro="",
                        download_url=""
                    )
                else:
                    item.update(
                        status="error",
                        progresso=max(0, min(99, int(resultado.get("progresso") or 0))),
                        arquivo="",
                        thumbnail="",
                        erro=(resultado.get("erro") or resultado.get("status") or "Não foi possível concluir este download.")[:500],
                        download_url=""
                    )

                estado["ativo"] = False
                fila_current_id = None

    except Exception as exc:
        log.exception("Falha geral no gerenciador de fila MEDIABOX")
        with lock:
            atual = next(
                (x for x in fila_downloads if x.get("id") == fila_current_id),
                None
            )
            if atual:
                atual.update(
                    status="error",
                    erro=str(exc)[:500],
                    progresso=0,
                    download_url="",
                    thumbnail=""
                )
    finally:
        with lock:
            fila_running = False
            fila_current_id = None
            if estado.get("ativo"):
                estado["ativo"] = False

def pesquisar_youtube(termo):
    termo = (termo or "").strip()
    if not termo:
        return []

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Configure YOUTUBE_API_KEY nas variáveis de ambiente do servidor."
        )

    parametros = {
        "part": "snippet",
        "type": "video,channel",
        "maxResults": 10,
        "q": termo,
        "key": api_key,
        "safeSearch": "strict"
    }

    try:
        resposta = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=parametros,
            timeout=20
        )
        dados = resposta.json()
    except requests.RequestException:
        raise RuntimeError("Não foi possível conectar à API do YouTube.")
    except ValueError:
        raise RuntimeError("O YouTube retornou uma resposta inválida.")

    if resposta.status_code != 200:
        erro = (dados.get("error") or {}).get("message")
        raise RuntimeError("Erro na API do YouTube: " + (
            erro or "Não foi possível realizar a pesquisa."
        ))

    saida = []
    for item in dados.get("items", []):
        identificador = item.get("id") or {}
        snippet = item.get("snippet") or {}
        video_id = identificador.get("videoId")
        channel_id = identificador.get("channelId")

        if video_id:
            url_resultado = "https://www.youtube.com/watch?v=" + video_id
            tipo_resultado = "Vídeo · " + (snippet.get("channelTitle") or "YouTube")
        elif channel_id:
            url_resultado = "https://www.youtube.com/channel/" + channel_id
            tipo_resultado = "Canal do YouTube"
        else:
            continue

        saida.append({
            "titulo": snippet.get("title") or "Sem título",
            "url": url_resultado,
            "tipo": tipo_resultado,
            "duracao": ""
        })

    return saida

def pesquisar_instagram(usuario):
    usuario = (usuario or "").strip()
    if "instagram.com/" in usuario.lower():
        if not usuario.startswith("http"):
            usuario = "https://" + usuario
        partes = [x for x in urlparse(usuario).path.split("/") if x]
        if not partes or partes[0].lower() in {"p", "reel", "reels", "stories", "explore"}:
            raise ValueError("Cole o @usuário ou o link direto do perfil, não o link de uma publicação.")
        usuario = partes[0]
    usuario = usuario.lstrip("@").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", usuario):
        raise ValueError("Digite um nome de usuário válido do Instagram.")

    url = f"https://www.instagram.com/{usuario}/"
    titulo = "@" + usuario
    aviso = ""
    foto = ""
    bio = ""
    seguidores = ""

    # Fonte alternativa gratuita: endpoint público de perfil usado pelo cliente web do Instagram.
    # Somente leitura; sem login, cookies, credenciais ou acesso a contas privadas.
    try:
        from curl_cffi import requests as curl_requests
        resposta_api = curl_requests.get(
            "https://i.instagram.com/api/v1/users/web_profile_info/",
            params={"username": usuario},
            headers={
                "X-IG-App-ID": "936619743392459",
                "Accept": "*/*",
                "User-Agent": "Instagram 360.0.0.0.0 Android"
            },
            impersonate="chrome",
            timeout=12
        )
        if resposta_api.status_code == 200:
            dados_api = resposta_api.json()
            user_api = (dados_api.get("data") or {}).get("user") or dados_api.get("user") or {}
            if isinstance(user_api, dict) and user_api:
                titulo = user_api.get("full_name") or titulo
                foto = user_api.get("profile_pic_url_hd") or user_api.get("profile_pic_url") or foto
                bio = (user_api.get("biography") or user_api.get("bio") or bio).strip()
                contagem_api = (
                    (user_api.get("edge_followed_by") or {}).get("count")
                    or user_api.get("follower_count")
                    or user_api.get("followers_count")
                )
                if contagem_api is not None:
                    seguidores = f"{int(contagem_api):,}".replace(",", ".")
    except Exception as erro_api:
        log.info("Fonte alternativa do Instagram indisponível para @%s: %s", usuario, erro_api)

    # Segunda fonte gratuita: metadados HTML do perfil público.
    # Não usa cookies, sessão de usuário, credenciais ou serviços pagos.
    try:
        resposta = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
            },
            timeout=18,
            allow_redirects=True
        )
        if resposta.status_code == 200:
            pagina = resposta.text

            # Lê meta tags sem depender da ordem dos atributos HTML.
            class MetaParser(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.meta = {}
                def handle_starttag(self, tag, attrs):
                    if tag.lower() != "meta":
                        return
                    dados = {str(k).lower(): (v or "") for k, v in attrs}
                    chave = (dados.get("property") or dados.get("name") or "").lower()
                    valor = dados.get("content", "")
                    if chave and valor:
                        self.meta[chave] = html.unescape(valor).strip()

            parser = MetaParser()
            parser.feed(pagina)
            metas = parser.meta
            og_titulo = metas.get("og:title", "")
            og_foto = metas.get("og:image", "")
            og_desc = metas.get("og:description", "")
            if og_titulo:
                titulo = og_titulo
            foto = og_foto or foto

            # Dados estruturados e JSON incorporado, quando o Instagram os disponibiliza.
            blocos_json = re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                pagina, re.I | re.S
            )
            for bloco in blocos_json:
                try:
                    estruturado = json.loads(html.unescape(bloco))
                    fila = estruturado if isinstance(estruturado, list) else [estruturado]
                    for item in fila:
                        if not isinstance(item, dict):
                            continue
                        foto_json = item.get("image")
                        if isinstance(foto_json, dict):
                            foto_json = foto_json.get("url")
                        if isinstance(foto_json, str) and not foto:
                            foto = foto_json
                        descricao = item.get("description")
                        if isinstance(descricao, str) and not bio:
                            bio = descricao.strip()
                except (ValueError, TypeError):
                    continue

            # Alguns perfis incluem estes campos no JSON da página.
            foto_match = re.search(r'"(?:profile_pic_url_hd|profile_pic_url)"\s*:\s*("(?:\\.|[^"\\])*")', pagina)
            if foto_match:
                try:
                    foto_json = json.loads(foto_match.group(1))
                    if foto_json:
                        foto = foto_json
                except (ValueError, TypeError):
                    pass

            bio_match = re.search(r'"(?:biography|bio)"\s*:\s*("(?:\\.|[^"\\])*")', pagina)
            if bio_match:
                try:
                    bio_json = json.loads(bio_match.group(1))
                    if isinstance(bio_json, str) and bio_json.strip():
                        bio = bio_json.strip()
                except (ValueError, TypeError):
                    pass

            seguidores_match = (
                re.search(r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)', pagina)
                or re.search(r'"(?:follower_count|followers_count)"\s*:\s*(\d+)', pagina)
            )
            if seguidores_match:
                seguidores = f"{int(seguidores_match.group(1)):,}".replace(",", ".")
            elif og_desc:
                contagem = re.search(r'([\d.,]+)\s*(?:followers|seguidores)', og_desc, re.I)
                if contagem:
                    seguidores = contagem.group(1)

            # A descrição OpenGraph pode conter a bio ou os dados de seguidores.
            if not bio and og_desc:
                descricao = re.sub(r'^\s*[\d.,]+\s*(?:followers|seguidores)[^·|]*[·|]\s*', '', og_desc, flags=re.I)
                descricao = re.sub(r'\s*[-|]\s*Instagram photos and videos.*$', '', descricao, flags=re.I)
                if descricao.strip() and descricao.strip().lower() != og_desc.strip().lower():
                    bio = descricao.strip()

            # Aceita somente imagens HTTPS dos domínios oficiais de mídia da Meta.
            if foto:
                foto = html.unescape(str(foto)).replace("\\u0026", "&").replace("\\/", "/")
                foto_host = (urlparse(foto).hostname or "").lower()
                if urlparse(foto).scheme != "https" or not (
                    foto_host.endswith(".cdninstagram.com")
                    or foto_host.endswith(".fbcdn.net")
                    or foto_host == "instagram.com"
                    or foto_host.endswith(".instagram.com")
                ):
                    foto = ""

            if not foto and not bio and not seguidores:
                aviso = "O Instagram não liberou os dados deste perfil nesta consulta gratuita. Você ainda pode abrir o perfil pelo botão abaixo."
        else:
            if not (foto or bio or seguidores):
                aviso = f"O Instagram limitou a consulta (HTTP {resposta.status_code}) nas duas fontes gratuitas. O link do perfil continua disponível."
    except requests.RequestException:
        aviso = "Não foi possível consultar os dados agora. O link do perfil continua disponível."

    return {
        "usuario": usuario,
        "titulo": titulo,
        "url": url,
        "foto": foto,
        "bio": bio,
        "seguidores": seguidores,
        "aviso": aviso
    }

HTML = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="application-name" content="MEDIABOX"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/brand/bluevision-mark.svg" type="image/svg+xml">
<meta name="theme-color" content="#080b14"><meta name="description" content="MEDIABOX — downloads e ferramentas de vídeo em um só lugar.">
<title>MEDIABOX — Video Studio</title><style>
:root{color-scheme:dark;--bg:#080b14;--panel:#101625;--panel2:#0c1220;--line:#263148;--text:#f4f7ff;--muted:#96a4bd;--cyan:#63e6e2;--blue:#6e8cff;--pink:#b18aff;--danger:#ff8294}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -15%,#1d2c50 0,transparent 48%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;line-height:1.5}
main{width:min(920px,92%);margin:0 auto;padding:26px 0 22px}.topline{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:38px}.brand{display:flex;align-items:center;gap:12px}.brand-mark{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,var(--cyan),var(--blue) 62%,var(--pink));color:#07101b;font-weight:950;font-size:16px;letter-spacing:-1px;box-shadow:0 8px 30px #63e6e222}.brand-name{font-size:18px;font-weight:850;letter-spacing:1.4px}.brand-sub{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase}.pill{border:1px solid #2a3c52;border-radius:999px;padding:7px 11px;color:#b9c8df;font-size:11px;letter-spacing:.7px;text-transform:uppercase;background:#0d1421}
.hero{position:relative;overflow:hidden;text-align:center;padding:31px 20px 34px;margin-bottom:22px;border:1px solid #26354d;border-radius:26px;background:linear-gradient(135deg,#141e32e8,#101523f2);box-shadow:0 20px 70px #0003}.hero:before{content:"";position:absolute;width:240px;height:240px;right:-110px;top:-140px;border-radius:50%;background:#6e8cff25;filter:blur(8px)}.eyebrow{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:3px;text-transform:uppercase}.hero h1{font-size:clamp(36px,9vw,62px);line-height:1.05;letter-spacing:-2px;margin:13px 0 12px;font-weight:900}.hero h1 span{background:linear-gradient(100deg,var(--cyan),#a3b5ff 55%,var(--pink));-webkit-background-clip:text;background-clip:text;color:transparent}.hero p{max-width:470px;margin:0 auto;color:#aab7ce;font-size:14px}.hero-tags{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:22px}.hero-tags span{font-size:11px;color:#d1dcf0;background:#0c1321;border:1px solid #2a3850;padding:6px 10px;border-radius:999px}
.card{background:linear-gradient(155deg,#121a2a,#0e1421);border:1px solid var(--line);border-radius:21px;padding:22px;margin:15px 0;box-shadow:0 12px 36px #0002}.section-head{display:flex;align-items:flex-start;gap:12px;margin-bottom:19px}.section-icon{flex:0 0 40px;height:40px;border:1px solid #34435c;border-radius:13px;background:linear-gradient(145deg,#1a2b3d,#171d31);display:grid;place-items:center;color:var(--cyan);font-size:17px;font-weight:800}.section-head h2{font-size:17px;margin:0 0 3px;letter-spacing:-.2px}.section-head p{margin:0;color:var(--muted);font-size:12px}
label{display:block;color:#bdc9dc;font-size:12px;font-weight:650;margin:0 0 8px}input,select,button{width:100%;padding:14px;border-radius:12px;border:1px solid #2b3850;background:#090f1b;color:var(--text);font:inherit;outline:none;transition:border-color .18s,box-shadow .18s,transform .18s}input::placeholder{color:#62718b}input:focus,select:focus{border-color:#6e8cff;box-shadow:0 0 0 3px #6e8cff20}.field{margin-bottom:15px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}.primary{background:linear-gradient(105deg,#62dedc,#83a0ff 65%,#ad8bff);border:0;color:#08101c;font-weight:850;cursor:pointer;box-shadow:0 8px 25px #728eff22}.primary:hover{filter:brightness(1.07);transform:translateY(-1px)}button:disabled{opacity:.55;cursor:wait;transform:none}.secondary{background:#121b2a;border:1px solid #34435c;color:#dce7f9;font-weight:700;cursor:pointer}.secondary:hover{border-color:#6e8cff}.status{color:#c3cee0;margin-top:14px;overflow-wrap:anywhere;font-size:13px}.bar{height:7px;background:#252f43;border-radius:99px;overflow:hidden;margin-top:10px}.fill{height:100%;width:0;background:linear-gradient(90deg,var(--cyan),var(--blue),var(--pink));border-radius:99px;transition:width .25s}.error{color:var(--danger);white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;margin-top:9px}.results{display:grid;gap:10px;margin-top:13px}.item{background:#0a111e;border:1px solid #28354b;border-radius:14px;padding:14px;overflow-wrap:anywhere}.item a{color:var(--cyan);font-size:13px;text-decoration:none}.item a:hover{text-decoration:underline}.item button{margin-top:10px}.small{font-size:11px;color:#8292ad;line-height:1.7}.path-note{margin:13px 0 0;padding:11px 13px;border:1px solid #243149;border-radius:12px;background:#0b111d}.path-note strong{color:#dce7fa;font-weight:650}.footer{border-top:1px solid #202a3c;margin-top:28px;padding:20px 8px 6px;text-align:center;color:#8290a9;font-size:11px}.footer strong{color:#c8d4e8;font-weight:700}.legal{max-width:600px;margin:8px auto 0}.muted{color:var(--muted)}
@media(max-width:560px){main{width:92%;padding-top:17px}.topline{margin-bottom:24px}.brand-mark{width:40px;height:40px}.hero{padding:28px 16px 30px;border-radius:21px}.hero h1{letter-spacing:-1.5px}.card{padding:17px;border-radius:18px}.grid{grid-template-columns:1fr;gap:0}.pill{font-size:9px;padding:6px 8px}}

/* MEDIABOX UI refresh */
:root{--bg:#070a12;--panel:#111a2b;--line:#293954;--cyan:#70f0e6;--blue:#7b9cff;--pink:#c09aff}
body{background:radial-gradient(ellipse at 10% 0%,#183b54 0,transparent 36%),radial-gradient(ellipse at 95% 12%,#35205a 0,transparent 30%),var(--bg);background-attachment:fixed}
main{width:min(980px,94%);padding-top:24px}
.topline{position:relative;margin-bottom:28px;padding:12px 14px;border:1px solid #2a3951;border-radius:18px;background:#0b1220d9;backdrop-filter:blur(14px)}
.brand-mark{width:48px;height:48px;border-radius:16px;object-fit:cover;box-shadow:0 0 0 5px #70f0e60c,0 10px 32px #70f0e622}
.brand-name{font-size:19px;letter-spacing:1.8px}
.hero{padding:clamp(28px,6vw,54px) 22px;margin-bottom:24px;border-radius:30px;background:linear-gradient(135deg,#17263beF,#101524f7);box-shadow:0 24px 80px #0005}
.hero h1{font-size:clamp(42px,10vw,76px);letter-spacing:-2.5px}
.hero p{font-size:15px;line-height:1.8}
.hero-tags span{padding:7px 12px;border-radius:999px}
.card{position:relative;border-radius:24px;padding:clamp(17px,4vw,26px);margin:18px 0;background:linear-gradient(145deg,#121d30f5,#0c1321f5);box-shadow:0 16px 45px #0003;transition:border-color .2s,transform .2s}
.card:hover{border-color:#3a5274}
.section-icon{flex-basis:44px;height:44px;border-radius:15px;font-size:19px}
.section-head h2{font-size:18px}
.section-head p{font-size:13px;line-height:1.6}
input,select,button{min-height:48px;border-radius:14px}
input,select{background:#080e19;border-color:#30415e}
input:focus,select:focus{border-color:var(--cyan);box-shadow:0 0 0 4px #70f0e615}
.primary{background:linear-gradient(110deg,#70f0e6,#8aa8ff 60%,#c09aff);box-shadow:0 10px 28px #7b9cff25}
.secondary{background:linear-gradient(145deg,#17243a,#101827);border-color:#344763}
.status{padding:10px 12px;border:1px solid #25344b;border-radius:12px;background:#080e19;font-variant-numeric:tabular-nums}
.bar{height:9px;background:#202c40}
.item{border-radius:16px;border-color:#30405a;background:#0a1220}
.item button{min-height:42px}
.path-note{border-radius:14px}
.footer{padding-top:24px}
.player-frame{width:100%;aspect-ratio:16/9;border:1px solid #344763;border-radius:17px;background:#050810;display:block}
.player-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.player-help{margin-top:10px;padding:12px 14px;border:1px solid #283951;border-radius:13px;background:#0a1220;color:#aab9d0;font-size:12px;line-height:1.7}
@media(max-width:560px){main{width:93%;padding-top:12px}.topline{margin-bottom:18px;padding:10px}.hero{border-radius:23px}.hero h1{letter-spacing:-1.5px}.card{border-radius:20px}.player-actions{grid-template-columns:1fr}}

.instagram-profile{display:grid;grid-template-columns:92px minmax(0,1fr);gap:16px;align-items:center;padding:18px;border:1px solid #344763;border-radius:18px;background:linear-gradient(145deg,#111e32,#0a1220);overflow:hidden}
.instagram-avatar{width:92px;height:92px;border-radius:50%;object-fit:cover;border:3px solid transparent;background:linear-gradient(#101827,#101827) padding-box,linear-gradient(135deg,#70f0e6,#7b9cff,#c09aff) border-box;box-shadow:0 8px 28px #0005}
.instagram-avatar-fallback{width:92px;height:92px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(135deg,#183b54,#35205a);color:#70f0e6;font-size:30px;font-weight:900;border:3px solid #526b9a}
.instagram-name{font-size:17px;font-weight:850;overflow-wrap:anywhere}.instagram-handle{color:#9eacc4;font-size:12px;margin-top:2px}
.instagram-stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:11px}.instagram-stat{padding:7px 10px;border:1px solid #30415a;border-radius:10px;background:#080e19;color:#dce7f9;font-size:12px}
.instagram-bio{grid-column:1/-1;padding-top:12px;border-top:1px solid #293954;color:#d2dced;font-size:13px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}
.instagram-profile-actions{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:9px}
.instagram-warning{grid-column:1/-1;margin-top:4px;padding:13px 14px;border:1px solid #80652f;border-radius:13px;background:linear-gradient(135deg,#302719,#211b19);color:#f4d99b;font-size:12px;line-height:1.65}.instagram-profile-actions a{min-height:46px}
@media(max-width:420px){.instagram-profile{grid-template-columns:70px minmax(0,1fr);gap:12px;padding:14px}.instagram-avatar,.instagram-avatar-fallback{width:70px;height:70px}.instagram-name{font-size:15px}.instagram-profile-actions{grid-template-columns:1fr}}



/* Efeitos de diagnóstico: suaves, diferentes e legíveis */
#error-intro[hidden]{display:none}
#error-intro{position:fixed;inset:0;z-index:100000;display:grid;place-items:center;padding:18px;background:rgba(3,6,16,.94);backdrop-filter:blur(14px);animation:introFade .35s ease-out}
#error-intro .intro-card{position:relative;width:min(440px,100%);min-height:320px;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:28px 22px;border:1px solid #6175a8;border-radius:26px;background:radial-gradient(ellipse at 50% 0%,#28466a 0,transparent 55%),linear-gradient(145deg,#121d30,#090f1d);box-shadow:0 28px 100px #000c;text-align:center;color:#f4f7ff}
#error-intro .intro-kicker{font-size:10px;font-weight:900;letter-spacing:2.2px;color:#8ceeff}
#error-intro .intro-icon{display:none}
#error-intro h2{margin:8px 0;font-size:clamp(22px,6vw,29px);background:linear-gradient(100deg,#8defff,#a7b4ff 58%,#d2a7ff);background-clip:text;-webkit-background-clip:text;color:transparent}
#error-intro p{max-width:330px;margin:0;color:#c4d0e5;font-size:13px;line-height:1.6}
#error-intro .intro-effect{position:relative;width:150px;height:126px;margin:12px auto 8px;display:grid;place-items:center;color:#a7efff}
#error-intro .intro-effect:before,#error-intro .intro-effect:after{content:"";position:absolute;inset:22px;border:1px solid #70dff0;border-radius:28px;box-shadow:0 0 24px #70dff033,inset 0 0 22px #70dff018}
#error-intro .intro-effect:after{inset:34px;border-color:#a99aff;border-radius:50%}
#error-intro .intro-effect span{font-size:42px;font-weight:950;letter-spacing:-2px;text-shadow:0 0 22px #70dff055}
#error-intro .intro-dots{display:flex;gap:7px;margin-top:12px}
#error-intro .intro-dots i{width:6px;height:6px;border-radius:50%;background:#8ceeff;opacity:.45;animation:introDot 1.5s ease-in-out infinite}
#error-intro .intro-dots i:nth-child(2){animation-delay:.18s}
#error-intro .intro-dots i:nth-child(3){animation-delay:.36s}
#error-intro .intro-card{width:min(540px,100%);max-height:91vh;overflow:auto}
#error-intro .intro-progress{width:min(390px,100%);height:7px;margin:13px auto 7px;overflow:hidden;border:1px solid #425b7a;border-radius:99px;background:#08111f}
#error-intro .intro-progress-fill{width:0;height:100%;border-radius:99px;background:linear-gradient(90deg,#70f0e6,#7b9cff,#c09aff);box-shadow:0 0 15px #70f0e677;transition:width .24s ease}
#error-intro .intro-progress-label{margin:0 0 8px;color:#93a9c9;font-size:10px;font-weight:800;letter-spacing:.7px}
#error-intro .intro-steps{display:grid;gap:6px;width:min(440px,100%);max-height:155px;overflow:auto;text-align:left;scrollbar-width:thin;scrollbar-color:#526a91 #101a2b}
#error-intro .intro-step{display:grid;grid-template-columns:22px minmax(0,1fr);gap:8px;align-items:center;padding:7px 9px;border:1px solid #2c405d;border-radius:10px;background:#080f1d}
#error-intro .intro-step-state{width:20px;height:20px;display:grid;place-items:center;border-radius:50%;font-size:11px;font-weight:900}
#error-intro .intro-step.is-ok .intro-step-state{background:#103b35;color:#75f4d1}
#error-intro .intro-step.is-atencao .intro-step-state{background:#493719;color:#ffd58c}
#error-intro .intro-step.is-falha .intro-step-state{background:#4a202c;color:#ff9eaf}
#error-intro .intro-step strong{display:block;color:#e6efff;font-size:10px}
#error-intro .intro-step small{display:block;margin-top:1px;color:#9aabc5;font-size:9px;line-height:1.4;overflow-wrap:anywhere}
#error-center .error-panel{position:relative;isolation:isolate;transform-origin:center;animation:mbPanelIn .42s cubic-bezier(.2,.8,.2,1)}
#error-center .error-diagnosis-summary{margin:9px auto 12px;padding:10px 12px;border:1px solid #36516b;border-radius:12px;background:#0a1725;color:#bcebf2;font-size:11px;line-height:1.55;text-align:center}
#error-center .error-suggestion{margin:10px 0;padding:11px 13px;border:1px solid #3b4770;border-radius:13px;background:linear-gradient(120deg,#111d33,#17152d)}
#error-center .error-suggestion strong{display:block;margin-bottom:4px;color:#a7c9ff;font-size:9px;letter-spacing:1.2px}
#error-center .error-suggestion p{margin:0;color:#d3def4;font-size:11px;line-height:1.5}
#error-center .error-actions{grid-template-columns:repeat(3,minmax(0,1fr))}
#error-center .error-history[hidden]{display:none!important}
#error-center .error-history{margin-top:12px;padding:12px;border:1px solid #344765;border-radius:14px;background:#080f1d}
#error-center .error-history-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;color:#bfefff;font-size:11px;font-weight:850}
#error-center .error-history-head button{width:auto;min-height:30px;padding:5px 8px;margin:0;font-size:10px}
#error-center .error-history-list{display:grid;gap:7px;max-height:180px;overflow:auto}
#error-center .error-history-item{padding:9px;border:1px solid #2b3c57;border-radius:10px;background:#0d1727}
#error-center .error-history-item strong{display:block;color:#e4edff;font-size:11px}
#error-center .error-history-item small{display:block;margin-top:3px;color:#93a4c0;font-size:9px;line-height:1.5}
#error-center .error-history-empty{margin:0;color:#94a5bf;font-size:11px}
@keyframes mbScannerSpin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes mbScannerSweep{0%,100%{opacity:.25;filter:brightness(.8)}50%{opacity:1;filter:brightness(1.45)}}
body[data-error-animation="scanner"] #error-intro .intro-effect:before{inset:9px;border-radius:50%;border-color:#70f0e6aa;background:conic-gradient(from 0deg,transparent 0 62%,#70f0e699 78%,transparent 92%);animation:mbScannerSpin 2.6s linear infinite}
body[data-error-animation="scanner"] #error-intro .intro-effect:after{inset:28px;border-radius:50%;border-style:dashed;animation:mbScannerSpin 7s linear infinite reverse}
body[data-error-animation="scanner"] #error-intro .intro-effect span{position:relative;z-index:1;color:#d8ffff;text-shadow:0 0 18px #70f0e6aa;animation:mbScannerSweep 1.8s ease-in-out infinite}
body[data-error-animation="scanner"] #error-center .error-panel{box-shadow:0 26px 90px #000b,0 0 36px #70f0e633}
@keyframes introFade{from{opacity:0}to{opacity:1}}
@keyframes mbPanelIn{from{opacity:0;transform:translateY(15px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes mbAuroraFlow{0%,100%{transform:translate(-5px,2px) scale(.96);filter:hue-rotate(0)}50%{transform:translate(5px,-3px) scale(1.06);filter:hue-rotate(18deg)}}
@keyframes mbRippleRing{0%{transform:scale(.72);opacity:.8}100%{transform:scale(1.48);opacity:0}}
@keyframes mbShimmerSweep{0%{left:-70%;opacity:0}18%{opacity:.35}65%{opacity:.16}100%{left:125%;opacity:0}}
@keyframes mbOrbitSoft{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes introDot{0%,80%,100%{transform:translateY(0);opacity:.35}40%{transform:translateY(-4px);opacity:1}}
body[data-error-animation="aurora"] #error-intro .intro-effect:before{background:linear-gradient(135deg,#70f0e622,#7b9cff22,#c09aff22);animation:mbAuroraFlow 4s ease-in-out infinite}
body[data-error-animation="aurora"] #error-center .error-panel{box-shadow:0 26px 90px #000b,0 0 34px #70dff01b}
body[data-error-animation="ripple"] #error-center .error-symbol{position:relative}
body[data-error-animation="ripple"] #error-center .error-symbol:before,body[data-error-animation="ripple"] #error-center .error-symbol:after{content:"";position:absolute;inset:-5px;border:1px solid #75eaff;border-radius:23px;animation:mbRippleRing 1.9s ease-out infinite;pointer-events:none}
body[data-error-animation="ripple"] #error-center .error-symbol:after{animation-delay:.95s}
body[data-error-animation="ripple"] #error-intro .intro-effect:before{border-radius:50%;animation:mbRippleRing 2s ease-out infinite}
body[data-error-animation="shimmer"] #error-center .error-panel:after{content:"";position:absolute;top:-15%;bottom:-15%;left:-70%;width:38%;transform:skewX(-18deg);background:linear-gradient(90deg,transparent,#b8f5ff24,transparent);animation:mbShimmerSweep 2.8s ease-in-out infinite;pointer-events:none}
body[data-error-animation="shimmer"] #error-intro .intro-card:after{content:"";position:absolute;inset:0;background:linear-gradient(115deg,transparent 35%,#b8f5ff15 48%,transparent 62%);transform:translateX(-100%);animation:mbShimmerSweep 2.8s ease-in-out infinite;pointer-events:none}
body[data-error-animation="orbit"] #error-center .error-symbol{position:relative}
body[data-error-animation="orbit"] #error-center .error-symbol:before{content:"";position:absolute;inset:-7px;border:1px solid #9d9aff;border-radius:50%;transform:rotate(-25deg) scaleX(.72);animation:mbOrbitSoft 5s linear infinite;pointer-events:none}
body[data-error-animation="orbit"] #error-center .error-symbol:after{content:"";position:absolute;inset:-12px;border:1px dashed #70dff088;border-radius:50%;transform:rotate(35deg) scaleX(.82);animation:mbOrbitSoft 8s linear infinite reverse;pointer-events:none}
body[data-error-animation="orbit"] #error-intro .intro-effect:after{border-style:dashed;animation:mbOrbitSoft 7s linear infinite}
body[data-error-animation="minimal"] #error-center .error-panel{animation:mbPanelIn .3s ease-out both;box-shadow:0 24px 75px #0009}
body[data-error-animation="minimal"] #error-intro .intro-effect:before,body[data-error-animation="minimal"] #error-intro .intro-effect:after{animation:none;box-shadow:none}
@media(prefers-reduced-motion:reduce){ #error-intro,#error-intro *,#error-center,#error-center *{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important}}
 
#error-center[hidden]{display:none}
#error-center{position:fixed;inset:0;z-index:99999;display:grid;place-items:center;padding:16px;background:rgba(3,7,18,.88);backdrop-filter:blur(10px);animation:errorFade .18s ease-out}
#error-center .error-panel{width:min(520px,100%);max-height:92vh;overflow:auto;padding:22px;border:1px solid #53658d;border-radius:25px;background:radial-gradient(ellipse at 10% 0%,#203b58 0,transparent 42%),radial-gradient(ellipse at 100% 0%,#39265c 0,transparent 38%),linear-gradient(145deg,#121d30,#090f1d);box-shadow:0 26px 90px #000b,0 0 45px #5d79ff20;color:#f4f7ff}
#error-center .error-top{display:flex;align-items:center;justify-content:space-between;gap:10px}
#error-center .error-brand{font-size:10px;font-weight:900;letter-spacing:2px;color:#8ceeff}
#error-center .error-status{padding:6px 9px;border:1px solid #d5a45a66;border-radius:99px;background:#d5a45a16;color:#ffd58c;font-size:10px;font-weight:850;letter-spacing:1px}
#error-center .error-symbol{width:62px;height:62px;margin:22px auto 12px;display:grid;place-items:center;border:1px solid #8aa7ff66;border-radius:20px;background:linear-gradient(145deg,#233d61,#30264e);font-size:30px;box-shadow:0 10px 35px #0005}
#error-center h2{margin:8px 0;text-align:center;font-size:clamp(21px,5vw,27px);line-height:1.2;background:linear-gradient(100deg,#8defff,#a7b4ff 58%,#d2a7ff);background-clip:text;-webkit-background-clip:text;color:transparent}
#error-center .error-summary{margin:0 auto 18px;max-width:420px;text-align:center;color:#c4d0e5;font-size:13px;line-height:1.65;overflow-wrap:anywhere}
#error-center .error-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
#error-center .error-fact{min-width:0;padding:11px;border:1px solid #344765;border-radius:13px;background:#0a1323}
#error-center .error-fact span{display:block;margin-bottom:5px;color:#8e9fbc;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase}
#error-center .error-fact strong{display:block;color:#e4edff;font-size:12px;overflow-wrap:anywhere}
#error-center details{margin-top:12px;border:1px solid #344765;border-radius:14px;background:#080f1d}
#error-center summary{padding:13px;color:#9fe9ff;font-size:12px;font-weight:850;cursor:pointer;list-style:none}
#error-center summary::-webkit-details-marker{display:none}
#error-center summary:after{content:"＋";float:right;color:#9fe9ff}
#error-center details[open] summary:after{content:"−"}
#error-center .error-technical{margin:0 12px 12px;padding:12px;border-radius:10px;background:#050a13;color:#c1cde0;font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto}
#error-center .error-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin-top:15px}
#error-center .error-actions button{min-height:46px;margin:0}
#error-center .error-copy{border:1px solid #4a5d80;background:#17243a;color:#e5edff}
@keyframes errorFade{from{opacity:0}to{opacity:1}}
@media(max-width:420px){ #error-center{padding:10px}#error-center .error-panel{padding:17px;border-radius:20px}#error-center .error-facts{gap:7px}#error-center .error-fact{padding:9px}#error-center .error-actions{grid-template-columns:1fr}}

 .preview-backdrop[hidden]{display:none}.preview-backdrop{position:fixed;inset:0;z-index:9998;background:#02040bdc;display:grid;place-items:center;padding:18px}.preview-dialog{width:min(460px,100%);padding:18px;border:1px solid #344763;border-radius:20px;background:linear-gradient(145deg,#121d30,#0c1321);box-shadow:0 24px 80px #0009}.preview-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.preview-close{width:42px;min-height:40px;padding:8px}.preview-video{position:relative;aspect-ratio:9/16;width:min(100%,320px);max-height:54vh;margin:14px auto;background:#05070c;border:1px solid #40516e;border-radius:13px;overflow:hidden}.preview-video video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}.preview-logo{position:absolute;z-index:3;width:38%;height:auto;max-width:240px;clip-path:inset(8% 8% 8% 8%);opacity:.96;filter:drop-shadow(0 1px 5px #000c);left:88%;top:88%;transform:translate(-50%,-50%);touch-action:none;cursor:grab;user-select:none;-webkit-user-drag:none}.preview-logo:active{cursor:grabbing}.preview-video{touch-action:none;cursor:crosshair;user-select:none;-webkit-user-select:none}.preview-video:after{pointer-events:none}.preview-mark{position:absolute;z-index:1;display:flex;flex-direction:column;line-height:1.02;text-align:left;filter:drop-shadow(0 1px 4px #000b);opacity:.86;font-size:10px}.preview-mark small{color:#fff;font-size:.62em;font-style:italic;font-weight:800}.preview-mark b{background:linear-gradient(100deg,#13cfff,#6975ff 55%,#c044ff);background-clip:text;-webkit-background-clip:text;color:transparent;font-size:1.12em;font-weight:950;letter-spacing:.4px}.pos-superior_esquerdo .preview-mark{top:12px;left:12px}.pos-superior_direito .preview-mark{top:12px;right:12px;text-align:right}.pos-inferior_esquerdo .preview-mark{bottom:12px;left:12px}.pos-inferior_direito .preview-mark{bottom:12px;right:12px;text-align:right}

/* Editor secreto local: não persiste e não envia alterações */
#mb-local-editor[hidden]{display:none}
#mb-local-editor{position:fixed;inset:0;z-index:200000;background:#030713ed;backdrop-filter:blur(12px);display:grid;place-items:center;padding:12px}
#mb-local-editor .mb-editor-panel{width:min(760px,100%);max-height:94vh;overflow:auto;padding:18px;border:1px solid #52698e;border-radius:20px;background:linear-gradient(145deg,#121d30,#080e19);box-shadow:0 24px 90px #000b}
#mb-local-editor h2{margin:0 0 5px;color:#dceaff;font-size:19px}
#mb-local-editor p{margin:0 0 14px;color:#9eacc4;font-size:12px;line-height:1.55}
#mb-local-editor label{margin:12px 0 6px}
#mb-local-editor input,#mb-local-editor textarea{display:block;width:100%;min-height:42px;padding:11px;border:1px solid #344763;border-radius:11px;background:#070d18;color:#f4f7ff;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}
#mb-local-editor textarea{min-height:130px;resize:vertical}
#mb-local-editor .mb-editor-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
#mb-local-editor .mb-editor-actions button{flex:1;min-width:120px}
#mb-local-editor .mb-editor-status{margin-top:10px;color:#9fe9c2;font-size:12px;min-height:18px}
#mb-local-editor .mb-editor-close{background:#17243a;border:1px solid #465a7b;color:#e5edff}
#mb-local-editor .mb-auto-run{margin:16px 0;padding:14px;border:1px solid #344763;border-radius:15px;background:linear-gradient(145deg,#0d1728,#090f1b)}
#mb-local-editor .mb-auto-run-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
#mb-local-editor .mb-auto-run-head strong{color:#70f0e6;font-size:12px;letter-spacing:1px}
#mb-local-editor .mb-auto-run-head span{color:#96a4bd;font-size:9px;letter-spacing:1px}
#mb-local-editor .mb-visual-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
#mb-local-editor .mb-visual-grid button{width:100%;min-width:0;padding:11px 8px;border:1px solid #344763;border-radius:11px;background:#17243a;color:#e4edff;font-size:10px;font-weight:850;letter-spacing:.35px}
#mb-local-editor .mb-visual-grid button.is-active{border-color:#70f0e6;background:#12313a;color:#9dfff2;box-shadow:0 0 0 2px #70f0e622,0 0 18px #00fff233}
#mb-local-editor .mb-auto-run-note{margin:10px 0 0;color:#96a4bd;font-size:11px;line-height:1.5}
@media(max-width:390px){ #mb-local-editor .mb-visual-grid{grid-template-columns:1fr}}


/* Configurações flutuantes: não navega, não recarrega e preserva o formulário */
#mb-settings[hidden],#mb-birthday[hidden],#mb-command-surprise[hidden]{display:none!important}
#mb-settings{position:fixed;inset:0;z-index:250000;display:grid;place-items:center;padding:14px;background:rgba(3,7,18,.76);backdrop-filter:blur(13px);-webkit-backdrop-filter:blur(13px);animation:mbSettingsFade .18s ease-out}
#mb-settings .mb-settings-dialog{width:min(700px,100%);max-height:91vh;overflow:auto;overscroll-behavior:contain;padding:20px;border:1px solid #3a4e70;border-radius:25px;background:radial-gradient(ellipse at 8% 0%,#203a58 0,transparent 43%),linear-gradient(145deg,#121d30,#090f1d);box-shadow:0 30px 110px #000c;color:#f4f7ff}
#mb-settings .mb-settings-head{position:sticky;top:-20px;z-index:3;display:flex;align-items:center;justify-content:space-between;gap:12px;margin:-2px -2px 16px;padding:3px 2px 14px;background:linear-gradient(180deg,#121d30 78%,#121d3000)}
#mb-settings .mb-settings-brand{display:flex;align-items:center;gap:12px;min-width:0}
#mb-settings .mb-settings-mark{width:43px;height:43px;flex:0 0 43px;display:grid;place-items:center;border-radius:14px;background:linear-gradient(135deg,#70f0e6,#8aa8ff 60%,#c09aff);color:#08101c;font-size:14px;font-weight:950;box-shadow:0 8px 24px #70f0e622}
#mb-settings .mb-settings-head small{display:block;color:#70f0e6;font-size:9px;font-weight:900;letter-spacing:1.8px}
#mb-settings .mb-settings-head h2{margin:2px 0 0;color:#f4f7ff;font-size:23px;letter-spacing:-.5px}
#mb-settings .mb-settings-head p{margin:1px 0 0;color:#96a4bd;font-size:11px}
#mb-settings .mb-settings-close{width:42px;min-height:42px;padding:6px;border:1px solid #405475;border-radius:13px;background:#17243a;color:#fff;font-size:22px;line-height:1;cursor:pointer}
#mb-settings .mb-settings-tabs{display:flex;gap:8px;padding:5px;margin-bottom:16px;border:1px solid #2c3d58;border-radius:15px;background:#080f1d}
#mb-settings .mb-settings-tabs button{flex:1;width:auto;min-height:43px;margin:0;padding:10px 12px;border:1px solid transparent;border-radius:11px;background:transparent;color:#aab8ce;font-size:12px;font-weight:850;cursor:pointer}
#mb-settings .mb-settings-tabs button[aria-selected="true"]{border-color:#45617d;background:linear-gradient(110deg,#173444,#202344);color:#c4fff8;box-shadow:inset 0 0 18px #70f0e60b}
#mb-settings .mb-settings-copy{margin:0 0 14px;color:#aab7ce;font-size:12px;line-height:1.65}
#mb-settings .mb-settings-kicker{display:block;margin:0 0 7px;color:#70f0e6;font-size:9px;font-weight:900;letter-spacing:1.5px}
#mb-settings .mb-error-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
#mb-settings .mb-error-option{display:grid;grid-template-columns:1fr auto;gap:8px 10px;align-items:center;width:100%;min-width:0;margin:0;padding:11px;text-align:left;border:1px solid #30435f;border-radius:16px;background:linear-gradient(145deg,#0d1728,#0a1220);color:#f4f7ff;cursor:pointer;transition:border-color .18s,background .18s,transform .18s}
#mb-settings .mb-error-option:hover{transform:translateY(-1px);border-color:#58769b}
#mb-settings .mb-error-option[aria-pressed="true"]{border-color:#70dfe0;background:linear-gradient(145deg,#102a37,#111b30);box-shadow:0 0 0 1px #70dfe033,0 9px 25px #0003}
#mb-settings .mb-error-swatch{position:relative;grid-column:1/-1;display:block;height:52px;overflow:hidden;border:1px solid #344c6c;border-radius:11px;background:#101a2b}
#mb-settings .mb-error-swatch:before,#mb-settings .mb-error-swatch:after{content:"";position:absolute;pointer-events:none}
#mb-settings .mb-error-option[data-error-choice="aurora"] .mb-error-swatch{background:radial-gradient(ellipse at 20% 80%,#23aeb455,transparent 48%),radial-gradient(ellipse at 85% 12%,#8a6bce66,transparent 48%),#0c1729}
#mb-settings .mb-error-option[data-error-choice="aurora"] .mb-error-swatch:before{inset:-30%;background:linear-gradient(110deg,transparent,#70f0e633,#a98aff44,transparent);animation:mbAuroraFlow 4s ease-in-out infinite}
#mb-settings .mb-error-option[data-error-choice="ripple"] .mb-error-swatch{background:radial-gradient(circle at 50% 50%,#284b69,#10182b 65%)}
#mb-settings .mb-error-option[data-error-choice="ripple"] .mb-error-swatch:before,#mb-settings .mb-error-option[data-error-choice="ripple"] .mb-error-swatch:after{width:42px;height:42px;left:calc(50% - 21px);top:4px;border:1px solid #8ceeff;border-radius:50%;animation:mbRippleRing 2s ease-out infinite}
#mb-settings .mb-error-option[data-error-choice="ripple"] .mb-error-swatch:after{animation-delay:1s}
#mb-settings .mb-error-option[data-error-choice="shimmer"] .mb-error-swatch{background:linear-gradient(125deg,#111b2f,#263b58 50%,#11182a)}
#mb-settings .mb-error-option[data-error-choice="shimmer"] .mb-error-swatch:before{top:-20%;bottom:-20%;left:-60%;width:42%;transform:skewX(-18deg);background:linear-gradient(90deg,transparent,#d4faff66,transparent);animation:mbShimmerSweep 2.8s ease-in-out infinite}
#mb-settings .mb-error-option[data-error-choice="orbit"] .mb-error-swatch{background:radial-gradient(circle at 50% 50%,#263b61,#0b1424 70%)}
#mb-settings .mb-error-option[data-error-choice="orbit"] .mb-error-swatch:before{width:38px;height:38px;left:calc(50% - 19px);top:6px;border:1px solid #a99aff;border-radius:50%;transform:scaleX(.65);animation:mbOrbitSoft 5s linear infinite}
#mb-settings .mb-error-option[data-error-choice="orbit"] .mb-error-swatch:after{width:46px;height:46px;left:calc(50% - 23px);top:2px;border:1px dashed #70f0e6aa;border-radius:50%;animation:mbOrbitSoft 8s linear infinite reverse}
#mb-settings .mb-error-option[data-error-choice="minimal"] .mb-error-swatch{background:linear-gradient(115deg,#1b2a42,#111a2a 55%,#263a55)}
#mb-settings .mb-error-option[data-error-choice="scanner"] .mb-error-swatch{background:radial-gradient(circle at 50% 50%,#193c54,#0a1220 72%);border-color:#70f0e688}
#mb-settings .mb-error-option[data-error-choice="scanner"] .mb-error-swatch:before{inset:4px;border:1px solid #70f0e6aa;border-radius:50%;background:conic-gradient(transparent,#70f0e688,transparent 70%);animation:mbScannerSpin 2.6s linear infinite}
#mb-settings .mb-error-option[data-error-choice="scanner"] .mb-error-swatch:after{width:34px;height:34px;left:calc(50% - 17px);top:8px;border:1px dashed #b19aff;border-radius:50%;animation:mbScannerSpin 5s linear infinite reverse}
#mb-settings .mb-error-option strong{display:block;font-size:12px;font-weight:850}
#mb-settings .mb-error-option small{display:block;margin-top:3px;color:#94a5bf;font-size:10px;line-height:1.45}
#mb-settings .mb-option-check{width:19px;height:19px;display:grid;place-items:center;border:1px solid #526783;border-radius:50%;color:transparent;font-size:12px}
#mb-settings .mb-error-option[aria-pressed="true"] .mb-option-check{border-color:#70f0e6;background:#70f0e6;color:#07111d}
#mb-settings .mb-settings-actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:14px}
#mb-settings .mb-settings-actions button{flex:1;width:auto;min-width:145px;min-height:45px;margin:0;padding:11px 14px;font-size:12px}
#mb-settings .mb-settings-secondary{background:#17243a;border:1px solid #435777;color:#e5edff}
#mb-settings .mb-command-box{padding:14px;border:1px solid #30435f;border-radius:17px;background:linear-gradient(145deg,#0d1728,#090f1b)}
#mb-settings .mb-command-row{display:flex;gap:8px;align-items:stretch}
#mb-settings .mb-command-row input{min-width:0;flex:1;margin:0;background:#070d18;border-color:#354b69}
#mb-settings .mb-command-row button{width:auto;min-width:116px;margin:0;padding:10px 13px}
#mb-settings .mb-command-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:13px}
#mb-settings .mb-command-pick{display:flex;align-items:center;gap:10px;width:100%;min-width:0;margin:0;padding:10px;border:1px solid #30435f;border-radius:12px;background:#111d30;color:#e5edff;text-align:left;cursor:pointer}
#mb-settings .mb-command-pick b{flex:0 0 48px;color:#8defff;font:900 13px ui-monospace,monospace}
#mb-settings .mb-command-pick span{min-width:0;color:#b8c7dc;font-size:10px;line-height:1.35}
#mb-settings .mb-command-status{min-height:20px;margin:10px 0 0;color:#9fe9c2;font-size:11px}
#mb-settings .mb-command-hint{margin:10px 0 0;color:#8292ad;font-size:10px;line-height:1.55}
#mb-birthday,#mb-command-surprise{position:fixed;inset:0;z-index:260000;display:grid;place-items:center;padding:16px;background:rgba(3,7,18,.84);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);animation:mbSettingsFade .2s ease-out}
#mb-birthday .mb-birthday-card,#mb-command-surprise .mb-command-card{position:relative;width:min(440px,100%);max-height:90vh;overflow:auto;padding:24px 20px;border:1px solid #52698e;border-radius:24px;background:radial-gradient(ellipse at 50% 0%,#263d64 0,transparent 54%),linear-gradient(145deg,#121d30,#090f1d);box-shadow:0 28px 100px #000b;text-align:center}
#mb-birthday .mb-birthday-close,#mb-command-surprise .mb-command-close{position:absolute;top:12px;right:12px;width:36px;min-height:36px;margin:0;padding:5px;border:1px solid #425675;border-radius:11px;background:#17243a;color:white;font-size:19px}
#mb-birthday .mb-birthday-kicker,#mb-command-surprise .mb-command-kicker{color:#8ceeff;font-size:9px;font-weight:900;letter-spacing:1.7px}
#mb-birthday .mb-bear{display:block;width:150px;height:158px;margin:12px auto 4px;filter:drop-shadow(0 12px 18px #0006)}
#mb-birthday h1,#mb-command-surprise h2{margin:9px 0;color:#f4f7ff;font-size:clamp(23px,6vw,29px)}
#mb-birthday .mb-speech,#mb-command-surprise .mb-command-message{color:#d7e6fb;font-size:13px;line-height:1.7}
#mb-birthday .mb-birthday-message{color:#aebed4;font-size:12px;line-height:1.7}
#mb-birthday .mb-birthday-footer{margin-top:14px;color:#70f0e6;font-size:9px;font-weight:900;letter-spacing:1.5px}
#mb-command-surprise .mb-command-symbol{width:76px;height:76px;display:grid;place-items:center;margin:16px auto 13px;border:1px solid #7190be;border-radius:24px;background:linear-gradient(145deg,#1e3c5e,#2a2850);color:#a8f4ff;font-size:26px;font-weight:950;box-shadow:0 10px 34px #70dff022}
#mb-command-surprise[data-theme="celebration"] .mb-command-symbol{border-radius:50%;animation:mbOrbitSoft 9s linear infinite}
#mb-command-surprise[data-theme="portal"] .mb-command-symbol{border-radius:50%;box-shadow:0 0 0 9px #9e8aff15,0 0 36px #9e8aff44}
#mb-command-surprise[data-theme="future"] .mb-command-symbol{background:linear-gradient(145deg,#174653,#302b61);box-shadow:0 0 34px #70f0e633}
#mb-command-surprise .mb-command-message{max-width:330px;margin:0 auto 8px}
@keyframes mbSettingsFade{from{opacity:0}to{opacity:1}}
@keyframes mbSettingsIn{from{opacity:0;transform:translateY(12px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
body.mb-overlay-open{overflow:hidden}
@media(max-width:560px){ #mb-settings{padding:8px;align-items:end} #mb-settings .mb-settings-dialog{max-height:94vh;padding:15px;border-radius:22px 22px 17px 17px} #mb-settings .mb-settings-head h2{font-size:20px} #mb-settings .mb-error-options{grid-template-columns:1fr 1fr;gap:7px} #mb-settings .mb-error-option{padding:8px} #mb-settings .mb-error-swatch{height:43px} #mb-settings .mb-error-option strong{font-size:11px} #mb-settings .mb-error-option small{font-size:9px} #mb-settings .mb-command-row{flex-direction:column} #mb-settings .mb-command-row button{width:100%;min-height:43px} }
@media(max-width:370px){ #mb-settings .mb-error-options,#mb-settings .mb-command-grid{grid-template-columns:1fr} }

/* MEDIABOX — layout de aplicativo mobile */
:root{--bv-accent:#70f0e6;--bv-blue:#7b9cff;--bv-violet:#c09aff}
main{padding-bottom:calc(132px + env(safe-area-inset-bottom))}
.bv-pages{min-height:50vh}
.bv-page[hidden]{display:none!important}
.bv-page:not([hidden]){display:block;animation:bvViewIn .22s ease-out both}
@keyframes bvViewIn{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:translateY(0)}}
.bv-dashboard{margin:16px 0 20px;padding:clamp(18px,4vw,25px);border:1px solid #304b68;border-radius:24px;background:radial-gradient(ellipse at 100% 0%,#32487944,transparent 48%),linear-gradient(145deg,#13243a,#0b1423);box-shadow:0 18px 52px #0004}
.bv-dashboard-kicker{color:#8defff;font-size:10px;font-weight:900;letter-spacing:2.2px}
.bv-dashboard h2{margin:8px 0 5px;font-size:clamp(22px,5vw,30px);letter-spacing:-.7px}
.bv-dashboard p{margin:0;color:#aabbd3;font-size:13px;line-height:1.7}
.bv-shortcut-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:18px}
.bv-shortcut{display:flex;align-items:center;gap:11px;min-width:0;min-height:72px;padding:12px;border:1px solid #314762;border-radius:16px;background:linear-gradient(145deg,#14243a,#0c1524);color:#e9f4ff;text-align:left;cursor:pointer}
.bv-shortcut:active{transform:scale(.985)}
.bv-shortcut-icon{flex:0 0 38px;width:38px;height:38px;display:grid;place-items:center;border-radius:12px;background:linear-gradient(145deg,#193d51,#2d315d);color:#9cf5f1;font-size:18px}
.bv-shortcut strong{display:block;font-size:12px}
.bv-shortcut small{display:block;margin-top:3px;color:#92a6c2;font-size:10px;line-height:1.4}
.bv-page-title{margin:8px 2px 16px}
.bv-page-title span{display:block;color:#8defff;font-size:10px;font-weight:900;letter-spacing:2px;text-transform:uppercase}
.bv-page-title h1{margin:5px 0 0;font-size:clamp(25px,6vw,34px);letter-spacing:-.8px}
#bv-nav{position:fixed;z-index:1400;left:50%;bottom:calc(10px + env(safe-area-inset-bottom));transform:translateX(-50%);width:min(720px,calc(100% - 18px));display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:4px;padding:8px 7px;border:1px solid #3a4b67;border-radius:23px;background:rgba(9,15,27,.92);box-shadow:0 16px 55px #0009,0 0 28px #70f0e610;backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px)}
#bv-nav button{width:auto;min-width:0;min-height:61px;margin:0;padding:7px 3px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;border:1px solid transparent;border-radius:16px;background:transparent;color:#91a1ba;font-size:10px;font-weight:750;line-height:1.15;cursor:pointer;box-shadow:none}
#bv-nav button .bv-nav-icon{font-size:19px;line-height:1}
#bv-nav button[aria-current="page"]{border-color:#70f0e633;background:linear-gradient(145deg,#17374a,#202342);color:#a9fff7;box-shadow:inset 0 0 18px #70f0e612}
#bv-nav button:focus-visible,.bv-shortcut:focus-visible{outline:2px solid #8defff;outline-offset:2px}
@media(min-width:760px){main{padding-bottom:145px}.bv-page-title{margin-top:16px}#bv-nav{bottom:18px}#bv-nav button{min-height:58px}}
@media(max-width:420px){
#bv-nav{width:calc(100% - 14px);padding:6px 5px;border-radius:20px}#bv-nav button{min-height:58px;font-size:8px}.bv-shortcut-grid{gap:8px}.bv-shortcut{padding:10px;gap:8px}.bv-shortcut-icon{width:34px;height:34px;flex-basis:34px}}
@media(prefers-reduced-motion:reduce){.bv-page:not([hidden]){animation:none}.bv-shortcut:active{transform:none}}

/* MEDIABOX ANDROID APP SHELL */
html,body{width:100%;max-width:100%;min-height:100%;margin:0;background:#070a12;overscroll-behavior-y:contain}
html{scroll-padding-top:90px;-webkit-text-size-adjust:100%}
body{min-height:100vh;min-height:100dvh;overflow-x:hidden;-webkit-tap-highlight-color:transparent;touch-action:pan-y;background:radial-gradient(ellipse at 12% 0%,#173b55 0,transparent 38%),radial-gradient(ellipse at 100% 10%,#38245c 0,transparent 30%),#070a12}
main{display:block;width:min(100%,760px);max-width:760px;min-height:100vh;min-height:100dvh;margin:0 auto;padding:0 16px calc(116px + env(safe-area-inset-bottom));background:linear-gradient(180deg,#0a1220a8 0,transparent 220px)}
.topline{position:sticky;top:0;z-index:1200;display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 -16px 20px;padding:calc(10px + env(safe-area-inset-top)) 16px 12px;border:0;border-bottom:1px solid #33445e;border-radius:0 0 21px 21px;background:rgba(7,12,22,.9);box-shadow:0 10px 30px #0004;backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px)}
.topline .brand{min-width:0;gap:11px}.topline .brand-mark{width:42px;height:42px;flex:0 0 42px;border-radius:14px}.topline .brand-name{font-size:16px;letter-spacing:1.6px}.topline .brand-sub{font-size:9px;letter-spacing:1.8px}
.bv-appbar-actions{display:flex;align-items:center;gap:8px}
.bv-appbar-button{display:grid;place-items:center;width:42px;min-width:42px;height:42px;min-height:42px;margin:0;padding:0;border:1px solid #344b68;border-radius:14px;background:linear-gradient(145deg,#172941,#10192a);color:#dff7ff;font-size:18px;box-shadow:none}
.bv-appbar-button:active{transform:scale(.94);background:#1b3850}.bv-install-button[hidden]{display:none!important}
.bv-screen-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 2px 17px}
.bv-screen-heading span{display:block;color:#88e9ef;font-size:9px;font-weight:900;letter-spacing:1.7px;text-transform:uppercase}
.bv-screen-heading h1{margin:3px 0 0;color:#f5f8ff;font-size:27px;line-height:1.15;letter-spacing:-.8px}
.bv-screen-heading-icon{display:grid;place-items:center;width:44px;height:44px;flex:0 0 44px;border:1px solid #3b5575;border-radius:15px;background:linear-gradient(145deg,#1a354e,#202342);color:#aaf7f4;font-size:20px}
.bv-dashboard{margin:0 0 18px;padding:22px 18px;border-radius:25px;border:1px solid #36516f;background:radial-gradient(ellipse at 100% 0%,#47629b42,transparent 50%),linear-gradient(145deg,#172b43,#0c1525);box-shadow:0 20px 48px #0005}
.bv-dashboard-kicker{font-size:9px;letter-spacing:1.7px}.bv-dashboard h2{font-size:24px;line-height:1.18;letter-spacing:-.7px}.bv-dashboard p{font-size:12px;line-height:1.65}
.bv-shortcut-grid{gap:9px;margin-top:16px}.bv-shortcut{min-height:76px;border-radius:17px;background:linear-gradient(145deg,#192d46,#0e192a);box-shadow:inset 0 1px #ffffff08;transition:transform .16s,border-color .16s}
.bv-shortcut:active{transform:scale(.975);border-color:#70f0e6}.bv-shortcut-icon{width:40px;height:40px;flex-basis:40px;border-radius:13px;background:linear-gradient(145deg,#1d4558,#343461);font-size:19px}
.card{margin:0 0 14px;padding:18px;border-radius:21px;border-color:#2d405b;background:linear-gradient(155deg,#121f32f5,#0b1422f7);box-shadow:0 12px 34px #0003}
.card+.card{margin-top:0}.section-head{align-items:center;margin-bottom:16px}.section-head h2{font-size:16px}.section-head p{font-size:11px;line-height:1.5}.section-icon{width:42px;height:42px;flex-basis:42px;border-radius:14px}
input,select,button{min-height:48px;border-radius:14px}input,select,textarea{font-size:16px!important}button{touch-action:manipulation}.primary,.secondary{min-height:49px}
.bv-tool-settings{display:grid;gap:12px;margin:0 0 14px;padding:17px;border:1px solid #344a68;border-radius:20px;background:linear-gradient(145deg,#15253a,#0c1524)}
.bv-tool-settings strong{display:block;color:#eef8ff;font-size:14px}.bv-tool-settings p{margin:4px 0 0;color:#9eb0ca;font-size:11px;line-height:1.55}.bv-tool-settings button{width:100%;margin:0}
.bv-account-links,.bv-account-actions{margin:0 0 14px}
.bv-account-actions{display:grid!important;grid-template-columns:1fr 1fr;align-items:stretch;gap:9px!important;flex-wrap:unset!important;padding:15px;border:1px solid #2d405b;border-radius:19px;background:#101b2b}
.bv-account-actions .pill{display:flex;align-items:center;justify-content:center;min-height:46px;padding:10px;border:1px solid #3a506f;border-radius:13px;background:#17263a;color:#e3efff;font-size:10px;text-align:center;white-space:normal}
.bv-account-actions form{display:flex;min-width:0;margin:0!important}.bv-account-actions form button{width:100%!important;min-height:46px;padding:10px;border:1px solid #3a506f;border-radius:13px;background:#17263a;color:#e3efff;font-size:11px}
.bv-account-links a{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:50px;margin:8px 0;padding:13px 14px;border:1px solid #344963;border-radius:15px;background:#111e30;color:#e9f4ff;text-decoration:none}
#bv-nav{position:fixed;z-index:3000;left:50%;bottom:calc(8px + env(safe-area-inset-bottom));transform:translateX(-50%);width:min(610px,calc(100% - 20px));display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:4px;padding:7px;border:1px solid #3c5271;border-radius:24px;background:rgba(8,14,25,.94);box-shadow:0 14px 48px #000b,0 0 28px #70f0e610;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}
#bv-nav button{width:auto;min-width:0;min-height:59px;margin:0;padding:7px 2px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;border:1px solid transparent;border-radius:16px;background:transparent;color:#95a7c1;font-size:9px;font-weight:800;line-height:1.1;box-shadow:none}
#bv-nav button .bv-nav-icon{display:grid;place-items:center;min-height:21px;font-size:20px;line-height:1}
#bv-nav button[aria-current="page"]{border-color:#70f0e650;background:linear-gradient(145deg,#173b4c,#202744);color:#a8fff6;box-shadow:inset 0 0 18px #70f0e612}
#bv-nav button:active{transform:scale(.94)}.bv-page:not([hidden]){animation:bvViewIn .18s ease-out both}.bv-page-title{margin:0 2px 14px}
#download-queue .queue-actions button{min-height:45px}.footer{padding-bottom:10px}
@media(min-width:761px){body{padding:20px 0}main{min-height:calc(100vh - 40px);border:1px solid #263953;border-radius:28px;overflow:visible;box-shadow:0 24px 90px #0006}.topline{border-radius:27px 27px 18px 18px}#bv-nav{bottom:20px}}
@media(max-width:420px){main{padding-left:13px;padding-right:13px;padding-bottom:calc(112px + env(safe-area-inset-bottom))}.topline{margin-left:-13px;margin-right:-13px;padding-left:13px;padding-right:13px}.bv-dashboard{padding:19px 15px}.bv-shortcut{min-height:72px;padding:10px}#bv-nav{width:calc(100% - 14px);padding:5px}#bv-nav button{min-height:58px;font-size:8px}.bv-account-actions{grid-template-columns:1fr}}
@media(display-mode:standalone){body{background:#070a12}main{padding-top:0}.topline{top:0}}
@media(prefers-reduced-motion:reduce){.bv-page:not([hidden]),.bv-shortcut{animation:none!important;transition:none!important}}


/* MEDIABOX — NATIVE ANDROID APP EXPERIENCE */
html,body{width:100%;max-width:100%;min-height:100%;margin:0;background:#070b14;overscroll-behavior-y:contain;-webkit-text-size-adjust:100%}
body{min-height:100vh;min-height:100dvh;overflow-x:hidden;-webkit-tap-highlight-color:transparent;touch-action:pan-y;background:radial-gradient(ellipse at 12% 0%,#173b55 0,transparent 38%),radial-gradient(ellipse at 100% 10%,#38245c 0,transparent 30%),#070b14}
main{display:block;width:100%;max-width:760px;min-height:100vh;min-height:100dvh;margin:0 auto;padding:0 16px calc(94px + env(safe-area-inset-bottom));border:0;border-radius:0;overflow:visible;background:linear-gradient(180deg,#0a1220a8 0,transparent 220px);box-shadow:none}
.topline{position:sticky;top:0;z-index:1200;display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 -16px 19px;padding:calc(10px + env(safe-area-inset-top)) 16px 12px;border:0;border-bottom:1px solid #263950;border-radius:0 0 18px 18px;background:rgba(8,14,25,.96);box-shadow:0 8px 26px #0005;backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px)}
.topline .brand{min-width:0;gap:10px}.topline .brand-mark{width:42px;height:42px;flex:0 0 42px;border-radius:13px;box-shadow:0 0 0 4px #70f0e610,0 8px 22px #0005}
.topline .brand-name{font-size:16px;letter-spacing:1.35px}.topline .brand-sub{font-size:9px;letter-spacing:1.25px;color:#8ea2bd}
.bv-appbar-actions{display:flex;align-items:center;gap:8px}.bv-appbar-button{width:42px;min-width:42px;height:42px;min-height:42px;margin:0;padding:0;border:1px solid #344b68;border-radius:13px;background:#142238;color:#e8f8ff;font-size:19px;box-shadow:none}
.bv-appbar-button:active{transform:scale(.94);background:#1b3850}
.bv-pages{min-height:60vh}.bv-page[hidden]{display:none!important}.bv-page:not([hidden]){display:block;animation:bvViewIn .18s ease-out both}
.bv-screen-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 2px 17px}
.bv-screen-heading span{color:#8defff;font-size:9px;font-weight:900;letter-spacing:1.5px}.bv-screen-heading h1{margin:4px 0 0;font-size:27px;line-height:1.16;letter-spacing:-.8px}
.bv-screen-heading-icon{width:44px;height:44px;flex:0 0 44px;display:grid;place-items:center;border:1px solid #3a5474;border-radius:14px;background:#17283f;color:#a7f7f2;font-size:20px}
.bv-dashboard{margin:0 0 16px;padding:20px 18px;border:1px solid #304a68;border-radius:22px;background:linear-gradient(145deg,#152840,#0d1727);box-shadow:0 15px 38px #0004}
.bv-dashboard-kicker{color:#8defff;font-size:9px;font-weight:900;letter-spacing:1.6px}.bv-dashboard h2{margin:8px 0 5px;font-size:23px;line-height:1.2;letter-spacing:-.5px}.bv-dashboard p{margin:0;color:#aabbd3;font-size:12px;line-height:1.65}
.bv-shortcut-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:16px}
.bv-shortcut{display:flex;align-items:center;gap:10px;min-width:0;min-height:70px;margin:0;padding:11px;border:1px solid #314762;border-radius:16px;background:#101d30;color:#e9f4ff;text-align:left;box-shadow:none}
.bv-shortcut:last-child{grid-column:1/-1;min-height:62px}.bv-shortcut:active{transform:scale(.98);border-color:#70f0e6}
.bv-shortcut-icon{width:38px;height:38px;flex:0 0 38px;display:grid;place-items:center;border-radius:12px;background:#1a3b50;color:#a8f7f0;font-size:19px}
.bv-shortcut strong{display:block;font-size:12px}.bv-shortcut small{display:block;margin-top:3px;color:#92a6c2;font-size:10px;line-height:1.4}
.card{margin:0 0 13px;padding:17px;border:1px solid #2d405b;border-radius:19px;background:#101a2a;box-shadow:0 9px 25px #0003}
.card+.card{margin-top:0}.section-head{align-items:center;gap:11px;margin-bottom:15px}.section-head h2{font-size:16px}.section-head p{font-size:11px;line-height:1.5}
.section-icon{width:41px;height:41px;flex:0 0 41px;border:1px solid #344c69;border-radius:13px;background:#17283f;color:#8defff;font-size:18px}
input,select,textarea{font-size:16px!important;min-height:48px;border-radius:13px;background:#091321;border-color:#30445f}
button{min-height:48px;border-radius:13px;touch-action:manipulation}.primary{background:linear-gradient(110deg,#70f0e6,#8aa8ff 60%,#c09aff);box-shadow:0 8px 22px #7b9cff20}
.secondary{background:#17243a;border-color:#344763}.status{border-radius:12px;background:#0b1422;border:1px solid #293d57;padding:10px 12px}
#bv-nav{position:fixed;z-index:3000;left:50%;right:auto;bottom:0;transform:translateX(-50%);width:min(760px,100%);display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:2px;padding:7px 8px calc(7px + env(safe-area-inset-bottom));border:1px solid #2c405c;border-bottom:0;border-radius:18px 18px 0 0;background:rgba(7,13,23,.98);box-shadow:0 -10px 34px #0008;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}
#bv-nav button{width:auto;min-width:0;min-height:58px;margin:0;padding:5px 2px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;border:1px solid transparent;border-radius:13px;background:transparent;color:#8fa2bd;font-size:9px;font-weight:800;line-height:1.1;box-shadow:none}
#bv-nav button .bv-nav-icon{width:42px;min-height:24px;display:grid;place-items:center;border:1px solid transparent;border-radius:999px;font-size:19px;line-height:1}
#bv-nav button[aria-current="page"]{border-color:transparent;background:transparent;color:#a8fff6;box-shadow:none}
#bv-nav button[aria-current="page"] .bv-nav-icon{border-color:#315568;background:#173847;color:#a8fff6}
#bv-nav button:active{transform:scale(.94)}
.bv-account-actions{margin-bottom:13px}.bv-account-links a{border-radius:14px;background:#111e30}
.footer{padding-bottom:12px}.legal{font-size:10px}
@media(min-width:761px){body{padding:0}main{width:min(760px,100%);min-height:100dvh;border:0;border-radius:0;box-shadow:none;padding-bottom:110px}#bv-nav{bottom:0}}
@media(max-width:420px){main{padding-left:13px;padding-right:13px;padding-bottom:calc(90px + env(safe-area-inset-bottom))}.topline{margin-left:-13px;margin-right:-13px;padding-left:13px;padding-right:13px}.bv-dashboard{padding:18px 15px}.bv-dashboard h2{font-size:21px}.bv-shortcut{min-height:66px;padding:9px;gap:8px}.bv-shortcut-icon{width:34px;height:34px;flex-basis:34px}#bv-nav{width:100%;padding-left:5px;padding-right:5px}#bv-nav button{min-height:56px;font-size:8px}#bv-nav button .bv-nav-icon{width:38px;font-size:18px}}
@media(display-mode:standalone){html,body{background:#070b14}main{padding-top:0}.topline{top:0}}
@media(prefers-reduced-motion:reduce){.bv-page:not([hidden]),.bv-shortcut{animation:none!important;transition:none!important}}


/* MEDIABOX MOBILE WEB REDESIGN — experiência do site dentro do WebToApp */
:root{color-scheme:dark;--bv-bg:#070b14;--bv-panel:#111d30;--bv-line:#2b405c;--bv-cyan:#78f2e8;--bv-blue:#83a2ff;--bv-purple:#b99aff}
html,body{width:100%;max-width:100%;min-height:100%;margin:0;background:var(--bv-bg);overscroll-behavior-y:contain;-webkit-text-size-adjust:100%}
body{min-height:100vh;min-height:100dvh;overflow-x:hidden;-webkit-tap-highlight-color:transparent;background:radial-gradient(ellipse at 12% 0%,#17445a 0,transparent 38%),radial-gradient(ellipse at 100% 12%,#39265c 0,transparent 32%),#070b14}
main{display:block;width:100%;max-width:760px;min-height:100vh;min-height:100dvh;margin:0 auto;padding:0 15px calc(98px + env(safe-area-inset-bottom));border:0;border-radius:0;background:linear-gradient(180deg,#0b1424a8 0,transparent 260px);box-shadow:none}
.topline{position:sticky;top:0;z-index:1200;display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 -15px 18px;padding:calc(10px + env(safe-area-inset-top)) 15px 11px;border:0;border-bottom:1px solid #2c405a;border-radius:0 0 19px 19px;background:rgba(7,13,24,.96);box-shadow:0 8px 28px #0005;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}
.topline .brand{min-width:0;gap:10px}.topline .brand-mark{width:42px;height:42px;flex:0 0 42px;border-radius:14px;object-fit:cover;box-shadow:0 0 0 4px #70f0e610,0 8px 24px #0005}
.topline .brand-name{font-size:16px;letter-spacing:1.5px}.topline .brand-sub{font-size:9px;letter-spacing:1.5px;color:#91a6c3}
.bv-appbar-actions{display:flex;align-items:center;gap:7px}.bv-appbar-button{display:grid;place-items:center;width:42px;min-width:42px;height:42px;min-height:42px;margin:0;padding:0;border:1px solid #38516f;border-radius:14px;background:#14243a;color:#e8f8ff;font-size:19px;box-shadow:none}
.bv-appbar-button:active{transform:scale(.94);background:#1b3850}
#bv-pages{min-height:60vh;padding-top:2px}.bv-page[hidden]{display:none!important}.bv-page:not([hidden]){display:block;animation:bvViewIn .2s ease-out both}
.bv-screen-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:5px 2px 17px}
.bv-screen-heading span{display:block;color:#8defff;font-size:9px;font-weight:900;letter-spacing:1.7px;text-transform:uppercase}
.bv-screen-heading h1{margin:4px 0 0;color:#f5f8ff;font-size:28px;line-height:1.15;letter-spacing:-.8px}
.bv-screen-heading-icon{display:grid;place-items:center;width:45px;height:45px;flex:0 0 45px;border:1px solid #3a5575;border-radius:15px;background:linear-gradient(145deg,#1a354e,#202342);color:#aaf7f4;font-size:20px}
.bv-home-dashboard{position:relative;isolation:isolate;overflow:hidden;margin:0 0 16px;padding:17px 16px 15px;border:1px solid #385473;border-radius:25px;background:radial-gradient(ellipse at 100% 0%,#5475ae42,transparent 48%),linear-gradient(150deg,#172b43 0%,#101a2b 58%,#0b1422 100%);box-shadow:0 20px 50px #0006}
.bv-home-dashboard:before{content:"";position:absolute;z-index:-1;width:250px;height:250px;right:-145px;top:95px;border:1px solid #8ceeff18;border-radius:50%;box-shadow:0 0 0 24px #8ceeff08,0 0 0 52px #8ceeff05}
.bv-home-topline{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#9eb4d0;font-size:8px;font-weight:900;letter-spacing:1.35px}
.bv-home-live{display:flex;align-items:center;gap:5px;padding:6px 8px;border:1px solid #3f665f;border-radius:99px;background:#102a2b;color:#a4f8dc;font-size:7px;letter-spacing:.75px;white-space:nowrap}
.bv-home-live i{width:6px;height:6px;border-radius:50%;background:#67e8bd;box-shadow:0 0 9px #67e8bd}
.bv-home-art{position:absolute;right:17px;top:70px;width:106px;height:106px;display:grid;place-items:center}
.bv-home-art-ring{position:absolute;inset:5px;border:1px solid #7cbbdf77;border-radius:30px;transform:rotate(45deg);box-shadow:0 0 35px #6f9dff1b,inset 0 0 25px #6f9dff14}
.bv-home-play{position:relative;z-index:2;width:70px;height:70px;display:grid;place-items:center;padding-left:5px;border:1px solid #93eaff88;border-radius:23px;background:linear-gradient(145deg,#1a536a,#293b70 60%,#503d78);color:#c6ffff;font-size:28px;text-shadow:0 0 16px #70f0e6;box-shadow:0 12px 30px #0006,0 0 25px #70f0e622}
.bv-home-art-label{position:absolute;right:-4px;bottom:0;padding:5px 7px;border:1px solid #536b96;border-radius:8px;background:#101b30;color:#bcd1ff;font-size:8px;font-weight:950;letter-spacing:2px}
.bv-home-copy{position:relative;z-index:2;max-width:78%;padding:25px 0 20px}
.bv-home-kicker{color:#85f1e7;font-size:9px;font-weight:950;letter-spacing:1.7px}
.bv-home-copy h2{margin:9px 0 9px;color:#fff;font-size:clamp(28px,7vw,37px);line-height:1.1;letter-spacing:-1.25px}
.bv-home-copy h2 span{background:linear-gradient(100deg,#79eee5,#93b0ff 65%,#c6a0ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.bv-home-copy p{margin:0;color:#afc0d7;font-size:11px;line-height:1.65}
.bv-home-main-action{position:relative;z-index:2;width:100%;min-height:66px;display:flex;align-items:center;gap:12px;margin:0;padding:10px 13px;border:1px solid #87e8e6;border-radius:17px;background:linear-gradient(105deg,#70eee2,#91adff 68%,#c09aff);color:#091321;text-align:left;box-shadow:0 10px 28px #6e9cff2b}
.bv-home-main-action:active{transform:scale(.985)}
.bv-home-main-icon{width:43px;height:43px;flex:0 0 43px;display:grid;place-items:center;border:1px solid #ffffff7a;border-radius:14px;background:#ffffff38;font-size:25px;font-weight:900}
.bv-home-main-action span:nth-child(2){min-width:0;flex:1}.bv-home-main-action strong,.bv-home-main-action small{display:block}
.bv-home-main-action strong{font-size:14px;font-weight:950}.bv-home-main-action small{margin-top:2px;color:#172943;font-size:9px;font-weight:750}
.bv-home-main-action>b{font-size:23px}
.bv-home-section-label{margin:20px 2px 9px;color:#9eb4d0;font-size:9px;font-weight:950;letter-spacing:1.7px}
.bv-home-shortcuts{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.bv-home-shortcut{display:flex;align-items:center;gap:8px;min-width:0;min-height:66px;margin:0;padding:9px;border:1px solid #344b68;border-radius:15px;background:linear-gradient(145deg,#182b43,#111c2d);color:#edf7ff;text-align:left;box-shadow:none}
.bv-home-shortcut:last-child{grid-column:1/-1}
.bv-home-shortcut:active{transform:scale(.98);border-color:#70f0e6}
.bv-home-shortcut-icon{width:36px;height:36px;flex:0 0 36px;display:grid;place-items:center;border:1px solid #3b5c77;border-radius:12px;background:#18384a;color:#a4f7f0;font-size:19px}
.bv-home-shortcut>span:nth-child(2){min-width:0;flex:1}.bv-home-shortcut strong,.bv-home-shortcut small{display:block;overflow-wrap:anywhere}
.bv-home-shortcut strong{font-size:11px}.bv-home-shortcut small{margin-top:2px;color:#96aac6;font-size:9px;line-height:1.35}
.bv-home-shortcut>b{color:#83e8e6;font-size:18px}
.bv-home-footnote{display:flex;justify-content:space-between;gap:8px;margin-top:16px;padding-top:11px;border-top:1px solid #ffffff12;color:#7189a8;font-size:8px;font-weight:900;letter-spacing:1.7px}
.card{margin:0 0 13px;padding:17px;border:1px solid #30445f;border-radius:20px;background:linear-gradient(155deg,#121f32f7,#0b1422f9);box-shadow:0 12px 32px #0004}
.card+.card{margin-top:0}.section-head{align-items:center;gap:11px;margin-bottom:15px}.section-head h2{font-size:16px}.section-head p{font-size:11px;line-height:1.5}
.section-icon{width:42px;height:42px;flex:0 0 42px;border:1px solid #385471;border-radius:14px;background:linear-gradient(145deg,#18344b,#1d2944);color:#91f2ec;font-size:18px}
input,select,textarea{width:100%;min-height:49px;padding:13px 14px;border:1px solid #344963;border-radius:13px;background:#091321;color:#f4f7ff;font-size:16px!important}
textarea{resize:vertical}input::placeholder,textarea::placeholder{color:#71839f}
input:focus,select:focus,textarea:focus{border-color:#78eee7;outline:0;box-shadow:0 0 0 3px #70f0e61c}
button{min-height:48px;border-radius:13px;touch-action:manipulation}button:active{transform:scale(.985)}
.primary{border:0;background:linear-gradient(110deg,#70f0e6,#8aa8ff 60%,#c09aff);color:#08101c;font-weight:900;box-shadow:0 8px 22px #7b9cff25}
.secondary{border:1px solid #3b5270;background:#17263c;color:#e5f0ff;font-weight:800}
.status{padding:10px 12px;border:1px solid #2e4560;border-radius:13px;background:#091522;color:#d5e5f7;font-size:12px;line-height:1.5}
.bar{height:8px;border-radius:99px;background:#24364f;overflow:hidden}.fill{background:linear-gradient(90deg,#70f0e6,#7b9cff,#c09aff)}
.item{padding:13px;border:1px solid #30445f;border-radius:15px;background:#0d1929}
.path-note{border:1px solid #30445f;border-radius:14px;background:#0c1726}
#download-queue .queue-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}
#download-queue .queue-actions button{min-width:0;min-height:45px;margin:0;padding:10px;font-size:11px}
#bv-nav{position:fixed;z-index:3000;left:50%;right:auto;bottom:0;transform:translateX(-50%);width:min(760px,100%);display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:3px;padding:7px 7px calc(7px + env(safe-area-inset-bottom));border:1px solid #2d4562;border-bottom:0;border-radius:20px 20px 0 0;background:rgba(7,13,24,.97);box-shadow:0 -10px 32px #0009;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}
#bv-nav button{width:auto;min-width:0;min-height:58px;margin:0;padding:5px 2px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;border:1px solid transparent;border-radius:14px;background:transparent;color:#93a7c3;font-size:8px;font-weight:850;line-height:1.1;box-shadow:none}
#bv-nav button .bv-nav-icon{width:39px;min-height:23px;display:grid;place-items:center;border:1px solid transparent;border-radius:99px;font-size:20px;line-height:1}
#bv-nav button[aria-current="page"]{border-color:transparent;background:transparent;color:#a8fff6;box-shadow:none}
#bv-nav button[aria-current="page"] .bv-nav-icon{border-color:#3c6575;background:#183b4a;color:#a8fff6;box-shadow:0 0 18px #70f0e619}
.bv-account-actions,.bv-account-links{margin:0 0 13px}
.bv-account-links a{min-height:50px;border:1px solid #344963;border-radius:14px;background:#111e30;color:#e9f4ff}
.bv-tool-settings{border:1px solid #344963;border-radius:19px;background:linear-gradient(145deg,#15253a,#0c1524)}
.footer{padding-bottom:12px}
@media(min-width:761px){body{padding:0}main{width:min(760px,100%);min-height:100dvh;padding-bottom:110px;border:0;border-radius:0;box-shadow:none}#bv-nav{bottom:0}}
@media(max-width:420px){main{padding-left:12px;padding-right:12px;padding-bottom:calc(91px + env(safe-area-inset-bottom))}.topline{margin-left:-12px;margin-right:-12px;padding-left:12px;padding-right:12px}.bv-home-dashboard{padding:15px 13px 13px;border-radius:22px}.bv-home-art{right:8px;top:69px;width:88px;height:88px}.bv-home-play{width:59px;height:59px;border-radius:19px;font-size:24px}.bv-home-copy{max-width:79%;padding-top:23px;padding-bottom:18px}.bv-home-copy h2{font-size:clamp(26px,7vw,32px)}.bv-home-copy p{font-size:10px}.bv-home-shortcut{min-height:62px;padding:8px;gap:7px}.bv-home-shortcut-icon{width:32px;height:32px;flex-basis:32px}.bv-home-shortcut strong{font-size:10px}#bv-nav{width:100%;padding-left:4px;padding-right:4px}#bv-nav button{min-height:55px;font-size:8px}}
@media(display-mode:standalone){html,body{background:#070b14}main{padding-top:0}.topline{top:0}}
@media(prefers-reduced-motion:reduce){.bv-page:not([hidden]),.bv-home-shortcut,.bv-home-main-action{animation:none!important;transition:none!important}}
</style></head><body><main>
<div id="error-intro" hidden aria-live="assertive" aria-label="Animação de diagnóstico">
  <div class="intro-card">
    <div class="intro-kicker">MEDIABOX · SISTEMA DE DIAGNÓSTICO</div>
    <div id="intro-icon" class="intro-icon" aria-hidden="true"></div>
    <h2 id="intro-title">Ops! Detectamos algo</h2>
    <p id="intro-message">Detectando a origem e preparando verificações reais...</p>
     <div class="intro-effect" aria-hidden="true"><span>BV</span></div>
    <div id="intro-progress" class="intro-progress" role="progressbar" aria-label="Progresso do pré-diagnóstico" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="intro-progress-fill" class="intro-progress-fill"></div></div>
    <div id="intro-progress-label" class="intro-progress-label">Preparando verificações...</div>
    <div id="intro-steps" class="intro-steps" aria-live="polite"></div>
    <div class="intro-dots" aria-hidden="true"><i></i><i></i><i></i></div>
  </div>
</div>
<div id="error-center" hidden role="alertdialog" aria-modal="true" aria-labelledby="error-title">
  <section class="error-panel">
    <div class="error-top"><span class="error-brand">MEDIABOX · DIAGNÓSTICO</span><span class="error-status">FALHA IDENTIFICADA</span></div>
    <div id="error-symbol" class="error-symbol" aria-hidden="true">⚠</div>
    <h2 id="error-title">Não foi possível concluir</h2>
    <p id="error-message" class="error-summary">Ocorreu um problema durante esta operação.</p>
    <p id="error-diagnosis-summary" class="error-diagnosis-summary">Aguardando verificações automáticas...</p>
    <div class="error-facts">
      <div class="error-fact"><span>Categoria</span><strong id="error-category">Operação</strong></div>
      <div class="error-fact"><span>Código</span><strong id="error-code">MB-000</strong></div>
      <div class="error-fact"><span>Resposta HTTP</span><strong id="error-http">Não disponível</strong></div>
      <div class="error-fact"><span>Horário</span><strong id="error-time">—</strong></div>
      <div class="error-fact"><span>Verificações</span><strong id="error-checks">—</strong></div>
      <div class="error-fact"><span>Ocorrências</span><strong id="error-repeats">1x</strong></div>
    </div>
    <div class="error-suggestion"><strong>PRÓXIMA AÇÃO SUGERIDA</strong><p id="error-suggestion">Aguardando o resultado das verificações.</p></div>
    <details><summary>DETALHES TÉCNICOS</summary><pre id="error-technical" class="error-technical">Aguardando diagnóstico...</pre></details>
    <div class="error-actions">
      <button type="button" class="error-copy" id="error-copy">Copiar diagnóstico</button>
      <button type="button" class="secondary" id="error-history-toggle">Histórico (0)</button>
      <button type="button" class="primary" onclick="fecharErroCentral()">Voltar ao MEDIABOX</button>
    </div>
    <section id="error-history" class="error-history" hidden>
      <div class="error-history-head"><span>DIAGNÓSTICOS RECENTES</span><button type="button" class="secondary" id="error-history-clear">Limpar histórico</button></div>
      <div id="error-history-list" class="error-history-list"></div>
    </section>
  </section>
</div>
<div class="topline"><div class="brand"><img class="brand-mark" src="/brand/bluevision-mark.svg" alt="MEDIABOX"><div><div class="brand-name">MEDIABOX</div><div class="brand-sub">VIDEO STUDIO</div></div></div><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><a class="pill" href="/admin" style="text-decoration:none">🔐 ADMIN CONTROL</a></div></div>


<div id="mb-settings" hidden>
 <section class="mb-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="mb-settings-title">
  <header class="mb-settings-head">
   <div class="mb-settings-brand"><span class="mb-settings-mark">BV</span><div><small>MEDIABOX · PAINEL RÁPIDO</small><h2 id="mb-settings-title">Configurações</h2><p>Personalize sem sair da página.</p></div></div>
   <button type="button" class="mb-settings-close" id="mb-settings-close" aria-label="Fechar configurações">×</button>
  </header>
  <nav class="mb-settings-tabs" aria-label="Abas de configuração">
   <button type="button" id="mb-tab-errors" aria-selected="true">✦ Animações</button>
   <button type="button" id="mb-tab-commands" aria-selected="false">⌘ Comandos</button>
  </nav>
  <section id="mb-settings-errors">
   <span class="mb-settings-kicker">EFEITOS DE DIAGNÓSTICO</span>
   <p class="mb-settings-copy">Escolha como as mensagens de diagnóstico aparecem. Cada opção tem um movimento diferente e discreto; o restante do MEDIABOX não muda.</p>
   <div class="mb-error-options">
    <button type="button" class="mb-error-option" data-error-choice="scanner" aria-pressed="false"><span class="mb-error-swatch"></span><span><strong>Scanner holográfico</strong><small>Varredura técnica com anéis azul e ciano.</small></span><span class="mb-option-check">✓</span></button>
    <button type="button" class="mb-error-option" data-error-choice="aurora" aria-pressed="true"><span class="mb-error-swatch"></span><span><strong>Aurora suave</strong><small>Brilho fluido em tons azul e ciano.</small></span><span class="mb-option-check">✓</span></button>
    <button type="button" class="mb-error-option" data-error-choice="ripple" aria-pressed="false"><span class="mb-error-swatch"></span><span><strong>Ondas de luz</strong><small>Círculos que se expandem suavemente.</small></span><span class="mb-option-check">✓</span></button>
    <button type="button" class="mb-error-option" data-error-choice="shimmer" aria-pressed="false"><span class="mb-error-swatch"></span><span><strong>Reflexo premium</strong><small>Um brilho atravessa o painel.</small></span><span class="mb-option-check">✓</span></button>
    <button type="button" class="mb-error-option" data-error-choice="orbit" aria-pressed="false"><span class="mb-error-swatch"></span><span><strong>Órbita azul</strong><small>Anéis finos giram ao redor do símbolo.</small></span><span class="mb-option-check">✓</span></button>
    <button type="button" class="mb-error-option" data-error-choice="minimal" aria-pressed="false"><span class="mb-error-swatch"></span><span><strong>Entrada limpa</strong><small>Só uma transição leve, sem efeitos extras.</small></span><span class="mb-option-check">✓</span></button>
   </div>
   <div class="mb-settings-actions"><button type="button" class="primary" id="mb-error-preview">◉ Ver prévia</button><button type="button" class="mb-settings-secondary" id="mb-error-reset">Restaurar padrão</button></div>
   <p class="mb-command-hint" id="mb-error-setting-status" aria-live="polite">Efeito atual: Aurora suave.</p>
  </section>
  <section id="mb-settings-commands" hidden>
   <span class="mb-settings-kicker">ATALHOS ESPECIAIS</span>
   <p class="mb-settings-copy">Digite um código ou toque em um dos atalhos. São surpresas visuais locais: não enviam comandos ao servidor nem alteram seus arquivos.</p>
   <div class="mb-command-box">
    <label for="mb-command-input">CÓDIGO DO COMANDO</label>
    <div class="mb-command-row"><input id="mb-command-input" type="text" inputmode="numeric" autocomplete="off" maxlength="20" placeholder="Ex.: 7777"><button type="button" class="primary" id="mb-command-run">Executar</button></div>
    <p class="mb-command-status" id="mb-command-status" aria-live="polite">Escolha um código abaixo.</p>
    <div class="mb-command-grid">
     <button type="button" class="mb-command-pick" data-command-pick="1234"><b>1234</b><span>Surpresa do urso ND</span></button>
     <button type="button" class="mb-command-pick" data-command-pick="7777"><b>7777</b><span>Chuva de estrelas</span></button>
     <button type="button" class="mb-command-pick" data-command-pick="4040"><b>4040</b><span>Portal secreto 404</span></button>
     <button type="button" class="mb-command-pick" data-command-pick="2026"><b>2026</b><span>Edição especial MEDIABOX</span></button>
     <button type="button" class="mb-command-pick" data-command-pick="0000"><b>0000</b><span>Restaurar animação padrão</span></button>
    </div>
    <p class="mb-command-hint">Os códigos são executados apenas nesta tela. O comando 1234 mantém a surpresa original do urso ND.</p>
   </div>
  </section>
 </section>
</div>
<div id="mb-birthday" hidden role="dialog" aria-modal="true" aria-labelledby="mb-birthday-title">
 <section class="mb-birthday-card">
  <button type="button" class="mb-birthday-close" id="mb-birthday-close" aria-label="Fechar surpresa">×</button>
  <div class="mb-birthday-kicker">MEDIABOX · COMANDO ESPECIAL 1234</div>
  <svg class="mb-bear" viewBox="0 0 180 190" role="img" aria-label="Urso de pelúcia com a identificação ND">
   <defs><linearGradient id="mbBearFur" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#f4c887"/><stop offset="1" stop-color="#a96b43"/></linearGradient><linearGradient id="mbBearBelly" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff0cf"/><stop offset="1" stop-color="#e9cda8"/></linearGradient></defs>
   <ellipse cx="90" cy="170" rx="57" ry="12" fill="#0006"/><circle cx="48" cy="40" r="25" fill="url(#mbBearFur)" stroke="#ffe2b8" stroke-width="3"/><circle cx="132" cy="40" r="25" fill="url(#mbBearFur)" stroke="#ffe2b8" stroke-width="3"/><circle cx="48" cy="40" r="12" fill="#d58f72"/><circle cx="132" cy="40" r="12" fill="#d58f72"/>
   <ellipse cx="90" cy="132" rx="48" ry="50" fill="url(#mbBearFur)" stroke="#ffe2b8" stroke-width="3"/><ellipse cx="40" cy="126" rx="17" ry="36" transform="rotate(-28 40 126)" fill="url(#mbBearFur)" stroke="#ffe2b8" stroke-width="3"/><ellipse cx="140" cy="126" rx="17" ry="36" transform="rotate(28 140 126)" fill="url(#mbBearFur)" stroke="#ffe2b8" stroke-width="3"/><ellipse cx="90" cy="140" rx="29" ry="32" fill="url(#mbBearBelly)"/>
   <circle cx="90" cy="69" r="51" fill="url(#mbBearFur)" stroke="#ffe2b8" stroke-width="3"/><ellipse cx="90" cy="88" rx="27" ry="19" fill="#fff0cf"/><ellipse cx="73" cy="66" rx="4" ry="5" fill="#332218"/><ellipse cx="107" cy="66" rx="4" ry="5" fill="#332218"/><ellipse cx="90" cy="82" rx="7" ry="5" fill="#493027"/><path d="M90 87q-2 10-11 9m11-9q2 10 11 9" fill="none" stroke="#493027" stroke-width="2.5" stroke-linecap="round"/><rect x="69" y="119" width="42" height="23" rx="8" fill="#233d61" stroke="#8ceeff" stroke-width="1.5"/><text x="90" y="135" text-anchor="middle" fill="#9ff8ff" font-size="13" font-weight="900" font-family="Arial,sans-serif">ND</text>
  </svg>
  <h1 id="mb-birthday-title">Feliz aniversário! 🎂</h1><div class="mb-speech">“Feliz aniversário!”</div>
  <p class="mb-birthday-message">O urso do ND chegou com uma mensagem especial: que seu dia seja cheio de alegria, carinho e momentos incríveis! 🧸</p>
  <div class="mb-birthday-footer">UMA SURPRESA ESPECIAL DO MEDIABOX</div>
 </section>
</div>
<div id="mb-command-surprise" hidden role="dialog" aria-modal="true" aria-labelledby="mb-command-title">
 <section class="mb-command-card" data-theme="celebration">
  <button type="button" class="mb-command-close" id="mb-command-surprise-close" aria-label="Fechar surpresa">×</button>
  <div class="mb-command-kicker">MEDIABOX · COMANDO ESPECIAL</div>
  <div class="mb-command-symbol" id="mb-command-symbol">✦</div>
  <h2 id="mb-command-title">Surpresa desbloqueada</h2>
  <p class="mb-command-message" id="mb-command-message"></p>
 </section>
</div>
<a href="/matematica" style="display:block;margin:15px 0;padding:17px;border:1px solid #70f0e6;border-radius:17px;background:linear-gradient(110deg,#142d42,#241b40);color:#70f0e6;text-decoration:none;font-weight:800;text-align:center">🧮 ABRIR MATEMÁTICA → Calculadora, tabuada e mais</a>
<header class="hero"><div class="eyebrow">Seu conteúdo, em um só lugar</div><h1>Blue<span>Vision</span></h1><p>Uma central simples para baixar vídeos e encontrar conteúdos.</p><div class="hero-tags"><span>Downloads</span><span>Pesquisa</span><span>Ferramentas de vídeo</span></div></header>
<section class="card"><div class="section-head"><div class="section-icon">↓</div><div><h2>Baixar vídeo</h2><p>Cole o link e escolha como deseja salvar.</p></div></div><form id="form">
<div class="field"><label for="url">LINK DO VÍDEO</label><input id="url" type="url" required placeholder="Cole aqui o link do YouTube, Instagram, Kwai ou TikTok"><button type="button" class="secondary" id="load-video-preview" style="margin-top:9px">▶ Carregar prévia real do link</button><p class="small" id="link-preview-status" role="status">Cole o link e carregue a prévia antes de posicionar a marca.</p></div>
<div class="grid"><div class="field"><label for="plat">PLATAFORMA</label><select id="plat"><option value="auto">Detectar automaticamente</option><option value="youtube">YouTube</option><option value="instagram">Instagram</option><option value="kwai">Kwai</option><option value="tiktok">TikTok</option><option value="outros">Outros compatíveis</option></select></div>
<div class="field"><label for="fmt">FORMATO DO ARQUIVO</label><select id="fmt"><option value="mp4">MP4 / padrão</option><option value="melhor">Melhor qualidade</option><option value="menor">Arquivo menor</option></select></div></div>
<div class="grid"><div class="field"><label for="watermark">MARCA D'ÁGUA</label><select id="watermark"><option value="dias_edits" selected>Sim — Dias Edits (original)</option><option value="mediabox">Sim — Powered by MEDIABOX</option><option value="nao">Não — sem marca</option></select></div>
<div class="field"><label for="watermark-position">POSIÇÃO DA MARCA</label><select id="watermark-position"><option value="superior_esquerdo">Superior esquerdo</option><option value="superior_direito">Superior direito</option><option value="inferior_esquerdo">Inferior esquerdo</option><option value="inferior_direito" selected>Inferior direito</option></select></div></div>
<div class="field"><button type="button" class="secondary" id="preview-watermark">👁 Visualizar e posicionar marca d'água</button><p class="small">Arraste a marca na prévia. A posição será aplicada ao vídeo final.</p><input type="hidden" id="watermark-x" value="88"><input type="hidden" id="watermark-y" value="88"></div>
<button class="primary" id="btn">Iniciar download</button></form><div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px"><div class="status" id="status" style="margin:0">Pronto para começar.</div><strong id="percent" style="font-size:20px;color:var(--cyan);font-variant-numeric:tabular-nums">0%</strong></div><div class="bar" role="progressbar" aria-label="Progresso do vídeo" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="fill" class="fill"></div></div><div id="err" class="error"></div>
<a id="download-link" class="primary" href="#" hidden
style="display:block;text-align:center;text-decoration:none;padding:14px;border-radius:14px;margin-top:14px">
⬇ Baixar vídeo concluído
</a>
<video id="download-preview" controls playsinline preload="metadata" hidden style="display:none;width:100%;max-height:62vh;margin-top:14px;border:1px solid #344763;border-radius:16px;background:#000"></video>
<div class="path-note small"><strong>Arquivos do MEDIABOX</strong><br>
<span class="muted">Quando o vídeo terminar, o botão para baixar aparecerá aqui.</span></div></section>

<section class="card" id="download-queue">
<style>
#download-queue .queue-input{display:block;width:100%;min-height:112px;box-sizing:border-box;padding:13px;border:1px solid #344763;border-radius:13px;background:#080e19;color:#f4f7ff;font:inherit;line-height:1.5;resize:vertical}
#download-queue .queue-help{margin:8px 0;color:var(--muted);font-size:12px;line-height:1.5}
#download-queue .queue-actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:12px}
#download-queue .queue-actions button{flex:1;min-width:135px}
#download-queue .queue-summary{margin:12px 0;color:var(--muted);font-size:12px}
#download-queue .queue-item{margin-top:10px;padding:13px;border:1px solid #344763;border-radius:14px;background:#0b1220}
#download-queue .queue-item-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
#download-queue .queue-state{font-size:11px;font-weight:800;color:var(--cyan)}
#download-queue .queue-url{margin-top:7px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}
#download-queue .queue-track{height:7px;margin-top:9px;border-radius:99px;background:#26354d;overflow:hidden}
#download-queue .queue-fill{height:100%;width:0;background:linear-gradient(90deg,var(--cyan),var(--blue),var(--pink));transition:width .25s}
#download-queue .queue-error{margin-top:8px;color:#ff9eaf;font-size:12px;overflow-wrap:anywhere}
#download-queue .queue-link,#download-queue .queue-retry{display:inline-block;margin-top:10px;padding:9px 12px;border-radius:10px;font-size:12px;font-weight:800}
#download-queue .queue-preview{display:block;width:100%;max-height:260px;margin-top:12px;border-radius:12px;background:#050914;border:1px solid #344763}
#download-queue .queue-thumb{display:block;width:100%;max-height:150px;object-fit:cover;margin-top:12px;border-radius:12px;border:1px solid #344763}
#download-queue .queue-link{background:#70f0e6;color:#08101c}
#download-queue .queue-retry{border:1px solid #52698e;background:#17243a;color:#e4edff;cursor:pointer}
#download-queue button:disabled{opacity:.5;cursor:not-allowed}
</style>
<div class="section-head"><div class="section-icon">☷</div><div><h2>MEDIABOX DOWNLOAD MANAGER</h2><p>Adicione vários links e baixe um vídeo por vez.</p></div></div>
<div class="field"><label for="queue-input">LINKS PARA A FILA · UM POR LINHA</label><textarea id="queue-input" class="queue-input" rows="4" placeholder="https://www.youtube.com/watch?v=...\nhttps://www.instagram.com/reel/...\nhttps://www.kwai.com/...\nhttps://www.tiktok.com/@perfil/video/..."></textarea><p class="queue-help">Até 20 links por envio. Plataforma e qualidade vêm do formulário acima; a marca-d’água é escolhida separadamente para cada nova adição.</p></div>
<div class="grid">
  <div class="field"><label for="queue-watermark">MARCA-D’ÁGUA NESTA ADIÇÃO</label><select id="queue-watermark"><option value="sim" selected>Sim — Dias Edits (original)</option><option value="nao">Não — manter original</option></select></div>
  <div class="field"><label for="queue-watermark-position">POSIÇÃO DA MARCA</label><select id="queue-watermark-position"><option value="superior_esquerdo">Superior esquerdo</option><option value="superior_direito">Superior direito</option><option value="inferior_esquerdo">Inferior esquerdo</option><option value="inferior_direito" selected>Inferior direito</option></select></div>
</div>
<div class="queue-actions"><button type="button" class="secondary" id="queue-add">＋ Adicionar links</button><button type="button" class="primary" id="queue-start">▶ Iniciar fila</button><button type="button" class="secondary" id="queue-pause">⏸ Pausar fila</button><button type="button" class="secondary" id="queue-live-toggle" aria-pressed="true">⏱ Ao vivo: ATIVADO</button><button type="button" class="secondary" id="queue-clear">Limpar finalizados</button></div>
<div id="queue-feedback" class="status" role="status" style="margin-top:12px">Pronto para receber links.</div>
<div id="queue-summary" class="queue-summary">Nenhum link na fila.</div>
<div id="queue-list" class="results" aria-live="polite"></div>
</section>

<section class="card"><div class="section-head"><div class="section-icon">⌕</div><div><h2>Pesquisar no YouTube</h2><p>Encontre um vídeo ou canal pelo nome.</p></div></div><form id="yt"><div class="field"><label for="term">NOME DO VÍDEO OU CANAL</label><input id="term" required placeholder="Ex.: nome do vídeo ou criador"></div><button class="secondary">Pesquisar no YouTube</button></form><div id="ytout" class="results"></div></section>


<section class="card"><div class="section-head"><div class="section-icon">🌐</div><div><h2>Navegador MEDIABOX</h2><p>Pesquise no Google, faça perguntas e abra páginas sem sair da central.</p></div></div><a class="primary" href="/navegador" style="display:block;text-align:center;text-decoration:none;padding:15px;border-radius:14px">🌐 Abrir navegador interno</a></section>

<p class="small legal">A disponibilidade dos downloads depende das regras, permissões e mudanças de cada plataforma. Salve apenas conteúdos seus ou que você tenha autorização para baixar.</p>
<footer class="footer"><strong>MEDIABOX</strong><div>Construído por Davi Dias</div><div>Todos os direitos reservados.</div></footer>

<div id="mb-local-editor" hidden role="dialog" aria-modal="true" aria-labelledby="mb-editor-title">
  <section class="mb-editor-panel">
    <h2 id="mb-editor-title">Editor temporário MEDIABOX</h2>
    <p>HTML e CSS são aplicados temporariamente nesta página. A área Python valida a sintaxe do código, mas não o executa nem altera os arquivos do servidor.</p>
    <div class="mb-auto-run">
      <div class="mb-auto-run-head"><strong>EXECUÇÃO AUTOMÁTICA</strong><span>ESCOLHA UM MODO</span></div>
      <div class="mb-visual-grid">
        <button type="button" data-mb-preset="dimensao404">DIMENSÃO 404 · HTML + CSS</button>
        <button type="button" data-mb-preset="cyber">CYBERPUNK NEON</button>
        <button type="button" data-mb-preset="matrix">MATRIX VERDE</button>
        <button type="button" data-mb-preset="vaporwave">VAPORWAVE</button>
        <button type="button" data-mb-preset="hologram">HOLOGRAMA</button>
        <button type="button" data-mb-preset="glitch">GLITCH RGB</button>
        <button type="button" data-mb-preset="terminal">TERMINAL HACKER</button>
        <button type="button" data-mb-preset="aurora">AURORA</button>
        <button type="button" data-mb-preset="gold">GOLD PREMIUM</button>
        <button type="button" data-mb-preset="ocean">OCEANO PROFUNDO</button>
        <button type="button" id="mb-preset-reset" class="mb-editor-close">DESATIVAR MODOS</button>
      </div>
      <p class="mb-auto-run-note">Os modos ficam disponíveis aqui no MEDIABOX. Toque em um nome para executar; use Restaurar página para desfazer todas as alterações temporárias.</p>
    </div>
    <label for="mb-editor-selector">SELETOR DO ELEMENTO HTML</label>
    <input id="mb-editor-selector" value=".hero h1" placeholder="Ex.: .hero h1 ou .hero p">
    <label for="mb-editor-html">HTML DO ELEMENTO</label>
    <textarea id="mb-editor-html" spellcheck="false" placeholder="Digite o HTML interno do elemento selecionado"></textarea>
    <label for="mb-editor-css">CSS TEMPORÁRIO</label>
    <textarea id="mb-editor-css" spellcheck="false" placeholder="Ex.: .hero h1 { color: cyan; }"></textarea>
    <label for="mb-editor-python">PYTHON — CÓDIGO</label>
    <textarea id="mb-editor-python" spellcheck="false" placeholder="Ex.: print(mensagem)"></textarea>
    <p class="mb-auto-run-note">Validação Python: verifica erros de sintaxe e informa a linha. O código não é executado.</p>
    <div class="mb-editor-actions"><button type="button" class="secondary" id="mb-editor-python-validate">✓ Validar sintaxe Python</button></div>
    <div class="mb-editor-actions">
      <button type="button" class="primary" id="mb-editor-apply">Aplicar alterações</button>
      <button type="button" class="secondary" id="mb-editor-reset">Restaurar página</button>
      <button type="button" class="mb-editor-close" id="mb-editor-close">Fechar</button>
    </div>
    <div id="mb-editor-status" class="mb-editor-status" role="status">Aguardando edição.</div>
  </section>
</div>
<div id="watermark-preview" hidden class="preview-backdrop" role="dialog" aria-modal="true"><div class="preview-dialog"><div class="preview-title"><strong>Prévia real + marca d'água</strong><button type="button" class="preview-close" id="close-watermark-preview">✕</button></div><p class="small">Assista ao vídeo e arraste a marca para qualquer ponto. A posição será usada no arquivo final.</p><div id="preview-video" class="preview-video pos-inferior_direito"><video id="preview-source" controls playsinline preload="metadata"></video><img class="preview-logo" id="preview-logo" src="/marca-dagua-preview?tipo=dias_edits" alt="Marca d'água Dias Edits" draggable="false"></div><p class="small" id="preview-video-title">Nenhum vídeo carregado.</p><p class="small">Posição: <strong id="preview-position-label">Inferior direito</strong></p></div></div>
</main><script>


const $=x=>document.getElementById(x);let timer=null;

// Downloads no próprio app: busca o arquivo sem navegar para /arquivo/ no WebView.
document.addEventListener("click",async evento=>{
 const link=evento.target instanceof Element?evento.target.closest("#download-link,.queue-link"):null;
 if(!link)return;
 evento.preventDefault();
 if(link.dataset.mbDownloadBusy==="1")return;
 const href=link.getAttribute("href");
 if(!href||href==="#")return;
 const textoOriginal=link.textContent;
 link.dataset.mbDownloadBusy="1";
 link.setAttribute("aria-busy","true");
 link.textContent="Preparando arquivo...";
 try{
  const resposta=await fetch(href,{method:"GET",credentials:"same-origin",cache:"no-store"});
  if(!resposta.ok){
   let detalhe="";
   try{const dados=await resposta.json();detalhe=dados.erro||dados.detail||""}catch(_){}
   throw new Error(detalhe||"O servidor respondeu HTTP "+resposta.status+".");
  }
  const arquivo=await resposta.blob();
  if(!arquivo.size)throw new Error("O servidor retornou um arquivo vazio.");
  const disposicao=resposta.headers.get("Content-Disposition")||"";
  let nome="";
  const utf8=disposicao.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if(utf8){try{nome=decodeURIComponent(utf8[1].trim().replace(/^"|"$/g,""))}catch(_){}}
  if(!nome){
   const simples=disposicao.match(/filename\s*=\s*"?([^";]+)"?/i);
   if(simples)nome=simples[1].trim();
  }
  if(!nome){
   const caminho=new URL(href,window.location.href).pathname;
   nome=decodeURIComponent(caminho.substring(caminho.lastIndexOf("/")+1)||"video.mp4");
  }
  const objetoUrl=URL.createObjectURL(arquivo);
  const salvar=document.createElement("a");
  salvar.href=objetoUrl;
  salvar.download=nome;
  salvar.style.display="none";
  document.body.appendChild(salvar);
  salvar.click();
  salvar.remove();
  window.setTimeout(()=>URL.revokeObjectURL(objetoUrl),60000);
  if($("status"))$("status").textContent="Arquivo preparado para salvar no dispositivo.";
  if($("queue-feedback")&&link.classList.contains("queue-link"))$("queue-feedback").textContent="Arquivo preparado para salvar no dispositivo.";
 }catch(erro){
  if($("err")&&link.id==="download-link")$("err").textContent=erro.message||"Falha ao preparar o download.";
  if(typeof mostrarErroCentral==="function")mostrarErroCentral(erro.message||"Falha ao preparar o download.",{codigo:"DOWNLOAD-INAPP-001",origem:"Download dentro do MEDIABOX"});
 }finally{
  delete link.dataset.mbDownloadBusy;
  link.removeAttribute("aria-busy");
  if(link.isConnected)link.textContent=textoOriginal;
 }
});
(function iniciarMEDIABOXApp(){
 const main=document.querySelector("main");
 const top=main&&main.querySelector(".topline");
 if(!main||!top||document.getElementById("bv-nav"))return;
 const hero=main.querySelector(".hero");
 const download=main.querySelector("#form")?.closest(".card");
 const queue=main.querySelector("#download-queue");
 const search=main.querySelector("#yt")?.closest(".card");
 const browser=Array.from(main.querySelectorAll(".card")).find(el=>(el.querySelector("h2")?.textContent||"").toLowerCase().includes("navegador"));
 const maths=main.querySelector('a[href="/matematica"]');
 const legal=main.querySelector(".legal");
 const footer=main.querySelector(".footer");
 const accountLinks=main.querySelector('a[href="/conquistas"]')?.closest("section");
 const accountControls=top.querySelector(":scope > div:last-child");
 const pagesRoot=document.createElement("div");
 pagesRoot.id="bv-pages";
 pagesRoot.className="bv-pages";
 top.insertAdjacentElement("afterend",pagesRoot);
 const pages={};
 function addPage(id,label,nodes){
  const page=document.createElement("section");
  page.className="bv-page";
  page.id="bv-page-"+id;
  page.dataset.bvPage=id;
  page.setAttribute("aria-label",label);
  page.hidden=true;
  if(id!=="inicio"){
   const heading=document.createElement("header");
   heading.className="bv-screen-heading";
   const copy=document.createElement("div");
   const eyebrow=document.createElement("span");eyebrow.textContent="MEDIABOX · APLICATIVO";
   const title=document.createElement("h1");title.textContent=label;
   copy.append(eyebrow,title);
   const icon=document.createElement("div");icon.className="bv-screen-heading-icon";icon.setAttribute("aria-hidden","true");
   icon.textContent=({baixar:"↓",fila:"☷",buscar:"⌕",ferramentas:"✦",conta:"♙"}[id]||"•");
   heading.append(copy,icon);page.appendChild(heading);
  }
  pagesRoot.appendChild(page);
  (nodes||[]).filter(Boolean).forEach(node=>page.appendChild(node));
  pages[id]=page;
 }
 const dashboard=document.createElement("section");
 dashboard.className="bv-dashboard bv-home-dashboard";
 dashboard.innerHTML='<div class="bv-home-topline"><span>MEDIABOX · VIDEO STUDIO</span><span class="bv-home-live"><i></i> CENTRAL DE VÍDEOS</span></div><div class="bv-home-art" aria-hidden="true"><span class="bv-home-art-ring"></span><span class="bv-home-play">▶</span><span class="bv-home-art-label">BV</span></div><div class="bv-home-copy"><div class="bv-home-kicker">SEU ESPAÇO DIGITAL</div><h2>Seu conteúdo.<br><span>Em movimento.</span></h2><p>Baixe vídeos, organize sua fila e encontre conteúdos em uma experiência feita para o celular.</p></div><button class="bv-home-main-action" type="button" data-bv-go="baixar"><span class="bv-home-main-icon">↓</span><span><strong>Começar download</strong><small>YouTube, Instagram, TikTok e mais</small></span><b>→</b></button><div class="bv-home-section-label">ACESSO RÁPIDO</div><div class="bv-home-shortcuts"><button class="bv-home-shortcut" type="button" data-bv-go="fila"><span class="bv-home-shortcut-icon">☷</span><span><strong>Minha fila</strong><small>Acompanhe seus vídeos</small></span><b>›</b></button><button class="bv-home-shortcut" type="button" data-bv-go="buscar"><span class="bv-home-shortcut-icon">⌕</span><span><strong>Pesquisar</strong><small>Vídeos e canais</small></span><b>›</b></button><button class="bv-home-shortcut" type="button" data-bv-go="ferramentas"><span class="bv-home-shortcut-icon">✦</span><span><strong>Ferramentas</strong><small>Mais recursos do MEDIABOX</small></span><b>›</b></button></div><div class="bv-home-footnote"><span>MEDIABOX</span><span>VIDEO HUB</span></div>';
 if(hero)hero.remove();
 addPage("inicio","Início",[dashboard]);
 addPage("baixar","Baixar vídeo",[download]);
 addPage("fila","Fila de downloads",[queue]);
 addPage("buscar","Pesquisar",[search]);
 const toolsIntro=document.createElement("section");
 toolsIntro.className="bv-dashboard";
 toolsIntro.innerHTML='<div class="bv-dashboard-kicker">MEDIABOX · CENTRAL DE RECURSOS</div><h2>Ferramentas</h2><p>Todos os recursos extras ficam reunidos aqui, sem ocupar as outras abas.</p>';
 const settingsButton=document.createElement("div");
 settingsButton.className="bv-tool-settings";
 settingsButton.appendChild(document.createElement("div"));
 settingsButton.firstElementChild.innerHTML='<strong>Personalização e comandos</strong><p>Animações, efeitos e comandos especiais do MEDIABOX.</p>';
 const settingsAction=document.createElement("button");
 settingsAction.type="button";settingsAction.className="primary";settingsAction.id="mb-settings-open";settingsAction.textContent="⚙ Configurações e comandos especiais";
 settingsButton.appendChild(settingsAction);
 const accountIntro=document.createElement("section");
 accountIntro.className="bv-dashboard";
 accountIntro.innerHTML='<div class="bv-dashboard-kicker">MEDIABOX · ÁREA PESSOAL</div><h2>Conta e progresso</h2><p>Acompanhe suas conquistas e abra a prévia avançada dos seus arquivos neste dispositivo.</p>';
 if(accountLinks)accountLinks.classList.add("bv-account-links");
 const equalizerLink=document.createElement("section");
 equalizerLink.className="card";
 equalizerLink.innerHTML='<div class="section-head"><div class="section-icon">🎚️</div><div><h2>Equalizador de áudio</h2><p>Equalizador PRO de 10 bandas, prévia e exportação.</p></div></div><a class="primary" href="/ferramentas/equalizador" style="display:block;text-align:center;text-decoration:none;padding:15px;border-radius:14px">Abrir Equalizador PRO</a>';
 addPage("ferramentas","Ferramentas",[toolsIntro,maths,browser,equalizerLink,legal,footer]);
 if(accountControls)accountControls.classList.add("bv-account-actions");
 addPage("conta","Conta e conquistas",[accountIntro,settingsButton,accountControls,accountLinks]);
 const reviewCard=document.createElement("section");reviewCard.className="card";reviewCard.id="bv-reviews";
 reviewCard.innerHTML='<div class="section-head"><div class="section-icon">★</div><div><h2>Avaliações do MEDIABOX</h2><p>Nota e opiniões reais de quem usa o app.</p></div></div><div class="bv-rating-overview"><div class="bv-rating-score"><strong id="bv-rating-average">0,0</strong><div id="bv-rating-stars">☆☆☆☆☆</div><small id="bv-rating-count">0 avaliações</small></div><div id="bv-rating-bars"></div></div><form id="bv-review-form" enctype="multipart/form-data"><h3>Deixe sua avaliação</h3><label>Nome público<input name="nome" maxlength="40" placeholder="Como seu nome aparecerá"></label><span>Sua nota</span><div class="bv-star-picker"><button type="button" data-review-star="1">★</button><button type="button" data-review-star="2">★</button><button type="button" data-review-star="3">★</button><button type="button" data-review-star="4">★</button><button type="button" data-review-star="5">★</button></div><input type="hidden" name="estrelas" id="bv-review-stars" value="0"><label>Comentário<textarea name="comentario" maxlength="600" rows="4" placeholder="Conte o que achou do MEDIABOX..."></textarea></label><label>Foto opcional (JPG, PNG ou WEBP até 1 MB)<input name="foto" type="file" accept="image/jpeg,image/png,image/webp"></label><button class="primary" type="submit" id="bv-review-submit">Publicar avaliação</button><p id="bv-review-status" role="status">Você pode atualizar sua avaliação quando quiser.</p></form><h3>Opiniões recentes · AO VIVO</h3><div id="bv-review-list">Carregando avaliações...</div>';
 const reviewCss=document.createElement("style");reviewCss.textContent='.bv-rating-overview{display:grid;grid-template-columns:100px 1fr;gap:12px;padding:14px;border:1px solid #344b68;border-radius:16px;background:#111e30}.bv-rating-score{text-align:center}.bv-rating-score strong{display:block;font-size:40px}.bv-rating-stars,.bv-review-item-stars{color:#ffc85a;letter-spacing:2px}.bv-rating-row{display:grid;grid-template-columns:22px 1fr 20px;gap:6px;align-items:center;margin:6px 0;font-size:10px}.bv-rating-track{height:7px;background:#26364e;border-radius:20px;overflow:hidden}.bv-rating-fill{height:100%;background:#ffc85a}#bv-review-form{display:grid;gap:9px;margin:15px 0;padding:14px;border:1px solid #30435f;border-radius:16px;background:#0d1727}#bv-review-form h3{margin:0}#bv-review-form label{display:grid;gap:6px;color:#bdcce1;font-size:11px}#bv-review-form input:not([type=file]),#bv-review-form textarea{width:100%;box-sizing:border-box;padding:11px;border:1px solid #344b68;border-radius:11px;background:#080f1c;color:white;font:inherit}.bv-star-picker{display:flex;gap:6px}.bv-star-picker button{width:42px;min-height:42px;margin:0;padding:0;background:#152238;border:1px solid #394d68;color:#65758c;font-size:24px}.bv-star-picker button[aria-pressed=true]{color:#ffc85a;border-color:#b68c42}#bv-review-form button[type=submit]{width:100%}#bv-review-status{margin:0;font-size:11px;color:#9cb1cb}.bv-review-item{padding:12px;margin:8px 0;border:1px solid #2d405b;border-radius:14px;background:#101c2d}.bv-review-person{display:flex;align-items:center;gap:9px}.bv-review-avatar{width:38px;height:38px;flex:0 0 38px;display:grid;place-items:center;border-radius:50%;background:#24415b;overflow:hidden}.bv-review-avatar img{width:100%;height:100%;object-fit:cover}.bv-review-person strong,.bv-review-person small{display:block}.bv-review-person small{color:#8799b5;font-size:9px}.bv-review-item-stars{margin-left:auto}.bv-review-comment{font-size:12px;line-height:1.5;overflow-wrap:anywhere}';document.head.appendChild(reviewCss);accountIntro.insertAdjacentElement("afterend",reviewCard);
 let pickedStars=0,busyReviews=false;
 function chooseStars(n){pickedStars=n;$("bv-review-stars").value=String(n);document.querySelectorAll("[data-review-star]").forEach(b=>b.setAttribute("aria-pressed",String(Number(b.dataset.reviewStar)<=n)));}
 document.querySelectorAll("[data-review-star]").forEach(b=>b.addEventListener("click",()=>chooseStars(Number(b.dataset.reviewStar))));
 function renderReviews(data){$("bv-rating-average").textContent=data.media_formatada||"0,0";$("bv-rating-count").textContent=(data.total||0)+" avaliações";$("bv-rating-stars").textContent="★".repeat(Math.round(data.media||0))+"☆".repeat(5-Math.round(data.media||0));const bars=$("bv-rating-bars");bars.replaceChildren();for(let n=5;n>=1;n--){const count=Number(data.distribuicao?.[String(n)]||0),row=document.createElement("div");row.className="bv-rating-row";const label=document.createElement("span");label.textContent=n+"★";const track=document.createElement("div");track.className="bv-rating-track";const fill=document.createElement("div");fill.className="bv-rating-fill";fill.style.width=(data.total?count/data.total*100:0)+"%";track.appendChild(fill);const qty=document.createElement("b");qty.textContent=String(count);row.append(label,track,qty);bars.appendChild(row);}const list=$("bv-review-list");list.replaceChildren();if(!data.avaliacoes.length){list.textContent="Ainda não há avaliações. Seja a primeira pessoa!";return;}data.avaliacoes.forEach(item=>{const card=document.createElement("article");card.className="bv-review-item";const head=document.createElement("div");head.className="bv-review-person";const avatar=document.createElement("div");avatar.className="bv-review-avatar";if(item.tem_foto){const img=document.createElement("img");img.src="/avaliacoes/foto/"+item.id;img.alt="Foto de "+item.nome;avatar.appendChild(img);}else avatar.textContent=(item.nome||"?").slice(0,1).toUpperCase();const who=document.createElement("div"),name=document.createElement("strong"),date=document.createElement("small");name.textContent=item.nome;date.textContent=item.data||"";who.append(name,date);const stars=document.createElement("span");stars.className="bv-review-item-stars";stars.textContent="★".repeat(item.estrelas)+"☆".repeat(5-item.estrelas);head.append(avatar,who,stars);card.appendChild(head);if(item.comentario){const p=document.createElement("p");p.className="bv-review-comment";p.textContent=item.comentario;card.appendChild(p);}list.appendChild(card);});}
 async function refreshReviews(){if(busyReviews)return;busyReviews=true;try{const res=await fetch("/avaliacoes/resumo",{cache:"no-store"});if(!res.ok)throw Error("Não foi possível carregar as avaliações.");renderReviews(await res.json());}catch(e){$("bv-review-list").textContent=e.message;}finally{busyReviews=false;}}
 $("bv-review-form").addEventListener("submit",async e=>{e.preventDefault();if(!pickedStars){$("bv-review-status").textContent="Escolha de 1 a 5 estrelas.";return;}const btn=$("bv-review-submit");btn.disabled=true;$("bv-review-status").textContent="Publicando...";try{const res=await fetch("/avaliacoes/enviar",{method:"POST",body:new FormData($("bv-review-form"))});const data=await res.json();if(!res.ok)throw Error(data.erro||"Falha ao publicar.");$("bv-review-status").textContent="🟢 VERDE — "+data.mensagem;renderReviews(data.resumo);$("bv-review-form").reset();chooseStars(0);}catch(err){$("bv-review-status").textContent="❌ DEU RED! "+err.message;}finally{btn.disabled=false;}});
 refreshReviews();window.setInterval(refreshReviews,15000);
 const nav=document.createElement("nav");
 nav.id="bv-nav";
 nav.setAttribute("aria-label","Navegação principal");
 nav.innerHTML='<button type="button" data-bv-view="inicio"><span class="bv-nav-icon">⌂</span><span>Início</span></button><button type="button" data-bv-view="baixar"><span class="bv-nav-icon">↓</span><span>Baixar</span></button><button type="button" data-bv-view="fila"><span class="bv-nav-icon">☷</span><span>Fila</span></button><button type="button" data-bv-view="buscar"><span class="bv-nav-icon">⌕</span><span>Buscar</span></button><button type="button" data-bv-view="ferramentas"><span class="bv-nav-icon">✦</span><span>Ferramentas</span></button>';
 const appbarActions=document.createElement("div");appbarActions.className="bv-appbar-actions";
 const installButton=document.createElement("button");installButton.type="button";installButton.id="bv-install-app";installButton.className="bv-appbar-button bv-install-button";installButton.setAttribute("aria-label","Instalar MEDIABOX");installButton.title="Instalar MEDIABOX";installButton.textContent="⇩";installButton.hidden=true;
 const accountButton=document.createElement("button");accountButton.type="button";accountButton.className="bv-appbar-button";accountButton.id="bv-account-open";accountButton.setAttribute("aria-label","Abrir conta");accountButton.title="Conta";accountButton.textContent="♙";
 appbarActions.append(installButton,accountButton);top.appendChild(appbarActions);document.body.appendChild(nav);
 accountButton.addEventListener("click",()=>mudarTela("conta","push"));
 let installPromptEvent=null;
 window.addEventListener("beforeinstallprompt",event=>{event.preventDefault();installPromptEvent=event;installButton.hidden=false;});
 installButton.addEventListener("click",async()=>{if(!installPromptEvent)return;installPromptEvent.prompt();await installPromptEvent.userChoice;installPromptEvent=null;installButton.hidden=true;});
 window.addEventListener("appinstalled",()=>{installPromptEvent=null;installButton.hidden=true;});
 const standalone=window.matchMedia("(display-mode:standalone)").matches||window.navigator.standalone===true;
 if(standalone)document.body.classList.add("bv-is-standalone");
 if("serviceWorker" in navigator&&location.protocol==="https:")navigator.serviceWorker.register("/service-worker.js",{scope:"/"}).catch(error=>console.warn("MEDIABOX PWA:",error));
 function mudarTela(id,historyMode){
  if(!pages[id])id="inicio";
  Object.keys(pages).forEach(key=>{pages[key].hidden=key!==id;});
  nav.querySelectorAll("[data-bv-view]").forEach(btn=>{
   if(btn.dataset.bvView===id)btn.setAttribute("aria-current","page");
   else btn.removeAttribute("aria-current");
  });
  if(historyMode==="push")window.history.pushState({blueVisionView:id},"","#bv-"+id);
  else if(historyMode==="replace")window.history.replaceState({blueVisionView:id},"","#bv-"+id);
  window.scrollTo({top:0,behavior:"smooth"});
 }
 nav.addEventListener("click",event=>{
  const btn=event.target.closest("[data-bv-view]");
  if(btn)mudarTela(btn.dataset.bvView,"push");
 });
 main.querySelectorAll("[data-bv-go]").forEach(btn=>btn.addEventListener("click",()=>mudarTela(btn.dataset.bvGo,"push")));
 window.addEventListener("popstate",()=>mudarTela((location.hash||"").replace("#bv-",""),"none"));
 const inicio=(location.hash||"").startsWith("#bv-")?location.hash.slice(4):"inicio";
 mudarTela(inicio,"replace");
})();

const MB_ERROR_ANIMATIONS={scanner:"Scanner holográfico",aurora:"Aurora suave",ripple:"Ondas de luz",shimmer:"Reflexo premium",orbit:"Órbita azul",minimal:"Entrada limpa"};
let mbErrorAnimation="scanner";
try{const salvo=localStorage.getItem("mediabox_error_animation");if(Object.prototype.hasOwnProperty.call(MB_ERROR_ANIMATIONS,salvo))mbErrorAnimation=salvo;}catch(_){}
function mbAplicarEstiloErro(animacao,salvar=true){
if(!Object.prototype.hasOwnProperty.call(MB_ERROR_ANIMATIONS,animacao))animacao="scanner";
mbErrorAnimation=animacao;document.body.setAttribute("data-error-animation",animacao);
document.querySelectorAll("[data-error-choice]").forEach(botao=>botao.setAttribute("aria-pressed",String(botao.dataset.errorChoice===animacao)));
const status=$("mb-error-setting-status");if(status)status.textContent="Animação atual: "+MB_ERROR_ANIMATIONS[animacao]+".";
if(salvar){try{localStorage.setItem("mediabox_error_animation",animacao);}catch(_){}}
}

let mbConfiguracaoAba="errors";
let mbFocoAntesConfiguracao=null;
function mbAbrirAbaConfiguracao(aba){
 const erros=aba!=="commands";
 mbConfiguracaoAba=erros?"errors":"commands";
 $("mb-settings-errors").hidden=!erros;$("mb-settings-commands").hidden=erros;
 $("mb-tab-errors").setAttribute("aria-selected",String(erros));
 $("mb-tab-commands").setAttribute("aria-selected",String(!erros));
}
function mbAbrirConfiguracoes(){
 mbFocoAntesConfiguracao=document.activeElement;
 $("mb-settings").hidden=false;document.body.classList.add("mb-overlay-open");
 mbAbrirAbaConfiguracao(mbConfiguracaoAba);
 $("mb-settings-close").focus();
}
function mbFecharConfiguracoes(){
 $("mb-settings").hidden=true;
 if($("mb-birthday").hidden&&$("mb-command-surprise").hidden)document.body.classList.remove("mb-overlay-open");
 if(mbFocoAntesConfiguracao&&mbFocoAntesConfiguracao.isConnected)mbFocoAntesConfiguracao.focus();
}
function mbPrevisualizarErro(){
 mbAplicarEstiloErro(mbErrorAnimation);
 $("error-intro").hidden=true;$("error-symbol").textContent="⚠";
 $("error-title").textContent="Prévia do efeito";
 $("error-message").textContent="Este é apenas um exemplo visual. Nenhum erro real foi provocado.";
 $("error-category").textContent="Demonstração";$("error-code").textContent="MB-PREVIEW";
 $("error-http").textContent="Não aplicável";$("error-time").textContent=new Date().toLocaleString("pt-BR");
 $("error-technical").textContent="MEDIABOX — PRÉ-VISUALIZAÇÃO\nEfeito: "+MB_ERROR_ANIMATIONS[mbErrorAnimation]+"\nNenhuma operação foi executada.";
 $("mb-settings").hidden=true;$("error-center").hidden=false;
}
function mbFecharComandoSurpresa(){
 $("mb-command-surprise").hidden=true;
 if($("mb-settings").hidden&&$("mb-birthday").hidden)document.body.classList.remove("mb-overlay-open");
}
function mbMostrarComando(titulo,mensagem,simbolo,tema){
 $("mb-command-title").textContent=titulo;$("mb-command-message").textContent=mensagem;
 $("mb-command-symbol").textContent=simbolo;
 $("mb-command-surprise").querySelector(".mb-command-card").dataset.theme=tema||"celebration";
 $("mb-settings").hidden=true;$("mb-command-surprise").hidden=false;
 document.body.classList.add("mb-overlay-open");
}
function mbExecutarComando(){
 const campo=$("mb-command-input"),comando=campo.value.trim().toUpperCase(),status=$("mb-command-status");
 if(!comando){status.textContent="Digite ou escolha um código primeiro.";return;}
 if(comando==="1234"){
  status.textContent="Código reconhecido. Abrindo a surpresa ND... 🟢 VERDE";
  $("mb-settings").hidden=true;$("mb-birthday").hidden=false;document.body.classList.add("mb-overlay-open");return;
 }
 if(comando==="0000"){
  mbAplicarEstiloErro("aurora");status.textContent="🟢 VERDE — Efeito padrão restaurado.";
  campo.value="";return;
 }
 const especiais={
  "7777":["Chuva de estrelas","Você desbloqueou uma pequena celebração visual do MEDIABOX. ✨","✦","celebration"],
  "4040":["Portal 404","Você encontrou uma passagem secreta. Nada foi alterado na sua página.","404","portal"],
  "2026":["MEDIABOX · EDIÇÃO 2026","Obrigado por explorar as ferramentas e os detalhes escondidos do Video Hub. 💚","MB","future"]
 };
 const item=especiais[comando];
 if(!item){status.textContent="Código não reconhecido. Escolha um dos atalhos exibidos.";return;}
 status.textContent="Comando reconhecido. 🟢 VERDE";
 mbMostrarComando(item[0],item[1],item[2],item[3]);campo.value="";
}
$("mb-settings-open").addEventListener("click",mbAbrirConfiguracoes);
$("mb-settings-close").addEventListener("click",mbFecharConfiguracoes);
$("mb-settings").addEventListener("click",e=>{if(e.target===$("mb-settings"))mbFecharConfiguracoes();});
$("mb-tab-errors").addEventListener("click",()=>mbAbrirAbaConfiguracao("errors"));
$("mb-tab-commands").addEventListener("click",()=>mbAbrirAbaConfiguracao("commands"));
$("mb-error-preview").addEventListener("click",mbPrevisualizarErro);
$("mb-error-reset").addEventListener("click",()=>mbAplicarEstiloErro("scanner"));
document.querySelectorAll("[data-error-choice]").forEach(botao=>botao.addEventListener("click",()=>mbAplicarEstiloErro(botao.dataset.errorChoice)));
$("mb-command-run").addEventListener("click",mbExecutarComando);
$("mb-command-input").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();mbExecutarComando();}});
document.querySelectorAll("[data-command-pick]").forEach(botao=>botao.addEventListener("click",()=>{
 $("mb-command-input").value=botao.dataset.commandPick;mbExecutarComando();
}));
$("mb-birthday-close").addEventListener("click",()=>{$("mb-birthday").hidden=true;if($("mb-settings").hidden&&$("mb-command-surprise").hidden)document.body.classList.remove("mb-overlay-open");});
$("mb-command-surprise-close").addEventListener("click",mbFecharComandoSurpresa);
$("mb-command-surprise").addEventListener("click",e=>{if(e.target===$("mb-command-surprise"))mbFecharComandoSurpresa();});
$("mb-birthday").addEventListener("click",e=>{if(e.target===$("mb-birthday")){$("mb-birthday").hidden=true;document.body.classList.remove("mb-overlay-open");}});
document.addEventListener("keydown",e=>{
 if(e.key==="Escape"){
  if(!$("mb-command-surprise").hidden)mbFecharComandoSurpresa();
  else if(!$("mb-birthday").hidden){$("mb-birthday").hidden=true;document.body.classList.remove("mb-overlay-open");}
  else if(!$("mb-settings").hidden)mbFecharConfiguracoes();
  else if(!$("error-center").hidden)fecharErroCentral();
 }
});
mbAplicarEstiloErro(mbErrorAnimation,false);


const mbEditorState={
  htmlTarget:null,htmlOriginal:"",styleNode:null,taps:0,tapTimer:null,
  visualStyleNode:null,chaosTimer:null,scrollSnapshot:null
};

const mbVisualPresets={
  dimensao404:{
    nome:"DIMENSÃO 404 · HTML + CSS",
    html:true,
    css:[
      'body[data-mb-visual-mode="dimensao404"]{background:radial-gradient(circle at 20% 20%,#6500ff55,transparent 40%),radial-gradient(circle at 80% 80%,#ff00cc44,transparent 40%),#050008;color:#00fff2}',
      'body[data-mb-visual-mode="dimensao404"] .hero,body[data-mb-visual-mode="dimensao404"] .card{border-color:#ff00e6;background:linear-gradient(145deg,#10051df2,#080012f2);box-shadow:0 0 22px #ff00e655}',
      'body[data-mb-visual-mode="dimensao404"] .hero h1,body[data-mb-visual-mode="dimensao404"] h2{color:#ff00e6;text-shadow:3px 0 #00fff2,-3px 0 #ff003c,0 0 18px #ff00e6}',
      'body[data-mb-visual-mode="dimensao404"] .section-icon{color:#00fff2;border-color:#ff00e6;background:#170822}',
      'body[data-mb-visual-mode="dimensao404"] .topline,body[data-mb-visual-mode="dimensao404"] .pill{border-color:#ff00e6;background:#10051d}',
      '#mb-chaos-stage{width:100%;margin:0 0 20px;padding:24px;border:2px solid #ff00e6;border-radius:18px;background:#10051df2;color:#00fff2;text-align:center;box-shadow:0 0 30px #ff00e688,inset 0 0 25px #00fff233}',
      '#mb-chaos-stage .mb-chaos-kicker{font-size:10px;letter-spacing:2px;color:#00fff2}',
      '#mb-chaos-stage h2{margin:12px 0;color:#ff00e6;font-size:clamp(29px,8vw,48px);text-shadow:3px 3px #00fff2;animation:mbChaosGlitch .18s infinite}',
      '#mb-chaos-stage p{color:#fff;line-height:1.7}',
      '#mb-chaos-stage button{max-width:260px;background:linear-gradient(90deg,#ff00e6,#6500ff,#00fff2);color:white;border:0;box-shadow:0 0 20px #ff00e6}',
      '#mb-chaos-terminal{min-height:105px;margin:18px 0 0;padding:14px;border:1px solid #00ff66;background:#000;color:#00ff66;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 monospace}',
      '@keyframes mbChaosGlitch{0%{transform:translate(0)}25%{transform:translate(2px,-1px)}50%{transform:translate(-2px,1px)}75%{transform:translate(1px,2px)}100%{transform:translate(0)}}'
    ].join("\n")
  },
  cyber:{
    nome:"CYBERPUNK NEON",
    css:[
      'body[data-mb-visual-mode="cyber"]{background:radial-gradient(circle at 15% 15%,#6500ff44,transparent 38%),radial-gradient(circle at 85% 85%,#ff00cc33,transparent 38%),#050008;color:#00fff2}',
      'body[data-mb-visual-mode="cyber"] .hero,body[data-mb-visual-mode="cyber"] .card,body[data-mb-visual-mode="cyber"] .item{border:1px solid #00fff2;background:#10051deF;box-shadow:0 0 18px #00fff255,0 0 28px #ff00e633}',
      'body[data-mb-visual-mode="cyber"] h1,body[data-mb-visual-mode="cyber"] h2,body[data-mb-visual-mode="cyber"] h3{color:#ff00e6;text-shadow:3px 0 #00fff2,-2px 0 #ff003c,0 0 18px #ff00e6}',
      'body[data-mb-visual-mode="cyber"] button{background:linear-gradient(90deg,#ff00e6,#6500ff,#00fff2);color:#fff;border:0;box-shadow:0 0 18px #ff00e6}',
      'body[data-mb-visual-mode="cyber"] input,body[data-mb-visual-mode="cyber"] select,body[data-mb-visual-mode="cyber"] textarea{background:#080012;color:#00fff2;border:1px solid #ff00e6}',
      'body[data-mb-visual-mode="cyber"] .topline,body[data-mb-visual-mode="cyber"] .pill{border-color:#ff00e6;background:#10051d}',
      'body[data-mb-visual-mode="cyber"] ::selection{background:#ff00e6;color:#fff}'
    ].join("\n")
  },
  matrix:{
    nome:"MATRIX VERDE",
    css:[
      'body[data-mb-visual-mode="matrix"]{background:radial-gradient(ellipse at top,#063d20,#020805 65%);color:#a8ffb8;font-family:ui-monospace,monospace}',
      'body[data-mb-visual-mode="matrix"] .hero,body[data-mb-visual-mode="matrix"] .card,body[data-mb-visual-mode="matrix"] .item{background:#03140beF;border:1px solid #00ff66;box-shadow:0 0 16px #00ff6633}',
      'body[data-mb-visual-mode="matrix"] h1,body[data-mb-visual-mode="matrix"] h2,body[data-mb-visual-mode="matrix"] h3{color:#00ff66;text-shadow:0 0 12px #00ff66}',
      'body[data-mb-visual-mode="matrix"] button{background:#063d20;color:#adffbd;border:1px solid #00ff66}',
      'body[data-mb-visual-mode="matrix"] input,body[data-mb-visual-mode="matrix"] select,body[data-mb-visual-mode="matrix"] textarea{background:#020805;color:#9dffae;border-color:#00a843}',
      'body[data-mb-visual-mode="matrix"] .section-icon{color:#00ff66;border-color:#00a843;background:#041b0c}'
    ].join("\n")
  },
  vaporwave:{
    nome:"VAPORWAVE",
    css:[
      'body[data-mb-visual-mode="vaporwave"]{background:linear-gradient(145deg,#18052d,#29104d 48%,#100c30);color:#ffe5ff}',
      'body[data-mb-visual-mode="vaporwave"] .hero,body[data-mb-visual-mode="vaporwave"] .card,body[data-mb-visual-mode="vaporwave"] .item{background:linear-gradient(135deg,#321052dd,#17113beF);border:1px solid #ff79df;box-shadow:0 0 22px #ff39cc33}',
      'body[data-mb-visual-mode="vaporwave"] h1,body[data-mb-visual-mode="vaporwave"] h2,body[data-mb-visual-mode="vaporwave"] h3{color:#ff9be9;text-shadow:0 0 15px #ff00cc}',
      'body[data-mb-visual-mode="vaporwave"] button{background:linear-gradient(110deg,#ff4fd8,#9b5cff,#43d9ff);color:#160622;border:0}',
      'body[data-mb-visual-mode="vaporwave"] input,body[data-mb-visual-mode="vaporwave"] select,body[data-mb-visual-mode="vaporwave"] textarea{background:#160b2e;color:#ffe5ff;border-color:#be67ff}',
      'body[data-mb-visual-mode="vaporwave"] .topline{border-color:#be67ff;background:#1b0c33}'
    ].join("\n")
  },
  hologram:{
    nome:"HOLOGRAMA",
    css:[
      'body[data-mb-visual-mode="hologram"]{background:radial-gradient(circle at 50% 0%,#144c74,#07111f 65%);color:#d9fbff}',
      'body[data-mb-visual-mode="hologram"] .hero,body[data-mb-visual-mode="hologram"] .card,body[data-mb-visual-mode="hologram"] .item{background:#102a3abf;border:1px solid #6ceaff88;backdrop-filter:blur(14px);box-shadow:0 12px 35px #00d9ff22,inset 0 0 22px #6ceaff0b}',
      'body[data-mb-visual-mode="hologram"] h1,body[data-mb-visual-mode="hologram"] h2,body[data-mb-visual-mode="hologram"] h3{color:#8cf4ff;text-shadow:0 0 16px #00d9ff}',
      'body[data-mb-visual-mode="hologram"] button{background:linear-gradient(110deg,#35c8df,#748aff);color:#061522;border:1px solid #9af5ff}',
      'body[data-mb-visual-mode="hologram"] input,body[data-mb-visual-mode="hologram"] select,body[data-mb-visual-mode="hologram"] textarea{background:#081c2a;color:#d9fbff;border-color:#4bcde8}'
    ].join("\n")
  },
  glitch:{
    nome:"GLITCH RGB",
    css:[
      'body[data-mb-visual-mode="glitch"]{background:linear-gradient(135deg,#080812,#171027 55%,#07121c);color:#f1f4ff}',
      'body[data-mb-visual-mode="glitch"] .hero,body[data-mb-visual-mode="glitch"] .card{border:1px solid #a5a1ff;background:#10101feF;box-shadow:5px 5px 0 #ff247a55,-5px -5px 0 #00eaff33}',
      'body[data-mb-visual-mode="glitch"] h1,body[data-mb-visual-mode="glitch"] h2,body[data-mb-visual-mode="glitch"] h3{color:#fff;text-shadow:3px 0 #ff247a,-3px 0 #00eaff;animation:mbRgbShift .32s steps(2,end) infinite}',
      'body[data-mb-visual-mode="glitch"] button{background:linear-gradient(90deg,#ff247a,#6e50ff,#00dfff);color:white;border:0}',
      'body[data-mb-visual-mode="glitch"] input,body[data-mb-visual-mode="glitch"] select,body[data-mb-visual-mode="glitch"] textarea{background:#090916;color:white;border-color:#8b78ff}',
      '@keyframes mbRgbShift{0%{transform:translateX(0)}25%{transform:translateX(1px)}50%{transform:translateX(-1px)}100%{transform:translateX(0)}}'
    ].join("\n")
  },
  terminal:{
    nome:"TERMINAL HACKER",
    css:[
      'body[data-mb-visual-mode="terminal"]{background:#030906;color:#9dffad;font-family:ui-monospace,SFMono-Regular,monospace}',
      'body[data-mb-visual-mode="terminal"] .hero,body[data-mb-visual-mode="terminal"] .card,body[data-mb-visual-mode="terminal"] .item{background:#050f09;border:1px solid #167d36;border-radius:5px;box-shadow:0 0 12px #00ff6622}',
      'body[data-mb-visual-mode="terminal"] h1,body[data-mb-visual-mode="terminal"] h2,body[data-mb-visual-mode="terminal"] h3{color:#72ff8b;text-shadow:0 0 9px #00ff66}',
      'body[data-mb-visual-mode="terminal"] button{background:#0b3218;color:#a5ffb4;border:1px solid #25b64e;border-radius:5px}',
      'body[data-mb-visual-mode="terminal"] input,body[data-mb-visual-mode="terminal"] select,body[data-mb-visual-mode="terminal"] textarea{background:#020805;color:#a5ffb4;border:1px solid #167d36;border-radius:5px}',
      'body[data-mb-visual-mode="terminal"] .topline{border:1px solid #167d36;border-radius:5px;background:#050f09}'
    ].join("\n")
  },
  aurora:{
    nome:"AURORA",
    css:[
      'body[data-mb-visual-mode="aurora"]{background:radial-gradient(ellipse at 15% 5%,#116b66 0,transparent 38%),radial-gradient(ellipse at 85% 20%,#543b8c 0,transparent 40%),radial-gradient(ellipse at 50% 100%,#154b73 0,transparent 45%),#07121e;color:#e7faff}',
      'body[data-mb-visual-mode="aurora"] .hero,body[data-mb-visual-mode="aurora"] .card,body[data-mb-visual-mode="aurora"] .item{background:linear-gradient(145deg,#102d38dd,#17142feF);border:1px solid #75e9cf77;box-shadow:0 12px 40px #16e0b522}',
      'body[data-mb-visual-mode="aurora"] h1,body[data-mb-visual-mode="aurora"] h2{color:#a1ffe3;text-shadow:0 0 16px #48e8c0}',
      'body[data-mb-visual-mode="aurora"] button{background:linear-gradient(110deg,#48dfbd,#7d91ff,#c28cff);color:#08131e;border:0}',
      'body[data-mb-visual-mode="aurora"] input,body[data-mb-visual-mode="aurora"] select,body[data-mb-visual-mode="aurora"] textarea{background:#091c29;color:#e7faff;border-color:#57bfae}'
    ].join("\n")
  },
  gold:{
    nome:"GOLD PREMIUM",
    css:[
      'body[data-mb-visual-mode="gold"]{background:radial-gradient(ellipse at top,#3c2b0d,#08090d 65%);color:#f8edcf}',
      'body[data-mb-visual-mode="gold"] .hero,body[data-mb-visual-mode="gold"] .card,body[data-mb-visual-mode="gold"] .item{background:linear-gradient(145deg,#211b10,#11100d);border:1px solid #a98742;box-shadow:0 12px 35px #c99a3330}',
      'body[data-mb-visual-mode="gold"] h1,body[data-mb-visual-mode="gold"] h2,body[data-mb-visual-mode="gold"] h3{color:#f3d78b;text-shadow:0 0 14px #b98931}',
      'body[data-mb-visual-mode="gold"] button{background:linear-gradient(110deg,#f0d58b,#bd8c35);color:#1a1206;border:0}',
      'body[data-mb-visual-mode="gold"] input,body[data-mb-visual-mode="gold"] select,body[data-mb-visual-mode="gold"] textarea{background:#100e09;color:#f8edcf;border-color:#9f7934}'
    ].join("\n")
  },
  ocean:{
    nome:"OCEANO PROFUNDO",
    css:[
      'body[data-mb-visual-mode="ocean"]{background:radial-gradient(ellipse at 50% 0%,#0a527a,#061323 65%);color:#e3f7ff}',
      'body[data-mb-visual-mode="ocean"] .hero,body[data-mb-visual-mode="ocean"] .card,body[data-mb-visual-mode="ocean"] .item{background:linear-gradient(145deg,#102b43,#0a192b);border:1px solid #2677a2;box-shadow:0 12px 34px #00aaff22}',
      'body[data-mb-visual-mode="ocean"] h1,body[data-mb-visual-mode="ocean"] h2,body[data-mb-visual-mode="ocean"] h3{color:#86e6ff;text-shadow:0 0 14px #00aaff}',
      'body[data-mb-visual-mode="ocean"] button{background:linear-gradient(110deg,#21b9d8,#4e83dc);color:#041321;border:0}',
      'body[data-mb-visual-mode="ocean"] input,body[data-mb-visual-mode="ocean"] select,body[data-mb-visual-mode="ocean"] textarea{background:#061728;color:#e3f7ff;border-color:#2677a2}'
    ].join("\n")
  }
};

function mbGuardarRolagemOriginal(){
  if(mbEditorState.scrollSnapshot)return;
  const propriedades=["overflow","overflow-x","overflow-y","height","min-height","position","display","align-items","justify-content","touch-action"];
  const guardar=el=>{
    const dados={};
    propriedades.forEach(prop=>dados[prop]={
      value:el.style.getPropertyValue(prop),
      priority:el.style.getPropertyPriority(prop)
    });
    return dados;
  };
  mbEditorState.scrollSnapshot={
    html:guardar(document.documentElement),
    body:guardar(document.body)
  };
}

function mbLiberarRolagem(){
  mbGuardarRolagemOriginal();
  const raiz=document.documentElement,corpo=document.body;
  raiz.style.setProperty("overflow-x","hidden","important");
  raiz.style.setProperty("overflow-y","auto","important");
  raiz.style.setProperty("height","auto","important");
  raiz.style.setProperty("min-height","100%","important");
  corpo.style.setProperty("overflow-x","hidden","important");
  corpo.style.setProperty("overflow-y","visible","important");
  corpo.style.setProperty("height","auto","important");
  corpo.style.setProperty("min-height","100vh","important");
  corpo.style.setProperty("display","block","important");
  corpo.style.setProperty("position","static","important");
  corpo.style.setProperty("align-items","initial","important");
  corpo.style.setProperty("justify-content","initial","important");
  corpo.style.setProperty("touch-action","pan-y","important");
}

function mbRestaurarRolagem(){
  const snap=mbEditorState.scrollSnapshot;
  if(!snap)return;
  const restaurar=(el,dados)=>{
    Object.keys(dados).forEach(prop=>{
      const item=dados[prop];
      if(item.value)el.style.setProperty(prop,item.value,item.priority);
      else el.style.removeProperty(prop);
    });
  };
  restaurar(document.documentElement,snap.html);
  restaurar(document.body,snap.body);
  mbEditorState.scrollSnapshot=null;
}

function mbPararTerminal(){
  if(mbEditorState.chaosTimer){
    clearInterval(mbEditorState.chaosTimer);
    mbEditorState.chaosTimer=null;
  }
}

function mbRemoverPainelCaos(){
  mbPararTerminal();
  const antigo=$("mb-chaos-stage");
  if(antigo)antigo.remove();
}

function mbAtualizarBotoesModo(id){
  document.querySelectorAll("[data-mb-preset]").forEach(botao=>{
    const ativo=botao.getAttribute("data-mb-preset")===id;
    botao.classList.toggle("is-active",ativo);
    botao.setAttribute("aria-pressed",ativo?"true":"false");
  });
}

function mbAtivarTerminalCaos(){
  const terminal=$("mb-chaos-terminal");
  if(!terminal)return;
  mbPararTerminal();
  const mensagens=[
    "> Sistema iniciado...",
    "> Abrindo portal dimensional...",
    "> Procurando outra realidade...",
    "> Erro 404: realidade nao encontrada",
    "> Reiniciando universo...",
    "> Universo reiniciado com sucesso!",
    "> VOCE E O BUG AGORA."
  ];
  terminal.textContent="";
  let indice=0;
  mbEditorState.chaosTimer=setInterval(()=>{
    if(!terminal.isConnected){mbPararTerminal();return;}
    terminal.textContent+=mensagens[indice]+"\n";
    indice++;
    if(indice>=mensagens.length)mbPararTerminal();
  },650);
}

function mbExecutarModoVisual(id){
  const preset=mbVisualPresets[id];
  if(!preset)return;
  mbGuardarRolagemOriginal();
  mbRemoverPainelCaos();
  if(!mbEditorState.visualStyleNode){
    mbEditorState.visualStyleNode=document.createElement("style");
    mbEditorState.visualStyleNode.id="mb-visual-presets-style";
    document.head.appendChild(mbEditorState.visualStyleNode);
  }
  document.body.setAttribute("data-mb-visual-mode",id);
  mbEditorState.visualStyleNode.textContent=preset.css;
  if(preset.html){
    const palco=document.createElement("section");
    palco.id="mb-chaos-stage";
    palco.className="mb-chaos-stage";
    palco.innerHTML='<div class="mb-chaos-kicker">MEDIABOX · SISTEMA DE DIAGNOSTICO</div><h2>DIMENSAO 404</h2><p>Voce entrou em uma area que nao deveria existir.</p><p>STATUS: REALIDADE INSTAVEL</p><button type="button" id="mb-chaos-launch">ATIVAR O CAOS</button><pre id="mb-chaos-terminal">&gt; Sistema iniciado...\n&gt; Aguardando comando...</pre>';
    const hero=document.querySelector("main .hero");
    if(hero)hero.insertAdjacentElement("afterend",palco);
    else document.querySelector("main").prepend(palco);
    $("mb-chaos-launch").addEventListener("click",mbAtivarTerminalCaos);
  }
  mbLiberarRolagem();
  mbAtualizarBotoesModo(id);
  $("mb-editor-status").textContent="Modo "+preset.nome+" executado. Use Restaurar página para voltar ao visual original.";
}

function mbDesativarModosVisuais(){
  mbRemoverPainelCaos();
  if(mbEditorState.visualStyleNode){
    mbEditorState.visualStyleNode.remove();
    mbEditorState.visualStyleNode=null;
  }
  document.body.removeAttribute("data-mb-visual-mode");
  mbAtualizarBotoesModo("");
}

(function iniciarEditorLocal(){
  const painel=$("mb-local-editor"),rodape=document.querySelector(".footer div:last-child");
  if(!painel||!rodape)return;
  const abrir=()=>{
    painel.hidden=false;
    $("mb-editor-status").textContent="Editor aberto. Escolha um modo visual ou use o editor manual.";
    mbLiberarRolagem();
  };
  rodape.addEventListener("click",()=>{
    mbEditorState.taps++;
    if(mbEditorState.tapTimer)clearTimeout(mbEditorState.tapTimer);
    mbEditorState.tapTimer=setTimeout(()=>{mbEditorState.taps=0},1800);
    if(mbEditorState.taps>=5){mbEditorState.taps=0;abrir();}
  });
  rodape.style.cursor="default";
  $("mb-editor-close").addEventListener("click",()=>{painel.hidden=true});
  painel.addEventListener("click",e=>{if(e.target===painel)painel.hidden=true});

  document.querySelectorAll("[data-mb-preset]").forEach(botao=>{
    botao.addEventListener("click",()=>mbExecutarModoVisual(botao.getAttribute("data-mb-preset")));
  });
  $("mb-preset-reset").addEventListener("click",()=>{
    mbDesativarModosVisuais();
    mbRestaurarRolagem();
    $("mb-editor-status").textContent="Modos visuais desativados.";
  });

  $("mb-editor-apply").addEventListener("click",()=>{
    const seletor=$("mb-editor-selector").value.trim();
    const htmlNovo=$("mb-editor-html").value;
    const cssNovo=$("mb-editor-css").value;
    try{
      if(!seletor)throw new Error("Informe o seletor CSS do elemento.");
      const alvo=document.querySelector(seletor);
      if(!alvo)throw new Error("Nenhum elemento encontrado para esse seletor.");
      if(alvo.closest("#mb-local-editor"))throw new Error("Escolha um elemento da pagina, nao do editor.");
      if(mbEditorState.htmlTarget!==alvo){
        mbEditorState.htmlTarget=alvo;
        mbEditorState.htmlOriginal=alvo.innerHTML;
      }
      const doc=new DOMParser().parseFromString("<div>"+htmlNovo+"</div>","text/html");
      doc.querySelectorAll("script,style,iframe,object,embed,base,meta,link").forEach(el=>el.remove());
      doc.querySelectorAll("*").forEach(el=>{
        [...el.attributes].forEach(attr=>{
          const nome=attr.name.toLowerCase(),valor=attr.value.trim().toLowerCase();
          if(nome.startsWith("on")||((nome==="href"||nome==="src"||nome==="xlink:href")&&valor.startsWith("javascript:"))||nome==="srcdoc")el.removeAttribute(attr.name);
        });
      });
      alvo.innerHTML=doc.body.firstElementChild?doc.body.firstElementChild.innerHTML:"";
      if(!mbEditorState.styleNode){
        mbEditorState.styleNode=document.createElement("style");
        mbEditorState.styleNode.id="mb-local-temporary-css";
        document.head.appendChild(mbEditorState.styleNode);
      }
      mbEditorState.styleNode.textContent=cssNovo;
      mbLiberarRolagem();
      $("mb-editor-status").textContent="Alteracoes aplicadas somente nesta pagina. A rolagem permanece liberada; atualize para descartar.";
    }catch(erro){$("mb-editor-status").textContent="Nao foi possivel aplicar: "+erro.message;}
  });

  $("mb-editor-python-validate").addEventListener("click",async()=>{
    const botao=$("mb-editor-python-validate");
    const codigo=$("mb-editor-python").value;
    if(!codigo.trim()){
      $("mb-editor-status").textContent="Digite ou cole um código Python para validar.";
      return;
    }
    botao.disabled=true;
    botao.textContent="Validando...";
    try{
      const resposta=await fetch("/painel/validar-python",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({codigo})
      });
      const dados=await resposta.json();
      if(!resposta.ok){
        const local=dados.linha?" Linha "+dados.linha+(dados.coluna?", coluna "+dados.coluna:"")+".":"";
        throw new Error((dados.erro||"Código Python inválido.")+local);
      }
      $("mb-editor-status").textContent=(dados.mensagem||"Sintaxe Python válida.")+" 🟢 VERDE";
    }catch(erro){
      $("mb-editor-status").textContent="❌ DEU RED! "+erro.message;
    }finally{
      botao.disabled=false;
      botao.textContent="✓ Validar sintaxe Python";
    }
  });

  $("mb-editor-reset").addEventListener("click",()=>{
    if(mbEditorState.htmlTarget&&mbEditorState.htmlTarget.isConnected)mbEditorState.htmlTarget.innerHTML=mbEditorState.htmlOriginal;
    mbEditorState.htmlTarget=null;
    mbEditorState.htmlOriginal="";
    if(mbEditorState.styleNode){mbEditorState.styleNode.remove();mbEditorState.styleNode=null;}
    mbDesativarModosVisuais();
    mbRestaurarRolagem();
    $("mb-editor-html").value="";
    $("mb-editor-css").value="";
    $("mb-editor-python").value="";
    $("mb-editor-status").textContent="Alteracoes temporarias restauradas. A pagina voltou ao visual original.";
  });
})();
function atualizarPreviaMarca(){
 const x=Math.max(0,Math.min(100,Number($("watermark-x").value)||88));
 const y=Math.max(0,Math.min(100,Number($("watermark-y").value)||88));
 const logo=$("preview-logo");
 const tipo=$("watermark").value;
 logo.hidden=tipo==="nao";
 if(tipo!=="nao"){
   const src="/marca-dagua-preview?tipo="+encodeURIComponent(tipo);
   if(!logo.src.endsWith(src))logo.src=src;
 }
 logo.style.left=x+"%";logo.style.top=y+"%";
 $("preview-position-label").textContent="Personalizada · "+Math.round(x)+"% horizontal / "+Math.round(y)+"% vertical";
}
(function configurarArrasteMarca(){
  const area=$("preview-video");
  let arrastando=false;
  function mover(ev){
    const r=area.getBoundingClientRect();
    if(!r.width||!r.height)return;
    const x=Math.max(0,Math.min(100,(ev.clientX-r.left)/r.width*100));
    const y=Math.max(0,Math.min(100,(ev.clientY-r.top)/r.height*100));
    $("watermark-x").value=x.toFixed(2);
    $("watermark-y").value=y.toFixed(2);
    atualizarPreviaMarca();
  }
  area.addEventListener("pointerdown",ev=>{
    if(ev.button!==undefined&&ev.button!==0)return;
    if(ev.target!==$("preview-logo"))return;
    arrastando=true;
    try{area.setPointerCapture(ev.pointerId)}catch(_){}
    mover(ev);
    ev.preventDefault();
  });
  area.addEventListener("pointermove",ev=>{if(arrastando){mover(ev);ev.preventDefault();}});
  function parar(){arrastando=false;}
  area.addEventListener("pointerup",parar);
  area.addEventListener("pointercancel",parar);
  area.addEventListener("lostpointercapture",parar);
})();;
let previewLinkCarregado="";
async function carregarPreviaDoLink(){
 const url=$("url").value.trim();
 if(!url){$("link-preview-status").textContent="Cole primeiro o link do vídeo.";return}
 const botao=$("load-video-preview");botao.disabled=true;botao.textContent="Buscando vídeo...";
 $("link-preview-status").textContent="Consultando o API-HUB e preparando a prévia...";
 try{
  let avisoApiHub="";
  try{
   const host=new URL(url).hostname.toLowerCase();
   if(["youtube.com","www.youtube.com","m.youtube.com","youtu.be","www.youtu.be"].includes(host)){
    const metaResp=await fetch("/api/youtube/video?url="+encodeURIComponent(url),{headers:{"Accept":"application/json"}});
    const meta=await metaResp.json();
    if(!metaResp.ok)throw new Error(meta.erro||"API-HUB indisponível.");
    const v=meta.video||{};
    const duracao=v.duration?" · duração "+v.duration:"";
    avisoApiHub="API-HUB: "+(v.title||"Vídeo identificado")+" · "+(v.channel||"Canal não informado")+duracao+". Metadados confirmados; a API oficial não entrega o arquivo MP4. ";
   }
  }catch(metaErro){avisoApiHub="API-HUB: "+metaErro.message+" "; }
  const resposta=await fetch("/preview/link",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
  const dados=await resposta.json();if(!resposta.ok)throw new Error(dados.erro||"Não foi possível carregar a prévia.");
  const video=$("preview-source");video.pause();video.src=dados.media_url;video.load();
  $("preview-video-title").textContent=dados.title||"Vídeo carregado";
  $("link-preview-status").textContent=avisoApiHub+"Prévia carregada. Abra o editor para assistir e posicionar a marca. 🟢";
  previewLinkCarregado=url;
  video.onerror=()=>{$("link-preview-status").textContent="A plataforma não permitiu reproduzir o fluxo diretamente no navegador. O download normal continua disponível.";};
 }catch(erro){$("link-preview-status").textContent=erro.message;mostrarSusto(erro.message)}
 finally{botao.disabled=false;botao.textContent="▶ Carregar prévia real do link"}
}
$("load-video-preview").addEventListener("click",carregarPreviaDoLink);
$("url").addEventListener("change",()=>{if($("url").value.trim()!==previewLinkCarregado)$("link-preview-status").textContent="Link alterado. Carregue novamente a prévia."});
$("preview-watermark").onclick=()=>{if(!$("preview-source").getAttribute("src")||previewLinkCarregado!==$("url").value.trim()){$("link-preview-status").textContent="Carregue novamente a prévia do link atual antes de abrir o posicionador.";return}$("watermark-preview").hidden=false;atualizarPreviaMarca()};
$("close-watermark-preview").onclick=()=>{$("watermark-preview").hidden=true;$("preview-source").pause()};
$("watermark").addEventListener("change",()=>atualizarPreviaMarca());
$("watermark-position").onchange=()=>{const p=$("watermark-position").value;const coords={superior_esquerdo:[15,15],superior_direito:[85,15],inferior_esquerdo:[15,85],inferior_direito:[85,85]};const v=coords[p]||coords.inferior_direito;$("watermark-x").value=v[0];$("watermark-y").value=v[1];atualizarPreviaMarca()};
$("watermark-preview").onclick=e=>{if(e.target===$("watermark-preview"))$("watermark-preview").hidden=true};
const errosCentraisExibidos=new Set();const errosContagem=new Map();let diagnosticoAtual="",erroAnimacaoAtiva=false,erroAnimacaoTimer=null,audioRisada=null,somRisadaAtivo=true;
let erroDiagnosticoSequencia=0,chaveErroAtual="",diagnosticosHistoricoMemoria=[];
const MB_DIAGNOSTIC_HISTORY_KEY="mediabox_diagnostic_history_v1";
function mbLerHistoricoDiagnostico(){
  try{
    const salvo=localStorage.getItem(MB_DIAGNOSTIC_HISTORY_KEY);
    const lista=salvo?JSON.parse(salvo):[];
    if(Array.isArray(lista)){diagnosticosHistoricoMemoria=lista.slice(0,15);return diagnosticosHistoricoMemoria;}
  }catch(_){}
  return diagnosticosHistoricoMemoria;
}
function mbSalvarHistoricoDiagnostico(item){
  const lista=[item,...mbLerHistoricoDiagnostico()].slice(0,15);
  diagnosticosHistoricoMemoria=lista;
  try{localStorage.setItem(MB_DIAGNOSTIC_HISTORY_KEY,JSON.stringify(lista));}catch(_){}
  const botao=$("error-history-toggle");
  if(botao)botao.textContent="Histórico ("+lista.length+")";
}
function mbRenderHistoricoDiagnostico(){
  const area=$("error-history-list");
  if(!area)return;
  area.replaceChildren();
  const lista=mbLerHistoricoDiagnostico();
  const botao=$("error-history-toggle");
  if(botao)botao.textContent="Histórico ("+lista.length+")";
  if(!lista.length){
    const vazio=document.createElement("p");vazio.className="error-history-empty";
    vazio.textContent="Nenhum diagnóstico salvo neste navegador ainda.";
    area.appendChild(vazio);return;
  }
  lista.forEach(item=>{
    const cartao=document.createElement("article");cartao.className="error-history-item";
    const titulo=document.createElement("strong");titulo.textContent=item.titulo||"Diagnóstico MEDIABOX";
    const detalhe=document.createElement("small");
    detalhe.textContent=(item.horario||"Horário não registrado")+" · "+(item.resumo||"Sem resumo")+". "+(item.mensagem||"");
    cartao.append(titulo,detalhe);area.appendChild(cartao);
  });
}
$("error-history-toggle").onclick=()=>{
  $("error-history").hidden=!$("error-history").hidden;
  if(!$("error-history").hidden)mbRenderHistoricoDiagnostico();
};
$("error-history-clear").onclick=()=>{
  diagnosticosHistoricoMemoria=[];
  try{localStorage.removeItem(MB_DIAGNOSTIC_HISTORY_KEY);}catch(_){}
  mbRenderHistoricoDiagnostico();
};
function mbAdicionarEtapaDiagnostico(item,concluidas,total){
  const area=$("intro-steps");
  if(!area)return;
  const linha=document.createElement("div");linha.className="intro-step is-"+item.status;
  const simbolo=document.createElement("span");simbolo.className="intro-step-state";
  simbolo.textContent=item.status==="ok"?"✓":item.status==="atencao"?"!":"×";
  const texto=document.createElement("div");
  const titulo=document.createElement("strong");titulo.textContent=item.nome;
  const detalhe=document.createElement("small");detalhe.textContent=item.detalhe;
  texto.append(titulo,detalhe);linha.append(simbolo,texto);area.appendChild(linha);
  const percentual=Math.min(100,Math.round((concluidas/Math.max(total,1))*100));
  $("intro-progress-fill").style.width=percentual+"%";
  $("intro-progress").setAttribute("aria-valuenow",String(percentual));
  $("intro-progress-label").textContent=concluidas+" de "+total+" verificações";
  area.scrollTop=area.scrollHeight;
}
function mbSugestaoDiagnostico(mensagem,origem,verificacoes){
  const falhas=verificacoes.filter(item=>item.status==="falha").map(item=>item.nome.toLowerCase()).join(" ");
  const alertas=verificacoes.filter(item=>item.status==="atencao").map(item=>item.nome.toLowerCase()).join(" ");
  const texto=(String(mensagem||"")+" "+String(origem||"")).toLowerCase();
  if(/armazenamento|espaço disponível/.test(falhas+" "+alertas))return "Confira o espaço livre e a permissão de gravação da pasta de mídia antes de tentar novamente.";
  if(/ffmpeg/.test(falhas+" "+alertas)||/ffmpeg|áudio|audio|mp3|equalizador/.test(texto))return "Verifique a disponibilidade do FFmpeg e tente novamente com um arquivo de origem válido.";
  if(/rotas principais|aplicação flask/.test(falhas))return "Há uma verificação do servidor que falhou. Confira o último deploy e os logs do Render antes de repetir a operação.";
  if(/runtime python|yt-dlp|requests/.test(falhas))return "Uma dependência do servidor não passou na verificação. Confira os requisitos instalados no último deploy.";
  if(/youtube|instagram|kwai|tiktok/.test(texto))return "As verificações locais não controlam bloqueios das plataformas. Confirme se o link está disponível e tente novamente mais tarde.";
  if(/rede|conexão|timeout|network/.test(texto)||/conexão do navegador/.test(falhas))return "Confira a conexão do dispositivo e se o MEDIABOX continua acessível no Render.";
  if(verificacoes.some(item=>item.status==="falha"))return "Use os itens marcados como falha acima como ponto de partida e confira os logs do servidor.";
  return "As verificações internas terminaram. Confira o link, os dados enviados e tente novamente; o diagnóstico não altera nem corrige arquivos automaticamente.";
}
async function executarPreDiagnostico(sequencia){
  const resultados=[];
  const totalPadrao=12;
  const adicionar=(nome,status,detalhe,total=totalPadrao)=>{
    const item={nome,status,detalhe:String(detalhe||"Verificação concluída.").slice(0,220)};
    resultados.push(item);
    if(sequencia===erroDiagnosticoSequencia){
      $("intro-message").textContent="Verificando: "+nome;
      mbAdicionarEtapaDiagnostico(item,resultados.length,total);
    }
  };

  adicionar("Conexão do navegador",navigator.onLine===false?"falha":"ok",
    navigator.onLine===false?"O navegador informa que está sem conexão.":"O navegador informa que a conexão está disponível.");

  const idsEssenciais=["error-center","error-intro","error-title","intro-steps","intro-progress-fill"];
  const ausentes=idsEssenciais.filter(id=>!$(id));
  adicionar("Interface do MEDIABOX",ausentes.length?"falha":"ok",
    ausentes.length?"Elementos ausentes: "+ausentes.join(", "):"Janela de diagnóstico e elementos principais encontrados.");

  const jsOk=typeof fetchOriginal==="function"&&typeof JSON==="object"&&typeof Promise==="function";
  adicionar("Ambiente JavaScript",jsOk?"ok":"falha",
    jsOk?"Fetch, JSON e Promises disponíveis no navegador.":"Uma API necessária do navegador não está disponível.");

  try{
    const chave="mb_diag_storage_test_"+Date.now();
    localStorage.setItem(chave,"ok");localStorage.removeItem(chave);
    adicionar("Histórico local","ok","O navegador permite salvar o histórico neste dispositivo.");
  }catch(_){
    adicionar("Histórico local","atencao","O navegador bloqueou o armazenamento local; o histórico ficará apenas nesta sessão.");
  }

  if(navigator.onLine===false){
    adicionar("Servidor Flask","falha","Consulta não realizada porque o navegador informou estar offline.",5);
    return resultados;
  }

  try{
    const controlador=typeof AbortController==="function"?new AbortController():null;
    const limite=controlador?setTimeout(()=>controlador.abort(),5000):null;
    const opcoes={method:"GET",cache:"no-store",credentials:"same-origin"};
    if(controlador)opcoes.signal=controlador.signal;
    let resposta;
    try{
      resposta=await fetchOriginal("/diagnostico/preflight?_="+Date.now(),opcoes);
    }finally{
      if(limite)clearTimeout(limite);
    }
    if(!resposta.ok){
      adicionar("Servidor Flask","falha","O endpoint de diagnóstico respondeu HTTP "+resposta.status+".",5);
      return resultados;
    }
    const dados=await resposta.json();
    if(!dados||!Array.isArray(dados.verificacoes)||!dados.verificacoes.length){
      adicionar("Servidor Flask","falha","O servidor não retornou uma lista válida de verificações.",5);
      return resultados;
    }
    dados.verificacoes.forEach(item=>{
      adicionar(item.nome||"Verificação do servidor",item.status||"atencao",item.detalhe||"Sem detalhes.");
    });
  }catch(erro){
    const detalhe=erro&&erro.name==="AbortError"?"O servidor demorou mais de 5 segundos para responder.":(erro&&erro.message)||"Não foi possível consultar o servidor.";
    adicionar("Servidor Flask","falha",detalhe,5);
  }
  return resultados;
}
async function iniciarAnimacaoErro(mensagem,tipo,contexto,relatorioBase,repeticoes){
  const sequencia=++erroDiagnosticoSequencia;
  const inicio=Date.now();
  if(erroAnimacaoTimer)clearTimeout(erroAnimacaoTimer);
  erroAnimacaoAtiva=true;
  $("error-center").hidden=true;
  $("error-history").hidden=true;
  $("error-intro").hidden=false;
  $("intro-title").textContent="Scanner holográfico iniciado";
  $("intro-message").textContent="Detectando a origem e executando verificações reais...";
  $("intro-steps").replaceChildren();
  $("intro-progress-fill").style.width="0%";
  $("intro-progress").setAttribute("aria-valuenow","0");
  $("intro-progress-label").textContent="Preparando 12 verificações...";
  const verificacoes=await executarPreDiagnostico(sequencia);
  if(sequencia!==erroDiagnosticoSequencia)return;
  const ocorrenciasAtuais=errosContagem.get(chaveErroAtual)||repeticoes||1;
  const falhas=verificacoes.filter(item=>item.status==="falha");
  const alertas=verificacoes.filter(item=>item.status==="atencao");
  const aprovadas=verificacoes.length-falhas.length-alertas.length;
  const resumo=verificacoes.length+" verificações: "+aprovadas+" OK, "+alertas.length+" atenção, "+falhas.length+" falha(s).";
  const sugestao=mbSugestaoDiagnostico(mensagem,contexto.origem||contexto.endpoint||"",verificacoes);
  $("error-diagnosis-summary").textContent=resumo;
  $("error-checks").textContent=String(verificacoes.length);
  $("error-repeats").textContent=String(ocorrenciasAtuais)+"x";
  $("error-suggestion").textContent=sugestao;
  diagnosticoAtual=relatorioBase.replace(/Ocorrências: \\d+/,"Ocorrências: "+ocorrenciasAtuais)+"\n\nPRÉ-DIAGNÓSTICO REAL\n"+resumo+"\n"+
    verificacoes.map(item=>"["+item.status.toUpperCase()+"] "+item.nome+": "+item.detalhe).join("\n")+
    "\n\nPróxima ação sugerida: "+sugestao;
  $("error-technical").textContent=diagnosticoAtual;
  $("intro-title").textContent=falhas.length?"Diagnóstico concluído com falhas":"Diagnóstico concluído";
  $("intro-message").textContent=resumo;
  $("intro-progress-fill").style.width="100%";
  $("intro-progress").setAttribute("aria-valuenow","100");
  $("intro-progress-label").textContent=verificacoes.length+" de "+verificacoes.length+" verificações concluídas";
  mbSalvarHistoricoDiagnostico({
    titulo:tipo.titulo,
    horario:new Date().toLocaleString("pt-BR"),
    resumo:resumo,
    mensagem:String(mensagem||"").slice(0,180),
    ocorrencias:ocorrenciasAtuais
  });
  const esperaReduzida=window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const duracaoRestante=Math.max(0,(esperaReduzida?0:650)-(Date.now()-inicio));
  await new Promise(resolve=>{erroAnimacaoTimer=setTimeout(resolve,duracaoRestante);});
  if(sequencia!==erroDiagnosticoSequencia)return;
  $("error-intro").hidden=true;
  $("error-center").hidden=false;
  erroAnimacaoAtiva=false;
  erroAnimacaoTimer=null;
}

function liberarAudioRisada(){
  try{
    const AudioCtx=window.AudioContext||window.webkitAudioContext;
    if(!AudioCtx)return;
    if(!audioRisada)audioRisada=new AudioCtx();
    if(audioRisada.state==="suspended")audioRisada.resume().catch(()=>{});
  }catch(_){}
}
document.addEventListener("pointerdown",liberarAudioRisada,{once:true,passive:true});
document.addEventListener("keydown",liberarAudioRisada,{once:true});
function tocarRisada(){
  if(!somRisadaAtivo)return;
  try{
    liberarAudioRisada();
    if(!audioRisada||audioRisada.state!=="running")return;
    const ctx=audioRisada,base=ctx.currentTime+.025;
    const master=ctx.createGain();
    master.gain.setValueAtTime(.72,base);
    master.connect(ctx.destination);

    // Primeiro um "miau" agudo; depois três gargalhadas curtas e saltitantes.
    const silabas=[
      {t:0,d:.30,a:390,b:850,c:510,vol:.105},
      {t:.34,d:.15,a:590,b:790,c:500,vol:.085},
      {t:.55,d:.14,a:520,b:760,c:440,vol:.09},
      {t:.75,d:.17,a:610,b:880,c:470,vol:.10},
      {t:1.00,d:.13,a:500,b:720,c:430,vol:.08}
    ];
    for(const s of silabas){
      const t=base+s.t,end=t+s.d;
      const osc=ctx.createOscillator(),formante=ctx.createBiquadFilter(),env=ctx.createGain();
      osc.type="sawtooth";
      osc.frequency.setValueAtTime(s.a,t);
      osc.frequency.exponentialRampToValueAtTime(s.b,t+s.d*.38);
      osc.frequency.exponentialRampToValueAtTime(s.c,end);
      formante.type="bandpass";
      formante.frequency.setValueAtTime(1050,t);
      formante.frequency.exponentialRampToValueAtTime(1450,t+s.d*.35);
      formante.Q.setValueAtTime(2.4,t);
      env.gain.setValueAtTime(.0001,t);
      env.gain.exponentialRampToValueAtTime(s.vol,t+.025);
      env.gain.setValueAtTime(s.vol*.72,t+s.d*.48);
      env.gain.exponentialRampToValueAtTime(.0001,end);
      osc.connect(formante);formante.connect(env);env.connect(master);
      osc.start(t);osc.stop(end+.015);
    }
    // Tremidinha grave de ronronar no final.
    const purr=ctx.createOscillator(),purrGain=ctx.createGain();
    purr.type="triangle";
    purr.frequency.setValueAtTime(92,base+.72);
    purrGain.gain.setValueAtTime(.0001,base+.72);
    purrGain.gain.linearRampToValueAtTime(.035,base+.82);
    purrGain.gain.exponentialRampToValueAtTime(.0001,base+1.2);
    purr.connect(purrGain);purrGain.connect(master);
    purr.start(base+.72);purr.stop(base+1.22);
    master.gain.setValueAtTime(.72,base+1.18);
    master.gain.linearRampToValueAtTime(.0001,base+1.28);
    // O nó permanece conectado até o envelope terminar naturalmente.
  }catch(_){}
}
function limparDadoTecnico(valor){
  return String(valor??"").replace(/([?&](?:token|key|api_key|signature)=)[^&\s]+/gi,"$1[OCULTO]").replace(/(Bearer\s+)[^\s]+/gi,"$1[OCULTO]").replace(/((?:password|senha|secret)\s*[:=]\s*)[^,\s]+/gi,"$1[OCULTO]").slice(0,2400);
}
function classificarErro(mensagem,origem){
  const t=(String(mensagem||"")+" "+String(origem||"")).toLowerCase();
  if(/tiktok/.test(t))return {categoria:"TikTok",titulo:"O TikTok não concluiu a solicitação",icone:"♪"};
  if(/youtube|429|verifica.*bot|vídeo não disponível/.test(t))return {categoria:"YouTube",titulo:"O YouTube não concluiu a solicitação",icone:"▶"};
  if(/instagram/.test(t))return {categoria:"Instagram",titulo:"Falha ao acessar o Instagram",icone:"◎"};
  if(/kwai/.test(t))return {categoria:"Kwai",titulo:"Não foi possível acessar o Kwai",icone:"▣"};
  if(/equalizador|áudio|audio|ffmpeg|mp3|conversão/.test(t))return {categoria:"Áudio / Equalizador",titulo:"Falha no processamento de áudio",icone:"♫"};
  if(/marca.?d.?água|watermark/.test(t))return {categoria:"Marca d'água",titulo:"Não foi possível aplicar a marca d'água",icone:"✦"};
  if(/pesquisa|pesquisar|search|api/.test(t))return {categoria:"Pesquisa / API",titulo:"A pesquisa não foi concluída",icone:"⌕"};
  if(/fila|queue/.test(t))return {categoria:"Fila de downloads",titulo:"A fila encontrou um problema",icone:"☷"};
  if(/arquivo|file|permissão|permission|armazenamento|storage/.test(t))return {categoria:"Arquivos / Armazenamento",titulo:"Problema ao acessar o arquivo",icone:"▤"};
  if(/timeout|timed out|conexão|connection|network|rede|fetch failed/.test(t))return {categoria:"Conexão",titulo:"A conexão foi interrompida",icone:"⌁"};
  if(/http.?5[0-9][0-9]|servidor|internal server|gateway/.test(t))return {categoria:"Servidor",titulo:"O servidor encontrou uma falha",icone:"◈"};
  return {categoria:"Operação geral",titulo:"Não foi possível concluir",icone:"⚠"};
}
function mostrarErroCentral(mensagem,contexto={}){
  const texto=limparDadoTecnico(mensagem||"Ocorreu um erro inesperado.");
  const origem=limparDadoTecnico(contexto.origem||contexto.endpoint||"MEDIABOX");
  const tipo=classificarErro(texto,origem);
  const chave=texto+"|"+origem+"|"+String(contexto.itemId||"");
  if(errosCentraisExibidos.has(chave)){
    const repeticoes=(errosContagem.get(chave)||1)+1;
    errosContagem.set(chave,repeticoes);
    if(chave===chaveErroAtual){
      $("error-repeats").textContent=String(repeticoes)+"x";
      diagnosticoAtual=diagnosticoAtual.replace(/Ocorrências: \d+/,"Ocorrências: "+repeticoes);
      $("error-technical").textContent=diagnosticoAtual;
    }
    return;
  }
  errosCentraisExibidos.add(chave);errosContagem.set(chave,1);chaveErroAtual=chave;
  if(errosCentraisExibidos.size>100){
    const maisAntiga=errosCentraisExibidos.values().next().value;
    errosCentraisExibidos.delete(maisAntiga);errosContagem.delete(maisAntiga);
  }
  const agora=Date.now();
  const codigo=limparDadoTecnico(contexto.codigo||"MB-OP-001");
  const http=contexto.status?String(contexto.status):"Não disponível";
  const endpoint=origem.replace(/([?&](?:token|key|api_key|signature)=)[^&\s]+/gi,"$1[OCULTO]");
  let detalhes=contexto.detalhes;
  if(typeof detalhes==="object"&&detalhes!==null){try{detalhes=JSON.stringify(detalhes,null,2)}catch(_){detalhes=String(detalhes)}}
  diagnosticoAtual=[
    "MEDIABOX — DIAGNÓSTICO DE ERRO",
    "Categoria: "+tipo.categoria,
    "Código: "+codigo,
    "HTTP: "+http,
    "Origem: "+endpoint,
    "Horário: "+new Date(agora).toLocaleString("pt-BR"),
    "Mensagem: "+texto,
    "Ocorrências: 1",
    "Detalhes: "+limparDadoTecnico(detalhes||"Nenhum detalhe adicional recebido.")
  ].join("\n");
  $("error-symbol").textContent=tipo.icone;
  $("error-title").textContent=tipo.titulo;
  $("error-message").textContent=texto;
  $("error-category").textContent=tipo.categoria;
  $("error-code").textContent=codigo;
  $("error-http").textContent=http;
  $("error-time").textContent=new Date(agora).toLocaleString("pt-BR");
  $("error-checks").textContent="—";
  $("error-repeats").textContent="1x";
  $("error-diagnosis-summary").textContent="Executando as verificações automáticas...";
  $("error-suggestion").textContent="Aguardando o resultado das verificações.";
  $("error-technical").textContent="Pré-diagnóstico em andamento...";
  iniciarAnimacaoErro(texto,tipo,{...contexto,origem},diagnosticoAtual,1);
}
function fecharErroCentral(){erroDiagnosticoSequencia++;erroAnimacaoAtiva=false;if(erroAnimacaoTimer)clearTimeout(erroAnimacaoTimer);erroAnimacaoTimer=null;$("error-intro").hidden=true;$("error-center").hidden=true;$("error-history").hidden=true;if($("mb-settings").hidden&&$("mb-birthday").hidden&&$("mb-command-surprise").hidden)document.body.classList.remove("mb-overlay-open")}
function mostrarSusto(mensagem,contexto={}){mostrarErroCentral(mensagem,contexto)}
function fecharSusto(){fecharErroCentral()}
$("error-copy").onclick=async()=>{
  try{await navigator.clipboard.writeText(diagnosticoAtual);$("error-copy").textContent="Diagnóstico copiado ✓"}
  catch(_){$("error-technical").focus();$("error-copy").textContent="Selecione os detalhes para copiar"}
};
async function solicitarRespostaCaptcha(pergunta){
  return new Promise(resolve=>{
    const fundo=document.createElement("div");
    fundo.style.cssText="position:fixed;inset:0;z-index:120000;display:grid;place-items:center;padding:18px;background:#030713e8;backdrop-filter:blur(12px)";
    const painel=document.createElement("form");
    painel.style.cssText="width:min(390px,100%);padding:24px;border:1px solid #496184;border-radius:22px;background:linear-gradient(145deg,#14233a,#09111f);box-shadow:0 25px 90px #000b;color:#f4f7ff;font-family:Inter,system-ui,Arial";
    const selo=document.createElement("div");selo.textContent="MEDIABOX • SEGURANÇA";selo.style.cssText="color:#70f0e6;font-size:10px;font-weight:900;letter-spacing:2px";
    const titulo=document.createElement("h2");titulo.textContent="Verificação anti-robô";titulo.style.cssText="margin:10px 0 6px;font-size:22px";
    const texto=document.createElement("p");texto.textContent="Para continuar, resolva esta conta simples:";texto.style.cssText="color:#aab7ce;font-size:13px;line-height:1.5";
    const conta=document.createElement("div");conta.textContent=pergunta;conta.style.cssText="margin:16px 0;padding:17px;text-align:center;border:1px solid #36516f;border-radius:14px;background:#0a1728;color:#8ff8ed;font-size:27px;font-weight:900;letter-spacing:2px";
    const campo=document.createElement("input");campo.type="text";campo.inputMode="numeric";campo.autocomplete="off";campo.placeholder="Digite o resultado";campo.required=true;campo.style.cssText="width:100%;padding:14px;border:1px solid #405777;border-radius:12px;background:#080e19;color:white;font:inherit;outline:none";
    const acoes=document.createElement("div");acoes.style.cssText="display:flex;gap:9px;margin-top:15px";
    const cancelar=document.createElement("button");cancelar.type="button";cancelar.textContent="Cancelar";cancelar.style.cssText="flex:1;padding:12px;border:1px solid #405777;border-radius:11px;background:#17243a;color:#e5edff;font-weight:800";
    const confirmar=document.createElement("button");confirmar.type="submit";confirmar.textContent="Verificar";confirmar.style.cssText="flex:1;padding:12px;border:0;border-radius:11px;background:linear-gradient(110deg,#70f0e6,#8aa8ff);color:#08101c;font-weight:900";
    acoes.append(cancelar,confirmar);painel.append(selo,titulo,texto,conta,campo,acoes);fundo.appendChild(painel);document.body.appendChild(fundo);
    const encerrar=valor=>{fundo.remove();resolve(valor)};
    cancelar.addEventListener("click",()=>encerrar(null));
    fundo.addEventListener("click",e=>{if(e.target===fundo)encerrar(null)});
    painel.addEventListener("submit",e=>{e.preventDefault();encerrar(campo.value.trim())});
    campo.focus();
  });
}
const fetchOriginal=window.fetch.bind(window);
window.fetch=async(...args)=>{
  const requisicao=args[0];
  const endpoint=typeof requisicao==="string"?requisicao:(requisicao&&requisicao.url)||"requisição";
  try{
    if((endpoint.split("?")[0]==="/start"||endpoint.split("?")[0]==="/fila/adicionar")&&
       String((args[1]||{}).method||"GET").toUpperCase()==="POST"){
      const desafioResponse=await fetchOriginal("/captcha/novo",{cache:"no-store"});
      const desafio=await desafioResponse.json();
      if(!desafioResponse.ok||!desafio.token||!desafio.question)throw new Error("Não foi possível carregar a verificação de segurança.");
      const respostaUsuario=await solicitarRespostaCaptcha(desafio.question);
      if(respostaUsuario===null)throw new Error("Verificação cancelada. Nenhuma operação foi iniciada.");
      let dadosEnvio={};
      try{dadosEnvio=JSON.parse((args[1]||{}).body||"{}")}catch(_){}
      dadosEnvio.captcha_token=desafio.token;
      dadosEnvio.captcha_answer=String(respostaUsuario).trim();
      args=[args[0],{...(args[1]||{}),body:JSON.stringify(dadosEnvio)}];
    }
    const resposta=await fetchOriginal(...args);
    let dados=null,texto="";
    try{
      texto=await resposta.clone().text();
      try{dados=JSON.parse(texto)}catch(_){}
    }catch(_){}
    const falhaAplicacao=!!(dados&&(
      (typeof dados.erro==="string"&&dados.erro.trim())||
      (typeof dados.error==="string"&&dados.error.trim())||
      dados.ok===false||
      dados.status==="error"
    ));
    if(!resposta.ok||falhaAplicacao){
      const mensagem=(dados&&(dados.erro||dados.error||dados.mensagem))||
        ("O servidor respondeu com HTTP "+resposta.status+".");
      mostrarErroCentral(mensagem,{
        codigo:!resposta.ok?"HTTP-"+resposta.status:"API-ERROR-001",
        status:resposta.status,endpoint,
        detalhes:(dados&&(dados.detalhes||dados))||texto||"O servidor não enviou detalhes adicionais."
      });
    }
    return resposta;
  }catch(erro){
    mostrarErroCentral(erro&&erro.message||"Não foi possível estabelecer comunicação com o servidor.",{
      codigo:"REDE-001",endpoint,detalhes:(erro&&erro.name)||"Falha de rede"
    });
    throw erro;
  }
};
window.addEventListener("error",evento=>{
  mostrarErroCentral(evento.message||"Ocorreu um erro no JavaScript da página.",{
    codigo:"JS-001",origem:(evento.filename||"JavaScript")+" : "+(evento.lineno||"?"),
    detalhes:evento.error&&evento.error.stack||"Erro de execução no navegador."
  });
});
window.addEventListener("unhandledrejection",evento=>{
  const erro=evento.reason;
  mostrarErroCentral(erro&&erro.message||String(erro||"Promessa rejeitada sem tratamento."),{
    codigo:"JS-PROMISE-001",origem:"JavaScript / Promise",
    detalhes:erro&&erro.stack||"Falha assíncrona sem tratamento."
  });
});
$("form").onsubmit=async e=>{e.preventDefault();$("btn").disabled=true;$("btn").textContent="Processando vídeo...";$("download-link").hidden=true;$("download-link").removeAttribute("href");const previaInicial=$("download-preview");previaInicial.pause();previaInicial.hidden=true;previaInicial.style.display="none";previaInicial.removeAttribute("src");previaInicial.load();$("err").textContent="";$("percent").textContent="0%";$("fill").style.width="0%";$("status").textContent="Preparando seu download...";try{let r=await fetch("/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:$("url").value,plataforma:$("plat").value,formato:$("fmt").value,marca_dagua:$("watermark").value!=="nao",marca_tipo:$("watermark").value,posicao_marca:$("watermark-position").value,marca_x:Number($("watermark-x").value),marca_y:Number($("watermark-y").value)})});let d=await r.json();if(!r.ok)throw Error(d.erro);if(timer)clearInterval(timer);await status();timer=setInterval(status,500)}catch(x){$("err").textContent=x.message;mostrarSusto(x.message);$("btn").disabled=false;$("btn").textContent="Iniciar download"}};
async function status(){try{
let r=await fetch("/status?_="+Date.now(),{cache:"no-store"}),d=await r.json();
let pct=Math.max(0,Math.min(100,Number(d.progresso)||0));
$("status").textContent=d.status||"Processando vídeo...";
$("percent").textContent=pct+"%";
$("fill").style.width=pct+"%";
$("fill").parentElement.setAttribute("aria-valuenow",String(pct));
$("err").textContent=d.erro||"";if(d.erro){mostrarErroCentral(d.erro,{codigo:"DOWNLOAD-ERROR-001",origem:"Download principal / "+(d.plataforma||"plataforma"),detalhes:d.status||"O servidor encerrou o download com falha."});}else if(!d.ativo&&/erro|falh|limite|indispon|verifica|recusou/i.test(d.status||"")){mostrarErroCentral(d.status,{codigo:"DOWNLOAD-STATUS-001",origem:"Download principal",detalhes:"O download terminou sem gerar um arquivo."});}
let link=$("download-link");
const downloadPreview=$("download-preview");
if(!d.ativo && d.arquivo && d.download_url && d.preview_url){
link.href=d.download_url;
downloadPreview.src=d.preview_url;
downloadPreview.hidden=false;downloadPreview.style.display="block";downloadPreview.load();
link.textContent="⬇ BAIXAR VÍDEO";
link.hidden=false;
link.style.display="block";
}else{
link.hidden=true;
link.style.display="none";
link.removeAttribute("href");
downloadPreview.pause();downloadPreview.hidden=true;downloadPreview.style.display="none";downloadPreview.removeAttribute("src");downloadPreview.load();
}
if(!d.ativo){
if(timer){clearInterval(timer);timer=null}
$("btn").disabled=false;$("btn").textContent="Iniciar download";
if(pct===100)$("status").textContent="Vídeo pronto para baixar.";
}
}catch(e){mostrarErroCentral(e&&e.message||"A resposta de status não pôde ser interpretada.",{codigo:"STATUS-JSON-001",origem:"Status do download",detalhes:e&&e.stack||"Resposta inválida do servidor."});}}

function avisoFila(texto, erro){
  $("queue-feedback").textContent=texto;
  $("queue-feedback").style.color=erro?"#ff9eaf":"var(--cyan)";
  if(erro)mostrarErroCentral(texto,{codigo:"FILA-VALIDACAO-001",origem:"Fila de downloads"});
}
let queueLiveEnabled=true;
let queueRefreshTimer=null;
function controlarAtualizacaoAoVivo(){
  const botao=$("queue-live-toggle");
  botao.textContent=queueLiveEnabled?"⏱ Ao vivo: ATIVADO":"⏱ Ao vivo: DESATIVADO";
  botao.setAttribute("aria-pressed",String(queueLiveEnabled));
  if(queueRefreshTimer){clearInterval(queueRefreshTimer);queueRefreshTimer=null;}
  if(queueLiveEnabled){
    queueRefreshTimer=setInterval(atualizarFila,2000);
    atualizarFila();
  }
}
async function atualizarFila(){
  try{
    const resposta=await fetch("/fila/status?_="+Date.now(),{cache:"no-store"});
    const dados=await resposta.json();
    if(!resposta.ok)throw Error(dados.erro||"Não foi possível consultar a fila.");
    const lista=$("queue-list");
    const cardsExistentes=new Map(
      Array.from(lista.querySelectorAll(".queue-item"))
        .map(card=>[card.dataset.itemId,card])
    );
    const itens=dados.itens||[];
    const falhaFila=itens.find(x=>x.status==="error"&&x.erro);
    if(falhaFila)mostrarErroCentral(falhaFila.erro,{codigo:"FILA-ITEM-001",origem:"Fila de downloads",itemId:falhaFila.id,detalhes:"Um item da fila terminou com falha."});
    $("queue-summary").textContent=itens.length
      ? dados.pendentes+" na fila · "+dados.baixando+" baixando · "+dados.concluidos+" concluídos · "+dados.falhas+" falhas"
      : "Nenhum link na fila.";

    const haPendentes=itens.some(x=>x.status==="pending");
    $("queue-start").disabled=(!!dados.executando&&!dados.pausada)||!haPendentes;
    $("queue-start").textContent=dados.pausada?"▶ Retomar fila":(dados.executando?"▶ Fila em andamento":"▶ Iniciar fila");
    $("queue-pause").disabled=!!dados.pausada||(!dados.executando&&!haPendentes);
    $("queue-pause").textContent=dados.pausada?"⏸ Pausa solicitada":"⏸ Pausar após o vídeo atual";

    $("queue-clear").disabled=!itens.some(x=>x.status==="done"||x.status==="error");

    if(dados.pausada){
      avisoFila(dados.executando
        ?"Pausa solicitada: o vídeo atual será concluído antes de parar."
        :"Fila pausada. Toque em Retomar fila para continuar.",false);
    }

    const nomes={pending:"NA FILA",downloading:"BAIXANDO",done:"CONCLUÍDO",error:"FALHOU"};
    itens.forEach((item,index)=>{
      const cardExistente=cardsExistentes.get(item.id);
      if(cardExistente&&cardExistente.querySelector("video")){
        // Mantém o player existente, inclusive quando está pausado ou tocando.
        // Atualiza também o link assinado de download, que tem validade limitada.
        const linkAtual=cardExistente.querySelector(".queue-link");
        if(linkAtual&&item.download_url)linkAtual.href=item.download_url;
        cardsExistentes.delete(item.id);
        return;
      }
      const card=document.createElement("div");
      card.className="queue-item";
      card.dataset.itemId=item.id;
      const head=document.createElement("div");
      head.className="queue-item-head";
      const title=document.createElement("strong");
      title.textContent="Vídeo "+(index+1);
      const state=document.createElement("span");
      state.className="queue-state";
      state.textContent=nomes[item.status]||"AGUARDANDO";
      head.append(title,state);
      const watermarkInfo=document.createElement("div");
      watermarkInfo.className="queue-help";
      watermarkInfo.textContent=item.marca_dagua
        ? "✦ Com marca MEDIABOX · "+({
            superior_esquerdo:"superior esquerdo",
            superior_direito:"superior direito",
            inferior_esquerdo:"inferior esquerdo",
            inferior_direito:"inferior direito"
          }[item.posicao_marca]||"inferior direito")
        : "Sem marca-d’água · vídeo original";
      const url=document.createElement("div");
      url.className="queue-url";
      url.textContent=item.url||"";
      const track=document.createElement("div");
      track.className="queue-track";
      const fill=document.createElement("div");
      fill.className="queue-fill";
      fill.style.width=Math.max(0,Math.min(100,Number(item.progresso)||0))+"%";
      track.append(fill);
      card.append(head,watermarkInfo,url,track);

      if(item.mensagem&&item.status==="downloading"){
        const message=document.createElement("div");
        message.className="queue-help";
        message.textContent=item.mensagem;
        card.append(message);
      }
      if(item.erro){
        mostrarErroCentral(item.erro,{codigo:"FILA-ITEM-001",origem:"Fila de downloads",itemId:item.id,detalhes:"O item "+(index+1)+" terminou com falha."});
        const error=document.createElement("div");
        error.className="queue-error";
        error.textContent=item.erro;
        card.append(error);
      }

      if(item.status==="done"&&item.preview_url){
        const previewTitle=document.createElement("div");
        previewTitle.className="queue-help";
        previewTitle.textContent="Prévia do vídeo — assista antes de baixar:";
        card.append(previewTitle);

        const video=document.createElement("video");
        video.className="queue-preview";
        video.controls=true;
        video.playsInline=true;
        video.preload="metadata";
        if(item.thumbnail_url)video.poster=item.thumbnail_url;
        video.src=item.preview_url;
        video.setAttribute("aria-label","Prévia para assistir antes de baixar");
        card.append(video);
      }

      if(item.status==="done"&&item.download_url){
        const link=document.createElement("a");
        link.className="queue-link";
        link.href=item.download_url;
        link.textContent="⬇ Baixar vídeo concluído";
        card.append(link);
      }
      if(item.status==="error"){
        const retry=document.createElement("button");
        retry.type="button";
        retry.className="queue-retry";
        retry.textContent="↻ Tentar novamente";
        retry.onclick=async()=>{
          retry.disabled=true;
          try{
            const r=await fetch("/fila/repetir/"+encodeURIComponent(item.id),{method:"POST"});
            const d=await r.json();
            if(!r.ok)throw Error(d.erro||"Não foi possível repetir.");
            for(const chave of errosCentraisExibidos){if(chave.endsWith("|"+item.id))errosCentraisExibidos.delete(chave);}
            avisoFila("Nova tentativa preparada.",false);
            await atualizarFila();
          }catch(e){avisoFila(e.message,true);retry.disabled=false;}
        };
        card.append(retry);
      }
      if(cardExistente)cardExistente.replaceWith(card);
      else lista.append(card);
      cardsExistentes.delete(item.id);
    });
    for(const card of cardsExistentes.values())card.remove();
  }catch(e){
    avisoFila(e.message||"Erro ao atualizar a fila.",true);
  }
}

$("queue-add").onclick=async()=>{
  const links=$("queue-input").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  if(!links.length){avisoFila("Cole pelo menos um link, um por linha.",true);return;}
  if(links.length>20){avisoFila("Adicione no máximo 20 links por vez.",true);return;}
  const botao=$("queue-add");
  botao.disabled=true;
  botao.textContent="Adicionando...";
  try{
    const resposta=await fetch("/fila/adicionar",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        urls:links,
        formato:$("fmt").value,
        plataforma:$("plat").value,
        
        marca_dagua:$("queue-watermark").value==="sim",
        posicao_marca:$("queue-watermark-position").value,
        marca_x:Number($("watermark-x").value),
        marca_y:Number($("watermark-y").value)
      })
    });
    const dados=await resposta.json();
    if(!resposta.ok)throw Error(dados.erro||"Não foi possível adicionar os links.");
    $("queue-input").value="";
    avisoFila(dados.adicionados+" link(s) adicionado(s) à fila. 🟢 VERDE",false);
    await atualizarFila();
  }catch(e){avisoFila(e.message,true);}
  finally{botao.disabled=false;botao.textContent="＋ Adicionar links";}
};

$("queue-start").onclick=async()=>{
  const botao=$("queue-start");
  botao.disabled=true;
  try{
    const resposta=await fetch("/fila/iniciar",{method:"POST"});
    const dados=await resposta.json();
    if(!resposta.ok)throw Error(dados.erro||"Não foi possível iniciar ou retomar a fila.");
    avisoFila(dados.mensagem||"Fila iniciada.",false);
    await atualizarFila();
  }catch(e){avisoFila(e.message,true);await atualizarFila();}
};
$("queue-pause").onclick=async()=>{
  const botao=$("queue-pause");
  botao.disabled=true;
  try{
    const resposta=await fetch("/fila/pausar",{method:"POST"});
    const dados=await resposta.json();
    if(!resposta.ok)throw Error(dados.erro||"Não foi possível pausar a fila.");
    avisoFila(dados.mensagem||"Pausa solicitada.",false);
    await atualizarFila();
  }catch(e){avisoFila(e.message,true);await atualizarFila();}
};
$("queue-live-toggle").onclick=()=>{
  queueLiveEnabled=!queueLiveEnabled;
  controlarAtualizacaoAoVivo();
  avisoFila(queueLiveEnabled
    ?"Acompanhamento em tempo real ativado."
    :"Acompanhamento em tempo real desativado. A fila continua trabalhando no servidor.",false);
};
$("queue-clear").onclick=async()=>{
  try{
    const resposta=await fetch("/fila/limpar",{method:"POST"});
    const dados=await resposta.json();
    if(!resposta.ok)throw Error(dados.erro||"Não foi possível limpar os finalizados.");
    avisoFila(dados.removidos+" item(ns) finalizado(s) removido(s).",false);
    await atualizarFila();
  }catch(e){avisoFila(e.message,true);}
};
controlarAtualizacaoAoVivo();

$("yt").onsubmit=async e=>{e.preventDefault();let o=$("ytout");o.textContent="Pesquisando...";try{let r=await fetch("/search/youtube",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({termo:$("term").value})}),d=await r.json();if(!r.ok)throw Error(d.erro);o.replaceChildren();if(!d.resultados.length){o.textContent="Nenhum resultado encontrado.";return}d.resultados.forEach(x=>{let el=document.createElement("div");el.className="item";let t=document.createElement("div");t.textContent=x.titulo;let s=document.createElement("div");s.className="small";s.textContent=[x.tipo,x.duracao].filter(Boolean).join(" · ");let a=document.createElement("a");a.href=x.url;a.target="_blank";a.rel="noopener";a.textContent="Abrir resultado";let b=document.createElement("button");b.className="secondary";b.textContent="Usar este link";b.onclick=()=>{$("url").value=x.url;$("plat").value="youtube";window.scrollTo({top:0,behavior:"smooth"})};el.append(t,s,a,b);o.append(el)})}catch(x){o.textContent=x.message;mostrarSusto(x.message)}};
</script></body></html>"""

@app.get("/manifest.webmanifest")
def bluevision_manifest():
    from flask import Response
    manifesto = {
        "id": "/",
        "name": "MEDIABOX — Video Studio",
        "short_name": "MEDIABOX",
        "description": "Seu aplicativo de vídeos, downloads e pesquisa.",
        "lang": "pt-BR",
        "start_url": "/?origem=aplicativo",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "portrait-primary",
        "theme_color": "#070a12",
        "background_color": "#070a12",
        "categories": ["video", "utilities"],
        "prefer_related_applications": False,
        "icons": [{"src": "/brand/bluevision-mark.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]
    }
    return Response(json.dumps(manifesto, ensure_ascii=False), mimetype="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/service-worker.js")
def bluevision_service_worker():
    from flask import Response
    codigo = r"""const CACHE_NAME = "bluevision-shell-v2";
const APP_ASSETS = ["/brand/bluevision-mark.svg"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith("bluevision-shell-") && key !== CACHE_NAME).map(key => caches.delete(key))
  )).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  // Páginas autenticadas, APIs e downloads nunca entram no cache offline.
  if (request.mode === "navigate" || url.pathname.startsWith("/status") || url.pathname.startsWith("/fila/") || url.pathname.startsWith("/arquivo/")) return;
  if (APP_ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => {
      if (response.ok) { const copy = response.clone(); caches.open(CACHE_NAME).then(cache => cache.put(request, copy)); }
      return response;
    })));
  }
});
"""
    return Response(codigo, mimetype="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/brand/bluevision-mark.svg")
def bluevision_brand_mark():
    return send_from_directory(BASE_DIR, "bluevision-mark.svg", mimetype="image/svg+xml", max_age=86400)


@app.get("/marca-dagua-preview")
def marca_dagua_preview():
    tipo = request.args.get("tipo", "dias_edits")
    try:
        logo = obter_logo_marca(tipo)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"erro": str(exc)}), 404
    mimetype = "image/jpeg" if logo.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return send_from_directory(os.path.dirname(logo), os.path.basename(logo), mimetype=mimetype, max_age=3600)


@app.get("/")
def inicio():
    resposta = app.make_response(render_template_string(HTML))
    resposta.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resposta.headers["Pragma"] = "no-cache"
    return resposta

@app.get("/navegador")
def navegador_mediabox():
    pagina = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#080b14"><title>MEDIABOX Browser</title>
<style>
:root{color-scheme:dark;--bg:#070a12;--panel:#111a2b;--line:#293954;--cyan:#70f0e6;--blue:#7b9cff;--pink:#c09aff;--muted:#96a4bd}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 10% 0%,#183b54 0,transparent 36%),radial-gradient(ellipse at 95% 12%,#35205a 0,transparent 30%),var(--bg);font-family:Inter,system-ui,Arial;color:#f4f7ff}
main{width:min(1100px,96%);margin:auto;padding:18px 0 28px}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 15px;border:1px solid var(--line);border-radius:17px;background:#0b1220d9}
.brand{font-weight:900;letter-spacing:1.5px}.brand span{display:block;color:var(--muted);font-size:10px;letter-spacing:2px}
a{color:var(--cyan);text-decoration:none}.back{padding:10px 13px;border:1px solid #344763;border-radius:12px}
.hero{text-align:center;padding:30px 10px 22px}.hero h1{margin:0;font-size:clamp(30px,7vw,48px);background:linear-gradient(100deg,var(--cyan),var(--blue),var(--pink));background-clip:text;color:transparent}.hero p{color:var(--muted);font-size:13px}
.search{display:flex;gap:9px;padding:10px;border:1px solid #344763;border-radius:18px;background:#0b1220;box-shadow:0 15px 45px #0004}
input{min-width:0;flex:1;padding:13px;border:0;outline:0;background:transparent;color:white;font:inherit;font-size:15px}
button{border:0;border-radius:13px;padding:12px 17px;background:linear-gradient(110deg,var(--cyan),#8aa8ff 60%,var(--pink));color:#08101c;font-weight:850;cursor:pointer}
.tools{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 14px}.tools button{background:#17243a;color:#e4edff;border:1px solid #344763;font-size:12px;padding:9px 12px}
.frame-wrap{overflow:hidden;border:1px solid #344763;border-radius:17px;background:#fff;min-height:65vh}
iframe{display:block;width:100%;height:68vh;min-height:480px;border:0;background:white}
.note{color:var(--muted);font-size:12px;line-height:1.6;padding:12px 3px}.note strong{color:#dce7fa}
@media(max-width:560px){main{width:94%;padding-top:10px}.search{gap:5px;padding:7px}.search input{font-size:14px}.search button{padding:11px 12px}.frame-wrap{border-radius:13px}iframe{height:70vh;min-height:430px}}
</style></head>
<body><main>
<header class="top"><div class="brand">MEDIABOX<span>BROWSER • PESQUISA NA WEB</span></div><a class="back" href="/">← Voltar</a></header>
<section class="hero"><h1>Pesquisar na web</h1><p>Use o Google para pesquisar perguntas, cálculos, vídeos e páginas.</p></section>
<form class="search" id="busca"><input id="q" autocomplete="off" placeholder="Pesquise qualquer coisa no Google..." aria-label="Pesquisar no Google" required><button type="submit">Pesquisar</button></form>
<nav class="tools"><button type="button" id="voltar">← Voltar</button><button type="button" id="avancar">Avançar →</button><button type="button" id="recarregar">↻ Atualizar</button><button type="button" id="inicio">⌂ Google</button></nav>
<div class="frame-wrap"><iframe id="pagina" name="pagina" title="Página aberta no navegador MEDIABOX" referrerpolicy="strict-origin-when-cross-origin" allow="fullscreen; autoplay; encrypted-media; picture-in-picture"></iframe></div>
<p class="note"><strong>Compatibilidade:</strong> a pesquisa usa o Google. Alguns sites impedem que suas páginas sejam exibidas dentro de outros sites; quando isso acontecer, use a opção de abrir a página no navegador do dispositivo. O MEDIABOX não consegue remover essas restrições.</p>
</main>
<script>
const campo=document.getElementById("q"),frame=document.getElementById("pagina");
function pesquisar(valor){const termo=(valor||"").trim();if(!termo)return;campo.value=termo;frame.src="https://www.google.com/search?igu=1&q="+encodeURIComponent(termo)}
document.getElementById("busca").addEventListener("submit",e=>{e.preventDefault();pesquisar(campo.value)});
document.getElementById("voltar").onclick=()=>{try{frame.contentWindow.history.back()}catch(e){}};
document.getElementById("avancar").onclick=()=>{try{frame.contentWindow.history.forward()}catch(e){}};
document.getElementById("recarregar").onclick=()=>{try{frame.contentWindow.location.reload()}catch(e){frame.src=frame.src}};
document.getElementById("inicio").onclick=()=>pesquisar("Google");
pesquisar("Google");
</script></body></html>"""
    return render_template_string(pagina)


@app.get("/status")
def status_route():
    with lock:
        dados = dict(estado)

    # Só libera o link quando o processamento terminou e o arquivo existe.
    nome_arquivo = dados.get("arquivo", "")
    caminho_pronto = os.path.join(PASTA, nome_arquivo) if nome_arquivo else ""
    arquivo_pronto = (
        not dados.get("ativo")
        and nome_arquivo == os.path.basename(nome_arquivo)
        and bool(nome_arquivo)
        and os.path.isfile(caminho_pronto)
        and os.path.getsize(caminho_pronto) > 0
    )
    if arquivo_pronto:
        token = download_signer.dumps({"nome": nome_arquivo})
        dados["download_url"] = url_for("entregar_arquivo", nome=nome_arquivo, token=token)
        dados["preview_url"] = url_for("previsualizar_arquivo", nome=nome_arquivo, token=token)
    else:
        dados.pop("download_url", None)
        dados.pop("preview_url", None)
    resposta = jsonify(dados)
    resposta.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resposta.headers["Pragma"] = "no-cache"
    return resposta


@app.get("/previa/<path:nome>")
def previsualizar_arquivo(nome):
    if nome != os.path.basename(nome):
        return jsonify({"erro": "Arquivo inválido."}), 404
    if not validar_token_download(nome, request.args.get("token")):
        return jsonify({"erro": "Link de prévia inválido ou expirado."}), 403
    caminho = os.path.join(PASTA, nome)
    if not os.path.isfile(caminho) or os.path.getsize(caminho) == 0:
        return jsonify({"erro": "Vídeo não encontrado."}), 404
    return send_from_directory(PASTA, nome, as_attachment=False, conditional=True)


@app.get("/arquivo/<path:nome>")
def entregar_arquivo(nome):
    if nome != os.path.basename(nome):
        return jsonify({"erro": "Arquivo inválido."}), 404

    if not validar_token_download(nome, request.args.get("token")):
        return jsonify({
            "erro": "Link de download inválido ou expirado."
        }), 403

    return send_from_directory(
        PASTA,
        nome,
        as_attachment=True,
        download_name=nome
    )


@app.get("/diagnostico/preflight")
def diagnostico_preflight():
    """Executa verificações reais e leves do ambiente MEDIABOX."""
    verificacoes = []

    def registrar(nome, status, detalhe):
        verificacoes.append({
            "nome": nome,
            "status": status,
            "detalhe": str(detalhe)[:220]
        })

    registrar("Runtime Python", "ok", "Python " + ".".join(str(x) for x in sys.version_info[:3]) + " ativo.")

    try:
        total_rotas = len(list(app.url_map.iter_rules()))
        registrar("Aplicação Flask", "ok", f"Flask ativo; {total_rotas} rotas registradas.")
    except Exception as exc:
        registrar("Aplicação Flask", "falha", "Não foi possível consultar as rotas: " + str(exc))

    try:
        registrar("Biblioteca requests", "ok", "requests " + str(requests.__version__) + " carregado.")
    except Exception as exc:
        registrar("Biblioteca requests", "falha", "Biblioteca indisponível: " + str(exc))

    try:
        versao_yt = getattr(getattr(yt_dlp, "version", None), "__version__", "carregado")
        registrar("yt-dlp", "ok", "yt-dlp " + str(versao_yt) + " carregado.")
    except Exception as exc:
        registrar("yt-dlp", "falha", "Não foi possível consultar o yt-dlp: " + str(exc))

    try:
        executavel_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if not executavel_ffmpeg or not os.path.isfile(executavel_ffmpeg):
            registrar("FFmpeg", "atencao", "Executável do FFmpeg não encontrado pelo imageio-ffmpeg.")
        else:
            teste_ffmpeg = subprocess.run(
                [executavel_ffmpeg, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=4,
                check=False
            )
            primeira_linha = (teste_ffmpeg.stdout or "").splitlines()
            if teste_ffmpeg.returncode == 0:
                registrar("FFmpeg", "ok", primeira_linha[0][:180] if primeira_linha else "Executável respondeu corretamente.")
            else:
                registrar("FFmpeg", "atencao", "O executável respondeu com código " + str(teste_ffmpeg.returncode) + ".")
    except Exception as exc:
        registrar("FFmpeg", "atencao", "Não foi possível confirmar o executável: " + str(exc))

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".mediabox-diagnostico-",
            suffix=".tmp",
            dir=PASTA,
            delete=True
        ) as arquivo_teste:
            arquivo_teste.write("MEDIABOX diagnóstico")
            arquivo_teste.flush()
        registrar("Armazenamento", "ok", "A pasta de mídia aceitou um teste temporário de escrita.")
    except Exception as exc:
        registrar("Armazenamento", "falha", "Não foi possível gravar na pasta de mídia: " + str(exc))

    try:
        espaco_livre = shutil.disk_usage(PASTA).free
        livre_mb = espaco_livre / (1024 * 1024)
        status_espaco = "atencao" if espaco_livre < 150 * 1024 * 1024 else "ok"
        registrar("Espaço disponível", status_espaco, f"{livre_mb:.1f} MB livres na unidade de mídia.")
    except Exception as exc:
        registrar("Espaço disponível", "atencao", "Não foi possível consultar o espaço: " + str(exc))

    try:
        rotas = {regra.rule for regra in app.url_map.iter_rules()}
        obrigatorias = {"/", "/start", "/status", "/fila/status"}
        ausentes = sorted(obrigatorias - rotas)
        if ausentes:
            registrar("Rotas principais", "falha", "Rotas ausentes: " + ", ".join(ausentes))
        else:
            registrar("Rotas principais", "ok", "Página inicial, download, status e fila registradas.")
    except Exception as exc:
        registrar("Rotas principais", "falha", "Não foi possível verificar as rotas: " + str(exc))

    falhas = sum(1 for item in verificacoes if item["status"] == "falha")
    alertas = sum(1 for item in verificacoes if item["status"] == "atencao")
    return jsonify({
        "ok": falhas == 0,
        "verificacoes": verificacoes,
        "totais": {
            "total": len(verificacoes),
            "ok": len(verificacoes) - falhas - alertas,
            "atencao": alertas,
            "falha": falhas
        }
    })


@app.post("/painel/validar-python")
def painel_validar_python():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return jsonify({"erro": "Envie os dados de validação em formato JSON."}), 400
    codigo = dados.get("codigo", "")
    if not isinstance(codigo, str):
        return jsonify({"erro": "Envie o código Python como texto."}), 400
    if len(codigo) > 50000:
        return jsonify({"erro": "O código excede o limite de 50.000 caracteres."}), 413
    if not codigo.strip():
        return jsonify({"erro": "Cole um código Python antes de validar."}), 400
    try:
        ast.parse(codigo, filename="<painel-secreto-mediabox>", mode="exec")
    except SyntaxError as exc:
        return jsonify({
            "erro": exc.msg,
            "linha": exc.lineno or 1,
            "coluna": exc.offset or 1
        }), 400
    except (ValueError, TypeError, RecursionError) as exc:
        return jsonify({"erro": str(exc) or "Não foi possível analisar o código."}), 400
    return jsonify({
        "ok": True,
        "mensagem": "Sintaxe Python válida. O código não foi executado."
    })


@app.get("/api/youtube/video")
def api_hub_youtube_video():
    """Proxy seguro para os metadados do YouTube no API-HUB; nunca retorna credenciais."""
    try:
        url = validar_url(request.args.get("url", ""))
        if detectar(url) != "youtube":
            return jsonify({"erro": "Este endpoint aceita somente links do YouTube."}), 422
        base_url = os.environ.get("API_HUB_BASE_URL", "https://api-hub-lwnq.onrender.com").rstrip("/")
        admin_token = os.environ.get("API_HUB_ADMIN_TOKEN", "").strip()
        if not admin_token:
            return jsonify({"erro": "Integração API-HUB pendente: configure API_HUB_ADMIN_TOKEN no Railway."}), 503
        resposta = requests.get(
            base_url + "/v1/google/youtube/video",
            params={"url": url},
            headers={"X-Admin-Token": admin_token, "Accept": "application/json"},
            timeout=(5, 22)
        )
        try:
            dados = resposta.json()
        except ValueError:
            dados = {}
        if resposta.status_code != 200:
            detalhe = dados.get("detail") or dados.get("erro") or "API-HUB indisponível."
            if isinstance(detalhe, dict):
                detalhe = detalhe.get("message") or "A consulta do YouTube foi recusada."
            return jsonify({"erro": str(detalhe)[:300]}), resposta.status_code if resposta.status_code in (401, 404, 422, 503) else 502
        saida = jsonify(dados)
        saida.headers["Cache-Control"] = "no-store"
        return saida
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 422
    except requests.Timeout:
        return jsonify({"erro": "O API-HUB demorou demais para responder. Tente novamente."}), 504
    except requests.RequestException:
        log.exception("Falha de comunicação com API-HUB")
        return jsonify({"erro": "Não foi possível conectar ao API-HUB agora."}), 502


@app.post("/preview/link")
def preview_link():
    """Resolve metadados e URL de reprodução sem baixar o arquivo completo."""
    dados = request.get_json(silent=True) or {}
    try:
        url = validar_url(dados.get("url", ""))
        if detectar(url) == "kwai":
            sessao_kwai = requests.Session()
            sessao_kwai.headers.update(HEADERS_KWAI)
            resposta_kwai = sessao_kwai.get(url, allow_redirects=True, timeout=(8, 18))
            resposta_kwai.raise_for_status()
            url_kwai = validar_url(resposta_kwai.url)
            media_url = extrair_kwai(resposta_kwai.text, url_kwai)
            titulo_kwai = extrair_titulo_kwai(resposta_kwai.text, url_kwai)
            duracao_kwai = None
            if not media_url:
                opcoes_kwai = {
                    "quiet": True, "no_warnings": True, "noplaylist": True,
                    "skip_download": True, "format": "best[ext=mp4]/best",
                    "http_headers": HEADERS_KWAI, "socket_timeout": 18
                }
                with yt_dlp.YoutubeDL(opcoes_kwai) as ydl:
                    info_kwai = ydl.extract_info(url_kwai, download=False)
                if info_kwai and info_kwai.get("entries"):
                    info_kwai = next((item for item in info_kwai["entries"] if item), None)
                if not info_kwai:
                    raise RuntimeError("O Kwai não retornou metadados públicos para a prévia.")
                media_url = info_kwai.get("url")
                duracao_kwai = info_kwai.get("duration")
                titulo_kwai = nome_seguro(info_kwai.get("title") or titulo_kwai)
                if not media_url:
                    formatos_kwai = [x for x in info_kwai.get("formats", [])
                                     if x.get("url") and x.get("vcodec") != "none"]
                    mp4_kwai = [x for x in formatos_kwai if x.get("ext") == "mp4"]
                    candidatos_kwai = mp4_kwai or formatos_kwai
                    media_url = candidatos_kwai[-1]["url"] if candidatos_kwai else None
            if not media_url:
                raise RuntimeError("O Kwai não disponibilizou um fluxo público compatível com a prévia.")
            media_url = validar_url(media_url)
            token_stream = secrets.token_urlsafe(32)
            cabecalhos_stream = {"User-Agent": HEADERS_KWAI["User-Agent"],
                                 "Referer": url_kwai,
                                 "Accept-Language": HEADERS_KWAI["Accept-Language"]}
            with preview_stream_lock:
                agora = time.time()
                preview_streams[token_stream] = {
                    "url": media_url, "headers": cabecalhos_stream, "created": agora
                }
            resposta = jsonify({"ok": True, "title": titulo_kwai,
                                "duration": duracao_kwai,
                                "media_url": url_for("preview_stream", token=token_stream)})
            resposta.headers["Cache-Control"] = "no-store"
            return resposta
        opcoes = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True,
            "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[ext=mp4]/best",
            "socket_timeout": 18, "extractor_retries": 1, "retries": 1,
        }
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("entries"):
            info = next((item for item in info["entries"] if item), None)
        if not info:
            return jsonify({"erro": "A plataforma não retornou um vídeo reproduzível."}), 422
        media_url = info.get("url")
        if not media_url:
            formatos = [item for item in info.get("formats", [])
                        if item.get("url") and item.get("vcodec") != "none"
                        and item.get("acodec") != "none"]
            formatos_mp4 = [item for item in formatos if item.get("ext") == "mp4"]
            candidatos = formatos_mp4 or formatos
            if candidatos:
                media_url = candidatos[-1].get("url")
        if not media_url or urlparse(media_url).scheme not in {"http", "https"}:
            return jsonify({"erro": "A plataforma não forneceu um fluxo direto compatível com a prévia."}), 422
        validar_url(media_url)
        token_stream = secrets.token_urlsafe(32)
        cabecalhos_origem = info.get("http_headers") or {}
        cabecalhos_permitidos = {"User-Agent", "Referer", "Accept", "Accept-Language"}
        cabecalhos_stream = {k: str(v) for k, v in cabecalhos_origem.items()
                             if k in cabecalhos_permitidos}
        cabecalhos_stream.setdefault("User-Agent", "Mozilla/5.0")
        agora = time.time()
        with preview_stream_lock:
            expirados = [chave for chave, item in preview_streams.items()
                         if agora - item["created"] > PREVIEW_STREAM_TTL]
            for chave in expirados:
                preview_streams.pop(chave, None)
            if len(preview_streams) >= 30:
                mais_antigo = min(preview_streams, key=lambda chave: preview_streams[chave]["created"])
                preview_streams.pop(mais_antigo, None)
            preview_streams[token_stream] = {
                "url": media_url, "headers": cabecalhos_stream, "created": agora
            }
        resposta = jsonify({"ok": True, "title": nome_seguro(info.get("title") or "Vídeo"),
                            "duration": info.get("duration"),
                            "media_url": url_for("preview_stream", token=token_stream)})
        resposta.headers["Cache-Control"] = "no-store"
        return resposta
    except Exception as exc:
        log.warning("Falha ao resolver prévia de vídeo: %s", exc)
        return jsonify({"erro": "Não foi possível obter a prévia deste link: " + str(exc)[:350]}), 422


@app.route("/preview/stream/<token>", methods=["GET", "HEAD"])
def preview_stream(token):
    """Proxy temporário com suporte a Range para reprodução no elemento video."""
    agora = time.time()
    with preview_stream_lock:
        item = preview_streams.get(token)
        if item and agora - item["created"] > PREVIEW_STREAM_TTL:
            preview_streams.pop(token, None)
            item = None
    if not item:
        return jsonify({"erro": "Prévia expirada. Carregue o link novamente."}), 404
    cabecalhos = dict(item["headers"])
    faixa = request.headers.get("Range")
    if faixa:
        cabecalhos["Range"] = faixa
    try:
        upstream = requests.request(
            request.method, item["url"], headers=cabecalhos,
            stream=request.method == "GET", allow_redirects=True,
            timeout=(8, 25)
        )
        if upstream.status_code not in (200, 206, 416):
            upstream.close()
            return jsonify({"erro": "A plataforma recusou a reprodução da prévia."}), 502
        saida_headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        for nome in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            valor = upstream.headers.get(nome)
            if valor:
                saida_headers[nome] = valor
        if request.method == "HEAD":
            status_code = upstream.status_code
            upstream.close()
            return Response(status=status_code, headers=saida_headers)
        def gerar_chunks():
            try:
                for bloco in upstream.iter_content(chunk_size=256 * 1024):
                    if bloco:
                        yield bloco
            finally:
                upstream.close()
        return Response(stream_with_context(gerar_chunks()),
                        status=upstream.status_code, headers=saida_headers,
                        direct_passthrough=True)
    except requests.RequestException as exc:
        log.warning("Falha no proxy de prévia: %s", exc)
        return jsonify({"erro": "Não foi possível transmitir a prévia deste vídeo."}), 502


@app.post("/start")
def start():
    d = request.get_json(silent=True) or {}
    captcha_ok, captcha_error = verificar_captcha(
        session, d.get("captcha_token", ""), d.get("captcha_answer", "")
    )
    if not captcha_ok:
        return jsonify({"erro": captcha_error}), 403
    try:
        url = validar_url(d.get("url", ""))
        formato = d.get("formato", "mp4")
        plataforma = d.get("plataforma", "auto")
        marca_dagua = d.get("marca_dagua", False) is True
        # O campo marca_tipo também recebe "nao" quando o usuário escolhe
        # baixar sem marca. Esse valor só é relevante quando a marca está ativa.
        marca_tipo = d.get("marca_tipo", "dias_edits")
        if marca_dagua:
            if marca_tipo not in {"dias_edits", "mediabox"}:
                return jsonify({"erro": "Tipo de marca d'água inválido."}), 400
        else:
            # Mantém um tipo interno válido, sem aplicar nenhuma imagem ao vídeo.
            marca_tipo = "dias_edits"
        posicao_marca = d.get("posicao_marca", "inferior_direito")
        marca_x = max(0.0, min(100.0, float(d.get("marca_x", 88))))
        marca_y = max(0.0, min(100.0, float(d.get("marca_y", 88))))
        posicoes_validas = {"superior_esquerdo", "superior_direito", "inferior_esquerdo", "inferior_direito"}
        if posicao_marca not in posicoes_validas: return jsonify({"erro": "Posição da marca d'água inválida."}), 400
        if formato not in FORMATOS: return jsonify({"erro": "Formato inválido."}), 400
        if plataforma not in PLATAFORMAS: return jsonify({"erro": "Plataforma inválida."}), 400
        with lock:
            if estado["ativo"] or fila_running: return jsonify({"erro": "Já existe um download ou uma fila em andamento."}), 409
            estado.update(ativo=True, progresso=0, status="Download iniciado.", arquivo="", erro="", plataforma=plataforma)
        usuario = session.get("mediabox_usuario", "desconhecido")
        registrar_evento(usuario, "download_iniciado", platform=plataforma, details=url[:250])
        threading.Thread(
            target=baixar,
            args=(url, formato, plataforma, usuario, marca_dagua, posicao_marca, marca_x, marca_y, marca_tipo),
            daemon=True
        ).start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)}), 400


@app.post("/fila/adicionar")
def fila_adicionar():
    d = request.get_json(silent=True) or {}
    captcha_ok, captcha_error = verificar_captcha(
        session, d.get("captcha_token", ""), d.get("captcha_answer", "")
    )
    if not captcha_ok:
        return jsonify({"erro": captcha_error}), 403
    raw_urls = d.get("urls", d.get("url", ""))

    if isinstance(raw_urls, str):
        urls = [x.strip() for x in raw_urls.replace("\r", "").split("\n") if x.strip()]
    elif isinstance(raw_urls, list):
        urls = [str(x).strip() for x in raw_urls if str(x).strip()]
    else:
        return jsonify({"erro": "Envie os links como texto ou lista."}), 400

    if not urls:
        return jsonify({"erro": "Cole pelo menos um link, um por linha."}), 400
    if len(urls) > MAX_FILA_BATCH:
        return jsonify({"erro": f"Adicione no máximo {MAX_FILA_BATCH} links por vez."}), 400

    formato = d.get("formato", "mp4")
    plataforma = d.get("plataforma", "auto")
    marca_dagua = d.get("marca_dagua") is True
    posicao_marca = d.get("posicao_marca", "inferior_direito")
    try:
        marca_x = max(0.0, min(100.0, float(d.get("marca_x", 88))))
        marca_y = max(0.0, min(100.0, float(d.get("marca_y", 88))))
    except (TypeError, ValueError):
        return jsonify({"erro": "Coordenadas da marca d'água inválidas."}), 400
    posicoes_validas = {
        "superior_esquerdo", "superior_direito",
        "inferior_esquerdo", "inferior_direito"
    }

    if formato not in FORMATOS:
        return jsonify({"erro": "Formato de arquivo inválido."}), 400
    if plataforma not in PLATAFORMAS:
        return jsonify({"erro": "Plataforma inválida."}), 400
    if posicao_marca not in posicoes_validas:
        return jsonify({"erro": "Posição da marca d'água inválida."}), 400

    novos = []
    try:
        for link in urls:
            link_validado = validar_url(link)
            novos.append({
                "id": secrets.token_urlsafe(10),
                "url": link_validado,
                "formato": formato,
                "plataforma": plataforma,
                "marca_dagua": marca_dagua,
                "posicao_marca": posicao_marca,
                "marca_x": marca_x,
                "marca_y": marca_y,
                "status": "pending",
                "progresso": 0,
                "arquivo": "",
                "erro": "",
                "download_url": ""
            })
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    with lock:
        if len(fila_downloads) + len(novos) > MAX_FILA_TOTAL:
            return jsonify({
                "erro": f"A fila aceita até {MAX_FILA_TOTAL} itens. Limpe os finalizados para adicionar mais."
            }), 409
        fila_downloads.extend(novos)

    registrar_evento(
        session.get("mediabox_usuario", "desconhecido"),
        "fila_adicionada",
        resultado="sucesso",
        details=f"{len(novos)} link(s) adicionados à fila"
    )
    return jsonify({"ok": True, "adicionados": len(novos)}), 201



@app.get("/fila/assistir/<item_id>")
def fila_assistir(item_id):
    with lock:
        item = next((x for x in fila_downloads if x.get("id") == item_id), None)
        nome = item.get("arquivo", "") if item and item.get("status") == "done" else ""
        caminho = os.path.join(PASTA, nome) if nome else ""
        valido = bool(nome) and nome == os.path.basename(nome) and os.path.isfile(caminho) and os.path.getsize(caminho) > 0

    if not valido:
        return jsonify({"erro": "Prévia indisponível. O vídeo ainda não terminou ou não está mais no armazenamento."}), 404

    return send_from_directory(PASTA, nome, as_attachment=False, conditional=True)


@app.get("/fila/miniatura/<item_id>")
def fila_miniatura(item_id):
    with lock:
        item = next((x for x in fila_downloads if x.get("id") == item_id), None)
        nome = item.get("thumbnail", "") if item and item.get("status") == "done" else ""
        caminho = os.path.join(PASTA, nome) if nome else ""
        valido = bool(nome) and nome == os.path.basename(nome) and os.path.isfile(caminho) and os.path.getsize(caminho) > 0

    if not valido:
        return jsonify({"erro": "Miniatura ainda não disponível."}), 404

    return send_from_directory(PASTA, nome, mimetype="image/jpeg", as_attachment=False, max_age=3600)



@app.get("/fila/status")
def fila_status():
    with lock:
        itens = []
        for item in fila_downloads:
            copia = {
                "id": item["id"],
                "url": item["url"],
                "marca_dagua": item.get("marca_dagua") is True,
                "posicao_marca": item.get("posicao_marca", "inferior_direito"),
                "status": item["status"],
                "progresso": item.get("progresso", 0),
                "arquivo": item.get("arquivo", ""),
                "erro": item.get("erro", ""),
                "download_url": "",
                "preview_url": "",
                "thumbnail_url": ""
            }

            if item["status"] == "downloading" and item["id"] == fila_current_id:
                copia["progresso"] = estado.get("progresso", 0)
                copia["mensagem"] = estado.get("status", "Baixando...")

            if item["status"] == "done":
                copia["preview_url"] = "/fila/assistir/" + item["id"]
                if item.get("thumbnail"):
                    copia["thumbnail_url"] = "/fila/miniatura/" + item["id"]

                nome = item.get("arquivo", "")
                caminho = os.path.join(PASTA, nome) if nome else ""
                if (
                    nome
                    and nome == os.path.basename(nome)
                    and os.path.isfile(caminho)
                    and os.path.getsize(caminho) > 0
                ):
                    token = download_signer.dumps({"nome": nome})
                    copia["download_url"] = (
                        "/arquivo/" + quote(nome, safe="")
                        + "?token=" + quote(token, safe="")
                    )

            itens.append(copia)

        resumo = {
            "itens": itens,
            "executando": fila_running,
            "pausada": fila_paused,
            "total": len(itens),
            "pendentes": sum(1 for x in itens if x["status"] == "pending"),
            "baixando": sum(1 for x in itens if x["status"] == "downloading"),
            "concluidos": sum(1 for x in itens if x["status"] == "done"),
            "falhas": sum(1 for x in itens if x["status"] == "error")
        }

    resposta = jsonify(resumo)
    resposta.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resposta

@app.post("/fila/iniciar")
def fila_iniciar():
    global fila_running, fila_paused
    usuario = session.get("mediabox_usuario", "desconhecido")

    with lock:
        if fila_running:
            if fila_paused:
                fila_paused = False
                return jsonify({"ok": True, "mensagem": "A fila será retomada após o item atual."})
            return jsonify({"erro": "A fila já está em andamento."}), 409
        if estado.get("ativo"):
            return jsonify({"erro": "Finalize o download individual antes de iniciar a fila."}), 409
        if not any(x.get("status") == "pending" for x in fila_downloads):
            return jsonify({"erro": "Não há links pendentes na fila."}), 400

        fila_paused = False
        fila_running = True

    threading.Thread(
        target=processar_fila_downloads,
        args=(usuario,),
        daemon=True
    ).start()
    return jsonify({"ok": True, "mensagem": "Fila iniciada ou retomada."}), 202


@app.post("/fila/pausar")
def fila_pausar():
    global fila_paused
    with lock:
        existe_trabalho = fila_running or any(
            x.get("status") == "pending" for x in fila_downloads
        )
        if not existe_trabalho:
            return jsonify({"erro": "Não há fila em execução ou links pendentes."}), 400

        fila_paused = True
        if not fila_running:
            estado.update(
                ativo=False,
                status="Fila pausada. Toque em Retomar para continuar.",
                erro=""
            )

    return jsonify({
        "ok": True,
        "mensagem": "Pausa solicitada. O vídeo atual termina antes de parar a fila."
    })

@app.post("/fila/repetir/<item_id>")
def fila_repetir(item_id):
    global fila_running, fila_paused
    usuario = session.get("mediabox_usuario", "desconhecido")
    iniciar_agora = False

    with lock:
        item = next((x for x in fila_downloads if x.get("id") == item_id), None)
        if not item:
            return jsonify({"erro": "Item não encontrado na fila."}), 404
        if item.get("status") != "error":
            return jsonify({"erro": "Somente downloads com falha podem ser repetidos."}), 409
        if not fila_running and estado.get("ativo"):
            return jsonify({"erro": "Finalize o download individual antes de repetir."}), 409

        item.update(status="pending", progresso=0, erro="", arquivo="", thumbnail="", download_url="")
        if not fila_running and not fila_paused:
            fila_running = True
            iniciar_agora = True

    if iniciar_agora:
        threading.Thread(
            target=processar_fila_downloads,
            args=(usuario,),
            daemon=True
        ).start()

    return jsonify({"ok": True, "mensagem": "Item preparado para nova tentativa."}), 202


@app.post("/fila/limpar")
def fila_limpar():
    with lock:
        antes = len(fila_downloads)
        fila_downloads[:] = [
            x for x in fila_downloads
            if x.get("status") not in {"done", "error"}
        ]
        removidos = antes - len(fila_downloads)

    return jsonify({"ok": True, "removidos": removidos})



@app.post("/search/youtube")
def search_youtube_route():
    try:
        d = request.get_json(silent=True) or {}
        return jsonify({"resultados": pesquisar_youtube(d.get("termo", ""))})
    except Exception as e:
        log.exception("Erro na pesquisa do YouTube")
        return jsonify({"erro": str(e)}), 500

@app.post("/search/instagram")
def search_instagram_route():
    try:
        d = request.get_json(silent=True) or {}
        perfil = pesquisar_instagram(d.get("usuario", ""))
        return jsonify({"perfil": perfil})
    except Exception as e:
        log.exception("Erro na consulta do Instagram")
        return jsonify({"erro": str(e)}), 500

if __name__ == "__main__":
    print("=" * 42)
    print(APP_NAME)
    print("Abra no celular: http://127.0.0.1:5000")
    print("Vídeos:", PASTA)
    print("Logs:", ARQUIVO_LOG)
    print("=" * 42)


# MEDIABOX_MATEMATICA_V1
@app.route("/matematica")
def matematica_mediabox():
    from flask import Response
    pagina = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEDIABOX Matemática</title>
<style>
body{background:#080d1c;color:white;font-family:Arial;padding:20px}
main{max-width:600px;margin:auto}
h1{color:#54dfff}
.card{background:#17223b;padding:20px;border-radius:18px;margin:15px 0}
input{width:100%;box-sizing:border-box;padding:14px;margin:8px 0;background:#0b1429;color:white;border:1px solid #456;border-radius:10px}
button{padding:13px;background:#087cff;color:white;border:0;border-radius:10px;margin:5px}
.resultado{color:#54dfff;font-size:19px;margin-top:15px}
a{color:#54dfff}
</style>
</head>
<body>
<main>
<a href="/">← Voltar ao MEDIABOX</a>
<h1>🧮 Matemática</h1>

<div class="card">
<h2>Calculadora</h2>
<input id="calc" placeholder="Ex: (25+15)*2">
<button onclick="calcular()">Calcular</button>
<div id="r1" class="resultado"></div>
</div>

<div class="card">
<h2>Tabuada</h2>
<input id="tab" type="number" placeholder="Número">
<button onclick="tabuada()">Gerar</button>
<div id="r2" class="resultado"></div>
</div>

<div class="card">
<h2>Porcentagem</h2>
<input id="por1" type="number" placeholder="Valor">
<input id="por2" type="number" placeholder="Porcentagem">
<button onclick="porcentagem()">Calcular</button>
<div id="r3" class="resultado"></div>
</div>

<div class="card">
<h2>Frações</h2>
<input id="fra1" type="number" placeholder="Numerador">
<input id="fra2" type="number" placeholder="Denominador">
<button onclick="fracao()">Calcular</button>
<div id="r4" class="resultado"></div>
</div>

<div class="card">
<h2>Equação: ax + b = c</h2>
<input id="a" type="number" placeholder="a">
<input id="b" type="number" placeholder="b">
<input id="c" type="number" placeholder="c">
<button onclick="equacao()">Resolver</button>
<div id="r5" class="resultado"></div>
</div>
</main>

<script>
function calcular(){
let s=document.getElementById('calc').value;
if(!/^[0-9+*/().%\s-]+$/.test(s)){r1.innerText="Expressão inválida";return}
try{
let n=Function('"use strict";return ('+s+')')();
r1.innerText=Number.isFinite(n)?"Resultado: "+n:"Cálculo inválido";
}catch(e){r1.innerText="Expressão inválida"}
}
function tabuada(){
let n=Number(tab.value);
r2.innerHTML=Array.from({length:10},(_,i)=>n+" × "+(i+1)+" = "+n*(i+1)).join("<br>");
}
function porcentagem(){
r3.innerText=Number(por1.value)*Number(por2.value)/100;
}
function fracao(){
let n=Number(fra1.value),d=Number(fra2.value);
r4.innerText=d===0?"Denominador inválido":n/d;
}
function equacao(){
let a=Number(document.getElementById("a").value);
let b=Number(document.getElementById("b").value);
let c=Number(document.getElementById("c").value);
r5.innerText=a===0?(b===c?"Infinitas soluções":"Sem solução"):"x = "+((c-b)/a);
}
</script>
</body>
</html>
"""
    return Response(pagina, content_type="text/html; charset=utf-8")





if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORTA, debug=False, threaded=True)