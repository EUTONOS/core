param([switch]$Once)
$probe = Join-Path $PSScriptRoot '..\src\eutonos\desktop_companion_probe.ps1'
& $probe @PSBoundParameters
