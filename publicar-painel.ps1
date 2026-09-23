# Publica o painel em https://painel.quantunlab.com.br, pelo tunel da Cloudflare.
#
# Roda uma vez so. Em ordem: senha do painel -> autorizacao da Cloudflare (abre o
# navegador, voce clica) -> tunel painel-daytrade -> DNS do subdominio -> inicio
# automatico junto com o Windows. Pode rodar de novo sem medo: cada passo que ja
# esta feito e pulado.
#
# Use o publicar-painel.cmd (duplo clique) para nao precisar mexer em politica de
# execucao do PowerShell.

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$raiz     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$exe      = Join-Path $raiz 'cloudflared.exe'
$pastaCf  = Join-Path $env:USERPROFILE '.cloudflared'
$cert     = Join-Path $pastaCf 'cert.pem'
$config   = Join-Path $pastaCf 'config.yml'
$acesso   = Join-Path $env:LOCALAPPDATA 'PainelDayTrade\acesso.json'
$tunel    = 'painel-daytrade'
$endereco = 'painel.quantunlab.com.br'
$porta    = 8501
$inicio   = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Tunel do Painel.vbs'

function Passo($n, $texto) {
    Write-Host ''
    Write-Host ("[$n] " + $texto) -ForegroundColor Cyan
}

Write-Host ''
Write-Host '  PUBLICAR O PAINEL EM painel.quantunlab.com.br' -ForegroundColor White
Write-Host '  ---------------------------------------------'

# ---------------------------------------------------------------- 1. senha
Passo '1/6' 'Senha do painel'
if (Test-Path $acesso) {
    Write-Host '      ja configurada (para trocar: python configurar_senha.py)'
} else {
    Write-Host '      O painel vai ficar acessivel pela internet, entao ele precisa de senha.'
    Write-Host '      Digite abaixo. Nao aparece na tela, nao fica salva em lugar nenhum:'
    Write-Host '      so o hash dela vai para %LOCALAPPDATA%\PainelDayTrade\acesso.json.'
    Write-Host ''
    $py = 'C:\Users\Vitor\AppData\Local\Python\pythoncore-3.14-64\python.exe'
    if (-not (Test-Path $py)) { $py = 'python' }
    & $py (Join-Path $raiz 'configurar_senha.py')
    if (-not (Test-Path $acesso)) {
        throw 'sem senha configurada. Nao vou publicar o painel aberto na internet; rode de novo.'
    }
}

# --------------------------------------------------- 2. autorizacao Cloudflare
Passo '2/6' 'Autorizacao da Cloudflare'
if (Test-Path $cert) {
    Write-Host '      ja autorizado (cert.pem presente)'
} else {
    Write-Host '      Vai abrir o navegador. Escolha quantunlab.com.br na lista e clique'
    Write-Host '      em Authorize. Esta janela espera por isso.'
    Write-Host ''
    & $exe tunnel login
    if (-not (Test-Path $cert)) {
        throw 'a autorizacao nao chegou (cert.pem nao apareceu). Rode de novo e clique em Authorize.'
    }
    Write-Host '      autorizado' -ForegroundColor Green
}

# -------------------------------------------------------------- 3. tunel
Passo '3/6' ('Tunel ' + $tunel)
$lista = & $exe tunnel list --output json | ConvertFrom-Json
$alvo  = $lista | Where-Object { $_.name -eq $tunel } | Select-Object -First 1
if ($alvo) {
    Write-Host ('      ja existe (id ' + $alvo.id + ')')
} else {
    & $exe tunnel create $tunel
    $lista = & $exe tunnel list --output json | ConvertFrom-Json
    $alvo  = $lista | Where-Object { $_.name -eq $tunel } | Select-Object -First 1
    if (-not $alvo) { throw 'nao consegui criar o tunel' }
    Write-Host ('      criado (id ' + $alvo.id + ')') -ForegroundColor Green
}
$credencial = Join-Path $pastaCf ($alvo.id + '.json')
if (-not (Test-Path $credencial)) { throw ('faltou o arquivo de credencial ' + $credencial) }

# -------------------------------------------------------------- 4. config.yml
Passo '4/6' 'Arquivo de configuracao'
$yaml = @"
# Tunel do painel de opcoes. Gerado por publicar-painel.ps1.
tunnel: $($alvo.id)
credentials-file: $credencial
no-autoupdate: true

ingress:
  - hostname: $endereco
    service: http://127.0.0.1:$porta
    originRequest:
      connectTimeout: 30s
      noHappyEyeballs: true
  - service: http_status:404
"@
[System.IO.File]::WriteAllText($config, $yaml, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ('      ' + $config)

# -------------------------------------------------------------- 5. DNS
Passo '5/6' ('DNS de ' + $endereco)
& $exe tunnel route dns --overwrite-dns $tunel $endereco
if ($LASTEXITCODE -ne 0) { throw 'nao consegui criar o registro de DNS' }

# -------------------------------------------- 6. inicio junto com o Windows
Passo '6/6' 'Inicio automatico'
$cmd = Join-Path $raiz 'tunel-painel.cmd'
$vbs = @"
' Tunel do painel: sobe em segundo plano ao entrar no Windows.
' Para desativar o inicio automatico, apague este arquivo.
CreateObject("WScript.Shell").Run """$cmd""", 0, False
"@
[System.IO.File]::WriteAllText($inicio, $vbs, (New-Object System.Text.UnicodeEncoding($false, $true)))
Write-Host ('      ' + $inicio)

$jaRoda = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
          Where-Object { $_.CommandLine -like "*$tunel*" }
if ($jaRoda) {
    Write-Host '      o tunel ja esta rodando'
} else {
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $inicio + '"') -WindowStyle Hidden
    Write-Host '      tunel iniciado'
}

# -------------------------------------------------------------- confere
Write-Host ''
Write-Host '  Conferindo...' -ForegroundColor Cyan
$ok = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri ('https://' + $endereco + '/_stcore/health') -TimeoutSec 8 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
Write-Host ''
if ($ok) {
    Write-Host ('  PRONTO: https://' + $endereco) -ForegroundColor Green
    Write-Host '  Abre no celular, em qualquer lugar, com a senha que voce definiu.'
} else {
    Write-Host ('  O tunel subiu, mas https://' + $endereco + ' ainda nao respondeu.') -ForegroundColor Yellow
    Write-Host '  Costuma ser propagacao de DNS (ate uns minutos). Veja tunnel.log e tente de novo.'
}
Write-Host ''
Read-Host '  Enter para fechar'
