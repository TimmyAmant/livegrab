# LiveGrab dependency installer: OBS Studio, Python 3.12 (per-user), Streamlink (per-user, includes FFmpeg)
param([string]$ResultFile = "$env:TEMP\livegrab_result.txt")
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Say($m) { Write-Output $m }
$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

function Download($url, $out) {
    try { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec 600; return (Test-Path $out) }
    catch { Say "  download failed: $($_.Exception.Message)"; return $false }
}
function Winget($id, $scope) {
    if (-not $HasWinget) { return $false }
    $a = @("install","-e","--id",$id,"--silent","--accept-package-agreements","--accept-source-agreements","--disable-interactivity")
    if ($scope) { $a += @("--scope",$scope) }
    $p = Start-Process winget -ArgumentList $a -Wait -PassThru -WindowStyle Hidden
    return ($p.ExitCode -eq 0)
}

# ---------- OBS ----------
function Find-OBS {
    $c = @()
    try { $r = (Get-ItemProperty "HKLM:\SOFTWARE\OBS Studio" -ErrorAction Stop)."(default)"; if ($r) { $c += "$r\bin\64bit\obs64.exe" } } catch {}
    $c += "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe"
    foreach ($p in $c) { if (Test-Path $p) { return $p } }
    return $null
}
Say "Checking OBS Studio..."
$obs = Find-OBS
if (-not $obs) {
    Say "  OBS not found. Installing OBS Studio (Windows may ask for permission)..."
    if (-not (Winget "OBSProject.OBSStudio" $null)) {
        try {
            $rel = Invoke-RestMethod "https://api.github.com/repos/obsproject/obs-studio/releases/latest" -TimeoutSec 60
            $asset = $rel.assets | Where-Object { $_.name -match "Windows.*(Installer|x64)\.exe$" } | Select-Object -First 1
            $f = "$env:TEMP\obs-installer.exe"
            if ($asset -and (Download $asset.browser_download_url $f)) { Start-Process $f -ArgumentList "/S" -Verb RunAs -Wait }
        } catch { Say "  $($_.Exception.Message)" }
    }
    $obs = Find-OBS
}
if ($obs) { Say "  OBS: $obs" } else { Say "  WARNING: OBS still not found. Install it from obsproject.com, then run this installer again." }

# ---------- Python 3.12 ----------
function Find-Python {
    $c = @("$env:LOCALAPPDATA\Programs\Python\Python312", "$env:ProgramFiles\Python312")
    foreach ($k in @("HKCU:\Software\Python\PythonCore\3.12\InstallPath","HKLM:\Software\Python\PythonCore\3.12\InstallPath")) {
        try { $v = (Get-ItemProperty $k -ErrorAction Stop)."(default)"; if ($v) { $c = @($v.TrimEnd('\')) + $c } } catch {}
    }
    foreach ($d in $c) { if ((Test-Path "$d\python.exe") -and (Test-Path "$d\python312.dll")) { return $d } }
    return $null
}
Say "Checking Python 3.12..."
$py = Find-Python
if (-not $py) {
    Say "  Installing Python 3.12 (just for your user, no PATH changes)..."
    $f = "$env:TEMP\python-3.12.10-amd64.exe"
    if (Download "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" $f) {
        Start-Process $f -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 Include_doc=0 Shortcuts=0" -Wait
    } else { Winget "Python.Python.3.12" "user" | Out-Null }
    $py = Find-Python
}
if ($py) { Say "  Python: $py" } else { Say "  ERROR: Python 3.12 could not be installed."; exit 2 }

# ---------- Streamlink ----------
function Find-Streamlink {
    foreach ($d in @("$env:LOCALAPPDATA\Programs\Streamlink", "$env:ProgramFiles\Streamlink", "${env:ProgramFiles(x86)}\Streamlink")) {
        if (Test-Path "$d\bin\streamlink.exe") { return $d }
    }
    $cmd = Get-Command streamlink -ErrorAction SilentlyContinue
    if ($cmd) { return (Split-Path (Split-Path $cmd.Source)) }
    return $null
}
Say "Checking Streamlink..."
$sl = Find-Streamlink
if (-not $sl) {
    Say "  Installing Streamlink (includes FFmpeg)..."
    $ok = $false
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/streamlink/windows-builds/releases/latest" -TimeoutSec 60
        $asset = $rel.assets | Where-Object { $_.name -match "x86_64\.exe$" } | Select-Object -First 1
        $f = "$env:TEMP\$($asset.name)"
        if ($asset -and (Download $asset.browser_download_url $f)) {
            Start-Process $f -ArgumentList "/S /CURRENTUSER" -Wait; $ok = $true
        }
    } catch { Say "  $($_.Exception.Message)" }
    if (-not (Find-Streamlink)) { Winget "Streamlink.Streamlink" "user" | Out-Null }
    $sl = Find-Streamlink
}
if ($sl) { Say "  Streamlink: $sl" } else { Say "  WARNING: Streamlink not found. The script will ask you to install it." }

Set-Content -Path $ResultFile -Value $py -Encoding ASCII
exit 0
