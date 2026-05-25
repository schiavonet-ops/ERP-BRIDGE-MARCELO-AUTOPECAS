"""
api/main.py — API REST: lê do banco local, escreve local + fila para Enfoque

Instalar:  pip install fastapi uvicorn fdb python-dotenv
Rodar:     uvicorn api.main:app --host 0.0.0.0 --port 8000
Docs:      http://localhost:8000/docs
"""

import sys, os, re, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

import core.local_db as db
from core.sync_worker import puxar_enfoque, enviar_fila, enfoque_online, _conectar
from core.nf_sync import puxar_nfs_enfoque  # ← ADIÇÃO 1

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
    grupo_nome: Optional[str] = None       # PRO_GRUPO (busca/cria por nome)
    grupo_codigo: Optional[int] = None     # PRO_GRUPO (codigo direto)
    secao_nome: Optional[str] = None       # PRO_SECAO (busca/cria por nome)
    secao_codigo: Optional[int] = None     # PRO_SECAO (codigo direto)
    marca_nome: Optional[str] = None       # PRO_MARCA (busca/cria por nome)
    marca_codigo: Optional[int] = None     # PRO_MARCA (codigo direto)
    # Campos da tabela ESTOQUE
    preco_sem_lucro: Optional[float] = None  # EST_VENDACUSTO
    preco_sugerido: Optional[float] = None   # EST_VENDASUGERIDO
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
    """
    Monta PRO_MEMO no formato modelo B.
    CORRIGIDO: remove delimitadores duplicados antes de montar,
    evitando que o conteudo ja venha com os cabecalhos incluidos.
    """
    # Limpa delimitadores que possam vir junto com o conteudo
    if aplicacao:
        aplicacao = re.sub(r'^-+\s*APLICACAO\s*-+\s*\n?', '', aplicacao, flags=re.MULTILINE)
        aplicacao = re.sub(r'^-+\s*\n?', '', aplicacao, flags=re.MULTILINE)
        aplicacao = aplicacao.strip()

    if conversao:
        conversao = re.sub(r'^-+\s*CONVERSAO\s*-+\s*\n?', '', conversao, flags=re.MULTILINE)
        conversao = re.sub(r'^-+\s*\n?', '', conversao, flags=re.MULTILINE)
        conversao = conversao.strip()

    partes = []
    if aplicacao:
        partes.append("-------APLICACAO-----------")
        partes.append(aplicacao)
    if conversao:
        if partes:
            partes.append("")  # linha em branco entre secoes
        partes.append("-------CONVERSAO-----------")
        partes.append(conversao)
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
    try:
        resultado = db.aplicar_movimentacao_local(codigo, body.quantidade, "baixar")
        db.enfileirar(codigo, "baixar", body.quantidade, body.referencia, body.origem)
        bg.add_task(_try_sync)
        return {**resultado, "sync": "enfileirado"}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

@app.post("/estoque/{codigo}/entrada")
def entrada(codigo: int, body: MovRequest, bg: BackgroundTasks):
    try:
        resultado = db.aplicar_movimentacao_local(codigo, body.quantidade, "entrada")
        db.enfileirar(codigo, "entrada", body.quantidade, body.referencia, body.origem)
        bg.add_task(_try_sync)
        return {**resultado, "sync": "enfileirado"}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

@app.post("/estoque/{codigo}/ajustar")
def ajustar(codigo: int, body: AjusteRequest, bg: BackgroundTasks):
    try:
        resultado = db.aplicar_movimentacao_local(codigo, body.quantidade_nova, "ajustar")
        db.enfileirar(codigo, "ajustar", body.quantidade_nova, body.referencia)
        bg.add_task(_try_sync)
        return {**resultado, "sync": "enfileirado"}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

@app.post("/estoque/os/{numero_os}")
def baixar_os(numero_os: str, body: OSRequest, bg: BackgroundTasks):
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

# ─── Helper: busca ou cria registro auxiliar (GRUPO/SECAO/MARCA) ─────────────

def _get_or_create(cur, tabela, campo_id, campo_nome, gerador, nome: str) -> int:
    nome_enc = nome.strip().encode("cp1252", errors="replace")
    cur.execute(f"SELECT {campo_id} FROM {tabela} WHERE UPPER({campo_nome}) = UPPER(?)", [nome_enc])
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(f"SELECT GEN_ID({gerador}, 1) FROM RDB$DATABASE")
    new_id = cur.fetchone()[0]
    cur.execute(
        f"INSERT INTO {tabela} ({campo_id}, {campo_nome}, {tabela[:3]}_DATAALTERACAO) VALUES (?, ?, ?)",
        [new_id, nome_enc, datetime.now()]
    )
    return new_id

@app.put("/produto/{codigo}")
def atualizar_produto(codigo: int, dados: ProdutoAtualizar):
    con = _conectar()
    try:
        cur = con.cursor()

        cur.execute("SELECT 1 FROM PRODUTO WHERE PRO_CODIGO = ?", [codigo])
        if not cur.fetchone():
            raise HTTPException(404, f"Produto {codigo} nao encontrado")

        atualizados = []
        agora = datetime.now()

        campos_p, valores_p = [], []

        if dados.descricao is not None:
            nome = dados.descricao.strip()
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
            # Busca memo atual para preservar o campo que nao foi enviado
            cur.execute("SELECT CAST(SUBSTRING(PRO_MEMO FROM 1 FOR 8000) AS VARCHAR(8000)) FROM PRODUTO WHERE PRO_CODIGO = ?", [codigo])
            row_memo = cur.fetchone()
            memo_atual = ""
            if row_memo and row_memo[0]:
                v = row_memo[0]
                memo_atual = v.decode("cp1252", errors="replace") if isinstance(v, bytes) else str(v)

            # Parse do memo atual para preservar o que nao foi enviado
            ap_atual, co_atual = _parse_memo(memo_atual)

            ap_final = dados.aplicacao if dados.aplicacao is not None else ap_atual
            co_final = dados.conversao if dados.conversao is not None else co_atual

            memo = _montar_memo(ap_final, co_final)
            campos_p.append("PRO_MEMO = ?")
            valores_p.append(memo.encode("cp1252", errors="replace"))
            atualizados.append("memo")

        grupo_id = None
        if dados.grupo_nome is not None:
            grupo_id = _get_or_create(cur, "GRUPO", "GRU_CODIGO", "GRU_NOME", "GRUPO", dados.grupo_nome)
        elif dados.grupo_codigo is not None:
            grupo_id = dados.grupo_codigo
        if grupo_id is not None:
            campos_p.append("PRO_GRUPO = ?")
            valores_p.append(grupo_id)
            atualizados.append("grupo")

        secao_id = None
        if dados.secao_nome is not None:
            secao_id = _get_or_create(cur, "SECAO", "SEC_CODIGO", "SEC_NOME", "SECAO", dados.secao_nome)
        elif dados.secao_codigo is not None:
            secao_id = dados.secao_codigo
        if secao_id is not None:
            campos_p.append("PRO_SECAO = ?")
            valores_p.append(secao_id)
            atualizados.append("secao")

        marca_id = None
        if dados.marca_nome is not None:
            marca_id = _get_or_create(cur, "MARCA", "MAR_CODIGO", "MAR_NOME", "MARCA", dados.marca_nome)
        elif dados.marca_codigo is not None:
            marca_id = dados.marca_codigo
        if marca_id is not None:
            campos_p.append("PRO_MARCA = ?")
            valores_p.append(marca_id)
            atualizados.append("marca")

        if campos_p:
            campos_p.append("PRO_DATAEDICAO = ?")
            valores_p.append(agora)
            valores_p.append(codigo)
            sql_p = f"UPDATE PRODUTO SET {', '.join(campos_p)} WHERE PRO_CODIGO = ?"
            cur.execute(sql_p, valores_p)

        campos_e, valores_e = [], []

        if dados.preco_sem_lucro is not None:
            campos_e.append("EST_VENDACUSTO = ?")
            valores_e.append(dados.preco_sem_lucro)
            atualizados.append("preco_sem_lucro")

        if dados.preco_sugerido is not None:
            campos_e.append("EST_VENDASUGERIDO = ?")
            valores_e.append(dados.preco_sugerido)
            atualizados.append("preco_sugerido")

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

        try:
            puxar_enfoque(codigo=codigo)
        except Exception:
            pass

        return {"ok": True, "codigo": codigo, "atualizados": atualizados}

    except HTTPException:
        raise
    except Exception as e:
        con.rollback()
        raise HTTPException(500, f"Erro: {e}")
    finally:
        con.close()


# ─── Parser de memo: separa APLICACAO e CONVERSAO ─────────────────────────────

def _parse_memo(memo_raw: str) -> tuple:
    """Separa PRO_MEMO em (aplicacao, conversao)."""
    if not memo_raw or not memo_raw.strip():
        return "", ""

    if "-------CONVERSAO-----------" in memo_raw:
        parts = memo_raw.split("-------CONVERSAO-----------", 1)
        aplicacao = parts[0].replace("-------APLICACAO-----------", "").strip()
        conversao = parts[1].strip()
        return aplicacao, conversao

    match = re.search(r"-{4,}", memo_raw)
    if match:
        before = memo_raw[:match.start()].strip()
        after = memo_raw[match.end():].strip()
        if before and after:
            return before, after

    lines = memo_raw.strip().splitlines()
    ap, co = [], []
    modo = "ap"
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if modo == "ap" and "  " in line and any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
            modo = "co"
        if modo == "co":
            co.append(s)
        else:
            ap.append(s)
    return "\n".join(ap), "\n".join(co)


# ─── Dados completos do produto para formulario de edicao ─────────────────────

@app.get("/produto/{codigo}/completo")
def produto_completo(codigo: int):
    con = _conectar()
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT
                p.PRO_CODIGO, p.PRO_NOME, p.PRO_CODPROPRIO, p.PRO_CODBARRA,
                p.PRO_LOCALIZACAO, p.PRO_GRUPO, p.PRO_SECAO, p.PRO_MARCA,
                CAST(SUBSTRING(p.PRO_MEMO FROM 1 FOR 8000) AS VARCHAR(8000)),
                p.PRO_NCM, p.PRO_CODBARRA2,
                COALESCE(e.EST_VENDA, 0), COALESCE(e.EST_VENDASUGERIDO, 0),
                COALESCE(e.EST_CUSTO, 0), COALESCE(e.EST_CUSTOMEDIO, 0),
                COALESCE(e.EST_MARGEM, 0), COALESCE(e.EST_PERCFIXO, 0),
                COALESCE(e.EST_PERCIMPOSTO, 0), COALESCE(e.EST_PERCCOMISSAO, 0),
                COALESCE(e.EST_PERCOUTROS, 0), COALESCE(e.EST_DESCONTOMAX, 0),
                COALESCE(e.EST_QTDE, 0), COALESCE(e.EST_MINIMO, 0)
            FROM PRODUTO p
            LEFT JOIN ESTOQUE e ON e.EST_PRODUTO = p.PRO_CODIGO
            WHERE p.PRO_CODIGO = ?
        """, [codigo])

        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Produto {codigo} nao encontrado")

        def s(v):
            if v is None: return ""
            if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
            return str(v).strip()

        def f(v):
            try: return float(v or 0)
            except: return 0.0

        def nome_aux(tabela, fid, fnome, cod):
            if not cod: return ""
            cur.execute(f"SELECT {fnome} FROM {tabela} WHERE {fid} = ?", [cod])
            r = cur.fetchone()
            if not r: return ""
            v = r[0]
            if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
            return str(v).strip()

        memo_raw = s(row[8])
        aplicacao, conversao = _parse_memo(memo_raw)

        return {
            "codigo":         row[0],
            "descricao":      s(row[1]),
            "codigo_proprio": s(row[2]),
            "cod_barras":     s(row[3]),
            "localizacao":    s(row[4]),
            "grupo_codigo":   row[5],
            "grupo_nome":     nome_aux("GRUPO","GRU_CODIGO","GRU_NOME", row[5]),
            "secao_codigo":   row[6],
            "secao_nome":     nome_aux("SECAO","SEC_CODIGO","SEC_NOME", row[6]),
            "marca_codigo":   row[7],
            "marca_nome":     nome_aux("MARCA","MAR_CODIGO","MAR_NOME", row[7]),
            "aplicacao":      aplicacao,
            "conversao":      conversao,
            "memo_raw":       memo_raw,
            "ncm":            s(row[9]),
            "cod_barras2":    s(row[10]),
            "preco_venda":    f(row[11]),
            "preco_sugerido": f(row[12]),
            "custo":          f(row[13]),
            "custo_medio":    f(row[14]),
            "margem":         f(row[15]),
            "perc_fixo":      f(row[16]),
            "perc_imposto":   f(row[17]),
            "perc_comissao":  f(row[18]),
            "perc_outros":    f(row[19]),
            "desconto_max":   f(row[20]),
            "estoque_atual":  f(row[21]),
            "estoque_minimo": f(row[22]),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro: {e}")
    finally:
        con.close()


# ─── Listagens auxiliares (Grupo / Secao / Marca) ─────────────────────────────

@app.get("/grupos")
def listar_grupos():
    con = _conectar()
    try:
        cur = con.cursor()
        cur.execute("SELECT GRU_CODIGO, GRU_NOME FROM GRUPO ORDER BY GRU_NOME")
        def s(v):
            if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
            return str(v).strip() if v else ""
        return [{"codigo": r[0], "nome": s(r[1])} for r in cur.fetchall()]
    finally:
        con.close()

@app.get("/secoes")
def listar_secoes():
    con = _conectar()
    try:
        cur = con.cursor()
        cur.execute("SELECT SEC_CODIGO, SEC_NOME FROM SECAO ORDER BY SEC_NOME")
        def s(v):
            if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
            return str(v).strip() if v else ""
        return [{"codigo": r[0], "nome": s(r[1])} for r in cur.fetchall()]
    finally:
        con.close()

@app.get("/marcas")
def listar_marcas():
    con = _conectar()
    try:
        cur = con.cursor()
        cur.execute("SELECT MAR_CODIGO, MAR_NOME FROM MARCA ORDER BY MAR_NOME")
        def s(v):
            if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
            return str(v).strip() if v else ""
        return [{"codigo": r[0], "nome": s(r[1])} for r in cur.fetchall()]
    finally:
        con.close()

# ─── Rotas de administração ────────────────────────────────────

@app.post("/sync/puxar")
def sync_puxar(completo: bool = Query(False)):
    s = db.status_sync()
    delta_desde = None if completo else s.get("ultima_sync")
    n = puxar_enfoque(delta_desde=delta_desde)
    return {"sincronizados": n, "modo": "completo" if completo else "delta"}

@app.post("/sync/enviar")
def sync_enviar():
    return enviar_fila()

@app.get("/sync/status")
def sync_status():
    return {
        "enfoque_online": enfoque_online(),
        **db.status_sync()
    }

@app.post("/sync/delta")
def sync_delta():
    """Força sync delta imediato — usado pelo EPP para atualizar sem esperar o cron."""
    s = db.status_sync()
    delta_desde = s.get("ultima_sync")
    n = puxar_enfoque(delta_desde=delta_desde)
    return {"sincronizados": n, "modo": "delta"}

# ─── Sync de NFs de entrada (separado, não interfere nos produtos) ────────────  ← ADIÇÃO 2

@app.post("/sync/nfs")
def sync_nfs(completo: bool = Query(False)):
    """Sincroniza NFs de compra do Enfoque → Supabase. Independente do sync de produtos."""
    from datetime import timedelta
    delta = None if completo else (datetime.now() - timedelta(days=7)).date()
    n = puxar_nfs_enfoque(delta_desde=delta)
    return {"sincronizadas": n, "modo": "completo" if completo else "delta"}


# ─── Histórico de movimentações do produto ────────────────────────────────────

@app.get("/produto/{codigo}/historico")
def produto_historico(codigo: int, limit: int = Query(100, le=500)):
    """
    Retorna histórico completo de movimentações do produto:
    vendas, OS, condicionais, entradas, ajustes.
    """
    con = _conectar()
    try:
        cur = con.cursor()

        def _s(v):
            if v is None: return ""
            if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
            return str(v).strip()

        def _f(v):
            try: return float(v or 0)
            except: return 0.0

        # Busca todas as movimentações do produto
        cur.execute(f"""
            SELECT FIRST {limit}
                m.MOV_CODIGO,
                m.MOV_DATA,
                m.MOV_HORAEMISSAO,
                m.MOV_QTDE,
                m.MOV_ESTOQUE,
                m.MOV_VALORUNI,
                m.MOV_VALORTOTAL,
                m.MOV_ORIGEM,
                m.MOV_NOTAENTRADA,
                m.MOV_NOTASAIDA,
                m.MOV_MEMO
            FROM MOVESTOQUE m
            WHERE m.MOV_PRODUTO = ?
              AND m.MOV_ISEXCLUIDO = 0
            ORDER BY m.MOV_CODIGO DESC
        """, [codigo])

        movimentos_raw = cur.fetchall()
        resultado = []

        for row in movimentos_raw:
            mov_codigo      = row[0]
            mov_data        = row[1]
            mov_hora        = row[2]
            mov_qtde        = _f(row[3])
            mov_saldo       = _f(row[4])
            mov_valor_uni   = _f(row[5])
            mov_valor_total = _f(row[6])
            mov_origem      = _s(row[7])
            not_entrada_id  = row[8]
            not_saida_id    = row[9]
            mov_memo        = _s(row[10])
            os_id           = None
            cond_id         = None

            tipo = "Ajuste"
            documento = ""
            cliente_nome = ""
            cliente_codigo = None
            numero_doc = ""
            detalhes = {}

            # ── Venda / OS / Condicional via NOTASAIDA ────────────────────
            if not_saida_id:
                tipo = "Venda"
                try:
                    cur.execute("""
                        SELECT ns.NOT_CODIGO, ns.NOT_NUMERO, ns.NOT_FICHA,
                               ns.NOT_DATA, ns.NOT_VALORTOTAL, ns.NOT_TIPO,
                               f.FIC_NOME,
                               os.ORD_CODIGO, os.ORD_STATUS, os.ORD_QUILOMETRAGEM,
                               v.VEI_PLACA, v.VEI_MODELO,
                               cn.CON_CODIGO, cn.CON_STATUS, cn.CON_DATAPREVISTA
                        FROM NOTASAIDA ns
                        LEFT JOIN FICHA f ON f.FIC_CODIGO = ns.NOT_FICHA
                        LEFT JOIN ORDEMSERVICO os ON os.ORD_CODIGO = ns.NOT_ORDEMSERVICO
                        LEFT JOIN VEICULO v ON v.VEI_CODIGO = os.ORD_VEICULO
                        LEFT JOIN CONDICIONAL cn ON cn.CON_CODIGO = ns.NOT_CONDICIONAL
                        WHERE ns.NOT_CODIGO = ?
                    """, [not_saida_id])
                    r = cur.fetchone()
                    if r:
                        numero_doc = _s(r[1]) or str(r[0])
                        cliente_codigo = r[2]
                        cliente_nome = _s(r[6])
                        not_tipo = r[5]

                        # Verifica se é OS
                        if r[7]:
                            tipo = "Ordem de Serviço"
                            os_id = r[7]
                            detalhes = {
                                "numero_nota": numero_doc,
                                "numero_os": r[7],
                                "status_os": r[8],
                                "quilometragem": r[9],
                                "veiculo_placa": _s(r[10]),
                                "veiculo_modelo": _s(r[11]),
                                "valor_total": _f(r[4])
                            }
                        # Verifica se é Condicional
                        elif r[12]:
                            tipo = "Condicional"
                            cond_id = r[12]
                            detalhes = {
                                "numero_nota": numero_doc,
                                "numero_condicional": r[12],
                                "status_condicional": r[13],
                                "data_prevista": str(r[14]) if r[14] else "",
                                "valor_total": _f(r[4])
                            }
                        else:
                            detalhes = {
                                "numero_nota": numero_doc,
                                "valor_total": _f(r[4])
                            }
                except Exception:
                    pass

            # ── Entrada (NOTAENTRADA) ──────────────────────────────────────
            elif not_entrada_id:
                tipo = "Entrada"
                try:
                    cur.execute("""
                        SELECT ne.NOT_CODIGO, ne.NOT_COMPROVANTE,
                               ne.NOT_DESCRICAO, ne.NOT_FICHA,
                               ne.NOT_VALORTOTAL, f.FIC_NOME
                        FROM NOTAENTRADA ne
                        LEFT JOIN FICHA f ON f.FIC_CODIGO = ne.NOT_FICHA
                        WHERE ne.NOT_CODIGO = ?
                    """, [not_entrada_id])
                    r = cur.fetchone()
                    if r:
                        numero_doc = _s(r[1]) or str(r[0])
                        descricao_entrada = _s(r[2])
                        cliente_codigo = r[3]
                        cliente_nome = _s(r[5])
                        # Detecta se é ajuste do bridge
                        if "bridge" in descricao_entrada.lower():
                            tipo = "Ajuste Bridge"
                        detalhes = {
                            "numero_entrada": numero_doc,
                            "descricao": descricao_entrada,
                            "valor_total": _f(r[4])
                        }
                except Exception:
                    pass

            # ── Detecta tipo pela origem e quantidade ──────────────────────
            if tipo == "Ajuste":
                if mov_origem == "V":
                    tipo = "Venda"
                elif mov_origem == "C":
                    tipo = "Compra/Entrada"
                elif mov_qtde > 0:
                    tipo = "Entrada"
                elif mov_qtde < 0:
                    tipo = "Saída"

            resultado.append({
                "mov_codigo":      mov_codigo,
                "data":            str(mov_data) if mov_data else "",
                "hora":            str(mov_hora) if mov_hora else "",
                "tipo":            tipo,
                "quantidade":      mov_qtde,
                "saldo_apos":      mov_saldo,
                "valor_unitario":  mov_valor_uni,
                "valor_total":     mov_valor_total,
                "documento":       numero_doc,
                "cliente_codigo":  cliente_codigo,
                "cliente_nome":    cliente_nome,
                "origem":          mov_origem,
                "detalhes":        detalhes,
            })

        return {
            "codigo": codigo,
            "total": len(resultado),
            "movimentos": resultado
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro: {e}")
    finally:
        con.close()
