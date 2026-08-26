import importlib
import os

import config


def test_snowflake_machine_id_respects_the_env_var_when_set():
    old_value = os.environ.get("SNOWFLAKE_MACHINE_ID")
    try:
        os.environ["SNOWFLAKE_MACHINE_ID"] = "42"
        importlib.reload(config)
        assert config.SNOWFLAKE_MACHINE_ID == 42
    finally:
        if old_value is None:
            os.environ.pop("SNOWFLAKE_MACHINE_ID", None)
        else:
            os.environ["SNOWFLAKE_MACHINE_ID"] = old_value
        importlib.reload(config)


def test_snowflake_machine_id_is_randomized_not_fixed_when_unset():
    # Regression guard: this used to default to a fixed "1" for every
    # process. Forgetting to set SNOWFLAKE_MACHINE_ID when scaling out to
    # multiple instances is an easy mistake (a single instance works fine
    # either way, so nothing fails locally) - a fixed default meant every
    # unconfigured instance would mint colliding ids in lockstep. A random
    # default at least turns a guaranteed collision into a low-probability one.
    old_value = os.environ.get("SNOWFLAKE_MACHINE_ID")
    try:
        os.environ.pop("SNOWFLAKE_MACHINE_ID", None)

        seen_values = set()
        for _ in range(30):
            importlib.reload(config)
            assert 0 <= config.SNOWFLAKE_MACHINE_ID <= 1023
            seen_values.add(config.SNOWFLAKE_MACHINE_ID)

        assert len(seen_values) > 1, "machine_id must not default to the same fixed value on every process start"
    finally:
        if old_value is None:
            os.environ.pop("SNOWFLAKE_MACHINE_ID", None)
        else:
            os.environ["SNOWFLAKE_MACHINE_ID"] = old_value
        importlib.reload(config)
