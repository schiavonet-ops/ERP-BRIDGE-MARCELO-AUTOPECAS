# Instalador WinSync — Marcelo Auto Peças

## Uso

**Para instalar/reinstalar o worker de forma definitiva:**

1. Baixar `instalar-definitivo.bat`
2. Clicar com botão direito → **Executar como administrador**
3. Aguardar concluir
4. Pronto — nunca mais precisa rodar comando

## O que faz

Substitui o Windows Service quebrado por uma **Tarefa Agendada do Windows**, que:

- ✅ Inicia automaticamente no boot (30s após login)
- ✅ Roda como `SYSTEM` (não precisa de usuário logado)
- ✅ Reinicia em 1 minuto se travar (até 999 tentativas)
- ✅ Watchdog separado verifica a cada 5 min e reinicia se preciso
- ✅ Logs em `C:\MarceloWebEPP\logs\`

## Por que Task Scheduler em vez de Service?

Python puro não implementa a API de Service Control Manager (SCM) do Windows. Por isso `sc create WinSyncSvc binPath=python.exe worker.py` cria um serviço que **sempre crasha com exit code 1** logo após iniciar — não importa quantas vezes você execute `sc start`.

As alternativas seriam:
- **pywin32 + win32serviceutil**: requer wrapper Python complexo, com risco de quebrar a cada update
- **NSSM**: precisa baixar binário externo, mais um ponto de falha
- **Task Scheduler**: nativo do Windows, sem dependências extras, mais robusto para scripts Python

Task Scheduler é a solução padrão da indústria para esse caso.

## Arquivos

- `instalar-definitivo.bat` — instalador completo (Task Scheduler + Watchdog)
- `corrigir_inicio_automatico.bat` — apenas ajusta serviço existente (use só se for downgrade)
- `instalar_servico.bat` — instalador via Windows Service (legado, NÃO use)
