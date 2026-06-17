# Marcelo Auto Peças — ERP Bridge (Worker de Sincronização)

Este repositório é o **worker de sincronização** entre o ERP Enfoque (Firebird local) e o sistema EPP online da Marcelo Auto Peças.

## O que faz
- Sincroniza produtos e estoque do Enfoque → Supabase (tabela `enfoque_produto`)
- Aplica movimentações de OS do EPP → Firebird (NOTAENTRADA + MOVESTOQUE)
- Sincroniza notas fiscais de compra
- Mantém banco local SQLite como espelho offline
- Expõe API REST em `sync.oficinaconecatada.tech`
- Roda no PC da oficina como serviço Windows (WinSyncSvc)

## Stack
- Python + FastAPI
- FDB (conector Firebird)
- SQLite (banco local)
- HTTPX (comunicação com Supabase)

## Repositório vinculado
**`schiavonet-ops/osintegrada`** — Frontend e backend web do EPP (sistema online).
- React 18 + TanStack Router + Supabase
- Produção: `oficinaconcatada.tech`
- **Este worker é essencial para o EPP funcionar com dados reais do Enfoque**

## Supabase
- Projeto: `ordem de servico`
- ID: `avxqyrkaddvtdogjsrtm`

## Arquivos principais
- `api/main.py` — API REST (endpoints de estoque, movimentações, sync)
- `core/sync_worker.py` — Sincronização Enfoque ↔ Supabase
- `core/local_db.py` — Banco local SQLite + hash de mudanças
- `core/nf_sync.py` — Sincronização de notas fiscais
- `core/scheduler.py` — Scheduler adaptativo (20s/35s/2min/5min)
