def validate_plan(
    *,
    profile: WorkbookProfile,
    plan: SpreadsheetQueryPlan,
) -> SpreadsheetQueryPlan:
    sheet = find_sheet(profile, plan.sheet_id)

    validate_metric_column(sheet, plan.metric)
    validate_group_column(sheet, plan.group_by_column_id)

    for item in plan.filters:
        validate_filter(sheet, item)

    return plan