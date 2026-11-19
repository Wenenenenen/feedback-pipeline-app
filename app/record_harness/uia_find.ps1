# 本机桌面录屏：用 UI 自动化在窗口里找控件，输出屏幕矩形（JSON）。
# 用法：
#   powershell -NoProfile -File uia_find.ps1 -Hwnd <窗口句柄> -Name <文件名>
#     在文件对话框或资源管理器里按名称找文件列表项，输出它「名称」一格的屏幕矩形；找不到时输出 {"found": false}。
#   powershell -NoProfile -File uia_find.ps1 -Hwnd <窗口句柄> -Names "a.png|b.json"
#     一次找多个文件，输出 {"items": [{name, found, index, cell, row}]}，index 是在列表里的顺序（判断是否相邻、能否 Shift 连选）。
#   powershell -NoProfile -File uia_find.ps1 -Kind menu -ProcId <进程号> -Name "打开方式|Open with"
#     在该进程的顶层窗口（右键菜单、子菜单）里找名称与其中任一项相同的菜单项，输出 {found, rect}。
#   powershell -NoProfile -File uia_find.ps1 -Kind dl -Hwnd <Chrome 窗口句柄> -Name <下载的文件名>
#     找 Chrome 下载气泡里这个文件的一行、它的「在文件夹中显示」按钮和工具栏的下载按钮，输出 {item, folder, toolbar}。
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

if ($Kind -eq 'dl') {
    # Chrome 下载气泡：-Hwnd 是 Chrome 主窗口，-Name 是下载的文件名。输出
    #   item    气泡里这个文件那一行的矩形（气泡没打开或没有这一行时为空）
    #   folder  这一行的「在文件夹中显示」按钮（指针悬停在这一行上时才出现）
    #   toolbar 工具栏上的下载按钮（气泡关了时点它重新打开）
    $root = $AE::FromHandle([IntPtr]$Hwnd)
    $btn = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::Button)
    $out = @{ item = $null; folder = $null; toolbar = $null }
    $winCond = New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'RootView')
    foreach ($bw in $root.FindAll($Scope::Descendants, $winCond)) {
        $bn = $bw.Current.Name
        if ($bn -notmatch '下载|[Dd]ownload') { continue }
        foreach ($b in $bw.FindAll($Scope::Descendants, $btn)) {
            $k = $b.Current
            if ($k.BoundingRectangle.IsEmpty -or $k.IsOffscreen) { continue }
            # 下载记录里可能有以前同名的下载：取最上面（最新）的一行；文件夹按钮只在悬停的那一行上出现
            if ($k.ClassName -eq 'DownloadBubbleTransparentButton' -and $k.Name.StartsWith("$Name ")) { if (-not $out.item) { $out.item = Rect-Of $b } }
            elseif (-not $out.folder -and ($k.Name -eq "在文件夹中显示 $Name" -or $k.Name -eq "Show in folder $Name")) { $out.folder = Rect-Of $b }
        }
    }
    $tbCond = New-Object System.Windows.Automation.OrCondition(
        (New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'PinnedActionToolbarButton')),
        (New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'DownloadToolbarButtonView')))
    foreach ($b in $root.FindAll($Scope::Descendants, $tbCond)) {
        if ($b.Current.Name -match '下载|[Dd]ownload' -and -not $b.Current.BoundingRectangle.IsEmpty) { $out.toolbar = Rect-Of $b; break }
    }
    $out | ConvertTo-Json -Compress
    exit 0
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
