# 代理节点检查与切换（Clash Verge / mihomo）：夜跑时代理节点连不上 chatgpt.com、github.com，出题、验收、推送都会卡住。
# 经 Clash Verge 的命名管道控制接口读取代理组，逐个测节点到这两个站点的延迟，切到两边都通且最快的节点。
# 不改订阅、规则和其他设置，不输出密钥。
# 用法（Windows）：
#   pwsh -File scripts\proxy-switch.ps1            只检查：当前节点能否访问两个站点
#   pwsh -File scripts\proxy-switch.ps1 -Switch    当前节点不通时，切到两边都通且延迟最低的节点
#   -Group <代理组名>  指定要切换的代理组；不指定时取第一个手动选择的代理组（GLOBAL 除外）
param([switch]$Switch, [string]$Group = '', [int]$TimeoutMs = 5000)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Targets = @('https://chatgpt.com', 'https://github.com')

$dir = Join-Path $env:APPDATA 'io.github.clash-verge-rev.clash-verge-rev'
$yaml = Get-Content (Join-Path $dir 'clash-verge.yaml') -Raw -Encoding UTF8
$pipe = ([regex]::Match($yaml, 'external-controller-pipe:\s*(\S+)')).Groups[1].Value
$secret = ([regex]::Match($yaml, '(?m)^secret:\s*(.*)$')).Groups[1].Value.Trim().Trim("'").Trim('"')
$pipeName = $pipe -replace '^\\\\\.\\pipe\\', ''
# 服务模式下实际管道名是 verge-mihomo-production-…，与配置里的 sidecar 名不同：优先用当前存在的 verge-mihomo 管道
$live = @(Get-ChildItem '\\.\pipe\' | Where-Object { $_.Name -like 'verge-mihomo-*' } | ForEach-Object Name)
if ($live.Count -gt 0 -and $live -notcontains $pipeName) { $pipeName = $live[0] }
if (-not $pipeName) { throw '没有找到 Clash Verge 的控制管道（verge-mihomo-*）' }

# 经命名管道发一个 HTTP 请求，返回响应正文（JSON 时解析成对象）
function Invoke-Mihomo([string]$Method, [string]$Path, [string]$Body = '') {
    $c = New-Object System.IO.Pipes.NamedPipeClientStream('.', $pipeName, [System.IO.Pipes.PipeDirection]::InOut)
    $c.Connect(3000)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
    $head = "$Method $Path HTTP/1.1`r`nHost: localhost`r`nAuthorization: Bearer $secret`r`nConnection: close`r`n"
    if ($Body) { $head += "Content-Type: application/json`r`nContent-Length: $($bytes.Length)`r`n" }
    $h = [Text.Encoding]::ASCII.GetBytes($head + "`r`n")
    $c.Write($h, 0, $h.Length)
    if ($Body) { $c.Write($bytes, 0, $bytes.Length) }
    $c.Flush()
    $ms = New-Object System.IO.MemoryStream
    $buf = New-Object byte[] 65536
    while (($n = $c.Read($buf, 0, $buf.Length)) -gt 0) { $ms.Write($buf, 0, $n) }
    $c.Close()
    $raw = $ms.ToArray()
    # 头部是 ASCII，按字节找到空行；正文按字节处理（节点名含中文，分块长度是字节数）
    $hdrEnd = -1
    for ($k = 0; $k -le $raw.Length - 4; $k++) {
        if ($raw[$k] -eq 13 -and $raw[$k + 1] -eq 10 -and $raw[$k + 2] -eq 13 -and $raw[$k + 3] -eq 10) { $hdrEnd = $k; break }
    }
    if ($hdrEnd -lt 0) { throw '控制接口返回的不是 HTTP 响应' }
    $head = [Text.Encoding]::ASCII.GetString($raw, 0, $hdrEnd)
    $status = [int]([regex]::Match($head, '^HTTP/\S+\s+(\d+)')).Groups[1].Value
    $pos = $hdrEnd + 4
    $out = New-Object System.IO.MemoryStream
    if ($head -match '(?im)^transfer-encoding:\s*chunked') {
        # 分块传输：逐块读出「十六进制长度行 + 数据 + CRLF」
        while ($pos -lt $raw.Length) {
            $e = $pos; while ($e -lt $raw.Length - 1 -and -not ($raw[$e] -eq 13 -and $raw[$e + 1] -eq 10)) { $e++ }
            $lenText = ([Text.Encoding]::ASCII.GetString($raw, $pos, $e - $pos) -split ';')[0].Trim()
            if (-not $lenText) { break }
            $len = [Convert]::ToInt32($lenText, 16); if ($len -eq 0) { break }
            $out.Write($raw, $e + 2, [Math]::Min($len, $raw.Length - $e - 2))
            $pos = $e + 2 + $len + 2
        }
    } else {
        $out.Write($raw, $pos, $raw.Length - $pos)
    }
    $body = [Text.Encoding]::UTF8.GetString($out.ToArray())
    $obj = $null
    try { $obj = $body | ConvertFrom-Json } catch { }
    return @{ status = $status; body = $obj; text = $body }
}

# 节点到某个站点的延迟（毫秒），不通返回 $null
function Get-Delay([string]$Name, [string]$Url) {
    $p = '/proxies/' + [Uri]::EscapeDataString($Name) + '/delay?timeout=' + $TimeoutMs + '&url=' + [Uri]::EscapeDataString($Url)
    $r = Invoke-Mihomo 'GET' $p
    if ($r.status -eq 200 -and $r.body.delay) { return [int]$r.body.delay }
    return $null
}

$proxies = (Invoke-Mihomo 'GET' '/proxies').body.proxies
$groups = $proxies.PSObject.Properties | Where-Object { $_.Value.type -eq 'Selector' -and $_.Name -ne 'GLOBAL' }
$g = if ($Group) { $groups | Where-Object { $_.Name -eq $Group } } else { $groups | Select-Object -First 1 }
if (-not $g) { throw "没有找到代理组 $Group" }
$gname = $g.Name
$now = $g.Value.now
Write-Output "代理组 $gname 当前节点 $now"

$ok = $true
foreach ($u in $Targets) {
    $d = Get-Delay $now $u
    # 单次测速偶发超时，当前节点不通时再测一次，避免误切
    if (-not $d) { $d = Get-Delay $now $u }
    Write-Output ("  {0} {1}" -f $u, $(if ($d) { "$d ms" } else { '不通' }))
    if (-not $d) { $ok = $false }
}
if ($ok) { Write-Output '当前节点正常，不切换'; exit 0 }
if (-not $Switch) { Write-Output '当前节点不通（加 -Switch 自动切换）'; exit 2 }

# 逐个测组内的节点（跳过子代理组和直连、拒绝），两个站点都通的按延迟之和排序
$best = $null
foreach ($name in $g.Value.all) {
    $p = $proxies.$name
    if (-not $p -or $p.type -in @('Selector', 'URLTest', 'Fallback', 'LoadBalance', 'Direct', 'Reject', 'Compatible', 'Pass')) { continue }
    $sum = 0; $good = $true
    foreach ($u in $Targets) {
        $d = Get-Delay $name $u
        if (-not $d) { $good = $false; break }
        $sum += $d
    }
    if ($good -and (-not $best -or $sum -lt $best.sum)) { $best = @{ name = $name; sum = $sum } }
}
if (-not $best) { Write-Output '组内没有两个站点都能访问的节点，未切换'; exit 3 }
$body = @{ name = $best.name } | ConvertTo-Json -Compress
$r = Invoke-Mihomo 'PUT' ('/proxies/' + [Uri]::EscapeDataString($gname)) $body
if ($r.status -ne 204 -and $r.status -ne 200) { throw "切换失败：HTTP $($r.status) $($r.text)" }
Write-Output "已切换到 $($best.name)（两个站点延迟合计 $($best.sum) ms）"
