import os
import sqlite3
from datetime import datetime, timezone
from flask import jsonify, request, session, redirect, url_for, render_template_string

ACHIEVEMENTS = {
    "first_download": ("Primeiro download", "Conclua seu primeiro download.", "⬇️", 1),
    "downloads_5": ("Colecionador", "Conclua 5 downloads.", "📦", 5),
    "downloads_25": ("Explorador", "Conclua 25 downloads.", "🚀", 25),
    "preview_audio": ("Ouvido atento", "Use a prévia de áudio.", "🎧", 1),
    "preview_video": ("Cinéfilo", "Use a prévia de vídeo.", "🎬", 1),
}

def register(app, db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    def connect():
        con = sqlite3.connect(db_path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS mb_achievement_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            event TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mb_achievement_user
        ON mb_achievement_events(username, event);
        """)

    def current_user():
        if not session.get("mediabox_autenticado"):
            return None
        return str(session.get("mediabox_usuario") or "").strip() or None

    def record(username, event):
        if not username:
            return
        with connect() as con:
            con.execute(
                "INSERT INTO mb_achievement_events(username,event,created_at) VALUES(?,?,?)",
                (username, event, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            )

    app.extensions["mediabox_achievement_record"] = record

    @app.get("/api/conquistas")
    def achievements_api():
        username = current_user()
        if not username:
            return jsonify({"erro": "Faça login para ver suas conquistas."}), 401
        with connect() as con:
            counts = {
                row["event"]: row["total"]
                for row in con.execute(
                    "SELECT event, COUNT(*) AS total FROM mb_achievement_events WHERE username=? GROUP BY event",
                    (username,)
                ).fetchall()
            }
        downloads = counts.get("download", 0)
        progress = {
            "first_download": downloads,
            "downloads_5": downloads,
            "downloads_25": downloads,
            "preview_audio": counts.get("preview_audio", 0),
            "preview_video": counts.get("preview_video", 0),
        }
        unlocked = []
        for key, (name, description, icon, target) in ACHIEVEMENTS.items():
            value = progress[key]
            unlocked.append({
                "id": key, "name": name, "description": description,
                "icon": icon, "progress": min(value, target), "target": target,
                "unlocked": value >= target
            })
        return jsonify({"usuario": username, "conquistas": unlocked,
                        "desbloqueadas": sum(1 for x in unlocked if x["unlocked"])})

    @app.post("/api/conquistas/evento")
    def achievements_event():
        username = current_user()
        if not username:
            return jsonify({"erro": "Faça login para continuar."}), 401
        data = request.get_json(silent=True) or {}
        event = str(data.get("evento") or "")
        if event not in {"preview_audio", "preview_video"}:
            return jsonify({"erro": "Evento inválido."}), 400
        record(username, event)
        return jsonify({"ok": True}), 201

    @app.get("/conquistas")
    def achievements_page():
        if not current_user():
            return redirect(url_for("login", next="/conquistas"))
        page = r"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Conquistas • BLUEVISION</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:18px;background:radial-gradient(ellipse at 10% 0%,#183b54,transparent 40%),#080b14;color:#f4f7ff;font-family:Inter,system-ui,Arial}
main{max-width:900px;margin:auto}.top,.card{border:1px solid #30415e;border-radius:18px;background:#111b2d;padding:17px;margin:12px 0}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}a{color:#70f0e6;text-decoration:none}
h1{margin:5px 0;font-size:25px}.muted{color:#9aabc5;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:11px}
.item{padding:15px;border:1px solid #344763;border-radius:15px;background:#0b1322}.icon{font-size:27px}.item h3{margin:8px 0}.bar{height:8px;background:#26354c;border-radius:20px;overflow:hidden;margin:12px 0}.bar i{display:block;height:100%;background:linear-gradient(90deg,#70f0e6,#8aa8ff,#c09aff)}
.state{font-size:11px;color:#70f0e6;font-weight:800}.locked{opacity:.72}button{padding:10px 14px;border:1px solid #435777;border-radius:11px;background:#17243a;color:#fff}
</style><main><header class="top"><div><div class="muted">BLUEVISION · PERFIL</div><h1>🏆 Central de conquistas</h1><div class="muted" id="summary">Carregando seu progresso...</div></div><a href="/">← Voltar ao BLUEVISION</a></header>
<section class="card"><p class="muted">Suas medalhas são vinculadas à sua conta. O progresso é atualizado após downloads concluídos e uso das prévias.</p><div class="grid" id="list"></div></section></main>
<script>
async function load(){const r=await fetch('/api/conquistas',{cache:'no-store'});if(!r.ok){document.getElementById('summary').textContent='Não foi possível carregar as conquistas.';return}
const d=await r.json();document.getElementById('summary').textContent=d.desbloqueadas+' de '+d.conquistas.length+' conquistas desbloqueadas · '+d.usuario;
document.getElementById('list').innerHTML=d.conquistas.map(x=>'<article class="item '+(x.unlocked?'':'locked')+'"><div class="icon">'+x.icon+'</div><h3>'+x.name+'</h3><p class="muted">'+x.description+'</p><div class="bar"><i style="width:'+Math.round(x.progress/x.target*100)+'%"></i></div><div class="state">'+(x.unlocked?'🟢 DESBLOQUEADA':'PROGRESSO: '+x.progress+'/'+x.target)+'</div></article>').join('')}
load();
</script></html>"""
        return render_template_string(page)

    @app.get("/previa")
    def advanced_preview():
        if not current_user():
            return redirect(url_for("login", next="/previa"))
        page = r"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Prévia avançada • BLUEVISION</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:18px;background:radial-gradient(ellipse at 10% 0%,#183b54,transparent 40%),#080b14;color:#f4f7ff;font-family:Inter,system-ui,Arial}
main{max-width:850px;margin:auto}.top,.panel{border:1px solid #30415e;border-radius:18px;background:#111b2d;padding:17px;margin:12px 0}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}a{color:#70f0e6;text-decoration:none}
h1{font-size:25px;margin:5px 0}p,.muted{color:#9aabc5;font-size:13px;line-height:1.6}
input{width:100%;padding:13px;border:1px solid #344763;border-radius:12px;background:#080e19;color:white}
video,audio{display:block;width:100%;max-height:55vh;margin-top:15px;border-radius:14px;background:#050914}
video[hidden],audio[hidden]{display:none}dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
dl div{padding:11px;border:1px solid #30415e;border-radius:12px;background:#0b1322}dt{color:#9aabc5;font-size:11px}dd{margin:5px 0 0;overflow-wrap:anywhere;font-size:13px}
</style><main><header class="top"><div><div class="muted">BLUEVISION · MEDIA LAB</div><h1>🎞️ Prévia avançada</h1><div class="muted">Confira o arquivo localmente antes de exportar.</div></div><a href="/">← Voltar</a></header>
<section class="panel"><p>Selecione um arquivo de áudio ou vídeo. A prévia acontece neste dispositivo; o arquivo não é enviado ao servidor.</p>
<input id="file" type="file" accept="audio/*,video/*"><video id="video" controls playsinline hidden></video><audio id="audio" controls hidden></audio>
<h3>Informações técnicas</h3><dl id="details"><div><dt>Arquivo</dt><dd>Aguardando seleção</dd></div></dl></section></main>
<script>
let objectUrl='';const file=document.getElementById('file'),video=document.getElementById('video'),audio=document.getElementById('audio'),details=document.getElementById('details');
file.addEventListener('change',async()=>{const f=file.files&&file.files[0];if(!f)return;if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=URL.createObjectURL(f);
const isVideo=f.type.startsWith('video/');video.hidden=!isVideo;audio.hidden=isVideo;video.pause();audio.pause();video.removeAttribute('src');audio.removeAttribute('src');(isVideo?video:audio).src=objectUrl;(isVideo?video:audio).load();
const rows=[['Nome',f.name],['Tipo MIME',f.type||'Não identificado'],['Tamanho',(f.size/1024/1024).toFixed(2)+' MB'],['Última modificação',new Date(f.lastModified).toLocaleString('pt-BR')]];
if(isVideo){await new Promise(resolve=>{video.onloadedmetadata=resolve;video.onerror=resolve});rows.push(['Resolução',video.videoWidth+' × '+video.videoHeight],['Duração',Number.isFinite(video.duration)?video.duration.toFixed(2)+' s':'Indisponível']);}
details.innerHTML=rows.map(x=>'<div><dt>'+x[0]+'</dt><dd>'+String(x[1]).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))+'</dd></div>').join('');
fetch('/api/conquistas/evento',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({evento:isVideo?'preview_video':'preview_audio'})}).catch(()=>{});
});
window.addEventListener('beforeunload',()=>{if(objectUrl)URL.revokeObjectURL(objectUrl)});
</script></html>"""
        return render_template_string(page)

    @app.after_request
    def add_feature_links(response):
        if request.path == "/" and response.status_code == 200 and response.mimetype == "text/html":
            try:
                html = response.get_data(as_text=True)
                marker = '<p class="small legal">'
                links = ('<section style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:18px 0">'
                         '<a href="/conquistas" style="display:block;padding:17px;border:1px solid #405475;border-radius:16px;background:#111b2d;color:#70f0e6;text-decoration:none;font-weight:800">🏆 Conquistas BLUEVISION <small style="display:block;color:#9aabc5;font-weight:400;margin-top:6px">Medalhas e progresso da sua conta</small></a>'
                         '<a href="/previa" style="display:block;padding:17px;border:1px solid #405475;border-radius:16px;background:#111b2d;color:#70f0e6;text-decoration:none;font-weight:800">🎞️ Prévia avançada <small style="display:block;color:#9aabc5;font-weight:400;margin-top:6px">Reproduza e analise arquivos locais</small></a></section>')
                if marker in html and "/conquistas" not in html:
                    response.set_data(html.replace(marker, links + marker, 1))
            except Exception:
                app.logger.exception("Falha ao inserir atalhos BLUEVISION")
        return response
