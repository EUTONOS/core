# Crosstalk rename step (slice 3B.10): rename ONE Claude Desktop chat through Desktop's own sidebar Rename control.
#
# Why: a chat's Remote Control name on the phone (claude.ai) only changes when the name is changed through Desktop's
# Rename box with text that differs from the current title. This script does exactly one such rename:
#   1. sidebar button "More options for <current>"  -> ExpandCollapsePattern.Expand()
#   2. menu item "Rename"                            -> InvokePattern.Invoke()
#   3. Edit "Session name" or inline "Rename" (holds the current title) -> SetFocus, Ctrl+A, type the new text, Enter
# ValuePattern.SetValue is ignored by the app (a React input), so the text is typed.
#
# Guards, each checked immediately before Ctrl+A, before typing and before Enter:
#   a. the foreground window is Desktop's main window (a claude.exe process whose MainWindowHandle is in front);
#   b. AutomationElement.FocusedElement is the Edit named "Session name" or "Rename" of that process;
#   c. before typing the edit still holds the current title; before Enter it holds exactly the new text.
# A failed guard sends {ESCAPE} only when the focus is still in that edit (cancelling the rename box), else nothing.
# It never reads, writes or uses the clipboard and never types anywhere except the verified rename edit.
# Text is typed with SendKeys (specials escaped); text with non-ASCII characters is typed as Unicode key events
# (SendInput) because SendKeys cannot type them. Titles are passed base64 (UTF-8) so no quoting can change them.
#
# Prints ONE JSON line: {tag, outcome, step, reason}. Outcomes: renamed | guard-failed | not-found | not-unique |
# no-change | error. Never prints a title. Exit code 0 only for "renamed".
param(
    [Parameter(Mandatory)][string]$CurrentB64,
    [Parameter(Mandatory)][string]$NewB64
)
$ErrorActionPreference = 'Stop'
$script:Step = 'start'

function Write-Result([string]$Outcome, [string]$Reason = '') {
    $row = [ordered]@{ tag = 'crosstalk.rename.ps.v1'; outcome = $Outcome; step = $script:Step; reason = $Reason }
    [Console]::Out.WriteLine(($row | ConvertTo-Json -Compress))
}

try {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Ct {
    public static class Native {
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [StructLayout(LayoutKind.Explicit, Size = 40)]
        public struct INPUT {
            [FieldOffset(0)] public uint type;
            [FieldOffset(8)] public ushort wVk;
            [FieldOffset(10)] public ushort wScan;
            [FieldOffset(12)] public uint dwFlags;
            [FieldOffset(16)] public uint time;
            [FieldOffset(24)] public IntPtr dwExtraInfo;
        }
        [DllImport("user32.dll", SetLastError = true)]
        static extern uint SendInput(uint n, INPUT[] inputs, int size);
        // Types the text as Unicode key events (down + up per UTF-16 unit). Only used for non-ASCII text.
        public static uint TypeUnicode(string text) {
            if (IntPtr.Size != 8) return 0;
            INPUT[] inputs = new INPUT[text.Length * 2];
            for (int i = 0; i < text.Length; i++) {
                inputs[2 * i].type = 1;     inputs[2 * i].wScan = text[i];     inputs[2 * i].dwFlags = 0x0004;
                inputs[2 * i + 1].type = 1; inputs[2 * i + 1].wScan = text[i]; inputs[2 * i + 1].dwFlags = 0x0006;
            }
            return SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        }
    }
}
'@

    $A = [System.Windows.Automation.AutomationElement]
    $Scope = [System.Windows.Automation.TreeScope]
    $Current = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($CurrentB64))
    $New = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($NewB64))
    if ([string]::IsNullOrEmpty($Current) -or [string]::IsNullOrEmpty($New)) { Write-Result 'error' 'empty-title'; exit 1 }
    if ($Current -ceq $New) { Write-Result 'no-change' 'new-title-equals-current'; exit 1 }

    function ConvertTo-SendKeys([string]$Text) {
        # SendKeys specials: + ^ % ~ ( ) { } [ ] each go inside braces.
        $sb = New-Object System.Text.StringBuilder
        foreach ($c in $Text.ToCharArray()) {
            if ('+^%~(){}[]'.IndexOf($c) -ge 0) { [void]$sb.Append('{').Append($c).Append('}') } else { [void]$sb.Append($c) }
        }
        $sb.ToString()
    }
    function Send-Text([string]$Text) {
        if ($Text -match '[^\x20-\x7E]') {
            if ([Ct.Native]::TypeUnicode($Text) -ne ($Text.Length * 2)) { throw 'unicode-send-failed' }
        } else {
            [System.Windows.Forms.SendKeys]::SendWait((ConvertTo-SendKeys $Text))
        }
    }
    function Edit-Value($Element) {
        try { $Element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch { $null }
    }
    # One guard evaluation. InEdit is true only when Desktop is in front AND the keyboard focus is the rename
    # edit of Desktop's process: the only state in which an {ESCAPE} may be sent.
    function Test-Guard($Want) {
        $out = @{ Ok = $false; Reason = ''; InEdit = $false }
        $fg = [Ct.Native]::GetForegroundWindow()
        if ($fg.ToInt64() -ne $script:Hwnd.ToInt64()) { $out.Reason = 'desktop-not-foreground'; return $out }
        try { $f = $A::FocusedElement } catch { $out.Reason = 'focus-unreadable'; return $out }
        if ($null -eq $f -or $f.Current.ProcessId -ne $script:DesktopPid -or
            $f.Current.ControlType -ne [System.Windows.Automation.ControlType]::Edit -or
            ($f.Current.Name -ne 'Session name' -and $f.Current.Name -ne 'Rename')) {
            $out.Reason = 'focus-not-in-session-name-edit'; return $out
        }
        $out.InEdit = $true
        if ($null -ne $Want) {
            $value = Edit-Value $f
            if ($null -eq $value -or $value -cne $Want) { $out.Reason = 'edit-value-mismatch'; return $out }
        }
        $out.Ok = $true
        $out
    }
    function Assert-Guard([string]$Step, $Want) {
        $script:Step = $Step
        $g = Test-Guard $Want
        if (-not $g.Ok) {
            if ($g.InEdit) { [System.Windows.Forms.SendKeys]::SendWait('{ESCAPE}') }
            Write-Result 'guard-failed' $g.Reason
            exit 1
        }
    }

    # ---- the Desktop main window: a claude.exe process whose main window is the foreground window right now
    $script:Step = 'window'
    $fg = [Ct.Native]::GetForegroundWindow()
    $proc = Get-Process claude -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle.ToInt64() -ne 0 -and $_.MainWindowHandle.ToInt64() -eq $fg.ToInt64() } |
        Select-Object -First 1
    if (-not $proc) { Write-Result 'guard-failed' 'desktop-not-foreground'; exit 1 }
    $script:Hwnd = $proc.MainWindowHandle
    $script:DesktopPid = $proc.Id
    $win = $A::FromHandle($script:Hwnd)

    # Desktop (Chromium) builds its accessibility tree lazily: the first query after a while can see ~15 elements and
    # none of the sidebar (live 2026-10-21: 15, then 380 a moment later). So every lookup polls, and a full walk wakes
    # the tree before each try. Returns ALL matches; callers decide what more than one means.
    function Find-All($Condition, [int]$Tries, [int]$WaitMs) {
        for ($i = 0; $i -lt $Tries; $i++) {
            if ($i -gt 0) { Start-Sleep -Milliseconds $WaitMs }
            [void]$win.FindAll($Scope::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
            $found = @($win.FindAll($Scope::Descendants, $Condition))
            if ($found.Count) { return ,$found }
        }
        return ,@()
    }
    function Named-Type($Type, $Name) {
        New-Object System.Windows.Automation.AndCondition(
            (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $Type)),
            (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, $Name)))
    }

    # ---- 1. sidebar "More options for <current>" -> Expand. Two chats with the same title are refused: the button
    #         cannot tell them apart, and renaming the wrong one is worse than not renaming.
    $script:Step = 'more-options'
    $mores = Find-All (Named-Type ([System.Windows.Automation.ControlType]::Button) ("More options for " + $Current)) 5 800
    if ($mores.Count -eq 0) { Write-Result 'not-found' 'more-options-button'; exit 1 }
    if ($mores.Count -gt 1) { Write-Result 'not-unique' 'more-options-button'; exit 1 }
    $more = $mores[0]
    $more.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()

    # ---- 2. menu item "Rename" -> Invoke
    $script:Step = 'rename-item'
    $items = Find-All (Named-Type ([System.Windows.Automation.ControlType]::MenuItem) 'Rename') 8 300
    $item = if ($items.Count -eq 1) { $items[0] } else { $null }
    if (-not $item) {
        try { $more.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Collapse() } catch {}
        Write-Result 'not-found' 'rename-menu-item'
        exit 1
    }
    $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()

    # ---- 3. the rename Edit (wait for it)
    $script:Step = 'edit'
    # Desktop renamed the box: the older dialog's Edit is "Session name"; since about 2026-10 the sidebar row turns
    # into an inline Edit named "Rename" (live 2026-10-21). Either is accepted, and only the one holding the current
    # title is used.
    Start-Sleep -Milliseconds 250
    $edits = Find-All (New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)),
        (New-Object System.Windows.Automation.OrCondition(
            (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Session name')),
            (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Rename')))))) 20 250
    $edits = @($edits | Where-Object { (Edit-Value $_) -ceq $Current })
    if ($edits.Count -eq 0) { Write-Result 'not-found' 'session-name-edit'; exit 1 }
    if ($edits.Count -gt 1) { Write-Result 'not-unique' 'session-name-edit'; exit 1 }
    $edit = $edits[0]
    $edit.SetFocus()
    Start-Sleep -Milliseconds 200

    # ---- 4. guarded keystrokes
    Assert-Guard 'before-select-all' $Current
    [System.Windows.Forms.SendKeys]::SendWait('^a')
    Assert-Guard 'before-typing' $null
    Send-Text $New
    Start-Sleep -Milliseconds 250
    Assert-Guard 'before-enter' $New
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Start-Sleep -Milliseconds 400
    $script:Step = 'done'
    Write-Result 'renamed'
    exit 0
} catch {
    Write-Result 'error' ($_.Exception.GetType().Name)
    exit 1
}
