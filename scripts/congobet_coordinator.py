from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Page, async_playwright

MATCHES_URL = "https://www.congobet.net/virtual/category/instant-league/8035/matches"
RESULTS_URL = "https://www.congobet.net/virtual/category/instant-league/8035/results"
HEADLESS = True
DEFAULT_TIMEOUT_MS = 10000
ROUND_INTERVAL_MINUTES = 2
MAX_ALLOWED_GAP_MINUTES = 3
ROOT = Path(__file__).resolve().parents[1]
ODDS_OUTPUT = ROOT / "congobet_combined_odds.json"
RESULTS_OUTPUT = ROOT / "cbet_results.json"
ARCHIVE_ODDS_DIR = ROOT / "archive" / "odds"
ARCHIVE_RESULTS_DIR = ROOT / "archive" / "results"
STATE_FILE = ROOT / "data" / "state" / "runtime_state.json"

for directory in [ARCHIVE_ODDS_DIR, ARCHIVE_RESULTS_DIR, STATE_FILE.parent]:
    directory.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def parse_hhmm(value: str) -> str | None:
    if not value:
        return None
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", value)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def shift_hhmm(value: str, minutes: int) -> str:
    base = datetime.strptime(value, "%H:%M")
    shifted = base + timedelta(minutes=minutes)
    return shifted.strftime("%H:%M")


def minute_gap(previous_time: str, current_time: str) -> int:
    prev_dt = datetime.strptime(previous_time, "%H:%M")
    curr_dt = datetime.strptime(current_time, "%H:%M")
    if curr_dt < prev_dt:
        curr_dt += timedelta(days=1)
    return int((curr_dt - prev_dt).total_seconds() // 60)


def compute_gap_journees(gap_minutes: int) -> int:
    if gap_minutes <= 0:
        return 1
    gap_journees = gap_minutes / ROUND_INTERVAL_MINUTES
    rounded_gap = round(gap_journees)
    if abs(gap_journees - rounded_gap) < 1e-9:
        return max(1, int(rounded_gap))
    return max(1, int(gap_journees))


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def archive_path(base_dir: Path, prefix: str) -> Path:
    now = utc_now()
    folder = base_dir / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{prefix}_{now.strftime('%Y%m%dT%H%M%SZ')}.json"


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "last_started_odds_hash": None,
            "pending_cycle": None,
            "last_completed_cycle_id": None,
            "last_results_hash": None,
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def safe_click(locator, timeout: int = 8000) -> bool:
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(timeout=timeout)
        return True
    except Exception:
        return False


async def wait_for_matches(page: Page, selector: str = "div.match", minimum: int = 1, timeout_ms: int = 15000) -> None:
    end_time = time.time() + timeout_ms / 1000
    last_error: Exception | None = None
    while time.time() < end_time:
        try:
            count = await page.locator(selector).count()
            if count >= minimum:
                return
        except Exception as exc:
            last_error = exc
        await page.wait_for_timeout(300)
    if last_error:
        raise last_error
    raise TimeoutError(f"Les matchs ne se sont pas chargés à temps pour le sélecteur: {selector}")


async def wait_for_results(page: Page, minimum: int = 1, timeout_ms: int = 20000) -> None:
    deadline = time.time() + timeout_ms / 1000
    selector = "hg-instant-league-results .result-container"
    while time.time() < deadline:
        count = await page.locator(selector).count()
        if count >= minimum:
            return
        await asyncio.sleep(0.3)
    raise TimeoutError("Les résultats ne se sont pas chargés à temps.")


def get_round_tabs(page: Page):
    return page.locator("hg-instant-league-round-picker li")


async def get_round_time(tab) -> str:
    try:
        return clean_text(await tab.locator(".time").inner_text(timeout=3000))
    except Exception:
        return clean_text(await tab.inner_text(timeout=3000))


async def switch_section_tab(page: Page, label: str) -> None:
    candidates = [
        page.get_by_text(label, exact=True),
        page.locator("button", has_text=label),
        page.locator("a", has_text=label),
        page.locator("li", has_text=label),
        page.locator("div", has_text=label),
    ]
    for locator in candidates:
        try:
            if await locator.count() <= 0:
                continue
            if await safe_click(locator.first):
                await page.wait_for_timeout(1200)
                return
        except Exception:
            continue
    raise RuntimeError(f"Impossible d'ouvrir l'onglet {label}.")


async def extract_results_reference(page: Page) -> dict[str, Any]:
    await switch_section_tab(page, "Résultats")

    end_time = time.time() + 15
    last_text = ""
    pattern = re.compile(r"Journée\s*(\d+)\s*-\s*(?:Aujourd['’]hui\s*)?((?:[01]?\d|2[0-3]):[0-5]\d)", re.IGNORECASE)

    while time.time() < end_time:
        try:
            body_text = clean_text(await page.locator("body").inner_text(timeout=3000))
        except Exception:
            body_text = ""
        last_text = body_text
        match = pattern.search(body_text)
        if match:
            return {
                "journee": int(match.group(1)),
                "results_time": match.group(2),
                "header_text": match.group(0),
                "is_empty": False,
            }
        await page.wait_for_timeout(400)

    print(f"[WARN] Onglet Résultats sans en-tête exploitable. Texte observé: {last_text[:250]}")
    return {"journee": None, "results_time": None, "header_text": None, "is_empty": True}


def trim_schedule_to_active_sequence(round_schedule: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(round_schedule) <= 1:
        return round_schedule

    segments: list[list[dict[str, Any]]] = []
    current_segment = [round_schedule[0]]
    for current in round_schedule[1:]:
        previous = current_segment[-1]
        gap = minute_gap(previous["round_time"], current["round_time"])
        if gap >= MAX_ALLOWED_GAP_MINUTES:
            segments.append(current_segment)
            current_segment = [current]
        else:
            current_segment.append(current)
    segments.append(current_segment)

    best = max(segments, key=lambda seg: (len(seg), seg[-1]["journee"]))
    if best != round_schedule:
        print(
            f"[INFO] Séquence active retenue: journée {best[0]['journee']}->{best[-1]['journee']} | "
            f"{best[0]['round_time']}->{best[-1]['round_time']}"
        )
    return best


async def build_round_schedule(page: Page, previous_journee: int | None, previous_results_time: str | None) -> list[dict[str, Any]]:
    await switch_section_tab(page, "Matchs")
    await wait_for_matches(page)

    tabs = get_round_tabs(page)
    total_tabs = await tabs.count()
    if total_tabs <= 0:
        return []

    tab_infos: list[dict[str, Any]] = []
    for round_index in range(total_tabs):
        tab = tabs.nth(round_index)
        raw_label = await get_round_time(tab)
        tab_infos.append({
            "round_index": round_index,
            "raw_label": raw_label,
            "extracted_time": parse_hhmm(raw_label),
        })

    first_visible_time = next((item["extracted_time"] for item in tab_infos if item["extracted_time"]), None)
    second_round_time = tab_infos[1]["extracted_time"] if total_tabs > 1 else None
    first_round_time = tab_infos[0]["extracted_time"]

    if not first_round_time and second_round_time:
        first_round_time = shift_hhmm(second_round_time, -ROUND_INTERVAL_MINUTES)
    elif not first_round_time and previous_results_time:
        first_round_time = shift_hhmm(previous_results_time, ROUND_INTERVAL_MINUTES)
    elif not first_round_time and first_visible_time:
        first_round_time = first_visible_time

    if not first_round_time:
        raise RuntimeError("Impossible de déterminer l'heure du premier round.")

    if previous_journee is None or previous_results_time is None:
        first_round_journee = 1
        second_round_journee = first_round_journee + 1 if total_tabs > 1 else None
    else:
        if second_round_time:
            gap_minutes = minute_gap(previous_results_time, second_round_time)
            gap_journees = compute_gap_journees(gap_minutes)
            second_round_journee = previous_journee + gap_journees
            first_round_journee = second_round_journee - 1
        else:
            first_round_journee = previous_journee + 1
            second_round_journee = first_round_journee + 1 if total_tabs > 1 else None

    round_schedule: list[dict[str, Any]] = []
    previous_time = first_round_time
    previous_journee_value = first_round_journee

    for round_index, tab_info in enumerate(tab_infos):
        raw_label = tab_info["raw_label"]
        extracted_time = tab_info["extracted_time"]

        if round_index == 0:
            round_time = first_round_time
            journee = first_round_journee
        elif round_index == 1:
            round_time = extracted_time or shift_hhmm(first_round_time, ROUND_INTERVAL_MINUTES)
            journee = second_round_journee if second_round_journee is not None else previous_journee_value + 1
        else:
            round_time = extracted_time or shift_hhmm(previous_time, ROUND_INTERVAL_MINUTES)
            gap = minute_gap(previous_time, round_time)
            if gap >= MAX_ALLOWED_GAP_MINUTES and round_index >= 2:
                print(f"[STOP] Écart de {gap} min détecté entre {previous_time} et {round_time}.")
                break
            journee = previous_journee_value + 1

        round_schedule.append({
            "round_index": round_index,
            "round_time": round_time,
            "journee": journee,
            "raw_label": raw_label,
        })
        previous_time = round_time
        previous_journee_value = journee

    return trim_schedule_to_active_sequence(round_schedule)


async def ensure_market_selected(page: Page, market_label: str) -> None:
    active_button = page.locator("hg-event-bet-type-picker button.active", has_text=market_label)
    if await active_button.count() > 0:
        return

    visible_button = page.locator("hg-event-bet-type-picker button", has_text=market_label)
    if await visible_button.count() > 0 and await safe_click(visible_button.first):
        await page.wait_for_timeout(1200)
        return

    select_box = page.locator("hg-event-bet-type-picker hg-select .selected").first
    if not await safe_click(select_box):
        raise RuntimeError(f"Impossible d'ouvrir le sélecteur pour le marché {market_label}.")

    option = page.locator("hg-event-bet-type-picker hg-select .dropdown .option", has_text=market_label).first
    if not await safe_click(option, timeout=10000):
        raise RuntimeError(f"Impossible de sélectionner le marché {market_label}.")

    await page.wait_for_timeout(1500)


async def extract_gng_labels(page: Page) -> list[str]:
    candidates: list[str] = []
    for selector in ["div[class*='header'] span", "div[class*='header'] div"]:
        try:
            texts = await page.locator(selector).all_inner_texts()
        except Exception:
            continue
        for text in texts:
            cleaned = clean_text(text)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
    filtered = [x for x in candidates if x.lower() in {"oui", "non"}]
    return filtered[:2] if len(filtered) >= 2 else ["Oui", "Non"]


async def extract_1x2_for_current_round(page: Page, round_time: str, round_index: int, journee: int) -> list[dict[str, Any]]:
    match_locator = page.locator("div.match.bet-type-1x2")
    rows: list[dict[str, Any]] = []

    for i in range(await match_locator.count()):
        card = match_locator.nth(i)
        team_spans = card.locator(".teams span")
        odd_spans = card.locator("span.odds")
        teams = [clean_text(await team_spans.nth(j).inner_text()) for j in range(await team_spans.count()) if clean_text(await team_spans.nth(j).inner_text())]
        odds = [clean_text(await odd_spans.nth(j).inner_text()) for j in range(await odd_spans.count()) if clean_text(await odd_spans.nth(j).inner_text())]
        if len(teams) >= 2 and len(odds) >= 3:
            rows.append({
                "unique_key": f"{round_time}|{teams[0]}|{teams[1]}",
                "round_index": round_index,
                "round_time": round_time,
                "journee": journee,
                "teams": {"home": teams[0], "away": teams[1]},
                "market": "1X2",
                "odds": {"1": odds[0], "X": odds[1], "2": odds[2]},
            })
    return rows


async def extract_gng_for_current_round(page: Page, round_time: str, round_index: int, journee: int) -> list[dict[str, Any]]:
    labels = await extract_gng_labels(page)
    cards = page.locator("div.match")
    rows: list[dict[str, Any]] = []

    for idx in range(await cards.count()):
        card = cards.nth(idx)
        teams = [clean_text(x) for x in await card.locator(".teams span").all_inner_texts() if clean_text(x)]
        odds = [clean_text(x) for x in await card.locator("hg-event-bet-type-item .odds").all_inner_texts() if clean_text(x)]
        if len(teams) < 2 or len(odds) < 2:
            continue
        rows.append({
            "unique_key": f"{round_time}|{teams[0]}|{teams[1]}",
            "round_index": round_index,
            "round_time": round_time,
            "journee": journee,
            "teams": {"home": teams[0], "away": teams[1]},
            "market": "G/NG",
            "odds": {labels[0]: odds[0], labels[1]: odds[1]},
        })
    return rows


def merge_market_rows(rows_1x2: list[dict[str, Any]], rows_gng: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows_1x2 + rows_gng:
        key = row["unique_key"]
        if key not in merged:
            merged[key] = {
                "unique_key": key,
                "round_index": row["round_index"],
                "round_time": row["round_time"],
                "journee": row.get("journee"),
                "teams": row["teams"],
                "markets": {},
            }
        merged[key]["markets"][row["market"]] = row["odds"]
    return list(merged.values())


async def extract_round_combined(page: Page, round_plan: dict[str, Any]) -> dict[str, Any]:
    tabs = get_round_tabs(page)
    round_index = round_plan["round_index"]
    if round_index >= await tabs.count():
        raise IndexError(f"round_index {round_index} hors limite")

    tab = tabs.nth(round_index)
    if not await safe_click(tab):
        await safe_click(tab.locator(".infos").first)

    await page.wait_for_timeout(1200)
    await wait_for_matches(page)
    await ensure_market_selected(page, "1X2")
    await wait_for_matches(page, selector="div.match.bet-type-1x2")
    rows_1x2 = await extract_1x2_for_current_round(page, round_plan["round_time"], round_index, round_plan["journee"])

    await ensure_market_selected(page, "G/NG")
    await wait_for_matches(page)
    rows_gng = await extract_gng_for_current_round(page, round_plan["round_time"], round_index, round_plan["journee"])
    matches = merge_market_rows(rows_1x2, rows_gng)

    return {
        "round_index": round_index,
        "round_time": round_plan["round_time"],
        "journee": round_plan["journee"],
        "matches_count": len(matches),
        "matches": matches,
    }


def flatten_rounds(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for rnd in rounds:
        flat.extend(rnd.get("matches", []))
    return flat


async def scrape_odds_payload() -> tuple[dict[str, Any], dict[str, Any]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        page = await browser.new_page(locale="fr-FR", viewport={"width": 1600, "height": 2400})
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        await page.goto(MATCHES_URL, wait_until="networkidle", timeout=120000)
        await page.wait_for_timeout(3000)
        await wait_for_matches(page)

        title_locator = page.locator("div.title-wrapper span").first
        competition = clean_text(await title_locator.inner_text()) if await page.locator("div.title-wrapper span").count() else "Instant League"
        results_reference = await extract_results_reference(page)
        round_schedule = await build_round_schedule(page, results_reference["journee"], results_reference["results_time"])

        processed_rounds: list[dict[str, Any]] = []
        skipped_rounds: list[str] = []
        for round_plan in round_schedule:
            try:
                round_data = await extract_round_combined(page, round_plan)
                processed_rounds.append(round_data)
                print(f"[ODDS] journée {round_data['journee']} | {round_data['round_time']} | {round_data['matches_count']} matchs")
            except PlaywrightTimeoutError as exc:
                skipped_rounds.append(f"index_{round_plan['round_index']}: timeout ({exc})")
            except Exception as exc:
                skipped_rounds.append(f"index_{round_plan['round_index']}: {exc}")

        await browser.close()

    flat_matches = flatten_rounds(processed_rounds)
    payload = {
        "source": {
            "site": "CongoBet",
            "url": MATCHES_URL,
            "competition": competition,
            "markets": ["1X2", "G/NG"],
        },
        "metadata": {
            "scraped_at_utc": iso_now(),
            "rounds_detected": len(round_schedule),
            "rounds_processed": len(processed_rounds),
            "rounds_skipped": skipped_rounds,
            "matches_count": len(flat_matches),
            "json_local_path": ODDS_OUTPUT.name,
            "json_drive_path": None,
        },
        "rounds": processed_rounds,
        "matches": flat_matches,
    }

    context = {
        "results_reference": results_reference,
        "last_round": processed_rounds[-1] if processed_rounds else None,
        "match_keys": [
            {
                "journee": match.get("journee"),
                "home": match.get("teams", {}).get("home"),
                "away": match.get("teams", {}).get("away"),
            }
            for match in flat_matches
        ],
    }
    return payload, context


def parse_score(score_text: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\s*[:\-]\s*(\d+)", clean_text(score_text))
    if not match:
        raise ValueError(f"Score introuvable dans: {score_text!r}")
    return int(match.group(1)), int(match.group(2))


def derive_gng(home_score: int, away_score: int) -> str:
    return "Oui" if home_score > 0 and away_score > 0 else "Non"


def parse_minutes(value: str) -> list[str]:
    value = clean_text(value)
    return re.findall(r"\d+'(?:\+\d+)?", value) if value else []


async def click_show_more_until_end(page: Page) -> int:
    clicks = 0
    results_selector = "hg-instant-league-results .result-container"
    while True:
        button = page.locator("text=/Afficher plus/i")
        if await button.count() == 0:
            break
        before_count = await page.locator(results_selector).count()
        current_button = button.first
        await current_button.scroll_into_view_if_needed()
        await current_button.click(timeout=10000)
        clicks += 1
        deadline = time.time() + 20
        while time.time() < deadline:
            after_count = await page.locator(results_selector).count()
            if after_count > before_count or await page.locator("text=/Afficher plus/i").count() == 0:
                break
            await asyncio.sleep(0.4)
        await asyncio.sleep(1.2)
    return clicks


async def extract_competition(page: Page) -> str:
    body_text = clean_text(await page.locator("body").inner_text())
    lines = [clean_text(line) for line in body_text.splitlines() if clean_text(line)]
    for idx, line in enumerate(lines):
        if line.upper() == "RÉSULTATS" and idx > 0:
            previous = clean_text(lines[idx - 1])
            if previous and previous.upper() not in {"VIRTUEL", "PROMOS", "FAQ"}:
                return previous
    for selector in ["div.title-wrapper span", "hg-entrypoint-title .title-wrapper span", "hg-entrypoint-title span"]:
        locator = page.locator(selector)
        if await locator.count() > 0:
            texts = [clean_text(x) for x in await locator.all_inner_texts() if clean_text(x)]
            for text in texts:
                if text and text.upper() not in {"RÉSULTATS", "MATCHS", "CLASSEMENT"}:
                    return text
    return "Instant League"


async def extract_result_rows(page: Page) -> list[dict[str, Any]]:
    containers = page.locator("hg-instant-league-results .result-container")
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for i in range(await containers.count()):
        container = containers.nth(i)
        round_label = clean_text(await container.locator(".header").inner_text())
        matchday_match = re.search(r"Journée\s+(\d+)", round_label, flags=re.IGNORECASE)
        matchday = int(matchday_match.group(1)) if matchday_match else None
        round_time = clean_text(round_label.split("-", 1)[1]) if "-" in round_label else ""
        rows = container.locator(".match-results .row")

        for j in range(await rows.count()):
            row = rows.nth(j)
            teams = [clean_text(x) for x in await row.locator(".team span").all_inner_texts() if clean_text(x)]
            if len(teams) < 2:
                continue
            score_text = clean_text(await row.locator(".match-score").inner_text())
            halftime_text = clean_text(await row.locator(".halfTime-score").inner_text()).replace("MT:", "").strip()
            home_score, away_score = parse_score(score_text)
            home_ht, away_ht = parse_score(halftime_text) if halftime_text else (None, None)
            home_goal_minutes = []
            away_goal_minutes = []
            try:
                home_goal_minutes = parse_minutes(await row.locator(".haltTime-goals.home span").inner_text())
            except Exception:
                pass
            try:
                away_goal_minutes = parse_minutes(await row.locator(".haltTime-goals.away span").inner_text())
            except Exception:
                pass
            gng_result = derive_gng(home_score, away_score)
            unique_key = f"{matchday}|{teams[0]}|{teams[1]}|{score_text}"
            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)
            records.append({
                "unique_key": unique_key,
                "round_label": round_label,
                "matchday": matchday,
                "round_time": round_time,
                "home_team": teams[0],
                "away_team": teams[1],
                "score": score_text,
                "home_score": home_score,
                "away_score": away_score,
                "halftime_score": halftime_text,
                "home_halftime_score": home_ht,
                "away_halftime_score": away_ht,
                "home_goal_minutes": home_goal_minutes,
                "away_goal_minutes": away_goal_minutes,
                "both_teams_scored": gng_result == "Oui",
                "gng_result": gng_result,
            })
    return records


async def scrape_results_payload() -> dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1600, "height": 2600})
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        await page.goto(RESULTS_URL, wait_until="networkidle", timeout=120000)
        await asyncio.sleep(3)
        await wait_for_results(page)
        competition = await extract_competition(page)
        show_more_clicks = await click_show_more_until_end(page)
        await wait_for_results(page, minimum=1)
        records = await extract_result_rows(page)
        round_labels = [clean_text(x) for x in await page.locator("hg-instant-league-results .result-container .header").all_inner_texts()]
        await browser.close()

    return {
        "source": {
            "site": "CongoBet",
            "url": RESULTS_URL,
            "competition": competition,
            "category": "Instant League",
            "market": "G/NG (résultat dérivé depuis le score final)",
            "rule": "Oui si les deux équipes marquent au moins un but, sinon Non.",
        },
        "metadata": {
            "scraped_at_utc": iso_now(),
            "show_more_clicks": show_more_clicks,
            "rounds_count": len(round_labels),
            "round_labels": round_labels,
            "records_count": len(records),
            "deduplication": "unique_key = matchday|home_team|away_team|score",
        },
        "matches": records,
    }


def build_pending_cycle(odds_payload: dict[str, Any]) -> dict[str, Any] | None:
    rounds = odds_payload.get("rounds", [])
    matches = odds_payload.get("matches", [])
    if not rounds or not matches:
        return None
    last_round = rounds[-1]
    cycle_core = {
        "last_round_time": last_round["round_time"],
        "last_journee": last_round["journee"],
        "match_keys": [
            {
                "journee": match.get("journee"),
                "home": match.get("teams", {}).get("home"),
                "away": match.get("teams", {}).get("away"),
            }
            for match in matches
        ],
    }
    cycle_hash = stable_hash(cycle_core)
    return {
        "cycle_id": cycle_hash[:16],
        "created_at_utc": iso_now(),
        "target_round_time": last_round["round_time"],
        "target_journee": last_round["journee"],
        "target_match_count": len(matches),
        "target_match_keys": cycle_core["match_keys"],
        "odds_hash": cycle_hash,
    }


def filter_results_to_pending(raw_payload: dict[str, Any], pending_cycle: dict[str, Any]) -> dict[str, Any]:
    target_keys = {
        (item["journee"], normalize_name(item["home"]), normalize_name(item["away"]))
        for item in pending_cycle["target_match_keys"]
        if item.get("journee") is not None and item.get("home") and item.get("away")
    }
    matched_records = [
        record
        for record in raw_payload["matches"]
        if (record.get("matchday"), normalize_name(record.get("home_team", "")), normalize_name(record.get("away_team", ""))) in target_keys
    ]

    seen_labels: set[str] = set()
    matched_round_labels: list[str] = []
    for record in matched_records:
        label = record.get("round_label")
        if label and label not in seen_labels:
            seen_labels.add(label)
            matched_round_labels.append(label)

    return {
        "source": raw_payload["source"],
        "metadata": {
            "scraped_at_utc": raw_payload["metadata"]["scraped_at_utc"],
            "show_more_clicks": raw_payload["metadata"]["show_more_clicks"],
            "rounds_count": len(matched_round_labels),
            "round_labels": matched_round_labels,
            "records_count": len(matched_records),
            "deduplication": raw_payload["metadata"]["deduplication"],
        },
        "matches": matched_records,
    }


def save_odds_outputs(odds_payload: dict[str, Any]) -> None:
    write_json(ODDS_OUTPUT, odds_payload)
    write_json(archive_path(ARCHIVE_ODDS_DIR, "congobet_combined_odds"), odds_payload)


def save_results_outputs(results_payload: dict[str, Any]) -> None:
    write_json(RESULTS_OUTPUT, results_payload)
    write_json(archive_path(ARCHIVE_RESULTS_DIR, "cbet_results"), results_payload)


async def maybe_start_new_cycle(state: dict[str, Any]) -> dict[str, Any]:
    odds_payload, _ = await scrape_odds_payload()
    if not odds_payload.get("matches"):
        print("[INFO] Aucun match de cote extrait.")
        return state

    pending_cycle = build_pending_cycle(odds_payload)
    if pending_cycle is None:
        print("[INFO] Aucun cycle exploitable détecté.")
        return state

    if pending_cycle["odds_hash"] == state.get("last_started_odds_hash"):
        print("[INFO] Même cycle de cotes déjà démarré, aucun redémarrage.")
        return state

    save_odds_outputs(odds_payload)
    state["last_started_odds_hash"] = pending_cycle["odds_hash"]
    state["pending_cycle"] = pending_cycle
    print(f"[CYCLE] Nouveau cycle démarré: {pending_cycle['cycle_id']} | cible journée {pending_cycle['target_journee']} à {pending_cycle['target_round_time']}")
    return state


async def maybe_complete_pending_cycle(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    pending_cycle = state.get("pending_cycle")
    if not pending_cycle:
        return state, False

    raw_results_payload = await scrape_results_payload()
    filtered_results = filter_results_to_pending(raw_results_payload, pending_cycle)
    matched = filtered_results["metadata"]["records_count"]
    expected = pending_cycle["target_match_count"]

    if matched < expected:
        print(f"[WAIT] Résultats incomplets pour le cycle {pending_cycle['cycle_id']}: {matched}/{expected}")
        return state, False

    results_hash = stable_hash(filtered_results["matches"])
    if results_hash != state.get("last_results_hash"):
        save_results_outputs(filtered_results)
        state["last_results_hash"] = results_hash

    state["last_completed_cycle_id"] = pending_cycle["cycle_id"]
    state["pending_cycle"] = None
    print(f"[DONE] Cycle terminé: {pending_cycle['cycle_id']} | résultats {matched}/{expected}")
    return state, True


async def main() -> None:
    state = load_state()

    if state.get("pending_cycle"):
        state, completed = await maybe_complete_pending_cycle(state)
        save_state(state)
        if completed:
            state = await maybe_start_new_cycle(state)
            save_state(state)
        return

    state = await maybe_start_new_cycle(state)
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
