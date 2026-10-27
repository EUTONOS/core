# Archive ONE Codex Desktop chat through the app's own sidebar "Archive chat" button (owner method, 2026-10-20).
#
# Why: a Codex chat that Codex Desktop holds open refuses a private app-server `thread/archive`
# ("thread already has an active writer"), and a thread archived through a private app-server still shows in the
# Desktop sidebar until the app refreshes. Pressing the sidebar button archives through the writer itself and
# updates the sidebar at once. UI Automation's InvokePattern presses it: no focus, no mouse, no keyboard, no clipboard.
#
#   1. find the one sidebar ListItem whose name is exactly -Title (Chromium builds its accessibility tree lazily,
#      so the first query may see almost nothing; the search retries);
#   2. under it, the Button "Archive chat" -> InvokePattern.Invoke();
#   3. readback: the ListItem is gone from the sidebar.
# Refuses when no or several chats carry the title. A title is not identity: confirm the thread id is archived
# afterwards (app-server `thread/list` with `archived: true`). -DryRun only finds the button.
param([Parameter(Mandatory)][string]$Title, [switch]$DryRun)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
$proc = Get-Process ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { throw "Codex Desktop window not found" }
$root = $A::FromHandle($proc.MainWindowHandle)
$itemCond = New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)
function Find-Items { @($root.FindAll($scope::Descendants, $itemCond) | Where-Object { $_.Current.Name -eq $Title }) }
$items = @()
for ($i = 0; $i -lt 4 -and -not $items.Count; $i++) {
    $items = Find-Items
    if (-not $items.Count) { Start-Sleep -Milliseconds 800 }
}
if ($items.Count -ne 1) { throw "expected one Codex sidebar chat titled '$Title', found $($items.Count)" }
$btnCond = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, "Archive chat")
$btn = $items[0].FindFirst($scope::Descendants, $btnCond)
if (-not $btn) { throw "no 'Archive chat' button under '$Title'" }
if ($DryRun) { "found 'Archive chat' for '$Title'"; return }
$btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
Start-Sleep -Milliseconds 1500
$left = (Find-Items).Count
if ($left) { throw "pressed 'Archive chat' but '$Title' is still in the sidebar" }
"archived '$Title' (gone from the Codex sidebar)"
