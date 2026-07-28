# 3-TEST-ET.ps1
# Ne yapar: HİÇBİR ŞEY PAYLAŞMADAN sistemi test eder. Haberleri çeker, metni
# üretir ve "şunu paylaşırdım" diye ekrana yazar. Güvenli — canlı paylaşım yapmaz.
# Çalıştırmak için: dosyaya sağ tık > "PowerShell ile çalıştır"
#   ya da terminalde: .\3-TEST-ET.ps1   (farklı profil: .\3-TEST-ET.ps1 spor)

param(
    [string]$Profile = "haber"
)

# Türkçe karakterler düzgün görünsün diye konsol kodlamasını ayarla.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

try {
    Set-Location -Path $PSScriptRoot

    $py = ".\.venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Write-Host "Once .\1-KUR.ps1 calistir." -ForegroundColor Red
        return
    }

    Write-Host "== TEST: '$Profile' profili (paylasim YAPILMAZ) ==" -ForegroundColor Cyan
    $env:PYTHONPATH = $PSScriptRoot
    & $py "main.py" --profile $Profile --dry-run --ignore-schedule --limit 1

    if ($LASTEXITCODE -eq 0) {
        Write-Host "`nTEST BASARILI (yukarida 'Paylasilacakti' satirini gor)." -ForegroundColor Green
    } else {
        Write-Host "`nTEST HATASI (cikis kodu $LASTEXITCODE)." -ForegroundColor Red
    }
}
catch {
    Write-Host "`nBEKLENMEDIK HATA: $_" -ForegroundColor Red
}
finally {
    # Pencere hemen kapanmasin ki yazilari okuyabilesin.
    Write-Host ""
    Read-Host "Bu pencereyi kapatmak icin ENTER'a bas"
}
