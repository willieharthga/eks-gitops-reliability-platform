[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Action = "help"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script:LocalDir = Join-Path $script:RepoRoot "infrastructure\local"
$script:ProjectOwner = "eks-gitops-reliability-platform"
$script:RegistryName = "gitops-registry"
$script:RegistryHost = "localhost"
$script:RegistryHostPort = 5001
$script:RegistryContainerPort = 5000
$script:RegistryUrl = "http://localhost:5001/v2/"
$script:RegistryImage = "registry:3.1.1@sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33"
$script:RegistryOwnerLabelKey = "io.gitops-reliability.platform.owner"
$script:KubeOwnerLabelKey = "platform.gitops-reliability.io/owner"
$script:KindNodeImage = "kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed"
$script:KindWaitTimeout = "120s"
$script:ContainerdRegistryDir = "/etc/containerd/certs.d/localhost:5001"
$script:ContainerdRegistryHost = '[host."http://gitops-registry:5000"]'
$script:Clusters = @("gitops-dev", "gitops-prod")

function Write-PlatformLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "[platform] $Message"
}

function Write-PlatformWarning {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine("[platform] WARNING: $Message")
}

function Show-Usage {
    @"
Usage: scripts/platform.ps1 <up|status|smoke|down|doctor>

  up      Create or reconcile the two owned kind clusters and local registry.
  status  Report registry and cluster health without changing resources.
  smoke   Verify the registry HTTP endpoint and Ready nodes without changes.
  down    Delete only the two owned clusters and the owned registry.
  doctor  Check local prerequisites and reserved-name conflicts without changes.
"@ | Write-Host
}

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Test-DockerReady {
    & docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Assert-PlatformCommands {
    foreach ($commandName in @("docker", "kind", "kubectl")) {
        Assert-Command -Name $commandName
    }
    if (-not (Test-DockerReady)) {
        throw "Docker is installed but its daemon is not reachable"
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$DiscardOutput
    )

    if ($DiscardOutput) {
        & $FilePath @Arguments | Out-Null
    }
    else {
        & $FilePath @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Assert-LocalFiles {
    foreach ($requiredFile in @(
        (Join-Path $script:LocalDir "kind-dev.yaml"),
        (Join-Path $script:LocalDir "kind-prod.yaml"),
        (Join-Path $script:LocalDir "local-registry-hosting.yaml")
    )) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required local configuration is missing: $requiredFile"
        }
    }
}

function Get-ClusterContext {
    param([Parameter(Mandatory = $true)][string]$Cluster)
    return "kind-$Cluster"
}

function Get-ClusterConfig {
    param([Parameter(Mandatory = $true)][string]$Cluster)
    switch ($Cluster) {
        "gitops-dev" { return Join-Path $script:LocalDir "kind-dev.yaml" }
        "gitops-prod" { return Join-Path $script:LocalDir "kind-prod.yaml" }
        default { throw "Refusing unknown cluster name: $Cluster" }
    }
}

function Get-KindClusters {
    $output = @(& kind get clusters 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "kind get clusters failed with exit code $LASTEXITCODE"
    }
    return @($output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -ne "" })
}

function Test-ClusterExists {
    param([Parameter(Mandatory = $true)][string]$Cluster)
    return (Get-KindClusters) -contains $Cluster
}

function Test-RegistryExists {
    & docker container inspect $script:RegistryName *> $null
    return $LASTEXITCODE -eq 0
}

function Get-DockerInspectValue {
    param([Parameter(Mandatory = $true)][string]$Template)
    $output = & docker container inspect --format $Template $script:RegistryName 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect registry container $($script:RegistryName)"
    }
    return ([string]$output).Trim()
}

function Assert-RegistryOwned {
    $actualName = Get-DockerInspectValue -Template '{{.Name}}'
    $actualOwner = Get-DockerInspectValue -Template "{{ index .Config.Labels `"$($script:RegistryOwnerLabelKey)`" }}"
    $actualImage = Get-DockerInspectValue -Template '{{.Config.Image}}'
    $bindingTemplate = "{{with (index (index .HostConfig.PortBindings `"$($script:RegistryContainerPort)/tcp`") 0)}}{{.HostIp}}:{{.HostPort}}{{end}}"
    $actualBinding = Get-DockerInspectValue -Template $bindingTemplate

    if ($actualName -cne "/$($script:RegistryName)") {
        throw "Registry lookup did not resolve the exact reserved name $($script:RegistryName)"
    }
    if ($actualOwner -cne $script:ProjectOwner) {
        throw "Container $($script:RegistryName) exists but is not owned by $($script:ProjectOwner); refusing to modify it"
    }
    if ($actualImage -cne $script:RegistryImage) {
        throw "Owned registry uses $actualImage, expected $($script:RegistryImage); remove it explicitly before changing versions"
    }
    if ($actualBinding -cne "127.0.0.1:$($script:RegistryHostPort)") {
        throw "Owned registry has unexpected port binding $actualBinding; expected 127.0.0.1:$($script:RegistryHostPort)"
    }
}

function Test-RegistryRunning {
    return (Get-DockerInspectValue -Template '{{.State.Running}}') -ceq "true"
}

function Wait-Registry {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-RegistryRunning) {
            $client = [System.Net.Sockets.TcpClient]::new()
            try {
                $connectTask = $client.ConnectAsync("127.0.0.1", $script:RegistryHostPort)
                if ($connectTask.Wait(1000) -and $client.Connected) {
                    return
                }
            }
            catch {
                # The service can briefly refuse connections while starting.
            }
            finally {
                $client.Dispose()
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Registry $($script:RegistryName) did not become reachable on localhost:$($script:RegistryHostPort)"
}

function Ensure-Registry {
    if (Test-RegistryExists) {
        Assert-RegistryOwned
        if (Test-RegistryRunning) {
            Write-PlatformLog "registry $($script:RegistryName) already running"
        }
        else {
            Write-PlatformLog "starting owned registry $($script:RegistryName)"
            Invoke-External -FilePath "docker" -Arguments @("start", $script:RegistryName) -DiscardOutput
        }
    }
    else {
        Write-PlatformLog "creating registry $($script:RegistryName) on localhost:$($script:RegistryHostPort)"
        Invoke-External -FilePath "docker" -Arguments @(
            "run", "--detach",
            "--restart=always",
            "--publish", "127.0.0.1:$($script:RegistryHostPort):$($script:RegistryContainerPort)",
            "--label", "$($script:RegistryOwnerLabelKey)=$($script:ProjectOwner)",
            "--name", $script:RegistryName,
            $script:RegistryImage
        ) -DiscardOutput
    }
    Wait-Registry
}

function Test-ClusterOwned {
    param([Parameter(Mandatory = $true)][string]$Cluster)
    $context = Get-ClusterContext -Cluster $Cluster
    $ownedNodes = @(& kubectl --context $context get nodes `
        --selector "$($script:KubeOwnerLabelKey)=$($script:ProjectOwner)" `
        --output name `
        --request-timeout=5s 2>$null)
    return ($LASTEXITCODE -eq 0) -and ($ownedNodes.Count -gt 0)
}

function Ensure-RegistryNetwork {
    $networkId = Get-DockerInspectValue -Template '{{with index .NetworkSettings.Networks "kind"}}{{.NetworkID}}{{end}}'
    if ([string]::IsNullOrWhiteSpace($networkId)) {
        Write-PlatformLog "connecting $($script:RegistryName) to the kind network"
        Invoke-External -FilePath "docker" -Arguments @("network", "connect", "kind", $script:RegistryName)
    }
}

function Set-NodeRegistryConfiguration {
    param([Parameter(Mandatory = $true)][string]$Cluster)

    $nodes = @(& kind get nodes --name $Cluster 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list kind nodes for cluster $Cluster"
    }
    $nodes = @($nodes | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -ne "" })
    if ($nodes.Count -eq 0) {
        throw "kind returned no nodes for cluster $Cluster"
    }

    foreach ($node in $nodes) {
        Invoke-External -FilePath "docker" -Arguments @(
            "exec", $node, "mkdir", "-p", $script:ContainerdRegistryDir
        ) -DiscardOutput
        $script:ContainerdRegistryHost | & docker exec --interactive $node cp /dev/stdin "$($script:ContainerdRegistryDir)/hosts.toml"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not write containerd registry configuration in node $node"
        }
    }
}

function Install-RegistryDiscovery {
    param([Parameter(Mandatory = $true)][string]$Cluster)
    Invoke-External -FilePath "kubectl" -Arguments @(
        "--context", (Get-ClusterContext -Cluster $Cluster),
        "apply", "--filename", (Join-Path $script:LocalDir "local-registry-hosting.yaml")
    ) -DiscardOutput
}

function Wait-Cluster {
    param([Parameter(Mandatory = $true)][string]$Cluster)
    $context = Get-ClusterContext -Cluster $Cluster
    Invoke-External -FilePath "kubectl" -Arguments @(
        "--context", $context, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=$($script:KindWaitTimeout)"
    ) -DiscardOutput
    Invoke-External -FilePath "kubectl" -Arguments @(
        "--context", $context, "rollout", "status", "--namespace", "kube-system", "deployment/coredns", "--timeout=$($script:KindWaitTimeout)"
    ) -DiscardOutput
}

function Ensure-Cluster {
    param([Parameter(Mandatory = $true)][string]$Cluster)

    if (Test-ClusterExists -Cluster $Cluster) {
        if (-not (Test-ClusterOwned -Cluster $Cluster)) {
            throw "Cluster $Cluster exists but lacks this project's ownership label; refusing to adopt it"
        }
        Write-PlatformLog "cluster $Cluster already exists"
    }
    else {
        Write-PlatformLog "creating cluster $Cluster"
        Invoke-External -FilePath "kind" -Arguments @(
            "create", "cluster",
            "--name", $Cluster,
            "--image", $script:KindNodeImage,
            "--config", (Get-ClusterConfig -Cluster $Cluster),
            "--wait", $script:KindWaitTimeout
        )
    }

    Ensure-RegistryNetwork
    Set-NodeRegistryConfiguration -Cluster $Cluster
    Install-RegistryDiscovery -Cluster $Cluster
    Wait-Cluster -Cluster $Cluster
    Write-PlatformLog "cluster $Cluster is ready"
}

function Start-Platform {
    Assert-PlatformCommands
    Assert-LocalFiles
    Ensure-Registry
    foreach ($cluster in $script:Clusters) {
        Ensure-Cluster -Cluster $cluster
    }
    Write-PlatformLog "local platform is ready"
}

function Get-PlatformStatus {
    Assert-PlatformCommands
    $unhealthy = $false

    if (-not (Test-RegistryExists)) {
        Write-Output "registry/$($script:RegistryName) missing"
        $unhealthy = $true
    }
    else {
        try {
            Assert-RegistryOwned
            if (Test-RegistryRunning) {
                Write-Output "registry/$($script:RegistryName) running localhost:$($script:RegistryHostPort)"
            }
            else {
                Write-Output "registry/$($script:RegistryName) stopped"
                $unhealthy = $true
            }
        }
        catch {
            Write-PlatformWarning $_.Exception.Message
            $unhealthy = $true
        }
    }

    foreach ($cluster in $script:Clusters) {
        $context = Get-ClusterContext -Cluster $cluster
        if (-not (Test-ClusterExists -Cluster $cluster)) {
            Write-Output "cluster/$cluster missing"
            $unhealthy = $true
        }
        elseif (-not (Test-ClusterOwned -Cluster $cluster)) {
            Write-Output "cluster/$cluster unowned-or-unreachable"
            $unhealthy = $true
        }
        else {
            & kubectl --context $context get --raw=/readyz --request-timeout=5s *> $null
            if ($LASTEXITCODE -eq 0) {
                Write-Output "cluster/$cluster ready"
            }
            else {
                Write-Output "cluster/$cluster not-ready"
                $unhealthy = $true
            }
        }
    }

    if ($unhealthy) {
        throw "One or more local platform resources are unhealthy"
    }
}

function Test-PlatformSmoke {
    Assert-PlatformCommands
    $uri = $script:RegistryUrl
    try {
        $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 5
    }
    catch {
        throw "Registry API did not respond successfully at $uri`: $($_.Exception.Message)"
    }
    if ($response.StatusCode -ne 200) {
        throw "Registry API returned HTTP $($response.StatusCode) at $uri"
    }
    Write-Output "registry/$($script:RegistryName) api-ready"

    foreach ($cluster in $script:Clusters) {
        $context = Get-ClusterContext -Cluster $cluster
        $nodeLines = @(& kubectl --context $context get nodes --no-headers --request-timeout=5s 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Could not get nodes from $cluster"
        }
        if ($nodeLines.Count -eq 0) {
            throw "Cluster $cluster reported no nodes"
        }
        foreach ($line in $nodeLines) {
            $columns = ([string]$line).Trim() -split "\s+"
            if (($columns.Count -lt 2) -or ($columns[1] -notmatch '^Ready(?:,|$)')) {
                throw "Cluster $cluster has a node that is not Ready: $line"
            }
        }
        Write-Output "cluster/$cluster nodes-ready"
    }
}

function Stop-Platform {
    Assert-PlatformCommands
    $failure = $false

    for ($index = $script:Clusters.Count - 1; $index -ge 0; $index--) {
        $cluster = $script:Clusters[$index]
        if (-not (Test-ClusterExists -Cluster $cluster)) {
            Write-PlatformLog "cluster $cluster already absent"
        }
        elseif (-not (Test-ClusterOwned -Cluster $cluster)) {
            Write-PlatformWarning "cluster $cluster is not verifiably owned; refusing to delete it"
            $failure = $true
        }
        else {
            Write-PlatformLog "deleting owned cluster $cluster"
            Invoke-External -FilePath "kind" -Arguments @("delete", "cluster", "--name", $cluster)
        }
    }

    if ($failure) {
        throw "Leaving the registry in place because at least one reserved cluster was not deleted"
    }

    if (Test-RegistryExists) {
        Assert-RegistryOwned
        Write-PlatformLog "deleting owned registry $($script:RegistryName)"
        Invoke-External -FilePath "docker" -Arguments @("container", "rm", "--force", $script:RegistryName) -DiscardOutput
    }
    else {
        Write-PlatformLog "registry $($script:RegistryName) already absent"
    }
    Write-PlatformLog "local platform is down"
}

function Test-PlatformDoctor {
    $failed = $false
    foreach ($commandName in @("docker", "kind", "kubectl")) {
        if ($null -ne (Get-Command $commandName -ErrorAction SilentlyContinue)) {
            Write-Output "tool/$commandName found"
        }
        else {
            Write-Output "tool/$commandName missing"
            $failed = $true
        }
    }

    foreach ($requiredFile in @(
        (Join-Path $script:LocalDir "kind-dev.yaml"),
        (Join-Path $script:LocalDir "kind-prod.yaml"),
        (Join-Path $script:LocalDir "local-registry-hosting.yaml")
    )) {
        $leafName = Split-Path -Leaf $requiredFile
        if (Test-Path -LiteralPath $requiredFile -PathType Leaf) {
            Write-Output "config/$leafName found"
        }
        else {
            Write-Output "config/$leafName missing"
            $failed = $true
        }
    }

    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -ne $dockerCommand) {
        if (Test-DockerReady) {
            Write-Output "docker/daemon reachable"
            if (Test-RegistryExists) {
                try {
                    Assert-RegistryOwned
                    Write-Output "reserved-name/$($script:RegistryName) owned"
                }
                catch {
                    Write-PlatformWarning $_.Exception.Message
                    $failed = $true
                }
            }
        }
        else {
            Write-Output "docker/daemon unreachable"
            $failed = $true
        }
    }

    $kindCommand = Get-Command kind -ErrorAction SilentlyContinue
    $kubectlCommand = Get-Command kubectl -ErrorAction SilentlyContinue
    if (($null -ne $dockerCommand) -and ($null -ne $kindCommand) -and ($null -ne $kubectlCommand) -and (Test-DockerReady)) {
        foreach ($cluster in $script:Clusters) {
            if (Test-ClusterExists -Cluster $cluster) {
                if (Test-ClusterOwned -Cluster $cluster) {
                    Write-Output "reserved-name/$cluster owned"
                }
                else {
                    Write-Output "reserved-name/$cluster conflict"
                    $failed = $true
                }
            }
        }
    }

    if ($failed) {
        throw "One or more prerequisite or ownership checks failed"
    }
}

switch ($Action.ToLowerInvariant()) {
    "up" { Start-Platform }
    "status" { Get-PlatformStatus }
    "smoke" { Test-PlatformSmoke }
    "down" { Stop-Platform }
    "doctor" { Test-PlatformDoctor }
    { $_ -in @("help", "-h", "--help", "") } { Show-Usage }
    default {
        Show-Usage
        throw "Unknown action: $Action"
    }
}
