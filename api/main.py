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
    descricao: Optional[str] = None
    codigo_proprio: Optional[str] = None
    localizacao: Optional[str] = None
    aplicacao: Optional[str] = None
    conversao: Optional[str] = None
    grupo_nome: Optional[str] = None
    grupo_codigo: Optional[int] = None
    secao_nome: Optional[str] = None
    secao_codigo: Optional[int] = None
    marca_nome: Optional[str] = None
    marca_codigo: Optional[int] = None
    preco_sem_lucro: Optional[float] = None
    preco_sugerido: Optional[float] = None
    preco_venda: Optional[float] = None
    margem: Optional[float] = None
    perc_comissao: Optional[float] = None
    perc_imposto: Optional[float] = None
    perc_fixo: Optional[float] = None
    perc_outros: Optional[float] = None

# ─── Background ───────────────────────────────────────────────

def _try_sync():
    if enfoque_online():
        enviar_fila()

# ─── Helper memo ──────────────────────────────────────────────

def _montar_memo(aplicacao: Optional[str], conversao: Optional[str]) -> str:
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
            partes.append("")
        partes.append("-------CONVERSAO-----------")
        partes.append(conversao)
    return "\n".join(partes)

def _parse_memo(memo_raw: str) -> tuple:
    if not memo_raw or not memo_raw.strip():
        return "", ""
    if "-------CONVERSAO-----------" in memo_raw:
        parts = memo_raw.split("-------CONVERSAO-----------", 1)
        return parts[0].replace("-------APLICACAO-----------", "").strip(), parts[1].strip()
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
        (co if modo == "co" else ap).append(s)
    return "\n".join(ap), "\n".join(co)

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

# ─── Rotas de escrita ─────────────────────────────────────────

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

# ─── Helper auxiliares ────────────────────────────────────────

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
            campos_p.append("PRO_NOME = ?")
            valores_p.append(dados.descricao.strip()[:120].encode("cp1252", errors="replace"))
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
            cur.execute("SELECT CAST(SUBSTRING(PRO_MEMO FROM 1 FOR 8000) AS VARCHAR(8000)) FROM PRODUTO WHERE PRO_CODIGO = ?", [codigo])
            row_memo = cur.fetchone()
            memo_atual = ""
            if row_memo and row_memo[0]:
                v = row_memo[0]
                memo_atual = v.decode("cp1252", errors="replace") if isinstance(v, bytes) else str(v)
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
            cur.execute(f"UPDATE PRODUTO SET {', '.join(campos_p)} WHERE PRO_CODIGO = ?", valores_p)

        campos_e, valores_e = [], []
        for campo_py, campo_sql in [
            ("preco_sem_lucro", "EST_VENDACUSTO"),
            ("preco_sugerido",  "EST_VENDASUGERIDO"),
            ("preco_venda",     "EST_VENDA"),
            ("margem",          "EST_MARGEM"),
            ("perc_comissao",   "EST_PERCCOMISSAO"),
            ("perc_imposto",    "EST_PERCIMPOSTO"),
            ("perc_fixo",       "EST_PERCFIXO"),
            ("perc_outros",     "EST_PERCOUTROS"),
        ]:
            val = getattr(dados, campo_py)
            if val is not None:
                campos_e.append(f"{campo_sql} = ?")
                valores_e.append(val)
                atualizados.append(campo_py)

        if campos_e:
            campos_e.append("EST_DATAALTERACAO = ?")
            valores_e.append(agora)
            valores_e.append(codigo)
            cur.execute(f"UPDATE ESTOQUE SET {', '.join(campos_e)} WHERE EST_PRODUTO = ?", valores_e)

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
            return v.decode("cp1252", errors="replace").strip() if isinstance(v, bytes) else str(v).strip()

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

@app.get("/grupos")
def listar_grupos():
    con = _conectar()
    try:
        cur = con.cursor()
        cur.execute("SELECT GRU_CODIGO, GRU_NOME FROM GRUPO ORDER BY GRU_NOME")
        def s(v):
            return v.decode("cp1252", errors="replace").strip() if isinstance(v, bytes) else str(v or "").strip()
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
            return v.decode("cp1252", errors="replace").strip() if isinstance(v, bytes) else str(v or "").strip()
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
            return v.decode("cp1252", errors="replace").strip() if isinstance(v, bytes) else str(v or "").strip()
        return [{"codigo": r[0], "nome": s(r[1])} for r in cur.fetchall()]
    finally:
        con.close()

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
    s = db.status_sync()
    delta_desde = s.get("ultima_sync")
    n = puxar_enfoque(delta_desde=delta_desde)
    return {"sincronizados": n, "modo": "delta"}

@app.get("/produto/{codigo}/historico")
def produto_historico(codigo: int, limit: int = Query(100, le=500)):
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

        cur.execute(f"""
            SELECT FIRST {limit}
                m.MOV_CODIGO, m.MOV_DATA, m.MOV_HORAEMISSAO,
                m.MOV_QTDE, m.MOV_ESTOQUE, m.MOV_VALORUNI, m.MOV_VALORTOTAL,
                m.MOV_ORIGEM, m.MOV_NOTAENTRADA, m.MOV_NOTASAIDA, m.MOV_MEMO
            FROM MOVESTOQUE m
            WHERE m.MOV_PRODUTO = ? AND m.MOV_ISEXCLUIDO = 0
            ORDER BY m.MOV_CODIGO DESC
        """, [codigo])

        resultado = []
        for row in cur.fetchall():
            not_entrada_id = row[8]
            not_saida_id   = row[9]
            mov_qtde       = _f(row[3])
            mov_origem     = _s(row[7])
            tipo = "Ajuste"
            numero_doc = ""
            cliente_nome = ""
            cliente_codigo = None
            detalhes = {}

            if not_saida_id:
                tipo = "Venda"
                try:
                    cur.execute("""
                        SELECT ns.NOT_NUMERO, ns.NOT_FICHA, ns.NOT_DATA,
                               ns.NOT_VALORTOTAL, f.FIC_NOME,
                               os.ORD_CODIGO, os.ORD_STATUS,
                               v.VEI_PLACA, v.VEI_MODELO,
                               cn.CON_CODIGO, cn.CON_STATUS
                        FROM NOTASAIDA ns
                        LEFT JOIN FICHA f ON f.FIC_CODIGO = ns.NOT_FICHA
                        LEFT JOIN ORDEMSERVICO os ON os.ORD_CODIGO = ns.NOT_ORDEMSERVICO
                        LEFT JOIN VEICULO v ON v.VEI_CODIGO = os.ORD_VEICULO
                        LEFT JOIN CONDICIONAL cn ON cn.CON_CODIGO = ns.NOT_CONDICIONAL
                        WHERE ns.NOT_CODIGO = ?
                    """, [not_saida_id])
                    r = cur.fetchone()
                    if r:
                        numero_doc = _s(r[0])
                        cliente_codigo = r[1]
                        cliente_nome = _s(r[4])
                        if r[5]:
                            tipo = "Ordem de Servico"
                            detalhes = {"numero_os": r[5], "status_os": r[6],
                                        "veiculo_placa": _s(r[7]), "veiculo_modelo": _s(r[8])}
                        elif r[9]:
                            tipo = "Condicional"
                            detalhes = {"numero_condicional": r[9], "status": r[10]}
                        else:
                            detalhes = {"numero_nota": numero_doc, "valor_total": _f(r[3])}
                except Exception:
                    pass

            elif not_entrada_id:
                tipo = "Entrada"
                try:
                    cur.execute("""
                        SELECT ne.NOT_COMPROVANTE, ne.NOT_DESCRICAO,
                               ne.NOT_FICHA, ne.NOT_VALORTOTAL, f.FIC_NOME
                        FROM NOTAENTRADA ne
                        LEFT JOIN FICHA f ON f.FIC_CODIGO = ne.NOT_FICHA
                        WHERE ne.NOT_CODIGO = ?
                    """, [not_entrada_id])
                    r = cur.fetchone()
                    if r:
                        numero_doc = _s(r[0])
                        descr = _s(r[1])
                        cliente_codigo = r[2]
                        cliente_nome = _s(r[4])
                        if "bridge" in descr.lower():
                            tipo = "Ajuste Bridge"
                        detalhes = {"numero_entrada": numero_doc, "descricao": descr,
                                    "valor_total": _f(r[3])}
                except Exception:
                    pass

            if tipo == "Ajuste":
                if mov_origem == "V": tipo = "Venda"
                elif mov_origem == "C": tipo = "Compra/Entrada"
                elif mov_qtde > 0: tipo = "Entrada"
                elif mov_qtde < 0: tipo = "Saida"

            resultado.append({
                "mov_codigo":     row[0],
                "data":           str(row[1]) if row[1] else "",
                "hora":           str(row[2]) if row[2] else "",
                "tipo":           tipo,
                "quantidade":     mov_qtde,
                "saldo_apos":     _f(row[4]),
                "valor_unitario": _f(row[5]),
                "valor_total":    _f(row[6]),
                "documento":      numero_doc,
                "cliente_codigo": cliente_codigo,
                "cliente_nome":   cliente_nome,
                "origem":         mov_origem,
                "detalhes":       detalhes,
            })

        return {"codigo": codigo, "total": len(resultado), "movimentos": resultado}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro: {e}")
    finally:
        con.close()
