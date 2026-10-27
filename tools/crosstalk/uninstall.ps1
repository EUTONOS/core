<#
.SYNOPSIS
  Remove the tracked Crosstalk kit while keeping unrelated configuration. OWNER-RUN ONLY.

.DESCRIPTION
  1. Prints what changed in the two config files since the backup (names/counts only). Look at this
     BEFORE restoring: if you clicked "always allow" for the Claude tool, a permission rule may now
     be in ~/.claude/settings.json (probe 3 reads that).
  2. Uninstalls the Claude plugin and removes the local marketplace (claude CLI).
  3. Removes the marked [mcp_servers.crosstalk_p0] block from ~/.codex/config.toml.
  4. Keeps other changes to ~/.codex/config.toml and ~/.claude/settings.json by default.
     Only -RestoreOriginalConfiguration restores entire original files and verifies pre-install
     hashes. This explicit option can roll back unrelated changes made since installation;
     current diverged files are first saved under ...\crosstalk-probe\install\diverged-<time>\.
  5. Removes the retired relay folder under %USERPROFILE%\.eutonos\crosstalk-probe\.
  6. Removes the "Crosstalk helper" Startup shortcut (only that file; a running helper is not stopped).
  7. Removes from ~/.claude/settings.json only the permission rules install recorded adding with -ApplyPermissions
     (after a backup); rules you added yourself are never touched.
  8. Leaves the probe journal and directory in %USERPROFILE%\.eutonos\crosstalk-probe\.

.PARAMETER KeepOtherChanges
  Backward-compatible explicit selection of the default surgical removal. Cannot be combined with
  -RestoreOriginalConfiguration. Original hashes may differ because other changes are preserved.

.PARAMETER RestoreOriginalConfiguration
  Explicitly restore both entire pre-install configuration files. This may roll back unrelated
  settings and permission changes made since installation. Original backups are preserved.

.EXAMPLE
  .\uninstall.ps1
#>
[CmdletBinding()]
param(
    [switch]$KeepOtherChanges,
    [switch]$RestoreOriginalConfiguration
)

$ErrorActionPreference = 'Stop'
if ($KeepOtherChanges -and $RestoreOriginalConfiguration) {
    throw 'Choose surgical removal (-KeepOtherChanges/default) or -RestoreOriginalConfiguration, not both.'
}
if ($RestoreOriginalConfiguration) {
    Write-Warning 'Restoring entire original configuration files can roll back unrelated settings and permission changes made since installation. Current diverged files will be saved first.'
}
$Kit = $PSScriptRoot
$Runtime = Join-Path $env:USERPROFILE '.eutonos\runtime\Scripts\python.exe'
$Helper = Join-Path $Kit 'crosstalk_p0_install.py'
$PluginId = 'crosstalk-p0@crosstalk-p0-local'
$MarketplaceName = 'crosstalk-p0-local'

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

if (-not (Test-Path -LiteralPath $Runtime)) { throw "EUTONOS runtime python not found at $Runtime" }
$state = (Invoke-Helper @('state')).state
# The Startup shortcut is its own file, so it goes even when there is no manifest.
$shortcut = Invoke-Helper @('helper-startup-remove')
Write-Host ("Startup shortcut: removed={0} ({1})" -f $shortcut.removed, $shortcut.shortcut)
if ($state -eq 'none') { Write-Host 'Nothing to uninstall (no install manifest).'; return }
if ($state -eq 'restored') { Write-Host 'Already restored.'; return }

Write-Host '--- changes since backup (names and counts only) ---'
(Invoke-Helper @('compare')) | ConvertTo-Json -Depth 8
Write-Host '-----------------------------------------------------'

$claudeCli = Find-ClaudeCli
if ($claudeCli) {
    # Failures here (plugin never installed, already removed) must not stop the file restore.
    & $claudeCli plugin uninstall $PluginId --scope user
    if ($LASTEXITCODE -ne 0) { Write-Warning "claude plugin uninstall exit $LASTEXITCODE (continuing)" }
    & $claudeCli plugin marketplace remove $MarketplaceName --scope user
    if ($LASTEXITCODE -ne 0) { Write-Warning "claude plugin marketplace remove exit $LASTEXITCODE (continuing)" }
}
else {
    Write-Warning 'claude CLI not found; Claude plugin removal remains pending. Default surgical removal preserves settings.json; remove the exact plugin and marketplace in Claude when available.'
}

Invoke-Helper @('toml-remove') | Out-Null
# The retired relay folder (jobs, heartbeats, ticket flag) under the device folder; nothing else there.
Invoke-Helper @('relay-folder-remove') | Out-Null
# Only the permission rules install recorded adding; nothing recorded means settings.json is not touched.
$permissions = Invoke-Helper @('permissions-remove')
Write-Host ("Permissions: removed {0} recorded entr(ies); backup: {1}" -f @($permissions.removed).Count, $permissions.backup)

$restoreArgs = @('restore')
if (-not $RestoreOriginalConfiguration) { $restoreArgs += '--keep-other-changes' }
$result = Invoke-Helper -Arguments $restoreArgs -OkCodes @(0, 3)
foreach ($f in $result.files) {
    Write-Host ("{0}: {1}; sha256 now={2}; matches pre-install={3}" -f $f.id, $f.action, $f.hash_after, $f.matches_pre_install)
    if ($f.diverged_copy) { Write-Host ("  diverged copy saved at {0}" -f $f.diverged_copy) }
}
Write-Host ("Probe journal and directory left in place: {0}" -f $result.journal_kept)

if ($RestoreOriginalConfiguration -and $result.hashes_match_pre_install -ne $true) {
    throw 'SHA-256 of a restored file does not match its pre-install hash. See the output above.'
}
Write-Host 'Uninstall complete.'
