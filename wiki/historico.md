# Histórico de Sessões — ERP Bridge Marcelo Auto Peças

## 2026-06-19 — Worker parado + solução definitiva de inicialização

### Problema
- Worker (WinSyncSvc) parado desde 18/06 22:08 (PC desligado à noite).
- Ao religar o PC, o serviço não subia sozinho.
- Tentativas de `sc start WinSyncSvc` falhavam com exit code 1 / erro 1066.
- Causa raiz: o serviço Windows foi criado com `binPath` apontando direto para
  `python.exe worker.py`. Python puro não implementa a API do Service Control
  Manager (SCM), então o serviço sempre crasha logo após iniciar.
- Fila `firebird_sync_queue` acumulou 9 itens pendentes (incluindo produto 106531).

### Solução definitiva
Abandonado o modelo "Windows Service". Migrado para **Task Scheduler**:
- Tarefa `WinSyncWorker`, trigger ONSTART (delay 30s), roda como SYSTEM.
- RestartCount=999, RestartInterval=1min, ExecutionTimeLimit=ilimitado.
- Criado **worker novo autocontido** (`installer/worker-simples.py`), baseado no
  antigo `fila_worker.py` do repo `ordemdeservico`. Não depende de venv, FastAPI
  nem do diretório `core/`. Só precisa de Python + fdb.
  - Detecta Python e fbclient.dll automaticamente (vários caminhos).
  - Instala fdb via pip se faltar.
  - Heartbeat próprio a cada ciclo (1 min); delta Firebird->EPP a cada 5 min.
- Instalador: `installer/instalar-simples.bat` (baixa o worker do GitHub e
  configura tudo). Distribuído via edge function para forçar download:
  `https://avxqyrkaddvtdogjsrtm.supabase.co/functions/v1/instalador`

### Resultado
- Worker rodando como `WorkerSimples-v1.0`, heartbeat OK.
- Fila zerada: 472 concluídos, 0 pendentes, 0 erros.
- PC pode ser desligado/religado sem intervenção manual.

### Egress (confirmação da otimização anterior)
- 17/06: 64,78 MB (pico durante migração)
- 18/06: 0,17 MB em 911 execuções
- Projeção: ~5 MB/mês no sync_pecas. Otimização anterior validada.

### Pendências / observações
- O MCP `WinSyncSvc Firebird` (`sync.oficinaconecatada.tech`) roda dentro do
  worker e fica indisponível quando o worker está parado. Considerar mover o
  endpoint MCP para fora do processo principal no futuro.
- Domínio `sync.oficinaconecatada.tech` não está na allowlist do sandbox Claude,
  então diagnóstico foi feito 100% via Supabase MCP + GitHub MCP.
