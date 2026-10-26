param(
    [string]$ProjectId = "ceo-dev123",
    [string]$Region = "us-central1",
    [string]$Repository = "ceosystem",
    [string]$StackName = "",
    [string]$GatewayServiceName = "",
    [string]$WorkerServiceName = "",
    [string]$Tag = "latest"
)


$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($StackName)) {
    $StackName = Split-Path -Leaf $repositoryRoot
}
if ($StackName -notmatch '^[a-z][a-z0-9-]*$') {
    throw "StackName must contain only lowercase letters, digits, and hyphens, and start with a letter."
}
if ([string]::IsNullOrWhiteSpace($GatewayServiceName)) {
    $GatewayServiceName = "$StackName-gateway"
}
if ([string]::IsNullOrWhiteSpace($WorkerServiceName)) {
    $WorkerServiceName = "$StackName-persistence-worker"
}

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

Require-Command docker

$gatewayImage = "$Region-docker.pkg.dev/$ProjectId/$Repository/$GatewayServiceName`:$Tag"
$workerImage = "$Region-docker.pkg.dev/$ProjectId/$Repository/$WorkerServiceName`:$Tag"

Write-Host "Building gateway image: $gatewayImage"
docker build -f services/agent_gateway_v3/Dockerfile -t $gatewayImage .

Write-Host "Pushing gateway image: $gatewayImage"
docker push $gatewayImage

Write-Host "Building worker image: $workerImage"
docker build -f services/agent_persistence_worker_v3/Dockerfile -t $workerImage .

Write-Host "Pushing worker image: $workerImage"
docker push $workerImage

[pscustomobject]@{
    gateway_image = $gatewayImage
    worker_image  = $workerImage
}
