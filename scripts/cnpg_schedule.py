"""CloudNativePG schedule compatibility for the private renderer and live gate."""


def cnpg_backup_schedule(value: str, *, allow_five_fields: bool = False) -> str:
    fields = value.split()
    zone = fields.pop(0) if fields and fields[0].startswith(("TZ=", "CRON_TZ=")) else ""
    # Preserve cron descriptors for validation by the CloudNativePG controller.
    if not (fields and fields[0].startswith("@")):
        if allow_five_fields and len(fields) == 5:
            fields.insert(0, "0")
        if len(fields) != 6:
            raise ValueError("CNPG_BACKUP_SCHEDULE requires a six-field cron schedule including seconds")
    return " ".join(([zone] if zone else []) + fields)
