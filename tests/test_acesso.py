"""Cadastro e login (spec 57, §5A.6). Roda no SQLite e, com PRODUTOS_DATABASE_URL, também num schema de teste do Postgres."""
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from produtos_core import acesso

ESQUEMA = ("CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, criado INTEGER);"
           "CREATE TABLE IF NOT EXISTS sessoes (hash TEXT PRIMARY KEY, usuario INTEGER, expira INTEGER);")


class BancoSQLite:
    def __init__(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQUEMA)
        self._t = threading.RLock()

    @contextmanager
    def tx(self):
        with self._t:
            yield self.con
            self.con.commit()

    def exec(self, sql, args=()):
        with self._t:
            c = self.con.execute(sql, args)
            self.con.commit()
            return c

    def um(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return dict(r) if r else None


def _bancos():
    yield "sqlite", BancoSQLite
    dsn = os.environ.get("PRODUTOS_DATABASE_URL")
    if dsn:
        from produtos_core.banco_pg import Banco
        yield "postgres", lambda: Banco(dsn, "teste_acesso_" + secrets.token_hex(4), ESQUEMA)


def montar(banco, captcha_valido="bom", dev=False):
    db = banco()
    app = FastAPI()
    caixa = []

    def enviar(para, assunto, texto, html):
        caixa.append({"para": para, "assunto": assunto, "texto": texto})
        return True

    def criar_usuario(email):
        if not db.um("SELECT id FROM usuarios WHERE email=?", (email,)):
            db.exec("INSERT INTO usuarios(email,criado) VALUES(?,?)", (email, acesso.agora()))
        return db.um("SELECT * FROM usuarios WHERE email=?", (email,))

    def login(u):
        t = secrets.token_urlsafe(16)
        db.exec("INSERT INTO sessoes(hash,usuario,expira) VALUES(?,?,?)", (t, u["id"], acesso.agora() + 3600))
        r = JSONResponse({"ok": True})
        r.set_cookie("s", t, httponly=True, path="/")
        return r

    def usuario(request: Request):
        s = db.um("SELECT usuario FROM sessoes WHERE hash=?", (request.cookies.get("s") or "",))
        if not s:
            raise HTTPException(401, "entre primeiro")
        return db.um("SELECT * FROM usuarios WHERE id=?", (s["usuario"],))

    @app.get("/api/conta")
    def conta(request: Request):
        return {"email": usuario(request)["email"]}

    acesso.registrar(app, db, enviar=enviar, login=login, usuario=usuario, criar_usuario=criar_usuario,
                     produto="Teste AI", base_url="https://teste.exemplo", destino="/app",
                     demo_email="demo@exemplo.com", demo_senha="demo-senha-123",
                     captcha=lambda token, ip: token == captcha_valido, dev=dev)
    return app, db, caixa


def _token(caixa, trecho):
    m = re.search(trecho + r"=([\w-]+)", caixa[-1]["texto"])
    assert m, caixa[-1]
    return m.group(1)


@pytest.fixture(params=[n for n, _ in _bancos()])
def ambiente(request):
    banco = dict(_bancos())[request.param]
    return montar(banco)


def test_cadastro_entra_na_hora_e_manda_confirmacao(ambiente):
    app, db, caixa = ambiente
    c = TestClient(app)
    r = c.post("/api/cadastro", json={"email": "Ana@Exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    assert r.status_code == 200, r.text
    assert c.get("/api/conta").json()["email"] == "ana@exemplo.com"
    assert "Confirme" in caixa[-1]["assunto"]
    t = _token(caixa, "t")
    c2 = TestClient(app)
    r = c2.get(f"/acesso/confirmar?t={t}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert db.um("SELECT email_confirmado FROM usuarios WHERE email=?", ("ana@exemplo.com",))["email_confirmado"]
    assert c2.get(f"/acesso/confirmar?t={t}", follow_redirects=False).headers["location"].endswith("link=invalido")


def test_cadastro_repetido_e_senha_curta(ambiente):
    app, _, _ = ambiente
    c = TestClient(app)
    assert c.post("/api/cadastro", json={"email": "b@exemplo.com", "senha": "curta", "captcha": "bom"}).status_code == 422
    assert c.post("/api/cadastro", json={"email": "b@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"}).status_code == 200
    assert c.post("/api/cadastro", json={"email": "b@exemplo.com", "senha": "outra-senha", "captcha": "bom"}).status_code == 409


def test_captcha_invalido_bloqueia_tudo(ambiente):
    app, _, caixa = ambiente
    c = TestClient(app)
    assert c.post("/api/cadastro", json={"email": "c@exemplo.com", "senha": "senha-boa-1", "captcha": "ruim"}).status_code == 403
    assert c.post("/api/cadastro", json={"email": "c@exemplo.com", "senha": "senha-boa-1"}).status_code == 403
    assert c.post("/api/entrar/senha", json={"email": "c@exemplo.com", "senha": "x", "captcha": "ruim"}).status_code == 403
    assert c.post("/api/entrar/link", json={"email": "c@exemplo.com", "captcha": ""}).status_code == 403
    assert c.post("/api/senha/esqueci", json={"email": "c@exemplo.com", "captcha": "ruim"}).status_code == 403
    assert caixa == []


def test_login_senha_e_trava_depois_de_5_erros(ambiente):
    app, _, _ = ambiente
    c = TestClient(app)
    c.post("/api/cadastro", json={"email": "d@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    c = TestClient(app)
    assert c.post("/api/entrar/senha", json={"email": "d@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"}).status_code == 200
    for _ in range(5):
        assert c.post("/api/entrar/senha", json={"email": "d@exemplo.com", "senha": "errada!!", "captcha": "bom"}).status_code == 400
    r = c.post("/api/entrar/senha", json={"email": "d@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    assert r.status_code == 429


def test_demo_entra_pela_senha_da_configuracao(ambiente):
    app, _, _ = ambiente
    c = TestClient(app)
    assert c.post("/api/entrar/senha", json={"email": "demo@exemplo.com", "senha": "demo-senha-123", "captcha": "bom"}).status_code == 200
    assert c.get("/api/senha").json()["tem_senha"] is True


def test_link_so_para_email_cadastrado_mesma_resposta_e_limite(ambiente):
    app, _, caixa = ambiente
    c = TestClient(app)
    c.post("/api/cadastro", json={"email": "e@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})  # 1 e-mail (confirmação)
    n = len(caixa)
    r1 = c.post("/api/entrar/link", json={"email": "ninguem@exemplo.com", "captcha": "bom"})
    assert r1.status_code == 200 and len(caixa) == n  # não cadastrado: nada enviado
    r2 = c.post("/api/entrar/link", json={"email": "e@exemplo.com", "captcha": "bom"})
    assert r2.status_code == 200 and r2.json() == r1.json() and len(caixa) == n + 1
    t = _token(caixa, "t")
    novo = TestClient(app)
    r = novo.get(f"/acesso/entrar?t={t}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert novo.get("/api/conta").json()["email"] == "e@exemplo.com"
    assert TestClient(app).get(f"/acesso/entrar?t={t}", follow_redirects=False).headers["location"].endswith("link=invalido")
    assert c.post("/api/entrar/link", json={"email": "e@exemplo.com", "captcha": "bom"}).status_code == 200  # 3º e-mail da hora
    assert c.post("/api/entrar/link", json={"email": "e@exemplo.com", "captcha": "bom"}).status_code == 429  # 4º recusado
    assert c.post("/api/senha/esqueci", json={"email": "e@exemplo.com", "captcha": "bom"}).status_code == 429  # soma os tipos


def test_link_vencido_recusado(ambiente):
    app, db, caixa = ambiente
    c = TestClient(app)
    c.post("/api/cadastro", json={"email": "f@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    c.post("/api/entrar/link", json={"email": "f@exemplo.com", "captcha": "bom"})
    t = _token(caixa, "t")
    db.exec("UPDATE acesso_links SET expira=? WHERE tipo='entrar'", (acesso.agora() - 1,))
    assert TestClient(app).get(f"/acesso/entrar?t={t}", follow_redirects=False).headers["location"].endswith("link=invalido")


def test_esqueci_e_redefinir(ambiente):
    app, _, caixa = ambiente
    c = TestClient(app)
    c.post("/api/cadastro", json={"email": "g@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    assert c.post("/api/senha/esqueci", json={"email": "g@exemplo.com", "captcha": "bom"}).status_code == 200
    t = _token(caixa, "redefinir")
    novo = TestClient(app)
    assert novo.post("/api/senha/redefinir", json={"token": t, "senha": "senha-nova-2"}).status_code == 200
    assert novo.get("/api/conta").json()["email"] == "g@exemplo.com"
    assert novo.post("/api/senha/redefinir", json={"token": t, "senha": "outra-senha-3"}).status_code == 400
    assert TestClient(app).post("/api/entrar/senha", json={"email": "g@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"}).status_code == 400
    assert TestClient(app).post("/api/entrar/senha", json={"email": "g@exemplo.com", "senha": "senha-nova-2", "captcha": "bom"}).status_code == 200


def test_conta_antiga_sem_senha_recebe_link(ambiente):
    app, db, caixa = ambiente
    db.exec("INSERT INTO usuarios(email,criado) VALUES(?,?)", ("h@exemplo.com", 1))
    c = TestClient(app)
    r = c.post("/api/entrar/senha", json={"email": "h@exemplo.com", "senha": "qualquer1", "captcha": "bom"})
    assert r.status_code == 400 and "link" in r.json()["detail"]
    assert c.post("/api/cadastro", json={"email": "h@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"}).status_code == 409
    assert "entrar" in caixa[-1]["assunto"]  # mandou link de acesso, não deixou definir senha às cegas


def test_teto_de_cadastros_por_endereco(ambiente):
    app, _, _ = ambiente
    c = TestClient(app)
    h = {"X-Forwarded-For": "203.0.113.9"}
    for i in range(10):
        assert c.post("/api/cadastro", json={"email": f"ip{i}@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"}, headers=h).status_code == 200
    assert c.post("/api/cadastro", json={"email": "ip10@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"}, headers=h).status_code == 429


def test_contas_separadas_por_produto():
    """Dois produtos (dois bancos): a conta de um não entra no outro."""
    a, _, _ = montar(BancoSQLite)
    b, _, _ = montar(BancoSQLite)
    TestClient(a).post("/api/cadastro", json={"email": "so-a@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    r = TestClient(b).post("/api/entrar/senha", json={"email": "so-a@exemplo.com", "senha": "senha-boa-1", "captcha": "bom"})
    assert r.status_code == 400 and r.json()["detail"] == "e-mail ou senha incorretos"


def test_captcha_real_desligado_sem_chave_e_bloqueia_token_ruim_com_chave():
    assert acesso.captcha_ok("", segredo="") is True
    assert acesso.captcha_ok("", segredo="2x0000000000000000000000000000000AA") is False
    # chave de teste oficial da Cloudflare que sempre falha
    assert acesso.captcha_ok("qualquer", segredo="2x0000000000000000000000000000000AA") is False
    assert acesso.captcha_ok("XXXX.DUMMY.TOKEN.XXXX", segredo="1x0000000000000000000000000000000AA") is True


def test_modo_dev_e_ajudante_de_teste():
    from produtos_core import teste
    app, db, _ = montar(BancoSQLite, captcha_valido="", dev=True)  # captcha "" = desligado, como nos testes dos produtos
    c = TestClient(app)
    assert teste.entrar(c, "nova@exemplo.com").status_code == 200          # cria a conta
    assert TestClient(app).post("/api/entrar/link", json={"email": "nova@exemplo.com"}).json()["link_dev"].startswith("https://teste.exemplo/acesso/entrar?t=")
    assert "link_dev" not in TestClient(app).post("/api/entrar/link", json={"email": "ninguem@exemplo.com"}).json()
    assert teste.entrar(TestClient(app), "nova@exemplo.com").status_code == 200  # entra pela senha
    db.exec("INSERT INTO usuarios(email,criado) VALUES(?,?)", ("webhook@exemplo.com", 1))
    w = TestClient(app)
    teste.entrar(w, "webhook@exemplo.com")                                   # conta sem senha entra pelo link
    assert w.get("/api/conta").json()["email"] == "webhook@exemplo.com"
    assert TestClient(app).post("/api/senha/esqueci", json={"email": "nova@exemplo.com"}).json()["token_dev"]
    sem_dev, _, _ = montar(BancoSQLite, captcha_valido="")
    TestClient(sem_dev).post("/api/cadastro", json={"email": "x@exemplo.com", "senha": "senha-boa-1"})
    assert "link_dev" not in TestClient(sem_dev).post("/api/entrar/link", json={"email": "x@exemplo.com"}).json()
