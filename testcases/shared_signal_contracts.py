"""Platform-neutral value contracts shared by Android and iOS validators."""


def epoch_history_contract(value, field, allow_empty):
    """Return the shared lifecycle-history expectation, observation, and failures."""
    failures = []
    if not isinstance(value, list):
        failures.append(f"ext.user.{field} must be an array")
    else:
        if not allow_empty and not value:
            failures.append(f"ext.user.{field} must contain the current lifecycle timestamp")
        valid_items = all(type(item) is int and item > 0 for item in value)
        if not valid_items:
            failures.append(f"ext.user.{field} must contain positive Unix epoch milliseconds")
        if valid_items and any(left >= right for left, right in zip(value, value[1:])):
            failures.append(f"ext.user.{field} must be strictly increasing")
    expected = {
        "type": "array of strictly increasing Unix epoch milliseconds",
        "empty_allowed": allow_empty,
        "cross_platform_contract": True,
    }
    actual = {
        "timestamp_count": len(value) if isinstance(value, list) else None,
        "timestamps": value,
    }
    return expected, actual, failures


def epoch_history_comparison_view(actual):
    """Describe the shared report presentation without prescribing capture paths."""
    return {
        "kind": "rule",
        "criterion": (
            "The decoded Bid Extended value must be an array of positive integer "
            "Unix epoch milliseconds in strictly increasing order."
        ),
        "actual": {"label": "Decoded Bid Extended", "value": actual},
    }
