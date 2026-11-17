# 本机桌面录屏：在窗口（如 Windows 文件对话框）里按名称找文件列表项，输出它「名称」一格的屏幕矩形（JSON）。
# 用法：powershell -NoProfile -File uia_find.ps1 -Hwnd <窗口句柄> -Name <文件名>
# 找不到时输出 {"found": false}；资源管理器隐藏了已知扩展名时，按去掉扩展名的名称再找一次。
param([long]$Hwnd, [string]$Name)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# 按物理像素取坐标（与 SendInput 一致）
Add-Type -Namespace FbUia -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[FbUia.Dpi]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]

function Find-Item($root, $text) {
    $cond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)),
        (New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $text)))
    return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
}

$root = $AE::FromHandle([IntPtr]$Hwnd)
$item = $null
# 对话框刚弹出时列表可能还没填好，最多等 3 秒
for ($i = 0; $i -lt 15 -and -not $item; $i++) {
    $item = Find-Item $root $Name
    if (-not $item) { $item = Find-Item $root ([System.IO.Path]::GetFileNameWithoutExtension($Name)) }
    if (-not $item) { Start-Sleep -Milliseconds 200 }
}
if (-not $item) { '{"found": false}'; exit 0 }

# 详细信息视图里列表项横跨整行，取「名称」那一格（第一个子元素）；没有子元素时用整项
$cell = $item.FindFirst([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
$r = if ($cell -and -not $cell.Current.BoundingRectangle.IsEmpty) { $cell.Current.BoundingRectangle } else { $item.Current.BoundingRectangle }
$row = $item.Current.BoundingRectangle
@{ found = $true; cell = @($r.Left, $r.Top, $r.Right, $r.Bottom); row = @($row.Left, $row.Top, $row.Right, $row.Bottom) } |
    ConvertTo-Json -Compress
