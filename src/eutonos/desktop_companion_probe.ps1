param([switch]$Once, [int]$ParentProcessId = 0)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class EutonosWindowObserver {
public delegate bool EnumWindowProc(IntPtr hwnd, IntPtr data);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowProc cb, IntPtr data);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
[DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint relation);
[StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
}
"@
$nativeContract = @{
    GetWindow = [IntPtr]; GetWindowRect = [bool]; GetForegroundWindow = [IntPtr]
    GetWindowThreadProcessId = [uint32]; GetWindowText = [int]; IsWindowVisible = [bool]
}
foreach ($nativeName in $nativeContract.Keys) {
    $method = [EutonosWindowObserver].GetMethod($nativeName)
    if ($null -eq $method -or $method.ReturnType -ne $nativeContract[$nativeName]) {
        throw 'Invalid native observer contract'
    }
}
if ($null -eq [EutonosWindowObserver].GetNestedType('Rect')) { throw 'Missing native rectangle contract' }
$parent = $null
if (-not $Once -and $ParentProcessId -gt 0) {
    try {
        $parent = [System.Diagnostics.Process]::GetProcessById($ParentProcessId)
        # Opening the handle pins this process instance even if its PID is
        # reused after exit. StartTime records the identity we accepted.
        $null = $parent.Handle
        $parentStart = $parent.StartTime.ToUniversalTime().Ticks
        if ($parent.HasExited) { exit 0 }
    } catch {
        exit 0
    }
}
do {
    if ($parent) {
        try {
            if ($parent.HasExited -or $parent.StartTime.ToUniversalTime().Ticks -ne $parentStart) { break }
        } catch {
            break
        }
    }
    try {
        $targets = @{}
        Get-Process -Name ChatGPT,Claude,Hermes -ErrorAction SilentlyContinue | ForEach-Object {
            $targets[[uint32]$_.Id] = if ($_.ProcessName -ieq 'Claude') { 'claude' } elseif ($_.ProcessName -ieq 'Hermes') { 'hermes' } else { 'codex' }
        }
        $observed = New-Object 'System.Collections.Generic.List[object]'
        $callback = [EutonosWindowObserver+EnumWindowProc]{
            param([IntPtr]$hwnd, [IntPtr]$data)
            if ($observed.Count -ge 64) { return $false }
            [uint32]$ownerPid = 0
            [void][EutonosWindowObserver]::GetWindowThreadProcessId($hwnd, [ref]$ownerPid)
            if ($targets.ContainsKey($ownerPid) -and [EutonosWindowObserver]::IsWindowVisible($hwnd) -and [EutonosWindowObserver]::GetWindow($hwnd, 4) -eq [IntPtr]::Zero) {
                $text = New-Object System.Text.StringBuilder 512
                [void][EutonosWindowObserver]::GetWindowText($hwnd, $text, 512)
                $bounds = New-Object EutonosWindowObserver+Rect
                [void][EutonosWindowObserver]::GetWindowRect($hwnd, [ref]$bounds)
                if (-not $text.Length -or ($bounds.Right - $bounds.Left) -lt 200 -or ($bounds.Bottom - $bounds.Top) -lt 100) { return $true }
                $observed.Add([pscustomobject]@{handle=$hwnd.ToInt64(); title=$text.ToString(); harness=$targets[$ownerPid]; pid=$ownerPid})
            }
            return $true
        }
        [void][EutonosWindowObserver]::EnumWindows($callback, [IntPtr]::Zero)
        [pscustomobject]@{ windows=@($observed.ToArray()); foreground=[EutonosWindowObserver]::GetForegroundWindow().ToInt64() } | ConvertTo-Json -Compress -Depth 4
    } catch {
        [pscustomobject]@{ windows=@(); foreground=0; error='window-observation-unavailable' } | ConvertTo-Json -Compress
    }
    if (-not $Once) { Start-Sleep -Milliseconds 500 }
} while (-not $Once)
if ($parent) { $parent.Dispose() }
