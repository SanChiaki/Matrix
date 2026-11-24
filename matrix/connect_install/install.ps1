# matrix-connect 安装脚本（Windows）：下载自带 Python 的独立程序，不需要预先装 uv、git、Python。
# Matrix 服务器在 /connect/install.ps1 提供它，设备页给的命令是（在 PowerShell 里运行）：
#
#   $env:MATRIX_SERVER="https://<服务器>"; irm https://<服务器>/connect/install.ps1 | iex
#
# 做的事：下载 matrix-connect-windows-x64.zip（从这台服务器的 /connect/dist/，服务器上没放就由它转到
# GitHub Release），核对 sha256，解压到 %LOCALAPPDATA%\matrix-connect\app，把这个目录加进用户 PATH；
# 然后运行 `matrix-connect setup <服务器>`（配对 + 装成登录时运行的后台任务）。
# 已经配对过的电脑只升级程序并重装后台任务（`matrix-connect install`），不重新配对。
#
# 环境变量：MATRIX_CONNECT_HOME（安装目录）、MATRIX_CONNECT_DOWNLOAD（下载地址前缀，默认 <服务器>/connect/dist）、
# MATRIX_CONNECT_NO_SETUP=1（只装不配对）、MATRIX_CONNECT_NO_MODIFY_PATH=1（不改用户 PATH）。
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Windows PowerShell 5 的进度条会让下载慢很多

function Fail($msg) { Write-Host "matrix-connect 安装失败：$msg" -ForegroundColor Red; throw $msg }

$server = $env:MATRIX_SERVER
if (-not $server) { Fail '要给服务器地址：$env:MATRIX_SERVER="https://<服务器>"; irm https://<服务器>/connect/install.ps1 | iex' }
$server = $server.TrimEnd("/")

# 只有 x64 版：ARM 的 Windows 用自带的 x64 仿真跑它
$asset = "matrix-connect-windows-x64.zip"
$base = if ($env:MATRIX_CONNECT_DOWNLOAD) { $env:MATRIX_CONNECT_DOWNLOAD.TrimEnd("/") } else { "$server/connect/dist" }
$root = if ($env:MATRIX_CONNECT_HOME) { $env:MATRIX_CONNECT_HOME } else { Join-Path $env:LOCALAPPDATA "matrix-connect" }
$app = Join-Path $root "app"
$exe = Join-Path $app "matrix-connect.exe"

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("matrix-connect-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  Write-Host "下载 $asset …"
  $zip = Join-Path $tmp $asset
  try { Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile $zip } catch { Fail "下载 $base/$asset 失败：$_" }
  try {
    $want = ((Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset.sha256").Content | Out-String).Trim().Split(" ")[0]
  } catch { $want = $null }
  if ($want) {
    $got = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash
    if ($got -ne $want.ToUpper()) { Fail "$asset 的 sha256 对不上（下载不完整或被篡改）" }
  }

  $x = Join-Path $tmp "x"
  Expand-Archive -Path $zip -DestinationPath $x -Force
  $new = Join-Path $x "matrix-connect"
  if (-not (Test-Path (Join-Path $new "matrix-connect.exe"))) { Fail "安装包里没有 matrix-connect.exe" }

  # 正在跑的后台任务占着旧文件：先停掉（setup / install 会重新拉起）
  if (Get-Command schtasks.exe -ErrorAction SilentlyContinue) { schtasks.exe /End /TN MatrixConnect 2>$null | Out-Null }
  Get-Process -Name matrix-connect, matrix-connectw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($app, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force
  New-Item -ItemType Directory -Force -Path $root | Out-Null
  $old = Join-Path $root "app.old"
  if (Test-Path $old) { Remove-Item -Recurse -Force $old }
  if (Test-Path $app) { Move-Item $app $old }
  Move-Item $new $app
  if (Test-Path $old) { Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue }
  Write-Host "已安装：$exe"
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

if ($env:MATRIX_CONNECT_NO_MODIFY_PATH -ne "1") {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $parts = @($userPath -split ";" | Where-Object { $_ })
  if ($parts -notcontains $app) {
    [Environment]::SetEnvironmentVariable("Path", (@($app) + $parts) -join ";", "User")
    Write-Host "已把 $app 加进用户 PATH（新开的终端生效）"
  }
  if (($env:Path -split ";") -notcontains $app) { $env:Path = "$app;$env:Path" }
}

if ($env:MATRIX_CONNECT_NO_SETUP -eq "1") {
  Write-Host "下一步：matrix-connect setup $server"
} else {
  & $exe status *> $null
  if ($LASTEXITCODE -eq 0) {
    Write-Host "这台电脑已经配对过：重装后台任务（不重新配对）"
    & $exe install
  } else {
    & $exe setup $server
  }
}
