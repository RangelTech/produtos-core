"""Banco compatível com o `Banco` SQLite dos produtos, mas gravando no Postgres central (banco `produtos`, um schema por
produto). Spec 57, seção 8. Os produtos continuam escrevendo SQL no dialeto deles; este módulo traduz o que é específico do
SQLite (placeholders `?`, `INSERT OR IGNORE/REPLACE`, `strftime`, `MAX(a,b)`, `PRAGMA table_info`, `LIKE`, `lastrowid`,
`executescript`, tipos do DDL) para o Postgres.

Uso no produto:  Banco(dsn, schema='astralai', esquema=ESQUEMA)   (mesma interface: tx(), exec(), um(), todos(), con)
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager

import psycopg

__all__ = ['Banco', 'traduzir', 'traduzir_ddl', 'dividir_script']


# ------------------------------------------------------------------ linhas no estilo sqlite3.Row
class Linha(dict):
    """dict que também aceita índice numérico (como sqlite3.Row): r['email'] e r[0]."""

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


def _fabrica_linhas(cursor):
    nomes = [c.name for c in cursor.description] if cursor.description else []

    def fazer(valores):
        return Linha(zip(nomes, valores))
    return fazer


# ------------------------------------------------------------------ tradução de SQL
def _partes(sql: str):
    """Divide em pedaços (texto_fora_de_aspas, literal) para nunca mexer dentro de strings."""
    out, i, n, buf = [], 0, len(sql), []
    while i < n:
        c = sql[i]
        if c == "'":
            if buf:
                out.append(('sql', ''.join(buf)))
                buf = []
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            out.append(('str', sql[i:j + 1]))
            i = j + 1
        else:
            buf.append(c)
            i += 1
    if buf:
        out.append(('sql', ''.join(buf)))
    return out


def _juntar(partes):
    return ''.join(p for _, p in partes)


_FMT = {'%Y': 'YYYY', '%m': 'MM', '%d': 'DD', '%H': 'HH24', '%M': 'MI', '%S': 'SS', '%j': 'DDD', '%W': 'WW'}


def _funcoes_de_data(sql: str) -> str:
    # strftime('%s','now') -> epoch agora
    sql = re.sub(r"strftime\(\s*'%s'\s*,\s*'now'\s*\)", "extract(epoch from now())", sql, flags=re.I)
    # strftime('%s', X) com X data/texto -> epoch de X
    sql = re.sub(r"strftime\(\s*'%s'\s*,\s*([^)]+?)\s*\)", r"extract(epoch from (\1)::timestamptz)", sql, flags=re.I)

    def _fmt(m):
        f = m.group(1)
        for a, b in _FMT.items():
            f = f.replace(a, b)
        alvo = m.group(2).strip()
        if alvo.lower() == "'now'":
            return f"to_char(now(), '{f}')"
        if m.group(3):
            return f"to_char(to_timestamp({alvo}), '{f}')"
        return f"to_char(({alvo})::timestamptz, '{f}')"
    sql = re.sub(r"strftime\(\s*'([^']*)'\s*,\s*([^,()]+?)\s*(,\s*'unixepoch')?\s*\)", _fmt, sql, flags=re.I)
    sql = re.sub(r"\bdate\(\s*'now'\s*\)", "to_char(now(), 'YYYY-MM-DD')", sql, flags=re.I)
    sql = re.sub(r"\bdatetime\(\s*'now'\s*\)", "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')", sql, flags=re.I)
    sql = re.sub(r"\bdate\(\s*([^,()]+?)\s*,\s*'unixepoch'\s*\)", r"to_char(to_timestamp(\1), 'YYYY-MM-DD')", sql, flags=re.I)
    sql = re.sub(r"\bdatetime\(\s*([^,()]+?)\s*,\s*'unixepoch'\s*\)", r"to_char(to_timestamp(\1), 'YYYY-MM-DD HH24:MI:SS')", sql, flags=re.I)
    return sql


def _min_max_escalar(sql: str) -> str:
    """MAX(a, b) e MIN(a, b) do SQLite (escalares, 2+ argumentos) viram GREATEST/LEAST."""
    out, i = [], 0
    padrao = re.compile(r'\b(MAX|MIN)\s*\(', re.I)
    while True:
        m = padrao.search(sql, i)
        if not m:
            out.append(sql[i:])
            break
        out.append(sql[i:m.start()])
        j, prof, virgula = m.end(), 1, False
        while j < len(sql) and prof:
            if sql[j] == '(':
                prof += 1
            elif sql[j] == ')':
                prof -= 1
            elif sql[j] == ',' and prof == 1:
                virgula = True
            j += 1
        interno = _min_max_escalar(sql[m.end():j - 1])
        nome = m.group(1).upper()
        if virgula:
            out.append(('GREATEST(' if nome == 'MAX' else 'LEAST(') + interno + ')')
        else:
            out.append(m.group(1) + '(' + interno + ')')
        i = j
    return ''.join(out)


def _having_com_apelido(sql: str) -> str:
    """O SQLite aceita apelido do SELECT no HAVING (`... SUM(v) s ... HAVING s < 0`); o Postgres não.
    Quando o HAVING não tem função (só apelidos e valores), a consulta vira um SELECT externo com WHERE."""
    m = re.search(r'\bHAVING\b', sql, re.I)
    if not m or re.search(r'\bHAVING\b', sql[m.end():], re.I):
        return sql
    resto = sql[m.end():]
    fim = re.search(r'\b(ORDER\s+BY|LIMIT|OFFSET)\b', resto, re.I)
    cond = resto[:fim.start()] if fim else resto
    cauda = resto[fim.start():] if fim else ''
    if '(' in cond:
        return sql
    return f'SELECT * FROM ({sql[:m.start()].rstrip()}) AS _q WHERE {cond.strip()} {cauda}'.rstrip()


def traduzir(sql: str, com_params: bool = True) -> tuple[str, dict]:
    """Traduz um comando DML/consulta. Devolve (sql_postgres, dicas) — dicas: {'ignorar': bool, 'substituir': tabela}.
    Primeiro as funcoes (datas, MIN/MAX escalar), depois placeholders e `%` (fora e dentro de literais)."""
    dicas = {}
    sql = _having_com_apelido(_min_max_escalar(_funcoes_de_data(sql)))
    novos = []
    for tipo, p in _partes(sql):
        if tipo == 'str':
            novos.append(p.replace('%', '%%') if com_params else p)
            continue
        if re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', p, re.I):
            dicas['ignorar'] = True
            p = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', p, flags=re.I)
        m = re.search(r'\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+("?[\w.]+"?)', p, flags=re.I)
        if m:
            dicas['substituir'] = m.group(1).strip('"')
            p = re.sub(r'\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\b', 'INSERT INTO', p, flags=re.I)
        p = re.sub(r'\bLIKE\b', 'ILIKE', p, flags=re.I)
        if com_params:
            p = p.replace('%', '%%').replace('?', '%s')
        novos.append(p)
    return ''.join(novos), dicas


_TIPOS = [
    (re.compile(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b', re.I), 'BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY'),
    (re.compile(r'\bINTEGER\s+PRIMARY\s+KEY\b', re.I), 'BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY'),
    (re.compile(r'\bINTEGER\b', re.I), 'BIGINT'),
    (re.compile(r'\bREAL\b', re.I), 'DOUBLE PRECISION'),
    (re.compile(r'\bBLOB\b', re.I), 'BYTEA'),
    (re.compile(r'\bAUTOINCREMENT\b', re.I), ''),
]


def traduzir_ddl(sql: str) -> str:
    partes = _partes(sql)
    out = []
    for tipo, p in partes:
        if tipo == 'sql':
            for rx, novo in _TIPOS:
                p = rx.sub(novo, p)
            p = re.sub(r'\bWITHOUT\s+ROWID\b', '', p, flags=re.I)
            p = re.sub(r'\bADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS)', 'ADD COLUMN IF NOT EXISTS ', p, flags=re.I)
        out.append(p)
    return ''.join(out)


def dividir_script(script: str) -> list[str]:
    """Divide um script em comandos (por ';' fora de aspas), tirando comentários de linha."""
    linhas = [l for l in script.splitlines() if not l.strip().startswith('--')]
    texto = '\n'.join(l.split(' -- ')[0] if "'" not in l else l for l in linhas)
    cmds, buf = [], []
    for tipo, p in _partes(texto):
        if tipo == 'str':
            buf.append(p)
            continue
        pedacos = p.split(';')
        for k, pe in enumerate(pedacos):
            buf.append(pe)
            if k < len(pedacos) - 1:
                c = ''.join(buf).strip()
                if c:
                    cmds.append(c)
                buf = []
    resto = ''.join(buf).strip()
    if resto:
        cmds.append(resto)
    return cmds


# ------------------------------------------------------------------ cursor e conexão compatíveis
class Cursor:
    def __init__(self, cur, lastrowid=None, linhas=None):
        self._cur, self.lastrowid = cur, lastrowid
        self._linhas = linhas

    @property
    def rowcount(self):
        return self._cur.rowcount if self._cur is not None else 0

    def fetchone(self):
        if self._linhas is not None:
            return self._linhas.pop(0) if self._linhas else None
        try:
            return self._cur.fetchone()
        except psycopg.ProgrammingError:
            return None

    def fetchall(self):
        if self._linhas is not None:
            r, self._linhas = self._linhas, []
            return r
        try:
            return self._cur.fetchall()
        except psycopg.ProgrammingError:
            return []

    def __iter__(self):
        return iter(self.fetchall())


class _Execucao:
    """Executa comandos traduzidos numa conexão psycopg (dentro ou fora de transação)."""

    def __init__(self, banco: 'Banco'):
        self.b = banco

    def execute(self, sql: str, args=()):
        return self.b._executar(sql, args)

    def commit(self):                                # compatibilidade: no Postgres cada comando fora de tx() ja e gravado
        return None

    def executescript(self, script: str):
        for c in dividir_script(script):
            self.b._executar(c, (), ddl=True)


class Banco:
    def __init__(self, dsn: str, schema: str, esquema: str | None = None):
        self.schema = schema
        self._trava = threading.RLock()
        self._pg = psycopg.connect(dsn, autocommit=True, row_factory=_fabrica_linhas)
        with self._pg.cursor() as c:
            c.execute('select 1 from pg_namespace where nspname=%s', (schema,))
            if not c.fetchone():                     # o usuario do produto nao cria schema; so o cria quem pode (testes)
                c.execute(f'CREATE SCHEMA "{schema}"')
            c.execute(f'SET search_path TO "{schema}"')
        self._colunas: dict[str, list[str]] = {}
        self._em_tx = False
        self._pk: dict[str, list[str]] = {}
        self.con = _Execucao(self)
        if esquema:
            self.con.executescript(esquema)

    # ---- introspecção (cache)
    def _cols(self, tabela: str) -> list[str]:
        if tabela not in self._colunas:
            with self._pg.cursor() as c:
                c.execute("select column_name from information_schema.columns where table_schema=%s and table_name=%s order by ordinal_position",
                          (self.schema, tabela))
                self._colunas[tabela] = [r['column_name'] for r in c.fetchall()]
        return self._colunas[tabela]

    def _pks(self, tabela: str) -> list[str]:
        if tabela not in self._pk:
            with self._pg.cursor() as c:
                c.execute("""select a.attname from pg_index i join pg_attribute a on a.attrelid=i.indrelid and a.attnum = any(i.indkey)
                             where i.indrelid = %s::regclass and i.indisprimary""", (f'"{self.schema}"."{tabela}"',))
                self._pk[tabela] = [r['attname'] for r in c.fetchall()]
        return self._pk[tabela]

    # ---- execução
    def _executar(self, sql: str, args=(), ddl: bool = False):
        s = sql.strip().rstrip(';')
        alto = s.upper()
        with self._trava:
            m = re.match(r'PRAGMA\s+table_info\s*\(\s*"?(\w+)"?\s*\)', s, re.I)
            if m:
                with self._pg.cursor() as c:
                    c.execute("""select ordinal_position-1 as cid, column_name as name, data_type as type,
                                 (is_nullable='NO')::int as notnull, column_default as dflt_value, 0 as pk
                                 from information_schema.columns where table_schema=%s and table_name=%s order by ordinal_position""",
                              (self.schema, m.group(1)))
                    return Cursor(None, linhas=c.fetchall())
            if alto.startswith('PRAGMA') or alto in ('BEGIN', 'BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK') or alto.startswith('VACUUM'):
                return Cursor(None, linhas=[])
            if ddl or re.match(r'(CREATE|ALTER|DROP)\b', alto):
                s2 = traduzir_ddl(s)
                with self._pg.cursor() as c:
                    c.execute(s2)
                self._colunas.clear(); self._pk.clear()
                return Cursor(None, linhas=[])
            s2, dicas = traduzir(s, com_params=bool(args))
            if alto.startswith('INSERT') and ' ON CONFLICT ' not in f' {s2.upper()} ':
                tabela = re.search(r'INSERT\s+INTO\s+"?(\w+)"?', s2, re.I).group(1)
                if dicas.get('substituir'):
                    pk = self._pks(tabela)
                    cols_ins = re.search(r'INTO\s+"?\w+"?\s*\(([^)]*)\)', s2, re.I)
                    cols = [x.strip().strip('"') for x in cols_ins.group(1).split(',')] if cols_ins else self._cols(tabela)
                    upd = ', '.join(f'{c}=EXCLUDED.{c}' for c in cols if c not in pk) or None
                    s2 = s2 + (f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {upd}" if pk and upd else ' ON CONFLICT DO NOTHING')
                elif dicas.get('ignorar'):
                    s2 = s2 + ' ON CONFLICT DO NOTHING'
            retorna_id = False
            if alto.startswith('INSERT') and 'RETURNING' not in s2.upper():
                tabela = re.search(r'INSERT\s+INTO\s+"?(\w+)"?', s2, re.I).group(1)
                if 'id' in self._cols(tabela):
                    s2, retorna_id = s2 + ' RETURNING id', True
            c = self._pg.cursor()
            try:
                if self._em_tx:                      # cada comando num savepoint: erro tratado pelo produto nao aborta a transacao
                    with self._pg.transaction():
                        c.execute(s2, tuple(args) if args else None)
                else:
                    c.execute(s2, tuple(args) if args else None)
            except psycopg.errors.IntegrityError as e:   # o codigo dos produtos trata sqlite3.IntegrityError
                raise sqlite3.IntegrityError(str(e)) from e
            if retorna_id:
                r = c.fetchone()
                return Cursor(c, lastrowid=(r['id'] if r else None), linhas=[])
            return Cursor(c)

    # ---- interface dos produtos
    @contextmanager
    def tx(self):
        with self._trava:
            with self._pg.transaction():
                self._em_tx = True
                try:
                    yield self.con
                finally:
                    self._em_tx = False

    def exec(self, sql: str, args: tuple = ()):
        return self._executar(sql, args)

    def um(self, sql: str, args: tuple = ()):
        r = self._executar(sql, args).fetchone()
        return dict(r) if r else None

    def todos(self, sql: str, args: tuple = ()):
        return [dict(r) for r in self._executar(sql, args).fetchall()]

    def fechar(self):
        self._pg.close()
