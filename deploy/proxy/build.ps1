<#
.SYNOPSIS
  tesla-http-proxy を deploy/proxy/VERSION の版でビルドする（docs/02_deploy.md 第2章）。

.DESCRIPTION
  ソースはリポジトリ直下の vehicle-command/ に置く（.gitignore で除外済み）。
  無ければ VERSION の REPO から clone し、COMMIT へ切り替えてからビルドする。
  出力は vehicle-command/cmd/tesla-http-proxy/tesla-http-proxy-arm64。
  ラズパイへは tesla-http-proxy という名前で転送する。

  ビルドの前に次を確認し、1つでも違えば中止する。
    - 手元の Go の版が VERSION の GO_VERSION と同じ
    - vehicle-command/ の origin が VERSION の REPO と同じ
    - タグ TAG が指すコミットが COMMIT と同じ
    - 追跡対象のファイルに変更が無い
  最後に SHA-256 を表示し、VERSION の SHA256 と一致するかを示す。
  一致しなくてもビルドは失敗扱いにしない。Go の出力はビルドしたディレクトリの
  パスと未追跡ファイルの有無（vcs.modified）でも変わるため、別の場所や状態で
  ビルドすると値が変わる。

.PARAMETER AllowGoMismatch
  Go の版が VERSION と違ってもビルドする。版を上げる作業のときだけ使う。
#>
param(
    [switch]$AllowGoMismatch
)

$ErrorActionPreference = 'Stop'

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $out = & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe $($Arguments -join ' ') が終了コード $LASTEXITCODE で失敗した"
    }
    return $out
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$cfg = @{}
Get-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'VERSION') | ForEach-Object {
    if ($_ -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
}
foreach ($key in 'REPO', 'TAG', 'COMMIT', 'GO_VERSION', 'GOOS', 'GOARCH') {
    if (-not $cfg[$key]) { throw "VERSION に $key が無い" }
}

# 1. Go の版
$goVersion = (Invoke-Native 'go' @('env', 'GOVERSION')) | Select-Object -First 1
if ($goVersion -ne $cfg.GO_VERSION) {
    $msg = "Go の版が違う（手元 $goVersion、VERSION $($cfg.GO_VERSION)）"
    if (-not $AllowGoMismatch) { throw "$msg。版を上げる作業なら -AllowGoMismatch を付ける" }
    Write-Warning $msg
}

# 2. ソースの取得と切り替え
$src = Join-Path $repoRoot 'vehicle-command'
if (-not (Test-Path (Join-Path $src '.git'))) {
    Invoke-Native 'git' @('clone', $cfg.REPO, $src) | Out-Null
}
$origin = (Invoke-Native 'git' @('-C', $src, 'remote', 'get-url', 'origin')) | Select-Object -First 1
$originNormalized = ($origin.TrimEnd('/') -replace '\.git$', '')
if ($originNormalized -ne $cfg.REPO.TrimEnd('/')) {
    throw "vehicle-command/ の origin が VERSION の REPO と違う（$origin）"
}
Invoke-Native 'git' @('-C', $src, 'fetch', '--tags', 'origin') | Out-Null

$tagCommit = (Invoke-Native 'git' @('-C', $src, 'rev-list', '-n', '1', $cfg.TAG)) | Select-Object -First 1
if ($tagCommit -ne $cfg.COMMIT) {
    throw "タグ $($cfg.TAG) が指すコミット $tagCommit が VERSION の COMMIT と違う"
}
$changes = Invoke-Native 'git' @('-C', $src, 'status', '--porcelain', '--untracked-files=no')
if ($changes) { throw "vehicle-command/ の追跡対象ファイルに変更がある：`n$($changes -join "`n")" }
Invoke-Native 'git' @('-C', $src, 'checkout', '--quiet', '--detach', $cfg.COMMIT) | Out-Null
$head = (Invoke-Native 'git' @('-C', $src, 'rev-parse', 'HEAD')) | Select-Object -First 1
if ($head -ne $cfg.COMMIT) { throw "HEAD が $head で、COMMIT と違う" }

# 3. ビルド（環境変数はこのスクリプトの中だけで変え、終わったら戻す）
$cmdDir = Join-Path $src 'cmd\tesla-http-proxy'
$out = Join-Path $cmdDir "tesla-http-proxy-$($cfg.GOARCH)"
$saved = @{ GOOS = $env:GOOS; GOARCH = $env:GOARCH; CGO_ENABLED = $env:CGO_ENABLED }
Push-Location $cmdDir
try {
    $env:GOOS = $cfg.GOOS
    $env:GOARCH = $cfg.GOARCH
    $env:CGO_ENABLED = '0'
    Invoke-Native 'go' @('build', '-o', $out) | Out-Null
}
finally {
    Pop-Location
    foreach ($name in $saved.Keys) {
        if ($null -eq $saved[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $saved[$name] }
    }
}

# 4. 結果
$hash = (Get-FileHash -Algorithm SHA256 $out).Hash.ToLowerInvariant()
Write-Output "出力    : $out"
Write-Output "版      : $($cfg.TAG) ($($cfg.COMMIT)) / $goVersion / $($cfg.GOOS)/$($cfg.GOARCH)"
Write-Output "SHA-256 : $hash"
if ($cfg.SHA256) {
    if ($hash -eq $cfg.SHA256) { Write-Output 'VERSION の SHA256 と一致する（ラズパイで動いているものと同じバイナリ）' }
    else { Write-Output 'VERSION の SHA256 とは一致しない。配置したら DEPLOY_LOG に新しい値を記録し、VERSION の SHA256 を更新する' }
}
Write-Output 'ラズパイへは tesla-http-proxy という名前で転送する（docs/02_deploy.md 第2章）'
