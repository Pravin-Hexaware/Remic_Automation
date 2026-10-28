import csv
import re
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import Page, Playwright, sync_playwright


URL = "https://freddiemac.mbs-securities.com/freddie/search/"
DOWNLOAD_DIR = Path(__file__).resolve().parent / "Remic-Downloads"
MIN_YEAR = 1975
MAX_YEAR = 2027

SELECTORS = {
	"advanced_search": "i[title='Toggle Advanced Search']",
	"security_type": "select[name='issrSpclSecuType']",
	"issuance_from_month": "select[name='issuanceFromMonth']",
	"issuance_from_year": "select[name='issuanceFromYear']",
	"issuance_to_month": "select[name='issuanceToMonth']",
	"issuance_to_year": "select[name='issuanceToYear']",
	"search_button": "button.search",
	"results_table": "ul.results-table",
	"result_rows": "ul.results-table > li.list-group-item",
	"document_table": "ul.document-table",
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


def log(message: str) -> None:
	print(f"[INFO] {message}")


def wait_for_page_settle(page: Page, minimum_wait_ms: int = 5000) -> None:
	page.wait_for_timeout(minimum_wait_ms)
	try:
		page.wait_for_load_state("networkidle", timeout=15000)
	except Exception:
		log("Network did not become idle; continuing after the minimum wait")


def safe_filename(name: str) -> str:
	cleaned = str(name or "downloaded_file").strip()
	cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
	return cleaned[:200] or "downloaded_file"


def unique_path(directory: Path, filename: str) -> Path:
	path = directory / safe_filename(filename)
	if not path.exists():
		return path

	stem, suffix = path.stem, path.suffix
	index = 2
	while True:
		candidate = directory / f"{stem}_{index}{suffix}"
		if not candidate.exists():
			return candidate
		index += 1


def is_oc_document(*values: str) -> bool:
	for value in values:
		text = unquote(str(value or "")).strip()
		if not text:
			continue

		path = urlparse(text).path if "://" in text else text.split("?", 1)[0].split("#", 1)[0]
		filename = Path(path.replace("\\", "/")).name
		if filename.casefold().endswith("oc"):
			return True
	return False


def extract_grid_cells(element) -> list[str]:
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


def find_table_columns(page: Page) -> list[str]:
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


def make_csv_record(columns: list[str], values: list[str]) -> dict[str, str]:
	if len(values) > len(columns):
		columns.extend(f"Column {index}" for index in range(len(columns) + 1, len(values) + 1))
	values = values + [""] * max(0, len(columns) - len(values))
	return dict(zip(columns, values))


def is_table_header_row(values: list[str], columns: list[str]) -> bool:
	if not values:
		return False

	first_value = re.sub(r"[^a-z0-9]", "", values[0].casefold())
	first_column = re.sub(r"[^a-z0-9]", "", columns[0].casefold()) if columns else ""
	return first_value == first_column == "cusip"


def year_csv_path(year_dir: Path, year_summary: dict) -> Path:
	csv_path = year_dir / f"remic_results_{year_summary['year']}.csv"
	return csv_path


def year_csv_fieldnames(year_summary: dict) -> list[str]:
	return year_summary["table_columns"] + ["File Name", "File Path"]


def rewrite_year_csv(year_dir: Path, year_summary: dict) -> Path:
	csv_path = year_csv_path(year_dir, year_summary)
	fieldnames = year_csv_fieldnames(year_summary)
	with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
		writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
		writer.writeheader()
		writer.writerows(year_summary["csv_rows"])
	year_summary["csv_fieldnames"] = fieldnames.copy()
	return csv_path


def initialize_year_csv(year_dir: Path, year_summary: dict) -> Path:
	year_summary["csv_rows"] = []
	return rewrite_year_csv(year_dir, year_summary)


def append_year_csv_row(year_dir: Path, year_summary: dict, record: dict[str, str]) -> None:
	year_summary["csv_rows"].append(record)
	fieldnames = year_csv_fieldnames(year_summary)
	if fieldnames != year_summary.get("csv_fieldnames"):
		rewrite_year_csv(year_dir, year_summary)
		return

	csv_path = year_csv_path(year_dir, year_summary)
	with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
		writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
		writer.writerow(record)


def write_year_report(year_dir: Path, year_summary: dict) -> Path:
	csv_path = year_csv_path(year_dir, year_summary)
	report_path = year_dir / f"year_report_{year_summary['year']}.txt"
	report_lines = [
		f"Freddie Mac REMIC OC report for {year_summary['year']}",
		"=" * 60,
		f"Result rows found: {year_summary['records_available']}",
		f"Records processed (unique trusts): {year_summary['records_processed']}",
		f"Records skipped as duplicate trusts: {year_summary['duplicates_skipped']}",
		f"Unique CUSIP IDs: {len(year_summary['unique_cusips'])}",
		f"Deals with OC files: {year_summary['deals_with_oc']}",
		f"OC files downloaded: {len(year_summary['files_downloaded'])}",
		f"Errors: {len(year_summary['errors'])}",
		f"CSV: {csv_path}",
		f"Download folder: {year_dir}",
	]
	report_lines.extend(f"CUSIP: {cusip}" for cusip in sorted(year_summary["unique_cusips"]))
	report_lines.extend(f"Error: {error}" for error in year_summary["errors"])
	report_path.write_text("\n".join(report_lines), encoding="utf-8")
	return report_path


def read_year(prompt: str) -> int:
	while True:
		raw_year = input(prompt).strip()
		if raw_year.isdigit() and MIN_YEAR <= int(raw_year) <= MAX_YEAR:
			return int(raw_year)
		print(f"Enter a year from {MIN_YEAR} through {MAX_YEAR}.")


def accept_terms_if_needed(page: Page) -> None:
	if "showTermConditions" in page.url:
		log("Terms page detected")
		page.get_by_role("button", name="Agree", exact=True).first.click(force=True)
		wait_for_page_settle(page)
		log("Accepted terms")


def open_search_page(page: Page) -> None:
	page.goto(URL, wait_until="domcontentloaded", timeout=60000)
	wait_for_page_settle(page)
	accept_terms_if_needed(page)


def submit_year_search(page: Page, year: int) -> None:
	toggle = page.locator(SELECTORS["advanced_search"])
	if toggle.count() and not page.locator(SELECTORS["issuance_from_year"]).is_visible():
		toggle.click(force=True)

	page.locator(SELECTORS["security_type"]).select_option("REMIC")
	page.locator(SELECTORS["issuance_from_month"]).select_option("1")
	page.locator(SELECTORS["issuance_from_year"]).select_option(str(year))
	page.locator(SELECTORS["issuance_to_month"]).select_option("12")
	page.locator(SELECTORS["issuance_to_year"]).select_option(str(year))
	log(f"Search criteria: REMIC, January {year} through December {year}")

	page.locator(SELECTORS["search_button"]).click()
	page.locator(SELECTORS["results_table"]).wait_for(state="visible", timeout=60000)
	wait_for_page_settle(page)
	log(f"Results loaded for {year}")


def process_year(page: Page, year: int, summary: dict) -> None:
	year_dir = DOWNLOAD_DIR / str(year)
	year_dir.mkdir(parents=True, exist_ok=True)
	year_summary = {
		"year": year,
		"records_available": 0,
		"records_processed": 0,
		"duplicates_skipped": 0,
		"deals_checked": 0,
		"deals_with_oc": 0,
		"files_downloaded": [],
		"errors": [],
		"table_columns": FALLBACK_TABLE_COLUMNS.copy(),
		"csv_rows": [],
		"unique_cusips": set(),
	}
	csv_path = initialize_year_csv(year_dir, year_summary)
	log(f"Created yearly CSV: {csv_path}")
	summary["active_year_summary"] = year_summary
	processed_deals = set()
	deal_files = {}

	log("=" * 70)
	log(f"Processing issuance year {year}")
	open_search_page(page)
	submit_year_search(page, year)
	year_summary["table_columns"] = find_table_columns(page)
	rewrite_year_csv(year_dir, year_summary)

	while True:
		active_button = page.locator("button.page-btn.active")
		current_page_text = active_button.inner_text().strip() if active_button.count() else "1"
		try:
			current_page = int(current_page_text)
		except ValueError:
			current_page = 1
		log(f"========== {year} / PAGE {current_page} ==========")

		rows = page.locator(SELECTORS["result_rows"])
		total_rows = rows.count()
		log(f"Visible result-list items: {total_rows}")

		for row_index in range(total_rows):
			row = rows.nth(row_index)
			grid_values = extract_grid_cells(row)
			if is_table_header_row(grid_values, year_summary["table_columns"]):
				log(f"Skipping repeated table header on page {current_page}")
				continue

			year_summary["records_available"] += 1
			record = make_csv_record(year_summary["table_columns"], grid_values)
			cusip_column = next(
				(column for column in year_summary["table_columns"] if column.strip().casefold() == "cusip"),
				year_summary["table_columns"][0],
			)
			cusip = record.get(cusip_column, "").strip()
			if cusip:
				year_summary["unique_cusips"].add(cusip)
			record["File Name"] = ""
			record["File Path"] = ""
			deal_link = row.locator("a[href*='/deal/']").first
			if deal_link.count() == 0:
				append_year_csv_row(year_dir, year_summary, record)
				continue

			deal_name = deal_link.inner_text().strip()
			deal_href = deal_link.get_attribute("href") or ""
			deal_key = deal_href.split("?", 1)[0].rstrip("/") or deal_name.casefold()
			if not deal_name or not deal_key:
				append_year_csv_row(year_dir, year_summary, record)
				continue
			if deal_key in processed_deals:
				year_summary["duplicates_skipped"] += 1
				cached_files = deal_files.get(deal_key, [])
				record["File Name"] = "; ".join(name for name, _ in cached_files)
				record["File Path"] = "; ".join(path for _, path in cached_files)
				append_year_csv_row(year_dir, year_summary, record)
				log(f"Skipping duplicate trust record: {deal_name}")
				continue

			processed_deals.add(deal_key)
			year_summary["records_processed"] += 1
			year_summary["deals_checked"] += 1
			log(f"Processing deal: {deal_name}")
			deal_files[deal_key] = []

			try:
				with page.context.expect_page(timeout=15000) as new_page_event:
					deal_link.click(button="middle")
				deal_page = new_page_event.value
				try:
					deal_page.wait_for_load_state("domcontentloaded", timeout=60000)
					wait_for_page_settle(deal_page)
					log(f"Opened deal: {deal_page.url}")
					deal_page.get_by_text("Documents", exact=True).click()
					deal_page.locator(SELECTORS["document_table"]).wait_for(state="visible", timeout=30000)
					wait_for_page_settle(deal_page)

					links = deal_page.locator(
						f"{SELECTORS['document_table']} a[href], {SELECTORS['document_table']} a[download]"
					)
					oc_links = []
					for link_index in range(links.count()):
						link = links.nth(link_index)
						download_name = link.get_attribute("download") or ""
						href = link.get_attribute("href") or ""
						label = link.inner_text().strip()
						if is_oc_document(download_name, href, label):
							oc_links.append(link)

					if not oc_links:
						log(f"No OC document for {deal_name}")
					else:
						year_summary["deals_with_oc"] += 1
						for link in oc_links:
							try:
								with deal_page.expect_download(timeout=60000) as download_event:
									link.click(force=True)
								download = download_event.value
								save_path = unique_path(year_dir, download.suggested_filename)
								download.save_as(str(save_path))
								year_summary["files_downloaded"].append(save_path.name)
								deal_files[deal_key].append((save_path.name, str(save_path.resolve())))
								log(f"Downloaded: {save_path}")
							except Exception as error:
								year_summary["errors"].append(f"{deal_name}: {error}")
								log(f"Download error for {deal_name}: {error}")
				finally:
					deal_page.close()
			except Exception as error:
				year_summary["errors"].append(f"{deal_name}: {error}")
				log(f"Deal error for {deal_name}: {error}")
			files_for_deal = deal_files[deal_key]
			record["File Name"] = "; ".join(name for name, _ in files_for_deal)
			record["File Path"] = "; ".join(path for _, path in files_for_deal)
			append_year_csv_row(year_dir, year_summary, record)

		next_button = page.get_by_role("button", name=str(current_page + 1), exact=True)
		if next_button.count() == 0:
			log(f"Last page reached for {year}")
			break

		log(f"Moving to page {current_page + 1}")
		next_button.click()
		expected_page = current_page + 1
		wait_for_page_settle(page)
		page.wait_for_function(
			"expected => { const active = document.querySelector('button.page-btn.active'); "
			"return active && active.textContent.trim() === String(expected); }",
			arg=expected_page,
			timeout=60000,
		)
		page.locator(SELECTORS["results_table"]).wait_for(state="visible", timeout=60000)
		log(f"Successfully moved to page {expected_page}")

	report_path = write_year_report(year_dir, year_summary)
	summary["total_records_available"] += year_summary["records_available"]
	summary["total_deals_checked"] += year_summary["deals_checked"]
	summary["total_deals_with_oc"] += year_summary["deals_with_oc"]
	summary["total_files_collected"] += len(year_summary["files_downloaded"])
	summary["total_duplicates_skipped"] += year_summary["duplicates_skipped"]
	summary["years"].append(year_summary)
	summary["active_year_summary"] = None
	log(
		f"Year {year} complete: {year_summary['records_processed']} records processed, "
		f"{year_summary['duplicates_skipped']} duplicate records skipped, "
		f"{len(year_summary['files_downloaded'])} OC files downloaded. "
		f"CSV: {csv_path}; report: {report_path}"
	)


def write_report(summary: dict) -> Path:
	DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
	lines = [
		"=" * 100,
		"FREDDIE MAC REMIC OC DOWNLOAD REPORT",
		"=" * 100,
		f"Job started: {summary['start_time']}",
		f"Requested years: {summary['start_year']} through {summary['end_year']}",
		f"Years completed: {len(summary['years'])}",
		f"Total result rows: {summary['total_records_available']}",
		f"Deals checked: {summary['total_deals_checked']}",
		f"Deals with OC documents: {summary['total_deals_with_oc']}",
		f"Duplicate trust records skipped: {summary['total_duplicates_skipped']}",
		f"Files downloaded: {summary['total_files_collected']}",
		"",
		"YEAR DETAILS:",
	]

	for year_detail in summary["years"]:
		lines.append(
			f"- {year_detail['year']}: records={year_detail['records_available']}, "
			f"deals={year_detail['deals_checked']}, OC deals={year_detail['deals_with_oc']}, "
			f"files={len(year_detail['files_downloaded'])}, errors={len(year_detail['errors'])}"
		)
		lines.extend(f"    File: {filename}" for filename in year_detail["files_downloaded"])
		lines.extend(f"    Error: {error}" for error in year_detail["errors"])

	report_path = DOWNLOAD_DIR / f"job_report_{datetime.now():%Y%m%d_%H%M%S}.txt"
	report_path.write_text("\n".join(lines), encoding="utf-8")
	return report_path


def run(playwright: Playwright, start_year: int, end_year: int) -> dict:
	summary = {
		"start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
		"start_year": start_year,
		"end_year": end_year,
		"total_records_available": 0,
		"total_deals_checked": 0,
		"total_deals_with_oc": 0,
		"total_duplicates_skipped": 0,
		"total_files_collected": 0,
		"years": [],
		"active_year_summary": None,
	}
	browser = playwright.chromium.launch(headless=False, slow_mo=150)
	context = browser.new_context(accept_downloads=True)
	page = context.new_page()
	total_years = end_year - start_year + 1

	try:
		for year_index, year in enumerate(range(start_year, end_year + 1), start=1):
			log(f"Starting year {year_index} of {total_years}: {year}")
			try:
				process_year(page, year, summary)
			except Exception as error:
				log(f"Year {year} failed: {error}")
				try:
					page.screenshot(path=f"error_{year}.png", full_page=True)
				except Exception:
					pass
				year_summary = summary.get("active_year_summary")
				if not year_summary or year_summary["year"] != year:
					year_summary = {
						"year": year,
						"records_available": 0,
						"records_processed": 0,
						"duplicates_skipped": 0,
						"deals_checked": 0,
						"deals_with_oc": 0,
						"files_downloaded": [],
						"errors": [],
						"table_columns": FALLBACK_TABLE_COLUMNS.copy(),
						"csv_rows": [],
						"unique_cusips": set(),
					}
				year_summary["errors"].append(str(error))
				year_dir = DOWNLOAD_DIR / str(year)
				year_dir.mkdir(parents=True, exist_ok=True)
				write_year_report(year_dir, year_summary)
				summary["total_records_available"] += year_summary["records_available"]
				summary["total_deals_checked"] += year_summary["deals_checked"]
				summary["total_deals_with_oc"] += year_summary["deals_with_oc"]
				summary["total_duplicates_skipped"] += year_summary["duplicates_skipped"]
				summary["total_files_collected"] += len(year_summary["files_downloaded"])
				summary["years"].append(year_summary)
				summary["active_year_summary"] = None
				page.goto(URL, wait_until="domcontentloaded", timeout=60000)
				accept_terms_if_needed(page)
			if year < end_year:
				log(f"Starting next year: {year + 1}")
	finally:
		browser.close()
		log("Browser closed")

	return summary


def main() -> None:
	start_year = read_year(f"Enter start year ({MIN_YEAR}-{MAX_YEAR}): ")
	end_year = read_year(f"Enter end year ({start_year}-{MAX_YEAR}): ")
	while end_year < start_year:
		print("End year must be greater than or equal to start year.")
		end_year = read_year(f"Enter end year ({start_year}-{MAX_YEAR}): ")

	DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
	try:
		with sync_playwright() as playwright:
			summary = run(playwright, start_year, end_year)
	except Exception:
		traceback.print_exc()
		raise

	report_path = write_report(summary)
	log(
		f"Completed: {summary['total_files_collected']} OC files saved across "
		f"{len(summary['years'])} year(s). Report: {report_path}"
	)


if __name__ == "__main__":
	main()
