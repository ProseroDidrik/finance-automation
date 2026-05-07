"""Pusha _params/Dotterbolagslista.xlsx till Azure Blob Storage.

Använd när du har uppdaterat Dotterbolagslistan lokalt och behöver att
molnsidan (webapp / scheduled jobs) ser den nya versionen. Master-filen
ligger i Dropbox lokalt (gitignored) och replikeras till Blob:en på
manuell push via det här skriptet.

Kör:
    py scripts/push_master.py            # default-path och env-styrt URL
    py scripts/push_master.py --dry-run

Konfiguration:
    MASTER_BLOB_URL    — full URL till mål-blobben. Auth via
                         DefaultAzureCredential (az login lokalt eller
                         Managed Identity om det skulle köras i en
                         GitHub Action).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOCAL = REPO / "_params" / "Dotterbolagslista.xlsx"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=DEFAULT_LOCAL,
                    help="Lokal fil att pusha. Default: _params/Dotterbolagslista.xlsx")
    ap.add_argument("--dry-run", action="store_true",
                    help="Visa vad som skulle pushas men ladda inte upp.")
    args = ap.parse_args()

    src: Path = args.source
    if not src.exists():
        sys.exit(f"FEL: källfilen saknas: {src}")

    blob_url = os.environ.get("MASTER_BLOB_URL")
    if not blob_url:
        sys.exit(
            "FEL: MASTER_BLOB_URL är inte satt. Ex:\n"
            "  $env:MASTER_BLOB_URL = 'https://<acct>.blob.core.windows.net/<container>/Dotterbolagslista.xlsx'"
        )

    size = src.stat().st_size
    print(f"Källa: {src}  ({size:,} bytes)")
    print(f"Mål:   {blob_url}")
    if args.dry_run:
        print("[dry-run] hoppar över upload.")
        return 0

    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobClient
    except ImportError as e:
        sys.exit(f"FEL: azure-identity / azure-storage-blob saknas: {e}")

    cred = DefaultAzureCredential()
    client = BlobClient.from_blob_url(blob_url, credential=cred)
    with open(src, "rb") as f:
        client.upload_blob(f, overwrite=True)
    print(f"OK: pushade {size:,} bytes till {blob_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
