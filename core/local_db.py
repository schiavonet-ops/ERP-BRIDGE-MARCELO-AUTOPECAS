"""
core/local_db.py — Banco local SQLite (espelho do Enfoque)

Fica salvo em data/estoque_local.db
Funciona mesmo com o Enfoque desligado.
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "estoque_local.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def inicializar():
    """Cria as tabelas se não existirem."""
    con = get_con()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS produto (
            codigo          INTEGER PRIMARY KEY,
            nome            TEXT,
            cod_fabricante  TEXT,
            cod_barras      TEXT,
            localizacao     TEXT,
            estoque         REAL DEFAULT 0,
            estoque_min     REAL DEFAULT 0,
            marca           TEXT,
            grupo           TEXT,
            memo            TEXT,
            ultima_sync     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_produto_nome ON produto(nome);
        CREATE INDEX IF NOT EXISTS idx_produto_cod  ON produto(cod_fabricante);

        CREATE TABLE IF NOT EXISTS sync_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo_produto  INTEGER NOT NULL,
            operacao        TEXT NOT NULL,
            quantidade      REAL NOT NULL,
            referencia      TEXT,
            origem          TEXT,
            criado_em       TEXT DEFAULT (datetime('now','localtime')),
            enviado         INTEGER DEFAULT 0,
            enviado_em      TEXT,
            erro            TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo        TEXT,
            mensagem    TEXT,
            data        TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    con.commit()
    con.close()
    _migrar_colunas()

def _migrar_colunas():
    """Adiciona colunas novas sem quebrar dados existentes."""
    con = get_con()
    cur = con.cursor()

    cur.execute("PRAGMA table_info(produto)")
    existentes = {row[1] for row in cur.fetchall()}

    novas = [
        ("aplicacao",     "TEXT DEFAULT ''"),
        ("conversao",     "TEXT DEFAULT ''"),
        ("subgrupo",      "TEXT DEFAULT ''"),
        ("ncm",           "TEXT DEFAULT ''"),
        ("cod_barras2",   "TEXT DEFAULT ''"),
        ("preco_venda",   "REAL DEFAULT 0"),
        ("margem",        "REAL DEFAULT 0"),
        ("perc_fixo",     "REAL DEFAULT 0"),
        ("perc_imposto",  "REAL DEFAULT 0"),
        ("perc_comissao", "REAL DEFAULT 0"),
        ("perc_outros",   "REAL DEFAULT 0"),
        ("custo",         "REAL DEFAULT 0"),
        ("custo_medio",   "REAL DEFAULT 0"),
        ("marca_nome",    "TEXT DEFAULT ''"),
        ("grupo_nome",    "TEXT DEFAULT ''"),
        ("subgrupo_nome", "TEXT DEFAULT ''"),
    ]

    for nome_col, definicao in novas:
        if nome_col not in existentes:
            cur.execute(f"ALTER TABLE produto ADD COLUMN {nome_col} {definicao}")

    # Índices para busca rápida em todos os campos
    indices = [
        ("idx_produto_aplicacao",     "aplicacao"),
        ("idx_produto_conversao",     "conversao"),
        ("idx_produto_marca_nome",    "marca_nome"),
        ("idx_produto_grupo_nome",    "grupo_nome"),
        ("idx_produto_ncm",           "ncm"),
        ("idx_produto_cod_barras2",   "cod_barras2"),
        ("idx_produto_localizacao",   "localizacao"),
    ]
    for idx_nome, campo in indices:
        cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_nome} ON produto({campo})")

    con.commit()
    con.close()

# ─── Produtos ────────────────────────────────────────────────

def upsert_produtos(produtos: list[dict]):
    """Insere ou atualiza produtos vindos do Enfoque."""
    con = get_con()
    agora = datetime.now().isoformat()
    con.executemany("""
        INSERT INTO produto (
            codigo, nome, cod_fabricante, cod_barras, localizacao,
            estoque, estoque_min, marca, grupo, memo, ultima_sync,
            aplicacao, conversao, subgrupo, ncm, cod_barras2,
            preco_venda, margem, perc_fixo, perc_imposto, perc_comissao,
            perc_outros, custo, custo_medio,
            marca_nome, grupo_nome, subgrupo_nome
        ) VALUES (
            :codigo, :nome, :cod_fabricante, :cod_barras, :localizacao,
            :estoque, :estoque_min, :marca, :grupo, :memo, :ultima_sync,
            :aplicacao, :conversao, :subgrupo, :ncm, :cod_barras2,
            :preco_venda, :margem, :perc_fixo, :perc_imposto, :perc_comissao,
            :perc_outros, :custo, :custo_medio,
            :marca_nome, :grupo_nome, :subgrupo_nome
        )
        ON CONFLICT(codigo) DO UPDATE SET
            nome           = excluded.nome,
            cod_fabricante = excluded.cod_fabricante,
            cod_barras     = excluded.cod_barras,
            localizacao    = excluded.localizacao,
            estoque        = excluded.estoque,
            estoque_min    = excluded.estoque_min,
            marca          = excluded.marca,
            grupo          = excluded.grupo,
            memo           = excluded.memo,
            ultima_sync    = excluded.ultima_sync,
            aplicacao      = excluded.aplicacao,
            conversao      = excluded.conversao,
            subgrupo       = excluded.subgrupo,
            ncm            = excluded.ncm,
            cod_barras2    = excluded.cod_barras2,
            preco_venda    = excluded.preco_venda,
            margem         = excluded.margem,
            perc_fixo      = excluded.perc_fixo,
            perc_imposto   = excluded.perc_imposto,
            perc_comissao  = excluded.perc_comissao,
            perc_outros    = excluded.perc_outros,
            custo          = excluded.custo,
            custo_medio    = excluded.custo_medio,
            marca_nome     = excluded.marca_nome,
            grupo_nome     = excluded.grupo_nome,
            subgrupo_nome  = excluded.subgrupo_nome
    """, [{
        **p,
        "ultima_sync":    agora,
        "aplicacao":      p.get("aplicacao", ""),
        "conversao":      p.get("conversao", ""),
        "subgrupo":       p.get("subgrupo", ""),
        "ncm":            p.get("ncm", ""),
        "cod_barras2":    p.get("cod_barras2", ""),
        "preco_venda":    p.get("preco_venda", 0),
        "margem":         p.get("margem", 0),
        "perc_fixo":      p.get("perc_fixo", 0),
        "perc_imposto":   p.get("perc_imposto", 0),
        "perc_comissao":  p.get("perc_comissao", 0),
        "perc_outros":    p.get("perc_outros", 0),
        "custo":          p.get("custo", 0),
        "custo_medio":    p.get("custo_medio", 0),
        "marca_nome":     p.get("marca_nome", ""),
        "grupo_nome":     p.get("grupo_nome", ""),
        "subgrupo_nome":  p.get("subgrupo_nome", ""),
    } for p in produtos])
    con.commit()
    con.close()

def get_produto(codigo: int) -> dict | None:
    con = get_con()
    row = con.execute(
        "SELECT * FROM produto WHERE codigo = ?", (codigo,)
    ).fetchone()
    con.close()
    return dict(row) if row else None

def buscar_produtos(texto: str, limit: int = 100) -> list[dict]:
    """Busca em TODOS os campos relevantes — nome, código, aplicação, conversão, marca, grupo, NCM, localização."""
    con = get_con()
    like = f"%{texto}%"
    rows = con.execute("""
        SELECT * FROM produto
        WHERE nome           LIKE ?1
           OR cod_fabricante LIKE ?1
           OR cod_barras      =   ?2
           OR cod_barras2    LIKE ?1
           OR localizacao    LIKE ?1
           OR aplicacao      LIKE ?1
           OR conversao      LIKE ?1
           OR marca_nome     LIKE ?1
           OR grupo_nome     LIKE ?1
           OR subgrupo_nome  LIKE ?1
           OR ncm            LIKE ?1
        ORDER BY
            CASE
                WHEN nome           LIKE ?2 THEN 0
                WHEN cod_fabricante  =   ?2 THEN 1
                WHEN cod_barras      =   ?2 THEN 2
                WHEN cod_barras2     =   ?2 THEN 3
                WHEN nome           LIKE ?1 THEN 4
                ELSE 5
            END,
            nome
        LIMIT ?3
    """, (like, texto, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def listar_estoque(so_com_saldo=False, limit=500, offset=0) -> list[dict]:
    filtro = "AND estoque > 0" if so_com_saldo else ""
    con = get_con()
    rows = con.execute(f"""
        SELECT * FROM produto
        WHERE 1=1 {filtro}
        ORDER BY nome
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def total_produtos() -> dict:
    con = get_con()
    r = con.execute("""
        SELECT
            COUNT(*)           as total,
            COUNT(CASE WHEN estoque > 0 THEN 1 END) as com_saldo,
            COUNT(CASE WHEN estoque <= 0 THEN 1 END) as zerados,
            COUNT(CASE WHEN estoque_min > 0 AND estoque <= estoque_min THEN 1 END) as abaixo_minimo
        FROM produto
    """).fetchone()
    con.close()
    return dict(r)

# ─── Movimentações locais ─────────────────────────────────────

def aplicar_movimentacao_local(codigo: int, quantidade: float, operacao: str) -> dict:
    con = get_con()
    row = con.execute(
        "SELECT estoque, nome FROM produto WHERE codigo = ?", (codigo,)
    ).fetchone()

    if not row:
        con.close()
        raise ValueError(f"Produto {codigo} não encontrado no banco local")

    estoque_atual = row["estoque"] or 0
    nome = row["nome"]

    if operacao == "baixar":
        if estoque_atual < quantidade:
            con.close()
            raise ValueError(
                f"Estoque insuficiente: {nome} tem {estoque_atual}, pedido {quantidade}"
            )
        novo = estoque_atual - quantidade
    elif operacao == "entrada":
        novo = estoque_atual + quantidade
    elif operacao == "ajustar":
        novo = quantidade
    else:
        con.close()
        raise ValueError(f"Operação inválida: {operacao}")

    con.execute(
        "UPDATE produto SET estoque = ? WHERE codigo = ?", (novo, codigo)
    )
    con.commit()
    con.close()

    return {
        "ok": True,
        "produto": codigo,
        "nome": nome,
        "estoque_anterior": estoque_atual,
        "estoque_novo": novo
    }

# ─── Fila de sync ─────────────────────────────────────────────

def enfileirar(codigo: int, operacao: str, quantidade: float,
               referencia: str = "", origem: str = "OS"):
    con = get_con()
    con.execute("""
        INSERT INTO sync_queue (codigo_produto, operacao, quantidade, referencia, origem)
        VALUES (?, ?, ?, ?, ?)
    """, (codigo, operacao, quantidade, referencia, origem))
    con.commit()
    con.close()

def pendentes() -> list[dict]:
    con = get_con()
    rows = con.execute("""
        SELECT * FROM sync_queue WHERE enviado = 0 ORDER BY id
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]

def marcar_enviado(queue_id: int):
    con = get_con()
    con.execute("""
        UPDATE sync_queue
        SET enviado = 1, enviado_em = datetime('now','localtime')
        WHERE id = ?
    """, (queue_id,))
    con.commit()
    con.close()

def marcar_erro(queue_id: int, erro: str):
    con = get_con()
    con.execute(
        "UPDATE sync_queue SET erro = ? WHERE id = ?",
        (erro, queue_id)
    )
    con.commit()
    con.close()

def log(tipo: str, mensagem: str):
    con = get_con()
    con.execute(
        "INSERT INTO sync_log (tipo, mensagem) VALUES (?, ?)",
        (tipo, mensagem)
    )
    con.commit()
    con.close()

def status_sync() -> dict:
    con = get_con()
    r = con.execute("""
        SELECT
            COUNT(CASE WHEN enviado = 0 AND erro IS NULL THEN 1 END) as pendentes,
            COUNT(CASE WHEN enviado = 1 THEN 1 END)                  as enviados,
            COUNT(CASE WHEN erro IS NOT NULL THEN 1 END)             as com_erro
        FROM sync_queue
    """).fetchone()
    ultima = con.execute(
        "SELECT data FROM sync_log WHERE tipo = 'sync_ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    return {
        **dict(r),
        "ultima_sync": ultima["data"] if ultima else None
    }

inicializar()
