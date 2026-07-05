from collections import defaultdict
from decimal import Decimal, InvalidOperation


def execute_query(
    *,
    file_path,
    profile,
    plan,
) -> dict:
    worksheet = open_selected_sheet(file_path, plan.sheet_id)

    totals = defaultdict(lambda: Decimal("0"))
    row_count = defaultdict(int)
    rows_scanned = 0
    rows_matched = 0
    ignored_numeric_rows = 0

    for row in iter_data_rows(worksheet, profile, plan.sheet_id):
        rows_scanned += 1

        if not matches_filters(row, plan.filters):
            continue

        rows_matched += 1
        group_key = row.get(plan.group_by_column_id, "全部")

        if plan.metric.operation == "count_rows":
            row_count[group_key] += 1
            continue

        value = to_decimal(row.get(plan.metric.column_id))
        if value is None:
            ignored_numeric_rows += 1
            continue

        totals[group_key] += value

    return build_result(
        plan=plan,
        totals=totals,
        row_count=row_count,
        rows_scanned=rows_scanned,
        rows_matched=rows_matched,
        ignored_numeric_rows=ignored_numeric_rows,
    )