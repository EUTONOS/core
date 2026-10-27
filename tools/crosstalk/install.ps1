<#
.SYNOPSIS
  Install or upgrade the tracked Crosstalk MVP kit. OWNER-RUN ONLY.

.DESCRIPTION
  1. Backs up ~/.codex/config.toml and ~/.claude/settings.json (SHA-256 recorded) under
     %USERPROFILE%\.eutonos\crosstalk-probe\install\.
  2. Adds ONE marked [mcp_servers.crosstalk_p0] block to ~/.codex/config.toml, with
     default_tools_approval_mode set from -ApprovalMode.
  3. Registers the local marketplace ./claude-plugin and installs the plugin crosstalk-p0 at USER scope
     with the claude CLI (declared in ~/.claude/settings.json).
  4. Creates or refreshes the "Crosstalk helper" Startup shortcut so the helper starts at Windows login. It points at
     -HelperPath (default: %USERPROFILE%\.eutonos\crosstalk-helper-src\tools\crosstalk\crosstalk_helper.py when that
     clean worktree exists, else the helper beside this script). It never starts the helper and restarts nothing.
  5. Shows the permission entries it would add to ~/.claude/settings.json. It writes that file ONLY with
     -ApplyPermissions (after a backup, adding only missing entries and recording them for uninstall).
  Messaging stays OFF until you run:  crosstalk_p0_cli.py enable

  Upgrades preserve the installed approval mode, controls, journal and original backups.
  An explicitly different -ApprovalMode refuses rather than changing permissions.

  This script touches only the files above and the claude plugin registry. It never opens a Codex or
  Claude credential file or store, and it prints no environment variable values.

.PARAMETER ApprovalMode
  Codex default_tools_approval_mode for the probe server: auto | prompt | writes | approve.
  Which value removes per-call prompts is one of the questions probe 1 answers (see PROBES.md).

.PARAMETER SkipCodex
  Do not touch ~/.codex/config.toml.

.PARAMETER SkipClaude
  Do not touch the Claude plugin registry or ~/.claude/settings.json.

.PARAMETER HelperPath
  The crosstalk_helper.py the Startup shortcut starts. Default: the clean helper worktree under
  %USERPROFILE%\.eutonos\crosstalk-helper-src when it exists, else the helper in this kit.

.PARAMETER SkipHelperShortcut
  Do not create or refresh the Startup shortcut.

.PARAMETER ApplyPermissions
  Add the missing permission entries to ~/.claude/settings.json (backup first; only missing entries; recorded so
  uninstall removes only those). Without it the entries are shown and nothing is written.

.EXAMPLE
  .\install.ps1 -ApprovalMode approve
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'prompt', 'writes', 'approve')]
    [string]$ApprovalMode = 'prompt',
    [switch]$SkipCodex,
    [switch]$SkipClaude,
    [string]$HelperPath,
    [switch]$SkipHelperShortcut,
    [switch]$ApplyPermissions
)

$ErrorActionPreference = 'Stop'
$Kit = $PSScriptRoot
$Runtime = Join-Path $env:USERPROFILE '.eutonos\runtime\Scripts\python.exe'
$Helper = Join-Path $Kit 'crosstalk_p0_install.py'
$Cli = Join-Path $Kit 'crosstalk_p0_cli.py'
$Marketplace = Join-Path $Kit 'claude-plugin'
$PluginId = 'crosstalk-p0@crosstalk-p0-local'

function Invoke-Helper {
    param([Parameter(Mandatory)][string[]]$Arguments, [int[]]$OkCodes = @(0))
    $text = (& $Runtime -I -B $Helper @Arguments) -join "`n"
    if ($OkCodes -notcontains $LASTEXITCODE) { throw "install helper failed ($($Arguments -join ' ')): $text" }
    return ($text | ConvertFrom-Json)
}

function Find-ClaudeCli {
    $found = Get-Command claude -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $fallback = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    return $null
}

function Install-HelperShortcutAndPermissions {
    # Both steps are idempotent. Neither starts, stops or restarts anything.
    if (-not $SkipHelperShortcut) {
        $shortcutArgs = @('helper-startup-install')
        if ($HelperPath) { $shortcutArgs += @('--helper-script', $HelperPath) }
        $shortcut = Invoke-Helper $shortcutArgs
        Write-Host ("Startup shortcut {0}: {1} -> {2} (the helper is not started; it runs from the next login)" -f $shortcut.action, $shortcut.shortcut, $shortcut.script)
    }
    if (-not $SkipClaude) {
        $permissionArgs = @('permissions')
        if ($ApplyPermissions) { $permissionArgs += '--apply-permissions' }
        $permissions = Invoke-Helper $permissionArgs
        if ($ApplyPermissions) {
            Write-Host ("Permissions: added {0} entr(ies) to settings.json (backup: {1})" -f @($permissions.added).Count, $permissions.backup)
        } else {
            Write-Host ("Permissions: would add {0} entr(ies): {1}. Re-run with -ApplyPermissions to write them." -f @($permissions.would_add).Count, (@($permissions.would_add) -join ', '))
        }
        if (@($permissions.missing).Count -gt 0) { Write-Warning ("Required permission(s) missing: {0}" -f (@($permissions.missing) -join ', ')) }
    }
}

if (-not (Test-Path -LiteralPath $Runtime)) { throw "EUTONOS runtime python not found at $Runtime" }
$claudeCli = $null
if (-not $SkipClaude) {
    $claudeCli = Find-ClaudeCli
    if (-not $claudeCli) { throw 'claude CLI not found; re-run with -SkipClaude to install only the Codex side' }
}

$state = (Invoke-Helper @('state')).state
Write-Host "Crosstalk P0 install state: $state"

if ($state -eq 'installed') {
    Write-Host 'Already installed: upgrading executable paths and Claude plugin; existing permissions and backups are kept.'
    $snapshot = (Invoke-Helper @('upgrade-snapshot')).snapshot
    Write-Host "Current configuration snapshot: $snapshot"
    if (-not $SkipCodex) {
        $upgradeArgs = @('toml-upgrade')
        if ($PSBoundParameters.ContainsKey('ApprovalMode')) { $upgradeArgs += @('--mode', $ApprovalMode) }
        $upgraded = Invoke-Helper $upgradeArgs
        Write-Host "Codex executable updated; preserved approval mode '$($upgraded.mode)'."
    }
    try {
        Invoke-Helper @('render-plugin') | Out-Null
        if (-not $SkipClaude) {
            Invoke-Helper @('rebind-marketplace') | Out-Null
            & $claudeCli plugin update $PluginId --scope user
            if ($LASTEXITCODE -ne 0) { throw "Claude plugin update failed (exit $LASTEXITCODE)" }
            Invoke-Helper @('verify-upgrade', '--snapshot', $snapshot) | Out-Null
            Write-Host 'Claude marketplace and cached plugin verified against the tracked kit; permissions preserved.'
        } else {
            Write-Warning 'Claude upgrade skipped. Its existing plugin may still use the old probe path.'
        }
        Invoke-Helper @('record-installed') | Out-Null
    } catch {
        Write-Warning "Upgrade incomplete: $($_.Exception.Message)"
        Write-Warning "Current configuration was preserved at $snapshot. Inspect the exact failed step; do not restore entire old settings or resend letters."
        throw
    }
    Install-HelperShortcutAndPermissions
    Write-Host 'Restart the apps to load the upgraded tools. Messaging controls and stored state were not changed.'
    Write-Host 'Follow docs/Crosstalk/README.md for the current native app setup and acceptance instructions.'
    return
}
if ($state -eq 'backed-up') {
    throw 'A previous install stopped half-way. Run uninstall.ps1 first, then install again.'
}

# 1. Backups (also records SHA-256 of both files).
$backup = Invoke-Helper @('backup')
foreach ($f in $backup.files) { Write-Host ("backup {0}: existed={1} sha256={2}" -f $f.id, $f.existed, $f.sha256) }

try {
    # 2. Render the plugin with absolute paths.
    Invoke-Helper @('render-plugin') | Out-Null

    # 3. Codex block.
    if (-not $SkipCodex) {
        $added = Invoke-Helper @('toml-add', '--mode', $ApprovalMode)
        Write-Host ("Codex: added [mcp_servers.crosstalk_p0] mode={0} parsedBack={1}" -f $added.mode, $added.parsed_back)
    }

    # 4. Claude plugin, user scope, from the local marketplace.
    if (-not $SkipClaude) {
        & $claudeCli plugin marketplace add $Marketplace --scope user
        if ($LASTEXITCODE -ne 0) { throw "claude plugin marketplace add failed (exit $LASTEXITCODE)" }
        & $claudeCli plugin install $PluginId --scope user
        if ($LASTEXITCODE -ne 0) { throw "claude plugin install failed (exit $LASTEXITCODE)" }
    }

    Invoke-Helper @('record-installed', '--mode', $ApprovalMode) | Out-Null
    Install-HelperShortcutAndPermissions
}
catch {
    Write-Warning "Install stopped: $($_.Exception.Message)"
    Write-Warning 'Run uninstall.ps1 for surgical removal preserving other configuration. Entire original configuration restore requires the explicit -RestoreOriginalConfiguration option.'
    throw
}

Write-Host ''
Write-Host 'Installed. Next:'
Write-Host '  1. Messaging is OFF. Enable it when a probe needs it:'
Write-Host ("     & `"$Runtime`" -I -B `"$Cli`" enable")
Write-Host '  2. Restart the native apps to load current rules and tools.'
Write-Host '  3. Follow docs/Crosstalk/README.md for acceptance. uninstall.ps1 removes this kit; it is not an upgrade step.'
