# 本机桌面录屏：用 UI 自动化在窗口里找控件，输出屏幕矩形（JSON）。
# 用法：
#   powershell -NoProfile -File uia_find.ps1 -Hwnd <窗口句柄> -Name <文件名>
#     在文件对话框或资源管理器里按名称找文件列表项，输出它「名称」一格的屏幕矩形；找不到时输出 {"found": false}。
#   powershell -NoProfile -File uia_find.ps1 -Hwnd <窗口句柄> -Names "a.png|b.json"
#     一次找多个文件，输出 {"items": [{name, found, index, cell, row}]}，index 是在列表里的顺序（判断是否相邻、能否 Shift 连选）。
#   powershell -NoProfile -File uia_find.ps1 -Kind menu -ProcId <进程号> -Name "打开方式|Open with"
#     在该进程的顶层窗口（右键菜单、子菜单）里找名称以其中任一项开头的菜单项，输出 {found, rect}。
# 资源管理器隐藏了已知扩展名时，按去掉扩展名的名称再找一次。
param([long]$Hwnd = 0, [string]$Name = '', [string]$Names = '', [string]$Kind = 'list', [int]$ProcId = 0)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# 按物理像素取坐标（与 SendInput 一致）
Add-Type -Namespace FbUia -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[FbUia.Dpi]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$Scope = [System.Windows.Automation.TreeScope]

function Rect-Of($el) {
    $r = $el.Current.BoundingRectangle
    return @($r.Left, $r.Top, $r.Right, $r.Bottom)
}

# 详细信息视图里列表项横跨整行，取「名称」那一格（第一个子元素）；没有子元素时用整项
function Cell-Of($item) {
    $cell = $item.FindFirst($Scope::Children, [System.Windows.Automation.Condition]::TrueCondition)
    if ($cell -and -not $cell.Current.BoundingRectangle.IsEmpty) { return Rect-Of $cell }
    return Rect-Of $item
}

function List-Items($root) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::ListItem)
    return @($root.FindAll($Scope::Descendants, $cond))
}

function Index-Of($items, $text) {
    $bare = [System.IO.Path]::GetFileNameWithoutExtension($text)
    for ($i = 0; $i -lt $items.Count; $i++) { if ($items[$i].Current.Name -eq $text) { return $i } }
    for ($i = 0; $i -lt $items.Count; $i++) { if ($items[$i].Current.Name -eq $bare) { return $i } }
    return -1
}

if ($Kind -eq 'menu') {
    # 右键菜单与子菜单是该进程新弹出的顶层窗口：逐个窗口找菜单项，最多等 3 秒
    $alts = $Name.Split('|') | Where-Object { $_ }
    $mcond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::MenuItem)
    $pcond = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $ProcId)
    for ($t = 0; $t -lt 15; $t++) {
        foreach ($w in $AE::RootElement.FindAll($Scope::Children, $pcond)) {
            foreach ($m in $w.FindAll($Scope::Descendants, $mcond)) {
                $n = $m.Current.Name
                foreach ($a in $alts) {
                    if ($n -and $n.StartsWith($a) -and -not $m.Current.BoundingRectangle.IsEmpty) {
                        @{ found = $true; name = $n; rect = (Rect-Of $m) } | ConvertTo-Json -Compress
                        exit 0
                    }
                }
            }
        }
        Start-Sleep -Milliseconds 200
    }
    '{"found": false}'
    exit 0
}

$root = $AE::FromHandle([IntPtr]$Hwnd)
$want = if ($Names) { $Names.Split('|') | Where-Object { $_ } } else { @($Name) }
# 对话框刚弹出时列表可能还没填好，最多等 3 秒，直到要找的文件都出现
$items = @()
for ($i = 0; $i -lt 15; $i++) {
    $items = List-Items $root
    $missing = @($want | Where-Object { (Index-Of $items $_) -lt 0 })
    if ($missing.Count -eq 0) { break }
    Start-Sleep -Milliseconds 200
}

if (-not $Names) {
    $k = Index-Of $items $Name
    if ($k -lt 0) { '{"found": false}'; exit 0 }
    @{ found = $true; index = $k; cell = (Cell-Of $items[$k]); row = (Rect-Of $items[$k]) } | ConvertTo-Json -Compress
    exit 0
}

$out = @()
foreach ($n in $want) {
    $k = Index-Of $items $n
    if ($k -lt 0) { $out += @{ name = $n; found = $false } }
    else { $out += @{ name = $n; found = $true; index = $k; cell = (Cell-Of $items[$k]); row = (Rect-Of $items[$k]) } }
}
@{ items = $out } | ConvertTo-Json -Compress -Depth 4
