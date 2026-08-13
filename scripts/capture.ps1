# Capture the reMarkable desktop-app window so Claude can read and mark handwritten answers.
#
#   .\capture.ps1                          capture the reMarkable window (works even when occluded)
#   .\capture.ps1 -Crop "0.30,0.05,0.70,1.0"   crop to a fraction of the window (L,T,R,B as 0-1)
#   .\capture.ps1 -List                    list window titles
#   .\capture.ps1 -Screen                  whole desktop instead
#   .\capture.ps1 -Diag                    window rect + monitor layout
#
# Output defaults to page.png beside this script. Claude then reads that file.
param(
  [string]$Window = "reMarkable",
  [string]$Out = "",
  [string]$Crop = "",
  [switch]$List,
  [switch]$Screen,
  [switch]$Diag
)

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinApi {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[WinApi]::SetProcessDPIAware() | Out-Null

if ($List) {
  Get-Process | Where-Object { $_.MainWindowTitle -ne "" } |
    Select-Object ProcessName, MainWindowTitle | Sort-Object ProcessName | Format-Table -AutoSize
  exit 0
}
if (-not $Out) { $Out = Join-Path $PSScriptRoot "page.png" }

function Test-Blank($bmp) {
  $seen = @{}
  for ($i = 0; $i -lt 60; $i++) {
    $px = $bmp.GetPixel([int]($bmp.Width * (Get-Random -Min 5 -Max 95) / 100),
                        [int]($bmp.Height * (Get-Random -Min 5 -Max 95) / 100))
    $seen[$px.ToArgb()] = 1
  }
  return ($seen.Keys.Count -le 2)
}
function Save-Cropped($bmp, $path, $cropSpec) {
  if ($cropSpec) {
    $f = $cropSpec.Split(",") | ForEach-Object { [double]$_ }
    $x = [int]($bmp.Width * $f[0]); $y = [int]($bmp.Height * $f[1])
    $w = [int]($bmp.Width * ($f[2] - $f[0])); $h = [int]($bmp.Height * ($f[3] - $f[1]))
    $rect = New-Object System.Drawing.Rectangle $x, $y, $w, $h
    $sub = $bmp.Clone($rect, $bmp.PixelFormat)
    $sub.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $dims = "$($sub.Width)x$($sub.Height) (cropped)"
    $sub.Dispose()
  } else {
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $dims = "$($bmp.Width)x$($bmp.Height)"
  }
  return $dims
}

if ($Screen) {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.X, $b.Y, 0, 0, (New-Object System.Drawing.Size($b.Width, $b.Height)))
  $g.Dispose()
  $d = Save-Cropped $bmp $Out $Crop
  Write-Output "captured [full desktop] $d -> $Out"
  $bmp.Dispose(); exit 0
}

$p = Get-Process | Where-Object { $_.MainWindowTitle -like "*$Window*" -and $_.MainWindowHandle -ne 0 } |
     Select-Object -First 1
if (-not $p) { Write-Output "NO WINDOW MATCHING '$Window' - is the app running?"; exit 1 }
$hwnd = $p.MainWindowHandle
if ([WinApi]::IsIconic($hwnd)) { [WinApi]::ShowWindow($hwnd, 9) | Out-Null; Start-Sleep -Milliseconds 400 }

$r = New-Object WinApi+RECT
[WinApi]::GetWindowRect($hwnd, [ref]$r) | Out-Null
$w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top

if ($Diag) {
  Write-Output "window '$($p.MainWindowTitle)' => ${w}x${ht} at ($($r.Left),$($r.Top))"
  foreach ($scr in [System.Windows.Forms.Screen]::AllScreens) {
    Write-Output ("monitor {0} {1} primary={2}" -f $scr.DeviceName, $scr.Bounds, $scr.Primary)
  }
  exit 0
}
if ($w -le 0 -or $ht -le 0) { Write-Output "BAD BOUNDS ${w}x${ht}"; exit 1 }

# Ask the window to paint itself - survives being behind other windows.
$bmp = New-Object System.Drawing.Bitmap $w, $ht
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [WinApi]::PrintWindow($hwnd, $hdc, 2)   # PW_RENDERFULLCONTENT
$g.ReleaseHdc($hdc); $g.Dispose()

if (-not $ok -or (Test-Blank $bmp)) {
  $bmp.Dispose()
  [WinApi]::SetForegroundWindow($hwnd) | Out-Null
  Start-Sleep -Milliseconds 500
  [WinApi]::GetWindowRect($hwnd, [ref]$r) | Out-Null
  $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
  $bmp = New-Object System.Drawing.Bitmap $w, $ht
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
  $g.Dispose()
  $d = Save-Cropped $bmp $Out $Crop
  Write-Output "captured [$($p.MainWindowTitle)] via screen-copy $d -> $Out"
} else {
  $d = Save-Cropped $bmp $Out $Crop
  Write-Output "captured [$($p.MainWindowTitle)] via PrintWindow $d -> $Out"
}
$bmp.Dispose()
