@echo off
chcp 65001 >nul
REM ============================================================
REM  INSTALADOR DEFINITIVO - WinSync via Task Scheduler
REM  Marcelo Auto Pecas
REM
REM  O que faz:
REM   1. Remove servico Windows quebrado (se existir)
REM   2. Cria tarefa agendada que inicia no boot
REM   3. Configura restart automatico em caso de falha
REM   4. Cria watchdog que verifica a cada 5 min
REM   5. Inicia o worker agora
REM
REM  Como usar: clique com botao direito > Executar como administrador
REM ============================================================

title Instalador WinSync - Marcelo Auto Pecas
color 0A

echo.
echo ============================================================
echo   INSTALADOR DEFINITIVO WinSync
echo   Marcelo Auto Pecas
echo ============================================================
echo.

REM Verifica admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo ERRO: Este script precisa ser executado como Administrador.
    echo.
    echo Feche esta janela, clique com botao direito no arquivo
    echo e escolha "Executar como administrador".
    echo.
    pause
    exit /b 1
)

REM Caminhos
set BASE=C:\MarceloWebEPP
set PYTHON=%BASE%\venv\Scripts\pythonw.exe
set PYTHON_FALLBACK=%BASE%\venv\Scripts\python.exe
set SCRIPT=%BASE%\monitor\worker.py
set LOG_DIR=%BASE%\logs
set TASK_WORKER=WinSyncWorker
set TASK_WATCHDOG=WinSyncWatchdog

REM Detecta python (pythonw para rodar escondido, senao python normal)
if not exist "%PYTHON%" set PYTHON=%PYTHON_FALLBACK%
if not exist "%PYTHON%" (
    color 0C
    echo ERRO: Python nao encontrado em %BASE%\venv\Scripts\
    echo.
    echo Verifique se o ambiente virtual foi criado corretamente.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    color 0C
    echo ERRO: Worker nao encontrado em %SCRIPT%
    pause
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [1/6] Removendo servico Windows antigo (se existir)...
sc query WinSyncSvc >nul 2>&1
if %errorlevel% equ 0 (
    sc stop WinSyncSvc >nul 2>&1
    timeout /t 3 /nobreak >nul
    sc delete WinSyncSvc >nul 2>&1
    echo     Servico antigo removido.
) else (
    echo     Nenhum servico antigo encontrado.
)

echo [2/6] Removendo tarefas antigas (se existirem)...
schtasks /Delete /TN "%TASK_WORKER%" /F >nul 2>&1
schtasks /Delete /TN "%TASK_WATCHDOG%" /F >nul 2>&1

echo [3/6] Criando tarefa principal: %TASK_WORKER%
echo     - Inicia no boot do Windows
echo     - Roda como SYSTEM (sem precisar login)
echo     - Restart automatico em caso de falha

REM Cria XML da tarefa principal
set XML_WORKER=%TEMP%\winsync_worker.xml
(
echo ^<?xml version="1.0" encoding="UTF-16"?^>
echo ^<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
echo   ^<RegistrationInfo^>
echo     ^<Description^>WinSync Worker - sincronizacao Firebird ^<-^> Supabase EPP^</Description^>
echo     ^<Author^>Marcelo Auto Pecas^</Author^>
echo   ^</RegistrationInfo^>
echo   ^<Triggers^>
echo     ^<BootTrigger^>
echo       ^<Enabled^>true^</Enabled^>
echo       ^<Delay^>PT30S^</Delay^>
echo     ^</BootTrigger^>
echo   ^</Triggers^>
echo   ^<Principals^>
echo     ^<Principal id="Author"^>
echo       ^<UserId^>S-1-5-18^</UserId^>
echo       ^<RunLevel^>HighestAvailable^</RunLevel^>
echo     ^</Principal^>
echo   ^</Principals^>
echo   ^<Settings^>
echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
echo     ^<DisallowStartIfOnBatteries^>false^</DisallowStartIfOnBatteries^>
echo     ^<StopIfGoingOnBatteries^>false^</StopIfGoingOnBatteries^>
echo     ^<AllowHardTerminate^>true^</AllowHardTerminate^>
echo     ^<StartWhenAvailable^>true^</StartWhenAvailable^>
echo     ^<RunOnlyIfNetworkAvailable^>false^</RunOnlyIfNetworkAvailable^>
echo     ^<IdleSettings^>
echo       ^<StopOnIdleEnd^>false^</StopOnIdleEnd^>
echo       ^<RestartOnIdle^>false^</RestartOnIdle^>
echo     ^</IdleSettings^>
echo     ^<AllowStartOnDemand^>true^</AllowStartOnDemand^>
echo     ^<Enabled^>true^</Enabled^>
echo     ^<Hidden^>false^</Hidden^>
echo     ^<RunOnlyIfIdle^>false^</RunOnlyIfIdle^>
echo     ^<DisallowStartOnRemoteAppSession^>false^</DisallowStartOnRemoteAppSession^>
echo     ^<UseUnifiedSchedulingEngine^>true^</UseUnifiedSchedulingEngine^>
echo     ^<WakeToRun^>false^</WakeToRun^>
echo     ^<ExecutionTimeLimit^>PT0S^</ExecutionTimeLimit^>
echo     ^<Priority^>7^</Priority^>
echo     ^<RestartOnFailure^>
echo       ^<Interval^>PT1M^</Interval^>
echo       ^<Count^>999^</Count^>
echo     ^</RestartOnFailure^>
echo   ^</Settings^>
echo   ^<Actions Context="Author"^>
echo     ^<Exec^>
echo       ^<Command^>%PYTHON%^</Command^>
echo       ^<Arguments^>"%SCRIPT%"^</Arguments^>
echo       ^<WorkingDirectory^>%BASE%^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%XML_WORKER%"

schtasks /Create /TN "%TASK_WORKER%" /XML "%XML_WORKER%" /F >nul
if %errorlevel% neq 0 (
    color 0C
    echo     ERRO ao criar tarefa principal.
    pause
    exit /b 1
)
echo     OK

echo [4/6] Criando watchdog: %TASK_WATCHDOG%
echo     - Verifica a cada 5 minutos se o worker esta rodando
echo     - Se nao estiver, inicia automaticamente

set WATCHDOG_BAT=%BASE%\monitor\watchdog.bat
(
echo @echo off
echo REM Watchdog WinSync - verifica e reinicia se necessario
echo schtasks /Query /TN "%TASK_WORKER%" /V /FO LIST 2^>nul ^| findstr /C:"Em execu" /C:"Running" ^>nul
echo if %%errorlevel%% neq 0 ^(
echo     echo [%%date%% %%time%%] Worker parado, reiniciando... ^>^> "%LOG_DIR%\watchdog.log"
echo     schtasks /Run /TN "%TASK_WORKER%" ^>nul 2^>^&1
echo ^)
) > "%WATCHDOG_BAT%"

schtasks /Create /TN "%TASK_WATCHDOG%" /TR "\"%WATCHDOG_BAT%\"" /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /F >nul
if %errorlevel% neq 0 (
    color 0E
    echo     AVISO: watchdog nao foi criado, mas o worker principal esta OK.
) else (
    echo     OK
)

echo [5/6] Iniciando o worker agora...
schtasks /Run /TN "%TASK_WORKER%" >nul 2>&1
timeout /t 3 /nobreak >nul
echo     OK

echo [6/6] Verificando status...
schtasks /Query /TN "%TASK_WORKER%" /FO LIST | findstr /C:"Status" /C:"Próxima" /C:"Next"

echo.
echo ============================================================
echo   INSTALACAO CONCLUIDA
echo ============================================================
echo.
echo   - Worker inicia automaticamente quando o Windows liga
echo   - Se travar, reinicia sozinho em 1 minuto (ate 999 tentativas)
echo   - Watchdog verifica a cada 5 min e reinicia se necessario
echo   - Logs em: %LOG_DIR%\
echo.
echo   Nao precisa mais rodar nenhum comando manual.
echo   Pode desligar e ligar o PC normalmente.
echo.
pause
