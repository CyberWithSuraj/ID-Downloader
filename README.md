# ID-PDF Downloader

A Linux-oriented Python utility for **authorized security testing and controlled assessment environments** where a web application exposes PDF content through an ID-based endpoint.

The tool accepts one or more IDs, requests the corresponding endpoint, validates downloaded content as PDF, and keeps a local SQLite ledger so successful downloads can be skipped on later runs. If the direct HTTP request does not return a usable PDF, it can fall back to a headless Chromium/Playwright workflow.

> **Authorization required:** Use this project only against applications, accounts, datasets, and systems that you own or have explicit permission to test. Do not use it to access another person's private documents or to enumerate unauthorized IDs.

## Features

- Single-ID, file-based, and range-based input
- Concurrent processing with a configurable worker limit
- Direct HTTP download with a browser fallback
- Basic PDF integrity validation
- SQLite download ledger
- Graceful handling of `SIGINT`/`SIGTERM`
- Retry handling for browser-based retrieval
- ZIP bundle generation
- Combined PDF generation
- Rich terminal progress and summary output

## Project Structure

```text
ID-PDF-Downloader/
├── pdf_engine.py
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
└── downloaded_pdfs/        # generated at runtime, not committed
    └── ...
```

Runtime-generated files/directories are intentionally excluded from Git:

```text
downloaded_pdfs/
archives/
download_ledger.db
failed_ids.txt
```

## Requirements

- Linux/macOS/Windows with Python 3.10+
- Python packages listed in `requirements.txt`
- Chromium browser installed for the Playwright fallback

## Installation

```bash
git clone <YOUR-GITHUB-REPOSITORY-URL>
cd ID-PDF-Downloader

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

On Kali Linux, use your normal Python/virtual-environment workflow.

## Configuration

Before running the tool, review the `BASE_URL` value in `pdf_engine.py` and replace the example endpoint with an endpoint belonging to your **authorized test environment**.

Do not commit real production URLs, credentials, session tokens, API keys, cookies, or private data to the repository.

## Usage

### Single ID

```bash
python3 pdf_engine.py --id 123
```

### IDs from a text file

Create a file such as `ids.txt`:

```text
101
102
103
```

Then:

```bash
python3 pdf_engine.py --file ids.txt
```

### ID range

For a controlled lab or authorized assessment:

```bash
python3 pdf_engine.py --range 100 120
```

### Control concurrency

```bash
python3 pdf_engine.py --file ids.txt --threads 4
```

Use a conservative worker count and follow the target application's testing rules and rate limits.

## Output

Successful PDF files are written to:

```text
downloaded_pdfs/
```

Post-processing creates:

```text
archives/PDF_Bundle.zip
archives/Master_Combined.pdf
```

The SQLite ledger is:

```text
download_ledger.db
```

This ledger allows already-successful downloads to be skipped when the corresponding valid PDF is still present.

## How It Works

The processing flow is:

```text
Input IDs
   │
   ▼
SQLite ledger check
   │
   ├── already downloaded ──► skip
   │
   ▼
Direct HTTP request
   │
   ├── valid PDF ───────────► save + record success
   │
   ▼
Playwright fallback
   │
   ├── valid PDF ───────────► save + record success
   │
   ▼
Retry / failure queue
   │
   ▼
ZIP + combined PDF post-processing
```

## Security / Responsible Use

This repository is intended for:

- authorized penetration testing
- internal security assessments
- CTFs and intentionally vulnerable labs
- applications where you have explicit permission to test

It must **not** be used to:

- access documents belonging to other users
- bypass authentication or authorization without permission
- enumerate production records without authorization
- download personal, financial, academic, medical, or other confidential information
- overload a target with excessive requests

If an ID-based endpoint exposes another user's document, stop testing once the issue is confirmed according to the applicable rules of engagement and follow the responsible-disclosure process.

## Reporting an Authorization Issue

For a legitimate assessment, record only the minimum evidence required to demonstrate the finding. Avoid publishing real personal information or downloaded confidential documents in screenshots, GitHub issues, reports, or social-media posts.

## Disclaimer

This software is provided for legitimate security testing and research. The author is not responsible for misuse of the tool or for unauthorized access to systems or data.

## License

See [`LICENSE`](LICENSE).
