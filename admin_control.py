import os
import json
import sqlite3
import secrets
import string
import shutil
import platform
import sys
import importlib.util
import importlib.metadata
import tempfile
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    request, session, redirect, url_for,
    render_template_string, flash
)
from werkzeug.security import generate_password_hash, check_password_hash

DB = None
ADMIN_FILE = None


def conectar():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def agora():
    return datetime.now().isoformat(timespec="seconds")


def inicializar(app, caminho_db, credenciais):
    global DB, ADMIN_FILE
    DB = caminho_db
    ADMIN_FILE = os.path.join(os.path.dirname(DB), "mediabox_admin_auth.json")

    with conectar() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            username TEXT,
            event TEXT NOT NULL,
            title TEXT,
            platform TEXT,
            result TEXT,
            details TEXT
        );
        """)

        con.execute("""
            INSERT OR IGNORE INTO users
            (username,password_hash,created_at,expires_at,active)
            VALUES (?,?,?,?,1)
        """, (
            credenciais["usuario"],
            credenciais["senha_hash"],
            agora(),
            None
        ))

    # Cria credenciais administrativas sem senha padrão pública.
    env_user = os.environ.get("MEDIABOX_ADMIN_USERNAME")
    env_hash = os.environ.get("MEDIABOX_ADMIN_PASSWORD_HASH")

    if env_user and env_hash:
        admin = {"username": env_user, "password_hash": env_hash}
    elif os.path.exists(ADMIN_FILE):
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            admin = json.load(f)
    else:
        senha = "".join(
            secrets.choice(string.ascii_letters + string.digits)
            for _ in range(20)
        )
        admin = {
            "username": "dono",
            "password_hash": generate_password_hash(senha)
        }
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(admin, f, indent=2)
        try:
            os.chmod(ADMIN_FILE, 0o600)
        except OSError:
            pass

        print("\n" + "=" * 45)
        print("BLUEVISION - SENHA INICIAL DO ADMINISTRADOR")
        print("Usuário:", admin["username"])
        print("Senha:", senha)
        print("GUARDE ESTA SENHA. ELA NÃO SERÁ EXIBIDA NOVAMENTE.")
        print("=" * 45 + "\n")

    app.config["MEDIABOX_ADMIN"] = admin

    @app.get("/admin/login")
    @app.post("/admin/login")
    def admin_login():
        if session.get("mediabox_admin"):
            return redirect(url_for("admin_painel"))

        erro = ""
        if request.method == "POST":
            usuario = request.form.get("username", "").strip()
            senha = request.form.get("password", "")
            dados = app.config["MEDIABOX_ADMIN"]

            if (
                usuario == dados["username"]
                and check_password_hash(dados["password_hash"], senha)
            ):
                session.permanent = True
                session["mediabox_admin"] = True
                session["mediabox_admin_user"] = usuario
                registrar_evento(usuario, "login_admin", resultado="sucesso")
                return redirect(url_for("admin_painel"))

            erro = "Usuário ou senha administrativos incorretos."

        return render_template_string("""
        <!doctype html><html lang="pt-BR"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Admin Login | BLUEVISION</title>
        <style>
        *{box-sizing:border-box}body{margin:0;background:#080d18;color:#eef4ff;
        font-family:Arial;min-height:100vh;display:grid;place-items:center;padding:20px}
        main{width:min(420px,100%);padding:28px;border:1px solid #30415e;
        border-radius:20px;background:#111c2e}
        h1{font-size:25px}p{color:#9aabc5}input,button{width:100%;padding:14px;
        margin-top:12px;border-radius:10px;font-size:15px}
        input{background:#080e19;border:1px solid #30415e;color:white}
        button{border:0;background:linear-gradient(100deg,#70f0e6,#8aa8ff);
        color:#07101b;font-weight:bold}.erro{color:#ff9ba9}
        a{color:#70f0e6}
        </style><main><p>BLUEVISION / SEGURANÇA</p><h1>ADMIN CONTROL</h1>
        <p>Entre com as credenciais administrativas.</p>
        <form method="post">
        <input name="username" placeholder="Usuário administrador" required>
        <input name="password" type="password" placeholder="Senha administrativa" required>
        <button>Entrar no painel</button></form><p class="muted">Acesso lembrado neste dispositivo por até 90 dias. Use “Sair do admin” em aparelhos compartilhados.</p>
        <p class="erro">{{ erro }}</p><a href="/">Voltar ao BlueVision</a>
        </main></html>
        """, erro=erro)

    def exigir_admin(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not session.get("mediabox_admin"):
                return redirect(url_for("admin_login"))
            return func(*args, **kwargs)
        return wrapper

    @app.get("/admin")
    @exigir_admin
    def admin_painel():
        with conectar() as con:
            total = con.execute("SELECT COUNT(*) FROM logs WHERE event='download'").fetchone()[0]
            sucessos = con.execute(
                "SELECT COUNT(*) FROM logs WHERE event='download' AND result='sucesso'"
            ).fetchone()[0]
            falhas = con.execute(
                "SELECT COUNT(*) FROM logs WHERE event='download' AND result='falha'"
            ).fetchone()[0]
            usuarios = con.execute(
                "SELECT * FROM users ORDER BY id DESC"
            ).fetchall()
            logs = con.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT 80"
            ).fetchall()

        return render_template_string("""
        <!doctype html><html lang="pt-BR"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>ADMIN CONTROL | BLUEVISION</title>
        <style>
        *{box-sizing:border-box}body{margin:0;background:#080d18;color:#eef4ff;
        font-family:Arial;padding:20px}main{max-width:1100px;margin:auto}
        header{display:flex;justify-content:space-between;align-items:center;gap:10px;
        flex-wrap:wrap}h1{font-size:25px}h2{font-size:19px}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
        .card{background:#111c2e;border:1px solid #293d58;border-radius:15px;
        padding:16px;margin:12px 0}.number{font-size:27px;font-weight:bold;color:#70f0e6}
        input,select,button{padding:11px;border-radius:9px;border:1px solid #344966;
        background:#080e19;color:white;margin:4px;max-width:100%}
        button{cursor:pointer;background:#173a53;border-color:#39718c}
        .danger{background:#512b3a}.ok{color:#70f0e6}.muted{color:#9aabc5}
        .tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;
        min-width:650px}td,th{text-align:left;padding:10px;border-bottom:1px solid #293d58;
        font-size:13px}th{color:#70f0e6}form.inline{display:inline}
        a{color:#70f0e6}small{color:#9aabc5}
        </style><main>
        <header><div><p class="muted">BLUEVISION / CENTRAL ADMINISTRATIVA</p>
        <h1>🔐 ADMIN CONTROL</h1></div>
        <div><a href="/admin/diagnostico">🔎 Diagnóstico PRO</a> · <a href="/admin/limpeza">🧹 Limpeza PRO</a> · <a href="/">Abrir BlueVision</a>
        <form class="inline" method="post" action="/admin/sair">
        <button class="danger">Sair do admin</button></form></div></header>

        <section class="grid">
        <div class="card"><small>DOWNLOADS</small><div class="number">{{ total }}</div></div>
        <div class="card"><small>SUCESSOS</small><div class="number">{{ sucessos }}</div></div>
        <div class="card"><small>FALHAS</small><div class="number">{{ falhas }}</div></div>
        <div class="card"><small>USUÁRIOS</small><div class="number">{{ usuarios|length }}</div></div>
        </section>

        <section class="card"><h2>＋ Criar usuário</h2>
        <form method="post" action="/admin/users/create">
        <input name="username" placeholder="Nome de usuário" required>
        <input name="password" type="password" placeholder="Senha inicial" required>
        <select name="days"><option value="7">7 dias</option>
        <option value="15">15 dias</option><option value="30" selected>30 dias</option>
        <option value="60">60 dias</option><option value="90">90 dias</option>
        <option value="0">Permanente</option></select>
        <button>Criar conta</button></form>
        <p class="muted">{{ mensagem }}</p></section>

        <section class="card"><h2>Gerenciar usuários</h2><div class="tablewrap">
        <table><tr><th>Usuário</th><th>Criado</th><th>Validade</th><th>Estado</th><th>Ações</th></tr>
        {% for u in usuarios %}<tr><td>{{ u.username }}</td>
        <td>{{ u.created_at }}</td><td>{{ u.expires_at or 'Permanente' }}</td>
        <td>{{ 'Ativo' if u.active else 'Bloqueado' }}</td><td>
        <form class="inline" method="post" action="/admin/users/{{u.id}}/toggle">
        <button>{{ 'Bloquear' if u.active else 'Desbloquear' }}</button></form>
        <form class="inline" method="post" action="/admin/users/{{u.id}}/renew">
        <select name="days"><option value="7">7 dias</option><option value="30">30 dias</option>
        <option value="90">90 dias</option><option value="0">Permanente</option></select>
        <button>Renovar</button></form>
        <form class="inline" method="post" action="/admin/users/{{u.id}}/delete"
        onsubmit="return confirm('Excluir esta conta?')">
        <button class="danger">Excluir</button></form></td></tr>{% endfor %}
        </table></div></section>

        <section class="card"><h2>Registros recentes</h2><div class="tablewrap">
        <table><tr><th>Data</th><th>Usuário</th><th>Evento</th><th>Vídeo / detalhe</th>
        <th>Plataforma</th><th>Resultado</th></tr>
        {% for l in logs %}<tr><td>{{l.created_at}}</td><td>{{l.username or '-'}}</td>
        <td>{{l.event}}</td><td>{{l.title or l.details or '-'}}</td>
        <td>{{l.platform or '-'}}</td><td>{{l.result or '-'}}</td></tr>{% endfor %}
        </table></div></section>
        </main></html>
        """, total=total, sucessos=sucessos, falhas=falhas,
        usuarios=usuarios, logs=logs,
        mensagem=session.pop("admin_message", ""))

    @app.get("/admin/diagnostico")
    @exigir_admin
    def admin_diagnostico():
        return render_template_string("""
        <!doctype html><html lang="pt-BR"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>DIAGNÓSTICO PRO | BLUEVISION</title>
        <style>
        *{box-sizing:border-box}body{margin:0;padding:18px;background:#080d18;color:#eef4ff;font-family:Arial}
        main{max-width:950px;margin:auto}.card{background:#111c2e;border:1px solid #293d58;border-radius:16px;padding:18px;margin:14px 0}
        h1{font-size:26px}.muted{color:#9aabc5}.btn{display:inline-block;padding:13px 18px;border-radius:10px;background:linear-gradient(100deg,#70f0e6,#8aa8ff);color:#07101b;text-decoration:none;font-weight:bold;border:0;cursor:pointer}
        table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #293d58}th{color:#70f0e6}
        .ok{color:#70f0e6;font-weight:bold}.warn{color:#ffd58c;font-weight:bold}.bad{color:#ff9ba9;font-weight:bold}
        .wrap{overflow-x:auto}a{color:#70f0e6}
        </style><main>
        <p class="muted">BLUEVISION / FERRAMENTA ADMINISTRATIVA</p><h1>🔎 DIAGNÓSTICO PRO</h1>
        <p class="muted">Verificação do ambiente, dependências, armazenamento e configuração. Valores secretos nunca são exibidos.</p>
        <section class="card"><form method="post"><button class="btn">▶ EXECUTAR DIAGNÓSTICO</button></form>
        <p><a href="/admin">← Voltar ao Admin Controle</a></p>
        {% if checks %}<p class="muted">Executado em {{ executed_at }} · {{ ok_count }} aprovado(s) · {{ warn_count }} alerta(s) · {{ bad_count }} falha(s)</p>
        <div class="wrap"><table><tr><th>Verificação</th><th>Estado</th><th>Resultado</th></tr>
        {% for item in checks %}<tr><td>{{item.name}}</td><td class="{{item.css}}">{{item.status}}</td><td>{{item.detail}}</td></tr>{% endfor %}
        </table></div>{% endif %}</section></main></html>
        """, checks=None, executed_at="", ok_count=0, warn_count=0, bad_count=0)

    @app.post("/admin/diagnostico")
    @exigir_admin
    def executar_admin_diagnostico():
        checks = []
        def add(name, status, detail):
            css = "ok" if status == "🟢 VERDE" else "warn" if status == "⚠️ ALERTA" else "bad"
            checks.append({"name": name, "status": status, "detail": str(detail), "css": css})

        add("Aplicação Flask", "🟢 VERDE", "A rota administrativa respondeu normalmente.")
        add("Python", "🟢 VERDE", platform.python_version() + " · " + sys.platform)

        for module, label, package in (
            ("yt_dlp", "yt-dlp", "yt-dlp"),
            ("requests", "Requests", "requests"),
            ("flask", "Flask", "Flask"),
            ("googleapiclient", "Cliente Google API", "google-api-python-client"),
        ):
            try:
                installed = importlib.util.find_spec(module) is not None
                version = importlib.metadata.version(package) if installed else ""
                add(label, "🟢 VERDE" if installed else "⚠️ ALERTA",
                    ("Instalado · versão " + version) if installed else "Pacote não encontrado neste ambiente.")
            except Exception:
                add(label, "⚠️ ALERTA", "Não foi possível confirmar a versão do pacote.")

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                if importlib.util.find_spec("imageio_ffmpeg"):
                    import imageio_ffmpeg
                    candidate = imageio_ffmpeg.get_ffmpeg_exe()
                    if os.path.isfile(candidate):
                        ffmpeg = candidate
            except Exception:
                pass
        add("FFmpeg", "🟢 VERDE" if ffmpeg else "⚠️ ALERTA",
            ("Disponível: " + ffmpeg) if ffmpeg else "Executável FFmpeg não localizado.")

        for key, label in (
            ("YOUTUBE_API_KEY", "YouTube Data API"),
            ("GEMINI_API_KEY", "Gemini API"),
            ("MEDIABOX_ADMIN_USERNAME", "Usuário admin por variável"),
            ("MEDIABOX_ADMIN_PASSWORD_HASH", "Hash admin por variável"),
        ):
            present = bool(os.environ.get(key))
            add(label, "🟢 VERDE" if present else "⚠️ ALERTA",
                "Configurada (valor oculto)." if present else "Variável ausente ou vazia.")

        try:
            usage = shutil.disk_usage(media_dir)
            free_gb = usage.free / (1024 ** 3)
            add("Armazenamento", "🟢 VERDE" if free_gb >= 0.1 else "⚠️ ALERTA",
                f"{media_dir} · {free_gb:.2f} GB livres.")
        except Exception as exc:
            add("Armazenamento", "❌ DEU RED!", "Não foi possível consultar o espaço: " + str(exc)[:180])

        try:
            with tempfile.NamedTemporaryFile(prefix=".mediabox_diag_", dir=media_dir, delete=True):
                pass
            add("Gravação na pasta de mídia", "🟢 VERDE", "Teste de escrita e remoção concluído.")
        except Exception as exc:
            add("Gravação na pasta de mídia", "❌ DEU RED!", str(exc)[:180])

        try:
            with conectar() as con:
                result = con.execute("PRAGMA integrity_check").fetchone()[0]
            add("Banco de dados SQLite", "🟢 VERDE" if result == "ok" else "❌ DEU RED!", result)
        except Exception as exc:
            add("Banco de dados SQLite", "❌ DEU RED!", str(exc)[:180])

        ok_count = sum(x["status"] == "🟢 VERDE" for x in checks)
        warn_count = sum(x["status"] == "⚠️ ALERTA" for x in checks)
        bad_count = sum(x["status"] == "❌ DEU RED!" for x in checks)
        registrar_evento(session.get("mediabox_admin_user"), "diagnostico_admin",
                         resultado="sucesso" if bad_count == 0 else "falha",
                         details=f"{ok_count} verde(s), {warn_count} alerta(s), {bad_count} falha(s)")
        return render_template_string("""
        <!doctype html><html lang="pt-BR"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>DIAGNÓSTICO PRO | BLUEVISION</title>
        <style>
        *{box-sizing:border-box}body{margin:0;padding:18px;background:#080d18;color:#eef4ff;font-family:Arial}
        main{max-width:950px;margin:auto}.card{background:#111c2e;border:1px solid #293d58;border-radius:16px;padding:18px;margin:14px 0}
        h1{font-size:26px}.muted{color:#9aabc5}.btn{display:inline-block;padding:13px 18px;border-radius:10px;background:linear-gradient(100deg,#70f0e6,#8aa8ff);color:#07101b;text-decoration:none;font-weight:bold;border:0;cursor:pointer}
        table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #293d58}th{color:#70f0e6}
        .ok{color:#70f0e6;font-weight:bold}.warn{color:#ffd58c;font-weight:bold}.bad{color:#ff9ba9;font-weight:bold}.wrap{overflow-x:auto}a{color:#70f0e6}
        </style><main><p class="muted">BLUEVISION / FERRAMENTA ADMINISTRATIVA</p><h1>🔎 DIAGNÓSTICO PRO</h1>
        <section class="card"><p class="muted">Executado em {{ executed_at }} · {{ ok_count }} aprovado(s) · {{ warn_count }} alerta(s) · {{ bad_count }} falha(s)</p>
        <div class="wrap"><table><tr><th>Verificação</th><th>Estado</th><th>Resultado</th></tr>
        {% for item in checks %}<tr><td>{{item.name}}</td><td class="{{item.css}}">{{item.status}}</td><td>{{item.detail}}</td></tr>{% endfor %}
        </table></div><form method="post"><button class="btn">↻ EXECUTAR NOVAMENTE</button></form><p><a href="/admin">← Voltar ao Admin Controle</a></p></section></main></html>
        """, checks=checks, executed_at=agora(), ok_count=ok_count,
             warn_count=warn_count, bad_count=bad_count)

    @app.post("/admin/users/create")
    @exigir_admin
    def admin_criar_usuario():
        usuario = request.form.get("username", "").strip()
        senha = request.form.get("password", "")
        dias = int(request.form.get("days", "30"))
        if not usuario or len(usuario) > 40 or len(senha) < 8:
            session["admin_message"] = "Use usuário válido e senha com pelo menos 8 caracteres."
        else:
            validade = (datetime.now() + timedelta(days=dias)).isoformat(timespec="seconds") if dias else None
            try:
                with conectar() as con:
                    con.execute("""
                    INSERT INTO users(username,password_hash,created_at,expires_at,active)
                    VALUES(?,?,?,?,1)
                    """, (usuario, generate_password_hash(senha), agora(), validade))
                registrar_evento(session.get("mediabox_admin_user"), "criou_usuario", details=usuario)
                session["admin_message"] = "Usuário criado com sucesso."
            except sqlite3.IntegrityError:
                session["admin_message"] = "Esse nome de usuário já existe."
        return redirect(url_for("admin_painel"))

    @app.post("/admin/users/<int:user_id>/toggle")
    @exigir_admin
    def admin_toggle_usuario(user_id):
        with conectar() as con:
            con.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (user_id,))
        registrar_evento(session.get("mediabox_admin_user"), "alterou_usuario", details=str(user_id))
        return redirect(url_for("admin_painel"))

    @app.post("/admin/users/<int:user_id>/renew")
    @exigir_admin
    def admin_renovar_usuario(user_id):
        dias = int(request.form.get("days", "30"))
        validade = (datetime.now() + timedelta(days=dias)).isoformat(timespec="seconds") if dias else None
        with conectar() as con:
            con.execute("UPDATE users SET expires_at=?,active=1 WHERE id=?", (validade, user_id))
        registrar_evento(session.get("mediabox_admin_user"), "renovou_usuario", details=str(user_id))
        return redirect(url_for("admin_painel"))

    @app.post("/admin/users/<int:user_id>/delete")
    @exigir_admin
    def admin_excluir_usuario(user_id):
        with conectar() as con:
            con.execute("DELETE FROM users WHERE id=?", (user_id,))
        registrar_evento(session.get("mediabox_admin_user"), "excluiu_usuario", details=str(user_id))
        return redirect(url_for("admin_painel"))

    @app.post("/admin/sair")
    @exigir_admin
    def admin_sair():
        usuario = session.get("mediabox_admin_user", "admin")
        session.pop("mediabox_admin", None)
        session.pop("mediabox_admin_user", None)
        registrar_evento(usuario, "logout_admin")
        return redirect(url_for("inicio"))


    # Registra as ferramentas extras do MEDIABOX sem alterar o fluxo de login.
    media_dir = os.environ.get("MEDIABOX_STORAGE_DIR")
    if media_dir:
        media_dir = os.path.abspath(media_dir)
    elif os.path.isdir("/storage/emulated/0"):
        media_dir = "/storage/emulated/0/Movies"
    else:
        media_dir = os.path.join(os.path.dirname(DB), "media")
    from mediabox_features import register_features
    register_features(app, os.path.dirname(DB), media_dir, app.logger)

    # Central de Limpeza PRO: inventário completo e remoção conservadora
    # de arquivos temporários antigos. Dados do usuário, logs, banco e
    # configurações nunca entram na lista de exclusão.
    TEMP_EXTENSOES_SEGURAS = {".part", ".ytdl", ".tmp", ".temp", ".download", ".aria2"}
    IDADE_MINIMA_LIMPEZA = 24 * 60 * 60

    def inventariar_media():
        raiz = os.path.realpath(media_dir)
        arquivos = []
        pastas = 0
        total_bytes = 0
        candidatos = []
        agora_ts = datetime.now().timestamp()

        if not os.path.isdir(raiz):
            os.makedirs(raiz, exist_ok=True)

        for atual, dirs, nomes in os.walk(raiz, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(atual, d))]
            pastas += len(dirs)
            for nome in nomes:
                caminho = os.path.join(atual, nome)
                try:
                    if os.path.islink(caminho) or not os.path.isfile(caminho):
                        continue
                    real = os.path.realpath(caminho)
                    if os.path.commonpath([raiz, real]) != raiz:
                        continue
                    stat = os.stat(caminho)
                    tamanho = stat.st_size
                    total_bytes += tamanho
                    relativo = os.path.relpath(caminho, raiz)
                    ext = os.path.splitext(nome)[1].lower()
                    item = {
                        "name": relativo, "size": tamanho,
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
                        "kind": "TEMPORÁRIO" if ext in TEMP_EXTENSOES_SEGURAS else
                                "LOG" if ext in {".log", ".log.1"} else "ARQUIVO"
                    }
                    arquivos.append(item)
                    if ext in TEMP_EXTENSOES_SEGURAS and (agora_ts - stat.st_mtime) >= IDADE_MINIMA_LIMPEZA:
                        candidatos.append((caminho, relativo, tamanho))
                except (OSError, ValueError):
                    continue

        return {
            "files": sorted(arquivos, key=lambda x: x["name"].lower()),
            "folders": pastas, "bytes": total_bytes,
            "candidates": candidatos
        }

    @app.get("/admin/limpeza")
    @exigir_admin
    def admin_limpeza():
        inventario = inventariar_media()
        token = session.setdefault("admin_limpeza_token", secrets.token_urlsafe(32))
        exibidos = inventario["files"][:300]
        return render_template_string("""
        <!doctype html><html lang="pt-BR"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>LIMPEZA PRO | BLUEVISION</title>
        <style>
        *{box-sizing:border-box}body{margin:0;padding:18px;background:#080d18;color:#eef4ff;font-family:Arial}
        main{max-width:1100px;margin:auto}.muted{color:#9aabc5}.card{background:#111c2e;border:1px solid #293d58;border-radius:16px;padding:18px;margin:14px 0}
        h1{font-size:26px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}
        .metric{font-size:25px;color:#70f0e6;font-weight:bold}.tablewrap{overflow-x:auto}
        table{width:100%;border-collapse:collapse;min-width:620px}td,th{text-align:left;padding:10px;border-bottom:1px solid #293d58;font-size:13px}
        th{color:#70f0e6}a{color:#70f0e6}.btn{padding:13px 18px;border-radius:10px;border:0;background:linear-gradient(100deg,#70f0e6,#8aa8ff);color:#07101b;font-weight:bold;cursor:pointer}
        .danger{background:#713347;color:#fff}.safe{color:#70f0e6}.warn{color:#ffd58c}.mutedbox{color:#9aabc5;font-size:13px;line-height:1.6}
        </style><main>
        <p class="muted">BLUEVISION / FERRAMENTA ADMINISTRATIVA</p><h1>🧹 CENTRAL DE LIMPEZA PRO</h1>
        <p class="muted">Inventário da pasta de mídia: {{ root }}</p>
        <section class="grid">
        <div class="card"><small>ARQUIVOS</small><div class="metric">{{ file_count }}</div></div>
        <div class="card"><small>PASTAS</small><div class="metric">{{ folder_count }}</div></div>
        <div class="card"><small>ESPAÇO OCUPADO</small><div class="metric">{{ total_size }}</div></div>
        <div class="card"><small>ESPAÇO POTENCIALMENTE LIBERÁVEL</small><div class="metric">{{ candidate_size }}</div><small>{{ candidate_count }} temporário(s) antigo(s)</small></div>
        </section>
        <section class="card"><h2>🧹 Limpeza segura</h2>
        <p class="mutedbox">O botão remove somente arquivos temporários com mais de 24 horas: .part, .ytdl, .tmp, .temp, .download e .aria2. Vídeos, áudios exportados, imagens, logs, banco de dados, credenciais e configurações são preservados.</p>
        <form method="post" action="/admin/limpeza/executar" onsubmit="return confirm('CONFIRMAR LIMPEZA? Somente temporários com mais de 24 horas serão removidos. Vídeos, áudios, logs e dados do MEDIABOX serão preservados.');">
        <input type="hidden" name="csrf_token" value="{{ token }}">
        <button class="btn danger" {% if candidate_count == 0 %}disabled{% endif %}>🗑️ LIMPAR TUDO — {{ candidate_count }} arquivo(s)</button>
        </form>
        {% if message %}<p class="safe">{{ message }}</p>{% endif %}
        </section>
        <section class="card"><h2>📂 Inventário de arquivos</h2><p class="mutedbox">Mostrando {{ shown_count }} de {{ file_count }} arquivo(s). Os arquivos são listados sem abrir ou modificar seu conteúdo.</p>
        <div class="tablewrap"><table><tr><th>Arquivo</th><th>Tipo</th><th>Tamanho</th><th>Modificado</th></tr>
        {% for x in files %}<tr><td>{{ x.name }}</td><td>{{ x.kind }}</td><td>{{ x.size_h }}</td><td>{{ x.modified }}</td></tr>{% endfor %}
        </table></div></section>
        <p><a href="/admin">← Voltar ao Admin Controle</a></p>
        </main></html>
        """, root=media_dir, file_count=file_count if False else len(inventario["files"]),
        folder_count=inventario["folders"], total_size=human_size(inventario["bytes"]),
        candidate_size=human_size(sum(x[2] for x in inventario["candidates"])),
        candidate_count=len(inventario["candidates"]), files=[
            {**x, "size_h": human_size(x["size"])} for x in exibidos
        ], shown_count=len(exibidos), token=token, message=session.pop("limpeza_message", ""))

    def human_size(value):
        value = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024

    @app.post("/admin/limpeza/executar")
    @exigir_admin
    def executar_admin_limpeza():
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("admin_limpeza_token", "")):
            return "Solicitação inválida. Atualize a página e tente novamente.", 400

        inventario = inventariar_media()
        removidos = 0
        bytes_liberados = 0
        erros = 0
        for caminho, relativo, tamanho in inventario["candidates"]:
            try:
                # Revalida o caminho e a idade imediatamente antes de excluir.
                raiz = os.path.realpath(media_dir)
                real = os.path.realpath(caminho)
                if os.path.commonpath([raiz, real]) != raiz or os.path.islink(caminho):
                    erros += 1
                    continue
                stat = os.stat(caminho)
                if datetime.now().timestamp() - stat.st_mtime < IDADE_MINIMA_LIMPEZA:
                    continue
                if os.path.splitext(caminho)[1].lower() not in TEMP_EXTENSOES_SEGURAS:
                    erros += 1
                    continue
                os.remove(caminho)
                removidos += 1
                bytes_liberados += tamanho
            except OSError:
                erros += 1

        registrar_evento(session.get("mediabox_admin_user"), "limpeza_admin",
                         resultado="sucesso" if erros == 0 else "parcial",
                         details=f"{removidos} arquivo(s), {human_size(bytes_liberados)} liberados, {erros} erro(s)")
        session["limpeza_message"] = (
            f"🟢 VERDE — {removidos} arquivo(s) temporário(s) removido(s); "
            f"{human_size(bytes_liberados)} liberados; {erros} erro(s)."
        )
        return redirect(url_for("admin_limpeza"))

def validar_usuario(usuario, senha):
    usuario = (usuario or "").strip().casefold()

    with conectar() as con:
        usuarios = con.execute("SELECT * FROM users").fetchall()

        u = next(
            (item for item in usuarios
             if item["username"].strip().casefold() == usuario),
            None
        )

        if not u or not u["active"]:
            return False

        if u["expires_at"]:
            try:
                if datetime.fromisoformat(u["expires_at"]) < datetime.now():
                    con.execute(
                        "UPDATE users SET active=0 WHERE id=?",
                        (u["id"],)
                    )
                    return False
            except ValueError:
                return False

        return check_password_hash(u["password_hash"], senha)

def registrar_evento(usuario, evento, title=None, platform=None,
                     resultado=None, details=None):
    with conectar() as con:
        con.execute("""
        INSERT INTO logs(created_at,username,event,title,platform,result,details)
        VALUES(?,?,?,?,?,?,?)
        """, (agora(), usuario, evento, title, platform, resultado, details))
