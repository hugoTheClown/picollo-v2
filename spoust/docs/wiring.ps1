# Generates docs/wiring.jpg (pinout + trigger wiring for the ESP32-C3 0.42" OLED board).
# Windows PowerShell 5.1, no extra installs:  powershell -ExecutionPolicy Bypass -File docs\wiring.ps1
# Rerun after changing pins in src/main.c.
Add-Type -AssemblyName System.Drawing

$W = 1100; $H = 1040
$bmp = New-Object System.Drawing.Bitmap $W, $H
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'
$g.TextRenderingHint = 'AntiAliasGridFit'

function C($r, $gg, $b) { [System.Drawing.Color]::FromArgb($r, $gg, $b) }
$BG     = C 20 20 24
$BOARD  = C 30 30 34
$EDGE   = C 90 90 100
$WIRE   = C 220 190 60
$GNDW   = C 150 150 150
$TEXT   = C 235 235 235
$DIM    = C 165 165 165
$USED   = C 230 195 60    # used by this project
$TAKEN  = C 220 90 80     # occupied on the board / avoid
$FREE   = C 110 190 120   # free
$PWR    = C 120 160 230   # power pins
$LEDC   = C 225 80 80

function Pen($c, $w) { New-Object System.Drawing.Pen $c, $w }
function Brush($c) { New-Object System.Drawing.SolidBrush $c }
function Font($size, $style = 'Regular', $name = 'Segoe UI') { New-Object System.Drawing.Font $name, $size, ([System.Drawing.FontStyle]$style), ([System.Drawing.GraphicsUnit]::Pixel) }

$fTitle = Font 28 'Bold'
$fBig   = Font 20 'Bold'
$fLabel = Font 17
$fSmall = Font 14
$fPin   = Font 16 'Bold' 'Consolas'

$left   = New-Object System.Drawing.StringFormat; $left.LineAlignment = 'Center'
$right  = New-Object System.Drawing.StringFormat; $right.LineAlignment = 'Center'; $right.Alignment = 'Far'
$center = New-Object System.Drawing.StringFormat; $center.LineAlignment = 'Center'; $center.Alignment = 'Center'

function Txt($s, $f, $c, $x, $y, $fmt = $left) { $g.DrawString($s, $f, (Brush $c), [single]$x, [single]$y, $fmt) }
function Line($c, $w, $x1, $y1, $x2, $y2) { $g.DrawLine((Pen $c $w), [single]$x1, [single]$y1, [single]$x2, [single]$y2) }
function RRect($x, $y, $w, $h, $r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $p.AddArc($x, $y, 2*$r, 2*$r, 180, 90); $p.AddArc($x+$w-2*$r, $y, 2*$r, 2*$r, 270, 90)
    $p.AddArc($x+$w-2*$r, $y+$h-2*$r, 2*$r, 2*$r, 0, 90); $p.AddArc($x, $y+$h-2*$r, 2*$r, 2*$r, 90, 90)
    $p.CloseFigure(); $p
}

$g.Clear($BG)
Txt "Picollo spoust - ESP32-C3 0.42`" OLED: pinout + wiring" $fTitle $TEXT 24 36

# ---- board ----
$bx = 330; $by = 110; $bw = 280; $bh = 590
$g.FillRectangle((Brush (C 60 60 66)), 430, 92, 80, 30)            # USB connector
$g.DrawRectangle((Pen $EDGE 2), 430, 92, 80, 30)
$path = RRect $bx $by $bw $bh 14
$g.FillPath((Brush $BOARD), $path); $g.DrawPath((Pen $EDGE 3), $path)
Txt "USB" $fSmall $DIM 470 101 $center

$g.FillRectangle((Brush (C 5 5 8)), 385, 150, 170, 80)             # OLED
$g.DrawRectangle((Pen (C 70 110 160) 2), 385, 150, 170, 80)
Txt "OLED 0.42`"" $fLabel $TEXT 470 178 $center
Txt "72x40, I2C 0x3C" $fSmall $DIM 470 202 $center
Txt "ESP32-C3" $fBig $TEXT 470 258 $center

# ---- pins (order per espboards.dev table, NOT verified on the board) ----
$leftPins = @(
    @('5V',     $USED,  'USB 5V / power in'),
    @('GND',    $USED,  ''),
    @('3V3',    $PWR,   '3.3 V out'),
    @('GPIO20', $TAKEN, 'UART RX (console)'),
    @('GPIO21', $TAKEN, 'UART TX (console)'),
    @('GPIO2',  $TAKEN, 'strapping pin'),
    @('GPIO1',  $FREE,  'free (ADC)'),
    @('GPIO0',  $FREE,  'free (ADC)')
)
$rightPins = @(
    @('GPIO10', $FREE,  'free'),
    @('GPIO9',  $TAKEN, 'BOOT btn, strap'),
    @('GPIO8',  $TAKEN, 'blue LED, strap'),
    @('GPIO7',  $USED,  ''),
    @('GPIO6',  $TAKEN, 'OLED SCL'),
    @('GPIO5',  $TAKEN, 'OLED SDA'),
    @('GPIO4',  $USED,  ''),
    @('GPIO3',  $FREE,  'free (ADC)')
)
$py0 = 320; $pstep = 50
function PinY($i) { $py0 + $i * $pstep }

for ($i = 0; $i -lt 8; $i++) {
    $y = PinY $i
    $p = $leftPins[$i]
    $g.FillEllipse((Brush $p[1]), $bx - 7, $y - 7, 14, 14)
    Txt $p[0] $fPin $p[1] ($bx + 16) $y
    if ($p[2]) { Txt $p[2] $fSmall $DIM ($bx - 16) $y $right }

    $p = $rightPins[$i]
    $g.FillEllipse((Brush $p[1]), $bx + $bw - 7, $y - 7, 14, 14)
    Txt $p[0] $fPin $p[1] ($bx + $bw - 16) $y $right
    if ($p[2]) { Txt $p[2] $fSmall $DIM ($bx + $bw + 16) $y }
}

$yGnd = PinY 1; $y5v = PinY 0
$yLed = PinY 3; $yBtn = PinY 6
$rx = $bx + $bw
$rail = 1010

# ---- power ----
Txt "USB cable / 5 V supply" $fSmall $DIM 470 74 $center

# ---- GND: from left GND pin around the bottom to the rail on the right ----
$yBot = $by + $bh + 40
$gx = 150
Line $GNDW 3 $bx $yGnd 322 $yGnd
Line $GNDW 3 322 $yGnd 322 ($yGnd + 22)
Line $GNDW 3 322 ($yGnd + 22) $gx ($yGnd + 22)
Line $GNDW 3 $gx ($yGnd + 22) $gx $yBot
Line $GNDW 3 $gx $yBot $rail $yBot
Line $GNDW 3 $rail $yBot $rail $yLed
Txt "GND" $fSmall $DIM ($rail + 10) ($yLed + 60)
Txt "GND" $fSmall $DIM ($gx - 10) ($yGnd + 60) $right

# ---- LED: GPIO7 -> R -> LED -> GND ----
Line $WIRE 3 $rx $yLed 760 $yLed
$g.DrawRectangle((Pen $DIM 2), 760, $yLed - 10, 50, 20)
Txt "R 220-330 ohm" $fSmall $DIM 785 ($yLed - 26) $center
Line $WIRE 3 810 $yLed 860 $yLed
$g.DrawEllipse((Pen $LEDC 3), 860, $yLed - 15, 30, 30)
Line $LEDC 2 867 ($yLed - 8) 883 ($yLed + 8)
Line $LEDC 2 883 ($yLed - 8) 867 ($yLed + 8)
Txt "status LED" $fLabel $TEXT 875 ($yLed + 32) $center
Line $WIRE 3 890 $yLed $rail $yLed

# ---- button: GPIO4 -> button -> GND, 100 nF across it ----
$btnX = 870
Line $WIRE 3 $rx $yBtn ($btnX - 30) $yBtn
$g.DrawRectangle((Pen $TEXT 3), $btnX - 30, $yBtn - 22, 60, 44)
$g.FillEllipse((Brush $TEXT), $btnX - 9, $yBtn - 9, 18, 18)
Txt "BUTTON" $fLabel $TEXT $btnX ($yBtn - 42) $center
Txt "momentary, NO" $fSmall $DIM $btnX ($yBtn + 38) $center
Line $WIRE 3 ($btnX + 30) $yBtn $rail $yBtn

$capX = 740; $capY = $yBtn + 55
Line $WIRE 2 $capX $yBtn $capX ($capY - 6)
Line $WIRE 4 ($capX - 16) ($capY - 6) ($capX + 16) ($capY - 6)
Line $WIRE 4 ($capX - 16) ($capY + 4) ($capX + 16) ($capY + 4)
Line $WIRE 2 $capX ($capY + 4) $capX ($capY + 30)
Line $WIRE 2 $capX ($capY + 30) $rail ($capY + 30)
Txt "C 100 nF" $fSmall $DIM ($capX + 24) ($capY - 1)

# ---- legend ----
$ly = $yBot + 40
Txt "Legend" $fBig $TEXT 24 $ly
$items = @(
    @($USED,  'used by spoust (button GPIO4, LED GPIO7, power)'),
    @($TAKEN, 'occupied on the board or risky (OLED, BOOT, strapping, console)'),
    @($FREE,  'free for later use'),
    @($PWR,   'power')
)
for ($i = 0; $i -lt $items.Count; $i++) {
    $y = $ly + 34 + $i * 26
    $g.FillEllipse((Brush $items[$i][0]), 24, $y - 7, 14, 14)
    Txt $items[$i][1] $fSmall $DIM 48 $y
}
$notes = @(
    'Button: GPIO4 to GND, internal pull-up (no resistor). 100 nF across the button = hw debounce.',
    'LED: GPIO7 -> 220-330 ohm -> LED anode, cathode -> GND. Lit while waiting for the 2nd press.',
    'OLED is on the board (SDA GPIO5, SCL GPIO6). Do not use GPIO5/6/8/9 for anything else.',
    'PIN ORDER ON THE HEADERS IS FROM espboards.dev AND NOT YET VERIFIED - check the silkscreen.'
)
for ($i = 0; $i -lt $notes.Count; $i++) {
    $c = if ($i -eq 3) { $TAKEN } else { $DIM }
    Txt $notes[$i] $fSmall $c 24 ($ly + 150 + $i * 24)
}

# ---- save ----
$out = Join-Path $PSScriptRoot 'wiring.jpg'
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$ep = New-Object System.Drawing.Imaging.EncoderParameters 1
$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), ([long]92)
$bmp.Save($out, $codec, $ep)
$g.Dispose(); $bmp.Dispose()
Write-Output "Saved $out"
