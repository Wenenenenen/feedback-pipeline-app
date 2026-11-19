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
    # 经典右键菜单（#32768 窗口）在 UI 自动化里是没有子元素的空面板，改用 Win32 菜单接口：
    # 向菜单窗口发 MN_GETHMENU 取菜单句柄，逐项读文字（去掉 & 助记符）与屏幕矩形。按 Z 序先找最上层的菜单（子菜单）
    Add-Type -Namespace FbUia -Name Menu -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr after, string cls, string title);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
[DllImport("user32.dll")] public static extern int GetMenuItemCount(IntPtr m);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetMenuString(IntPtr m, uint id, System.Text.StringBuilder s, int max, uint flags);
[DllImport("user32.dll")] public static extern bool GetMenuItemRect(IntPtr h, IntPtr m, uint i, out RECT r);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
'@
    $alts = $Name.Split('|') | Where-Object { $_ }
    # 菜单文字去掉助记符与快捷键说明后再比较：「打开方式(&H)」→「打开方式」，「&Open with」→「Open with」；
    # 要完全相等，避免「Open with」匹配到「Open with Sublime Text」
    function Clean-Name([string]$s) {
        $s = ($s -split "`t")[0]
        $s = $s -replace '\(&.\)$', '' -replace '&', '' -replace '(\.\.\.|…)$', ''
        return $s.Trim()
    }
    function Find-Win32Menu {
        $w = [IntPtr]::Zero
        while (($w = [FbUia.Menu]::FindWindowEx([IntPtr]::Zero, $w, '#32768', [NullString]::Value)) -ne [IntPtr]::Zero) {
            if (-not [FbUia.Menu]::IsWindowVisible($w)) { continue }
            $hm = [FbUia.Menu]::SendMessage($w, 0x01E1, [IntPtr]::Zero, [IntPtr]::Zero)
            if ($hm -eq [IntPtr]::Zero) { continue }
            $cnt = [FbUia.Menu]::GetMenuItemCount($hm)
            for ($i = 0; $i -lt $cnt; $i++) {
                $sb = New-Object System.Text.StringBuilder 256
                [void][FbUia.Menu]::GetMenuString($hm, $i, $sb, 256, 0x400)
                $n = Clean-Name $sb.ToString()
                foreach ($a in $alts) {
                    if ($n -and $n -eq $a) {
                        $r = New-Object FbUia.Menu+RECT
                        if ([FbUia.Menu]::GetMenuItemRect([IntPtr]::Zero, $hm, $i, [ref]$r) -and $r.Right -gt $r.Left) {
                            return @{ found = $true; name = $n; rect = @($r.Left, $r.Top, $r.Right, $r.Bottom) }
                        }
                    }
                }
            }
        }
        return $null
    }
    # 新式右键菜单（Win11 默认）的菜单项在 UI 自动化里可见：在该进程的顶层窗口里找；两种都最多等 3 秒
    $mcond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::MenuItem)
    $pcond = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $ProcId)
    for ($t = 0; $t -lt 15; $t++) {
        $hit = Find-Win32Menu
        if ($hit) { $hit | ConvertTo-Json -Compress; exit 0 }
        foreach ($w in $AE::RootElement.FindAll($Scope::Children, $pcond)) {
            foreach ($m in $w.FindAll($Scope::Descendants, $mcond)) {
                $n = Clean-Name $m.Current.Name
                foreach ($a in $alts) {
                    if ($n -and $n -eq $a -and -not $m.Current.BoundingRectangle.IsEmpty) {
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
