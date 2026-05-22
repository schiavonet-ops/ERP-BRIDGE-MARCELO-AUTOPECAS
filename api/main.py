"""
api/main.py — API REST: lê do banco local, escreve local + fila para Enfoque

Instalar:  pip install fastapi uvicorn fdb python-dotenv
Rodar:     uvicorn api.main:app --host 0.0.0.0 --port 8000
Docs:      http://localhost:8000/docs
"""

import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

import core.local_db as db
from core.sync_worker import puxar_enfoque, enviar_fila, enfoque_online, _conectar

app = FastAPI(
    title="Estoque Bridge — Enfoque ↔ AutoPeças Pro",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Schemas ──────────────────────────────────────────────────

class MovRequest(BaseModel):
    quantidade: float
    referencia: Optional[str] = ""
    origem: Optional[str] = "OS"

class AjusteRequest(BaseModel):
    quantidade_nova: float
    referencia: Optional[str] = "AJUSTE MANUAL"

class ItemOS(BaseModel):
    codigo_produto: int
    quantidade: float

class OSRequest(BaseModel):
    numero_os: str
    itens: list[ItemOS]

class ProdutoAtualizar(BaseModel):
    # Campos da tabela PRODUTO
    descricao: Optional[str] = None        # PRO_NOME (max 120)
    codigo_proprio: Optional[str] = None   # PRO_CODPROPRIO (max 60)
    localizacao: Optional[str] = None      # PRO_LOCALIZACAO (max 50)
    aplicacao: Optional[str] = None        # parte 1 do PRO_MEMO
    conversao: Optional[str] = None        # parte 2 do PRO_MEMO
    grupo_codigo: Optional[int] = None     # PRO_GRUPO
    secao_codigo: Optional[int] = None     # PRO_SECAO
    marca_codigo: Optional[int] = None     # PRO_MARCA
    # Campos da tabela ESTOQUE
    preco_venda: Optional[float] = None    # EST_VENDA
    margem: Optional[float] = None         # EST_MARGEM
    perc_comissao: Optional[float] = None  # EST_PERCCOMISSAO
    perc_imposto: Optional[float] = None   # EST_PERCIMPOSTO
    perc_fixo: Optional[float] = None      # EST_PERCFIXO
    perc_outros: Optional[float] = None    # EST_PERCOUTROS

# ─── Background: tenta enviar fila sempre que Enfoque voltar ──

def _try_sync():
    if enfoque_online():
        enviar_fila()

# ─── Helper: monta PRO_MEMO no formato modelo B ────────────────

def _montar_memo(aplicacao: Optional[str], conversao: Optional[str]) -> str:
    """Concatena APLICACAO + CONVERSAO no formato modelo B."""
    partes = []
    if aplicacao and aplicacao.strip():
        partes.append("-------APLICACAO-----------")
        partes.append(aplicacao.strip())
    if conversao and conversao.strip():
        if partes:
            partes.append("")  # linha em branco entre seções
        partes.append("-------CONVERSAO-----------")
        partes.append(conversao.strip())
    return "\n".join(partes)

# ─── Rotas de leitura ─────────────────────────────────────────

@app.get("/ping")
def ping():
    enfoque = enfoque_online()
    s = db.status_sync()
    return {
        "status": "ok",
        "enfoque_online": enfoque,
        "sync_pendentes": s["pendentes"],
        "ultima_sync": s["ultima_sync"]
    }

@app.get("/estoque/resumo")
def resumo():
    """Totais para o dashboard: total, com saldo, zerados, abaixo do mínimo."""
    return db.total_produtos()

@app.get("/estoque")
def lista(
    so_com_saldo: bool = Query(False),
    limit: int = Query(500, le=2000),
    offset: int = Query(0)
):
    produtos = db.listar_estoque(so_com_saldo=so_com_saldo, limit=limit, offset=offset)
    return {"total": len(produtos), "produtos": produtos}

@app.get("/estoque/buscar/{texto}")
def buscar(texto: str):
    resultado = db.buscar_produtos(texto)
    return {"total": len(resultado), "produtos": resultado}

@app.get("/estoque/{codigo}")
def detalhe(codigo: int):
    p = db.get_produto(codigo)
    if not p:
        raise HTTPException(404, detail=f"Produto {codigo} não encontrado")
    return p

# ─── Rotas de escrita (local imediato + fila para Enfoque) ────

@app.post("/estoque/{codigo}/baixar")
def baixar(codigo: int, body: MovRequest, bg: BackgroundTasks):
    """Baixa estoque. Aplica local agora, envia ao Enfoque em background."""
    try:
        resultado = db.aplicar_movimentacao_local(codigo, body.quantidade, "baixar")
        db.enfileirar(codigo, "baixar", body.quantidade, body.referencia, body.origem)
        bg.add_task(_try_sync)
        return {**resultado, "sync": "enfileirado"}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

@app.post("/estoque/{codigo}/entrada")
def entrada(codigo: int, body: MovRequest, bg: BackgroundTasks):
    """Entrada de estoque."""
    try:
        resultado = db.aplicar_movimentacao_local(codigo, body.quantidade, "entrada")
        db.enfileirar(codigo, "entrada", body.quantidade, body.referencia, body.origem)
        bg.add_task(_try_sync)
        return {**resultado, "sync": "enfileirado"}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

@app.post("/estoque/{codigo}/ajustar")
def ajustar(codigo: int, body: AjusteRequest, bg: BackgroundTasks):
    """Define estoque para valor exato."""
    try:
        resultado = db.aplicar_movimentacao_local(codigo, body.quantidade_nova, "ajustar")
        db.enfileirar(codigo, "ajustar", body.quantidade_nova, body.referencia)
        bg.add_task(_try_sync)
        return {**resultado, "sync": "enfileirado"}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

@app.post("/estoque/os/{numero_os}")
def baixar_os(numero_os: str, body: OSRequest, bg: BackgroundTasks):
    """Baixa todos os itens de uma OS de uma vez."""
    resultados, erros = [], []
    for item in body.itens:
        try:
            r = db.aplicar_movimentacao_local(item.codigo_produto, item.quantidade, "baixar")
            db.enfileirar(item.codigo_produto, "baixar", item.quantidade, numero_os, "OS")
            resultados.append(r)
        except ValueError as e:
            erros.append({"codigo": item.codigo_produto, "erro": str(e)})
    bg.add_task(_try_sync)
    return {
        "numero_os": numero_os,
        "processados": len(resultados),
        "erros": len(erros),
        "resultados": resultados,
        "erros_detalhe": erros
    }

# ─── Atualização de cadastro de produto no Enfoque ────────────

@app.put("/produto/{codigo}")
def atualizar_produto(codigo: int, dados: ProdutoAtualizar):
    """
    Atualiza cadastro de produto no Enfoque (PRODUTO + ESTOQUE).
    Todos os campos são opcionais.
    Adiciona marker '(BRIDGE)' na descrição automaticamente.

    Campos suportados:
      PRODUTO: descricao, codigo_proprio, localizacao, aplicacao, conversao,
               grupo_codigo, secao_codigo, marca_codigo
      ESTOQUE: preco_venda, margem, perc_comissao, perc_imposto,
               perc_fixo, perc_outros
    """
    con = _conectar()
    try:
        cur = con.cursor()

        # Verifica se o produto existe
        cur.execute("SELECT 1 FROM PRODUTO WHERE PRO_CODIGO = ?", [codigo])
        if not cur.fetchone():
            raise HTTPException(404, f"Produto {codigo} nao encontrado")

        atualizados = []
        agora = datetime.now()

        # ─── UPDATE em PRODUTO ───
        campos_p, valores_p = [], []

        if dados.descricao is not None:
            nome = dados.descricao.strip()
            if "(BRIDGE)" not in nome:
                nome = f"{nome} (BRIDGE)"
            campos_p.append("PRO_NOME = ?")
            valores_p.append(nome[:120].encode("cp1252", errors="replace"))
            atualizados.append("descricao")

        if dados.codigo_proprio is not None:
            campos_p.append("PRO_CODPROPRIO = ?")
            valores_p.append(dados.codigo_proprio.strip()[:60].encode("cp1252", errors="replace"))
            atualizados.append("codigo_proprio")

        if dados.localizacao is not None:
            campos_p.append("PRO_LOCALIZACAO = ?")
            valores_p.append(dados.localizacao.strip()[:50].encode("cp1252", errors="replace"))
            atualizados.append("localizacao")

        if dados.aplicacao is not None or dados.conversao is not None:
            memo = _montar_memo(dados.aplicacao, dados.conversao)
            campos_p.append("PRO_MEMO = ?")
            valores_p.append(memo.encode("cp1252", errors="replace"))
            atualizados.append("memo")

        if dados.grupo_codigo is not None:
            campos_p.append("PRO_GRUPO = ?")
            valores_p.append(dados.grupo_codigo)
            atualizados.append("grupo")

        if dados.secao_codigo is not None:
            campos_p.append("PRO_SECAO = ?")
            valores_p.append(dados.secao_codigo)
            atualizados.append("secao")

        if dados.marca_codigo is not None:
            campos_p.append("PRO_MARCA = ?")
            valores_p.append(dados.marca_codigo)
            atualizados.append("marca")

        if campos_p:
            campos_p.append("PRO_DATAEDICAO = ?")
            valores_p.append(agora)
            valores_p.append(codigo)
            sql_p = f"UPDATE PRODUTO SET {', '.join(campos_p)} WHERE PRO_CODIGO = ?"
            cur.execute(sql_p, valores_p)

        # ─── UPDATE em ESTOQUE ───
        campos_e, valores_e = [], []

        if dados.preco_venda is not None:
            campos_e.append("EST_VENDA = ?")
            valores_e.append(dados.preco_venda)
            atualizados.append("preco_venda")

        if dados.margem is not None:
            campos_e.append("EST_MARGEM = ?")
            valores_e.append(dados.margem)
            atualizados.append("margem")

        if dados.perc_comissao is not None:
            campos_e.append("EST_PERCCOMISSAO = ?")
            valores_e.append(dados.perc_comissao)
            atualizados.append("perc_comissao")

        if dados.perc_imposto is not None:
            campos_e.append("EST_PERCIMPOSTO = ?")
            valores_e.append(dados.perc_imposto)
            atualizados.append("perc_imposto")

        if dados.perc_fixo is not None:
            campos_e.append("EST_PERCFIXO = ?")
            valores_e.append(dados.perc_fixo)
            atualizados.append("perc_fixo")

        if dados.perc_outros is not None:
            campos_e.append("EST_PERCOUTROS = ?")
            valores_e.append(dados.perc_outros)
            atualizados.append("perc_outros")

        if campos_e:
            campos_e.append("EST_DATAALTERACAO = ?")
            valores_e.append(agora)
            valores_e.append(codigo)
            sql_e = f"UPDATE ESTOQUE SET {', '.join(campos_e)} WHERE EST_PRODUTO = ?"
            cur.execute(sql_e, valores_e)

        if not atualizados:
            raise HTTPException(400, "Nenhum campo informado")

        con.commit()
        return {"ok": True, "codigo": codigo, "atualizados": atualizados}

    except HTTPException:
        raise
    except Exception as e:
        con.rollback()
        raise HTTPException(500, f"Erro: {e}")
    finally:
        con.close()

# ─── Rotas de administração ────────────────────────────────────

@app.post("/sync/puxar")
def sync_puxar(completo: bool = Query(False)):
    """Força sincronização do Enfoque para o banco local."""
    s = db.status_sync()
    delta_desde = None if completo else s.get("ultima_sync")
    n = puxar_enfoque(delta_desde=delta_desde)
    return {"sincronizados": n, "modo": "completo" if completo else "delta"}

@app.post("/sync/enviar")
def sync_enviar():
    """Força envio da fila pendente para o Enfoque."""
    return enviar_fila()

@app.get("/sync/status")
def sync_status():
    return {
        "enfoque_online": enfoque_online(),
        **db.status_sync()
    }
