
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
