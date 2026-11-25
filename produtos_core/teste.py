"""Ajudante dos testes dos produtos: entrar com um TestClient sem depender de e-mail (o app precisa rodar com dev=True)."""
from __future__ import annotations

from urllib.parse import urlparse

SENHA_TESTE = "senha-teste-1"


def entrar(c, email: str, senha: str = SENHA_TESTE):
    """Cria a conta (ou entra nela). Conta que já existia sem senha (webhook, admin) entra pelo link de acesso."""
    r = c.post("/api/cadastro", json={"email": email, "senha": senha})
    if r.status_code == 200:
        return r
    r = c.post("/api/entrar/senha", json={"email": email, "senha": senha})
    if r.status_code == 200:
        return r
    j = c.post("/api/entrar/link", json={"email": email}).json()
    assert j.get("link_dev"), f"não consegui entrar como {email}: {j}"
    u = urlparse(j["link_dev"])
    r = c.get(f"{u.path}?{u.query}", follow_redirects=False)
    assert r.status_code == 303 and "invalido" not in r.headers.get("location", ""), r.headers
    return r
