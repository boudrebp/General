param(
    [Parameter(Mandatory = $true)]
    [string]$Endpoint,

    [string]$TemplatePath = "Sample MeterException Payload",
    [string]$EventsPath = "meter_events.txt",
    [string]$TokenUrl,
    [string]$TokenClientId,
    [string]$TokenClientSecret,
    [string]$TokenProjectId,
    [string]$TokenScope,
    [string]$TokenGrantType = "client_credentials",
    [string[]]$Header,
    [int]$TimeoutSec = 20,
    [int]$DelayMs = 0,
    [switch]$DryRun,
    [switch]$StopOnError
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NowIsoUtc {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Parse-Headers {
    param([string[]]$Items)

    $headers = @{}
    if (-not $Items) {
        return $headers
    }

    foreach ($item in $Items) {
        if ($item -notmatch ":") {
            throw "Invalid header format (expected Key:Value): $item"
        }
        $parts = $item -split ":", 2
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        $headers[$key] = $value
    }

    return $headers
}

function Get-BearerToken {
    param(
        [string]$Url,
        [string]$ClientId,
        [string]$ClientSecret,
        [string]$Scope,
        [string]$GrantType,
        [int]$Timeout
    )

    $body = @{
        grant_type    = $GrantType
        client_id     = $ClientId
        client_secret = $ClientSecret
    }

    if (-not [string]::IsNullOrWhiteSpace($Scope)) {
        $body.scope = $Scope
    }

    try {
        $response = Invoke-RestMethod -Uri $Url -Method POST -ContentType "application/x-www-form-urlencoded" -Body $body -TimeoutSec $Timeout -ErrorAction Stop
    }
    catch {
        throw "Token request failed: $($_.Exception.Message)"
    }

    if ($null -eq $response -or [string]::IsNullOrWhiteSpace($response.access_token)) {
        throw "Token response does not contain a valid access_token"
    }

    return [string]$response.access_token
}

function Parse-EventLine {
    param(
        [string]$Line,
        [int]$LineNumber
    )

    $raw = $Line.Trim()
    if ([string]::IsNullOrWhiteSpace($raw) -or $raw.StartsWith("#")) {
        return $null
    }

    $delimiter = ","
    if ($raw.Contains("|")) {
        $delimiter = "|"
    }

    $parts = $raw.Split($delimiter) | ForEach-Object { $_.Trim() }
    if ($parts.Count -lt 3) {
        throw "Line $LineNumber: expected meter_id,timestamp,name[,outage_id]"
    }

    $meterId = $parts[0]
    $timestamp = $parts[1]

    $name = $null
    $outageId = $null

    if ($parts.Count -eq 3) {
        $name = $parts[2]
    }
    else {
        if ($parts.Count -gt 4) {
            $name = ($parts[2..($parts.Count - 2)] -join $delimiter).Trim()
        }
        else {
            $name = $parts[2]
        }
        $outageId = $parts[$parts.Count - 1]
        if ([string]::IsNullOrWhiteSpace($outageId)) {
            $outageId = $null
        }
    }

    if ([string]::IsNullOrWhiteSpace($meterId) -or
        [string]::IsNullOrWhiteSpace($timestamp) -or
        [string]::IsNullOrWhiteSpace($name)) {
        throw "Line $LineNumber: meter_id, timestamp, and name are required"
    }

    return [PSCustomObject]@{
        MeterId = $meterId
        Timestamp = $timestamp
        Name = $name
        OutageId = $outageId
    }
}

function Resolve-IdFromName {
    param([string]$Name)

    switch ($Name.Trim().ToLowerInvariant()) {
        "primary power down" { return "18001" }
        "primary power up" { return "18002" }
        default {
            throw "Name must be either 'Primary Power Up' or 'Primary Power Down'"
        }
    }
}

function Build-PayloadJson {
    param(
        [string]$TemplateJson,
        [string]$MeterId,
        [string]$Timestamp,
        [string]$Name,
        [string]$OutageId
    )

    $json = $TemplateJson.Replace("{{meter_id}}", $MeterId).Replace("{{$isoTimestamp}}", $Timestamp)
    $payload = $json | ConvertFrom-Json -Depth 100

    $id = Resolve-IdFromName -Name $Name
    $meterException = $payload."s:Envelope"."s:Body".ExceptionsArrived.input.MeterExceptionCollection.MeterException[0]

    $meterException.Name = $Name
    $meterException.ID = $id

    if (-not [string]::IsNullOrWhiteSpace($OutageId)) {
        $meterException.Arguments."a:Argument"."a:Value" = $OutageId
    }

    return ($payload | ConvertTo-Json -Depth 100 -Compress)
}

$success = 0
$failed = 0

try {
    $templateJson = Get-Content -Path $TemplatePath -Raw -Encoding UTF8
    $headers = Parse-Headers -Items $Header
    if (-not [string]::IsNullOrWhiteSpace($TokenUrl)) {
        if ([string]::IsNullOrWhiteSpace($TokenClientId) -or [string]::IsNullOrWhiteSpace($TokenClientSecret)) {
            throw "When -TokenUrl is set, -TokenClientId and -TokenClientSecret are required"
        }

        $resolvedScope = $TokenScope
        if (-not [string]::IsNullOrWhiteSpace($resolvedScope) -and $resolvedScope.Contains("{{projectID}}")) {
            if ([string]::IsNullOrWhiteSpace($TokenProjectId)) {
                throw "-TokenProjectId is required when -TokenScope contains {{projectID}}"
            }
            $resolvedScope = $resolvedScope.Replace("{{projectID}}", $TokenProjectId)
        }

        $token = Get-BearerToken -Url $TokenUrl -ClientId $TokenClientId -ClientSecret $TokenClientSecret -Scope $resolvedScope -GrantType $TokenGrantType -Timeout $TimeoutSec
        $headers["Authorization"] = "Bearer $token"
        Write-Host "Token acquired successfully; using bearer authorization header"
    }
    $lines = Get-Content -Path $EventsPath -Encoding UTF8
}
catch {
    Write-Error "Setup error: $($_.Exception.Message)"
    exit 2
}

for ($i = 0; $i -lt $lines.Count; $i++) {
    $lineNumber = $i + 1

    try {
        $event = Parse-EventLine -Line $lines[$i] -LineNumber $lineNumber
    }
    catch {
        $failed++
        Write-Error $_.Exception.Message
        if ($StopOnError) { break }
        continue
    }

    if ($null -eq $event) {
        continue
    }

    $meterId = $event.MeterId
    $timestamp = $event.Timestamp
    $name = $event.Name
    $outageId = $event.OutageId

    if ($timestamp.Trim().ToLowerInvariant() -eq "now") {
        $timestamp = Get-NowIsoUtc
    }

    try {
        $payloadJson = Build-PayloadJson `
            -TemplateJson $templateJson `
            -MeterId $meterId `
            -Timestamp $timestamp `
            -Name $name `
            -OutageId $outageId
    }
    catch {
        $failed++
        Write-Error "Line $lineNumber: payload build error: $($_.Exception.Message)"
        if ($StopOnError) { break }
        continue
    }

    if ($DryRun) {
        Write-Host "Line $lineNumber: DRY RUN meter_id=$meterId name=$name"
        Write-Host $payloadJson
        $success++
    }
    else {
        try {
            $invokeParams = @{
                Uri         = $Endpoint
                Method      = "POST"
                ContentType = "application/json"
                Body        = $payloadJson
                TimeoutSec  = $TimeoutSec
                ErrorAction = "Stop"
            }

            if ($headers.Count -gt 0) {
                $invokeParams.Headers = $headers
            }

            $null = Invoke-RestMethod @invokeParams
            Write-Host "Line $lineNumber: sent meter_id=$meterId name=$name status=success"
            $success++
        }
        catch {
            $failed++
            Write-Error "Line $lineNumber: failed meter_id=$meterId name=$name error=$($_.Exception.Message)"
            if ($StopOnError) { break }
        }
    }

    if ($DelayMs -gt 0) {
        Start-Sleep -Milliseconds $DelayMs
    }
}

Write-Host "Complete: success=$success failed=$failed"
if ($failed -gt 0) {
    exit 1
}

exit 0
