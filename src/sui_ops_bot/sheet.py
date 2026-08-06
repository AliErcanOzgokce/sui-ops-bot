"""Google Sheet store, the shared source of truth.

The sheet itself holds the state: three infra columns (Slack Channel, Slack TS,
Bot Refs) plus the two classification columns (Product, Type) let the in-memory
index be rebuilt on every boot, so redeploys on ephemeral hosts never lose
tracking. Both entrypoints construct a single :class:`SheetStore`.
"""
from __future__ import annotations

import json
import threading

import gspread
from google.oauth2.service_account import Credentials

from . import config
from .logutil import log


class Row:
    """A tracked sheet row plus the Slack references the bot needs."""

    def __init__(self, row_number: int, values: dict):
        self.row_number = row_number
        self.values = values
        refs = {}
        raw = values.get("Bot Refs", "")
        if raw:
            try:
                refs = json.loads(raw)
            except Exception:
                refs = {}
        self.refs = refs

    @property
    def status(self) -> str:
        return self.values.get("Status", "")

    @property
    def product(self) -> str:
        return self.values.get("Product", "")

    @property
    def type(self) -> str:
        return self.values.get("Type", "")

    @property
    def original_ts(self) -> str:
        return self.values.get("Slack TS", "")

    @property
    def anchor_ts(self) -> str:
        return self.refs.get("anchor_ts", "")

    @property
    def confirm_ts(self) -> str:
        return self.refs.get("confirm_ts", "")

    @property
    def owner_uid(self) -> str:
        return self.refs.get("owner_uid", "")

    @property
    def slack_channel(self) -> str:
        return self.values.get("Slack Channel", "")


class SheetStore:
    def __init__(self, sheet_id: str, tab: str, creds_file: str):
        creds = Credentials.from_service_account_file(creds_file, scopes=config.GOOGLE_SCOPES)
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(sheet_id)
        if config.SHEET_GID:
            self.ws = self.sh.get_worksheet_by_id(int(config.SHEET_GID))
        else:
            self.ws = self.sh.worksheet(tab)
        self.gid = self.ws.id
        self.sheet_id = sheet_id
        self.lock = threading.RLock()
        self.header: list[str] = []
        self.col_index: dict[str, int] = {}   # column name -> 0-based index
        self.rows: dict[int, Row] = {}          # row_number -> Row
        self.ts_index: dict[str, int] = {}      # any slack ts -> row_number
        self._ensure_header()
        self.reload()

    # -- header / schema -----------------------------------------------------
    def _ensure_header(self) -> None:
        """Locate the real header row (this sheet has intro rows above it, so it is
        not row 1) and append only the bot-managed columns if missing, never the
        human ones, which already exist. Product/Type are appended alongside the
        infra columns; historical rows just have them blank."""
        with self.lock:
            values = self.ws.get_all_values()
            self.header_row = 1
            for i, raw in enumerate(values, start=1):
                if "ID" in raw and "Date Asked" in raw:
                    self.header_row = i
                    break
            header = values[self.header_row - 1] if values else []
            missing = [c for c in config.MANAGED_COLUMNS if c not in header]
            if missing:
                start = len(header)
                rng = (f"{gspread.utils.rowcol_to_a1(self.header_row, start + 1)}:"
                       f"{gspread.utils.rowcol_to_a1(self.header_row, start + len(missing))}")
                self.ws.update(range_name=rng, values=[missing])
                header = header + missing
                log(f"added managed columns to header row {self.header_row}: {missing}")
            self.header = header
            self.col_index = {name: i for i, name in enumerate(header) if name}
            # This sheet calls the notes column "Notes / Handoff".
            self.notes_col = next(
                (h for h in header if h.strip().lower().startswith("notes")), "Notes")
            self.data_start = self.header_row + 1

    # -- read ----------------------------------------------------------------
    def reload(self) -> None:
        """Re-read the sheet and rebuild the ts index (called on boot and after
        every mutation; escalation volume is low so this stays well under quota).
        Includes ALL data rows so status reports reflect the whole board."""
        with self.lock:
            values = self.ws.get_all_values()
            self.rows.clear()
            self.ts_index.clear()
            for i, raw in enumerate(values, start=1):
                if i < self.data_start:
                    continue
                rowvals = {name: (raw[idx] if idx < len(raw) else "")
                           for name, idx in self.col_index.items()}
                if not rowvals.get("ID") and not rowvals.get("Slack TS"):
                    continue
                row = Row(i, rowvals)
                self.rows[i] = row
                for ts in (row.original_ts, row.anchor_ts, row.confirm_ts):
                    if ts:
                        self.ts_index[ts] = i

    def find_by_ts(self, ts: str) -> Row | None:
        with self.lock:
            rn = self.ts_index.get(ts)
            return self.rows.get(rn) if rn else None

    def ts_tracked(self, ts: str) -> bool:
        with self.lock:
            return ts in self.ts_index

    def _next_row(self) -> int:
        return (max(self.rows) if self.rows else self.header_row) + 1

    # -- write ---------------------------------------------------------------
    def append(self, fields: dict) -> Row:
        """Write a new row at an explicitly computed position (append_row's
        table-detection is unreliable here because of the intro rows). ID is left
        to the sheet's own formula, replicated for this row so it stays automatic."""
        with self.lock:
            r = self._next_row()
            width = len(self.header)
            rowvals = ["" for _ in range(width)]
            for name, val in fields.items():
                if name in self.col_index:
                    rowvals[self.col_index[name]] = val
            if "ID" in self.col_index:
                rowvals[self.col_index["ID"]] = (
                    f'=IF(COUNTA($B{r}:$L{r})=0;"";MAX($A${self.header_row}:A{r - 1})+1)')
            rng = (f"{gspread.utils.rowcol_to_a1(r, 1)}:"
                   f"{gspread.utils.rowcol_to_a1(r, width)}")
            # USER_ENTERED so dates become real dates and the ID formula evaluates.
            self.ws.update(range_name=rng, values=[rowvals],
                           value_input_option="USER_ENTERED")
            # Then force Slack TS to text (RAW) so Sheets does not parse the numeric-
            # looking ts as a float and round it, which would break ts -> row matching.
            ts = fields.get("Slack TS", "")
            if ts and "Slack TS" in self.col_index:
                cell = gspread.utils.rowcol_to_a1(r, self.col_index["Slack TS"] + 1)
                self.ws.update(range_name=cell, values=[[ts]], value_input_option="RAW")
            self.reload()
            return self.find_by_ts(ts)

    def delete_row(self, row_number: int) -> None:
        with self.lock:
            self.ws.delete_rows(row_number)
            self.reload()

    def set(self, row_number: int, updates: dict) -> None:
        with self.lock:
            cells = []
            for name, val in updates.items():
                if name not in self.col_index:
                    continue
                a1 = gspread.utils.rowcol_to_a1(row_number, self.col_index[name] + 1)
                cells.append({"range": a1, "values": [[val]]})
            if cells:
                self.ws.batch_update(cells, value_input_option="USER_ENTERED")
            self.reload()

    def set_refs(self, row_number: int, **kv) -> None:
        with self.lock:
            row = self.rows.get(row_number)
            refs = dict(row.refs) if row else {}
            refs.update({k: v for k, v in kv.items() if v is not None})
            self.set(row_number, {"Bot Refs": json.dumps(refs)})

    def row_link(self, row_number: int) -> str:
        return (f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"
                f"/edit#gid={self.gid}&range=A{row_number}")

    def open_rows(self) -> list[Row]:
        with self.lock:
            return [r for r in self.rows.values() if r.status in config.OPEN_STATUSES]
