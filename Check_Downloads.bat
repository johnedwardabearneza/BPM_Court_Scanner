@echo off
title BPM Court Scanner - Live Download Stats
cls
echo ========================================================
echo        BPM Court Scanner - Live Download Counter
echo ========================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; try { $data = (Invoke-RestMethod -Uri 'https://api.github.com/repos/QWARDZ/BPM-Court-Scanner/releases' -Headers @{'User-Agent'='BPM-Scanner'}); $total = 0; foreach ($rel in $data) { Write-Host ('Release: ' + $rel.tag_name) -ForegroundColor Cyan; foreach ($asset in $rel.assets) { Write-Host ('  - ' + $asset.name.PadRight(26) + ': ' + $asset.download_count + ' download(s)') -ForegroundColor Green; $total += $asset.download_count } }; Write-Host ''; Write-Host '========================================================' -ForegroundColor Gray; Write-Host ('  TOTAL DOWNLOADS: ' + $total) -ForegroundColor Yellow; Write-Host '========================================================' -ForegroundColor Gray; } catch { Write-Host ('Failed to fetch stats: ' + $_.Exception.Message) -ForegroundColor Red }"
echo.
pause

