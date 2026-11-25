"""Admin padrão (spec 57 §5.1): usuários no /admin, no SQLite e (com PRODUTOS_DATABASE_URL) no Postgres."""
import os, secrets, sqlite3, threading, time
from contextlib import contextmanager
import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from produtos_core import admin_usuarios

ESQ = ("CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, criado INTEGER, plano TEXT, ativo_ate INTEGER);"
       "CREATE TABLE IF NOT EXISTS assinaturas (usuario INTEGER PRIMARY KEY, plano TEXT, ativo_ate INTEGER);"
       "CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY, usuario INTEGER, texto TEXT);")


class BancoSQLite:
    def __init__(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False); self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQ); self._t = threading.RLock()
    def exec(self, sql, args=()):
        with self._t:
            c = self.con.execute(sql, args); self.con.commit(); return c
    def um(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone(); return dict(r) if r else None
    def todos(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]


def bancos():
    yield "sqlite", BancoSQLite
    if os.environ.get("PRODUTOS_DATABASE_URL"):
        from produtos_core.banco_pg import Banco
        yield "postgres", lambda: Banco(os.environ["PRODUTOS_DATABASE_URL"], "teste_admin_" + secrets.token_hex(4), ESQ)


@pytest.fixture(params=[n for n, _ in bancos()])
def cli(request):
    db = dict(bancos())[request.param]()
    r = APIRouter(prefix="/api/painel")

    def checar(req: Request):
        if req.headers.get("x-ok") != "1":
            raise HTTPException(401, "entre no painel")
    admin_usuarios.registrar(r, db, checar)
    app = FastAPI(); app.include_router(r)
    for e in ("ana@x.com", "demo@rangeltech.net", "bia@x.com"):
        db.exec("INSERT INTO usuarios(email,criado,plano,ativo_ate) VALUES(?,?,?,?)", (e, int(time.time()), "light", 0))
    ana = db.um("SELECT id FROM usuarios WHERE email='ana@x.com'")["id"]
    db.exec("INSERT INTO pedidos(usuario,texto) VALUES(?,?)", (ana, "a")); db.exec("INSERT INTO pedidos(usuario,texto) VALUES(?,?)", (ana, "b"))
    return TestClient(app, headers={"x-ok": "1"}), db, ana


def test_listar_e_login(cli):
    c, db, _ = cli
    assert TestClient(c.app).get("/api/painel/usuarios").status_code == 401
    j = c.get("/api/painel/usuarios").json()
    assert j["total"] == 3 and {u["email"] for u in j["usuarios"] if u["qa"]} == {"demo@rangeltech.net"}
    assert [u["email"] for u in c.get("/api/painel/usuarios?q=BIA").json()["usuarios"]] == ["bia@x.com"]


def test_trocar_plano_e_dar_dias(cli):
    c, db, ana = cli
    r = c.put(f"/api/painel/usuarios/{ana}", json={"plano": "max", "dias": 30})
    assert r.status_code == 200
    u = [x for x in c.get("/api/painel/usuarios?q=ana").json()["usuarios"]][0]
    assert u["plano"] == "max" and u["ativo"] and 29 * 86400 < u["ativo_ate"] - time.time() <= 30 * 86400
    assert db.um("SELECT plano FROM assinaturas WHERE usuario=?", (ana,))["plano"] == "max"
    assert c.put(f"/api/painel/usuarios/{ana}", json={"dias": 0}).status_code == 200
    assert not c.get("/api/painel/usuarios?q=ana").json()["usuarios"][0]["ativo"]
    assert c.put(f"/api/painel/usuarios/{ana}", json={"plano": "x; drop"}).status_code == 422
    assert c.put("/api/painel/usuarios/999999", json={"dias": 1}).status_code == 404


def test_apagar_leva_as_linhas_e_recusa_qa(cli):
    c, db, ana = cli
    qa = db.um("SELECT id FROM usuarios WHERE email='demo@rangeltech.net'")["id"]
    assert c.delete(f"/api/painel/usuarios/{qa}").status_code == 409
    r = c.delete(f"/api/painel/usuarios/{ana}")
    assert r.status_code == 200 and r.json()["linhas_removidas"].get("pedidos") == 2
    assert db.um("SELECT COUNT(*) AS n FROM pedidos")["n"] == 0 and c.get("/api/painel/usuarios").json()["total"] == 2
