"""Vinhos — backend Flask + SQLite

Endpoints REST usados pelo frontend em `static/index.html`.
Persistência em `/data/vinhos.db` (volume Coolify).
Import automático a partir de link do Vivino via scrape direto (urllib + regex,
sem headless browser — Vivino serve os dados em JSON-LD/JSON embutido no HTML).
"""
import os, re, json, sqlite3, uuid, shutil, datetime as dt, mimetypes
import urllib.request, urllib.error
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, abort, Response

# Em produção (Docker/Coolify) o default é /data — path absoluto real do container.
# Em dev local no Windows, "/data" sem DB_PATH setado vira silenciosamente C:\data
# (drive-relative root), fora do projeto — já causou perda de dado real por engano.
# Fallback seguro: se não é Linux, usa uma pasta local ao lado do app.py.
_default_data_dir = Path("/data") if os.name != "nt" else (Path(__file__).parent / "data")
DB_PATH = Path(os.environ.get("DB_PATH") or (_default_data_dir / "vinhos.db"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (_default_data_dir / "vinhos-uploads"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or (_default_data_dir / "backups"))
BACKUP_KEEP = 50  # nº de snapshots mantidos (mais antigos são descartados)
STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)

# ---------------------------------------------------------------- db

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def backup_db(reason):
    """Snapshot do banco antes de qualquer operação destrutiva (exclusão) e a
    cada boot. Não protege contra corrupção do disco, mas cobre o caso real que
    já aconteceu aqui: exclusão errada (minha, de um bug, ou de um clique)."""
    if not DB_PATH.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        dest = BACKUP_DIR / f"vinhos-{ts}-{reason}.db"
        shutil.copy2(DB_PATH, dest)
        # poda backups antigos, mantém os BACKUP_KEEP mais recentes
        snaps = sorted(BACKUP_DIR.glob("vinhos-*.db"))
        for old in snaps[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
    except Exception as e:
        print(f"[backup] falhou ({reason}): {e}", flush=True)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS vinho (
      id TEXT PRIMARY KEY,
      nome TEXT NOT NULL,
      produtor TEXT NOT NULL,
      safra INTEGER,
      tipo TEXT NOT NULL DEFAULT 'tinto',
      pais TEXT NOT NULL,
      regiao TEXT,
      uvas TEXT,
      volume_ml INTEGER DEFAULT 750,
      teor_alcoolico REAL,
      preco_brl REAL,
      data_compra TEXT,
      local_compra TEXT,
      estoque INTEGER NOT NULL DEFAULT 0,
      rating REAL,
      status_manual TEXT,
      notas_degustacao TEXT,
      foto_url TEXT,
      rating_vivino REAL,
      avaliacoes_vivino INTEGER,
      id_vivino TEXT,
      classificacao TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    con.commit()
    # Migração leve para bancos já existentes (criados antes deste campo existir).
    try:
        con.execute("ALTER TABLE vinho ADD COLUMN classificacao TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe
    con.close()


def calc_status(row):
    if row["status_manual"]:
        return row["status_manual"]
    if (row["estoque"] or 0) > 0:
        return "adega"
    if row["rating"] is not None:
        return "bebido"
    return "wishlist"


def row_to_dict(row):
    d = dict(row)
    d["status"] = calc_status(row)
    return d


# ---------------------------------------------------------------- vivino scraper (real, sem headless browser)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

TIPO_MAP = {
    "red": "tinto", "tinto": "tinto",
    "white": "branco", "branco": "branco",
    "rose": "rose", "rosé": "rose",
    "sparkling": "espumante", "espumante": "espumante",
    "fortified": "fortificado", "fortificado": "fortificado", "dessert": "fortificado",
}


def parse_vivino_id(url):
    m = re.search(r"vivino\.com/(?:[^\s]*?/)?(?:wines|w)/(\d+)", url or "", re.I)
    return m.group(1) if m else None


def extract_vivino_url(text):
    """Extrai a URL limpa de um texto solto (ex: compartilhamento do app do Vivino
    com frase + @menção antes do link). Preserva o path original (slug/formato)
    em vez de reconstruir só a partir do ID — importante pois /wines/<id> às vezes
    404 quando só /w/<id> com slug funciona."""
    m = re.search(r"https?://[^\s]*vivino\.com/[^\s]*", text or "", re.I)
    if not m:
        return None
    return re.sub(r"[.,;:!?)\]}'\"]+$", "", m.group(0))


def scrape_vivino(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", "ignore")

    result = {"nome": None, "produtor": None, "safra": None, "tipo": None,
              "pais": None, "regiao": None, "uvas": None,
              "teor_alcoolico": None, "foto_url": None,
              "rating_vivino": None, "avaliacoes": None,
              "notas_degustacao": None}

    m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
            title = ld.get("name", "") or ""
            ym = re.match(r"^(\d{4})\s+(.+)$", title)
            if ym:
                result["safra"] = int(ym.group(1))
                result["nome"] = ym.group(2).strip()
            else:
                result["nome"] = title
            brand = ld.get("brand", {})
            if isinstance(brand, dict):
                result["produtor"] = brand.get("name")
            imgs = ld.get("image") or []
            if imgs:
                result["foto_url"] = imgs[0]
            agg = ld.get("aggregateRating", {})
            if agg:
                try:
                    v = float(agg.get("ratingValue"))
                    if v > 0:
                        result["rating_vivino"] = v
                except Exception:
                    pass
                try:
                    result["avaliacoes"] = int(agg.get("ratingCount"))
                except Exception:
                    pass
        except Exception:
            pass

    # Nota "overall" (todas as safras) — cobre o caso da safra específica ter poucas
    # avaliações (Vivino esconde o aggregateRating do JSON-LD nesse caso).
    for m2 in re.finditer(
        r'"status"\s*:\s*"Normal"\s*,\s*"ratings_count"\s*:\s*(\d+)\s*,\s*"ratings_average"\s*:\s*([\d.]+)[^{}]*?"vintages_count"\s*:\s*(\d+)',
        html,
    ):
        try:
            avg = float(m2.group(2))
            if avg > 0:
                result["rating_vivino"] = avg
                result["avaliacoes"] = int(m2.group(1))
                break
        except Exception:
            pass

    m = re.search(r'"country":\s*\{[^}]*?"name":"([^"]+)"', html)
    if m:
        result["pais"] = m.group(1)

    m = re.search(r'"region":\s*\{[^}]*?"name":"([^"]+)"', html)
    if m and m.group(1) != "Classificação na região do vinho":
        result["regiao"] = m.group(1)
    if not result["regiao"]:
        m2 = re.search(r'"regions?":\s*\[[^\]]*?"name":"([^"]+)"', html)
        if m2:
            result["regiao"] = m2.group(1)

    grapes = re.findall(r'"grapes?":\s*\[(.*?)\]', html, re.S)
    if grapes:
        names = re.findall(r'"name":"([^"]+)"', grapes[0])
        if names:
            seen, out = set(), []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            result["uvas"] = ", ".join(out)

    m = re.search(r'"wine_type"\s*:\s*"([a-zA-Z]+)"', html)
    if not m:
        m = re.search(r'"type"\s*:\s*"(red|white|rose|sparkling|fortified|dessert)"', html)
    if m:
        result["tipo"] = TIPO_MAP.get(m.group(1).lower())
    if not result["tipo"]:
        m = re.search(r"Vinho (tinto|branco|rosé|rose|espumante|fortificado)", html, re.I)
        if m:
            result["tipo"] = TIPO_MAP.get(m.group(1).lower())

    m = re.search(r'"alcohol"\s*:\s*"?([\d.]+)"?', html)
    if m:
        try:
            v = float(m.group(1))
            if v > 0:
                result["teor_alcoolico"] = v
        except Exception:
            pass

    flavors = re.findall(r'"primary_keywords":\s*\[(.*?)\]', html, re.S)
    if flavors:
        kws = re.findall(r'"name":"([^"]+)"', flavors[0])
        if kws:
            result["notas_degustacao"] = "Perfil coletivo Vivino · notas de " + ", ".join(kws[:8]) + "."

    return result


# ---------------------------------------------------------------- routes: estáticos

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/uploads/<path:fname>")
def uploads(fname):
    return send_from_directory(UPLOAD_DIR, fname)


# ---------------------------------------------------------------- routes: vivino

@app.get("/api/vivino")
def api_vivino():
    raw = (request.args.get("url") or "").strip()
    # tolera texto solto (ex: compartilhamento do app: "Veja esse vinho @Vivino: https://...")
    url = extract_vivino_url(raw) or raw
    vid = parse_vivino_id(url)
    if not vid:
        return jsonify({"error": "URL do Vivino inválida. Use https://www.vivino.com/wines/<id> ou https://www.vivino.com/pt-BR/<slug>/w/<id>"}), 400
    try:
        data = scrape_vivino(url)
        data["id_vivino"] = vid
        return jsonify(data)
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"Vivino retornou HTTP {e.code}. Tente outro link."}), 502
    except Exception as e:
        return jsonify({"error": f"Erro ao raspar: {type(e).__name__}: {e}"}), 500


# ---------------------------------------------------------------- routes: backups

@app.get("/api/backups")
def list_backups():
    if not BACKUP_DIR.exists():
        return jsonify([])
    snaps = sorted(BACKUP_DIR.glob("vinhos-*.db"), reverse=True)
    return jsonify([
        {"arquivo": p.name, "tamanho_kb": round(p.stat().st_size / 1024, 1),
         "modificado_em": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
        for p in snaps
    ])


@app.post("/api/backups/<nome_arquivo>/restaurar")
def restaurar_backup(nome_arquivo):
    origem = BACKUP_DIR / nome_arquivo
    if not origem.exists() or not origem.name.startswith("vinhos-"):
        return jsonify({"error": "Backup não encontrado"}), 404
    backup_db("antes-restaurar")  # nunca restaura sem guardar o estado atual antes
    shutil.copy2(origem, DB_PATH)
    return jsonify({"status": "restaurado", "de": nome_arquivo})


# ---------------------------------------------------------------- routes: CRUD vinhos

CAMPOS_EDITAVEIS = [
    "nome", "produtor", "safra", "tipo", "pais", "regiao", "uvas",
    "volume_ml", "teor_alcoolico", "preco_brl", "data_compra", "local_compra",
    "estoque", "rating", "status_manual", "notas_degustacao", "foto_url",
    "rating_vivino", "avaliacoes_vivino", "id_vivino", "classificacao",
]


@app.get("/api/vinhos")
def list_vinhos():
    con = db()
    rows = con.execute("SELECT * FROM vinho ORDER BY created_at DESC").fetchall()
    con.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.get("/api/vinhos/<vid>")
def get_vinho(vid):
    con = db()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    con.close()
    if not row:
        abort(404)
    return jsonify(row_to_dict(row))


@app.post("/api/vinhos")
def create_vinho():
    payload = request.get_json(force=True) or {}
    if not payload.get("nome") or not payload.get("produtor") or not payload.get("pais"):
        return jsonify({"error": "nome, produtor e pais são obrigatórios"}), 422

    vid = uuid.uuid4().hex[:12]
    now = dt.datetime.utcnow().isoformat()
    campos = {k: payload.get(k) for k in CAMPOS_EDITAVEIS}
    campos["volume_ml"] = campos.get("volume_ml") or 750
    campos["estoque"] = campos.get("estoque") or 0

    con = db()
    cols = ["id", "created_at", "updated_at"] + list(campos.keys())
    vals = [vid, now, now] + list(campos.values())
    placeholders = ", ".join("?" for _ in vals)
    con.execute(f"INSERT INTO vinho ({', '.join(cols)}) VALUES ({placeholders})", vals)
    con.commit()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    con.close()
    return jsonify(row_to_dict(row)), 201


@app.put("/api/vinhos/<vid>")
def update_vinho(vid):
    payload = request.get_json(force=True) or {}
    con = db()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    if not row:
        con.close()
        abort(404)

    campos = {k: payload[k] for k in CAMPOS_EDITAVEIS if k in payload}
    if campos:
        sets = ", ".join(f"{k} = ?" for k in campos.keys())
        vals = list(campos.values()) + [dt.datetime.utcnow().isoformat(), vid]
        con.execute(f"UPDATE vinho SET {sets}, updated_at = ? WHERE id = ?", vals)
        con.commit()

    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    con.close()
    return jsonify(row_to_dict(row))


@app.delete("/api/vinhos/<vid>")
def delete_vinho(vid):
    con = db()
    row = con.execute("SELECT id FROM vinho WHERE id = ?", (vid,)).fetchone()
    if not row:
        con.close()
        abort(404)
    backup_db(f"antes-excluir-{vid}")
    con.execute("DELETE FROM vinho WHERE id = ?", (vid,))
    con.commit()
    con.close()
    return "", 204


@app.post("/api/vinhos/<vid>/estoque")
def ajustar_estoque(vid):
    delta = int((request.get_json(force=True) or {}).get("delta", 0))
    con = db()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    if not row:
        con.close()
        abort(404)
    novo = max(0, (row["estoque"] or 0) + delta)
    con.execute(
        "UPDATE vinho SET estoque = ?, updated_at = ? WHERE id = ?",
        (novo, dt.datetime.utcnow().isoformat(), vid),
    )
    con.commit()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    con.close()
    return jsonify(row_to_dict(row))


@app.post("/api/vinhos/<vid>/consumir")
def consumir(vid):
    payload = request.get_json(force=True) or {}
    con = db()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    if not row:
        con.close()
        abort(404)

    novo_estoque = max(0, (row["estoque"] or 0) - 1)
    rating = payload.get("rating")
    notas = payload.get("notas") or row["notas_degustacao"]
    classificacao = payload.get("classificacao", row["classificacao"])
    con.execute(
        """UPDATE vinho SET estoque = ?, rating = ?, notas_degustacao = ?, classificacao = ?,
           status_manual = NULL, updated_at = ? WHERE id = ?""",
        (novo_estoque, rating, notas, classificacao, dt.datetime.utcnow().isoformat(), vid),
    )
    con.commit()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    con.close()
    return jsonify(row_to_dict(row))


@app.post("/api/vinhos/<vid>/foto")
def upload_foto(vid):
    con = db()
    row = con.execute("SELECT id FROM vinho WHERE id = ?", (vid,)).fetchone()
    if not row:
        con.close()
        abort(404)

    file = request.files.get("file")
    if not file:
        con.close()
        return jsonify({"error": "Nenhum arquivo enviado"}), 422

    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        con.close()
        return jsonify({"error": "Só aceita JPG ou PNG"}), 400

    contents = file.read()
    if len(contents) > 5 * 1024 * 1024:
        con.close()
        return jsonify({"error": "Arquivo maior que 5MB"}), 400

    fname = f"{vid}-{uuid.uuid4().hex[:8]}{ext}"
    (UPLOAD_DIR / fname).write_bytes(contents)
    foto_url = f"/uploads/{fname}"

    con.execute(
        "UPDATE vinho SET foto_url = ?, updated_at = ? WHERE id = ?",
        (foto_url, dt.datetime.utcnow().isoformat(), vid),
    )
    con.commit()
    row = con.execute("SELECT * FROM vinho WHERE id = ?", (vid,)).fetchone()
    con.close()
    return jsonify(row_to_dict(row))


init_db()
backup_db("boot")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9111))
    app.run(host="127.0.0.1", port=port, debug=True)
