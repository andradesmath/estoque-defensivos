@echo off
REM ---------------------------------------------------------------------------
REM Robos do SGI desktop - disparados pelo Agendador de Tarefas do Windows no
REM primeiro logon do dia (ver README: "Agendamento ao ligar o notebook").
REM
REM Roda COMPRAS (entradas) e depois TRANSFERENCIAS (saidas para Piata), nessa
REM ordem e UM DE CADA VEZ. A ordem em si nao importa pro saldo; o que importa e
REM nao rodar os dois ao mesmo tempo: os dois dirigem a MESMA janela do SGI por
REM cliques reais, entao em paralelo um roubaria o foco do outro e os dois
REM clicariam no lugar errado. Por isso e um .bat com os dois, e nao duas tarefas
REM separadas no Agendador.
REM
REM Por que um .bat e nao chamar o python direto na tarefa: aqui da pra fixar a
REM pasta do projeto (o Agendador inicia em C:\Windows\system32) e guardar a saida
REM num log por dia, que e como voce confere depois se rodou e o que entrou.
REM
REM Nao precisa de parametro de data em nenhum dos dois:
REM   - compras olha sync_dias_compras e busca os dias que faltam;
REM   - transferencias comeca na ultima transferencia gravada e vai ate hoje.
REM Ou seja, os dias em que o notebook ficou desligado entram no proximo logon.
REM ---------------------------------------------------------------------------

set PROJETO=C:\Users\Admin\Documents\estoque-defensivos\estoque-defensivos
set LOGS=%PROJETO%\logs
if not exist "%LOGS%" mkdir "%LOGS%"

REM Nome do log por dia: sgi_AAAA-MM-DD.log
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set HOJE=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%
set LOG=%LOGS%\sgi_%HOJE%.log

cd /d "%PROJETO%"
echo ============================================================ >> "%LOG%"
echo Inicio: %date% %time% >> "%LOG%"

echo --- COMPRAS --- >> "%LOG%"
py -3.11-32 scripts\sync_compras_sgi.py >> "%LOG%" 2>&1
set RC_COMPRAS=%ERRORLEVEL%

echo --- TRANSFERENCIAS --- >> "%LOG%"
py -3.11-32 scripts\sync_transferencias_sgi.py >> "%LOG%" 2>&1
set RC_TRANSF=%ERRORLEVEL%

echo Fim: %date% %time% (compras: %RC_COMPRAS%, transferencias: %RC_TRANSF%) >> "%LOG%"

REM Sai diferente de zero se QUALQUER um falhou - assim a tarefa aparece como
REM "com erro" no Agendador em vez de passar batido.
if not "%RC_COMPRAS%"=="0" exit /b %RC_COMPRAS%
exit /b %RC_TRANSF%
