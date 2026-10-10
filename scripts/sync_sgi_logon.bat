@echo off
REM ---------------------------------------------------------------------------
REM Robos do SGI desktop - COMPRAS (entradas) e TRANSFERENCIAS (saidas p/ Piata).
REM
REM Dois modos:
REM   sync_sgi_logon.bat          -> modo agendado (Agendador de Tarefas, "ao
REM                                  fazer logon"). Grava tudo no log e RODA NO
REM                                  MAXIMO UMA VEZ POR DIA.
REM   sync_sgi_logon.bat agora    -> modo manual (atalho da area de trabalho).
REM                                  Mostra a saida na tela e roda mesmo que ja
REM                                  tenha rodado hoje.
REM
REM POR QUE a trava de uma vez por dia: o gatilho do Agendador e "ao fazer logon",
REM e ele dispara em TODO logon - nao existe gatilho nativo "primeiro logon do
REM dia". E os robos nao sao no-op nas execucoes seguintes: compras sempre
REM rebusca os dias recentes (janela) e transferencias sempre reconsulta o
REM periodo. Sem a trava, cada logon abriria o SGI e tomaria o foco da maquina
REM por 1-2 minutos. O marcador (logs\ultimo_dia.txt) guarda o dia da ULTIMA
REM execucao BEM-SUCEDIDA - se falhar, nao marca, e o proximo logon tenta de novo.
REM
REM Os dois robos rodam EM SEQUENCIA, nunca em paralelo: dirigem a MESMA janela do
REM SGI por cliques reais, entao um roubaria o foco do outro.
REM
REM Nenhum dos dois leva data: compras olha sync_dias_compras e transferencias
REM comeca na ultima data gravada. Os dias de notebook desligado entram sozinhos.
REM ---------------------------------------------------------------------------

set PROJETO=C:\Users\Admin\Documents\estoque-defensivos\estoque-defensivos
set LOGS=%PROJETO%\logs
if not exist "%LOGS%" mkdir "%LOGS%"

REM Data de hoje via PowerShell, e nao via wmic: o wmic saiu das versoes recentes
REM do Windows, e sem a data o marcador nunca bateria (rodaria em todo logon).
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set HOJE=%%I
set LOG=%LOGS%\sgi_%HOJE%.log
set MARCADOR=%LOGS%\ultimo_dia.txt

set MANUAL=0
if /i "%~1"=="agora" set MANUAL=1

if "%MANUAL%"=="1" goto :rodar
if not exist "%MARCADOR%" goto :rodar
set ULTIMO=
set /p ULTIMO=<"%MARCADOR%"
if not "%ULTIMO%"=="%HOJE%" goto :rodar
echo Ja sincronizou hoje em %HOJE% - nada a fazer neste logon. >> "%LOG%"
exit /b 0

:rodar
cd /d "%PROJETO%"
if "%MANUAL%"=="1" goto :manual

echo ============================================================ >> "%LOG%"
echo Inicio: %date% %time% >> "%LOG%"
echo --- COMPRAS --- >> "%LOG%"
py -3.11-32 scripts\sync_compras_sgi.py >> "%LOG%" 2>&1
set RC_COMPRAS=%ERRORLEVEL%
echo --- TRANSFERENCIAS --- >> "%LOG%"
py -3.11-32 scripts\sync_transferencias_sgi.py >> "%LOG%" 2>&1
set RC_TRANSF=%ERRORLEVEL%
echo Fim: %date% %time% (compras: %RC_COMPRAS%, transferencias: %RC_TRANSF%) >> "%LOG%"
goto :fim

:manual
echo.
echo  NAO MEXA NO COMPUTADOR ENQUANTO RODA - a automacao depende de cliques
echo  reais e o SGI precisa ficar na frente. Leva 1 a 3 minutos.
echo.
echo ===================== COMPRAS =====================
py -3.11-32 scripts\sync_compras_sgi.py
set RC_COMPRAS=%ERRORLEVEL%
echo.
echo ================= TRANSFERENCIAS ==================
py -3.11-32 scripts\sync_transferencias_sgi.py
set RC_TRANSF=%ERRORLEVEL%

:fim
if not "%RC_COMPRAS%"=="0" goto :falhou
if not "%RC_TRANSF%"=="0" goto :falhou
REM So marca o dia quando OS DOIS deram certo.
>"%MARCADOR%" echo %HOJE%
if "%MANUAL%"=="1" (
    echo.
    echo  Tudo certo. Pode fechar esta janela.
    pause
)
exit /b 0

:falhou
if "%MANUAL%"=="1" (
    echo.
    echo  FALHOU - compras: %RC_COMPRAS%, transferencias: %RC_TRANSF%
    echo  A mensagem de erro esta logo acima. O dia NAO foi marcado como feito,
    echo  entao da pra tentar de novo clicando no icone outra vez.
    pause
)
if not "%RC_COMPRAS%"=="0" exit /b %RC_COMPRAS%
exit /b %RC_TRANSF%
