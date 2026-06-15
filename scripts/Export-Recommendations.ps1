param(
    [string]$ImportWorkbook = "C:\path\to\source-data\booking-import.xlsx",
    [string]$AdHocsWorkbook = ".\data\July 2026 Ad hocs.xlsx",
    [string]$PostcodeWorkbook = ".\data\Staff and SU names and postcode.xlsx",
    [string]$OutputPath = ".\RecommenderSystem\output\july-2026-recommendations.csv",
    [int]$TopN = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Convert-ColumnLettersToNumber {
    param([string]$Letters)
    $n = 0
    foreach ($ch in $Letters.ToUpperInvariant().ToCharArray()) {
        $n = ($n * 26) + ([int][char]$ch - [int][char]'A' + 1)
    }
    return $n
}

function Get-CellColumn {
    param([string]$CellReference)
    if ($CellReference -match '^([A-Z]+)') {
        return (Convert-ColumnLettersToNumber $matches[1])
    }
    return 1
}

function Get-XlsxSharedStrings {
    param($Zip)
    $shared = @()
    $entry = $Zip.Entries | Where-Object FullName -eq 'xl/sharedStrings.xml'
    if (-not $entry) { return $shared }

    $reader = [IO.StreamReader]::new($entry.Open())
    try {
        [xml]$xml = $reader.ReadToEnd()
    } finally {
        $reader.Close()
    }

    foreach ($item in $xml.sst.si) {
        $shared += (($item.InnerText) -replace "`r?`n", " ").Trim()
    }
    return $shared
}

function Get-XlsxSheetTargets {
    param($Zip)
    $workbookEntry = $Zip.Entries | Where-Object FullName -eq 'xl/workbook.xml'
    $relsEntry = $Zip.Entries | Where-Object FullName -eq 'xl/_rels/workbook.xml.rels'

    $reader = [IO.StreamReader]::new($workbookEntry.Open())
    try {
        [xml]$workbook = $reader.ReadToEnd()
    } finally {
        $reader.Close()
    }

    $reader = [IO.StreamReader]::new($relsEntry.Open())
    try {
        [xml]$rels = $reader.ReadToEnd()
    } finally {
        $reader.Close()
    }

    $ns = [Xml.XmlNamespaceManager]::new($workbook.NameTable)
    $ns.AddNamespace('m', 'http://schemas.openxmlformats.org/spreadsheetml/2006/main')
    $ns.AddNamespace('r', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships')

    $targets = @{}
    foreach ($sheet in $workbook.SelectNodes('//m:sheet', $ns)) {
        $rid = $sheet.GetAttribute('id', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships')
        $rel = $rels.Relationships.Relationship | Where-Object { $_.Id -eq $rid } | Select-Object -First 1
        if (-not $rel) { continue }
        $target = $rel.Target -replace '^/', ''
        if ($target -notmatch '^xl/') { $target = "xl/$target" }
        $targets[$sheet.name] = $target
    }
    return $targets
}

function Get-XlsxCellText {
    param($Cell, [array]$SharedStrings)
    if (-not $Cell) { return "" }
    $cellType = $Cell.GetAttribute('t')
    $valueNode = $Cell.SelectSingleNode('*[local-name()="v"]')
    if ($cellType -eq 's' -and $valueNode -ne $null -and "$($valueNode.InnerText)" -ne "") {
        return $SharedStrings[[int]$valueNode.InnerText]
    }
    if ($cellType -eq 'inlineStr') {
        return (($Cell.is.InnerText) -replace "`r?`n", " ").Trim()
    }
    if ($valueNode -eq $null) { return "" }
    return (([string]$valueNode.InnerText) -replace "`r?`n", " ").Trim()
}

function Read-XlsxSheet {
    param([string]$Path, [string]$SheetName)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $zip = [IO.Compression.ZipFile]::OpenRead($resolved)
    try {
        $shared = Get-XlsxSharedStrings $zip
        $targets = Get-XlsxSheetTargets $zip
        if (-not $targets.ContainsKey($SheetName)) {
            return @()
        }

        $entry = $zip.Entries | Where-Object FullName -eq $targets[$SheetName]
        $reader = [IO.StreamReader]::new($entry.Open())
        try {
            [xml]$xml = $reader.ReadToEnd()
        } finally {
            $reader.Close()
        }

        $rows = @()
        foreach ($row in $xml.worksheet.sheetData.row) {
            $values = @{}
            foreach ($cell in $row.SelectNodes('*[local-name()="c"]')) {
                $col = Get-CellColumn $cell.GetAttribute('r')
                $values[$col] = Get-XlsxCellText $cell $shared
            }
            $rows += [pscustomobject]@{
                RowNumber = [int]$row.GetAttribute('r')
                Values = $values
            }
        }
        return $rows
    } finally {
        $zip.Dispose()
    }
}

function Convert-RowsToObjects {
    param([array]$Rows, [int]$HeaderRowIndex = 0)
    if ($Rows.Count -le $HeaderRowIndex) { return @() }

    $headerRow = $Rows[$HeaderRowIndex].Values
    $headers = @{}
    foreach ($key in $headerRow.Keys) {
        $name = [string]$headerRow[$key]
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $headers[[int]$key] = $name.Trim()
        }
    }

    $objects = @()
    foreach ($row in ($Rows | Select-Object -Skip ($HeaderRowIndex + 1))) {
        $obj = [ordered]@{}
        foreach ($col in ($headers.Keys | Sort-Object)) {
            $obj[$headers[$col]] = if ($row.Values.ContainsKey($col)) { $row.Values[$col] } else { "" }
        }
        $objects += [pscustomobject]$obj
    }
    return $objects
}

function Normalize-Key {
    param([string]$Value)
    return (($Value -replace '[^a-zA-Z0-9]', '').ToLowerInvariant())
}

function Get-Field {
    param($Object, [string]$Name)
    if (-not $Object) { return "" }
    $target = Normalize-Key $Name
    foreach ($prop in $Object.PSObject.Properties) {
        if ((Normalize-Key $prop.Name) -eq $target) {
            return [string]$prop.Value
        }
    }
    return ""
}

function Get-PostcodeArea {
    param([string]$Postcode)
    if ([string]::IsNullOrWhiteSpace($Postcode)) { return "" }
    $first = ($Postcode.Trim().ToUpperInvariant() -split '\s+')[0]
    if ($first -match '^[A-Z]+') { return $matches[0] }
    return $first
}

function Test-Yes {
    param([string]$Value)
    return ($Value -match '^(y|yes|true|1)$')
}

function Test-TimeOverlap {
    param([string]$StartA, [string]$EndA, [string]$StartB, [string]$EndB)
    if ([string]::IsNullOrWhiteSpace($StartB) -or [string]::IsNullOrWhiteSpace($EndB)) { return $true }
    try {
        $a1 = [TimeSpan]::Parse($StartA)
        $a2 = [TimeSpan]::Parse($EndA)
        $b1 = [TimeSpan]::Parse($StartB)
        $b2 = [TimeSpan]::Parse($EndB)
        return ($a1 -lt $b2 -and $b1 -lt $a2)
    } catch {
        return $true
    }
}

function Get-AdHocNotesByChild {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    $notes = @{}
    foreach ($sheet in @('Phase 1', 'Phase 2')) {
        $rows = Convert-RowsToObjects (Read-XlsxSheet -Path $Path -SheetName $sheet)
        foreach ($row in $rows) {
            $firstProperty = $row.PSObject.Properties | Select-Object -First 1
            $name = if ($firstProperty) { [string]$firstProperty.Value } else { "" }
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            $chunks = @()
            foreach ($prop in $row.PSObject.Properties) {
                if (-not [string]::IsNullOrWhiteSpace([string]$prop.Value)) {
                    $chunks += "$($prop.Name): $($prop.Value)"
                }
            }
            $notes[(Normalize-Key $name)] = ($chunks -join " | ")
        }
    }
    return $notes
}

function Build-PostcodeMaps {
    param([string]$Path)
    $maps = @{
        Staff = @{}
        Children = @{}
    }
    if (-not (Test-Path -LiteralPath $Path)) { return $maps }

    $staff = Convert-RowsToObjects (Read-XlsxSheet -Path $Path -SheetName 'STAFF')
    foreach ($row in $staff) {
        $supportWorker = Get-Field $row 'Support Worker'
        $address = Get-Field $row 'Address'
        if ($supportWorker) {
            $maps.Staff[(Normalize-Key $supportWorker)] = [pscustomobject]@{
                Postcode = $address
                Area = Get-PostcodeArea $address
                Travel = Get-Field $row 'How they get to sessions'
                Languages = Get-Field $row 'Languages Spoken'
            }
        }
    }

    $childrenRows = Read-XlsxSheet -Path $Path -SheetName 'SU'
    foreach ($row in ($childrenRows | Select-Object -Skip 1)) {
        $first = if ($row.Values.ContainsKey(1)) { $row.Values[1] } else { "" }
        $last = if ($row.Values.ContainsKey(2)) { $row.Values[2] } else { "" }
        $postcode = if ($row.Values.ContainsKey(3)) { $row.Values[3] } else { "" }
        $name = "$first $last".Trim()
        if ($name) {
            $maps.Children[(Normalize-Key $name)] = [pscustomobject]@{
                Postcode = $postcode
                Area = Get-PostcodeArea $postcode
                SupportLevel = if ($row.Values.ContainsKey(4)) { $row.Values[4] } else { "" }
            }
        }
    }
    return $maps
}

function Get-WorkersFromPostcodeWorkbook {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }

    $staffRows = Convert-RowsToObjects (Read-XlsxSheet -Path $Path -SheetName 'STAFF')
    $fallbackWorkers = @()
    foreach ($row in $staffRows) {
        $name = Get-Field $row 'Support Worker'
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $travel = Get-Field $row 'How they get to sessions'
        $fallbackWorkers += [pscustomobject]@{
            'SUPPORT WORKER ID' = ""
            'SUPPORT WORKER NAME' = $name
            'CAR' = if ($travel -match 'car|drive') { "Yes" } else { "No" }
            'EXPERIENCE' = ""
            'COMMENTS' = "Fallback candidate from staff postcode workbook; monthly Support Workers tab was empty."
        }
    }
    return $fallbackWorkers
}

function Worker-IsUnavailable {
    param($Worker, $Booking, [array]$Availability)
    $workerId = Get-Field $Worker 'SUPPORT WORKER ID'
    if ([string]::IsNullOrWhiteSpace($workerId)) { return $false }
    $date = Get-Field $Booking 'BOOKING DATE'
    $start = Get-Field $Booking 'BOOKING START TIME'
    $finish = Get-Field $Booking 'BOOKING FINISH TIME'

    foreach ($row in $Availability) {
        if ((Get-Field $row 'SUPPORT WORKER ID') -ne $workerId) { continue }
        if ((Get-Field $row 'DATE') -ne $date) { continue }
        if (Test-Yes (Get-Field $row 'ALL DAY')) { return $true }
        if (Test-TimeOverlap $start $finish (Get-Field $row 'FROM_1') (Get-Field $row 'TO_1')) { return $true }
        if (-not [string]::IsNullOrWhiteSpace((Get-Field $row 'FROM_2')) -and (Test-TimeOverlap $start $finish (Get-Field $row 'FROM_2') (Get-Field $row 'TO_2'))) { return $true }
    }
    return $false
}

function Get-ExplicitMatch {
    param($Worker, $Booking, [array]$Matching)
    if ([string]::IsNullOrWhiteSpace((Get-Field $Worker 'SUPPORT WORKER ID'))) { return "" }
    foreach ($row in $Matching) {
        if ((Get-Field $row 'SUPPORT WORKER ID') -eq (Get-Field $Worker 'SUPPORT WORKER ID') -and
            (Get-Field $row 'CHILD ID') -eq (Get-Field $Booking 'CHILD ID')) {
            return (Get-Field $row 'APPROPRIATE')
        }
    }
    return ""
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null

$bookings = Convert-RowsToObjects (Read-XlsxSheet -Path $ImportWorkbook -SheetName 'Bookings')
$children = Convert-RowsToObjects (Read-XlsxSheet -Path $ImportWorkbook -SheetName 'Childs')
$workers = Convert-RowsToObjects (Read-XlsxSheet -Path $ImportWorkbook -SheetName 'Support Workers')
$availability = Convert-RowsToObjects (Read-XlsxSheet -Path $ImportWorkbook -SheetName 'Availability')
$matching = Convert-RowsToObjects (Read-XlsxSheet -Path $ImportWorkbook -SheetName 'Matching')
$adHocNotes = Get-AdHocNotesByChild -Path $AdHocsWorkbook
$postcodeMaps = Build-PostcodeMaps -Path $PostcodeWorkbook

if (@($workers).Count -eq 0) {
    $workers = Get-WorkersFromPostcodeWorkbook -Path $PostcodeWorkbook
}

$childrenById = @{}
foreach ($child in $children) {
    $childIdForMap = Get-Field $child 'CHILD ID'
    if ($childIdForMap) { $childrenById[$childIdForMap] = $child }
}

$recommendations = @()

foreach ($booking in $bookings) {
    $bookingChildId = Get-Field $booking 'CHILD ID'
    if ([string]::IsNullOrWhiteSpace($bookingChildId)) { continue }

    $leadSwId = Get-Field $booking 'Lead SW ID'
    $secondSwId = Get-Field $booking '2nd SW ID'
    $bookingDate = Get-Field $booking 'BOOKING DATE'
    $bookingStart = Get-Field $booking 'BOOKING START TIME'
    $bookingFinish = Get-Field $booking 'BOOKING FINISH TIME'

    $neededSlots = @()
    if ([string]::IsNullOrWhiteSpace($leadSwId)) { $neededSlots += "Lead" }
    if ((Test-Yes (Get-Field $booking '2to1 REQUIRED')) -and [string]::IsNullOrWhiteSpace($secondSwId)) { $neededSlots += "Second" }
    if ($neededSlots.Count -eq 0) { continue }

    $childName = Get-Field $booking 'CHILD NAME'
    $childKey = Normalize-Key $childName
    $childGeo = $postcodeMaps.Children[$childKey]
    $adHoc = if ($adHocNotes.ContainsKey($childKey)) { $adHocNotes[$childKey] } else { "" }

    foreach ($slot in $neededSlots) {
        $slotResults = @()
        foreach ($worker in $workers) {
            $workerId = Get-Field $worker 'SUPPORT WORKER ID'
            $workerName = Get-Field $worker 'SUPPORT WORKER NAME'
            if ([string]::IsNullOrWhiteSpace($workerId) -and [string]::IsNullOrWhiteSpace($workerName)) { continue }
            if (-not [string]::IsNullOrWhiteSpace($workerId) -and ($workerId -eq $leadSwId -or $workerId -eq $secondSwId)) { continue }
            if (Worker-IsUnavailable -Worker $worker -Booking $booking -Availability $availability) { continue }

            $explicit = Get-ExplicitMatch -Worker $worker -Booking $booking -Matching $matching
            if ($explicit -match '^(n|no|false|inappropriate)$') { continue }

            $score = 0
            $reasons = @()
            if ($explicit -match '^(y|yes|true|appropriate)$') {
                $score += 40
                $reasons += "explicit appropriate match"
            }

            if ($adHoc -and $adHoc.ToLowerInvariant().Contains($workerName.ToLowerInvariant())) {
                $score += 30
                $reasons += "family/ad hoc notes request this worker"
            }

            if ((Test-Yes (Get-Field $booking 'Car')) -and (Test-Yes (Get-Field $worker 'CAR'))) {
                $score += 15
                $reasons += "car available"
            }

            $workerGeo = $postcodeMaps.Staff[(Normalize-Key $workerName)]
            if ($workerGeo -and $childGeo -and $workerGeo.Area -and $workerGeo.Area -eq $childGeo.Area) {
                $score += 12
                $reasons += "same postcode area $($workerGeo.Area)"
            }

            if (-not [string]::IsNullOrWhiteSpace((Get-Field $worker 'EXPERIENCE'))) {
                $score += 5
                $reasons += "experience noted"
            }

            $workerComments = Get-Field $worker 'COMMENTS'
            if (-not [string]::IsNullOrWhiteSpace($workerComments) -and $workerComments -notmatch 'Fallback candidate') {
                $score += 3
                $reasons += "worker comments available"
            }

            if ($reasons.Count -eq 0) { $reasons += "available; no stronger match signal found" }

            $slotResults += [pscustomobject]@{
                Score = $score
                BookingDate = $bookingDate
                Start = $bookingStart
                Finish = $bookingFinish
                ChildId = $bookingChildId
                ChildName = $childName
                Slot = $slot
                WorkerId = $workerId
                WorkerName = $workerName
                ExplicitMatch = $explicit
                ChildPostcodeArea = if ($childGeo) { $childGeo.Area } else { "" }
                WorkerPostcodeArea = if ($workerGeo) { $workerGeo.Area } else { "" }
                Reasons = ($reasons -join "; ")
                AdHocNotes = $adHoc
            }
        }

        $recommendations += $slotResults |
            Sort-Object @{Expression='Score';Descending=$true}, WorkerName |
            Select-Object -First $TopN
    }
}

$recommendations |
    Sort-Object BookingDate, Start, ChildName, Slot, @{Expression='Score';Descending=$true} |
    Export-Csv -LiteralPath $OutputPath -NoTypeInformation -Encoding UTF8

Write-Output "Wrote $($recommendations.Count) recommendations to $OutputPath"
