# 1-KUR.ps1
# Ne yapar: Sanal ortam (.venv) yoksa kurar, tüm Python paketlerini ve tarayıcıyı
# indirir. Ben bunu senin için ZATEN çalıştırdım; sadece bilgisayar değişirse veya
# bir şey bozulursa tekrar çalıştırman gerekir.
# Çalıştırmak için: dosyaya sağ tık > "PowerShell ile çalıştır"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

try {
    Set-Location -Path $PSScriptRoot

    Write-Host "== Sanal ortam hazirlaniyor ==" -ForegroundColor Cyan
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
    }

    $py = ".\.venv\Scripts\python.exe"

    Write-Host "== Paketler kuruluyor (biraz surebilir) ==" -ForegroundColor Cyan
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt

    Write-Host "== Tarayici (Chromium) indiriliyor ==" -ForegroundColor Cyan
    & $py -m playwright install chromium

    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "== .env olusturuldu (GEMINI_API_KEY'i sonra doldur) ==" -ForegroundColor Yellow
    }

    Write-Host "`nKURULUM TAMAM. Siradaki adim: .\3-TEST-ET.ps1" -ForegroundColor Green
}
catch {
    Write-Host "`nBEKLENMEDIK HATA: $_" -ForegroundColor Red
}
finally {
    Write-Host ""
    Read-Host "Bu pencereyi kapatmak icin ENTER'a bas"
}
