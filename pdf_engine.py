#!/usr/bin/env python3

import argparse
import asyncio
import gc
import os
import signal
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

from playwright.async_api import async_playwright
import httpx
from pypdf import PdfWriter

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table


# ==============================================================
# SYSTEM CONFIGURATION & PATHS
# ==============================================================

# URL is entered interactively when running:
# python3 pdf_engine.py
#
# Example:
# https://example.com/path/file.php?id=
BASE_URL = ""

OUTPUT_DIR = Path("downloaded_pdfs")
ARCHIVE_DIR = Path("archives")
DB_FILE = Path("download_ledger.db")
FAILED_LOG = Path("failed_ids.txt")

MAX_RETRIES = 5
TIMEOUT_SEC = 30.0

console = Console()
SHUTDOWN_REQUESTED = False


# ==============================================================
# LINUX SIGNAL HANDLER
# ==============================================================

def setup_signal_handlers():
    def linux_signal_handler(sig, frame):
        global SHUTDOWN_REQUESTED

        console.print(
            "\n[bold red][!] Intercepted Interrupt Signal "
            "(SIGINT/SIGTERM). Graceful Stopping...[/bold red]"
        )

        SHUTDOWN_REQUESTED = True

    signal.signal(signal.SIGINT, linux_signal_handler)
    signal.signal(signal.SIGTERM, linux_signal_handler)


# ==============================================================
# SQLITE WAL LEDGER
# ==============================================================

class LinuxDatabaseLedger:

    def __init__(self, db_path=DB_FILE):
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False
        )
        self.init_db()

    def init_db(self):
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pdf_ledger (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    file_path TEXT,
                    file_size INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def is_downloaded(self, id_val: str) -> bool:
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT status
            FROM pdf_ledger
            WHERE id = ?
            AND status = 'SUCCESS'
            """,
            (id_val,),
        )

        return cursor.fetchone() is not None

    def record_status(
        self,
        id_val: str,
        status: str,
        path: str = "",
        size: int = 0
    ):
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO pdf_ledger
                (id, status, file_path, file_size)
                VALUES (?, ?, ?, ?)
                """,
                (id_val, status, path, size),
            )


ledger = LinuxDatabaseLedger()


# ==============================================================
# HELPER INTEGRITY FUNCTIONS
# ==============================================================

def is_valid_pdf(file_path: Path) -> bool:

    if not file_path.exists():
        return False

    if file_path.stat().st_size < 500:
        return False

    try:
        with open(file_path, "rb") as f:
            header = f.read(5)

        return header.startswith(b"%PDF-")

    except Exception:
        return False


# ==============================================================
# DIRECT HTTP STREAM
# ==============================================================

async def direct_http_stream(
    client: httpx.AsyncClient,
    id_value: str,
    output_file: Path
) -> bool:

    url = BASE_URL.format(id_value)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
    }

    try:

        res = await client.get(
            url,
            headers=headers,
            follow_redirects=True
        )

        if res.status_code == 200 and len(res.content) > 500:

            output_file.write_bytes(res.content)

            if is_valid_pdf(output_file):
                return True

            output_file.unlink(missing_ok=True)

    except Exception:
        pass

    return False


# ==============================================================
# WORKER PIPELINE
# ==============================================================

async def download_worker(
    context,
    http_client,
    id_value: str,
    semaphore: asyncio.Semaphore,
    progress,
    task_id,
    stats,
    retry_queue: list
):

    global SHUTDOWN_REQUESTED

    if SHUTDOWN_REQUESTED:
        return

    async with semaphore:

        output_file = OUTPUT_DIR / f"id_{id_value}.pdf"

        # ------------------------------------------------------
        # 1. Skip if already downloaded
        # ------------------------------------------------------

        if (
            ledger.is_downloaded(id_value)
            and is_valid_pdf(output_file)
        ):

            stats["skipped"] += 1

            progress.update(
                task_id,
                advance=1
            )

            return

        # ------------------------------------------------------
        # 2. Fast HTTP Stream
        # ------------------------------------------------------

        if await direct_http_stream(
            http_client,
            id_value,
            output_file
        ):

            stats["success"] += 1

            ledger.record_status(
                id_value,
                "SUCCESS",
                str(output_file),
                output_file.stat().st_size
            )

            progress.update(
                task_id,
                advance=1
            )

            return

        # ------------------------------------------------------
        # 3. Headless Browser Fallback
        # ------------------------------------------------------

        page = await context.new_page()

        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,css,woff2}",
            lambda route: route.abort()
        )

        download_success = False

        for attempt in range(1, MAX_RETRIES + 1):

            if SHUTDOWN_REQUESTED:
                break

            try:

                response = await page.goto(
                    BASE_URL.format(id_value),
                    wait_until="domcontentloaded",
                    timeout=int(TIMEOUT_SEC * 1000)
                )

                if response and response.status == 200:

                    await page.pdf(
                        path=str(output_file),
                        format="A4",
                        print_background=True,
                        margin={
                            "top": "5mm",
                            "bottom": "5mm",
                            "left": "5mm",
                            "right": "5mm",
                        },
                    )

                    if is_valid_pdf(output_file):

                        stats["success"] += 1

                        ledger.record_status(
                            id_value,
                            "SUCCESS",
                            str(output_file),
                            output_file.stat().st_size
                        )

                        download_success = True
                        break

                    else:

                        output_file.unlink(
                            missing_ok=True
                        )

                await asyncio.sleep(
                    1.2 ** attempt
                )

            except Exception:

                await asyncio.sleep(
                    1.2 ** attempt
                )

        await page.close()

        if (
            not download_success
            and not SHUTDOWN_REQUESTED
        ):
            retry_queue.append(id_value)

        progress.update(
            task_id,
            advance=1
        )


# ==============================================================
# POST-PROCESSING BUNDLER
# ==============================================================

def linux_post_processing(id_list: list):

    ARCHIVE_DIR.mkdir(
        exist_ok=True
    )

    console.print(
        "\n[bold cyan]"
        "=== Automatic Post-Processing & Archiving ==="
        "[/bold cyan]"
    )

    # ----------------------------------------------------------
    # Create ZIP Archive
    # ----------------------------------------------------------

    zip_path = ARCHIVE_DIR / "PDF_Bundle.zip"

    with zipfile.ZipFile(
        zip_path,
        "w",
        zipfile.ZIP_DEFLATED
    ) as zipf:

        for id_val in id_list:

            pdf_path = (
                OUTPUT_DIR /
                f"id_{id_val}.pdf"
            )

            if is_valid_pdf(pdf_path):

                zipf.write(
                    pdf_path,
                    arcname=pdf_path.name
                )

    console.print(
        f"[green][OK] Archive ZIP Created:[/green] "
        f"{zip_path}"
    )

    # ----------------------------------------------------------
    # Merge PDFs
    # ----------------------------------------------------------

    merger = PdfWriter()

    merged_count = 0

    merged_path = (
        ARCHIVE_DIR /
        "Master_Combined.pdf"
    )

    for id_val in id_list:

        pdf_path = (
            OUTPUT_DIR /
            f"id_{id_val}.pdf"
        )

        if is_valid_pdf(pdf_path):

            try:

                merger.append(
                    str(pdf_path)
                )

                merged_count += 1

            except Exception:
                pass

    if merged_count > 0:

        merger.write(
            str(merged_path)
        )

        merger.close()

        console.print(
            f"[green][OK] Master PDF Combined "
            f"({merged_count} Files):[/green] "
            f"{merged_path}"
        )


# ==============================================================
# MAIN ASYNC ENGINE
# ==============================================================

async def run_linux_engine(
    id_list: list,
    concurrency: int
):

    OUTPUT_DIR.mkdir(
        exist_ok=True
    )

    semaphore = asyncio.Semaphore(
        concurrency
    )

    stats = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }

    retry_queue = []

    start_time = time.time()

    async with httpx.AsyncClient(
        verify=False,
        timeout=TIMEOUT_SEC
    ) as http_client:

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={
                    "width": 1280,
                    "height": 800,
                },
            )

            with Progress(
                SpinnerColumn(),
                TextColumn(
                    "[bold cyan]{task.description}"
                ),
                BarColumn(bar_width=None),
                TextColumn(
                    "[progress.percentage]"
                    "{task.percentage:>3.0f}%"
                ),
                TimeRemainingColumn(),
                console=console,
            ) as progress:

                task_id = progress.add_task(
                    (
                        f"[yellow]Downloading Queue "
                        f"({concurrency} Parallel Threads)..."
                    ),
                    total=len(id_list),
                )

                tasks = [
                    download_worker(
                        context,
                        http_client,
                        id_val,
                        semaphore,
                        progress,
                        task_id,
                        stats,
                        retry_queue,
                    )
                    for id_val in id_list
                ]

                await asyncio.gather(
                    *tasks
                )

            await browser.close()

    gc.collect()

    elapsed = round(
        time.time() - start_time,
        2
    )

    # ----------------------------------------------------------
    # Output Summary
    # ----------------------------------------------------------

    table = Table(
        title="Autonomous Scraping Summary",
        show_header=True,
        header_style="bold green",
    )

    table.add_column(
        "Category",
        style="dim",
        width=25
    )

    table.add_column(
        "Count",
        justify="right"
    )

    table.add_row(
        "Total Queued IDs",
        str(len(id_list))
    )

    table.add_row(
        "Downloaded [OK]",
        f"[green]{stats['success']}[/green]"
    )

    table.add_row(
        "DB Ledger Skipped",
        f"[blue]{stats['skipped']}[/blue]"
    )

    table.add_row(
        "Permanently Failed",
        f"[red]{len(retry_queue)}[/red]"
    )

    table.add_row(
        "Execution Time",
        f"{elapsed}s"
    )

    console.print(table)

    if (
        stats["success"] > 0
        or stats["skipped"] > 0
    ) and not SHUTDOWN_REQUESTED:

        linux_post_processing(
            id_list
        )


# ==============================================================
# INTERACTIVE + CLI MODE
# ==============================================================

def main():

    global BASE_URL

    setup_signal_handlers()

    parser = argparse.ArgumentParser(
        description="High-Speed Autonomous Linux PDF Downloader"
    )

    parser.add_argument(
        "--range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="ID Range (e.g., --range 10000 99999)"
    )

    parser.add_argument(
        "--file",
        type=str,
        metavar="FILE",
        help="TXT File Path containing IDs"
    )

    parser.add_argument(
        "--id",
        type=str,
        metavar="ID",
        help="Single ID to download"
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Number of parallel workers"
    )

    parser.add_argument(
        "--url",
        type=str,
        help=(
            "Base PDF URL ending with id= "
            "(e.g. https://example.com/file.php?id=)"
        )
    )

    args = parser.parse_args()

    # ==========================================================
    # INTERACTIVE MODE
    # ==========================================================

    if len(sys.argv) == 1:

        console.print()

        console.print(
            Panel.fit(
                "[bold cyan]PDF ENGINE[/bold cyan]\n"
                "[white]Authorized Security Testing / "
                "Assessment Tool[/white]",
                border_style="cyan",
            )
        )

        console.print()

        console.print(
            "[bold yellow]IMPORTANT:[/bold yellow] "
            "Use this tool only on systems you own or are "
            "explicitly authorized to test."
        )

        console.print()

        # ------------------------------------------------------
        # STEP 1 - TARGET URL
        # ------------------------------------------------------

        console.print(
            "[bold cyan]Step 1 - Enter Target URL[/bold cyan]"
        )

        console.print(
            "[dim]The URL must contain the ID parameter "
            "at the end.[/dim]"
        )

        console.print(
            "[dim]Example: "
            "https://example.com/path/file.php?id=[/dim]"
        )

        console.print()

        while True:

            BASE_URL = input(
                "Enter URL: "
            ).strip()

            if not BASE_URL:

                console.print(
                    "[red][!] URL cannot be empty.[/red]"
                )

                continue

            if "?" not in BASE_URL or "id=" not in BASE_URL:

                console.print(
                    "[red][!] Invalid URL format.[/red]"
                )

                console.print(
                    "[yellow]Example:[/yellow] "
                    "https://example.com/path/file.php?id="
                )

                continue

            if not BASE_URL.endswith("id="):

                if BASE_URL.endswith("id"):

                    BASE_URL += "="

                else:

                    console.print(
                        "[yellow][!] URL should normally "
                        "end with id=[/yellow]"
                    )

                    console.print(
                        "[dim]Example: "
                        "https://example.com/file.php?id=[/dim]"
                    )

                    confirm = input(
                        "Continue with this URL? [y/N]: "
                    ).strip().lower()

                    if confirm != "y":
                        continue

            break

        # ------------------------------------------------------
        # STEP 2 - ID INPUT METHOD
        # ------------------------------------------------------

        console.print()

        console.print(
            "[bold cyan]"
            "Step 2 - Select ID Input Method"
            "[/bold cyan]"
        )

        console.print()

        console.print(
            "  [bold green]1[/bold green] - Single ID"
        )

        console.print(
            "  [bold green]2[/bold green] - ID Range"
        )

        console.print(
            "  [bold green]3[/bold green] - IDs from TXT file"
        )

        console.print()

        while True:

            choice = input(
                "Select option [1/2/3]: "
            ).strip()

            if choice in ("1", "2", "3"):
                break

            console.print(
                "[red][!] Please select 1, 2, or 3.[/red]"
            )

        id_list = []

        # ------------------------------------------------------
        # SINGLE ID
        # ------------------------------------------------------

        if choice == "1":

            console.print()

            console.print(
                "[bold cyan]Example:[/bold cyan] 12345"
            )

            id_value = input(
                "Enter ID: "
            ).strip()

            if not id_value:

                console.print(
                    "[red][!] ID cannot be empty.[/red]"
                )

                return

            id_list = [
                id_value
            ]

        # ------------------------------------------------------
        # ID RANGE
        # ------------------------------------------------------

        elif choice == "2":

            console.print()

            console.print(
                "[bold cyan]Example:[/bold cyan] "
                "10000 to 10020"
            )

            while True:

                try:

                    start_id = int(
                        input(
                            "Enter START ID: "
                        ).strip()
                    )

                    end_id = int(
                        input(
                            "Enter END ID: "
                        ).strip()
                    )

                    if end_id < start_id:

                        console.print(
                            "[red][!] END ID must be "
                            "greater than or equal to "
                            "START ID.[/red]"
                        )

                        continue

                    id_list = [
                        str(i)
                        for i in range(
                            start_id,
                            end_id + 1
                        )
                    ]

                    break

                except ValueError:

                    console.print(
                        "[red][!] Please enter valid "
                        "numbers.[/red]"
                    )

        # ------------------------------------------------------
        # TXT FILE
        # ------------------------------------------------------

        elif choice == "3":

            console.print()

            console.print(
                "[bold cyan]Example:[/bold cyan] ids.txt"
            )

            file_path = Path(
                input(
                    "Enter TXT file path: "
                ).strip()
            )

            if not file_path.exists():

                console.print(
                    f"[red][!] File '{file_path}' "
                    f"not found![/red]"
                )

                return

            try:

                with open(
                    file_path,
                    "r",
                    encoding="utf-8"
                ) as f:

                    id_list = [
                        line.strip()
                        for line in f
                        if line.strip()
                    ]

            except Exception as e:

                console.print(
                    f"[red][!] Could not read file: "
                    f"{e}[/red]"
                )

                return

            if not id_list:

                console.print(
                    "[red][!] No IDs found in "
                    "the file.[/red]"
                )

                return

        # ------------------------------------------------------
        # STEP 3 - WORKER CONFIGURATION
        # ------------------------------------------------------

        console.print()

        console.print(
            "[bold cyan]"
            "Step 3 - Worker Configuration"
            "[/bold cyan]"
        )

        console.print(
            "[dim]For authorized testing, start with "
            "a low value such as 2-4.[/dim]"
        )

        console.print()

        while True:

            try:

                threads_input = input(
                    "Number of workers [default: 4]: "
                ).strip()

                if not threads_input:

                    threads = 4

                else:

                    threads = int(
                        threads_input
                    )

                if threads < 1:
                    raise ValueError

                break

            except ValueError:

                console.print(
                    "[red][!] Enter a valid number "
                    "greater than 0.[/red]"
                )

        # ------------------------------------------------------
        # CONFIGURATION SUMMARY
        # ------------------------------------------------------

        console.print()

        console.print(
            Panel(
                f"[bold cyan]Target URL:[/bold cyan]\n"
                f"{BASE_URL}\n\n"
                f"[bold cyan]Total IDs:[/bold cyan] "
                f"{len(id_list)}\n\n"
                f"[bold cyan]Workers:[/bold cyan] "
                f"{threads}",
                title="Configuration Summary",
                border_style="green",
            )
        )

        console.print()

        confirm = input(
            "Start processing? [y/N]: "
        ).strip().lower()

        if confirm != "y":

            console.print(
                "[yellow]Operation cancelled.[/yellow]"
            )

            return

        console.print()

        asyncio.run(
            run_linux_engine(
                id_list,
                threads
            )
        )

        return

    # ==========================================================
    # ORIGINAL CLI MODE
    # ==========================================================

    if args.url:

        BASE_URL = args.url

    if not BASE_URL:

        console.print(
            "[red]Error: BASE_URL is not configured.[/red]"
        )

        console.print(
            "[yellow]Use interactive mode:[/yellow]"
        )

        console.print(
            "python3 pdf_engine.py"
        )

        console.print(
            "[yellow]Or provide --url.[/yellow]"
        )

        sys.exit(1)

    id_list = []

    if args.range:

        start_id, end_id = args.range

        id_list = [
            str(i)
            for i in range(
                start_id,
                end_id + 1
            )
        ]

    elif args.file:

        file_path = Path(
            args.file
        )

        if file_path.exists():

            with open(
                file_path,
                "r",
                encoding="utf-8"
            ) as f:

                id_list = [
                    line.strip()
                    for line in f
                    if line.strip()
                ]

        else:

            console.print(
                f"[red]Error: File "
                f"'{file_path}' not found![/red]"
            )

            sys.exit(1)

    elif args.id:

        id_list = [
            args.id
        ]

    else:

        console.print(
            "[yellow]Usage Examples:[/yellow]"
        )

        console.print(
            '  python3 pdf_engine.py '
            '--url "https://example.com/file.php?id=" '
            '--id 123'
        )

        console.print(
            '  python3 pdf_engine.py '
            '--url "https://example.com/file.php?id=" '
            '--range 100 120 --threads 4'
        )

        console.print(
            '  python3 pdf_engine.py '
            '--url "https://example.com/file.php?id=" '
            '--file list.txt'
        )

        sys.exit(0)

    threads = (
        args.threads
        if args.threads
        else 4
    )

    if threads < 1:

        console.print(
            "[red]Error: threads must be "
            "greater than 0.[/red]"
        )

        sys.exit(1)

    console.print(
        f"[bold green]"
        f"[+] Queued {len(id_list)} IDs "
        f"for processing with "
        f"{threads} workers..."
        f"[/bold green]"
    )

    asyncio.run(
        run_linux_engine(
            id_list,
            threads
        )
    )


# ==============================================================
# PROGRAM ENTRY POINT
# ==============================================================

if __name__ == "__main__":
    main()
