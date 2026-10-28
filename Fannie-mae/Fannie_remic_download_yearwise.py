import csv
from pathlib import Path
import re
import traceback
from urllib.parse import unquote, urljoin, urlparse

from playwright.sync_api import sync_playwright

URL = "https://fanniemae.mbs-securities.com/fannie/search"
MIN_YEAR = 1975
MAX_YEAR = 2027

SELECTORS = {
    "advanced_search": "i[title='Toggle Advanced Search']",
    "security_type": "select[name='issrSpclSecuType']",
    "from_year": "select[name='issuanceFromYear']",
    "to_year": "select[name='issuanceToYear']",
    "search_button": "button.search",
    "results_table": "ul.results-table",
    "result_rows": "ul.results-table > li.list-group-item",
    "document_links": "a[href*='/api/download/'], ul.document-table a[href], a[download]",
}

FALLBACK_TABLE_COLUMNS = [
    "CUSIP",
    "Security Identifier",
    "Security Type",
    "Prepayment Speed",
    "Issue Date",
    "Maturity Date",
    "WA Net Interest Rate",
    "Issuance Investor Security UPB",
    "Current Investor Security UPB",
    "Social Indicator",
    "Green Indicator",
    "Trust Identifier",
    "Class Identifier",
]


def log(message, log_path=None):
    formatted = f"[INFO] {message}"
    print(formatted)
    if log_path is not None:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(formatted + "\n")


def unique_download_path(download_dir, filename):
    safe_name = Path(filename).name or "supplement.pdf"
    target = download_dir / safe_name
    suffix = 2
    while target.exists():
        target = download_dir / f"{Path(safe_name).stem}_{suffix}{Path(safe_name).suffix}"
        suffix += 1
    return target


def is_supplement_document(*values):
    searchable_text = " ".join(unquote(str(value or "")) for value in values)
    normalized_text = re.sub(r"[^a-z0-9]+", " ", searchable_text.casefold())
    return "supplement" in normalized_text or re.search(r"\bsupp\b", normalized_text) is not None


def document_description(link):
    return {
        "download": link.get_attribute("download") or "",
        "href": link.get_attribute("href") or "",
        "label": link.inner_text().strip(),
        "title": link.get_attribute("title") or "",
        "aria_label": link.get_attribute("aria-label") or "",
    }


def save_supplement_download(deal_page, link, files_dir, cusip):
    document = document_description(link)
    href = document["href"]
    supplement_url = urljoin(deal_page.url, href)
    suggested_name = document["download"] or Path(urlparse(href).path).name

    try:
        with deal_page.expect_download(timeout=20000) as download_event:
            link.click(force=True)
        download = download_event.value
        save_path = unique_download_path(files_dir, download.suggested_filename)
        download.save_as(str(save_path))
        return save_path
    except Exception as download_error:
        if not supplement_url.startswith(("http://", "https://")):
            raise download_error

        response = deal_page.context.request.get(supplement_url, timeout=60000)
        if not response.ok:
            raise RuntimeError(
                f"Download event failed ({download_error}); HTTP {response.status} for {supplement_url}"
            ) from download_error

        body = response.body()
        content_type = response.headers.get("content-type", "").casefold()
        if not body.startswith(b"%PDF") and "pdf" not in content_type:
            raise RuntimeError(
                f"Document link did not return a PDF (content-type: {content_type or 'unknown'})"
            ) from download_error

        filename = suggested_name or f"{cusip or 'supplement'}_supplement.pdf"
        if not Path(filename).suffix:
            filename += ".pdf"
        save_path = unique_download_path(files_dir, filename)
        save_path.write_bytes(body)
        return save_path


def extract_grid_cells(element):
    return element.evaluate(
        """row => {
            const cellSelector = 'td, [role="cell"], [class*="col-"]';
            let cells = Array.from(row.querySelectorAll(cellSelector)).filter(cell =>
                !cell.querySelector(cellSelector)
            );
            if (!cells.length) {
                cells = Array.from(row.children);
                while (cells.length === 1 && cells[0].children.length > 1) {
                    cells = Array.from(cells[0].children);
                }
            }
            return cells.map(cell => (cell.innerText || cell.textContent || '').trim());
        }"""
    )


def find_table_columns(page):
    for header_selector in (
        "ul.results-table [role='columnheader']",
        "ul.results-table thead th",
        "ul.results-table > li",
    ):
        items = page.locator(header_selector)
        for index in range(items.count()):
            item = items.nth(index)
            if item.locator("a[href*='/deal/']").count():
                continue
            cells = [cell for cell in extract_grid_cells(item) if cell]
            joined = " ".join(cells).casefold()
            if "cusip" in joined and "issue date" in joined:
                return cells
    return FALLBACK_TABLE_COLUMNS.copy()


def make_csv_record(columns, values):
    if len(values) > len(columns):
        columns.extend(
            f"Column {index}"
            for index in range(len(columns) + 1, len(values) + 1)
        )
    values = values + [""] * max(0, len(columns) - len(values))
    return dict(zip(columns, values))


def is_table_header_row(values, columns):
    if not values:
        return False
    first_value = re.sub(r"[^a-z0-9]", "", values[0].casefold())
    first_column = re.sub(r"[^a-z0-9]", "", columns[0].casefold()) if columns else ""
    return first_value == first_column == "cusip"


def year_csv_path(year_dir, year):
    return year_dir / f"remic_results_{year}.csv"


def year_csv_fieldnames(year_summary):
    return year_summary["table_columns"] + ["File Name", "File Path"]


def rewrite_year_csv(year_dir, year_summary):
    csv_path = year_csv_path(year_dir, year_summary["year"])
    fieldnames = year_csv_fieldnames(year_summary)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(year_summary["csv_rows"])
    year_summary["csv_fieldnames"] = fieldnames.copy()
    return csv_path


def append_year_csv_row(year_dir, year_summary, record):
    year_summary["csv_rows"].append(record)
    fieldnames = year_csv_fieldnames(year_summary)
    if fieldnames != year_summary.get("csv_fieldnames"):
        rewrite_year_csv(year_dir, year_summary)
        return

    csv_path = year_csv_path(year_dir, year_summary["year"])
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(record)


def write_year_report(year, year_dir, files_dir, stats):
    report_path = year_dir / f"year_report_{year}.txt"
    csv_path = year_csv_path(year_dir, year)
    failed_cusips = sorted(stats["failed_cusips"])
    report_lines = [
        f"Fannie Mae REMIC supplement report for {year}",
        "=" * 60,
        f"Result rows found: {stats['records_available']}",
        f"Unique deals processed: {stats['records_processed']}",
        f"Duplicate deals skipped: {stats['duplicates_skipped']}",
        f"Unique CUSIP IDs: {len(stats['unique_cusips'])}",
        f"Unique supplement files downloaded: {len(stats['downloaded_supplements'])}",
        f"Records unable to process: {len(stats['errors'])}",
        f"Failed CUSIP IDs: {failed_cusips}",
        f"CSV: {csv_path}",
        f"Download folder: {files_dir}",
    ]
    report_lines.extend(
        f"CUSIP: {cusip}" for cusip in sorted(stats["unique_cusips"])
    )
    report_lines.extend(
        f"Error: CUSIP {error['cusip']} | {error['message']}"
        for error in stats["errors"]
    )
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return report_path


def process_year(page, context, year):
    year_dir = Path("Remic-Documents-all") / str(year)
    files_dir = year_dir / "files"
    year_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    log_path = year_dir / f"year_log_{year}.txt"
    log_path.write_text("", encoding="utf-8")

    stats = {
        "year": year,
        "records_available": 0,
        "records_processed": 0,
        "duplicates_skipped": 0,
        "unique_cusips": set(),
        "downloaded_supplements": set(),
        "supplement_files": {},
        "failed_cusips": set(),
        "errors": [],
        "table_columns": FALLBACK_TABLE_COLUMNS.copy(),
        "csv_rows": [],
    }

    processed_deals = set()
    downloaded_supplements = set()
    deal_files = {}

    log(f"========== STARTING YEAR {year} ==========", log_path)
    try:
        log("Opening Search URL", log_path)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        log(f"Current URL: {page.url}", log_path)

        if "showTermConditions" in page.url:
            log("Terms Page Detected", log_path)
            page.get_by_role("button", name="Agree", exact=True).wait_for(
                state="visible", timeout=15000
            )
            page.get_by_role("button", name="Agree", exact=True).click()
            page.wait_for_url("**/search*", timeout=30000)
            page.wait_for_load_state("networkidle")
            log("Search Page Loaded", log_path)

        page.locator(SELECTORS["advanced_search"]).click()
        page.locator(SELECTORS["security_type"]).select_option("REMIC")
        page.locator(SELECTORS["from_year"]).select_option(str(year))
        page.locator(SELECTORS["to_year"]).select_option(str(year))
        log(f"Search filters: REMIC, {year} through {year}", log_path)

        page.locator(SELECTORS["search_button"]).click()
        log("Search Submitted", log_path)
        page.locator(SELECTORS["results_table"]).wait_for(
            state="visible", timeout=60000
        )
        page.wait_for_timeout(3000)
        stats["table_columns"] = find_table_columns(page)
        csv_path = rewrite_year_csv(year_dir, stats)
        log(f"Created yearly CSV: {csv_path}", log_path)

        while True:
            active_button = page.locator("button.page-btn.active")
            current_page = int(active_button.inner_text().strip())
            log(f"========== PROCESSING PAGE {current_page} ==========", log_path)

            rows = page.locator(SELECTORS["result_rows"])
            total_rows = rows.count()
            log(f"Visible result-list items: {total_rows}", log_path)

            for row_index in range(total_rows):
                current_row = rows.nth(row_index)
                record = None
                row_written = False
                cusip = None
                deal_page = None
                try:
                    grid_values = extract_grid_cells(current_row)
                    if is_table_header_row(grid_values, stats["table_columns"]):
                        log(f"Skipping repeated table header on page {current_page}", log_path)
                        continue

                    stats["records_available"] += 1
                    record = make_csv_record(stats["table_columns"], grid_values)
                    cusip_column = next(
                        (
                            column
                            for column in stats["table_columns"]
                            if column.strip().casefold() == "cusip"
                        ),
                        stats["table_columns"][0],
                    )
                    cusip = record.get(cusip_column, "").strip()
                    if cusip:
                        stats["unique_cusips"].add(cusip)
                    record["File Name"] = ""
                    record["File Path"] = ""

                    trust_link = current_row.locator("a[href*='/deal/']").first
                    if trust_link.count() == 0:
                        failed_id = cusip or f"UNKNOWN_PAGE_{current_page}_ROW_{row_index}"
                        stats["failed_cusips"].add(failed_id)
                        stats["errors"].append(
                            {"cusip": failed_id, "message": "Deal link not found in result row"}
                        )
                        log(f"Row Error | CUSIP: {failed_id} | Deal link not found", log_path)
                        append_year_csv_row(year_dir, stats, record)
                        row_written = True
                        continue

                    deal_name = trust_link.inner_text().strip()
                    deal_href = trust_link.get_attribute("href") or ""
                    deal_key = deal_href.split("?", 1)[0].rstrip("/") or deal_name.casefold()
                    if not deal_name or not deal_key:
                        append_year_csv_row(year_dir, stats, record)
                        row_written = True
                        continue

                    if deal_key in processed_deals:
                        stats["duplicates_skipped"] += 1
                        log(f"Skipping Duplicate Deal: {deal_name}", log_path)
                        cached_files = deal_files.get(deal_key, [])
                        record["File Name"] = "; ".join(name for name, _ in cached_files)
                        record["File Path"] = "; ".join(path for _, path in cached_files)
                        append_year_csv_row(year_dir, stats, record)
                        row_written = True
                        continue

                    processed_deals.add(deal_key)
                    deal_files[deal_key] = []
                    log(f"Processing Deal: {deal_name} | CUSIP: {cusip or 'Unknown'}", log_path)

                    try:
                        with context.expect_page() as new_page_event:
                            trust_link.click(button="middle")
                        deal_page = new_page_event.value
                        deal_page.wait_for_load_state("networkidle")
                        log(f"Deal Page Opened: {deal_page.url}", log_path)

                        deal_page.get_by_text("Documents", exact=True).click(timeout=30000)
                        deal_page.locator(SELECTORS["document_links"]).first.wait_for(
                            state="visible", timeout=30000
                        )

                        document_links = deal_page.locator(SELECTORS["document_links"])
                        supplement_links = []
                        visible_documents = []
                        for link_index in range(document_links.count()):
                            link = document_links.nth(link_index)
                            description = document_description(link)
                            visible_documents.append(
                                description["download"]
                                or description["label"]
                                or description["href"]
                            )
                            if is_supplement_document(*description.values()):
                                supplement_links.append((link, description))

                        if not supplement_links:
                            log(
                                f"No supplement link detected for {deal_name}; "
                                f"document links: {visible_documents}",
                                log_path,
                            )
                        for supplement_doc, description in supplement_links:
                            supplement_url = description["href"]
                            supplement_key = (
                                urljoin(deal_page.url, supplement_url)
                                if supplement_url
                                else f"{deal_name}:{description['label']}"
                            )

                            if supplement_key in downloaded_supplements:
                                log(f"Skipping Duplicate Supplement: {deal_name}", log_path)
                                cached_file = stats["supplement_files"].get(supplement_key)
                                if cached_file:
                                    deal_files[deal_key].append(cached_file)
                                continue

                            save_path = save_supplement_download(
                                deal_page, supplement_doc, files_dir, cusip
                            )
                            downloaded_supplements.add(supplement_key)
                            stats["downloaded_supplements"].add(supplement_key)
                            cached_file = (save_path.name, str(save_path.resolve()))
                            stats["supplement_files"][supplement_key] = cached_file
                            deal_files[deal_key].append(cached_file)
                            log(f"Downloaded supplement: {save_path}", log_path)
                    except Exception as deal_error:
                        failed_id = cusip or f"UNKNOWN_PAGE_{current_page}_ROW_{row_index}"
                        stats["failed_cusips"].add(failed_id)
                        stats["errors"].append(
                            {"cusip": failed_id, "message": str(deal_error)}
                        )
                        log(f"Deal Error | CUSIP: {failed_id} | {deal_error}", log_path)
                    finally:
                        if deal_page is not None and not deal_page.is_closed():
                            deal_page.close()

                    cached_files = deal_files[deal_key]
                    record["File Name"] = "; ".join(name for name, _ in cached_files)
                    record["File Path"] = "; ".join(path for _, path in cached_files)
                    stats["records_processed"] += 1
                    append_year_csv_row(year_dir, stats, record)
                    row_written = True
                    log(f"Completed Deal: {deal_name}", log_path)
                except Exception as row_error:
                    failed_id = cusip or f"UNKNOWN_PAGE_{current_page}_ROW_{row_index}"
                    stats["failed_cusips"].add(failed_id)
                    stats["errors"].append(
                        {"cusip": failed_id, "message": str(row_error)}
                    )
                    log(f"Row Error | CUSIP: {failed_id} | {row_error}", log_path)
                    if record is not None and not row_written:
                        append_year_csv_row(year_dir, stats, record)
                finally:
                    if deal_page is not None and not deal_page.is_closed():
                        deal_page.close()

            next_page = current_page + 1
            next_button = page.get_by_role("button", name=str(next_page), exact=True)
            if next_button.count() == 0:
                log("LAST PAGE REACHED", log_path)
                break

            log(f"Navigating To Page {next_page}", log_path)
            next_button.click()
            page.locator("button.page-btn.active").filter(
                has_text=str(next_page)
            ).wait_for(state="visible", timeout=30000)
            page.locator(SELECTORS["results_table"]).wait_for(state="visible")
            page.wait_for_load_state("networkidle")

        log(f"PROCESS COMPLETED FOR {year}", log_path)
    except Exception as year_error:
        error_message = str(year_error)
        stats["errors"].append({"cusip": "UNKNOWN", "message": error_message})
        stats["failed_cusips"].add("UNKNOWN")
        log(f"Year Error: {error_message}", log_path)
        log(traceback.format_exc(), log_path)
    finally:
        report_path = write_year_report(year, year_dir, files_dir, stats)
        log(f"Year report written: {report_path}", log_path)
        log(
            f"Year {year} summary: {stats['records_processed']} processed, "
            f"{len(stats['errors'])} errors, "
            f"{len(stats['downloaded_supplements'])} unique supplements",
            log_path,
        )


def read_year_range():
    print(
        "\nPlease enter the start and end year. "
        "Each year will be searched and reported separately.\n"
    )
    while True:
        from_year = input("Enter Issuance Starting Year (Example: 2025): ").strip()
        to_year = input("Enter Issuance Ending Year (Example: 2025): ").strip()

        if not (from_year.isdigit() and to_year.isdigit()):
            print("\n[ERROR] Please enter valid numeric years.\n")
            continue

        from_year = int(from_year)
        to_year = int(to_year)
        if not (MIN_YEAR <= from_year <= MAX_YEAR):
            print(f"\n[ERROR] Starting Year must be between {MIN_YEAR} and {MAX_YEAR}.\n")
            continue
        if not (MIN_YEAR <= to_year <= MAX_YEAR):
            print(f"\n[ERROR] Ending Year must be between {MIN_YEAR} and {MAX_YEAR}.\n")
            continue
        if from_year > to_year:
            print("\n[ERROR] Starting Year cannot be greater than Ending Year.\n")
            continue
        return from_year, to_year


def main():
    from_year, to_year = read_year_range()
    print(f"\n[INFO] Valid Year Range Selected: {from_year} to {to_year}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, slow_mo=300)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            for year in range(from_year, to_year + 1):
                process_year(page, context, year)
        finally:
            browser.close()
            print("[INFO] Browser Closed")


if __name__ == "__main__":
    main()