@echo off
rem Para o painel: encerra o laco do iniciar-painel.cmd e o Streamlit da porta 8501.
powershell -NoProfile -Command "$alvos = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*iniciar-painel.cmd*' -or ($_.CommandLine -like '*streamlit*run*app.py*' -and $_.CommandLine -like '*8501*')) }; if (-not $alvos) { 'Painel nao estava rodando.' } else { $alvos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; 'Encerrado PID ' + $_.ProcessId } }"
