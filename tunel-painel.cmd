@echo off
rem Tunel da Cloudflare: publica o painel local (127.0.0.1:8501) em
rem https://painel.quantunlab.com.br e reinicia se cair. Chamado ao entrar no
rem Windows pelo "Tunel do Painel.vbs" da pasta Inicializar. Log em tunnel.log.
rem Para parar: feche o cloudflared.exe pelo Gerenciador de Tarefas.
cd /d "%~dp0"

rem Uma instancia so: se ja ha um cloudflared servindo este tunel, esta sai.
wmic process where "name='cloudflared.exe'" get commandline 2>nul | findstr /c:"painel-daytrade" >nul
if not errorlevel 1 (
  echo [%date% %time%] o tunel painel-daytrade ja esta rodando; esta instancia vai sair>> tunnel.log
  exit /b 0
)

if not exist "%USERPROFILE%\.cloudflared\config.yml" (
  echo [%date% %time%] sem config.yml; rode o publicar-painel.cmd primeiro>> tunnel.log
  exit /b 1
)

:laco
rem Log com rotacao simples: acima de 5 MB vira tunnel.old.log
if exist tunnel.log for %%A in (tunnel.log) do if %%~zA GTR 5000000 move /y tunnel.log tunnel.old.log >nul
echo [%date% %time%] iniciando o tunel painel-daytrade>> tunnel.log
rem Caminho completo: o Windows daqui nao procura executavel na pasta atual.
"%~dp0cloudflared.exe" --config "%USERPROFILE%\.cloudflared\config.yml" --no-autoupdate tunnel run painel-daytrade >> tunnel.log 2>&1
echo [%date% %time%] o tunel encerrou; nova tentativa em 30 s>> tunnel.log
ping -n 31 127.0.0.1 >nul
goto laco
