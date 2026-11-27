import sqlite3, time
from contextlib import contextmanager
from flask import request, jsonify, session, Response

def register(app, db_path, logger=None, event_logger=None):
    @contextmanager
    def connect():
        db=sqlite3.connect(db_path,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS bluevision_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '',
            photo BLOB,
            photo_mime TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_bv_reviews_updated ON bluevision_reviews(updated_at DESC)")

    def summary():
        with connect() as db:
            totals=db.execute("SELECT COUNT(*) total,COALESCE(AVG(stars),0) media FROM bluevision_reviews").fetchone()
            dist={int(r["stars"]):int(r["n"]) for r in db.execute("SELECT stars,COUNT(*) n FROM bluevision_reviews GROUP BY stars")}
            rows=db.execute("SELECT id,display_name,stars,comment,photo_mime,updated_at FROM bluevision_reviews ORDER BY updated_at DESC,id DESC LIMIT 40").fetchall()
        count=int(totals["total"] or 0); avg=round(float(totals["media"] or 0),1)
        return {"total":count,"media":avg,"media_formatada":f"{avg:.1f}".replace(".",","),"distribuicao":{str(i):dist.get(i,0) for i in range(1,6)},"avaliacoes":[{"id":r["id"],"nome":r["display_name"],"estrelas":r["stars"],"comentario":r["comment"],"tem_foto":bool(r["photo_mime"]),"data":r["updated_at"]} for r in rows]}

    @app.get("/avaliacoes/resumo")
    def resumo():
        response=jsonify(summary()); response.headers["Cache-Control"]="no-store"; return response

    @app.get("/avaliacoes/foto/<int:review_id>")
    def photo(review_id):
        with connect() as db: row=db.execute("SELECT photo,photo_mime FROM bluevision_reviews WHERE id=?",(review_id,)).fetchone()
        if not row or not row["photo"] or row["photo_mime"] not in ("image/jpeg","image/png","image/webp"): return "",404
        return Response(row["photo"],mimetype=row["photo_mime"],headers={"Cache-Control":"private,max-age=300","X-Content-Type-Options":"nosniff"})

    @app.post("/avaliacoes/enviar")
    def submit():
        user=str(session.get("mediabox_usuario","")).strip()
        if not user: return jsonify({"erro":"Entre na sua conta para avaliar."}),401
        name=(request.form.get("nome") or user).strip()
        comment=(request.form.get("comentario") or "").strip()
        try: stars=int(request.form.get("estrelas","0"))
        except (ValueError,TypeError): stars=0
        if not 1<=stars<=5: return jsonify({"erro":"Escolha de 1 a 5 estrelas."}),400
        if not name or len(name)>40: return jsonify({"erro":"Nome: máximo de 40 caracteres."}),400
        if len(comment)>600: return jsonify({"erro":"Comentário: máximo de 600 caracteres."}),400
        photo=request.files.get("foto"); blob=None; mime=None
        if photo and photo.filename:
            blob=photo.read(1024*1024+1)
            if len(blob)>1024*1024: return jsonify({"erro":"A foto deve ter até 1 MB."}),413
            if blob.startswith(b"\xff\xd8\xff"): mime="image/jpeg"
            elif blob.startswith(b"\x89PNG\r\n\x1a\n"): mime="image/png"
            elif len(blob)>=12 and blob[:4]==b"RIFF" and blob[8:12]==b"WEBP": mime="image/webp"
            else: return jsonify({"erro":"Envie uma foto JPG, PNG ou WEBP válida."}),400
        now=time.strftime("%Y-%m-%d %H:%M:%S",time.gmtime())
        try:
            with connect() as db:
                old=db.execute("SELECT photo,photo_mime FROM bluevision_reviews WHERE username=?",(user,)).fetchone()
                if blob is None and old: blob,mime=old["photo"],old["photo_mime"]
                db.execute("""INSERT INTO bluevision_reviews(username,display_name,stars,comment,photo,photo_mime,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET
                    display_name=excluded.display_name,stars=excluded.stars,comment=excluded.comment,
                    photo=excluded.photo,photo_mime=excluded.photo_mime,updated_at=excluded.updated_at""",
                    (user,name,stars,comment,blob,mime,now,now))
        except sqlite3.Error:
            if logger: logger.exception("Erro ao salvar avaliação BlueVision")
            return jsonify({"erro":"Não foi possível salvar a avaliação agora."}),500
        if event_logger:
            try: event_logger(user,"avaliacao_bluevision",resultado="sucesso",details=f"{stars} estrela(s)")
            except Exception: pass
        return jsonify({"ok":True,"mensagem":"Avaliação publicada com sucesso.", "resumo":summary()})
