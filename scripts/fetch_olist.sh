#!/usr/bin/env bash
# Downloads the Olist Brazilian E-Commerce dataset into data/raw and verifies it.
# Requires the Kaggle CLI (uv tool install kaggle) and a Kaggle API token in ~/.kaggle/kaggle.json
# or the KAGGLE_USERNAME / KAGGLE_KEY environment variables.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
raw="$root/data/raw"
mkdir -p "$raw"

kaggle datasets download -d olistbr/brazilian-ecommerce -p "$raw" --unzip

expected=(
  olist_customers_dataset.csv olist_geolocation_dataset.csv olist_order_items_dataset.csv
  olist_order_payments_dataset.csv olist_order_reviews_dataset.csv olist_orders_dataset.csv
  olist_products_dataset.csv olist_sellers_dataset.csv product_category_name_translation.csv
)
for f in "${expected[@]}"; do
  [[ -f "$raw/$f" ]] || { echo "Missing file: $f" >&2; exit 1; }
done
(cd "$raw" && sha256sum "${expected[@]}" > SHA256SUMS.txt)
echo "Olist dataset ready in $raw (${#expected[@]} files; checksums in SHA256SUMS.txt)"
