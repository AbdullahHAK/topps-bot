# Builds dist\topps-bot-extension.zip for the client.
$root = $PSScriptRoot
New-Item -ItemType Directory -Force "$root\dist" | Out-Null
Compress-Archive -Path "$root\extension\*" -DestinationPath "$root\dist\topps-bot-extension.zip" -Force
Write-Output "Built dist\topps-bot-extension.zip"
