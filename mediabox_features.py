import os
import secrets
import re
import time
import subprocess
import threading
from datetime import datetime
from urllib.parse import quote

import requests
from flask import (
    request, render_template_string, jsonify, send_from_directory,
    redirect, url_for
)

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

BASE = None
MEDIA = None
LOG = None
APP = None
EXPORT_JOBS = {}

STYLE = """
<style>
:root{color-scheme:dark;--bg:#070a12;--panel:#111a2b;--line:#293954;--cyan:#70f0e6;--blue:#7b9cff;--pink:#c09aff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 10% 0%,#183b54 0,transparent 36%),radial-gradient(ellipse at 95% 12%,#35205a 0,transparent 30%),var(--bg);color:#f4f7ff;font-family:Inter,system-ui,Arial;padding:18px}
main{width:min(950px,96%);margin:auto}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:13px 16px;border:1px solid var(--line);border-radius:17px;background:#0b1220}
a{color:var(--cyan);text-decoration:none}.back{padding:9px 12px;border:1px solid #344763;border-radius:11px}
h1{font-size:clamp(26px,6vw,40px);margin:24px 0 8px}p,.muted{color:#9aabc5;line-height:1.65}
.card{padding:19px;margin:14px 0;border:1px solid var(--line);border-radius:19px;background:linear-gradient(145deg,#121d30f5,#0c1321f5)}
input,select,textarea,button{width:100%;padding:13px;margin:6px 0 12px;border:1px solid #30415e;border-radius:12px;background:#080e19;color:white;font:inherit}
button{background:linear-gradient(110deg,#70f0e6,#8aa8ff 60%,#c09aff);color:#08101c;font-weight:850;cursor:pointer;border:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.item{padding:14px;border:1px solid #30405a;border-radius:14px;background:#0a1220;overflow-wrap:anywhere}
.small{font-size:12px;color:#9aabc5}.result{white-space:pre-wrap;overflow-wrap:anywhere;color:#dce7f9}
audio,video,img.preview,canvas{width:100%;max-width:100%;border-radius:12px;background:#050810}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #293954}
@media(max-width:520px){body{padding:10px}.card{padding:14px}}
</style>
"""

ERROR_CENTER_STYLE = """
<style>
#mb-error[hidden]{display:none}
#mb-error{position:fixed;inset:0;z-index:999999;display:grid;place-items:center;padding:12px;background:#030712eF;backdrop-filter:blur(9px)}
#mb-error .mb-error-panel{width:min(520px,100%);max-height:92vh;overflow:auto;padding:20px;border:1px solid #53658d;border-radius:23px;background:radial-gradient(ellipse at 10% 0%,#203b58 0,transparent 42%),radial-gradient(ellipse at 100% 0%,#39265c 0,transparent 38%),linear-gradient(145deg,#121d30,#090f1d);box-shadow:0 26px 90px #000b;color:#f4f7ff}
#mb-error .mb-error-head{display:flex;justify-content:space-between;gap:10px;color:#8ceeff;font-size:10px;font-weight:900;letter-spacing:1.5px}
#mb-error .mb-error-badge{color:#ffd58c;border:1px solid #d5a45a66;background:#d5a45a16;padding:6px 9px;border-radius:30px}
#mb-error .mb-error-icon{width:58px;height:58px;margin:20px auto 10px;display:grid;place-items:center;border:1px solid #8aa7ff66;border-radius:18px;background:linear-gradient(145deg,#233d61,#30264e);font-size:29px}
#mb-error h2{text-align:center;margin:8px 0;font-size:24px;background:linear-gradient(100deg,#8defff,#a7b4ff 58%,#d2a7ff);background-clip:text;-webkit-background-clip:text;color:transparent}
#mb-error .mb-error-message{text-align:center;color:#c4d0e5;font-size:13px;line-height:1.65;overflow-wrap:anywhere}
#mb-error .mb-error-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:16px}
#mb-error .mb-error-grid div{padding:10px;border:1px solid #344765;border-radius:12px;background:#0a1323;min-width:0}
#mb-error .mb-error-grid small{display:block;color:#8e9fbc;font-size:10px;text-transform:uppercase}
#mb-error .mb-error-grid strong{display:block;color:#e4edff;font-size:12px;overflow-wrap:anywhere}
#mb-error details{margin-top:11px;border:1px solid #344765;border-radius:13px;background:#080f1d}
#mb-error summary{padding:12px;color:#9fe9ff;font-size:12px;font-weight:850;cursor:pointer}
#mb-error pre{margin:0 10px 10px;padding:11px;background:#050a13;border-radius:9px;color:#c1cde0;font:11px/1.6 monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:170px;overflow:auto}
#mb-error .mb-error-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px}
#mb-error button{margin:0;min-height:44px}
#mb-error .mb-error-copy{background:#17243a;color:#e5edff;border:1px solid #4a5d80}
@media(max-width:420px){ #mb-error .mb-error-panel{padding:15px}#mb-error .mb-error-actions{grid-template-columns:1fr}}
</style>
<script>
(function(){
 if(window.__mediaboxErrorCenterInstalled)return;
 window.__mediaboxErrorCenterInstalled=true;
 let lastKey="",lastAt=0,report="";
 const $=id=>document.getElementById(id);
 const clean=v=>String(v??"").replace(/([?&](?:token|key|api_key|signature)=)[^&\\s]+/gi,"$1[OCULTO]").replace(/(Bearer\\s+)[^\\s]+/gi,"$1[OCULTO]").replace(/((?:password|senha|secret)\\s*[:=]\\s*)[^,\\s]+/gi,"$1[OCULTO]").slice(0,2200);
 function category(msg,origin){
  const t=(String(msg||"")+" "+String(origin||"")).toLowerCase();
  if(/youtube|429/.test(t))return ["YouTube","O YouTube não concluiu a solicitação","▶"];
  if(/instagram/.test(t))return ["Instagram","Falha ao acessar o Instagram","◎"];
  if(/kwai/.test(t))return ["Kwai","Não foi possível acessar o Kwai","▣"];
  if(/equalizador|áudio|audio|ffmpeg|mp3/.test(t))return ["Áudio / Equalizador","Falha no processamento de áudio","♫"];
  if(/marca.?d.?água|watermark/.test(t))return ["Marca d'água","Falha ao aplicar a marca d'água","✦"];
  if(/pesquisa|search|api/.test(t))return ["Pesquisa / API","A pesquisa não foi concluída","⌕"];
  if(/fila|queue/.test(t))return ["Fila de downloads","A fila encontrou um problema","☷"];
  if(/arquivo|file|storage|armazenamento/.test(t))return ["Arquivos","Problema ao acessar o arquivo","▤"];
  if(/timeout|conexão|connection|network|rede|fetch/.test(t))return ["Conexão","A conexão foi interrompida","⌁"];
  if(/http.?5[0-9][0-9]|servidor|gateway/.test(t))return ["Servidor","O servidor encontrou uma falha","◈"];
  return ["Operação geral","Não foi possível concluir","⚠"];
 }
 function show(message,origin="Ferramenta MEDIABOX",code="MB-OP-001",http="",details="Sem detalhes adicionais."){
  const msg=clean(message||"Ocorreu um erro inesperado."),kind=category(msg,origin),now=Date.now(),key=msg;
  if(key===lastKey&&now-lastAt<3000)return;
  lastKey=key;lastAt=now;
  report=["MEDIABOX — DIAGNÓSTICO","Categoria: "+kind[0],"Código: "+code,"HTTP: "+(http||"Não disponível"),"Origem: "+clean(origin),"Horário: "+new Date(now).toLocaleString("pt-BR"),"Mensagem: "+msg,"Detalhes: "+clean(details)].join("\\n");
  $("mb-error-icon").textContent=kind[2];$("mb-error-title").textContent=kind[1];$("mb-error-message").textContent=msg;
  $("mb-error-category").textContent=kind[0];$("mb-error-code").textContent=code;$("mb-error-http").textContent=http||"Não disponível";
  $("mb-error-time").textContent=new Date(now).toLocaleString("pt-BR");$("mb-error-details").textContent=report;$("mb-error").hidden=false;
 }
 window.mediaboxShowError=show;
 window.mediaboxCloseError=()=>{$("mb-error").hidden=true};
 $("mb-error-copy").onclick=async()=>{try{await navigator.clipboard.writeText(report);$("mb-error-copy").textContent="Diagnóstico copiado ✓"}catch(_){$("mb-error-copy").textContent="Selecione os detalhes para copiar"}};
 const nativeFetch=window.fetch.bind(window);
 window.fetch=async(...args)=>{
  const req=args[0],url=typeof req==="string"?req:(req&&req.url)||"requisição";
  try{
   const response=await nativeFetch(...args);
   let data=null;
   try{data=await response.clone().json()}catch(_){}
   if(!response.ok)show(data&&(data.erro||data.error||data.mensagem)||("O servidor respondeu HTTP "+response.status),url,"HTTP-"+response.status,response.status,data||"Resposta sem JSON");
   else if(data&&data.status==="error")show(data.mensagem||data.erro||"A operação falhou.",url,"JOB-ERROR-001","200",data);
   else if(data&&data.ok===false)show(data.erro||data.mensagem||"A operação não foi concluída.",url,"API-ERROR-001","200",data);
   return response;
  }catch(error){show(error&&error.message||"Falha de conexão.",url,"REDE-001","",error&&error.stack||"Falha de rede");throw error;}
 };
 window.addEventListener("error",e=>show(e.message||"Erro no JavaScript.",e.filename||"JavaScript","JS-001","",e.error&&e.error.stack||"Erro de execução"));
 window.addEventListener("unhandledrejection",e=>show(e.reason&&e.reason.message||String(e.reason||"Falha assíncrona."),"JavaScript / Promise","JS-PROMISE-001","",e.reason&&e.reason.stack||"Promessa rejeitada"));
})();
</script>
"""

def page(title, body):
    center = """
<div id="mb-error" hidden role="alertdialog" aria-modal="true" aria-labelledby="mb-error-title">
 <section class="mb-error-panel">
  <div class="mb-error-head"><span>MEDIABOX · DIAGNÓSTICO</span><span class="mb-error-badge">FALHA IDENTIFICADA</span></div>
  <div id="mb-error-icon" class="mb-error-icon">⚠</div>
  <h2 id="mb-error-title">Não foi possível concluir</h2>
  <p id="mb-error-message" class="mb-error-message">Ocorreu um problema durante esta operação.</p>
  <div class="mb-error-grid">
   <div><small>Categoria</small><strong id="mb-error-category">Operação geral</strong></div>
   <div><small>Código</small><strong id="mb-error-code">MB-000</strong></div>
   <div><small>Resposta HTTP</small><strong id="mb-error-http">—</strong></div>
   <div><small>Horário</small><strong id="mb-error-time">—</strong></div>
  </div>
  <details open><summary>DETALHES TÉCNICOS</summary><pre id="mb-error-details">Aguardando diagnóstico...</pre></details>
  <div class="mb-error-actions"><button type="button" class="mb-error-copy" id="mb-error-copy">Copiar diagnóstico</button><button type="button" onclick="window.mediaboxCloseError()">Voltar à ferramenta</button></div>
 </section>
</div>
"""
    return '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + title + ' • MEDIABOX</title>' + STYLE + '<body><main><header class="top"><strong>MEDIABOX • ' + title + '</strong><a class="back" href="/">← Voltar</a></header>' + body + '</main>' + center + ERROR_CENTER_STYLE + '</body></html>'

def files_by_type(exts):
    found = []
    try:
        for name in os.listdir(MEDIA):
            path = os.path.join(MEDIA, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in exts:
                st = os.stat(path)
                found.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    return sorted(found, key=lambda x: x["mtime"], reverse=True)[:250]

def fmt_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024

def safe_media_name(name):
    name = os.path.basename(name or "")
    path = os.path.realpath(os.path.join(MEDIA, name))
    return name if os.path.dirname(path) == os.path.realpath(MEDIA) and os.path.isfile(path) else None

def ffmpeg_path():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def register_features(app, base_dir, media_dir, logger):
    global APP, BASE, MEDIA, LOG
    APP, BASE, MEDIA, LOG = app, base_dir, media_dir, logger
    os.makedirs(MEDIA, exist_ok=True)

    @app.after_request
    def add_feature_cards(response):
        if request.path == "/" and response.mimetype == "text/html":
            try:
                source = response.get_data(as_text=True)
                marker = '<p class="small legal">'
                cards = ""
                if marker in source:
                    response.set_data(source.replace(marker, cards + marker, 1))
            except Exception:
                logger.exception("Não foi possível inserir os cartões de ferramentas")
        return response

    @app.get("/biblioteca")
    def biblioteca():
        items = files_by_type(VIDEO_EXT | AUDIO_EXT | IMAGE_EXT)
        body = """<h1>📚 Minha biblioteca</h1><p>Arquivos encontrados na pasta de mídia do MEDIABOX.</p><section class="card"><div class="grid">{% for f in items %}<article class="item"><b>{{f.name}}</b><p class="small">{{f.kind}} · {{f.size}} · {{f.date}}</p><a href="/extras/media/{{f.name|urlencode}}" target="_blank">Abrir arquivo</a></article>{% else %}<p>Nenhum arquivo encontrado ainda. Baixe um vídeo para começar.</p>{% endfor %}</div></section>"""
        for item in items:
            ext = os.path.splitext(item["name"])[1].lower()
            item["kind"] = "Vídeo" if ext in VIDEO_EXT else "Áudio" if ext in AUDIO_EXT else "Imagem"
            item["size"] = fmt_size(item["size"])
            item["date"] = datetime.fromtimestamp(item["mtime"]).strftime("%d/%m/%Y %H:%M")
        return render_template_string(page("Minha biblioteca", body), items=items)

    @app.get("/extras/media/<path:name>")
    def extra_media(name):
        safe = safe_media_name(name)
        if not safe:
            return jsonify({"erro": "Arquivo não encontrado."}), 404
        return send_from_directory(MEDIA, safe, as_attachment=False, conditional=True)

    @app.get("/musica")
    def musica():
        items = files_by_type(AUDIO_EXT)
        body = """<h1>🎵 Player MEDIABOX</h1>
<p>Ouça suas músicas e reproduza vídeos MP4 diretamente da pasta SnapTube do celular. Os arquivos locais não são enviados ao Render.</p>
<section class="card">
<h2>🎧 Músicas da biblioteca</h2>
<audio id="player" controls preload="metadata"></audio>
<p id="tocando" class="muted">Escolha uma faixa abaixo.</p>
<div class="grid">{% for f in items %}<button type="button" onclick="playTrack({{f.name|tojson}})">▶ {{f.name}}</button>{% else %}<p>Nenhum áudio encontrado na biblioteca do servidor.</p>{% endfor %}</div>
</section>
<section class="card">
<h2>🎵🎬 Áudios MP3 e vídeos MP4 da pasta SnapTube</h2>
<p class="small">Selecione a pasta SnapTube. O MEDIABOX encontrará arquivos MP3 e MP4 e reproduzirá o tipo escolhido sem enviar os arquivos ao Render.</p>
<label for="snaptubeFolder">Selecionar pasta SnapTube</label>
<input id="snaptubeFolder" type="file" accept="audio/*,video/mp4,.mp3,.mp4" multiple webkitdirectory directory>
<p id="folderStatus" class="muted">Nenhuma pasta selecionada.</p>
<div id="mp4List" class="grid"></div>
<audio id="localAudioPlayer" controls preload="metadata" style="display:none"></audio>
<video id="mp4Player" controls playsinline preload="metadata" style="display:none;max-height:65vh"></video>
<p id="mp4Now" class="muted"></p>
</section>
<script>
function playTrack(n){const p=document.getElementById('player');p.src='/extras/media/'+encodeURIComponent(n);p.play();document.getElementById('tocando').textContent='Tocando: '+n}
const folderInput=document.getElementById('snaptubeFolder'),list=document.getElementById('mp4List'),vp=document.getElementById('mp4Player'),ap=document.getElementById('localAudioPlayer'),status=document.getElementById('folderStatus'),now=document.getElementById('mp4Now');
let activeUrl=null;
function clearLocalPlayers(){
  if(activeUrl){URL.revokeObjectURL(activeUrl);activeUrl=null;}
  [vp,ap].forEach(p=>{p.pause();p.removeAttribute('src');p.load();p.style.display='none';});
}
folderInput.addEventListener('change',()=>{
  clearLocalPlayers(); list.replaceChildren(); now.textContent='';
  const selected=Array.from(folderInput.files||[]);
  if(!selected.length){status.textContent='Nenhum arquivo selecionado.';return;}
  // Android/Chrome pode não informar webkitRelativePath corretamente.
  // Como o usuário já escolheu a pasta, filtre apenas pela extensão.
  const fromSnapTube=selected.filter(f=>{
    const name=f.name.toLowerCase();
    return name.endsWith('.mp3') || name.endsWith('.mp4');
  });
  const audios=fromSnapTube.filter(f=>f.name.toLowerCase().endsWith('.mp3')).length;
  const videos=fromSnapTube.length-audios;
  status.textContent=fromSnapTube.length+' arquivo(s) encontrado(s): '+audios+' MP3 e '+videos+' MP4.';
  if(!fromSnapTube.length){status.textContent='Não encontrei arquivos MP3 ou MP4 na seleção. Abra a pasta SnapTube e selecione-a novamente.';return;}
  fromSnapTube.sort((a,b)=>a.name.localeCompare(b.name,'pt-BR'));
  fromSnapTube.forEach(file=>{
    const isAudio=file.name.toLowerCase().endsWith('.mp3');
    const btn=document.createElement('button'); btn.type='button'; btn.textContent=(isAudio?'🎵 ':'▶ ')+file.name;
    btn.addEventListener('click',()=>{
      clearLocalPlayers();
      activeUrl=URL.createObjectURL(file);
      const target=isAudio?ap:vp;
      target.src=activeUrl; target.style.display='block'; target.load();
      target.play().catch(()=>{});
      now.textContent=(isAudio?'Tocando áudio: ':'Reproduzindo vídeo: ')+file.name;
    });
    list.appendChild(btn);
  });
});
window.addEventListener('beforeunload',clearLocalPlayers);
</script>"""
        return render_template_string(page("Player de música", body), items=items)

    @app.get("/editor-video")
    def editor_video():
        body = """<h1>🎬 MEDIABOX Studio</h1>
<p>Editor multipista: importe seu vídeo, adicione textos em camadas, trilha de áudio, filtros e faça a exportação em MP4. Os arquivos só são enviados ao servidor quando você toca em Exportar.</p>
<style>
.studio{display:grid;gap:18px}.studio>.card{position:relative;overflow:hidden;border:1px solid #304b70;background:linear-gradient(145deg,rgba(19,31,54,.98),rgba(8,14,27,.98));box-shadow:0 16px 42px #0005}
.studio h2{display:flex;align-items:center;gap:9px;font-size:19px;letter-spacing:.2px}.studio h2:before{content:"";width:5px;height:22px;border-radius:9px;background:linear-gradient(#70f0e6,#a28cff);display:inline-block}
.stage{position:relative;background:radial-gradient(ellipse at center,#1b2940,#03060d 70%);border:1px solid #3b5275;border-radius:20px;padding:12px;box-shadow:inset 0 0 35px #0008}.stage video{display:block;width:100%;max-height:52vh;object-fit:contain;background:#000;border-radius:13px}
.track{border:1px solid #395274;background:linear-gradient(120deg,#101d32,#0a1221);border-radius:15px;padding:14px;margin:11px 0;box-shadow:0 7px 18px #0003}.trackhead{display:flex;justify-content:space-between;gap:8px;align-items:center}.trackhead b{color:#70f0e6}.track input{margin-bottom:4px}
.studio input,.studio select{background:#080f1d;border-color:#354b6b}.studio input:focus,.studio select:focus{outline:2px solid #70f0e688;border-color:#70f0e6}
.controls{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.controls label,.studio label{font-size:12px;color:#b7c8e0;font-weight:650}.controls input,.controls select{margin-top:6px}
.layerrow{display:grid;grid-template-columns:1fr 1fr;gap:8px}.timeline{padding:15px;border:1px solid #3c5575;border-radius:16px;background:linear-gradient(120deg,#091323,#101b2c)}.timeline .bar{height:11px;border-radius:9px;background:linear-gradient(90deg,#70f0e6,#7b9cff,#c09aff);margin:12px 0;box-shadow:0 0 16px #70f0e633}
.pill{display:inline-block;padding:6px 10px;border:1px solid #426184;border-radius:30px;color:#b9d8ff;font-size:11px;background:#102039}.status{white-space:pre-wrap;color:#70f0e6;font-size:13px;padding:10px 12px;background:#07121f;border:1px solid #263f5e;border-radius:12px;min-height:42px}.danger{background:#311724;color:#ffdce4;border:1px solid #6e3548}.smallbtn{width:auto;padding:8px 11px;margin:0}
.previewtext{color:#fff;font-weight:900;text-shadow:0 2px 5px #000;font-size:28px;text-align:center;pointer-events:none;white-space:pre-wrap;overflow-wrap:anywhere;z-index:3}.text-overlay{position:absolute;inset:12px 12px 48px;display:flex;flex-direction:column;justify-content:center;align-items:center;pointer-events:none;text-align:center}.text-overlay .previewtext{width:100%;margin:0 0 6px}.hidden{display:none!important}
#exportBtn{min-height:52px;font-size:16px;box-shadow:0 8px 25px #70f0e633}#exportBtn:disabled{opacity:.72;filter:saturate(.7)}#downloadResult a{display:block;padding:14px;border-radius:13px;background:#102d2a;border:1px solid #287e70;font-weight:800;text-align:center}
@media(max-width:520px){.controls,.layerrow{grid-template-columns:1fr}.studio>.card{padding:14px}.track{padding:11px}}
</style>
<div class="studio">
<section class="card">
<h2>1. Seu projeto</h2>
<label>Importar vídeo (MP4, MOV, WebM)</label><input id="videoFile" type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm">
<div class="stage"><video id="preview" controls playsinline preload="metadata"></video><div id="textOverlay" class="text-overlay"></div><div id="empty" class="muted">Escolha um vídeo para começar a editar.</div></div>
<div class="timeline"><div class="trackhead"><b>LINHA DO TEMPO</b><span class="pill" id="durationLabel">00:00</span></div><div class="bar"></div><div class="controls"><label>Início (segundos)<input id="trimStart" type="number" min="0" step="0.1" value="0"></label><label>Fim (segundos)<input id="trimEnd" type="number" min="0" step="0.1" value="0"></label></div><p class="small">Defina o trecho que deseja exportar. O vídeo original não será alterado.</p></div>
</section>
<section class="card">
<h2>2. Filtros e ajustes</h2>
<div class="controls"><label>Filtro<select id="filter"><option value="none">Original</option><option value="cinema">Cinema</option><option value="bw">Preto e branco</option><option value="vintage">Vintage</option><option value="warm">Quente</option><option value="cool">Frio</option><option value="sepia">Sépia</option></select></label><label>Proporção<select id="ratio"><option value="original">Original</option><option value="9:16">Vertical 9:16</option><option value="1:1">Quadrado 1:1</option><option value="16:9">Paisagem 16:9</option></select></label><label>Brilho <span id="brightVal">100%</span><input id="brightness" type="range" min="50" max="160" value="100"></label><label>Contraste <span id="contrastVal">100%</span><input id="contrast" type="range" min="50" max="180" value="100"></label><label>Saturação <span id="satVal">100%</span><input id="saturation" type="range" min="0" max="200" value="100"></label><label>Velocidade<select id="speed"><option value="1">1x normal</option><option value="0.5">0,5x</option><option value="0.75">0,75x</option><option value="1.25">1,25x</option><option value="1.5">1,5x</option><option value="2">2x</option></select></label></div>
</section>
<section class="card">
<div class="trackhead"><h2>3. Camadas de texto</h2><button class="smallbtn" type="button" id="addText">＋ Adicionar texto</button></div>
<p class="small">Cada texto vira uma faixa independente. Você escolhe quando aparece, posição, cor e tamanho.</p>
<div id="textTracks"><div class="muted">Nenhuma camada adicionada.</div></div>
</section>
<section class="card">
<h2>4. Camada de áudio</h2>
<label>Adicionar música (MP3, WAV, M4A)</label><input id="audioFile" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg">
<label>Volume da música <span id="volumeVal">80%</span></label><input id="volume" type="range" min="0" max="150" value="80">
<p class="small">A trilha adicionada será usada como áudio do vídeo exportado.</p>
</section>
<section class="card" style="border:1px solid #70f0e6;background:linear-gradient(135deg,#12283a,#17152f)">
<h2>✨ Edição automática por comando</h2>
<p>Envie o vídeo e descreva o resultado. O MEDIABOX ajusta o estilo, texto, formato e procura uma faixa de áudio na sua biblioteca do servidor.</p>
<label>O que você quer que o editor faça?</label>
<textarea id="autoCommand" rows="4" maxlength="700" placeholder="Ex.: edição de carro cinematográfica, cortes dinâmicos, vertical 9:16, texto AGUDO MÁGICO e música chamada Energia Total"></textarea>
<button type="button" id="autoApply">✨ Montar minha edição automaticamente</button>
<p id="autoStatus" class="status">A edição será aplicada aos controles abaixo. Confira a prévia antes de exportar.</p>
</section>
<section class="card">
<h2>5. Exportar projeto</h2>
<div class="controls"><label>Qualidade<select id="quality"><option value="720">HD 720p</option><option value="1080">Full HD 1080p</option><option value="480">SD 480p</option></select></label><label>Nome do vídeo<input id="outputName" maxlength="60" value="mediabox_editado"></label></div>
<button type="button" id="exportBtn">🚀 Exportar vídeo MP4</button>
<p id="exportStatus" class="status">Pronto para editar.</p>
<div id="downloadResult"></div>
</section>
</div>
<script>
const $=id=>document.getElementById(id);
const video=$('preview'),videoFile=$('videoFile'),tracks=$('textTracks');
let videoUrl=null,audioUrl=null,layerCount=0;
const filters={
 none:'none',cinema:'contrast(1.18) saturate(1.2) brightness(.94)',bw:'grayscale(1) contrast(1.12)',vintage:'sepia(.48) contrast(1.08) saturate(.82)',warm:'saturate(1.2) sepia(.16)',cool:'saturate(1.08) hue-rotate(12deg)',sepia:'sepia(1)'
};
function cssFilter(){return filters[$('filter').value]+' brightness('+(+$('brightness').value/100)+') contrast('+(+$('contrast').value/100)+') saturate('+(+$('saturation').value/100)+')'}
function updatePreview(){video.style.filter=cssFilter();video.playbackRate=+$('speed').value;renderTextPreview()}
function renderTextPreview(){
 const overlay=$('textOverlay');overlay.replaceChildren();
 const now=video.currentTime||0;
 tracks.querySelectorAll('.track').forEach(t=>{
  const text=t.querySelector('.txt').value;
  const from=Number(t.querySelector('.from').value||0),to=Number(t.querySelector('.to').value||0);
  if(!text.trim()||now<from||now>to)return;
  const el=document.createElement('div');el.className='previewtext';el.textContent=text;
  el.style.color=t.querySelector('.color').value;
  el.style.fontSize=Math.max(14,Math.min(100,Number(t.querySelector('.size').value)||42))+'px';
  const pos=t.querySelector('.pos').value;
  el.style.alignSelf='stretch';
  if(pos==='top')overlay.style.justifyContent='flex-start';
  else if(pos==='bottom')overlay.style.justifyContent='flex-end';
  else overlay.style.justifyContent='center';
  overlay.appendChild(el);
 });
}
videoFile.addEventListener('change',()=>{
 const file=videoFile.files[0];if(!file)return;
 if(videoUrl)URL.revokeObjectURL(videoUrl);
 videoUrl=URL.createObjectURL(file);video.src=videoUrl;$('empty').classList.add('hidden');
 video.onloadedmetadata=()=>{$('durationLabel').textContent=video.duration.toFixed(1)+'s';$('trimStart').max=video.duration;$('trimEnd').max=video.duration;$('trimEnd').value=video.duration.toFixed(1);$('trimStart').value=0};
 updatePreview();renderTextPreview();
});
video.addEventListener('timeupdate',renderTextPreview);
['filter','brightness','contrast','saturation','speed'].forEach(id=>$(id).addEventListener('input',()=>{
 updatePreview();
 if(id==='brightness')$('brightVal').textContent=$('brightness').value+'%';
 if(id==='contrast')$('contrastVal').textContent=$('contrast').value+'%';
 if(id==='saturation')$('satVal').textContent=$('saturation').value+'%';
}));
$('volume').addEventListener('input',()=>$('volumeVal').textContent=$('volume').value+'%');
$('addText').addEventListener('click',()=>{
 if(tracks.querySelector('.muted'))tracks.innerHTML='';
 const n=++layerCount,div=document.createElement('div');div.className='track';div.dataset.layer=n;
 div.innerHTML='<div class="trackhead"><b>CAMADA DE TEXTO '+n+'</b><button type="button" class="smallbtn danger remove">Remover</button></div><label>Texto</label><input class="txt" maxlength="180" placeholder="Digite seu texto aqui" value="Seu texto"><div class="layerrow"><label>Cor<input class="color" type="color" value="#ffffff"></label><label>Tamanho<input class="size" type="number" min="14" max="100" value="42"></label><label>Início (s)<input class="from" type="number" min="0" step="0.1" value="0"></label><label>Fim (s)<input class="to" type="number" min="0" step="0.1" value="5"></label></div><label>Posição<select class="pos"><option value="center">Centro</option><option value="top">Topo</option><option value="bottom">Embaixo</option></select></label>';
 div.querySelector('.remove').onclick=()=>{div.remove();if(!tracks.children.length)tracks.innerHTML='<div class="muted">Nenhuma camada adicionada.</div>'};
 tracks.appendChild(div);
 div.querySelectorAll('input,select').forEach(el=>el.addEventListener('input',renderTextPreview));
 renderTextPreview();
});
let autoCuts=[];
$('autoApply').addEventListener('click',async()=>{
 const command=$('autoCommand').value.trim(),file=videoFile.files[0];
 if(!file){$('autoStatus').textContent='Escolha seu vídeo primeiro.';return}
 if(!command){$('autoStatus').textContent='Descreva a edição desejada.';return}
 const btn=$('autoApply');btn.disabled=true;$('autoStatus').textContent='A Gemini está analisando o vídeo. Aguarde...';
 try{
  const fd=new FormData();fd.append('video',file,file.name);fd.append('comando',command);fd.append('duracao',String(video.duration||0));
  const resp=await fetch('/editor-video/analisar',{method:'POST',body:fd});const data=await resp.json();if(!resp.ok)throw new Error(data.erro||'Falha na análise.');
  autoCuts=data.cortes||[];if(!autoCuts.length)throw new Error('A IA não encontrou trechos adequados.');
  const set=(id,v)=>{const el=$(id);if(el){el.value=v;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}};
  set('trimStart',autoCuts[0].inicio);set('trimEnd',autoCuts[autoCuts.length-1].fim);
  if(data.filtro)set('filter',data.filtro);if(data.proporcao)set('ratio',data.proporcao);if(data.velocidade)set('speed',data.velocidade);
  updatePreview();$('autoStatus').textContent='🟢 A IA selecionou '+autoCuts.length+' momentos. '+(data.resumo||'Confira os trechos e exporte o MP4.')+'\\n'+autoCuts.map((c,i)=>(i+1)+') '+c.inicio+'s até '+c.fim+'s').join('\\n');
 }catch(err){autoCuts=[];$('autoStatus').textContent='❌ DEU RED! '+err.message}
 finally{btn.disabled=false}
});
$('exportBtn').addEventListener('click',async()=>{
 const file=videoFile.files[0];if(!file){$('exportStatus').textContent='Escolha um vídeo primeiro.';return}
 const start=+$('trimStart').value,end=+$('trimEnd').value;
 if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end>video.duration+0.1){$('exportStatus').textContent='Confira o início e o fim do corte.';return}
 const btn=$('exportBtn');btn.disabled=true;btn.textContent='⏳ Enviando projeto...';$('downloadResult').replaceChildren();$('exportStatus').textContent='Enviando vídeo para o editor...';
 try{
  const data=new FormData();data.append('video',file,file.name);data.append('inicio',start);data.append('fim',end);
  data.append('filtro',$('filter').value);data.append('brilho',$('brightness').value);data.append('contraste',$('contrast').value);data.append('saturacao',$('saturation').value);data.append('velocidade',$('speed').value);data.append('proporcao',$('ratio').value);data.append('qualidade',$('quality').value);data.append('nome',$('outputName').value);
  const layers=[...tracks.querySelectorAll('.track')].map(t=>({text:t.querySelector('.txt').value,color:t.querySelector('.color').value,size:t.querySelector('.size').value,start:t.querySelector('.from').value,end:t.querySelector('.to').value,pos:t.querySelector('.pos').value})).filter(x=>x.text.trim());
  data.append('camadas',JSON.stringify(layers));data.append('cortes',JSON.stringify(autoCuts));const audio=$('audioFile').files[0];if(audio)data.append('audio',audio,audio.name);data.append('volume',$('volume').value);
  const response=await fetch('/editor-video/exportar',{method:'POST',body:data});const raw=await response.text();let result;
  try{result=JSON.parse(raw)}catch(_){throw new Error('O servidor retornou uma página de erro em vez de JSON. Tente um vídeo menor.')}
  if(!response.ok)throw new Error(result.erro||'Não foi possível iniciar a exportação.');
  if(!result.job_id)throw new Error('O servidor não confirmou o início da exportação.');
  $('exportStatus').textContent='⏳ Renderização em segundo plano iniciada...';btn.textContent='⏳ Renderizando...';
  while(true){
   await new Promise(resolve=>setTimeout(resolve,2000));
   const check=await fetch('/editor-video/status/'+encodeURIComponent(result.job_id),{cache:'no-store'});const rawState=await check.text();let state;
   try{state=JSON.parse(rawState)}catch(_){throw new Error('Não foi possível consultar o progresso da renderização.')}
   if(!check.ok)throw new Error(state.erro||'Falha ao consultar a exportação.');
   $('exportStatus').textContent='⏳ '+(state.mensagem||'Renderizando...')+(state.progresso?' ('+state.progresso+'%)':'');
   if(state.status==='done'){$('exportStatus').textContent='🟢 Renderização concluída!';const link=document.createElement('a');link.href=state.url;link.download=state.nome;link.textContent='⬇ Baixar vídeo MP4: '+state.nome;$('downloadResult').appendChild(link);break}
   if(state.status==='error')throw new Error(state.mensagem||'Falha ao renderizar.');
  }
 }catch(e){$('exportStatus').textContent='❌ DEU RED! '+e.message}
 finally{btn.disabled=false;btn.textContent='🚀 Exportar vídeo MP4'}
});
</script>"""
        return page("MEDIABOX Studio", body)

    @app.get("/editor-video/musicas")
    def editor_video_musicas():
        query=(request.args.get("q") or "").strip().lower()[:80]
        items=[]
        for item in files_by_type(AUDIO_EXT):
            name=item["name"]
            if not query or query in name.lower():
                items.append({"name":name,"url":"/editor-video/musica/"+quote(name)})
        return jsonify({"items":items[:20]})

    @app.get("/editor-video/musica/<path:name>")
    def editor_video_musica(name):
        safe=safe_media_name(name)
        if not safe or os.path.splitext(safe)[1].lower() not in AUDIO_EXT:
            return jsonify({"erro":"Faixa de áudio não encontrada na biblioteca."}),404
        return send_from_directory(MEDIA,safe,as_attachment=True)

    @app.post("/editor-video/exportar")
    def editor_video_exportar():
        from werkzeug.utils import secure_filename
        import json
        upload = request.files.get("video")
        if not upload or not upload.filename:
            return jsonify({"erro": "Envie um vídeo para editar."}), 400
        extensao = os.path.splitext(secure_filename(upload.filename))[1].lower()
        if extensao not in VIDEO_EXT:
            return jsonify({"erro": "Formato de vídeo não suportado."}), 400
        temporarios = []
        saida = None
        try:
            inicio = max(0.0, float(request.form.get("inicio", "0")))
            fim = float(request.form.get("fim", "0"))
            duracao = fim - inicio
            cortes_raw = json.loads(request.form.get("cortes", "[]") or "[]")
            if not isinstance(cortes_raw, list) or len(cortes_raw) > 7:
                raise ValueError("A montagem automática aceita até 7 trechos.")
            cortes_ok = []
            for corte in cortes_raw:
                a = max(0.0, float(corte.get("inicio", 0)))
                b = float(corte.get("fim", 0))
                if b <= a or b-a < 1 or b-a > 12:
                    raise ValueError("Um dos trechos escolhidos pela IA é inválido.")
                cortes_ok.append((a,b))
            cortes_ok.sort()
            for idx in range(1, len(cortes_ok)):
                if cortes_ok[idx][0] < cortes_ok[idx-1][1]:
                    raise ValueError("Os trechos automáticos não podem se sobrepor.")
            if cortes_ok:
                duracao = sum(b-a for a,b in cortes_ok)
                inicio = 0.0
            if duracao <= 0 or duracao > 1800:
                raise ValueError("O trecho deve ter duração entre 0 e 30 minutos.")
            qualidade = request.form.get("qualidade", "720")
            if qualidade not in {"480", "720", "1080"}: qualidade = "720"
            proporcao = request.form.get("proporcao", "original")
            filtros = {"none":"", "cinema":"eq=contrast=1.18:saturation=1.2:brightness=-0.04", "bw":"hue=s=0,eq=contrast=1.12", "vintage":"colorchannelmixer=.9:.1:0:0:.05:.85:.1:0:0:.1:.8,eq=contrast=1.08:saturation=.82", "warm":"colorbalance=rs=.08:gs=.02:bs=-.06", "cool":"colorbalance=rs=-.05:gs=.01:bs=.08", "sepia":"colorchannelmixer=.393:.769:.189:.349:.686:.168:.272:.534:.131"}
            filtro = filtros.get(request.form.get("filtro", "none"), "")
            brilho = max(50,min(160,int(request.form.get("brilho","100"))))/100
            contraste = max(50,min(180,int(request.form.get("contraste","100"))))/100
            saturacao = max(0,min(200,int(request.form.get("saturacao","100"))))/100
            vf=[]
            if filtro: vf.append(filtro)
            vf.append(f"eq=brightness={brilho-1:.3f}:contrast={contraste:.3f}:saturation={saturacao:.3f}")
            if proporcao=="9:16": vf.append("scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2")
            elif proporcao=="1:1": vf.append("scale=720:720:force_original_aspect_ratio=decrease,pad=720:720:(ow-iw)/2:(oh-ih)/2")
            elif proporcao=="16:9": vf.append("scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2")
            else: vf.append(f"scale='min({qualidade},iw)':-2")
            camadas=json.loads(request.form.get("camadas","[]"))
            if not isinstance(camadas,list) or len(camadas)>8: raise ValueError("Use no máximo 8 camadas de texto.")
            for camada in camadas:
                texto=str(camada.get("text","")).strip()[:180]
                if not texto: continue
                cor=str(camada.get("color","#ffffff"))
                if not re.fullmatch(r"#[0-9a-fA-F]{6}",cor): cor="#ffffff"
                tamanho=max(14,min(100,int(camada.get("size",42))))
                t0=max(0.0,float(camada.get("start",0))); t1=min(duracao,max(t0,float(camada.get("end",duracao))))
                if t1<=t0: continue
                pos=camada.get("pos","center"); y="h*0.12" if pos=="top" else "h-th-30" if pos=="bottom" else "(h-th)/2"
                tp=os.path.join(MEDIA,f".mb_text_{secrets.token_hex(5)}.txt")
                with open(tp,"w",encoding="utf-8") as tf: tf.write(texto.replace("\r"," ").replace("\n"," "))
                temporarios.append(tp)
                vf.append(f"drawtext=textfile='{tp}':fontcolor={cor}:fontsize={tamanho}:x=(w-tw)/2:y={y}:enable='between(t,{t0:.3f},{t1:.3f})':shadowcolor=black@0.8:shadowx=2:shadowy=2")
            nome=secure_filename(request.form.get("nome","mediabox_editado"))[:50] or "mediabox_editado"
            saida_nome=nome+"_"+str(int(time.time()))+".mp4"; saida=os.path.join(MEDIA,saida_nome)
            entrada=os.path.join(MEDIA,".mb_input_"+secrets.token_hex(5)+extensao); temporarios.append(entrada); upload.save(entrada)
            if cortes_ok:
                partes=[]
                lista=os.path.join(MEDIA,".mb_concat_"+secrets.token_hex(5)+".txt"); temporarios.append(lista)
                for a,b in cortes_ok:
                    parte=os.path.join(MEDIA,".mb_cut_"+secrets.token_hex(5)+".mp4"); temporarios.append(parte)
                    subprocess.run([ffmpeg_path(),"-y","-hide_banner","-loglevel","error","-ss",str(a),"-i",entrada,"-t",str(b-a),"-map","0:v:0","-map","0:a?","-c:v","libx264","-preset","ultrafast","-crf","23","-c:a","aac","-avoid_negative_ts","make_zero",parte],check=True,timeout=240,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                    partes.append(parte)
                with open(lista,"w",encoding="utf-8") as fp:
                    for parte in partes:
                        fp.write("file '" + parte + "'\n")
                uniao=os.path.join(MEDIA,".mb_join_"+secrets.token_hex(5)+".mp4"); temporarios.append(uniao)
                subprocess.run([ffmpeg_path(),"-y","-hide_banner","-loglevel","error","-f","concat","-safe","0","-i",lista,"-c","copy",uniao],check=True,timeout=240,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                entrada=uniao; inicio=0.0
            audio=request.files.get("audio")
            comando=[ffmpeg_path(),"-y","-hide_banner","-loglevel","error","-ss",str(inicio),"-i",entrada]
            if audio and audio.filename:
                audio_ext=os.path.splitext(secure_filename(audio.filename))[1].lower()
                if audio_ext not in AUDIO_EXT: raise ValueError("Formato de áudio não suportado.")
                audio_path=os.path.join(MEDIA,".mb_audio_"+secrets.token_hex(5)+audio_ext); temporarios.append(audio_path); audio.save(audio_path)
                volume=max(0,min(150,int(request.form.get("volume","80"))))/100
                comando += ["-i",audio_path,"-map","0:v:0","-map","1:a:0","-af",f"volume={volume:.2f}","-shortest"]
            else: comando += ["-map","0:v:0","-map","0:a?"]
            velocidade=max(0.5,min(2.0,float(request.form.get("velocidade","1"))))
            vf.append(f"setpts=PTS/{velocidade:.3f}")
            comando += ["-t",str(duracao),"-vf",",".join(vf),"-c:v","libx264","-preset","ultrafast","-crf","23","-c:a","aac","-movflags","+faststart",saida]
            job_id=secrets.token_urlsafe(16); job_files=list(temporarios)
            EXPORT_JOBS[job_id]={"status":"processing","mensagem":"Renderizando vídeo no servidor...","progresso":5}
            def worker():
                try:
                    subprocess.run(comando,check=True,timeout=900,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                    if not os.path.isfile(saida) or os.path.getsize(saida)<1024: raise RuntimeError("O arquivo exportado não passou na verificação.")
                    EXPORT_JOBS[job_id]={"status":"done","mensagem":"Vídeo pronto!","progresso":100,"nome":saida_nome,"url":"/extras/media/"+quote(saida_nome)}
                except subprocess.TimeoutExpired:
                    EXPORT_JOBS[job_id]={"status":"error","mensagem":"A exportação passou de 15 minutos. Tente um vídeo menor ou qualidade inferior."}
                except Exception as exc:
                    LOG.exception("Falha na exportação do MEDIABOX Studio")
                    EXPORT_JOBS[job_id]={"status":"error","mensagem":"Falha ao renderizar: "+str(exc)[:220]}
                finally:
                    for path in job_files:
                        try:
                            if os.path.exists(path): os.remove(path)
                        except OSError: pass
                    if EXPORT_JOBS.get(job_id,{}).get("status")=="error" and saida and os.path.exists(saida):
                        try: os.remove(saida)
                        except OSError: pass
            threading.Thread(target=worker,daemon=True).start()
            temporarios.clear()
            return jsonify({"ok":True,"job_id":job_id,"mensagem":"Exportação iniciada."}),202
        except ValueError as exc:
            return jsonify({"erro":str(exc)}),400
        except Exception as exc:
            LOG.exception("Falha ao preparar exportação do MEDIABOX Studio")
            return jsonify({"erro":"Não foi possível iniciar a exportação: "+str(exc)[:220]}),500
        finally:
            for path in temporarios:
                try:
                    if os.path.exists(path): os.remove(path)
                except OSError: pass

    @app.get("/editor-video/status/<job_id>")
    def editor_video_status(job_id):
        job=EXPORT_JOBS.get(job_id)
        if not job: return jsonify({"erro":"Exportação não encontrada. Inicie novamente."}),404
        return jsonify(job)

    @app.post("/editor-video/analisar")
    def editor_video_analisar():
        import json, mimetypes
        chave = os.environ.get("GEMINI_API_KEY", "").strip()
        if not chave: return jsonify({"erro":"Configure GEMINI_API_KEY nas variáveis do Render."}), 503
        upload = request.files.get("video")
        pedido = (request.form.get("comando") or "").strip()[:700]
        if not upload or not upload.filename or not pedido: return jsonify({"erro":"Envie um vídeo e descreva a edição."}), 400
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in VIDEO_EXT: return jsonify({"erro":"Formato de vídeo não suportado."}), 400
        temp = os.path.join(MEDIA, ".mb_ai_" + secrets.token_hex(8) + ext)
        file_name = None
        try:
            upload.save(temp)
            size = os.path.getsize(temp)
            if size > 120*1024*1024: return jsonify({"erro":"Vídeo acima de 120 MB. Envie um arquivo menor."}), 413
            duration = float(request.form.get("duracao", "0") or 0)
            if duration <= 0 or duration > 1800: return jsonify({"erro":"Não consegui identificar a duração do vídeo (limite de 30 minutos)."}), 400
            mime = mimetypes.guess_type(upload.filename)[0] or "video/mp4"
            root = "https://generativelanguage.googleapis.com"
            start = requests.post(root+"/upload/v1beta/files", headers={"x-goog-api-key":chave,"X-Goog-Upload-Protocol":"resumable","X-Goog-Upload-Command":"start","X-Goog-Upload-Header-Content-Length":str(size),"X-Goog-Upload-Header-Content-Type":mime,"Content-Type":"application/json"}, json={"file":{"display_name":"mediabox-"+secrets.token_hex(4)}}, timeout=30)
            start.raise_for_status(); upload_url = start.headers.get("x-goog-upload-url")
            if not upload_url: raise RuntimeError("Gemini não forneceu o endereço de envio do vídeo.")
            with open(temp,"rb") as stream:
                sent = requests.post(upload_url, headers={"x-goog-api-key":chave,"X-Goog-Upload-Offset":"0","X-Goog-Upload-Command":"upload, finalize","Content-Length":str(size)}, data=stream, timeout=240)
            sent.raise_for_status(); info = sent.json().get("file",{}); file_name=info.get("name"); uri=info.get("uri")
            if not file_name or not uri: raise RuntimeError("Gemini não confirmou o recebimento do vídeo.")
            for _ in range(60):
                if info.get("state") == "ACTIVE": break
                if info.get("state") == "FAILED": raise RuntimeError("Gemini não conseguiu processar este vídeo.")
                time.sleep(2); check=requests.get(root+"/v1beta/"+file_name,headers={"x-goog-api-key":chave},timeout=20); check.raise_for_status(); info=check.json()
            if info.get("state") != "ACTIVE": raise RuntimeError("O processamento do vídeo demorou demais. Tente novamente.")
            prompt = ("Assista ao vídeo e escolha de 3 a 7 melhores momentos REAIS para uma montagem. Pedido: "+pedido+". Duração total: "+str(duration)+" segundos. Retorne intervalos em ordem cronológica, sem sobreposição, dentro da duração, cada um de 1 a 12 segundos. Não invente cenas nem timestamps. Retorne somente JSON válido com as chaves resumo, cortes (lista de objetos com inicio, fim e motivo), filtro, proporcao e velocidade. filtro permitido: none, cinema, bw, vintage, warm, cool ou sepia. proporcao permitida: original, 9:16, 1:1 ou 16:9. velocidade permitida: 0.5, 1, 1.25 ou 1.5.")
            answer=requests.post(root+"/v1beta/models/gemini-3.1-pro-preview:generateContent",headers={"x-goog-api-key":chave},json={"contents":[{"parts":[{"file_data":{"mime_type":mime,"file_uri":uri}},{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0.2}},timeout=240)
            answer.raise_for_status(); raw=answer.json()["candidates"][0]["content"]["parts"][0]["text"]; result=json.loads(raw)
            cuts=[]
            for item in result.get("cortes",[])[:7]:
                try: a=max(0.0,float(item["inicio"])); b=min(duration,float(item["fim"]))
                except (ValueError,TypeError,KeyError): continue
                if b-a>=1: cuts.append({"inicio":round(a,2),"fim":round(b,2),"motivo":str(item.get("motivo",""))[:100]})
            cuts.sort(key=lambda x:x["inicio"]); clean=[]
            for cut in cuts:
                if not clean or cut["inicio"]>=clean[-1]["fim"]: clean.append(cut)
            if not clean: raise ValueError("A IA não encontrou trechos válidos. Tente outro vídeo ou pedido.")
            return jsonify({"ok":True,"resumo":str(result.get("resumo","Montagem analisada."))[:250],"cortes":clean,"filtro":result.get("filtro") if result.get("filtro") in {"none","cinema","bw","vintage","warm","cool","sepia"} else "none","proporcao":result.get("proporcao") if result.get("proporcao") in {"original","9:16","1:1","16:9"} else "original","velocidade":str(result.get("velocidade","1")) if str(result.get("velocidade","1")) in {"0.5","1","1.25","1.5"} else "1"})
        except requests.HTTPError as exc:
            detail=""
            try: detail=exc.response.json().get("error",{}).get("message","")
            except Exception: pass
            return jsonify({"erro":"Erro na API Gemini: "+(detail[:200] or str(exc)[:120])}), 502
        except Exception as exc:
            logger("Falha na análise automática: "+str(exc))
            return jsonify({"erro":"Não foi possível analisar o vídeo: "+str(exc)[:200]}), 500
        finally:
            if file_name:
                try: requests.delete("https://generativelanguage.googleapis.com/v1beta/"+file_name,headers={"x-goog-api-key":chave},timeout=10)
                except Exception: pass
            try:
                if os.path.exists(temp): os.remove(temp)
            except OSError: pass
    @app.route("/mediabox-ia", methods=["GET", "POST"])
    def mediabox_ia():
        resposta = ""
        erro = ""
        if request.method == "POST":
            pergunta = (request.form.get("pergunta") or "").strip()[:4000]
            chave = os.environ.get("GEMINI_API_KEY", "").strip()
            if not pergunta:
                erro = "Escreva uma pergunta."
            elif not chave:
                erro = "A IA ainda não está conectada. Configure GEMINI_API_KEY nas variáveis do Render para ativar as respostas."
            else:
                try:
                    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent"
                    r = requests.post(url, headers={"x-goog-api-key": chave}, json={"contents":[{"parts":[{"text":pergunta}]}]}, timeout=60)
                    r.raise_for_status()
                    dados = r.json()
                    resposta = dados["candidates"][0]["content"]["parts"][0]["text"]
                except Exception as exc:
                    erro = "A IA não respondeu nesta tentativa: " + str(exc)[:250]
        body = """<h1>🤖 MEDIABOX IA</h1><p>Assistente para dúvidas, ideias de vídeos, títulos, roteiros e organização de conteúdo.</p><section class="card"><form method="post"><textarea name="pergunta" rows="5" maxlength="4000" placeholder="Digite sua pergunta..." required>{{pergunta}}</textarea><button>Enviar pergunta</button></form>{% if erro %}<p class="result">{{erro}}</p>{% endif %}{% if resposta %}<h2>Resposta</h2><div class="item result">{{resposta}}</div>{% endif %}</section>"""
        return render_template_string(page("MEDIABOX IA", body), pergunta=request.form.get("pergunta", ""), resposta=resposta, erro=erro)

    @app.route("/clima", methods=["GET", "POST"])
    def clima():
        dados = None
        erro = ""
        cidade = (request.values.get("cidade") or "").strip()[:100]
        if cidade:
            try:
                geo = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": cidade, "count": 1, "language": "pt", "format": "json"}, timeout=12)
                geo.raise_for_status()
                resultados = geo.json().get("results") or []
                if not resultados:
                    raise ValueError("Cidade não encontrada. Confira o nome e tente novamente.")
                local = resultados[0]
                meteo = requests.get("https://api.open-meteo.com/v1/forecast", params={"latitude": local["latitude"], "longitude": local["longitude"], "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m", "daily": "temperature_2m_max,temperature_2m_min", "timezone": "auto"}, timeout=12)
                meteo.raise_for_status()
                atual = meteo.json().get("current", {})
                codigos = {0:"Céu limpo",1:"Predominantemente limpo",2:"Parcialmente nublado",3:"Nublado",45:"Nevoeiro",48:"Nevoeiro com gelo",51:"Garoa leve",53:"Garoa moderada",55:"Garoa intensa",61:"Chuva leve",63:"Chuva moderada",65:"Chuva forte",71:"Neve leve",80:"Pancadas de chuva",81:"Pancadas moderadas",82:"Pancadas fortes",95:"Trovoadas"}
                dados = {"local": local.get("name"), "regiao": local.get("admin1", ""), "pais": local.get("country", ""), "temp": atual.get("temperature_2m"), "sensacao": atual.get("apparent_temperature"), "umidade": atual.get("relative_humidity_2m"), "vento": atual.get("wind_speed_10m"), "descricao": codigos.get(atual.get("weather_code"), "Condição variável")}
            except Exception as exc:
                erro = str(exc)[:300]
        body = """<h1>🌤️ Previsão do tempo</h1><p>Consulte as condições atuais de uma cidade usando dados meteorológicos públicos.</p><section class="card"><form method="get"><label>Cidade</label><input name="cidade" value="{{cidade}}" placeholder="Ex.: Salvador" required><button>Consultar clima</button></form>{% if erro %}<p class="result">{{erro}}</p>{% endif %}{% if dados %}<h2>{{dados.local}}{% if dados.regiao %}, {{dados.regiao}}{% endif %} — {{dados.pais}}</h2><div class="grid"><div class="item"><b>{{dados.temp}} °C</b><p>Temperatura atual</p></div><div class="item"><b>{{dados.sensacao}} °C</b><p>Sensação térmica</p></div><div class="item"><b>{{dados.descricao}}</b><p>Condição</p></div><div class="item"><b>{{dados.umidade}}%</b><p>Umidade</p></div><div class="item"><b>{{dados.vento}} km/h</b><p>Vento</p></div></div><p class="small">Dados consultados no momento da pesquisa. Não substituem alertas oficiais.</p>{% endif %}</section>"""
        return render_template_string(page("Previsão do tempo", body), cidade=cidade, dados=dados, erro=erro)

    @app.get("/editor-imagem")
    def editor_imagem():
        body = """<h1>🖼️ Editor de imagens</h1><p>Edite uma imagem no próprio navegador. O arquivo original não é enviado ao servidor.</p><section class="card"><input id="file" type="file" accept="image/*"><img id="preview" class="preview" hidden alt="Prévia da imagem"><label>Brilho</label><input id="bright" type="range" min="30" max="180" value="100"><label>Contraste</label><input id="contrast" type="range" min="30" max="180" value="100"><div class="grid"><button type="button" id="left">↶ Girar</button><button type="button" id="right">↷ Girar</button></div><button type="button" id="save">Baixar imagem editada</button></section><script>let img=new Image(),angle=0;const f=document.getElementById('file'),p=document.getElementById('preview');function draw(){if(!img.src)return;const c=document.createElement('canvas'),x=c.getContext('2d'),w=img.naturalWidth,h=img.naturalHeight;c.width=(Math.abs(angle)%180===90)?h:w;c.height=(Math.abs(angle)%180===90)?w:h;x.translate(c.width/2,c.height/2);x.rotate(angle*Math.PI/180);x.filter='brightness('+document.getElementById('bright').value+'%) contrast('+document.getElementById('contrast').value+'%)';x.drawImage(img,-w/2,-h/2);p.src=c.toDataURL('image/png');p.hidden=false}f.onchange=()=>{const u=URL.createObjectURL(f.files[0]);img.onload=draw;img.src=u};document.getElementById('bright').oninput=draw;document.getElementById('contrast').oninput=draw;document.getElementById('left').onclick=()=>{angle-=90;draw()};document.getElementById('right').onclick=()=>{angle+=90;draw()};document.getElementById('save').onclick=()=>{if(!img.src)return alert('Escolha uma imagem primeiro.');const a=document.createElement('a');a.href=p.src;a.download='mediabox-imagem-editada.png';a.click()};</script>"""
        return page("Editor de imagens", body)

    @app.route("/capas", methods=["GET", "POST"])
    def capas():
        erro = ""
        resultado = ""
        videos = files_by_type(VIDEO_EXT)
        if request.method == "POST":
            name = safe_media_name(request.form.get("arquivo"))
            if not name or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                erro = "Selecione um vídeo válido."
            else:
                try:
                    segundo = max(0.0, min(36000.0, float(request.form.get("segundo", "1"))))
                    destino = os.path.splitext(name)[0][:70] + "_capa_" + str(int(time.time())) + ".jpg"
                    subprocess.run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(segundo), "-i", os.path.join(MEDIA, name), "-frames:v", "1", "-q:v", "2", os.path.join(MEDIA, destino)], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if not os.path.isfile(os.path.join(MEDIA, destino)):
                        raise RuntimeError("A miniatura não foi criada.")
                    resultado = destino
                except Exception as exc:
                    erro = "Não foi possível gerar a capa: " + str(exc)[:250]
        body = """<h1>🎨 Criador de capas</h1><p>Escolha um vídeo e o segundo que deseja usar como miniatura.</p><section class="card"><form method="post"><label>Vídeo</label><select name="arquivo" required>{% for f in videos %}<option value="{{f.name}}">{{f.name}}</option>{% endfor %}</select><label>Capturar em (segundos)</label><input type="number" name="segundo" min="0" step="0.1" value="1"><button>Gerar capa</button></form>{% if erro %}<p class="result">{{erro}}</p>{% endif %}{% if resultado %}<p>🟢 Capa criada: <a href="/extras/media/{{resultado|urlencode}}" target="_blank">{{resultado}}</a></p><img class="preview" src="/extras/media/{{resultado|urlencode}}" alt="Capa gerada">{% endif %}{% if not videos %}<p>Nenhum vídeo encontrado na biblioteca.</p>{% endif %}</section>"""
        return render_template_string(page("Criador de capas", body), videos=videos, erro=erro, resultado=resultado)

    @app.get("/estatisticas")
    def estatisticas():
        grupos = {"Vídeos": VIDEO_EXT, "Áudios": AUDIO_EXT, "Imagens": IMAGE_EXT}
        resumo = []
        total = 0
        total_bytes = 0
        for label, exts in grupos.items():
            items = files_by_type(exts)
            tamanho = sum(x["size"] for x in items)
            total += len(items)
            total_bytes += tamanho
            resumo.append({"label": label, "count": len(items), "size": fmt_size(tamanho)})
        body = """<h1>📊 Estatísticas do MEDIABOX</h1><p>Resumo dos arquivos armazenados na pasta de mídia.</p><section class="card"><div class="grid"><div class="item"><b>{{total}}</b><p>Arquivos encontrados</p></div><div class="item"><b>{{tamanho}}</b><p>Espaço ocupado</p></div>{% for x in resumo %}<div class="item"><b>{{x.count}}</b><p>{{x.label}} · {{x.size}}</p></div>{% endfor %}</div><p class="small">A contagem considera até 250 arquivos por categoria na tela da biblioteca; as estatísticas somam os arquivos retornados pela pasta.</p></section>"""
        return render_template_string(page("Estatísticas", body), total=total, tamanho=fmt_size(total_bytes), resumo=resumo)


    # Ferramentas adicionais MEDIABOX — processamento real com FFmpeg
    from werkzeug.utils import secure_filename
    import json as _json
    import math as _math

    def tool_upload(field, allowed, prefix):
        upload = request.files.get(field)
        if not upload or not upload.filename:
            raise ValueError("Selecione um arquivo primeiro.")
        ext = os.path.splitext(secure_filename(upload.filename))[1].lower()
        if ext not in allowed:
            raise ValueError("Formato de arquivo não suportado.")
        path = os.path.join(MEDIA, "." + prefix + "_" + secrets.token_hex(8) + ext)
        upload.save(path)
        if os.path.getsize(path) > 120 * 1024 * 1024:
            try: os.remove(path)
            except OSError: pass
            raise ValueError("O arquivo ultrapassa o limite de 120 MB.")
        return path, ext

    def tool_output_name(prefix):
        return prefix + "_" + str(int(time.time())) + "_" + secrets.token_hex(3) + ".mp4"

    def tool_result(title, message="", filename=None, error=""):
        body = """<h1>{{title}}</h1><section class="card">
{% if error %}<p class="result" style="color:#ff9eae">❌ DEU RED! {{error}}</p>{% endif %}
{% if message %}<p class="result">{{message}}</p>{% endif %}
{% if filename %}<p>🟢 Arquivo criado: <a href="/extras/media/{{filename|urlencode}}" download>Baixar {{filename}}</a></p>
{% if filename.lower().endswith(('.mp4','.mov','.webm','.mkv')) %}<video controls class="preview" src="/extras/media/{{filename|urlencode}}"></video>{% elif filename.lower().endswith(('.jpg','.jpeg','.png','.webp')) %}<img class="preview" src="/extras/media/{{filename|urlencode}}" alt="Resultado">{% endif %}{% endif %}
<a class="back" href="/ferramentas">← Todas as ferramentas</a></section>"""
        return render_template_string(page(title, body), title=title, message=message, filename=filename, error=error)

    @app.get("/ferramentas")
    def ferramentas_mediabox():
        body = """<h1>🎚️ Equalizador de áudio</h1><p>O Equalizador PRO continua disponível com 10 bandas, prévia e exportação.</p><section class="card"><a class="item" href="/ferramentas/equalizador">Abrir Equalizador PRO</a></section>"""
        return page("Ferramentas", body)

    @app.route("/ferramentas/capa", methods=["GET", "POST"])
    def ferramenta_capa():
        if request.method == "GET":
            body = """<h1>🖼️ Capturador de capas</h1><p>Extraia um quadro de um vídeo e salve como imagem JPG.</p><section class="card"><form method="post" enctype="multipart/form-data"><label>Vídeo</label><input type="file" name="video" accept="video/*" required><label>Momento em segundos</label><input type="number" name="segundo" min="0" step="0.1" value="1"><button>Extrair capa</button></form></section>"""
            return page("Capturador de capas", body)
        temp = None
        try:
            temp, _ = tool_upload("video", VIDEO_EXT, "mb_cover")
            second = max(0.0, float(request.form.get("segundo", "1")))
            name = "mediabox_capa_" + str(int(time.time())) + ".jpg"
            out = os.path.join(MEDIA, name)
            subprocess.run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(second), "-i", temp, "-frames:v", "1", "-q:v", "2", out], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if not os.path.isfile(out) or os.path.getsize(out) < 500: raise RuntimeError("O FFmpeg não gerou a imagem.")
            return tool_result("Capturador de capas", "Quadro extraído com sucesso.", name)
        except Exception as exc:
            return tool_result("Capturador de capas", error=str(exc)[:240])
        finally:
            if temp and os.path.exists(temp): os.remove(temp)

    @app.route("/ferramentas/compressor", methods=["GET", "POST"])
    def ferramenta_compressor():
        if request.method == "GET":
            body = """<h1>🗜️ Compressor de vídeo</h1><p>Reencode o vídeo em MP4 para tentar reduzir o tamanho. O resultado depende do arquivo original e da qualidade escolhida.</p><section class="card"><form method="post" enctype="multipart/form-data"><label>Vídeo</label><input type="file" name="video" accept="video/*" required><label>Qualidade</label><select name="qualidade"><option value="28">Menor arquivo</option><option value="25" selected>Equilibrada</option><option value="21">Maior qualidade</option></select><button>Comprimir vídeo</button></form><p class="small">Limite de entrada: 120 MB. O processamento ocorre no servidor.</p></section>"""
            return page("Compressor de vídeo", body)
        temp = None
        try:
            temp, _ = tool_upload("video", VIDEO_EXT, "mb_compress")
            crf = request.form.get("qualidade", "25")
            if crf not in {"21", "25", "28"}: crf = "25"
            name = tool_output_name("mediabox_comprimido")
            out = os.path.join(MEDIA, name)
            subprocess.run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-i", temp, "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out], check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if not os.path.isfile(out) or os.path.getsize(out) < 1024: raise RuntimeError("O vídeo comprimido não passou na verificação.")
            original = os.path.getsize(temp); final = os.path.getsize(out)
            msg = "Original: " + fmt_size(original) + " · Resultado: " + fmt_size(final)
            msg += " · Redução: " + (str(round((1-final/original)*100,1))+"%" if final < original else "não houve redução neste arquivo")
            return tool_result("Compressor de vídeo", msg, name)
        except subprocess.TimeoutExpired:
            return tool_result("Compressor de vídeo", error="O processamento excedeu 10 minutos. Tente um vídeo menor.")
        except Exception as exc:
            return tool_result("Compressor de vídeo", error=str(exc)[:240])
        finally:
            if temp and os.path.exists(temp): os.remove(temp)

    @app.route("/ferramentas/informacoes", methods=["GET", "POST"])
    def ferramenta_informacoes():
        if request.method == "GET":
            body = """<h1>🔎 Informações de mídia</h1><p>Identifique duração, resolução, codec, FPS, bitrate e tamanho do arquivo.</p><section class="card"><form method="post" enctype="multipart/form-data"><label>Vídeo ou áudio</label><input type="file" name="media" accept="video/*,audio/*" required><button>Examinar arquivo</button></form></section>"""
            return page("Informações de mídia", body)
        temp = None
        try:
            temp, _ = tool_upload("media", VIDEO_EXT | AUDIO_EXT, "mb_info")
            run = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", temp], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
            raw = run.stderr or ""
            duration = re.search(r"Duration:\s*([0-9:.]+)", raw)
            streams = re.findall(r"Stream #.*", raw)
            resolution = re.search(r"(\d{2,5}x\d{2,5})", raw)
            bitrate = re.search(r"bitrate:\s*([^,\n]+)", raw)
            codecs = []
            for line in streams:
                m = re.search(r"(Video|Audio):\s*([^,]+)", line)
                if m: codecs.append(m.group(1)+": "+m.group(2).strip())
            msg = "Duração: " + (duration.group(1) if duration else "não identificada") + "\n"
            msg += "Resolução: " + (resolution.group(1) if resolution else "não aplicável") + "\n"
            msg += "Bitrate: " + (bitrate.group(1) if bitrate else "não identificado") + "\n"
            msg += "Tamanho: " + fmt_size(os.path.getsize(temp)) + "\n"
            msg += "\n".join(codecs) if codecs else "Streams: detalhes não identificados"
            return tool_result("Informações de mídia", msg)
        except Exception as exc:
            return tool_result("Informações de mídia", error=str(exc)[:240])
        finally:
            if temp and os.path.exists(temp): os.remove(temp)

    @app.route("/ferramentas/velocidade", methods=["GET", "POST"])
    def ferramenta_velocidade():
        if request.method == "GET":
            body = """<h1>⏩ Controlador de velocidade</h1><p>Altere a velocidade e exporte um novo MP4.</p><section class="card"><form method="post" enctype="multipart/form-data"><label>Vídeo</label><input type="file" name="video" accept="video/*" required><label>Velocidade</label><select name="velocidade"><option value="0.5">0,5× — metade</option><option value="0.75">0,75×</option><option value="1.25">1,25×</option><option value="1.5">1,5×</option><option value="2">2× — dobro</option></select><button>Aplicar velocidade</button></form></section>"""
            return page("Controlador de velocidade", body)
        temp = None
        try:
            temp, _ = tool_upload("video", VIDEO_EXT, "mb_speed")
            speed = float(request.form.get("velocidade", "1"))
            if speed not in {0.5, 0.75, 1.25, 1.5, 2.0}: raise ValueError("Velocidade inválida.")
            name = tool_output_name("mediabox_velocidade")
            out = os.path.join(MEDIA, name)
            factor = 1.0/speed
            atempo = []
            remaining = speed
            while remaining < 0.5: atempo.append("atempo=0.5"); remaining /= 0.5
            while remaining > 2.0: atempo.append("atempo=2.0"); remaining /= 2.0
            atempo.append("atempo="+str(round(remaining,3)))
            subprocess.run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-i", temp, "-vf", "setpts=PTS/"+str(speed), "-af", ",".join(atempo), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", out], check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if not os.path.isfile(out) or os.path.getsize(out) < 1024: raise RuntimeError("O vídeo alterado não passou na verificação.")
            return tool_result("Controlador de velocidade", "Velocidade aplicada: "+str(speed)+"×.", name)
        except subprocess.TimeoutExpired:
            return tool_result("Controlador de velocidade", error="O processamento excedeu 10 minutos.")
        except Exception as exc:
            return tool_result("Controlador de velocidade", error=str(exc)[:240])
        finally:
            if temp and os.path.exists(temp): os.remove(temp)

    @app.get("/ferramentas/fundo")
    def ferramenta_fundo():
        body = """<h1>✂️ Removedor de fundo</h1><p>Ferramenta local para remover fundos de cor uniforme (por exemplo, verde, azul ou branco). Não usa IA de segmentação automática.</p><section class="card"><input id="imgFile" type="file" accept="image/*"><label>Cor do fundo a remover</label><input id="keyColor" type="color" value="#00ff00"><label>Tolerância</label><input id="tolerance" type="range" min="0" max="180" value="55"><p id="tolVal" class="small">55</p><img id="imgPreview" class="preview" hidden alt="Prévia"><button id="removeBg" type="button">Remover cor e preparar PNG</button><button id="saveBg" type="button" disabled>Baixar PNG transparente</button><p id="bgStatus" class="result"></p></section><script>
let bgImage=new Image(),bgCanvas=document.createElement('canvas'),bgUrl=null;
const byId=id=>document.getElementById(id);
byId('imgFile').onchange=()=>{const f=byId('imgFile').files[0];if(!f)return;if(bgUrl)URL.revokeObjectURL(bgUrl);bgUrl=URL.createObjectURL(f);bgImage.onload=()=>{const c=byId('imgPreview');c.src=bgUrl;c.hidden=false;byId('bgStatus').textContent='Imagem carregada: '+bgImage.naturalWidth+'×'+bgImage.naturalHeight;};bgImage.src=bgUrl;byId('saveBg').disabled=true;};
byId('tolerance').oninput=()=>byId('tolVal').textContent=byId('tolerance').value;
byId('removeBg').onclick=()=>{if(!bgImage.src)return byId('bgStatus').textContent='Escolha uma imagem primeiro.';const w=bgImage.naturalWidth,h=bgImage.naturalHeight;if(w*h>16000000)return byId('bgStatus').textContent='Imagem muito grande para processar no navegador.';bgCanvas.width=w;bgCanvas.height=h;const ctx=bgCanvas.getContext('2d',{willReadFrequently:true});ctx.drawImage(bgImage,0,0);const data=ctx.getImageData(0,0,w,h),target=byId('keyColor').value;const tr=parseInt(target.slice(1,3),16),tg=parseInt(target.slice(3,5),16),tb=parseInt(target.slice(5,7),16),limit=Number(byId('tolerance').value)*Math.sqrt(3);for(let i=0;i<data.data.length;i+=4){const d=Math.hypot(data.data[i]-tr,data.data[i+1]-tg,data.data[i+2]-tb);if(d<limit)data.data[i+3]=0;else if(d<limit+24)data.data[i+3]=Math.min(data.data[i+3],Math.round((d-limit)/24*255));}ctx.putImageData(data,0,0);byId('imgPreview').src=bgCanvas.toDataURL('image/png');byId('saveBg').disabled=false;byId('bgStatus').textContent='🟢 Fundo da cor selecionada removido. Confira as bordas antes de baixar.';};
byId('saveBg').onclick=()=>{const a=document.createElement('a');a.href=bgCanvas.toDataURL('image/png');a.download='mediabox-fundo-removido.png';a.click();};
</script>"""
        return page("Removedor de fundo", body)

    @app.route("/ferramentas/colagem", methods=["GET", "POST"])
    def ferramenta_colagem():
        if request.method == "GET":
            body = """<h1>🎞️ Colagem de vídeos</h1><p>Combine de 2 a 4 vídeos em uma composição 2×2. O áudio é removido para manter a montagem sincronizada.</p><section class="card"><form method="post" enctype="multipart/form-data" id="collageForm"><label>Vídeo 1</label><input type="file" name="videos" accept="video/*" required><label>Vídeo 2</label><input type="file" name="video_2" accept="video/*" required><label>Vídeo 3 (opcional)</label><input type="file" name="video_3" accept="video/*"><label>Vídeo 4 (opcional)</label><input type="file" name="video_4" accept="video/*"><p id="collageCount" class="small">Selecione pelo menos 2 vídeos. Você pode escolher cada arquivo separadamente.</p><button>Gerar colagem</button></form><script>(function(){const f=document.getElementById('collageForm');if(!f)return;const ins=[...f.querySelectorAll('input[type=file]')],out=document.getElementById('collageCount');function upd(){const n=ins.filter(x=>x.files&&x.files.length).length;out.textContent=n+' vídeo(s) selecionado(s). '+(n<2?'Escolha mais '+(2-n)+'.':n>4?'Limite: 4 vídeos.':'Pronto para gerar.');}ins.forEach(x=>x.addEventListener('change',upd));f.addEventListener('submit',e=>{const n=ins.filter(x=>x.files&&x.files.length).length;if(n<2||n>4){e.preventDefault();out.textContent='Selecione de 2 a 4 vídeos antes de gerar.';}});})();</script></section>"""
            return page("Colagem de vídeos", body)
        temps = []
        try:
            uploads = []
            for field in ("videos", "video_2", "video_3", "video_4"):
                uploads.extend(u for u in request.files.getlist(field) if u and u.filename)
            if len(uploads) < 2 or len(uploads) > 4: raise ValueError("Selecione de 2 a 4 vídeos.")
            paths = []
            total_bytes = 0
            for u in uploads:
                ext = os.path.splitext(secure_filename(u.filename))[1].lower()
                if ext not in VIDEO_EXT: raise ValueError("Um dos arquivos tem formato não suportado.")
                path = os.path.join(MEDIA, ".mb_tile_"+secrets.token_hex(7)+ext)
                u.save(path); temps.append(path)
                size = os.path.getsize(path)
                total_bytes += size
                if size > 40*1024*1024: raise ValueError("Cada vídeo deve ter no máximo 40 MB.")
                if total_bytes > 100*1024*1024: raise ValueError("O total dos vídeos deve ser de no máximo 100 MB.")
                paths.append(path)
            # Limita threads, resolução, taxa de quadros e duração para evitar falta de memória no Render.
            cmd=[ffmpeg_path(),"-y","-hide_banner","-loglevel","error","-threads","1","-filter_threads","1","-filter_complex_threads","1"]
            for p in paths: cmd += ["-t","45","-i",p]
            filters=[]
            for i in range(len(paths)):
                filters.append(f"[{i}:v]fps=24,scale=640:360:force_original_aspect_ratio=increase,crop=640:360,setsar=1[v{i}]")
            layout={2:"0_0|640_0",3:"0_0|640_0|0_360",4:"0_0|640_0|0_360|640_360"}[len(paths)]
            inputs="".join(f"[v{i}]" for i in range(len(paths)))
            filters.append(inputs+f"xstack=inputs={len(paths)}:layout={layout}:fill=black[outv]")
            name=tool_output_name("mediabox_colagem")
            out=os.path.join(MEDIA,name)
            cmd += ["-filter_complex",";".join(filters),"-map","[outv]","-an","-c:v","libx264","-threads","1","-preset","ultrafast","-crf","28","-shortest","-movflags","+faststart",out]
            result = subprocess.run(cmd,check=False,timeout=300,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            if result.returncode != 0:
                detalhe = (result.stderr or b"").decode("utf-8","replace").strip().splitlines()
                LOG.error("Falha FFmpeg na colagem: %s", " | ".join(detalhe[-8:])[:1600])
                raise RuntimeError("O FFmpeg não conseguiu montar estes vídeos. Tente arquivos MP4 menores e com até 45 segundos.")
            if not os.path.isfile(out) or os.path.getsize(out)<1024: raise RuntimeError("A colagem não passou na verificação.")
            return tool_result("Colagem de vídeos", str(len(paths))+" vídeos organizados na composição. Limite: 45 segundos por vídeo.", name)
        except subprocess.TimeoutExpired:
            return tool_result("Colagem de vídeos", error="A montagem excedeu 5 minutos. Tente vídeos menores.")
        except Exception as exc:
            LOG.exception("Falha na ferramenta de colagem")
            return tool_result("Colagem de vídeos", error=str(exc)[:240])
        finally:
            for p in temps:
                try:
                    if os.path.exists(p): os.remove(p)
                except OSError: pass

    @app.route("/ferramentas/equalizador", methods=["GET", "POST"])
    def ferramenta_equalizador():
        bandas = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
        if request.method == "GET":
            body = """<h1>🎚️ Equalizador PRO</h1>
<p>Equalização de 10 bandas, frequência manual, prévia no navegador e exportação MP3.</p>
<style>
.eq-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px}
.eq-band{padding:12px;border:1px solid #30405a;border-radius:13px;background:#0a1220}
.eq-band label{display:block;font-weight:700;color:#dce7f9}
.eq-band input[type=range]{padding:0;margin:12px 0 4px;accent-color:#70f0e6}
.eq-value{color:#70f0e6;font-size:12px}
.eq-actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}
.eq-actions button{margin:4px 0}
.eq-secondary{background:#17243a;color:#e4edff;border:1px solid #344763}
.eq-status{white-space:pre-wrap;padding:11px;border:1px solid #30405a;border-radius:12px;background:#080e19;color:#dce7f9}
#eqGraph{width:100%;height:130px;border:1px solid #30405a;border-radius:12px;background:#050810}
.eq-toggles{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px}
.eq-toggles label{padding:9px;background:#0a1220;border:1px solid #30405a;border-radius:10px}
.eq-toggles input{width:auto;margin:0 6px 0 0}
</style>
<section class="card">
<form method="post" enctype="multipart/form-data" id="eqForm">
<label>🎵 Arquivo de áudio</label>
<input type="file" name="audio" id="eqFile" accept="audio/*,.mp3,.m4a,.aac,.ogg,.wav,.flac" required>
<div class="eq-actions">
<button type="button" class="eq-secondary" id="eqPreview">▶ Prévia ao vivo</button>
<button type="button" class="eq-secondary" id="eqStop">■ Parar prévia</button>
<button type="button" class="eq-secondary" id="eqReset">↺ Zerar ajustes</button>
</div>
<p class="small">A prévia é processada no navegador. A exportação final é processada no servidor.</p>
<h2>🎛️ Presets</h2>
<div class="eq-actions">
<button type="button" class="eq-secondary" data-preset="flat">Original</button>
<button type="button" class="eq-secondary" data-preset="rock">Rock</button>
<button type="button" class="eq-secondary" data-preset="pop">Pop</button>
<button type="button" class="eq-secondary" data-preset="funk">Funk</button>
<button type="button" class="eq-secondary" data-preset="bass">Graves fortes</button>
<button type="button" class="eq-secondary" data-preset="voice">Voz clara</button>
<button type="button" class="eq-secondary" data-preset="podcast">Podcast</button>
<button type="button" class="eq-secondary" data-preset="electronic">Eletrônica</button>
<button type="button" class="eq-secondary" data-preset="classical">Clássica</button>
<button type="button" class="eq-secondary" data-preset="acoustic">Acústica</button>
</div>
<h2>🎚️ Equalizador de 10 bandas</h2>
<div class="eq-grid" id="eqBands">
<div class="eq-band"><label>31 Hz · Subgrave</label><input type="range" name="band_31" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>62 Hz · Grave</label><input type="range" name="band_62" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>125 Hz · Grave</label><input type="range" name="band_125" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>250 Hz · Corpo</label><input type="range" name="band_250" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>500 Hz · Médio</label><input type="range" name="band_500" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>1 kHz · Médio</label><input type="range" name="band_1000" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>2 kHz · Presença</label><input type="range" name="band_2000" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>4 kHz · Brilho</label><input type="range" name="band_4000" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>8 kHz · Agudos</label><input type="range" name="band_8000" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
<div class="eq-band"><label>16 kHz · Ar</label><input type="range" name="band_16000" min="-12" max="12" value="0"><span class="eq-value">0 dB</span></div>
</div>
<h2>🧪 Frequência manual</h2>
<div class="grid">
<label>Frequência (Hz)<input type="number" name="manual_freq" id="manualFreq" min="20" max="20000" step="1" value="500"></label>
<label>Ganho manual (dB)<input type="number" name="manual_gain" id="manualGain" min="-12" max="12" step="0.5" value="0"></label>
</div>
<p class="small">Digite qualquer frequência entre 20 e 20.000 Hz, por exemplo 500 Hz.</p>
<h2>🔊 Ajustes adicionais</h2>
<div class="grid">
<label>Volume de saída (%)<input type="number" name="volume" min="0" max="150" value="100"></label>
<label>Qualidade MP3<select name="quality"><option value="0">Máxima (V0)</option><option value="2" selected>Alta (V2)</option><option value="4">Média (V4)</option><option value="6">Leve (V6)</option></select></label>
<label>Filtro passa-altas<select name="highpass"><option value="0">Desativado</option><option value="30">30 Hz</option><option value="60">60 Hz</option><option value="80">80 Hz</option><option value="100">100 Hz</option></select></label>
<label>Filtro passa-baixas<select name="lowpass"><option value="0">Desativado</option><option value="12000">12 kHz</option><option value="14000">14 kHz</option><option value="16000">16 kHz</option><option value="18000">18 kHz</option></select></label>
</div>
<div class="eq-toggles">
<label><input type="checkbox" name="compressor" value="1">Compressor dinâmico</label>
<label><input type="checkbox" name="limiter" value="1" checked>Limitador de pico</label>
<label><input type="checkbox" name="normalize" value="1">Normalizar volume</label>
<label><input type="checkbox" name="echo" value="1">Eco suave</label>
<label><input type="checkbox" name="stereo" value="1">Ampliar estéreo</label>
</div>
<h2>📊 Analisador de áudio</h2>
<canvas id="eqGraph" width="900" height="130"></canvas>
<audio id="eqAudio" controls style="width:100%;margin-top:10px"></audio>
<p class="small">O gráfico acompanha o áudio durante a prévia. O volume e os efeitos podem variar conforme o navegador.</p>
<button id="eqSubmit" type="submit">🚀 Exportar áudio equalizado</button>
<p class="eq-status" id="eqStatus" role="status">Selecione um áudio para começar. Ganhos limitados a -12 dB até +12 dB para evitar ajustes extremos.</p>
</form></section>
<script>
(()=>{
const form=document.getElementById('eqForm'),file=document.getElementById('eqFile'),audio=document.getElementById('eqAudio'),status=document.getElementById('eqStatus'),canvas=document.getElementById('eqGraph'),ctx=canvas.getContext('2d');
const freqs=[31,62,125,250,500,1000,2000,4000,8000,16000];
const presets={
flat:[0,0,0,0,0,0,0,0,0,0],
rock:[4,5,3,-1,-2,1,3,4,3,2],
pop:[-1,2,4,3,1,2,3,2,1,0],
funk:[5,5,3,0,-2,1,3,3,2,1],
bass:[7,6,5,2,0,0,0,0,-1,-2],
voice:[-2,-1,0,1,3,4,4,2,0,-1],
podcast:[-3,-2,0,2,3,3,2,1,-1,-2],
electronic:[4,5,2,0,-1,1,3,4,4,3],
classical:[0,1,1,0,0,1,2,2,1,1],
acoustic:[1,2,1,1,0,1,2,2,1,0]
};
let objectUrl=null,audioCtx=null,source=null,filters=[],analyser=null,raf=0;
const inputs=freqs.map(f=>form.elements['band_'+f]);
function updateLabels(){inputs.forEach(el=>{el.parentElement.querySelector('.eq-value').textContent=Number(el.value).toFixed(1)+' dB';});}
function setPreset(name){const vals=presets[name]||presets.flat;inputs.forEach((el,i)=>el.value=vals[i]);updateLabels();syncPreview();}
function syncPreview(){if(!audioCtx||!filters.length)return;filters.forEach((node,i)=>node.gain.setTargetAtTime(Number(inputs[i].value),audioCtx.currentTime,0.04));}
inputs.forEach(el=>el.addEventListener('input',()=>{updateLabels();syncPreview();}));
document.querySelectorAll('[data-preset]').forEach(b=>b.addEventListener('click',()=>setPreset(b.dataset.preset)));
document.getElementById('eqReset').addEventListener('click',()=>{setPreset('flat');form.elements.manual_gain.value=0;form.elements.volume.value=100;form.elements.highpass.value='0';form.elements.lowpass.value='0';form.querySelectorAll('.eq-toggles input').forEach(x=>x.checked=x.name==='limiter');});
file.addEventListener('change',()=>{if(objectUrl)URL.revokeObjectURL(objectUrl);if(file.files[0]){objectUrl=URL.createObjectURL(file.files[0]);audio.src=objectUrl;status.textContent='Arquivo carregado: '+file.files[0].name;}});
function draw(){if(!analyser)return;const data=new Uint8Array(analyser.frequencyBinCount);analyser.getByteFrequencyData(data);ctx.clearRect(0,0,canvas.width,canvas.height);const bar=canvas.width/data.length*2;for(let i=0;i<data.length;i+=2){const h=data[i]/255*canvas.height;ctx.fillStyle='#70f0e6';ctx.fillRect(i*bar/2,canvas.height-h,Math.max(1,bar-1),h);}raf=requestAnimationFrame(draw);}
async function stopPreview(){if(raf)cancelAnimationFrame(raf);raf=0;try{audio.pause();}catch(e){}}
document.getElementById('eqStop').addEventListener('click',stopPreview);
document.getElementById('eqPreview').addEventListener('click',async()=>{if(!file.files[0]){status.textContent='Selecione um arquivo de áudio primeiro.';return;}try{await stopPreview();if(!audioCtx){audioCtx=new (window.AudioContext||window.webkitAudioContext)();source=audioCtx.createMediaElementSource(audio);let node=source;freqs.forEach((f,i)=>{const biq=audioCtx.createBiquadFilter();biq.type='peaking';biq.frequency.value=f;biq.Q.value=1;biq.gain.value=Number(inputs[i].value);node.connect(biq);node=biq;filters.push(biq);});analyser=audioCtx.createAnalyser();analyser.fftSize=256;node.connect(analyser);analyser.connect(audioCtx.destination);}else{syncPreview();}audio.currentTime=0;await audioCtx.resume();await audio.play();draw();status.textContent='Prévia ao vivo ativada. Ajuste as bandas enquanto escuta.';}catch(e){status.textContent='Não foi possível iniciar a prévia neste navegador: '+e.message;}});
form.addEventListener('submit',async e=>{
 e.preventDefault();
 const f=file.files[0],btn=document.getElementById('eqSubmit');
 if(!f){status.textContent='Escolha um arquivo de áudio.';return;}
 if(f.size>120*1024*1024){status.textContent='Arquivo acima de 120 MB. Escolha um arquivo menor.';return;}
 const mf=Number(form.elements.manual_freq.value),mg=Number(form.elements.manual_gain.value);
 if(form.elements.manual_gain.value!==''&&(!Number.isFinite(mg)||mg < -12||mg > 12)){status.textContent='O ganho manual precisa ficar entre -12 e +12 dB.';return;}
 if(form.elements.manual_freq.value!==''&&(!Number.isFinite(mf)||mf<20||mf>20000)){status.textContent='A frequência manual precisa ficar entre 20 e 20.000 Hz.';return;}
 btn.disabled=true;btn.textContent='Enviando áudio...';status.textContent='Enviando arquivo para iniciar a exportação...';
 try{
  const payload=new FormData(form);
  // Snapshot explícito das bandas no instante da exportação.
  freqs.forEach((freq,i)=>payload.set('band_'+freq,String(Number(inputs[i].value)||0)));
  const response=await fetch(form.action||location.href,{method:'POST',body:payload});
  const data=await response.json();
  if(!response.ok||!data.ok||!data.job_id)throw new Error(data.erro||'Não foi possível iniciar a exportação.');
  status.textContent='Áudio recebido! Processamento em segundo plano iniciado...';btn.textContent='Processando...';
  const jobUrl='/ferramentas/equalizador/status/'+encodeURIComponent(data.job_id);
  while(true){
   await new Promise(resolve=>setTimeout(resolve,2000));
   const check=await fetch(jobUrl,{cache:'no-store'}),job=await check.json();
   if(!check.ok||job.status==='error')throw new Error(job.mensagem||'A exportação falhou.');
   if(job.status==='done'){
    status.textContent='🟢 VERDE — '+job.mensagem+' ';
    if(job.analise){
      const a=job.analise, o=a.original||{}, x=a.exportado||{};
      const report=document.createElement('div');
      report.style.cssText='margin-top:10px;padding:12px;border:1px solid #304b70;border-radius:10px;color:#dce7f9;line-height:1.6';
      report.textContent='ANÁLISE DO ÁUDIO\\nOriginal: volume médio '+o.volume_medio_db+' dB | pico '+o.pico_maximo_db+' dB\\nMP3 exportado: volume médio '+x.volume_medio_db+' dB | pico '+x.pico_maximo_db+' dB\\n'+a.filtros_solicitados+' filtro(s) equalizador solicitado(s); '+a.filtros_registrados_ffmpeg+' registro(s) encontrado(s) no FFmpeg.\\n'+(a.variacao_volume_medio_db===null?'Comparação de volume indisponível.':'Variação do volume médio: '+a.variacao_volume_medio_db+' dB.');
      status.appendChild(document.createElement('br'));status.appendChild(report);
    }
    const link=document.createElement('a');link.href=job.url;link.download=job.nome||'';link.textContent='⬇ Baixar áudio MP3';link.style.display='inline-block';link.style.marginTop='10px';link.style.padding='12px 16px';link.style.borderRadius='10px';link.style.background='#70f0e6';link.style.color='#08101c';link.style.fontWeight='800';status.appendChild(document.createElement('br'));status.appendChild(link);
    btn.disabled=false;btn.textContent='🚀 Exportar outro áudio';return;
   }
   status.textContent='Processando áudio no servidor... Aguarde, você pode manter esta página aberta.';
  }
 }catch(err){if(window.mediaboxShowError)window.mediaboxShowError(err.message||'Erro ao exportar.','Equalizador PRO','EQUALIZADOR-001','',err.stack||'Falha durante a exportação.');status.textContent='❌ DEU RED! '+(err.message||'Erro ao exportar.');btn.disabled=false;btn.textContent='🚀 Tentar exportar novamente';}
});
updateLabels();
})();
</script>"""
            return page("Equalizador PRO", body)
        temp = None
        out = None
        try:
            temp, ext = tool_upload("audio", AUDIO_EXT, "mb_eq")
            if not os.path.isfile(temp) or os.path.getsize(temp) < 128:
                raise ValueError("O arquivo está vazio ou não contém áudio válido.")
            filters = []
            for freq in bandas:
                raw = request.form.get("band_" + str(freq), "0")
                try:
                    gain = float(raw)
                except (TypeError, ValueError):
                    gain = 0.0
                if not _math.isfinite(gain):
                    gain = 0.0
                gain = max(-12.0, min(12.0, gain))
                if abs(gain) >= 0.05:
                    filters.append(f"equalizer=frequency={freq}:width_type=q:width=1:gain={gain:.1f}")
            manual_raw = (request.form.get("manual_freq") or "").strip()
            manual_gain_raw = (request.form.get("manual_gain") or "0").strip()
            if manual_raw:
                try:
                    manual_freq = float(manual_raw)
                    manual_gain = float(manual_gain_raw)
                except (TypeError, ValueError):
                    raise ValueError("Frequência ou ganho manual inválido.")
                if not _math.isfinite(manual_freq) or not 20 <= manual_freq <= 20000:
                    raise ValueError("A frequência manual deve ficar entre 20 e 20.000 Hz.")
                if not _math.isfinite(manual_gain):
                    raise ValueError("O ganho manual precisa ser um número válido.")
                manual_gain = max(-12.0, min(12.0, manual_gain))
                if abs(manual_gain) >= 0.05:
                    filters.append(f"equalizer=frequency={manual_freq:.1f}:width_type=q:width=1:gain={manual_gain:.1f}")
            try:
                volume = float(request.form.get("volume", "100")) / 100.0
            except (TypeError, ValueError):
                volume = 1.0
            if not _math.isfinite(volume):
                volume = 1.0
            volume = max(0.0, min(1.5, volume))
            if abs(volume - 1.0) > 0.001:
                filters.append(f"volume={volume:.3f}")
            highpass = request.form.get("highpass", "0")
            if highpass in {"30", "60", "80", "100"}:
                filters.append(f"highpass=f={highpass}")
            lowpass = request.form.get("lowpass", "0")
            if lowpass in {"12000", "14000", "16000", "18000"}:
                filters.append(f"lowpass=f={lowpass}")
            if request.form.get("compressor") == "1":
                filters.append("acompressor=threshold=0.125:ratio=2:attack=20:release=250")
            if request.form.get("limiter") == "1":
                filters.append("alimiter=limit=0.95")
            if request.form.get("normalize") == "1":
                filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
            if request.form.get("echo") == "1":
                filters.append("aecho=0.8:0.9:45:0.18")
            if request.form.get("stereo") == "1":
                filters.append("extrastereo=m=1.35")
            audio_filter = ",".join(filters) if filters else "anull"
            band_snapshot = {str(freq): request.form.get("band_" + str(freq), "0") for freq in bandas}
            LOG.info("Equalizador PRO: bandas recebidas=%s; filtros=%s", band_snapshot, audio_filter[:1200])
            quality = request.form.get("quality", "2")
            if quality not in {"0", "2", "4", "6"}:
                quality = "2"
            name = "mediabox_equalizador_pro_" + str(int(time.time())) + "_" + secrets.token_hex(3) + ".mp3"
            out = os.path.join(MEDIA, name)
            # Mapeia explicitamente o fluxo de áudio pelo grafo de filtros.
            command = [
                ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "info",
                "-threads", "1", "-i", temp,
                "-filter_complex", f"[0:a:0]{audio_filter}[audio_processado]",
                "-map", "[audio_processado]", "-vn",
                "-c:a", "libmp3lame", "-q:a", quality,
                "-map_metadata", "-1", out
            ]
            job_id = secrets.token_urlsafe(16)
            temp_job = temp
            temp = None
            EXPORT_JOBS[job_id] = {"status": "processing", "mensagem": "Processando áudio no servidor...", "progresso": 5}

            def analisar_niveis_audio(audio_path):
                """Mede pico e volume médio decodificando o áudio com FFmpeg."""
                probe = subprocess.run(
                    [ffmpeg_path(), "-hide_banner", "-nostats", "-i", audio_path,
                     "-vn", "-af", "volumedetect", "-f", "null", "-"],
                    check=False, timeout=180, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE
                )
                log = (probe.stderr or b"").decode("utf-8", "replace")
                if probe.returncode != 0:
                    raise RuntimeError("Não foi possível analisar os níveis do áudio.")
                mean_match = re.search(r"mean_volume:\s*(-?inf|[-+]?\d+(?:\.\d+)?) dB", log)
                peak_match = re.search(r"max_volume:\s*(-?inf|[-+]?\d+(?:\.\d+)?) dB", log)
                return {
                    "volume_medio_db": mean_match.group(1) if mean_match else "indisponível",
                    "pico_maximo_db": peak_match.group(1) if peak_match else "indisponível"
                }

            def worker_equalizador():
                try:
                    result = subprocess.run(command, check=False, timeout=900, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    ffmpeg_log = (result.stderr or b"").decode("utf-8", "replace")
                    if result.returncode != 0:
                        detail = ffmpeg_log.strip().splitlines()
                        LOG.error("Falha FFmpeg no equalizador: %s", " | ".join(detail[-8:])[:1600])
                        raise RuntimeError("O FFmpeg não conseguiu exportar este áudio. Confira se o arquivo está íntegro e tente MP3, WAV ou M4A.")
                    if not os.path.isfile(out) or os.path.getsize(out) < 500:
                        raise RuntimeError("O arquivo de saída não passou na verificação.")
                    filtros_esperados = audio_filter.count("equalizer=")
                    filtros_confirmados = len(re.findall(r"Parsed_equalizer_\d+", ffmpeg_log))
                    if filtros_esperados and filtros_confirmados < filtros_esperados:
                        LOG.warning("Equalizador: esperados %s filtros; registros FFmpeg encontrados=%s", filtros_esperados, filtros_confirmados)
                    analise_original = analisar_niveis_audio(temp_job)
                    analise_exportado = analisar_niveis_audio(out)
                    mean_original = analise_original["volume_medio_db"]
                    mean_exportado = analise_exportado["volume_medio_db"]
                    comparacao = "Níveis medidos nos dois arquivos."
                    try:
                        delta = float(mean_exportado) - float(mean_original)
                        comparacao = "Volume médio mudou %.2f dB." % delta
                    except (TypeError, ValueError):
                        delta = None
                    diagnostico = {
                        "original": analise_original,
                        "exportado": analise_exportado,
                        "variacao_volume_medio_db": round(delta, 2) if delta is not None else None,
                        "filtros_solicitados": filtros_esperados,
                        "filtros_registrados_ffmpeg": filtros_confirmados,
                        "ganhos_bandas_db": band_snapshot,
                        "filtro_ffmpeg": audio_filter
                    }
                    LOG.info("Equalizador PRO diagnóstico=%s", diagnostico)
                    EXPORT_JOBS[job_id] = {
                        "status": "done",
                        "mensagem": "Exportação concluída. Análise do original e do MP3 final disponível.",
                        "progresso": 100, "nome": name,
                        "url": "/extras/media/" + quote(name),
                        "analise": diagnostico
                    }
                except subprocess.TimeoutExpired:
                    EXPORT_JOBS[job_id] = {"status": "error", "mensagem": "O processamento passou de 15 minutos. Tente um áudio menor."}
                except Exception as exc:
                    LOG.exception("Falha na ferramenta Equalizador PRO")
                    EXPORT_JOBS[job_id] = {"status": "error", "mensagem": str(exc)[:240]}
                finally:
                    try:
                        if temp_job and os.path.exists(temp_job):
                            os.remove(temp_job)
                    except OSError:
                        LOG.warning("Não foi possível remover o áudio temporário do equalizador.")
                    if EXPORT_JOBS.get(job_id, {}).get("status") == "error" and out and os.path.exists(out):
                        try:
                            os.remove(out)
                        except OSError:
                            LOG.warning("Não foi possível remover a exportação incompleta do equalizador.")

            threading.Thread(target=worker_equalizador, daemon=True).start()
            return jsonify({"ok": True, "job_id": job_id, "mensagem": "Exportação iniciada."}), 202
        except ValueError as exc:
            return jsonify({"ok": False, "erro": str(exc)}), 400
        except Exception as exc:
            LOG.exception("Falha ao iniciar a ferramenta Equalizador PRO")
            return jsonify({"ok": False, "erro": "Não foi possível iniciar a exportação: " + str(exc)[:220]}), 500
        finally:
            if temp and os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    LOG.warning("Não foi possível remover o upload temporário do equalizador.")

    @app.get("/ferramentas/equalizador/status/<job_id>")
    def ferramenta_equalizador_status(job_id):
        job = EXPORT_JOBS.get(job_id)
        if not job:
            return jsonify({"status": "error", "mensagem": "Exportação não encontrada. Envie o áudio novamente."}), 404
        return jsonify(job)
