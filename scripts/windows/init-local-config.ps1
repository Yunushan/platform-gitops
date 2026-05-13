$ErrorActionPreference = "Stop"

function Copy-IfMissing($Source, $Destination) {
  if (!(Test-Path $Destination)) {
    Copy-Item $Source $Destination
    Write-Host "created $Destination"
  } else {
    Write-Host "kept existing $Destination"
  }
}

Copy-IfMissing "config/cluster.example.yaml" "config/cluster.local.yaml"
Copy-IfMissing "inventory/hosts.example.ini" "inventory/hosts.local.ini"
Write-Host "Edit only .local files. They are ignored by git."
