# Downloads the Olist Brazilian E-Commerce dataset into data/raw and verifies it.
# Requires the Kaggle CLI (uv tool install kaggle) and a Kaggle API token in
# %USERPROFILE%\.kaggle\kaggle.json or the KAGGLE_USERNAME / KAGGLE_KEY environment variables.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$raw = Join-Path $root "data\raw"
New-Item -ItemType Directory -Force -Path $raw | Out-Null

kaggle datasets download -d olistbr/brazilian-ecommerce -p $raw --unzip
if ($LASTEXITCODE -ne 0) { throw "kaggle download failed (exit $LASTEXITCODE)" }

$expected = @(
  "olist_customers_dataset.csv", "olist_geolocation_dataset.csv", "olist_order_items_dataset.csv",
  "olist_order_payments_dataset.csv", "olist_order_reviews_dataset.csv", "olist_orders_dataset.csv",
  "olist_products_dataset.csv", "olist_sellers_dataset.csv", "product_category_name_translation.csv"
)
$missing = $expected | Where-Object { -not (Test-Path (Join-Path $raw $_)) }
if ($missing) { throw "Missing files: $($missing -join ', ')" }

$sums = $expected | ForEach-Object {
  $hash = (Get-FileHash -Algorithm SHA256 -Path (Join-Path $raw $_)).Hash.ToLower()
  "$hash  $_"
}
$sums | Set-Content -Encoding ascii -Path (Join-Path $raw "SHA256SUMS.txt")
Write-Host "Olist dataset ready in $raw ($($expected.Count) files; checksums in SHA256SUMS.txt)"
