"""Admin padrão (spec 57 §5.1): rotas de usuários no /admin de todo produto, com o mesmo login do painel do produto.

  GET    /api/painel/usuarios?q=&pagina=   lista (id, e-mail, plano, ativo até, criado, ativo, qa)
  PUT    /api/painel/usuarios/{id}         {"plano": "...", "dias": N}  troca o plano e/ou dá N dias a partir de hoje
  DELETE /api/painel/usuarios/{id}         apaga o usuário e as linhas dele em todas as tabelas (recusa o usuário de QA)
Funciona no SQLite (desenvolvimento) e no Postgres (banco `produtos`).
"""
from __future__ import annotations

import os
import re
import time

from fastapi import HTTPException, Request

QA_PADRAO = "demo@rangeltech.net"


def _agora() -> int:
    return int(time.time())


def _qa(email: str | None) -> bool:
    qas = {QA_PADRAO, (os.environ.get("RT_QA_EMAIL") or "").lower()}
    return bool(email) and email.lower() in qas


def _tabelas_com_usuario(db) -> list[tuple[str, str]]:
    """(tabela, coluna) de toda tabela que guarda o id do usuário, menos a própria `usuarios`."""
    if hasattr(db, "schema"):  # Postgres (produtos_core.banco_pg)
        rows = db.todos("SELECT table_name AS t, column_name AS c FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND column_name IN ('usuario', 'usuario_id') AND table_name <> 'usuarios'")
        return [(r["t"], r["c"]) for r in rows]
    out = []
    for r in db.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name <> 'usuarios'").fetchall():
        nome = r[0]
        cols = {c[1] for c in db.con.execute(f'PRAGMA table_info("{nome}")')}
        for c in ("usuario", "usuario_id"):
            if c in cols:
                out.append((nome, c))
    return out


def _tem_assinaturas(db) -> bool:
    return any(t == "assinaturas" and c == "usuario" for t, c in _tabelas_com_usuario(db))


def registrar(r, db, checar) -> None:
    """Acrescenta as rotas de usuários ao APIRouter do admin do produto (prefixo /api/painel)."""

    def ativo_ate(u: dict) -> int:
        v = int(u.get("ativo_ate") or 0)
        if _tem_assinaturas(db):
            a = db.um("SELECT MAX(ativo_ate) AS m FROM assinaturas WHERE usuario=?", (u["id"],))
            v = max(v, int((a or {}).get("m") or 0))
        return v

    @r.get("/usuarios")
    def listar(request: Request, q: str = "", pagina: int = 0):
        checar(request)
        pagina = max(0, int(pagina))
        rows = db.todos("SELECT id, email, plano, ativo_ate, criado FROM usuarios WHERE email LIKE ? ORDER BY criado DESC LIMIT 100 OFFSET ?",
                        (f"%{q.strip().lower()}%", pagina * 100))
        agora = _agora()
        itens = []
        for u in rows:
            ate = ativo_ate(u)
            itens.append({"id": u["id"], "email": u["email"], "plano": u.get("plano"), "ativo_ate": ate, "criado": u.get("criado"),
                          "ativo": ate > agora, "qa": _qa(u["email"])})
        total = db.um("SELECT COUNT(*) AS n FROM usuarios WHERE email LIKE ?", (f"%{q.strip().lower()}%",))["n"]
        return {"usuarios": itens, "total": total, "pagina": pagina}

    @r.put("/usuarios/{uid}")
    async def alterar(uid: int, request: Request):
        checar(request)
        b = await request.json()
        u = db.um("SELECT * FROM usuarios WHERE id=?", (uid,))
        if not u:
            raise HTTPException(404, "usuário não encontrado")
        plano = str(b.get("plano") if b.get("plano") is not None else (u.get("plano") or "")).strip()[:40]
        if plano and not re.fullmatch(r"[\w.-]{1,40}", plano):
            raise HTTPException(422, "plano inválido")
        dias = b.get("dias")
        ate = int(u.get("ativo_ate") or 0)
        if dias is not None:
            try:
                dias = int(dias)
            except (TypeError, ValueError):
                raise HTTPException(422, "dias inválido")
            if not 0 <= dias <= 3650:
                raise HTTPException(422, "dias entre 0 e 3650")
            ate = _agora() + dias * 86400 if dias else _agora()
        db.exec("UPDATE usuarios SET plano=?, ativo_ate=? WHERE id=?", (plano or None, ate, uid))
        if _tem_assinaturas(db):
            if db.um("SELECT 1 AS x FROM assinaturas WHERE usuario=?", (uid,)):
                db.exec("UPDATE assinaturas SET plano=?, ativo_ate=? WHERE usuario=?", (plano or None, ate, uid))
            else:
                try:
                    db.exec("INSERT INTO assinaturas(usuario,plano,ativo_ate) VALUES(?,?,?)", (uid, plano or None, ate))
                except Exception:  # noqa: BLE001  (assinaturas de algum produto pode exigir outras colunas)
                    pass
        return {"ok": True, "plano": plano, "ativo_ate": ate}

    @r.delete("/usuarios/{uid}")
    def apagar(uid: int, request: Request):
        checar(request)
        u = db.um("SELECT * FROM usuarios WHERE id=?", (uid,))
        if not u:
            raise HTTPException(404, "usuário não encontrado")
        if _qa(u["email"]):
            raise HTTPException(409, "o usuário de QA não pode ser apagado")
        removidas = {}
        for tabela, col in _tabelas_com_usuario(db):
            n = db.exec(f'DELETE FROM "{tabela}" WHERE "{col}"=?', (uid,)).rowcount
            if n:
                removidas[tabela] = n
        db.exec("DELETE FROM usuarios WHERE id=?", (uid,))
        return {"ok": True, "email": u["email"], "linhas_removidas": removidas}
