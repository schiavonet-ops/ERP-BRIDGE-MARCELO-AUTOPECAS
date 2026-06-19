@echo off
REM ============================================================
REM  instalar_servico.bat — Marcelo Auto Pecas / WinSyncSvc
REM  Deve ser executado como Administrador
REM ============================================================

echo.
echo === Instalador WinSyncSvc ===
echo.

REM -- Caminhos
set BASE=C:\MarceloWebEPP
set PYTHON=%BASE%\venv\Scripts\python.exe
set SCRIPT=%BASE%\monitor\worker.py
set SERVICO=WinSyncSvc

REM -- Verificar se está rodando como admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERRO: Execute este script como Administrador.
    pause
    exit /b 1
)

REM -- Parar e remover servico existente se houver
sc query %SERVICO% >nul 2>&1
if %errorlevel% equ 0 (
    echo Parando servico existente...
    sc stop %SERVICO% >nul 2>&1
    timeout /t 3 /nobreak >nul
    sc delete %SERVICO% >nul 2>&1
    timeout /t 2 /nobreak >nul
    echo Servico anterior removido.
)

REM -- Criar servico com inicio automatico
echo Criando servico %SERVICO%...
sc create %SERVICO% binPath= "\"%PYTHON%\" \"%SCRIPT%\"" start= auto DisplayName= "WinSyncSvc - Marcelo Auto Pecas"

if %errorlevel% neq 0 (
    echo ERRO ao criar servico. Verifique os caminhos acima.
    pause
    exit /b 1
)

REM -- Configurar reinicio automatico em caso de falha
REM    1a falha: reinicia em 30s | 2a falha: reinicia em 60s | demais: reinicia em 120s
echo Configurando reinicio automatico em caso de falha...
sc failure %SERVICO% reset= 86400 actions= restart/30000/restart/60000/restart/120000

REM -- Descricao do servico
sc description %SERVICO% "Sincroniza dados entre ERP Enfoque (Firebird) e Supabase EPP. Inicio automatico com o Windows."

REM -- Iniciar agora
echo Iniciando servico...
sc start %SERVICO%

timeout /t 3 /nobreak >nul

REM -- Verificar status
sc query %SERVICO% | findstr STATE

echo.
echo === Instalacao concluida ===
echo O servico %SERVICO% esta configurado para iniciar automaticamente com o Windows.
echo Em caso de falha, reinicia sozinho apos 30s (1a vez), 60s (2a vez), 120s (demais).
echo.
pause
