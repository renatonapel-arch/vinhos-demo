# Vinhos — demo VPS

App pessoal do Renato para inventário, avaliação e wishlist da coleção de vinhos.

Embedado no [clavis-renato](https://clavis-renato.napel.com.br) → **Vida → Vinhos**.
Standalone em https://vinhos.demos.napel.com.br.

## Stack
- Flask 3 + gunicorn + SQLite (`/data/vinhos.db`)
- Frontend: single-file `static/index.html` (Tailwind-like CSS próprio, sem framework)
- Import automático via link do Vivino: scrape direto (urllib + regex — Vivino serve os
  dados em JSON-LD/JSON embutido no HTML, sem precisar de headless browser)

## Rodar local
```bash
python app.py
# ou
DB_PATH=/tmp/dev-vinhos.db PORT=9111 gunicorn -c gunicorn_conf.py app:app
```
Abre em http://localhost:9111 (ou a porta escolhida).

## Endpoints REST
- `GET /api/vinhos` · `POST /api/vinhos` · `PUT /api/vinhos/:id` · `DELETE /api/vinhos/:id`
- `POST /api/vinhos/:id/estoque` (body: `{"delta": 1}`)
- `POST /api/vinhos/:id/consumir` (body: `{"rating": 4.3, "notas": "...", "data": "2026-07-21"}`)
- `POST /api/vinhos/:id/foto` (multipart, campo `file`)
- `GET /api/vivino?url=https://www.vivino.com/wines/<id>` — scrape real
- `GET /api/backups` — lista snapshots do banco
- `POST /api/backups/<arquivo>/restaurar` — restaura um snapshot (faz backup do estado atual antes)

## Backups automáticos

Snapshot do `vinhos.db` é criado automaticamente:
- A cada boot do backend
- Antes de qualquer `DELETE /api/vinhos/:id`

Ficam em `<data_dir>/backups/vinhos-<timestamp>-<motivo>.db` — mantém os 50 mais recentes.
Pra restaurar um: `GET /api/backups` pra listar, depois `POST /api/backups/<arquivo>/restaurar`.

## Deploy
Push em `main` → deploy manual no Coolify (não tem auto-deploy).
Volume `/data` no Coolify para persistir `vinhos.db` + `/data/vinhos-uploads` + `/data/backups`.
