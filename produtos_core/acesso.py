"""Cadastro e login dos produtos (spec 57, §5A), um módulo só para os 12 produtos.

- Cadastro: e-mail + senha (mínimo 8) + captcha. Entra na hora; o link de confirmar o e-mail chega depois e não bloqueia.
- Entrar: e-mail + senha + captcha. 5 senhas erradas seguidas travam o e-mail por 15 minutos.
- Entrar por link: só manda se o e-mail tem conta naquele produto; a resposta é sempre a mesma.
- Esqueci a senha: link por e-mail que abre a tela de senha nova.
- No máximo 3 e-mails de acesso por e-mail por hora (link, recuperação e confirmação somados).
- Teto de 10 cadastros por hora por endereço de internet.
- Captcha: Cloudflare Turnstile invisível. Sem a chave secreta no ambiente o captcha fica desligado (desenvolvimento).
  Se a Cloudflare não responder, deixa passar (os limites seguem valendo).

A senha continua no formato PBKDF2-SHA256 que os produtos já usavam (`pbkdf2$rodadas$sal$hash`), então nenhuma senha
existente muda. As tabelas novas ficam no schema do próprio produto (contas separadas por produto, §8.3).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

log = logging.getLogger("produtos_core.acesso")

RODADAS = 200_000
SENHA_MIN = 8
LINK_VALIDO_S = 15 * 60
CONFIRMAR_VALIDO_S = 7 * 24 * 3600
MAX_EMAILS_HORA = 3
MAX_CADASTROS_IP_HORA = 10
MAX_ERROS_SENHA = 5
TRAVA_S = 15 * 60
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MSG_LINK = "Se esse e-mail tiver cadastro, enviamos o link. Confira também o spam."
RE_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,24}$", re.I)

ESQUEMA = [
    "CREATE TABLE IF NOT EXISTS acesso_links (hash TEXT PRIMARY KEY, email TEXT NOT NULL, tipo TEXT NOT NULL, "
    "expira INTEGER NOT NULL, usado INTEGER, criado INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS acesso_links_email ON acesso_links(email, criado)",
    "CREATE TABLE IF NOT EXISTS acesso_eventos (chave TEXT NOT NULL, criado INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS acesso_eventos_chave ON acesso_eventos(chave, criado)",
]


def agora() -> int:
    return int(time.time())


def normalizar_email(e) -> str | None:
    e = str(e or "").strip().lower()
    return e if RE_EMAIL.match(e) else None


def hash_senha(senha: str) -> str:
    sal = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), RODADAS)
    return f"pbkdf2${RODADAS}${sal}${dk.hex()}"


def conferir_senha(guardado: str | None, senha: str) -> bool:
    try:
        _, rod, sal, h = (guardado or "").split("$")
        dk = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), int(rod))
        return hmac.compare_digest(dk.hex(), h)
    except (ValueError, TypeError):
        return False


def ip_da_requisicao(request) -> str:
    """Atrás do Traefik o endereço real vem no X-Forwarded-For (o primeiro da lista)."""
    xff = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    ip = xff.split(",")[0].strip()
    return ip or (request.client.host if request.client else "")


def captcha_ok(token: str, ip: str = "", segredo: str | None = None) -> bool:
    """Confere o token do Turnstile. Sem chave secreta: desligado (True). Cloudflare fora do ar: deixa passar."""
    segredo = os.environ.get("TURNSTILE_SECRET_KEY", "") if segredo is None else segredo
    if not segredo:
        return True
    if not token:
        return False
    corpo = urllib.parse.urlencode({"secret": segredo, "response": token, "remoteip": ip}).encode()
    try:
        req = urllib.request.Request(SITEVERIFY, data=corpo, headers={"User-Agent": "rangeltech-produtos/1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return bool(json.loads(r.read().decode()).get("success"))
    except Exception as e:  # noqa: BLE001
        log.warning("Turnstile sem resposta (%s); deixando passar", e)
        return True


def _h(valor: str) -> str:
    return hashlib.sha256(valor.encode()).hexdigest()


def registrar(app, db, *, enviar, login, usuario, criar_usuario, produto: str, base_url: str,
              destino: str = "/app", cor: str = "#7c5cff", demo_email: str = "", demo_senha: str = "",
              captcha=None, dev: bool = False, antes_cadastro=None, depois_cadastro=None,
              pagina_entrar: str = "/") -> None:
    """Liga as rotas de cadastro e login no app FastAPI do produto.

    enviar(para, assunto, texto, html) -> bool   e-mail pelo SMTP do produto (o produto põe a moldura dele)
    login(u) -> resposta com o cookie de sessão  (a mesma função que o produto já usa)
    usuario(request) -> usuário logado (401 se não houver)
    criar_usuario(email) -> dict                 cria (ou devolve) o usuário no esquema do produto
    captcha(token, ip) -> bool                   opcional (testes); padrão: Turnstile
    dev=True                                     devolve o link na resposta (`link_dev`), só para testes locais
    antes_cadastro(corpo) -> None                campos extras do produto (valida; HTTPException para recusar)
    depois_cadastro(usuario, corpo, request)     grava os campos extras depois de criar a conta
    pagina_entrar                                página com o formulário de entrar (recebe ?redefinir= e ?link=invalido)
    """
    verificar = captcha or captcha_ok
    for sql in ESQUEMA:
        db.con.execute(sql)
    cols = {r[1] for r in db.con.execute("PRAGMA table_info(usuarios)")}
    if "senha_hash" not in cols:
        db.con.execute("ALTER TABLE usuarios ADD COLUMN senha_hash TEXT")
    if "email_confirmado" not in cols:
        db.con.execute("ALTER TABLE usuarios ADD COLUMN email_confirmado INTEGER")
    try:
        db.con.commit()
    except Exception:  # noqa: BLE001
        pass
    base = base_url.rstrip("/")

    def _demo() -> tuple[str, str]:
        """demo_email/demo_senha podem ser valor ou função (lidos na hora: o admin e os testes mudam a configuração)."""
        e = demo_email() if callable(demo_email) else demo_email
        s_ = demo_senha() if callable(demo_senha) else demo_senha
        return (e or "").lower(), s_ or ""

    # ------------------------------------------------------------------ apoio
    def _evento(chave: str) -> None:
        db.exec("INSERT INTO acesso_eventos(chave,criado) VALUES(?,?)", (chave, agora()))

    def _conta(chave: str, janela: int) -> int:
        r = db.um("SELECT COUNT(*) AS n FROM acesso_eventos WHERE chave=? AND criado>?", (chave, agora() - janela))
        return int(r["n"]) if r else 0

    def _travado(email: str) -> bool:
        return _conta("erro:" + email, TRAVA_S) >= MAX_ERROS_SENHA

    def _limpar_erros(email: str) -> None:
        db.exec("DELETE FROM acesso_eventos WHERE chave=?", ("erro:" + email,))

    def _emails_na_hora(email: str) -> int:
        r = db.um("SELECT COUNT(*) AS n FROM acesso_links WHERE email=? AND criado>?", (email, agora() - 3600))
        return int(r["n"]) if r else 0

    def _novo_link(email: str, tipo: str, validade: int) -> str | None:
        """Cria o link se o e-mail ainda tem cota na hora. Devolve o token (só ele vai no e-mail)."""
        if _emails_na_hora(email) >= MAX_EMAILS_HORA:
            return None
        token = secrets.token_urlsafe(32)
        db.exec("INSERT INTO acesso_links(hash,email,tipo,expira,usado,criado) VALUES(?,?,?,?,NULL,?)",
                (_h(token), email, tipo, agora() + validade, agora()))
        return token

    def _usar_link(token: str, tipo: str) -> str | None:
        """Consome o link (uso único). Devolve o e-mail, ou None se não existe, venceu ou já foi usado."""
        if not token or len(token) > 200:
            return None
        r = db.um("SELECT * FROM acesso_links WHERE hash=?", (_h(token),))
        if not r or r["tipo"] != tipo or r["usado"] or r["expira"] < agora():
            return None
        db.exec("UPDATE acesso_links SET usado=? WHERE hash=? AND usado IS NULL", (agora(), _h(token)))
        return r["email"]

    ultimo: dict = {}

    def _botao(href: str, rotulo: str) -> str:
        return (f'<p><a href="{href}" style="display:inline-block;background:{cor};color:#fff;text-decoration:none;'
                f'font-weight:700;padding:14px 22px;border-radius:12px">{rotulo}</a></p>'
                f'<p style="color:#8b8b98;font-size:13px">Se o botão não abrir, copie este endereço: {href}</p>')

    def _mandar(email: str, tipo: str) -> bool:
        """Manda o e-mail; se o SMTP falhar, o link é apagado (não vale nem conta na cota de 3 por hora)."""
        antes = {r["hash"] for r in _links_recentes(email)}
        ok = _mandar_sem_desfazer(email, tipo)
        if not ok and not dev:  # em dev (testes, sem SMTP) o link fica, para o link_dev funcionar
            for r in _links_recentes(email):
                if r["hash"] not in antes:
                    db.exec("DELETE FROM acesso_links WHERE hash=?", (r["hash"],))
        return ok

    def _links_recentes(email: str) -> list:
        lst = db.todos("SELECT hash FROM acesso_links WHERE email=? AND criado>?", (email, agora() - 3600)) if hasattr(db, "todos") else             [dict(r) for r in db.con.execute("SELECT hash FROM acesso_links WHERE email=? AND criado>?", (email, agora() - 3600)).fetchall()]
        return [dict(r) for r in lst]

    def _mandar_sem_desfazer(email: str, tipo: str) -> bool:
        if tipo == "entrar":
            t = _novo_link(email, "entrar", LINK_VALIDO_S)
            if not t:
                return False
            url = f"{base}/acesso/entrar?t={t}"
            ultimo["url"] = url
            return enviar(email, f"Seu link para entrar no {produto}",
                          f"Clique para entrar no {produto}: {url}\nVale por 15 minutos e só funciona uma vez.",
                          f'<p style="font-size:16px">Clique no botão para entrar no {produto}.</p>{_botao(url, "Entrar agora")}'
                          '<p style="color:#8b8b98">Vale por 15 minutos e só funciona uma vez.</p>')
        if tipo == "redefinir":
            t = _novo_link(email, "redefinir", LINK_VALIDO_S)
            if not t:
                return False
            url = f"{base}{pagina_entrar}?redefinir={t}"
            ultimo["url"], ultimo["token"] = url, t
            return enviar(email, f"Crie uma senha nova no {produto}",
                          f"Para criar uma senha nova no {produto}: {url}\nVale por 15 minutos.",
                          f'<p style="font-size:16px">Recebemos um pedido para trocar a senha do {produto}.</p>'
                          f'{_botao(url, "Criar senha nova")}<p style="color:#8b8b98">Vale por 15 minutos. Não pediu? Ignore.</p>')
        t = _novo_link(email, "confirmar", CONFIRMAR_VALIDO_S)
        if not t:
            return False
        url = f"{base}/acesso/confirmar?t={t}"
        return enviar(email, f"Confirme seu e-mail no {produto}",
                      f"Bem-vindo ao {produto}! Confirme seu e-mail: {url}",
                      f'<p style="font-size:16px">Bem-vindo ao {produto}! Sua conta já está pronta.</p>'
                      f'<p>Falta só confirmar o e-mail:</p>{_botao(url, "Confirmar e-mail")}')

    def _redirecionar_logado(u: dict, para: str):
        r = login(u)
        red = RedirectResponse(para, status_code=303)
        for v in r.headers.getlist("set-cookie"):
            red.headers.append("set-cookie", v)
        return red

    def _validar_nova(senha) -> str:
        senha = str(senha or "")
        if len(senha) < SENHA_MIN:
            raise HTTPException(422, f"a senha precisa de pelo menos {SENHA_MIN} caracteres")
        if len(senha) > 200:
            raise HTTPException(422, "senha longa demais")
        return senha

    async def _corpo(request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "corpo inválido (envie JSON)")
        return b if isinstance(b, dict) else {}

    def _exigir_captcha(b: dict, request) -> None:
        if not verificar(str(b.get("captcha") or ""), ip_da_requisicao(request)):
            raise HTTPException(403, "não conseguimos confirmar que é uma pessoa; recarregue a página e tente de novo")

    # ------------------------------------------------------------------ rotas
    @app.get("/api/acesso/config")
    def acesso_config():
        return {"turnstile": os.environ.get("TURNSTILE_SITE_KEY", "") if os.environ.get("TURNSTILE_SECRET_KEY") else ""}

    @app.post("/api/cadastro")
    async def cadastro(request: Request):
        b = await _corpo(request)
        email = normalizar_email(b.get("email"))
        if not email:
            raise HTTPException(422, "e-mail inválido")
        senha = _validar_nova(b.get("senha"))
        if antes_cadastro:
            antes_cadastro(b)
        _exigir_captcha(b, request)
        ip = ip_da_requisicao(request)
        if ip and _conta("cadastro:" + _h(ip), 3600) >= MAX_CADASTROS_IP_HORA:
            raise HTTPException(429, "muitos cadastros daqui agora; tente de novo mais tarde")
        u = db.um("SELECT * FROM usuarios WHERE email=?", (email,))
        if u and u.get("senha_hash"):
            raise HTTPException(409, "esse e-mail já tem conta: entre com a senha ou use 'Esqueci minha senha'")
        if u:  # conta antiga sem senha (criada pelo código ou pela compra): manda link, não deixa definir a senha às cegas
            _mandar(email, "entrar")
            raise HTTPException(409, "esse e-mail já tem conta: mandamos um link de acesso para ele")
        if ip:
            _evento("cadastro:" + _h(ip))
        u = criar_usuario(email)
        db.exec("UPDATE usuarios SET senha_hash=? WHERE id=?", (hash_senha(senha), u["id"]))
        if depois_cadastro:
            depois_cadastro(u, b, request)
        _mandar(email, "confirmar")
        return login(db.um("SELECT * FROM usuarios WHERE id=?", (u["id"],)))

    @app.post("/api/entrar/senha")
    async def entrar_senha(request: Request):
        b = await _corpo(request)
        email = normalizar_email(b.get("email"))
        senha = str(b.get("senha") or "")
        if not email or not senha:
            raise HTTPException(400, "preencha e-mail e senha")
        _exigir_captcha(b, request)
        if _travado(email):
            raise HTTPException(429, "muitas tentativas; espere 15 minutos ou use 'Entrar por link no e-mail'")
        de, ds = _demo()
        if de and ds and email == de and hmac.compare_digest(senha, ds):
            return login(criar_usuario(email))
        u = db.um("SELECT * FROM usuarios WHERE email=?", (email,))
        if not u or not conferir_senha(u.get("senha_hash"), senha):
            _evento("erro:" + email)
            if u and not u.get("senha_hash"):
                raise HTTPException(400, "essa conta ainda não tem senha: use 'Entrar por link no e-mail' e crie uma")
            raise HTTPException(400, "e-mail ou senha incorretos")
        _limpar_erros(email)
        return login(u)

    @app.post("/api/entrar/link")
    async def entrar_link(request: Request):
        b = await _corpo(request)
        email = normalizar_email(b.get("email"))
        if not email:
            raise HTTPException(422, "e-mail inválido")
        _exigir_captcha(b, request)
        if db.um("SELECT id FROM usuarios WHERE email=?", (email,)) and _emails_na_hora(email) >= MAX_EMAILS_HORA:
            raise HTTPException(429, "já mandamos 3 e-mails para esse endereço nesta hora; tente de novo mais tarde")
        ultimo.clear()
        if db.um("SELECT id FROM usuarios WHERE email=?", (email,)):
            _mandar(email, "entrar")
        return {"ok": True, "mensagem": MSG_LINK, **({"link_dev": ultimo["url"]} if dev and ultimo.get("url") else {})}

    @app.get("/acesso/entrar")
    def abrir_link(t: str = ""):
        email = _usar_link(t, "entrar")
        u = db.um("SELECT * FROM usuarios WHERE email=?", (email,)) if email else None
        if not u:
            return RedirectResponse(f"{pagina_entrar}?entrar=1&link=invalido", status_code=303)
        if not u.get("email_confirmado"):  # quem abriu o link provou que é dono do e-mail
            db.exec("UPDATE usuarios SET email_confirmado=? WHERE id=?", (agora(), u["id"]))
        _limpar_erros(email)
        return _redirecionar_logado(u, destino)

    @app.post("/api/acesso/link-qa")
    async def link_qa(request: Request):
        """Painel único (spec 57 §6.2A): link de entrada de uso único para o USUÁRIO DE QA, pedido pelo painel com o token de
        serviço (ACESSO_PAINEL_TOKEN). Não serve para nenhum outro e-mail. Não manda e-mail e não gasta a cota de 3 por hora."""
        token = os.environ.get("ACESSO_PAINEL_TOKEN", "")
        enviado = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(enviado, token):
            raise HTTPException(401, "token do painel inválido")
        qa = _demo()[0]
        if not qa:
            raise HTTPException(404, "este produto não tem usuário de QA configurado")
        criar_usuario(qa)
        t = secrets.token_urlsafe(32)
        db.exec("INSERT INTO acesso_links(hash,email,tipo,expira,usado,criado) VALUES(?,?,?,?,NULL,?)",
                (_h(t), qa, "entrar", agora() + 120, agora() - 7200))  # vale 2 min; criado "antigo" para não contar na cota
        log.info("link de QA pedido pelo painel para %s", qa)
        return {"link": f"{base}/acesso/entrar?t={t}", "email": qa, "valido_s": 120}

    @app.get("/acesso/confirmar")
    def confirmar(t: str = ""):
        email = _usar_link(t, "confirmar")
        if not email:
            return RedirectResponse(f"{pagina_entrar}?entrar=1&link=invalido", status_code=303)
        db.exec("UPDATE usuarios SET email_confirmado=? WHERE email=?", (agora(), email))
        u = db.um("SELECT * FROM usuarios WHERE email=?", (email,))
        return _redirecionar_logado(u, destino) if u else RedirectResponse("/", status_code=303)

    @app.post("/api/senha/esqueci")
    async def esqueci(request: Request):
        b = await _corpo(request)
        email = normalizar_email(b.get("email"))
        if not email:
            raise HTTPException(422, "e-mail inválido")
        _exigir_captcha(b, request)
        if db.um("SELECT id FROM usuarios WHERE email=?", (email,)) and _emails_na_hora(email) >= MAX_EMAILS_HORA:
            raise HTTPException(429, "já mandamos 3 e-mails para esse endereço nesta hora; tente de novo mais tarde")
        ultimo.clear()
        if db.um("SELECT id FROM usuarios WHERE email=?", (email,)):
            _mandar(email, "redefinir")
        return {"ok": True, "mensagem": MSG_LINK, **({"token_dev": ultimo["token"]} if dev and ultimo.get("token") else {})}

    @app.post("/api/senha/redefinir")
    async def redefinir(request: Request):
        b = await _corpo(request)
        nova = _validar_nova(b.get("senha"))
        email = _usar_link(str(b.get("token") or ""), "redefinir")
        u = db.um("SELECT * FROM usuarios WHERE email=?", (email,)) if email else None
        if not u:
            raise HTTPException(400, "link vencido ou já usado; peça outro em 'Esqueci minha senha'")
        db.exec("UPDATE usuarios SET senha_hash=?, email_confirmado=COALESCE(email_confirmado, ?) WHERE id=?",
                (hash_senha(nova), agora(), u["id"]))
        _limpar_erros(email)
        return login(u)

    @app.get("/api/senha")
    def tem_senha(request: Request):
        u = usuario(request)
        de = _demo()[0]
        return {"tem_senha": bool(u.get("senha_hash")) or bool(de and u["email"] == de), "email": u["email"]}

    @app.post("/api/senha")
    async def definir_senha(request: Request):
        """Cria a senha (conta sem senha) ou troca (pede a atual)."""
        u = usuario(request)
        b = await _corpo(request)
        nova = _validar_nova(b.get("senha"))
        if u.get("senha_hash") and not conferir_senha(u["senha_hash"], str(b.get("atual") or "")):
            raise HTTPException(400, "a senha atual não confere (esqueceu? use 'Esqueci minha senha' na tela de entrar)")
        db.exec("UPDATE usuarios SET senha_hash=? WHERE id=?", (hash_senha(nova), u["id"]))
        return {"ok": True}
