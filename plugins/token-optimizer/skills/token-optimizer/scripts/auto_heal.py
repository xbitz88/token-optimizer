#!/usr/bin/env python3
"""Token Optimizer — Standalone Auto-Heal & Self-Repair Routine.

Automated background maintenance for Hermes and Antigravity:
1. Prunes unreferenced stale staging and backup directories (.old.*).
2. Verifies and repairs plugin hook paths and measure-path locators.
3. Sanitizes trends.db against un-capped idle duration outliers (>1440m).
4. Keeps dashboard.html and trends rollups freshly synchronized offline.
5. NEVER launches persistent background daemons (0% idle CPU).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def _detect_runtime() -> str:
    explicit = os.environ.get("TOKEN_OPTIMIZER_RUNTIME", "").strip().lower()
    if explicit in ("hermes", "antigravity", "codex", "claude"):
        return explicit
    if os.environ.get("HERMES_HOME"):
        return "hermes"
    # Check if inside Hermes directory
    script_str = str(Path(__file__).resolve())
    if "hermes" in script_str.lower():
        return "hermes"
    return "antigravity"


def _resolve_paths(runtime: str) -> dict[str, Any]:
    user_home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())

    if runtime == "hermes":
        hermes_home_env = os.environ.get("HERMES_HOME")
        if hermes_home_env:
            hermes_home = Path(hermes_home_env)
        elif os.name == "nt":
            hermes_home = Path(os.environ.get("LOCALAPPDATA", str(user_home / "AppData" / "Local"))) / "hermes"
        else:
            hermes_home = user_home / ".hermes"

        plugins_dir = hermes_home / "plugins"
        plugin_dir = plugins_dir / "token-optimizer"
        backups_dir = hermes_home / "_backups" / "token-optimizer"
        trends_db = backups_dir / "trends.db"
        dashboard_html = backups_dir / "dashboard.html"

        measure_py = None
        locator = plugin_dir / "measure-path"
        if locator.is_file():
            try:
                measure_py = Path(locator.read_text(encoding="utf-8").strip())
            except Exception:
                pass
        if not measure_py or not measure_py.is_file():
            if os.name == "nt":
                candidate = Path(r"C:\dev\wizardinglab-token_optimizer\skills\token-optimizer\scripts\measure.py")
                if candidate.is_file():
                    measure_py = candidate
            else:
                candidate = user_home / "dev" / "token-optimizer" / "skills" / "token-optimizer" / "scripts" / "measure.py"
                if candidate.is_file():
                    measure_py = candidate

        return {
            "runtime": "hermes",
            "home": hermes_home,
            "plugins_dir": plugins_dir,
            "plugin_dir": plugin_dir,
            "backups_dir": backups_dir,
            "trends_db": trends_db,
            "dashboard_html": dashboard_html,
            "measure_py": measure_py,
        }
    else:
        gemini_home = user_home / ".gemini"
        plugins_dir = gemini_home / "config" / "plugins"
        plugin_dir = plugins_dir / "token-optimizer"
        backups_dir = gemini_home / "_backups" / "token-optimizer"
        trends_db = backups_dir / "trends.db"
        dashboard_html = backups_dir / "dashboard.html"
        measure_py = plugin_dir / "skills" / "token-optimizer" / "scripts" / "measure.py"

        return {
            "runtime": "antigravity",
            "home": gemini_home,
            "plugins_dir": plugins_dir,
            "plugin_dir": plugin_dir,
            "backups_dir": backups_dir,
            "trends_db": trends_db,
            "dashboard_html": dashboard_html,
            "measure_py": measure_py,
        }


def clean_stale_directories(plugins_dir: Path, max_age_seconds: int = 86400) -> list[str]:
    actions = []
    if not plugins_dir or not plugins_dir.is_dir():
        return actions

    referenced_names = set()
    for conf_file in plugins_dir.glob("**/hooks.json"):
        try:
            content = conf_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"\.token-optimizer\.[a-zA-Z0-9_.]+", content):
                referenced_names.add(m.group(0))
        except Exception:
            pass

    now = time.time()
    for item in plugins_dir.iterdir():
        if not item.is_dir():
            continue
        if item.name in referenced_names:
            continue
        if item.name.startswith(".token-optimizer.old."):
            try:
                age = now - item.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(item, ignore_errors=True)
                    actions.append(f"Pruned stale folder: {item.name}")
            except Exception:
                pass
    return actions


def repair_plugin_wiring(paths: dict[str, Any]) -> list[str]:
    actions = []
    runtime = paths["runtime"]
    plugin_dir = paths["plugin_dir"]

    if runtime == "antigravity":
        hooks_file = plugin_dir / "hooks.json"
        if hooks_file.is_file():
            try:
                content = hooks_file.read_text(encoding="utf-8")
                if ".token-optimizer.stage." in content:
                    fixed = re.sub(r"\.token-optimizer\.stage\.[^\\]+", "token-optimizer", content)
                    hooks_file.write_text(fixed, encoding="utf-8")
                    actions.append("Repaired hooks.json paths to permanent plugin directory")
            except Exception as exc:
                actions.append(f"Failed inspecting hooks.json: {exc}")

    elif runtime == "hermes":
        locator = plugin_dir / "measure-path"
        measure_py = paths.get("measure_py")
        if locator.is_file() and measure_py and measure_py.is_file():
            try:
                current_target = locator.read_text(encoding="utf-8").strip()
                if not Path(current_target).is_file():
                    locator.write_text(f"{measure_py}\n", encoding="utf-8")
                    actions.append(f"Repaired measure-path to {measure_py}")
            except Exception as exc:
                actions.append(f"Locator check note: {exc}")

    return actions


def sanitize_all_trends_dbs(paths: dict[str, Any]) -> list[str]:
    actions = []
    home = paths["home"]

    target_dbs = []
    primary_db = paths.get("trends_db")
    if primary_db and primary_db.is_file():
        target_dbs.append(primary_db)

    # Search profile trends.db as well
    if home and home.is_dir():
        for candidate in home.glob("profiles/*/_backups/token-optimizer/trends.db"):
            if candidate.is_file() and candidate not in target_dbs:
                target_dbs.append(candidate)

    for db_path in target_dbs:
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            cur = conn.cursor()
            cur.execute("UPDATE session_log SET duration_minutes = 1440.0 WHERE duration_minutes > 1440.0")
            if cur.rowcount > 0:
                conn.commit()
                actions.append(f"Capped {cur.rowcount} outlier session duration(s) to 1440m in {db_path.name}")
            conn.close()
        except Exception as exc:
            actions.append(f"Database sanitation note ({db_path}): {exc}")

    return actions


def refresh_rollups_and_dashboard(paths: dict[str, Any]) -> list[str]:
    actions = []
    measure_py = paths.get("measure_py")
    if not measure_py or not measure_py.is_file():
        return actions

    runtime = paths["runtime"]
    try:
        env = {
            **os.environ,
            "TOKEN_OPTIMIZER_RUNTIME": runtime,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        if runtime == "hermes":
            env["HERMES_HOME"] = str(paths["home"])
            rollup_cmd = "hermes-rollup"
        else:
            rollup_cmd = "antigravity-rollup"

        subprocess.run(
            [sys.executable, str(measure_py), rollup_cmd, "--quiet"],
            env=env,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [sys.executable, str(measure_py), "dashboard", "--quiet"],
            env=env,
            capture_output=True,
            timeout=30,
        )
        actions.append(f"Refreshed {runtime} rollups and offline dashboard HTML")
    except Exception as exc:
        actions.append(f"Refresh notice: {exc}")
    return actions


def run_auto_heal(quiet: bool = False, full_refresh: bool = False, runtime: str | None = None) -> dict:
    rt = runtime or _detect_runtime()
    paths = _resolve_paths(rt)
    all_actions = []

    all_actions.extend(clean_stale_directories(paths.get("plugins_dir")))
    all_actions.extend(repair_plugin_wiring(paths))
    all_actions.extend(sanitize_all_trends_dbs(paths))

    if full_refresh:
        all_actions.extend(refresh_rollups_and_dashboard(paths))

    status = {
        "timestamp": time.time(),
        "runtime": rt,
        "status": "healthy",
        "actions_taken": all_actions,
    }

    if not quiet:
        print(f"[Token Optimizer Auto-Heal] Runtime: {rt}")
        if all_actions:
            for act in all_actions:
                print(f"  [REPAIRED] {act}")
        else:
            print("  [OK] All systems healthy, no repairs needed.")

    return status


def main():
    parser = argparse.ArgumentParser(description="Token Optimizer Auto-Healer")
    parser.add_argument("--quiet", action="store_true", help="Suppress console output")
    parser.add_argument("--full", action="store_true", help="Also trigger complete rollup and dashboard regeneration")
    parser.add_argument("--runtime", choices=["hermes", "antigravity", "codex", "claude"], help="Force specific runtime")
    args = parser.parse_args()

    run_auto_heal(quiet=args.quiet, full_refresh=args.full, runtime=args.runtime)


if __name__ == "__main__":
    main()
