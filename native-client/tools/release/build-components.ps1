param([string]$Compiler = "g++", [string]$CCompiler = "gcc")
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$native = Join-Path $root 'native-client'
$ooz = Join-Path $root 'third-party\gooz'
New-Item -ItemType Directory -Force -Path "$native\artifacts" | Out-Null
$decoderArgs = @('-O2', '-msse4.1', '-mssse3', '-static', '-Xlinker', '--nxcompat', '-Xlinker', '--dynamicbase',
    '-I', $ooz, '-o', "$PSScriptRoot\decoder.exe", "$PSScriptRoot\decoder.cpp",
    "$ooz\kraken.cpp", "$ooz\bitknit.cpp", "$ooz\lzna.cpp")
& $Compiler @decoderArgs
if ($LASTEXITCODE -ne 0) { throw 'Decoder compilation failed' }
$adapter = "$native\artifacts\offline-native-adapter.dll"
$adapterArgs = @('-shared', '-O2', '-Wall', '-Wextra', '-Werror', '-static-libgcc',
    '-o', $adapter, "$native\adapter\probe.c", '-lws2_32')
& $CCompiler @adapterArgs
if ($LASTEXITCODE -ne 0) { throw 'Local adapter compilation failed' }
[ordered]@{
    adapter_sha256 = (Get-FileHash -LiteralPath $adapter -Algorithm SHA256).Hash.ToLowerInvariant()
    decoder_sha256 = (Get-FileHash -LiteralPath "$PSScriptRoot\decoder.exe" -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json | Set-Content -LiteralPath "$PSScriptRoot\build-info.json" -Encoding utf8
