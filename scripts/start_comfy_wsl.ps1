#Requires -Version 5.1
# Arrow keys + Enter to pick GPU; then start ComfyUI inside WSL.
# Run from start_comfy_wsl.bat (which cds to %SystemRoot% first — avoids WSL "Failed to translate Z:\")

$ErrorActionPreference = "Stop"

$LaunchSh = if ($env:COMFY_LAUNCH_SH) { $env:COMFY_LAUNCH_SH } else {
    "/home/zlh-linux/ComfyUI/custom_nodes/zlhNode/scripts/wsl_launch_comfy.sh"
}

function Read-GpuChoice {
    $opts = @(
        @{ Arg = "";  Label = "1,0  (physical GPU 1 first, then 0)" },
        @{ Arg = "0"; Label = "GPU 0 only" },
        @{ Arg = "1"; Label = "GPU 1 only" }
    )
    $i = 0
    $prev = [Console]::CursorVisible
    [Console]::CursorVisible = $false
    try {
        while ($true) {
            Clear-Host
            Write-Host "Select GPU mode (Up / Down, Enter to confirm)`n"
            for ($j = 0; $j -lt $opts.Count; $j++) {
                $prefix = if ($j -eq $i) { "> " } else { "  " }
                Write-Host ($prefix + $opts[$j].Label)
            }
            $k = [Console]::ReadKey($true)
            switch ($k.Key) {
                "UpArrow"   { if ($i -gt 0) { $i-- } }
                "DownArrow" { if ($i -lt $opts.Count - 1) { $i++ } }
                "Enter"     { return $opts[$i].Arg }
            }
        }
    }
    finally {
        [Console]::CursorVisible = $prev
    }
}

Clear-Host
Write-Host "Starting ComfyUI via WSL ...`n"
$choice = Read-GpuChoice

# WSL interop breaks on SUBST/pushd drive (Z:); use a path Windows can translate.
Set-Location $env:SystemRoot

Write-Host "`nUsing: $LaunchSh"
if ($choice -eq "") {
    Write-Host "Argument: <empty> -> CUDA_VISIBLE_DEVICES=1,0 in Linux`n"
} else {
    Write-Host "Argument: $choice -> CUDA_VISIBLE_DEVICES=$choice in Linux`n"
}

if ($env:WSL_DISTRO_NAME) {
    if ($choice -eq "") {
        & wsl -d $env:WSL_DISTRO_NAME bash --noprofile --norc $LaunchSh
    } else {
        & wsl -d $env:WSL_DISTRO_NAME bash --noprofile --norc $LaunchSh $choice
    }
} else {
    if ($choice -eq "") {
        & wsl bash --noprofile --norc $LaunchSh
    } else {
        & wsl bash --noprofile --norc $LaunchSh $choice
    }
}

$code = $LASTEXITCODE
Write-Host "`nWSL exited with code $code"
Read-Host "Press Enter to close"
