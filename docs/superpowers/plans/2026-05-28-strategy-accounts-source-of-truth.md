# Strategy Accounts Source Of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `strategy_accounts.yaml` the operator-facing source of truth for strategy enabled state, readonly visibility, signal participation, account routing, and order posture.

**Architecture:** Extend the existing `strategy_account_map_path` overlay so mapped `enabled` values override `strategies[].enabled` after app fragments and base configs are composed. Keep `live_readonly` from executing strategies by mode/endpoint behavior, but allow enabled strategies to appear in read-only operator views. Preserve config fingerprint behavior because the mapping file is already included in identity bytes.

**Tech Stack:** Python 3.11, Pydantic config models, YAML operator configs, pytest.

---

## File Structure

- Modify `src/maestro/config/strategy_account_mapping.py`: apply `enabled` from map entries onto strategy configs.
- Modify `src/maestro/config/models.py`: allow enabled strategies in `live_readonly` configs while retaining approval/order constraints.
- Modify `tests/test_config_validation.py`: add failing tests for `enabled` overlay and live_readonly display-enabled strategies.
- Modify `configs/operator/strategy_accounts.yaml` and `/root/maestro-operator/strategy_accounts.yaml`: add operator-facing `enabled` values.
- Modify docs if needed after behavior changes.

### Task 1: Overlay enabled from strategy account map

**Files:**
- Modify: `src/maestro/config/strategy_account_mapping.py`
- Test: `tests/test_config_validation.py`

- [ ] **Step 1: Add failing test**

Add to `tests/test_config_validation.py` near `test_shared_strategy_account_map_applies_phase_controls`:

```python
def test_shared_strategy_account_map_overrides_strategy_enabled(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy["enabled"] = False
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "ataraxia": {
                        "enabled": True,
                        "account_id": "kis_isa",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "snowball_us": {
                        "enabled": True,
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "trading_agents": {
                        "enabled": False,
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [(strategy.id, strategy.enabled) for strategy in config.strategies] == [
        ("ataraxia", True),
        ("snowball_us", True),
        ("trading_agents", False),
    ]
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_config_validation.py::test_shared_strategy_account_map_overrides_strategy_enabled -q
```

Expected: FAIL because the map does not yet override `enabled`.

- [ ] **Step 3: Implement overlay**

In `_apply_strategy_account_map`, after confirming `mapped_config` is a dict, add:

```python
if "enabled" in mapped_config:
    strategy_values["enabled"] = bool(mapped_config["enabled"])
```

- [ ] **Step 4: Verify GREEN**

Run the focused test again. Expected: PASS.

### Task 2: Allow enabled strategies in live_readonly as display state

**Files:**
- Modify: `src/maestro/config/models.py`
- Test: `tests/test_config_validation.py`

- [ ] **Step 1: Replace old rejection test**

Change `test_live_readonly_rejects_enabled_strategies` into `test_live_readonly_allows_enabled_strategies_for_operator_views` and assert `load_config` succeeds with an enabled strategy while approval/order constraints remain enforced by existing tests.

- [ ] **Step 2: Verify RED**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_config_validation.py::test_live_readonly_allows_enabled_strategies_for_operator_views -q
```

Expected: FAIL with `live_readonly mode does not run strategies`.

- [ ] **Step 3: Remove the live_readonly enabled-strategy rejection**

In `MaestroConfig.validate_mode_contract`, remove only this block:

```python
if enabled_strategies:
    raise ValueError(
        "live_readonly mode does not run strategies; disable: "
        + ", ".join(enabled_strategies)
    )
```

Keep approval, order posture, and enabled broker account validation unchanged.

- [ ] **Step 4: Verify GREEN**

Run the focused test. Expected: PASS.

### Task 3: Update operator configs and verify Dashboard behavior

**Files:**
- Modify: `configs/operator/strategy_accounts.yaml`
- Modify: `/root/maestro-operator/strategy_accounts.yaml`
- Test: `tests/test_config_validation.py`, Dashboard snapshot curl

- [ ] **Step 1: Add enabled fields**

Set:

```yaml
ataraxia.enabled: true
snowball_us.enabled: true
trading_agents.enabled: false
```

in both repo and live operator `strategy_accounts.yaml`.

- [ ] **Step 2: Run config tests**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_config_validation.py::test_operator_symphony_phase_configs_share_state_and_route_strategies tests/test_config_validation.py::test_shared_strategy_account_map_applies_phase_controls tests/test_config_validation.py::test_shared_strategy_account_map_overrides_strategy_enabled -q
```

Expected: PASS.

- [ ] **Step 3: Restart dashboard and inspect Virtuoso metrics**

Run:

```bash
systemctl restart maestro-dashboard.service
python -c 'import json, urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:8503/api/dashboard/snapshot?display_currency=KRW")); print(data["virtuoso_apps"]["metrics"]); print(data["virtuoso_apps"]["overview"])'
```

Expected: Enabled Apps is 2, Ataraxia and Snowball are enabled, TradingAgents remains disabled.

### Task 4: Final verification

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_config_validation.py tests/test_dashboard_server.py -q
/root/projects/Symphony/Maestro/.venv/bin/ruff check src/maestro/config/strategy_account_mapping.py src/maestro/config/models.py tests/test_config_validation.py
```

Expected: all pass.
