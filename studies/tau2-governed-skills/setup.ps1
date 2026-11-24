# Clone and pin tau2-bench, create the virtualenv, install everything. Safe to re-run.
#
#   powershell -ExecutionPolicy Bypass -File studies\tau2-skills\setup.ps1
#
# The venv lives at a short path by default: tau2's dependency litellm ships files whose
# paths overflow Windows' 260-character limit under this repo's worktree paths.
param([string]$Venv = "$env:USERPROFILE\.venvs\tau2-skills",
      [string]$Python = "python")
$ErrorActionPreference = "Stop"
$study = $PSScriptRoot
$vendor = Join-Path $study "vendor\tau2-bench"
$tag = "v1.0.1"
$commit = "fc0055dc4e0a316c3f83133267fbd6faaa770992"

if (-not (Test-Path $vendor)) {
    git clone --filter=blob:none --no-checkout https://github.com/sierra-research/tau2-bench.git $vendor
}
git -C $vendor config core.longpaths true
git -C $vendor fetch --tags --quiet
git -C $vendor checkout -q -f $tag
$head = (git -C $vendor rev-parse HEAD).Trim()
if ($head -ne $commit) { throw "tau2-bench $tag resolved to $head, expected $commit" }

if (-not (Test-Path "$Venv\Scripts\python.exe")) { & $Python -m venv $Venv }
& "$Venv\Scripts\python.exe" -m pip install -q --upgrade pip
Push-Location $study
try { & "$Venv\Scripts\python.exe" -m pip install -q -r requirements.txt }
finally { Pop-Location }
Write-Host "tau2-bench $tag ($commit) installed into $Venv"
Write-Host "Next: cd $study; & '$Venv\Scripts\python.exe' -X utf8 -m pytest tests -q"
