# Binocan DBC tools

A small Python package for working on `binocan.dbc`: a browser view of the
database, a consistency check, a busload calculator and C regeneration. It
runs anywhere Python 3.10+ runs (cantools 43 and later need 3.10) and needs only `cantools`, which it offers to
install or update when it is missing or outdated.

Run everything from this folder (`tools/dbc_editor/`); the DBC argument
defaults to `binocan.dbc` at the repository root.

```bash
python -m binocan_dbc edit        # open the DBC in your browser
python -m binocan_dbc check       # overlaps, DLC, ranges, nodes, duplicates
python -m binocan_dbc busload     # theoretical bus load
python -m binocan_dbc generate    # regenerate src/binocan.c and src/binocan.h
python -m binocan_dbc doctor      # Python, pip and cantools versions
```

Or install it into your environment with `pip install -e tools/dbc_editor`
and use the `binocan-dbc` command instead of `python -m binocan_dbc`.

## cantools

Before any command, the tool checks the cantools installed for the Python that
runs it:

- not installed: asks to run `pip install "cantools>=43.0.2"`;
- older than 43.0.2 (the version that produced the committed C files): asks to upgrade;
- older than the latest on PyPI: offers the update (skipped silently when offline).

`--yes` answers those prompts for scripts, `--no-update-check` skips the PyPI query.

**Externally managed Python (Homebrew on macOS, Debian and Ubuntu).** These
refuse `pip install` into the system Python (PEP 668). The tool detects that,
offers to create a private virtual environment in `tools/dbc_editor/.venv`
(ignored by git), installs cantools there and re-runs your command with it.
Later runs find that environment on their own. `--venv` forces it even when
the system Python could install cantools; delete `.venv` to start over.
On Debian or Ubuntu the venv step needs `sudo apt install python3-venv` once.

## Browser editor

`edit` starts a server on `127.0.0.1` and opens a link carrying a random
session token; nothing else on the machine or in the browser can call it.

- **Messages**: edit name, frame ID, extended flag, length, senders, send type,
  cycle time and comment; add, duplicate or delete messages. The signal table
  edits name, start bit, length, byte order, signed/unsigned/float, factor,
  offset, min, max, unit, start value (raw), receivers, value choices (one
  `value = label` per line, or "use table…") and comment. The 8 x 8 bit grid
  marks MSB and LSB and outlines overlaps; a selector picks multiplexer values.
- **Bit layout**: drag a signal in the grid to move it, or drag the handle on its
  last bit to change its length. A dashed outline previews the new position (red
  where it would overlap another signal). Intel and Motorola signals follow their
  own bit order; every drag is one undoable edit.
- **Nodes**: click a matrix cell to cycle none, RX, TX; rename, comment, add
  and delete nodes (deleting a node still in use asks to remove it everywhere).
- **Value tables**: create, rename, edit and delete global tables; editing a
  table updates the signals that use it.
- **Busload**: live load of the working copy, with what-if cycle times and bitrate.
- **Check**: the consistency issues, refreshed after every edit.
- **Undo / Redo** (Ctrl+Z, Ctrl+Y), **Save** (Ctrl+S) and **Generate C**,
  which is only offered after a save because C is generated from the saved file.

Every edit is applied to a copy of the database, written out, read back and
checked before it is accepted, so the browser always shows what a save would
write. A change cantools cannot represent is refused with a message instead of
being silently dropped. Problems the Check tab reports as errors (overlapping
signals, values that do not fit, ...) block saving.

### Cycle-time tables in the docs

Saving also regenerates the tables in `README.md` and `TOO.MD` that sit between
`<!-- dbc:cycle-table ids=0x100-0x1FF -->` and `<!-- /dbc:cycle-table -->`.
Rows are the DBC messages in that ID range. Message name, ID, transmitter,
periodicity and send type come from the DBC; other columns (such as Key
Signals) keep their hand-written text, and a cell that already says the same in
its own words is left alone. After a save the tool lists the generated C files
that are now out of date and any message that no table covers.

## Busload

Uses the same frame-size formula as vxGauge's `decoder/common/busload.py`
(47 + 8 x DLC bits for 11-bit IDs, 67 + 8 x DLC for 29-bit, 20% average bit
stuffing). Messages whose `GenMsgSendType` is `SendOnRequest` are left out,
so request-only frames such as the UDS channels never count, whatever their ID.

```bash
python -m binocan_dbc busload --baud 250000 -o 0x100:20 0x300:50
```

## Saving and fidelity

Saving goes through
`binocan_dbc.model.save`, which:

- writes signals in a fixed order so repeated saves give identical text;
- restores attribute definitions cantools drops on output (`Baudrate`);
- re-reads the written file and refuses the save if it differs from the model;
- refuses if the file changed on disk since it was opened, and keeps a `.bak`.

## Tests

```bash
cd tools/dbc_editor
python -m unittest discover -s tests
```
