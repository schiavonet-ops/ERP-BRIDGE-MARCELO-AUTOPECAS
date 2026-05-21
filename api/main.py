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

import core.local_db as db
from core.sync_worker import puxar_enfoque, enviar_fila, enfoque_online

app = FastAPI(
    title="Estoque Bridge — Enfoque ↔ AutoPeças Pro",
    version="1.0.0"
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

# ─── Background: tenta enviar fila sempre que Enfoque voltar ──

def _try_sync():
    if enfoque_online():
        enviar_fila()

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

# --- Enriquecimento com IA -----------------------------------

import httpx

class EnriquecerRequest(BaseModel):
    aprovar: bool = False

class SalvarEnriquecimentoRequest(BaseModel):
    nome: Optional[str] = None
    aplicacao: Optional[str] = None
    similares: Optional[str] = None

@app.get("/produto/{codigo}/enriquecer")
async def enriquecer_produto(codigo: int):
    """Chama Claude API para sugerir nome, aplicacao e similares."""
    p = db.get_produto(codigo)
    if not p:
        raise HTTPException(404, detail=f"Produto {codigo} nao encontrado")

    prompt = f"""Voce e um especialista em autopecas brasileiro.
Dado o produto: "{p['nome']}" (codigo fabricante: {p.get('cod_fabricante','')})

Responda APENAS em JSON valido, sem markdown, sem explicacao:
{{
  "nome_padronizado": "nome corrigido e padronizado em maiusculas",
  "aplicacao": "lista de veiculos separados por | Ex: FIAT PALIO 2000-2010 | FIAT SIENA 2001-2011",
  "similares": "outras marcas e codigos separados por | Ex: NAKATA NKF1234 | COFAP GS123"
}}

Se nao souber aplicacao ou similares, deixe string vazia."""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )

    if resp.status_code != 200:
        raise HTTPException(502, detail="Erro ao chamar Claude API")

    texto = resp.json()["content"][0]["text"].strip()
    try:
        import json
        sugestao = json.loads(texto)
    except Exception:
        raise HTTPException(502, detail=f"Resposta invalida da IA: {texto}")

    return {
        "codigo": codigo,
        "nome_atual": p["nome"],
        "sugestao": sugestao
    }

@app.post("/produto/{codigo}/salvar-enriquecimento")
def salvar_enriquecimento(codigo: int, body: SalvarEnriquecimentoRequest, bg: BackgroundTasks):
    """Salva enriquecimento no banco local e enfileira para o Enfoque (PRO_MEMO)."""
    p = db.get_produto(codigo)
    if not p:
        raise HTTPException(404, detail=f"Produto {codigo} nao encontrado")

    memo_parts = []
    if body.aplicacao:
        memo_parts.append(f"APLICACAO: {body.aplicacao}")
    if body.similares:
        memo_parts.append(f"SIMILARES: {body.similares}")
    memo = "\n".join(memo_parts)

    db.enfileirar(codigo, "atualizar_memo", 0, memo, "ENRIQUECIMENTO")
    bg.add_task(_try_sync)

    return {"status": "enfileirado", "codigo": codigo, "memo": memo}
